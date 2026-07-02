"""Agent-artifact definitions — what counts as an "agent-affecting" file.

Pure-stdlib, import-light module (safe to import from hooks, which must stay
fast): the pattern table of artifact classes across every IDE C3 knows, path
classification, filesystem discovery, and the pending-signal writer used by
hooks and C3's own writers (install-mcp, claude_md) to attribute changes.

Artifact identity: ``<class>:<name>`` — e.g. ``instructions:CLAUDE.md``,
``settings:.claude/settings.local.json``, ``mcp:.mcp.json``,
``skill:browcontrol``, ``agent:code-reviewer``, ``command:deploy``.
Provider (claude-code/codex/gemini/vscode/cursor) is a field, not part of
the id. v1 scope is project-level only: profiles whose config lives in the
home directory (``config_path_global``) are skipped.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.ide import PROFILES

# Attribution sources for history events / pending signals.
SOURCES = ("c3_edit", "hook", "scan", "install_mcp", "restore")

# Directory-unit classes rooted under .claude/ (Claude Code extensions).
_CLAUDE_DIR_CLASSES = (
    ("skill", ".claude/skills"),
    ("agent", ".claude/agents"),
    ("command", ".claude/commands"),
    ("plugin", ".claude/plugins"),
)

MAX_MEMBERS_PER_UNIT = 200


@dataclass
class ArtifactRef:
    """Classification result for a single path."""
    id: str            # "skill:browcontrol"
    cls: str           # instructions | settings | mcp | skill | agent | command | plugin
    name: str          # "browcontrol", "CLAUDE.md", ".mcp.json"
    provider: str      # "claude-code", "codex", ...
    root: str          # unit root rel path ("CLAUDE.md" or ".claude/skills/browcontrol")
    roles: tuple = ()  # extra roles when one file serves several (gemini settings+mcp)


@dataclass
class ArtifactUnit:
    """A discovered artifact unit with its member files."""
    id: str
    cls: str
    name: str
    provider: str
    root: str
    roles: tuple = ()
    members: list = field(default_factory=list)  # sorted rel paths


def _norm(rel_path: str) -> str:
    """Normalize a project-relative path: forward slashes, no leading ./"""
    p = str(rel_path).replace("\\", "/").lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _build_file_table() -> dict:
    """rel_path -> ArtifactRef for single-file artifacts, from IDE PROFILES.

    Per profile the MCP config is registered before the settings path so a
    file serving both (gemini's .gemini/settings.json) lands in class 'mcp'
    with 'settings' merged into roles. Duplicate paths across profiles keep
    the first provider (dict order: claude-code first).
    """
    table: dict[str, ArtifactRef] = {}

    def _add(rel: Optional[str], cls: str, provider: str) -> None:
        if not rel:
            return
        rel = _norm(rel)
        existing = table.get(rel)
        if existing is not None:
            if cls not in (existing.cls,) + tuple(existing.roles):
                existing.roles = tuple(existing.roles) + (cls,)
            return
        table[rel] = ArtifactRef(
            id=f"{cls}:{rel}", cls=cls, name=rel, provider=provider, root=rel)

    for profile in PROFILES.values():
        if not profile.config_path_global:
            _add(profile.config_path, "mcp", profile.name)
        _add(profile.settings_path, "settings", profile.name)
        _add(profile.instructions_file, "instructions", profile.name)
    _add(".claude/settings.json", "settings", "claude-code")
    return table


_FILE_TABLE = _build_file_table()


def classify_path(rel_path: str) -> Optional[ArtifactRef]:
    """Classify a project-relative path as an artifact (or None).

    Pure string matching — no filesystem access, hook-safe.
    """
    rel = _norm(rel_path)
    if not rel:
        return None

    hit = _FILE_TABLE.get(rel)
    if hit is not None:
        return hit

    for cls, root in _CLAUDE_DIR_CLASSES:
        prefix = root + "/"
        if not rel.startswith(prefix):
            continue
        tail = rel[len(prefix):]
        if not tail:
            return None
        if "/" in tail:
            # Nested: unit is the first-level dir (skills/plugins/agents),
            # except commands where subdirs namespace individual .md files.
            if cls == "command":
                if not tail.endswith(".md"):
                    return None
                name = tail[:-3]
                return ArtifactRef(id=f"{cls}:{name}", cls=cls, name=name,
                                   provider="claude-code", root=rel)
            name = tail.split("/", 1)[0]
            return ArtifactRef(id=f"{cls}:{name}", cls=cls, name=name,
                               provider="claude-code", root=f"{root}/{name}")
        # Direct child file: agents/commands are per-file units (strip .md);
        # a stray file directly under skills/ or plugins/ is not an artifact.
        if cls in ("agent", "command"):
            if not tail.endswith(".md"):
                return None
            name = tail[:-3]
            return ArtifactRef(id=f"{cls}:{name}", cls=cls, name=name,
                               provider="claude-code", root=rel)
        return None
    return None


def _walk_files(root: Path) -> list:
    """All non-symlink files under root (recursive), capped, sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            if fp.is_symlink():
                continue
            out.append(fp)
            if len(out) >= MAX_MEMBERS_PER_UNIT:
                return out
    return out


def discover_units(project_path) -> list:
    """Discover all artifact units present on disk.

    Explicit-root walk, deliberately NOT gitignore-aware: settings.local.json
    is usually gitignored and must still be tracked. Skips symlinks.
    """
    project = Path(project_path)
    units: list[ArtifactUnit] = []

    for rel, ref in _FILE_TABLE.items():
        fp = project / rel
        if fp.is_file() and not fp.is_symlink():
            units.append(ArtifactUnit(id=ref.id, cls=ref.cls, name=ref.name,
                                      provider=ref.provider, root=ref.root,
                                      roles=tuple(ref.roles), members=[rel]))

    for cls, root in _CLAUDE_DIR_CLASSES:
        base = project / root
        if not base.is_dir():
            continue
        if cls in ("skill", "plugin"):
            for child in sorted(base.iterdir()):
                if not child.is_dir() or child.is_symlink():
                    continue
                members = [_norm(str(f.relative_to(project))) for f in _walk_files(child)]
                if members:
                    units.append(ArtifactUnit(
                        id=f"{cls}:{child.name}", cls=cls, name=child.name,
                        provider="claude-code", root=f"{root}/{child.name}",
                        members=members))
        else:  # agent, command — per-file units (recursive for namespacing)
            for f in _walk_files(base):
                rel = _norm(str(f.relative_to(project)))
                ref = classify_path(rel)
                if ref is None:
                    continue
                if ref.root != rel and cls == "agent":
                    # directory-form agent (.claude/agents/foo/**): group once
                    existing = next((u for u in units if u.id == ref.id), None)
                    if existing is not None:
                        if len(existing.members) < MAX_MEMBERS_PER_UNIT:
                            existing.members.append(rel)
                        continue
                    units.append(ArtifactUnit(id=ref.id, cls=ref.cls, name=ref.name,
                                              provider=ref.provider, root=ref.root,
                                              members=[rel]))
                    continue
                units.append(ArtifactUnit(id=ref.id, cls=ref.cls, name=ref.name,
                                          provider=ref.provider, root=ref.root,
                                          members=[rel]))
    return units


def note_pending_write(project_path, rel_path: str, source: str,
                       session_id: str = "", summary: str = "",
                       tool: str = "") -> None:
    """Append a pending capture signal for the artifact scanner. Best-effort:
    swallows every error — a failed signal only means the change is later
    attributed to 'scan' instead of its true source."""
    try:
        base = Path(project_path) / ".c3" / "agent_artifacts"
        base.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "path": _norm(rel_path),
            "source": source if source in SOURCES else "scan",
            "tool": tool,
            "session_id": session_id,
            "summary": summary,
        }, ensure_ascii=False)
        with open(base / "pending.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
