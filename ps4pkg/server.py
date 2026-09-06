"""Servidor HTTP: interface web, API e o catalogo local que o FPKGi consome."""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import catalog, ps4
from .config import COVER_DIR, ConfigError, ensure_dirs, lan_ip, settings
from .engine import UA
from .jobs import manager

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

_cat_lock = threading.Lock()
_cat = {"data": None}


def get_catalog(force=False):
    with _cat_lock:
        if force or _cat["data"] is None:
            _cat["data"] = catalog.refresh() if force else catalog.load()
        return _cat["data"]


def library():
    """PKGs ja baixados por completo, na pasta de destino."""
    out = []
    dest = settings.dest
    if not dest.exists():
        return out
    for p in sorted(dest.iterdir()):
        if p.is_file() and p.suffix.lower() == ".pkg":
            st = p.stat()
            out.append({"filename": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return out


def partials():
    """Downloads incompletos, com quanto ja foi baixado de verdade."""
    out = []
    inc = settings.incomplete
    if not inc.exists():
        return out
    for js in sorted(inc.glob("*.json")):
        try:
            st = json.loads(js.read_text())
        except Exception:
            continue
        chunk, size = st.get("chunk", 0), st.get("size", 0)
        got = (sum(min(chunk, size - i * chunk) for i in st.get("done", []))
               + sum(int(v) for v in (st.get("partial") or {}).values()))
        out.append({"filename": js.stem, "size": size, "downloaded": got,
                    "percent": round(got * 100.0 / size, 2) if size else 0.0})
    return out


TITLE_ID_RE = re.compile(r"((?:CUSA|PPSA|NPUB|NPEB|NPJB|NPHB)\d{5})(?!\d)", re.I)


def guess_title_id(filename):
    """PKGs baixados por fora costumam trazer o Title ID no nome do arquivo."""
    m = TITLE_ID_RE.search(filename)
    return m.group(1).upper() if m else ""


def local_fpkgi_catalog(kind="games"):
    """Monta um JSON no formato do FPKGi apontando pros arquivos deste PC."""
    base = f"http://{lan_ip()}:{settings['http_port']}"
    by_name, by_tid = {}, {}
    for it in get_catalog().get("items", []):
        by_name[it["filename"]] = it
        by_tid.setdefault(it["title_id"], it)
    data = {}
    for f in library():
        meta = by_name.get(f["filename"])
        tid = guess_title_id(f["filename"])
        if meta is None:
            meta = dict(by_tid.get(tid, {}))
            if tid:
                meta["title_id"] = tid
        url = f"{base}/files/{urllib.parse.quote(f['filename'])}"
        data[url] = {
            "title_id": meta.get("title_id") or tid or "LOCAL00000",
            "region": meta.get("region") or "USA",
            "name": meta.get("name") or Path(f["filename"]).stem,
            "version": meta.get("version") or "01.00",
            "release": meta.get("release") or "01-01-2020",
            "size": f["size"],
            "min_fw": meta.get("min_fw") or "9.00",
            "cover_url": meta.get("cover_url") or "",
        }
    return {"DATA": data}


HAVE_FFMPEG = shutil.which("ffmpeg") is not None
# As capas vem do mesmo servidor lento dos jogos, entao vale ir buscando as da
# pagina em paralelo enquanto o usuario olha a lista.
COVER_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cover")
_warming = set()
_warm_guard = threading.Lock()
_cover_locks = {}
_cover_locks_guard = threading.Lock()


def cover_path(url):
    return COVER_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")


def _cover_lock(url):
    """Uma capa pedida por 10 cartoes ao mesmo tempo deve ser baixada uma vez."""
    with _cover_locks_guard:
        return _cover_locks.setdefault(url, threading.Lock())


def fetch_cover(url):
    """Baixa a capa e reduz pra miniatura. Os originais tem ~440 KB cada --
    24 deles numa pagina seriam 10 MB vindos de um servidor lento."""
    from .engine import archive_mirrors, ascii_url

    cp = cover_path(url)
    if cp.exists() and cp.stat().st_size > 0:
        return cp
    with _cover_lock(url):
        if cp.exists() and cp.stat().st_size > 0:
            return cp
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        raw = None
        for cand in [ascii_url(url)] + [ascii_url(m) for m in archive_mirrors(url)]:
            try:
                req = urllib.request.Request(cand, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                break
            except Exception:
                continue
        if not raw:
            return None
        if HAVE_FFMPEG:
            tmp_in = cp.with_suffix(".src")
            tmp_in.write_bytes(raw)
            try:
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", str(tmp_in),
                     "-vf", "scale=256:-1", "-q:v", "4", str(cp)],
                    check=True, timeout=60,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                cp.write_bytes(raw)
            finally:
                tmp_in.unlink(missing_ok=True)
        else:
            cp.write_bytes(raw)
        return cp if cp.exists() and cp.stat().st_size else None


def warm_covers(items):
    """Pre-carrega as capas da pagina atual, sem repetir o que ja esta na fila."""
    for it in items:
        u = it.get("cover_url")
        if not u or cover_path(u).exists():
            continue
        with _warm_guard:
            if u in _warming:
                continue
            _warming.add(u)

        def job(url=u):
            try:
                fetch_cover(url)
            except Exception:
                pass
            finally:
                with _warm_guard:
                    _warming.discard(url)

        COVER_POOL.submit(job)


ORIGIN_LABELS = [
    ("duskaryon", "Duskaryon"),
    ("archive.org", "archive.org"),
]


def origin_label(host):
    """Rotulo curto de onde o pacote veio, segundo o proprio console."""
    if not host:
        return "desconhecida"
    if host == "local":
        return "local / USB"
    low = host.lower()
    for needle, label in ORIGIN_LABELS:
        if needle in low:
            return label
    return host


def installed_view():
    """Titulos instalados, com o melhor nome disponivel.

    O catalogo do FPKGi tem nomes melhores que a pronunciation.xml do console
    ("Marvel's Spider-Man" contra "spider man"), entao ele vem primeiro.
    """
    items = ps4.installed_titles()
    by_tid = {}
    for it in get_catalog().get("items", []):
        by_tid.setdefault(it["title_id"], it)
    # casar por Title ID, nao por nome de arquivo: um pkg baixado por fora tem
    # outro nome, mas continua sendo a copia local do mesmo titulo
    local_tids, local_names = set(), {}
    for f in library():
        tid = guess_title_id(f["filename"])
        if tid:
            local_tids.add(tid)
            local_names.setdefault(tid, f["filename"])
    by_fname = {i["filename"]: i for i in get_catalog().get("items", [])}
    for f in library():
        meta = by_fname.get(f["filename"])
        if meta and meta["title_id"]:
            local_tids.add(meta["title_id"])
            local_names.setdefault(meta["title_id"], f["filename"])

    out = []
    for t in items:
        meta = by_tid.get(t["title_id"])
        tid = t["title_id"]
        out.append({
            **t,
            "name": (meta or {}).get("name") or t["name"] or tid,
            "origin": origin_label(t["origin_host"]),
            "in_catalog": meta is not None,
            "local_copy": tid in local_tids,
            "local_file": local_names.get(tid, ""),
        })
    out.sort(key=lambda x: (-x["size"], x["name"].lower()))
    return out


def app_icon_path(title_id):
    return COVER_DIR / f"app_{title_id}.jpg"


def fetch_app_icon(title_id):
    """icon0.png do console, reduzido. Os originais passam de 400 KB."""
    cp = app_icon_path(title_id)
    if cp.exists() and cp.stat().st_size:
        return cp
    with _cover_lock("app:" + title_id):
        if cp.exists() and cp.stat().st_size:
            return cp
        raw = ps4.read_app_icon(title_id)
        if not raw:
            return None
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        if HAVE_FFMPEG:
            tmp = cp.with_suffix(".src")
            tmp.write_bytes(raw)
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp),
                                "-vf", "scale=256:-1", "-q:v", "4", str(cp)],
                               check=True, timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                cp.write_bytes(raw)
            finally:
                tmp.unlink(missing_ok=True)
        else:
            cp.write_bytes(raw)
        return cp if cp.exists() and cp.stat().st_size else None


class Handler(BaseHTTPRequestHandler):
    server_version = "ps4pkg-manager/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # o console fica pro progresso dos downloads

    # ---------- helpers ----------

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    # ---------- roteamento ----------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._static("index.html")
            if p.startswith("/web/"):
                return self._static(p[5:])
            if p == "/api/status":
                return self._api_status()
            if p == "/api/catalog":
                return self._api_catalog(q)
            if p == "/api/queue":
                return self._json({"jobs": manager.snapshot()})
            if p == "/api/library":
                return self._json({"files": library(), "partials": partials()})
            if p == "/api/cover":
                return self._api_cover(q)
            if p == "/api/preflight":
                return self._api_preflight()
            if p == "/api/installed":
                try:
                    return self._json({"titles": installed_view()})
                except Exception as e:
                    # console desligado nao pode derrubar a aba Biblioteca
                    return self._json({"titles": [],
                                       "error": f"{type(e).__name__}: {e}"})
            if p == "/api/appicon":
                tid = (q.get("tid") or [""])[0]
                cp = fetch_app_icon(re.sub(r"[^A-Za-z0-9]", "", tid)[:16])
                if cp is None:
                    return self._send(404, b"", "text/plain")
                return self._send(200, cp.read_bytes(), "image/jpeg",
                                  {"Cache-Control": "public, max-age=604800"})
            if p == "/api/destinations":
                return self._json({"destinations": ps4.destinations(),
                                   "default": settings["ps4_default_destination"]})
            if p == "/api/ps4/backups":
                return self._json({"backups": [str(b) for b in ps4.list_backups()]})
            if p.startswith("/fpkgi/"):
                kind = Path(p).stem
                return self._json(local_fpkgi_catalog(kind))
            if p.startswith("/files/"):
                return self._serve_pkg(urllib.parse.unquote(p[7:]))
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        p, b = u.path, self._body()
        try:
            if p == "/api/refresh":
                get_catalog(force=True)
                return self._json({"ok": True, "catalog": self._cat_meta()})
            if p == "/api/queue/add":
                items = b.get("items") or ([b["item"]] if b.get("item") else [])
                added = [manager.add_download(i)[0]["id"] for i in items]
                return self._json({"ok": True, "added": added})
            if p == "/api/installed/refresh":
                ps4.invalidate_installed_cache()
                try:
                    return self._json({"ok": True, "titles": installed_view()})
                except Exception as e:
                    return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            if p == "/api/recheck":
                a = catalog.availability(get_catalog().get("items", []), force=True)
                return self._json({"ok": True, "ok_items": sum(1 for v in a.values() if v),
                                   "total_items": len(a),
                                   "blocked": [k for k, v in a.items() if not v]})
            if p == "/api/queue/clear":
                return self._json({"ok": True, "removed": manager.clear_finished()})
            m = re.fullmatch(r"/api/queue/([0-9a-f]+)/(cancel|retry|remove|up|down)", p)
            if m:
                jid, act = m.groups()
                if act == "cancel":
                    ok = manager.cancel(jid)
                elif act == "retry":
                    ok = manager.retry(jid)
                elif act == "remove":
                    ok = manager.remove(jid, delete_partial=bool(b.get("delete_partial")))
                else:
                    ok = manager.move(jid, -1 if act == "up" else 1)
                return self._json({"ok": ok})
            if p in ("/api/library/transfer", "/api/library/push"):
                job, new = manager.add_transfer(
                    b["filename"],
                    b.get("dest") or b.get("remote_dir"),
                    b.get("delete_after"),
                )
                return self._json({"ok": True, "id": job["id"], "new": new,
                                   "dest": job["remote_dir"],
                                   "dest_name": job.get("dest_name")})
            if p == "/api/library/delete":
                f = settings.dest / Path(b["filename"]).name
                if f.exists() and f.suffix.lower() == ".pkg":
                    f.unlink()
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "arquivo nao encontrado"}, 404)
            if p == "/api/settings":
                allowed = {"connections", "parallel_jobs", "chunk_mb", "dest",
                           "ps4_host", "ps4_ftp_port", "ps4_fpkgi_dir",
                           "ps4_destinations", "ps4_default_destination",
                           "delete_after_transfer", "catalog_ttl_hours",
                           "extra_sources"}
                try:
                    settings.update(**{k: v for k, v in b.items() if k in allowed})
                except ConfigError as e:
                    return self._json({"error": str(e)}, 400)
                ensure_dirs()
                return self._json({"ok": True, "settings": settings.as_dict()})
            if p == "/api/ps4/point":
                kinds = b.get("kinds") or ["games"]
                base = f"http://{lan_ip()}:{settings['http_port']}"
                backup, before, after = ps4.point_fpkgi_at(base, kinds)
                return self._json({"ok": True, "backup": str(backup),
                                   "before": before, "after": after})
            if p == "/api/ps4/restore":
                ps4.restore_config(b["backup"])
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------- handlers ----------

    def _cat_meta(self):
        c = get_catalog()
        return {"count": len(c.get("items", [])), "fetched_at": c.get("fetched_at"),
                "sources": c.get("sources", {}), "errors": c.get("errors", {})}

    def _unavailable_count(self):
        try:
            items = get_catalog().get("items", [])
            avail = catalog.availability(items)
            return sum(1 for i in catalog.annotate([dict(x) for x in items], avail)
                       if i["available"] is False)
        except Exception:
            return 0

    def _api_preflight(self):
        """Checagem ao vivo das premissas. Cada item diz o que esta errado e o
        que fazer -- e o que a aba Guia mostra em vez de prometer que funciona."""
        checks = []

        def add(cid, label, ok, detail, hint="", critical=True):
            checks.append({"id": cid, "label": label, "ok": ok, "detail": detail,
                           "hint": hint, "critical": critical})

        # 1. console
        online, welcome = ps4.ping(timeout=4)
        add("ps4", "Console acessível por FTP", online,
            f"{settings['ps4_host']}:{settings['ps4_ftp_port']}"
            + (f" — {welcome}" if online else f" — {welcome}"),
            "" if online else "Ligue o PS4, carregue o GoldHEN e ative o servidor FTP "
                             "nas opções dele. Confira o IP em Ajustes.")

        # 2. config do FPKGi, no caminho configurado
        if online:
            try:
                cfg = ps4.get_config()
                urls = cfg.get("PREFERENCES", {}).get("CONTENT_URLS", {}) or {}
                n = len([v for v in urls.values() if v])
                pointed = [k for k, v in urls.items()
                           if v and f":{settings['http_port']}/fpkgi/" in str(v)]
                add("fpkgi", "config.json do FPKGi legível", True,
                    f"{ps4.fpkgi_config_path()} — {n} fonte(s)"
                    + (f", apontando pra cá: {', '.join(pointed)}" if pointed else ""))
            except Exception as e:
                add("fpkgi", "config.json do FPKGi legível", False,
                    f"{ps4.fpkgi_config_path()} — {type(e).__name__}",
                    "Corrija 'Pasta do FPKGi no console' em Ajustes, ou preencha "
                    "extra_sources para trabalhar sem o console.")
        else:
            add("fpkgi", "config.json do FPKGi legível", None,
                "não dá pra checar com o console offline")

        # 3. destino local gravavel de verdade
        d = settings.dest
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".ps4pkg_write_test"
            probe.write_bytes(b"x")
            probe.unlink()
            add("dest", "Pasta de destino gravável", True, str(d))
        except Exception as e:
            add("dest", "Pasta de destino gravável", False,
                f"{d} — {type(e).__name__}: {e}",
                "O disco externo pode não estar montado, ou o caminho em Ajustes "
                "está errado.")

        # 4. espaco
        try:
            du = shutil.disk_usage(d)
            gb = du.free / 1024 ** 3
            add("space", "Espaço em disco", gb > 10,
                f"{gb:.1f} GB livres de {du.total / 1024 ** 3:.1f} GB",
                "" if gb > 10 else "Jogos grandes passam de 40 GB. Libere espaço "
                                   "ou aponte o destino para outro disco.",
                critical=False)
        except Exception as e:
            add("space", "Espaço em disco", None, str(e), critical=False)

        # 5. catalogo
        c = get_catalog()
        n = len(c.get("items", []))
        age = (time.time() - c.get("fetched_at", 0)) / 3600.0 if c.get("fetched_at") else None
        add("catalog", "Catálogo carregado", n > 0,
            f"{n} itens de {len(c.get('sources', {}))} fonte(s)"
            + (f", atualizado há {age:.1f}h" if age is not None else ""),
            "" if n else "Sem catálogo: o console está offline e não há extra_sources "
                         "configurado. Clique em 'Atualizar catálogo'.")
        if c.get("errors"):
            add("catalog_err", "Fontes com erro", False,
                "; ".join(f"{k}: {v}" for k, v in c["errors"].items()),
                "A fonte pode ter saído do ar. As outras continuam funcionando.",
                critical=False)

        # 6. o que o archive.org ainda serve
        try:
            av = catalog.availability(c.get("items", []))
            blocked = [k for k, v in av.items() if not v]
            nb = sum(1 for i in catalog.annotate([dict(x) for x in c.get("items", [])], av)
                     if i["available"] is False)
            add("avail", "Pacotes disponíveis na origem", not blocked,
                f"{n - nb} de {n} baixáveis"
                + (f" — bloqueados: {', '.join(blocked)}" if blocked else ""),
                "Itens bloqueados são decisão do archive.org, não tem contorno. "
                "Use a caixa 'só disponíveis' para escondê-los." if blocked else "",
                critical=False)
        except Exception as e:
            add("avail", "Pacotes disponíveis na origem", None, str(e), critical=False)

        # 7. alcance na LAN (o PS4 precisa chegar aqui)
        ip = lan_ip()
        add("lan", "Servidor visível na rede local", settings["http_host"] == "0.0.0.0",
            f"http://{ip}:{settings['http_port']} (escutando em {settings['http_host']})",
            "" if settings["http_host"] == "0.0.0.0"
                else "Com http_host em 127.0.0.1 o console não alcança este PC: "
                     "o catálogo local e a instalação pela LAN não funcionam.",
            critical=False)

        # 8. capas
        add("ffmpeg", "ffmpeg (miniaturas das capas)", HAVE_FFMPEG,
            "presente — capas reduzidas de ~440 KB para ~38 KB" if HAVE_FFMPEG
            else "ausente — as capas vêm em tamanho original e a grade fica lenta",
            "" if HAVE_FFMPEG else "Opcional. Instale com: sudo dnf install ffmpeg",
            critical=False)

        # 9. downloads pela metade
        pr = partials()
        if pr:
            add("partials", "Downloads incompletos", None,
                f"{len(pr)} em andamento/pausados — "
                + ", ".join(f"{x['filename'][:28]} ({x['percent']}%)" for x in pr[:3]),
                "Retomam do ponto exato ao serem enfileirados de novo.",
                critical=False)

        blockers = [c for c in checks if c["critical"] and c["ok"] is False]
        return self._json({
            "checks": checks,
            "ready": not blockers,
            "blockers": [c["label"] for c in blockers],
            "settings": settings.as_dict(),
            "destinations": ps4.destinations(),
            "fpkgi_config": ps4.fpkgi_config_path(),
            "lan_url": f"http://{ip}:{settings['http_port']}",
            "paths": {
                "app": str(Path(__file__).resolve().parent.parent),
                "dest": str(d),
                "incomplete": str(settings.incomplete),
                "state": str(COVER_DIR.parent),
            },
        })

    def _api_status(self):
        du = shutil.disk_usage(settings.dest) if settings.dest.exists() else None
        online, detail = ps4.ping(timeout=3, max_age=20)
        self._json({
            "settings": settings.as_dict(),
            "catalog": self._cat_meta(),
            "disk": {"free": du.free, "total": du.total} if du else None,
            "ps4": {"online": online, "detail": detail},
            "lan_url": f"http://{lan_ip()}:{settings['http_port']}",
            "library_count": len(library()),
            "destinations": ps4.destinations(),
            "unavailable": self._unavailable_count(),
        })

    def _api_catalog(self, q):
        def one(k, d=""):
            return (q.get(k) or [d])[0]
        c = get_catalog()
        items = catalog.search(
            c.get("items", []), one("q"), one("region"), one("kind"),
            one("sort", "name"), one("desc") == "1",
        )
        if one("avail") == "1":
            try:
                av = catalog.availability(c.get("items", []))
                items = [i for i in catalog.annotate([dict(x) for x in items], av)
                         if i["available"] is not False]
            except Exception:
                pass
        page = max(1, int(one("page", "1") or 1))
        per = min(200, max(1, int(one("per", "60") or 60)))
        start = (page - 1) * per
        have = {f["filename"] for f in library()}
        try:
            avail = catalog.availability(c.get("items", []))
        except Exception:
            avail = {}
        page_items = catalog.annotate([dict(i) for i in items[start:start + per]], avail)
        out = []
        for d in page_items:
            d["downloaded"] = d["filename"] in have
            out.append(d)
        warm_covers(out)
        regions = sorted({i["region"] for i in c.get("items", []) if i["region"]})
        kinds = sorted({i["kind"] for i in c.get("items", [])})
        self._json({"total": len(items), "page": page, "per": per,
                    "items": out, "regions": regions, "kinds": kinds,
                    "catalog": self._cat_meta()})

    def _api_cover(self, q):
        url = (q.get("u") or [""])[0]
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "url invalida"}, 400)
        cp = fetch_cover(url)
        if cp is None:
            return self._send(404, b"", "text/plain")
        self._send(200, cp.read_bytes(), "image/jpeg",
                   {"Cache-Control": "public, max-age=604800"})

    def _static(self, rel):
        f = (WEB_DIR / rel).resolve()
        if not str(f).startswith(str(WEB_DIR.resolve())) or not f.exists():
            return self._send(404, b"nao encontrado", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype)

    def _serve_pkg(self, name):
        """Entrega o .pkg pro FPKGi. Precisa de Range pra ele retomar."""
        f = (settings.dest / Path(name).name)
        if not f.exists() or not f.is_file():
            return self._send(404, b"nao encontrado", "text/plain")
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = RANGE_RE.fullmatch(rng.strip())
            if m:
                s, e = m.groups()
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                if start >= size:
                    return self._send(416, b"", "text/plain",
                                      {"Content-Range": f"bytes */{size}"})
                end = min(end, size - 1)
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                buf = fh.read(min(1024 * 1024, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)


def serve():
    ensure_dirs()
    mimetypes.add_type("application/json", ".json")
    host, port = settings["http_host"], int(settings["http_port"])
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{lan_ip()}:{port}"
    print(f"  interface   http://localhost:{port}")
    print(f"  na LAN      {url}   (use esta no FPKGi)")
    print(f"  destino     {settings.dest}")
    print(f"  fila        {len([j for j in manager.snapshot() if j['status'] == 'queued'])} na espera\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando (downloads em andamento retomam do ponto na proxima vez)")
