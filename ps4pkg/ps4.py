"""Conversa com o PS4 pelo FTP do GoldHEN.

Host, porta e todos os caminhos vem das configuracoes -- nada fixo aqui.
O servidor do GoldHEN 2.2 anuncia SIZE, MDTM e REST STREAM, entao da pra
retomar upload interrompido e conferir o tamanho no console depois.
"""
import ftplib
import io
import json
import time
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


def ping(timeout=5):
    """(online, detalhe) -- usado pelo painel de status da interface."""
    try:
        ftp = connect(timeout=timeout)
        try:
            return True, (ftp.getwelcome() or "").strip()
        finally:
            _quit(ftp)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


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
