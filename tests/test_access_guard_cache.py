"""Access Guard reuses built rules only while nothing they were built from moved.

verdict() runs for every path a search, read or watcher event touches, and
building the rules read each scope's config three times, the leases file and
the keyring: 2-3 ms and seven file reads per verdict. These tests pin when a
cached build may answer: every file it read still has the stat signature it
had, the entry is younger than _RULES_TTL_S, no lease row has lapsed since,
and no file it read was written within the last _RACY_S seconds. Writers in
this process clear the cache outright.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services import access_guard as ag


def _backdate(path: Path, seconds: float = 60.0) -> None:
    """Move *path*'s mtime into the past so its signature is trusted."""
    past = time.time() - seconds
    os.utime(path, (past, past))


class CacheCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.proj = base / "proj"
        for d in (self.home / ".c3", self.proj / ".c3"):
            d.mkdir(parents=True)
        patcher = mock.patch.object(ag, "_global_base", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = self.proj / ".c3" / "config.json"
        ag.invalidate_cache()
        self.addCleanup(ag.invalidate_cache)

    def tearDown(self):
        self._tmp.cleanup()

    def write_access(self, section, *, age=60.0):
        self.cfg.write_text(json.dumps({"access": section}), encoding="utf-8")
        if age:
            _backdate(self.cfg, age)

    def denied(self, rel):
        return ag.check(str(self.proj / rel), "read", str(self.proj)) is not None

    def builds(self):
        return mock.patch.object(ag, "_build_rules", wraps=ag._build_rules)

    def entry_ttl(self) -> float:
        (entry,) = ag._rules_cache.values()
        return entry[1] - time.monotonic()


class TestRuleCache(CacheCase):
    def test_unchanged_files_build_once(self):
        self.write_access({"deny": ["secrets/**"]})
        with self.builds() as build:
            for _ in range(5):
                self.assertTrue(self.denied("secrets/key.txt"))
        self.assertEqual(build.call_count, 1)

    def test_a_config_edit_applies_on_the_next_verdict(self):
        self.write_access({"deny": ["secrets/**"]})
        self.assertTrue(self.denied("secrets/key.txt"))
        self.write_access({"deny": []}, age=30.0)  # another mtime, still trusted
        self.assertFalse(self.denied("secrets/key.txt"))

    def test_a_global_config_appearing_forces_a_build(self):
        with self.builds() as build:
            ag.load_all(str(self.proj))
            gcfg = self.home / ".c3" / "config.json"
            gcfg.write_text(json.dumps({"access": {"deny": []}}), encoding="utf-8")
            _backdate(gcfg)
            ag.load_all(str(self.proj))
        self.assertEqual(build.call_count, 2)

    def test_a_fresh_file_is_never_trusted(self):
        """Two writes inside one timestamp tick can leave mtime and size equal."""
        self.write_access({"deny": ["secrets/**"]}, age=0)
        with self.builds() as build:
            self.denied("secrets/key.txt")
            self.denied("secrets/key.txt")
        self.assertEqual(build.call_count, 2)
        self.assertEqual(ag._rules_cache, {})

    def test_entries_expire_after_the_ttl(self):
        """Keyring attestations have no file to stat; the TTL bounds them."""
        self.write_access({"deny": []})
        with self.builds() as build, mock.patch.object(ag, "_RULES_TTL_S", 0.0):
            ag.load_all(str(self.proj))
            ag.load_all(str(self.proj))
        self.assertEqual(build.call_count, 2)

    def test_invalidate_cache_forces_a_build(self):
        self.write_access({"deny": []})
        with self.builds() as build:
            ag.load_all(str(self.proj))
            ag.invalidate_cache()
            ag.load_all(str(self.proj))
        self.assertEqual(build.call_count, 2)

    def test_the_scope_writer_clears_the_cache(self):
        self.write_access({"deny": []})
        ag.load_all(str(self.proj))
        self.assertTrue(ag._rules_cache)
        ag._write_scope_config(self.cfg, {"access": {"deny": ["secrets/**"]}})
        self.assertEqual(ag._rules_cache, {})
        self.assertTrue(self.denied("secrets/key.txt"))

    def test_each_project_has_its_own_entry(self):
        other = Path(self._tmp.name) / "other"
        (other / ".c3").mkdir(parents=True)
        self.write_access({"deny": ["secrets/**"]})
        self.assertTrue(self.denied("secrets/key.txt"))
        self.assertIsNone(ag.check(str(other / "secrets" / "key.txt"), "read", str(other)))

    def test_callers_get_copies(self):
        self.write_access({"deny": ["secrets/**"]})
        rules, _masks, _corrupt = ag.load_all(str(self.proj))
        rules.clear()
        self.assertTrue(self.denied("secrets/key.txt"))

    def test_has_active_rules_sees_one_user_rule(self):
        self.assertFalse(ag.has_active_rules(str(self.proj)))
        self.write_access({"deny": ["secrets/**"]}, age=0)
        self.assertTrue(ag.has_active_rules(str(self.proj)))


class TestLeaseLapse(CacheCase):
    def leases(self, *rows):
        path = self.home / ".c3" / ag.LEASES_FILE
        path.write_text(json.dumps({"leases": list(rows)}), encoding="utf-8")
        _backdate(path)
        return path

    @staticmethod
    def row(seconds):
        at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return {"id": f"bls_{seconds}", "expires_at": at.isoformat()}

    def test_an_entry_never_outlives_the_soonest_lease(self):
        self.leases(self.row(600), self.row(1))
        ag.load_all(str(self.proj))
        self.assertLessEqual(self.entry_ttl(), 1.0)

    def test_rows_that_cannot_lapse_do_not_shorten_it(self):
        self.leases(self.row(-60), {"id": "x", "expires_at": "soon"}, {"id": "y"},
                    {"id": "naive", "expires_at": "2099-01-01T00:00:00"}, "junk")
        ag.load_all(str(self.proj))
        self.assertGreater(self.entry_ttl(), ag._RULES_TTL_S - 1)

    def test_an_unreadable_leases_file_is_not_cached_past_this_verdict(self):
        path = self.home / ".c3" / ag.LEASES_FILE
        path.write_text("{broken", encoding="utf-8")
        _backdate(path)
        with self.builds() as build:
            ag.load_all(str(self.proj))
            ag.load_all(str(self.proj))
        self.assertEqual(build.call_count, 2)

    def test_next_lease_lapse(self):
        self.assertTrue(28 < ag._next_lease_lapse(self.leases(self.row(30), self.row(90))) <= 30)
        self.assertIsNone(ag._next_lease_lapse(self.leases(self.row(-5))))
        self.assertIsNone(ag._next_lease_lapse(self.leases()))


if __name__ == "__main__":
    unittest.main()
