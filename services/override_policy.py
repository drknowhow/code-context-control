"""Override policy — the ``override`` section of ``.c3/config.json``.

Frozen spec: docs/override-requests.md §3.1 (schema), §2 (which layers are
escalatable at all). This module only *resolves* policy; it never writes it.
Mutation is a human surface (``c3 override``, the desktop Settings UI, or the
mobile Guard tab), and ``.c3/**`` is in ``access_guard.BUILTIN_WRITE_DENY`` so
the agent cannot reach the file even with native tools.

Three properties this file exists to hold:

1. **Default off, everywhere.** ``enabled`` and every layer default to
   ``False``. A C3 that has never heard of overrides behaves exactly as it
   does today, and the hot path costs one dict lookup.
2. **Tightening-only merge.** Global (``~/.c3``) and project scopes AND their
   booleans and take the *smaller* number, so a project can never widen what
   global forbids.
3. **Fail closed.** An unparseable, wrong-typed, or unknown-keyed ``override``
   section evaluates as disabled with a loud warning — never as permissive.
   Same rule as ``access``: unknown keys are a hard error precisely so a
   future key can never silently no-op on an older C3.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# ── Layer keys (the `override.layers` schema) ───────────────────────────────
LAYER_DISCIPLINE = "discipline"
LAYER_ACCESS_READONLY = "access_readonly"
LAYER_ACCESS_DENY = "access_deny"
LAYER_ACCESS_BUILTIN = "access_builtin"
LAYER_MASK = "mask"
LAYER_SHELL_WARN = "shell_warn"

#: Declaration order = display order in `c3 override policy`.
LAYER_KEYS = (
    LAYER_DISCIPLINE,
    LAYER_ACCESS_READONLY,
    LAYER_ACCESS_DENY,
    LAYER_ACCESS_BUILTIN,
    LAYER_MASK,
    LAYER_SHELL_WARN,
)

#: Layers whose approval requires the rule glob typed by hand (spec §8/§11).
TYPED_CONFIRM_LAYERS = frozenset({LAYER_ACCESS_DENY, LAYER_ACCESS_BUILTIN})

# ── Gate layers (the `layer` field on a Grant — spec §3.4) ──────────────────
GATE_ACCESS = "access"
GATE_DISCIPLINE = "discipline"
GATE_MASK = "mask"
GATE_SHELL = "shell"
GATE_LAYERS = (GATE_ACCESS, GATE_DISCIPLINE, GATE_MASK, GATE_SHELL)

#: Which gate consults a grant minted for a given layers-key.
GATE_FOR_LAYER_KEY = {
    LAYER_DISCIPLINE: GATE_DISCIPLINE,
    LAYER_ACCESS_READONLY: GATE_ACCESS,
    LAYER_ACCESS_DENY: GATE_ACCESS,
    LAYER_ACCESS_BUILTIN: GATE_ACCESS,
    LAYER_MASK: GATE_MASK,
    LAYER_SHELL_WARN: GATE_SHELL,
}

# ── Stable machine tags — API, do not change (spec §5/§6/§7) ────────────────
TAG_GRANTED = "[c3-override:granted]"
TAG_NOT_ESCALATABLE = "[c3-override:not-escalatable]"
TAG_OFFER = "[c3-override]"

#: The rule token a discipline denial is recorded under. Discipline has no
#: glob, but §4 requires an exact `rule` match, so it needs a stable name.
RULE_DISCIPLINE = "<discipline:native-write-block>"
#: Ditto for the c3_shell soft-warn layer.
RULE_SHELL_WARN = "<shell:soft-warn>"

# ── Hard ceilings. Config may tighten these; nothing may widen them. ────────
HARD_MAX_TTL_S = 900          # 15 minutes (spec §1)
HARD_MAX_USES = 50            # a "session grant" is still not unlimited
HARD_MAX_REQUEST_TTL_S = 3600

NOTIFY_SEVERITIES = frozenset({"info", "warning", "critical"})
CHANNELS = frozenset({"mobile", "desktop", "both"})

DEFAULTS = {
    "enabled": False,
    "channel": "mobile",
    "layers": {k: False for k in LAYER_KEYS},
    "max_ttl_s": 900,
    "default_uses": 1,
    "request_ttl_s": 600,
    "max_pending_per_session": 3,
    "max_requests_per_hour": 20,
    "notify_severity": "critical",
    "allow_session_grants": False,
    # The command run when a request is DECIDED, so the asking agent hears
    # about it (services/override_wake.py). None = nobody is listening, which
    # is the pre-2.73 behaviour and the reason a grant could expire unused
    # while the user assumed their tap had done something.
    "wake": None,
}

_VALID_KEYS = frozenset(DEFAULTS)
_BOOL_KEYS = ("enabled", "allow_session_grants")
#: (key, hard ceiling or None) — every one is min-merged across scopes.
_INT_KEYS = (
    ("max_ttl_s", HARD_MAX_TTL_S),
    ("default_uses", HARD_MAX_USES),
    ("request_ttl_s", HARD_MAX_REQUEST_TTL_S),
    ("max_pending_per_session", None),
    ("max_requests_per_hour", None),
)

_CORRUPT = object()  # sentinel: scope's override section invalid → fail closed


# ── Never-escalatable surfaces (spec §2, the "never" rows) ─────────────────

def _norm_glob(glob) -> str:
    return str(glob).replace("\\", "/").casefold()


def _absolute_denies() -> frozenset:
    """Tier-0 globs, read live so a change there cannot drift from here."""
    try:
        from services import access_guard as ag  # noqa: PLC0415 — lazy
        return frozenset(_norm_glob(g) for g in ag.BUILTIN_ABSOLUTE_DENY)
    except Exception:
        # Fail closed: if we cannot read the Tier-0 list, treat the known
        # members as denied anyway rather than declaring them escalatable.
        return frozenset({"**/.c3/secrets.enc", "**/.c3/cred_state.json"})


#: Files inside `.c3/` that no grant may ever authorise writing, whatever the
#: rule that denied. Belt-and-braces on top of §2: `override_grants.json` is
#: here so an approved `**/.c3/**` write can never be turned into the agent
#: minting its own grants (spec §11 threat 3).
FORBIDDEN_TARGET_NAMES = frozenset({
    "secrets.enc",
    "cred_state.json",
    "config.json",
    "override_grants.json",
    "overrides.jsonl",
})


def forbidden_target(path_key: str) -> bool:
    """True when *path_key* is a vault/policy file no grant may ever cover.

    *path_key* is an ``access_guard.canonicalize()`` canon string: POSIX
    separators, casefolded.
    """
    parts = str(path_key or "").split("/")
    return (len(parts) >= 2
            and parts[-2] == ".c3"
            and parts[-1] in FORBIDDEN_TARGET_NAMES)


def rule_class_for_denial(denial) -> str | None:
    """The ``override.layers`` key an access-layer ``Denial`` maps to.

    ``None`` means **never escalatable** — the caller must not offer a
    request, must not consult grants, and must not say why. Silence beats
    advertising that a request surface exists for the vault (spec §6).
    """
    rule = str(getattr(denial, "rule", "") or "")
    kind = str(getattr(denial, "kind", "") or "")
    scope = str(getattr(denial, "scope", "") or "")
    if not rule:
        return None
    # Synthetic spelling rules (<unc>, <ads>, …) and <corrupt-config>: there is
    # nothing to approve — the path is unrepresentable or the config is broken.
    if rule.startswith("<"):
        return None
    if _norm_glob(rule) in _absolute_denies():
        return None
    if kind == "mask":
        return LAYER_MASK
    if kind == "read_only":
        return LAYER_ACCESS_READONLY
    if kind == "deny":
        return LAYER_ACCESS_BUILTIN if scope == "builtin" else LAYER_ACCESS_DENY
    return None


# ── Policy value object ────────────────────────────────────────────────────

@dataclass(frozen=True)
class OverridePolicy:
    """Effective, already-merged policy for one project."""
    enabled: bool = False
    channel: str = "mobile"
    layers: dict = field(default_factory=lambda: dict(DEFAULTS["layers"]))
    max_ttl_s: int = 900
    default_uses: int = 1
    request_ttl_s: int = 600
    max_pending_per_session: int = 3
    max_requests_per_hour: int = 20
    notify_severity: str = "critical"
    allow_session_grants: bool = False
    wake: dict | None = None
    warnings: tuple = ()
    corrupt_scopes: tuple = ()

    def escalatable(self, layer_key: str) -> bool:
        """True iff the feature is on AND this layer was opted into."""
        return bool(self.enabled) and bool(self.layers.get(layer_key, False))

    def clamp_ttl(self, requested: int | None) -> int:
        want = self.max_ttl_s if requested is None else int(requested)
        return max(1, min(want, self.max_ttl_s, HARD_MAX_TTL_S))

    def clamp_uses(self, requested: int | None) -> int:
        want = self.default_uses if requested is None else int(requested)
        ceiling = self.default_uses if not self.allow_session_grants else HARD_MAX_USES
        return max(1, min(want, ceiling, HARD_MAX_USES))

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "channel": self.channel,
            "layers": dict(self.layers),
            "max_ttl_s": self.max_ttl_s,
            "default_uses": self.default_uses,
            "request_ttl_s": self.request_ttl_s,
            "max_pending_per_session": self.max_pending_per_session,
            "max_requests_per_hour": self.max_requests_per_hour,
            "notify_severity": self.notify_severity,
            "allow_session_grants": self.allow_session_grants,
            # The spec itself never crosses the wire. It is an argv the box
            # will run, and it can carry a conversation id or a path that says
            # more about this machine than a policy screen needs to. A phone
            # only needs to know whether anything is listening.
            "wake_configured": bool(self.wake),
            "warnings": list(self.warnings),
            "corrupt_scopes": list(self.corrupt_scopes),
        }


DISABLED = OverridePolicy()


def _validate(section: dict):
    """The raw section, or ``_CORRUPT``. Every rejection is a hard error."""
    if not isinstance(section, dict):
        return _CORRUPT
    if set(section) - _VALID_KEYS:
        return _CORRUPT  # unknown key — a future knob must never no-op
    for key in _BOOL_KEYS:
        if key in section and not isinstance(section[key], bool):
            return _CORRUPT
    for key, _ceiling in _INT_KEYS:
        if key not in section:
            continue
        val = section[key]
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            return _CORRUPT
    if "channel" in section and section["channel"] not in CHANNELS:
        return _CORRUPT
    if ("notify_severity" in section
            and section["notify_severity"] not in NOTIFY_SEVERITIES):
        return _CORRUPT
    layers = section.get("layers")
    if layers is not None:
        if not isinstance(layers, dict) or set(layers) - set(LAYER_KEYS):
            return _CORRUPT
        if any(not isinstance(v, bool) for v in layers.values()):
            return _CORRUPT
    if "wake" in section:
        # Lazy import: override_wake reads a resolved policy back out of this
        # module on the fire path, and a module-level import would close that
        # loop. A validator we cannot even load reads as corrupt, not as
        # "probably fine" — this key names a command the machine will run.
        try:
            from services import override_wake as owake  # noqa: PLC0415
            valid = owake.validate_spec(section["wake"])
        except Exception:
            return _CORRUPT
        if not valid:
            return _CORRUPT
    return section


def _read_scope(base: Path):
    """``(section_or_None, corrupt)`` for one scope. Absent file ⇒ no opinion."""
    cfg = base / ".c3" / "config.json"
    if not cfg.is_file():
        return None, False
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return None, True
    if not isinstance(data, dict):
        return None, True
    section = data.get("override")
    if section is None:
        return None, False
    checked = _validate(section)
    if checked is _CORRUPT:
        return None, True
    return checked, False


def _global_base() -> Path | None:
    """``~`` — deliberately NOT honouring ``C3_HOME``.

    ``enforcement_policy`` lets ``C3_HOME`` relocate the global scope, which
    is fine for a workflow preference. Here the global scope only ever
    *tightens*, so pointing it at an empty directory would be a widening
    vector reachable from an environment variable. Matches
    ``access_guard._global_base``.
    """
    try:
        home = Path.home()
        return home if str(home) not in ("", "/") else None
    except Exception:
        return None


def resolve(project_path: str = ".") -> OverridePolicy:
    """Effective policy for *project_path*: defaults ← global ← project.

    Booleans AND across the scopes that express an opinion; numbers take the
    minimum and are then clamped to the hard ceilings. A corrupt scope
    disables the feature outright — there is no partial-trust reading of a
    config we could not parse.
    """
    try:
        proj = Path(project_path).resolve()
    except Exception:
        return OverridePolicy(warnings=("project path could not be resolved; "
                                        "overrides disabled",))
    gbase = _global_base()

    sections, corrupt, warnings = [], [], []
    for scope, base in (("global", gbase), ("project", proj)):
        if base is None or (scope == "global" and gbase == proj):
            continue
        section, bad = _read_scope(base)
        if bad:
            corrupt.append(scope)
            warnings.append(
                f"[c3-override] the '{scope}' scope's `override` section is "
                f"invalid or unparseable — overrides are DISABLED "
                f"(fix {base / '.c3' / 'config.json'} by hand)")
        elif section is not None:
            sections.append(section)

    if corrupt:
        return OverridePolicy(warnings=tuple(warnings),
                              corrupt_scopes=tuple(corrupt))

    values = dict(DEFAULTS)
    values["layers"] = dict(DEFAULTS["layers"])

    for key in _BOOL_KEYS:
        opinions = [s[key] for s in sections if key in s]
        values[key] = all(opinions) if opinions else DEFAULTS[key]

    for key, ceiling in _INT_KEYS:
        opinions = [s[key] for s in sections if key in s]
        val = min(opinions) if opinions else DEFAULTS[key]
        values[key] = min(val, ceiling) if ceiling else val

    for layer in LAYER_KEYS:
        opinions = [s["layers"][layer] for s in sections
                    if isinstance(s.get("layers"), dict) and layer in s["layers"]]
        values["layers"][layer] = all(opinions) if opinions else False

    # Presentation-only keys: last scope with an opinion wins (project last).
    for key in ("channel", "notify_severity"):
        for s in sections:
            if key in s:
                values[key] = s[key]

    # `wake` is not a permission, so it has no tightening direction to merge
    # along — it is "who to tell", and the nearest scope is the one that knows.
    # Project last, so a project's own wake replaces the machine-wide default
    # rather than firing both.
    for s in sections:
        if "wake" in s:
            values["wake"] = s["wake"]

    return OverridePolicy(warnings=tuple(warnings), **values)


def project_root_for(path) -> Path | None:
    """Nearest ancestor of *path* that owns a ``.c3`` directory.

    Lets a refusal decide whether to offer an override without every call site
    threading a project through. Only ever runs on the denial path.
    """
    try:
        p = Path(path).resolve()
    except Exception:
        return None
    for cand in (p, *p.parents):
        try:
            if (cand / ".c3").is_dir():
                return cand
        except OSError:
            continue
    return None


def resolve_for_path(path) -> OverridePolicy:
    """Policy for whichever project owns *path*; DISABLED when none does."""
    root = project_root_for(path)
    return resolve(str(root)) if root else DISABLED


def offer_line(layer_key: str, path, tool: str, op: str) -> str:
    """The single line appended to a refusal when a layer IS escalatable.

    Absent — never a different string — when it is not (spec §6).
    """
    return (
        f"\n{TAG_OFFER} You may ask the user to allow this once:\n"
        f"  c3_override(action='request', path={str(path)!r}, tool={tool!r}, "
        f"op={op!r},\n"
        f"              why='<one sentence, concrete>')"
    )
