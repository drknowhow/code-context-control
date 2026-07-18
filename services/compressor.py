"""
AST-based Code Compression Service

Parses source files into AST and generates compressed summaries:
- Function/class signatures only (skip bodies)
- Structural outline mode
- Smart truncation with context preservation
- Diff-only mode for edits
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from core import count_tokens, measure_savings
from services.parser import HAS_TREE_SITTER, extract_sections_ast

# Language-specific comment patterns
COMMENT_PATTERNS = {
    '.py': (r'#.*$', r'"""[\s\S]*?"""', r"'''[\s\S]*?'''"),
    '.js': (r'//.*$', r'/\*[\s\S]*?\*/'),
    '.ts': (r'//.*$', r'/\*[\s\S]*?\*/'),
    '.tsx': (r'//.*$', r'/\*[\s\S]*?\*/'),
    '.jsx': (r'//.*$', r'/\*[\s\S]*?\*/'),
    '.r': (r'#.*$',),
    '.R': (r'#.*$',),
}

# Regex-based structural extractors (fallback when tree-sitter unavailable)
STRUCTURE_PATTERNS = {
    '.py': {
        'class': r'^class\s+(\w+).*?:',
        'function': r'^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^:]+))?:',
        'import': r'^(?:from\s+\S+\s+)?import\s+.+$',
        'decorator': r'^@\w+',
        'assignment': r'^([A-Z_][A-Z_0-9]*)\s*=',
    },
    '.js': {
        'class': r'^(?:export\s+)?class\s+(\w+)',
        'function': r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)',
        'arrow': r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>',
        'import': r'^import\s+.+$',
        'export': r'^export\s+(?:default\s+)?(?:const|let|var|function|class)\s+(\w+)',
    },
    '.ts': {
        'interface': r'^(?:export\s+)?interface\s+(\w+)',
        'type': r'^(?:export\s+)?type\s+(\w+)',
        'class': r'^(?:export\s+)?class\s+(\w+)',
        'function': r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[<(]',
        'arrow': r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*.+?\s*)?=\s*(?:async\s+)?\([^)]*\)\s*=>',
        'import': r'^import\s+.+$',
        'enum': r'^(?:export\s+)?enum\s+(\w+)',
    },
    '.r': {
        'function': r'^(\w+)\s*<-\s*function\s*\(([^)]*)\)',
        'assignment': r'^(\w+)\s*<-',
        'library': r'^(?:library|require)\s*\(.+\)',
    },
    '.R': {
        'function': r'^(\w+)\s*<-\s*function\s*\(([^)]*)\)',
        'assignment': r'^(\w+)\s*<-',
        'library': r'^(?:library|require)\s*\(.+\)',
    },
}

# Extend for more file types
for ext in ['.tsx', '.jsx']:
    STRUCTURE_PATTERNS[ext] = STRUCTURE_PATTERNS['.ts'].copy()


# ── Large-file fast path (P8a) ──────────────────────────────────────────────
# Files exceeding EITHER threshold skip the full regex/AST structural pipeline
# and get a cheap header built from a bounded scan of the file head instead.
# Module-level so deployments (and tests) can tune them.
LARGE_FILE_LINE_THRESHOLD = 10_000       # fast path when line count >= this
LARGE_FILE_BYTE_THRESHOLD = 200 * 1024   # fast path when file size >= this (~200 KB)
LARGE_FILE_SCAN_BYTES = 64 * 1024        # bounded scan window for the header preview
LARGE_FILE_PREVIEW_LINES = 30            # max import/signature lines in the preview

LARGE_FILE_GUIDANCE = (
    "file too large for full compression — use c3_compress(mode='diff'), "
    "c3_read(lines=[start,end]), or c3_search to target sections"
)

# Human-readable language names for the fast-path header (keys are lowercase
# extensions — compress_file lowercases the suffix before lookup). Display
# names only; this does NOT add compression support for new languages.
_EXT_LANGUAGE_NAMES = {
    '.py': 'Python',
    '.js': 'JavaScript', '.jsx': 'JavaScript (JSX)',
    '.ts': 'TypeScript', '.tsx': 'TypeScript (TSX)',
    '.r': 'R',
    '.go': 'Go', '.rs': 'Rust', '.java': 'Java', '.c': 'C', '.cpp': 'C++',
    '.cs': 'C#', '.rb': 'Ruby', '.php': 'PHP', '.swift': 'Swift',
    '.kt': 'Kotlin', '.scala': 'Scala', '.lua': 'Lua', '.pl': 'Perl',
    '.html': 'HTML', '.htm': 'HTML', '.css': 'CSS',
    '.json': 'JSON', '.yaml': 'YAML', '.yml': 'YAML', '.toml': 'TOML',
    '.xml': 'XML', '.md': 'Markdown', '.sh': 'Shell', '.ps1': 'PowerShell',
    '.bat': 'Batch', '.sql': 'SQL', '.csv': 'CSV', '.txt': 'Plain text',
}

# Per-process memo of extensions whose AST/tree-sitter parse RAISED:
# {ext: "ExcType: message"}. Once an extension lands here, subsequent files
# with the same extension skip the AST attempt and go straight to
# regex/generic extraction instead of re-attempting a known-failing parse.
AST_PARSE_FAILURES: dict = {}


PROTECTED_COMPRESS_FILES = {
    "cli/c3.py",
    "cli/ui.html",
    "cli/docs.html",
    "core/config.py",
    "CLAUDE.md",
    "GEMINI.md",
    "AGENTS.md",
    "README.md",
    "c3.bat",
    "install.bat",
    "install.sh",
    "pyproject.toml",
    ".codex/config.toml",
    ".vscode/mcp.json",
    ".gemini/settings.json",
}


class CodeCompressor:
    """Compresses source code files into token-efficient summaries."""

    def __init__(self, cache_dir: str = ".c3/cache",
                 project_root: Optional[str] = None,
                 protected_files: Optional[Iterable[str]] = None,
                 router: Optional[Any] = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._file_hashes = {}
        self._mem_cache = {}  # In-memory LRU: {cache_key: result} — skips JSON parse on repeat access
        self._MEM_CACHE_MAX = 128
        self.project_root = (Path(project_root).resolve()
                             if project_root else self.cache_dir.parent.parent.resolve())
        self._protected_files = set(PROTECTED_COMPRESS_FILES)
        if protected_files:
            self._protected_files.update(self._normalize_rel_path(p) for p in protected_files)
        self.router = router

    @staticmethod
    def _normalize_rel_path(path: str) -> str:
        return str(path).replace("\\", "/").lstrip("./")

    def _relative_to_project(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except Exception:
            return self._normalize_rel_path(str(path))

    def is_protected_file(self, filepath: Path) -> bool:
        normalized = self._normalize_rel_path(self._relative_to_project(filepath))
        return normalized in self._protected_files

    def get_protected_files(self) -> list:
        return sorted(self._protected_files)

    def compress_file(self, filepath: str, mode: str = "structure") -> dict:
        """
        Compress a source file.

        Modes:
        - "structure": Function/class signatures + imports (most compressed)
        - "outline": Structure + docstrings + key comments
        - "smart": Adaptive - more detail for small files, less for large
        - "diff": Only changes since last seen (requires prior state)
        - "summary": High-level LLM summary (requires router)
        - "bug_scan": Structure map + annotated exception-handling hotspots with line numbers
        """
        filepath = Path(filepath).resolve()
        if not filepath.exists():
            return {"error": f"File not found: {filepath}", "compressed": ""}
        if self.is_protected_file(filepath):
            return {
                "error": f"Compression is blocked for protected file: {self._relative_to_project(filepath)}",
                "compressed": "",
                "protected_files": self.get_protected_files(),
            }

        content = filepath.read_text(encoding="utf-8", errors='replace')
        content_hash = hashlib.md5(content.encode()).hexdigest()
        ext = filepath.suffix.lower()

        # Check persistent cache (except for diff/summary which have their own logic)
        if mode not in ("diff", "summary"):
            cache_key = f"{content_hash}_{mode}{ext}.json"
            # Fast path: in-memory cache (no JSON parse / disk I/O)
            if cache_key in self._mem_cache:
                hit = dict(self._mem_cache[cache_key])
                hit["filepath"] = str(filepath)
                return hit
            cache_file = self.cache_dir / cache_key
            if cache_file.exists():
                try:
                    with open(cache_file, encoding="utf-8") as f:
                        cached_result = json.load(f)
                    cached_result["filepath"] = str(filepath)
                    self._mem_cache[cache_key] = cached_result
                    return cached_result
                except Exception:
                    pass

        # Large-file fast path (P8a): files exceeding either threshold skip
        # the full regex/AST structural pipeline and get a cheap bounded-scan
        # header instead. Runs AFTER the cache check so pre-existing cache
        # entries (keyed on content hash) still win. diff/summary are exempt:
        # diff is the recommended targeted alternative and summary delegates
        # to the router.
        if mode not in ("diff", "summary"):
            line_count = content.count("\n")
            if content and not content.endswith("\n"):
                line_count += 1
            try:
                byte_size = filepath.stat().st_size
            except OSError:
                byte_size = len(content.encode("utf-8", errors="replace"))
            if (line_count >= LARGE_FILE_LINE_THRESHOLD
                    or byte_size >= LARGE_FILE_BYTE_THRESHOLD):
                return self._compress_large_file(
                    filepath, content, content_hash, ext, mode,
                    byte_size, line_count)

        if mode == "diff":
            return self._diff_compress(filepath, content)

        if mode == "summary":
            if not self.router:
                return {"error": "Summary mode requires a router", "compressed": ""}
            sum_res = self.router.summarize(content, style="concise")
            summary = sum_res.get("summary", "Could not summarize")
            result = f"# {filepath.name} — SUMMARY\n{summary}"
            return {"compressed": result, "mode": "summary", **measure_savings(content, result)}

        if mode == "bug_scan":
            # Structure map + exception-handling annotation pass
            structure = self._extract_structure(content, ext, "outline")
            exception_section = self._scan_exception_handlers(content)
            compressed_parts = [structure]
            if exception_section:
                compressed_parts.append(exception_section)
            compressed = "\n".join(compressed_parts)
            header = f"# {filepath.name} ({filepath.suffix}) — {len(content.splitlines())} lines [bug_scan]\n"
            result = header + compressed
            savings = measure_savings(content, result)
            savings["compressed"] = result
            savings["mode"] = "bug_scan"
            savings["filepath"] = str(filepath)
            self._file_hashes[str(filepath)] = content_hash
            cache_key = f"{content_hash}_bug_scan{ext}.json"
            self._mem_cache[cache_key] = dict(savings)
            cache_file = self.cache_dir / cache_key
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(savings, f, indent=2)
            except Exception:
                pass
            return savings

        if mode == "smart":
            tokens = count_tokens(content)
            if tokens < 80:
                return {"compressed": content, "mode": "full", **measure_savings(content, content)}
            elif tokens < 400:
                actual_mode = "outline"
                compressed = self._extract_structure(content, ext, "outline")
            else:
                # Try structure first; if savings < 30%, fall back to outline
                compressed = self._extract_structure(content, ext, "structure")
                structure_tokens = count_tokens(compressed)
                if tokens > 0 and (1 - structure_tokens / tokens) < 0.30:
                    actual_mode = "outline"
                    compressed = self._extract_structure(content, ext, "outline")
                else:
                    actual_mode = "structure"
                    # Reuse already-computed structure — no second call needed
        else:
            actual_mode = mode
            compressed = self._extract_structure(content, ext, actual_mode)

        # Build result
        header = f"# {filepath.name} ({filepath.suffix}) — {len(content.splitlines())} lines\n"
        result = header + compressed

        savings = measure_savings(content, result)
        savings["compressed"] = result
        savings["mode"] = mode
        savings["filepath"] = str(filepath)

        # Cache hash for diff mode
        self._file_hashes[str(filepath)] = content_hash

        # Persist to cache (except diff which uses its own file format)
        if mode not in ("diff", "summary"):
            cache_key = f"{content_hash}_{mode}{ext}.json"
            # Store in memory first (fast path for repeat access)
            if len(self._mem_cache) >= self._MEM_CACHE_MAX:
                # Evict oldest quarter
                keys = list(self._mem_cache.keys())
                for k in keys[:len(keys) // 4]:
                    del self._mem_cache[k]
            self._mem_cache[cache_key] = dict(savings)
            cache_file = self.cache_dir / cache_key
            try:
                with open(cache_file, 'w', encoding="utf-8") as f:
                    json.dump(savings, f, indent=2)
            except Exception:
                pass

        return savings

    def _compress_large_file(self, filepath: Path, content: str,
                             content_hash: str, ext: str, mode: str,
                             byte_size: int, line_count: int) -> dict:
        """Large-file fast path (P8a): cheap structural header, bounded scan.

        Skips the full regex/AST pipeline entirely. The result keeps the same
        contract as a normal compress result — ``compressed``/``mode``/
        ``filepath`` plus the measure_savings keys — so callers and the
        raw->compressed token accounting are unaffected.
        """
        language = _EXT_LANGUAGE_NAMES.get(ext, ext.lstrip(".") or "unknown")
        preview = self._scan_head_signatures(content, ext)

        parts = [
            f"# {filepath.name} ({ext}) — {line_count} lines, "
            f"{byte_size:,} bytes — {language} [large-file fast path]",
            f"# NOTE: {LARGE_FILE_GUIDANCE}",
        ]
        if preview:
            scanned_kb = max(1, LARGE_FILE_SCAN_BYTES // 1024)
            parts.append(
                f"\n## Imports/signatures from the first ~{scanned_kb} KB "
                f"(max {LARGE_FILE_PREVIEW_LINES} lines):")
            parts.extend(preview)
        result = "\n".join(parts)

        savings = measure_savings(content, result)
        savings["compressed"] = result
        savings["mode"] = mode
        savings["filepath"] = str(filepath)
        savings["fast_path"] = "large_file"
        savings["line_count"] = line_count
        savings["byte_size"] = byte_size

        # Same bookkeeping as the normal path: hash for diff mode + cache
        # store under the standard content-hash key. Existing disk-cache
        # entries are untouched (same key format, and the earlier cache check
        # already returned them before reaching this point).
        self._file_hashes[str(filepath)] = content_hash
        cache_key = f"{content_hash}_{mode}{ext}.json"
        if len(self._mem_cache) >= self._MEM_CACHE_MAX:
            keys = list(self._mem_cache.keys())
            for k in keys[:len(keys) // 4]:
                del self._mem_cache[k]
        self._mem_cache[cache_key] = dict(savings)
        cache_file = self.cache_dir / cache_key
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(savings, f, indent=2)
        except Exception:
            pass

        return savings

    def _scan_head_signatures(self, content: str, ext: str) -> list:
        """Bounded scan for the large-file fast path header.

        Collects up to LARGE_FILE_PREVIEW_LINES import/signature lines from
        the first ~LARGE_FILE_SCAN_BYTES characters of the file (chars ≈
        bytes for typical source). Uses the extension's STRUCTURE_PATTERNS
        when known, else a generic declaration heuristic. Never scans beyond
        the window — that bound is the whole point of the fast path.
        """
        window = content[:LARGE_FILE_SCAN_BYTES]
        if len(content) > len(window):
            # Drop the trailing partial line so regexes only see whole lines
            cut = window.rfind("\n")
            if cut > 0:
                window = window[:cut]

        patterns = STRUCTURE_PATTERNS.get(ext)
        found = []
        for line in window.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if patterns:
                for pattern in patterns.values():
                    if re.match(pattern, stripped, re.MULTILINE):
                        found.append(stripped)
                        break
            else:
                lowered = stripped.lower()
                if (any(kw in lowered for kw in (
                        'import', 'require', 'include', 'function', 'class ',
                        'def ', 'module', 'export', 'package '))
                        or re.match(r'^[A-Za-z_]\w*\s*[=({<]', stripped)):
                    found.append(stripped)
            if len(found) >= LARGE_FILE_PREVIEW_LINES:
                break
        return found

    def _extract_structure(self, content: str, ext: str, mode: str) -> str:
        """Extract structural elements from source code."""
        # 1. Try Tree-sitter AST extraction first (if available and not
        #    disabled). Extensions whose parse previously RAISED in this
        #    process are memoized in AST_PARSE_FAILURES and skip straight to
        #    regex/generic — no repeated attempt-and-catch per file (P8a).
        if HAS_TREE_SITTER and ext not in AST_PARSE_FAILURES:
            sections = None
            try:
                sections = extract_sections_ast(content, ext)
            except Exception as exc:
                # Memoize the parse failure for this extension so later files
                # with the same extension go straight to the regex fallback.
                AST_PARSE_FAILURES[ext] = f"{type(exc).__name__}: {exc}"
            if sections:
                try:
                    return self._render_ast_sections(sections, content, mode)
                except Exception:
                    # Render failure is not a parser failure — do not memoize;
                    # fall back to regex for this file only.
                    pass

        # 2. Fall back to regex-based extraction
        lines = content.split('\n')
        patterns = STRUCTURE_PATTERNS.get(ext, {})

        if not patterns:
            return self._generic_compress(content, ext)

        extracted = []
        i = 0
        indent_stack = []  # (indent_level, kind) for nesting context (2a)

        while i < len(lines):
            line = lines[i]
            stripped = line.rstrip()
            lstripped = line.lstrip()
            indent = len(line) - len(lstripped)

            # Update indent stack — pop entries at same or lower indent (2a)
            while indent_stack and indent <= indent_stack[-1][0] and lstripped:
                indent_stack.pop()

            matched = False
            for kind, pattern in patterns.items():
                if re.match(pattern, lstripped, re.MULTILINE):
                    matched = True

                    # Compute hierarchical prefix from indent stack (2a)
                    nesting_prefix = "  " * len(indent_stack)

                    if kind in ('import', 'library'):
                        extracted.append(stripped)
                    elif kind == 'decorator':
                        extracted.append(f"{nesting_prefix}{stripped}")
                    elif kind in ('class', 'interface', 'enum', 'type'):
                        extracted.append(f"\n{nesting_prefix}{stripped}")
                        indent_stack.append((indent, kind))
                        if mode == "outline":
                            doc = self._extract_docstring(lines, i + 1, ext)
                            if doc:
                                extracted.append(f"{nesting_prefix}  {doc}")
                    elif kind in ('function', 'arrow'):
                        extracted.append(f"{nesting_prefix}{stripped}")
                        if mode == "outline":
                            doc = self._extract_docstring(lines, i + 1, ext)
                            if doc:
                                extracted.append(f"{nesting_prefix}  {doc}")
                    elif kind == 'assignment':
                        extracted.append(f"{nesting_prefix}{stripped}")
                        # Capture multi-line assignments up to 3 continuation lines (2c)
                        if stripped.rstrip().endswith((',', '{', '[', '(')):
                            for j in range(1, 4):
                                if i + j < len(lines):
                                    cont = lines[i + j].rstrip()
                                    if cont.strip():
                                        extracted.append(f"{nesting_prefix}  {cont.strip()}")
                                    if not cont.rstrip().endswith((',', '{', '[', '(')):
                                        break
                    elif kind == 'export':
                        extracted.append(f"{nesting_prefix}{stripped}")
                    break

            i += 1

        return '\n'.join(extracted)

    def _render_ast_sections(self, sections: list, content: str, mode: str) -> str:
        """Convert Tree-sitter sections into a compressed text summary."""
        lines = content.splitlines()
        extracted = []

        # Track imports separately to group them at top
        imports = [s for s in sections if s.get("type") == "import"]
        if imports:
            for s in imports:
                line_idx = s["line_start"] - 1
                if 0 <= line_idx < len(lines):
                    extracted.append(lines[line_idx].strip())
            extracted.append("")

        # Depth-first traversal of classes and functions
        def _render_node(node_list, depth=0):
            prefix = "  " * depth
            for s in node_list:
                stype = s.get("type")
                if stype == "import":
                    continue

                name = s.get("name", "unnamed")
                start, end = s["line_start"], s["line_end"]
                if 1 <= start <= len(lines):
                    # For signature extraction:
                    # Take up to the first 3 lines of the section to capture multi-line signatures
                    sig_lines = lines[start-1:min(start+2, end)]
                    # Heuristic: stop at the first line ending with { or :
                    sig_found = False
                    decl = ""
                    for line in sig_lines:
                        clean = line.strip()
                        decl += " " + clean
                        if any(clean.endswith(c) for c in (':', '{')):
                            sig_found = True
                            break

                    decl = decl.strip()
                    if decl.endswith("{") or decl.endswith(":"):
                        decl = decl[:-1].strip()

                    if stype == "class":
                        # Use the language-appropriate declaration (preserves
                        # extends/implements/generics/visibility for JS/TS/Java/
                        # Go/Rust). Fall back to Python-style only when empty.
                        class_decl = decl if decl else f"class {name}:"
                        extracted.append(f"\n{prefix}{class_decl}")
                    else:
                        extracted.append(f"{prefix}{decl}")

                    if mode == "outline":
                        # Find docstring if it's within the section (start is already 1-indexed)
                        doc = self._extract_docstring(lines, start, "")
                        if doc:
                            extracted.append(f"{prefix}  \"\"\" {doc} \"\"\"")

                if "children" in s and s["children"]:
                    _render_node(s["children"], depth + 1)

        _render_node([s for s in sections if s.get("type") != "import"])
        return "\n".join(extracted)

    def _extract_docstring(self, lines: list, start: int, ext: str) -> Optional[str]:
        """Extract docstring/JSDoc from position."""
        if start >= len(lines):
            return None

        line = lines[start].strip()

        # Python docstrings — first line only
        if ext == '.py' and (line.startswith('"""') or line.startswith("'''")):
            quote = line[:3]
            if line.endswith(quote) and len(line) > 6:
                return line[3:-3].strip()
            # Multi-line: take just the first line
            first = line[3:].strip()
            if first:
                return first
            # First content line
            if start + 1 < len(lines):
                return lines[start + 1].strip()
            return None

        # JSDoc — first meaningful line only
        if line.startswith('/**'):
            for j in range(start, min(start + 10, len(lines))):
                cleaned = lines[j].strip().lstrip('/*').rstrip('*/').strip()
                if cleaned:
                    return cleaned
                if '*/' in lines[j]:
                    break
            return None

        return None

    def _scan_exception_handlers(self, content: str) -> str:
        """Scan for exception-handling hotspots and return an annotated section.

        Returns a formatted block listing every bare/broad except clause with:
          - line number
          - the except line itself
          - the immediately enclosing function name (if detectable)

        Returns an empty string if no exception handlers are found.
        """
        lines = content.splitlines()
        # Patterns ranked from most to least problematic
        _EXCEPT_PATTERNS = [
            (re.compile(r"^\s*except\s*:"),          "bare-except"),
            (re.compile(r"^\s*except\s+Exception\s*:"), "broad-except"),
            (re.compile(r"^\s*except\s+Exception\s+as\s+\w+\s*:"), "broad-except"),
            (re.compile(r"^\s*except\s+\("),          "multi-except"),
        ]
        _FUNC_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")

        hits: list[str] = []
        # Track the most recently seen function name for context
        current_func = "<module>"
        for idx, line in enumerate(lines, start=1):
            m = _FUNC_DEF.match(line)
            if m:
                current_func = m.group(1)
            for pattern, label in _EXCEPT_PATTERNS:
                if pattern.match(line):
                    # Show up to 2 continuation lines (body of the except block)
                    body_lines = []
                    for j in range(idx, min(idx + 2, len(lines))):
                        body = lines[j].strip()
                        if body and not body.startswith("except") and not body.startswith("try"):
                            body_lines.append(body)
                    body_preview = " | ".join(body_lines[:2]) if body_lines else ""
                    suffix = f"  → {body_preview}" if body_preview else ""
                    hits.append(f"  L{idx} [{label}] in `{current_func}`: {line.strip()}{suffix}")
                    break  # one label per line

        if not hits:
            return ""
        header = f"\n# Exception-handling hotspots ({len(hits)} found):"
        return header + "\n" + "\n".join(hits)

    def _generic_compress(self, content: str, ext: str) -> str:
        """Fallback compression for unknown languages."""
        lines = content.split('\n')
        # Keep non-empty lines that look structural
        kept = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip pure comment lines
            if any(stripped.startswith(c) for c in ('#', '//', '/*', '*', '--')):
                continue
            # Keep lines that look like declarations/definitions
            if any(kw in stripped.lower() for kw in ('function', 'class', 'def ', 'module', 'export', 'import', 'require', 'const ', 'let ', 'var ', 'type ', 'interface ')):
                kept.append(stripped)
            elif re.match(r'^[A-Za-z_]\w*\s*[=(<{]', stripped):
                kept.append(stripped)
        return '\n'.join(kept)

    def _diff_compress(self, filepath: Path, current_content: str) -> dict:
        """Generate diff-based compression against cached version."""
        cache_file = self.cache_dir / f"{filepath.name}.cache"
        current_hash = hashlib.md5(current_content.encode()).hexdigest()

        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8", errors="replace")
            cached_hash = hashlib.md5(cached.encode()).hexdigest()

            if cached_hash == current_hash:
                result = f"# {filepath.name} — NO CHANGES"
                return {"compressed": result, "mode": "diff-unchanged", **measure_savings(current_content, result)}

            # Generate contextual diff
            diff = self._contextual_diff(cached.split('\n'), current_content.split('\n'), filepath.name)
            savings = measure_savings(current_content, diff)
            savings["compressed"] = diff
            savings["mode"] = "diff"
        else:
            # No cache — fall back to structure mode
            compressed = self._extract_structure(current_content, filepath.suffix.lower(), "structure")
            header = f"# {filepath.name} (FIRST SEEN) — {len(current_content.splitlines())} lines\n"
            result = header + compressed
            savings = measure_savings(current_content, result)
            savings["compressed"] = result
            savings["mode"] = "diff-first"

        # Update cache
        cache_file.write_text(current_content, encoding="utf-8")
        return savings

    def _contextual_diff(self, old_lines: list, new_lines: list, filename: str) -> str:
        """Generate a contextual diff with surrounding structure."""
        import difflib
        differ = difflib.unified_diff(old_lines, new_lines, lineterm='', n=1)
        diff_text = '\n'.join(differ)

        if not diff_text.strip():
            return f"# {filename} — NO CHANGES"

        header = f"# {filename} — CHANGES ONLY\n"
        return header + diff_text

    def compress_directory(self, dirpath: str, mode: str = "smart",
                          extensions: Optional[list] = None,
                          max_files: int = 50) -> dict:
        """Compress an entire directory of source files."""
        dirpath = Path(dirpath).resolve()
        if not dirpath.is_dir():
            return {"error": f"Not a directory: {dirpath}"}

        default_exts = {'.py', '.js', '.ts', '.tsx', '.jsx', '.r', '.R',
                       '.css', '.html', '.json', '.yaml', '.yml', '.md'}
        allowed = set(extensions) if extensions else default_exts

        results = []
        total_original = 0
        total_compressed = 0
        skipped_protected = []

        # Pruned walk (shared SKIP_DIRS) - never descends into
        # node_modules/.git/etc., stops as soon as the cap is reached.
        from services.scanner import iter_files

        count = 0
        for fpath in iter_files(dirpath, exts={e.lower() for e in allowed}):
            if count >= max_files:
                break
            if self.is_protected_file(fpath):
                skipped_protected.append(self._relative_to_project(fpath))
                continue

            result = self.compress_file(str(fpath), mode)
            if "error" not in result:
                results.append(result)
                total_original += result.get("original_tokens", 0)
                total_compressed += result.get("compressed_tokens", 0)
                count += 1

        combined = '\n\n---\n\n'.join(r["compressed"] for r in results)
        savings_pct = ((total_original - total_compressed) / total_original * 100) if total_original > 0 else 0

        return {
            "files_processed": len(results),
            "total_original_tokens": total_original,
            "total_compressed_tokens": total_compressed,
            "savings_pct": round(savings_pct, 1),
            "combined_output": combined,
            "file_results": results,
            "protected_files": self.get_protected_files(),
            "skipped_protected_files": sorted(skipped_protected),
        }
