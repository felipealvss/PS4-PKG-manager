"""Configuracao central do ps4pkg-manager.

Tudo que aponta pra algum lugar -- pasta local de download, caminhos dentro do
FTP do console, onde o FPKGi guarda o config -- e variavel. Nada de caminho
fixo no codigo: o firmware, o GoldHEN e o proprio FPKGi mudam de lugar com o
tempo, e nao da pra depender de editar fonte quando isso acontecer.
"""
import json
import os
import socket
from pathlib import Path

APP = "ps4pkg-manager"
STATE_DIR = Path(os.environ.get("PS4PKG_STATE", Path.home() / ".local/share/ps4pkg"))
COVER_DIR = STATE_DIR / "covers"
CONFIG_FILE = STATE_DIR / "settings.json"

# Estes sao apenas os valores de partida. O que voce ajustar na interface fica
# em ~/.local/share/ps4pkg/settings.json e tem precedencia sobre tudo isto.
DEFAULTS = {
    # onde os downloads caem, neste computador
    "dest": str(Path.home() / "Downloads" / "ps4-pkg"),

    # --- console, via FTP do GoldHEN ---
    # troque pelo IP do seu PS4 (Ajustes > IP do PS4)
    "ps4_host": "192.168.1.2",
    "ps4_ftp_port": 2121,
    # base do FPKGi no console; o config.json sai daqui
    "ps4_fpkgi_dir": "/data/FPKGi",
    # destinos possiveis pra transferencia final
    "ps4_destinations": [
        {"name": "Pasta de PKG do console", "path": "/data/pkg"},
        {"name": "FPKGi - fila de downloads", "path": "/data/FPKGi/Downloads"},
    ],
    "ps4_default_destination": "/data/pkg",
    # apaga a copia local depois de transferir e conferir o tamanho no console
    "delete_after_transfer": False,

    # --- download ---
    # archive.org limita ~3 Mbps por IP; mais de 16 conexoes nao acelera.
    "connections": 16,
    # um de cada vez: downloads simultaneos dividem o mesmo teto.
    "parallel_jobs": 1,
    "chunk_mb": 4,

    # --- servidor local ---
    "http_host": "0.0.0.0",
    "http_port": 8420,

    "catalog_ttl_hours": 12,
    # fontes de catalogo extras, alem das que o FPKGi ja tem configuradas
    "extra_sources": {},
}


class ConfigError(ValueError):
    """Valor de configuracao invalido, com mensagem pra mostrar ao usuario."""


def validate_local_dir(value) -> str:
    """Pasta local de destino. Recusa URL: o motor grava com pwrite/ftruncate,
    que so existem em sistema de arquivos local."""
    v = str(value or "").strip()
    if not v:
        raise ConfigError("a pasta de destino nao pode ficar vazia")
    if "://" in v:
        raise ConfigError(
            f"'{v}' e uma URL, nao uma pasta local. Downloads precisam de disco "
            "local (escrita em offset arbitrario). Pra mandar pro console, use a "
            "transferencia da aba Biblioteca."
        )
    if not v.startswith("/"):
        raise ConfigError(f"informe o caminho completo, comecando com '/': '{v}'")
    return str(Path(v))


def validate_remote_path(value, label="caminho no console") -> str:
    """Caminho dentro do FTP do console."""
    v = str(value or "").strip()
    if not v:
        raise ConfigError(f"{label} nao pode ficar vazio")
    if "://" in v:
        raise ConfigError(f"{label}: informe so o caminho, sem ftp://  ('{v}')")
    if not v.startswith("/"):
        raise ConfigError(f"{label} deve comecar com '/': '{v}'")
    return "/" + v.strip("/") if v != "/" else "/"


def normalize_destinations(value):
    """Aceita lista de {name, path}, dict {nome: caminho} ou texto 'nome = /caminho'."""
    items = []
    if isinstance(value, str):
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, path = line.partition("=")
            if not path:
                name, path = line, line
            items.append({"name": name.strip(), "path": path.strip()})
    elif isinstance(value, dict):
        items = [{"name": k, "path": v} for k, v in value.items()]
    elif isinstance(value, list):
        for d in value:
            if isinstance(d, dict) and d.get("path"):
                items.append({"name": (d.get("name") or d["path"]).strip(),
                              "path": d["path"]})
            elif isinstance(d, str):
                items.append({"name": d, "path": d})
    else:
        raise ConfigError("lista de destinos em formato desconhecido")

    out, seen = [], set()
    for d in items:
        path = validate_remote_path(d["path"], "destino")
        if path in seen:
            continue
        seen.add(path)
        out.append({"name": (d["name"] or path).strip(), "path": path})
    if not out:
        raise ConfigError("e preciso ao menos um destino no console")
    return out


VALIDATORS = {
    "dest": validate_local_dir,
    "ps4_fpkgi_dir": lambda v: validate_remote_path(v, "pasta do FPKGi"),
    "ps4_default_destination": lambda v: validate_remote_path(v, "destino padrao"),
    "ps4_destinations": normalize_destinations,
    "connections": lambda v: max(1, min(64, int(v))),
    "parallel_jobs": lambda v: max(1, min(8, int(v))),
    "chunk_mb": lambda v: max(1, min(64, int(v))),
    "ps4_ftp_port": lambda v: int(v),
    "http_port": lambda v: int(v),
    "catalog_ttl_hours": lambda v: max(0, float(v)),
    "delete_after_transfer": lambda v: bool(v),
}


def _migrate(cfg):
    """Versoes antigas tinham um unico 'ps4_ftp_upload_dir'."""
    legacy = cfg.pop("ps4_ftp_upload_dir", None)
    if legacy:
        try:
            path = validate_remote_path(legacy)
        except ConfigError:
            return cfg
        cfg.setdefault("ps4_default_destination", path)
        dests = cfg.get("ps4_destinations") or []
        if not any(d.get("path") == path for d in dests):
            dests.append({"name": "Destino anterior", "path": path})
            cfg["ps4_destinations"] = dests
    return cfg


def _load():
    cfg = json.loads(json.dumps(DEFAULTS))  # copia profunda
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
    cfg = _migrate(cfg)
    # um settings.json editado a mao nao pode derrubar o programa
    for key, fn in VALIDATORS.items():
        if key in cfg:
            try:
                cfg[key] = fn(cfg[key])
            except Exception:
                cfg[key] = DEFAULTS[key]
    return cfg


def _save(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    os.replace(tmp, CONFIG_FILE)


class Settings:
    """Wrapper que valida antes de gravar. So persiste se tudo passar."""

    def __init__(self):
        self._d = _load()

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def update(self, **kw):
        """Valida tudo primeiro; um valor ruim nao chega ao disco."""
        clean = {}
        for k, v in kw.items():
            fn = VALIDATORS.get(k)
            clean[k] = fn(v) if fn else v
        self._d.update(clean)
        _save(self._d)
        return clean

    def as_dict(self):
        return json.loads(json.dumps(self._d))

    @property
    def dest(self) -> Path:
        return Path(self._d["dest"])

    @property
    def incomplete(self) -> Path:
        return self.dest / ".incomplete"


settings = Settings()


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    settings.dest.mkdir(parents=True, exist_ok=True)
    settings.incomplete.mkdir(parents=True, exist_ok=True)


def lan_ip(peer=None) -> str:
    """IP desta maquina na LAN, visto a partir do PS4."""
    peer = peer or settings["ps4_host"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()
