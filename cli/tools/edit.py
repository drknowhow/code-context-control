"""c3_edit — in-place file patch: read + replace + write + ledger log in one step.

Bypasses the native Edit tool's requirement for a prior native Read call,
so c3_read → c3_edit works without an intermediate redundant native read.

Parallel safety:
- Different files: safe to call concurrently (no shared state).
- Same file: serialized via per-file threading.Lock (_file_locks).
- Same file, multiple hunks: use the `edits` batch parameter — one read/write cycle.
"""
import difflib
import json
import threading
from pathlib import Path

from services import credential_store as _cs

# Per-file locks — keyed by resolved absolute path string.
_file_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_lock:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


def _read_preserving_newlines(path: Path) -> tuple[str, str]:
    """Read a file's text and detect its dominant newline style.

    Returns (content, newline) where `content` has all line endings
    normalized to ``\n`` (so existing replace logic is unchanged) and
    `newline` is the EOL to write back: ``\r\n`` if CRLF dominates the
    file, otherwise ``\n``. This avoids Python's text-mode write rewriting
    every line to ``os.linesep`` on Windows.

    Decodes with errors="surrogateescape" so files containing non-UTF-8
    bytes are still editable: invalid bytes round-trip losslessly through
    _write_preserving_newlines instead of raising UnicodeDecodeError.
    """
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    lf_only = raw.count(b"\n") - crlf
    newline = "\r\n" if crlf > lf_only else "\n"
    content = raw.decode("utf-8", errors="surrogateescape")
    # Normalize to \n internally so replacement matching is EOL-agnostic.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return content, newline


def _write_preserving_newlines(path: Path, content: str, newline: str) -> None:
    """Write `content` (which uses ``\n``) back using the original EOL style.

    Uses ``newline=""`` so Python performs no translation; we emit the
    detected EOL explicitly so an LF-only file stays LF-only on Windows.
    """
    if newline != "\n":
        content = content.replace("\n", newline)
    with open(path, "w", encoding="utf-8", errors="surrogateescape",
              newline="") as fh:
        fh.write(content)


# Unicode lookalike substitutions used as a fallback when the literal
# old_string is not found. Strictly 1:1 (same-length) substitutions so
# positions are preserved — we locate the match on the normalized string
# and splice the replacement into the original content at the same offsets.
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


def _positional_replace(content: str, norm_content: str, norm_old: str,
                         new: str, replace_all: bool) -> str:
    """Replace occurrences of `norm_old` in `norm_content` with `new`, splicing
    into the matching offsets of the original `content`. Safe because
    _LOOKALIKE_TRANS is 1:1 (character lengths are preserved)."""
    parts: list[str] = []
    i = 0
    n = len(norm_old)
    while True:
        pos = norm_content.find(norm_old, i)
        if pos < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:pos])
        parts.append(new)
        i = pos + n
        if not replace_all:
            parts.append(content[i:])
            break
    return "".join(parts)


def _apply_replacement(content: str, old: str, new: str, replace_all: bool):
    """Try direct replace; on zero matches, retry with unicode-lookalike
    normalization (curly quotes, unicode dashes, NBSP).

    Returns (new_content, count, used_fallback):
      - (str, count, bool) when at least one match was applied
      - (None, 0, False)   when no match is found (even after fallback)
      - (None, count, bool) when count > 1 and replace_all is False
    """
    count = content.count(old)
    if count >= 1:
        if count > 1 and not replace_all:
            return (None, count, False)
        return (content.replace(old, new, -1 if replace_all else 1), count, False)

    # Fallback: normalize unicode lookalikes on both sides.
    nc = _norm(content)
    no = _norm(old)
    if nc == content and no == old:
        # Neither side contained any lookalike chars — genuinely not found.
        return (None, 0, False)
    count = nc.count(no)
    if count == 0:
        return (None, 0, False)
    if count > 1 and not replace_all:
        return (None, count, True)
    return (_positional_replace(content, nc, no, new, replace_all), count, True)


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


def _not_found_payload(near, file_label: str) -> str:
    """Render a _closest_region result as an error-message appendix. The
    region text is emitted verbatim (no indentation) so it can be copied
    straight into a retry old_string."""
    if not near:
        return ""
    lo, hi, region, ratio = near
    region = _display_safe(region)
    if len(region) > _REGION_CAP:
        region = (region[:_REGION_CAP]
                  + f"\n⟦trimmed — run c3_read(file_path='{file_label}', "
                  f"lines=[{lo},{hi}]) for the rest⟧")
    return (f"\n  closest match: L{lo}-L{hi} ({ratio:.0%} similar). "
            f"Actual file text between the markers:\n"
            f"⟦L{lo}-L{hi}⟧\n{region}\n⟦end⟧\n"
            f"  Retry with old_string copied exactly from the text above "
            f"(markers excluded) — no need to re-read the file.")


def handle_edit(file_path: str, old_string: str, new_string: str,
                summary: str, tags: str, replace_all: bool,
                svc, finalize, edits: str = "") -> str:
    """Find old_string in file, replace with new_string, write back, log to ledger.

    edits: optional JSON list of {old_string, new_string, summary?} dicts for
           batch same-file patching in a single read/write cycle.
    """
    if not file_path:
        return finalize("c3_edit", {}, "file_path is required", "missing param")

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
            path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" → write content exactly as given; no os.linesep
            # translation, so the caller's line endings are preserved verbatim.
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_string)
        except Exception as e:
            return finalize("c3_edit", {"file": file_path},
                            f"Create error: {e}", "create error")

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        n_new = new_string.count("\n") + 1 if new_string else 0
        create_summary = summary or f"Created {rel} ({n_new}L)"
        _log_to_ledger(rel, create_summary, tag_list, svc,
                       detail={"old_string": "", "new_string": new_string[:_DETAIL_CAP], "created": True})
        short = f"✓ {rel} [created, +{n_new}L]" + (f" — {summary}" if summary else "")
        return finalize("c3_edit", {"file": file_path}, short, f"{rel} created")

    # Parse tag list once
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    file_lock = _get_file_lock(path)

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

        with file_lock:
            try:
                content, _newline = _read_preserving_newlines(path)
            except Exception as e:
                return finalize("c3_edit", {"file": file_path},
                                f"Read error: {e}", "read error")

            results = []
            statuses = []   # parallel to results: 'ok' | 'miss' | 'ambiguous' | 'skipped'
            any_normalized = False
            any_applied = False
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

                new_content, count, used_fallback = _apply_replacement(content, old, new, r_all)
                if new_content is None and count == 0:
                    near = _closest_region(content, old)
                    loc = (f" (closest: L{near[0]}-L{near[1]}, {near[3]:.0%} similar)"
                           if near else "")
                    results.append(f"  patch[{i}]: NOT FOUND — {old[:80]!r}{loc}")
                    statuses.append("miss")
                    if near and not first_miss:
                        first_miss = _not_found_payload(near, file_path)
                    continue
                if new_content is None:
                    tag = " (unicode-normalized)" if used_fallback else ""
                    results.append(f"  patch[{i}]: AMBIGUOUS ({count} matches){tag} — {old[:60]!r}")
                    statuses.append("ambiguous")
                    continue

                content = new_content
                any_applied = True
                n = count if r_all else 1
                if used_fallback:
                    any_normalized = True

                n_old = old.count("\n") + 1
                n_new = new.count("\n") + 1
                desc = patch_summary or f"{old[:50]!r} → {new[:50]!r}"
                results.append(f"  patch[{i}]: -{n_old}L +{n_new}L"
                                + (f" ({n}x)" if n > 1 else "")
                                + (" [norm]" if used_fallback else "")
                                + f" | {desc}")
                statuses.append("ok")

            # Only touch the file when at least one patch actually changed it —
            # avoids rewriting (and re-EOL-normalizing) an unchanged file and
            # logging a phantom ledger entry when every patch missed.
            if any_applied:
                try:
                    _write_preserving_newlines(path, content, _newline)
                except Exception as e:
                    return finalize("c3_edit", {"file": file_path},
                                    f"Write error: {e}", "write error")

        # Log batch to ledger as one entry (store each patch's old/new for diff view)
        if any_applied:
            batch_detail = {"patches": [
                {
                    "old_string": p.get("old_string", "")[:_DETAIL_CAP],
                    "new_string": p.get("new_string", "")[:_DETAIL_CAP],
                    **({"summary": p["summary"]} if p.get("summary") else {}),
                }
                for p in edit_list if p.get("old_string") is not None
            ]}
            _log_to_ledger(rel, summary or f"Batch edit: {len(edit_list)} patches", tag_list, svc, detail=batch_detail)

        # Classify from the structured status list, not by substring-scanning
        # the human-readable result lines — a patch *summary* containing words
        # like 'NOT FOUND' used to be miscounted as a failure.
        applied = statuses.count("ok")
        norm_tag = " [unicode-normalized]" if any_normalized else ""
        short = f"✓ {rel} — {applied}/{len(edit_list)} patches applied{norm_tag}"
        if applied < len(edit_list):
            failed = [r for r, s in zip(results, statuses) if s != "ok"]
            short += "\n" + "\n".join(failed)
            if first_miss:
                short += first_miss
        return finalize("c3_edit", {"file": file_path}, short,
                        f"{rel} patched ({len(edit_list)} patches)")

    # ── Single-edit mode ──────────────────────────────────────────────────────
    if old_string is None:
        return finalize("c3_edit", {"file": file_path}, "old_string is required", "missing param")

    with file_lock:
        try:
            content, _newline = _read_preserving_newlines(path)
        except Exception as e:
            return finalize("c3_edit", {"file": file_path},
                            f"Read error: {e}", "read error")

        new_content, count, used_fallback = _apply_replacement(
            content, old_string, new_string, replace_all)

        if new_content is None and count == 0:
            hint = ""
            if _norm(old_string) != old_string or _norm(content) != content:
                hint = "\n  hint: unicode-lookalike normalization also failed to match."
            hint += _not_found_payload(_closest_region(content, old_string), file_path)
            return finalize("c3_edit", {"file": file_path},
                            f"old_string not found in {file_path}\n"
                            f"  searched for: {old_string[:120]!r}{hint}",
                            "not found")
        if new_content is None:
            hint = " (after unicode-lookalike normalization)" if used_fallback else ""
            return finalize("c3_edit", {"file": file_path},
                            f"old_string matches {count} locations{hint} — add more context to make it unique, "
                            f"or pass replace_all=true to replace all occurrences.",
                            "ambiguous")

        occurrences = count if replace_all else 1

        try:
            _write_preserving_newlines(path, new_content, _newline)
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
    if used_fallback:
        single_detail["unicode_normalized"] = True
    _log_to_ledger(rel, auto_summary, tag_list, svc, detail=single_detail)

    n_old = old_string.count("\n") + 1
    n_new = new_string.count("\n") + 1
    delta = f"-{n_old}+{n_new}L"
    occ = f" ({occurrences}x)" if occurrences > 1 else ""
    norm_tag = " [unicode-normalized]" if used_fallback else ""
    short = f"✓ {rel} [{delta}]{occ}{norm_tag}" + (f" — {summary}" if summary else "")
    return finalize("c3_edit", {"file": file_path}, short, f"{rel} patched")


_DETAIL_CAP = 2000  # chars stored per old/new string in the ledger


def _log_to_ledger(rel: str, summary: str, tag_list, svc, detail: dict = None) -> None:
    """Log an edit to the ledger, activity log, and session manager. Never raises."""
    if not svc.edit_ledger:
        return
    try:
        entry = svc.edit_ledger.log_edit(
            file=rel,
            change_type="modified",
            summary=summary,
            tags=tag_list,
            detail=detail,
        )
        if svc.activity_log:
            svc.activity_log.log("file_change", {
                "file": rel,
                "change_type": "modified",
                "summary": summary,
                "edit_id": entry.get("id", ""),
            })
        if svc.session_mgr and hasattr(svc.session_mgr, "log_file_change"):
            svc.session_mgr.log_file_change(rel, "modified")
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
