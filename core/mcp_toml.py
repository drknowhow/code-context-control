"""Shared TOML helpers for the MCP-server sections of IDE config files
(Codex's ``config.toml``, etc.).

These were duplicated — and had quietly drifted — between ``cli/server.py`` and
``cli/hub_server.py``. Consolidating them keeps parse/write behaviour in one
place (the same triplication pattern that once let a CORS bug live in three
servers). The reconciled versions adopt the more robust behaviour from each
copy: ``parse`` strips surrounding quotes from keys, and ``remove`` deletes a
file that becomes empty instead of leaving an empty stub.
"""
from __future__ import annotations

import re
from pathlib import Path


def merge_toml_section(toml_path: Path, section: str, entries: dict,
                       defaults: dict | None = None) -> None:
    """Update owned keys while preserving user settings, subtables and comments.

    Parse and serialize before replacing anything: malformed user TOML is an
    error, never an invitation to silently replace the file.
    """
    import os
    import tempfile

    import tomlkit

    content = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    document = tomlkit.parse(content)
    table = document
    for key in section.split("."):
        if key not in table:
            table[key] = tomlkit.table()
        table = table[key]
        if not hasattr(table, "items"):
            raise ValueError(f"{section} must be a TOML table")
    for key, value in (defaults or {}).items():
        if key not in table:
            table[key] = value
    for key, value in entries.items():
        table[key] = value
    rendered = tomlkit.dumps(document)
    tomlkit.parse(rendered)
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=toml_path.name + ".", dir=toml_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(rendered)
        os.replace(temporary, toml_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _strip_toml_comment(raw: str) -> str:
    """Strip a trailing ``#`` comment, but only when the ``#`` is outside a
    quoted string.

    A naive ``raw.split("#", 1)[0]`` truncates values that legitimately contain
    ``#`` inside quotes (e.g. a Windows path like ``"C:/tools/c#/run.exe"``).
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return raw[:i]
    return raw


def parse_toml_mcp_servers(content: str) -> dict:
    """Parse ``[mcp_servers.<name>]`` sections from TOML content into a dict."""
    servers: dict = {}
    current_server = None

    for raw in content.splitlines():
        line = _strip_toml_comment(raw).strip()
        if not line:
            continue

        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section.startswith("mcp_servers."):
                current_server = section.split(".", 1)[1]
                servers.setdefault(current_server, {})
            else:
                current_server = None
            continue

        if not current_server or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().strip('"')
        value = value.strip()

        if key == "args":
            servers[current_server]["args"] = re.findall(r"[\"']([^\"']*)[\"']", value)
        elif key in ("command", "type"):
            match = re.match(r"^[\"'](.*)[\"']$", value)
            servers[current_server][key] = match.group(1) if match else value
        elif key == "enabled":
            low = value.lower()
            if low.startswith("true"):
                servers[current_server]["enabled"] = True
            elif low.startswith("false"):
                servers[current_server]["enabled"] = False
        else:
            servers[current_server][key] = value

    return servers


def toml_escape_str(value: str) -> str:
    """Escape a string for a double-quoted TOML value (Windows ``\\`` → ``/``)."""
    return value.replace("\\", "/")


def upsert_toml_section(toml_path: Path, section: str, entries: dict) -> None:
    """Add or replace a dotted TOML section in-place."""
    content = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    header = f"[{section}]"

    # When skipping the target section, also skip any of its dotted child
    # subtables (e.g. ``[mcp_servers.c3.env]`` under ``[mcp_servers.c3]``).
    # Otherwise a child table is left orphaned beneath the freshly re-appended
    # section, corrupting the file on re-run.
    child_prefix = f"{header[:-1]}."  # "[mcp_servers.c3]" -> "[mcp_servers.c3."
    lines = content.splitlines()
    new_lines = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skip = True
            continue
        if skip and stripped.startswith("["):
            # A following section header ends the skip — unless it is a dotted
            # child of the target section, which we also drop.
            if not stripped.startswith(child_prefix):
                skip = False
        if not skip:
            new_lines.append(line)

    content = "\n".join(new_lines).rstrip()
    section_lines = [f"\n\n{header}"]
    for key, value in entries.items():
        if isinstance(value, list):
            items = ", ".join(f'"{toml_escape_str(str(item))}"' for item in value)
            section_lines.append(f"{key} = [{items}]")
        elif isinstance(value, bool):
            section_lines.append(f'{key} = {"true" if value else "false"}')
        else:
            section_lines.append(f'{key} = "{toml_escape_str(str(value))}"')
    section_lines.append("")

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(content + "\n".join(section_lines), encoding="utf-8")


def remove_toml_section(toml_path: Path, section: str) -> bool:
    """Remove a dotted TOML section. Deletes the file if it becomes empty.
    Returns True if the section was found and removed."""
    if not toml_path.exists():
        return False
    content = toml_path.read_text(encoding="utf-8")
    header = f"[{section}]"

    lines = content.splitlines()
    new_lines = []
    skip = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skip = True
            removed = True
            continue
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            new_lines.append(line)

    if removed:
        remaining = "\n".join(new_lines).rstrip()
        if remaining:
            toml_path.write_text(remaining + "\n", encoding="utf-8")
        else:
            toml_path.unlink()
    return removed
