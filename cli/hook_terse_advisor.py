"""Stop hook: detect verbose responses and nudge user to activate /terse.

Triggered by the Claude Code 'Stop' event (fires after each response turn).
Reads the transcript to measure the last assistant message's character count.
If verbose and /terse not already active, prints a one-time nudge per session.

State file: ~/.c3/terse_advisor.json
  {
    "dismissed": false,         -- permanent silence
    "remind_after": null,       -- ISO timestamp; snooze until then
    "last_nudge_session": null  -- session_id of last nudged session
  }

Silence forever : c3 terse dismiss
Snooze 24h      : c3 terse later
Reset state     : c3 terse reset
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import log_hook_error  # noqa: E402

_STATE_FILE = Path.home() / ".c3" / "terse_advisor.json"
_VERBOSE_THRESHOLD = 600   # chars of assistant text that counts as "verbose"
_SCAN_ENTRIES = 60          # how many recent transcript entries to scan


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"dismissed": False, "remind_after": None, "last_nudge_session": None}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), "utf-8")


def _extract_text(content) -> str:
    """Extract plain text from a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _read_tail(path: str, n: int) -> list:
    """Return the last *n* non-empty JSONL entries from *path*."""
    entries = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return entries[-n:]


def _last_assistant_char_count(entries: list) -> int:
    """Return char count of the last assistant message text, or 0."""
    for entry in reversed(entries):
        entry_type = entry.get("type", "")
        if entry_type in ("progress", "file-history-snapshot", "system"):
            continue
        role = entry.get("role", "")
        msg = entry.get("message", {})
        if isinstance(msg, dict):
            role = role or msg.get("role", "")
            content = msg.get("content", "")
        else:
            content = entry.get("content", "")

        if role == "assistant":
            return len(_extract_text(content))
    return 0


def _terse_active(entries: list) -> bool:
    """Return True if /terse was recently activated in this transcript."""
    for entry in reversed(entries):
        entry_type = entry.get("type", "")

        # Slash-command invocation entries
        if entry_type in ("command", "slash_command"):
            cmd = entry.get("command", "") or entry.get("command-name", "") or ""
            if "terse" in cmd.lower():
                return True

        role = entry.get("role", "")
        msg = entry.get("message", {})
        if isinstance(msg, dict):
            role = role or msg.get("role", "")
            content = msg.get("content", "")
        else:
            content = entry.get("content", "")

        if role == "user":
            text = _extract_text(content).lower()
            # Detect /terse invocation or skill expansion markers
            if "/terse" in text or "terse output mode" in text:
                return True

    return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        log_hook_error("hook_terse_advisor", exc)
        sys.exit(0)

    try:
        state = _load_state()

        # Permanent dismiss
        if state.get("dismissed"):
            sys.exit(0)

        # Snooze check
        remind_after = state.get("remind_after")
        if remind_after:
            try:
                if datetime.now(timezone.utc) < datetime.fromisoformat(remind_after):
                    sys.exit(0)
            except Exception:
                pass

        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        if not transcript_path:
            sys.exit(0)

        # Only nudge once per session
        if session_id and state.get("last_nudge_session") == session_id:
            sys.exit(0)

        entries = _read_tail(transcript_path, _SCAN_ENTRIES)
        if not entries:
            sys.exit(0)

        # Skip if terse already active
        if _terse_active(entries):
            sys.exit(0)

        # Check verbosity
        char_count = _last_assistant_char_count(entries)
        if char_count < _VERBOSE_THRESHOLD:
            sys.exit(0)

        # Nudge
        bar = "─" * 52
        print(f"\n{bar}")
        print(f"[C3] Verbose response (~{char_count} chars). /terse saves ~50% output tokens.")
        print("     Type /terse to activate.")
        print("     Silence: c3 terse dismiss  |  Snooze 24h: c3 terse later")
        print(bar)

        # Update state
        state["last_nudge_session"] = session_id
        _save_state(state)

    except Exception as exc:
        log_hook_error("hook_terse_advisor", exc)

    sys.exit(0)


if __name__ == "__main__":
    main()
