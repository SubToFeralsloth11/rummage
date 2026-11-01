"""Incremental indexer: scan folders, extract new/changed files, then embed pending chunks."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np

from . import config, db, embed
from .extract import Extracted, chunk_text, extract, folder_listing, in_asset_dir, kind_for

EMBED_BATCH = 128
COMMIT_EVERY = 25


@dataclass
class Progress:
    running: bool = False
    phase: str = "idle"  # scanning | reading | embedding | idle
    done: int = 0
    total: int = 0
    current: str = ""
    started: float = 0.0
    finished: float = 0.0
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: int = 0
    log: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["log"] = d["log"][-5:]
        return d


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEP = re.compile(r"[\W_]+")


def name_words(path: Path) -> str:
    """'enemyPathfinding_v2.gd' in 'Zombie Survival/scripts' -> searchable words."""
    parts = [path.stem, path.suffix.lstrip(".")] + list(path.parts[-4:-1])
    words = []
    for p in parts:
        words.append(p)
        split = _SEP.sub(" ", _CAMEL.sub(" ", p)).strip()
        if split and split != p:
            words.append(split)
    return " ".join(words)


def walk(roots: list[config.Root]):
    """Yield (path, stat, allow_ocr, kind) for every indexable file and folder under the roots."""
    seen: set[str] = set()
    for root in roots:
        if not os.path.isdir(root.path):
            continue
        for dirpath, dirs, files in os.walk(root.path, followlinks=False):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in config.SKIP_DIRS
                and not d.endswith(config.SKIP_DIR_SUFFIXES)
            ]
            if dirpath not in seen:
                seen.add(dirpath)
                try:
                    yield Path(dirpath), os.stat(dirpath), root.ocr, "folder"
                except OSError:
                    pass
            for name in files:
                if name.startswith("."):
                    continue
                p = Path(dirpath, name)
                kind = kind_for(p)
                if kind is None:
                    continue
                # In no-OCR roots (code folders) images and media in asset folders are just noise.
                if not root.ocr and kind in ("image", "media") and in_asset_dir(p):
                    continue
                sp = str(p)
                if sp in seen:
                    continue
                seen.add(sp)
                try:
                    st = p.stat()
                except OSError:
                    continue
                yield p, st, root.ocr, kind


class Indexer:
    def __init__(self) -> None:
        self.progress = Progress()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.on_change = None  # callback after embeddings change (search cache invalidation)

    def start_background(self) -> bool:
        with self._lock:
            if self.progress.running:
                return False
            self.progress = Progress(running=True, started=time.time())
        threading.Thread(target=self._run_safe, daemon=True, name="rummage-indexer").start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run_safe(self) -> None:
        try:
            self.run(already_marked=True)
        except Exception as e:  # keep the server alive; surface the error in status
            self.progress.log.append(f"indexer crashed: {e!r}")
        finally:
            self.progress.running = False
            self.progress.phase = "idle"
            self.progress.finished = time.time()

    def _note(self, msg: str, verbose: bool) -> None:
        self.progress.log.append(msg)
        if verbose:
            print(msg, flush=True)

    def run(self, verbose: bool = False, already_marked: bool = False) -> Progress:
        if not already_marked:
            self.progress = Progress(running=True, started=time.time())
        self._stop.clear()
        pr = self.progress
        conn = db.connect()
        try:
            self._scan_and_read(conn, pr, verbose)
            if not self._stop.is_set():
                self._embed_pending(conn, pr, verbose)
        finally:
            conn.close()
            pr.running = False
            pr.phase = "idle"
            pr.finished = time.time()
            pr.current = ""
        self._note(
            f"done in {pr.finished - pr.started:.1f}s: +{pr.added} new, {pr.updated} changed, "
            f"{pr.removed} removed, {pr.errors} errors",
            verbose,
        )
        return pr

    def _scan_and_read(self, conn: sqlite3.Connection, pr: Progress, verbose: bool) -> None:
        pr.phase = "scanning"
        known = {
            r["path"]: (r["id"], r["mtime"], r["size"])
            for r in conn.execute("SELECT id, path, mtime, size FROM files")
        }
        todo = []
        seen = set()
        for p, st, allow_ocr, kind in walk(config.load_roots()):
            sp = str(p)
            seen.add(sp)
            pr.done = len(seen)
            prev = known.get(sp)
            if prev is None or prev[1] != st.st_mtime or prev[2] != st.st_size:
                todo.append((p, st, allow_ocr, kind, prev[0] if prev else None))
        gone = [fid for path, (fid, _, _) in known.items() if path not in seen]
        for fid in gone:
            db.delete_file(conn, fid)
        pr.removed = len(gone)
        conn.commit()
        self._note(f"scanned {len(seen)} files and folders: {len(todo)} to read, {len(gone)} removed", verbose)

        pr.phase = "reading"
        pr.total = len(todo)
        pr.done = 0
        for i, (p, st, allow_ocr, kind, old_id) in enumerate(todo):
            if self._stop.is_set():
                break
            pr.current = str(p)
            error = None
            try:
                ex = Extracted("folder", folder_listing(p)) if kind == "folder" else extract(p, st.st_size, allow_ocr)
            except Exception as e:
                ex = None
                error = f"{type(e).__name__}: {e}"[:300]
                pr.errors += 1
            if ex:
                kind = ex.kind
            text = ex.text if ex else ""
            if old_id is not None:
                db.delete_file(conn, old_id)
                pr.updated += 1
            else:
                pr.added += 1
            fid = conn.execute(
                "INSERT INTO files(path, name, ext, kind, size, mtime, indexed_at, ocr, error)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (str(p), p.name, "" if kind == "folder" else p.suffix.lower().lstrip("."), kind, st.st_size, st.st_mtime,
                 time.time(), int(bool(ex and ex.ocr)), error),
            ).lastrowid
            words = name_words(p)
            for ord_, chunk in enumerate(chunk_text(text) or [""]):
                cid = conn.execute(
                    "INSERT INTO chunks(file_id, ord, text) VALUES(?,?,?)", (fid, ord_, chunk)
                ).lastrowid
                conn.execute(
                    "INSERT INTO fts(rowid, text, name) VALUES(?,?,?)",
                    (cid, chunk, words if ord_ == 0 else ""),
                )
            pr.done = i + 1
            if verbose and (i + 1) % 200 == 0:
                print(f"  read {i + 1}/{len(todo)}", flush=True)
            if (i + 1) % COMMIT_EVERY == 0:
                conn.commit()
        conn.commit()

    def _embed_pending(self, conn: sqlite3.Connection, pr: Progress, verbose: bool) -> None:
        total = conn.execute("SELECT COUNT(*) FROM chunks WHERE emb IS NULL").fetchone()[0]
        if not total:
            return
        pr.phase = "embedding"
        pr.total = total
        pr.done = 0
        pr.current = "loading AI model"
        embed.get_model()
        while not self._stop.is_set():
            rows = conn.execute(
                "SELECT c.id, c.text, f.name FROM chunks c JOIN files f ON f.id = c.file_id"
                " WHERE c.emb IS NULL LIMIT ?",
                (EMBED_BATCH,),
            ).fetchall()
            if not rows:
                break
            pr.current = rows[0]["name"]
            vecs = embed.embed_passages([f"{r['name']}: {r['text']}"[:2000] for r in rows])
            conn.executemany(
                "UPDATE chunks SET emb=? WHERE id=?",
                [(v.astype(np.float32).tobytes(), r["id"]) for v, r in zip(vecs, rows)],
            )
            conn.commit()
            pr.done += len(rows)
            if verbose and pr.done % (EMBED_BATCH * 20) < EMBED_BATCH:
                print(f"  embedded {pr.done}/{total}", flush=True)
            if self.on_change:
                self.on_change()
