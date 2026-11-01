"""Local web UI + JSON API on 127.0.0.1 (never exposed to the network)."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import config, db, embed
from .blendthumb import read_thumbnail, to_png
from .extract import IMAGE_EXTS
from .indexer import Indexer
from .search import Searcher

PKG = Path(__file__).parent
UI_PATH = PKG / "ui.html"
STATIC_DIR = PKG / "static"
THUMB_DIR = config.DATA_DIR / "thumbs"
CONVERT_DIR = config.DATA_DIR / "converted"
RESCAN_SECONDS = 15 * 60
BROWSER_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
BROWSER_VIDEO_EXTS = {"mp4", "m4v", "webm", "mov"}
BLENDER = next((p for p in ("/Applications/Blender.app/Contents/MacOS/Blender", shutil.which("blender")) if p and os.path.exists(p)), None)
FFMPEG = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
# Secret in every file URL: lets sandboxed HTML previews load their own CSS/JS/images
# while other websites (which can't learn the token) still can't read your files.
TOKEN = secrets.token_urlsafe(18)
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")
mimetypes.add_type("application/wasm", ".wasm")

indexer = Indexer()
searcher = Searcher()
indexer.on_change = searcher.invalidate
_convert_lock = threading.Lock()
_asset_cache: dict[tuple[str, str], str | None] = {}


def _file_row(file_id: int):
    conn = db.connect()
    try:
        return conn.execute("SELECT id, path, name, kind, ext, size, mtime FROM files WHERE id=?", (file_id,)).fetchone()
    finally:
        conn.close()


def _roots() -> list[str]:
    return [r.path for r in config.load_roots()]


def _inside_roots(p: str) -> bool:
    """p must already be resolved (realpath); roots are resolved too, so symlinks like /tmp match."""
    return any(p == r or p.startswith(r + os.sep) for r in map(os.path.realpath, _roots()))


def file_url(row) -> str:
    return f"/f/{TOKEN}/{row['id']}/{quote(row['name'])}"


# ---------- assets that live somewhere else ----------
def _search_base(start: str) -> str:
    """Where to look for a missing texture: the project folder (or root) containing the model."""
    roots = [r for r in _roots() if start == r or start.startswith(r + os.sep)]
    root = max(roots, key=len, default=start)
    cur = start
    while cur != root and cur.startswith(root):
        if any(os.path.exists(os.path.join(cur, m)) for m in ("project.godot", ".git", "package.json", "pack.mcmeta")):
            return cur
        cur = os.path.dirname(cur)
    # No project marker: go up at most 3 levels, never above the root.
    cur = start
    for _ in range(3):
        if cur == root:
            break
        cur = os.path.dirname(cur)
    return cur


def _walk_files(base: str, limit: int = 40000):
    n = 0
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in config.SKIP_DIRS
                   and not d.endswith(config.SKIP_DIR_SUFFIXES)]
        for f in files:
            n += 1
            if n > limit:
                return
            yield dirpath, f


def find_asset(model_dir: str, wanted: str) -> str | None:
    """Find 'wood_albedo.png' anywhere in the model's project when it isn't where the model says."""
    name = wanted.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not name:
        return None
    key = (model_dir, name)
    if key in _asset_cache:
        return _asset_cache[key]
    base = _search_base(model_dir)
    stem = os.path.splitext(name)[0]
    best = None
    for dirpath, f in _walk_files(base):
        fl = f.lower()
        if fl == name:
            best = os.path.join(dirpath, f)
            break
        # Same name, different image format (texture.tga referenced, texture.png on disk).
        if best is None and os.path.splitext(fl)[0] == stem and fl.rsplit(".", 1)[-1] in IMAGE_EXTS | {"tga"}:
            best = os.path.join(dirpath, f)
    _asset_cache[key] = best
    return best


def nearby_images(row, limit: int = 300) -> list[dict]:
    """Images in the same project as a model, for the texture picker."""
    model_dir = os.path.dirname(row["path"])
    base = _search_base(model_dir)
    out = []
    for dirpath, f in _walk_files(base):
        if f.rsplit(".", 1)[-1].lower() in BROWSER_IMAGE_EXTS - {"svg"}:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, model_dir)
            out.append({"name": f, "rel": rel, "url": f"/f/{TOKEN}/{row['id']}/{quote(rel)}",
                        "dir": os.path.relpath(dirpath, base)})
            if len(out) >= limit:
                break
    # Likely textures first: names that share words with the model, then "texture"-ish folders.
    stem = os.path.splitext(row["name"])[0].lower()
    out.sort(key=lambda x: (stem[:4] not in x["name"].lower(), "tex" not in x["dir"].lower(), x["dir"], x["name"]))
    return out


# ---------- thumbnails ----------
def _thumb_path(row, ext: str = "jpg") -> Path:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    return THUMB_DIR / f"{row['id']}_{int(row['mtime'])}.{ext}"


def _thumbnail(row) -> tuple[bytes, str] | None:
    path = Path(row["path"])
    if not path.exists():
        return None
    ext, kind = row["ext"], row["kind"]
    if kind == "image" and ext in BROWSER_IMAGE_EXTS and row["size"] < 300_000:
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png"
    if ext == "blend":
        out = _thumb_path(row, "png")
        if not out.exists():
            t = read_thumbnail(str(path))
            if not t:
                return None
            out.write_bytes(to_png(*t))
        return out.read_bytes(), "image/png"
    out = _thumb_path(row)
    if not out.exists():
        if kind == "image":
            cmd = ["sips", "-s", "format", "jpeg", "-Z", "480", str(path), "--out", str(out)]
        elif kind == "video" and FFMPEG:
            cmd = [FFMPEG, "-v", "error", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1",
                   "-vf", "scale=480:-2", str(out)]
        else:
            return _quicklook(path, out)
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return None
        if not out.exists() and kind == "video" and FFMPEG:  # clip shorter than 1 s
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(path), "-frames:v", "1",
                            "-vf", "scale=480:-2", str(out)], capture_output=True, timeout=30)
        if not out.exists():
            return None
    return out.read_bytes(), "image/jpeg"


def _quicklook(path: Path, out: Path) -> tuple[bytes, str] | None:
    """macOS Quick Look renders thumbnails for PSD, Keynote, Pages, USDZ, EPUB, …"""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(["qlmanage", "-t", "-s", "480", "-o", tmp, str(path)], capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            return None
        made = next(Path(tmp).glob("*.png"), None)
        if made is None:
            return None
        png = out.with_suffix(".png")
        shutil.move(str(made), png)
    return png.read_bytes(), "image/png"


# ---------- conversions ----------
BLEND_EXPORT = (
    "import bpy, sys\n"
    "out = sys.argv[sys.argv.index('--') + 1]\n"
    "bpy.ops.export_scene.gltf(filepath=out, export_format='GLB', export_animations=True, export_apply=True)\n"
)


def convert(row) -> tuple[str | None, str | None]:
    """blend -> glb (Blender), video -> browser-friendly mp4 (ffmpeg). Returns (url, error)."""
    ext = row["ext"]
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    if ext == "blend":
        if not BLENDER:
            return None, "Blender isn't installed, so .blend files can't be shown in 3D."
        out = CONVERT_DIR / f"{row['id']}_{int(row['mtime'])}.glb"
        cmd = [BLENDER, "-b", row["path"], "--python-expr", BLEND_EXPORT, "--", str(out)]
        timeout = 300
    elif row["kind"] == "video":
        if not FFMPEG:
            return None, "ffmpeg isn't installed, so this video format can't be converted."
        out = CONVERT_DIR / f"{row['id']}_{int(row['mtime'])}.mp4"
        cmd = [FFMPEG, "-v", "error", "-y", "-i", row["path"], "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "26", "-vf", "scale='min(1280,iw)':-2", "-c:a", "aac", "-movflags", "+faststart", str(out)]
        timeout = 1200
    else:
        return None, "Nothing to convert for this file type."
    with _convert_lock:  # one heavy conversion at a time
        if not out.exists():
            tmp = out.with_suffix(".part" + out.suffix)
            cmd[-1] = str(tmp)
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                tmp.unlink(missing_ok=True)
                return None, "Conversion took too long and was stopped."
            if r.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
                return None, "Conversion failed: " + " ".join(tail)[:300]
            tmp.rename(out)
    return f"/c/{TOKEN}/{out.name}", None


# ---------- status ----------
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
    s["tools"] = {"blender": bool(BLENDER), "ffmpeg": bool(FFMPEG)}
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "Rummage/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, cache: bool = False, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _local_host(self) -> bool:
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _local_only(self) -> bool:
        # Block cross-site requests from web pages (CSRF / DNS rebinding).
        origin = self.headers.get("Origin")
        if not self._local_host():
            return False
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    # ----- raw files (video seeking, model textures, HTML pages with their CSS/JS) -----
    def _serve_path(self, path: str, html_sandbox: bool = True):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("text/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                else:  # suffix range: last N bytes
                    start, end = max(0, size - int(b)), size - 1
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")  # token in the URL is the access check
        if html_sandbox:
            # Opened directly or framed, a page from your disk runs as an opaque origin:
            # its scripts can't call Rummage's API or read other files.
            self.send_header("Content-Security-Policy",
                             "sandbox allow-scripts allow-forms allow-popups allow-modals allow-pointer-lock allow-downloads")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    buf = f.read(min(1 << 20, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser stopped reading (e.g. video seek)

    def _serve_file_route(self, parts: list[str]):
        # /f/<token>/<id>/<path relative to the file's folder>
        if len(parts) < 4 or not secrets.compare_digest(parts[1], TOKEN):
            return self._json({"error": "forbidden"}, 403)
        try:
            row = _file_row(int(parts[2]))
        except ValueError:
            return self._json({"error": "bad id"}, 400)
        if row is None:
            return self._json({"error": "not found"}, 404)
        base = os.path.dirname(row["path"]) if row["kind"] != "folder" else row["path"]
        rel = unquote("/".join(parts[3:]))
        target = os.path.realpath(os.path.join(base, rel))
        if not (os.path.isfile(target) and _inside_roots(target)):
            # Texture or script referenced from elsewhere (or by an absolute path from another computer).
            found = find_asset(base, rel)
            if not found or not _inside_roots(os.path.realpath(found)):
                return self._json({"error": "not found"}, 404)
            target = found
        return self._serve_path(target)

    def _serve_converted(self, parts: list[str]):
        if len(parts) != 3 or not secrets.compare_digest(parts[1], TOKEN):
            return self._json({"error": "forbidden"}, 403)
        p = CONVERT_DIR / os.path.basename(unquote(parts[2]))
        return self._serve_path(str(p), html_sandbox=False) if p.is_file() else self._json({"error": "not found"}, 404)

    def _serve_static(self, rel: str):
        p = (STATIC_DIR / unquote(rel)).resolve()
        if not str(p).startswith(str(STATIC_DIR.resolve()) + os.sep) or not p.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return self._send(p.read_bytes(), ctype + ("; charset=utf-8" if "javascript" in ctype else ""), cache=True)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if not self._local_host():
            return self._json({"error": "forbidden"}, 403)
        if parts[0] == "f":
            return self._serve_file_route(parts)
        if parts[0] == "c":
            return self._serve_converted(parts)
        if not self._local_only():
            return self._json({"error": "forbidden"}, 403)
        if parts[0] == "static":
            return self._serve_static("/".join(parts[1:]))
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                html = UI_PATH.read_text(encoding="utf-8").replace("__RUMMAGE_TOKEN__", TOKEN)
                return self._send(html.encode(), "text/html; charset=utf-8")
            if u.path == "/api/search":
                q = qs.get("q", "").strip()
                if not q:
                    return self._json({"results": [], "chips": [], "terms": [], "total": 0, "semantic": embed.is_loaded()})
                return self._json(searcher.search(
                    q, limit=min(int(qs.get("limit", 40)), 200), offset=max(int(qs.get("offset", 0)), 0)
                ))
            if u.path == "/api/status":
                return self._json(status())
            if u.path == "/api/preview":
                pv = searcher.preview(int(qs["id"]))
                if not pv:
                    return self._json({"error": "not found"}, 404)
                pv["url"] = file_url(pv)
                return self._json(pv)
            if u.path == "/api/thumb":
                row = _file_row(int(qs["id"]))
                if row is None:
                    return self._json({"error": "not found"}, 404)
                t = _thumbnail(row)
                if t is None:
                    return self._json({"error": "no thumbnail"}, 404)
                return self._send(t[0], t[1], cache=True)
            if u.path == "/api/nearby-images":
                row = _file_row(int(qs["id"]))
                if row is None:
                    return self._json({"error": "not found"}, 404)
                return self._json({"images": nearby_images(row)})
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
            if u.path == "/api/convert":
                row = _file_row(int(body["id"]))
                if row is None:
                    return self._json({"error": "not found"}, 404)
                url, err = convert(row)
                return self._json({"url": url} if url else {"error": err}, 200 if url else 422)
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


def serve(port: int = 47615, open_browser: bool = True, index_on_start: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
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
