"""Coverage for `c3 upgrade`, version comparison, and the VersionCheckAgent nudge.

All network (PyPI) and subprocess (pip) calls are mocked — these tests never
reach the network or mutate the environment.
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import c3 as c3mod  # noqa: E402
from services.agents import VersionCheckAgent  # noqa: E402
from services.notifications import NotificationStore  # noqa: E402


class TestVersionTuple(unittest.TestCase):
    def test_ordering(self):
        self.assertLess(c3mod._version_tuple("2.35.0"), c3mod._version_tuple("2.36.0"))
        self.assertEqual(c3mod._version_tuple("2.36.0"), c3mod._version_tuple("2.36.0"))
        self.assertLess(c3mod._version_tuple("2.9.0"), c3mod._version_tuple("2.10.0"))
        self.assertGreater(c3mod._version_tuple("2.36.1"), c3mod._version_tuple("2.36.0"))

    def test_tolerates_garbage(self):
        # Non-numeric suffixes degrade gracefully rather than raising.
        self.assertEqual(c3mod._version_tuple("2.36.0rc1"), (2, 36, 0))
        self.assertEqual(c3mod._version_tuple(""), (0,))


class TestCmdUpgrade(unittest.TestCase):
    def _run(self, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            c3mod.cmd_upgrade(SimpleNamespace(**kwargs))
        return buf.getvalue()

    def test_check_reports_update_without_installing(self):
        with mock.patch.object(c3mod, "_latest_pypi_version", return_value="999.0.0"), \
             mock.patch("subprocess.run") as run:
            out = self._run(check=True)
        self.assertIn("Update available", out)
        run.assert_not_called()

    def test_up_to_date_returns_early(self):
        with mock.patch.object(c3mod, "_latest_pypi_version", return_value="0.0.1"), \
             mock.patch("subprocess.run") as run:
            out = self._run(check=False)
        self.assertIn("up to date", out)
        run.assert_not_called()

    def test_source_install_advises_git_pull(self):
        with mock.patch.object(c3mod, "_latest_pypi_version", return_value="999.0.0"), \
             mock.patch.object(c3mod, "_installed_distribution", return_value=None), \
             mock.patch("subprocess.run") as run:
            out = self._run(check=False)
        self.assertIn("git pull", out)
        run.assert_not_called()

    def test_pip_install_invoked_when_installed(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(c3mod, "_latest_pypi_version", return_value="999.0.0"), \
             mock.patch.object(c3mod, "_installed_distribution", return_value=object()), \
             mock.patch.object(c3mod, "_is_editable_install", return_value=False), \
             mock.patch("subprocess.run", return_value=fake) as run:
            self._run(check=False)
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertIn("-U", argv)
        self.assertIn("code-context-control[tui]", argv)
        self.assertIn("install", argv)


class TestVersionCheckAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        (self.proj / ".c3").mkdir()
        self.notifs = NotificationStore(str(self.proj))

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, current):
        return VersionCheckAgent(self.notifs, current_version=current,
                                 project_path=str(self.proj), enabled=False)

    def test_notifies_when_newer(self):
        agent = self._agent("2.35.0")
        with mock.patch.object(agent, "_fetch_latest", return_value="2.36.0"):
            agent.check()
        self.assertIn("Update available", [n["title"] for n in self.notifs.get_history()])

    def test_no_notify_when_current(self):
        agent = self._agent("2.36.0")
        with mock.patch.object(agent, "_fetch_latest", return_value="2.36.0"):
            agent.check()
        self.assertEqual(self.notifs.get_history(), [])

    def test_throttled_second_call_skips_fetch(self):
        agent = self._agent("2.35.0")
        with mock.patch.object(agent, "_fetch_latest", return_value="2.36.0") as fetch:
            agent.check()  # first call fetches and records timestamp
            agent.check()  # within the 24h window — must not fetch again
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
