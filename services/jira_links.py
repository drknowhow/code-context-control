"""Local issue-key detection — links C3 activity to Jira issues.

Pure local string work: scans the current git branch name and the edit
ledger for Jira issue keys (``PROJ-123``). No network, no Jira client, so
everything here works (and is testable) without a configured account.
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b")
_ISSUE_KEY_RE_CI = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)-(\d+)\b")

# Acronym-dash-number collisions that are never Jira project keys.
_NON_PROJECT_PREFIXES = {
    "UTF", "SHA", "ISO", "RFC", "CVE", "MD", "AES", "RSA",
    "GPT", "HTTP", "HTTPS", "TLS", "SSL", "IPV", "OAUTH",
}


def _collect(matches) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for prefix, number in matches:
        prefix = prefix.upper()
        if prefix in _NON_PROJECT_PREFIXES:
            continue
        key = f"{prefix}-{number}"
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def extract_issue_keys(text: str) -> list[str]:
    """Ordered, de-duplicated issue keys found in *text* (uppercase only —
    prose convention). Acronym false positives (UTF-8, SHA-256) filtered."""
    if not text:
        return []
    return _collect(ISSUE_KEY_RE.findall(text))


def extract_issue_keys_loose(text: str) -> list[str]:
    """Case-insensitive variant for branch names, where keys are often
    written lowercase (``feature/proj-123-fix``). Normalizes to uppercase."""
    if not text:
        return []
    return _collect(_ISSUE_KEY_RE_CI.findall(text))


def current_branch(project_path: str) -> str:
    """Branch name from .git/HEAD without spawning git (fast + hang-proof
    on Windows). Empty string on detached HEAD or missing repo."""
    head = Path(project_path) / ".git" / "HEAD"
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content.startswith("ref:"):
        return ""
    ref = content.split(":", 1)[1].strip()
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def branch_issue_keys(project_path: str) -> dict:
    """``{"branch": name, "keys": [PROJ-123, ...]}`` for the current branch."""
    branch = current_branch(project_path)
    return {"branch": branch, "keys": extract_issue_keys_loose(branch)}


def ledger_activity(project_path: str, *, key: str = "", limit: int = 25) -> list[dict]:
    """Recent edit-ledger entries that mention Jira issue keys, newest first.

    Scans file path + summary + tags of each entry; enrichment patches
    (entries carrying ``target_id``) are skipped. When *key* is given, only
    entries mentioning that key are returned.
    """
    ledger = Path(project_path) / ".c3" / "edit_ledger.jsonl"
    if not ledger.exists():
        return []
    # Tail-bounded read so a large ledger never costs full-file parsing.
    try:
        with open(ledger, encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=2000)
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("target_id"):
            continue
        text = " ".join([
            str(entry.get("file", "")),
            str(entry.get("summary", "")),
            " ".join(str(t) for t in entry.get("tags") or []),
        ])
        keys = extract_issue_keys(text)
        if not keys:
            continue
        if key and key.upper() not in keys:
            continue
        out.append({
            "ts": entry.get("timestamp") or entry.get("ts") or "",
            "file": entry.get("file", ""),
            "summary": entry.get("summary", ""),
            "change_type": entry.get("change_type", ""),
            "keys": keys,
        })
    out.reverse()  # ledger is append-only — newest last on disk
    return out[:max(1, limit)]
