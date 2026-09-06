"""Catalogos do FPKGi.

O FPKGi nao guarda a lista de jogos dentro do .pkg: com populateViaWeb=true
ele busca JSONs remotos em tempo de execucao. As URLs desses JSONs estao no
config.json do console. Este modulo le esse config por FTP, baixa os mesmos
JSONs e mantem um cache local.
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import STATE_DIR, settings
from .engine import UA, filename_for
from . import ps4

CACHE = STATE_DIR / "catalog.json"
KINDS = ["games", "apps", "updates", "DLC", "demos", "homebrew",
         "emulators", "themes", "PS1", "PS2", "PSP", "PS5"]


def fpkgi_content_urls():
    """URLs de catalogo configuradas no FPKGi do console (+ extras locais)."""
    urls = {}
    try:
        raw = ps4.read_ps4_file(ps4.fpkgi_config_path())
        cfg = json.loads(raw.decode("utf-8", "replace"))
        got = cfg.get("PREFERENCES", {}).get("CONTENT_URLS", {}) or {}
        urls = {k: v for k, v in got.items() if v}
    except Exception:
        pass
    urls.update({k: v for k, v in (settings.get("extra_sources") or {}).items() if v})
    return urls


def _fetch_json(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _normalize(kind, url, meta):
    size = meta.get("size") or 0
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    return {
        "url": url,
        "kind": kind,
        "name": meta.get("name") or filename_for(url),
        "title_id": meta.get("title_id") or "",
        "region": meta.get("region") or "",
        "version": meta.get("version") or "",
        "release": meta.get("release") or "",
        "min_fw": str(meta.get("min_fw") or ""),
        "size": size,
        "cover_url": meta.get("cover_url") or "",
        "filename": filename_for(url),
    }


def refresh(progress=None):
    """Rebaixa todos os catalogos configurados e regrava o cache."""
    sources = fpkgi_content_urls()
    items, errors = [], {}
    for kind, url in sources.items():
        if progress:
            progress(f"baixando catalogo {kind}")
        try:
            data = _fetch_json(url).get("DATA") or {}
            for pkg_url, meta in data.items():
                if isinstance(meta, dict):
                    items.append(_normalize(kind, pkg_url, meta))
        except Exception as e:
            errors[kind] = f"{type(e).__name__}: {e}"

    # Se duas fontes trouxerem o mesmo pacote, a primeira vence.
    seen, unique = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique.append(it)

    payload = {
        "fetched_at": time.time(),
        "sources": sources,
        "errors": errors,
        "items": unique,
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(CACHE)
    return payload


def load(auto_refresh=True):
    """Catalogo do cache; rebaixa se estiver vencido ou ausente."""
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text())
            age_h = (time.time() - data.get("fetched_at", 0)) / 3600.0
            if not auto_refresh or age_h < settings["catalog_ttl_hours"]:
                return data
        except Exception:
            pass
    try:
        return refresh()
    except Exception:
        if CACHE.exists():
            return json.loads(CACHE.read_text())
        return {"fetched_at": 0, "sources": {}, "errors": {}, "items": []}


def search(items, q="", region="", kind="", sort="name", desc=False):
    out = items
    if q:
        terms = q.lower().split()
        def hit(it):
            hay = f"{it['name']} {it['title_id']} {it['filename']}".lower()
            return all(t in hay for t in terms)
        out = [it for it in out if hit(it)]
    if region:
        out = [it for it in out if it["region"] == region]
    if kind:
        out = [it for it in out if it["kind"] == kind]
    keys = {
        "name": lambda it: it["name"].lower(),
        "size": lambda it: it["size"],
        "region": lambda it: (it["region"], it["name"].lower()),
        "title_id": lambda it: it["title_id"],
    }
    return sorted(out, key=keys.get(sort, keys["name"]), reverse=desc)


# ---------- disponibilidade ----------
# Parte do acervo ja foi bloqueada no archive.org. O metadata continua listando
# os arquivos, entao so uma requisicao real diz a verdade. O bloqueio e por
# item (a-z), nao por arquivo, entao basta sondar um arquivo de cada item.

AVAIL = STATE_DIR / "availability.json"


def _group_by_item(items):
    from .engine import archive_item
    groups = {}
    for it in items:
        key = archive_item(it["url"])[0] or urllib.parse.urlsplit(it["url"]).netloc
        groups.setdefault(key, []).append(it)
    return groups


def availability(items=None, force=False, ttl_hours=24, workers=8):
    """{item: True/False} -- cacheado, porque cada sondagem custa uma requisicao."""
    import concurrent.futures as cf
    from .engine import resolve

    cache = {}
    if AVAIL.exists() and not force:
        try:
            raw = json.loads(AVAIL.read_text())
            if (time.time() - raw.get("checked_at", 0)) / 3600.0 < ttl_hours:
                cache = raw.get("items", {})
        except Exception:
            pass

    items = items if items is not None else load(auto_refresh=False).get("items", [])
    groups = _group_by_item(items)
    todo = [k for k in groups if k not in cache]
    if todo:
        def probe(key):
            try:
                resolve(groups[key][0]["url"], timeout=30)
                return key, True
            except Exception:
                return key, False
        with cf.ThreadPoolExecutor(workers) as ex:
            for key, ok in ex.map(probe, todo):
                cache[key] = ok
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        AVAIL.write_text(json.dumps({"checked_at": time.time(), "items": cache}))
    return cache


def annotate(items, avail):
    from .engine import archive_item
    for it in items:
        key = archive_item(it["url"])[0] or urllib.parse.urlsplit(it["url"]).netloc
        it["available"] = avail.get(key)
    return items
