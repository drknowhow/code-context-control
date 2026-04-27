"""c3_impact — Blast-radius analysis for a symbol or file before edits.

Inspired by flytohub/flyto-indexer. Pure Python + git grep, zero deps.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

_SKIP_DIRS = frozenset({
    ".git", ".c3", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", "dist", "build", ".eggs",
})
_CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".lua", ".swift",
    ".kt", ".scala", ".r", ".sh", ".bash",
})


# ── Grep helpers ─────────────────────────────────────────────────────────────

def _grep_git(target: str, project: Path, timeout: int = 15) -> list:
    """Try git grep -n -w. Returns list of (rel_path, lineno) or None on failure."""
    try:
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["git", "grep", "-n", "-w", "--", target],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=str(project), **kw,
        )
        if result.returncode not in (0, 1):  # 0=found, 1=no matches, else error
            return None
        refs = []
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) >= 2:
                try:
                    refs.append((parts[0].replace("\\", "/"), int(parts[1])))
                except ValueError:
                    pass
        return refs
    except Exception:
        return None


def _grep_python(target: str, project: Path) -> list:
    """Pure-Python fallback grep. Returns list of (rel_path, lineno)."""
    pattern = re.compile(r"\b" + re.escape(target) + r"\b")
    refs = []
    for root, dirs, files in os.walk(str(project)):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in _CODE_EXTS:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        try:
                            rel = str(fpath.relative_to(project)).replace("\\", "/")
                        except ValueError:
                            rel = str(fpath).replace("\\", "/")
                        refs.append((rel, i))
            except Exception:
                continue
    return refs


def _get_unstaged_files(project: Path) -> set:
    """Return set of rel-paths with uncommitted changes."""
    try:
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL, cwd=str(project), **kw,
        )
        if result.returncode == 0:
            return {l.strip().replace("\\", "/") for l in result.stdout.splitlines() if l.strip()}
    except Exception:
        pass
    return set()


# ── Risk scoring ─────────────────────────────────────────────────────────────

def _risk(n_files: int) -> str:
    if n_files == 0:
        return "SAFE"
    if n_files <= 2:
        return "LOW"
    if n_files <= 6:
        return "MEDIUM"
    return "HIGH"


# ── Main handler ─────────────────────────────────────────────────────────────

def handle_impact(target: str, file_path: str, mode: str, svc, finalize) -> str:
    if not target:
        return "[impact:error] target required — provide a symbol name, function, or class"

    project = Path(svc.project_path)

    # Gather references
    refs = _grep_git(target, project)
    if refs is None:
        refs = _grep_python(target, project)

    # Group by file, tracking line numbers
    by_file: dict = {}
    for rel, lineno in refs:
        by_file.setdefault(rel, []).append(lineno)

    # Exclude the source file itself
    if file_path:
        norm = file_path.replace("\\", "/").lstrip("./")
        by_file = {k: v for k, v in by_file.items() if not k.endswith(norm)}

    # Unstaged overlay for mode='unstaged'
    unstaged: set = set()
    if mode == "unstaged":
        unstaged = _get_unstaged_files(project)

    n = len(by_file)
    risk = _risk(n)

    # ── Format output ──────────────────────────────────────────────────────
    lines = [f"[impact] '{target}' — {n} file(s) affected, risk: {risk}"]

    if not by_file:
        lines.append("  No references found outside source file.")
        lines.append("  Safe to rename/remove.")
    else:
        # Sort: unstaged first (most relevant), then by hit count desc
        def _sort_key(item):
            fp, lnos = item
            return (0 if fp in unstaged else 1, -len(lnos))

        for fp, lnos in sorted(by_file.items(), key=_sort_key)[:20]:
            marker = " [unstaged]" if fp in unstaged else ""
            sample = ",".join(str(l) for l in lnos[:4])
            more = f"+{len(lnos) - 4}" if len(lnos) > 4 else ""
            lines.append(f"  {fp}:{sample}{more}{marker}")

        if n > 20:
            lines.append(f"  ... and {n - 20} more files")

    if mode == "unstaged" and not unstaged:
        lines.append("  (no uncommitted changes detected)")

    lines.append(f"Risk: {risk}" + (" — review all call sites before editing" if risk in ("MEDIUM", "HIGH") else ""))

    return finalize("c3_impact", {"target": target, "mode": mode},
                    "\n".join(lines), f"{n}f/{risk.lower()}")
