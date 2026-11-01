"""Command line: rummage [serve|index|search|folders|add|remove|stats]."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from . import config, db


def _cmd_serve(a):
    from .server import serve

    serve(port=a.port, open_browser=not a.no_browser, index_on_start=not a.no_index)


def _cmd_index(a):
    from .indexer import Indexer

    Indexer().run(verbose=True)


def _cmd_search(a):
    from .search import Searcher

    res = Searcher().search(" ".join(a.query), limit=a.limit, wait_for_model=True)
    chips = ", ".join(c["label"] for c in res["chips"])
    print(f"{len(res['results'])} results in {res['took_ms']} ms"
          + (f"  [filters: {chips}]" if chips else "")
          + ("" if res["semantic"] or not res["terms"] else "  (keyword only: no embeddings yet)"))
    for r in res["results"]:
        when = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d")
        print(f"\n\033[1m{r['name']}\033[0m  {r['kind']} · {when} · {r['match']}\n  {r['path']}")
        if r["snippet"]:
            print("  " + r["snippet"][:200])


def _cmd_folders(a):
    for r in config.load_roots():
        print(f"{r.path}  (OCR {'on' if r.ocr else 'off'})")


def _cmd_add(a):
    config.add_root(a.path, ocr=not a.no_ocr)
    _cmd_folders(a)
    print("Run `rummage index` (or open the app) to index it.")


def _cmd_remove(a):
    config.remove_root(a.path)
    _cmd_folders(a)


def _cmd_stats(a):
    conn = db.connect()
    s = db.stats(conn)
    print(f"{s['files']} files, {s['chunks']} chunks, {s['embedded']} embedded")
    for k, n in sorted(s["kinds"].items(), key=lambda x: -x[1]):
        print(f"  {k:6} {n}")
    print(f"index: {config.DB_PATH}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rummage", description="Offline search for everything on your Mac.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="open the search app (default)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--no-index", action="store_true", help="don't rescan on start")
    s.set_defaults(fn=_cmd_serve)
    sub.add_parser("index", help="scan folders and update the index").set_defaults(fn=_cmd_index)
    s = sub.add_parser("search", help="search from the terminal")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.set_defaults(fn=_cmd_search)
    sub.add_parser("folders", help="list indexed folders").set_defaults(fn=_cmd_folders)
    s = sub.add_parser("add", help="add a folder")
    s.add_argument("path")
    s.add_argument("--no-ocr", action="store_true", help="don't read text out of images in this folder")
    s.set_defaults(fn=_cmd_add)
    s = sub.add_parser("remove", help="remove a folder")
    s.add_argument("path")
    s.set_defaults(fn=_cmd_remove)
    sub.add_parser("stats", help="index size").set_defaults(fn=_cmd_stats)
    a = ap.parse_args(argv)
    if not a.cmd:
        a = ap.parse_args(["serve", *(argv or sys.argv[1:])])
    try:
        a.fn(a)
    except KeyboardInterrupt:
        print("\nstopped (progress so far is saved)")
