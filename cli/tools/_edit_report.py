"""What c3_edit says about an edit besides the ✓ line: where it landed, what changed.

Two concerns, both read-only:
- ``locate``: a RELATIVE file_path resolves against the project root, which is
  the main checkout. When the repo has other git worktrees, the caller may have
  meant one of them, so the response names the resolved path and any worktree
  holding the same relative path, and refuses outright when the path exists only
  in a worktree.
- ``diff_block``: a compact unified diff of the whole file, before → after.
"""
import difflib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

_WORKTREE_TTL_S = 60.0
_GIT_TIMEOUT_S = 3.0
_worktree_cache: dict[str, tuple[float, list[Path]]] = {}
_cache_lock = threading.Lock()

DIFF_MAX_LINES = 24
DIFF_MAX_CHARS = 1500
_LINE_MAX_CHARS = 200


def _git_worktree_roots(cwd: Path) -> list[Path]:
    """Every non-bare worktree root of the repo containing ``cwd``, main first.

    Returns [] when git is missing, times out, or ``cwd`` is not in a repo.
    """
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            ["git", "worktree", "list", "--porcelain"], cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", **kwargs)
    except OSError:
        return []
    try:
        out, _ = proc.communicate(timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, stdin=subprocess.DEVNULL, **kwargs)
        else:
            proc.kill()
        proc.communicate()
        return []
    if proc.returncode != 0:
        return []
    roots, current, bare = [], None, False
    for line in (out or "").splitlines() + [""]:
        if line.startswith("worktree "):
            current, bare = line[len("worktree "):].strip(), False
        elif line == "bare":
            bare = True
        elif not line and current:
            if not bare:
                roots.append(Path(current).resolve())
            current = None
    return roots


def worktree_roots(project_path: str) -> list[Path]:
    """Cached ``_git_worktree_roots`` for a project, refreshed every 60s."""
    key = str(Path(project_path).resolve())
    now = time.monotonic()
    with _cache_lock:
        hit = _worktree_cache.get(key)
        if hit and now - hit[0] < _WORKTREE_TTL_S:
            return hit[1]
    roots = _git_worktree_roots(Path(key))
    with _cache_lock:
        _worktree_cache[key] = (now, roots)
    return roots


def _other_trees(project_root: Path, roots: list[Path]) -> list[tuple[Path, Path]]:
    """(worktree root, project dir inside it) for every worktree but our own.

    A project that is a subdirectory of its repo maps to the same subdirectory
    in each worktree.
    """
    own = None
    for root in roots:
        if (project_root == root or root in project_root.parents) and (
                own is None or len(root.parts) > len(own.parts)):
            own = root
    if own is None:
        return []
    prefix = project_root.relative_to(own)
    return [(root, root / prefix) for root in roots if root != own]


def locate(file_path: str, path: Path, project_path: str) -> tuple[str, str]:
    """Check a resolved edit target against the repo's other worktrees.

    Returns ``(refusal, note)``, at most one non-empty. ``refusal`` is set when a
    relative ``file_path`` does not exist in this checkout but does in another
    worktree. ``note`` is extra response text for a relative path in a repo
    with other worktrees. Absolute paths always return ``("", "")``.
    """
    if Path(file_path).is_absolute():
        return "", ""
    project_root = Path(project_path).resolve()
    others = _other_trees(project_root, worktree_roots(project_path))
    if not others:
        return "", ""
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return "", ""
    hits = [proj / rel for _, proj in others if (proj / rel).exists()]
    if not path.exists() and hits:
        listed = "\n".join(f"    {h}" for h in hits)
        return (f"[c3_edit:wrong-tree] {file_path} does not exist in this checkout "
                f"({project_root}) but does in another git worktree:\n{listed}\n"
                f"  Nothing was written. Pass the absolute path of the copy you mean; "
                f"to create a new file here, pass its absolute path in this checkout."), ""
    if not path.exists():
        return "", (f"\n  ⚠ created in the MAIN checkout: {path} — this repo has "
                    f"{len(others)} other worktree(s). If you meant one of them, "
                    f"delete this file and pass an absolute path.")
    note = f"\n  → {path}"
    if hits:
        note += (f"\n  ⚠ relative path edited this checkout; the same path also exists "
                 f"in: {', '.join(str(h) for h in hits)}. Pass an absolute path to "
                 f"edit a worktree.")
    return "", note


def show_diff(project_path: str) -> bool:
    """``.c3/config.json`` key ``edit.show_diff``; true unless set to false."""
    cfg = Path(project_path) / ".c3" / "config.json"
    if not cfg.is_file():
        return True
    try:
        section = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("edit")
    except (OSError, ValueError, AttributeError):
        return True
    if isinstance(section, dict) and isinstance(section.get("show_diff"), bool):
        return section["show_diff"]
    return True


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _clip(line: str) -> str:
    return line if len(line) <= _LINE_MAX_CHARS else line[:_LINE_MAX_CHARS] + "…"


def diff_block(before: str, after: str, project_path: str) -> str:
    """Response text for a whole-file before → after diff, or "" when disabled/unchanged.

    Unified diff, 2 context lines, ``@@`` hunk headers with real line numbers,
    no file headers, EOL-stripped lines, capped at DIFF_MAX_LINES lines and
    DIFF_MAX_CHARS characters with a trailing count of what was cut.
    """
    if not show_diff(project_path):
        return ""
    body = list(difflib.unified_diff(_lines(before), _lines(after), n=2,
                                     lineterm=""))[2:]
    if not body:
        return ""
    shown, used = [], 0
    for line in body:
        line = _clip(line)
        if len(shown) >= DIFF_MAX_LINES or used + len(line) + 3 > DIFF_MAX_CHARS:
            break
        shown.append("  " + line)
        used += len(line) + 3
    out = "\n" + "\n".join(shown)
    if len(shown) < len(body):
        out += f"\n  … {len(body) - len(shown)} more diff lines"
    return out
