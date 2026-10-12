"""services/ui_registry (2.155.0): the UI registry has one writer at a time.

Three processes used to rewrite ~/.c3/registry.json from their own snapshot
(every `c3 ui` at startup, the hub sweep, every GET /api/projects). A server
that registered between another writer's read and write was lost; a busy
server that missed one probe was dropped. Lost entries became orphans: six
`c3 ui` processes (3.3 GB) on one machine that nothing could reap. These pin
the patch-under-lock contract and the "dead = port AND pid" rule.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import ui_registry  # noqa: E402


def _entry(port, **extra):
    e = {"port": port, "project_path": f"/p/{port}", "project_name": f"p{port}",
         "started_at": 1.0, "pid": 100 + port, "owner": "session", "owner_session": "s"}
    e.update(extra)
    return e


class TestRegistryFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "registry.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_corrupt_file_reads_as_empty(self):
        self.assertEqual(ui_registry.read(self.path), [])
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(ui_registry.read(self.path), [])
        self.path.write_text(json.dumps({"port": 1}), encoding="utf-8")
        self.assertEqual(ui_registry.read(self.path), [])          # not a list
        self.path.write_text(json.dumps([1, {"port": 2}]), encoding="utf-8")
        self.assertEqual(ui_registry.read(self.path), [{"port": 2}])  # rows only

    def test_upsert_adds_then_replaces_by_port(self):
        self.assertTrue(ui_registry.upsert(_entry(3333), self.path))
        self.assertTrue(ui_registry.upsert(_entry(3334), self.path))
        self.assertTrue(ui_registry.upsert(_entry(3333, pid=999), self.path))
        rows = ui_registry.read(self.path)
        self.assertEqual(sorted(r["port"] for r in rows), [3333, 3334])
        self.assertEqual(next(r for r in rows if r["port"] == 3333)["pid"], 999)

    def test_upsert_never_prunes_siblings(self):
        """The old _register_session dropped every other entry whose port
        missed a 0.3 s probe. None of these ports answer; all survive."""
        for port in (3333, 3334, 3335):
            ui_registry.upsert(_entry(port), self.path)
        self.assertEqual(len(ui_registry.read(self.path)), 3)

    def test_upsert_without_a_port_is_refused(self):
        self.assertFalse(ui_registry.upsert({"project_path": "/p"}, self.path))
        self.assertFalse(self.path.exists())

    def test_ensure_writes_only_when_missing_or_moved(self):
        entry = _entry(3333)
        self.assertTrue(ui_registry.ensure(entry, self.path))
        stamp = self.path.stat().st_mtime_ns
        with mock.patch.object(ui_registry, "upsert", wraps=ui_registry.upsert) as up:
            self.assertTrue(ui_registry.ensure(entry, self.path))   # present: no write
            up.assert_not_called()
            self.assertEqual(self.path.stat().st_mtime_ns, stamp)
            self.assertTrue(ui_registry.ensure(_entry(3333, pid=1), self.path))  # pid moved
            up.assert_called_once()
        # A lost entry comes back on the next re-assert.
        ui_registry.write([], self.path)
        self.assertTrue(ui_registry.ensure(entry, self.path))
        self.assertEqual([r["port"] for r in ui_registry.read(self.path)], [3333])

    def test_remove_ports_applies_to_a_fresh_read(self):
        """The sweep decides from its probes; a server that registered in
        the meantime is not in its snapshot and must survive the removal."""
        ui_registry.upsert(_entry(3333), self.path)
        ui_registry.upsert(_entry(3334), self.path)
        snapshot_ports = [3333]
        ui_registry.upsert(_entry(3335), self.path)     # registered after the snapshot
        self.assertEqual(ui_registry.remove_ports(snapshot_ports, self.path), 1)
        self.assertEqual(sorted(r["port"] for r in ui_registry.read(self.path)), [3334, 3335])
        self.assertEqual(ui_registry.remove_ports([9999, "x", None], self.path), 0)
        self.assertEqual(ui_registry.remove_ports([], self.path), 0)

    def test_remove_ports_can_drop_portless_rows(self):
        ui_registry.write([_entry(3333), {"project_path": "/p"}], self.path)
        self.assertEqual(ui_registry.remove_ports([], self.path), 0)
        self.assertEqual(len(ui_registry.read(self.path)), 2)
        self.assertEqual(ui_registry.remove_ports([], self.path, drop_portless=True), 1)
        self.assertEqual([r["port"] for r in ui_registry.read(self.path)], [3333])

    def test_concurrent_writers_lose_nothing(self):
        """Twenty threads each upsert their own port while one removes a
        port none of them own. Every upsert must be in the final file."""
        ui_registry.upsert(_entry(1), self.path)
        errors: list = []

        def register(port):
            try:
                self.assertTrue(ui_registry.upsert(_entry(port), self.path))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(4000 + i,)) for i in range(20)]
        threads.append(threading.Thread(target=ui_registry.remove_ports, args=([1], self.path)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        ports = sorted(r["port"] for r in ui_registry.read(self.path))
        self.assertEqual(ports, list(range(4000, 4020)))

    def test_lock_timeout_is_a_refusal_not_a_hang(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with ui_registry.locked(self.path):
                held.set()
                release.wait(10)

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(held.wait(5))
        try:
            with self.assertRaises(TimeoutError):
                with ui_registry.locked(self.path, timeout_s=0.2):
                    pass
            # The best-effort writers swallow it and report False / 0, and
            # the file is left exactly as it was.
            before = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
            with mock.patch.object(ui_registry, "LOCK_TIMEOUT_S", 0.2):
                self.assertFalse(ui_registry.upsert(_entry(5), self.path))
                self.assertEqual(ui_registry.remove_ports([5], self.path), 0)
            after = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
            self.assertEqual(after, before)
        finally:
            release.set()
            t.join(5)


class TestLiveness(unittest.TestCase):
    def test_own_pid_is_alive_and_an_impossible_pid_is_not(self):
        self.assertTrue(ui_registry.pid_alive(os.getpid()))
        self.assertFalse(ui_registry.pid_alive(0))
        self.assertFalse(ui_registry.pid_alive(-5))
        self.assertFalse(ui_registry.pid_alive("nope"))
        self.assertFalse(ui_registry.pid_alive(None))

    def test_entry_is_alive_on_port_or_on_pid(self):
        entry = {"port": 3333, "pid": 42}
        calls = []
        self.assertTrue(ui_registry.entry_alive(
            entry, port_probe=lambda p: calls.append(("port", p)) or True,
            pid_probe=lambda p: calls.append(("pid", p)) or False))
        self.assertEqual(calls, [("port", 3333)])            # port answered: no pid probe
        self.assertTrue(ui_registry.entry_alive(
            entry, port_probe=lambda p: False, pid_probe=lambda p: True))   # busy, not dead
        self.assertFalse(ui_registry.entry_alive(
            entry, port_probe=lambda p: False, pid_probe=lambda p: False))
        self.assertFalse(ui_registry.entry_alive(
            {"project_path": "/p"}, port_probe=lambda p: True, pid_probe=lambda p: False))


if __name__ == "__main__":
    unittest.main()
