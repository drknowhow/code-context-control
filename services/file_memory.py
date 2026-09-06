"""File Memory Store — persistent structural index of source files.

Maintains per-file records with section maps (classes, functions, imports)
and exact line ranges so Claude can do targeted reads with offset/limit
instead of reading entire files.

Storage: .c3/file_memory/ directory, one JSON file per source file.
"""
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from services.compressor import STRUCTURE_PATTERNS
from services.file_map import render_map
from services.parser import PARSER_VERSION, extract_sections_ast
from services.text_index import TextIndex

# Extensions we know how to extract structure from
CODE_EXTENSIONS = {'.py', '.js', '.ts', '.tsx', '.jsx', '.r', '.R',
                   '.go', '.rs', '.java', '.rb', '.c', '.cpp', '.h', '.cs',
                   '.html', '.htm', '.md', '.css', '.json', '.yaml', '.yml'}

# Language detection by extension
LANG_MAP = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
    '.tsx': 'typescript', '.jsx': 'javascript', '.r': 'R', '.R': 'R',
    '.go': 'go', '.rs': 'rust', '.java': 'java', '.rb': 'ruby',
    '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.cs': 'csharp',
    '.html': 'html', '.htm': 'html', '.md': 'markdown', '.css': 'css',
    '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
}


class FileMemoryStore:
    """Persistent structural index of source files."""

    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self.store_dir = self.project_path / ".c3" / "file_memory"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._queue_state_path = self.store_dir / "_queue.json"
        self._diag_path = self.store_dir / "_diagnostics.jsonl"
        self._map_cache = {}
        self._search_index = TextIndex()
        # Building the search index reads every tracked record off disk — for a
        # large project that is tens of thousands of files (~2x that many reads
        # via list_tracked + get), far too heavy for the constructor, which sits
        # on the MCP handshake path. Defer it: built lazily on first search(),
        # mirroring ConversationStore._ensure_search_index.
        self._search_dirty = True
        # Guards the lazy index: the first search() build can race a background
        # update()'s add_or_update on the shared TextIndex. Reentrant so
        # search() may hold it across _ensure_search_index() + the query.
        self._search_lock = threading.RLock()
        # One-time cleanup of pre-2.121.0 generated summaries (marker-gated).
        try:
            self.purge_summaries()
        except Exception:
            pass

    #: Marker written once the pre-2.121.0 `summary` fields are gone.
    _PURGE_MARKER = "_summaries_purged"

    def purge_summaries(self) -> int:
        """Drop the generated `summary` from every stored record, once.

        Those summaries were produced from symbol NAMES only and half of them
        were cut mid-sentence (measured 2026-09-06: 134 of 267); the map no
        longer renders them and nothing else reads them. Returns the number
        of records rewritten. Idempotent via a marker file.
        """
        marker = self.store_dir / self._PURGE_MARKER
        if marker.exists():
            return 0
        rewritten = 0
        for f in self.store_dir.glob("*.json"):
            if f.name.startswith("_"):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            if "summary" not in data:
                continue
            data.pop("summary", None)
            try:
                rel = str(data.get("path") or "").replace("\\", "/")
                data["path"] = rel
                self._save(rel, data)
                rewritten += 1
            except Exception:
                continue
        try:
            marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        except Exception:
            pass
        return rewritten

    def get(self, rel_path: str) -> Optional[dict]:
        """Load a file's memory record, or None if not tracked."""
        rel_path = str(rel_path).replace("\\", "/")
        store_file = self._store_path(rel_path)
        if not store_file.exists():
            return None
        try:
            with open(store_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def drop(self, rel_path: str) -> bool:
        """Delete a file's memory record and any cached map.

        Mask-activation primitive: a record extracted from the raw file is
        raw-derived content, so it must not survive the rule that masks that
        file (docs/mask-guard.md §6, row 7).
        """
        store_file = self._store_path(rel_path)
        removed = False
        if store_file.exists():
            try:
                store_file.unlink()
                removed = True
            except Exception:
                return False
        with self._search_lock:
            self._map_cache.pop(rel_path, None)
            try:
                self._search_index.remove(rel_path)
            except Exception:
                self._search_dirty = True
        return removed

    def update(self, rel_path: str, ai_summary: str = None) -> Optional[dict]:
        """Re-extract sections from file and persist the record.

        Returns the updated record, or None if the file doesn't exist.
        `ai_summary` is accepted for call-site compatibility and ignored:
        maps carry no generated prose (docs/file-map.md).
        """
        rel_path = str(rel_path).replace("\\", "/")
        full_path = self.project_path / rel_path
        if not full_path.exists():
            return None

        try:
            stat = full_path.stat()
        except Exception:
            return None

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        ext = full_path.suffix.lower()
        lines = content.splitlines()
        content_hash = hashlib.md5(content.encode()).hexdigest()

        # Check if we already have an up-to-date record
        existing = self.get(rel_path)
        if existing and existing.get("content_hash") == content_hash:
            # If it was a generic "full file" but now we have structural tools, force update
            was_generic = len(existing.get("sections", [])) <= 1 and existing.get("sections", [{}])[0].get("name") == "(full file)"
            # Also force re-extraction when the parser logic has been bumped
            stale_parser = (existing.get("parser_version") != PARSER_VERSION
                            or "parser" not in existing)  # pre-2.120.0 record
            if not ((was_generic and ext in CODE_EXTENSIONS) or stale_parser):
                with self._search_lock:
                    if not self._search_dirty:
                        self._search_index.add_or_update(rel_path, self._search_doc(existing))
                self._cache_map(rel_path, existing)
                return existing
            # If we are here, we are forcing a fresh extraction

        sections, parser = self._extract_sections_with_parser(full_path, content)

        record = {
            "path": rel_path,
            "content_hash": content_hash,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lines": len(lines),
            "size_bytes": stat.st_size,
            "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
            "language": LANG_MAP.get(ext, ext.lstrip('.')),
            "parser_version": PARSER_VERSION,
            # Which extractor produced `sections` (C0 measurement, 2.120.0):
            # tree_sitter | regex | generic. Telemetry folds this so the map
            # remediation can grade renderer changes per parser, not in bulk.
            "parser": parser,
            "sections": sections,
        }

        self._save(rel_path, record)
        self._cache_map(rel_path, record)
        with self._search_lock:
            if not self._search_dirty:
                self._search_index.add_or_update(rel_path, self._search_doc(record))
        return record

    def get_map(self, rel_path: str) -> Optional[str]:
        """Return a formatted structural map for Claude consumption.

        Returns None if no record exists. Call update() first to ensure fresh data.
        """
        record = self.get(rel_path)
        if not record:
            return None
        return self._cache_map(rel_path, record)

    def get_or_build_map(self, rel_path: str) -> str:
        """Get map if cached, otherwise build it on-demand."""
        record = self.get(rel_path)

        # Check staleness
        if record and not self.needs_update(rel_path):
            return self._cache_map(rel_path, record)

        # Build fresh
        updated = self.update(rel_path)
        if updated:
            return self._cache_map(rel_path, updated)

        return f"[file_map] Could not build map for {rel_path} — file not found or unreadable."

    def get_or_build_dense_map(self, rel_path: str) -> str:
        """Retired alias: the canonical map IS the dense map (2.121.0)."""
        return self.get_or_build_map(rel_path)

    def needs_update(self, rel_path: str) -> bool:
        """True if the file has changed since we last indexed it."""
        record = self.get(rel_path)
        if not record:
            return True

        full_path = self.project_path / rel_path
        if not full_path.exists():
            return False

        try:
            stat = full_path.stat()
            current_mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
            if (
                record.get("mtime_ns") == current_mtime_ns
                and record.get("size_bytes") == stat.st_size
            ):
                return False
            content = full_path.read_text(encoding="utf-8", errors="replace")
            current_hash = hashlib.md5(content.encode()).hexdigest()
            return current_hash != record.get("content_hash")
        except Exception:
            return True

    def get_symbol_ranges(self, rel_path: str, symbol_names: list[str], return_matches: bool = False) -> list:
        """Resolve symbol names to line ranges (1-indexed).
        Supports exact match and substring/partial match (e.g. 'handle_req' matches 'handle_request_data').
        Supports exact regex if anchored (e.g. '^cmd_benchmark$').
        """
        record = self.get(rel_path)
        if not record or "sections" not in record:
            return []

        ranges = []
        matches = []

        # Pre-compile regexes
        compiled_targets = []
        for name in symbol_names:
            if name.startswith('^') and name.endswith('$'):
                try:
                    compiled_targets.append((name, re.compile(name, re.IGNORECASE)))
                except Exception:
                    compiled_targets.append((name, name.lower()))
            elif name in ('<main>', '<globals>', '<imports>'):
                compiled_targets.append((name, name))
            else:
                compiled_targets.append((name, name.lower()))

        def _matches(section_name: str, target_data) -> bool:
            orig_name, target = target_data
            sn = section_name.lower()
            if isinstance(target, re.Pattern):
                return bool(target.match(section_name))
            if orig_name in ('<main>', '<globals>', '<imports>'):
                return False # Handled separately if needed, or matched below if actually named that
            if sn == target:
                return True
            # Substring match
            if target in sn or sn in target:
                return True
            return False

        def search_sections(sections, parent_name=""):
            for sec in sections:
                sec_name = sec.get("name", "")
                qualified = f"{parent_name}.{sec_name}" if parent_name else sec_name
                for target_data in compiled_targets:
                    hit = _matches(sec_name, target_data)
                    # A dotted target (`Class.method`, as the canonical map
                    # prints it) matches the qualified name exactly and
                    # never a bare name that merely contains a fragment.
                    if "." in str(target_data[0]) and not isinstance(target_data[1], re.Pattern):
                        hit = qualified.lower() == str(target_data[0]).lower()
                    if hit:
                        ranges.append((sec["line_start"], sec["line_end"]))
                        matches.append({"target": target_data[0], "match": qualified,
                                        "range": (sec["line_start"], sec["line_end"])})

                if "children" in sec:
                    search_sections(sec["children"], qualified if sec.get("type") in
                                    ("class", "impl", "trait", "struct", "enum", "interface")
                                    else parent_name)

        search_sections(record["sections"])

        # Deduplicate matches
        unique_matches = []
        seen = set()
        for m in matches:
            key = (m["target"], m["match"], m["range"])
            if key not in seen:
                seen.add(key)
                unique_matches.append(m)

        unique_ranges = list(set(ranges))

        if return_matches:
            return unique_matches
        return unique_ranges

    def list_tracked(self) -> list:
        """Return relative paths of all tracked files."""
        tracked = []
        for f in self.store_dir.glob("*.json"):
            if f.name.startswith("_"):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
                tracked.append(data.get("path", ""))
            except Exception:
                continue
        return [p for p in tracked if p]

    def prune_stale(self) -> list[str]:
        """Remove records whose source file no longer exists on disk.

        Deleted/renamed files used to leave their file_memory records behind
        forever (tracked-count drift vs the real index). Called periodically
        by the retention sweep. Records that fail to parse are left alone
        (unreadable is not the same as stale). Returns the pruned relative
        paths. Never raises.
        """
        pruned: list[str] = []
        try:
            for store_file in list(self.store_dir.glob("*.json")):
                if store_file.name.startswith("_"):
                    continue
                try:
                    with open(store_file, encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    continue
                rel = data.get("path", "")
                if not rel:
                    continue
                try:
                    if (self.project_path / rel).exists():
                        continue
                except OSError:
                    continue
                try:
                    store_file.unlink()
                except OSError:
                    continue
                pruned.append(rel)
                self._map_cache.pop(rel.replace("\\", "/"), None)
        except Exception:
            return pruned
        if pruned:
            with self._search_lock:
                if not self._search_dirty:
                    for rel in pruned:
                        try:
                            self._search_index.remove(rel)
                        except Exception:
                            self._search_dirty = True
                            break
        return pruned

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        with self._search_lock:
            self._ensure_search_index()
            ranked = self._search_index.search(query, top_k=top_k)
        results = []
        for rel_path, score in ranked:
            record = self.get(rel_path)
            if not record:
                continue
            results.append({
                "path": rel_path,
                "language": record.get("language", ""),
                "summary": record.get("summary"),
                "score": round(score, 4),
                "sections": len(record.get("sections", [])),
            })
        return results

    def queue_for_update(self, rel_path: str):
        """Add a file to the async update queue (used by Read hook)."""
        try:
            state = self._load_queue_state()
            pending = state.get("pending", [])
            inflight = state.get("inflight", [])
            if rel_path not in pending and rel_path not in inflight:
                pending.append(rel_path)
            state["pending"] = pending
            self._save_queue_state(state)
        except Exception:
            self._record_diag("queue_for_update_failed", rel_path)

    def drain_queue(self) -> list:
        """Claim queued work without dropping it on crash."""
        try:
            state = self._load_queue_state()
            pending = state.get("pending", [])
            inflight = state.get("inflight", [])
            if inflight:
                claimed = inflight
            else:
                claimed = []
                seen = set()
                for path in pending:
                    clean = path.strip()
                    if clean and clean not in seen:
                        seen.add(clean)
                        claimed.append(clean)
                state["pending"] = []
                state["inflight"] = claimed
                self._save_queue_state(state)
            return claimed
        except Exception:
            self._record_diag("drain_queue_failed", "")
            return []

    def complete_updates(self, rel_paths: list[str], failed: bool = False):
        try:
            state = self._load_queue_state()
            inflight = [p for p in state.get("inflight", []) if p not in set(rel_paths)]
            if failed:
                pending = state.get("pending", [])
                for path in rel_paths:
                    if path not in pending:
                        pending.append(path)
                state["pending"] = pending
            state["inflight"] = inflight
            self._save_queue_state(state)
        except Exception:
            self._record_diag("complete_updates_failed", ",".join(rel_paths))

    # ── Private ──────────────────────────────────────────────

    def _store_path(self, rel_path: str) -> Path:
        """Map a relative file path to its JSON store file."""
        key = hashlib.md5(rel_path.replace("\\", "/").encode()).hexdigest()
        return self.store_dir / f"{key}.json"

    def _save(self, rel_path: str, record: dict):
        """Persist a record to disk. The stored path is always posix."""
        store_file = self._store_path(rel_path)
        if isinstance(record, dict) and record.get("path"):
            record["path"] = str(record["path"]).replace("\\", "/")
        try:
            with open(store_file, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
        except Exception:
            self._record_diag("save_failed", rel_path)

    def _cache_map(self, rel_path: str, record: dict) -> str:
        """Return cached formatted map when the record content hash is unchanged."""
        cache_key = rel_path.replace("\\", "/")
        content_hash = record.get("content_hash")
        cached = self._map_cache.get(cache_key)
        if cached and cached[0] == content_hash:
            return cached[1]
        rendered = render_map(record)
        self._map_cache[cache_key] = (content_hash, rendered)
        return rendered

    def _search_doc(self, record: dict) -> str:
        fields = [record.get("path", ""), record.get("language", "")]
        for section in record.get("sections", []):
            fields.append(section.get("name", ""))
            fields.append(section.get("type", ""))
            fields.append(section.get("doc", ""))
            for child in section.get("children", []):
                fields.append(child.get("name", ""))
                fields.append(child.get("type", ""))
        return " ".join(str(field) for field in fields if field)

    def _ensure_search_index(self):
        """Build the search index on first use (deferred off __init__).

        The build reads every tracked record from disk, so it must never run on
        the constructor / MCP handshake path. Mirrors ConversationStore.
        """
        with self._search_lock:
            if not self._search_dirty:
                return
            self._rebuild_search_index()
            self._search_dirty = False

    def _rebuild_search_index(self):
        docs = {}
        for rel_path in self.list_tracked():
            record = self.get(rel_path)
            if record:
                docs[rel_path] = self._search_doc(record)
        self._search_index.rebuild(docs)

    def _load_queue_state(self) -> dict:
        if not self._queue_state_path.exists():
            return {"pending": [], "inflight": []}
        try:
            with open(self._queue_state_path, encoding="utf-8") as handle:
                state = json.load(handle)
        except Exception:
            return {"pending": [], "inflight": []}
        state.setdefault("pending", [])
        state.setdefault("inflight", [])
        return state

    def _save_queue_state(self, state: dict):
        with open(self._queue_state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)

    def _record_diag(self, kind: str, rel_path: str, detail: str = ""):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "path": rel_path,
            "detail": detail,
        }
        try:
            with open(self._diag_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    #: Parser attribution values stored on records and folded by telemetry.
    PARSER_TREE_SITTER = "tree_sitter"
    PARSER_REGEX = "regex"
    PARSER_GENERIC = "generic"

    def _extract_sections(self, filepath: Path, content: str) -> list:
        """Extract structural sections with line ranges from source code."""
        return self._extract_sections_with_parser(filepath, content)[0]

    def _extract_sections_with_parser(self, filepath: Path, content: str) -> tuple:
        """(sections, parser) — the sections plus WHICH extractor produced them.

        tree_sitter when the AST walk handled the extension, regex when it
        fell back to STRUCTURE_PATTERNS, generic when the file is a single
        "(full file)" section. Kept separate from _extract_sections so the
        older single-value call keeps working.
        """
        ext = filepath.suffix.lower()

        # Try AST parser first
        ast_sections = extract_sections_ast(content, ext)
        if ast_sections is not None:
            return ast_sections, self.PARSER_TREE_SITTER

        lines = content.splitlines()
        patterns = STRUCTURE_PATTERNS.get(ext, {})

        if not patterns:
            return self._extract_generic_sections(lines), self.PARSER_GENERIC

        sections = []
        i = 0
        current_class = None  # Track current class for method nesting

        while i < len(lines):
            line = lines[i]
            stripped = line.rstrip()
            lstripped = line.lstrip()
            indent = len(line) - len(lstripped)

            # End class scope when indent returns to class level or lower
            if current_class and indent <= current_class.get("_indent", 0) and lstripped:
                # Finalize the class's line_end
                current_class["line_end"] = i  # Previous line (0-indexed, but we display 1-indexed)
                current_class = None

            for kind, pattern in patterns.items():
                # Match against lstripped so indented methods are detected
                if re.match(pattern, lstripped, re.MULTILINE):
                    line_start = i + 1  # 1-indexed
                    line_end = self._find_block_end(lines, i, ext)

                    section = {
                        "type": self._normalize_type(kind),
                        "name": self._extract_name(kind, lstripped),
                        "line_start": line_start,
                        "line_end": line_end,
                        "signature": lstripped,
                    }

                    # Extract docstring
                    doc = self._extract_docstring(lines, i + 1, ext)
                    if doc:
                        section["doc"] = doc

                    if kind == 'decorator':
                        # Skip standalone decorator lines — they'll be captured
                        # as part of the next function/class definition
                        pass
                    elif kind in ('class', 'interface', 'enum'):
                        section["children"] = []
                        section["_indent"] = indent
                        sections.append(section)
                        current_class = section
                    elif current_class and indent > current_class.get("_indent", 0):
                        # Method inside a class
                        section["type"] = "method"
                        current_class["children"].append(section)
                    else:
                        sections.append(section)

                    break
            i += 1

        # Finalize any open class
        if current_class:
            current_class["line_end"] = len(lines)

        # Clean up internal tracking keys
        for s in sections:
            s.pop("_indent", None)
            for child in s.get("children", []):
                child.pop("_indent", None)

        return sections, self.PARSER_REGEX

    def _extract_generic_sections(self, lines: list) -> list:
        """Fallback for unknown languages — just report line count."""
        return [{"type": "content", "name": "(full file)", "line_start": 1, "line_end": len(lines)}]

    def _find_block_end(self, lines: list, start: int, ext: str) -> int:
        """Find the end line of a code block starting at `start`."""
        if ext == '.py':
            return self._find_python_block_end(lines, start)
        # For brace-based languages, find matching brace
        if ext in ('.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.c', '.cpp', '.h', '.cs'):
            return self._find_brace_block_end(lines, start)
        # Default: use indentation
        return self._find_python_block_end(lines, start)

    def _find_python_block_end(self, lines: list, start: int) -> int:
        """Find end of a Python block by indentation."""
        if start >= len(lines):
            return start + 1

        base_indent = len(lines[start]) - len(lines[start].lstrip())

        for i in range(start + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                return i  # 1-indexed
            # Check for decorators at same level (next function)
            if current_indent == base_indent and stripped.startswith('@'):
                return i
        return len(lines)

    def _find_brace_block_end(self, lines: list, start: int) -> int:
        """Find end of a brace-delimited block.

        Counts braces only in *code*, skipping any ``{`` / ``}`` that appear
        inside string/char/template literals or line/block comments. Without
        this, a brace inside a string (e.g. ``log("}")``) before the real close
        would prematurely zero the depth and truncate the block's line range.
        """
        depth = 0
        found_open = False
        # Cross-line state: which delimiter we're inside, and whether we're in a
        # block comment. Strings (", ', `) honor backslash escapes; block
        # comments and backtick (template/raw) strings span lines.
        in_string = None        # the active quote char, or None
        in_block_comment = False
        for i in range(start, len(lines)):
            line = lines[i]
            j = 0
            n = len(line)
            while j < n:
                ch = line[j]
                if in_block_comment:
                    if ch == '*' and j + 1 < n and line[j + 1] == '/':
                        in_block_comment = False
                        j += 2
                        continue
                    j += 1
                    continue
                if in_string is not None:
                    if ch == '\\' and in_string != '`':
                        # Escaped char inside a normal string/char literal.
                        j += 2
                        continue
                    if ch == in_string:
                        in_string = None
                    j += 1
                    continue
                # Not in a string or block comment — look for openers.
                if ch == '/' and j + 1 < n and line[j + 1] == '/':
                    break  # rest of the line is a line comment
                if ch == '/' and j + 1 < n and line[j + 1] == '*':
                    in_block_comment = True
                    j += 2
                    continue
                if ch in ('"', "'", '`'):
                    in_string = ch
                    j += 1
                    continue
                if ch == '{':
                    depth += 1
                    found_open = True
                elif ch == '}':
                    depth -= 1
                    if found_open and depth == 0:
                        return i + 1  # 1-indexed
                j += 1
        return len(lines)

    def _normalize_type(self, kind: str) -> str:
        """Map pattern kind to standard section type."""
        mapping = {
            'arrow': 'function',
            'assignment': 'constant',
            'library': 'import',
            'export': 'function',
            'decorator': 'decorator',
        }
        return mapping.get(kind, kind)

    def _extract_name(self, kind: str, line: str) -> str:
        """Extract the name from a matched line."""
        if kind in ('import', 'library'):
            return line.strip()

        # Try to extract identifier from common patterns
        # class Foo, def foo, function foo, const foo, etc.
        m = re.match(r'.*?(?:class|def|function|interface|enum|type|const|let|var)\s+(\w+)', line)
        if m:
            return m.group(1)

        # Assignment: FOO_BAR = ...
        m = re.match(r'^([A-Z_][A-Z_0-9]*)\s*=', line.strip())
        if m:
            return m.group(1)

        # Arrow: const foo = (...) =>
        m = re.match(r'(?:export\s+)?(?:const|let|var)\s+(\w+)', line.strip())
        if m:
            return m.group(1)

        return line.strip()[:50]

    def _extract_docstring(self, lines: list, start: int, ext: str) -> Optional[str]:
        """Extract first line of docstring/JSDoc if present."""
        if start >= len(lines):
            return None
        line = lines[start].strip()

        if ext == '.py' and (line.startswith('"""') or line.startswith("'''")):
            quote = line[:3]
            if line.endswith(quote) and len(line) > 6:
                return line[3:-3].strip()
            first = line[3:].strip()
            if first:
                return first
            if start + 1 < len(lines):
                return lines[start + 1].strip()
        elif line.startswith('/**'):
            for j in range(start, min(start + 10, len(lines))):
                cleaned = lines[j].strip().lstrip('/*').rstrip('*/').strip()
                if cleaned:
                    return cleaned
                if '*/' in lines[j]:
                    break
        return None

    def _format_map(self, record: dict) -> str:
        """Canonical map (services/file_map.render_map). Kept as a name."""
        return render_map(record)

    def _extract_params(self, signature: str) -> str:
        """Extract parameter list from a function signature."""
        m = re.search(r'\(([^)]*)\)', signature)
        if m:
            params = m.group(1).strip()
            # Shorten if too long
            if len(params) > 60:
                params = params[:57] + "..."
            return params
        return ""

    def _format_dense_map(self, record: dict) -> str:
        """Retired: there is one map now (2.121.0)."""
        return render_map(record)
