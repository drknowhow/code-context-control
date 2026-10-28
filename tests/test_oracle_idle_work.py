"""What the Oracle does while nothing happens.

A phone holding /api/mobile/feed?wait= used to stat four feed files of every
registered project once a second (360 stats a second over 90 projects). Now
a project whose feed and live-heartbeat directory have not changed for
_HOT_WINDOW_S is checked every _COLD_EVERY_S, and the rest every tick.
_registered_roots() resolved every project on each call; it is now reused for
_ROOTS_TTL_S. The discovery MCP server's uvicorn loop woke ten times a second
to notice a shutdown request; it now wakes once a second.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from oracle import mcp_oracle
from oracle.services import mobile_api


class _Clock:
    """monotonic() and sleep() for _hold_for_items, without the waiting."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    @staticmethod
    def time():
        return time.time()


class TestHeldFeedWait(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.hot = {"path": str(base / "hot"), "name": "hot"}
        self.cold = {"path": str(base / "cold"), "name": "cold"}
        now = time.time()
        self.mtimes = {"hot": now - 60, "cold": now - 3600}
        self.calls = []
        self.clock = _Clock()
        for patcher in (
            mock.patch.object(mobile_api, "time", self.clock),
            mock.patch.object(mobile_api, "_feed_mtime", self.feed_mtime),
            mock.patch.object(mobile_api, "principal", return_value={"kind": "test-idle"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def feed_mtime(self, targets):
        self.calls.append([t["name"] for t in targets])
        return max((self.mtimes[t["name"]] for t in targets), default=0.0)

    def change_at(self, name, when):
        """Move *name*'s feed mtime once the clock reaches *when*."""
        step = self.clock.sleep

        def sleep(seconds):
            step(seconds)
            if self.clock.now == when:
                self.mtimes[name] += 1

        self.clock.sleep = sleep

    def test_cold_targets_are_checked_every_cold_interval(self):
        found, waited = mobile_api._hold_for_items([self.hot, self.cold], list, 12)
        self.assertEqual((found, waited), ([], 12.0))
        held = self.calls[2:]  # after _split_hot's baseline of each target
        self.assertEqual(held.count(["hot"]), 12)
        self.assertEqual(held.count(["cold"]), 1)

    def test_a_hot_change_wakes_the_hold_on_the_next_tick(self):
        self.change_at("hot", 1002.0)
        found, waited = mobile_api._hold_for_items([self.hot, self.cold],
                                                   lambda: ["item"], 30)
        self.assertEqual((found, waited), (["item"], 2.0))

    def test_a_cold_change_wakes_the_hold_within_the_cold_interval(self):
        self.change_at("cold", 1003.0)
        found, waited = mobile_api._hold_for_items([self.hot, self.cold],
                                                   lambda: ["item"], 30)
        self.assertEqual((found, waited), (["item"], 10.0))

    def test_a_change_that_yields_nothing_rebaselines(self):
        self.change_at("hot", 1002.0)
        collect = mock.Mock(return_value=[])
        mobile_api._hold_for_items([self.hot, self.cold], collect, 6)
        collect.assert_called_once()

    def test_one_target_is_always_hot(self):
        hot, cold, hot_m, _cold_m = mobile_api._split_hot([self.cold])
        self.assertEqual((hot, cold, hot_m), ([self.cold], [], self.mtimes["cold"]))

    def test_a_heartbeat_makes_a_target_hot_without_counting_as_a_change(self):
        (Path(self.cold["path"]) / ".c3" / "live").mkdir(parents=True)
        hot, cold, hot_m, cold_m = mobile_api._split_hot([self.hot, self.cold])
        self.assertEqual((hot, cold), ([self.hot, self.cold], []))
        self.assertEqual((hot_m, cold_m), (self.mtimes["hot"], 0.0))


class TestRegisteredRootsMemo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name)
        self.scanner = mock.Mock()
        self.scanner.discover.return_value = [
            {"path": str(self.proj), "has_c3": True},
            {"path": str(self.proj / "plain"), "has_c3": False},
        ]
        for name, value in (("_scanner", self.scanner), ("_roots_memo", None),
                            ("_checker", None), ("_reporter", None)):
            patcher = mock.patch.object(mobile_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reused_within_the_ttl(self):
        first = mobile_api._registered_roots()
        self.assertEqual(first, [self.proj.resolve()])
        second = mobile_api._registered_roots()
        self.assertEqual(second, first)
        self.assertEqual(self.scanner.discover.call_count, 1)
        second.clear()  # each caller gets its own list
        self.assertEqual(mobile_api._registered_roots(), first)

    def test_asked_again_after_the_ttl(self):
        with mock.patch.object(mobile_api, "_ROOTS_TTL_S", 0.0):
            mobile_api._registered_roots()
            mobile_api._registered_roots()
        self.assertEqual(self.scanner.discover.call_count, 2)

    def test_another_scanner_is_asked_itself(self):
        mobile_api._registered_roots()
        other = mock.Mock()
        other.discover.return_value = []
        with mock.patch.object(mobile_api, "_scanner", other):
            self.assertEqual(mobile_api._registered_roots(), [])

    def test_init_services_forgets_it(self):
        mobile_api._registered_roots()
        mobile_api.init_services(self.scanner)
        mobile_api._registered_roots()
        self.assertEqual(self.scanner.discover.call_count, 2)


class TestSlowTickServer(unittest.TestCase):
    def test_uvicorn_server_gets_the_slow_loop(self):
        try:
            import uvicorn
        except ImportError:
            self.skipTest("uvicorn is not installed")
        cls = mcp_oracle._slow_tick_server_class(uvicorn.Server)
        self.assertIsNot(cls, uvicorn.Server)
        self.assertTrue(issubclass(cls, uvicorn.Server))

    def test_ticks_once_a_second_on_the_date_header_cadence(self):
        """uvicorn refreshes its Date header when counter % 10 == 0."""
        counters, sleeps = [], []

        class Base:
            async def main_loop(self):
                raise AssertionError("replaced by the subclass")

            async def on_tick(self, counter):
                counters.append(counter)
                return len(counters) >= 4

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        server = mcp_oracle._slow_tick_server_class(Base)()
        with mock.patch.object(mcp_oracle, "asyncio", types.SimpleNamespace(sleep=fake_sleep)):
            asyncio.run(server.main_loop())
        self.assertEqual(counters, [0, 10, 20, 30])
        self.assertEqual(sleeps, [1.0, 1.0, 1.0])

    def test_an_unfamiliar_loop_is_left_alone(self):
        class Base:
            def main_loop(self):
                pass

            async def on_tick(self, counter):
                return True

        self.assertIs(mcp_oracle._slow_tick_server_class(Base), Base)


if __name__ == "__main__":
    unittest.main()
