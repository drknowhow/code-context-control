"""SLTM Vector Store with local fallback indexing."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services.ollama_client import OllamaClient
from services.text_index import TextIndex

SLTM_CATEGORIES = [
    "design_docs",
    "api_contracts",
    "bug_history",
    "terminal_summaries",
    "code_notes",
    "general",
]


class VectorStore:
    """Hybrid TF-IDF + vector search over categorized memory collections."""

    def __init__(self, project_path: str, config: dict | None = None):
        self.project_path = Path(project_path)
        self.config = config or {}
        self._lock = threading.Lock()

        base_url = self.config.get("ollama_base_url", "http://localhost:11434")
        self.ollama = OllamaClient(base_url)
        self.embed_model = self.config.get("embed_model", "nomic-embed-text")
        self.alpha = self.config.get("sltm_alpha", 0.5)

        self._chroma_client = None
        self._collections: dict[str, object] = {}
        self._chroma_available = False
        self._ollama_available = False

        self._fallback_dir = self.project_path / ".c3" / "sltm" / "fallback"
        self._fallback_dir.mkdir(parents=True, exist_ok=True)
        self._fallback_data: dict[str, list[dict]] = {}
        self._records_by_id: dict[str, dict] = {}
        self._text_index = TextIndex()

        # Heavy backend init (chromadb import/client + ollama probe) and the
        # fallback load are deferred to first use so build_runtime stays fast
        # and the MCP handshake doesn't time out. See _ensure_ready().
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure_ready(self):
        """Lazily init chromadb/ollama backends + fallback data on first use.

        Deferred from __init__ so build_runtime (and the MCP handshake) stays
        fast. Idempotent and thread-safe via double-checked locking.
        """
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._init_backends()
            self._load_fallback()
            self._initialized = True

    def warm(self):
        """Pre-initialize backends (used for background warm-up)."""
        self._ensure_ready()

    def _init_backends(self):
        if self.config.get("disable_vector_backend"):
            self._chroma_available = False
            self._ollama_available = False
            return
        try:
            import chromadb
            from chromadb.config import Settings

            persist_dir = str(self.project_path / ".c3" / "sltm" / "chromadb")
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(
                path=persist_dir,
                settings=Settings(anonymized_telemetry=False),
            )
            for cat in SLTM_CATEGORIES:
                self._collections[cat] = self._chroma_client.get_or_create_collection(
                    name=cat,
                    metadata={"hnsw:space": "cosine"},
                )
            self._chroma_available = True
        except Exception:
            self._chroma_available = False

        self._ollama_available = self.ollama.is_available(timeout=2) and self.ollama.has_model(self.embed_model)

    @property
    def vector_enabled(self) -> bool:
        return self._chroma_available and self._ollama_available

    def add(
        self,
        text: str,
        category: str = "general",
        metadata: dict | None = None,
        record_id: str | None = None,
    ) -> dict:
        """Store a record in the SLTM using a stable record id when provided."""
        self._ensure_ready()
        if category not in SLTM_CATEGORIES:
            category = "general"

        record_id = record_id or uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        meta = dict(metadata or {})
        meta.update({"timestamp": now, "category": category})
        record = {
            "id": record_id,
            "text": text,
            "metadata": meta,
            "timestamp": now,
            "category": category,
        }

        with self._lock:
            self._delete_locked(record_id)
            self._fallback_data.setdefault(category, []).append(record)
            self._save_fallback_category(category)
            self._records_by_id[record_id] = record
            self._text_index.add_or_update(record_id, self._searchable_text(record))

            if self.vector_enabled:
                try:
                    embedding = self.ollama.embed(text, model=self.embed_model)
                    if embedding:
                        flat_meta = {
                            key: value if isinstance(value, (int, float, bool)) else str(value)
                            for key, value in meta.items()
                        }
                        self._collections[category].add(
                            ids=[record_id],
                            embeddings=[embedding],
                            documents=[text],
                            metadatas=[flat_meta],
                        )
                except Exception:
                    pass

        return {
            "stored": True,
            "id": record_id,
            "category": category,
            "vector_indexed": self.vector_enabled,
            "total_records": len(self._records_by_id),
        }

    def search(self, query: str, category: str = "", top_k: int = 5) -> list[dict]:
        self._ensure_ready()
        categories = [category] if category and category in SLTM_CATEGORIES else list(SLTM_CATEGORIES)
        allowed = set(categories)

        docs = {
            doc_id: record
            for doc_id, record in self._records_by_id.items()
            if record.get("category") in allowed
        }
        if not docs:
            return []

        tfidf_scores = {
            doc_id: score
            for doc_id, score in self._text_index.search(query, top_k=max(top_k * 5, 20))
            if doc_id in docs
        }

        vector_scores: dict[str, float] = {}
        if self.vector_enabled:
            try:
                query_embedding = self.ollama.embed(query, model=self.embed_model)
                if query_embedding:
                    for cat in categories:
                        col = self._collections.get(cat)
                        if not col or col.count() == 0:
                            continue
                        results = col.query(
                            query_embeddings=[query_embedding],
                            n_results=min(max(top_k * 3, top_k), col.count()),
                        )
                        ids = (results or {}).get("ids") or []
                        distances = (results or {}).get("distances") or []
                        for i, rid in enumerate(ids[0] if ids else []):
                            dist = distances[0][i] if distances and distances[0] else 0
                            vector_scores[rid] = max(vector_scores.get(rid, 0.0), max(0.0, 1.0 - dist))
            except Exception:
                pass

        candidate_ids = set(tfidf_scores) | set(vector_scores)
        if not candidate_ids:
            return []

        max_tfidf = max(tfidf_scores.values()) if tfidf_scores else 1.0
        max_tfidf = max(max_tfidf, 0.001)
        min_score = float(self.config.get("sltm_min_score", 0.3))
        combined = []
        for doc_id in candidate_ids:
            if doc_id not in docs:
                continue
            tfidf_score = tfidf_scores.get(doc_id, 0.0) / max_tfidf
            vector_score = vector_scores.get(doc_id, 0.0)
            score = self.alpha * tfidf_score + (1 - self.alpha) * vector_score if vector_scores else tfidf_score
            if score >= min_score:
                combined.append((doc_id, score))
        combined.sort(key=lambda item: item[1], reverse=True)

        results = []
        for doc_id, score in combined[:top_k]:
            record = docs[doc_id]
            results.append({
                "id": doc_id,
                "text": record.get("text", ""),
                "category": record.get("category", "general"),
                "score": round(score, 4),
                "metadata": record.get("metadata", {}),
                "timestamp": record.get("timestamp", ""),
                "search_method": "hybrid" if vector_scores else "tfidf",
            })
        return results

    def delete(self, record_id: str) -> dict:
        self._ensure_ready()
        deleted = False
        with self._lock:
            deleted = self._delete_locked(record_id)
        return {"deleted": deleted, "id": record_id}

    def get_stats(self) -> dict:
        stats = {
            "vector_enabled": self.vector_enabled,
            "chromadb_available": self._chroma_available,
            "ollama_available": self._ollama_available,
            "embed_model": self.embed_model,
            "alpha": self.alpha,
            "collections": {},
            "total_records": len(self._records_by_id),
        }
        for cat in SLTM_CATEGORIES:
            fallback_count = len(self._fallback_data.get(cat, []))
            chroma_count = 0
            if self._chroma_available and cat in self._collections:
                try:
                    chroma_count = self._collections[cat].count()
                except Exception:
                    pass
            stats["collections"][cat] = {
                "fallback_count": fallback_count,
                "chroma_count": chroma_count,
            }
        return stats

    def _delete_locked(self, record_id: str) -> bool:
        deleted = False
        existing = self._records_by_id.pop(record_id, None)
        if existing:
            self._text_index.remove(record_id)
        for cat in SLTM_CATEGORIES:
            records = self._fallback_data.get(cat, [])
            kept = [record for record in records if record.get("id") != record_id]
            if len(kept) != len(records):
                self._fallback_data[cat] = kept
                self._save_fallback_category(cat)
                deleted = True
            if self._chroma_available and cat in self._collections:
                try:
                    self._collections[cat].delete(ids=[record_id])
                except Exception:
                    pass
        return deleted or existing is not None

    def _searchable_text(self, record: dict) -> str:
        metadata = record.get("metadata", {})
        fields = [
            record.get("text", ""),
            record.get("category", ""),
            metadata.get("source", ""),
            metadata.get("fact_id", ""),
            metadata.get("source_session", ""),
        ]
        return " ".join(str(field) for field in fields if field)

    def _load_fallback(self):
        docs = {}
        self._records_by_id = {}
        for cat in SLTM_CATEGORIES:
            path = self._fallback_dir / f"{cat}.json"
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as handle:
                        records = json.load(handle)
                except Exception:
                    records = []
            else:
                records = []
            normalized = []
            for record in records:
                record = {
                    "id": record.get("id"),
                    "text": record.get("text", ""),
                    "metadata": dict(record.get("metadata", {})),
                    "timestamp": record.get("timestamp", ""),
                    "category": record.get("category", cat),
                }
                if not record["id"]:
                    continue
                normalized.append(record)
                self._records_by_id[record["id"]] = record
                docs[record["id"]] = self._searchable_text(record)
            self._fallback_data[cat] = normalized
        self._text_index.rebuild(docs)

    def _save_fallback_category(self, category: str):
        path = self._fallback_dir / f"{category}.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self._fallback_data.get(category, []), handle, indent=2)
