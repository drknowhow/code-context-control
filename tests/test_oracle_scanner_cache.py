"""Tests for the ProjectScanner TTL cache.

discover() sits on the hot path of every Oracle tool call
(validate_project_path) and several endpoints call it repeatedly per request;
the TTL cache must serve copies (callers mutate the returned dicts), honor
force=True, and never cache a failed/empty discovery.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.services.project_scanner as ps  # noqa: E402


class _FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now


def _make_scanner(projects, ttl=20.0):
    """Scanner whose underlying reads are counted and controllable."""
    scanner = ps.ProjectScanner(ttl=ttl)
    calls = {"n": 0}

    def fake_from_hub():
        calls["n"] += 1
        return [dict(p) for p in projects]

    scanner._from_hub = fake_from_hub
    scanner._from_file = lambda: []
    # _enrich touches the filesystem; identity keeps these tests hermetic.
    scanner._enrich = lambda p: p
    return scanner, calls


class TestScannerTTLCache(unittest.TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        patcher = mock.patch.object(ps, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_read_within_ttl(self):
        scanner, calls = _make_scanner([{"path": "C:/a", "tags": []}])
        for _ in range(5):
            result = scanner.discover()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result, [{"path": "C:/a", "tags": []}])

    def test_refresh_after_expiry(self):
        scanner, calls = _make_scanner([{"path": "C:/a", "tags": []}], ttl=20.0)
        scanner.discover()
        self.clock.now += 19.0
        scanner.discover()
        self.assertEqual(calls["n"], 1)
        self.clock.now += 2.0  # past TTL
        scanner.discover()
        self.assertEqual(calls["n"], 2)

    def test_force_bypasses_cache(self):
        scanner, calls = _make_scanner([{"path": "C:/a", "tags": []}])
        scanner.discover()
        scanner.discover(force=True)
        self.assertEqual(calls["n"], 2)

    def test_copy_semantics_guard_cache_poisoning(self):
        # api_projects mutates the returned dicts (health fields); a cached
        # reference would leak those mutations into the next caller.
        scanner, _ = _make_scanner([{"path": "C:/a", "tags": ["x"]}])
        first = scanner.discover()
        first[0]["health_status"] = "error"
        first[0]["tags"].append("poisoned")
        second = scanner.discover()
        self.assertNotIn("health_status", second[0])
        self.assertEqual(second[0]["tags"], ["x"])

    def test_empty_discovery_not_cached(self):
        scanner, calls = _make_scanner([])
        self.assertEqual(scanner.discover(), [])
        self.assertEqual(scanner.discover(), [])
        self.assertEqual(calls["n"], 2)  # retried, not served from cache


if __name__ == "__main__":
    unittest.main()
