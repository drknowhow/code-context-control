"""SQLite store for CodeIndex: documents, chunks, and the FTS5 search table.

Replaces ``.c3/index/index.json`` (2.107.0). That file held every chunk's
content plus the TF-IDF vectors as one JSON blob — 30 MB for 513 files, ~120
MB at the file cap — rewritten whole on every rebuild, and every rebuild was
whole: the watcher triggered one after ten file changes. Here a document is
a row with a content hash, its chunks are rows, and the FTS5 table (when
SQLite has FTS5) is maintained per document, so :meth:`CodeIndex.refresh`
touches only what changed.

Layout (``SCHEMA_VERSION``; an older or foreign schema is rebuilt, never
migrated in place — the store is derived data):

* ``meta(key, value)``
* ``docs(doc_id, path, full_path, lines, tokens, mtime, content_hash)``
* ``chunks(chunk_id, doc_id, name, type, line_start, line_end, tokens,
  content, doc_kind, lang)``
* ``chunk_fts(chunk_id UNINDEXED, path, symbol, kind, body)`` — FTS5,
  ``unicode61 tokenchars '_'`` over text pre-tokenized by
  :func:`services.lexical_index.tokenize_code`.

Connections are opened per call: the MCP server, the hub and a CLI can all
hold one project. Full writes go to a temp file and are swapped in
atomically; incremental writes run in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path

from services import lexical_index as _lexical
from services.lexical_index import Filters, doc_kind, lang_of, tokenize_code

log = logging.getLogger("c3.index_store")

SCHEMA_VERSION = 2
DB_NAME = "index.sqlite"
LEGACY_FILES = ("index.json", "lexical.sqlite")

# BM25 column weights: chunk_id (unindexed, 0), path, symbol, kind, body.
_WEIGHTS = (0.0, 3.0, 6.0, 1.0, 1.0)
_TOKEN_OK = re.compile(r"^[a-z0-9_]+$")

_DDL = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, path TEXT NOT NULL, full_path TEXT NOT NULL, "
    "lines INTEGER NOT NULL, tokens INTEGER NOT NULL, mtime REAL NOT NULL, content_hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chunks(chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, name TEXT, "
    "type TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, tokens INTEGER NOT NULL, "
    "content TEXT NOT NULL, doc_kind TEXT NOT NULL, lang TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id)",
)
_DDL_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(chunk_id UNINDEXED, path, symbol, kind, body, "
            "tokenize=\"unicode61 tokenchars '_'\")")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _fts_text(chunk: dict, doc_id: str) -> tuple[str, str, str, str]:
    path_tokens = tokenize_code(doc_id.replace("\\", "/").replace("/", " ").replace(".", " "))
    symbol_tokens = tokenize_code(chunk.get("name") or "")
    kind = f"{doc_kind(doc_id)} {chunk.get('type') or 'block'} {lang_of(doc_id)}".strip()
    body_tokens = tokenize_code(chunk.get("content") or "")
    return (" ".join(path_tokens), " ".join(symbol_tokens), kind, " ".join(body_tokens))


class IndexStore:
    """Persistence + FTS5 retrieval for one project's code index."""

    def __init__(self, index_dir):
        self.index_dir = Path(index_dir)
        self.path = self.index_dir / DB_NAME
        # Looked up through the module so a test can patch the probe.
        self.fts = _lexical.fts5_available()

    # ── connections / schema ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        for stmt in _DDL:
            conn.execute(stmt)
        if self.fts:
            conn.execute(_DDL_FTS)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _schema_ok(self, conn: sqlite3.Connection) -> bool:
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                return False
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            if not {"meta", "docs", "chunks"} <= names:
                return False
            if self.fts and "chunk_fts" not in names:
                return False
            return True
        except sqlite3.Error:
            return False

    def exists(self) -> bool:
        """A store at this schema with at least one document."""
        if not self.path.exists():
            return False
        try:
            conn = self._connect()
            try:
                return self._schema_ok(conn) and conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] > 0
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def counts(self) -> tuple[int, int]:
        """``(documents, chunks)`` without loading content; ``(0, 0)`` when absent."""
        if not self.path.exists():
            return (0, 0)
        try:
            conn = self._connect()
            try:
                if not self._schema_ok(conn):
                    return (0, 0)
                d = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                c = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                return (int(d), int(c))
            finally:
                conn.close()
        except sqlite3.Error:
            return (0, 0)

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def remove_legacy_files(self) -> list:
        """Delete pre-2.107.0 artefacts (index.json, lexical.sqlite) once the
        store is written. Returns the names removed."""
        removed = []
        for name in LEGACY_FILES:
            for cand in (self.index_dir / name, self.index_dir / (name + "-wal"), self.index_dir / (name + "-shm")):
                try:
                    if cand.exists():
                        cand.unlink()
                        removed.append(cand.name)
                except OSError:
                    pass
        return removed

    # ── write ───────────────────────────────────────────────────────────────

    @staticmethod
    def _doc_row(doc_id: str, doc: dict, mtime: float, digest: str) -> tuple:
        return (doc_id, doc.get("path") or doc_id, doc.get("full_path") or "", int(doc.get("lines") or 0),
                int(doc.get("tokens") or 0), float(mtime or 0.0), digest)

    @staticmethod
    def _chunk_row(chunk_id: str, chunk: dict) -> tuple:
        doc_id = chunk.get("doc_id") or ""
        return (chunk_id, doc_id, chunk.get("name"), (chunk.get("type") or "block").lower(),
                int(chunk.get("line_start") or 0), int(chunk.get("line_end") or 0),
                int(chunk.get("tokens") or 0), chunk.get("content") or "", doc_kind(doc_id), lang_of(doc_id))

    def _insert_doc(self, conn, doc_id: str, doc: dict, chunks: list, mtime: float, digest: str) -> int:
        conn.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?,?,?)", self._doc_row(doc_id, doc, mtime, digest))
        rows = [self._chunk_row(cid, c) for cid, c in chunks]
        conn.executemany("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        if self.fts:
            conn.executemany("INSERT INTO chunk_fts VALUES (?,?,?,?,?)",
                             [(cid, *_fts_text(c, doc_id)) for cid, c in chunks])
        return len(rows)

    def _delete_doc(self, conn, doc_id: str) -> None:
        if self.fts:
            conn.execute("DELETE FROM chunk_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE doc_id = ?)",
                         (doc_id,))
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))

    def write_all(self, documents: dict, chunks: dict, mtimes: dict, hashes: dict, meta: dict | None = None) -> int:
        """Full write: temp file, then atomic swap. Returns chunk rows written."""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".sqlite.tmp")
        for stale in (tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm")):
            try:
                stale.unlink()
            except OSError:
                pass
        by_doc: dict[str, list] = {}
        for cid, chunk in chunks.items():
            by_doc.setdefault(chunk.get("doc_id") or "", []).append((cid, chunk))
        conn = sqlite3.connect(str(tmp))
        rows = 0
        try:
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            self._create_schema(conn)
            for doc_id, doc in documents.items():
                rows += self._insert_doc(conn, doc_id, doc, by_doc.get(doc_id, []),
                                         mtimes.get(doc_id, 0.0), hashes.get(doc_id, ""))
            for key, value in (meta or {}).items():
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
            conn.commit()
        finally:
            conn.close()
        for stale in (self.path.with_name(self.path.name + "-wal"), self.path.with_name(self.path.name + "-shm")):
            try:
                stale.unlink()
            except OSError:
                pass
        try:
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("index store swap failed (%s); keeping the previous store", exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return 0
        return rows

    def apply(self, upserts: dict, deletes, mtimes: dict, hashes: dict, meta: dict | None = None) -> int:
        """Incremental write in one transaction.

        ``upserts``: ``{doc_id: (doc_meta, [(chunk_id, chunk), ...])}``;
        ``deletes``: doc_ids to drop. Returns chunk rows written.
        """
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))
        conn = self._connect()
        rows = 0
        try:
            if not self._schema_ok(conn):
                raise RuntimeError("index store schema mismatch")
            conn.execute("BEGIN")
            for doc_id in deletes:
                self._delete_doc(conn, doc_id)
            for doc_id, (doc, chunk_pairs) in upserts.items():
                self._delete_doc(conn, doc_id)
                rows += self._insert_doc(conn, doc_id, doc, chunk_pairs, mtimes.get(doc_id, 0.0),
                                         hashes.get(doc_id, ""))
            for key, value in (meta or {}).items():
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return rows

    # ── read ────────────────────────────────────────────────────────────────

    def load(self) -> dict | None:
        """Everything CodeIndex keeps in memory, or None when the store is
        absent, foreign or empty."""
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            if not self._schema_ok(conn):
                return None
            documents, mtimes, hashes = {}, {}, {}
            for doc_id, path, full_path, lines, tokens, mtime, digest in conn.execute(
                    "SELECT doc_id, path, full_path, lines, tokens, mtime, content_hash FROM docs"):
                documents[doc_id] = {"path": path, "full_path": full_path, "lines": lines, "tokens": tokens}
                mtimes[doc_id] = mtime
                hashes[doc_id] = digest
            if not documents:
                return None
            chunks = {}
            for cid, doc_id, name, ctype, ls, le, tokens, content in conn.execute(
                    "SELECT chunk_id, doc_id, name, type, line_start, line_end, tokens, content FROM chunks"):
                chunks[cid] = {"id": cid, "doc_id": doc_id, "content": content, "tokens": tokens,
                               "type": ctype, "name": name, "line_start": ls, "line_end": le}
            meta = {}
            for key, value in conn.execute("SELECT key, value FROM meta"):
                try:
                    meta[key] = json.loads(value)
                except (TypeError, ValueError):
                    meta[key] = value
            fts_rows = 0
            if self.fts:
                try:
                    fts_rows = int(conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0])
                except sqlite3.Error:
                    fts_rows = 0
            return {"documents": documents, "chunks": chunks, "mtimes": mtimes, "hashes": hashes,
                    "meta": meta, "fts_rows": fts_rows}
        except sqlite3.Error as exc:
            log.debug("index store load failed: %s", exc)
            return None
        finally:
            conn.close()

    def doc_hashes(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            conn = self._connect()
            try:
                if not self._schema_ok(conn):
                    return {}
                return dict(conn.execute("SELECT doc_id, content_hash FROM docs").fetchall())
            finally:
                conn.close()
        except sqlite3.Error:
            return {}

    # ── FTS5 query ──────────────────────────────────────────────────────────

    @staticmethod
    def _match_expr(tokens: list[str]) -> str:
        safe = [t for t in tokens if _TOKEN_OK.match(t)]
        return " OR ".join(f'"{t}"' for t in safe)

    def search(self, query_tokens: list[str], limit: int = 40,
               filters: Filters | None = None, allowed_docs=None) -> list[tuple[str, float]]:
        """``[(chunk_id, score)]`` best first; score is ``-bm25`` (positive, larger is better)."""
        if not self.fts or not self.path.exists():
            return []
        expr = self._match_expr(query_tokens)
        if not expr:
            return []
        filters = filters or Filters()
        conn = self._connect()
        try:
            params: list = [*_WEIGHTS, expr]
            sql = (
                "SELECT f.chunk_id, bm25(chunk_fts, ?, ?, ?, ?, ?) AS rank "
                "FROM chunk_fts f JOIN chunks c ON c.chunk_id = f.chunk_id "
                "WHERE chunk_fts MATCH ?")
            if filters.langs:
                sql += " AND c.lang IN (%s)" % ",".join("?" * len(filters.langs))
                params.extend(sorted(filters.langs))
            if filters.kinds:
                sql += " AND (c.doc_kind IN (%s) OR c.type IN (%s))" % (
                    ",".join("?" * len(filters.kinds)), ",".join("?" * len(filters.kinds)))
                params.extend(sorted(filters.kinds))
                params.extend(sorted(filters.kinds))
            if allowed_docs is not None:
                allowed = list(allowed_docs)
                if not allowed:
                    return []
                conn.execute("CREATE TEMP TABLE allowed_docs(doc_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT OR IGNORE INTO allowed_docs VALUES (?)", [(d,) for d in allowed])
                sql += " AND c.doc_id IN (SELECT doc_id FROM allowed_docs)"
            sql += " ORDER BY rank, f.chunk_id LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            log.debug("fts search failed: %s", exc)
            return []
        finally:
            conn.close()
        return [(cid, -float(rank)) for cid, rank in rows]
