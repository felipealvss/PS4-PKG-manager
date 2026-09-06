"""Conversa com o PS4 pelo FTP do GoldHEN.

Host, porta e todos os caminhos vem das configuracoes -- nada fixo aqui.
O servidor do GoldHEN 2.2 anuncia SIZE, MDTM e REST STREAM, entao da pra
retomar upload interrompido e conferir o tamanho no console depois.
"""
import ftplib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import STATE_DIR, ConfigError, settings, validate_remote_path

BACKUP_DIR = STATE_DIR / "backups"


# ---------- caminhos, todos derivados das configuracoes ----------

def fpkgi_dir() -> str:
    return validate_remote_path(settings["ps4_fpkgi_dir"], "pasta do FPKGi")


def fpkgi_config_path() -> str:
    base = fpkgi_dir()
    return f"{'' if base == '/' else base}/config.json"


def destinations():
    """Destinos de transferencia configurados, com o padrao sempre presente."""
    dests = [dict(d) for d in settings["ps4_destinations"]]
    default = settings["ps4_default_destination"]
    if not any(d["path"] == default for d in dests):
        dests.insert(0, {"name": default, "path": default})
    for d in dests:
        d["default"] = d["path"] == default
    return dests


def resolve_destination(value=None) -> str:
    """Aceita o caminho ou o nome de um destino; sem argumento, devolve o padrao."""
    if not value:
        return settings["ps4_default_destination"]
    for d in destinations():
        if value in (d["path"], d["name"]):
            return d["path"]
    return validate_remote_path(value, "destino")


# ---------- conexao ----------

def connect(timeout=20):
    ftp = ftplib.FTP()
    ftp.connect(settings["ps4_host"], int(settings["ps4_ftp_port"]), timeout=timeout)
    ftp.login()  # o servidor do GoldHEN aceita login anonimo
    return ftp


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


_ping_cache = {"t": 0.0, "val": None, "key": None}
_ping_lock = threading.Lock()


def ping(timeout=5, max_age=0):
    """(online, detalhe) -- usado pelo painel de status da interface.

    Com o console desligado cada tentativa custa o timeout inteiro, e a
    interface consulta o status a cada 15s. max_age reaproveita a ultima
    resposta por alguns segundos; o preflight chama sem cache.
    """
    key = (settings["ps4_host"], settings["ps4_ftp_port"])
    if max_age:
        with _ping_lock:
            c = _ping_cache
            if c["key"] == key and c["val"] and time.time() - c["t"] < max_age:
                return c["val"]
    try:
        ftp = connect(timeout=timeout)
        try:
            res = (True, (ftp.getwelcome() or "").strip())
        finally:
            _quit(ftp)
    except Exception as e:
        res = (False, f"{type(e).__name__}: {e}")
    with _ping_lock:
        _ping_cache.update(t=time.time(), val=res, key=key)
    return res


# ---------- arquivos ----------

def read_ps4_file(path, timeout=20) -> bytes:
    ftp = connect(timeout=timeout)
    try:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write)
        return buf.getvalue()
    finally:
        _quit(ftp)


def write_ps4_file(path, data: bytes, timeout=30):
    ftp = connect(timeout=timeout)
    try:
        ftp.storbinary(f"STOR {path}", io.BytesIO(data))
    finally:
        _quit(ftp)


def listdir(path=None, timeout=20):
    ftp = connect(timeout=timeout)
    try:
        lines = []
        ftp.retrlines(f"LIST {path or fpkgi_dir()}", lines.append)
        return lines
    finally:
        _quit(ftp)


def remote_size(ftp, path):
    """Tamanho no console, ou None se o arquivo ainda nao existe."""
    try:
        return ftp.size(path)
    except (ftplib.error_perm, ftplib.error_temp):
        return None
    except Exception:
        return None


def delete_remote(path, timeout=20):
    ftp = connect(timeout=timeout)
    try:
        ftp.delete(path)
        return True
    finally:
        _quit(ftp)


def _mkdirs(ftp, path):
    cur = ""
    for part in [p for p in path.strip("/").split("/") if p]:
        cur += "/" + part
        try:
            ftp.mkd(cur)
        except ftplib.error_perm:
            pass  # ja existe


# ---------- transferencia para o destino final ----------

def transfer(local_path, destination=None, on_progress=None, cancel=None,
             timeout=60, verify=True):
    """Manda o pacote pro destino final no console.

    Retoma de onde parou se ja houver um arquivo parcial la (REST STREAM), e
    confere o tamanho no fim. Um .pkg de 40 GB nao pode recomecar do zero
    porque a conexao caiu em 90%.

    Devolve {"remote", "size", "resumed_from", "skipped", "elapsed"}.
    """
    local_path = Path(local_path)
    if not local_path.is_file():
        raise FileNotFoundError(f"nao encontrei {local_path}")
    size = local_path.stat().st_size
    remote_dir = resolve_destination(destination)
    remote = f"{'' if remote_dir == '/' else remote_dir}/{local_path.name}"

    ftp = connect(timeout=timeout)
    t0 = time.time()
    try:
        _mkdirs(ftp, remote_dir)
        already = remote_size(ftp, remote) or 0

        if already == size:
            if on_progress:
                on_progress({"sent": size, "size": size, "percent": 100.0,
                             "speed": 0, "resumed_from": already})
            return {"remote": remote, "size": size, "resumed_from": already,
                    "skipped": True, "elapsed": 0.0}

        if already > size:
            # sobra do console: um arquivo diferente com o mesmo nome
            try:
                ftp.delete(remote)
            except Exception:
                pass
            already = 0

        sent = {"n": already, "t0": time.time()}

        def cb(block):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("transferencia cancelada")
            sent["n"] += len(block)
            if on_progress:
                el = max(0.001, time.time() - sent["t0"])
                on_progress({
                    "sent": sent["n"],
                    "size": size,
                    "percent": round(sent["n"] * 100.0 / size, 2) if size else 0.0,
                    # velocidade do que esta indo agora, sem contar o que ja estava la
                    "speed": int((sent["n"] - already) / el),
                    "resumed_from": already,
                })

        with open(local_path, "rb") as f:
            if already:
                f.seek(already)
                ftp.storbinary(f"STOR {remote}", f, blocksize=1024 * 1024,
                               callback=cb, rest=already)
            else:
                ftp.storbinary(f"STOR {remote}", f, blocksize=1024 * 1024,
                               callback=cb)

        if verify:
            final = remote_size(ftp, remote)
            if final is None:
                raise IOError(f"o console nao informou o tamanho de {remote}")
            if final != size:
                raise IOError(
                    f"transferencia incompleta: {final} de {size} bytes no console"
                )
    finally:
        _quit(ftp)

    return {"remote": remote, "size": size, "resumed_from": already,
            "skipped": False, "elapsed": time.time() - t0}


# alias historico
upload = transfer


# ---------- instalador remoto do console (Remote Package Installer) ----------
#
# O console baixa o pacote direto do nosso servidor e instala, sem passar por
# /data/pkg. Isso dispensa a copia intermediaria: um jogo de 44 GB deixa de
# exigir 88 GB livres no console.
#
# Duas descobertas ao mapear a API, ambas obrigatorias para funcionar:
#   1. as respostas trazem numeros em hexadecimal (0x1CC), o que nao e JSON
#      valido -- json.loads() sozinho falha;
#   2. o cliente HTTP do console decodifica a URL e nao a recodifica ao
#      requisitar, entao nome de arquivo com espaco ou colchete quebra com
#      "Unable to set up prerequisites". As URLs precisam ser ASCII simples.

RPI_PORT = 12800
_HEXNUM = re.compile(rb'(:\s*)0x([0-9A-Fa-f]+)')


def _rpi_json(raw: bytes):
    """A API devolve 0x1CC onde JSON exige decimal."""
    fixed = _HEXNUM.sub(lambda m: m.group(1) + str(int(m.group(2), 16)).encode(), raw)
    return json.loads(fixed.decode("utf-8", "replace"))


def _rpi_post(path, payload, timeout=25):
    url = f"http://{settings['ps4_host']}:{RPI_PORT}{path}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _rpi_json(r.read())
    except urllib.error.HTTPError as e:
        try:
            return _rpi_json(e.read())
        except Exception:
            raise IOError(f"instalador remoto respondeu HTTP {e.code}")


_rpi_cache = {"t": 0.0, "val": None, "key": None}
_rpi_lock = threading.Lock()


def rpi_available(timeout=3, max_age=0):
    """(disponivel, detalhe). Nao instala nada -- so bate na porta.

    Quando o Package Installer sai de foco no console a porta continua aceitando
    TCP mas nao responde, e cada checagem custa o timeout inteiro. max_age
    reaproveita a ultima resposta; o preflight chama sem cache.
    """
    key = settings["ps4_host"]
    if max_age:
        with _rpi_lock:
            c = _rpi_cache
            if c["key"] == key and c["val"] and time.time() - c["t"] < max_age:
                return c["val"]
    try:
        d = _rpi_post("/api/is_exists", {"title_id": "CUSA00000"}, timeout=timeout)
        res = (d.get("status") == "success", "instalador remoto respondendo")
    except Exception as e:
        res = (False, "o Package Installer nao esta respondendo "
                      f"({type(e).__name__}) - deixe o app aberto no console")
    with _rpi_lock:
        _rpi_cache.update(t=time.time(), val=res, key=key)
    return res


def rpi_is_installed(title_id, timeout=10):
    try:
        d = _rpi_post("/api/is_exists", {"title_id": title_id}, timeout=timeout)
        return str(d.get("exists")).lower() == "true", int(d.get("size") or 0)
    except Exception:
        return False, 0


def rpi_install(urls, timeout=40):
    """Manda o console baixar e instalar. Devolve {task_id, title}."""
    d = _rpi_post("/api/install", {"type": "direct", "packages": list(urls)},
                  timeout=timeout)
    if d.get("status") != "success":
        raise IOError(d.get("error") or "o instalador remoto recusou o pacote")
    return {"task_id": d.get("task_id"), "title": d.get("title") or ""}


def rpi_progress(task_id, timeout=15):
    """Progresso da tarefa, normalizado."""
    d = _rpi_post("/api/get_task_progress", {"task_id": int(task_id)}, timeout=timeout)
    if d.get("status") != "success" or "error_code" in d:
        code = d.get("error_code")
        raise IOError(f"tarefa {task_id} sem progresso"
                      + (f" (codigo {code:#x})" if isinstance(code, int) else ""))
    total = int(d.get("length_total") or 0)
    sent = int(d.get("transferred_total") or 0)
    return {
        "size": total,
        "transferred": sent,
        "percent": round(sent * 100.0 / total, 2) if total else 0.0,
        "preparing": int(d.get("preparing_percent") or 0),
        "installing": int(d.get("local_copy_percent") or 0),
        "rest_sec": int(d.get("rest_sec_total") or 0),
        "error": int(d.get("error") or 0),
        # transferido por inteiro e copia local concluida
        "done": bool(total and sent >= total and int(d.get("local_copy_percent") or 0) >= 100),
    }


# ---------- o que ja esta instalado no console (somente leitura) ----------

APP_DIR = "/user/app"
APPMETA_DIR = "/user/appmeta"
# nomes que a pronunciation.xml traz para homebrew feito em Unity: nao sao titulos
_GENERIC_NAMES = {"unity", "game", "app", "application"}

_installed_cache = {"t": 0.0, "val": None, "key": None}
_installed_lock = threading.Lock()


def _origin_of(app_json):
    """De onde o pacote veio, segundo o proprio console."""
    try:
        piece = (app_json.get("pieces") or [{}])[0]
        url = piece.get("url") or ""
        size = int(piece.get("fileSize") or 0)
    except Exception:
        return "", 0
    if url.startswith(("http://", "https://")):
        return url.split("/")[2], size
    return ("local" if url else ""), size


def _name_from_pronunciation(raw):
    """A pronunciation.xml guarda o titulo falado -- serve de nome quando o
    catalogo nao conhece o Title ID."""
    try:
        txt = raw.decode("utf-8-sig", "replace")
    except Exception:
        return ""
    m = re.search(r"<text[^>]*>([^<]+)</text>", txt)
    if not m:
        return ""
    name = m.group(1).strip()
    return "" if name.lower() in _GENERIC_NAMES else name


def installed_titles(max_age=600, timeout=45):
    """Titulos instalados no console.

    Estritamente somente leitura: nada aqui escreve em /user/app. Usa uma unica
    conexao FTP para todas as leituras e guarda o resultado em cache, porque sao
    dezenas de arquivos pequenos e a interface consulta com frequencia.
    """
    key = (settings["ps4_host"], settings["ps4_ftp_port"])
    with _installed_lock:
        c = _installed_cache
        if c["key"] == key and c["val"] is not None and time.time() - c["t"] < max_age:
            return c["val"]

    ftp = connect(timeout=timeout)
    try:
        def names(path):
            lines = []
            try:
                ftp.retrlines(f"LIST {path}", lines.append)
            except Exception:
                return []
            out = []
            for ln in lines:
                parts = ln.split(None, 8)
                if len(parts) >= 9 and parts[8] not in (".", ".."):
                    out.append(parts[8])
            return out

        def read(path):
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {path}", buf.write)
                return buf.getvalue()
            except Exception:
                return None

        ids = names(APP_DIR)
        has_meta = set(names(APPMETA_DIR))
        out = []
        for tid in sorted(ids):
            raw = read(f"{APP_DIR}/{tid}/app.json")
            host, size = "", 0
            if raw:
                try:
                    host, size = _origin_of(json.loads(raw))
                except Exception:
                    pass
            name = ""
            if tid in has_meta:
                pr = read(f"{APPMETA_DIR}/{tid}/pronunciation.xml")
                if pr:
                    name = _name_from_pronunciation(pr)
            out.append({
                "title_id": tid,
                "name": name,
                "size": size,
                "origin_host": host,
                "has_icon": tid in has_meta,
            })
    finally:
        _quit(ftp)

    with _installed_lock:
        _installed_cache.update(t=time.time(), val=out, key=key)
    return out


def invalidate_installed_cache():
    with _installed_lock:
        _installed_cache.update(t=0.0, val=None)


def read_app_icon(title_id, timeout=30):
    """icon0.png de um titulo instalado, ou None."""
    tid = re.sub(r"[^A-Za-z0-9]", "", str(title_id))[:16]
    if not tid:
        return None
    ftp = connect(timeout=timeout)
    try:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {APPMETA_DIR}/{tid}/icon0.png", buf.write)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        _quit(ftp)


# ---------- integracao: fazer o FPKGi apontar pro PC ----------

def backup_config() -> Path:
    """Guarda uma copia do config.json do FPKGi antes de qualquer alteracao."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    raw = read_ps4_file(fpkgi_config_path())
    dest = BACKUP_DIR / f"FPKGi-config-{time.strftime('%Y%m%d-%H%M%S')}.json"
    dest.write_bytes(raw)
    return dest


def get_config() -> dict:
    return json.loads(read_ps4_file(fpkgi_config_path()).decode("utf-8", "replace"))


def point_fpkgi_at(base_url, kinds=("games",)):
    """Troca as CONTENT_URLS do FPKGi pelo catalogo servido por este PC.

    Sempre grava um backup antes. Devolve (backup, urls_antigas, urls_novas).
    """
    backup = backup_config()
    cfg = get_config()
    prefs = cfg.setdefault("PREFERENCES", {}).setdefault("CONTENT_URLS", {})
    before = dict(prefs)
    for kind in kinds:
        prefs[kind] = f"{base_url.rstrip('/')}/fpkgi/{kind}.json"
    write_ps4_file(fpkgi_config_path(), json.dumps(cfg, indent=2).encode())
    return backup, before, dict(prefs)


def restore_config(backup_path):
    raw = Path(backup_path).read_bytes()
    json.loads(raw.decode("utf-8", "replace"))  # nao restaura lixo
    write_ps4_file(fpkgi_config_path(), raw)
    return True


def list_backups():
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("FPKGi-config-*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
