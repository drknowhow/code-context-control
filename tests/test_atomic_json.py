"""services.atomic_json — the one publish step for C3's small JSON stores.

Every test here fails against the shape these call sites carried before
(`<name>.tmp` + a single `os.replace`), which is the shape that crashed shell
job supervisors on Windows with `PermissionError: [WinError 5]`. The point of
the module is that the lock store and the three `config.json` writers stop
re-deriving it, so the last class checks each of those call sites actually
routes through it rather than only the helper being correct in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import atomic_json as aj  # noqa: E402


class TestTempNaming(unittest.TestCase):
    """The temp must be unique per WRITE, not per path and not per process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _replace_spy(self):
        seen: list[str] = []
        real = os.replace

        def spy(src, dst):
            seen.append(Path(src).name)
            return real(src, dst)

        return seen, spy

    def test_temp_is_not_the_shared_name(self):
        seen, spy = self._replace_spy()
        with patch.object(aj.os, "replace", spy):
            aj.write_json_atomic(self.path, {"a": 1})
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], "config.json.tmp",
                            "shared temp name — concurrent writers race on it")
        self.assertTrue(seen[0].startswith("config.json.tmp"))
        self.assertIn(str(os.getpid()), seen[0])

    def test_temp_differs_between_writes_in_one_process(self):
        """Threads share a pid, so `.tmp<pid>` collides exactly as `.tmp` did."""
        seen, spy = self._replace_spy()
        with patch.object(aj.os, "replace", spy):
            for i in range(5):
                aj.write_json_atomic(self.path, {"a": i})
        self.assertEqual(len(set(seen)), 5, f"temp name reused: {seen}")


class TestReplaceRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_transient_permission_error_is_retried(self):
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:            # two transient failures, then success
                raise PermissionError(5, "Access is denied")
            return real(src, dst)

        with patch.object(aj.os, "replace", flaky):
            aj.write_json_atomic(self.path, {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"ok": True})

    def test_persistent_permission_error_still_raises(self):
        """Retrying must not turn a real failure into a silently lost write."""
        def always(src, dst):
            raise PermissionError(5, "Access is denied")

        with patch.object(aj.os, "replace", always):
            with self.assertRaises(PermissionError):
                aj.write_json_atomic(self.path, {"a": 1})

    def test_a_non_permission_oserror_is_not_retried(self):
        """Only the Windows sharing violation is transient; the rest surface."""
        calls = {"n": 0}

        def broken(src, dst):
            calls["n"] += 1
            raise OSError(22, "Invalid argument")

        with patch.object(aj.os, "replace", broken):
            with self.assertRaises(OSError):
                aj.write_json_atomic(self.path, {"a": 1})
        self.assertEqual(calls["n"], 1)

    def test_no_temp_is_left_behind_when_replace_fails(self):
        def always(src, dst):
            raise PermissionError(5, "Access is denied")

        with patch.object(aj.os, "replace", always):
            with self.assertRaises(PermissionError):
                aj.write_json_atomic(self.path, {"a": 1})
        leftovers = list(self.path.parent.glob("config.json.tmp*"))
        self.assertEqual(leftovers, [], f"orphaned temps: {leftovers}")


class TestSerialisationIsPreserved(unittest.TestCase):
    """Each call site keeps the exact bytes it wrote before the refactor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "f.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_is_indent_2_with_trailing_newline(self):
        aj.write_json_atomic(self.path, {"a": 1})
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         '{\n  "a": 1\n}\n')

    def test_trailing_newline_can_be_suppressed(self):
        aj.write_json_atomic(self.path, {"a": 1}, trailing_newline=False)
        self.assertEqual(self.path.read_text(encoding="utf-8"),
                         '{\n  "a": 1\n}')

    def test_ensure_ascii_false_keeps_non_ascii_literal(self):
        aj.write_json_atomic(self.path, {"k": "café"}, ensure_ascii=False)
        self.assertIn("café", self.path.read_text(encoding="utf-8"))

    def test_parent_directory_is_created(self):
        deep = Path(self._tmp.name) / "a" / "b" / "c.json"
        aj.write_json_atomic(deep, {"a": 1})
        self.assertEqual(json.loads(deep.read_text(encoding="utf-8")), {"a": 1})


class TestFsync(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "f.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_durable_by_default(self):
        """A truncated config.json fails the guard closed; flush before publish."""
        with patch.object(aj.os, "fsync") as fs:
            aj.write_json_atomic(self.path, {"a": 1})
        self.assertEqual(fs.call_count, 1)

    def test_opt_out_for_ephemeral_state(self):
        with patch.object(aj.os, "fsync") as fs:
            aj.write_json_atomic(self.path, {"a": 1}, fsync=False)
        fs.assert_not_called()


class TestConcurrency(unittest.TestCase):
    def test_threads_never_publish_a_truncated_file(self):
        """The end-to-end property, and the one `.tmp<pid>` fails."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "locks.json"
            errors: list[Exception] = []

            def writer(i):
                try:
                    for _ in range(20):
                        aj.write_json_atomic(path, {"writer": i})
                except Exception as exc:      # noqa: BLE001 — surfaced below
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,))
                       for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"concurrent writes failed: {errors}")
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(doc["writer"], range(6))
            leftovers = list(path.parent.glob("locks.json.tmp*"))
            self.assertEqual(leftovers, [], f"orphaned temps: {leftovers}")


class TestCallSitesUseIt(unittest.TestCase):
    """The refactor's actual claim: no copy of the shape is left behind.

    A source scan rather than a behavioural probe, because the four sites sit
    behind a Flask route, an advisory file lock and a scope resolver — and
    because the thing being asserted IS textual: nobody re-derives the publish
    step. `cli/_hook_utils` is exempt on purpose (a PreToolUse subprocess
    imports nothing from `services`).
    """

    ROOT = Path(__file__).resolve().parents[1]
    SITES = (
        "services/agent_locks.py",
        "services/access_guard.py",
        "services/shell_output.py",
        "cli/hub_server.py",
        "oracle/services/mobile_api.py",
    )

    def test_each_site_imports_the_helper(self):
        for rel in self.SITES:
            with self.subTest(file=rel):
                src = (self.ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("from services.atomic_json import", src)

    def test_no_site_still_writes_a_shared_temp(self):
        for rel in self.SITES:
            with self.subTest(file=rel):
                src = (self.ROOT / rel).read_text(encoding="utf-8")
                for line in src.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#") or '"""' in stripped:
                        continue          # prose about the old shape is fine
                    self.assertNotIn('.name + ".tmp"', stripped)
                    self.assertNotIn('.name}.tmp"', stripped)
                    self.assertNotIn('"config.json.tmp"', stripped)


if __name__ == "__main__":
    unittest.main()
