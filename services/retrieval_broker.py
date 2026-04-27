"""Unified retrieval broker across facts, conversations, files, sessions, and snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from services.text_index import TextIndex


class MemoryRetrievalBroker:
    """Normalizes retrieval across the project's memory sources."""

    def __init__(self, project_path: str, memory_store, conversation_store=None, file_memory=None, snapshots=None):
        self.project_path = Path(project_path)
        self.memory_store = memory_store
        self.conversation_store = conversation_store
        self.file_memory = file_memory
        self.snapshots = snapshots
        self._session_index = TextIndex()
        self._session_meta = {}
        self._session_dirty = True

    def mark_sessions_dirty(self):
        self._session_dirty = True

    def search(self, query: str, top_k: int = 5) -> dict:
        self._session_dirty = True
        self._ensure_session_index()

        fact_results = self.memory_store.recall(query, top_k=top_k)
        conversation_results = self.conversation_store.search(query, limit=top_k) if self.conversation_store else []
        file_results = self.file_memory.search(query, top_k=top_k) if self.file_memory else []
        snapshot_results = self.snapshots.search(query, top_k=top_k) if self.snapshots else []

        session_hits = []
        for session_id, score in self._session_index.search(query, top_k=top_k):
            meta = self._session_meta.get(session_id)
            if not meta:
                continue
            session_hits.append({**meta, "score": round(score, 4)})

        merged = []
        for fact in fact_results:
            merged.append({
                "kind": "fact",
                "id": fact["id"],
                "title": fact.get("category", "fact"),
                "text": fact.get("fact", ""),
                "score": float(fact.get("score", 0.0)),
                "payload": fact,
            })
        for convo in conversation_results:
            merged.append({
                "kind": "conversation",
                "id": convo["turn_key"],
                "title": convo.get("session_title", convo.get("session_id", "")),
                "text": convo.get("snippet") or convo.get("text", ""),
                "score": float(convo.get("score", 0.0)),
                "payload": convo,
            })
        for session in session_hits:
            merged.append({
                "kind": "session",
                "id": session["session_id"],
                "title": session.get("summary") or session.get("session_id", ""),
                "text": session.get("summary", ""),
                "score": float(session.get("score", 0.0)),
                "payload": session,
            })
        for file_hit in file_results:
            merged.append({
                "kind": "file",
                "id": file_hit["path"],
                "title": file_hit["path"],
                "text": file_hit.get("summary") or "",
                "score": float(file_hit.get("score", 0.0)),
                "payload": file_hit,
            })
        for snap in snapshot_results:
            merged.append({
                "kind": "snapshot",
                "id": snap["snapshot_id"],
                "title": snap.get("task_description", ""),
                "text": snap.get("task_description", ""),
                "score": float(snap.get("score", 0.0)),
                "payload": snap,
            })

        merged.sort(key=lambda item: item["score"], reverse=True)
        return {
            "facts": fact_results,
            "conversations": conversation_results[:top_k],
            "sessions": session_hits[:top_k],
            "files": file_results[:top_k],
            "snapshots": snapshot_results[:top_k],
            "results": merged[:top_k * 3],
        }

    def _ensure_session_index(self):
        if not self._session_dirty:
            return
        session_dir = self.project_path / ".c3" / "sessions"
        docs = {}
        meta = {}
        if session_dir.exists():
            for path in sorted(session_dir.glob("session_*.json"), reverse=True):
                try:
                    with open(path, encoding="utf-8") as handle:
                        session = json.load(handle)
                except Exception:
                    continue
                session_id = session.get("id") or session.get("session_id")
                if not session_id:
                    continue
                text_parts = [session.get("description", ""), session.get("summary", "")]
                for decision in session.get("decisions", []):
                    text_parts.append(decision.get("decision", ""))
                    text_parts.append(decision.get("reasoning", ""))
                text_parts.extend(session.get("context_notes", []))
                docs[session_id] = " ".join(part for part in text_parts if part)
                meta[session_id] = {
                    "session_id": session_id,
                    "started": session.get("started", ""),
                    "summary": session.get("summary", ""),
                }
        self._session_index.rebuild(docs)
        self._session_meta = meta
        self._session_dirty = False
