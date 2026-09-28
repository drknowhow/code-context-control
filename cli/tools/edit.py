"""c3_edit — in-place file patch: read + replace + write + ledger log in one step.

Bypasses the native Edit tool's requirement for a prior native Read call,
so c3_read → c3_edit works without an intermediate redundant native read.

Parallel safety:
- Different files: safe to call concurrently (no shared state).
- Same file: serialized by _edit_lock — a threading.Lock (other threads in this
  process) plus a cross-process file lock (other c3-mcp processes). Every Claude
  Code session runs its own c3-mcp server, so the second layer is the one that
  actually stops two sessions clobbering each other. Covers create, single-edit
  and batch modes alike.
- Same file, multiple hunks: use the `edits` batch parameter — one read/write cycle.

See docs/agent-locks.md §5 (Layer A).
"""
import bisect
import codecs
import difflib
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cli.tools import _edit_report, _grants
from cli.tools._helpers import finalize_with_tokens
from services import access_guard, agent_locks, edit_blobs, read_stamps
from services import credential_store as _cs
from services.atomic_json import write_bytes_atomic
from services.task_store import _FileLock

_FAILURES = {
    "missing param": "invalid",
    "bad edits param": "invalid",
    "empty old_string": "invalid",
    "bad overwrite": "invalid",
    "vault-protected": "denied",
    "access-denied": "denied",
    "wrong tree": "wrong_tree",
    "lock held": "locked",
    "lock busy": "locked",
    "stale read": "stale",
    "not found": "not_found",
    "ambiguous": "ambiguous",
    "lookalike": "lookalike",
    "encoding": "encoding",
    "unread": "unread",
    "read error": "io_error",
    "write error": "io_error",
    "create error": "io_error",
}
_NOOP = "noop"
_BATCH_FAILURES = {"miss": "not_found", "ambiguous": "ambiguous",
                   "lookalike": "lookalike", "skipped": "invalid"}


class _Report:
    """`finalize` for one c3_edit call: records the outcome and wall time of
    whichever exit is taken, and remembers in `wrote` whether it wrote.

    The outcome is the `outcome` keyword when a call site passes one, else the
    code `_FAILURES` gives the summary slug, else "success". Other keywords
    land in the telemetry detail. `ok` reaches the activity log, which the
    enforcement hook reads to decide whether this call unlocks native Edit.
    """

    def __init__(self, finalize, svc):
        self._finalize = finalize
        self._svc = svc
        self._started = time.monotonic()
        self.wrote = False

    def __call__(self, tool, args, text, summary="", **detail):
        outcome = detail.pop("outcome", "") or _FAILURES.get(summary, "success")
        self.wrote = outcome == "success"
        return finalize_with_tokens(
            self._finalize, self._svc, tool, args, text, summary,
            duration_ms=round((time.monotonic() - self._started) * 1000, 1),
            detail={"outcome": outcome, **detail},
            ok=outcome in ("success", _NOOP))


def _session_id(svc) -> str:
    """This agent's lease identity — see `cli.tools._grants.session_id`.

    Was defined here and copied into locks.py and override.py, each with a
    comment saying it had to match. P2a makes that invariant load-bearing:
    a grant is minted under the id c3_override computes and consumed under
    the one c3_edit computes, so a divergence would make every approval
    silently fail to apply. One definition, three callers.
    """
    return _grants.session_id(svc)

# ── Same-file serialization ───────────────────────────────────────────────
# In-process locks, keyed by resolved absolute path string.
_file_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()

# Cross-process lock sidecars. Machine-global and keyed by a hash of the
# RESOLVED TARGET path — deliberately not under svc.project_path, because
# c3_project(action='edit') proxies into handle_edit with the *caller's* svc.
# A project-scoped sidecar would hand two agents editing the same file two
# different locks, i.e. no mutual exclusion at all.
_EDIT_LOCK_DIR = Path.home() / ".c3" / "edit_locks"


def _get_file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_lock:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


def _lock_sidecar(path: Path) -> Path:
    """Sidecar for `path`. Two spellings of one file MUST return one sidecar.

    resolve() here even though handle_edit already resolves: mutual exclusion
    silently evaporates if any caller hashes an unresolved path, and "silently"
    is the whole problem — you get a green badge and a lost edit. macOS
    /var → /private/var and Windows 8.3 names (RUNNER~1 → runneradmin) are the
    two spellings that actually bite. Non-strict, so a not-yet-created file
    still resolves.

    normcase, not casefold: Windows paths are case-insensitive (so two
    spellings must collide), POSIX paths are not (so they must not).
    """
    try:
        path = path.resolve()
    except OSError:
        pass  # unresolvable: hash what we were given rather than lock nothing
    digest = hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    return _EDIT_LOCK_DIR / f"{digest}.lock"


@contextmanager
def _edit_lock(path: Path):
    """Serialize edits to `path` across threads AND processes.

    Mirrors the `with self._lock, _FileLock(...)` guard in
    services/task_store.py. Raises TimeoutError when another process holds the
    file past _FileLock's bounded wait — surfaced as a refusal rather than
    blocking a session indefinitely behind a wedged holder.
    """
    with _get_file_lock(path), _FileLock(_lock_sidecar(path)):
        yield


def _read_preserving_newlines(path: Path) -> str:
    """A file's text exactly as stored: line endings are not touched.

    Decodes with errors="surrogateescape" so files containing non-UTF-8
    bytes are still editable: invalid bytes round-trip losslessly through
    _write_preserving_newlines instead of raising UnicodeDecodeError.
    """
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


# UTF-32 first: its little-endian BOM begins with the UTF-16 one.
_FOREIGN_BOMS = ((codecs.BOM_UTF32_LE, "UTF-32"), (codecs.BOM_UTF32_BE, "UTF-32"),
                 (codecs.BOM_UTF16_LE, "UTF-16"), (codecs.BOM_UTF16_BE, "UTF-16"))


def _encoding_refusal(raw: bytes, file_label: str) -> str:
    """A refusal when `raw` opens with a UTF-16 or UTF-32 byte-order mark,
    else "". Read as UTF-8 such a file is NUL-separated characters that no
    old_string matches."""
    name = next((n for bom, n in _FOREIGN_BOMS if raw.startswith(bom)), "")
    if not name:
        return ""
    return (f"[c3_edit:encoding] {file_label} is {name} text. c3_edit and "
            f"c3_read work on UTF-8 only, so nothing was written.\n"
            f"  Convert the file to UTF-8 first, or change it with the tool "
            f"that owns it.")


def _write_preserving_newlines(path: Path, content: str) -> None:
    """Atomically replace `path` with `content`, encoded the way it was read.

    Raises PermissionError for an existing read-only target rather than
    publishing a new file over it.
    """
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError(f"{path} is read-only")
    write_bytes_atomic(path, content.encode("utf-8", errors="surrogateescape"))


_EOLS = ("\n", "\r\n", "\r")
_EOL_RE = re.compile(r"\r\n|\r|\n")


def _eol_norm(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n") if s else s


def _dominant_eol(text: str, default: str = "\n") -> str:
    """The most frequent EOL in `text`; `default` when absent or tied with it."""
    counts = dict.fromkeys(_EOLS, 0)
    for m in _EOL_RE.finditer(text):
        counts[m.group()] += 1
    best = max(counts.values())
    if best == 0 or counts.get(default) == best:
        return default
    return max(_EOLS, key=counts.__getitem__)


# Typographic characters a model tends to retype as ASCII. An old_string that
# matches only after this fold is refused, not applied: new_string would
# overwrite the file's characters inside the span with the retyped ones.
# Strictly 1:1 so a folded offset is also an offset in the file text.
_LOOKALIKE_TRANS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",  # single curly quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',  # double curly quotes
    "′": "'", "″": '"',                                 # prime / double prime
    "‐": "-", "‑": "-", "‒": "-", "–": "-",  # hyphen / dashes
    "—": "-", "―": "-", "−": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",  # non-breaking / figure / narrow-nbsp
})

# Bytes that aren't valid UTF-8 decode to lone surrogates U+DC80-U+DCFF under
# errors="surrogateescape" (how _read_preserving_newlines reads files), while
# c3_read renders those same bytes as U+FFFD (errors="replace"). Fold both to
# U+FFFD — 1:1 like the lookalikes — so an old_string copied from c3_read
# output still matches file content around undecodable bytes.
_SURROGATE_TRANS = {c: "\ufffd" for c in range(0xDC80, 0xDD00)}
_LOOKALIKE_TRANS.update(_SURROGATE_TRANS)


def _display_safe(s: str) -> str:
    """Fold surrogateescape'd bytes to U+FFFD for error-message display —
    lone surrogates cannot be encoded for MCP transport."""
    return s.translate(_SURROGATE_TRANS) if s else s


def _norm(s: str) -> str:
    return s.translate(_LOOKALIKE_TRANS) if s else s


def _positional_replace(content: str, view: str, crlf_at: list, needle: str,
                        new: str, replace_all: bool, file_eol: str) -> str:
    """Replace `needle` found in `view` by splicing `new` into `content`.

    `view` is `content` with every EOL as ``\\n``; `crlf_at` holds the sorted view offsets of the ``\\n``
    characters that are ``\\r\\n`` in `content`, so view offset v is raw
    offset v + bisect_left(crlf_at, v). `new` uses ``\\n`` and is written with
    the dominant EOL of the span it replaces (`file_eol` when that span has
    none). Bytes outside the replaced spans are copied unchanged.
    """
    parts: list[str] = []
    raw_i = i = 0
    n = len(needle)
    while True:
        pos = view.find(needle, i)
        if pos < 0:
            break
        start = pos + bisect.bisect_left(crlf_at, pos)
        end = pos + n + bisect.bisect_left(crlf_at, pos + n)
        parts.append(content[raw_i:start])
        eol = _dominant_eol(content[start:end], file_eol)
        parts.append(new if eol == "\n" else new.replace("\n", eol))
        raw_i, i = end, pos + n
        if not replace_all:
            break
    parts.append(content[raw_i:])
    return "".join(parts)


def _apply_replacement(content: str, old: str, new: str, replace_all: bool):
    """Replace `old` with `new` in `content` without disturbing other bytes.

    Matching ignores EOL style (``\\r\\n``, ``\\r`` and ``\\n`` are equal), so a
    LF old_string matches a CRLF, CR-only or mixed file, and reads an
    undecodable byte as the U+FFFD that c3_read showed for it. Only the
    matched spans change.

    Returns (new_content, count, lookalike):
      - (str, count, False)   when at least one match was applied
      - (None, 0, False)      when no match is found, or `old` is empty
      - (None, count, False)  when count > 1 and replace_all is False
      - (None, count, True)   when `old` matches only after the lookalike
                              fold; never applied
    """
    if not old:
        return (None, 0, False)
    old, new = _eol_norm(old), _eol_norm(new)
    view = _eol_norm(content)
    crlf_at = [m.start() - k for k, m in enumerate(re.finditer("\r\n", content))]
    file_eol = _dominant_eol(content)

    count = view.count(old)
    if count == 0:
        view = view.translate(_SURROGATE_TRANS)
        old = old.translate(_SURROGATE_TRANS)
        count = view.count(old)
    if count == 0:
        folded = _norm(view).count(_norm(old))
        return (None, folded, folded > 0)
    if count > 1 and not replace_all:
        return (None, count, False)
    return (_positional_replace(content, view, crlf_at, old, new, replace_all,
                                file_eol), count, False)


def _closest_region(content: str, old: str,
                    max_scan_lines: int = 40000, context: int = 2):
    """Locate the file region most similar to a failed old_string.

    Returns (start_line, end_line, region_text, ratio) — 1-based inclusive
    line numbers, region padded with `context` lines each side — or None when
    no region clears the similarity floor. Powers the 'closest match' payload
    in not-found errors so a mismatched edit can be repaired without
    re-reading the file (the moment agents historically drifted back to
    native Read).
    """
    file_lines = content.split("\n")
    old_lines = old.split("\n")
    n, total = len(old_lines), len(file_lines)
    if not old.strip() or total > max_scan_lines:
        return None

    # Anchor on old_string's most distinctive line, shortlist file lines that
    # resemble it (cheap quick_ratio pass), then score full windows aligned
    # to each shortlisted line (expensive ratio, capped at 8 candidates).
    anchor_off, anchor_line = max(enumerate(old_lines),
                                  key=lambda p: len(p[1].strip()))
    anchor = anchor_line.strip()
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(anchor)
    scored = []
    for i, line in enumerate(file_lines):
        sm.set_seq1(line.strip())
        if sm.real_quick_ratio() < 0.6:
            continue
        q = sm.quick_ratio()
        if q >= 0.6:
            scored.append((q, i))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])

    win = difflib.SequenceMatcher(autojunk=False)
    win.set_seq2(old)
    best_ratio, best_start = 0.0, None
    for _, i in scored[:8]:
        start = max(0, min(i - anchor_off, total - n))
        win.set_seq1("\n".join(file_lines[start:start + n]))
        r = win.ratio()
        if r > best_ratio:
            best_ratio, best_start = r, start
    if best_start is None or best_ratio < 0.5:
        return None
    lo = max(0, best_start - context)
    hi = min(total, best_start + n + context)
    return lo + 1, hi, "\n".join(file_lines[lo:hi]), best_ratio


_REGION_CAP = 4000


def _marked_region(lo: int, hi: int, region: str, file_label: str) -> str:
    """File lines lo..hi between markers, verbatim and unindented so they can
    be copied straight into a retry old_string."""
    region = _display_safe(region)
    if len(region) > _REGION_CAP:
        region = (region[:_REGION_CAP]
                  + f"\n⟦trimmed — run c3_read(file_path='{file_label}', "
                  f"lines=[{lo},{hi}]) for the rest⟧")
    return f"⟦L{lo}-L{hi}⟧\n{region}\n⟦end⟧\n"


def _not_found_payload(near, file_label: str) -> str:
    """Render a _closest_region result as an error-message appendix."""
    if not near:
        return ""
    lo, hi, region, ratio = near
    return (f"\n  closest match: L{lo}-L{hi} ({ratio:.0%} similar). "
            f"Actual file text between the markers:\n"
            + _marked_region(lo, hi, region, file_label)
            + "  Retry with old_string copied exactly from the text above "
              "(markers excluded) — no need to re-read the file.")


def _revert_note(images: dict) -> str:
    """Response text for a write whose images were not kept."""
    blob = images.get("blob") or ""
    if not blob.startswith("skipped:"):
        return ""
    return (f"\n  ⚠ c3_edits cannot revert this edit: its before and after "
            f"images were not kept ({blob.partition(':')[2]}).")


def _lookalike_payload(content: str, old: str, count: int,
                       file_label: str) -> str:
    """The file's own text for the first place `old` matches after the
    lookalike fold, as an error-message appendix."""
    view, old = _eol_norm(content), _eol_norm(old)
    pos = _norm(view).find(_norm(old))
    lo = view.count("\n", 0, pos) + 1
    hi = lo + old.count("\n")
    region = "\n".join(view.split("\n")[lo - 1:hi])
    places = f" ({count} places; the first is shown)" if count > 1 else ""
    return (f"\n  It matches only when curly quotes, dashes and non-breaking "
            f"spaces are read as their ASCII lookalikes{places}. "
            f"Nothing was written.\n"
            f"  Actual file text between the markers:\n"
            + _marked_region(lo, hi, region, file_label)
            + "  Retry with old_string copied exactly from the text above "
              "(markers excluded), and keep those characters in new_string "
              "unless you mean to change them.")


def handle_edit(file_path: str, old_string: str, new_string: str,
                summary: str, tags: str, replace_all: bool,
                svc, finalize, edits: str = "", overwrite: bool = False) -> str:
    """Find old_string in file, replace with new_string, write back, log to ledger.

    edits: optional JSON list of {old_string, new_string, summary?} dicts for
           batch same-file patching in a single read/write cycle.
    overwrite: replace all of an existing file with new_string. The session
           must have read the file, and it must not have changed since.
    """
    finalize = _Report(finalize, svc)
    if not file_path:
        return finalize("c3_edit", {}, "file_path is required", "missing param")
    if overwrite and (old_string or edits):
        return finalize("c3_edit", {"file": file_path},
                        "overwrite replaces the whole file: pass new_string "
                        "only, without old_string or edits", "bad overwrite")

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(svc.project_path) / path
    path = path.resolve()

    # Vault write-guard: the credential registry/state is never agent-writable
    # (covers create, edit, and batch modes — and c3_project's edit proxy).
    guard = _cs.vault_guard_reason(path)
    if guard:
        return finalize("c3_edit", {"file": file_path}, guard, "vault-protected")

    # Relative path for ledger + display (computed even for new files)
    try:
        rel = str(path.relative_to(Path(svc.project_path).resolve())).replace("\\", "/")
    except ValueError:
        rel = file_path

    wrong_tree, where = _edit_report.locate(file_path, path, svc.project_path)
    if wrong_tree:
        return finalize("c3_edit", {"file": file_path}, wrong_tree, "wrong tree")

    op = "write" if path.exists() else "create"
    session_id = _session_id(svc)
    had_lease = agent_locks.holds(str(path), svc.project_path, session_id)
    refusal = _write_gate(svc, path, rel, file_path, op, "c3_edit", summary,
                          finalize)
    if refusal is not None:
        return refusal

    # Everything that reads or writes the file runs under one lock, held for
    # the whole read → modify → write cycle. Create mode is inside it too:
    # without that, two agents creating the same path both "succeed" and the
    # loser's content is silently gone.
    try:
        with _edit_lock(path):
            if overwrite and path.exists():
                return _overwrite_locked(path, rel, file_path, new_string,
                                         summary, tags, svc, finalize, where)
            return _edit_locked(path, rel, file_path, old_string, new_string,
                                summary, tags, replace_all, svc, finalize, edits,
                                where)
    except TimeoutError:
        return finalize(
            "c3_edit", {"file": file_path},
            f"[c3-lock:busy] {rel} is held by another C3 process and did not free "
            f"up in time.\n"
            f"  This is contention, not an error — do not route around it via "
            f"c3_shell or native Write. Retry, or edit a different file.",
            "lock busy")
    finally:
        # A lease exists to keep others off a file this session is changing.
        # One taken by a call that changed nothing protects no work.
        if finalize.wrote:
            _edit_report.note_write(path, svc.project_path)
        elif not had_lease:
            agent_locks.give_back(str(path), svc.project_path, session_id)


def _write_gate(svc, path: Path, rel: str, file_path: str, op: str,
                tool: str, intent: str, finalize) -> str | None:
    """The gates every C3 write to a project file passes before `_edit_lock`.

    Access Guard write verdict for `op` (write|create|delete), honouring a
    live grant and auto-filing a confirm hold's Override Request; then the
    agent-lock check; then this session's lease on `path`, taken before the
    work so a second agent is blocked for its whole duration.

    Returns a finalized refusal under `tool`, or None once the lease is held.
    """
    denial = access_guard.check(str(path), op, svc.project_path)
    if denial and not _grants.allow(svc, denial, tool=tool, op=op,
                                    path=str(path)):
        rid, note = _grants.confirm_request(svc, denial, tool=tool,
                                            op=op, path=str(path))
        return finalize(tool, {"file": file_path},
                        access_guard.refusal(denial, file_path, op,
                                             request_id=rid,
                                             request_note=note),
                        "access-denied")

    # Agent Locks (Layer B) come AFTER the policy guards: an agent must never
    # be told a file is busy when it was never allowed to write it in the
    # first place (docs/agent-locks.md §5).
    session_id = _session_id(svc)
    holder = agent_locks.check(str(path), svc.project_path, session_id)
    if holder:
        held_for = round(time.time() - holder.get("acquired_at", time.time()))
        return finalize(tool, {"file": file_path,
                               "holder": holder.get("agent_id", ""),
                               "held_for_s": held_for},
                        agent_locks.refusal(holder, rel), "lock held")
    agent_locks.lease(str(path), svc.project_path, session_id, intent=intent)
    return None


def _overwrite_locked(path: Path, rel: str, file_path: str, new_string: str,
                      summary: str, tags: str, svc, finalize, where: str) -> str:
    """Replace all of an existing file, keeping its line endings and BOM.
    Always called under _edit_lock."""
    args = {"file": file_path, "overwrite": True}
    slug = "unread" if read_stamps.get(path) is None else "stale read"
    guard = read_stamps.check_whole(path, file_path)
    if guard.refusal:
        return finalize("c3_edit", args, guard.refusal, slug)
    try:
        pre_image = path.read_bytes()
    except OSError as e:
        return finalize("c3_edit", args, f"Read error: {e}", "read error")
    foreign = _encoding_refusal(pre_image, file_path)
    if foreign:
        return finalize("c3_edit", args, foreign, "encoding")

    content = pre_image.decode("utf-8", errors="surrogateescape")
    eol = _dominant_eol(content)
    new_content = _eol_norm(new_string)
    if eol != "\n":
        new_content = new_content.replace("\n", eol)
    if content.startswith("﻿") and not new_content.startswith("﻿"):
        new_content = "﻿" + new_content
    if new_content == content:
        return finalize("c3_edit", args,
                        f"{rel} unchanged — new_string is the file's current "
                        f"text; nothing was written or logged.",
                        f"{rel} unchanged", outcome=_NOOP, n_attempted=1,
                        n_applied=0)

    edit_blobs.keep_pre(svc.project_path, path, pre_image)
    try:
        _write_preserving_newlines(path, new_content)
        guard.written()
    except Exception as e:
        return finalize("c3_edit", args, f"Write error: {e}", "write error")

    images = edit_blobs.record_edit(svc.project_path, path, pre_image)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    n_old = len(content.splitlines())
    n_new = len(new_content.splitlines())
    deferred = _log_to_ledger(
        rel, summary or f"Overwrote {rel} ({n_new}L)", tag_list, svc,
        detail={"old_string": "", "new_string": new_string[:_DETAIL_CAP],
                "overwritten": True, **images})
    short = (f"✓ {rel} [overwritten, -{n_old}+{n_new}L]"
             + (f" — {summary}" if summary else "")
             + where + _revert_note(images) + _display_safe(
                 _edit_report.diff_block(content, new_content, svc.project_path)))
    return finalize("c3_edit", args, short + deferred, f"{rel} overwritten",
                    n_attempted=1, n_applied=1, blob=images.get("blob"))


def _edit_locked(path: Path, rel: str, file_path: str, old_string: str,
                 new_string: str, summary: str, tags: str, replace_all: bool,
                 svc, finalize, edits: str, where: str = "") -> str:
    """Create / batch / single-edit bodies. Always called under _edit_lock.

    where: text from ``_edit_report.locate`` appended after a success line.
    """
    stale = read_stamps.check(path, svc.project_path, file_path, old_string,
                              edits, replace_all, norm=_norm)
    if stale.refusal:
        return finalize("c3_edit", {"file": file_path}, stale.refusal, "stale read")
    finalize = stale.wrap(finalize)

    # ── Create mode ───────────────────────────────────────────────────────────
    # File doesn't exist + single-edit mode + empty old_string → create file.
    # Batch mode always requires an existing file.
    if not path.exists():
        if edits:
            return finalize("c3_edit", {"file": file_path},
                            f"File not found: {file_path} (batch edits require an existing file)",
                            "not found")
        if old_string:
            return finalize("c3_edit", {"file": file_path},
                            f"File not found: {file_path}", "not found")

        try:
            write_bytes_atomic(path, new_string.encode("utf-8"))
            stale.written()
        except Exception as e:
            return finalize("c3_edit", {"file": file_path},
                            f"Create error: {e}", "create error")
        images = edit_blobs.record_edit(svc.project_path, path, None)

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        n_new = new_string.count("\n") + 1 if new_string else 0
        create_summary = summary or f"Created {rel} ({n_new}L)"
        deferred = _log_to_ledger(
            rel, create_summary, tag_list, svc,
            detail={"old_string": "", "new_string": new_string[:_DETAIL_CAP],
                    "created": True, **images})
        short = (f"✓ {rel} [created, +{n_new}L]" + (f" — {summary}" if summary else "")
                 + where + _revert_note(images))
        return finalize("c3_edit", {"file": file_path}, short + deferred,
                        f"{rel} created", n_attempted=1, n_applied=1,
                        blob=images.get("blob"))

    # Parse tag list once
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    # ── Batch mode ────────────────────────────────────────────────────────────
    if edits:
        try:
            edit_list = json.loads(edits) if isinstance(edits, str) else edits
        except json.JSONDecodeError as exc:
            return finalize("c3_edit", {"file": file_path},
                            f"edits must be a valid JSON list: {exc}", "bad edits param")

        if not isinstance(edit_list, list) or not edit_list:
            return finalize("c3_edit", {"file": file_path},
                            "edits must be a non-empty JSON list", "bad edits param")

        if not all(isinstance(p, dict) for p in edit_list):
            return finalize("c3_edit", {"file": file_path},
                            "edits must be a JSON list of objects "
                            "({old_string, new_string, ...}); a non-object element was found",
                            "bad edits param")

        try:
            pre_image = path.read_bytes()
            content = _read_preserving_newlines(path)
        except Exception as e:
            return finalize("c3_edit", {"file": file_path},
                            f"Read error: {e}", "read error")
        foreign = _encoding_refusal(pre_image, file_path)
        if foreign:
            return finalize("c3_edit", {"file": file_path}, foreign, "encoding")
        original = content

        results = []
        statuses = []   # parallel to results: 'ok' | 'miss' | 'lookalike' | 'ambiguous' | 'skipped' | 'noop'
        first_miss = ""
        for i, patch in enumerate(edit_list):
            old = patch.get("old_string", "")
            new = patch.get("new_string", "")
            patch_summary = patch.get("summary", "")
            r_all = patch.get("replace_all", False)

            if not old:
                results.append(f"  patch[{i}]: skipped — empty old_string")
                statuses.append("skipped")
                continue

            new_content, count, lookalike = _apply_replacement(content, old, new, r_all)
            if lookalike:
                results.append(f"  patch[{i}]: LOOKALIKE ONLY — {old[:80]!r}")
                statuses.append("lookalike")
                if not first_miss:
                    on_disk = _apply_replacement(original, old, new, r_all)
                    first_miss = (
                        _lookalike_payload(original, old, on_disk[1], file_path)
                        if on_disk[2] else
                        _lookalike_payload(content, old, count, file_path))
                continue
            if new_content is None and count == 0:
                # Nothing is written on a miss, so line numbers are the
                # file's; an earlier patch's result is the second choice.
                near = (_closest_region(_eol_norm(original), _eol_norm(old))
                        or _closest_region(_eol_norm(content), _eol_norm(old)))
                loc = (f" (closest: L{near[0]}-L{near[1]}, {near[3]:.0%} similar)"
                       if near else "")
                results.append(f"  patch[{i}]: NOT FOUND — {old[:80]!r}{loc}")
                statuses.append("miss")
                if near and not first_miss:
                    first_miss = _not_found_payload(near, file_path)
                continue
            if new_content is None:
                results.append(f"  patch[{i}]: AMBIGUOUS ({count} matches) — {old[:60]!r}")
                statuses.append("ambiguous")
                continue
            if new_content == content:
                results.append(f"  patch[{i}]: no change — new_string equals "
                               f"old_string — {old[:60]!r}")
                statuses.append("noop")
                continue

            content = new_content
            n = count if r_all else 1

            n_old = old.count("\n") + 1
            n_new = new.count("\n") + 1
            desc = patch_summary or f"{old[:50]!r} → {new[:50]!r}"
            results.append(f"  patch[{i}]: -{n_old}L +{n_new}L"
                            + (f" ({n}x)" if n > 1 else "")
                            + f" | {desc}")
            statuses.append("ok")

        # All or nothing: the patches that did match are half of a change
        # once one of them cannot be placed.
        total = len(edit_list)
        failed = [r for r, s in zip(results, statuses) if s in _BATCH_FAILURES]
        if failed:
            outcome = next(_BATCH_FAILURES[s] for s in statuses
                           if s in _BATCH_FAILURES)
            return finalize(
                "c3_edit", {"file": file_path},
                f"{rel} unchanged — {len(failed)} of {total} patches could not "
                f"be placed, so none were applied.\n" + "\n".join(failed)
                + first_miss
                + "\n  Resend the whole batch with those patches corrected.",
                f"{rel} batch refused", outcome=outcome, n_attempted=total,
                n_applied=0)

        unchanged = [r for r, s in zip(results, statuses) if s == "noop"]
        if content == original:
            return finalize(
                "c3_edit", {"file": file_path},
                f"{rel} unchanged — no patch changes the text; nothing was "
                f"written or logged.\n" + "\n".join(unchanged),
                f"{rel} unchanged", outcome=_NOOP, n_attempted=total,
                n_applied=0)

        edit_blobs.keep_pre(svc.project_path, path, pre_image)
        try:
            _write_preserving_newlines(path, content)
            stale.written()
        except Exception as e:
            return finalize("c3_edit", {"file": file_path},
                            f"Write error: {e}", "write error")

        # One ledger entry for the batch, with each patch's old/new for the diff view.
        batch_detail = {"patches": [
            {
                "old_string": p.get("old_string", "")[:_DETAIL_CAP],
                "new_string": p.get("new_string", "")[:_DETAIL_CAP],
                **({"summary": p["summary"]} if p.get("summary") else {}),
            }
            for p in edit_list if p.get("old_string") is not None
        ]}
        batch_detail.update(
            edit_blobs.record_edit(svc.project_path, path, pre_image))
        deferred = _log_to_ledger(
            rel, summary or f"Batch edit: {total} patches",
            tag_list, svc, detail=batch_detail)

        applied = statuses.count("ok")
        short = (f"✓ {rel} — {applied}/{total} patches applied"
                 + "".join(f"\n{r}" for r in unchanged)
                 + where + _revert_note(batch_detail) + _display_safe(
                     _edit_report.diff_block(original, content, svc.project_path)))
        return finalize("c3_edit", {"file": file_path}, short + deferred,
                        f"{rel} patched ({applied}/{total} patches)",
                        n_attempted=total, n_applied=applied,
                        blob=batch_detail.get("blob"))

    # ── Single-edit mode ──────────────────────────────────────────────────────
    if not old_string:
        return finalize("c3_edit", {"file": file_path},
                        f"old_string is empty but {file_path} exists — pass the "
                        f"text to replace, or use edits", "empty old_string")

    try:
        pre_image = path.read_bytes()
        content = _read_preserving_newlines(path)
    except Exception as e:
        return finalize("c3_edit", {"file": file_path},
                        f"Read error: {e}", "read error")
    foreign = _encoding_refusal(pre_image, file_path)
    if foreign:
        return finalize("c3_edit", {"file": file_path}, foreign, "encoding")

    new_content, count, lookalike = _apply_replacement(
        content, old_string, new_string, replace_all)

    if lookalike:
        return finalize("c3_edit", {"file": file_path},
                        f"[c3_edit:lookalike] old_string is not in {file_path} "
                        f"as written."
                        + _lookalike_payload(content, old_string, count, file_path),
                        "lookalike")
    if new_content is None and count == 0:
        hint = _not_found_payload(
            _closest_region(_eol_norm(content), _eol_norm(old_string)), file_path)
        return finalize("c3_edit", {"file": file_path},
                        f"old_string not found in {file_path}\n"
                        f"  searched for: {old_string[:120]!r}{hint}",
                        "not found")
    if new_content is None:
        return finalize("c3_edit", {"file": file_path},
                        f"old_string matches {count} locations — add more context to make it unique, "
                        f"or pass replace_all=true to replace all occurrences.",
                        "ambiguous")

    if new_content == content:
        return finalize("c3_edit", {"file": file_path},
                        f"{rel} unchanged — new_string equals old_string; "
                        f"nothing was written or logged.", f"{rel} unchanged",
                        outcome=_NOOP, n_attempted=1, n_applied=0)

    occurrences = count if replace_all else 1

    edit_blobs.keep_pre(svc.project_path, path, pre_image)
    try:
        _write_preserving_newlines(path, new_content)
        stale.written()
    except Exception as e:
        return finalize("c3_edit", {"file": file_path},
                        f"Write error: {e}", "write error")

    auto_summary = (summary or
                    f"Replaced: {old_string[:60]!r} → {new_string[:60]!r}"
                    + (f" ({occurrences}x)" if occurrences > 1 else ""))
    single_detail = {
        "old_string": old_string[:_DETAIL_CAP],
        "new_string": new_string[:_DETAIL_CAP],
    }
    single_detail.update(
        edit_blobs.record_edit(svc.project_path, path, pre_image))
    deferred = _log_to_ledger(rel, auto_summary, tag_list, svc,
                              detail=single_detail)

    n_old = old_string.count("\n") + 1
    n_new = new_string.count("\n") + 1
    delta = f"-{n_old}+{n_new}L"
    occ = f" ({occurrences}x)" if occurrences > 1 else ""
    short = (f"✓ {rel} [{delta}]{occ}" + (f" — {summary}" if summary else "")
             + where + _revert_note(single_detail) + _display_safe(
                 _edit_report.diff_block(content, new_content, svc.project_path)))
    return finalize("c3_edit", {"file": file_path}, short + deferred,
                    f"{rel} patched", n_attempted=1, n_applied=1,
                    blob=single_detail.get("blob"))


_DETAIL_CAP = 2000  # chars stored per old/new string in the ledger

#: How long the post-write bookkeeping gets before the caller stops waiting on
#: it. Normal is milliseconds — a JSONL append and a git call that already caps
#: itself at 4s. Ten seconds is far outside normal and far inside the harness's
#: idle timeout, which is the number this exists to stay away from.
_LEDGER_DEADLINE_S = 10.0

_DEFERRED_NOTE = (
    "\n  [c3:ledger-deferred] The FILE WRITE SUCCEEDED. Recording it in the "
    "ledger outran {sec:.0f}s and was left running in the background, so the "
    "edit may be missing from c3_edits history and from "
    "c3_edits(action='verify') corroboration.\n"
    "  Do not re-apply this edit on the strength of that absence — the file is "
    "the evidence, and it already has the change."
)


def _log_to_ledger(rel: str, summary: str, tag_list, svc,
                   detail: dict = None, change_type: str = "modified") -> str:
    """Record an edit in the ledger, activity log, and session manager.

    Never raises, and — since #74 — never blocks the caller indefinitely.

    The write has already happened by the time this runs. That ordering is what
    makes a stall here so expensive: one logged `c3_edit` hung until the harness
    aborted it at 1800s while the file on disk was already correct, so half an
    hour bought a report of work that had finished in milliseconds.

    The host-side cause of that stall is not C3's (see #74 — the box hits
    commit-charge exhaustion in short unpredictable windows), and nothing here
    prevents it. What this does prevent is *paying 1800s to find out*. The
    bookkeeping runs on a daemon thread; if it outlives the deadline the caller
    returns anyway, with the response saying plainly that the write landed and
    the record may not have. The thread is left alone rather than killed — it
    may well finish a moment later, and a half-written ledger entry would be
    worse than a late one.

    Returns "" normally, or a note to append to the response when the deadline
    passed. Reporting a degraded record is the point; a silent one would make
    this indistinguishable from a clean run, which is the failure class this
    whole subsystem exists to remove.
    """
    if not svc.edit_ledger:
        return ""
    done = threading.Event()

    def _record() -> None:
        try:
            _log_to_ledger_blocking(rel, summary, tag_list, svc, detail,
                                    change_type)
        finally:
            done.set()

    threading.Thread(target=_record, name="c3-edit-ledger",
                     daemon=True).start()
    if done.wait(_LEDGER_DEADLINE_S):
        return ""
    return _DEFERRED_NOTE.format(sec=_LEDGER_DEADLINE_S)


def _log_to_ledger_blocking(rel: str, summary: str, tag_list, svc,
                            detail: dict = None,
                            change_type: str = "modified") -> None:
    """The bookkeeping itself. Never raises; may block. Always run on a thread."""
    if not svc.edit_ledger:
        return
    try:
        entry = svc.edit_ledger.log_edit(
            file=rel,
            change_type=change_type,
            summary=summary,
            tags=tag_list,
            detail=detail,
        )
        if svc.activity_log:
            svc.activity_log.log("file_change", {
                "file": rel,
                "change_type": change_type,
                "summary": summary,
                "edit_id": entry.get("id", ""),
            })
        if svc.session_mgr and hasattr(svc.session_mgr, "log_file_change"):
            svc.session_mgr.log_file_change(rel, change_type)
    except Exception:
        pass
    # Agent-artifact capture: synchronous, fully attributed (session + summary).
    try:
        from services.artifact_defs import classify_path
        store = getattr(svc, "artifact_store", None)
        if store is not None and classify_path(rel) is not None:
            session = getattr(getattr(svc, "session_mgr", None),
                              "current_session", None) or {}
            store.note_write(rel, "c3_edit", session_id=session.get("id", ""),
                             summary=summary)
    except Exception:
        pass
