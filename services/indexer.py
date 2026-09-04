"""
Smart Local Index Service

Builds a searchable index of your codebase using TF-IDF and code structure analysis.
Retrieves only the most relevant code snippets for a given query, dramatically reducing
the amount of code Claude needs to read.
"""
import json
import logging
import math
import os
import re
import time
from collections import Counter, OrderedDict
from pathlib import Path

from core import count_tokens
from services.index_store import IndexStore, content_hash
from services.lexical_index import Filters, doc_kind, intent_prior, tokenize_code
from services.scanner import SKIP_DIRS, iter_files

log = logging.getLogger("c3.indexer")


def _masked_content(fpath, project_path):
    """Index-time content for one file: the masked view when masked, else raw.

    Returns ``None`` when the path is masked but its view cannot be produced —
    the caller must skip the file rather than fall back to raw bytes.
    """
    from services import access_guard, mask_mirror
    try:
        v = access_guard.verdict(str(fpath), "read", str(project_path))
    except Exception:
        return None
    if v.denial:
        return None
    if not v.masked:
        return fpath.read_text(encoding="utf-8", errors="replace")
    try:
        return mask_mirror.render_for_path(fpath, project_path).text
    except Exception:
        return None


# Co-occurrence is O(unique_tokens^2) per chunk, so one minified vendor
# bundle can dominate a whole index build. Both bounds below are deliberate
# and their effect is REPORTED in build_index()'s stats rather than applied
# silently — a capped build must never look like a complete one.
#
# 200: chunks above this are minified/bundled/data blobs, not code. The
# pruning step keeps only the top-5 co-occurring terms per token with at
# least 3 hits, so such a chunk contributes noise that is discarded anyway.
_COOC_MAX_CHUNK_TOKENS = 200
# 20M: whole-pass ceiling on pair updates, so no repository shape can make
# this step unbounded. ~10s of CPU on the boxes this runs on.
_COOC_MAX_PAIR_UPDATES = 20_000_000

# The file universe c3 indexes. Module-level so the search tool's `exact` and
# `files` actions can walk the SAME set without a live CodeIndex (tests hand
# them a mock indexer; before 2.105.0 `exact` walked file_memory instead and
# saw 427 of 513 indexed files on this repo).
DEFAULT_CODE_EXTS = frozenset({
    # Python
    '.py', '.pyi', '.pyx',
    # JavaScript / TypeScript
    '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
    # Web
    '.html', '.htm', '.css', '.scss', '.sass', '.less', '.vue', '.svelte',
    # Markdown
    '.md', '.mdx',
    # Data / Config
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env.example',
    '.xml', '.csv',
    # Systems
    '.c', '.h', '.cpp', '.cxx', '.cc', '.hpp', '.hxx',
    '.rs', '.go', '.java', '.kt', '.kts', '.scala',
    '.cs', '.fs', '.vb',
    # Scripting
    '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
    '.rb', '.pl', '.pm', '.lua', '.php',
    '.r', '.R', '.jl',
    # Query / Schema
    '.sql', '.graphql', '.gql', '.prisma',
    # Functional
    '.hs', '.ex', '.exs', '.erl', '.clj', '.cljs', '.elm', '.ml', '.mli',
    # Mobile
    '.swift', '.m', '.mm', '.dart',
    # Docs / Markup
    '.rst', '.tex', '.adoc',
    # DevOps / IaC
    '.tf', '.hcl', '.dockerfile', '.nix',
    # Other
    '.proto', '.thrift', '.zig', '.nim', '.v',
    '.makefile', '.cmake',
})

# A chunk larger than the caller's token budget used to be skipped outright,
# so a class with many methods could never be returned for its own name (231
# such chunks on this repo at the default budget). Now a window of at most
# this many tokens, anchored on the first line that mentions a query term,
# stands in for it.
_WINDOW_MAX_TOKENS = 400


class CodeIndex:
    """TF-IDF based code search index with structural awareness."""

    def __init__(self, project_path: str, index_dir: str = ".c3/index",
                 cooccurrence: "bool | None" = None):
        self.project_path = Path(project_path)
        self.index_dir = self.project_path / index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)

        # Index data
        self.documents = {}       # doc_id -> {path, content, chunks}
        self.chunks = {}          # chunk_id -> {doc_id, content, type, name, line_start, line_end}
        self.idf = {}             # term -> IDF score
        self.chunk_tfidf = {}     # chunk_id -> {term: tfidf_score}
        self.symbols = {}         # symbol_name (lowercased, dotted) -> [chunk_ids]
        self._symbol_tail = {}    # last dotted segment -> [chunk_ids]; derived from symbols
        # Bounded LRU — an unbounded dict grew indefinitely over long sessions.
        self._search_cache: "OrderedDict" = OrderedDict()
        self._search_cache_max = 128
        # Memoized query expansion + bigrams. Agents repeat the same queries.
        self._expand_cache: dict = {}
        self._cooccurrence = {}   # term -> {term: count} for auto-synonyms
        self._cooccurrence_stats = {}
        # None -> read `search_cooccurrence_synonyms` from .c3/config.json
        # (default off: the pass exhausted its 20M-pair budget on a 513-file
        # repo and its picks lifted CHANGELOG headings over source files).
        self._cooccurrence_enabled = cooccurrence
        self._file_mtimes = {}    # doc_id -> mtime for recency bias (persisted)

        # Lexical engine (services/lexical_index): SQLite FTS5 / BM25 when
        # available, else the TF-IDF scan below. `search_engine: "tfidf"` in
        # .c3/config.json forces the fallback; `search_synonyms` is the only
        # synonym source (the hardcoded C3-vocabulary map is gone).
        cfg = self._project_config()
        self._engine_pref = str(cfg.get("search_engine") or "auto").lower()
        self._synonyms = {
            str(k).lower(): [str(v).lower() for v in (vals or [])]
            for k, vals in (cfg.get("search_synonyms") or {}).items()
            if isinstance(vals, (list, tuple))
        }
        # Persistence: services/index_store (SQLite; FTS5 table when available).
        # index.json is gone since 2.107.0 — a legacy one is rebuilt into the
        # store on first load.
        self._store = IndexStore(self.index_dir)
        self._hashes: dict = {}   # doc_id -> content hash; drives refresh()
        self._lexical_ok = False

        # Hybrid fusion (services/retrieval, plan P3): a dense backend — any
        # object with `.ready` and `.candidates(query, limit)`, in practice the
        # EmbeddingIndex the runtime attaches — is fused with the lexical
        # ranking by Reciprocal Rank Fusion. `search_fusion: "off"` disables it;
        # `search_rrf_k` tunes the constant (60).
        self.dense = None
        self._fusion_pref = str(cfg.get("search_fusion") or "auto").lower()
        try:
            self._rrf_k = int(cfg.get("search_rrf_k") or 60)
        except (TypeError, ValueError):
            self._rrf_k = 60
        # Optional reranker (services/reranker, plan P4): a cross-encoder over
        # the top few candidates of a natural-language query. Off unless
        # `search_rerank: "auto"` — it has to earn default on the suite.
        self.reranker = None
        self._rerank_pref = str(cfg.get("search_rerank") or "off").lower()
        try:
            self._rerank_top_n = max(4, int(cfg.get("search_rerank_top_n") or 16))
        except (TypeError, ValueError):
            self._rerank_top_n = 16

        # Config - shared pruned-walk skip set (services/scanner.py)
        self.skip_dirs = set(SKIP_DIRS)
        self.code_exts = set(DEFAULT_CODE_EXTS)

        # Sub-project exclusion: designated child folders carry their own
        # .c3 index, so the parent skips them (relative-path-prefix match —
        # name-based skip_dirs would catch same-named siblings).
        try:
            from services.subprojects import exclusion_prefixes, is_excluded
            self.exclude_prefixes = exclusion_prefixes(self.project_path)
            self._is_excluded = is_excluded
        except Exception:
            self.exclude_prefixes = []
            self._is_excluded = None

    _DEFAULT_MAX_FILES = 2000

    def _project_config(self) -> dict:
        try:
            cfg = json.loads((self.project_path / '.c3' / 'config.json').read_text(encoding='utf-8'))
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    @property
    def lexical_engine(self) -> str:
        """``fts5`` when queries go through SQLite FTS5, else ``tfidf``."""
        return "fts5" if self._lexical_ok else "tfidf"

    def _dense_ready(self) -> bool:
        dense = self.dense
        if dense is None or self._fusion_pref == "off":
            return False
        try:
            return bool(getattr(dense, "ready", False))
        except Exception:
            return False

    @property
    def fusion(self) -> str:
        """``rrf`` when a ready dense backend is fused into ``code`` queries, else ``off``."""
        return "rrf" if self._dense_ready() else "off"

    def _rerank_ready(self) -> bool:
        rr = self.reranker
        if rr is None or self._rerank_pref == "off":
            return False
        try:
            return bool(getattr(rr, "ready", False))
        except Exception:
            return False

    @property
    def rerank(self) -> str:
        """Reranker name when natural-language ``code`` queries are reranked, else ``off``."""
        if not self._rerank_ready():
            return "off"
        return str(getattr(self.reranker, "name", None) or "on")

    def _apply_rerank(self, query: str, base_tokens: list, ranked: list, exact_ids: set) -> list:
        """Cross-encoder pass over the top ``search_rerank_top_n`` candidates.

        Identifier-shaped queries are left alone (services/reranker
        .is_natural_language). Exact-symbol matches keep their place ahead of
        the reranked block; candidates beyond the block keep their order.
        Any reranker failure or empty answer leaves ``ranked`` unchanged.
        """
        from services.reranker import is_natural_language, passage_text

        if not is_natural_language(query, base_tokens):
            return ranked
        n = self._rerank_top_n
        head = ranked[:n]
        exact_head = [(cid, s) for cid, s in head if cid in exact_ids]
        block = [(cid, s) for cid, s in head if cid not in exact_ids and cid in self.chunks]
        if len(block) < 2:
            return ranked
        docs = [(cid, passage_text(self.chunks[cid])) for cid, _ in block]
        try:
            scored = self.reranker.rerank(query, docs)
        except Exception as exc:
            log.debug("rerank failed (%s); fused order kept", exc)
            return ranked
        if not scored:
            return ranked
        order = {cid: i for i, (cid, _) in enumerate(scored)}
        block.sort(key=lambda cs: (order.get(cs[0], len(order)), -cs[1], cs[0]))
        return exact_head + block + ranked[n:]

    def _fuse_dense(self, query: str, lexical_ranked: list, filters: Filters,
                    exact_ids: set, want: int) -> list:
        """RRF of the lexical ranking with the dense backend's candidates.

        Each list contributes its top ``n`` (20..50, from ``want``); dense ids
        that no longer exist in the index (stale vectors after a refresh) or
        fail the filters are dropped before fusion. Exact-symbol matches keep
        their override through the rank key. Lexical candidates beyond ``n``
        trail the fused block so budget filling can continue past it. Any
        backend failure returns the lexical ranking unchanged.
        """
        from services.retrieval import rrf

        n = max(20, min(50, int(want) * 8))
        try:
            raw = self.dense.candidates(query, limit=n * 2)
        except Exception as exc:
            log.debug("dense candidates failed (%s); lexical only", exc)
            return lexical_ranked
        dense = []
        for cid, _score in raw or []:
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            if not filters.chunk_ok(chunk["doc_id"], chunk.get("type") or ""):
                continue
            dense.append(cid)
            if len(dense) >= n:
                break
        if not dense:
            return lexical_ranked
        lexical = [cid for cid, _ in lexical_ranked[:n]]
        fused = rrf([lexical, dense], k=self._rrf_k)
        for cid in exact_ids:
            fused.setdefault(cid, 0.0)
        ranked = sorted(fused.items(), key=self._rank_key(exact_ids), reverse=True)
        tail = [(cid, score) for cid, score in lexical_ranked[n:] if cid not in fused]
        return ranked + tail

    def _lexical_wanted(self) -> bool:
        return self._engine_pref != "tfidf" and self._store.fts

    def index_exists(self) -> bool:
        """A usable store, or a legacy index.json that a load would migrate."""
        return self._store.exists() or (self.index_dir / "index.json").exists()

    def needs_migration(self) -> bool:
        """True when only a pre-2.107.0 layout is on disk: loading it triggers
        a full rebuild, which callers on a latency-sensitive path (the MCP
        handshake) should move to a background thread."""
        return (not self._store.exists()) and (
            (self.index_dir / "index.json").exists() or (self.index_dir / "lexical.sqlite").exists())

    def _configured_max_files(self) -> int:
        """``index_max_files`` from .c3/config.json; default 2000."""
        try:
            cfg = json.loads((self.project_path / '.c3' / 'config.json')
                             .read_text(encoding='utf-8'))
            val = int(cfg.get('index_max_files', self._DEFAULT_MAX_FILES))
            return val if val > 0 else self._DEFAULT_MAX_FILES
        except Exception:
            return self._DEFAULT_MAX_FILES

    def build_index(self, max_files: int = None, on_progress=None) -> dict:
        """Build the full code index.

        max_files: cap on files indexed. None reads ``index_max_files``
            from .c3/config.json (default 2000). Traversal continues past
            the cap only to count what was left out, so callers can report
            "indexed N of M" instead of silently truncating.
        on_progress: callable(entries_seen, files_indexed, chunks_created),
            invoked during the scan (directory granularity plus per file).
        """
        if max_files is None:
            max_files = self._configured_max_files()

        self.documents = {}
        self.chunks = {}
        self.symbols = {}
        self._hashes = {}
        self._file_mtimes = {}
        self._search_cache: OrderedDict = OrderedDict()
        self._expand_cache = {}

        files_indexed = 0
        chunks_created = 0
        files_capped = 0
        scan_stats = {"entries": 0}

        def _report(entries_seen=None, _yielded=None):
            if entries_seen is not None:
                scan_stats["entries"] = entries_seen
            if on_progress is not None:
                try:
                    on_progress(scan_stats["entries"], files_indexed,
                                chunks_created)
                except Exception:
                    pass

        exclude_parts = None
        if self.exclude_prefixes and self._is_excluded is not None:
            def exclude_parts(parts, _c=self._is_excluded,
                              _p=self.exclude_prefixes):
                return _c(parts, _p)

        # Mask Guard: gate the per-file verdict on a single cheap check, so a
        # project with no mask rules pays nothing for this (docs/mask-guard.md
        # §2). When rules exist, masked files are indexed FROM THE VIEW — the
        # index must never hold content the agent is not allowed to read.
        try:
            from services import access_guard as _ag
            _mask_active = _ag.has_mask_rules(str(self.project_path))
        except Exception:
            _mask_active = False

        for fpath in iter_files(self.project_path, exts=self.code_exts,
                                skip_dirs=self.skip_dirs,
                                exclude_parts=exclude_parts,
                                on_progress=_report):
            if files_indexed >= max_files:
                # Walk on (cheap after pruning) to count coverage;
                # reading and chunking stop at the cap.
                files_capped += 1
                continue

            try:
                if _mask_active:
                    content = _masked_content(fpath, self.project_path)
                    if content is None:
                        # Masked but unrenderable — indexing the raw bytes
                        # would put pre-mask content into search snippets
                        # (docs/mask-guard.md §6, row 2). Skip it instead.
                        continue
                else:
                    content = fpath.read_text(encoding='utf-8',
                                              errors='replace')
            except Exception:
                continue

            rel_path = str(fpath.relative_to(self.project_path))
            self._ingest_file(rel_path, fpath, content)
            chunks_created = len(self.chunks)
            files_indexed += 1
            _report()

        self._finish_build()
        rows = self._save_index()
        self._lexical_ok = self._lexical_wanted() and rows > 0
        legacy_removed = self._store.remove_legacy_files()

        return {
            "files_indexed": files_indexed,
            "chunks_created": chunks_created,
            "unique_symbols": len(self.symbols),
            "index_path": str(self.index_dir),
            "entries_scanned": scan_stats["entries"],
            "files_capped": files_capped,
            "max_files": max_files,
            "cooccurrence": getattr(self, "_cooccurrence_stats", {}),
            "lexical": {"engine": self.lexical_engine, "rows": rows},
            "store": str(self._store.path),
            "legacy_removed": legacy_removed,
            "mode": "full",
        }

    # ── Shared build steps ─────────────────────────────────────────────────

    def _ingest_file(self, doc_id: str, fpath: Path, content: str) -> list:
        """Register one file: document entry, chunks, hash, mtime. Returns its chunks."""
        self.documents[doc_id] = {
            "path": doc_id,
            "full_path": str(fpath),
            "lines": len(content.splitlines()),
            "tokens": count_tokens(content),
        }
        self._hashes[doc_id] = content_hash(content)
        file_chunks = self._chunk_file(content, fpath.suffix.lower(), doc_id)
        for chunk in file_chunks:
            self.chunks[chunk["id"]] = chunk
        try:
            self._file_mtimes[doc_id] = os.path.getmtime(str(fpath))
        except Exception:
            pass
        return file_chunks

    def _rebuild_symbols(self) -> None:
        """``symbols`` (lowercased name -> chunk ids) is derived from chunks,
        never persisted."""
        symbols: dict = {}
        for chunk_id, chunk in self.chunks.items():
            name = chunk.get("name")
            if name:
                symbols.setdefault(name.lower(), []).append(chunk_id)
        self.symbols = symbols
        self._rebuild_symbol_tail()

    def _finish_build(self) -> None:
        """Derived structures after chunks changed: symbols, co-occurrence,
        and — only when FTS5 will not answer — the TF-IDF vectors."""
        self._rebuild_symbols()
        # TF-IDF is the fallback engine. It re-tokenizes every chunk, so it is
        # built eagerly only when FTS5 is unavailable; otherwise on demand.
        self.chunk_tfidf, self.idf = {}, {}
        if not self._lexical_wanted():
            self._build_tfidf()
        if self._cooccurrence_wanted():
            self._build_cooccurrence()
        else:
            self._cooccurrence = {}
            self._cooccurrence_stats = {"disabled": True, "chunks_skipped": 0,
                                        "pair_updates": 0, "budget_exhausted": False}
        self._search_cache = OrderedDict()
        self._expand_cache = {}

    def _ensure_tfidf(self) -> None:
        if not self.chunk_tfidf and self.chunks:
            self._build_tfidf()

    def _read_indexable(self, fpath: Path, mask_active: bool):
        try:
            if mask_active:
                return _masked_content(fpath, self.project_path)
            return fpath.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return None

    def _exclude_parts_fn(self):
        if self.exclude_prefixes and self._is_excluded is not None:
            def exclude_parts(parts, _c=self._is_excluded, _p=self.exclude_prefixes):
                return _c(parts, _p)
            return exclude_parts
        return None

    def refresh(self, paths=None, on_progress=None) -> dict:
        """Incremental update: re-chunk only files whose content hash changed.

        ``paths``: absolute or project-relative paths the caller knows changed
        (the watcher's list); a path that no longer exists drops its document.
        ``None`` walks the whole manifest and compares every file's hash —
        reading and hashing, not chunking and tokenizing, so it stays cheap.
        Falls back to :meth:`build_index` when no store exists yet, when only a
        legacy layout is on disk, or when the incremental write fails.
        """
        t0 = time.perf_counter()
        if not self.chunks and not self._load_index():
            result = self.build_index(on_progress=on_progress)
            return result
        if not self._store.exists():
            return self.build_index(on_progress=on_progress)

        exclude_parts = self._exclude_parts_fn()
        try:
            from services import access_guard as _ag
            mask_active = _ag.has_mask_rules(str(self.project_path))
        except Exception:
            mask_active = False

        candidates: list = []
        removed: list = []
        if paths is None:
            max_files = self._configured_max_files()
            seen = 0
            for fpath in iter_files(self.project_path, exts=self.code_exts,
                                    skip_dirs=self.skip_dirs, exclude_parts=exclude_parts):
                if seen >= max_files:
                    break
                seen += 1
                candidates.append(fpath)
            current = {str(p.relative_to(self.project_path)) for p in candidates}
            removed = [d for d in self.documents if d not in current]
        else:
            root = self.project_path.resolve()
            for raw in paths:
                fp = Path(raw)
                if not fp.is_absolute():
                    fp = self.project_path / fp
                try:
                    rel = str(fp.resolve().relative_to(root))
                except (ValueError, OSError):
                    continue
                fp = self.project_path / rel
                if fp.is_file() and fp.suffix.lower() in self.code_exts:
                    parts = Path(rel).parts
                    if any(part in self.skip_dirs for part in parts[:-1]):
                        continue
                    if exclude_parts is not None and exclude_parts(parts):
                        continue
                    candidates.append(fp)
                elif rel in self.documents:
                    removed.append(rel)

        by_doc: dict = {}
        for cid, chunk in self.chunks.items():
            by_doc.setdefault(chunk.get("doc_id"), []).append(cid)

        upserts: dict = {}
        changed = added = unchanged = skipped = 0
        for fpath in candidates:
            rel = str(fpath.relative_to(self.project_path))
            content = self._read_indexable(fpath, mask_active)
            if content is None:
                skipped += 1
                continue
            digest = content_hash(content)
            if rel in self.documents and self._hashes.get(rel) == digest:
                unchanged += 1
                continue
            existed = rel in self.documents
            for cid in by_doc.pop(rel, []):
                self.chunks.pop(cid, None)
            file_chunks = self._ingest_file(rel, fpath, content)
            upserts[rel] = (self.documents[rel], [(c["id"], c) for c in file_chunks])
            if existed:
                changed += 1
            else:
                added += 1
            if on_progress is not None:
                try:
                    on_progress(len(candidates), changed + added, len(self.chunks))
                except Exception:
                    pass
        for rel in removed:
            for cid in by_doc.pop(rel, []):
                self.chunks.pop(cid, None)
            self.documents.pop(rel, None)
            self._hashes.pop(rel, None)
            self._file_mtimes.pop(rel, None)

        rows = 0
        if upserts or removed:
            self._finish_build()
            try:
                rows = self._store.apply(upserts, removed, self._file_mtimes, self._hashes,
                                         meta=self._store_meta())
            except Exception as exc:
                log.warning("incremental index write failed (%s); rebuilding", exc)
                return self.build_index(on_progress=on_progress)
            self._lexical_ok = self._lexical_wanted() and bool(self.chunks)

        return {
            "mode": "incremental",
            "files_checked": len(candidates),
            "files_changed": changed,
            "files_added": added,
            "files_removed": len(removed),
            "files_unchanged": unchanged,
            "files_skipped": skipped,
            "chunks_written": rows,
            "chunks_total": len(self.chunks),
            "files_indexed": len(self.documents),
            "seconds": round(time.perf_counter() - t0, 3),
            "lexical": {"engine": self.lexical_engine},
        }

    def _store_meta(self) -> dict:
        return {"cooccurrence": getattr(self, "_cooccurrence_stats", {}),
                "cooccurrence_map": self._cooccurrence}

    def _chunk_file(self, content: str, ext: str, doc_id: str) -> list:
        """Split a file into meaningful chunks (functions, classes, blocks)."""
        lines = content.split('\n')
        chunks = []

        try:
            from services.parser import extract_sections_ast
            ast_sections = extract_sections_ast(content, ext)
            if ast_sections:
                ast_chunks = self._chunk_by_ast(ast_sections, lines, doc_id)
                if ast_chunks:
                    return ast_chunks
        except Exception:
            pass

        # Try structural chunking first
        if ext in ('.py', '.r', '.R'):
            chunks = self._chunk_by_indent(lines, doc_id, ext)
        elif ext in ('.js', '.ts', '.tsx', '.jsx'):
            chunks = self._chunk_by_braces(lines, doc_id, ext)

        # Fallback: fixed-size chunks with overlap
        if not chunks:
            chunks = self._chunk_fixed(lines, doc_id, chunk_size=40, overlap=10)

        return chunks

    def _chunk_by_ast(self, sections: list, lines: list, doc_id: str) -> list:
        chunks = []
        from core import count_tokens

        def process_section(sec, parent_name=""):
            name = sec.get("name", "unnamed")
            full_name = f"{parent_name}.{name}" if parent_name else name
            start = sec["line_start"] - 1 # 0-indexed
            end = sec["line_end"] - 1
            chunk_content = '\n'.join(lines[start:end+1])

            if sec.get("type") != "import":
                chunks.append({
                    "id": f"{doc_id}::{full_name}",
                    "doc_id": doc_id,
                    "content": chunk_content,
                    "tokens": count_tokens(chunk_content),
                    "type": sec.get("type", "block"),
                    "name": full_name,
                    "line_start": start,
                    "line_end": end,
                })

            for child in sec.get("children", []):
                process_section(child, full_name)

        for sec in sections:
            process_section(sec)

        return chunks

    def _chunk_by_indent(self, lines: list, doc_id: str, ext: str) -> list:
        """Chunk Python/R files by definitions, including class methods."""
        chunks = []
        current_chunk = []
        current_name = None
        current_type = None
        chunk_start = 0
        class_stack = []

        def flush_chunk(end_index: int):
            nonlocal current_chunk, chunk_start, current_name, current_type
            if not current_chunk:
                return
            content = '\n'.join(current_chunk)
            chunks.append({
                "id": f"{doc_id}::{current_name or f'block_{chunk_start}'}",
                "doc_id": doc_id,
                "content": content,
                "tokens": count_tokens(content),
                "type": current_type or "block",
                "name": current_name,
                "line_start": chunk_start,
                "line_end": end_index,
            })
            current_chunk = []

        for i, line in enumerate(lines):
            stripped = line.rstrip()
            lstripped = line.lstrip()
            indent = len(line) - len(lstripped)

            while class_stack and indent <= class_stack[-1][0] and lstripped:
                class_stack.pop()

            # Detect definitions
            is_definition = False
            name = None
            ctype = None

            if ext == '.py':
                m = re.match(r'^(class|(?:async\s+)?def)\s+(\w+)', lstripped)
                if m:
                    is_definition = True
                    ctype = 'class' if m.group(1) == 'class' else 'function'
                    name = m.group(2)
                    if ctype == 'function' and class_stack:
                        name = f"{class_stack[-1][1]}.{name}"
            elif ext in ('.r', '.R'):
                m = re.match(r'^(\w+)\s*<-\s*function', lstripped)
                if m:
                    is_definition = True
                    ctype = 'function'
                    name = m.group(1)

            if is_definition and current_chunk:
                flush_chunk(i - 1)
                chunk_start = i

            current_chunk.append(stripped)
            if is_definition:
                current_name = name
                current_type = ctype
                if ctype == 'class' and ext == '.py':
                    class_stack.append((indent, m.group(2)))

        # Save last chunk
        flush_chunk(len(lines) - 1)

        return chunks

    def _chunk_by_braces(self, lines: list, doc_id: str, ext: str) -> list:
        """Chunk JS/TS files by top-level declarations."""
        chunks = []
        current_chunk = []
        current_name = None
        current_type = None
        chunk_start = 0
        brace_depth = 0

        for i, line in enumerate(lines):
            stripped = line.rstrip()
            current_chunk.append(stripped)

            # Track brace depth
            brace_depth += stripped.count('{') - stripped.count('}')

            # Detect top-level declarations at depth 0
            if brace_depth <= 0:
                m = re.match(
                    r'^(?:export\s+)?(?:default\s+)?(?:async\s+)?'
                    r'(?:function|class|const|let|var|interface|type|enum)\s+(\w+)',
                    stripped
                )
                if m and len(current_chunk) > 1:
                    name = m.group(1)
                    # Save accumulated chunk
                    prev_lines = current_chunk[:-1]
                    if prev_lines:
                        chunks.append({
                            "id": f"{doc_id}::{current_name or f'block_{chunk_start}'}",
                            "doc_id": doc_id,
                            "content": '\n'.join(prev_lines),
                            "tokens": count_tokens('\n'.join(prev_lines)),
                            "type": current_type or "block",
                            "name": current_name,
                            "line_start": chunk_start,
                            "line_end": i - 1,
                        })
                    current_chunk = [stripped]
                    current_name = name
                    current_type = "declaration"
                    chunk_start = i
                    brace_depth = stripped.count('{') - stripped.count('}')

        if current_chunk:
            chunks.append({
                "id": f"{doc_id}::{current_name or f'block_{chunk_start}'}",
                "doc_id": doc_id,
                "content": '\n'.join(current_chunk),
                "tokens": count_tokens('\n'.join(current_chunk)),
                "type": current_type or "block",
                "name": current_name,
                "line_start": chunk_start,
                "line_end": len(lines) - 1,
            })

        return chunks

    def _chunk_fixed(self, lines: list, doc_id: str,
                     chunk_size: int = 40, overlap: int = 10) -> list:
        """Fixed-size chunking with overlap."""
        chunks = []
        for i in range(0, len(lines), chunk_size - overlap):
            chunk_lines = lines[i:i + chunk_size]
            if not any(l.strip() for l in chunk_lines):
                continue
            chunks.append({
                "id": f"{doc_id}::chunk_{i}",
                "doc_id": doc_id,
                "content": '\n'.join(chunk_lines),
                "tokens": count_tokens('\n'.join(chunk_lines)),
                "type": "block",
                "name": None,
                "line_start": i,
                "line_end": min(i + chunk_size, len(lines)) - 1,
            })
        return chunks

    def _tokenize(self, text: str) -> list:
        """Code tokens: identifiers verbatim plus their parts, digits kept.

        Shared with the FTS5 engine (services/lexical_index.tokenize_code) so
        both paths agree on what a term is. The previous ``[a-zA-Z]{2,}``
        tokenizer turned ``sha256`` into ``sha`` and lost ``v2`` and ``S256``
        entirely.
        """
        return tokenize_code(text)

    def _expand_query_tokens(self, query: str) -> tuple:
        """Expand query with synonyms + return phrase bigrams.

        Returns (expanded_tokens, bigrams). Memoized per query string —
        repeat searches (agentic flows) skip re-expansion.
        Bigrams are returned (not stored on self) so concurrent searches
        don't race on shared state.
        """
        cached = self._expand_cache.get(query)
        if cached is not None:
            return cached

        base_tokens = tokenize_code(query, dedupe=True)
        if not base_tokens:
            self._expand_cache[query] = ([], [])
            if len(self._expand_cache) > 256:
                self._expand_cache.pop(next(iter(self._expand_cache)))
            return [], []

        expanded = list(base_tokens)
        seen = set(base_tokens)
        for token in base_tokens:
            # Configured synonyms only (`search_synonyms` in .c3/config.json).
            # The map that used to live here was C3's own vocabulary
            # (registry->profile->ide, delegate->ollama) and shipped to every
            # project.
            for related in self._synonyms.get(token, []):
                if related not in seen:
                    expanded.append(related)
                    seen.add(related)
            # Co-occurrence synonyms (learned from index)
            if token in self._cooccurrence:
                for related in list(self._cooccurrence[token].keys())[:3]:
                    if related not in seen:
                        expanded.append(related)
                        seen.add(related)

        # Bigrams for phrase matching — returned to caller, never stored on self
        # (thread-safety: concurrent searches would race on a shared attribute).
        bigrams = [
            (base_tokens[i], base_tokens[i + 1])
            for i in range(len(base_tokens) - 1)
        ]
        self._expand_cache[query] = (expanded, bigrams)
        if len(self._expand_cache) > 256:
            self._expand_cache.pop(next(iter(self._expand_cache)))
        return expanded, bigrams

    def _score_chunk(self, chunk_id: str, query: str, query_tokens: list,
                     query_bigrams: list = None, max_mtime: float = 0.0) -> float:
        """Combine TF-IDF relevance with path/name heuristics, bigrams, and recency.

        ``max_mtime`` is computed once per query by :meth:`search`; before it
        was a parameter this method recomputed ``max()`` over every file for
        every chunk scored.
        """
        tfidf = self.chunk_tfidf.get(chunk_id, {})
        chunk = self.chunks[chunk_id]
        doc_id = chunk["doc_id"]
        path_lower = doc_id.lower()
        name_lower = (chunk.get("name") or "").lower()
        path_parts = [part for part in re.split(r'[/\\._-]+', path_lower) if part]
        chunk_tokens = chunk.get("tokens")
        if not chunk_tokens:
            chunk_tokens = count_tokens(chunk["content"])
            chunk["tokens"] = chunk_tokens

        score = sum(tfidf.get(qt, 0) for qt in query_tokens)
        if score <= 0:
            return 0.0

        for qt in query_tokens:
            if chunk.get("name"):
                if qt == name_lower:
                    score *= 3.2
                elif qt in name_lower:
                    score *= 1.7

            if qt in path_parts:
                score += 1.5
            elif qt in path_lower:
                score += 0.75

        # Cache lowercased content + tokens per-chunk. Before this cache,
        # the tokenizer ran for every chunk on every search — dominant hot-path cost.
        content_lower = chunk.get("_content_lower")
        if content_lower is None:
            content_lower = (chunk.get("content") or "").lower()
            chunk["_content_lower"] = content_lower

        query_lower = query.lower()
        if query_lower and query_lower in content_lower:
            score *= 2.0

        query_joined = query_lower.replace(" ", "_")
        if len(query_joined) > 3 and query_joined in content_lower:
            score *= 1.8

        if any(part in query_lower for part in path_parts[:2]):
            score += 0.4

        # Bigram scoring — bigrams are passed in (not read from self) so the
        # search path is thread-safe. Chunk tokens are cached after first use.
        if query_bigrams:
            chunk_content_tokens = chunk.get("_content_tokens")
            if chunk_content_tokens is None:
                chunk_content_tokens = self._tokenize(content_lower)
                chunk["_content_tokens"] = chunk_content_tokens
            for t1, t2 in query_bigrams:
                for j in range(len(chunk_content_tokens) - 1):
                    if chunk_content_tokens[j] == t1 and chunk_content_tokens[j + 1] == t2:
                        score *= 1.5
                        break

        # Recency bias — recently modified files get a small boost (kept weak:
        # "recent" is not reliably "relevant").
        mtime = self._file_mtimes.get(doc_id, 0)
        if mtime > 0 and max_mtime > 0:
            age_ratio = mtime / max_mtime  # 1.0 for newest, lower for older
            score *= (0.9 + 0.2 * age_ratio)  # up to 1.1x for newest files

        score += min(len(path_parts), 6) * 0.02
        size_penalty = 1.0 + max(0.0, chunk_tokens - 450) / 1200.0
        return score / size_penalty

    def _build_tfidf(self):
        """Build TF-IDF scores for all chunks."""
        N = len(self.chunks)
        if N == 0:
            return

        # Document frequency
        df = Counter()
        chunk_tf = {}

        for chunk_id, chunk in self.chunks.items():
            tokens = self._tokenize(chunk["content"])
            # Include file path tokens
            tokens += self._tokenize(chunk["doc_id"])
            if chunk.get("name"):
                tokens += self._tokenize(chunk["name"]) * 3  # Boost symbol names

            tf = Counter(tokens)
            chunk_tf[chunk_id] = tf
            for term in set(tokens):
                df[term] += 1

        # IDF
        self.idf = {term: math.log(N / (1 + freq)) for term, freq in df.items()}

        # TF-IDF per chunk
        self.chunk_tfidf = {}
        for chunk_id, tf in chunk_tf.items():
            self.chunk_tfidf[chunk_id] = {}
            max_tf = max(tf.values()) if tf else 1
            for term, freq in tf.items():
                normalized_tf = 0.5 + 0.5 * (freq / max_tf)
                self.chunk_tfidf[chunk_id][term] = normalized_tf * self.idf.get(term, 0)

    def _build_cooccurrence(self):
        """Build lightweight co-occurrence map from indexed chunks for auto-synonyms.

        Bounded on purpose: the pair loop is quadratic in a chunk's unique
        token count, so a single minified bundle (798 unique tokens x 4091
        chunks = 1.9e9 pair updates, measured on a real project) can hang a
        build that has nothing else wrong with it. Skipped chunks and an
        exhausted budget are recorded in ``self._cooccurrence_stats`` and
        returned by :meth:`build_index`.
        """
        self._cooccurrence = {}
        skipped = 0
        updates = 0
        budget_hit = False
        for chunk in self.chunks.values():
            # Sorted, not a bare set: Counter insertion order decides ties in
            # most_common() below, and set order follows the per-process hash
            # seed. Unsorted, the same repo yielded different synonyms — and
            # different ranks — from one process to the next.
            tokens = sorted(set(self._tokenize(chunk["content"])))
            if len(tokens) > _COOC_MAX_CHUNK_TOKENS:
                skipped += 1
                continue
            if updates + len(tokens) * (len(tokens) - 1) > _COOC_MAX_PAIR_UPDATES:
                budget_hit = True
                break
            for t in tokens:
                if t not in self._cooccurrence:
                    self._cooccurrence[t] = Counter()
                for t2 in tokens:
                    if t != t2:
                        self._cooccurrence[t][t2] += 1
            updates += len(tokens) * (len(tokens) - 1)
        self._cooccurrence_stats = {
            "chunks_skipped": skipped,
            "pair_updates": updates,
            "budget_exhausted": budget_hit,
        }
        # Prune: keep only top-5 co-occurring terms per token (minimum 3 co-occurrences)
        pruned = {}
        for term, counts in self._cooccurrence.items():
            # Ties broken alphabetically so the pruned map is identical on
            # every machine and every run (see the sorted() above).
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            top = [(t, c) for t, c in ranked[:5] if c >= 3]
            if top:
                pruned[term] = dict(top)
        self._cooccurrence = pruned

    def _cooccurrence_wanted(self) -> bool:
        """Constructor flag wins; else ``search_cooccurrence_synonyms`` in
        .c3/config.json; default off."""
        if self._cooccurrence_enabled is not None:
            return bool(self._cooccurrence_enabled)
        try:
            cfg = json.loads((self.project_path / '.c3' / 'config.json')
                             .read_text(encoding='utf-8'))
            return bool(cfg.get('search_cooccurrence_synonyms', False))
        except Exception:
            return False

    def _rebuild_symbol_tail(self):
        """``symbols`` keys are dotted (``invoice.compute_total``); agents ask
        for the tail. Derived, never persisted."""
        tail: dict = {}
        for name, chunk_ids in self.symbols.items():
            tail.setdefault(name.rsplit(".", 1)[-1], []).extend(chunk_ids)
        self._symbol_tail = tail

    def _exact_symbol_ids(self, query: str) -> set:
        """Chunks whose symbol name IS the query (one identifier, any case).

        These rank ahead of every scored chunk: an agent typing a symbol
        name wants its definition, not the chunk that mentions it most.
        """
        q = query.strip().lower()
        if not q or " " in q:
            return set()
        ids = set(self.symbols.get(q, ()))
        ids.update(self._symbol_tail.get(q, ()))
        return ids

    def _window_chunk(self, chunk: dict, query_tokens: list, budget_tokens: int):
        """Budget-sized window of an oversized chunk.

        Anchored a few lines above the first line that mentions a query term
        (the class header, for a class-name query), grown downward until the
        window would exceed ``min(_WINDOW_MAX_TOKENS, budget)``, and closed
        with a note naming the full range so the agent can ``c3_read`` it.
        Returns None when nothing fits.
        """
        lines = (chunk.get("content") or "").split("\n")
        if not lines or budget_tokens < 40:
            return None
        target = max(40, min(_WINDOW_MAX_TOKENS, budget_tokens))
        terms = [t for t in query_tokens if t]
        anchor = 0
        for i, line in enumerate(lines):
            low = line.lower()
            if any(t in low for t in terms):
                anchor = i
                break
        start = max(0, anchor - 3)
        end, used = start, 0
        while end < len(lines):
            cost = max(1, len(lines[end]) // 4)
            if used + cost > target and end > start:
                break
            used += cost
            end += 1
        window = lines[start:end]
        base = int(chunk.get("line_start") or 0)
        note_len = 40  # reserve for the trailer
        text = "\n".join(window)
        tokens = count_tokens(text)
        while tokens + note_len > budget_tokens and len(window) > 1:
            window = window[: max(1, len(window) * 3 // 4)]
            text = "\n".join(window)
            tokens = count_tokens(text)
        if tokens + note_len > budget_tokens:
            return None
        abs_start, abs_end = base + start, base + start + len(window) - 1
        note = (f"[window L{abs_start}-{abs_end} of {chunk.get('type', 'chunk')} "
                f"L{chunk.get('line_start')}-{chunk.get('line_end')}, "
                f"{chunk.get('tokens')} tok; c3_read(lines=...) for the rest]")
        content = text + "\n" + note
        return {"content": content, "line_start": abs_start, "line_end": abs_end,
                "tokens": count_tokens(content)}

    def _rank_key(self, exact_ids: set):
        """Exact symbol matches first; then score; then named/structural
        chunks and shorter paths on ties."""
        def key(item):
            chunk = self.chunks[item[0]]
            return (
                1 if item[0] in exact_ids else 0,
                item[1],
                1 if chunk.get("name") else 0,
                1 if chunk.get("type") in {"function", "class", "method", "declaration"} else 0,
                -len(chunk["doc_id"]),
            )
        return key

    @staticmethod
    def _additive_boosts(chunk: dict, base_tokens: list) -> float:
        """Small, capped, additive: BM25 already weights the symbol and path
        columns, so these only settle near-ties in favour of a chunk whose
        whole name or path the query spelled out."""
        boost = 0.0
        name_tokens = set(tokenize_code(chunk.get("name") or ""))
        if name_tokens and name_tokens <= set(base_tokens):
            boost += 0.25
        doc_id = chunk.get("doc_id") or ""
        path_tokens = set(tokenize_code(doc_id.replace("\\", "/").replace("/", " ").replace(".", " ")))
        hits = sum(1 for t in base_tokens if t in path_tokens)
        boost += min(0.15, 0.05 * hits)
        return boost

    def _recency_factor(self, doc_id: str, max_mtime: float) -> float:
        mtime = self._file_mtimes.get(doc_id, 0)
        if mtime > 0 and max_mtime > 0:
            return 0.9 + 0.2 * (mtime / max_mtime)
        return 1.0

    def _rank_lexical(self, query_tokens: list, base_tokens: list, filters: Filters,
                      exact_ids: set, max_mtime: float, want: int):
        """FTS5/BM25 candidates -> normalised score + boosts + intent prior.

        Returns None when the engine produced nothing usable, so the caller
        can fall back to the TF-IDF scan for this query.
        """
        allowed_docs = None
        if filters.paths:
            allowed_docs = [d for d in self.documents if filters.path_ok(d)]
        try:
            candidates = self._store.search(query_tokens, limit=max(40, want * 8),
                                            filters=filters, allowed_docs=allowed_docs)
        except Exception:
            return None
        if not candidates and not exact_ids:
            return []
        best = max((s for _, s in candidates), default=0.0)
        scores = {}
        for cid, raw in candidates:
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            doc_id = chunk["doc_id"]
            if not filters.chunk_ok(doc_id, chunk.get("type") or ""):
                continue
            score = (raw / best) if best > 0 else 0.0
            score += self._additive_boosts(chunk, base_tokens)
            score += intent_prior(base_tokens, doc_kind(doc_id))
            score *= self._recency_factor(doc_id, max_mtime)
            scores[cid] = score
        for cid in exact_ids:
            scores.setdefault(cid, 0.001)
        return sorted(scores.items(), key=self._rank_key(exact_ids), reverse=True)

    def _rank_tfidf(self, query: str, query_tokens: list, query_bigrams: list,
                    filters: Filters, exact_ids: set, max_mtime: float):
        """The pre-FTS5 scorer: a linear scan over every chunk."""
        self._ensure_tfidf()
        scores = {}
        for chunk_id in self.chunk_tfidf:
            chunk = self.chunks.get(chunk_id)
            if chunk is None or not filters.chunk_ok(chunk["doc_id"], chunk.get("type") or ""):
                continue
            score = self._score_chunk(chunk_id, query, query_tokens, query_bigrams,
                                      max_mtime=max_mtime)
            if score > 0:
                scores[chunk_id] = score
        for cid in exact_ids:
            scores.setdefault(cid, 0.001)
        return sorted(scores.items(), key=self._rank_key(exact_ids), reverse=True)

    def search(self, query: str, top_k: int = 5, max_tokens: int = 4000,
               include_content: bool = True, path=None, lang=None, kind=None,
               fusion: bool = True) -> list:
        """Search the index and return most relevant chunks.

        Set include_content=False to get metadata only (saves ~70% tokens).
        A chunk whose symbol name equals the query ranks first; a chunk too
        large for the budget is returned as a window (``windowed: True``)
        instead of being skipped. ``path`` / ``lang`` / ``kind`` narrow the
        candidates (services/lexical_index.Filters). ``fusion=False`` returns
        the lexical ranking alone even when a dense backend is attached.
        """
        if not self.chunks:
            self._load_index()
            if not self.chunks:
                return []

        filters = Filters(path=path, lang=lang, kind=kind)
        fuse = bool(fusion) and self._dense_ready()
        rerank_now = self._rerank_ready()
        cache_key = (query, int(top_k), int(max_tokens), bool(include_content), filters.key(), fuse, rerank_now)
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            self._search_cache.move_to_end(cache_key)
            return [dict(item) for item in cached]

        query_tokens, query_bigrams = self._expand_query_tokens(query)
        if not query_tokens:
            return []
        base_tokens = tokenize_code(query, dedupe=True)

        max_mtime = max(self._file_mtimes.values()) if self._file_mtimes else 0.0
        exact_ids = {
            cid for cid in self._exact_symbol_ids(query)
            if cid in self.chunks
            and filters.chunk_ok(self.chunks[cid]["doc_id"], self.chunks[cid].get("type") or "")
        }

        ranked = None
        if self._lexical_ok:
            ranked = self._rank_lexical(query_tokens, base_tokens, filters, exact_ids,
                                        max_mtime, want=top_k)
        if ranked is None:
            ranked = self._rank_tfidf(query, query_tokens, query_bigrams, filters,
                                      exact_ids, max_mtime)
        if fuse:
            ranked = self._fuse_dense(query, ranked, filters, exact_ids, want=top_k)
        if rerank_now:
            ranked = self._apply_rerank(query, base_tokens, ranked, exact_ids)

        # Collect results up to token budget
        results = []
        token_budget = max_tokens
        seen_docs = set()

        for chunk_id, score in ranked[:top_k * 4]:
            chunk = self.chunks[chunk_id]
            chunk_tokens = chunk.get("tokens") or count_tokens(chunk["content"])
            window = None

            if chunk_tokens > token_budget:
                window = self._window_chunk(chunk, query_tokens, token_budget)
                if window is None:
                    continue
                chunk_tokens = window["tokens"]

            if chunk["doc_id"] in seen_docs and len(results) >= max(2, top_k // 2):
                continue

            doc = self.documents.get(chunk["doc_id"], {})
            line_start = window["line_start"] if window else chunk['line_start']
            line_end = window["line_end"] if window else chunk['line_end']
            result = {
                "chunk_id": chunk_id,
                "file": chunk["doc_id"],
                "name": chunk.get("name"),
                "type": chunk["type"],
                "lines": f"{line_start}-{line_end}",
                "tokens": chunk_tokens,
                "file_tokens": doc.get("tokens", chunk_tokens),
                "score": round(score, 3),
            }
            if window:
                result["windowed"] = True
            if chunk_id in exact_ids:
                # Callers that filter on relative score must keep this one:
                # a windowed class scores low on TF-IDF and is still the
                # definition the query named.
                result["exact_symbol"] = True
            if include_content:
                result["content"] = window["content"] if window else chunk["content"]

            results.append(result)

            token_budget -= chunk_tokens
            seen_docs.add(chunk["doc_id"])

            if len(results) >= top_k or token_budget <= 0:
                break

        self._search_cache[cache_key] = [dict(item) for item in results]
        if len(self._search_cache) > self._search_cache_max:
            self._search_cache.popitem(last=False)
        return results

    def get_context(self, query: str, top_k: int = 5, max_tokens: int = 4000) -> str:
        """Get a formatted context string ready to pass to Claude."""
        results = self.search(query, top_k, max_tokens)

        if not results:
            return "No relevant code found in index."

        sections = []
        total_tokens = 0

        for r in results:
            section = f"## {r['file']} (L{r['lines']})"
            if r['name']:
                section += f" — {r['name']}"
            section += f"\n```\n{r['content']}\n```"
            sections.append(section)
            total_tokens += r['tokens']

        header = f"# Relevant Code Context ({total_tokens} tokens, {len(results)} chunks)\n"
        return header + '\n\n'.join(sections)

    def _save_index(self) -> int:
        """Write the whole in-memory index to the SQLite store (atomic swap).
        Returns chunk rows written."""
        return self._store.write_all(self.documents, self.chunks, self._file_mtimes,
                                     self._hashes, meta=self._store_meta())

    def _load_index(self) -> bool:
        """Load the index from the SQLite store.

        A pre-2.107.0 layout (index.json, or the 2.106.0 lexical.sqlite side
        table) has no store to load: it is rebuilt, and the legacy files are
        removed once the store is written.
        """
        data = self._store.load()
        if data is None:
            if self.needs_migration():
                log.info("legacy index layout in %s; rebuilding into %s",
                         self.index_dir, self._store.path.name)
                self.build_index()
                return bool(self.chunks)
            return False
        self.documents = data["documents"]
        self.chunks = data["chunks"]
        self._file_mtimes = data["mtimes"]
        self._hashes = data["hashes"]
        meta = data.get("meta") or {}
        self._cooccurrence = meta.get("cooccurrence_map") or {}
        self._cooccurrence_stats = meta.get("cooccurrence") or {}
        self._rebuild_symbols()
        # TF-IDF vectors are not persisted: built on demand by the fallback path.
        self.chunk_tfidf, self.idf = {}, {}
        self._lexical_ok = self._lexical_wanted() and data.get("fts_rows", 0) > 0
        self._search_cache = OrderedDict()
        self._expand_cache = {}
        return True

    def get_stats(self) -> dict:
        """Get index statistics."""
        if not self.documents:
            self._load_index()

        total_tokens = sum(d.get("tokens", 0) for d in self.documents.values())
        return {
            "files_indexed": len(self.documents),
            "total_chunks": len(self.chunks),
            "total_tokens_in_codebase": total_tokens,
            "unique_symbols": len(self.symbols),
            "lexical_engine": self.lexical_engine,
            "index_size_kb": round(self._store.size_bytes() / 1024, 1),
        }
