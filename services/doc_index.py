"""DocIndex — Document-aware chunking layer for project docs, configs, and docstrings.

Chunks markdown by heading boundaries, extracts inline docstrings from code,
and indexes config files as whole-file chunks. Each chunk carries a priority
score based on its source type and file importance.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from core import count_tokens

log = logging.getLogger(__name__)

# Files that get boosted priority (pattern -> priority multiplier)
_PRIORITY_FILES = {
    "CLAUDE.md": 2.0,
    "AGENTS.md": 1.8,
    "README.md": 1.5,
    "CONTRIBUTING.md": 1.3,
    "ARCHITECTURE.md": 1.5,
    "CHANGELOG.md": 0.8,
}

# Priority by source type
_SOURCE_PRIORITY = {
    "markdown": 1.2,
    "docstring": 1.0,
    "config": 0.8,
}

# Max tokens per chunk before splitting
_CHUNK_MAX_TOKENS = 400

# Config file patterns to index
_CONFIG_PATTERNS = [
    ".mcp.json", "pyproject.toml", "package.json", "tsconfig.json",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example",
]


class DocIndex:
    """Document-aware index for project docs, configs, and docstrings."""

    def __init__(self, project_path: str, index_dir: str = ".c3/doc_index"):
        self.project_path = Path(project_path)
        self.index_dir = self.project_path / index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.chunks: dict = {}  # chunk_id -> chunk dict
        self._file_hashes: dict = {}  # rel_path -> content hash
        self._hash_file = self.index_dir / "file_hashes.json"
        self._index_file = self.index_dir / "index.json"

        self._load_hashes()
        self._load_index()

    # --- Persistence ---

    def _load_hashes(self):
        if self._hash_file.exists():
            try:
                self._file_hashes = json.loads(self._hash_file.read_text(encoding="utf-8"))
            except Exception:
                self._file_hashes = {}

    def _save_hashes(self):
        self._hash_file.write_text(json.dumps(self._file_hashes), encoding="utf-8")

    def _load_index(self):
        if self._index_file.exists():
            try:
                data = json.loads(self._index_file.read_text(encoding="utf-8"))
                self.chunks = data.get("chunks", {})
            except Exception:
                self.chunks = {}

    def _save_index(self):
        self._index_file.write_text(
            json.dumps({"chunks": self.chunks}, default=str),
            encoding="utf-8",
        )

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # --- Build ---

    def build(self, force: bool = False) -> dict:
        """Build or incrementally update the doc index."""
        stats = {"docs_indexed": 0, "chunks_created": 0, "skipped": 0}

        files_to_index = self._discover_files()
        old_hashes = dict(self._file_hashes)
        new_hashes = {}

        for rel_path, fpath in files_to_index:
            try:
                content = fpath.read_text(errors="replace")
            except Exception:
                continue

            h = self._content_hash(content)
            new_hashes[rel_path] = h

            if not force and old_hashes.get(rel_path) == h:
                stats["skipped"] += 1
                continue

            # Remove old chunks for this file
            self._remove_file_chunks(rel_path)

            # Chunk based on file type
            ext = fpath.suffix.lower()
            name = fpath.name

            if ext in (".md", ".mdx", ".rst", ".adoc"):
                new_chunks = self._chunk_markdown(content, rel_path, name)
            elif ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"):
                new_chunks = self._chunk_docstrings(content, rel_path, ext)
            elif name in _CONFIG_PATTERNS or ext in (".toml", ".yaml", ".yml", ".json", ".ini"):
                new_chunks = self._chunk_config(content, rel_path, name)
            else:
                continue

            for chunk in new_chunks:
                self.chunks[chunk["id"]] = chunk

            stats["docs_indexed"] += 1
            stats["chunks_created"] += len(new_chunks)

        # Remove chunks for deleted files
        deleted = set(old_hashes.keys()) - set(new_hashes.keys())
        for rel_path in deleted:
            self._remove_file_chunks(rel_path)

        self._file_hashes = new_hashes
        self._save_hashes()
        self._save_index()

        log.info("DocIndex built: %s", stats)
        return stats

    def _remove_file_chunks(self, doc_id: str):
        to_remove = [cid for cid, c in self.chunks.items() if c.get("doc_id") == doc_id]
        for cid in to_remove:
            del self.chunks[cid]

    def _discover_files(self) -> list[tuple[str, Path]]:
        """Find all doc, config, and code files to index."""
        skip_dirs = {
            "node_modules", ".git", "__pycache__", ".c3", "venv",
            "env", ".venv", "dist", "build", ".next", ".cache",
            "coverage", ".pytest_cache",
        }

        files = []

        # Markdown docs
        for ext in ("*.md", "*.mdx", "*.rst", "*.adoc"):
            for fpath in self.project_path.rglob(ext):
                if any(skip in fpath.parts for skip in skip_dirs):
                    continue
                rel = str(fpath.relative_to(self.project_path))
                files.append((rel, fpath))

        # Config files at project root
        for pattern in _CONFIG_PATTERNS:
            fpath = self.project_path / pattern
            if fpath.is_file():
                files.append((pattern, fpath))

        # Code files for docstring extraction (top-level only + key dirs)
        code_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java"}
        code_dirs = [self.project_path]
        for subdir in ("cli", "core", "services", "src", "lib", "app", "pkg"):
            d = self.project_path / subdir
            if d.is_dir():
                code_dirs.append(d)

        seen = set()
        for code_dir in code_dirs:
            for fpath in code_dir.glob("*"):
                if not fpath.is_file():
                    continue
                if fpath.suffix.lower() not in code_exts:
                    continue
                rel = str(fpath.relative_to(self.project_path))
                if rel not in seen:
                    seen.add(rel)
                    files.append((rel, fpath))

        return files

    # --- Markdown chunking ---

    def _chunk_markdown(self, content: str, doc_id: str, filename: str) -> list:
        """Split markdown by heading boundaries."""
        lines = content.split("\n")
        sections = []
        current_heading = filename
        current_lines = []
        current_start = 0
        heading_path = [filename]

        for i, line in enumerate(lines):
            heading_match = re.match(r"^(#{1,6})\s+(.+)", line)
            if heading_match:
                # Flush previous section
                if current_lines:
                    text = "\n".join(current_lines).strip()
                    if text:
                        sections.append(self._make_doc_chunk(
                            doc_id, current_heading, text,
                            heading_path[:], current_start, i - 1,
                            "markdown", filename,
                        ))
                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                current_heading = f"{heading_match.group(1)} {heading_text}"
                # Update heading path
                heading_path = heading_path[:1]  # keep filename
                if level > 1:
                    heading_path.append(heading_text)
                current_lines = [line]
                current_start = i
            else:
                current_lines.append(line)

        # Flush last section
        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append(self._make_doc_chunk(
                    doc_id, current_heading, text,
                    heading_path[:], current_start, len(lines) - 1,
                    "markdown", filename,
                ))

        # Split oversized chunks
        result = []
        for chunk in sections:
            tokens = chunk["tokens"]
            if tokens > _CHUNK_MAX_TOKENS:
                result.extend(self._split_chunk(chunk))
            else:
                result.append(chunk)

        return result

    def _split_chunk(self, chunk: dict) -> list:
        """Split an oversized chunk into smaller pieces."""
        lines = chunk["content"].split("\n")
        parts = []
        current = []
        current_tokens = 0

        for line in lines:
            line_tokens = count_tokens(line)
            if current_tokens + line_tokens > _CHUNK_MAX_TOKENS and current:
                parts.append("\n".join(current))
                current = [line]
                current_tokens = line_tokens
            else:
                current.append(line)
                current_tokens += line_tokens

        if current:
            parts.append("\n".join(current))

        result = []
        for idx, part_text in enumerate(parts):
            part_text = part_text.strip()
            if not part_text:
                continue
            c = dict(chunk)
            c["id"] = f"{chunk['id']}::{idx}" if idx > 0 else chunk["id"]
            c["content"] = part_text
            c["tokens"] = count_tokens(part_text)
            result.append(c)

        return result

    # --- Docstring chunking ---

    def _chunk_docstrings(self, content: str, doc_id: str, ext: str) -> list:
        """Extract module and symbol docstrings from code files."""
        chunks = []

        if ext == ".py":
            chunks.extend(self._extract_python_docstrings(content, doc_id))
        elif ext in (".js", ".ts", ".tsx", ".jsx"):
            chunks.extend(self._extract_jsdoc_comments(content, doc_id))

        return chunks

    def _extract_python_docstrings(self, content: str, doc_id: str) -> list:
        """Extract Python module and class/function docstrings."""
        chunks = []
        lines = content.split("\n")

        # Module docstring: triple-quote at the start (within first 10 lines)
        module_doc = self._find_python_docstring(lines, 0)
        if module_doc:
            chunks.append(self._make_doc_chunk(
                doc_id, f"{doc_id}::module", module_doc["text"],
                [doc_id], module_doc["start"], module_doc["end"],
                "docstring", doc_id,
            ))

        # Class and function docstrings
        pattern = re.compile(r"^\s*(class|def|async\s+def)\s+(\w+)")
        for i, line in enumerate(lines):
            m = pattern.match(line)
            if m:
                kind = m.group(1).replace("async ", "")
                name = m.group(2)
                # Look for docstring on the next non-empty line after the def/class line
                doc = self._find_python_docstring(lines, i + 1)
                if doc and doc["text"]:
                    chunks.append(self._make_doc_chunk(
                        doc_id, f"{doc_id}::{name}", doc["text"],
                        [doc_id, name], doc["start"], doc["end"],
                        "docstring", doc_id,
                    ))

        return chunks

    def _find_python_docstring(self, lines: list, start: int) -> Optional[dict]:
        """Find a triple-quoted docstring starting near `start`."""
        # Skip blank lines and decorators
        i = start
        while i < len(lines) and i < start + 5:
            stripped = lines[i].strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                break
            if stripped == "" or stripped.startswith("@") or stripped.startswith("#"):
                i += 1
                continue
            # If line contains colon (def/class body start), skip to next line
            if stripped.endswith(":"):
                i += 1
                continue
            return None
        else:
            return None

        if i >= len(lines):
            return None

        quote = lines[i].strip()[:3]
        doc_start = i

        # Single-line docstring
        if lines[i].strip().count(quote) >= 2:
            text = lines[i].strip().strip(quote).strip()
            if text:
                return {"text": text, "start": doc_start, "end": doc_start}
            return None

        # Multi-line docstring
        doc_lines = [lines[i].strip().lstrip(quote)]
        i += 1
        while i < len(lines):
            if quote in lines[i]:
                doc_lines.append(lines[i].strip().rstrip(quote).strip())
                break
            doc_lines.append(lines[i].strip())
            i += 1

        text = "\n".join(doc_lines).strip()
        if len(text) < 10:  # skip trivial docstrings
            return None
        return {"text": text, "start": doc_start, "end": i}

    def _extract_jsdoc_comments(self, content: str, doc_id: str) -> list:
        """Extract JSDoc comments (/** ... */) from JS/TS files."""
        chunks = []
        pattern = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)

        for match in pattern.finditer(content):
            text = match.group(1).strip()
            # Clean up JSDoc formatting
            clean_lines = []
            for line in text.split("\n"):
                line = line.strip().lstrip("* ").strip()
                if line:
                    clean_lines.append(line)
            text = "\n".join(clean_lines)

            if len(text) < 20:  # skip trivial comments
                continue

            start_line = content[:match.start()].count("\n")
            end_line = content[:match.end()].count("\n")

            chunks.append(self._make_doc_chunk(
                doc_id, f"{doc_id}::jsdoc:{start_line}",
                text, [doc_id], start_line, end_line,
                "docstring", doc_id,
            ))

        return chunks

    # --- Config chunking ---

    def _chunk_config(self, content: str, doc_id: str, filename: str) -> list:
        """Index config files as whole-file chunks."""
        tokens = count_tokens(content)
        if tokens < 5:
            return []

        # For large configs, only keep the first _CHUNK_MAX_TOKENS worth
        if tokens > _CHUNK_MAX_TOKENS:
            lines = content.split("\n")
            truncated = []
            running = 0
            for line in lines:
                lt = count_tokens(line)
                if running + lt > _CHUNK_MAX_TOKENS:
                    break
                truncated.append(line)
                running += lt
            content = "\n".join(truncated)
            tokens = running

        return [self._make_doc_chunk(
            doc_id, f"{doc_id}::config", content,
            [filename], 0, content.count("\n"),
            "config", filename,
        )]

    # --- Helpers ---

    def _make_doc_chunk(self, doc_id: str, chunk_id: str, content: str,
                        heading_path: list, line_start: int, line_end: int,
                        source_type: str, filename: str) -> dict:
        """Create a standardized chunk dict with priority scoring."""
        # Calculate priority
        base_priority = _SOURCE_PRIORITY.get(source_type, 1.0)
        file_boost = 1.0
        for pattern, boost in _PRIORITY_FILES.items():
            if filename == pattern or doc_id.endswith(pattern):
                file_boost = boost
                break

        # Docs in a docs/ directory get a small boost
        if doc_id.startswith("docs/") or doc_id.startswith("docs\\"):
            file_boost = max(file_boost, 1.2)

        priority = round(base_priority * file_boost, 2)

        return {
            "id": chunk_id,
            "doc_id": doc_id,
            "content": content,
            "tokens": count_tokens(content),
            "kind": "doc",
            "heading_path": heading_path,
            "source_type": source_type,
            "priority": priority,
            "line_start": line_start,
            "line_end": line_end,
        }

    # --- Search ---

    def search(self, query: str, top_k: int = 5) -> list:
        """Simple TF-IDF-like keyword search over doc chunks."""
        if not self.chunks:
            return []

        query_tokens = set(re.findall(r"\w+", query.lower()))
        if not query_tokens:
            return []

        scored = []
        for cid, chunk in self.chunks.items():
            content_tokens = set(re.findall(r"\w+", chunk["content"].lower()))
            if not content_tokens:
                continue

            # Jaccard-like overlap
            overlap = query_tokens & content_tokens
            if not overlap:
                continue

            score = len(overlap) / len(query_tokens | content_tokens)

            # Boost by heading path match
            heading_text = " ".join(chunk.get("heading_path", [])).lower()
            heading_tokens = set(re.findall(r"\w+", heading_text))
            heading_overlap = query_tokens & heading_tokens
            if heading_overlap:
                score += 0.3 * len(heading_overlap) / len(query_tokens)

            # Apply priority multiplier
            score *= chunk.get("priority", 1.0)

            scored.append((cid, score))

        scored.sort(key=lambda x: x[1], reverse=True)

        results = []
        for cid, score in scored[:top_k]:
            chunk = self.chunks[cid]
            results.append({
                **chunk,
                "score": round(score, 4),
            })

        return results

    def search_semantic(self, query: str, embedding_index, top_k: int = 5) -> list:
        """Search using embedding index if available, falling back to keyword."""
        # For now, keyword search is the primary method for docs.
        # Semantic search via EmbeddingIndex searches code chunks.
        # We use keyword search here which works well for docs.
        return self.search(query, top_k=top_k)

    def get_stats(self) -> dict:
        by_type = {}
        for c in self.chunks.values():
            st = c.get("source_type", "unknown")
            by_type[st] = by_type.get(st, 0) + 1

        return {
            "total_chunks": len(self.chunks),
            "files_tracked": len(self._file_hashes),
            "by_source_type": by_type,
        }
