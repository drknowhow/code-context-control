"""Access Guard v1 — the single path-policy evaluator for every C3 surface.

Implements docs/access-guard.md (frozen spec). One evaluator, one
canonicalizer; consumers (tool handlers, hooks, shell scanner, unlock map)
call this module and never resolve or match paths themselves.

Semantics: ``deny`` = no read/write/create/enumerate; ``read_only`` = no
write. Scopes (global ``~/.c3`` + project ``.c3``) UNION and only tighten.
Builtins are hardcoded and non-overridable. A corrupt or invalid access
section makes that scope evaluate deny-all (fail closed) with a loud reason.

Stdlib-only by design: hooks import this in a subprocess on every native
tool call.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# ── Rule model ──────────────────────────────────────────────────────────────

_KIND_DENY = "deny"
_KIND_READ_ONLY = "read_only"
_KIND_MASK = "mask"
_VALID_KEYS = {_KIND_DENY, _KIND_READ_ONLY, _KIND_MASK}

# `access` keys that are not rule kinds. Kept separate from _VALID_KEYS so the
# "unknown key ⇒ corrupt" rule (which exists so `allow` can never silently
# no-op) still applies to everything else.
_KEY_DISABLE_BUILTIN = "disable_builtin"
_VALID_SECTION_KEYS = _VALID_KEYS | {_KEY_DISABLE_BUILTIN}

# Keyring namespace for builtin opt-out attestations. Distinct from c3-creds /
# c3-bitbucket / c3-jira so a vault wipe cannot silently re-enable a builtin
# (or, worse, silently disable one).
_ACCESS_KEYRING_SERVICE = "c3-access"

# Mask presets (docs/mask-guard.md §4). Names + param schema live here so the
# hook subprocess can validate config without importing the transform engines;
# the engines themselves are in services/mask_presets.py.
# Bumping a preset's version invalidates every materialized view built with it.
MASK_PRESETS = {
    "redact_secrets":  {"version": 1, "params": {}},
    "redact_columns":  {"version": 1, "params": {"columns": list}},
    "sample_rows":     {"version": 1, "params": {"count": int, "strategy": str}},
    "signatures_only": {"version": 1, "params": {}},
}
_SAMPLE_STRATEGIES = ("first", "last")

# Builtins: product integrity only (docs/access-guard.md §1), in two tiers.
#
# Tier 0 — ABSOLUTE. The credential vault. Never disableable, because these
# paths already carry a dedicated guard (credential_store.vault_guard_reason)
# AND their own human-only escalation (agent_readable). An opt-out here would
# add nothing but a shorter route to the same secrets.
BUILTIN_ABSOLUTE_DENY = ("**/.c3/secrets.enc", "**/.c3/cred_state.json")

# Tier 1 — a human may switch these off, but only through the two-key opt-out
# below: a config entry AND a keyring attestation. Default is all enforced.
BUILTIN_DENY = ("**/.env*",)
BUILTIN_WRITE_DENY = ("**/.c3/**", "**/.claude/settings*.json", "**/.git/**")

#: Globs `c3 access disable-builtin` accepts. Order is display order.
DISABLEABLE_BUILTINS = BUILTIN_DENY + BUILTIN_WRITE_DENY

# Spelling rules — they deny how a path is WRITTEN, not where it points, so
# they have no glob to list. A refusal cites one of these by name, so they
# must be discoverable via `c3 access list` (#50).
SYNTHETIC_RULES = (
    ("<unc>", "UNC / network path outside the project"),
    ("<unresolvable>", "path could not be resolved"),
    ("<empty-component>", "path component empties after normalization"),
    ("<8.3-alias>", "8.3 short-name component on a non-existing target"),
    ("<ads>", "NTFS alternate data stream syntax (Windows only)"),
)

# Seeded into GLOBAL scope by install/CLI (user-removable); not enforced here.
DEFAULT_GLOBAL_RULES = ("*.pem", "id_rsa*", "*.key")

# Stable machine tags — API, do not change (docs/access-guard.md §4,
# docs/mask-guard.md §5).
TAG_DENIED = "[c3-access:denied]"
TAG_READ_ONLY = "[c3-access:read_only]"
TAG_LIMITED = "[c3-access:limited]"
TAG_MASKED = "[c3-mask:transformed]"
TAG_MASK_LIMITED = "[c3-mask:limited]"
TAG_MASK_STALE = "[c3-mask:stale]"
TAG_MASK_UNSUPPORTED = "[c3-mask:unsupported]"

_PATH_CAP = 200  # interpolation length cap for refusal strings


@dataclass(frozen=True)
class Rule:
    glob: str          # POSIX-canonical, casefolded
    kind: str          # deny | read_only
    scope: str         # builtin | global | project
    _re: re.Pattern
    _basename: bool    # no '/' in glob → match against the leaf name

    def matches(self, canon: str, rel: str, name: str) -> bool:
        if self._basename:
            return bool(self._re.match(name))
        return bool(self._re.match(rel)) or bool(self._re.match(canon))


@dataclass(frozen=True)
class MaskRule:
    """A ``mask`` rule: which paths, rendered by which versioned preset."""
    glob: str          # POSIX-canonical, casefolded
    preset: str        # key of MASK_PRESETS
    params: tuple      # sorted ((key, value), ...) — hashable + deterministic
    scope: str         # global | project
    _rule: Rule

    def matches(self, canon: str, rel: str, name: str) -> bool:
        return self._rule.matches(canon, rel, name)

    @property
    def params_dict(self) -> dict:
        return dict(self.params)

    def identity(self) -> tuple:
        """(preset, params) — two rules with the same identity may overlap."""
        return (self.preset, self.params)


@dataclass(frozen=True)
class Verdict:
    """Full policy answer. ``kind`` ∈ allowed | masked | read_only | denied.

    Mask-aware content surfaces call ``verdict()`` and serve ``mask_rule``'s
    materialized view. Everything else calls ``check()``, which fails closed
    on a masked path — an un-migrated call site refuses rather than leaks.
    """
    kind: str
    denial: "Denial | None" = None
    mask_rule: MaskRule | None = None

    @property
    def masked(self) -> bool:
        return self.kind == "masked"

    @property
    def allowed(self) -> bool:
        return self.kind == "allowed"


@dataclass(frozen=True)
class Denial:
    rule: str          # matched glob (or a synthetic reason marker)
    kind: str          # deny | read_only
    scope: str         # builtin | global | project
    reason: str        # short evaluator-internal reason


class AccessDenied(Exception):
    """Raised by enforce(); carries the formatted refusal in ``message``.

    Except-Exception-continue loops (e.g. c3_delegate) must re-raise this.
    """

    def __init__(self, denial: Denial, message: str):
        super().__init__(message)
        self.denial = denial
        self.message = message


# ── Glob compilation ────────────────────────────────────────────────────────

def _glob_to_re(pat: str) -> re.Pattern:
    """POSIX glob → regex. ``**`` crosses separators; ``*``/``?`` do not."""
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _compile(glob: str, kind: str, scope: str) -> Rule:
    canon = glob.replace("\\", "/").casefold().strip()
    if canon.endswith("/**"):
        # `X/**` also protects X itself (gitignore-style prefix semantics).
        canon_alt = canon[:-3]
        rx = re.compile(
            f"(?:{_glob_to_re(canon).pattern}|{_glob_to_re(canon_alt).pattern})")
    else:
        rx = _glob_to_re(canon)
    return Rule(glob=canon, kind=kind, scope=scope, _re=rx,
                _basename="/" not in canon)


def validate_globs(globs) -> str:
    """'' when every pattern compiles; else the first offending pattern."""
    for g in globs:
        if not isinstance(g, str) or not g.strip():
            return repr(g)
        try:
            _compile(g, _KIND_DENY, "check")
        except re.error:
            return g
    return ""


_ABSOLUTE_RULES = tuple(
    _compile(g, _KIND_DENY, "builtin") for g in BUILTIN_ABSOLUTE_DENY
)
_BUILTIN_RULES = tuple(
    [_compile(g, _KIND_DENY, "builtin") for g in BUILTIN_DENY]
    + [_compile(g, _KIND_READ_ONLY, "builtin") for g in BUILTIN_WRITE_DENY]
)


# ── Builtin opt-out (two-key) ───────────────────────────────────────────────
#
# Turning a builtin off requires BOTH of:
#   1. `access.disable_builtin: ["<glob>"]` in the GLOBAL config, and
#   2. a keyring attestation written only by `c3 access disable-builtin`
#      (CLI) or the Access tab.
#
# The point of the second key is that an agent which manages to write
# config.json — the exact move a prompt-injected agent would make to grant
# itself write access to ~/.claude/settings.json — still cannot produce the
# attestation, so the builtin stays on. Same construction as the credential
# vault's agent_readable flag (credential_store.verify_agent_readable).
#
# GLOBAL scope only, deliberately: project scopes may only ever TIGHTEN
# (spec §1). A per-project opt-out would let a cloned repo loosen the guard.
#
# Every failure path here leaves the builtin ENFORCED.


def _norm_builtin(glob) -> str:
    """Same canonical form _compile() stores on Rule.glob, so they compare."""
    return str(glob or "").replace("\\", "/").casefold().strip()


def _builtin_attest_account(glob: str) -> str:
    return f"builtin_disabled|{_norm_builtin(glob)}"


def _attest_builtin_disabled(glob: str, disabled: bool) -> bool:
    """Write the keyring half of the opt-out. False when it did not stick.

    A caller that ignores False would leave the config saying "disabled" while
    evaluation keeps enforcing — so set_builtin_disabled() surfaces it.
    """
    try:
        import keyring  # noqa: PLC0415 — lazy: access_guard is stdlib-only on
        keyring.set_password(  # the hot path (hooks import it per tool call)
            _ACCESS_KEYRING_SERVICE, _builtin_attest_account(glob),
            "1" if disabled else "0")
        return True
    except Exception:
        return False


def _verify_builtin_disabled(glob: str) -> bool:
    """True only when the keyring agrees. Fails closed on any error."""
    try:
        import keyring  # noqa: PLC0415 — see above
        return keyring.get_password(
            _ACCESS_KEYRING_SERVICE, _builtin_attest_account(glob)) == "1"
    except Exception:
        return False


def _configured_disable_list() -> list:
    """Raw `access.disable_builtin` from the global config. [] on any problem."""
    base = _global_base()
    if base is None:
        return []
    cfg = base / ".c3" / "config.json"
    if not cfg.is_file():
        return []
    try:
        section = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("access")
    except Exception:
        return []
    if not isinstance(section, dict):
        return []
    raw = section.get(_KEY_DISABLE_BUILTIN, [])
    return [g for g in raw if isinstance(g, str)] if isinstance(raw, list) else []


def disabled_builtins() -> frozenset:
    """Canonical globs of builtins that are genuinely off right now.

    A glob counts only when it is a Tier-1 builtin, listed in the global
    config, AND attested in the keyring. Anything else — a Tier-0 vault glob,
    an unknown glob, a config edited outside the API — is silently NOT
    disabled, which is the safe direction.

    Costs nothing when nobody has opted out: the keyring is only touched once
    the config list is non-empty.
    """
    listed = _configured_disable_list()
    if not listed:
        return frozenset()
    allowed = {_norm_builtin(g) for g in DISABLEABLE_BUILTINS}
    return frozenset(
        c for c in (_norm_builtin(g) for g in listed)
        if c in allowed and _verify_builtin_disabled(c)
    )


# ── Mask rules ──────────────────────────────────────────────────────────────

def validate_mask_entry(entry) -> str:
    """'' when *entry* is a well-formed mask rule; else a human reason.

    Strict by construction: an unknown preset or a mistyped param must be a
    loud config error, never a silently-skipped rule that leaves a path
    unmasked while the UI claims otherwise.
    """
    if not isinstance(entry, dict):
        return f"mask entry must be an object, got {type(entry).__name__}"
    unknown = set(entry) - {"glob", "preset", "params"}
    if unknown:
        return f"unknown mask key(s): {', '.join(sorted(unknown))}"
    glob = entry.get("glob")
    if not isinstance(glob, str) or not glob.strip():
        return f"mask 'glob' must be a non-empty string, got {glob!r}"
    if validate_globs([glob]):
        return f"mask glob does not compile: {glob!r}"
    preset = entry.get("preset")
    if preset not in MASK_PRESETS:
        return (f"unknown mask preset {preset!r} — expected one of "
                f"{', '.join(sorted(MASK_PRESETS))}")
    schema = MASK_PRESETS[preset]["params"]
    params = entry.get("params") or {}
    if not isinstance(params, dict):
        return f"mask 'params' must be an object, got {type(params).__name__}"
    extra = set(params) - set(schema)
    if extra:
        return (f"preset {preset!r} takes no param(s): "
                f"{', '.join(sorted(extra))}")
    for key, expected in schema.items():
        if key not in params:
            return f"preset {preset!r} requires param {key!r}"
        value = params[key]
        if expected is int and (isinstance(value, bool)
                                or not isinstance(value, int)):
            return f"param {key!r} must be an integer, got {value!r}"
        if expected is int and value < 1:
            return f"param {key!r} must be >= 1, got {value!r}"
        if expected is str and not (isinstance(value, str) and value.strip()):
            return f"param {key!r} must be a non-empty string, got {value!r}"
        if expected is list and not (isinstance(value, list) and value
                                     and all(isinstance(v, str) and v.strip()
                                             for v in value)):
            return f"param {key!r} must be a non-empty list of strings"
    if preset == "sample_rows" and params["strategy"] not in _SAMPLE_STRATEGIES:
        return (f"param 'strategy' must be one of "
                f"{', '.join(_SAMPLE_STRATEGIES)}, got {params['strategy']!r}")
    return ""


_PARAM_TYPE_NAMES = {int: "int", str: "str", list: "list[str]"}


def preset_catalog() -> dict:
    """JSON-safe description of MASK_PRESETS for REST/UI consumers.

    MASK_PRESETS stores Python types for validation; those cannot cross a
    JSON boundary, so the wire format names them instead.
    """
    return {
        name: {
            "version": spec["version"],
            "params": {key: _PARAM_TYPE_NAMES.get(kind, str(kind))
                       for key, kind in spec["params"].items()},
            "choices": ({"strategy": list(_SAMPLE_STRATEGIES)}
                        if name == "sample_rows" else {}),
        }
        for name, spec in MASK_PRESETS.items()
    }


def _freeze_params(params: dict) -> tuple:
    """Deterministic hashable params — the view hash depends on this order."""
    out = []
    for key in sorted(params):
        value = params[key]
        out.append((key, tuple(value) if isinstance(value, list) else value))
    return tuple(out)


def _compile_mask(entry: dict, scope: str) -> MaskRule:
    glob = entry["glob"]
    return MaskRule(
        glob=glob.replace("\\", "/").casefold().strip(),
        preset=entry["preset"],
        params=_freeze_params(entry.get("params") or {}),
        scope=scope,
        _rule=_compile(glob, _KIND_MASK, scope),
    )


def _install_dir_rule() -> Rule | None:
    """Write-deny on the installed C3 package (agent self-modification).

    Applies only to installed layouts (site-/dist-packages). A dev checkout
    is exempt: there, editing the C3 tree is the work itself, and the repo's
    own rules/builtins still apply.
    """
    try:
        import cli  # noqa: PLC0415 — lazy: not all callers have it importable
        pkg = Path(cli.__file__).resolve().parent
        parts = {p.casefold() for p in pkg.parts}
        if "site-packages" not in parts and "dist-packages" not in parts:
            return None
        canon = str(pkg.parent).replace("\\", "/").casefold().rstrip("/")
        return _compile(canon + "/**", _KIND_READ_ONLY, "builtin")
    except Exception:
        return None


# ── Config loading (two scopes, fail closed per scope) ──────────────────────

_CORRUPT = object()  # sentinel: scope config invalid → deny-all for the scope


def _read_scope_rules(base: Path, scope: str):
    """``(rules, mask_rules)`` for one scope, ``([], [])`` when absent,
    ``_CORRUPT`` when invalid."""
    cfg = base / ".c3" / "config.json"
    if not cfg.is_file():
        return [], []
    try:
        section = (json.loads(cfg.read_text(encoding="utf-8"))
                   or {}).get("access")
    except Exception:
        return _CORRUPT
    if section is None:
        return [], []
    if not isinstance(section, dict):
        return _CORRUPT
    unknown = set(section) - _VALID_SECTION_KEYS
    if unknown:
        return _CORRUPT  # hard error — 'allow' must never silently no-op
    # Builtin opt-out is global-only, because a project scope may only ever
    # TIGHTEN. A project-scope entry is a loud error, not a silent no-op: the
    # UI must never claim a builtin is off while evaluation still enforces it.
    if scope == "project" and section.get(_KEY_DISABLE_BUILTIN):
        return _CORRUPT
    rules = []
    for kind in (_KIND_DENY, _KIND_READ_ONLY):
        globs = section.get(kind, [])
        if not isinstance(globs, list) or validate_globs(globs):
            return _CORRUPT
        rules.extend(_compile(g, kind, scope) for g in globs)
    entries = section.get(_KIND_MASK, [])
    if not isinstance(entries, list):
        return _CORRUPT
    mask_rules = []
    for entry in entries:
        if validate_mask_entry(entry):
            return _CORRUPT
        mask_rules.append(_compile_mask(entry, scope))
    return rules, mask_rules


def _global_base() -> Path | None:
    try:
        home = Path.home()
        return home if str(home) not in ("", "/") else None
    except Exception:
        return None


def load_all(project_path: str = ".") -> tuple:
    """(rules, mask_rules, corrupt_scopes) — one read per scope."""
    disabled = disabled_builtins()
    rules = list(_ABSOLUTE_RULES)
    rules.extend(r for r in _BUILTIN_RULES if r.glob not in disabled)
    inst = _install_dir_rule()
    if inst:
        rules.append(inst)
    mask_rules, corrupt = [], []
    gbase = _global_base()
    proj = Path(project_path).resolve()
    for scope, base in (("global", gbase), ("project", proj)):
        if base is None or (scope == "global" and gbase == proj):
            continue
        scoped = _read_scope_rules(base, scope)
        if scoped is _CORRUPT:
            corrupt.append(scope)
        else:
            rules.extend(scoped[0])
            mask_rules.extend(scoped[1])
    return rules, mask_rules, corrupt


def load_rules(project_path: str = ".") -> tuple:
    """(rules, corrupt_scopes) — union of builtin + global + project scopes."""
    rules, _mask, corrupt = load_all(project_path)
    return rules, corrupt


def load_mask_rules(project_path: str = ".") -> tuple:
    """(mask_rules, corrupt_scopes) — the ``access.mask`` registry."""
    _rules, mask_rules, corrupt = load_all(project_path)
    return mask_rules, corrupt


def has_mask_rules(project_path: str = ".") -> bool:
    """True when any mask rule is active — drives the mask search footer."""
    mask_rules, corrupt = load_mask_rules(project_path)
    return bool(mask_rules or corrupt)


def has_active_rules(project_path: str = ".") -> bool:
    """True when any user rules (or corrupt scopes) exist — drives S4."""
    rules, mask_rules, corrupt = load_all(project_path)
    # Count the install-dir rule only when it actually loaded: in a dev
    # checkout it is absent, and a fixed "+1" made a single user rule
    # invisible to S4 (footer suppressed while filtering was active).
    n_baseline = (len(_ABSOLUTE_RULES)
                  + len(_BUILTIN_RULES) - len(disabled_builtins())
                  + (1 if _install_dir_rule() else 0))
    return bool(corrupt) or bool(mask_rules) or len(rules) > n_baseline


# ── Canonicalization (docs/access-guard.md §2 — the ONE implementation) ─────

_ADS_RE = re.compile(r"^[a-zA-Z]:$")
_SHORTNAME_RE = re.compile(r"~\d")


def canonicalize(path, project_root=".") -> tuple:
    """(canon_posix_casefolded, rel_posix_casefolded, denial_or_None).

    Handles device/UNC prefixes, nearest-existing-parent resolution for
    not-yet-existing targets, trailing dot/space stripping, ADS rejection,
    casefolding, and the 8.3 short-name predicate. ``rel`` is '' when the
    path is outside ``project_root``.
    """
    s = str(path)
    if s.startswith("\\\\?\\UNC\\") or s.startswith("//?/UNC/"):
        s = "\\\\" + s[8:]
    elif s[:4] in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
        s = s[4:]
    if s.startswith(("\\\\", "//")):
        return "", "", Denial("<unc>", _KIND_DENY, "builtin",
                              "UNC paths are denied by default")
    p = Path(s)
    if not p.is_absolute():
        p = Path(project_root) / p

    # Nearest-existing-parent resolve: resolve the deepest existing ancestor,
    # then re-append the residual components with trailing dot/space stripped.
    residual = []
    probe = p
    while not probe.exists() and probe.parent != probe:
        residual.append(probe.name)
        probe = probe.parent
    try:
        resolved = probe.resolve()
    except OSError:
        return "", "", Denial("<unresolvable>", _KIND_DENY, "builtin",
                              "path could not be resolved")
    windows = os.name == "nt"
    for name in reversed(residual):
        cleaned = name
        if windows:
            # Creating 'x.' or 'x ' lands on 'x' on NTFS — model it.
            while cleaned != cleaned.rstrip(". "):
                cleaned = cleaned.rstrip(". ")
        if not cleaned:
            return "", "", Denial("<empty-component>", _KIND_DENY, "builtin",
                                  "path component empties after normalization")
        if windows and _SHORTNAME_RE.search(cleaned):
            return "", "", Denial("<8.3-alias>", _KIND_DENY, "builtin",
                                  "8.3 short-name component on a "
                                  "non-existing target")
        resolved = resolved / cleaned

    canon_str = str(resolved).replace("\\", "/")
    if windows:
        body = canon_str[2:] if _ADS_RE.match(canon_str[:2]) else canon_str
        if ":" in body:
            return "", "", Denial("<ads>", _KIND_DENY, "builtin",
                                  "NTFS alternate data stream syntax is denied")
    canon = canon_str.casefold()

    try:
        root = str(Path(project_root).resolve()).replace("\\", "/").casefold()
        root = root.rstrip("/")  # drive-root projects resolve to 'c:/'
        rel = canon[len(root) + 1:] if canon.startswith(root + "/") else ""
    except Exception:
        rel = ""
    return canon, rel, None


# ── Evaluation ──────────────────────────────────────────────────────────────

def verdict(path, operation: str, project_path: str = ".") -> Verdict:
    """Full policy answer, including the ``masked`` outcome.

    Precedence: ``deny`` > ``mask`` > ``read_only`` (docs/mask-guard.md §4).
    A mask rule denies every write-class operation — masked content is
    read-only, always, with no override (§3).

    Overlapping mask rules that disagree on (preset, params) are a config
    error, not a precedence puzzle: rendering would depend on rule order and
    stop being diffable, so the path fails closed until the user fixes it.
    """
    op_write = operation in ("write", "create", "delete")
    canon, rel, denial = canonicalize(path, project_path)
    if denial:
        return Verdict("denied", denial)
    rules, mask_rules, corrupt = load_all(project_path)
    if corrupt:
        return Verdict("denied", Denial(
            "<corrupt-config>", _KIND_DENY, ",".join(corrupt),
            "access section invalid or unparseable — scope fails closed "
            "(fix .c3/config.json 'access')"))
    name = canon.rsplit("/", 1)[-1]
    hit_ro = None
    for rule in rules:
        if not rule.matches(canon, rel, name):
            continue
        if rule.kind == _KIND_DENY:
            return Verdict("denied", Denial(rule.glob, _KIND_DENY, rule.scope,
                                            "deny rule"))
        if hit_ro is None:
            hit_ro = rule

    hits = [m for m in mask_rules if m.matches(canon, rel, name)]
    if hits:
        identities = {m.identity() for m in hits}
        if len(identities) > 1:
            globs = ", ".join(sorted(f"'{m.glob}'->{m.preset}" for m in hits))
            return Verdict("denied", Denial(
                globs, _KIND_MASK, hits[0].scope,
                "overlapping mask rules disagree on preset/params — "
                "rendering would depend on rule order; remove the conflict "
                "in .c3/config.json 'access.mask'"))
        hit = hits[0]
        if op_write:
            return Verdict("denied", Denial(hit.glob, _KIND_MASK, hit.scope,
                                            "mask rule (masked = read-only)"),
                           hit)
        return Verdict("masked", None, hit)

    if hit_ro is not None and op_write:
        return Verdict("read_only", Denial(hit_ro.glob, _KIND_READ_ONLY,
                                           hit_ro.scope, "read-only rule"))
    return Verdict("allowed")


def check(path, operation: str, project_path: str = ".") -> Denial | None:
    """None when allowed; a Denial otherwise. operation: read|write|create|delete.

    create/delete evaluate as write. Builtin write-denies apply only to
    write-class operations; ``deny`` rules apply to everything (R1+R2).

    **Masked paths deny here, including for reads.** This is the fail-closed
    contract for un-migrated call sites: a surface that has not been taught to
    serve the materialized view refuses rather than leaking raw bytes. Content
    surfaces that CAN serve a masked view call ``verdict()`` instead.
    """
    v = verdict(path, operation, project_path)
    if v.denial:
        return v.denial
    if v.masked:
        rule = v.mask_rule
        return Denial(rule.glob, _KIND_MASK, rule.scope,
                      "mask rule — raw read denied on a non-mask-aware "
                      "surface")
    return None


# ── Refusal strings (docs/access-guard.md §4 — verbatim) ────────────────────

def _cap(s: str) -> str:
    s = str(s)
    if len(s) <= _PATH_CAP:
        return s
    return s[: _PATH_CAP // 2 - 2] + " … " + s[-(_PATH_CAP // 2 - 2):]


def _override_offer(denial: Denial, path, operation: str, tool: str) -> str:
    """The one line docs/override-requests.md §6 appends — or ''.

    Emitted ONLY when the layer is escalatable AND the project turned
    overrides on. Silence, not an explanation, is the answer for the layers
    that can never be escalated: an agent must not learn from a refusal that
    a request surface exists for the credential vault (§6).

    Restricted to the hook surface on purpose. The offer promises that a human
    'yes' makes the retry work, and today only the PreToolUse gates consult
    grants — the ``c3_*`` content surfaces do not (see §13, phase P2a). An
    offer on a surface that would still refuse after approval is worse than
    no offer.
    """
    try:
        from services import override_policy as _op  # noqa: PLC0415 — cycle
        layer = _op.rule_class_for_denial(denial)
        if layer is None:
            return ""
        if not _op.resolve_for_path(path).escalatable(layer):
            return ""
        return _op.offer_line(layer, path, tool, operation)
    except Exception:
        return ""


def refusal(denial: Denial, path, operation: str, *, surface: str = "mcp",
            tool: str = "", project: str = "") -> str:
    """The exact S1/S2/S3/S5 string for a denial (see frozen spec).

    Mask denials use S6 (raw access on a surface that cannot render) and S7
    (write to a masked path) — docs/mask-guard.md §5.

    On the hook surface an escalatable denial gains one appended line telling
    the agent it may ASK (docs/override-requests.md §6). The pinned strings
    above are unchanged: the append is absent unless the project opted in,
    which is off by default.
    """
    p, glob, scope = _cap(path), denial.rule, denial.scope
    if denial.kind == _KIND_MASK:
        if operation in ("write", "create", "delete"):
            return (
                f"{TAG_MASKED} {operation} denied for {p} by Mask Guard rule "
                f"'{glob}' ({scope} scope). Masked paths are read-only: what "
                "you read was a policy-transformed view, so an edit expressed "
                "against it cannot be applied to the real file. This is a "
                "policy decision, not a transient error — do not retry or "
                "route around it. Mark the affected step blocked, continue "
                "with unaffected files, and report the skip to the user. "
                "Rules: `c3 access list` or the Access tab."
            )
        return (
            f"{TAG_MASK_UNSUPPORTED} raw {operation} denied for {p} by Mask "
            f"Guard rule '{glob}' ({scope} scope). This surface cannot render "
            "the transformed view, so it refuses rather than serve the "
            "original. Use `c3_read` or `c3_compress`, which serve the masked "
            "view. Do not attempt the shell, git, or another tool to obtain "
            "the raw content — that is the same policy decision. Rules: "
            "`c3 access list` or the Access tab."
        )
    if denial.kind == _KIND_READ_ONLY:
        return (
            f"{TAG_READ_ONLY} write denied for {p} by Access Guard rule "
            f"'{glob}' ({scope} scope). The effective policy is read-only; "
            "reads are evaluated separately. Do not retry the write. Mark "
            "the affected step blocked and continue with unaffected files; "
            "report the skip. Rules: `c3 access list` or the Access tab."
        )
    if surface == "hook":
        return (
            f"{TAG_DENIED} native {_cap(tool)} {operation} denied for {p} by "
            f"Access Guard rule '{glob}' ({scope} scope). This is a policy "
            "decision, not a transient error — do not retry through another "
            "tool or the shell. Mark the affected step blocked and continue "
            "with unaffected files. Rules: `c3 access list`."
        ) + _override_offer(denial, path, operation, tool)
    if surface == "proxy":
        return (
            f"{TAG_DENIED} {operation} denied for {p} through project "
            f"'{_cap(project)}' by that project's Access Guard rule "
            f"'{glob}' ({scope} scope). The target project's effective "
            "policy governs proxied access — do not retry. Mark the "
            "affected step blocked and continue with unaffected work."
        )
    return (
        f"{TAG_DENIED} {operation} denied for {p} by Access Guard rule "
        f"'{glob}' ({scope} scope). This is a policy decision, not a "
        "transient error — do not retry or route around it. Mark the "
        "affected step blocked and continue with unaffected files; report "
        "the skip to the user. Rules: `c3 access list` or the Access tab."
    )


def search_footer(project_path: str = ".") -> str:
    """S4 — appended to search output whenever any rules are active."""
    if not has_active_rules(project_path):
        return ""
    return (
        f"{TAG_LIMITED} results are limited to paths permitted by Access "
        "Guard. Absence is not evidence a path does not exist — check "
        "`c3 access list` before concluding missing work, and report the "
        "limitation if required work appears to be missing."
    )


_VIEW_CLASS = {
    "redact_secrets": "redacted",
    "redact_columns": "redacted",
    "sample_rows": "sampled",
    "signatures_only": "structure_only",
}


def mask_header(rule: MaskRule, path="") -> str:
    """The banner prepended to every transformed payload (§5).

    Loud about evidence quality at the point of use; quiet about the policy
    inventory. The coarse view class is included because the agent's
    conclusions depend on it — 'sampled' and 'redacted' fail differently.
    """
    view = _VIEW_CLASS.get(rule.preset, "transformed")
    where = f" {_cap(path)}" if path else ""
    return (
        f"{TAG_MASKED} view={view}{where} — this is a policy-transformed view, "
        "not the original file. Content may be redacted, substituted, or "
        "truncated. Do not treat literals here as real values, do not copy "
        "them into other files, and do not draw conclusions about data "
        "volume or completeness from it. This path is read-only: `c3_edit` "
        "will refuse."
    )


def mask_footer(project_path: str = ".") -> str:
    """Appended to search output whenever any mask rule is active."""
    if not has_mask_rules(project_path):
        return ""
    return (
        f"{TAG_MASK_LIMITED} some searchable content is represented by "
        "policy-transformed views; matches and absence may both differ from "
        "the originals. Check `c3 access list` before concluding that "
        "something is missing, and report the limitation if it affects the "
        "work."
    )


def enforce(path, operation: str, project_path: str = ".", *,
            surface: str = "mcp", tool: str = "", project: str = "") -> None:
    """Raise AccessDenied when ``operation`` on ``path`` is not permitted."""
    denial = check(path, operation, project_path)
    if denial:
        _record_denial(denial, path, operation, project_path, tool)
        raise AccessDenied(denial, refusal(
            denial, path, operation, surface=surface, tool=tool,
            project=project))


def _record_denial(denial, path, operation: str, project_path: str,
                   tool: str) -> None:
    """Feed `c3 access stats` (docs/access-guard.md §3 denial logging).

    Imported lazily and best-effort: this module is imported by hook
    subprocesses on every native tool call, and telemetry must never be able
    to turn a policy decision into a crash.
    """
    try:
        from services import access_telemetry
        access_telemetry.record(
            layer=access_telemetry.LAYER_ACCESS,
            rule=denial.rule, scope=denial.scope,
            tool=tool or surface_default(), operation=operation,
            path=str(path), project_path=project_path,
        )
    except Exception:
        pass


def surface_default() -> str:
    return "c3_mcp"


# ── Rule management (HUMAN surfaces only: UI / REST / `c3 access` CLI) ──────
# Privileged internal writes: this module maintains its own store (the
# `access` section of config.json) server-side on behalf of a human surface.
# The builtin .c3 write-deny governs agent tools, not the module writing its
# own state. No agent-facing mutation surface exists (docs/access-guard.md §1);
# callers are responsible for ledger/activity logging.

# §5 coverage matrix — single source for the UI tab, c3_status, and the guide.
COVERAGE_MATRIX = (
    "Enforced: C3 MCP tools (all agents using C3) · Claude Code native tools "
    "(hooks) · c3_shell (best-effort scan, advisory). NOT enforced: "
    "non-Claude agents' raw shell, direct file APIs, editors. "
    "Masking: c3_read/c3_compress/c3_search serve a transformed view; "
    "c3_shell content reads, git content commands, c3_validate and "
    "c3_delegate REFUSE over masked paths rather than sanitize their output. "
    "The real bytes stay on disk — your editor and any non-C3 reader see them "
    "unmasked. Mask Guard is context hygiene, not containment."
)

_VALID_SCOPES = ("global", "project")


def _scope_config_path(scope: str, project_path: str = ".") -> Path:
    """Config file that owns *scope*'s rules (never the builtin pseudo-scope)."""
    if scope == "project":
        return Path(project_path).resolve() / ".c3" / "config.json"
    if scope == "global":
        base = _global_base()
        if base is None:
            raise ValueError("global scope unavailable: no home directory")
        return base / ".c3" / "config.json"
    raise ValueError(f"unknown scope '{scope}' — expected one of: "
                     f"{', '.join(_VALID_SCOPES)}")


def _norm_glob(glob) -> str:
    """POSIX forward-slash canonical storage form (spec §1)."""
    return str(glob or "").replace("\\", "/").strip()


def _str_list(value) -> list:
    return [g for g in value if isinstance(g, str)] if isinstance(value, list) else []


def _raw_scope_section(cfg: Path) -> tuple:
    """(section_dict, corrupt) — best-effort raw view of one scope's rules."""
    if not cfg.is_file():
        return {}, False
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}, True
    section = data.get("access") if isinstance(data, dict) else None
    if section is None:
        return {}, False
    if not isinstance(section, dict):
        return {}, True
    corrupt = bool(set(section) - _VALID_SECTION_KEYS)
    for kind in (_KIND_DENY, _KIND_READ_ONLY):
        globs = section.get(kind, [])
        if not isinstance(globs, list) or validate_globs(_str_list(globs)) \
                or len(_str_list(globs)) != len(globs):
            corrupt = True
    entries = section.get(_KIND_MASK, [])
    if not isinstance(entries, list) or any(validate_mask_entry(e)
                                            for e in entries):
        corrupt = True
    return section, corrupt


def _mask_list(value) -> list:
    """Recoverable mask entries for display; malformed ones are dropped."""
    if not isinstance(value, list):
        return []
    return [{"glob": _norm_glob(e.get("glob")), "preset": e.get("preset"),
             "params": e.get("params") or {}}
            for e in value if isinstance(e, dict) and not validate_mask_entry(e)]


def list_rules(project_path: str = ".") -> dict:
    """Raw config view per scope + the always-on ``builtin`` pseudo-scope.

    {"builtin": {"deny": [...], "read_only": [...], "corrupt": False},
     "global":  {"deny": [...], "read_only": [...], "corrupt": bool},
     "project": {...}}

    Builtin write-denies surface under ``read_only`` (they deny write-class
    operations only; reads stay open). Corrupt scopes still show whatever
    string globs are recoverable, flagged ``corrupt`` — that scope evaluates
    deny-all until the human repairs config.json by hand.
    """
    disabled = disabled_builtins()
    builtin_deny = list(BUILTIN_ABSOLUTE_DENY) + [
        g for g in BUILTIN_DENY if _norm_builtin(g) not in disabled]
    builtin_ro = [g for g in BUILTIN_WRITE_DENY
                  if _norm_builtin(g) not in disabled]
    inst = _install_dir_rule()
    if inst:
        builtin_ro.append(inst.glob)
    out = {"builtin": {
        # deny/read_only list what is ENFORCED right now, so a caller that
        # only reads these two keys is never told a disabled builtin is on.
        "deny": builtin_deny,
        "read_only": builtin_ro,
        "mask": [], "corrupt": False,
        # Opt-out surface for `c3 access list` and the Access tab.
        "absolute": list(BUILTIN_ABSOLUTE_DENY),
        "disableable": list(DISABLEABLE_BUILTINS),
        "disabled": sorted(disabled),
    }}
    for scope in _VALID_SCOPES:
        try:
            cfg = _scope_config_path(scope, project_path)
        except ValueError:
            out[scope] = {"deny": [], "read_only": [], "mask": [],
                          "corrupt": False}
            continue
        section, corrupt = _raw_scope_section(cfg)
        out[scope] = {
            _KIND_DENY: _str_list(section.get(_KIND_DENY)),
            _KIND_READ_ONLY: _str_list(section.get(_KIND_READ_ONLY)),
            _KIND_MASK: _mask_list(section.get(_KIND_MASK)),
            "corrupt": corrupt,
        }
    return out


def _load_config_for_write(cfg: Path) -> tuple:
    """(config_dict, access_section) for read-modify-write of one scope.

    Refuses corrupt state: unparseable JSON, a non-dict access section,
    unknown keys (especially ``allow``), or non-string-list globs are never
    silently rewritten — the scope fails closed until the human repairs
    config.json by hand (frozen spec §1).
    """
    data = {}
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise ValueError(
                f"{cfg} is not valid JSON — fix it by hand before editing "
                f"access rules ({exc})")
    if not isinstance(data, dict):
        raise ValueError(f"{cfg} root is not a JSON object — fix it by hand")
    section = data.setdefault("access", {})
    if not isinstance(section, dict) or set(section) - _VALID_SECTION_KEYS:
        raise ValueError(
            "access section is invalid (unknown keys or wrong shape) — the "
            "scope fails closed; fix config.json 'access' by hand")
    for kind in (_KIND_DENY, _KIND_READ_ONLY):
        globs = section.get(kind, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ValueError(
                f"access.{kind} must be a list of glob strings — the scope "
                "fails closed; fix config.json 'access' by hand")
    entries = section.get(_KIND_MASK, [])
    if not isinstance(entries, list):
        raise ValueError(
            "access.mask must be a list of {glob, preset, params} objects — "
            "the scope fails closed; fix config.json 'access' by hand")
    for entry in entries:
        bad = validate_mask_entry(entry)
        if bad:
            raise ValueError(
                f"access.mask has an invalid entry ({bad}) — the scope fails "
                "closed; fix config.json 'access' by hand")
    return data, section


def _write_scope_config(cfg: Path, data: dict) -> None:
    """Atomic same-directory replace; privileged internal write."""
    cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.with_name(cfg.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, cfg)


def set_rule(glob, kind: str, scope: str, project_path: str = ".") -> dict:
    """Add one rule to *scope*'s config. Human surfaces only; callers log.

    Returns {"glob", "kind", "scope", "added"} — ``added`` False when an
    equivalent (casefolded) glob is already present. Raises ValueError on an
    unknown kind/scope, an invalid glob, or a corrupt config.
    """
    if kind == _KIND_MASK:
        raise ValueError("mask rules carry a preset and params — use "
                         "set_mask_rule()/remove_mask_rule()")
    if kind not in _VALID_KEYS:
        raise ValueError(f"unknown kind '{kind}' — expected one of: "
                         f"{_KIND_DENY}, {_KIND_READ_ONLY}")
    canon = _norm_glob(glob)
    bad = validate_globs([canon])
    if bad:
        raise ValueError(f"invalid glob: {bad}")
    cfg = _scope_config_path(scope, project_path)
    data, section = _load_config_for_write(cfg)
    target = section.setdefault(kind, [])
    if any(_norm_glob(g).casefold() == canon.casefold() for g in target):
        return {"glob": canon, "kind": kind, "scope": scope, "added": False}
    target.append(canon)
    _write_scope_config(cfg, data)
    return {"glob": canon, "kind": kind, "scope": scope, "added": True}


def set_builtin_disabled(glob, disabled: bool) -> dict:
    """Switch a Tier-1 builtin off (or back on). GLOBAL scope, human surfaces
    only (`c3 access disable-builtin` / Access tab); callers log to the ledger.

    Writes BOTH keys — the config entry and the keyring attestation — because
    either alone is inert: config without attestation fails closed (the
    builtin keeps enforcing), and attestation without config is never read.

    Returns {"glob", "disabled", "changed", "attested"}. Raises ValueError for
    a Tier-0 vault glob, an unrecognized glob, an unavailable global scope, or
    a keyring that will not hold the attestation.
    """
    canon = _norm_builtin(glob)
    if canon in {_norm_builtin(g) for g in BUILTIN_ABSOLUTE_DENY}:
        raise ValueError(
            f"'{_norm_glob(glob)}' guards the credential vault and cannot be "
            "disabled. Secrets are reached through `c3 creds` with the "
            "per-entry agent_readable flag, not by widening the guard.")
    if canon not in {_norm_builtin(g) for g in DISABLEABLE_BUILTINS}:
        raise ValueError(
            f"'{_norm_glob(glob)}' is not a disableable builtin — expected one "
            f"of: {', '.join(DISABLEABLE_BUILTINS)}")

    cfg = _scope_config_path("global")  # raises when there is no home
    data, section = _load_config_for_write(cfg)
    listed = section.setdefault(_KEY_DISABLE_BUILTIN, [])
    if not isinstance(listed, list) or not all(isinstance(g, str) for g in listed):
        raise ValueError(
            "access.disable_builtin must be a list of glob strings — the scope "
            "fails closed; fix config.json 'access' by hand")
    present = any(_norm_builtin(g) == canon for g in listed)

    if disabled:
        # Attestation FIRST. If the keyring will not take it, leave config
        # untouched rather than persist a claim that evaluation ignores.
        if not _attest_builtin_disabled(canon, True):
            raise ValueError(
                "keyring unavailable, so the opt-out cannot be attested and "
                "would not take effect. The builtin stays enforced.")
        if not present:
            listed.append(_norm_glob(glob))
            _write_scope_config(cfg, data)
        return {"glob": canon, "disabled": True,
                "changed": not present, "attested": True}

    # Re-enabling: drop the config entry first (that alone restores
    # enforcement), then clear the attestation best-effort.
    if present:
        section[_KEY_DISABLE_BUILTIN] = [
            g for g in listed if _norm_builtin(g) != canon]
        _write_scope_config(cfg, data)
    attested = _attest_builtin_disabled(canon, False)
    return {"glob": canon, "disabled": False,
            "changed": present, "attested": attested}


def remove_rule(glob, kind: str, scope: str, project_path: str = ".") -> dict:
    """Remove one rule from *scope*'s config. Human surfaces only; callers log.

    Returns {"glob", "kind", "scope", "removed"}. Raises ValueError on an
    unknown kind/scope or a corrupt config (a corrupt section is repaired by
    hand, never rewritten here).
    """
    if kind == _KIND_MASK:
        raise ValueError("mask rules carry a preset and params — use "
                         "set_mask_rule()/remove_mask_rule()")
    if kind not in _VALID_KEYS:
        raise ValueError(f"unknown kind '{kind}' — expected one of: "
                         f"{_KIND_DENY}, {_KIND_READ_ONLY}")
    canon = _norm_glob(glob)
    cfg = _scope_config_path(scope, project_path)
    data, section = _load_config_for_write(cfg)
    target = section.get(kind, [])
    keep = [g for g in target if _norm_glob(g).casefold() != canon.casefold()]
    removed = len(keep) != len(target)
    if removed:
        section[kind] = keep
        _write_scope_config(cfg, data)
    return {"glob": canon, "kind": kind, "scope": scope, "removed": removed}


def set_mask_rule(glob, preset: str, params: dict | None = None,
                  scope: str = "project", project_path: str = ".") -> dict:
    """Add one mask rule to *scope*'s config. Human surfaces only.

    Returns {"glob", "preset", "params", "scope", "added", "replaced"}.
    A same-glob rule is REPLACED rather than duplicated: two rules on one glob
    with different presets is exactly the overlap that fails closed at
    evaluation time, so the write surface must not be able to create it.

    Callers are responsible for running the activation transaction
    (services/mask_activation.py) after this returns — the rule is not safe to
    rely on until derived artifacts have been purged.
    """
    canon = _norm_glob(glob)
    entry = {"glob": canon, "preset": preset, "params": dict(params or {})}
    bad = validate_mask_entry(entry)
    if bad:
        raise ValueError(bad)
    cfg = _scope_config_path(scope, project_path)
    data, section = _load_config_for_write(cfg)
    target = section.setdefault(_KIND_MASK, [])
    replaced = False
    for i, existing in enumerate(list(target)):
        if _norm_glob(existing.get("glob")).casefold() == canon.casefold():
            if existing == entry:
                return {**entry, "scope": scope, "added": False,
                        "replaced": False}
            target[i] = entry
            replaced = True
            break
    if not replaced:
        target.append(entry)
    _write_scope_config(cfg, data)
    return {**entry, "scope": scope, "added": not replaced,
            "replaced": replaced}


def remove_mask_rule(glob, scope: str = "project",
                     project_path: str = ".") -> dict:
    """Remove the mask rule for *glob* from *scope*. Human surfaces only."""
    canon = _norm_glob(glob)
    cfg = _scope_config_path(scope, project_path)
    data, section = _load_config_for_write(cfg)
    target = section.get(_KIND_MASK, [])
    keep = [e for e in target
            if _norm_glob(e.get("glob")).casefold() != canon.casefold()]
    removed = len(keep) != len(target)
    if removed:
        section[_KIND_MASK] = keep
        _write_scope_config(cfg, data)
    return {"glob": canon, "scope": scope, "removed": removed}


def masked_globs(project_path: str = ".") -> list:
    """Every active mask glob — the activation purge's work list."""
    mask_rules, _corrupt = load_mask_rules(project_path)
    return sorted({m.glob for m in mask_rules})
