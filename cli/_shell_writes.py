"""Best-effort extraction of the files a shell command writes.

Field report 2026-08-22, ISSUE-1's buried finding: ``Bash`` sits in the
PreToolUse matcher, but tool discipline never looked at what a shell
command does to files. ``python -c "open(f,'w')"``, ``cat > f <<EOF`` and
``sed -i`` were never nudged toward c3_edit and never reached the ledger,
so every agent that met a blocked ``Write`` simply went round it. This
module is the one place that answers "which files does this command
probably write?" for both the PreToolUse nudge (hook_pretool_enforce) and
the after-the-fact ledger entry (hook_edit_ledger).

Deliberately conservative, like the access guard's shell scan: a missed
target costs one missing advisory line and one missing ledger row; a false
target costs a misleading hint, so tokens that are shell syntax, streams
(``/dev/null``, ``&1``) or pip-style ``>=`` version specs are skipped.
Paths resolve against the directory the command runs in, following ``cd``.
Never raises.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n]")
_CD_RE = re.compile(r"""^\s*cd\s+(?:/d\s+)?(?P<dir>"[^"]+"|'[^']+'|\S+)\s*$""", re.IGNORECASE)
# `>` / `>>` / `>|` preceded by whitespace or line start (so `foo>=3.0` is not a redirect).
_REDIRECT_RE = re.compile(r"""(?:^|\s)\d?>{1,2}\|?\s*(?P<t>"[^"]+"|'[^']+'|\S+)""")
_TEE_RE = re.compile(r"""(?:^|\|)\s*tee\s+(?:-[a-zA-Z]+\s+)*(?P<t>"[^"]+"|'[^']+'|\S+)""")
_SED_INPLACE_RE = re.compile(r"""(?:^|\s)sed\s+(?P<rest>.*)$""")
_PY_OPEN_RE = re.compile(r"""open\(\s*(?P<q>['"])(?P<t>[^'"]+)(?P=q)\s*,\s*['"][wax]""")
_PY_WRITE_TEXT_RE = re.compile(r"""Path\(\s*(?P<q>['"])(?P<t>[^'"]+)(?P=q)\s*\)\s*\.write_(?:text|bytes)\(""")
_FILE_CMDS = {"cp", "mv", "touch", "rm", "install"}
_STREAMS = {"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "&1", "&2", "-"}
_SYNTAX = set("$`(){}|<>*?")
MAX_TARGETS = 20


def _clean(tok: str) -> str:
    return tok.strip().strip("\"'`;,")


def _skip(tok: str) -> bool:
    if not tok or tok.startswith("-") or tok.startswith("=") or tok in _STREAMS:
        return True
    if tok.lower() in _STREAMS or tok.startswith("&"):
        return True
    return bool(_SYNTAX & set(tok))


def _segment_targets(segment: str) -> list[str]:
    out: list[str] = []
    for m in _REDIRECT_RE.finditer(segment):
        out.append(_clean(m.group("t")))
    for m in _TEE_RE.finditer(segment):
        out.append(_clean(m.group("t")))
    for m in _PY_OPEN_RE.finditer(segment):
        out.append(m.group("t"))
    for m in _PY_WRITE_TEXT_RE.finditer(segment):
        out.append(m.group("t"))
    words = [w for w in re.split(r"\s+", segment.strip()) if w]
    if words:
        head = words[0].rsplit("/", 1)[-1].lower()
        args = [_clean(w) for w in words[1:] if not w.startswith("-")]
        if head in ("cp", "mv", "install") and len(args) >= 2:
            out.append(args[-1])
        elif head in ("touch", "rm") and args:
            out.extend(args)
        elif head == "sed":
            flags = [w for w in words[1:] if w.startswith("-")]
            if any(f.startswith("-i") or f == "--in-place" for f in flags):
                # last non-flag, non-script argument(s): the files
                files = [a for a in args if not (a.startswith("s/") or a.startswith("s|") or "/" in a and a.count("/") >= 2 and a.startswith("s"))]
                out.extend(files[-2:] if files else [])
    return [t for t in out if not _skip(t)]


def _cd_target(segment: str, cwd: str) -> str | None:
    m = _CD_RE.match(segment)
    if not m:
        return None
    target = m.group("dir").strip("\"'")
    try:
        return target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd, target))
    except (OSError, ValueError):
        return None


def shell_write_targets(cmd: str, cwd: str) -> list[str]:
    """Absolute paths the command probably writes, in order, de-duplicated.

    ``cwd`` is the directory the command runs in (the project root for
    Claude Code hooks). Relative targets resolve against it, and a ``cd``
    segment moves it for the segments that follow.
    """
    found: list[str] = []
    seen: set[str] = set()
    here = cwd
    try:
        for segment in _SEGMENT_SPLIT.split(cmd or ""):
            for tok in _segment_targets(segment):
                if re.match(r"^/[a-z]/", tok):  # MSYS /c/foo -> C:/foo
                    tok = f"{tok[1]}:{tok[2:]}"
                try:
                    p = Path(tok) if os.path.isabs(tok) else Path(here) / tok
                    key = os.path.normcase(os.path.normpath(str(p)))
                except (OSError, ValueError):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                found.append(str(p))
                if len(found) >= MAX_TARGETS:
                    return found
            moved = _cd_target(segment, here)
            if moved is not None:
                here = moved
    except Exception:
        return found
    return found
