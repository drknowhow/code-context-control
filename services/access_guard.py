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
_VALID_KEYS = {_KIND_DENY, _KIND_READ_ONLY}

# Builtins: product integrity only (docs/access-guard.md §1). Non-overridable.
BUILTIN_DENY = ("**/.env*", "**/.c3/secrets.enc", "**/.c3/cred_state.json")
BUILTIN_WRITE_DENY = ("**/.c3/**", "**/.claude/settings*.json", "**/.git/**")

# Seeded into GLOBAL scope by install/CLI (user-removable); not enforced here.
DEFAULT_GLOBAL_RULES = ("*.pem", "id_rsa*", "*.key")

# Stable machine tags — API, do not change (docs/access-guard.md §4).
TAG_DENIED = "[c3-access:denied]"
TAG_READ_ONLY = "[c3-access:read_only]"
TAG_LIMITED = "[c3-access:limited]"

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


_BUILTIN_RULES = tuple(
    [_compile(g, _KIND_DENY, "builtin") for g in BUILTIN_DENY]
    + [_compile(g, _KIND_READ_ONLY, "builtin") for g in BUILTIN_WRITE_DENY]
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
    """List of Rule for one scope, [] when absent, _CORRUPT when invalid."""
    cfg = base / ".c3" / "config.json"
    if not cfg.is_file():
        return []
    try:
        section = (json.loads(cfg.read_text(encoding="utf-8"))
                   or {}).get("access")
    except Exception:
        return _CORRUPT
    if section is None:
        return []
    if not isinstance(section, dict):
        return _CORRUPT
    unknown = set(section) - _VALID_KEYS
    if unknown:
        return _CORRUPT  # hard error — 'allow' must never silently no-op
    rules = []
    for kind in (_KIND_DENY, _KIND_READ_ONLY):
        globs = section.get(kind, [])
        if not isinstance(globs, list) or validate_globs(globs):
            return _CORRUPT
        rules.extend(_compile(g, kind, scope) for g in globs)
    return rules


def _global_base() -> Path | None:
    try:
        home = Path.home()
        return home if str(home) not in ("", "/") else None
    except Exception:
        return None


def load_rules(project_path: str = ".") -> tuple:
    """(rules, corrupt_scopes) — union of builtin + global + project scopes."""
    rules = list(_BUILTIN_RULES)
    inst = _install_dir_rule()
    if inst:
        rules.append(inst)
    corrupt = []
    gbase = _global_base()
    proj = Path(project_path).resolve()
    for scope, base in (("global", gbase), ("project", proj)):
        if base is None or (scope == "global" and gbase == proj):
            continue
        scoped = _read_scope_rules(base, scope)
        if scoped is _CORRUPT:
            corrupt.append(scope)
        else:
            rules.extend(scoped)
    return rules, corrupt


def has_active_rules(project_path: str = ".") -> bool:
    """True when any user rules (or corrupt scopes) exist — drives S4."""
    rules, corrupt = load_rules(project_path)
    # Count the install-dir rule only when it actually loaded: in a dev
    # checkout it is absent, and a fixed "+1" made a single user rule
    # invisible to S4 (footer suppressed while filtering was active).
    n_baseline = len(_BUILTIN_RULES) + (1 if _install_dir_rule() else 0)
    return bool(corrupt) or len(rules) > n_baseline


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

def check(path, operation: str, project_path: str = ".") -> Denial | None:
    """None when allowed; a Denial otherwise. operation: read|write|create|delete.

    create/delete evaluate as write. Builtin write-denies apply only to
    write-class operations; ``deny`` rules apply to everything (R1+R2).
    """
    op_write = operation in ("write", "create", "delete")
    canon, rel, denial = canonicalize(path, project_path)
    if denial:
        return denial
    rules, corrupt = load_rules(project_path)
    if corrupt:
        return Denial("<corrupt-config>", _KIND_DENY, ",".join(corrupt),
                      "access section invalid or unparseable — scope fails "
                      "closed (fix .c3/config.json 'access')")
    name = canon.rsplit("/", 1)[-1]
    hit_ro = None
    for rule in rules:
        if not rule.matches(canon, rel, name):
            continue
        if rule.kind == _KIND_DENY:
            return Denial(rule.glob, _KIND_DENY, rule.scope, "deny rule")
        if hit_ro is None:
            hit_ro = rule
    if hit_ro is not None and op_write:
        return Denial(hit_ro.glob, _KIND_READ_ONLY, hit_ro.scope,
                      "read-only rule")
    return None


# ── Refusal strings (docs/access-guard.md §4 — verbatim) ────────────────────

def _cap(s: str) -> str:
    s = str(s)
    if len(s) <= _PATH_CAP:
        return s
    return s[: _PATH_CAP // 2 - 2] + " … " + s[-(_PATH_CAP // 2 - 2):]


def refusal(denial: Denial, path, operation: str, *, surface: str = "mcp",
            tool: str = "", project: str = "") -> str:
    """The exact S1/S2/S3/S5 string for a denial (see frozen spec)."""
    p, glob, scope = _cap(path), denial.rule, denial.scope
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
        )
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


def enforce(path, operation: str, project_path: str = ".", *,
            surface: str = "mcp", tool: str = "", project: str = "") -> None:
    """Raise AccessDenied when ``operation`` on ``path`` is not permitted."""
    denial = check(path, operation, project_path)
    if denial:
        raise AccessDenied(denial, refusal(
            denial, path, operation, surface=surface, tool=tool,
            project=project))


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
    "non-Claude agents' raw shell, direct file APIs, editors."
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
    corrupt = bool(set(section) - _VALID_KEYS)
    for kind in _VALID_KEYS:
        globs = section.get(kind, [])
        if not isinstance(globs, list) or validate_globs(_str_list(globs)) \
                or len(_str_list(globs)) != len(globs):
            corrupt = True
    return section, corrupt


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
    builtin_ro = list(BUILTIN_WRITE_DENY)
    inst = _install_dir_rule()
    if inst:
        builtin_ro.append(inst.glob)
    out = {"builtin": {"deny": list(BUILTIN_DENY), "read_only": builtin_ro,
                       "corrupt": False}}
    for scope in _VALID_SCOPES:
        try:
            cfg = _scope_config_path(scope, project_path)
        except ValueError:
            out[scope] = {"deny": [], "read_only": [], "corrupt": False}
            continue
        section, corrupt = _raw_scope_section(cfg)
        out[scope] = {
            _KIND_DENY: _str_list(section.get(_KIND_DENY)),
            _KIND_READ_ONLY: _str_list(section.get(_KIND_READ_ONLY)),
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
    if not isinstance(section, dict) or set(section) - _VALID_KEYS:
        raise ValueError(
            "access section is invalid (unknown keys or wrong shape) — the "
            "scope fails closed; fix config.json 'access' by hand")
    for kind in _VALID_KEYS:
        globs = section.get(kind, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise ValueError(
                f"access.{kind} must be a list of glob strings — the scope "
                "fails closed; fix config.json 'access' by hand")
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


def remove_rule(glob, kind: str, scope: str, project_path: str = ".") -> dict:
    """Remove one rule from *scope*'s config. Human surfaces only; callers log.

    Returns {"glob", "kind", "scope", "removed"}. Raises ValueError on an
    unknown kind/scope or a corrupt config (a corrupt section is repaired by
    hand, never rewritten here).
    """
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
