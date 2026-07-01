"""Ghost-file GENERATION regression tests (root-cause, not detector).

The detector tests live in test_ghost_files.py. THIS file proves the *source* of
ghost files is fixed: passing prompt / diff / code TEXT as an argv argument to a
Windows ``.cmd``/``.bat`` CLI shim (claude.CMD, gemini.CMD, codex.CMD, aider) no
longer lets cmd.exe re-interpret ``>`` / ``<`` / ``&`` / ``|`` as redirects and
spawn 0-byte files (CVE-2024-24576 / "BatBadBut").

Mechanism recap (Windows only): launching a batch shim with an argv *list* runs
it via an implicit ``cmd.exe /c``. Python's ``list2cmdline`` escapes quotes with
``\\"`` (MSVCRT convention), which cmd.exe ignores — an odd number of ``"`` in
any argument desyncs cmd.exe's quote state, exposing following metacharacters.
``services.win_subprocess.harden_win_argv`` rewrites the invocation to an
explicit ``cmd.exe /d /s /c`` string with cmd.exe-correct ``""`` quoting.

Every hardened spawn site (cli/tools/delegate.py, services/e2e_benchmark.py,
services/e2e_evaluator.py, services/bench/external/*) routes through that helper,
so exercising the helper against each site's argv shape + the observed adversarial
payloads is the regression guard.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "cli"))

from services.win_subprocess import harden_win_argv, is_batch_shim  # noqa: E402
from hook_ghost_files import sweep_ghost_files  # noqa: E402

WINDOWS = sys.platform == "win32"

# Adversarial payloads drawn from real observed ghost filenames. Each, on the
# broken path, redirects to a 0-byte file named after the token past the ``>``.
ADVERSARIAL_PAYLOADS = [
    'def route(x = "d) -> tuple[int, str]: pass',      # -> tuple[int
    '""" docstring fence then f() -> Optional[str]',   # -> Optional[str]
    'see "ref" > L88 and L90 for context',             # L88 / L90
    'install flask>=3.0.0 and rich>=10',               # 3.0.0 / 10
    'trailing metachars `word` and 3.0.0` and $null`', # backtick leaks
    'braces {new: dict} and paren (commit 9d4e2aa)',   # {new / 9d4e2aa)
    'redirect a | b & c > out < in ^ caret',           # every operator
    'percent %TEMP% bang !VAR! literal survive',       # env-expansion chars
]

# argv shapes matching each fixed spawn site (text argument marked <PAYLOAD>).
SITE_ARGV_SHAPES = {
    "delegate_claude":  ["-p", "<PAYLOAD>", "--output-format", "text"],
    "delegate_gemini":  ["-p", "<PAYLOAD>", "--output-format", "json",
                         "--approval-mode", "yolo"],
    "delegate_codex":   ["exec", "-m", "gpt-5.3-codex-spark", "--sandbox",
                         "read-only", "<PAYLOAD>"],
    "benchmark_claude": ["-p", "<PAYLOAD>", "--output-format", "json",
                         "--permission-mode", "bypassPermissions"],
    "evaluator_judge":  ["-p", "<PAYLOAD>", "--output-format", "text"],
    "bench_aider":      ["--model", "gpt-4o-mini", "--yes-always",
                         "--message", "<PAYLOAD>"],
}


def _make_neutral_shim(directory: Path, name: str = "fake_cli.cmd") -> Path:
    """A .cmd that counts its args and NEVER echoes the payload (a real CLI does
    not echo the prompt back through cmd.exe), isolating launch-time re-parse."""
    shim = directory / name
    shim.write_text(
        "@echo off\r\nset /a n=0\r\n:loop\r\n"
        'if "%~1"=="" goto end\r\n'
        "set /a n+=1\r\nshift\r\ngoto loop\r\n:end\r\necho RAN n=%n%\r\n",
        encoding="ascii",
    )
    return shim


def _run(target, cwd: Path):
    proc = subprocess.Popen(
        target, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, cwd=str(cwd),
    )
    out, err = proc.communicate(timeout=20)
    return out, err


class TestHelperPassthrough(unittest.TestCase):
    """Pure-logic tests — run on every platform."""

    def test_empty_argv_unchanged(self):
        self.assertEqual(harden_win_argv([]), [])

    def test_non_batch_exe_unchanged(self):
        argv = ["python", "-c", 'print("x")']
        # A .exe / bare interpreter is never wrapped: returned list is identical.
        self.assertEqual(harden_win_argv(argv), argv)

    @unittest.skipIf(WINDOWS, "POSIX-only: no batch shims exist off Windows")
    def test_posix_always_passthrough(self):
        argv = ["gemini.cmd", "-p", 'x -> tuple[int]']
        self.assertEqual(harden_win_argv(argv), argv)
        self.assertFalse(is_batch_shim("gemini.cmd"))

    @unittest.skipUnless(WINDOWS, "batch-shim detection is Windows-only")
    def test_batch_shim_detected_and_wrapped(self):
        with tempfile.TemporaryDirectory() as td:
            shim = _make_neutral_shim(Path(td))
            self.assertTrue(is_batch_shim(str(shim)))
            target = harden_win_argv([str(shim), "-p", 'x -> tuple[int]'])
            self.assertIsInstance(target, str)
            self.assertIn("cmd.exe /d /s /c", target)


@unittest.skipUnless(WINDOWS, "ghost generation is a Windows cmd.exe batch-shim issue")
class TestNoGhostGeneration(unittest.TestCase):
    """End-to-end: every site shape x every adversarial payload -> zero ghosts."""

    def test_control_unhardened_path_can_ghost(self):
        """Sanity: the OLD plain-list path DOES create ghosts on this box, so the
        fix is load-bearing. If a future interpreter neutralizes it, skip rather
        than fail (the fix is still correct)."""
        produced_any = False
        with tempfile.TemporaryDirectory() as td:
            shim = _make_neutral_shim(Path(td))
            for i, payload in enumerate(ADVERSARIAL_PAYLOADS):
                cwd = Path(td) / f"ctl{i}"
                cwd.mkdir()
                _run([str(shim), "-p", payload, "--out", "x"], cwd)
                if any(cwd.iterdir()):
                    produced_any = True
        if not produced_any:
            self.skipTest("interpreter already neutralizes batch re-parse")
        self.assertTrue(produced_any)

    def test_hardened_path_never_ghosts(self):
        with tempfile.TemporaryDirectory() as td:
            shim = _make_neutral_shim(Path(td))
            for site, shape in SITE_ARGV_SHAPES.items():
                for i, payload in enumerate(ADVERSARIAL_PAYLOADS):
                    cwd = Path(td) / f"{site}_{i}"
                    cwd.mkdir()
                    argv = [str(shim)] + [
                        payload if tok == "<PAYLOAD>" else tok for tok in shape
                    ]
                    _run(harden_win_argv(argv), cwd)
                    stray = sorted(p.name for p in cwd.iterdir())
                    self.assertEqual(
                        stray, [],
                        f"[{site}] payload {payload!r} created ghost(s): {stray}",
                    )

    def test_batbadbut_injection_neutralized(self):
        """The classic argument-injection variant must not run the injected
        command (which would both inject AND drop a ghost)."""
        with tempfile.TemporaryDirectory() as td:
            shim = _make_neutral_shim(Path(td))
            cwd = Path(td) / "inj"
            cwd.mkdir()
            payload = 'ok" & echo pwned> pwned.txt & rem '
            _run(harden_win_argv([str(shim), "-p", payload]), cwd)
            self.assertFalse(
                (cwd / "pwned.txt").exists(),
                "cmd.exe injection produced pwned.txt — hardening failed",
            )
            self.assertEqual(sorted(p.name for p in cwd.iterdir()), [])


class TestSweepLibraryFunction(unittest.TestCase):
    """The importable sweeper background agents use to self-clean the root."""

    def test_sweep_removes_ghosts_keeps_real_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tuple[int").touch()          # ghost (partial annotation)
            (root / "3.0.0").touch()               # ghost (version leak)
            (root / "L88").touch()                 # ghost (line-ref token)
            (root / "real_module.py").write_text("x = 1\n", encoding="utf-8")
            (root / "README.md").write_text("# hi\n", encoding="utf-8")

            deleted = sweep_ghost_files(root)

            self.assertIn("tuple[int", deleted)
            self.assertIn("3.0.0", deleted)
            self.assertIn("L88", deleted)
            self.assertTrue((root / "real_module.py").exists())
            self.assertTrue((root / "README.md").exists())

    def test_sweep_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(sweep_ghost_files(Path(td)), [])

    def test_sweep_accepts_str_path_and_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(sweep_ghost_files(td), [])
        # Non-existent path must not raise.
        self.assertEqual(sweep_ghost_files(str(Path(td) / "gone")), [])


if __name__ == "__main__":
    unittest.main()
