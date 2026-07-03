import tempfile
import unittest
from pathlib import Path

from services.memory_queue import MemoryQueue


class TestMemoryQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.queue = MemoryQueue(str(self.project))

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_creates_pending_job(self):
        job = self.queue.enqueue("session_digest", "20260703_120000", {"decisions": ["x"]})
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(job["kind"], "session_digest")
        pending = self.queue.claim_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["job_id"], job["job_id"])

    def test_enqueue_same_session_overwrites_not_duplicates(self):
        first = self.queue.enqueue("session_digest", "sid-1", {"n": 1})
        second = self.queue.enqueue("session_digest", "sid-1", {"n": 2})
        pending = self.queue.claim_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["n"], 2)
        # job identity and creation time survive the overwrite
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(second["created_at"], first["created_at"])

    def test_reenqueue_after_done_reopens_job(self):
        job = self.queue.enqueue("session_digest", "sid-1", {})
        self.queue.mark(job, "done", tier_used="cloud")
        self.assertEqual(self.queue.claim_pending(), [])
        self.queue.enqueue("session_digest", "sid-1", {"fresh": True})
        pending = self.queue.claim_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 0)

    def test_mark_pending_counts_attempt(self):
        job = self.queue.enqueue("session_digest", "sid-1", {})
        job = self.queue.mark(job, "pending", error="ollama down")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["last_error"], "ollama down")
        job = self.queue.mark(job, "pending", error="still down")
        self.assertEqual(job["attempts"], 2)

    def test_claim_skips_terminal_states(self):
        for sid, status in (("a", "done"), ("b", "done_degraded"), ("c", "failed")):
            job = self.queue.enqueue("session_digest", sid, {})
            self.queue.mark(job, status)
        live = self.queue.enqueue("session_digest", "d", {})
        pending = self.queue.claim_pending()
        self.assertEqual([j["session_id"] for j in pending], [live["session_id"]])

    def test_atomic_write_leaves_no_tmp_files(self):
        self.queue.enqueue("session_digest", "sid-1", {"payload": "x" * 500})
        leftovers = list(self.queue.dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_session_id_is_sanitized_for_filenames(self):
        job = self.queue.enqueue("session_digest", "bad/sid:with*chars", {})
        self.assertEqual(len(self.queue.claim_pending()), 1)
        self.assertEqual(job["session_id"], "bad/sid:with*chars")

    def test_prune_removes_only_old_terminal_jobs(self):
        import os
        import time
        done = self.queue.enqueue("session_digest", "old-done", {})
        self.queue.mark(done, "done")
        pending = self.queue.enqueue("session_digest", "old-pending", {})
        old = time.time() - 30 * 86400
        for path in self.queue.dir.glob("*.json"):
            os.utime(path, (old, old))
        removed = self.queue.prune(keep_days=14)
        self.assertEqual(removed, 1)
        remaining = self.queue.claim_pending()
        self.assertEqual([j["session_id"] for j in remaining], [pending["session_id"]])


if __name__ == "__main__":
    unittest.main()
