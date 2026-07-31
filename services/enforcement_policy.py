"""Enforcement Policy — the user-tunable tool-discipline tier.

This is the knob for LAYER C: how hard C3 pushes the agent toward ``c3_*``
tools. It is deliberately SEPARATE from Access Guard (``services/access_guard``),
which is a security boundary over *paths*. Loosening tool discipline must never
loosen path policy, so nothing in this module is consulted by the access
evaluator and nothing here can widen a ``deny``/``read_only``/``mask`` rule.

Modes
-----
``strict``    native Edit/Write/MultiEdit are hard-denied unless a qualifying
              ``c3_*`` call happened first. The pre-v2.66 behavior.
``advisory``  native writes are allowed with a nudge. The edit ledger still
              captures them — ``hook_edit_ledger`` runs PostToolUse and is
              unaffected by this setting.
``off``       no tool-discipline nudging at all. Access Guard, the credential
              vault guard, and agent locks all still enforce.

Resolution order
----------------
project ``.c3/config.json`` → global ``~/.c3/config.json`` → ``DEFAULT_MODE``.

``DEFAULT_MODE`` is ``strict`` so that an install with no ``enforcement``
section keeps its existing behavior. New modes are written explicitly by
``c3 init`` / ``c3 permissions`` / ``c3 enforce``; nothing is derived at read
time. That is what keeps an upgrade from silently changing how an existing
project behaves.

Provenance (``set_by``)
-----------------------
``tier`` — written as a side effect of choosing a permission tier. A later
tier change may overwrite it.
``user`` — set explicitly via ``c3 enforce``. A tier change leaves it alone,
so an explicit choice is never clobbered by an unrelated command.

Stdlib-only by design: a hook subprocess imports this on every native tool
call, so it must not pull in the service layer.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Modes ───────────────────────────────────────────────────────────────────

MODE_STRICT = "strict"
MODE_ADVISORY = "advisory"
MODE_OFF = "off"
MODES = (MODE_STRICT, MODE_ADVISORY, MODE_OFF)

#: No ``enforcement`` section anywhere ⇒ this. Chosen so upgrading C3 never
#: changes how an existing project behaves (see module docstring).
DEFAULT_MODE = MODE_STRICT

MODE_HELP = {
    MODE_STRICT: "Block native Edit/Write until a c3_* tool runs first "
                 "(maximum ledger fidelity)",
    MODE_ADVISORY: "Allow native Edit/Write with a nudge — ledger still "
                   "captures every change (recommended)",
    MODE_OFF: "No tool-discipline nudging. Access Guard and the vault guard "
              "still enforce",
}

#: Permission tier → the tool-discipline mode that matches what the tier
#: *says* it does. Resolves the contradiction where `permissive` ("all tools
#: pre-approved") still had every native Edit hard-denied by the hook.
TIER_TO_MODE = {
    "read-only": MODE_STRICT,
    "c3-strict": MODE_STRICT,
    "standard": MODE_ADVISORY,
    "permissive": MODE_OFF,
}

SET_BY_TIER = "tier"
SET_BY_USER = "user"

#: Age limit for the "a c3_* tool just ran" signal. Was a hardcoded 600 in
#: hook_pretool_enforce; now tunable because a long single-file refactor can
#: legitimately outrun it and get re-blocked mid-task.
DEFAULT_SIGNAL_TTL_S = 600
_MIN_SIGNAL_TTL_S = 30
_MAX_SIGNAL_TTL_S = 86_400

_SECTION = "enforcement"

#: Tools this policy may govern. A `blocked_tools` override naming anything
#: outside this set is a config error (it would silently no-op otherwise —
#: same reasoning as access_guard's "unknown key ⇒ corrupt" rule).
GOVERNABLE_TOOLS = frozenset({
    "Read", "Grep", "Glob", "FindFiles", "SearchText",
    "Edit", "Write", "MultiEdit",
})

_DEFAULT_BLOCKED = ("Edit", "Write", "MultiEdit")


@dataclass(frozen=True)
class Policy:
    """Resolved tool-discipline policy for one project."""
    mode: str = DEFAULT_MODE
    set_by: str = ""
    signal_ttl_s: int = DEFAULT_SIGNAL_TTL_S
    blocked_tools: frozenset = field(default_factory=lambda: frozenset(_DEFAULT_BLOCKED))
    scope: str = "default"          # project | global | default
    warnings: tuple = ()            # surfaced by the hook dispatcher

    @property
    def blocks_writes(self) -> bool:
        return self.mode == MODE_STRICT

    @property
    def nudges(self) -> bool:
        """False in ``off`` mode — the hook returns before emitting hints."""
        return self.mode != MODE_OFF

    def describe(self) -> str:
        src = {"project": "project config", "global": "global config",
               "default": "default"}.get(self.scope, self.scope)
        by = f", set by {self.set_by}" if self.set_by else ""
        return f"{self.mode} ({src}{by})"


def derive_from_tier(tier: str) -> str:
    """The discipline mode matching a permission tier. Unknown tier → default."""
    return TIER_TO_MODE.get(str(tier or "").strip().lower(), DEFAULT_MODE)


def _config_path(scope: str, project_path: str = ".") -> Path | None:
    if scope == "project":
        return Path(project_path) / ".c3" / "config.json"
    home = os.environ.get("C3_HOME") or os.path.expanduser("~")
    if not home or home == "~":
        return None
    return Path(home) / ".c3" / "config.json"


def _read_section(path: Path | None) -> tuple:
    """(section_dict_or_None, warning). Missing file/section → (None, "")."""
    if path is None or not path.exists():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, (f"{path} is unreadable ({type(exc).__name__}) — "
                      f"tool discipline falls back to '{DEFAULT_MODE}'")
    if not isinstance(data, dict):
        return None, f"{path} root is not an object"
    section = data.get(_SECTION)
    if section is None:
        return None, ""
    if not isinstance(section, dict):
        return None, (f"{path} '{_SECTION}' must be an object — "
                      f"falling back to '{DEFAULT_MODE}'")
    return section, ""


def _coerce(section: dict, scope: str) -> Policy:
    """Build a Policy from a raw config section. Invalid fields fail CLOSED
    (strict) with a warning rather than silently relaxing enforcement."""
    warnings: list = []

    raw_mode = str(section.get("mode") or "").strip().lower()
    if raw_mode not in MODES:
        if raw_mode:
            warnings.append(
                f"enforcement.mode '{raw_mode}' is not one of "
                f"{', '.join(MODES)} — using '{DEFAULT_MODE}'")
        mode = DEFAULT_MODE
    else:
        mode = raw_mode

    ttl = section.get("signal_ttl_s", DEFAULT_SIGNAL_TTL_S)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        warnings.append("enforcement.signal_ttl_s must be an integer — "
                        f"using {DEFAULT_SIGNAL_TTL_S}")
        ttl = DEFAULT_SIGNAL_TTL_S
    if not (_MIN_SIGNAL_TTL_S <= ttl <= _MAX_SIGNAL_TTL_S):
        warnings.append(
            f"enforcement.signal_ttl_s {ttl} outside "
            f"{_MIN_SIGNAL_TTL_S}..{_MAX_SIGNAL_TTL_S} — clamped")
        ttl = max(_MIN_SIGNAL_TTL_S, min(_MAX_SIGNAL_TTL_S, ttl))

    raw_blocked = section.get("blocked_tools")
    if raw_blocked is None:
        blocked = frozenset(_DEFAULT_BLOCKED)
    elif isinstance(raw_blocked, list) and all(isinstance(t, str) for t in raw_blocked):
        unknown = [t for t in raw_blocked if t not in GOVERNABLE_TOOLS]
        if unknown:
            warnings.append(
                "enforcement.blocked_tools names tools this policy does not "
                f"govern ({', '.join(sorted(unknown))}) — using defaults")
            blocked = frozenset(_DEFAULT_BLOCKED)
        else:
            blocked = frozenset(raw_blocked)
    else:
        warnings.append("enforcement.blocked_tools must be a list of tool "
                        "names — using defaults")
        blocked = frozenset(_DEFAULT_BLOCKED)

    set_by = str(section.get("set_by") or "").strip().lower()
    if set_by not in (SET_BY_TIER, SET_BY_USER, ""):
        set_by = ""

    return Policy(mode=mode, set_by=set_by, signal_ttl_s=ttl,
                  blocked_tools=blocked, scope=scope,
                  warnings=tuple(warnings))


def resolve(project_path: str = ".") -> Policy:
    """Effective policy: project → global → default. Never raises."""
    all_warnings: list = []
    for scope in ("project", "global"):
        try:
            section, warn = _read_section(_config_path(scope, project_path))
        except Exception:
            section, warn = None, ""
        if warn:
            all_warnings.append(warn)
        if section is not None:
            policy = _coerce(section, scope)
            if all_warnings:
                policy = Policy(
                    mode=policy.mode, set_by=policy.set_by,
                    signal_ttl_s=policy.signal_ttl_s,
                    blocked_tools=policy.blocked_tools, scope=policy.scope,
                    warnings=tuple(all_warnings) + policy.warnings)
            return policy
    return Policy(warnings=tuple(all_warnings))


def resolve_mode(project_path: str = ".") -> str:
    """Just the mode string — for callers that need nothing else."""
    return resolve(project_path).mode


def resolve_global() -> Policy:
    """The global (``~/.c3``) section alone, ignoring any project override.

    An unset section returns ``mode=""`` so callers can tell "not configured"
    (everything inherits the built-in default) from an explicit choice.
    Never raises.
    """
    try:
        section, warn = _read_section(_config_path("global"))
    except Exception:
        section, warn = None, ""
    if section is None:
        return Policy(mode="", scope="default",
                      warnings=(warn,) if warn else ())
    policy = _coerce(section, "global")
    if warn:
        policy = Policy(mode=policy.mode, set_by=policy.set_by,
                        signal_ttl_s=policy.signal_ttl_s,
                        blocked_tools=policy.blocked_tools, scope=policy.scope,
                        warnings=(warn,) + policy.warnings)
    return policy


# ── Mutation (human surfaces only; callers log to the ledger) ────────────────

def _load_config_data(cfg: Path) -> dict:
    """Read a config file for a mutation. Unreadable JSON is a hard error —
    a blind overwrite would destroy whatever the user had there."""
    if not cfg.exists():
        return {}
    try:
        loaded = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            f"{cfg} is unreadable ({type(exc).__name__}); fix it by hand "
            "before changing enforcement") from exc
    return loaded if isinstance(loaded, dict) else {}


def _write_config(cfg: Path, data: dict) -> None:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.with_name(f"{cfg.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, cfg)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _validate_signal_ttl(signal_ttl_s) -> int:
    """Writes are exact — clamping stays a read-time courtesy for hand-edited
    files; a surface asking for an out-of-range value gets told, not adjusted."""
    try:
        ttl = int(signal_ttl_s)
    except (TypeError, ValueError):
        raise ValueError("signal_ttl_s must be an integer") from None
    if not (_MIN_SIGNAL_TTL_S <= ttl <= _MAX_SIGNAL_TTL_S):
        raise ValueError(
            f"signal_ttl_s {ttl} is outside "
            f"{_MIN_SIGNAL_TTL_S}..{_MAX_SIGNAL_TTL_S}")
    return ttl


def _validate_blocked_tools(blocked_tools) -> list:
    if not isinstance(blocked_tools, (list, tuple)) or \
            not all(isinstance(t, str) for t in blocked_tools):
        raise ValueError("blocked_tools must be a list of tool names")
    unknown = sorted(set(blocked_tools) - GOVERNABLE_TOOLS)
    if unknown:
        raise ValueError(
            "blocked_tools names tools this policy does not govern: "
            + ", ".join(unknown) + " — governable: "
            + ", ".join(sorted(GOVERNABLE_TOOLS)))
    return sorted(set(blocked_tools))


def set_mode(mode: str, project_path: str = ".", *,
             set_by: str = SET_BY_USER, scope: str = "project",
             signal_ttl_s: int | None = None,
             blocked_tools: list | None = None) -> dict:
    """Persist a discipline mode. Returns {"mode", "previous", "changed",
    "scope", "set_by", "path"}. Raises ValueError on a bad mode/scope/field.

    ``set_by=SET_BY_TIER`` will NOT overwrite an existing ``user`` choice —
    picking a permission tier must not silently undo an explicit ``c3 enforce``.
    """
    mode = str(mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(
            f"'{mode}' is not a discipline mode — expected one of: "
            + ", ".join(MODES))
    if scope not in ("project", "global"):
        raise ValueError(f"unknown scope '{scope}' — expected project|global")
    ttl = None if signal_ttl_s is None else _validate_signal_ttl(signal_ttl_s)
    blocked = (None if blocked_tools is None
               else _validate_blocked_tools(blocked_tools))

    cfg = _config_path(scope, project_path)
    if cfg is None:
        raise ValueError("no home directory available for the global scope")

    data = _load_config_data(cfg)
    section = data.get(_SECTION)
    if not isinstance(section, dict):
        section = {}
    previous = str(section.get("mode") or "")
    prior_set_by = str(section.get("set_by") or "").strip().lower()

    # A tier-derived write defers to an explicit user choice.
    if set_by == SET_BY_TIER and prior_set_by == SET_BY_USER:
        return {"mode": previous or DEFAULT_MODE, "previous": previous,
                "changed": False, "scope": scope, "set_by": SET_BY_USER,
                "path": str(cfg), "deferred": True}

    section["mode"] = mode
    section["set_by"] = set_by
    if ttl is not None:
        section["signal_ttl_s"] = ttl
    if blocked is not None:
        section["blocked_tools"] = blocked
    data[_SECTION] = section
    _write_config(cfg, data)

    return {"mode": mode, "previous": previous, "changed": previous != mode,
            "scope": scope, "set_by": set_by, "path": str(cfg),
            "deferred": False}


def set_fields(project_path: str = ".", *, scope: str = "project",
               signal_ttl_s=None, blocked_tools=None) -> dict:
    """Partial update of the non-mode fields. Never touches ``mode``/``set_by``
    (a ttl tweak must not turn a tier-derived choice into a "user" one).

    Refuses to CREATE an enforcement section: ``resolve()`` stops at the first
    scope whose section exists, and a mode-less section coerces to the strict
    default — so writing only a ttl here would silently shadow an inherited
    mode. Set a mode at this scope first.
    """
    if scope not in ("project", "global"):
        raise ValueError(f"unknown scope '{scope}' — expected project|global")
    if signal_ttl_s is None and blocked_tools is None:
        raise ValueError("nothing to set — pass signal_ttl_s or blocked_tools")
    ttl = None if signal_ttl_s is None else _validate_signal_ttl(signal_ttl_s)
    blocked = (None if blocked_tools is None
               else _validate_blocked_tools(blocked_tools))

    cfg = _config_path(scope, project_path)
    if cfg is None:
        raise ValueError("no home directory available for the global scope")

    data = _load_config_data(cfg)
    section = data.get(_SECTION)
    if not (isinstance(section, dict)
            and str(section.get("mode") or "").strip().lower() in MODES):
        raise ValueError(
            f"the {scope} scope has no discipline mode set — pick a mode "
            "first; a partial enforcement section would shadow the "
            "inherited one")

    changed = False
    result: dict = {"scope": scope, "path": str(cfg)}
    if ttl is not None:
        changed = changed or section.get("signal_ttl_s") != ttl
        section["signal_ttl_s"] = ttl
        result["signal_ttl_s"] = ttl
    if blocked is not None:
        changed = changed or sorted(section.get("blocked_tools") or ()) != blocked
        section["blocked_tools"] = blocked
        result["blocked_tools"] = blocked
    data[_SECTION] = section
    _write_config(cfg, data)
    result["changed"] = changed
    return result
