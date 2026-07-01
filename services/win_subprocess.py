"""Windows batch-shim-safe subprocess launching (CVE-2024-24576 / "BatBadBut").

Root cause of the C3 "ghost file" epidemic
-------------------------------------------
Many spawn sites resolve a CLI name (``claude``, ``gemini``, ``codex``, ``aider``)
via :func:`shutil.which`, which on Windows returns a ``.cmd``/``.bat`` *shim*
(e.g. ``…\\npm\\gemini.CMD``). Launching a batch file with an argv **list** makes
Windows run it through an implicit ``cmd.exe /c <command-line>``. Python builds
that command line with :func:`subprocess.list2cmdline`, whose ``\\"`` quote
escaping follows the MSVCRT / ``CommandLineToArgvW`` convention — which **cmd.exe
does not honour**. An *odd* number of ``"`` characters anywhere in an argument
therefore desyncs cmd.exe's quote state, and any ``>`` / ``<`` / ``&`` / ``|`` that
follows is interpreted as a real shell redirect/operator. The redirect target
becomes a 0-byte file in the current directory — a "ghost".

This is exactly why prompt / diff / code text carrying return annotations
(``-> tuple[int, str]``), pip specifiers (``flask>=3.0.0``), or line-reference
markers (``> L88``) spawned files named ``tuple[int``, ``3.0.0``, ``L88`` etc.
The running interpreter (CPython 3.14) does **not** neutralise this.

The fix
-------
:func:`harden_win_argv` rewrites the invocation, on Windows only and only when
``argv[0]`` resolves to a ``.cmd``/``.bat`` shim, into an explicit
``cmd.exe /d /s /c "<line>"`` command **string** with cmd.exe-correct quoting:
every argument is wrapped in double quotes and every embedded ``"`` is doubled
(``""``) — cmd.exe's own escaping, which keeps the quote state perfectly
balanced so no metacharacter can ever escape a quoted region. It is returned as
a single string so that Python does not re-apply ``list2cmdline``. POSIX and
non-batch executables are returned unchanged.

Doubling ``"`` -> ``""`` is also the correct escaping for the *downstream* argv
parser (``CommandLineToArgvW`` treats ``""`` inside quotes as one literal quote),
so the child CLI still receives the intended argument.
"""

from __future__ import annotations

import os
import shutil
import sys

__all__ = ["harden_win_argv", "is_batch_shim"]

# Extensions Windows executes through cmd.exe (implicit ``cmd /c``).
_BATCH_EXTS = (".cmd", ".bat")


def is_batch_shim(executable: str) -> bool:
    """Return True if *executable* is (or resolves to) a Windows ``.cmd``/``.bat``.

    Accepts a bare command name (resolved via PATH) or a full path. Always False
    off Windows.
    """
    if sys.platform != "win32" or not executable:
        return False
    low = executable.lower()
    if low.endswith(_BATCH_EXTS):
        return True
    # Bare name or extensionless path: resolve through PATH/PATHEXT.
    if not os.path.splitext(executable)[1]:
        resolved = shutil.which(executable)
        if resolved and resolved.lower().endswith(_BATCH_EXTS):
            return True
    return False


def _cmd_quote(arg: str) -> str:
    """Quote a single argument for cmd.exe so its quote state stays balanced.

    Wrap in double quotes and double every embedded quote. This prevents an odd
    quote count from desyncing cmd.exe and exposing ``> < & |`` as operators, and
    is simultaneously valid MSVCRT quoting for the downstream argv parser.
    """
    return '"' + str(arg).replace('"', '""') + '"'


def harden_win_argv(argv: list[str]):
    """Return a launch target safe from cmd.exe batch-shim re-parse.

    * Off Windows, or when ``argv[0]`` is not a ``.cmd``/``.bat`` shim, returns
      *argv* unchanged (still launched with ``shell=False`` by the caller).
    * Otherwise returns a single command-line **string**
      ``cmd.exe /d /s /c "<quoted line>"`` that the caller passes straight to
      ``subprocess.Popen``/``run`` (with ``shell=False``). Windows forwards the
      string verbatim to cmd.exe, so ``list2cmdline`` never mangles our quoting.

    ``/d`` skips AutoRun, ``/s`` gives predictable outer-quote stripping, ``/c``
    runs then exits.
    """
    if not argv:
        return argv
    if sys.platform != "win32":
        return argv
    if not is_batch_shim(argv[0]):
        return argv
    inner = " ".join(_cmd_quote(a) for a in argv)
    return f'cmd.exe /d /s /c "{inner}"'
