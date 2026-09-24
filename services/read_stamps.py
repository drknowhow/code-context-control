"""What this agent session has been shown of each file, and whether an edit
still targets text it saw.

One c3-mcp process serves one agent session, and only that process's c3_read
and c3_edit (plus c3_project's proxies, which run in the caller's process)
touch this store, so module state is per-session state.

`record(path)` stamps a file's current bytes. `check(...)` compares a file's
current bytes to its stamp; when an edit's target region lies in or within
NEAR_LINES of a region that changed since the stamp, it returns a refusal
carrying the current text of that region. `edit.stale_guard` in
``.c3/config.json`` selects "refuse" (default), "warn" or "off".
"""
import difflib
import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

MODES = ("refuse", "warn", "off")
DEFAULT_MODE = "refuse"
MAX_FILES = 64
MAX_TOTAL_CHARS = 16 * 1024 * 1024
MAX_FILE_CHARS = 4 * 1024 * 1024
NEAR_LINES = 3
_CONTEXT = 2
_REGION_CAP = 4000
_SURROGATE_TRANS = {c: "�" for c in range(0xDC80, 0xDD00)}


@dataclass
class Stamp:
    sha256: str
    mtime_ns: int
    size: int
    text: str | None


_stamps: "OrderedDict[str, Stamp]" = OrderedDict()
_total_chars = 0
_lock = threading.Lock()


def _key(path) -> str:
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        pass
    return os.path.normcase(str(p))


def _eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode(raw: bytes) -> str:
    return _eol(raw.decode("utf-8", errors="surrogateescape"))


def _drop(key: str) -> None:
    global _total_chars
    old = _stamps.pop(key, None)
    if old is not None and old.text is not None:
        _total_chars -= len(old.text)


def _store(key: str, stamp: Stamp) -> None:
    global _total_chars
    with _lock:
        _drop(key)
        _stamps[key] = stamp
        if stamp.text is not None:
            _total_chars += len(stamp.text)
        while len(_stamps) > MAX_FILES or _total_chars > MAX_TOTAL_CHARS:
            oldest = next(iter(_stamps))
            if oldest == key:
                break
            _drop(oldest)


def _snapshot(path: Path):
    """(Stamp, text) for the file's current bytes; text is always decoded."""
    st = path.stat()
    raw = path.read_bytes()
    text = _decode(raw)
    kept = text if len(text) <= MAX_FILE_CHARS else None
    return Stamp(hashlib.sha256(raw).hexdigest(), st.st_mtime_ns, len(raw), kept), text


def record(path) -> None:
    """Stamp `path` as seen by this session. A file that cannot be read has
    its stamp dropped, so no later edit is checked against stale knowledge."""
    key = _key(path)
    try:
        stamp, _ = _snapshot(Path(path))
    except OSError:
        with _lock:
            _drop(key)
        return
    _store(key, stamp)


def get(path) -> Stamp | None:
    key = _key(path)
    with _lock:
        stamp = _stamps.get(key)
        if stamp is not None:
            _stamps.move_to_end(key)
        return stamp


def mode(project_path) -> str:
    """``.c3/config.json`` → ``edit.stale_guard``. Unknown/missing → default."""
    cfg = Path(project_path) / ".c3" / "config.json"
    if not cfg.is_file():
        return DEFAULT_MODE
    try:
        section = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("edit")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_MODE
    value = section.get("stale_guard") if isinstance(section, dict) else None
    return value if value in MODES else DEFAULT_MODE


def changed_ranges(before: str, after: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of `after` that differ from `before`. A
    pure deletion is reported as the two lines either side of the gap."""
    a, b = before.split("\n"), after.split("\n")
    out = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if j2 > j1:
            out.append((j1 + 1, j2))
        else:
            out.append((max(1, j1), min(len(b), j1 + 1)))
    return out


def _targets(content: str, olds, norm) -> list[tuple[int, int]]:
    """Line ranges the edit's old_strings match in `content`."""
    norm_content = None
    out = []
    for old, every in olds:
        if not old:
            continue
        hay, needle = content, old
        if hay.find(needle) < 0 and norm is not None:
            if norm_content is None:
                norm_content = norm(content)
            hay, needle = norm_content, norm(old)
        pos = hay.find(needle)
        while pos >= 0:
            start = hay.count("\n", 0, pos) + 1
            out.append((start, start + needle.count("\n", 0, len(needle) - 1)))
            if not every:
                break
            pos = hay.find(needle, pos + len(needle))
    return out


def _olds(old_string: str, edits, replace_all: bool) -> list[tuple[str, bool]]:
    if not edits:
        return [(_eol(old_string or ""), bool(replace_all))]
    try:
        patches = json.loads(edits) if isinstance(edits, str) else edits
    except ValueError:
        return []
    if not isinstance(patches, list):
        return []
    return [(_eol(str(p.get("old_string") or "")), bool(p.get("replace_all")))
            for p in patches if isinstance(p, dict)]


def _near(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return b[0] <= a[1] + NEAR_LINES and b[1] >= a[0] - NEAR_LINES


def _fmt(ranges) -> str:
    return ", ".join(f"L{a}" if a == b else f"L{a}-L{b}" for a, b in ranges)


class Guard:
    """The outcome of `check` for one c3_edit call.

    `refusal` is non-empty when the edit must not run. Otherwise the caller
    runs the edit, calls `written()` after each successful write, and routes
    its result through `wrap(finalize)` so the change note (if any) reaches
    the agent only when something was actually written.
    """

    def __init__(self, path, refusal: str = "", note: str = ""):
        self.path = path
        self.refusal = refusal
        self.note = note
        self.applied = False

    def written(self) -> None:
        record(self.path)
        self.applied = True

    def wrap(self, finalize):
        if not self.note:
            return finalize

        def _finalize(tool, args, text, *rest, **kw):
            if self.applied:
                text = text + self.note
            return finalize(tool, args, text, *rest, **kw)
        return _finalize


def _region_block(text: str, lo: int, hi: int, label: str) -> str:
    lines = text.split("\n")
    lo, hi = max(1, lo), min(len(lines), hi)
    region = "\n".join(lines[lo - 1:hi]).translate(_SURROGATE_TRANS)
    if len(region) > _REGION_CAP:
        region = (region[:_REGION_CAP]
                  + f"\n⟦trimmed — run c3_read(file_path='{label}', "
                  f"lines=[{lo},{hi}]) for the rest⟧")
    return (f"  Current file text between the markers:\n"
            f"⟦L{lo}-L{hi}⟧\n{region}\n⟦end⟧\n")


def check(path, project_path, label: str, old_string: str, edits,
          replace_all: bool, norm=None) -> Guard:
    """Compare `path` to this session's stamp before an edit is applied.

    No stamp, mode "off", unchanged bytes, or an unreadable file → a Guard
    with nothing to say. `norm` is the matcher's lookalike fold, so regions
    are located the same way the edit will locate them.
    """
    guard = Guard(path)
    setting = mode(project_path)
    if setting == "off":
        return guard
    stamp = get(path)
    if stamp is None:
        return guard
    p = Path(path)
    try:
        st = p.stat()
        if st.st_mtime_ns == stamp.mtime_ns and st.st_size == stamp.size:
            return guard
        current, text = _snapshot(p)
    except OSError:
        return guard
    if current.sha256 == stamp.sha256:
        return guard

    if stamp.text is None:
        guard.note = ("\n[c3_edit:stale] file changed outside this session "
                      "since your read (too large to localize); edit applied "
                      "— check the result.")
        return guard

    changes = changed_ranges(stamp.text, text)
    targets = _targets(text, _olds(old_string, edits, replace_all), norm)
    hits = [c for c in changes if any(_near(t, c) for t in targets)]
    if not hits:
        guard.note = (f"\nfile changed outside this session since your read "
                      f"({_fmt(changes)}); edit applied — region untouched.")
        return guard
    if setting == "warn":
        guard.note = (f"\n[c3_edit:stale] file changed outside this session "
                      f"since your read ({_fmt(changes)}), in or next to the "
                      f"region you edited; edit applied — check the result.")
        return guard

    near_targets = [t for t in targets if any(_near(t, c) for c in hits)]
    lo = min(r[0] for r in hits + near_targets) - _CONTEXT
    hi = max(r[1] for r in hits + near_targets) + _CONTEXT
    _store(_key(path), current)
    guard.refusal = (
        f"[c3_edit:stale] {label} changed outside this session since your "
        f"last read: {_fmt(changes)}.\n"
        f"  Your edit targets {_fmt(near_targets)}, in or next to that change. "
        f"Nothing was written.\n"
        + _region_block(text, lo, hi, label)
        + "  You have now seen this text: retry with old_string copied from it "
          "(markers excluded) — no need to re-read the file.")
    return guard
