"""Regression tests for the CLI health-check subprocess hang.

check_gemini/check_codex/check_claude used subprocess.run(capture_output=True,
timeout=10). That looks safe and is not: when the timeout fires on Windows,
CPython's own handler kills only the DIRECT child and then calls
process.communicate() a SECOND time with no timeout (the _mswindows branch of
run() in Lib/subprocess.py). That join never finishes while a surviving
grandchild still holds the stdout/stderr write-ends, so run() hangs inside its
own timeout handler -- observed wedging the c3-delegate-prewarm thread for 10h
and leaking its two reader threads.

tests/test_cli_smoke.py already documents this footgun and works around it for
test code; _probe_cli_version applies the same convention to production code.
"""
import subprocess
import sys

import pytest

from cli.tools import delegate

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _TimingOutPopen:
    """Popen whose first communicate() times out, like a wedged CLI."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 424242
        self.returncode = None
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.communicate_timeouts = []
        self.alive = True
        _TimingOutPopen.instances.append(self)

    def communicate(self, input=None, timeout=None):
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd="fake --version", timeout=timeout)
        if timeout is None:
            # This is precisely the CPython behaviour we are avoiding.
            raise AssertionError(
                "second communicate() must be bounded, not open-ended"
            )
        return "", ""

    def poll(self):
        return None if self.alive else 0

    def kill(self):
        self.alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def timing_out_popen(monkeypatch):
    _TimingOutPopen.instances = []
    killed = []

    def _fake_kill_tree(proc):
        killed.append(proc)
        proc.alive = False
        proc.returncode = -9

    monkeypatch.setattr(delegate.subprocess, "Popen", _TimingOutPopen)
    monkeypatch.setattr(delegate, "_kill_proc_tree", _fake_kill_tree)
    return killed


# ---------------------------------------------------------------------------
# _probe_cli_version
# ---------------------------------------------------------------------------


def test_probe_returns_version_for_a_responsive_binary():
    """Happy path against a real process: `python --version`."""
    probed = delegate._probe_cli_version(sys.executable, timeout=30)

    assert probed is not None, "probe timed out on a trivially fast command"
    out, err, code = probed
    assert code == 0
    # Older CPython printed the version on stderr; accept either stream.
    assert "Python" in (out or "") + (err or "")


def test_probe_passes_devnull_stdin_and_pipes():
    """stdin must never be inherited -- an interactive CLI would block."""
    captured = {}

    class _Recorder(_TimingOutPopen):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            captured["argv"] = args[0] if args else None
            super().__init__(*args, **kwargs)

        def communicate(self, input=None, timeout=None):
            self.communicate_timeouts.append(timeout)
            self.alive = False
            self.returncode = 0
            return "v1.2.3\n", ""

    real_popen = delegate.subprocess.Popen
    delegate.subprocess.Popen = _Recorder
    try:
        probed = delegate._probe_cli_version("somecli", timeout=10)
    finally:
        delegate.subprocess.Popen = real_popen

    assert probed == ("v1.2.3", "", 0)
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE


def test_probe_timeout_kills_the_tree_and_bounds_the_second_communicate(
    timing_out_popen,
):
    """The whole point: no open-ended communicate(), and a TREE kill."""
    killed = timing_out_popen

    probed = delegate._probe_cli_version("hangingcli", timeout=1)

    assert probed is None, "a timed-out probe must report timeout, not a version"

    proc = _TimingOutPopen.instances[-1]
    # Two communicate() calls: the bounded first, and a bounded reap.
    assert len(proc.communicate_timeouts) == 2
    assert proc.communicate_timeouts[0] == 1
    assert proc.communicate_timeouts[1] is not None, (
        "second communicate() was open-ended -- this is the 10h hang"
    )
    # Tree kill, not Popen.kill(): grandchildren hold the pipe write-ends.
    assert killed == [proc], "expected _kill_proc_tree, not a bare kill()"
    # Pipes closed regardless of which way we left.
    assert proc.stdout.closed and proc.stderr.closed


def test_probe_does_not_leak_reader_threads_on_timeout(timing_out_popen):
    """A wedged probe must not accumulate handles across repeated calls."""
    for _ in range(5):
        assert delegate._probe_cli_version("hangingcli", timeout=1) is None

    assert len(_TimingOutPopen.instances) == 5
    for proc in _TimingOutPopen.instances:
        assert proc.stdout.closed and proc.stderr.closed
        assert not proc.alive


# ---------------------------------------------------------------------------
# The health checks that the prewarm thread calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "check_name,cli_name",
    [("check_gemini", "gemini"), ("check_codex", "codex"), ("check_claude", "claude")],
)
def test_health_checks_report_timeout_without_hanging(
    check_name, cli_name, timing_out_popen, monkeypatch
):
    monkeypatch.setattr(delegate, "_which", lambda name: f"/usr/bin/{name}")

    result = getattr(delegate, check_name)()

    assert result["status"] == "timeout"
    assert cli_name in result["detail"]


@pytest.mark.parametrize(
    "check_name,flag",
    [
        ("check_gemini", "_gemini_available"),
        ("check_codex", "_codex_available"),
        ("check_claude", "_claude_available"),
    ],
)
def test_health_checks_report_ok_and_set_availability(
    check_name, flag, monkeypatch
):
    monkeypatch.setattr(delegate, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        delegate, "_probe_cli_version", lambda exe, timeout=10: ("1.2.3", "", 0)
    )

    result = getattr(delegate, check_name)()

    assert result == {"status": "ok", "version": "1.2.3"}
    assert getattr(delegate, flag) is True


@pytest.mark.parametrize(
    "check_name", ["check_gemini", "check_codex", "check_claude"]
)
def test_health_checks_report_nonzero_exit_as_error(check_name, monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        delegate, "_probe_cli_version", lambda exe, timeout=10: ("", "boom", 3)
    )

    result = getattr(delegate, check_name)()

    assert result["status"] == "error"
    assert result["detail"] == "boom"


@pytest.mark.parametrize(
    "check_name", ["check_gemini", "check_codex", "check_claude"]
)
def test_health_checks_report_not_installed(check_name, monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: None)

    result = getattr(delegate, check_name)()

    assert result["status"] == "not_installed"


def test_no_health_check_uses_subprocess_run():
    """subprocess.run(timeout=) is the footgun; keep it out of these paths."""
    import inspect

    for name in ("check_gemini", "check_codex", "check_claude"):
        src = inspect.getsource(getattr(delegate, name))
        assert "subprocess.run" not in src, (
            f"{name} went back to subprocess.run -- see _probe_cli_version"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
