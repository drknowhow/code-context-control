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
import time
from pathlib import Path

log = logging.getLogger("c3.embedding_index")


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
        self._file_hashes: dict[str, str] = {}  # doc_id -> content hash
        self._lock = threading.Lock()
        self._chunk_map: dict[str, dict] = {}  # chunk_id -> metadata

        self._init_backends()
        self._load_hashes()

    # ── Backend init ──────────────────────────────────────

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
            self._ollama_ok = (
                self.ollama.is_available(timeout=2)
                and self.ollama.has_model(self.embed_model)
            )
        except Exception:
            self._ollama_ok = False

    @property
    def ready(self) -> bool:
        """True when both chromadb and Ollama embeddings are available."""
        return self._available and self._ollama_ok

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

    def build(self, code_index, force: bool = False) -> dict:
        """Build or incrementally update the embedding index from CodeIndex chunks.

        Args:
            code_index: A CodeIndex instance with populated chunks/documents.
            force: If True, re-embed all files regardless of hash.

        Returns:
            Stats dict with files_processed, chunks_embedded, chunks_skipped, etc.
        """
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

        with self._lock:
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

            self._save_hashes()

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
        """Remove all embedded chunks belonging to a file."""
        if not self._collection:
            return
        try:
            self._collection.delete(where={"doc_id": doc_id})
        except Exception:
            # Some chromadb versions don't support where-delete well;
            # fall back to getting IDs first
            try:
                results = self._collection.get(where={"doc_id": doc_id})
                if results and results.get("ids"):
                    self._collection.delete(ids=results["ids"])
            except Exception:
                pass

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
