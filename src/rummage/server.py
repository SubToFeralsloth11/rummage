"""Local web UI + JSON API on 127.0.0.1 (never exposed to the network)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, embed
from .indexer import Indexer
from .search import Searcher

UI_PATH = Path(__file__).with_name("ui.html")
THUMB_DIR = config.DATA_DIR / "thumbs"
RESCAN_SECONDS = 15 * 60
BROWSER_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
        "webp": "image/webp", "bmp": "image/bmp"}

indexer = Indexer()
searcher = Searcher()
indexer.on_change = searcher.invalidate


def _file_row(file_id: int):
    conn = db.connect()
    try:
        return conn.execute("SELECT id, path, kind, ext, size, mtime FROM files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()


def _thumbnail(row) -> tuple[bytes, str] | None:
    """Small JPEG via macOS `sips` (handles HEIC/TIFF too); cached by id+mtime."""
    path = Path(row["path"])
    if not path.exists():
        return None
    if row["ext"] in BROWSER_IMAGE_EXTS and row["size"] < 300_000:
        return path.read_bytes(), MIME[row["ext"]]
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_DIR / f"{row['id']}_{int(row['mtime'])}.jpg"
    if not out.exists():
        r = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-Z", "480", str(path), "--out", str(out)],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not out.exists():
            return None
    return out.read_bytes(), "image/jpeg"


def _pick_folder() -> str | None:
    r = subprocess.run(
        ["osascript", "-e", 'POSIX path of (choose folder with prompt "Pick a folder for Rummage to search")'],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip().rstrip("/") or None


def status() -> dict:
    conn = db.connect()
    try:
        s = db.stats(conn)
    finally:
        conn.close()
    s["roots"] = [{"path": r.path, "ocr": r.ocr, "exists": Path(r.path).is_dir()} for r in config.load_roots()]
    s["indexing"] = indexer.progress.to_dict()
    s["model_ready"] = embed.is_loaded()
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "Rummage/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _local_only(self) -> bool:
        # Block cross-site requests from web pages (CSRF / DNS rebinding).
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return False
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(UI_PATH.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/search":
                q = qs.get("q", "").strip()
                if not q:
                    return self._json({"results": [], "chips": [], "terms": [], "semantic": embed.is_loaded()})
                return self._json(searcher.search(q, limit=min(int(qs.get("limit", 40)), 200)))
            if u.path == "/api/status":
                return self._json(status())
            if u.path == "/api/preview":
                pv = searcher.preview(int(qs["id"]))
                return self._json(pv) if pv else self._json({"error": "not found"}, 404)
            if u.path == "/api/thumb":
                row = _file_row(int(qs["id"]))
                if row is None or row["kind"] != "image":
                    return self._json({"error": "not found"}, 404)
                t = _thumbnail(row)
                if t is None:
                    return self._json({"error": "no thumbnail"}, 404)
                return self._send(t[0], t[1], cache=True)
            return self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)

    def do_POST(self):
        if not self._local_only():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/index":
                started = indexer.start_background()
                return self._json({"started": started, "indexing": indexer.progress.to_dict()})
            if u.path == "/api/open":
                row = _file_row(int(body["id"]))
                if row is None:
                    return self._json({"error": "not found"}, 404)
                args = ["open", "-R", row["path"]] if body.get("reveal") else ["open", row["path"]]
                subprocess.Popen(args)
                return self._json({"ok": True})
            if u.path == "/api/roots/pick":
                p = _pick_folder()
                if not p:
                    return self._json({"cancelled": True})
                config.add_root(p, bool(body.get("ocr", True)))
                indexer.start_background()
                return self._json(status())
            if u.path == "/api/roots/remove":
                config.remove_root(body["path"])
                indexer.start_background()  # drops that folder's files from the index
                return self._json(status())
            if u.path == "/api/roots/ocr":
                roots = config.load_roots()
                for r in roots:
                    if r.path == body["path"]:
                        r.ocr = bool(body["ocr"])
                config.save_roots(roots)
                return self._json(status())
            return self._json({"error": "not found"}, 404)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)


def _rescan_loop():
    while True:
        time.sleep(RESCAN_SECONDS)
        indexer.start_background()


def serve(port: int = 8765, open_browser: bool = True, index_on_start: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Rummage running at {url}  (Ctrl+C to quit)", flush=True)
    searcher.warm_model()
    if index_on_start:
        indexer.start_background()
    threading.Thread(target=_rescan_loop, daemon=True, name="rummage-rescan").start()
    if open_browser:
        subprocess.Popen(["open", url])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        indexer.stop()
        httpd.server_close()
