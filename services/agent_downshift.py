"""Give a native Claude Code subagent a model when the caller left it blank.

Measured 2026-09-14 over ~/.claude/projects transcripts since 2026-07-01: 963
of 991 ``Agent`` calls had no ``model``, and on Claude Code 2.1.270 such a call
runs on the PARENT's model — ``Explore`` included (a probe with a Sonnet parent
put all of an Explore run on Sonnet). So a Fable or Opus session pays its own
rate for every search-and-summarise branch it spawns.

The same probe proved the lever: a PreToolUse *command* hook that returns
``hookSpecificOutput.updatedInput`` with ``model`` set changes the model the
subagent runs on (``opus`` appeared in ``modelUsage`` only with the hook).

Policy (``resolve``), all from ``.c3/config.json → delegate``:

- ``agent_downshift``: ``"one_down"`` (default) — one tier below the parent,
  Fable → Opus → Sonnet; ``"off"``; or a fixed alias (``"sonnet"``) used only
  when it is below the parent.
- ``agent_downshift_floor``: ``"sonnet"``. Never below it: on the delegate
  evals Haiku took 28 turns where Sonnet took 8 on a lookup, and missed
  subtle specs Sonnet got right. So a Sonnet parent is left alone.
- ``agent_downshift_skip``: ``["fork", "Plan"]``. A fork ignores ``model``;
  planning is where the parent's strength is the point.
- ``agent_models``: ``{subagent_type: alias}`` — an explicit per-type choice,
  applied whenever it is below the parent (or the parent is unknown).

Never touched: a call that names a model, and a subagent whose definition
(``.claude/agents/*.md``, project or user) sets ``model:``. Plugin agents
(``plugin:name``) are left alone — their definitions are not ours to read.

The parent model is not in any hook payload (checked: SessionStart,
UserPromptSubmit and PreToolUse carry none). It is read from the transcript
(the last assistant row's ``message.model``), else the last model this project
saw in the past 14 days, else Claude settings' ``model``; unknown means no
change.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

TIERS = ("haiku", "sonnet", "opus", "fable")  # cheapest first
SUBAGENT_TOOLS = ("Agent", "Task")
DEFAULT_SKIP = ("fork", "Plan")
STATE_FILE = "agent_downshift_state.json"
_STATE_MAX_AGE = timedelta(days=14)
_TRANSCRIPT_TAIL = 512_000


def rank(model) -> int | None:
    """0 haiku … 3 fable, from an alias or a full id; None when not a tier."""
    m = str(model or "").strip().lower()
    for i in range(len(TIERS) - 1, -1, -1):
        if TIERS[i] in m:
            return i
    return None


def _alias(value) -> str:
    """The Agent tool's alias for a tier name or a full model id."""
    r = rank(value)
    return TIERS[r] if r is not None else ""


def resolve(subagent_type: str, parent_model: str, cfg: dict) -> tuple[str, str]:
    """``(alias_or_empty, reason)`` for one Agent call with no ``model``."""
    mode = str(cfg.get("agent_downshift", "one_down") or "off").strip().lower()
    if mode in ("off", "false", "0", "none", "inherit"):
        return "", "off"
    stype = str(subagent_type or "general-purpose")
    skip = cfg.get("agent_downshift_skip")
    skip = list(DEFAULT_SKIP) if skip is None else [str(s) for s in skip]
    if stype in skip:
        return "", "skipped_type"
    parent = rank(parent_model)
    overrides = cfg.get("agent_models") or {}
    if isinstance(overrides, dict) and stype in overrides:
        target = _alias(overrides[stype])
        if not target:
            return "", "override_inherit"
        if parent is not None and rank(target) >= parent:
            return "", "not_a_downshift"
        return target, "override"
    if parent is None:
        return "", "parent_unknown"
    if mode == "one_down":
        wanted = parent - 1
    else:
        fixed = _alias(mode)
        if not fixed:
            return "", "bad_config"
        wanted = rank(fixed)
    floor = rank(_alias(cfg.get("agent_downshift_floor", "sonnet")) or "sonnet")
    wanted = max(wanted, floor)
    if wanted >= parent:
        return "", "at_floor"
    return TIERS[wanted], mode if mode != "one_down" else "one_down"


# ── Parent model ────────────────────────────────────────────────────────────


def transcript_model(path) -> str:
    """The model of the latest assistant row in a Claude Code transcript."""
    try:
        p = Path(str(path))
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > _TRANSCRIPT_TAIL:
                fh.seek(size - _TRANSCRIPT_TAIL)
            tail = fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    for line in reversed(tail.splitlines()):
        if '"model"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") if isinstance(row, dict) else None
        model = msg.get("model") if isinstance(msg, dict) else None
        if row.get("type") == "assistant" and rank(model) is not None:
            return str(model)
    return ""


def _state_path(project) -> Path:
    return Path(project) / ".c3" / STATE_FILE


def remember_parent(project, model: str) -> None:
    try:
        path = _state_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"last_parent_model": model,
                                   "seen_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _remembered(project) -> str:
    try:
        data = json.loads(_state_path(project).read_text(encoding="utf-8"))
        seen = datetime.fromisoformat(str(data.get("seen_at")))
        if datetime.now(timezone.utc) - seen <= _STATE_MAX_AGE:
            return str(data.get("last_parent_model") or "")
    except (OSError, ValueError, TypeError):
        pass
    return ""


def _settings_model(project) -> str:
    for p in (Path(project) / ".claude" / "settings.local.json", Path(project) / ".claude" / "settings.json",
              Path.home() / ".claude" / "settings.json"):
        try:
            model = json.loads(p.read_text(encoding="utf-8")).get("model")
        except (OSError, ValueError, AttributeError):
            continue
        if rank(model) is not None:
            return str(model)
    return ""


def parent_model(payload: dict, project) -> tuple[str, str]:
    """``(model, source)``: transcript, remembered, settings, or ('', 'unknown')."""
    model = transcript_model(payload.get("transcript_path") or "")
    if model:
        remember_parent(project, model)
        return model, "transcript"
    model = _remembered(project)
    if model:
        return model, "remembered"
    model = _settings_model(project)
    if model:
        return model, "settings"
    return "", "unknown"


# ── Agent definitions ───────────────────────────────────────────────────────


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip().lower()] = value.strip().strip("'\"")
    return out


def definition_sets_model(subagent_type: str, project) -> bool:
    """True when a project or user agent definition for this type pins a model."""
    name = str(subagent_type or "")
    if not name:
        return False
    for folder in (Path(project) / ".claude" / "agents", Path.home() / ".claude" / "agents"):
        try:
            files = sorted(folder.glob("*.md"))
        except OSError:
            continue
        for f in files:
            try:
                meta = _frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if (meta.get("name") or f.stem) != name:
                continue
            model = meta.get("model", "").strip().lower()
            return bool(model) and model != "inherit"
    return False
