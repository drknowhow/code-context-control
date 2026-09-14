"""Write mode for the Claude delegate: a worker that edits inside a write set.

The caller — the stronger model — decides the change and names the files the
delegate may touch (``write_paths``). A headless ``claude -p`` worker makes
the change with Read/Grep/Glob/Edit/Write and nothing else. Four fences, each
enough on its own for the case it covers:

1. Claude Code permission rules: ``Edit()`` allow rules are the write set, and
   Access Guard's deny / read_only / confirm / mask globs are Read and Edit
   denies. Measured on 2.1.270: under ``--permission-mode dontAsk`` a write
   outside the allow rules is refused, and ``permission_denials`` names it.
2. ``--restricted``: no command-running tools, file tools confined to the
   project, and writes to settings, git and tool-configuration files refused.
3. The PreToolUse hook (``hook_access_guard.py --worker-state``): Access
   Guard's verdict on the canonical path with no grants and no filed requests
   (a worker cannot wait for a human), the write set again, ``.git/`` and
   ``.c3/`` never, the credential vault never, and another agent's file lock.
   It fails closed.
4. The same hook saves each file's pre-image before its first write, so C3
   can show the caller exactly what changed, log it to the edit ledger, and
   report a change even when the worker was killed at its deadline.

This module is the part both sides share: the hook imports it, so it stays
light (stdlib and ``services.access_guard`` only).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

WORKER_TOOLS = "Read,Grep,Glob,Edit,Write"
MAX_WRITE_PATHS = 50
# Never delegate-writable, whatever the write set says: git internals and
# C3's own state (config, ledger, vault, telemetry).
FORBIDDEN_PREFIXES = (".git/", ".c3/")
_SPEC = "spec.json"
_PRE = "pre"
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def parse_write_paths(raw) -> tuple[list[str], str]:
    """``(globs, error)`` from the caller's comma- or newline-separated list.

    Globs are project-relative POSIX (``**`` crosses directories, ``*`` does
    not). A bare name means that file at the project root, not anywhere: the
    write set is the caller's exact scope, so it never widens by basename.
    """
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\n]", str(raw or ""))
    globs: list[str] = []
    for item in items:
        g = str(item or "").strip().replace("\\", "/")
        while g.startswith("./"):
            g = g[2:]
        if not g:
            continue
        if g.startswith("/") or _DRIVE_RE.match(g) or g.startswith("~"):
            return [], f"write_paths entry {item!r} is absolute; give project-relative paths"
        if ".." in g.split("/"):
            return [], f"write_paths entry {item!r} leaves the project"
        low = g.casefold()
        if any(low == p.rstrip("/") or low.startswith(p) for p in FORBIDDEN_PREFIXES):
            return [], f"write_paths entry {item!r} is under .git/ or .c3/, which a delegate never writes"
        if g not in globs:
            globs.append(g)
    if not globs:
        return [], "write_paths is empty"
    if len(globs) > MAX_WRITE_PATHS:
        return [], f"write_paths has {len(globs)} entries (max {MAX_WRITE_PATHS}); use a directory glob"
    return globs, ""


def _glob_re(glob: str) -> re.Pattern:
    from services.access_guard import _glob_to_re
    return _glob_to_re(glob.casefold())


def in_write_set(rel: str, globs) -> bool:
    """Whether a casefolded project-relative path is inside the write set."""
    rel = str(rel or "").replace("\\", "/").casefold()
    if not rel:
        return False
    return any(_glob_re(g).match(rel) for g in globs)


def forbidden(rel: str) -> bool:
    rel = str(rel or "").replace("\\", "/").casefold()
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in FORBIDDEN_PREFIXES)


def new_state_dir(project_path, globs, session_id: str = "") -> Path:
    """A private temp dir for one run: the spec the hook reads, and pre-images."""
    state = Path(tempfile.mkdtemp(prefix="c3-dwrite-"))
    (state / _PRE).mkdir()
    spec = {"project": str(project_path), "write_paths": list(globs), "session_id": session_id}
    (state / _SPEC).write_text(json.dumps(spec), encoding="utf-8")
    return state


def load_spec(state_dir) -> dict:
    spec = json.loads((Path(state_dir) / _SPEC).read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or not isinstance(spec.get("write_paths"), list):
        raise ValueError("worker spec is malformed")
    return spec


def snapshot(state_dir, path: str, canon: str) -> None:
    """Save ``path``'s content before its first write in this run.

    Keyed by the canonical path, so two spellings of one file share a
    snapshot. The marker is created exclusively: a later write to the same
    file keeps the first pre-image. Raises on failure — the hook denies then.
    """
    key = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:24]
    pre = Path(state_dir) / _PRE
    marker = pre / f"{key}.json"
    try:
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return
    existed = os.path.isfile(path)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"path": str(path), "existed": existed}, fh)
    if existed:
        with open(path, "rb") as src, open(pre / f"{key}.bin", "wb") as dst:
            dst.write(src.read())


def _display_rel(path: str, project_path) -> str:
    try:
        rel = os.path.relpath(path, str(project_path))
    except ValueError:
        return str(path)
    return rel.replace("\\", "/")


def _text(data: bytes | None) -> str | None:
    if data is None:
        return ""
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def collect_changes(state_dir, project_path) -> list[dict]:
    """Every file the worker actually changed, with its diff.

    ``{rel, path, change, added, removed, diff, binary}``; ``change`` is
    ``created`` / ``modified`` / ``deleted``. A snapshot whose file is
    byte-identical afterwards (a refused or no-op write) is not a change.
    """
    pre = Path(state_dir) / _PRE
    changes: list[dict] = []
    for marker in sorted(pre.glob("*.json")):
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        path = str(info.get("path") or "")
        if not path:
            continue
        old = None
        if info.get("existed"):
            try:
                old = (pre / f"{marker.stem}.bin").read_bytes()
            except OSError:
                continue
        try:
            new = Path(path).read_bytes() if os.path.isfile(path) else None
        except OSError:
            new = None
        if old == new:
            continue
        rel = _display_rel(path, project_path)
        change = "created" if old is None else ("deleted" if new is None else "modified")
        old_text, new_text = _text(old), _text(new)
        row = {"rel": rel, "path": path, "change": change, "added": 0, "removed": 0,
               "diff": "", "binary": old_text is None or new_text is None}
        if not row["binary"]:
            # Compared by line content, so a CRLF file diffs as cleanly as an
            # LF one; a change that is only line endings or a final newline
            # is named instead of showing an empty diff.
            lines = list(difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile="/dev/null" if old is None else f"a/{rel}",
                tofile="/dev/null" if new is None else f"b/{rel}", lineterm=""))
            row["added"] = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
            row["removed"] = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
            if lines:
                row["diff"] = "\n".join(lines) + "\n"
            else:
                row["note"] = "line endings or final newline only"
        changes.append(row)
    return changes


def denial_lines(denials, project_path) -> list[str]:
    """One line per refused tool call from ``permission_denials``."""
    out: list[str] = []
    for d in denials or []:
        if not isinstance(d, dict):
            continue
        ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        target = ti.get("file_path") or ti.get("path") or ti.get("pattern") or ""
        shown = _display_rel(str(target), project_path) if target else ""
        line = f"{d.get('tool_name') or '?'} {shown}".strip()
        if line not in out:
            out.append(line)
    return out


def render(changes: list[dict], *, header: str, report: str, refused: list[str],
           max_diff_chars: int) -> str:
    """The caller's answer: what changed, what was refused, the worker's own
    report, then the diff (capped; the ledger keeps every change)."""
    marks = {"created": "A", "modified": "M", "deleted": "D"}
    lines = [header]
    for c in changes:
        stat = "binary" if c["binary"] else (c.get("note") or f"+{c['added']} -{c['removed']}")
        lines.append(f"  {marks.get(c['change'], '?')} {c['rel']} ({stat})")
    if refused:
        lines.append(f"Refused ({len(refused)}): " + "; ".join(refused[:10])
                     + (" …" if len(refused) > 10 else ""))
    lines += ["", "--- worker report ---", (report or "(none)").strip()]
    diff = "".join(c["diff"] for c in changes if c["diff"])
    if diff:
        lines += ["", "--- diff ---"]
        if len(diff) > max_diff_chars:
            cut = diff[:max_diff_chars]
            lines.append(cut[:cut.rfind("\n") + 1] if "\n" in cut else cut)
            lines.append(f"[diff truncated at {max_diff_chars} chars of {len(diff)}; "
                         "read the changed files or c3_edits(action='list') for the rest]")
        else:
            lines.append(diff.rstrip("\n"))
    return "\n".join(lines)
