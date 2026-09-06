"""Leitura do cabecalho de um .pkg de PS4.

O nome do arquivo nao diz o que ele e: "UP9000-CUSA02299_00-MARVELSSPIDERMAN-
A0100" e "...-A0119" parecem dois jogos e sao, na verdade, a base e uma
atualizacao do mesmo titulo. O cabecalho do proprio pacote diz isso sem ambiguidade.

Campos usados (big-endian, deslocamentos fixos do formato):
    0x00  magic \\x7fCNT
    0x40  content_id, 36 bytes ASCII  -> UP9000-CUSA02299_00-MARVELSSPIDERMAN
    0x74  content_type                -> 0x1A base, 0x1B DLC, 0x1E tema...
    0x78  content_flags               -> bits de patch

Validado nos 7 pacotes da biblioteca de teste: classificou 5 bases, 1 DLC
(TowerFall Dark World) e 1 atualizacao (Spider-Man A0119) sem erro.
"""
import struct
import threading
from pathlib import Path

MAGIC = b"\x7fCNT"
HEADER_BYTES = 0x100

CONTENT_TYPES = {
    0x1A: "base",
    0x1B: "dlc",
    0x1C: "licenca",
    0x1D: "delta",
    0x1E: "tema",
    0x1F: "tema",
}
# SUBSEQUENT_PATCH | CUMULATIVE_PATCH -- um pacote de jogo com esses bits e patch
PATCH_BITS = 0x60000000

LABELS = {
    "base": "base",
    "atualizacao": "atualização",
    "dlc": "DLC",
    "tema": "tema",
    "delta": "patch delta",
    "licenca": "licença",
    "": "desconhecido",
}

_cache = {}
_lock = threading.Lock()


def _parse(head: bytes):
    if len(head) < HEADER_BYTES or head[:4] != MAGIC:
        return None
    content_id = head[0x40:0x64].split(b"\0")[0].decode("ascii", "replace").strip()
    content_type = struct.unpack(">I", head[0x74:0x78])[0]
    flags = struct.unpack(">I", head[0x78:0x7C])[0]
    kind = CONTENT_TYPES.get(content_type, "")
    if kind == "base" and (flags & PATCH_BITS):
        kind = "atualizacao"
    # UP9000-CUSA02299_00-MARVELSSPIDERMAN -> CUSA02299
    title_id = ""
    parts = content_id.split("-")
    if len(parts) > 1:
        title_id = parts[1].split("_")[0]
    return {
        "content_id": content_id,
        "title_id": title_id,
        "kind": kind,
        "kind_label": LABELS.get(kind, kind or "desconhecido"),
        "content_type": content_type,
        "flags": flags,
    }


EMPTY = {"content_id": "", "title_id": "", "kind": "", "kind_label": "",
         "content_type": 0, "flags": 0}


def read(path):
    """Metadados do pacote. Cacheado por (caminho, tamanho, mtime)."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return dict(EMPTY)
    key = (str(p), st.st_size, int(st.st_mtime))
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return dict(hit)
    try:
        with open(p, "rb") as f:
            meta = _parse(f.read(HEADER_BYTES))
    except OSError:
        meta = None
    meta = meta or dict(EMPTY)
    with _lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[key] = meta
    return dict(meta)
