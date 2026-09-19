"""Open a visible terminal window in a directory, running one command.

Extracted from the Hub's ``/api/projects/launch-ide`` (2.143.0) so the
session-resume route spawns through the same code instead of a copy.

``argv`` is a list. A single element is passed through as the command string
exactly as ``launch-ide`` always did (its ``custom`` IDE sends a free-form
command line); several elements are quoted for the platform's shell, so a
caller that builds its command from parts never interpolates them.

Windows tries Windows Terminal (``wt -d <dir> cmd /k ...``) and falls back to
a classic console; macOS drives Terminal.app; Linux tries the usual
emulators in order.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

__all__ = ["spawn_terminal", "command_line"]


def command_line(argv: list[str]) -> str:
    """The command as one POSIX shell string (one element passes through)."""
    if len(argv) == 1:
        return argv[0]
    return " ".join(shlex.quote(a) for a in argv)


def spawn_terminal(cwd, argv: list[str]) -> None:
    """Open a new terminal in ``cwd`` running ``argv``. Raises on failure to
    start anything (Linux: no known terminal emulator)."""
    if not argv:
        raise ValueError("argv is empty")
    path = Path(cwd)
    if sys.platform == "win32":
        try:
            # Windows Terminal needs a full command to run; cmd /k keeps the
            # window open after the command exits.
            subprocess.Popen(
                ["wt", "-d", str(path), "cmd", "/k", *argv],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except FileNotFoundError:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "cmd", "/k", *argv],
                cwd=str(path),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return
    cmd = command_line(argv)
    if sys.platform == "darwin":
        script = (
            f'tell application "Terminal" to do script '
            f'"cd {shlex.quote(str(path))} && {cmd}"'
        )
        subprocess.Popen(["osascript", "-e", script])
        return
    q = shlex.quote(str(path))
    for term_args in [
        ["gnome-terminal", "--", "bash", "-c", f"cd {q} && {cmd}; exec bash"],
        ["xterm", "-e", f"bash -c 'cd {q} && {cmd}; exec bash'"],
        ["konsole", "-e", "bash", "-c", f"cd {q} && {cmd}; exec bash"],
        ["xfce4-terminal", "--command", f"bash -c 'cd {q} && {cmd}; exec bash'"],
    ]:
        try:
            subprocess.Popen(term_args, start_new_session=True)
            return
        except FileNotFoundError:
            continue
    raise FileNotFoundError("no terminal emulator found (gnome-terminal, xterm, konsole, xfce4-terminal)")
