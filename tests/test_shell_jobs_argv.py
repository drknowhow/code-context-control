import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services import shell_jobs as sj

PY = sys.executable
CODE = ("import sys, os; print(sys.stdin.read().upper()); "
        "print('dropped' if 'C3_DROP_ME' not in os.environ else 'kept')")


def _allow(_path):
    return None


class TestSupervisorArgv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.project = root / "proj"
        (self.project / ".c3").mkdir(parents=True)
        self._env = patch.dict(os.environ, {"C3_SHELL_OUT_DIR": str(root / "store"), "C3_DROP_ME": "1"})
        self._env.start()
        self.store = sj.JobStore()

    def tearDown(self):
        self._env.stop()
        for _ in range(20):
            try:
                self._tmp.cleanup()
                break
            except (PermissionError, OSError):
                time.sleep(0.25)

    def _wait(self, job, timeout=40.0):
        deadline = time.monotonic() + timeout
        while True:
            fresh = self.store.load(job) or job
            if fresh.terminal or time.monotonic() > deadline:
                return fresh
            time.sleep(0.2)

    def _stdout(self, job) -> str:
        meta = self.store.output_store.resolve(job.output_id, project_path=job.project_path,
                                               session_id=job.session_id, guard_check=_allow)
        return meta.stream_path("stdout").read_bytes().decode("utf-8")

    def test_argv_stdin_and_env_drop(self):
        payload = {"cmd": "python -c ...", "argv": [PY, "-c", CODE], "stdin_text": "hello\nworld",
                   "env_drop": ["C3_DROP_ME"]}
        job = self.store.start(project_path=str(self.project), session_id="scheduler", cmd="python -c ...",
                               cwd=str(self.project), timeout_s=60, payload=payload)
        job = self._wait(job)
        self.assertEqual((job.status, job.exit_code, job.shell), ("done", 0, "argv"))
        out = self._stdout(job)
        self.assertIn("HELLO\nWORLD", out.replace("\r\n", "\n"))
        self.assertIn("dropped", out)

    def test_cmd_payload_unchanged(self):
        job = self.store.start(project_path=str(self.project), session_id="scheduler", cmd="echo plain",
                               cwd=str(self.project), timeout_s=60, payload={"cmd": "echo plain"})
        job = self._wait(job)
        self.assertEqual((job.status, job.exit_code), ("done", 0))
        self.assertNotEqual(job.shell, "argv")
        self.assertIn("plain", self._stdout(job))


if __name__ == "__main__":
    unittest.main()
