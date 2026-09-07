"""Interceptação de downloads do Duskaryon pela fila BGFT do console.

Quando voce inicia um download no app Duskaryon, o console cria uma tarefa em
/user/bgft/task/<id>/ cujo d0.pdb (fallback d1.pdb) traz, em texto embutido:

  URL de origem   http://dl.duskaryon.com/<hash>?.pkg   (as vezes +1 byte de lixo)
  content_id      UP0700-CUSA09072_00-DBFDLCCHARA00005| (com '|' no fim)
  caminho destino .../app.pkg | patch.pkg | ac.pkg      -> base | update | dlc

Classificacao e agrupamento saem so disso -- nada e pedido ao Duskaryon. Baixar
exige a URL, e a autorizacao esta atrelada a sessao ativa do console (validado:
URL fresca baixa, URL de sessao antiga da 403).
"""
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request

from .config import STATE_DIR, settings

DL_HOST = "dl.duskaryon.com"
PS4_UA = "PS4/11.520 libhttp/11.520 (PlayStation 4)"
TASK_DIR = "/user/bgft/task"
CAPTURES = STATE_DIR / "duskaryon_captures.json"

# a URL real termina em .pkg; o [0-9a-f]{16,} para antes do byte de lixo
# cada peca do download vem num JSON limpo embutido no d0.pdb
_MANIFEST_RE = re.compile(
    r'\{"numberOfSplitFiles":\d+,"packageDigest":"[0-9A-Fa-f]+","pieces":\[[^\]]*\]\}')
_CID_RE = re.compile(r"([A-Z]{2}\d{4}-CUSA\d+_00-[A-Za-z0-9]+)")
_CUSA_RE = re.compile(r"CUSA\d{5}")
# caminho de destino -> tipo (app.pkg=base, patch.pkg=update, ac.pkg=dlc)
_DEST_RE = re.compile(r"/user/[ -~]*?/(app|patch|ac)\.pkg")
_DEST_KIND = {"app": "base", "patch": "update", "ac": "dlc"}
KIND_LABEL = {"base": "base", "update": "atualização", "dlc": "DLC"}
# fallback: URL solta (com possivel byte de lixo no fim, cortado no .pkg)
_URL_RE = re.compile(r"https?://dl\.duskaryon\.com/[0-9a-fA-F]{16,}\?\.pkg")


def _clean_name(text, content_id):
    """Nome legivel do titulo dentro do d0.pdb (ex.: 'Marvels Spider-Man')."""
    m = re.search(r"\}([ -~]{3,40}?)(?:https?://|/user/|\x00)", text)
    if m:
        return m.group(1).strip().rstrip("ji").strip()
    return ""


_FILESIZE_RE = re.compile(rb'"fileSize":\s*(\d+)')
_DIGEST_RE = re.compile(r'"packageDigest":"([0-9A-Fa-f]+)"')


def _clean_name(text):
    """Nome legivel do titulo dentro do d0.pdb (ex.: 'Marvels Spider-Man').

    E uma string curta, com espaco ou letra minuscula, que nao e URL, caminho,
    content_id nem JSON. Pega a mais parecida com nome.
    """
    for m in re.finditer(r"[ -~]{3,48}", text):
        s = m.group().strip().rstrip("ji|").strip()
        if (2 < len(s) <= 46 and (" " in s or not s.isupper())
                and not any(b in s for b in ("http", "/", "{", "\\", ".pkg",
                                             ".png", "cover", "duskaryon"))
                and not re.match(r"^[A-Z]{2}\d{4}-", s)
                and not re.fullmatch(r"[0-9A-Fa-f]{16,}", s)
                and any(c.isalpha() for c in s)):
            return s
    return ""


def parse_task(blob: bytes):
    """Peças de download de uma tarefa bgft. None se nao houver jogo Duskaryon.

    Associacao por posicao (robusta a JSON truncado): cada URL de jogo recebe o
    tipo do caminho de destino mais proximo (app.pkg=base, patch.pkg=update,
    ac.pkg=dlc) e o tamanho do "fileSize" mais proximo. Nao depende do manifesto
    JSON estar inteiro -- alguns vem cortados no d0.pdb.
    """
    text = blob.decode("latin1", "replace")

    dests = [(m.start(), _DEST_KIND[m.group(1)]) for m in _DEST_RE.finditer(text)]
    sizes = [(m.start(), int(m.group(1))) for m in _FILESIZE_RE.finditer(blob)]
    digests = [(m.start(), m.group(1)) for m in _DIGEST_RE.finditer(text)]

    def nearest(pos, items, default=None):
        return min(items, key=lambda it: abs(it[0] - pos))[1] if items else default

    pieces, seen = [], set()
    for m in _URL_RE.finditer(text):
        url = m.group()
        if url in seen:
            continue
        seen.add(url)
        pos = m.start()
        kind = nearest(pos, dests, "")
        pieces.append({
            "url": url,
            "size": nearest(pos, sizes, 0) or 0,
            "digest": nearest(pos, digests, "") or "",
            "kind": kind,
            "kind_label": KIND_LABEL.get(kind, kind or "?"),
        })
    if not pieces:
        return None

    cid_m = _CID_RE.search(text)
    content_id = cid_m.group(1) if cid_m else ""
    cusa_m = _CUSA_RE.search(content_id) or _CUSA_RE.search(text)
    cusa = cusa_m.group(0) if cusa_m else ""
    return {
        "content_id": content_id,
        "cusa": cusa,
        "name": _clean_name(text),
        "pieces": pieces,
    }


def read_task(ftp, task_id):
    """Le d0.pdb (ou d1.pdb) de uma tarefa e devolve a info, ou None."""
    for fn in ("d0.pdb", "d1.pdb"):
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {TASK_DIR}/{task_id}/{fn}", buf.write)
        except Exception:
            continue
        info = parse_task(buf.getvalue())
        if info:
            info["task_id"] = task_id
            return info
    return None


def list_tasks(ftp):
    """{task_id: assinatura} de /user/bgft/task; a assinatura (mtime) muda quando a tarefa muda."""
    lines = []
    try:
        ftp.retrlines(f"LIST {TASK_DIR}", lines.append)
    except Exception:
        return {}
    out = {}
    for l in lines:
        p = l.split(None, 8)
        if len(p) >= 9 and p[8] not in (".", ".."):
            out[p[8]] = " ".join(p[5:8])
    return out


def probe_size(url, timeout=20):
    """Tamanho pelo Content-Range (Range 0-0, UA de PS4). 0 se nao autorizado."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": PS4_UA, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range") or ""
            tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
            return int(tail) if tail.isdigit() else 0
    except Exception:
        return 0


# o pkgmeta usa 'atualizacao'; o modulo Duskaryon usa 'update'
_PKGMETA_KIND = {"base": "base", "atualizacao": "update", "dlc": "dlc",
                 "tema": "tema", "delta": "update", "licenca": "dlc"}


def probe_kind(url, timeout=20):
    """Tipo pelo cabecalho do PKG (256 bytes, UA de PS4). '' se nao autorizado."""
    from . import pkgmeta
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": PS4_UA, "Range": "bytes=0-255"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            meta = pkgmeta._parse(r.read(256).ljust(0x100, b"\0"))
        k = (meta or {}).get("kind", "")
        return _PKGMETA_KIND.get(k, k)
    except Exception:
        return ""


# ---------- armazenamento de capturas + coletor ----------

class Captures:
    """Capturas persistidas, deduplicadas por URL. Cada uma espera decisao."""

    def __init__(self):
        self._lock = threading.RLock()
        self._d = {}
        if CAPTURES.exists():
            try:
                self._d = json.loads(CAPTURES.read_text())
            except Exception:
                self._d = {}

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CAPTURES.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._d, ensure_ascii=False))
        tmp.replace(CAPTURES)

    def add(self, piece):
        """piece: dict com url, cusa, kind, kind_label, size, content_id, name, task_id."""
        with self._lock:
            url = piece["url"]
            old = self._d.get(url)
            if old:
                # ja capturado: so atualiza tamanho se estava faltando
                if not old.get("size") and piece.get("size"):
                    old["size"] = piece["size"]
                    self._save()
                return False
            piece = dict(piece)
            piece.setdefault("status", "captured")
            piece["captured_at"] = time.time()
            self._d[url] = piece
            self._save()
            return True

    def set_status(self, url, status):
        with self._lock:
            if url in self._d:
                self._d[url]["status"] = status
                self._save()
                return True
        return False

    def remove(self, url):
        with self._lock:
            if self._d.pop(url, None) is not None:
                self._save()
                return True
        return False

    def clear(self, keep_active=True):
        with self._lock:
            if keep_active:
                self._d = {u: c for u, c in self._d.items()
                           if c.get("status") in ("queued", "downloading")}
            else:
                self._d = {}
            self._save()

    def all(self):
        with self._lock:
            return [dict(c) for c in self._d.values()]

    def get(self, url):
        with self._lock:
            c = self._d.get(url)
            return dict(c) if c else None

    def reclassify(self):
        """Re-sonda tipo e tamanho das capturas que ficaram sem (manifesto
        truncado ou probe falho na captura). Usa o cabeçalho do PKG pela URL --
        precisa da sessão do console ativa. Best-effort."""
        with self._lock:
            targets = [dict(c) for c in self._d.values()
                       if c.get("kind") in ("", "?", None) or not c.get("size")]
        fixed = 0
        for c in targets:
            url = c["url"]
            new = {}
            if c.get("kind") in ("", "?", None):
                k = probe_kind(url)
                if k:
                    new["kind"] = k
                    new["kind_label"] = KIND_LABEL.get(k, k)
            if not c.get("size"):
                sz = probe_size(url)
                if sz:
                    new["size"] = sz
            if new:
                with self._lock:
                    if url in self._d:
                        self._d[url].update(new)
                        fixed += 1
        if fixed:
            with self._lock:
                self._save()
        return fixed


class Sniffer:
    """Vigia /user/bgft/task e captura downloads Duskaryon novos -- so quando ligado.

    Nao baixa nada: apenas registra o que apareceu, para o usuario decidir. A
    linha de base e tirada no momento em que a interceptacao e ligada, entao so
    downloads iniciados a partir dai sao capturados.
    """

    def __init__(self, captures, interval=5):
        self.captures = captures
        self.interval = interval
        self._stop = threading.Event()
        self._seen = None            # None = precisa refazer a linha de base
        self._last_error = None
        self._active = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="dk-sniff")
        self._thread.start()

    def status(self):
        return {"on": bool(settings.get("duskaryon_sniff")),
                "active": self._active, "error": self._last_error}

    def _loop(self):
        from . import ps4
        while not self._stop.is_set():
            if not settings.get("duskaryon_sniff"):
                self._seen = None
                self._active = False
                self._stop.wait(2.0)
                continue
            try:
                ftp = ps4.connect(timeout=15)
                try:
                    tasks = list_tasks(ftp)
                    if self._seen is None:
                        self._seen = tasks           # linha de base: nao captura o que ja existia
                    else:
                        for tid, sig in tasks.items():
                            if self._seen.get(tid) != sig:
                                self._capture(ftp, tid)
                        self._seen = tasks
                    self._active = True
                    self._last_error = None
                finally:
                    try:
                        ftp.quit()
                    except Exception:
                        pass
            except Exception as e:
                self._active = False
                self._last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)

    def _capture(self, ftp, task_id):
        info = read_task(ftp, task_id)
        if not info:
            return
        for pc in info["pieces"]:
            size = pc.get("size") or probe_size(pc["url"])
            kind, kind_label = pc["kind"], pc["kind_label"]
            if not kind:                       # ultimo recurso: le o cabecalho do PKG
                kind = probe_kind(pc["url"])
                kind_label = KIND_LABEL.get(kind, "?")
            self.captures.add({
                "url": pc["url"],
                "cusa": info["cusa"],
                "content_id": info["content_id"],
                "name": info.get("name", ""),
                "kind": kind,
                "kind_label": kind_label,
                "size": size,
                "digest": pc.get("digest", ""),
                "task_id": task_id,
            })

    def stop(self):
        self._stop.set()


captures = Captures()
sniffer = Sniffer(captures)
