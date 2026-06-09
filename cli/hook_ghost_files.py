#!/usr/bin/env python3
"""PostToolUse hook for Bash: detect and warn about ghost files.

Ghost files are 0-byte (or near-0) files created in the project root when
shell metacharacters in Bash commands are misinterpreted — e.g., Python type
annotations like `-> dict` becoming `> dict` (output redirect), or pip
specifiers like `flask>=3.0.0` becoming `> =3.0.0`.

Runs after every Bash tool call. Scans the project root (non-recursively) for
files that match ghost-file heuristics and emits a warning + auto-deletes them.

Supports both Claude Code (PostToolUse) and Gemini CLI (AfterTool).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import emit_additional_context, log_hook_error  # noqa: E402

# ── Ghost-file detection heuristics ──────────────────────────────────────────

# Python builtin / typing names that should never be standalone files
_PYTHON_TYPE_NAMES = {
    "dict", "str", "int", "float", "bool", "list", "set", "tuple",
    "bytes", "bytearray", "complex", "frozenset", "memoryview",
    "type", "object", "range", "slice", "property", "classmethod",
    "staticmethod", "super", "None", "True", "False", "Ellipsis",
    # typing module
    "Any", "Union", "Optional", "List", "Dict", "Set", "Tuple",
    "Callable", "Iterator", "Generator", "Sequence", "Mapping",
    "Awaitable", "Coroutine", "AsyncIterator", "AsyncGenerator",
}

# Common heredoc / here-string end-markers that leak as filenames when
# Bash or PowerShell misparses a `<<EOF` / `@'...'@` block. Match is
# size-agnostic: a non-empty `EOF` file is still a ghost.
_HEREDOC_MARKERS = {
    "EOF", "EOM", "EOL", "END", "STOP", "DONE",
    "MARK", "MARKER", "DELIM", "DELIMITER",
    "INPUT", "OUTPUT", "DATA", "BLOCK", "HEREDOC",
    "'@", "@'",  # PowerShell here-string fragments
}

# Max file size to consider a ghost (bytes). Genuine files are usually larger.
_MAX_GHOST_SIZE = 4096

# Version-number pattern: 3.0.0, 1.2, 10.20.30 — usually from pip specifiers.
import re as _re

_VERSION_RE = _re.compile(r"^\d+(\.\d+)+[`'\"$|]*$")

# Extensions that are definitely NOT ghost files
_SAFE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".c", ".cpp", ".h", ".cs", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".sql", ".md", ".txt",
    ".sh", ".bat", ".ps1", ".r", ".xml", ".csv", ".ini", ".cfg",
    ".lock", ".log", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".gz",
    ".zip", ".tar", ".whl", ".egg", ".pyc", ".pyo", ".so",
    ".dll", ".exe", ".bin", ".dat", ".db", ".sqlite", ".gitignore",
    ".gitattributes", ".editorconfig", ".prettierrc", ".eslintrc",
    ".flake8", ".mypy", ".env", ".dockerignore", ".pdf",
}

# Known legitimate root-level files (no extension) in typical projects
_SAFE_NAMES = {
    "Makefile", "Dockerfile", "Procfile", "Vagrantfile", "Gemfile",
    "Rakefile", "Brewfile", "Pipfile", "LICENSE", "CHANGELOG",
    "CONTRIBUTING", "AUTHORS", "CODEOWNERS", "Makefile.am",
}


def _is_ghost_file(path: Path) -> bool:
    """Return True if a file in the project root looks like a ghost."""
    name = path.name

    # Skip dotfiles/directories
    if name.startswith("."):
        return False

    # Skip directories
    if path.is_dir():
        return False

    suffix = path.suffix.lower()

    # Skip files with safe extensions (real code/config files)
    if suffix and suffix in _SAFE_EXTENSIONS:
        return False

    # Skip known legitimate extensionless files
    if name in _SAFE_NAMES:
        return False

    # Skip files that are too large to be ghosts
    try:
        size = path.stat().st_size
    except OSError:
        return False

    if size > _MAX_GHOST_SIZE:
        return False

    # Treat a "suffix" as real only if it matches a letter-led pattern
    # (e.g. ".py", ".json"). Things like ".0" or ".0`" from a version
    # string like "3.0.0`" are NOT real extensions — they're shell-redirect
    # artifacts, and were escaping the 0-byte-extensionless filter below.
    real_suffix = suffix and suffix[1:2].isalpha()

    # ── Positive signals ─────────────────────────────────────

    # Bare Python type name (e.g., "dict", "str")
    if name in _PYTHON_TYPE_NAMES:
        return True

    # HEREDOC end-marker leaked as filename (e.g., "EOF", "'@", "END")
    if name in _HEREDOC_MARKERS and not real_suffix:
        return True

    # Partial type annotation (e.g., "tuple[float", "dict[str")
    if "[" in name and not real_suffix:
        return True

    # Partial function-call syntax (e.g., "parseApiResponse(await")
    # — fragments of JS/Python that Bash tokenized as a filename.
    if ("(" in name or ")" in name) and not real_suffix:
        return True

    # Shell redirect artifact: starts with = (e.g., "=3.0.0" from >=3.0.0)
    if name.startswith("="):
        return True

    # Starts with > or < (rare but possible)
    if name.startswith(">") or name.startswith("<"):
        return True

    # Trailing backtick or other metacharacter — command-substitution leakage
    if name.endswith("`") or name.endswith("$") or name.endswith("|"):
        return True

    # Version-like name (e.g., "3.0.0", "3.0.0`") without a real extension
    # — classic `pip install foo>=3.0.0` ghost.
    if size == 0 and not real_suffix and _VERSION_RE.match(name):
        return True

    # 0-byte file with no real extension and not in safe names
    if size == 0 and not real_suffix:
        return True

    return False


def scan_ghost_files(project_root: Path) -> list[dict]:
    """Scan project root for ghost files. Returns list of {path, name, size, reason}."""
    ghosts = []
    try:
        for entry in project_root.iterdir():
            if not entry.is_file():
                continue
            if not _is_ghost_file(entry):
                continue
            size = entry.stat().st_size
            name = entry.name

            # Determine reason
            if name in _HEREDOC_MARKERS:
                reason = "heredoc end-marker leak"
            elif name in _PYTHON_TYPE_NAMES:
                reason = "Python type name"
            elif "[" in name:
                reason = "partial type annotation"
            elif "(" in name or ")" in name:
                reason = "partial function-call syntax"
            elif name.startswith("="):
                reason = "pip version redirect (>=X.Y.Z)"
            elif name.startswith(">") or name.startswith("<"):
                reason = "shell redirect artifact"
            elif name.endswith("`") or name.endswith("$") or name.endswith("|"):
                reason = "shell metacharacter leak"
            elif _VERSION_RE.match(name):
                reason = "version-number leak (pip specifier)"
            elif size == 0:
                reason = "0-byte extensionless file"
            else:
                reason = "suspicious extensionless file"

            ghosts.append({
                "path": str(entry),
                "name": name,
                "size": size,
                "reason": reason,
            })
    except OSError:
        pass
    return ghosts


def cleanup_ghost_files(ghosts: list[dict]) -> list[str]:
    """Delete ghost files. Returns list of successfully deleted names."""
    deleted = []
    for g in ghosts:
        try:
            os.remove(g["path"])
            deleted.append(g["name"])
        except OSError:
            pass
    return deleted


# Tools whose output can carry shell-meta text that leaks into 0-byte files:
# native shells, c3_shell (its `N->Mtok` filter header), and file reads whose
# content has `-> Type` hints. A downstream shell sees `> word` and creates an
# empty file named `word`.
_GHOST_TRIGGER_TOOLS = (
    "Bash", "run_shell_command",
    "mcp__c3__c3_shell",
    "mcp__c3__c3_read", "Read", "read_file",
)


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        tool_name = data.get("tool_name", "")

        if tool_name not in _GHOST_TRIGGER_TOOLS:
            return

        is_gemini = isinstance(data.get("tool_response", ""), dict)
        project_root = Path.cwd()

        ghosts = scan_ghost_files(project_root)
        if not ghosts:
            return

        # Auto-delete ghost files
        deleted = cleanup_ghost_files(ghosts)

        if deleted:
            names = ", ".join(f'"{n}"' for n in deleted)
            msg = (
                f"[c3:ghost-cleanup] Deleted {len(deleted)} ghost file(s) from project root: {names}. "
                f"These were created by shell metacharacter misinterpretation in Bash commands "
                f"(e.g., `> dict` from `-> dict` in Python type annotations, "
                f"or `> =3.0.0` from `>=3.0.0` in pip specifiers). "
                f"Tip: quote Bash commands carefully to avoid shell redirects."
            )
            emit_additional_context(msg, is_gemini)

    except Exception as _e:
        log_hook_error("hook_ghost_files", _e)


if __name__ == "__main__":
    main()
