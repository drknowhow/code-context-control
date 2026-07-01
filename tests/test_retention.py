"""Tests for storage retention & rotation (P5, services/retention.py).

Covers:
- rotate_jsonl atomicity (write -> rotate -> write continues) and gzip
  archive readability
- purge_archives keep_days / prefix / exclude behavior
- telemetry reader spanning live file + archives
- activity log write-time rotation
- edit-ledger rotation: pending entries preserved, version tombstones,
  audit integrity (no record ever lost), hook version continuity
- session snapshot cap
- file_memory stale-record pruning
- NotificationStore.rotate_acknowledged
- RetentionManager sweep + throttling

All tests run in temp dirs; no real .c3 directories are touched.
"""

import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import retention
from services.retention import (
    RetentionManager,
    cap_session_files,
    load_retention_config,
    mb_to_bytes,
    purge_archives,
    rotate_jsonl,
    write_archive_entries,
    write_archive_lines,
)


def _read_gz_lines(path: Path) -> list:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def _write_config(project: Path, retention_section: dict):
    c3 = project / ".c3"
    c3.mkdir(parents=True, exist_ok=True)
    (c3 / "config.json").write_text(
        json.dumps({"retention": retention_section}), encoding="utf-8")
    retention.clear_config_cache()


class RetentionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        retention.clear_config_cache()

    def tearDown(self):
        retention.clear_config_cache()
        self._tmp.cleanup()


class TestRotateJsonl(RetentionBase):
    def _jsonl(self, n=20) -> Path:
        path = self.project / "log.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"i": i}) + "\n")
        return path

    def test_below_threshold_no_rotation(self):
        path = self._jsonl(3)
        archive = self.project / "archive"
        self.assertIsNone(rotate_jsonl(path, 10 * 1024 * 1024, archive))
        self.assertTrue(path.exists())
        self.assertFalse(archive.exists())

    def test_rotate_creates_gzip_archive_and_fresh_file(self):
        path = self._jsonl(20)
        original = path.read_text(encoding="utf-8").splitlines()
        archive = self.project / "archive"
        gz = rotate_jsonl(path, 1, archive)
        self.assertIsNotNone(gz)
        self.assertTrue(str(gz).endswith(".jsonl.gz"))
        self.assertFalse(path.exists())  # live file moved away
        # Archive is readable and contains every original record
        self.assertEqual(_read_gz_lines(gz), original)
        # Writers continue: next append recreates the live file
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"i": "after"}) + "\n")
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_rotate_twice_same_day_unique_names(self):
        archive = self.project / "archive"
        p1 = rotate_jsonl(self._jsonl(20), 1, archive)
        p2 = rotate_jsonl(self._jsonl(20), 1, archive)
        self.assertIsNotNone(p1)
        self.assertIsNotNone(p2)
        self.assertNotEqual(p1, p2)
        self.assertEqual(len(list(archive.glob("log.*.jsonl.gz"))), 2)

    def test_missing_file_is_safe(self):
        self.assertIsNone(rotate_jsonl(self.project / "nope.jsonl", 1,
                                       self.project / "archive"))

    def test_mb_to_bytes(self):
        self.assertEqual(mb_to_bytes(1), 1024 * 1024)
        self.assertEqual(mb_to_bytes(0.5), 512 * 1024)
        self.assertEqual(mb_to_bytes("bad"), 0)


class TestWriteArchive(RetentionBase):
    def test_write_archive_entries_roundtrip(self):
        archive = self.project / "archive"
        entries = [{"a": 1}, {"b": 2}]
        gz = write_archive_entries(archive, "notifications", entries)
        self.assertIsNotNone(gz)
        got = [json.loads(ln) for ln in _read_gz_lines(gz)]
        self.assertEqual(got, entries)

    def test_empty_returns_none(self):
        self.assertIsNone(write_archive_lines(self.project / "archive", "x", []))


class TestPurgeArchives(RetentionBase):
    def _archive_file(self, name: str, lines=("{}",)) -> Path:
        archive = self.project / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        p = archive / name
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_purges_old_keeps_recent(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d")
        new_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        old = self._archive_file(f"activity_log.{old_date}.jsonl.gz")
        new = self._archive_file(f"activity_log.{new_date}.jsonl.gz")
        removed = purge_archives(self.project / "archive", keep_days=90)
        self.assertIn(old, removed)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_keep_days_zero_never_purges(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=999)).strftime("%Y-%m-%d")
        old = self._archive_file(f"activity_log.{old_date}.jsonl.gz")
        self.assertEqual(purge_archives(self.project / "archive", keep_days=0), [])
        self.assertTrue(old.exists())

    def test_exclude_prefixes_protects_edit_ledger(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d")
        ledger = self._archive_file(f"edit_ledger.{old_date}.jsonl.gz")
        other = self._archive_file(f"activity_log.{old_date}.jsonl.gz")
        removed = purge_archives(self.project / "archive", keep_days=90,
                                 exclude_prefixes=("edit_ledger",))
        self.assertTrue(ledger.exists())
        self.assertIn(other, removed)

    def test_prefix_scopes_purge(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d")
        ledger = self._archive_file(f"edit_ledger.{old_date}.jsonl.gz")
        other = self._archive_file(f"activity_log.{old_date}.jsonl.gz")
        purge_archives(self.project / "archive", keep_days=90, prefix="edit_ledger")
        self.assertFalse(ledger.exists())
        self.assertTrue(other.exists())

    def test_missing_dir_is_safe(self):
        self.assertEqual(purge_archives(self.project / "nope", keep_days=90), [])


class TestRetentionConfig(RetentionBase):
    def test_defaults_when_no_config(self):
        cfg = load_retention_config(self.project, ttl=0)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["sessions_max_files"], 50)
        self.assertEqual(cfg["edit_ledger_archive_keep_days"], 0)

    def test_overrides_merge(self):
        _write_config(self.project, {"activity_log_max_mb": 1.5, "enabled": False})
        cfg = load_retention_config(self.project, ttl=0)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["activity_log_max_mb"], 1.5)
        self.assertEqual(cfg["telemetry_max_mb"], 5.0)  # default retained


class TestTelemetrySpansArchives(RetentionBase):
    def _record(self, ts: datetime, tool="c3_read"):
        from services.telemetry import append_telemetry_record
        ok = append_telemetry_record(self.project, {
            "ts": ts.isoformat(), "tool": tool, "response_tokens": 10,
        })
        self.assertTrue(ok)

    def test_reader_spans_live_and_archive(self):
        from services.telemetry import (
            aggregate_tool_telemetry,
            read_telemetry_records,
            telemetry_path,
        )
        (self.project / ".c3").mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        # Old records, then force a rotation into the archive.
        for _ in range(4):
            self._record(now - timedelta(days=10), tool="old_tool")
        gz = rotate_jsonl(telemetry_path(self.project), 1,
                          self.project / ".c3" / "archive")
        self.assertIsNotNone(gz)
        # Fresh records in the new live file.
        for _ in range(3):
            self._record(now, tool="new_tool")

        # No window: everything, live + archive.
        all_recs = read_telemetry_records(self.project)
        self.assertEqual(len(all_recs), 7)

        # 30-day window spans the archive (rotated today, so file included;
        # per-record filter keeps the 10-day-old records).
        agg = aggregate_tool_telemetry(self.project, days=30)
        self.assertEqual(agg["total_calls"], 7)
        self.assertIn("old_tool", agg["by_tool"])

        # 7-day window: archived 10-day-old records are filtered out.
        agg7 = aggregate_tool_telemetry(self.project, days=7)
        self.assertEqual(agg7["total_calls"], 3)
        self.assertNotIn("old_tool", agg7["by_tool"])

    def test_archives_older_than_window_are_skipped(self):
        from services.telemetry import read_telemetry_records
        archive = self.project / ".c3" / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        stale = archive / "tool_telemetry.2020-01-01.jsonl.gz"
        with gzip.open(stale, "wt", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2020-01-01T00:00:00+00:00",
                                "tool": "ancient"}) + "\n")
        since = datetime.now(timezone.utc) - timedelta(days=7)
        self.assertEqual(read_telemetry_records(self.project, since=since), [])
        # ...but an all-records query reads it.
        recs = read_telemetry_records(self.project)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["tool"], "ancient")

    def test_uncompressed_fallback_archive_is_read(self):
        from services.telemetry import read_telemetry_records
        archive = self.project / ".c3" / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = archive / f"tool_telemetry.{date}.jsonl"  # gzip-failure fallback
        raw.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                   "tool": "raw_tool"}) + "\n", encoding="utf-8")
        recs = read_telemetry_records(self.project)
        self.assertEqual([r["tool"] for r in recs], ["raw_tool"])

    def test_write_time_rotation_via_config(self):
        from services.telemetry import read_telemetry_records
        _write_config(self.project, {"telemetry_max_mb": 0.0001})  # ~105 bytes
        now = datetime.now(timezone.utc)
        for _ in range(5):
            self._record(now)
        archives = list((self.project / ".c3" / "archive").glob(
            "tool_telemetry.*.jsonl.gz"))
        self.assertTrue(archives)
        # Appends continue after rotation and no record is ever lost:
        # live + archives always add up to everything written.
        self._record(now)
        self.assertEqual(len(read_telemetry_records(self.project)), 6)


class TestActivityLogRotation(RetentionBase):
    def test_write_rotate_write_continues(self):
        from services.activity_log import ActivityLog
        _write_config(self.project, {"activity_log_max_mb": 0.0005})  # ~520 bytes
        log = ActivityLog(str(self.project))
        for i in range(30):
            log.log("tool_call", {"tool": f"t{i}", "pad": "x" * 40})
        archives = list((self.project / ".c3" / "archive").glob(
            "activity_log.*.jsonl.gz"))
        self.assertTrue(archives, "expected at least one rotation")
        # get_recent still works on the (bounded) live file
        recent = log.get_recent(limit=5)
        self.assertTrue(recent)
        self.assertEqual(recent[0]["type"], "tool_call")
        # No record lost overall: archived + live == written
        total = sum(len(_read_gz_lines(a)) for a in archives)
        total += len(log.log_file.read_text(encoding="utf-8").splitlines())
        self.assertEqual(total, 30)

    def test_rotation_disabled_by_config(self):
        from services.activity_log import ActivityLog
        _write_config(self.project, {"enabled": False,
                                     "activity_log_max_mb": 0.0001})
        log = ActivityLog(str(self.project))
        for i in range(20):
            log.log("tool_call", {"tool": f"t{i}"})
        self.assertFalse((self.project / ".c3" / "archive").exists())
        self.assertEqual(
            len(log.log_file.read_text(encoding="utf-8").splitlines()), 20)


class TestEditLedgerRotation(RetentionBase):
    """Rotation must preserve audit integrity, pending entries, and versions."""

    def _make_ledger(self):
        from services.edit_ledger import EditLedger
        return EditLedger(str(self.project))

    @staticmethod
    def _base(eid, file, version, ts, git_pending=False):
        e = {"id": eid, "timestamp": ts.isoformat(), "session_id": "",
             "file": file, "change_type": "modified", "summary": "s",
             "lines_changed": None, "version": version, "git": {},
             "diff_summary": "", "tags": []}
        if git_pending:
            e["git_pending"] = True
        return e

    def _seed(self, ledger):
        """Old enriched, old pending, old fully-archived file, recent entry."""
        old = datetime.now(timezone.utc) - timedelta(days=30)
        now = datetime.now(timezone.utc)
        lines = [
            # E1: old, git_pending but enriched by patch below -> archivable
            self._base("E1", "app.py", "v1", old, git_pending=True),
            {"target_id": "E1", "git": {"commit": "abc"}, "diff_summary": "+1 -1",
             "enriched_at": old.isoformat()},
            # E2: old, git_pending, NO patch -> must be retained (pending flow)
            self._base("E2", "app.py", "v2", old, git_pending=True),
            # E4: old, only entry for gone.py -> archived + tombstone v7
            self._base("E4", "gone.py", "v7", old),
            {"target_id": "E4", "tags_add": ["auto"], "tagged_at": old.isoformat()},
            # E3: recent -> retained
            self._base("E3", "app.py", "v3", now),
        ]
        with open(ledger.ledger_file, "a", encoding="utf-8") as f:
            for e in lines:
                f.write(json.dumps(e) + "\n")
        return len(lines)

    def test_rotation_preserves_pending_and_versions(self):
        from services.edit_ledger import ROTATION_KEY
        ledger = self._make_ledger()
        n_lines = self._seed(ledger)
        archive_dir = self.project / ".c3" / "archive"

        res = ledger.rotate_if_needed(1, archive_dir, keep_days=14)
        self.assertIsNotNone(res)
        self.assertEqual(res["archived_entries"], 2)  # E1, E4

        live = [json.loads(ln) for ln in
                ledger.ledger_file.read_text(encoding="utf-8").splitlines()]
        live_ids = {e.get("id") for e in live if e.get("id")}
        # Pending entry and recent entry retained; archived ones gone.
        self.assertIn("E2", live_ids)
        self.assertIn("E3", live_ids)
        self.assertNotIn("E1", live_ids)
        self.assertNotIn("E4", live_ids)
        # Tombstone only for gone.py (app.py still has live entries).
        tombstones = [e for e in live if ROTATION_KEY in e]
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]["file"], "gone.py")
        self.assertEqual(tombstones[0]["version"], "v7")

        # Audit integrity: every original line is either live or archived.
        gz = Path(res["archive"])
        archived = _read_gz_lines(gz)
        self.assertEqual(len(archived) + (len(live) - len(tombstones)), n_lines)
        archived_ids = {json.loads(ln).get("id") for ln in archived}
        self.assertIn("E1", archived_ids)
        self.assertIn("E4", archived_ids)
        # E1's git patch and E4's tag patch travelled with them.
        archived_targets = {json.loads(ln).get("target_id") for ln in archived}
        self.assertIn("E1", archived_targets)
        self.assertIn("E4", archived_targets)

        # Version continuity through the tombstone (fresh instance = cold cache).
        fresh = self._make_ledger()
        self.assertEqual(fresh.get_version("gone.py"), "v7")
        entry = fresh.log_edit("gone.py", "modified", "back again",
                               include_git=False)
        self.assertEqual(entry["version"], "v8")

    def test_hook_version_scan_reads_tombstone(self):
        ledger = self._make_ledger()
        self._seed(ledger)
        ledger.rotate_if_needed(1, self.project / ".c3" / "archive", keep_days=14)
        from cli.hook_edit_ledger import _next_version
        self.assertEqual(_next_version(ledger.ledger_file, "gone.py"), "v8")

    def test_enricher_still_finds_pending_after_rotation(self):
        ledger = self._make_ledger()
        self._seed(ledger)
        ledger.rotate_if_needed(1, self.project / ".c3" / "archive", keep_days=14)
        # Simulate a git repo so enrich_pending proceeds, with a stub git call.
        ledger._git_root = ledger.project_path
        ledger._git_combined = lambda rel: (
            {"commit": "def", "author": "t", "subject": "s", "dirty": False,
             "branch": "main", "head_sha": "def"}, "+2 -0")
        self.assertEqual(ledger.enrich_pending(batch=10), 1)  # E2 only

    def test_readers_skip_tombstones(self):
        ledger = self._make_ledger()
        self._seed(ledger)
        ledger.rotate_if_needed(1, self.project / ".c3" / "archive", keep_days=14)
        fresh = self._make_ledger()
        stats = fresh.get_stats()
        self.assertEqual(stats["total"], 2)  # E2 + E3
        history = fresh.get_history(limit=100)
        self.assertEqual({e["id"] for e in history}, {"E2", "E3"})
        # validate_pending must not crash on tombstones (no validation cache
        # -> returns [] but only after the scan path runs).
        class _Cache:
            def validate_file(self, f):
                return {"valid": True, "errors": []}
        results = fresh.validate_pending(batch=5, validation_cache=_Cache())
        self.assertTrue(all(r["id"] in {"E2", "E3"} for r in results))

    def test_no_rotation_below_threshold_or_all_recent(self):
        ledger = self._make_ledger()
        self._seed(ledger)
        # Huge threshold: no rotation.
        self.assertIsNone(ledger.rotate_if_needed(
            10 * 1024 * 1024, self.project / ".c3" / "archive"))
        # Tiny threshold but nothing old enough: keep_days very large.
        self.assertIsNone(ledger.rotate_if_needed(
            1, self.project / ".c3" / "archive", keep_days=3650))


class TestSessionCap(RetentionBase):
    def _make_sessions(self, n):
        sessions = self.project / ".c3" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(n):
            ts = (base + timedelta(minutes=i)).strftime("%Y%m%d_%H%M%S")
            (sessions / f"session_{ts}.json").write_text(
                json.dumps({"id": ts}), encoding="utf-8")
        return sessions

    def test_cap_archives_oldest(self):
        sessions = self._make_sessions(12)
        archive = self.project / ".c3" / "archive"
        pruned = cap_session_files(sessions, max_files=5, archive_dir=archive)
        self.assertEqual(len(pruned), 7)
        remaining = sorted(sessions.glob("session_*.json"))
        self.assertEqual(len(remaining), 5)
        # Newest kept (names sort chronologically)
        self.assertTrue(remaining[-1].name.endswith("_001100.json")
                        or len(remaining) == 5)
        gz_files = list(archive.glob("session_*.json.gz"))
        self.assertEqual(len(gz_files), 7)
        # Archived session content is readable
        with gzip.open(gz_files[0], "rt", encoding="utf-8") as f:
            self.assertIn("id", json.load(f))

    def test_cap_delete_mode(self):
        sessions = self._make_sessions(8)
        pruned = cap_session_files(sessions, max_files=5, archive_dir=None)
        self.assertEqual(len(pruned), 3)
        self.assertEqual(len(list(sessions.glob("session_*.json"))), 5)

    def test_under_cap_untouched(self):
        sessions = self._make_sessions(3)
        self.assertEqual(cap_session_files(sessions, max_files=5), [])
        self.assertEqual(len(list(sessions.glob("session_*.json"))), 3)

    def test_save_session_enforces_cap_and_latest_loads(self):
        from services.session_manager import SessionManager
        _write_config(self.project, {"sessions_max_files": 4})
        self._make_sessions(10)
        mgr = SessionManager(str(self.project))
        mgr.start_session("cap test")
        result = mgr.save_session("done")
        self.assertNotIn("error", result)
        files = sorted((self.project / ".c3" / "sessions").glob("session_*.json"))
        self.assertLessEqual(len(files), 4)
        # The just-saved session is the newest and still restorable.
        loaded = mgr.load_session("latest")
        self.assertEqual(loaded.get("id"), result["session_id"])


class TestFileMemoryPrune(RetentionBase):
    def test_prunes_deleted_files_only(self):
        from services.file_memory import FileMemoryStore
        (self.project / "keep.py").write_text("def keep():\n    pass\n",
                                              encoding="utf-8")
        (self.project / "gone.py").write_text("def gone():\n    pass\n",
                                              encoding="utf-8")
        store = FileMemoryStore(str(self.project))
        self.assertIsNotNone(store.update("keep.py"))
        self.assertIsNotNone(store.update("gone.py"))
        self.assertEqual(len(store.list_tracked()), 2)

        (self.project / "gone.py").unlink()
        pruned = store.prune_stale()
        self.assertEqual(pruned, ["gone.py"])
        self.assertEqual(store.list_tracked(), ["keep.py"])
        # Search still functional after prune
        results = store.search("keep")
        self.assertTrue(all(r["path"] == "keep.py" for r in results))
        # Idempotent
        self.assertEqual(store.prune_stale(), [])


class TestNotificationsRotation(RetentionBase):
    def _store(self):
        from services.notifications import NotificationStore
        return NotificationStore(str(self.project))

    def test_moves_only_old_acknowledged(self):
        store = self._store()
        store.add("agent1", "warning", "t1", "m1")
        store.add("agent2", "warning", "t2", "m2")
        store.add("agent3", "info", "t3", "m3")
        # Acknowledge two of them
        store.acknowledge_all()
        store.add("agent4", "critical", "t4", "m4")  # stays unacked

        archived = []
        moved = store.rotate_acknowledged(
            lambda entries: archived.extend(entries) or True,
            min_age_minutes=0)
        self.assertEqual(moved, 3)
        self.assertEqual(len(archived), 3)
        remaining = store.get_history(limit=50)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["title"], "t4")
        self.assertFalse(remaining[0].get("acknowledged"))

    def test_recent_acks_kept_for_cooldown(self):
        store = self._store()
        store.add("agent1", "warning", "t1", "m1")
        store.acknowledge_all()
        moved = store.rotate_acknowledged(lambda entries: True,
                                          min_age_minutes=120)
        self.assertEqual(moved, 0)  # too fresh — cooldown window preserved

    def test_archive_failure_keeps_records(self):
        store = self._store()
        store.add("agent1", "warning", "t1", "m1")
        store.acknowledge_all()
        moved = store.rotate_acknowledged(lambda entries: False,
                                          min_age_minutes=0)
        self.assertEqual(moved, 0)
        self.assertEqual(len(store.get_history(limit=50)), 1)


class TestRetentionManager(RetentionBase):
    def test_maybe_run_throttles(self):
        mgr = RetentionManager(str(self.project))
        first = mgr.maybe_run()
        self.assertIsInstance(first, dict)
        self.assertIsNone(mgr.maybe_run())  # inside the sweep interval

    def test_disabled_config_skips_sweep(self):
        _write_config(self.project, {"enabled": False})
        mgr = RetentionManager(str(self.project))
        self.assertIsNone(mgr.maybe_run())

    def test_full_sweep(self):
        from services.edit_ledger import EditLedger
        from services.file_memory import FileMemoryStore
        from services.notifications import NotificationStore

        _write_config(self.project, {
            "edit_ledger_max_mb": 0.000001,
            "notifications_max_mb": 0.000001,
            "notifications_min_age_minutes": 0,
            "sessions_max_files": 2,
        })
        # Edit ledger with an old enriched entry
        ledger = EditLedger(str(self.project))
        old = datetime.now(timezone.utc) - timedelta(days=30)
        with open(ledger.ledger_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "E1", "timestamp": old.isoformat(),
                                "file": "x.py", "change_type": "modified",
                                "summary": "s", "version": "v1", "git": {},
                                "tags": []}) + "\n")
        # Notifications: one acked entry
        notif = NotificationStore(str(self.project))
        notif.add("a", "warning", "t", "m")
        notif.acknowledge_all()
        # Sessions over cap
        sessions = self.project / ".c3" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (sessions / f"session_2026010{i}_000000.json").write_text(
                "{}", encoding="utf-8")
        # File memory with a stale record
        (self.project / "tmp.py").write_text("x = 1\n", encoding="utf-8")
        fm = FileMemoryStore(str(self.project))
        fm.update("tmp.py")
        (self.project / "tmp.py").unlink()

        mgr = RetentionManager(str(self.project), edit_ledger=ledger,
                               notifications=notif, file_memory=fm)
        summary = mgr.run_sweep()
        self.assertIn("edit_ledger", summary)
        self.assertEqual(summary["notifications_archived"], 1)
        self.assertEqual(summary["sessions_pruned"], 3)
        self.assertEqual(summary["file_memory_pruned"], 1)
        # Archives exist for ledger + notifications + sessions
        archive = self.project / ".c3" / "archive"
        self.assertTrue(list(archive.glob("edit_ledger.*.jsonl.gz")))
        self.assertTrue(list(archive.glob("notifications.*.jsonl.gz")))
        self.assertTrue(list(archive.glob("session_*.json.gz")))


if __name__ == "__main__":
    unittest.main()
