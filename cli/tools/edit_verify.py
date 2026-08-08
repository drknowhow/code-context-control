"""`c3_edits(action='verify')` — did that edit land?

Written for #74. A `c3_edit` call can fail *after* doing its work: one call in a
logged session hung until the harness aborted it at the 1800s MCP idle timeout,
and the write had fully landed; another dropped the MCP connection and had
written nothing. Both reached the caller as an error, and nothing distinguished
them.

That ambiguity is the defect, separately from whatever causes the stall. The
retry that is safe after the second incident is a double-apply after the first,
and the case where that actually corrupts a file is not exotic — any edit whose
``new_string`` contains its ``old_string`` (appending a line, wrapping a call)
matches again on a retry and applies twice. `c3_edit`'s own not-found error is
what saves the ordinary case, which is luck rather than a property.

**The file is the primary evidence; the ledger corroborates.** In that order,
because they answer different questions: the file says whether the intended text
is there now, the ledger says whether *this* edit is what put it there. Neither
alone is enough —

- text present, no ledger entry → it may have been there all along;
- ledger entry, text absent → something wrote and something else overwrote it.

So there are three verdicts, and the third one is the honest half:

- ``NOT_APPLIED`` — ``new_string`` is absent from the file. Definitive: had the
  edit landed, its own replacement text would be there. Safe to retry.
- ``APPLIED`` — ``new_string`` is present *and* a ledger entry for this file
  records this exact old/new pair. Definitive: do not retry.
- ``INCONCLUSIVE`` — everything else, with the specific check that failed named
  in the output. Never collapsed into either of the above; a verifier that
  guesses is worth less than one that says it cannot tell, because the caller
  can always fall back to reading the file.

Batch (`edits=`) verification reports per patch, since `c3_edit`'s batch mode
writes the file once after applying every hunk in memory — so "half written" is
not a state it can leave behind, but "patch 2 missed while 1 and 3 applied" very
much is, and that is what the caller needs itemized.
"""
from __future__ import annotations

import json
from pathlib import Path

from cli.tools import _grants
from services import access_guard

VERDICT_APPLIED = "APPLIED"
VERDICT_NOT_APPLIED = "NOT_APPLIED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

#: How far back to look for a corroborating ledger entry. Generous on purpose:
#: the failure this exists for is a call that hung for half an hour, so a window
#: measured in a handful of entries would miss the case it was built for.
_DEFAULT_LOOKBACK = 200

#: The ledger caps stored old/new strings (``_DETAIL_CAP`` in cli/tools/edit.py).
#: A long ``new_string`` is therefore recorded truncated, and comparing it whole
#: would report INCONCLUSIVE for every large edit — which is to say, for exactly
#: the edits most likely to have timed out. Compare on the stored prefix.
_DETAIL_CAP = 2000


def _rel(file_path: str, project_path: str) -> str:
    """The ledger's spelling of ``file_path``.

    The caller should be able to paste the same ``file_path`` they gave
    ``c3_edit`` — which is frequently absolute, and which the ledger stores
    project-relative with forward slashes. Making them convert by hand is how a
    verification tool goes unused at the moment it is needed.
    """
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(project_path) / p
    try:
        p = p.resolve()
    except OSError:
        pass
    try:
        return str(p.relative_to(Path(project_path).resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(file_path).replace("\\", "/")


def _read(path: Path):
    """File text with EOLs normalized the way ``c3_edit`` normalizes them.

    Without this, an edit to a CRLF file verifies as NOT_APPLIED: c3_edit
    matches against LF-normalized content, so a multi-line ``new_string`` the
    caller passed never appears verbatim in the bytes on disk.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="surrogateescape")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _pairs_from_ledger(entry: dict) -> list:
    """Every (old, new) pair an entry records — single edits and batches alike."""
    detail = entry.get("detail") or {}
    if not isinstance(detail, dict):
        return []
    patches = detail.get("patches")
    if isinstance(patches, list):
        return [
            (str(p.get("old_string", "")), str(p.get("new_string", "")))
            for p in patches if isinstance(p, dict)
        ]
    if "old_string" in detail or "new_string" in detail:
        return [(str(detail.get("old_string", "")),
                 str(detail.get("new_string", "")))]
    return []


def _ledger_match(ledger, rel: str, old: str, new: str, limit: int):
    """The most recent ledger entry recording this exact old/new pair, or None.

    Compared against the ledger's truncated prefixes, not the full strings — see
    ``_DETAIL_CAP``. Truncation means a *prefix* match is the strongest claim
    available here, so two edits sharing a 2000-character prefix would be
    indistinguishable. That is a real limit, and it is why the file check leads
    and this one only corroborates.
    """
    if ledger is None:
        return None
    try:
        entries = ledger.get_history(file=rel, limit=limit)
    except Exception:
        return None
    want = (old[:_DETAIL_CAP], new[:_DETAIL_CAP])
    for entry in reversed(entries or []):
        for pair in _pairs_from_ledger(entry):
            if pair == want:
                return entry
    return None


def _verify_one(content, old: str, new: str, ledger, rel: str,
                limit: int) -> dict:
    """Verdict for one (old, new) pair. Pure apart from the ledger read."""
    if content is None:
        return {
            "verdict": VERDICT_NOT_APPLIED,
            "why": "the file does not exist or could not be read, so nothing "
                   "was written to it",
            "entry": None,
        }
    if not new:
        # A deletion: new_string is empty. "Is the replacement present" has no
        # answer, so the ledger is the only positive evidence available.
        entry = _ledger_match(ledger, rel, old, new, limit)
        if old and old in content:
            return {"verdict": VERDICT_NOT_APPLIED,
                    "why": "the text this edit removes is still present",
                    "entry": None}
        if entry is not None:
            return {"verdict": VERDICT_APPLIED,
                    "why": f"the removed text is gone and ledger entry "
                           f"{entry.get('id', '')} records this deletion",
                    "entry": entry}
        return {"verdict": VERDICT_INCONCLUSIVE,
                "why": "the text is absent but no ledger entry records this "
                       "deletion — it may never have been there",
                "entry": None}

    if new not in content:
        return {
            "verdict": VERDICT_NOT_APPLIED,
            "why": "new_string is not in the file; had this edit landed, its "
                   "own replacement text would be there",
            "entry": None,
        }

    entry = _ledger_match(ledger, rel, old, new, limit)
    if entry is not None:
        return {
            "verdict": VERDICT_APPLIED,
            "why": f"new_string is present and ledger entry "
                   f"{entry.get('id', '')} ({entry.get('timestamp', '')[:19]}) "
                   f"records this exact old/new pair",
            "entry": entry,
        }
    return {
        "verdict": VERDICT_INCONCLUSIVE,
        "why": "new_string is present but no ledger entry records this edit — "
               "the text may have been there already, or been written by "
               "something other than c3_edit. Read the file before retrying",
        "entry": None,
    }


def verify(file_path: str, old_string: str, new_string: str, edits: str,
           svc, limit: int = _DEFAULT_LOOKBACK):
    """Verify one edit or a batch. Returns (body, summary) for ``finalize``.

    Takes the same arguments the original ``c3_edit`` call took, so recovering
    from an aborted edit means re-sending it to a different action rather than
    reconstructing anything.
    """
    if not file_path:
        return "file is required", "missing file"

    project_path = str(getattr(svc, "project_path", "") or "")
    rel = _rel(file_path, project_path)
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(project_path) / path

    # Access Guard, read verdict, before a single byte is read. Caught by
    # tests/test_access_guard_meta.py rather than by review — and it was right
    # to catch it. This tool returns a verdict rather than content, which is
    # exactly why it is worth guarding: "does .env contain X" answered one
    # substring at a time is a content read with extra steps.
    denial = access_guard.check(str(path), "read", project_path)
    if denial and not _grants.allow(svc, denial, tool="c3_edits", op="read",
                                    path=str(path)):
        return (access_guard.refusal(denial, file_path, "read"),
                "access-denied")

    content = _read(path)
    ledger = getattr(svc, "edit_ledger", None)

    if edits:
        try:
            edit_list = json.loads(edits) if isinstance(edits, str) else edits
        except json.JSONDecodeError as exc:
            return f"edits must be a valid JSON list: {exc}", "bad edits param"
        if not isinstance(edit_list, list) or not edit_list:
            return "edits must be a non-empty JSON list", "bad edits param"

        lines = [f"[edits:verify] {rel} — {len(edit_list)} patch(es)"]
        verdicts = []
        for i, patch in enumerate(edit_list):
            if not isinstance(patch, dict):
                lines.append(f"  patch[{i}]: INCONCLUSIVE — not an object")
                verdicts.append(VERDICT_INCONCLUSIVE)
                continue
            r = _verify_one(content, str(patch.get("old_string", "")),
                            str(patch.get("new_string", "")), ledger, rel, limit)
            verdicts.append(r["verdict"])
            lines.append(f"  patch[{i}]: {r['verdict']} — {r['why']}")
        # A batch writes once, so a mixed result means individual hunks did not
        # match — not that the file was left half-written. Say so, rather than
        # letting the caller infer a torn write that cannot happen.
        lines.append("  note: c3_edit's batch mode writes the file once after "
                     "applying every hunk in memory, so a mixed result means "
                     "some hunks did not match — not a partial write.")
        counts = {v: verdicts.count(v) for v in sorted(set(verdicts))}
        summary = ", ".join(f"{n} {v.lower()}" for v, n in counts.items())
        return "\n".join(lines), f"{rel}: {summary}"

    if not old_string and not new_string:
        return ("old_string and/or new_string are required — verify answers "
                "'did THIS edit land', which needs the edit"), "missing params"

    r = _verify_one(content, old_string, new_string, ledger, rel, limit)
    lines = [f"[edits:verify] {rel} — {r['verdict']}", f"  {r['why']}"]
    if r["verdict"] == VERDICT_NOT_APPLIED:
        lines.append("  → safe to re-send the original c3_edit call.")
    elif r["verdict"] == VERDICT_APPLIED:
        lines.append("  → do NOT retry; re-sending would either fail to match "
                     "or apply a second time.")
    else:
        lines.append("  → read the file before deciding; do not retry blind.")
    return "\n".join(lines), f"{rel}: {r['verdict']}"
