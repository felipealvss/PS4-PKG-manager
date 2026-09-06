#!/usr/bin/env python3
"""ps4pkg -- gerenciador de downloads de PKG de PS4 (catalogo do FPKGi)."""
import argparse
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ps4pkg import catalog, ps4  # noqa: E402
from ps4pkg.config import ensure_dirs, lan_ip, settings  # noqa: E402


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def cmd_serve(a):
    from ps4pkg.server import serve
    print("\nps4pkg-manager")
    if a.open:
        threading_open(f"http://localhost:{settings['http_port']}")
    serve()


def threading_open(url):
    import threading
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()


def cmd_refresh(a):
    data = catalog.refresh(progress=lambda m: print(f"  {m}"))
    print(f"\n{len(data['items'])} itens de {len(data['sources'])} fonte(s)")
    for k, v in data.get("errors", {}).items():
        print(f"  ! {k}: {v}")


def cmd_search(a):
    data = catalog.load()
    hits = catalog.search(data["items"], " ".join(a.query), a.region, a.kind, a.sort)
    for it in hits[: a.limit]:
        print(f"{it['title_id']:<12} {it['region']:<4} {human(it['size']):>9}  {it['name']}")
    print(f"\n{len(hits)} resultado(s)")


def cmd_get(a):
    from ps4pkg.jobs import manager
    data = catalog.load()
    hits = catalog.search(data["items"], " ".join(a.query))
    if not hits:
        print("nada encontrado")
        return 1
    if len(hits) > 1 and not a.all:
        print("varios resultados -- refine a busca ou use --all:")
        for it in hits[:15]:
            print(f"  {it['title_id']:<12} {human(it['size']):>9}  {it['name']}")
        return 1
    for it in hits:
        job, new = manager.add_download(it)
        print(("+ " if new else "= ") + f"{it['name']} ({human(it['size'])})")
    return watch_queue()


def cmd_queue(a):
    return watch_queue(once=not a.watch)


def watch_queue(once=False):
    from ps4pkg.jobs import manager
    try:
        while True:
            jobs = [j for j in manager.snapshot()
                    if j["status"] in ("queued", "downloading", "transferring")]
            if not jobs and once:
                pass
            lines = []
            for j in manager.snapshot():
                p = j["progress"]
                bar = ""
                if j["status"] in ("downloading", "transferring"):
                    fill = int(p["percent"] / 5)
                    bar = f" [{'#' * fill}{'.' * (20 - fill)}] {p['percent']:5.1f}% {human(p['speed'])}/s"
                lines.append(f"  {j['status']:<12}{j['name'][:44]:<46}{human(j['size']):>10}{bar}")
            print("\n".join(lines) or "  (fila vazia)")
            if once or not jobs:
                return 0
            time.sleep(2)
            print(f"\033[{len(lines) + 1}A\033[J", end="")
    except KeyboardInterrupt:
        print("\n(a fila continua rodando se o servidor estiver ativo)")
        return 0


def cmd_library(a):
    from ps4pkg.server import library, partials
    for f in library():
        print(f"  {human(f['size']):>10}  {f['filename']}")
    for p in partials():
        print(f"  {p['percent']:5.1f}% incompleto  {p['filename']}")


def cmd_push(a):
    from ps4pkg.jobs import manager
    job, _ = manager.add_transfer(a.filename, a.dest, a.delete_after)
    print(f"-> {job['dest_name']} ({job['remote_dir']})")
    return watch_queue()


def cmd_dests(a):
    for d in ps4.destinations():
        print(f"  {'*' if d['default'] else ' '} {d['name']:<32} {d['path']}")
    print("\n  (* = padrao)  pasta do FPKGi: " + ps4.fpkgi_dir())


def cmd_link(a):
    base = f"http://{lan_ip()}:{settings['http_port']}"
    backup, before, after = ps4.point_fpkgi_at(base, a.kinds)
    print(f"backup do config do FPKGi: {backup}")
    for k in a.kinds:
        print(f"  {k}: {before.get(k)}  ->  {after.get(k)}")
    print("\nReinicie o FPKGi no PS4. O servidor precisa estar rodando (ps4pkg serve).")


def cmd_unlink(a):
    backups = ps4.list_backups()
    if not backups:
        print("nenhum backup encontrado")
        return 1
    ps4.restore_config(backups[0])
    print(f"config do FPKGi restaurado de {backups[0].name}")


def cmd_status(a):
    import shutil
    online, detail = ps4.ping()
    data = catalog.load(auto_refresh=False)
    du = shutil.disk_usage(settings.dest) if settings.dest.exists() else None
    print(f"  destino    {settings.dest}")
    if du:
        print(f"  livre      {human(du.free)} de {human(du.total)}")
    print(f"  catalogo   {len(data.get('items', []))} itens, "
          f"{len(data.get('sources', {}))} fonte(s)")
    print(f"  PS4        {'online' if online else 'offline'} "
          f"({settings['ps4_host']}:{settings['ps4_ftp_port']}) {detail}")
    print(f"  FPKGi      {ps4.fpkgi_config_path()}")
    print(f"  destino    {settings['ps4_default_destination']} (padrao de "
          f"{len(ps4.destinations())} configurados)")
    print(f"  este PC    http://{lan_ip()}:{settings['http_port']}")


def main():
    ensure_dirs()
    ap = argparse.ArgumentParser(prog="ps4pkg", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="sobe a interface web (padrao)")
    s.add_argument("--open", action="store_true", help="abre o navegador")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("refresh", help="rebaixa os catalogos")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("search", help="busca no catalogo")
    s.add_argument("query", nargs="+")
    s.add_argument("--region", default="")
    s.add_argument("--kind", default="")
    s.add_argument("--sort", default="name")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("get", help="enfileira e baixa")
    s.add_argument("query", nargs="+")
    s.add_argument("--all", action="store_true", help="pega todos os resultados")
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("queue", help="mostra a fila")
    s.add_argument("--watch", action="store_true")
    s.set_defaults(func=cmd_queue)

    s = sub.add_parser("library", help="lista o que ja foi baixado")
    s.set_defaults(func=cmd_library)

    s = sub.add_parser("push", help="transfere um pkg pro destino final no PS4")
    s.add_argument("filename")
    s.add_argument("--dest", default=None,
                   help="nome ou caminho do destino (padrao: o configurado)")
    s.add_argument("--delete-after", action="store_true", default=None,
                   help="apaga a copia local apos o console confirmar o tamanho")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("dests", help="lista os destinos configurados no console")
    s.set_defaults(func=cmd_dests)

    s = sub.add_parser("link", help="aponta o FPKGi pro catalogo deste PC")
    s.add_argument("--kinds", nargs="+", default=["games"])
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("unlink", help="restaura o config original do FPKGi")
    s.set_defaults(func=cmd_unlink)

    s = sub.add_parser("status", help="resumo do ambiente")
    s.set_defaults(func=cmd_status)

    a = ap.parse_args()
    if not a.cmd:
        a = ap.parse_args(["serve"])
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
