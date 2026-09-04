"""Advisory bypass hints for c3_shell (shell remediation S4).

Measured 2026-09-04 over 7,278 shell_exec events: 48% of c3_shell commands
were cat/head/tail/sed/grep/rg/find/ls over project files — reads and
searches that the code-intelligence tools answer within budget, indexed,
and with the index's exclusions (node_modules, minified bundles, .c3 logs),
none of which a raw shell read gets. Cod's ruling for this phase: advisory
only. Refusing on size would push agents to the native shell and lose
every control; the S1 budget already removes the catastrophic cost. So this
module produces at most ONE line per response naming the c3_* call that
answers the same question, and telemetry records that a hint was shown so
the follow rate and the false-positive rate can be measured before anything
is promoted.

Pure: ``bypass_hint(cmd, project_path) -> str | None``. Never rewrites the
command, never refuses, never inspects the filesystem beyond a cheap
"is this path inside the project" check.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

_PREFIX = re.compile(
    r"""^(?:\s*(?:cd\s+(?:"[^"]*"|'[^']*'|\S+)\s*(?:&&|;)\s*|\w+=(?:"[^"]*"|'[^']*'|\S*)\s*;?\s*))*""")
_OPERATORS = {"|", "||", "&&", ";", ">", ">>", "<", "2>", "2>&1", "&"}
_READERS = {"cat", "head", "tail", "less", "more", "type"}
_SEARCHERS = {"grep", "egrep", "fgrep", "rg"}
_FINDERS = {"find", "ls", "tree", "dir"}
_SED_RANGE = re.compile(r"^(\d+),(\d+)p$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SKIP_DIRS = ("node_modules", ".git", "dist", "build", ".c3", "__pycache__")
_MAX_LINES_HINT = 200


def _first_segment(cmd: str) -> list[str]:
    """Tokens of the first simple command after any cd/VAR= prefix."""
    rest = _PREFIX.sub("", (cmd or "").strip())
    if not rest:
        return []
    try:
        toks = shlex.split(rest, posix=True)
    except ValueError:
        toks = rest.split()
    out: list[str] = []
    for tok in toks:
        if tok in _OPERATORS:
            break
        out.append(tok)
    return out


def _head(tok: str) -> str:
    head = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return head[:-4] if head.endswith(".exe") else head


def _inside_project(path: str, project_path: str) -> bool:
    """True when ``path`` is relative or resolves under the project root."""
    if not path or path.startswith("-"):
        return False
    if any(seg in _SKIP_DIRS for seg in re.split(r"[\\/]", path)):
        return False
    if path.startswith(("~", "$")) or "${" in path:
        return False                                   # home or a variable: not a project path we can name
    p = Path(path)
    posix_absolute = path.startswith(("/", "\\"))   # Path("/var/log") is not absolute on Windows
    if not p.is_absolute() and not posix_absolute:
        return True
    if posix_absolute and not p.is_absolute():
        return False
    try:
        root = Path(project_path).resolve()
        return str(p.resolve()).lower().startswith(str(root).lower() + os.sep) or p.resolve() == root
    except Exception:
        return False


def _rel(path: str) -> str:
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") or "."


def _positional(toks: list[str], flags_with_arg: set[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for tok in toks[1:]:
        if skip:
            skip = False
            continue
        if tok in flags_with_arg:
            skip = True
            continue
        if tok.startswith("-") and len(tok) > 1:
            continue
        out.append(tok)
    return out


def bypass_hint(cmd: str, project_path: str) -> str | None:
    """One advisory line, or None when the command is not a plain read/search."""
    toks = _first_segment(cmd)
    if not toks:
        return None
    head = _head(toks[0])

    if head in _READERS:
        files = [t for t in _positional(toks, {"-n", "-c"}) if _inside_project(t, project_path)]
        if len(files) != 1:
            return None
        f = _rel(files[0])
        if head == "tail":
            return (f"[c3_shell:hint] a read of {f}: c3_compress(file_path='{f}', mode='map') "
                    f"then c3_read(file_path='{f}', lines=[a,b]) returns the region within budget and indexed")
        lines = ""
        if head == "head":
            m = re.search(r"(?:^|\s)-n\s*(\d+)|(?:^|\s)-(\d+)\b", " ".join(toks[1:]))
            n = int(next(g for g in m.groups() if g)) if m else 10
            lines = f", lines=[1,{n}]"
        return (f"[c3_shell:hint] a read of {f}: c3_read(file_path='{f}'{lines}) returns it within "
                f"budget and indexed; c3_compress(mode='map') first for a large file")

    if head == "sed":
        # sed -n 'a,bp' file
        args = toks[1:]
        rng = None
        files: list[str] = []
        for tok in args:
            m = _SED_RANGE.match(tok.strip("'\""))
            if m:
                rng = (int(m.group(1)), int(m.group(2)))
            elif tok == "-n" or tok.startswith("-"):
                continue
            else:
                files.append(tok)
        files = [t for t in files if _inside_project(t, project_path)]
        if rng is None or len(files) != 1:
            return None
        f = _rel(files[0])
        return (f"[c3_shell:hint] a read of {f} lines {rng[0]}-{rng[1]}: "
                f"c3_read(file_path='{f}', lines=[{rng[0]},{rng[1]}]) returns it within budget and indexed")

    if head in _SEARCHERS:
        pos = _positional(toks, {"-e", "--regexp", "-f", "--file", "-A", "-B", "-C", "-m", "-d",
                                 "--include", "--exclude", "--exclude-dir", "-t", "--type", "-g", "--glob"})
        # -e pattern form
        pattern = None
        for i, tok in enumerate(toks[1:], 1):
            if tok in ("-e", "--regexp") and i + 1 < len(toks):
                pattern = toks[i + 1]
                break
        if pattern is None:
            if not pos:
                return None
            pattern, pos = pos[0], pos[1:]
        targets = [t for t in pos if _inside_project(t, project_path)]
        if pos and not targets:
            return None  # searching outside the project: not ours to hint
        flags = " ".join(toks[1:])
        ignore = ", ignore_case=True" if re.search(r"(?:^|\s)-\w*i\w*(?:\s|$)", flags) else ""
        path_arg = f", path='{_rel(targets[0])}'" if targets and _rel(targets[0]) not in (".", "") else ""
        action = "code" if _IDENTIFIER.match(pattern) else "exact"
        safe = pattern.replace("'", "\\'")
        return (f"[c3_shell:hint] a search: c3_search(action='{action}', query='{safe}'{path_arg}{ignore}) "
                f"runs over the index (no node_modules, minified bundles or .c3 logs) within budget")

    if head in _FINDERS:
        pos = _positional(toks, {"-name", "-iname", "-path", "-type", "-maxdepth", "-mindepth", "-newer", "-size"})
        name = None
        for i, tok in enumerate(toks[1:], 1):
            if tok in ("-name", "-iname") and i + 1 < len(toks):
                name = toks[i + 1]
                break
        if head == "find" and name is None:
            return None
        targets = [t for t in pos if _inside_project(t, project_path)]
        if head in ("ls", "tree", "dir"):
            if "-R" not in toks and "-r" not in toks and head != "tree":
                return None  # a plain ls is cheap and not a search
            q = _rel(targets[0]) if targets else "*"
            return (f"[c3_shell:hint] a listing: c3_search(action='files', query='{q}') or "
                    f"c3_compress(file_path='<dir>', mode='map') walks the index instead")
        path_arg = f", path='{_rel(targets[0])}'" if targets and _rel(targets[0]) != "." else ""
        return (f"[c3_shell:hint] a filename search: c3_search(action='files', query='{name}'{path_arg}) "
                f"walks the index (no node_modules or build output) within budget")

    return None
