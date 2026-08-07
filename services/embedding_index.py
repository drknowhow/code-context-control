"""
Incremental Embedding Index for semantic code search.

Embeds code chunks from CodeIndex into a chromadb collection using Ollama
embeddings. Tracks file content hashes to only re-embed changed files.
Falls back gracefully when Ollama or chromadb are unavailable.
"""

import hashlib
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("c3.embedding_index")
_SEARCH_INIT_WAIT_SECONDS = 0.25
# Upper bound on how long a caller will park waiting for an in-flight build.
# A redundant build is worth far less than a responsive server, so we give up
# and degrade rather than block. See _acquire_build_lock().
_BUILD_LOCK_WAIT_SECONDS = 30.0


class EmbeddingIndex:
    """Semantic code search via embeddings over CodeIndex chunks."""

    def __init__(
        self,
        project_path: str,
        ollama_client,
        embed_model: str = "nomic-embed-text",
        batch_size: int = 32,
    ):
        self.project_path = Path(project_path)
        self.ollama = ollama_client
        self.embed_model = embed_model
        self.batch_size = batch_size

        self._index_dir = self.project_path / ".c3" / "embeddings"
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._hash_file = self._index_dir / "file_hashes.json"

        self._chroma_client = None
        self._collection = None
        self._available = False
        self._ollama_ok = False
        self._ollama_up = False
        self._model_ok = False
        self._file_hashes: dict[str, str] = {}  # doc_id -> content hash
        self._lock = threading.Lock()
        self._lock_warned = False  # WARN once, not once per blocked caller
        self._chunk_map: dict[str, dict] = {}  # chunk_id -> metadata

        # Heavy backend init (chromadb import/client + ollama probe) and hash
        # load are deferred to first use so build_runtime stays fast and the MCP
        # handshake doesn't time out. See _ensure_ready().
        self._initialized = False
        self._init_lock = threading.Lock()

    # ── Backend init ──────────────────────────────────────

    def _ensure_ready(self, wait_timeout: float | None = None) -> bool:
        """Lazily init chromadb/ollama backends + file hashes on first use.

        Deferred from __init__ so build_runtime (and the MCP handshake) stays
        fast. Idempotent and thread-safe via double-checked locking.
        """
        if self._initialized:
            return True
        if wait_timeout is None:
            acquired = self._init_lock.acquire()
        else:
            acquired = self._init_lock.acquire(timeout=max(0.0, wait_timeout))
        if not acquired:
            return False
        try:
            if self._initialized:
                return True
            self._init_backends()
            self._load_hashes()
            self._initialized = True
            return True
        finally:
            self._init_lock.release()

    def warm(self):
        """Pre-initialize backends (used for background warm-up)."""
        self._ensure_ready()

    def _init_backends(self):
        """Initialize chromadb collection and check Ollama."""
        try:
            import chromadb
            from chromadb.config import Settings

            persist_dir = str(self._index_dir / "chromadb")
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(
                path=persist_dir,
                settings=Settings(anonymized_telemetry=False),
            )
            self._collection = self._chroma_client.get_or_create_collection(
                name="code_embeddings",
                metadata={"hnsw:space": "cosine"},
            )
            self._available = True
        except Exception as e:
            log.debug("chromadb unavailable for embedding index: %s", e)
            self._available = False

        try:
            self._ollama_up = self.ollama.is_available(timeout=2)
            self._model_ok = (
                self._ollama_up and self.ollama.has_model(self.embed_model)
            )
        except Exception:
            self._ollama_up = False
            self._model_ok = False
        self._ollama_ok = self._ollama_up and self._model_ok

    @property
    def ready(self) -> bool:
        """True when both chromadb and Ollama embeddings are available."""
        return self._available and self._ollama_ok

    def probe(self) -> dict:
        """Explicitly initialize backends and report readiness.

        ``ready`` alone never triggers backend init (status reporters must
        stay cheap for build_runtime/MCP handshake), so gating a build on a
        fresh instance's ``ready`` always skipped. Init/CLI flows call this
        instead: it pays the one-time backend cost, then reports truthfully.
        """
        self._ensure_ready()
        return {
            "ready": self.ready,
            "chromadb": self._available,
            "ollama": self._ollama_up,
            "model": self._model_ok,
        }

    def unavailable_reason(self) -> str:
        """Human-readable reason ``ready`` is False (after probe/init)."""
        if not self._available:
            return "chromadb not installed"
        if not self._ollama_up:
            return "Ollama not reachable"
        if not self._model_ok:
            return (f"embed model '{self.embed_model}' not pulled "
                    f"(run: ollama pull {self.embed_model})")
        return ""

    # ── Hash tracking ─────────────────────────────────────

    def _load_hashes(self):
        """Load persisted file content hashes."""
        if self._hash_file.exists():
            try:
                with open(self._hash_file, encoding='utf-8') as f:
                    self._file_hashes = json.load(f)
            except Exception:
                self._file_hashes = {}

    def _save_hashes(self):
        """Persist file content hashes."""
        try:
            with open(self._hash_file, "w", encoding='utf-8') as f:
                json.dump(self._file_hashes, f)
        except Exception:
            pass

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode(errors="replace")).hexdigest()[:16]

    # ── Build / Update ────────────────────────────────────

    def _acquire_build_lock(self, timeout: float | None = None) -> bool:
        """Acquire the build lock with a bound. Mirrors ``_ensure_ready``.

        An unbounded ``with self._lock`` turns any slow backend call into a
        dead server: whoever holds the lock never returns, every later caller
        parks behind it forever, and the MCP client kills the tool call at its
        own timeout while the event loop still looks perfectly healthy. A
        bounded acquire degrades instead — the caller skips the embedding work
        and serves its request without it.

        Returns True when the lock is held (caller MUST release it).
        """
        wait = _BUILD_LOCK_WAIT_SECONDS if timeout is None else timeout
        if self._lock.acquire(timeout=max(0.0, wait)):
            return True
        if not self._lock_warned:
            self._lock_warned = True
            log.warning(
                "Embedding index build lock still held after %.1fs — skipping "
                "this build. Semantic search keeps serving whatever is already "
                "indexed; this is logged once per index instance.",
                wait,
            )
        return False

    def _busy_result(self) -> dict:
        """Build stats shaped like a normal return, marked degraded."""
        return {
            "error": "Embedding index busy (build already in flight); skipped",
            "available": True,
            "degraded": True,
            "files_processed": 0,
            "files_skipped": 0,
            "chunks_embedded": 0,
            "chunks_skipped": 0,
            "errors": 0,
            "total_embedded": 0,
        }

    def build(self, code_index, force: bool = False, on_progress=None) -> dict:
        """Build or incrementally update the embedding index from CodeIndex chunks.

        Args:
            code_index: A CodeIndex instance with populated chunks/documents.
            force: If True, re-embed all files regardless of hash.
            on_progress: callable(files_done, files_total, chunks_embedded),
                invoked per file (skipped files count as done).

        Returns:
            Stats dict with files_processed, chunks_embedded, chunks_skipped, etc.
        """
        self._ensure_ready()
        if not self.ready:
            return {"error": "Embedding backends unavailable", "available": False}

        if not code_index.chunks:
            code_index._load_index()
        if not code_index.chunks:
            return {"error": "No code index chunks found. Build code index first."}

        # Group chunks by doc_id (file)
        chunks_by_file: dict[str, list[tuple[str, dict]]] = {}
        for chunk_id, chunk in code_index.chunks.items():
            doc_id = chunk.get("doc_id", "")
            if doc_id:
                chunks_by_file.setdefault(doc_id, []).append((chunk_id, chunk))

        files_processed = 0
        chunks_embedded = 0
        chunks_skipped = 0
        files_skipped = 0
        errors = 0
        stale_ids = []
        files_total = len(chunks_by_file)

        def _report():
            if on_progress is not None:
                try:
                    on_progress(files_processed + files_skipped, files_total,
                                chunks_embedded)
                except Exception:
                    pass

        if not self._acquire_build_lock():
            return self._busy_result()
        try:
            # Detect deleted files — remove their embeddings
            indexed_files = set(self._file_hashes.keys())
            current_files = set(chunks_by_file.keys())
            for removed_file in indexed_files - current_files:
                self._remove_file_chunks(removed_file)
                del self._file_hashes[removed_file]

            for doc_id, file_chunks in chunks_by_file.items():
                # Check if file content changed
                content = "".join(c.get("content", "") for _, c in file_chunks)
                new_hash = self._content_hash(content)

                if not force and self._file_hashes.get(doc_id) == new_hash:
                    files_skipped += 1
                    chunks_skipped += len(file_chunks)
                    _report()
                    continue

                # Remove old chunks for this file before re-embedding
                self._remove_file_chunks(doc_id)

                # Batch embed
                batch_ids = []
                batch_texts = []
                batch_metas = []
                for chunk_id, chunk in file_chunks:
                    text = chunk.get("content", "").strip()
                    if not text or len(text) < 20:
                        chunks_skipped += 1
                        continue

                    # Prefix with file path + symbol for richer embeddings
                    name = chunk.get("name", "")
                    prefix = f"File: {doc_id}"
                    if name:
                        prefix += f" | {chunk.get('type', 'symbol')}: {name}"
                    embed_text = f"{prefix}\n{text}"

                    batch_ids.append(chunk_id)
                    batch_texts.append(embed_text)
                    batch_metas.append({
                        "doc_id": doc_id,
                        "name": name or "",
                        "type": chunk.get("type", "chunk"),
                        "line_start": chunk.get("line_start", 0),
                        "line_end": chunk.get("line_end", 0),
                    })

                    if len(batch_ids) >= self.batch_size:
                        ok = self._embed_batch(batch_ids, batch_texts, batch_metas)
                        if ok:
                            chunks_embedded += len(batch_ids)
                        else:
                            errors += len(batch_ids)
                        batch_ids, batch_texts, batch_metas = [], [], []

                # Flush remaining batch
                if batch_ids:
                    ok = self._embed_batch(batch_ids, batch_texts, batch_metas)
                    if ok:
                        chunks_embedded += len(batch_ids)
                    else:
                        errors += len(batch_ids)

                self._file_hashes[doc_id] = new_hash
                files_processed += 1
                _report()

            self._save_hashes()
        finally:
            self._lock.release()

        return {
            "files_processed": files_processed,
            "files_skipped": files_skipped,
            "chunks_embedded": chunks_embedded,
            "chunks_skipped": chunks_skipped,
            "errors": errors,
            "total_embedded": self._collection.count() if self._collection else 0,
        }

    def _embed_batch(self, ids: list, texts: list, metas: list) -> bool:
        """Embed and store a batch of chunks. Returns True on success."""
        try:
            embeddings = self.ollama.embed_batch(texts, model=self.embed_model)
            if not embeddings or len(embeddings) != len(ids):
                return False
            self._collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metas,
            )
            return True
        except Exception as e:
            log.debug("Embedding batch failed: %s", e)
            return False

    def _remove_file_chunks(self, doc_id: str):
        """Remove all embedded chunks belonging to a file.

        Resolves the ids first and deletes by id. It never calls
        ``delete(where=...)``.

        ``collection.delete(where=...)`` has been observed to never return
        inside the chromadb Rust bindings (``chromadb/api/rust.py``,
        ``RustBindingsAPI._delete``): two py-spy dumps four minutes apart with
        byte-identical frames, 0.031s of CPU over 3s, and no writes to
        chroma.sqlite3 for ten hours. Because this is a *hang* and not an
        exception, the ``except``-guarded fallback that used to live here could
        never fire — the comment right below it already suspected the
        where-delete, but an ``except`` clause cannot catch a thread that never
        comes back. Worse, build() called this while holding the build lock, so
        one wedged delete took every later caller down with it.

        Deleting by explicit id keeps the metadata filtering on ``get()``,
        which does return, and leaves ``delete()`` with the one argument shape
        that has never been seen to stall.
        """
        if not self._collection:
            return
        try:
            try:
                # include=[] skips fetching documents/embeddings we throw away.
                results = self._collection.get(
                    where={"doc_id": doc_id}, include=[])
            except Exception:
                # Older chromadb (we support >=0.4.24) may reject include=[].
                results = self._collection.get(where={"doc_id": doc_id})
            ids = (results or {}).get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
        except Exception as e:
            log.debug("Removing chunks for %s failed: %s", doc_id, e)

    # ── Search ────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        max_tokens: int = 2000,
    ) -> list[dict]:
        """Semantic search over embedded code chunks.

        Returns list of dicts with: file, lines, name, type, content, score, tokens.
        """
        if not self._ensure_ready(wait_timeout=_SEARCH_INIT_WAIT_SECONDS):
            return []
        if not self.ready or not self._collection or self._collection.count() == 0:
            return []

        try:
            query_embedding = self.ollama.embed(query, model=self.embed_model)
            if not query_embedding:
                return []

            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k * 2, self._collection.count()),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            log.debug("Semantic search failed: %s", e)
            return []

        if not results or not results.get("ids") or not results["ids"][0]:
            return []

        ids = results["ids"][0]
        documents = results["documents"][0] if results.get("documents") else []
        metadatas = results["metadatas"][0] if results.get("metadatas") else []
        distances = results["distances"][0] if results.get("distances") else []

        from core import count_tokens

        output = []
        total_tokens = 0
        for i, chunk_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            dist = distances[i] if i < len(distances) else 1.0

            # chromadb cosine distance: 0 = identical, 2 = opposite
            score = max(0.0, 1.0 - dist)

            # Strip the prefix we added during embedding
            content = doc
            if "\n" in content:
                content = content.split("\n", 1)[1]

            tok = count_tokens(content)
            if total_tokens + tok > max_tokens and output:
                break

            line_start = meta.get("line_start", 0)
            line_end = meta.get("line_end", 0)
            lines_str = f"{line_start}-{line_end}" if line_start else "?"

            output.append({
                "file": meta.get("doc_id", "?"),
                "lines": lines_str,
                "name": meta.get("name", ""),
                "type": meta.get("type", "chunk"),
                "content": content,
                "score": round(score, 4),
                "tokens": tok,
            })
            total_tokens += tok

            if len(output) >= top_k:
                break

        return output

    # ── Stats ─────────────────────────────────────────────

    def get_stats(self) -> dict:
        count = self._collection.count() if self._collection else 0
        return {
            "ready": self.ready,
            "chromadb_available": self._available,
            "ollama_available": self._ollama_ok,
            "embed_model": self.embed_model,
            "total_embedded_chunks": count,
            "files_tracked": len(self._file_hashes),
        }
