"""
ConversationStore — records and searches full conversation turns.

Storage layout under .c3/conversations/:
  sessions.json          — session metadata index
  {session_id}.jsonl     — full turn records (one JSON object per line)
  {session_id}.jsonl.gz  — gzip-compressed archive for old sessions

Sources:
  - Claude Code: auto-synced from ~/.claude/projects/<slug>/*.jsonl
  - All IDEs: manual logging via add_turn() (called by c3_convo_log MCP tool)
"""

import gzip
import json
import math
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from core import count_tokens
from services.text_index import TextIndex


class ConversationStore:
    """Stores, indexes, and searches full conversation turns."""

    COMPRESS_AFTER_DAYS = 30
    MAX_TURNS_PER_SESSION = 1000
    MAX_TEXT_LEN = 50000        # characters kept per turn (preserve full assistant outputs)
    MAX_SEARCH_TEXT = 1200      # characters used per chunk for TF-IDF scoring
    MAX_TRANSCRIPT_FILES = 100
    SEARCH_CHUNK_CHARS = 1200
    SEARCH_CHUNK_OVERLAP = 200

    def __init__(self, project_path: str):
        self.project_path = Path(project_path).resolve()
        self.store_dir = self.project_path / ".c3" / "conversations"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_file = self.store_dir / "sessions.json"
        self._sessions: list = []   # in-memory cache, cleared on write
        self._search_index = TextIndex()
        self._search_meta: dict[str, dict] = {}
        self._search_dirty = True

    # ── Public API ──────────────────────────────────────────────────────────

    def sync(self, source: str = "all", force: bool = False) -> dict:
        """Sync conversations from known transcript providers and import adapters.

        source:
          - "all" (default): sync all supported sources
          - "claude": sync Claude Code transcripts only
          - "gemini": sync Gemini CLI transcripts only
          - "imports": sync external import files from .c3/conversations/imports

        Returns {synced, total, by_source, errors?}.
        """
        selected = self._normalize_source(source)
        claude_dir = self._find_transcript_dir()
        gemini_dir = self._find_gemini_transcript_dir()
        imports_root = self.store_dir / "imports"
        imports_available = imports_root.exists() and any(imports_root.rglob("*.jsonl"))

        availability = {
            "claude": bool(claude_dir),
            "gemini": bool(gemini_dir),
            "imports": bool(imports_available),
        }

        providers = []
        warnings = []
        if selected in ("all", "claude"):
            if availability["claude"]:
                providers.append(("claude", lambda: self._sync_claude(force=force)))
            elif selected == "claude":
                warnings.append("Claude transcript directory not found for this project")
            elif not availability["gemini"]:
                if availability["imports"]:
                    warnings.append("Claude transcript directory not found; synced imports source instead")
                else:
                    warnings.append("Claude transcript directory not found; skipped claude source")

        if selected in ("all", "gemini"):
            if availability["gemini"]:
                providers.append(("gemini", lambda: self._sync_gemini(force=force)))
            elif selected == "gemini":
                warnings.append("Gemini transcript directory not found for this project")

        if selected in ("all", "imports"):
            if availability["imports"]:
                providers.append(("imports", lambda: self._sync_imports(force=force)))
            elif selected == "imports":
                warnings.append("No import transcripts found under .c3/conversations/imports")

        total_synced = 0
        by_source = {}
        errors = []
        for name, provider in providers:
            try:
                synced = int(provider() or 0)
                by_source[name] = synced
                total_synced += synced
            except Exception as e:
                by_source[name] = 0
                errors.append(f"{name}: {str(e)[:120]}")

        self._compress_old()
        result = {
            "synced": total_synced,
            "total": len(self._load_sessions()),
            "by_source": by_source,
            "requested_source": selected,
            "forced": bool(force),
            "available_sources": availability,
        }
        if warnings:
            result["warnings"] = warnings
        if errors:
            result["errors"] = errors
        self._search_dirty = True
        return result

    def list_sessions(self, limit: int = 100) -> list:
        """Return session metadata sorted by most recent first."""
        sessions = self._load_sessions()
        sessions.sort(key=lambda s: s.get("started", 0), reverse=True)
        return sessions[:limit]

    def get_session(self, session_id: str, offset: int = 0, limit: int = None) -> list:
        """Return turn list for a session, optionally paginated."""
        turns = self._read_turns(session_id)
        if offset < 0:
            offset = 0
        if limit is None or limit <= 0:
            return turns[offset:]
        return turns[offset:offset + limit]

    def add_turn(self, session_id: str, role: str, text: str,
                 tool_calls: list = None, ts: float = None, source: str = "manual") -> dict:
        """Manually append a single turn to a session (for non-Claude-Code IDEs).

        Creates the session metadata if it does not exist yet.
        """
        ts = ts or time.time()
        tokens = count_tokens(text)
        turn = {
            "id": f"t{int(ts * 1000)}",
            "ts": ts,
            "role": role,
            "text": text[:self.MAX_TEXT_LEN],
            "tokens": tokens,
            "source": self._normalize_source(source),
        }
        if tool_calls:
            turn["tool_calls"] = tool_calls

        # Append to JSONL
        session_file = self.store_dir / f"{session_id}.jsonl"
        with open(session_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")

        # Update session index
        sessions = self._load_sessions()
        existing = next((s for s in sessions if s["session_id"] == session_id), None)
        if existing:
            existing["turns"] = existing.get("turns", 0) + 1
            existing["ended"] = ts
            existing["user_tokens"] = existing.get("user_tokens", 0) + (tokens if role == "user" else 0)
            existing["assistant_tokens"] = existing.get("assistant_tokens", 0) + (tokens if role == "assistant" else 0)
            if source and existing.get("source") in (None, "", "manual"):
                existing["source"] = self._normalize_source(source)
            if role == "user" and existing.get("turns", 0) == 1:
                existing["title"] = text[:100].replace("\n", " ")
        else:
            sessions.append({
                "session_id": session_id,
                "title": (text[:100].replace("\n", " ") if role == "user" else session_id[:24]),
                "source": self._normalize_source(source),
                "source_file": None,
                "source_mtime": 0,
                "started": ts,
                "ended": ts,
                "turns": 1,
                "user_tokens": tokens if role == "user" else 0,
                "assistant_tokens": tokens if role == "assistant" else 0,
                "compressed": False,
            })
        self._save_sessions(sessions)
        self._search_dirty = True
        return turn

    def search(self, query: str, limit: int = 30, session_id: str = None) -> list:
        """TF-IDF search over chunked conversation turns."""
        self._ensure_search_index()
        if not self._search_meta:
            return []

        ranked = self._search_index.search(query, top_k=max(limit * 4, 20))
        results = []
        seen_turns = set()
        for key, score in ranked:
            meta = self._search_meta.get(key)
            if not meta:
                continue
            if session_id and meta["session_id"] != session_id:
                continue
            turn_key = meta["turn_key"]
            if turn_key in seen_turns:
                continue
            seen_turns.add(turn_key)
            results.append({
                "session_id": meta["session_id"],
                "session_title": meta["session_title"],
                "source": meta["source"],
                "ts": meta["ts"],
                "role": meta["role"],
                "text": meta["text"],
                "snippet": meta["snippet"],
                "tokens": meta["tokens"],
                "turn_source": meta["turn_source"],
                "turn_key": turn_key,
                "chunk_key": key,
                "chunk_index": meta["chunk_index"],
                "score": round(score, 4),
            })
            if len(results) >= limit:
                break
        return results

    def get_stats(self) -> dict:
        """Return aggregate statistics."""
        sessions = self._load_sessions()
        by_source = Counter(self._normalize_source(s.get("source", "manual")) for s in sessions)
        return {
            "sessions": len(sessions),
            "turns": sum(s.get("turns", 0) for s in sessions),
            "user_tokens": sum(s.get("user_tokens", 0) for s in sessions),
            "assistant_tokens": sum(s.get("assistant_tokens", 0) for s in sessions),
            "compressed_sessions": sum(1 for s in sessions if s.get("compressed")),
            "sources": dict(by_source),
        }

    # ── Transcript Parsing ─────────────────────────────────────────────────

    def _find_transcript_dir(self):
        """Locate Claude Code's project transcript directory."""
        import re as _re
        home = Path.home()
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.exists():
            return None

        project_str = str(self.project_path)

        # Claude Code slugifies the absolute path by replacing every
        # non-alphanumeric character with '-' and stripping leading dashes.
        slug = _re.sub(r"[^a-zA-Z0-9]", "-", project_str).lstrip("-")
        direct = projects_dir / slug
        if direct.exists():
            return direct

        # Fallback 1: old slug algorithm (kept for backwards compat with
        # directories created by earlier versions of C3 or other tools).
        old_slug = project_str.replace("\\", "--").replace("/", "--").replace(":", "").lstrip("-")
        if old_slug != slug:
            old_direct = projects_dir / old_slug
            if old_direct.exists():
                return old_direct

        # Fallback 2: normalize both sides to bare alphanumerics and find the
        # best-matching directory (handles edge-cases in slugification variants).
        def _bare(s):
            return _re.sub(r"[^a-z0-9]", "", s.lower())

        target_bare = _bare(project_str)
        best = None
        best_len = 0
        for d in projects_dir.iterdir():
            if not d.is_dir():
                continue
            d_bare = _bare(d.name)
            # Must share all alphanumeric chars of the project path
            if d_bare == target_bare:
                n = len(list(d.glob("*.jsonl")))
                if n > best_len:
                    best = d
                    best_len = n
        return best

    def _sync_claude(self, force: bool = False) -> int:
        """Sync transcripts from Claude Code."""
        transcript_dir = self._find_transcript_dir()
        if not transcript_dir:
            return 0

        jsonl_files = sorted(
            transcript_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:self.MAX_TRANSCRIPT_FILES]

        existing = {
            f"claude::{s.get('source_file')}": s
            for s in self._load_sessions()
            if s.get("source_file")
        }
        synced = 0
        for jf in jsonl_files:
            try:
                mtime = jf.stat().st_mtime
                source_file = jf.name
                existing_key = f"claude::{source_file}"
                if (not force) and existing_key in existing and abs(existing[existing_key].get("source_mtime", 0) - mtime) < 1:
                    continue

                turns = self._extract_turns(jf)
                if not turns:
                    continue
                for t in turns:
                    t["source"] = "claude"

                session_id = jf.stem
                self._upsert_synced_session(
                    session_id=session_id,
                    turns=turns,
                    source="claude",
                    source_file=source_file,
                    source_mtime=mtime,
                )
                synced += 1
            except Exception:
                continue
        return synced

    def _find_gemini_transcript_dir(self):
        """Locate Gemini CLI's project transcript directory."""
        import hashlib
        home = Path.home()
        project_str = str(self.project_path)
        slug = hashlib.sha256(project_str.encode('utf-8')).hexdigest()
        chats_dir = home / ".gemini" / "tmp" / slug / "chats"
        if chats_dir.exists() and chats_dir.is_dir():
            return chats_dir
        return None

    def _sync_gemini(self, force: bool = False) -> int:
        """Sync transcripts from Gemini CLI."""
        transcript_dir = self._find_gemini_transcript_dir()
        if not transcript_dir:
            return 0

        json_files = sorted(
            transcript_dir.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:self.MAX_TRANSCRIPT_FILES]

        existing = {
            f"gemini::{s.get('source_file')}": s
            for s in self._load_sessions()
            if s.get("source_file")
        }
        synced = 0
        for jf in json_files:
            try:
                mtime = jf.stat().st_mtime
                source_file = jf.name
                existing_key = f"gemini::{source_file}"
                if (not force) and existing_key in existing and abs(existing[existing_key].get("source_mtime", 0) - mtime) < 1:
                    continue

                turns = self._extract_turns_gemini(jf)
                if not turns:
                    continue

                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                session_id = data.get("sessionId", jf.stem)

                self._upsert_synced_session(
                    session_id=session_id,
                    turns=turns,
                    source="gemini",
                    source_file=source_file,
                    source_mtime=mtime,
                )
                synced += 1
            except Exception:
                continue
        return synced

    def _extract_turns_gemini(self, json_path: Path) -> list:
        turns = []
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            messages = data.get("messages", [])
            for i, msg in enumerate(messages):
                role = "assistant" if msg.get("type") == "gemini" else msg.get("type", "user")
                text = msg.get("content", "")
                ts_raw = msg.get("timestamp")

                ts = json_path.stat().st_mtime
                if ts_raw:
                    try:
                        ts_raw = ts_raw.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(ts_raw)
                        ts = dt.timestamp()
                    except:
                        pass

                tokens = 0
                tok_data = msg.get("tokens", {})
                if isinstance(tok_data, dict):
                    tokens = tok_data.get("output", 0) if role == "assistant" else tok_data.get("input", 0)
                if not tokens:
                    tokens = count_tokens(text)

                turns.append({
                    "id": msg.get("id", f"t{int(ts * 1000)}_{i}"),
                    "ts": ts,
                    "role": role,
                    "text": text[:self.MAX_TEXT_LEN],
                    "tokens": tokens,
                    "source": "gemini",
                    "tool_calls": []
                })
        except Exception:
            pass
        return turns

    def _sync_imports(self, force: bool = False) -> int:
        """Sync generic JSONL imports from .c3/conversations/imports/<source>/*.jsonl."""
        imports_root = self.store_dir / "imports"
        if not imports_root.exists():
            return 0

        jsonl_files = sorted(imports_root.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        existing = {
            f"{self._normalize_source(s.get('source', 'manual'))}::{s.get('source_file')}": s
            for s in self._load_sessions()
            if s.get("source_file")
        }

        synced = 0
        for jf in jsonl_files[:self.MAX_TRANSCRIPT_FILES]:
            try:
                rel = jf.relative_to(imports_root)
                source = self._normalize_source(rel.parts[0] if len(rel.parts) > 1 else "imports")
                mtime = jf.stat().st_mtime
                source_file = str(rel).replace("\\", "/")
                existing_key = f"{source}::{source_file}"
                if (not force) and existing_key in existing and abs(existing[existing_key].get("source_mtime", 0) - mtime) < 1:
                    continue

                turns = self._extract_turns_generic(jf, source=source)
                if not turns:
                    continue

                # Keep source namespaced to avoid collisions with Claude stem ids.
                session_id = f"{source}_{jf.stem}"
                self._upsert_synced_session(
                    session_id=session_id,
                    turns=turns,
                    source=source,
                    source_file=source_file,
                    source_mtime=mtime,
                )
                synced += 1
            except Exception:
                continue
        return synced

    def _upsert_synced_session(self, session_id: str, turns: list, source: str, source_file: str, source_mtime: float):
        """Write synced turns and upsert session metadata."""
        first_user = next((t.get("text", "") for t in turns if t.get("role") == "user"), "")
        title = (first_user[:100].strip() or session_id[:24]).replace("\n", " ")
        user_tok = sum(t.get("tokens", 0) for t in turns if t.get("role") == "user")
        asst_tok = sum(t.get("tokens", 0) for t in turns if t.get("role") == "assistant")

        meta = {
            "session_id": session_id,
            "title": title,
            "source": self._normalize_source(source),
            "source_file": source_file,
            "source_mtime": source_mtime,
            "started": turns[0]["ts"] if turns else time.time(),
            "ended": turns[-1]["ts"] if turns else time.time(),
            "turns": len(turns),
            "user_tokens": user_tok,
            "assistant_tokens": asst_tok,
            "compressed": False,
        }
        self._write_turns(session_id, turns)
        self._upsert_session(meta)

    def _extract_turns(self, jsonl_path: Path) -> list:
        """Parse a Claude Code JSONL file into a list of turn dicts."""
        entries = []
        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return []

        turns = []
        turn_num = 0
        for entry in entries:
            etype = entry.get("type", "")
            if etype in ("progress", "file-history-snapshot", "system"):
                continue

            role = entry.get("role", "")
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = role or msg.get("role", "")

            if role not in ("user", "assistant"):
                continue

            text, tool_calls = self._extract_content(entry)
            if not text and not tool_calls:
                continue

            turn_num += 1
            ts_raw = entry.get("timestamp", "")
            t = {
                "id": f"t{turn_num:04d}",
                "ts": self._parse_ts(ts_raw),
                "role": role,
                "text": text[:self.MAX_TEXT_LEN],
                "tokens": count_tokens(text),
            }
            if tool_calls:
                t["tool_calls"] = tool_calls
            turns.append(t)

            if len(turns) >= self.MAX_TURNS_PER_SESSION:
                break

        return turns

    def _extract_turns_generic(self, jsonl_path: Path, source: str = "imports") -> list:
        """Parse generic JSONL transcripts from non-Claude systems.

        Supported line shapes:
          - {"role":"user|assistant","text":"...","ts":...}
          - {"role":"...","content":"..."}
          - {"message":{"role":"...","content":"..."},"timestamp":"..."}
          - {"content":[{"type":"text","text":"..."}], "role":"..."}
        """
        entries = []
        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return []

        turns = []
        turn_num = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            role = (entry.get("role") or "").strip().lower()
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = role or (msg.get("role") or "").strip().lower()
            if role not in ("user", "assistant"):
                continue

            text, tool_calls = self._extract_content(entry)
            if not text and isinstance(entry.get("text"), str):
                text = entry.get("text", "")
            if not text and isinstance(msg, dict):
                mcontent = msg.get("content")
                if isinstance(mcontent, str):
                    text = mcontent

            if not text and not tool_calls:
                continue

            turn_num += 1
            ts_raw = entry.get("ts", entry.get("timestamp", ""))
            t = {
                "id": f"t{turn_num:04d}",
                "ts": self._parse_ts(ts_raw),
                "role": role,
                "text": (text or "")[:self.MAX_TEXT_LEN],
                "tokens": count_tokens(text or ""),
                "source": self._normalize_source(source),
            }
            if tool_calls:
                t["tool_calls"] = tool_calls
            turns.append(t)
            if len(turns) >= self.MAX_TURNS_PER_SESSION:
                break
        return turns

    def _extract_content(self, entry: dict):
        """Return (text_str, tool_calls_list) from a transcript entry."""
        parts = []
        tool_calls = []

        content = entry.get("content", "")
        msg = entry.get("message", {})
        if isinstance(msg, dict):
            content = content or msg.get("content", "")

        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    if isinstance(block, str):
                        parts.append(block)
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    t = block.get("text", "")
                    if t:
                        parts.append(t)
                elif btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    args_str = ""
                    if isinstance(inp, dict):
                        args_str = " ".join(
                            f"{k}={str(v)[:80]}" for k, v in list(inp.items())[:4]
                        )[:160]
                    tool_calls.append({"tool": name, "args": args_str})
                # Skip tool_result and thinking blocks

        # Preserve line boundaries so markdown lists/checkboxes remain parseable in UI.
        return "\n".join(p for p in parts if p).strip(), tool_calls

    @staticmethod
    def _parse_ts(ts_raw) -> float:
        if isinstance(ts_raw, (int, float)):
            return float(ts_raw)
        if isinstance(ts_raw, str) and ts_raw:
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                pass
        return time.time()

    # ── Storage ────────────────────────────────────────────────────────────

    def _write_turns(self, session_id: str, turns: list):
        path = self.store_dir / f"{session_id}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for t in turns:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        self._search_dirty = True

    def _read_turns(self, session_id: str) -> list:
        gz_path = self.store_dir / f"{session_id}.jsonl.gz"
        plain_path = self.store_dir / f"{session_id}.jsonl"
        turns = []

        source = None
        if gz_path.exists():
            source = gz_path
            opener = lambda p: gzip.open(p, "rt", encoding="utf-8")
        elif plain_path.exists():
            source = plain_path
            opener = lambda p: open(p, encoding="utf-8")

        if source:
            try:
                with opener(source) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                turns.append(json.loads(line))
                            except Exception:
                                pass
            except Exception:
                pass

        return turns

    def _compress_old(self):
        """gzip-compress session files older than COMPRESS_AFTER_DAYS."""
        cutoff = time.time() - self.COMPRESS_AFTER_DAYS * 86400
        sessions = self._load_sessions()
        changed = False
        for s in sessions:
            if s.get("compressed") or s.get("ended", 0) > cutoff:
                continue
            sid = s["session_id"]
            plain = self.store_dir / f"{sid}.jsonl"
            gz = self.store_dir / f"{sid}.jsonl.gz"
            if not plain.exists() or gz.exists():
                continue
            try:
                with open(plain, "rb") as fin, gzip.open(gz, "wb") as fout:
                    fout.write(fin.read())
                plain.unlink()
                s["compressed"] = True
                changed = True
            except Exception:
                pass
        if changed:
            self._save_sessions(sessions)
            self._search_dirty = True

    def _upsert_session(self, meta: dict):
        sessions = self._load_sessions()
        for i, s in enumerate(sessions):
            if s["session_id"] == meta["session_id"]:
                sessions[i] = meta
                self._save_sessions(sessions)
                return
        sessions.append(meta)
        self._save_sessions(sessions)

    def _load_sessions(self) -> list:
        if self._sessions:
            return self._sessions
        if self._sessions_file.exists():
            try:
                with open(self._sessions_file, encoding="utf-8") as f:
                    loaded = json.load(f)
                changed = False
                for s in loaded:
                    src = s.get("source")
                    if not src:
                        src = "claude" if s.get("source_file") else "manual"
                        s["source"] = src
                        changed = True
                    norm = self._normalize_source(src)
                    if norm != src:
                        s["source"] = norm
                        changed = True
                self._sessions = loaded
                if changed:
                    self._save_sessions(self._sessions)
                return self._sessions
            except Exception:
                pass
        self._sessions = []
        return self._sessions

    def _ensure_search_index(self):
        if not self._search_dirty:
            return

        docs = {}
        meta = {}
        for session in self._load_sessions():
            sid = session.get("session_id", "")
            if not sid:
                continue
            try:
                turns = self._read_turns(sid)
            except Exception:
                continue
            for turn in turns:
                turn_key = f"{sid}:{turn.get('id', '')}"
                for chunk_index, snippet in enumerate(self._chunk_text(turn.get("text", ""))):
                    chunk_key = f"{turn_key}:{chunk_index}"
                    docs[chunk_key] = snippet[:self.MAX_SEARCH_TEXT]
                    meta[chunk_key] = {
                        "turn_key": turn_key,
                        "session_id": sid,
                        "session_title": session.get("title", ""),
                        "source": session.get("source", "manual"),
                        "ts": turn.get("ts", 0),
                        "role": turn.get("role", ""),
                        "text": turn.get("text", ""),
                        "snippet": snippet,
                        "tokens": turn.get("tokens", 0),
                        "turn_source": turn.get("source", session.get("source", "manual")),
                        "chunk_index": chunk_index,
                    }
        self._search_index.rebuild(docs)
        self._search_meta = meta
        self._search_dirty = False

    def _save_sessions(self, sessions: list):
        self._sessions = sessions
        with open(self._sessions_file, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
        self._search_dirty = True

    def _chunk_text(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self.SEARCH_CHUNK_CHARS:
            return [text]

        chunks = []
        step = max(1, self.SEARCH_CHUNK_CHARS - self.SEARCH_CHUNK_OVERLAP)
        start = 0
        while start < len(text):
            end = min(len(text), start + self.SEARCH_CHUNK_CHARS)
            chunks.append(text[start:end])
            if end >= len(text):
                break
            start += step
        return chunks

    # ── TF-IDF Search ──────────────────────────────────────────────────────

    def _tfidf_search(self, query: str, docs: dict, top_k: int) -> list:
        q_terms = Counter(self._tokenize(query))
        N = len(docs)
        if N == 0 or not q_terms:
            return []

        # Build document-frequency table
        df: Counter = Counter()
        tok_docs: dict = {}
        for key, text in docs.items():
            terms = self._tokenize(text)
            tok_docs[key] = Counter(terms)
            for t in set(terms):
                df[t] += 1

        scores: dict = {}
        for key, term_counts in tok_docs.items():
            total = sum(term_counts.values()) or 1
            score = 0.0
            for term, q_count in q_terms.items():
                tf = term_counts.get(term, 0) / total
                idf = math.log((N + 1) / (df.get(term, 0) + 1)) + 1
                score += tf * idf * q_count
            if score > 0:
                scores[key] = score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    @staticmethod
    def _tokenize(text: str) -> list:
        return re.findall(r"[a-z0-9_]+", text.lower())

    @staticmethod
    def _normalize_source(source: str) -> str:
        raw = (source or "manual").strip().lower()
        if not raw:
            return "manual"
        aliases = {
            "transcript": "claude",
            "claude-code": "claude",
            "claude_code": "claude",
            "mcp": "manual",
        }
        return aliases.get(raw, raw)
