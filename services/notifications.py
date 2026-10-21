"""NotificationStore — Thread-safe notification queue for background agents.

Persists to .c3/notifications.jsonl. Supports dedup, severity filtering,
and auto-acknowledgement when surfaced to Claude via tool responses.

Several processes write one project's file at once: every MCP server (the
auto-ack on each tool response), hook subprocesses (session open and end, the
override request filed at a confirm denial), the Oracle (the phone's and the
desk's acks) and the job supervisor. So every method that can rewrite the
file holds a cross-process lock, and a rewrite is published by atomic
replace. Before 2.158.2 neither was true: a writer that read the file while
another was rewriting it read a truncated copy and wrote that back, and
whatever had been appended in between was gone, including approval requests
the desk and the phone were waiting for.
"""
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.atomic_json import write_text_atomic

# How long to suppress a repeated agent+title after it has been acknowledged.
_COOLDOWN_MINUTES = {"critical": 5, "warning": 30, "info": 60}

# Duplicate collapse: keep the most severe label when merging records.
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# How long a writer waits for another process to finish its rewrite. A
# rewrite takes milliseconds; the bound is for a holder that is alive but
# stuck. Past it the write goes ahead unlocked, as before 2.158.2, rather than
# being dropped: a notification nobody receives is what the lock prevents.
_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.01

# Event kinds written by C3's own producers (v2.126.0). A client ROUTES on
# ``kind`` + ``ref_id`` rather than parsing the title; every kind here also
# lands in .c3/notifications.jsonl, which is one of the files the mobile
# gateway's long-poll watches — so writing one of these is what wakes a
# waiting desktop or phone client.
#   shell_job  ref_id = job id        (services/shell_jobs.py, the supervisor)
#   ci         ref_id = run id        (services/ci_runner.py)
#   session    ref_id = host session  (cli/hook_session_open.py, hook_session_end.py)
#   mcp        ref_id = C3 session id (cli/mcp_server.py, runtime ready)
#   override   ref_id = request id    (services/override_requests.py)
#   schedule   ref_id = run id, or approval id for a pending approval
#                                     (oracle/services/scheduler.py)
EVENT_KINDS = ("shell_job", "ci", "session", "mcp", "override", "schedule")


def notify(project_path, agent: str, severity: str, title: str, message: str,
           *, kind: str = "", ref_id: str = "",
           replace_if_unacked: bool = False) -> dict | None:
    """Best-effort ``NotificationStore.add`` that NEVER raises.

    For producers that run in odd processes — the detached job supervisor,
    a short-lived hook subprocess, a background thread — where a
    notification failure must not fail the thing it reports on (the same
    shape ``services.override_requests._notify`` uses). Returns the entry,
    or None when deduped or when the store could not be written.
    """
    try:
        return NotificationStore(str(project_path)).add(
            agent=agent, severity=severity, title=title, message=message,
            kind=kind, ref_id=ref_id, replace_if_unacked=replace_if_unacked)
    except Exception:
        return None


def _recency_key(entry: dict) -> str:
    """Sort key: last occurrence of a (possibly collapsed) notification."""
    return entry.get("last_seen") or entry.get("timestamp", "")


def _entry_count(entry: dict) -> int:
    """Occurrence count of a (possibly collapsed) record; legacy rows count as 1."""
    try:
        return max(1, int(entry.get("count", 1) or 1))
    except (TypeError, ValueError):
        return 1


class _FileLock:
    """Advisory cross-process lock on a sidecar file, released by the OS if
    the holder dies. The shape of ``services.task_store._FileLock``, except
    that ``acquire`` gives up quietly: it returns False on timeout instead of
    raising, and the caller carries on without the lock."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self, timeout_s: float = _LOCK_TIMEOUT_S) -> bool:
        try:
            fh = open(self.path, "ab")  # never read/written; fd exists to hold the lock
        except OSError:
            return False
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(_LOCK_POLL_S)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


class NotificationStore:
    """Thread-safe JSONL notification store for background agents."""

    def __init__(self, project_path: str):
        self._file = Path(project_path) / ".c3" / "notifications.jsonl"
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._file.with_name(self._file.name + ".lock")
        self._lock = threading.Lock()
        # Retro-cleanup runs at most once per store instance, lazily on first
        # access, so pre-existing duplicate backlogs self-heal without a
        # dedicated maintenance pass.
        self._collapsed = False

    @contextmanager
    def _locked(self):
        """This instance's thread lock plus the cross-process file lock. Every
        method that can rewrite the file runs under both; instances are made
        per call all over C3, so the thread lock alone never covered two
        threads, let alone two processes."""
        with self._lock:
            file_lock = _FileLock(self._lock_file)
            file_lock.acquire()
            try:
                yield
            finally:
                file_lock.release()

    def add(self, agent: str, severity: str, title: str, message: str,
            ai_enhanced: bool = False, replace_if_unacked: bool = False,
            kind: str = "", ref_id: str = "") -> dict | None:
        """Append a notification. Dedup: an identical (agent, title, message)
        that is still unacknowledged collapses into the existing record — its
        ``count`` is bumped and ``last_seen`` refreshed instead of appending a
        duplicate line.

        severity: 'info', 'warning', 'critical'
        replace_if_unacked: if True and an unacked notification with the same agent+title
            already exists, update its message/severity in-place (bumping
            count/last_seen) instead of appending a new entry. Use for
            high-frequency agents (budget, index, key-file drift) to prevent pile-up.
        kind: machine-readable class for clients that ROUTE on a tap rather
            than parse the title — 'override' for an approval request. Omitted
            from the record when empty, so every existing producer and every
            stored line is unchanged.
        ref_id: the id of the thing the notification is ABOUT (e.g. the
            override request), which is not the notification's own ``id``.
            A client needs both: ``id`` to acknowledge, ``ref_id`` to navigate.
            It is also part of the dedup identity: an entry with a ref_id
            only collapses into, or is suppressed by, an entry with the same
            ref_id (unless ``replace_if_unacked``).
        Returns the entry if written/updated, None if deduped.
        """
        with self._locked():
            self._maybe_collapse_locked()
            message_hash = hashlib.md5((message or "").encode("utf-8")).hexdigest()[:12]
            cooldown = timedelta(minutes=_COOLDOWN_MINUTES.get(severity, 30))
            now = datetime.now(timezone.utc)
            entries = self._read_all()
            for existing in entries:
                if existing.get("agent") != agent or existing.get("title") != title:
                    continue
                # A notification that names what it is about is that thing's
                # own event: a second approval request on the same file is a
                # new request, not a repeat of the first. Matched on agent and
                # title alone, an acknowledged request's cooldown swallowed
                # the next request's notification, and the desk and the phone,
                # which learn of requests from this file, never showed it.
                # `replace_if_unacked` keeps title scope: that caller asked for
                # one record per title.
                if (ref_id and not replace_if_unacked
                        and (existing.get("ref_id") or "") != ref_id):
                    continue
                if not existing.get("acknowledged"):
                    if replace_if_unacked:
                        # Update in-place — prevents repeated pile-up for chatty agents
                        existing["message"] = message
                        existing["message_hash"] = message_hash
                        existing["severity"] = severity
                        existing["timestamp"] = now.isoformat()
                        existing["last_seen"] = now.isoformat()
                        existing["count"] = _entry_count(existing) + 1
                        existing["ai_enhanced"] = ai_enhanced
                        # Refresh the routing fields too: an in-place update
                        # keeps the record but the thing it points AT may have
                        # changed (a new request id under the same title).
                        if kind:
                            existing["kind"] = kind
                        if ref_id:
                            existing["ref_id"] = ref_id
                        self._write_all(entries)
                        return existing
                    # Same notification still pending — collapse into the
                    # existing record instead of appending a duplicate.
                    if existing.get("message_hash") == message_hash:
                        existing["count"] = _entry_count(existing) + 1
                        existing["last_seen"] = now.isoformat()
                        self._write_all(entries)
                        return None
                else:
                    # Already acknowledged — suppress if within cooldown window
                    try:
                        acked_at = datetime.fromisoformat(existing["timestamp"])
                        if acked_at.tzinfo is None:
                            acked_at = acked_at.replace(tzinfo=timezone.utc)
                        if now - acked_at < cooldown:
                            return None
                    except (KeyError, ValueError):
                        pass

            entry = {
                "id": uuid.uuid4().hex[:12],
                "agent": agent,
                "severity": severity,
                "title": title,
                "message": message,
                "message_hash": message_hash,
                "timestamp": now.isoformat(),
                "last_seen": now.isoformat(),
                "count": 1,
                "acknowledged": False,
                "ai_enhanced": ai_enhanced,
            }
            # Additive only: absent keys rather than empty ones, so a client
            # can test presence and old lines stay byte-identical in shape.
            if kind:
                entry["kind"] = kind
            if ref_id:
                entry["ref_id"] = ref_id
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            return entry

    def get_pending_count(self) -> int:
        """Return count of unacknowledged warning/critical notifications without consuming them."""
        with self._locked():
            self._maybe_collapse_locked()
            return sum(
                1 for e in self._read_all()
                if not e.get("acknowledged")
                and e.get("severity") in ("warning", "critical")
            )

    # Default filter: only notifications a human should act on.
    # Info-level entries are archival; they show up in get_history(), not here.
    _ACTIONABLE = ("warning", "critical")

    def get_unacknowledged(self, limit: int = 5, severities=None) -> list:
        """Return unacknowledged notifications, newest first.

        severities: tuple of severities to include. Defaults to actionable
            ('warning', 'critical') so auto-chatter 'info' events don't
            drown real signals. Pass severities=() or ('info','warning','critical')
            to include info events (e.g. for an activity log view).
        """
        if severities is None:
            severities = self._ACTIONABLE
        with self._locked():
            self._maybe_collapse_locked()
            entries = [
                e for e in self._read_all()
                if not e.get("acknowledged")
                and (not severities or e.get("severity") in severities)
            ]
            entries.sort(key=_recency_key, reverse=True)
            return entries[:limit]

    def get_suppressed_info_count(self) -> int:
        """Count of unacknowledged 'info' notifications not shown in actionable view."""
        with self._locked():
            self._maybe_collapse_locked()
            return sum(
                1 for e in self._read_all()
                if not e.get("acknowledged") and e.get("severity") == "info"
            )

    def get_history(self, limit: int = 50) -> list:
        """Return all notifications (including acknowledged) for the activity console, newest first."""
        with self._lock:
            entries = self._read_all()
            entries.sort(key=_recency_key, reverse=True)
            return entries[:limit]

    def acknowledge(self, notification_id: str) -> bool:
        """Mark a single notification as acknowledged."""
        with self._locked():
            return self._set_ack(lambda e: e.get("id") == notification_id)

    def acknowledge_all(self) -> int:
        """Mark all unacknowledged notifications as acknowledged. Returns count."""
        with self._locked():
            entries = self._read_all()
            count = 0
            for e in entries:
                if not e.get("acknowledged"):
                    e["acknowledged"] = True
                    count += 1
            if count:
                self._write_all(entries)
            return count

    def get_pending_summary(self) -> str:
        """Format up to 3 unacked warning/critical notifications for prepending.

        Auto-acknowledges those included. Returns empty string if none.
        """
        with self._locked():
            self._maybe_collapse_locked()
            entries = self._read_all()
            pending = [
                e for e in entries
                if not e.get("acknowledged")
                and e.get("severity") in ("warning", "critical")
            ]
            # Newest first
            pending.sort(key=_recency_key, reverse=True)
            pending = pending[:3]

            if not pending:
                return ""

            # Auto-acknowledge
            pending_ids = {e["id"] for e in pending}
            for e in entries:
                if e.get("id") in pending_ids:
                    e["acknowledged"] = True
            self._write_all(entries)

            # Format
            lines = ["[c3:agents]"]
            for e in pending:
                prefix = "!!" if e["severity"] == "critical" else "!"
                count = _entry_count(e)
                repeat = f" (x{count})" if count > 1 else ""
                lines.append(f"{prefix} {e['agent']}: {e['title']} — {e['message']}{repeat}")
            return "\n".join(lines)

    def rotate_acknowledged(self, archive_fn, min_age_minutes: int = 120) -> int:
        """Move old acknowledged entries out of the live file (storage retention).

        ``archive_fn(entries) -> bool`` receives the entries to move and must
        persist them (e.g. services.retention.write_archive_entries into a
        gzip archive), returning truthy on success. The live file is only
        rewritten AFTER archiving succeeds, so records are never dropped.

        Unacknowledged entries always stay. Acknowledged entries younger
        than ``min_age_minutes`` also stay, so add()'s post-ack cooldown
        suppression window keeps working. Returns the count moved.
        """
        with self._locked():
            entries = self._read_all()
            horizon = datetime.now(timezone.utc) - timedelta(
                minutes=max(0, min_age_minutes))

            def _old_enough(e: dict) -> bool:
                try:
                    dt = datetime.fromisoformat(_recency_key(e))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt < horizon
                except (ValueError, TypeError):
                    return True  # unparseable timestamp — treat as old

            move = [e for e in entries
                    if e.get("acknowledged") and _old_enough(e)]
            if not move:
                return 0
            try:
                if not archive_fn(move):
                    return 0
            except Exception:
                return 0
            move_ids = {id(e) for e in move}
            self._write_all([e for e in entries if id(e) not in move_ids])
            return len(move)

    def collapse_duplicates(self) -> int:
        """Retro-cleanup: merge unacknowledged duplicates into one record each.

        Unacknowledged entries sharing (agent, title, ref_id) collapse into
        the most recent record, which absorbs the group's total ``count``, the
        max ``last_seen``, and the highest severity. The newest message wins —
        older same-title messages are stale snapshots of the same condition.
        Entries about different things (different ``ref_id``) never merge.
        Acknowledged history is never touched. Returns the number of duplicate
        records removed.
        """
        with self._locked():
            self._collapsed = True
            return self._collapse_locked()

    def _maybe_collapse_locked(self):
        """Run the duplicate collapse once per store instance. Caller must hold _lock."""
        if self._collapsed:
            return
        self._collapsed = True
        try:
            self._collapse_locked()
        except Exception:
            pass  # maintenance is best-effort — never break reads/writes

    def _collapse_locked(self) -> int:
        """Merge unacked duplicates (same agent+title+ref_id). Caller must hold _locked()."""
        entries = self._read_all()
        groups: dict[tuple, list] = {}
        for e in entries:
            if e.get("acknowledged"):
                continue
            groups.setdefault((e.get("agent"), e.get("title"),
                               e.get("ref_id") or ""), []).append(e)

        drop_ids: set[int] = set()
        removed = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            survivor = max(group, key=_recency_key)
            survivor["count"] = sum(_entry_count(e) for e in group)
            survivor["last_seen"] = max(_recency_key(e) for e in group)
            survivor["severity"] = max(
                (e.get("severity", "info") for e in group),
                key=lambda s: _SEVERITY_RANK.get(s, 0),
            )
            for e in group:
                if e is not survivor:
                    drop_ids.add(id(e))
                    removed += 1
        if removed:
            self._write_all([e for e in entries if id(e) not in drop_ids])
        return removed

    def _read_all(self) -> list:
        """Read all entries from JSONL file. Caller must hold _lock."""
        if not self._file.exists():
            return []
        entries = []
        for line in self._file.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _write_all(self, entries: list):
        """Rewrite entire file. Caller must hold _locked().

        Published by atomic replace, so a reader that takes no lock (the
        gateway feed, the hub) sees the old file or the new one, never the
        truncated middle of a rewrite. No fsync: this is a feed, and what
        readers need is the replace, not durability across a power cut.
        """
        write_text_atomic(self._file,
                          "".join(json.dumps(e) + "\n" for e in entries),
                          fsync=False)

    def _set_ack(self, predicate) -> bool:
        """Acknowledge entries matching predicate. Caller must hold _lock."""
        entries = self._read_all()
        found = False
        for e in entries:
            if not e.get("acknowledged") and predicate(e):
                e["acknowledged"] = True
                found = True
        if found:
            self._write_all(entries)
        return found
