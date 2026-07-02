"""Tests for C3Bridge's runtime cache (shared ProjectRuntimeCache adoption).

Pins the WI-4 unification contract: validation against discovered projects
happens before any build, cache hits don't rebuild, LRU eviction stops
runtimes, blocked (write) actions never touch a runtime, and the on_build
vector-warm hook fires exactly once per newly built runtime.

build_runtime/stop_runtime are stubbed at the services.project_runtime module
level (the cache calls those bindings), so no real C3Runtime is constructed.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import services.project_runtime as pr  # noqa: E402
from oracle.services import c3_bridge as cb  # noqa: E402
from oracle.services.c3_bridge import C3Bridge  # noqa: E402


class _FakeRuntime:
    def __init__(self, path):
        self.project_path = path
        self.embedding_index = None
        self.vector_store = None
        self.indexer = object()


class _StubScanner:
    def __init__(self, paths):
        self._paths = paths

    def discover(self, force=False):
        return [{"path": p, "has_c3": True} for p in self._paths]


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously."""

    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _mkproject(tmp: str, name: str) -> str:
    d = Path(tmp) / name
    (d / ".c3").mkdir(parents=True)
    return str(d.resolve())


class _BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p1 = _mkproject(self.tmp.name, "proj1")
        self.p2 = _mkproject(self.tmp.name, "proj2")
        self.p3 = _mkproject(self.tmp.name, "proj3")
        self.builds: list[str] = []
        self.stops: list[str] = []

        def fake_build(path, ide_name="claude-code"):
            self.builds.append(path)
            return _FakeRuntime(path)

        def fake_stop(rt):
            self.stops.append(rt.project_path)

        for patcher in (
            mock.patch.object(pr, "build_runtime", fake_build),
            mock.patch.object(pr, "stop_runtime", fake_stop),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _bridge(self, paths, max_cached=8, on_build=None):
        bridge = C3Bridge(scanner=_StubScanner(paths))
        # Swap in a size/hook-controlled cache for deterministic tests.
        bridge._cache = pr.ProjectRuntimeCache(
            ide_name="claude-code", max_cached=max_cached, on_build=on_build
        )
        return bridge


class TestValidationBeforeBuild(_BridgeTestBase):
    def test_unknown_path_rejected_before_build(self):
        bridge = self._bridge([self.p1])
        with self.assertRaises(ValueError):
            bridge.get_runtime(self.p2)  # exists on disk but not discovered
        self.assertEqual(self.builds, [])

    def test_blocked_actions_never_touch_a_runtime(self):
        bridge = self._bridge([self.p1])
        cases = [
            bridge.c3_memory(self.p1, action="add", fact="x"),
            bridge.c3_memory(self.p1, action="delete", fact_id="1"),
            bridge.c3_edits(self.p1, action="log", summary="x"),
            bridge.c3_status(self.p1, view="ghost_files"),
        ]
        for result in cases:
            self.assertIn("error", result)
        self.assertEqual(self.builds, [])


class TestCacheBehaviour(_BridgeTestBase):
    def test_cache_hit_builds_once(self):
        bridge = self._bridge([self.p1])
        rt_a = bridge.get_runtime(self.p1)
        rt_b = bridge.get_runtime(self.p1)
        self.assertIs(rt_a, rt_b)
        self.assertEqual(self.builds, [self.p1])

    def test_lru_eviction_stops_oldest_runtime(self):
        bridge = self._bridge([self.p1, self.p2, self.p3], max_cached=2)
        bridge.get_runtime(self.p1)
        bridge.get_runtime(self.p2)
        bridge.get_runtime(self.p3)
        self.assertEqual(self.stops, [self.p1])
        self.assertEqual(len(self.builds), 3)

    def test_shutdown_stops_all(self):
        bridge = self._bridge([self.p1, self.p2])
        bridge.get_runtime(self.p1)
        bridge.get_runtime(self.p2)
        bridge.shutdown()
        self.assertEqual(sorted(self.stops), sorted([self.p1, self.p2]))


class TestWarmOnBuild(_BridgeTestBase):
    def test_on_build_fires_once_per_built_runtime(self):
        warmed: list[str] = []
        bridge = self._bridge(
            [self.p1, self.p2], on_build=lambda rt: warmed.append(rt.project_path)
        )
        bridge.get_runtime(self.p1)
        bridge.get_runtime(self.p1)  # cache hit — no second warm
        bridge.get_runtime(self.p2)
        self.assertEqual(warmed, [self.p1, self.p2])

    def test_default_hook_is_warm_runtime(self):
        bridge = C3Bridge(scanner=_StubScanner([self.p1]))
        self.assertEqual(bridge._cache._on_build, bridge._warm_runtime)

    def test_warm_runtime_warms_both_backends(self):
        bridge = C3Bridge(scanner=_StubScanner([self.p1]))
        rt = _FakeRuntime(self.p1)
        rt.embedding_index = mock.Mock()
        rt.vector_store = mock.Mock()
        with mock.patch.object(cb.threading, "Thread", _InlineThread):
            bridge._warm_runtime(rt)
        rt.embedding_index.build.assert_called_once_with(rt.indexer)
        rt.vector_store.warm.assert_called_once_with()

    def test_warm_runtime_swallows_backend_failures(self):
        bridge = C3Bridge(scanner=_StubScanner([self.p1]))
        rt = _FakeRuntime(self.p1)
        rt.embedding_index = mock.Mock()
        rt.embedding_index.build.side_effect = RuntimeError("boom")
        rt.vector_store = mock.Mock()
        with mock.patch.object(cb.threading, "Thread", _InlineThread):
            bridge._warm_runtime(rt)  # must not raise
        rt.vector_store.warm.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
