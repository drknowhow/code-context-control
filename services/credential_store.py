"""Named credential vault — global and per-project secrets for C3.

Secrets live in the OS-native secret store (Windows Credential Manager,
macOS Keychain, Linux Secret Service) via the ``keyring`` library, keyed by
``(realm, name)`` where realm is ``global`` or ``proj|<resolved path>``.
Values larger than ``FILE_STORAGE_THRESHOLD`` bytes are routed to a
Fernet-encrypted sidecar (``.c3/secrets.enc``) whose random master key
itself lives in the keyring — Windows caps credential blobs at ~2.5KB.

Only the non-secret registry (description, type, env_var, flags, storage,
value_len, timestamps) lives in ``.c3/config.json`` (project scope) or
``~/.c3/config.json`` (global scope). Values are NEVER written to config,
and no function in this module returns a value except ``get_value`` /
``resolve`` / ``expand_templates`` — agent-facing surfaces must only call
those at the subprocess boundary (see cli/tools/shell.py) or behind the
per-entry ``agent_readable`` gate (see cli/tools/credentials.py).

Resolution is per-entry and realm-atomic: when a project's registry has a
name, the project realm is authoritative for it — a project entry whose
value is missing does NOT fall through to the global value. This is the
security invariant that keeps a cloned repository's committed
``.c3/config.json`` from redirecting or auto-injecting secrets it does not
own: behavioral flags (``inject``, ``agent_readable``) are only honored
from the realm that actually holds the value.

Realms embed the resolved project path, so moving a project directory
orphans its project-scoped keyring values (re-set them after a move).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

KEYRING_SERVICE = "c3-creds"
VALID_TYPES = ("token", "env", "multiline")
VALID_SCOPES = ("project", "global")
FILE_STORAGE_THRESHOLD = 1024  # bytes; larger values go to the encrypted sidecar

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TEMPLATE_RE = re.compile(r"\{\{cred:([A-Za-z_][A-Za-z0-9_]*)\}\}")

# Process-local plaintext of values injected/revealed this session, so tool
# output that echoes one back can be scrubbed before it reaches the model.
# Best-effort: a transformed (base64'd, split, …) value will not match.
_ACTIVE_SECRETS: dict[str, str] = {}


def _keyring_module():
    """Import keyring lazily so import-time failures don't crash unrelated code."""
    try:
        import keyring  # type: ignore[import-untyped]
        return keyring
    except ImportError as exc:  # pragma: no cover — only triggered on broken installs
        raise RuntimeError(
            "The 'keyring' package is required for credential storage. "
            "Run: pip install keyring"
        ) from exc


def _crypto_module():
    """Return the Fernet class, imported lazily — only needed for large values."""
    try:
        from cryptography.fernet import Fernet  # type: ignore[import-untyped]
        return Fernet
    except ImportError as exc:
        raise CredentialError(
            "The 'cryptography' package is required to store values larger "
            f"than {FILE_STORAGE_THRESHOLD} bytes. Run: pip install cryptography"
        ) from exc


class CredentialError(RuntimeError):
    """Raised when keyring/crypto/config I/O fails or validation rejects input."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_name(name: str, *, what: str = "name") -> str:
    if not name or not _NAME_RE.match(name):
        raise CredentialError(
            f"{what} must match [A-Za-z_][A-Za-z0-9_]* (env-var safe), got {name!r}"
        )
    return name


def _global_base() -> Optional[Path]:
    """Home directory for global scope, or None when unresolvable."""
    try:
        return Path.home()
    except Exception:
        return None


def global_base() -> Optional[Path]:
    """Public accessor for the global-scope base directory (home)."""
    return _global_base()


def _project_is_home(project_path: str) -> bool:
    home = _global_base()
    if home is None:
        return False
    try:
        return Path(project_path).resolve() == home.resolve()
    except Exception:
        return False


def _norm_scope(scope: str, project_path: str) -> str:
    """Validate scope; coerce project→global when the project dir IS home
    (both would read/write the same config file — one realm must own it)."""
    if scope not in VALID_SCOPES:
        raise CredentialError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
    if scope == "project" and _project_is_home(project_path):
        return "global"
    return scope


def realm(scope: str, project_path: str = ".") -> str:
    """Keyring realm string: ``global`` or ``proj|<resolved project path>``."""
    scope = _norm_scope(scope, project_path)
    if scope == "global":
        return "global"
    return "proj|" + os.path.normcase(str(Path(project_path).resolve()))


def _account(realm_s: str, name: str) -> str:
    return f"{realm_s}|{name}"


def _flag_account(realm_s: str, name: str) -> str:
    return f"{realm_s}|{name}::agent_readable"


# Files the agent must never write through c3 tool surfaces: the vault
# registry (config.json) and its sidecar state. Editing these outside the
# credentials API is how a prompt-injected agent would grant itself reveal
# access. Mirrored in cli/hook_pretool_enforce.py (parity-tested).
VAULT_PROTECTED_FILES = frozenset({"config.json", "secrets.enc", "cred_state.json"})


def vault_guard_reason(path) -> str:
    """Non-empty refusal message when ``path`` is a vault file under .c3/."""
    p = Path(path)
    if p.name.lower() in VAULT_PROTECTED_FILES and p.parent.name.lower() == ".c3":
        return (
            f"[c3:vault-protected] {p.name} belongs to the credential vault "
            "(registry/state) and cannot be modified by the agent. Changes go "
            "through the Credentials UI or the `c3 creds` CLI — ask the user."
        )
    return ""


def _write_flag_attestation(realm_s: str, name: str, readable: bool) -> None:
    """Keyring copy of agent_readable — reveal trusts registry AND keyring.

    A registry flag flipped by editing config.json directly (outside this
    API) disagrees with the attestation, and reveal fails closed.
    """
    try:
        _keyring_module().set_password(
            KEYRING_SERVICE, _flag_account(realm_s, name), "1" if readable else "0")
    except Exception:
        pass  # keyring-less env: verify_agent_readable fails closed anyway


def verify_agent_readable(name: str, *, scope: str, project_path: str = ".") -> bool:
    """True only when the keyring attestation agrees the flag is enabled.

    Fails closed: a missing attestation (pre-v2.61.2 entry, keyring failure,
    or a registry edited outside this API) reads as not-readable.
    """
    try:
        realm_s = realm(_norm_scope(scope, project_path), project_path)
        return _keyring_module().get_password(
            KEYRING_SERVICE, _flag_account(realm_s, name)) == "1"
    except Exception:
        return False


def _scope_dir(scope: str, project_path: str) -> Optional[Path]:
    """Base directory holding .c3/ for the scope; None when global is unresolvable."""
    if scope == "global":
        return _global_base()
    return Path(project_path)


# ── Config registry I/O ───────────────────────────────────


def _config_path(base: Path) -> Path:
    return base / ".c3" / "config.json"


def _load_config(base: Path) -> dict:
    path = _config_path(base)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(base: Path, config: dict) -> None:
    path = _config_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _creds_section(config: dict) -> dict:
    section = config.get("credentials")
    if not isinstance(section, dict):
        section = {}
    entries = section.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    section["entries"] = entries
    return section


def _read_entries(scope: str, project_path: str) -> dict:
    """Registry entries of ONE scope's config file (no cross-scope merging)."""
    base = _scope_dir(scope, project_path)
    if base is None:
        return {}
    section = _creds_section(_load_config(base))
    return {n: e for n, e in section["entries"].items() if isinstance(e, dict)}


def _owning_scope(name: str, project_path: str) -> str:
    """Which scope's registry owns this name: project shadows global, and a
    project entry never falls through to the global value (realm-atomic)."""
    if not _project_is_home(project_path) and name in _read_entries("project", project_path):
        return "project"
    if name in _read_entries("global", project_path):
        return "global"
    return ""


# ── Encrypted sidecar (large values) ──────────────────────


def _master_key(realm_s: str, *, create: bool) -> Optional[bytes]:
    keyring = _keyring_module()
    account = "master|" + realm_s
    try:
        key = keyring.get_password(KEYRING_SERVICE, account)
    except Exception:
        key = None
    if key:
        return key.encode("utf-8")
    if not create:
        return None
    fernet_cls = _crypto_module()
    new_key = fernet_cls.generate_key().decode("utf-8")
    try:
        keyring.set_password(KEYRING_SERVICE, account, new_key)
    except Exception as exc:
        raise CredentialError(f"keyring write failed for master key: {exc}") from exc
    return new_key.encode("utf-8")


def _secrets_path(scope: str, project_path: str) -> Optional[Path]:
    base = _scope_dir(scope, project_path)
    return None if base is None else base / ".c3" / "secrets.enc"


_GITIGNORE_ENTRIES = ("secrets.enc", "cred_state.json")


def _ensure_c3_gitignore(base: Path) -> None:
    """Keep the encrypted sidecar and usage state out of git even in projects
    that track their .c3/ directory. Idempotent, append-only, never raises."""
    try:
        path = base / ".c3" / ".gitignore"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = {ln.strip() for ln in existing.splitlines()}
        missing = [e for e in _GITIGNORE_ENTRIES if e not in lines]
        if not missing:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        joined = "".join(f"{e}\n" for e in missing)
        with open(path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(joined)
    except Exception:
        pass


def _load_sidecar(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _file_set(scope: str, project_path: str, realm_s: str, name: str, value: str) -> None:
    path = _secrets_path(scope, project_path)
    if path is None:
        raise CredentialError("global scope unavailable: no home directory")
    fernet_cls = _crypto_module()
    fernet = fernet_cls(_master_key(realm_s, create=True))
    data = _load_sidecar(path)
    data[name] = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    _ensure_c3_gitignore(path.parent.parent)


def _file_get(scope: str, project_path: str, realm_s: str, name: str) -> Optional[str]:
    path = _secrets_path(scope, project_path)
    if path is None:
        return None
    token = _load_sidecar(path).get(name)
    if not token:
        return None
    key = _master_key(realm_s, create=False)
    if key is None:
        return None
    try:
        fernet = _crypto_module()(key)
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _file_delete(scope: str, project_path: str, name: str) -> bool:
    path = _secrets_path(scope, project_path)
    if path is None:
        return False
    data = _load_sidecar(path)
    if name not in data:
        return False
    data.pop(name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────


def set_credential(
    name: str,
    value: str,
    *,
    scope: str = "project",
    project_path: str = ".",
    description: str = "",
    ctype: str = "token",
    env_var: str = "",
    agent_readable: bool = False,
    inject: bool = False,
) -> dict:
    """Store the value (keyring or encrypted sidecar) and register the entry.

    Returns the non-secret registry entry. ``env_var`` defaults to the entry
    name at injection time when empty. Callers enforcing the "agent cannot
    raise agent_readable on an existing entry" rule must check the current
    entry first — this layer stores what it is told.
    """
    _validate_name(name)
    if not value:
        raise CredentialError("value is required")
    if ctype not in VALID_TYPES:
        raise CredentialError(f"type must be one of {VALID_TYPES}, got {ctype!r}")
    if env_var:
        _validate_name(env_var, what="env_var")
    scope = _norm_scope(scope, project_path)
    base = _scope_dir(scope, project_path)
    if base is None:
        raise CredentialError("global scope unavailable: no home directory")
    realm_s = realm(scope, project_path)

    raw = value.encode("utf-8")
    storage = "file" if len(raw) > FILE_STORAGE_THRESHOLD else "keyring"
    if storage == "keyring":
        keyring = _keyring_module()
        try:
            keyring.set_password(KEYRING_SERVICE, _account(realm_s, name), value)
        except Exception as exc:
            raise CredentialError(f"keyring write failed: {exc}") from exc
        _file_delete(scope, project_path, name)  # value may have shrunk past the threshold
    else:
        _file_set(scope, project_path, realm_s, name, value)
        try:
            _keyring_module().delete_password(KEYRING_SERVICE, _account(realm_s, name))
        except Exception:
            pass

    config = _load_config(base)
    section = _creds_section(config)
    prev = section["entries"].get(name)
    created = prev.get("created") if isinstance(prev, dict) else ""
    now = _utcnow()
    entry = {
        "description": description,
        "type": ctype,
        "env_var": env_var,
        "agent_readable": bool(agent_readable),
        "inject": bool(inject),
        "storage": storage,
        "value_len": len(raw),
        "created": created or now,
        "updated": now,
    }
    section["entries"][name] = entry
    config["credentials"] = section
    _save_config(base, config)
    _write_flag_attestation(realm_s, name, bool(agent_readable))
    return dict(entry)


_UPDATABLE_FIELDS = ("description", "type", "env_var", "agent_readable", "inject")


def update_metadata(name: str, *, scope: str, project_path: str = ".", **fields) -> dict:
    """Update non-secret fields of an existing entry; the value is untouched."""
    scope = _norm_scope(scope, project_path)
    base = _scope_dir(scope, project_path)
    if base is None:
        raise CredentialError("global scope unavailable: no home directory")
    unknown = set(fields) - set(_UPDATABLE_FIELDS)
    if unknown:
        raise CredentialError(f"unknown metadata fields: {sorted(unknown)}")
    if "type" in fields and fields["type"] not in VALID_TYPES:
        raise CredentialError(f"type must be one of {VALID_TYPES}")
    if fields.get("env_var"):
        _validate_name(fields["env_var"], what="env_var")
    config = _load_config(base)
    section = _creds_section(config)
    entry = section["entries"].get(name)
    if not isinstance(entry, dict):
        raise CredentialError(f"unknown credential {name!r} in {scope} scope")
    for key in ("agent_readable", "inject"):
        if key in fields:
            fields[key] = bool(fields[key])
    entry.update(fields)
    entry["updated"] = _utcnow()
    config["credentials"] = section
    _save_config(base, config)
    if "agent_readable" in fields:
        _write_flag_attestation(
            realm(scope, project_path), name, bool(fields["agent_readable"]))
    return dict(entry)


def get_entry(name: str, *, project_path: str = ".") -> dict:
    """Resolved non-secret entry with ``name`` and ``scope`` included, else {}."""
    scope = _owning_scope(name, project_path)
    if not scope:
        return {}
    entry = dict(_read_entries(scope, project_path)[name])
    entry["name"] = name
    entry["scope"] = scope
    return entry


def list_entries(project_path: str = ".") -> dict:
    """Merged registry view — global entries shadowed per-name by project ones.
    Values are never included."""
    merged: dict[str, dict] = {}
    for scope in ("global", "project"):
        if scope == "project" and _project_is_home(project_path):
            continue
        for name, entry in _read_entries(scope, project_path).items():
            out = dict(entry)
            out["scope"] = scope
            merged[name] = out
    return dict(sorted(merged.items()))


PUBLIC_FIELDS = ("scope", "type", "value_len", "env_var", "inject",
                 "agent_readable", "description", "storage", "created",
                 "updated")


def public_entry(name: str, entry: dict, *, usage=None,
                 shadows_global=None) -> dict:
    """Explicit allowlist serializer — structurally cannot emit a value.

    Lives here, beside _UPDATABLE_FIELDS and the entry-shape literal that
    define these field names, so a new field cannot be added to the store and
    silently leak through a surface that forgot to update its own copy. Every
    HTTP surface serializes through this."""
    rec = {"name": name}
    for key in PUBLIC_FIELDS:
        rec[key] = entry.get(key, "")
    rec["value_len"] = entry.get("value_len", 0)
    rec["inject"] = bool(entry.get("inject"))
    rec["agent_readable"] = bool(entry.get("agent_readable"))
    if usage is not None:
        rec["last_used"] = (usage.get(name) or {}).get("last_used", "")
        rec["use_count"] = (usage.get(name) or {}).get("use_count", 0)
    if shadows_global is not None:
        rec["shadows_global"] = bool(shadows_global)
    return rec


def is_resolvable(name: str, *, project_path: str = ".", scope: str = "") -> bool:
    """Whether the stored value can still be decoded — a bool, never the value.

    Exists so a surface that must provably never hold plaintext (the mobile
    gateway) can answer "is this credential still good?" without importing
    get_value at all. That absence is what the source-grep invariant test
    asserts, which is a stronger guarantee than reviewing call sites."""
    return get_value(name, project_path=project_path, scope=scope) is not None


def get_value(name: str, *, project_path: str = ".", scope: str = "") -> Optional[str]:
    """Decoded value from the owning realm, or None. Never falls through:
    a project-registered name resolves in the project realm or not at all."""
    owning = _norm_scope(scope, project_path) if scope else _owning_scope(name, project_path)
    if not owning:
        return None
    entries = _read_entries(owning, project_path)
    entry = entries.get(name)
    if not isinstance(entry, dict):
        return None
    realm_s = realm(owning, project_path)
    if entry.get("storage") == "file":
        return _file_get(owning, project_path, realm_s, name)
    try:
        return _keyring_module().get_password(KEYRING_SERVICE, _account(realm_s, name))
    except Exception:
        return None


def delete_credential(name: str, *, scope: str, project_path: str = ".") -> bool:
    """Remove the value (keyring + sidecar) and the registry entry."""
    scope = _norm_scope(scope, project_path)
    base = _scope_dir(scope, project_path)
    if base is None:
        return False
    realm_s = realm(scope, project_path)
    removed_value = _file_delete(scope, project_path, name)
    try:
        _keyring_module().delete_password(KEYRING_SERVICE, _account(realm_s, name))
        removed_value = True
    except Exception:
        pass
    try:
        _keyring_module().delete_password(KEYRING_SERVICE, _flag_account(realm_s, name))
    except Exception:
        pass
    config = _load_config(base)
    section = _creds_section(config)
    removed_entry = name in section["entries"]
    section["entries"].pop(name, None)
    config["credentials"] = section
    _save_config(base, config)
    _ACTIVE_SECRETS.pop(name, None)
    return removed_value or removed_entry


def resolve(names: list, project_path: str = ".") -> tuple:
    """``({name: value}, [missing])`` for injection. Registers each resolved
    value for output redaction."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        val = get_value(name, project_path=project_path)
        if val is None:
            missing.append(name)
        else:
            values[name] = val
            register_active_secret(name, val)
    return values, missing


def fingerprint(name: str, *, project_path: str = ".") -> str:
    """First 8 hex of sha256(value), computed live — deliberately NOT persisted
    in config.json (a committed fingerprint would be an offline guessing
    oracle for weak secrets). Empty string when unresolvable."""
    val = get_value(name, project_path=project_path)
    if val is None:
        return ""
    return hashlib.sha256(val.encode("utf-8")).hexdigest()[:8]


def touch_last_used(names: list, project_path: str = ".") -> None:
    """Record last_used/use_count in .c3/cred_state.json of each entry's owning
    scope. Volatile state — deliberately kept out of config.json. Never raises."""
    for name in names:
        try:
            scope = _owning_scope(name, project_path)
            if not scope:
                continue
            base = _scope_dir(scope, project_path)
            if base is None:
                continue
            path = base / ".c3" / "cred_state.json"
            state: dict = {}
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        state = loaded
                except Exception:
                    state = {}
            rec = state.get(name) if isinstance(state.get(name), dict) else {}
            rec = {"last_used": _utcnow(), "use_count": int(rec.get("use_count", 0)) + 1}
            state[name] = rec
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            _ensure_c3_gitignore(base)
        except Exception:
            continue


def read_usage_state(project_path: str = ".") -> dict:
    """Merged {name: {last_used, use_count}} across both scopes (project wins)."""
    merged: dict[str, dict] = {}
    for scope in ("global", "project"):
        base = _scope_dir(scope, project_path)
        if base is None:
            continue
        path = base / ".c3" / "cred_state.json"
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            continue
        if isinstance(state, dict):
            for name, rec in state.items():
                if isinstance(rec, dict):
                    merged[name] = dict(rec)
    return merged


def import_env(
    text: str, *, scope: str = "project", project_path: str = ".", overwrite: bool = False
) -> dict:
    """Parse KEY=VALUE lines (.env style) into credentials of type ``env``.

    Skips comments/blank lines, tolerates a leading ``export ``, strips one
    layer of matching quotes. Without ``overwrite``, names already registered
    in the TARGET scope are skipped (shadowing the other scope is allowed).
    Returns {"created": [...], "skipped": [...]}.
    """
    scope = _norm_scope(scope, project_path)
    existing = _read_entries(scope, project_path)
    created: list[str] = []
    skipped: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        if not _NAME_RE.match(key) or not val:
            skipped.append(key or line[:24])
            continue
        if not overwrite and key in existing:
            skipped.append(key)
            continue
        set_credential(key, val, scope=scope, project_path=project_path, ctype="env")
        created.append(key)
    return {"created": created, "skipped": skipped}


def expand_templates(cmd: str, project_path: str = ".") -> tuple:
    """Replace ``{{cred:NAME}}`` with decoded values, server-side only.

    Returns ``(expanded_cmd, used_names, missing_names)``. Callers must log
    the RAW template form, never the expanded string, and must scrub captured
    output with :func:`redact_text`.
    """
    used: list[str] = []
    missing: list[str] = []

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        val = get_value(name, project_path=project_path)
        if val is None:
            missing.append(name)
            return match.group(0)
        used.append(name)
        register_active_secret(name, val)
        return val

    return _TEMPLATE_RE.sub(_sub, cmd or ""), used, missing


def register_active_secret(name: str, value: str) -> None:
    """Track a decoded value (process-local) so later output can be scrubbed."""
    if value and len(value) >= 4:
        _ACTIVE_SECRETS[name] = value


def redact_text(text: str) -> str:
    """Replace any tracked decoded value with ``[cred:NAME]``. Best-effort."""
    if not text or not _ACTIVE_SECRETS:
        return text
    for name, value in _ACTIVE_SECRETS.items():
        if value in text:
            text = text.replace(value, f"[cred:{name}]")
    return text


def redact_obj(obj):
    """Recursively redact tracked values from nested dict/list/str structures
    (used on tool-call args before they are persisted to logs)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
