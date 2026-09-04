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
# Plain kinds hold one opaque value; structured kinds hold a JSON object of
# named fields, addressed as NAME.field at the injection boundary and never
# resolvable whole (get_value without a field returns None for them).
VALID_TYPES = ("token", "env", "multiline", "address", "identity", "card",
               "login")
STRUCTURED_TYPES = frozenset({"address", "identity", "card", "login"})
VALID_SCOPES = ("project", "global")
FILE_STORAGE_THRESHOLD = 1024  # bytes; larger values go to the encrypted sidecar

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TEMPLATE_RE = re.compile(
    r"\{\{cred:([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?\}\}")

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


def _struct_account(realm_s: str, name: str) -> str:
    return f"{realm_s}|{name}::structured"


# Files the agent must never write through c3 tool surfaces: the vault
# registry (config.json) and its sidecar state. Editing these outside the
# credentials API is how a prompt-injected agent would grant itself reveal
# access. Mirrored in cli/hook_pretool_enforce.py (parity-tested).
VAULT_PROTECTED_FILES = frozenset({
    "config.json", "secrets.enc", "cred_state.json",
    # Usage telemetry (services/cred_telemetry.py) — matching is exact
    # filename, so the rotation file needs its own entry.
    "cred_usage.jsonl", "cred_usage.jsonl.1",
})


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


def _write_struct_attestation(realm_s: str, name: str, ctype: str) -> None:
    """Keyring copy of an entry's structured-ness, mirroring the
    agent_readable attestation: a registry ``type`` rewritten by editing
    config.json directly cannot demote a card back to a whole-value token.
    Plain entries clear the attestation."""
    keyring = _keyring_module()
    account = _struct_account(realm_s, name)
    try:
        if ctype in STRUCTURED_TYPES:
            keyring.set_password(KEYRING_SERVICE, account, ctype)
        else:
            keyring.delete_password(KEYRING_SERVICE, account)
    except Exception:
        pass  # union with the registry below still fails toward restriction


def structured_type(name: str, *, project_path: str = ".", scope: str = "") -> str:
    """The structured ctype of an entry, or "" for plain entries.

    Union of the registry ``type`` and the keyring attestation — if EITHER
    says structured, the entry is treated as structured. That direction is
    deliberate: a hostile registry edit can only ADD restrictions (a token
    rewritten to "card" stops resolving whole, which is a nuisance, not a
    disclosure), never strip them from a real card.
    """
    owning = _norm_scope(scope, project_path) if scope else _owning_scope(name, project_path)
    if not owning:
        return ""
    entry = _read_entries(owning, project_path).get(name)
    reg_type = entry.get("type", "") if isinstance(entry, dict) else ""
    att = ""
    try:
        att = _keyring_module().get_password(
            KEYRING_SERVICE, _struct_account(realm(owning, project_path), name)) or ""
    except Exception:
        att = ""
    if att in STRUCTURED_TYPES:
        return att
    if reg_type in STRUCTURED_TYPES:
        return reg_type
    return ""


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


_GITIGNORE_ENTRIES = ("secrets.enc", "cred_state.json", "cred_usage.jsonl*")


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


# ── Structured kinds (address / identity / card / login) ─
# One canonical JSON object per entry, stored through the same
# keyring/Fernet path as any other value. Validation errors name FIELD
# NAMES only — never the submitted content, which would put the very
# values this feature exists to protect into error text, tool output,
# and the ledger.

_SCHEMAS: dict = {
    "card": {
        "required": ("cardholder", "number", "expiry"),
        "optional": ("cvc", "billing_zip"),
    },
    "address": {
        "required": ("street1", "city", "state", "zip"),
        "optional": ("recipient", "street2", "country", "phone"),
    },
    "identity": {
        "required": ("full_name",),
        "optional": ("dob", "ssn", "phone", "email"),
    },
    # A login — a website, a server, a database, anything with one
    # unambiguous target. STORAGE ONLY: C3 never drives a browser, opens an
    # SSH session, or types these anywhere.
    #
    # `canonical_target` is the binding an external, out-of-process runner is
    # required to check against where it is ACTUALLY about to authenticate,
    # BEFORE typing. It lives in the record rather than being passed in by
    # the caller for one reason: an agent that can choose the destination can
    # exfiltrate the password to it.
    #
    # `canonical_origin` (v2.58.0-v2.118.0) stays a KNOWN field so stored
    # entries and partial updates keep validating; on input it is an alias
    # that normalizes into `canonical_target`, and on read it resolves ONLY
    # for an https target (see get_value). A browser broker asking for it on
    # an `ssh://` entry gets nothing, which is the fail-closed direction.
    #
    # `password` is optional because a key-based server login has none — but
    # one of password/private_key is required, checked in _validate_structured.
    "login": {
        "required": ("site_id", "canonical_target", "username"),
        "optional": ("password", "private_key", "passphrase", "totp_secret",
                     "canonical_origin"),
    },
}

_STRUCT_FIELD_MAX = 256  # chars per field value
#: Per-field overrides. A PEM/OpenSSH private key is thousands of characters,
#: and raising the global cap to fit it would loosen every other field. The
#: oversize path already exists: a structured blob over FILE_STORAGE_THRESHOLD
#: goes to the Fernet sidecar rather than the keyring.
_STRUCT_FIELD_MAX_BY_FIELD = {"private_key": 8192}


def _field_max(field: str) -> int:
    return _STRUCT_FIELD_MAX_BY_FIELD.get(field, _STRUCT_FIELD_MAX)


def schema_fields(ctype: str) -> tuple:
    """(required, optional) field names for a structured ctype."""
    spec = _SCHEMAS.get(ctype) or {}
    return tuple(spec.get("required", ())), tuple(spec.get("optional", ()))


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_brand(number: str) -> str:
    """Display-only brand from the IIN prefix; never affects validation."""
    if number.startswith("4"):
        return "visa"
    if number[:2] in {"34", "37"}:
        return "amex"
    two, four = number[:2], number[:4]
    if two in {"51", "52", "53", "54", "55"} or (
            four.isdigit() and 2221 <= int(four) <= 2720):
        return "mastercard"
    if four == "6011" or two == "65" or number[:3] in {"644", "645", "646",
                                                       "647", "648", "649"}:
        return "discover"
    return "card"


def _normalize_expiry(raw: str) -> str:
    """Accept MM/YY, M/YY, MM/YYYY, YYYY-MM, MM-YY; return MM/YY or raise."""
    s = raw.strip().replace("-", "/")
    parts = s.split("/")
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        if len(a) == 4 and a.isdigit():  # YYYY/MM
            year, month = a, b
        else:  # MM/YY or MM/YYYY
            month, year = a, b
        if month.isdigit() and year.isdigit() and 1 <= int(month) <= 12 \
                and len(year) in (2, 4):
            return f"{int(month):02d}/{year[-2:]}"
    raise CredentialError("field 'expiry' must be MM/YY (or MM/YYYY, YYYY-MM)")


_SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


#: Schemes a login target may name. An allowlist, not a denylist: validation
#: has to be deterministic, and the UI renders a picker from it. Every member
#: either encrypts by definition or negotiates TLS in normal use.
TARGET_SCHEMES = (
    "https",                                   # web
    "ssh", "sftp", "ftps", "rdp", "smb", "vnc", "winrm",   # hosts
    "ldaps", "imaps", "smtps", "amqps",                    # services
    "postgres", "postgresql", "mysql", "mariadb", "mssql",  # databases
    "mongodb", "redis",
)
#: Refused by name rather than by omission, so the error can say why.
_CLEARTEXT_SCHEMES = {
    "http": "https", "ftp": "ftps", "telnet": "ssh", "rsh": "ssh",
    "smtp": "smtps", "imap": "imaps", "ldap": "ldaps", "amqp": "amqps",
}
#: The one scheme whose target is also a web ORIGIN, and therefore the only
#: one `canonical_origin` may ever resolve for.
_ORIGIN_SCHEME = "https"


def _normalize_target(raw: str, field: str = "canonical_target") -> str:
    """Accept one authenticable target; return scheme://host[:port] lowercased.

    Rejects anything that is not a bare target. A path, query, fragment or
    userinfo would make prefix-style comparison in a downstream runner
    ambiguous, and ambiguity in that check is the whole attack — so the rule
    is the same for `ssh://` as it always was for `https://`.

    Cleartext schemes are refused by name. Typing a password over cleartext
    is never correct, and an attacker who can force a downgrade should not
    also inherit a match.
    """
    s = (raw or "").strip().rstrip("/")
    if "://" not in s:
        raise CredentialError(
            f"field '{field}' must be a full target, e.g. "
            "https://example.com or ssh://build01.lan:22")
    scheme, _, rest = s.partition("://")
    scheme = scheme.lower()
    if scheme in _CLEARTEXT_SCHEMES:
        raise CredentialError(
            f"field '{field}' must not use cleartext '{scheme}' — "
            f"use '{_CLEARTEXT_SCHEMES[scheme]}'")
    if scheme not in TARGET_SCHEMES:
        raise CredentialError(
            f"field '{field}' has an unsupported scheme '{scheme}' — "
            f"expected one of: {', '.join(TARGET_SCHEMES)}")
    if any(ch in rest for ch in "/?#"):
        raise CredentialError(
            f"field '{field}' must be scheme://host[:port] only — "
            "no path, query or fragment")
    if "@" in rest:
        raise CredentialError(f"field '{field}' must not contain userinfo")
    host, _, port = rest.partition(":")
    host = host.lower()
    if not host or not re.match(r"^[a-z0-9.-]+$", host) or ".." in host:
        raise CredentialError(f"field '{field}' has an invalid host")
    if port:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise CredentialError(f"field '{field}' has an invalid port")
        return f"{scheme}://{host}:{int(port)}"
    return f"{scheme}://{host}"


def target_scheme(target: str) -> str:
    """The scheme of a stored target, or '' — never raises."""
    return str(target or "").partition("://")[0].lower()


_PEM_RE = re.compile(r"^-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def parse_structured_value(value) -> dict:
    """Parse a submitted structured payload (dict or JSON text) into a flat
    {field: str} dict. Never echoes submitted content in errors."""
    data = value
    if isinstance(data, str):
        try:
            data = json.loads(data or "{}")
        except Exception:
            raise CredentialError(
                "value must be a JSON object of fields for structured types")
    if not isinstance(data, dict):
        raise CredentialError(
            "value must be a JSON object of fields for structured types")
    out: dict = {}
    for key, val in data.items():
        if val is None:
            out[str(key)] = None  # explicit deletion marker for merge
            continue
        if not isinstance(val, str):
            raise CredentialError(f"field {key!r} must be a string")
        out[str(key)] = val.strip()
    return out


def _validate_structured(ctype: str, fields: dict) -> dict:
    """Validate + normalize a COMPLETE field dict for ctype. Returns the
    canonical dict. Error text carries field names only."""
    required, optional = schema_fields(ctype)
    known = set(required) | set(optional)
    unknown = sorted(set(fields) - known)
    if unknown:
        raise CredentialError(
            f"unknown field(s) {unknown} for type {ctype!r} — "
            f"valid: {', '.join(list(required) + list(optional))}")
    out = {}
    for key, val in fields.items():
        if not val:
            continue  # empty/None optional fields are simply absent
        cap = _field_max(key)
        if len(val) > cap:
            raise CredentialError(
                f"field {key!r} exceeds {cap} characters")
        out[key] = val
    if ctype == "login":
        # Accept the pre-2.118.0 spelling on input and fold it in, so stored
        # entries and partial updates keep working without a migration.
        # Never both: two different targets on one record is ambiguous, and
        # ambiguity is what the field exists to remove.
        legacy = out.pop("canonical_origin", "")
        if legacy and out.get("canonical_target") \
                and _normalize_target(legacy) != _normalize_target(
                    out["canonical_target"]):
            raise CredentialError(
                "fields 'canonical_target' and 'canonical_origin' disagree — "
                "set only 'canonical_target'")
        if legacy and not out.get("canonical_target"):
            out["canonical_target"] = legacy
    missing = [f for f in required if not out.get(f)]
    if missing:
        raise CredentialError(
            f"missing required field(s) {missing} for type {ctype!r}")
    if ctype == "card":
        number = re.sub(r"[ -]", "", out["number"])
        if not number.isdigit() or not 12 <= len(number) <= 19:
            raise CredentialError("field 'number' must be 12-19 digits")
        if not _luhn_ok(number):
            raise CredentialError("field 'number' failed checksum")
        out["number"] = number
        out["expiry"] = _normalize_expiry(out["expiry"])
        cvc = out.get("cvc", "")
        if cvc and (not cvc.isdigit() or not 3 <= len(cvc) <= 4):
            raise CredentialError("field 'cvc' must be 3-4 digits")
    if ctype == "login":
        if not _SITE_ID_RE.match(out["site_id"]):
            raise CredentialError(
                "field 'site_id' must be lowercase alphanumeric with "
                "'.', '_' or '-' (max 64 chars)")
        out["canonical_target"] = _normalize_target(out["canonical_target"])
        # A login with neither is not a credential. `password` stopped being
        # a required FIELD when key-based server logins arrived, but the
        # record must still carry a secret.
        if not out.get("password") and not out.get("private_key"):
            raise CredentialError(
                "a login needs a secret: set 'password', 'private_key', or "
                "both")
        key_blob = out.get("private_key", "")
        if key_blob and not _PEM_RE.match(key_blob.strip()):
            # A pasted public key or a passphrase in the wrong box would
            # otherwise be stored as a private key and fail much later, in a
            # runner, as an authentication error nobody traces back here.
            raise CredentialError(
                "field 'private_key' must be a PEM/OpenSSH private key "
                "(begins '-----BEGIN … PRIVATE KEY-----')")
        if out.get("passphrase") and not key_blob:
            raise CredentialError(
                "field 'passphrase' has no 'private_key' to unlock")
        # `[\s\-]`, NOT `[ -]`: the latter is a character RANGE from space
        # (0x20) to hyphen (0x2D) and therefore swallows the digits 2-7 that
        # base32 is built from. A seed pasted in the usual spaced form would
        # have its digits silently deleted, still pass the base32 check, and
        # then generate wrong codes forever — indistinguishable from a wrong
        # password. Found by the broker's RFC 6238 vectors after this shipped.
        seed = re.sub(r"[\s\-]", "", out.get("totp_secret", "")).upper()
        if seed:
            # base32 alphabet only; a malformed seed silently producing wrong
            # codes looks like a password failure and sends you debugging the
            # wrong half of the login.
            if not re.match(r"^[A-Z2-7]+=*$", seed):
                raise CredentialError(
                    "field 'totp_secret' must be base32 (A-Z, 2-7)")
            out["totp_secret"] = seed
    return out


def _display_projection(ctype: str, fields: dict) -> dict:
    """Non-sensitive registry metadata, computed server-side ONLY.

    Deliberately thin: a card shows brand + last4 (what a receipt shows) and
    NOT expiry — PAN+expiry is two of the three card-not-present fields. An
    address shows city/state, never the street. Identity shows the name as a
    label, the same sensitivity class as a user-written description.
    """
    if ctype == "card":
        return {"brand": _card_brand(fields["number"]),
                "last4": fields["number"][-4:]}
    if ctype == "address":
        return {"city": fields.get("city", ""), "state": fields.get("state", "")}
    if ctype == "identity":
        return {"label": fields.get("full_name", "")}
    if ctype == "login":
        # site_id and target only. The username is deliberately withheld:
        # username + target is half the credential, and the registry is the
        # one part of this record that non-secret surfaces are allowed to
        # render. `has_totp` / `has_key` are booleans so the UI can show a
        # 2FA or key badge without the secret going near a projection.
        target = fields.get("canonical_target", "")
        return {"site_id": fields.get("site_id", ""),
                "scheme": target_scheme(target),
                "target": target,
                "has_totp": bool(fields.get("totp_secret")),
                "has_key": bool(fields.get("private_key"))}
    return {}


# ── Public API ────────────────────────────────────────────


def _store_value(name: str, value: str, *, scope: str, project_path: str,
                 realm_s: str) -> tuple:
    """Write the value to whichever backend its size calls for.

    Returns ``(storage, raw_bytes)``. Shared by :func:`set_credential` and
    :func:`set_value` so the threshold decision and the "clean up the backend
    we are no longer using" half exist once — a value that grew past the
    threshold must leave the keyring, and one that shrank must leave the
    sidecar, or a stale copy outlives the entry.
    """
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
    return storage, raw


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
    source: str = "",
) -> dict:
    """Store the value (keyring or encrypted sidecar) and register the entry.

    Returns the non-secret registry entry. ``env_var`` defaults to the entry
    name at injection time when empty. ``source`` records the ``.env`` an
    import read this value from, so a later re-sync can find it again.
    Callers enforcing the "agent cannot raise agent_readable on an existing
    entry" rule must check the current entry first — this layer stores what it
    is told.

    Structured types (STRUCTURED_TYPES) take a JSON object of fields as
    ``value``. They are inject-only: ``agent_readable``/``inject`` are
    refused, the plain/structured boundary of an existing entry is immutable
    (delete and re-create to cross it), and a partial field dict MERGES into
    the existing payload so one field can be updated without resubmitting
    the rest.
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

    is_structured = ctype in STRUCTURED_TYPES
    prev_entry = _read_entries(scope, project_path).get(name)
    prev_struct = structured_type(name, project_path=project_path,
                                  scope=scope) if prev_entry else ""
    if prev_entry is not None:
        if bool(prev_struct) != is_structured or (prev_struct and
                                                  prev_struct != ctype):
            raise CredentialError(
                f"cannot change {name!r} from "
                f"{prev_struct or prev_entry.get('type', 'plain')!r} to "
                f"{ctype!r} — delete the entry and re-create it")
    display: dict = {}
    field_names: list = []
    if is_structured:
        if agent_readable or inject:
            raise CredentialError(
                f"{ctype} entries are inject-only: agent_readable and "
                "inject must stay false")
        fields = parse_structured_value(value)
        if prev_entry is not None:
            existing_raw = _get_raw(name, project_path=project_path, scope=scope)
            try:
                existing = json.loads(existing_raw) if existing_raw else {}
            except Exception:
                existing = {}
            merged = dict(existing) if isinstance(existing, dict) else {}
            for key, val in fields.items():
                if val is None:
                    merged.pop(key, None)
                else:
                    merged[key] = val
            fields = merged
        else:
            fields = {k: v for k, v in fields.items() if v is not None}
        fields = _validate_structured(ctype, fields)
        display = _display_projection(ctype, fields)
        field_names = sorted(fields)
        value = json.dumps(fields, sort_keys=True, separators=(",", ":"))

    storage, raw = _store_value(name, value, scope=scope,
                                project_path=project_path, realm_s=realm_s)

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
        "source": {"path": source, "at": now} if source else "",
    }
    if is_structured:
        entry["display"] = display
        entry["fields"] = field_names
    section["entries"][name] = entry
    config["credentials"] = section
    _save_config(base, config)
    _write_flag_attestation(realm_s, name, bool(agent_readable))
    _write_struct_attestation(realm_s, name, ctype)
    return dict(entry)


def set_value(name: str, value: str, *, scope: str = "project",
              project_path: str = ".", source=None) -> dict:
    """Replace the VALUE of an existing entry, keeping everything else.

    The counterpart to :func:`update_metadata`, which changes everything
    except the value. Only ``storage``, ``value_len`` and ``updated`` move;
    ``description``, ``env_var``, ``inject``, ``agent_readable`` and
    ``created`` survive, and the keyring attestations are left alone rather
    than rewritten to a default.

    This exists because a re-import is not a re-creation. ``set_credential``
    writes a whole fresh entry, so routing a rotation through it reset every
    setting the user had made — including ``inject``, which silently stopped
    auto-injection into every ``c3_shell`` run.

    ``type`` is preserved (a user who chose ``token`` keeps ``token``) except
    that a value containing a newline promotes to ``multiline``: what the type
    describes is the value, so the stored type cannot outrank the new content.
    Structured kinds are refused — their boundary stays delete-then-recreate.
    """
    _validate_name(name)
    if not value:
        raise CredentialError("value is required")
    scope = _norm_scope(scope, project_path)
    base = _scope_dir(scope, project_path)
    if base is None:
        raise CredentialError("global scope unavailable: no home directory")
    entry = _read_entries(scope, project_path).get(name)
    if entry is None:
        raise CredentialError(f"{name!r} is not set in {scope} scope")
    ctype = str(entry.get("type") or "token")
    if ctype in STRUCTURED_TYPES or structured_type(
            name, project_path=project_path, scope=scope):
        raise CredentialError(
            f"{name!r} is a structured entry — delete it and re-create it to "
            "store a plain value")

    realm_s = realm(scope, project_path)
    storage, raw = _store_value(name, value, scope=scope,
                                project_path=project_path, realm_s=realm_s)
    if "\n" in value and ctype != "multiline":
        ctype = "multiline"

    config = _load_config(base)
    section = _creds_section(config)
    now = _utcnow()
    updated = dict(section["entries"].get(name) or entry)
    updated.pop("scope", None)  # injected at read time, never stored
    updated.update({"type": ctype, "storage": storage,
                    "value_len": len(raw), "updated": now})
    if source:
        # Re-synced from a file: re-stamp so "last synced" means something.
        updated["source"] = {"path": source, "at": now}
    section["entries"][name] = updated
    config["credentials"] = section
    _save_config(base, config)
    return dict(updated)


def clear_source(name: str, *, scope: str = "project",
                 project_path: str = ".") -> dict:
    """Forget which ``.env`` this entry came from.

    Deliberately not reachable through :func:`update_metadata` — ``source`` is
    a record of something that happened, so it is written by an import and
    cleared by an explicit act, never edited to say a file it never came from.
    """
    _validate_name(name)
    scope = _norm_scope(scope, project_path)
    base = _scope_dir(scope, project_path)
    if base is None:
        raise CredentialError("global scope unavailable: no home directory")
    config = _load_config(base)
    section = _creds_section(config)
    entry = section["entries"].get(name)
    if not isinstance(entry, dict):
        raise CredentialError(f"unknown credential {name!r} in {scope} scope")
    entry["source"] = ""
    entry["updated"] = _utcnow()
    config["credentials"] = section
    _save_config(base, config)
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
    cur_struct = structured_type(name, project_path=project_path, scope=scope)
    if cur_struct:
        if "type" in fields and fields["type"] != cur_struct:
            raise CredentialError(
                f"{name!r} is structured ({cur_struct}) — its type is "
                "immutable; delete the entry and re-create it")
        if fields.get("agent_readable") or fields.get("inject"):
            raise CredentialError(
                f"{cur_struct} entries are inject-only: agent_readable and "
                "inject must stay false")
    elif fields.get("type") in STRUCTURED_TYPES:
        raise CredentialError(
            "a plain entry cannot become structured via metadata — "
            "delete it and re-create with a field payload")
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
                 "updated", "display", "fields", "source")


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
    raw = _get_raw(name, project_path=project_path, scope=scope)
    if raw is None:
        return False
    if structured_type(name, project_path=project_path, scope=scope):
        try:
            return isinstance(json.loads(raw), dict)
        except Exception:
            return False
    return True


def _get_raw(name: str, *, project_path: str = ".", scope: str = "") -> Optional[str]:
    """Decoded stored string from the owning realm, WITHOUT the structured
    gate. Internal: every public value path must go through get_value."""
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


def get_value(name: str, *, project_path: str = ".", scope: str = "",
              field: Optional[str] = None) -> Optional[str]:
    """Decoded value from the owning realm, or None. Never falls through:
    a project-registered name resolves in the project realm or not at all.

    Structured entries resolve per-FIELD only: without ``field`` they return
    None, so a hostile registry flag (``inject: true`` written into a cloned
    config.json) lands the name in resolve()'s missing list instead of
    putting a whole card payload into a subprocess env. A ``field`` on a
    plain entry also returns None.
    """
    stype = structured_type(name, project_path=project_path, scope=scope)
    if stype:
        if not field:
            return None
        raw = _get_raw(name, project_path=project_path, scope=scope)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        val = _resolve_field(stype, data, field)
        return val if isinstance(val, str) and val else None
    if field:
        return None
    return _get_raw(name, project_path=project_path, scope=scope)


def _resolve_field(ctype: str, data: dict, field: str):
    """One field out of a decoded structured payload, aliases applied.

    The only alias is `login.canonical_origin`, and it is deliberately
    ASYMMETRIC: it resolves to the target ONLY when that target is an https
    origin. A browser broker that pins a credential by asking for
    `canonical_origin` therefore gets nothing for an `ssh://` or
    `postgres://` entry and fails closed, instead of being handed a string
    that is not a web origin and cannot be compared to a top-level frame.
    Old records that still store the field literally read back unchanged.
    """
    if field in data:
        return data.get(field)
    if ctype == "login" and field == "canonical_origin":
        target = data.get("canonical_target", "")
        if target_scheme(target) == _ORIGIN_SCHEME:
            return target
        return None
    if ctype == "login" and field == "canonical_target":
        return data.get("canonical_origin")  # pre-2.118.0 records
    return None


def get_structured_fields(name: str, *, project_path: str = ".",
                          scope: str = "") -> Optional[dict]:
    """Full decoded field dict of a structured entry — the human read-back
    path for ``c3 creds get --show``.

    NEVER import this from an HTTP surface (server/hub/mobile/oracle): the
    wire is write-only, and the mobile source-grep invariant test asserts
    this name is absent there. CLI + local tooling only.
    """
    if not structured_type(name, project_path=project_path, scope=scope):
        return None
    raw = _get_raw(name, project_path=project_path, scope=scope)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


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
    try:
        _keyring_module().delete_password(KEYRING_SERVICE, _struct_account(realm_s, name))
    except Exception:
        pass
    config = _load_config(base)
    section = _creds_section(config)
    removed_entry = name in section["entries"]
    section["entries"].pop(name, None)
    config["credentials"] = section
    _save_config(base, config)
    _ACTIVE_SECRETS.pop(name, None)
    for ref in [r for r in _ACTIVE_SECRETS if r.startswith(name + ".")]:
        _ACTIVE_SECRETS.pop(ref, None)
    return removed_value or removed_entry


def resolve(names: list, project_path: str = ".") -> tuple:
    """``({ref: value}, [missing])`` for injection. Registers each resolved
    value for output redaction.

    A ref is a plain name (``NPM_TOKEN``) or a dotted field of a structured
    entry (``CARD.number``). A bare structured name lands in missing —
    whole-payload resolution is refused at get_value.
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    for ref in names:
        name, _, field = ref.partition(".")
        val = get_value(name, project_path=project_path, field=field or None)
        if val is None:
            missing.append(ref)
        else:
            values[ref] = val
            register_active_secret(ref, val)
    return values, missing


def fingerprint(name: str, *, project_path: str = ".") -> str:
    """First 8 hex of sha256(value), computed live — deliberately NOT persisted
    in config.json (a committed fingerprint would be an offline guessing
    oracle for weak secrets). Empty string when unresolvable."""
    val = get_value(name, project_path=project_path)
    if val is None:
        return ""
    return _digest(val)


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


def _digest(value: str) -> str:
    """First 8 hex of sha256(value). Shared by :func:`fingerprint` and by the
    import preview, which has to identify a not-yet-stored value without
    echoing any part of it."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


# Escapes honoured inside a DOUBLE-quoted .env value. Single-quoted values are
# literal (POSIX), so nothing is unescaped there.
_ENV_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


def _unescape_double(raw: str) -> str:
    out: list = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw) and raw[i + 1] in _ENV_ESCAPES:
            out.append(_ENV_ESCAPES[raw[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _closing_quote(segment: str, quote: str):
    """Index of the unescaped closing ``quote`` in ``segment``, else None."""
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\" and quote == '"':
            i += 2
            continue
        if ch == quote:
            return i
        i += 1
    return None


def _strip_inline_comment(raw: str) -> str:
    """Drop a trailing ``# comment`` from an UNQUOTED value.

    Only a ``#`` that opens a whitespace-delimited token counts, so a value
    like ``pa#ss`` survives intact. Quoted values never reach here.
    """
    idx = 0
    while True:
        hit = raw.find("#", idx)
        if hit == -1:
            return raw
        if hit == 0 or raw[hit - 1] in " \t":
            return raw[:hit]
        idx = hit + 1


# Row-level notes (a line was unusable or superseded) as opposed to
# credential-level skips (a named value we declined to store).
_LINE_NOTES = frozenset({"no-assignment", "duplicate"})

_IMPORT_REASON_TEXT = {
    "no-assignment": "no KEY=VALUE on this line",
    "bad-name": "not a usable credential name",
    "empty": "no value",
    "unterminated-quote": "quote never closes",
    "duplicate": "redefined later in the file",
    "exists": "already exists (enable overwrite to replace)",
    "structured": "would flatten a structured entry (delete it first)",
    "deselected": "not selected",
}


def read_env_file(path) -> str:
    """Read a ``.env`` off disk as text, for every surface that imports one.

    Never ``read_text(encoding="utf-8")``: a `.env` written by a Windows editor
    is routinely cp1252/latin-1, and that call raises `UnicodeDecodeError` —
    which the CLI's ``except (CredentialError, RuntimeError)`` did not catch,
    so the command died on a traceback. Nor ``errors="replace"``: silently
    mangling a byte inside a secret is worse than refusing the file, and 2.92.2
    was exactly that bug elsewhere in the codebase.

    So: strict UTF-8 (BOM tolerated), then strict cp1252, then give up loudly.
    Raises :class:`CredentialError` with the path on any failure.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise CredentialError(f"cannot read {p}: {exc}") from exc
    # PowerShell's `>` wrote UTF-16LE for years, and cp1252 decodes those
    # bytes without complaint straight into mojibake, so check the BOM first.
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise CredentialError(f"{p}: malformed UTF-16") from exc
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CredentialError(
        f"{p} is neither UTF-8 nor cp1252 — re-save it as UTF-8 and retry")


def parse_env(text: str) -> list:
    """Parse ``.env`` text into ordered rows. Pure: no I/O, no store access.

    Each row is ``{"name", "value", "line", "ok", "reason"}``; ``reason`` is
    ``""`` for a usable row and otherwise one of ``no-assignment``,
    ``bad-name``, ``empty``, ``unterminated-quote``, ``duplicate``.

    Handles what a real ``.env`` actually contains: a UTF-8 BOM, CRLF, a
    leading ``export``, inline comments on unquoted values, single quotes as
    literal and double quotes with ``\\n``-style escapes, and — the reason this
    function exists — a value spanning several lines inside one pair of quotes.
    The previous line-at-a-time parser truncated those at the first newline and
    dropped the rest without reporting anything, so a PEM key imported as a
    single dangling-quote fragment and the caller was told it succeeded.

    Deliberately unsupported: ``${VAR}`` interpolation. A vault entry whose
    value silently depends on another entry is worse than an explicit one.
    """
    lines = (text or "").lstrip("﻿").splitlines()
    rows: list = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        start_line = i
        if "=" not in stripped:
            # Previously discarded in silence, which is precisely what hid a
            # truncated multi-line value. The line content is NOT echoed: a
            # stray line in a .env is far more likely to be key material than
            # anything a user needs to read back.
            rows.append({"name": f"line {start_line}", "value": "",
                         "line": start_line, "ok": False,
                         "reason": "no-assignment"})
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        reason = ""

        quote = val[:1] if val[:1] in ("\"", "'") else ""
        if quote:
            body = val[1:]
            closed = _closing_quote(body, quote)
            if closed is not None:
                val = body[:closed]
            else:
                # Consume following lines until the quote closes, keeping the
                # newlines verbatim — the PEM / JSON-blob case.
                parts = [body]
                while i < len(lines):
                    nxt = lines[i]
                    i += 1
                    shut = _closing_quote(nxt, quote)
                    if shut is not None:
                        parts.append(nxt[:shut])
                        break
                    parts.append(nxt)
                else:
                    reason = "unterminated-quote"
                val = "\n".join(parts)
            if quote == '"':
                val = _unescape_double(val)
        else:
            val = _strip_inline_comment(val).strip()

        if not reason and not _NAME_RE.match(key):
            reason = "bad-name"
        elif not reason and not val:
            reason = "empty"
        rows.append({"name": key[:128] or f"line {start_line}", "value": val,
                     "line": start_line, "ok": not reason, "reason": reason})

    # Last definition of a name wins, as in every shell and dotenv loader.
    by_name: dict = {}
    for row in rows:
        if row["ok"]:
            by_name.setdefault(row["name"], []).append(row)
    for dupes in by_name.values():
        for row in dupes[:-1]:
            row["ok"] = False
            row["reason"] = "duplicate"
    return rows


def text_digest(text: str) -> str:
    """Identify a body of ``.env`` text without keeping any of it.

    Lets a commit prove it is applying the same file the user previewed, so a
    file edited between the two calls is refused instead of silently importing
    new content under the old, ticked row list.
    """
    return _digest(text or "")


def source_paths(project_path: str = ".", scope: str = "") -> list:
    """Every ``.env`` a credential in this vault remembers being imported from.

    DERIVED from the entries, never stored beside them. A second registry of
    "known sources" would be a hand-maintained copy of a list the entries
    already imply, and a copy is what silently goes stale.
    """
    out: dict = {}
    entries = (_read_entries(scope, project_path) if scope
               else list_entries(project_path))
    for entry in entries.values():
        src = entry.get("source") or {}
        path = src.get("path") if isinstance(src, dict) else ""
        if not path:
            continue
        seen = out.setdefault(path, {"path": path, "count": 0, "at": ""})
        seen["count"] += 1
        seen["at"] = max(seen["at"], str(src.get("at") or ""))
    return [out[k] for k in sorted(out)]


def import_env(
    text: str, *, scope: str = "project", project_path: str = ".",
    overwrite: bool = False, preview: bool = False, only=None,
    source: str = "", compare: bool = False,
) -> dict:
    """Parse ``.env`` text (see :func:`parse_env`) into credentials.

    Without ``overwrite``, names already registered in the TARGET scope are
    skipped (shadowing the other scope is allowed). ``only`` restricts the
    import to a set of names. ``preview=True`` classifies every row and writes
    nothing, so a caller can show the user what would happen before any value
    reaches the keyring.

    A value containing a newline is stored as ``multiline`` rather than ``env``
    — that is what the type is for, and it makes a PEM legible in the UI.

    ``source`` records which ``.env`` an entry came from, so a later re-sync
    can find the file again without the user re-picking it.

    ``compare`` turns the preview into a DIFF: a row whose stored value
    already equals the file's is reported as ``action="current"`` instead of
    ``"replace"``, and ``vanished`` names entries this same file used to
    define and no longer does. The comparison is a digest of both values made
    server-side; neither value, nor the stored value's digest, goes anywhere.

    Returns ``{"created", "skipped", "reasons", "rows", "preview", "digest",
    "vanished"}``. ``created`` and ``skipped`` remain plain name lists for
    callers that predate the rest. ``rows`` carries a value's LENGTH and
    FINGERPRINT and never any part of the value itself.
    """
    scope = _norm_scope(scope, project_path)
    existing = _read_entries(scope, project_path)
    wanted = set(only) if only is not None else None
    created: list = []
    skipped: list = []
    reasons: dict = {}
    out_rows: list = []
    seen_names: set = set()

    for row in parse_env(text):
        name, val, reason = row["name"], row["value"], row["reason"]
        if not reason:
            if wanted is not None and name not in wanted:
                reason = "deselected"
            elif existing.get(name, {}).get("type") in STRUCTURED_TYPES:
                # Never let a .env import silently flatten a card into an env
                # var, even with overwrite — the boundary is delete-then-recreate.
                reason = "structured"
            elif name in existing and not overwrite:
                reason = "exists"

        ctype = "multiline" if "\n" in val else "env"
        if not row["reason"]:
            seen_names.add(name)
        action = "skip" if reason else (
            "replace" if name in existing else "create")
        if compare and action == "replace":
            stored = _get_raw(name, project_path=project_path, scope=scope)
            if stored is not None and _digest(stored) == _digest(val):
                action = "current"
        out_rows.append({
            "name": name, "line": row["line"], "reason": reason,
            "detail": _IMPORT_REASON_TEXT.get(reason, ""),
            "action": action,
            "ctype": ctype, "value_len": len(val),
            "fingerprint": _digest(val) if not reason else "",
        })
        if reason:
            if reason not in _LINE_NOTES:
                skipped.append(name)
                reasons[name] = reason
            continue
        if not preview:
            if name in existing:
                # A re-import rotates the value; it does not re-create the
                # entry. set_credential would blank description/env_var and
                # turn inject and agent_readable back off.
                set_value(name, val, scope=scope, project_path=project_path,
                          source=source)
            else:
                set_credential(name, val, scope=scope,
                               project_path=project_path, ctype=ctype,
                               source=source)
        created.append(name)

    vanished: list = []
    if compare and source:
        for name, entry in existing.items():
            if name in seen_names:
                continue
            src = entry.get("source") or {}
            if isinstance(src, dict) and src.get("path") == source:
                vanished.append(name)

    return {"created": created, "skipped": skipped, "reasons": reasons,
            "rows": out_rows, "preview": bool(preview),
            "digest": _digest(text or ""), "vanished": sorted(vanished)}


def expand_templates(cmd: str, project_path: str = ".") -> tuple:
    """Replace ``{{cred:NAME}}`` / ``{{cred:NAME.field}}`` with decoded
    values, server-side only.

    Returns ``(expanded_cmd, used_refs, missing_refs)``. Callers must log
    the RAW template form, never the expanded string, and must scrub captured
    output with :func:`redact_text`.
    """
    used: list[str] = []
    missing: list[str] = []

    def _sub(match: "re.Match[str]") -> str:
        name, field = match.group(1), match.group(2)
        ref = f"{name}.{field}" if field else name
        val = get_value(name, project_path=project_path, field=field)
        if val is None:
            missing.append(ref)
            return match.group(0)
        used.append(ref)
        register_active_secret(ref, val)
        return val

    return _TEMPLATE_RE.sub(_sub, cmd or ""), used, missing


def describe_missing(refs: list, project_path: str = ".") -> dict:
    """{ref: reason} for refs that failed to resolve — built from registry
    metadata only (field NAMES, never values), so error paths stay decode-free."""
    out: dict = {}
    for ref in refs:
        name, _, field = ref.partition(".")
        entry = get_entry(name, project_path=project_path)
        if not entry:
            out[ref] = "unknown credential"
            continue
        stype = structured_type(name, project_path=project_path)
        fields = ", ".join(entry.get("fields") or []) or "none recorded"
        if stype and not field:
            out[ref] = (f"structured ({stype}) — address a field, e.g. "
                        f"{{{{cred:{name}.{(entry.get('fields') or ['field'])[0]}}}}}; "
                        f"fields: {fields}")
        elif stype:
            out[ref] = f"no field {field!r} on {name}; fields: {fields}"
        elif field:
            out[ref] = f"{name} is not structured — drop the .{field} suffix"
        else:
            out[ref] = "registered but its value is missing from this realm's store"
    return out


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
