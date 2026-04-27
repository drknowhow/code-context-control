"""
Transcript Index — TF-IDF search over Claude Code .jsonl conversation transcripts.

Indexes past Claude Code sessions for semantic retrieval, enabling
context recall from previous conversations without re-reading full transcripts.
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

from core import count_tokens


class TranscriptIndex:
    """TF-IDF index over Claude Code .jsonl transcripts."""

    MAX_TRANSCRIPT_FILES = 50
    MAX_CHARS_PER_TURN = 2000
    MAX_TOOL_INPUT_CHARS = 200

    def __init__(self, project_path: str, data_dir: str = ".c3/transcript_index"):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / "index.json"
        self.manifest_file = self.data_dir / "manifest.json"
        self.index = {}       # {turn_id: {text, session_file, timestamp, turn_num}}
        self.manifest = {}    # {file_path: {size, line_count}}

    def find_transcript_dir(self) -> "Path | None":
        """Locate Claude Code transcript directory for this project."""
        import re as _re
        home = Path.home()
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.exists():
            return None

        # Claude Code slugifies the absolute path by replacing every
        # non-alphanumeric character with '-' and stripping leading dashes.
        project_str = str(self.project_path.resolve())
        slug = _re.sub(r"[^a-zA-Z0-9]", "-", project_str).lstrip("-")

        transcript_dir = projects_dir / slug
        if transcript_dir.exists():
            return transcript_dir

        # Fallback: normalize both sides to bare alphanumerics for a
        # variant-proof comparison (handles old slug formats).
        def _bare(s):
            return _re.sub(r"[^a-z0-9]", "", s.lower())

        target_bare = _bare(project_str)
        project_name = self.project_path.resolve().name.lower()
        for d in projects_dir.iterdir():
            if d.is_dir() and (_bare(d.name) == target_bare or project_name in d.name.lower()):
                # Check if it has .jsonl files
                if list(d.glob("*.jsonl")):
                    return d

        return None

    def build_index(self, force: bool = False) -> dict:
        """Build or incrementally update the transcript index.

        Returns {files_scanned, turns_indexed, new_files}.
        """
        transcript_dir = self.find_transcript_dir()
        if not transcript_dir:
            return {"files_scanned": 0, "turns_indexed": 0, "new_files": 0,
                    "error": "No transcript directory found"}

        # Load existing manifest and index
        if not force:
            self._load_manifest()
            self._load_index()

        # Find .jsonl files, limited to most recent
        jsonl_files = sorted(
            transcript_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )[:self.MAX_TRANSCRIPT_FILES]

        files_scanned = 0
        new_files = 0
        total_turns = len(self.index)

        for jf in jsonl_files:
            fpath = str(jf)
            try:
                stat = jf.stat()
                file_info = {"size": stat.st_size, "line_count": sum(1 for _ in open(jf, encoding="utf-8", errors="replace"))}
            except Exception:
                continue

            # Skip unchanged files (unless force)
            if not force and fpath in self.manifest:
                existing = self.manifest[fpath]
                if existing.get("size") == file_info["size"] and existing.get("line_count") == file_info["line_count"]:
                    continue

            # Extract turns from this file
            turns = self._extract_turns(jf)
            session_name = jf.stem

            # Remove old turns from this file
            self.index = {
                tid: data for tid, data in self.index.items()
                if data.get("session_file") != session_name
            }

            # Add new turns
            for turn in turns:
                turn["session_file"] = session_name
                self.index[turn["turn_id"]] = turn

            self.manifest[fpath] = file_info
            files_scanned += 1
            new_files += 1

        # Save
        self._save_index()
        self._save_manifest()

        return {
            "files_scanned": files_scanned,
            "turns_indexed": len(self.index),
            "new_files": new_files,
        }

    def _extract_turns(self, jsonl_path: Path) -> list:
        """Extract conversation turns from a .jsonl transcript.

        Groups sequential user+assistant entries into turns.
        """
        turns = []
        entries = []

        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return []

        turn_num = 0
        i = 0
        while i < len(entries):
            entry = entries[i]

            # Skip non-message types
            entry_type = entry.get("type", "")
            if entry_type in ("progress", "file-history-snapshot", "system"):
                i += 1
                continue

            role = entry.get("role", "")
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = role or msg.get("role", "")

            if role == "user":
                # Collect user text
                user_text = self._extract_text_from_entry(entry)

                # Look ahead for assistant response
                assistant_text = ""
                j = i + 1
                while j < len(entries):
                    next_entry = entries[j]
                    next_type = next_entry.get("type", "")
                    if next_type in ("progress", "file-history-snapshot"):
                        j += 1
                        continue
                    next_role = next_entry.get("role", "")
                    next_msg = next_entry.get("message", {})
                    if isinstance(next_msg, dict):
                        next_role = next_role or next_msg.get("role", "")
                    if next_role == "assistant":
                        assistant_text = self._extract_text_from_entry(next_entry)
                        j += 1
                        break
                    else:
                        break

                combined = (user_text + " " + assistant_text).strip()
                if combined:
                    turn_num += 1
                    turn_id = f"{jsonl_path.stem}_t{turn_num}"
                    turns.append({
                        "turn_id": turn_id,
                        "text": combined[:self.MAX_CHARS_PER_TURN],
                        "timestamp": entry.get("timestamp", ""),
                        "turn_num": turn_num,
                    })
                i = j
            else:
                i += 1

        return turns

    def _extract_text_from_entry(self, entry: dict) -> str:
        """Extract searchable text from a transcript entry."""
        parts = []

        # Direct content field
        content = entry.get("content", "")
        msg = entry.get("message", {})
        if isinstance(msg, dict):
            content = content or msg.get("content", "")

        if isinstance(content, str) and content:
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = str(block.get("input", ""))[:self.MAX_TOOL_INPUT_CHARS]
                        parts.append(f"[tool:{tool_name}] {tool_input}")
                    elif btype == "tool_result":
                        pass  # Skip — too verbose
                    # Skip thinking blocks
                elif isinstance(block, str):
                    parts.append(block)

        return " ".join(parts)

    def search(self, query: str, top_k: int = 5, max_tokens: int = 4000) -> list:
        """Search transcript index via TF-IDF.

        Returns [{turn_id, text, session_file, timestamp, score, tokens}].
        """
        if not self.index:
            self._load_index()
        if not self.index:
            return []

        docs = {tid: data["text"] for tid, data in self.index.items()}
        ranked = self._tfidf_search(query, docs, top_k)

        results = []
        total_tokens = 0
        for turn_id, score in ranked:
            data = self.index[turn_id]
            text = data["text"]
            tokens = count_tokens(text)
            if total_tokens + tokens > max_tokens and results:
                break
            total_tokens += tokens
            results.append({
                "turn_id": turn_id,
                "text": text,
                "session_file": data.get("session_file", ""),
                "timestamp": data.get("timestamp", ""),
                "score": round(score, 3),
                "tokens": tokens,
            })

        return results

    # ─── TF-IDF (same algorithm as MemoryStore) ─────────────

    def _tokenize(self, text: str) -> list:
        """Tokenize text — camelCase split, snake_case split."""
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        text = text.replace('_', ' ').replace('-', ' ')
        return re.findall(r'[a-zA-Z]{2,}', text.lower())

    def _tfidf_search(self, query: str, docs: dict, top_k: int) -> list:
        """Generic TF-IDF search over a dict of {id: text}."""
        if not docs:
            return []
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        N = len(docs)
        df = Counter()
        doc_tf = {}
        for doc_id, text in docs.items():
            tokens = self._tokenize(text)
            tf = Counter(tokens)
            doc_tf[doc_id] = tf
            for t in set(tokens):
                df[t] += 1

        idf = {t: math.log(N / (1 + freq)) for t, freq in df.items()}

        scores = {}
        for doc_id, tf in doc_tf.items():
            max_tf = max(tf.values()) if tf else 1
            score = 0
            for qt in query_tokens:
                if qt in tf:
                    ntf = 0.5 + 0.5 * (tf[qt] / max_tf)
                    score += ntf * idf.get(qt, 0)
            if score > 0:
                scores[doc_id] = score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    # ─── Persistence ─────────────────────────────────────────

    def _load_index(self):
        if self.index_file.exists():
            try:
                with open(self.index_file, encoding="utf-8") as f:
                    self.index = json.load(f)
            except Exception:
                self.index = {}

    def _save_index(self):
        with open(self.index_file, 'w', encoding="utf-8") as f:
            json.dump(self.index, f)

    def _load_manifest(self):
        if self.manifest_file.exists():
            try:
                with open(self.manifest_file, encoding="utf-8") as f:
                    self.manifest = json.load(f)
            except Exception:
                self.manifest = {}

    def _save_manifest(self):
        with open(self.manifest_file, 'w', encoding="utf-8") as f:
            json.dump(self.manifest, f)
