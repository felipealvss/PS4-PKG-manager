"""Motor de download segmentado com retomada.

archive.org limita a banda por conexao, entao a estrategia e abrir varias
conexoes com Range e gravar cada pedaco direto no offset certo do arquivo.
O progresso por pedaco vai pro disco, entao um download interrompido
recomeca de onde parou -- inclusive depois de reiniciar a maquina.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) ps4pkg-manager/1.0"
PKG_MAGIC = b"\x7fCNT"
READ_BLOCK = 256 * 1024


class Cancelled(Exception):
    pass


def _request(url, headers=None, timeout=60, method="GET"):
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    h.update(headers or {})
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=h, method=method), timeout=timeout
    )


def filename_for(url: str) -> str:
    """Nome de arquivo seguro derivado da URL do pacote."""
    raw = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    # NTFS nao aceita estes; o catalogo atual nao usa, mas outras fontes podem.
    for ch in '\\/:*?"<>|':
        raw = raw.replace(ch, "_")
    raw = raw.strip().strip(".") or "download.pkg"
    return raw[:200]


def ascii_url(url: str) -> str:
    """urllib exige URL ASCII; alguns pacotes tem acento no nome (Ragnarok, OKAMI)."""
    sp = urllib.parse.urlsplit(url)
    safe = "/%:@!$&'()*+,;=~-._"
    return urllib.parse.urlunsplit((
        sp.scheme, sp.netloc,
        urllib.parse.quote(sp.path, safe=safe),
        urllib.parse.quote(sp.query, safe=safe + "?&="),
        sp.fragment,
    ))


def archive_item(url):
    """(item, arquivo) se for uma URL /download/ do archive.org."""
    sp = urllib.parse.urlsplit(url)
    if not sp.netloc.endswith("archive.org"):
        return None, None
    parts = sp.path.split("/")
    if len(parts) < 4 or parts[1] != "download":
        return None, None
    return parts[2], "/".join(parts[3:])


def archive_mirrors(url, timeout=25):
    """Nos ia* do item. O redirect padrao cai em nos dn* que as vezes dao 401."""
    item, rest = archive_item(url)
    if not item:
        return []
    try:
        with _request(f"https://archive.org/metadata/{item}", timeout=timeout) as r:
            meta = json.loads(r.read().decode())
    except Exception:
        return []
    d = meta.get("dir")
    if not d:
        return []
    return [f"https://{s}{d}/{rest}" for s in (meta.get("workable_servers") or [])]


def _probe(url, timeout=30):
    """Range de 1 byte: devolve (url_final, tamanho) ou levanta excecao."""
    with _request(url, {"Range": "bytes=0-0"}, timeout=timeout) as r:
        if r.status != 206:
            raise IOError(f"servidor nao aceita Range (HTTP {r.status})")
        cr = r.headers.get("Content-Range") or ""
        tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        return r.geturl(), int(tail) if tail.isdigit() else 0


def resolve(url, timeout=45):
    """Acha um espelho que sirva o arquivo. Devolve (url_final, tamanho, True).

    Tenta a URL do catalogo e, se falhar, os nos ia* do item. Parte do acervo
    ja foi bloqueada no archive.org -- nesse caso todos os espelhos dao 401/403.
    """
    tried, errors = [], []
    for cand in [ascii_url(url)] + [ascii_url(m) for m in archive_mirrors(url, timeout)]:
        if cand in tried:
            continue
        tried.append(cand)
        host = urllib.parse.urlsplit(cand).netloc
        try:
            final, size = _probe(cand, timeout)
            if size:
                return final, size, True
            errors.append(f"{host}: sem tamanho")
        except urllib.error.HTTPError as e:
            errors.append(f"{host}: HTTP {e.code}")
        except Exception as e:
            errors.append(f"{host}: {type(e).__name__}")
    if any("401" in e or "403" in e for e in errors):
        raise IOError("pacote indisponivel no archive.org (item bloqueado). "
                      + "; ".join(errors))
    raise IOError("nenhum espelho respondeu. " + "; ".join(errors))


class Download:
    """Um download segmentado, retomavel, com N conexoes.

    O estado guarda os pedacos concluidos E quanto ja foi lido de cada pedaco
    em andamento. Sem isso, cair no meio de um pedaco jogaria fora ate 8 MB por
    conexao -- o que doi quando cada conexao anda a ~50 KB/s.
    """

    def __init__(self, url, dest_dir, incomplete_dir, *, filename=None,
                 connections=16, chunk_mb=4, expected_size=0):
        self.url = url
        self.dest_dir = Path(dest_dir)
        self.incomplete_dir = Path(incomplete_dir)
        self.filename = filename or filename_for(url)
        self.connections = max(1, int(connections))
        self.chunk = max(1, int(chunk_mb)) * 1024 * 1024
        self.size = int(expected_size or 0)
        self.final_url = url

        self.part = self.incomplete_dir / (self.filename + ".part")
        self.state_file = self.incomplete_dir / (self.filename + ".json")
        self.target = self.dest_dir / self.filename

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.done = set()
        self.partial = {}        # indice do pedaco -> bytes ja gravados
        self._pending = []
        self._failed = []
        self.downloaded = 0
        self.error = None
        self.status = "pending"
        self._recent = deque(maxlen=400)
        self._fd = None
        self._dirty = False

    # ---------- estado em disco ----------

    def _chunk_len(self, idx):
        return min(self.chunk, self.size - idx * self.chunk)

    def _load_state(self):
        if not self.state_file.exists() or not self.part.exists():
            return False
        try:
            st = json.loads(self.state_file.read_text())
        except Exception:
            return False
        if st.get("url") != self.url or st.get("chunk") != self.chunk:
            return False
        if self.size and st.get("size") != self.size:
            return False
        # um .part de tamanho errado invalida os offsets gravados
        if self.part.stat().st_size != st.get("size"):
            return False
        self.size = st["size"]
        self.done = set(st.get("done", []))
        self.partial = {int(k): int(v) for k, v in (st.get("partial") or {}).items()
                        if int(v) > 0 and int(k) not in self.done}
        return True

    def _save_state(self):
        with self._lock:
            payload = {
                "url": self.url,
                "final_url": self.final_url,
                "size": self.size,
                "chunk": self.chunk,
                "done": sorted(self.done),
                "partial": {str(k): v for k, v in self.partial.items() if v > 0},
                "saved_at": time.time(),
            }
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.state_file)
        self._dirty = False

    # ---------- metricas ----------

    def speed(self) -> float:
        """Bytes por segundo, media movel dos ultimos ~8 segundos."""
        now = time.time()
        with self._lock:
            pts = [(t, n) for t, n in self._recent if now - t <= 8.0]
        if len(pts) < 2:
            return 0.0
        span = now - pts[0][0]
        return sum(n for _, n in pts) / span if span > 0.5 else 0.0

    def progress(self):
        sp = self.speed()
        with self._lock:
            got, total = self.downloaded, self.size
        eta = int((total - got) / sp) if sp > 1024 and total > got else None
        return {
            "downloaded": got,
            "size": total,
            "percent": round(got * 100.0 / total, 2) if total else 0.0,
            "speed": int(sp),
            "eta": eta,
            "status": self.status,
            "error": self.error,
            "connections": self.connections,
        }

    def cancel(self):
        self._stop.set()

    # ---------- download ----------

    def _fetch_chunk(self, idx):
        base = idx * self.chunk
        want = self._chunk_len(idx)
        end = base + want - 1
        with self._lock:
            off = self.partial.get(idx, 0)
        if off >= want:
            with self._lock:
                self.done.add(idx)
                self.partial.pop(idx, None)
                self._dirty = True
            return

        got = 0
        with _request(self.final_url, {"Range": f"bytes={base + off}-{end}"}) as r:
            if r.status != 206:
                raise IOError(f"servidor ignorou Range (HTTP {r.status})")
            while True:
                if self._stop.is_set():
                    raise Cancelled()
                b = r.read(READ_BLOCK)
                if not b:
                    break
                os.pwrite(self._fd, b, base + off + got)
                got += len(b)
                # os bytes ja estao no disco no offset certo: registrar o
                # avanco parcial e o que salva o trabalho numa queda.
                with self._lock:
                    self.partial[idx] = off + got
                    self.downloaded += len(b)
                    self._recent.append((time.time(), len(b)))
                    self._dirty = True
        if off + got != want:
            raise IOError(f"pedaco incompleto ({off + got}/{want} bytes)")
        with self._lock:
            self.done.add(idx)
            self.partial.pop(idx, None)
            self._dirty = True

    def _worker(self):
        while not self._stop.is_set():
            with self._lock:
                if not self._pending:
                    return
                idx = self._pending.pop(0)
            for attempt in range(7):
                if self._stop.is_set():
                    return
                try:
                    self._fetch_chunk(idx)
                    break
                except Cancelled:
                    return
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
                    # backoff: o archive.org derruba conexao quando forcamos
                    for _ in range(min(2 ** attempt, 30)):
                        if self._stop.is_set():
                            return
                        time.sleep(1)
            else:
                with self._lock:
                    self._failed.append(idx)

    def run(self, on_tick=None):
        self.status = "resolving"
        self.incomplete_dir.mkdir(parents=True, exist_ok=True)
        self.dest_dir.mkdir(parents=True, exist_ok=True)

        if self.target.exists() and self.size and self.target.stat().st_size == self.size:
            self.status = "done"
            self.downloaded = self.size
            return self.target

        self.final_url, size, ranges = resolve(self.url)
        if size:
            self.size = size
        if not self.size:
            raise IOError("servidor nao informou o tamanho do arquivo")
        if not ranges:
            raise IOError("servidor nao aceita download segmentado (sem Accept-Ranges)")

        if not self._load_state():
            self.done, self.partial = set(), {}
        nchunks = (self.size + self.chunk - 1) // self.chunk
        self.partial = {k: v for k, v in self.partial.items() if k < nchunks}
        # os pedacos ja comecados vao primeiro, pra fechar o que esta pela metade
        rest = [i for i in range(nchunks) if i not in self.done]
        self._pending = sorted(rest, key=lambda i: (i not in self.partial, i))
        self.downloaded = (sum(self._chunk_len(i) for i in self.done)
                           + sum(self.partial.values()))

        self._fd = os.open(self.part, os.O_RDWR | os.O_CREAT)
        try:
            if os.fstat(self._fd).st_size != self.size:
                os.ftruncate(self._fd, self.size)
            self._save_state()
            self.status = "downloading"

            nthreads = min(self.connections, max(1, len(self._pending)))
            threads = [threading.Thread(target=self._worker, daemon=True, name=f"dl{i}")
                       for i in range(nthreads)]
            for t in threads:
                t.start()

            last_save = time.time()
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
                if on_tick:
                    on_tick(self.progress())
                if self._dirty and time.time() - last_save > 3:
                    self._save_state()
                    last_save = time.time()
            for t in threads:
                t.join()
            self._save_state()
        finally:
            os.close(self._fd)
            self._fd = None

        if self._stop.is_set():
            self.status = "cancelled"
            raise Cancelled()
        if self._failed or len(self.done) != nchunks:
            self.status = "error"
            raise IOError(
                f"{nchunks - len(self.done)} pedaco(s) falharam apos varias tentativas. "
                f"Ultimo erro: {self.error or 'desconhecido'}"
            )

        self._verify()
        os.replace(self.part, self.target)
        self.state_file.unlink(missing_ok=True)
        self.status = "done"
        return self.target

    def _verify(self):
        if self.part.stat().st_size != self.size:
            raise IOError("tamanho final diferente do esperado")
        if self.filename.lower().endswith(".pkg"):
            with open(self.part, "rb") as f:
                magic = f.read(4)
            if magic != PKG_MAGIC:
                raise IOError(
                    f"nao parece um PKG de PS4 (assinatura {magic!r}, esperado {PKG_MAGIC!r})"
                )
