"""Paths, folders to index, and what to skip."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("RUMMAGE_HOME", HOME / ".rummage")).expanduser()
DB_PATH = DATA_DIR / "index.db"
MODELS_DIR = DATA_DIR / "models"
CONFIG_PATH = DATA_DIR / "config.json"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_THREADS = 3  # leave cores free on a 6-core / 8 GB machine

# Directory names never descended into (hidden dirs are skipped too).
SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", "env", "build", "dist", "target",
    "DerivedData", "Pods", "Library", "site-packages", "bower_components",
    "vendor", "coverage", "out", "obj", "bin",
}
# Suffixes of directory names to skip (git worktrees like "Game-wt").
SKIP_DIR_SUFFIXES = ("-wt", ".app", ".xcassets", ".photoslibrary", ".bundle", ".framework")
# Folder names whose images are game/app assets, not documents: filename-indexed only, never OCR'd.
ASSET_DIRS = {"assets", "textures", "sprites", "sounds", "audio", "addons", "icons", "fonts", "materials", "models"}

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 60 * 1024 * 1024
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MIN_OCR_IMAGE_BYTES = 12 * 1024
MAX_CHUNKS_PER_FILE = 80
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150


@dataclass
class Root:
    path: str
    ocr: bool = True


def _default_roots() -> list[Root]:
    roots = [Root(str(HOME / d), True) for d in ("Desktop", "Documents", "Downloads")]
    if (HOME / "Code").is_dir():
        roots.append(Root(str(HOME / "Code"), False))
    return roots


def load_roots() -> list[Root]:
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text())
        return [Root(r["path"], bool(r.get("ocr", True))) for r in data.get("roots", [])]
    roots = _default_roots()
    save_roots(roots)
    return roots


def save_roots(roots: list[Root]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"roots": [asdict(r) for r in roots]}, indent=2))


def add_root(path: str, ocr: bool = True) -> list[Root]:
    p = str(Path(path).expanduser().resolve())
    if not Path(p).is_dir():
        raise ValueError(f"not a folder: {p}")
    roots = [r for r in load_roots() if r.path != p]
    roots.append(Root(p, ocr))
    save_roots(roots)
    return roots


def remove_root(path: str) -> list[Root]:
    p = str(Path(path).expanduser().resolve())
    roots = [r for r in load_roots() if r.path != p]
    save_roots(roots)
    return roots
