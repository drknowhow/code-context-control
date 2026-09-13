"""Hook command strings that survive whichever shell the host picks.

Codex and Grok Build both run a hook's ``command`` through a shell they choose
at runtime (cmd.exe, PowerShell, pwsh or Git Bash on Windows). A quoted
interpreter path followed by arguments is a parse error in PowerShell unless
it is prefixed with ``&``, cmd.exe expands ``%variables%`` and metacharacters
inside project paths, and Git Bash rewrites ``/c``. An encoded PowerShell
invocation contains no quotes, spaces or ``$`` a host shell could reinterpret,
and stdin still reaches the child as the hook payload.
"""
import base64
import shlex


def posix_command(argv: list) -> str:
    return shlex.join(str(arg) for arg in argv)


def powershell_encoded_command(argv: list) -> str:
    script = "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv) + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded
