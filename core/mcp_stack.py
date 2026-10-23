"""Whether the installed fastmcp and mcp packages can load together.

fastmcp 3.x needs ``mcp<2`` and 4.x needs ``mcp>=2``, and pip installing some
other tool can move mcp out from under fastmcp anyway. Reads installed
metadata without importing either package, so the MCP server and the
SessionStart hook can name the mismatch and the pip command that repairs it.
"""
from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

# fastmcp-slim declares mcp under these extras; fastmcp 3.2 and earlier
# declare it unconditionally ("").
_EXTRAS = ("", "client", "server", "mcp")


def _packaging():
    """packaging's (Requirement, InvalidRequirement, canonicalize_name), or None."""
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.utils import canonicalize_name
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
            from pip._vendor.packaging.utils import canonicalize_name
        except ImportError:
            return None
    return Requirement, InvalidRequirement, canonicalize_name


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _mcp_requirement(requirement_cls, invalid_requirement, canonicalize_name):
    """The mcp Requirement the installed fastmcp declares, or None."""
    fastmcp = _version("fastmcp")
    if fastmcp is None:
        return None
    # From 3.3 the fastmcp wheel only depends on fastmcp-slim, which carries
    # the mcp pin. A slim left behind at another version is not the code in
    # use, so read it only when the two versions match.
    dists = ["fastmcp"]
    if _version("fastmcp-slim") == fastmcp:
        dists.insert(0, "fastmcp-slim")
    for dist in dists:
        for raw in metadata.requires(dist) or []:
            try:
                req = requirement_cls(raw)
            except invalid_requirement:
                continue
            if canonicalize_name(req.name) != "mcp":
                continue
            if req.marker is None or any(
                    req.marker.evaluate({"extra": e}) for e in _EXTRAS):
                return req
    return None


def _console_python(executable: str) -> str:
    """python.exe in place of pythonw.exe: the repair command needs a console."""
    exe = Path(executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return executable


def problem(executable: str | None = None) -> str | None:
    """Describe a fastmcp/mcp mismatch and how to repair it, or return None.

    None also covers what this cannot judge: fastmcp not installed, no mcp pin
    in its metadata, or no ``packaging`` to read the pin with.
    """
    tools = _packaging()
    if tools is None:
        return None
    req = _mcp_requirement(*tools)
    if req is None:
        return None
    fastmcp = _version("fastmcp")
    mcp = _version("mcp")
    spec = str(req.specifier)
    python = _console_python(executable or sys.executable)
    pip = f'"{python}" -m pip'
    if mcp is None:
        return (f"C3's MCP server cannot start: fastmcp {fastmcp} needs "
                f"mcp{spec}, and mcp is not installed in {sys.prefix}.\n"
                f'Repair: {pip} install "mcp{spec}", then restart the session.')
    if req.specifier.contains(mcp, prereleases=True):
        return None
    too_new = any(s.operator in ("<", "<=") and not s.contains(mcp, prereleases=True)
                  for s in req.specifier)
    lines = [
        f"C3's MCP server cannot start: fastmcp {fastmcp} needs mcp{spec}, "
        f"but mcp {mcp} is installed in {sys.prefix}. Installing another "
        "package that wants a different mcp moves it; pip warns about the "
        "conflict but installs anyway.",
        "Repair with one of:",
    ]
    if too_new:
        lines.append(f"  {pip} install --upgrade fastmcp   "
                     f"(moves fastmcp to the release built for mcp {mcp})")
    lines.append(f'  {pip} install "mcp{spec}"   '
                 f"(moves mcp back to what fastmcp {fastmcp} needs)")
    lines.append(f"Then run {pip} check to see whether another package "
                 "needs the other mcp line, and restart the session.")
    return "\n".join(lines)
