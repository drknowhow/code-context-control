"""Discovery API rate limiting + audit logging (#33).

A leaked Bearer token previously allowed unbounded tool calls, and
``c3_search_cross`` fans out a full runtime per project. These pin the token
bucket's arithmetic, the privacy contract on the audit log (hashes, never
values), and the fail-open behaviour of auditing.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from oracle.services import discovery_audit as da  # noqa: E402


class _Clock:
    """Manual monotonic clock — rate tests must not depend on wall time."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


class TestRateLimiter(unittest.TestCase):
    def test_burst_is_allowed_then_denied(self):
        clock = _Clock()
        rl = da.RateLimiter(per_minute=60, burst=5, clock=clock)
        for i in range(5):
            allowed, _ = rl.check("a")
            self.assertTrue(allowed, f"call {i} should be allowed")
        allowed, retry = rl.check("a")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)

    def test_bucket_refills_over_time(self):
        clock = _Clock()
        rl = da.RateLimiter(per_minute=60, burst=2, clock=clock)
        rl.check("a")
        rl.check("a")
        self.assertFalse(rl.check("a")[0])
        clock.advance(1.0)  # 60/min == 1 token/sec
        self.assertTrue(rl.check("a")[0])

    def test_refill_is_capped_at_burst(self):
        clock = _Clock()
        rl = da.RateLimiter(per_minute=60, burst=3, clock=clock)
        clock.advance(3600)  # an hour of idling must not bank an hour of calls
        for _ in range(3):
            self.assertTrue(rl.check("a")[0])
        self.assertFalse(rl.check("a")[0])

    def test_callers_have_independent_buckets(self):
        clock = _Clock()
        rl = da.RateLimiter(per_minute=60, burst=1, clock=clock)
        self.assertTrue(rl.check("a")[0])
        self.assertFalse(rl.check("a")[0])
        self.assertTrue(rl.check("b")[0], "one caller must not throttle another")

    def test_zero_disables_limiting(self):
        rl = da.RateLimiter(per_minute=0)
        self.assertFalse(rl.enabled)
        for _ in range(1000):
            self.assertTrue(rl.check("a")[0])

    def test_default_burst_derives_from_budget(self):
        self.assertEqual(da.RateLimiter(per_minute=60).burst, 15)
        self.assertEqual(da.RateLimiter(per_minute=4).burst, 5)  # floor

    def test_retry_after_shrinks_as_bucket_refills(self):
        clock = _Clock()
        rl = da.RateLimiter(per_minute=60, burst=1, clock=clock)
        rl.check("a")
        _, first = rl.check("a")
        clock.advance(0.5)
        _, second = rl.check("a")
        self.assertLess(second, first)


class TestCallerIdentity(unittest.TestCase):
    def test_token_is_never_stored_verbatim(self):
        token = "super-secret-bearer-token"
        cid = da.caller_id(token, "127.0.0.1")
        self.assertNotIn(token, cid)
        self.assertTrue(cid.startswith("key:"))

    def test_same_token_is_stable_across_addresses(self):
        self.assertEqual(da.caller_id("t", "10.0.0.1"), da.caller_id("t", "10.0.0.2"))

    def test_distinct_tokens_differ(self):
        self.assertNotEqual(da.caller_id("a", None), da.caller_id("b", None))

    def test_falls_back_to_address_then_anon(self):
        self.assertEqual(da.caller_id(None, "1.2.3.4"), "addr:1.2.3.4")
        self.assertEqual(da.caller_id(None, None), "anon")


class TestArgsFingerprint(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        self.assertEqual(da.args_fingerprint({"a": 1, "b": 2}),
                         da.args_fingerprint({"b": 2, "a": 1}))

    def test_different_values_differ(self):
        self.assertNotEqual(da.args_fingerprint({"q": "x"}),
                            da.args_fingerprint({"q": "y"}))

    def test_unserializable_args_do_not_raise(self):
        self.assertTrue(da.args_fingerprint({"f": object()}))


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_record_writes_one_jsonl_line(self):
        da.record("c3_search", caller="key:abc", args={"q": "x"},
                  duration_ms=12.34, base_dir=self.dir)
        lines = da.audit_path(self.dir).read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["tool"], "c3_search")
        self.assertEqual(entry["caller"], "key:abc")
        self.assertEqual(entry["duration_ms"], 12.3)
        self.assertEqual(entry["status"], "ok")

    def test_argument_values_never_reach_the_log(self):
        secret = "C:/clients/acme/salaries.csv"
        da.record("c3_read", caller="key:abc", args={"path": secret},
                  base_dir=self.dir)
        raw = da.audit_path(self.dir).read_text("utf-8")
        self.assertNotIn(secret, raw)
        self.assertNotIn("acme", raw)
        self.assertIn("args_hash", raw)

    def test_records_append_rather_than_replace(self):
        for i in range(3):
            da.record(f"tool{i}", caller="key:abc", base_dir=self.dir)
        self.assertEqual(
            len(da.audit_path(self.dir).read_text("utf-8").splitlines()), 3)

    def test_read_recent_is_newest_first_and_limited(self):
        for i in range(5):
            da.record(f"tool{i}", caller="key:abc", base_dir=self.dir)
        recent = da.read_recent(limit=2, base_dir=self.dir)
        self.assertEqual([e["tool"] for e in recent], ["tool4", "tool3"])

    def test_read_recent_on_missing_file_is_empty(self):
        self.assertEqual(da.read_recent(base_dir=self.dir), [])

    def test_read_recent_skips_corrupt_lines(self):
        da.record("good", caller="key:abc", base_dir=self.dir)
        with da.audit_path(self.dir).open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual([e["tool"] for e in da.read_recent(base_dir=self.dir)],
                         ["good"])

    def test_record_never_raises_on_unwritable_dir(self):
        bad = self.dir / "a-file"
        bad.write_text("x", "utf-8")
        da.record("t", caller="c", base_dir=bad / "nested")  # must not raise


class TestServerWiring(unittest.TestCase):
    """Config surface + which routes the limiter actually covers."""

    def test_config_defaults_present(self):
        from oracle.config import DEFAULTS
        self.assertEqual(DEFAULTS["api_rate_limit_per_min"], 60)
        self.assertTrue(DEFAULTS["api_audit_enabled"])

    def test_only_tool_executing_paths_are_throttled(self):
        source = (REPO_ROOT / "oracle" / "oracle_server.py").read_text("utf-8")
        block = source.split("_RATE_LIMITED_PATHS = ", 1)[1].split("\n", 1)[0]
        self.assertIn("/api/discovery/call", block)
        self.assertNotIn("/api/discovery/tools\"", block)
        self.assertNotIn("openapi", block)


if __name__ == "__main__":
    unittest.main()
