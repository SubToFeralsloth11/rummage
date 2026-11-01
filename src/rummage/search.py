"""Hybrid search: FTS5 keyword ranking + embedding similarity, fused per file."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time

import numpy as np

from . import config, db, embed
from .query import Parsed, parse

RRF_K = 20
FTS_CANDIDATES = 400
VEC_CANDIDATES = 300
# bge-small: unrelated text scores ~0.45-0.56, real matches ~0.6+ (measured on sample files).
VEC_MIN_SIM = 0.58
VEC_REL_DROP = 0.12  # also drop anything this far below the best match
SNIPPET_CHARS = 300
MIN_COVERAGE = 0.5      # keyword-only hits must contain at least half the query words
REL_SCORE_FLOOR = 0.2   # drop results scoring under 20% of the best one
HOME = os.path.expanduser("~")
PROJECT_MARKERS = (
    "project.godot", ".git", "package.json", "Cargo.toml", "pyproject.toml", "go.mod",
    "Package.swift", "pack.mcmeta", "build.gradle", "CMakeLists.txt",
)
PROJECT_MIN_HITS = 2   # a project needs at least this many matching files to show up
PROJECT_HIT_FLOOR = 0.4  # each counted file must score at least 40% of the best file
PROJECT_WEIGHT = 0.9
PROJECT_DECAY = 0.6    # each further matching file adds less


def _fts_term(t: str) -> str:
    return '"' + t.replace('"', '""') + '"*'


def _term_regex(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")", re.I)


def make_snippet(text: str, rx: re.Pattern | None) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= SNIPPET_CHARS:
        return text
    start = 0
    if rx and (m := rx.search(text)):
        start = max(0, m.start() - SNIPPET_CHARS // 3)
        if start:
            sp = text.find(" ", start)
            start = sp + 1 if 0 <= sp - start < 20 else start
    piece = text[start:start + SNIPPET_CHARS]
    return ("… " if start else "") + piece + (" …" if start + SNIPPET_CHARS < len(text) else "")


def join_chunks(chunks: list[str]) -> str:
    """Rebuild full text from overlapping chunks."""
    out = ""
    for c in chunks:
        if not out:
            out = c
            continue
        overlap = 0
        for k in range(min(250, len(c), len(out)), 10, -1):
            if out.endswith(c[:k]):
                overlap = k
                break
        out += ("" if overlap else "\n") + c[overlap:]
    return out


class Searcher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirty = True
        self._loaded_at = 0.0
        self._chunk_ids = np.zeros(0, dtype=np.int64)
        self._file_ids = np.zeros(0, dtype=np.int64)
        self._mat = np.zeros((0, 384), dtype=np.float32)
        self._model_loading = False
        self._project_cache: dict[str, str] = {}

    def invalidate(self) -> None:
        self._dirty = True

    # ---- vectors -------------------------------------------------------
    def _vectors(self, conn: sqlite3.Connection):
        with self._lock:
            # While indexing, invalidations arrive constantly; reload at most every 10 s.
            if self._dirty and (time.time() - self._loaded_at > 10 or not len(self._chunk_ids)):
                rows = conn.execute(
                    "SELECT id, file_id, emb FROM chunks WHERE emb IS NOT NULL AND length(emb) > 0"
                ).fetchall()
                if rows:
                    self._chunk_ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
                    self._file_ids = np.fromiter((r[1] for r in rows), dtype=np.int64, count=len(rows))
                    self._mat = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                else:
                    self._chunk_ids = np.zeros(0, dtype=np.int64)
                    self._file_ids = np.zeros(0, dtype=np.int64)
                    self._mat = np.zeros((0, 384), dtype=np.float32)
                self._dirty = False
                self._loaded_at = time.time()
            return self._chunk_ids, self._file_ids, self._mat

    def warm_model(self) -> None:
        """Load the embedding model in the background so the first search isn't slow."""
        if embed.is_loaded() or self._model_loading:
            return
        self._model_loading = True

        def load():
            try:
                embed.get_model()
            finally:
                self._model_loading = False

        threading.Thread(target=load, daemon=True, name="rummage-model").start()

    # ---- filters -------------------------------------------------------
    def _eligible(self, conn: sqlite3.Connection, p: Parsed) -> set[int] | None:
        if not p.has_filters:
            return None
        where, args = [], []
        type_conds = []
        if p.kinds:
            type_conds.append(f"kind IN ({','.join('?' * len(p.kinds))})")
            args += sorted(p.kinds)
        if p.exts:
            type_conds.append(f"ext IN ({','.join('?' * len(p.exts))})")
            args += sorted(p.exts)
        if type_conds:
            where.append("(" + " OR ".join(type_conds) + ")")
        if p.screenshot:
            where.append(
                "(lower(name) LIKE 'screenshot%' OR lower(name) LIKE 'screen shot%'"
                " OR lower(name) LIKE 'cleanshot%' OR lower(name) LIKE '%screen%')"
            )
        date_conds, date_args = [], []
        if p.after is not None or p.before is not None:
            rng = []
            if p.after is not None:
                rng.append("mtime >= ?")
                date_args.append(p.after)
            if p.before is not None:
                rng.append("mtime < ?")
                date_args.append(p.before)
            date_conds.append("(" + " AND ".join(rng) + ")")
        if p.month is not None:
            date_conds.append("CAST(strftime('%m', mtime, 'unixepoch', 'localtime') AS INTEGER) = ?")
            date_args.append(p.month)
        if p.month_words:
            # "receipt from October": also accept files whose text mentions October.
            match = "(" + " OR ".join(p.month_words) + ")"
            if p.after is not None and p.before is not None and p.month is None:
                match += " AND " + time.strftime("%Y", time.localtime(p.after))
            date_conds.append(
                "id IN (SELECT c.file_id FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH ?)"
            )
            date_args.append(match)
        if date_conds:
            where.append("(" + " OR ".join(date_conds) + ")")
            args += date_args
        sql = "SELECT id FROM files" + (" WHERE " + " AND ".join(where) if where else "")
        return {r[0] for r in conn.execute(sql, args)}

    # ---- search --------------------------------------------------------
    def search(self, q: str, limit: int = 40, offset: int = 0, wait_for_model: bool = False) -> dict:
        t0 = time.time()
        p = parse(q)
        conn = db.connect()
        try:
            return self._search(conn, p, limit, offset, wait_for_model, t0)
        finally:
            conn.close()

    def _search(self, conn, p: Parsed, limit: int, offset: int, wait_for_model: bool, t0: float) -> dict:
        eligible = self._eligible(conn, p)
        rx = _term_regex(p.terms)
        files: dict[int, dict] = {}  # file_id -> {fts_rank, vec_rank, fts_chunk, vec_chunk, sim}

        if p.terms:
            match = " OR ".join(_fts_term(t) for t in p.terms)
            lim = FTS_CANDIDATES * (5 if eligible is not None else 1)
            rows = conn.execute(
                "SELECT fts.rowid AS cid, c.file_id AS fid FROM fts JOIN chunks c ON c.id = fts.rowid"
                " WHERE fts MATCH ? ORDER BY bm25(fts, 1.0, 4.0) LIMIT ?",
                (match, lim),
            ).fetchall()
            rank = 0
            for r in rows:
                fid = r["fid"]
                if eligible is not None and fid not in eligible:
                    continue
                if fid not in files:
                    files[fid] = {"fts_rank": rank, "fts_chunk": r["cid"]}
                    rank += 1
                elif "fts_rank" not in files[fid]:
                    files[fid].update(fts_rank=rank, fts_chunk=r["cid"])
                    rank += 1

        semantic = False
        if p.text and (embed.is_loaded() or wait_for_model):
            chunk_ids, file_ids, mat = self._vectors(conn)
            if len(chunk_ids):
                semantic = True
                sims = mat @ embed.embed_query(p.text)
                if eligible is not None:
                    sims = np.where(np.isin(file_ids, np.fromiter(eligible, dtype=np.int64)), sims, -1.0)
                k = min(VEC_CANDIDATES * 4, len(sims))
                top = np.argpartition(-sims, k - 1)[:k]
                top = top[np.argsort(-sims[top])]
                rank = 0
                floor = max(VEC_MIN_SIM, float(sims[top[0]]) - VEC_REL_DROP) if len(top) else VEC_MIN_SIM
                for i in top:
                    s = float(sims[i])
                    if s < floor or rank >= VEC_CANDIDATES:
                        break
                    fid = int(file_ids[i])
                    entry = files.setdefault(fid, {})
                    if "vec_rank" not in entry:
                        entry.update(vec_rank=rank, vec_chunk=int(chunk_ids[i]), sim=s)
                        rank += 1
        elif p.text:
            self.warm_model()

        if not p.text and eligible is not None:
            # Pure filter query ("pdfs from last week"): newest first.
            ids = sorted(eligible)
            rows = conn.execute(
                f"SELECT id FROM files WHERE id IN ({','.join('?' * len(ids))}) ORDER BY mtime DESC LIMIT ?",
                (*ids, offset + limit),
            ).fetchall() if ids else []
            for n, r in enumerate(rows):
                files[r[0]] = {"recent_rank": n}

        if not files:
            return self._result(p, [], t0, semantic)

        meta = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT * FROM files WHERE id IN ({','.join('?' * len(files))})", tuple(files)
            )
        }
        chunk_ids_needed = {e.get("fts_chunk") or e.get("vec_chunk") for e in files.values()} - {None}
        chunk_text = {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(chunk_ids_needed))})",
                tuple(chunk_ids_needed),
            )
        } if chunk_ids_needed else {}

        # Which query words each file contains anywhere (name, any chunk, code tags).
        term_files: list[set[int]] = []
        if len(p.terms) >= 2:
            for t in p.terms:
                term_files.append({r[0] for r in conn.execute(
                    "SELECT DISTINCT c.file_id FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH ? LIMIT 50000",
                    (_fts_term(t),),
                )})

        scored = []
        for fid, e in files.items():
            m = meta.get(fid)
            if m is None:
                continue
            cid = e.get("fts_chunk") or e.get("vec_chunk")
            text = chunk_text.get(cid, "")
            score = 0.0
            if "fts_rank" in e:
                if term_files:
                    coverage = sum(fid in s for s in term_files) / len(term_files)
                else:
                    coverage = 1.0
                # "petrol station game": a file that only says "game" is noise unless it also matches by meaning.
                if coverage < MIN_COVERAGE and "vec_rank" not in e:
                    continue
                score += (0.3 + 0.7 * coverage) / (RRF_K + e["fts_rank"])
                name_l = m["name"].lower()
                if p.terms and all(t in name_l for t in p.terms):
                    score += 0.03
            if "vec_rank" in e:
                score += 1.0 / (RRF_K + e["vec_rank"])
            if "recent_rank" in e:
                score += 1.0 / (RRF_K + e["recent_rank"])
            match = (
                "both" if "fts_rank" in e and "vec_rank" in e
                else "keyword" if "fts_rank" in e
                else "meaning" if "vec_rank" in e
                else "filter"
            )
            scored.append((score, fid, match, text))
        scored.sort(key=lambda x: -x[0])
        if scored and p.text:
            floor = scored[0][0] * REL_SCORE_FLOOR
            scored = [s for s in scored if s[0] >= floor]

        def row(fid: int, score: float, match: str, text: str, m=None) -> dict:
            m = m or meta[fid]
            d = os.path.dirname(m["path"])
            return {
                "id": fid,
                "path": m["path"],
                "name": m["name"],
                "dir": d.replace(HOME, "~", 1) if d.startswith(HOME) else d,
                "kind": m["kind"],
                "ext": m["ext"],
                "size": m["size"],
                "mtime": m["mtime"],
                "ocr": bool(m["ocr"]),
                "snippet": make_snippet(text, rx),
                "match": match,
                "score": round(score, 5),
                "summary": json.loads(m["summary"]) if m["summary"] else None,
            }

        results = [row(fid, score, match, text) for score, fid, match, text in scored]
        if p.text and not (p.kinds or p.exts):
            results = self._roll_up_projects(conn, results, row)
        total = len(results)
        if not p.text and eligible is not None:
            total = len(eligible)  # pure filter query: everything that passes the filter
        return self._result(p, results[offset:offset + limit], t0, semantic, total)

    # ---- projects ------------------------------------------------------
    def _project_of(self, path: str, roots: list[str]) -> str | None:
        """Nearest folder with a project marker (project.godot, .git, …), else the top folder under its root."""
        d = os.path.dirname(path)
        root = max((r for r in roots if d == r or d.startswith(r + os.sep)), key=len, default=None)
        if root is None or d == root:
            return None
        if d in self._project_cache:
            return self._project_cache[d]
        cur, found = d, None
        while cur != root and cur.startswith(root):
            if any(os.path.exists(os.path.join(cur, m)) for m in PROJECT_MARKERS):
                found = cur
                break
            cur = os.path.dirname(cur)
        if found is None:
            found = os.path.join(root, os.path.relpath(d, root).split(os.sep)[0])
        self._project_cache[d] = found
        return found

    def _roll_up_projects(self, conn, results: list[dict], row) -> list[dict]:
        """'that petrol station game': files matching inside one project lift the project folder itself."""
        roots = [r.path for r in config.load_roots()]
        groups: dict[str, list[dict]] = {}
        # Only reasonably strong matches count, so a project isn't built out of weak meaning-only noise.
        best = max((r["score"] for r in results if r["kind"] != "folder"), default=0)
        for r in results:
            if r["kind"] == "folder" or r["score"] < best * PROJECT_HIT_FLOOR:
                continue
            proj = self._project_of(r["path"], roots)
            if proj:
                groups.setdefault(proj, []).append(r)
        by_path = {r["path"]: r for r in results}
        added = []
        for proj, hits in groups.items():
            if len(hits) < PROJECT_MIN_HITS:
                continue
            hits.sort(key=lambda h: -h["score"])
            score = PROJECT_WEIGHT * sum(h["score"] * PROJECT_DECAY ** i for i, h in enumerate(hits[:8]))
            summary = [{k: h[k] for k in ("id", "name", "path", "dir", "kind", "ext", "size", "mtime", "ocr", "snippet", "match", "score", "summary")}
                       for h in hits[:6]]
            if proj in by_path:
                entry = by_path[proj]
                entry["score"] = round(entry["score"] + score, 5)
            else:
                m = conn.execute("SELECT * FROM files WHERE path=?", (proj,)).fetchone()
                if m is None:
                    continue
                entry = row(m["id"], score, "project", "", m)
                added.append(entry)
            entry["match"] = "project"
            entry["hits"] = summary
            entry["hit_count"] = len(hits)
            entry["snippet"] = f"Matched in {len(hits)} files: " + ", ".join(h["name"] for h in hits[:6])
        results = results + added
        results.sort(key=lambda r: -r["score"])
        return results

    def _result(self, p: Parsed, results: list, t0: float, semantic: bool, total: int | None = None) -> dict:
        return {
            "query": p.raw,
            "terms": p.terms,
            "chips": p.chips,
            "semantic": semantic,
            "results": results,
            "total": len(results) if total is None else total,
            "took_ms": round((time.time() - t0) * 1000, 1),
        }

    def preview(self, file_id: int) -> dict | None:
        conn = db.connect()
        try:
            m = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if m is None:
                return None
            chunks = [r[0] for r in conn.execute("SELECT text FROM chunks WHERE file_id=? ORDER BY ord", (file_id,))]
            return {
                "id": file_id, "path": m["path"], "name": m["name"], "kind": m["kind"], "ext": m["ext"],
                "size": m["size"], "mtime": m["mtime"], "ocr": bool(m["ocr"]), "error": m["error"],
                "text": join_chunks([c for c in chunks if c]),
                "summary": json.loads(m["summary"]) if m["summary"] else None,
            }
        finally:
            conn.close()
