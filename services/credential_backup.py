"""Passphrase-locked second copy of every credential value.

The OS keychain is the vault's only copy of a value, and Windows Credential
Manager can lose every entry at once (it did twice on one box in September
2026). ``~/.c3/vault_backup.json`` keeps a copy that depends on neither the
keychain nor DPAPI:

* each value is sealed to an X25519 public key (ephemeral ECDH, HKDF-SHA256,
  AES-GCM with ``realm|name`` as associated data). Sealing needs no secret,
  so every value write updates the backup without a prompt;
* the matching private key is stored wrapped under a key derived from a
  passphrase the user chose (scrypt). Reading anything back needs it.

Only :func:`restore` decrypts, and it writes each value straight back into
the keychain; no function here returns a value.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services import credential_store as cs

FORMAT = "c3-vault-backup/1"
BACKUP_NAME = "vault_backup.json"
SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE = 16
_HKDF_INFO = FORMAT.encode("ascii")
_WRAP_AAD = (FORMAT + "|private-key").encode("ascii")


class BackupError(RuntimeError):
    """Backup missing, locked with a different passphrase, or unreadable."""


def _crypto():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise BackupError(
            "the 'cryptography' package is required for the vault backup. "
            "Run: pip install cryptography") from exc
    return {
        "InvalidTag": InvalidTag, "hashes": hashes, "serialization": serialization,
        "X25519PrivateKey": X25519PrivateKey, "X25519PublicKey": X25519PublicKey,
        "AESGCM": AESGCM, "HKDF": HKDF, "Scrypt": Scrypt,
    }


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backup_path() -> Optional[Path]:
    home = cs.global_base()
    return None if home is None else home / ".c3" / BACKUP_NAME


def _load() -> dict:
    path = backup_path()
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError(f"{path} is not a {FORMAT} file")
    return data


def _save(data: dict) -> None:
    from services.atomic_json import write_json_atomic
    path = backup_path()
    if path is None:
        raise BackupError("no home directory for the backup file")
    write_json_atomic(path, data)


def is_enabled() -> bool:
    path = backup_path()
    return path is not None and path.exists()


def _kek(c: dict, passphrase: str, kdf: dict) -> bytes:
    return c["Scrypt"](salt=_unb64(kdf["salt"]), length=32, n=int(kdf["n"]),
                       r=int(kdf["r"]), p=int(kdf["p"])).derive(
                           passphrase.encode("utf-8"))


def _wrap(c: dict, private_raw: bytes, passphrase: str) -> tuple:
    kdf = {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
           "salt": _b64(os.urandom(16))}
    nonce = os.urandom(12)
    ct = c["AESGCM"](_kek(c, passphrase, kdf)).encrypt(nonce, private_raw, _WRAP_AAD)
    return kdf, {"nonce": _b64(nonce), "ct": _b64(ct)}


def _unwrap(c: dict, data: dict, passphrase: str):
    wrapped = data["wrapped_key"]
    try:
        raw = c["AESGCM"](_kek(c, passphrase, data["kdf"])).decrypt(
            _unb64(wrapped["nonce"]), _unb64(wrapped["ct"]), _WRAP_AAD)
    except c["InvalidTag"] as exc:
        raise BackupError("wrong passphrase for the vault backup") from exc
    return c["X25519PrivateKey"].from_private_bytes(raw)


def _check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(
            f"the backup passphrase must be at least {MIN_PASSPHRASE} characters")


def _raw_public(c: dict, key) -> bytes:
    ser = c["serialization"]
    return key.public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)


def _record_key(c: dict, shared: bytes, epk: bytes, pub: bytes) -> bytes:
    return c["HKDF"](algorithm=c["hashes"].SHA256(), length=32, salt=epk + pub,
                     info=_HKDF_INFO).derive(shared)


def init(passphrase: str) -> None:
    """Create the backup file. Refuses to replace one that exists."""
    _check_passphrase(passphrase)
    if is_enabled():
        raise BackupError(f"a backup already exists at {backup_path()}")
    c = _crypto()
    ser = c["serialization"]
    private = c["X25519PrivateKey"].generate()
    private_raw = private.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    kdf, wrapped = _wrap(c, private_raw, passphrase)
    _save({"format": FORMAT, "created": _now(),
           "public_key": _b64(_raw_public(c, private.public_key())),
           "kdf": kdf, "wrapped_key": wrapped, "records": {}})


def change_passphrase(old: str, new: str) -> None:
    """Re-wrap the private key; the sealed records stay as they are."""
    _check_passphrase(new)
    data = _load()
    if not data:
        raise BackupError("no vault backup to change")
    c = _crypto()
    ser = c["serialization"]
    private = _unwrap(c, data, old)
    data["kdf"], data["wrapped_key"] = _wrap(c, private.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()), new)
    _save(data)


def seal(realm_s: str, name: str, value: str) -> str:
    """Add or replace one value's sealed copy.

    Returns ``"saved"``, ``"off"`` (no backup configured) or
    ``"failed: <reason>"``. Never raises: the keychain write it follows has
    already succeeded, and a set must not fail because its copy did."""
    try:
        data = _load()
        if not data:
            return "off"
        c = _crypto()
        pub_raw = _unb64(data["public_key"])
        ephemeral = c["X25519PrivateKey"].generate()
        epk = _raw_public(c, ephemeral.public_key())
        key = _record_key(c, ephemeral.exchange(
            c["X25519PublicKey"].from_public_bytes(pub_raw)), epk, pub_raw)
        nonce = os.urandom(12)
        ct = c["AESGCM"](key).encrypt(nonce, value.encode("utf-8"),
                                      f"{realm_s}|{name}".encode("utf-8"))
        data.setdefault("records", {}).setdefault(realm_s, {})[name] = {
            "epk": _b64(epk), "nonce": _b64(nonce), "ct": _b64(ct), "at": _now()}
        _save(data)
        return "saved"
    except (BackupError, OSError, KeyError, ValueError) as exc:
        return f"failed: {exc}"


def drop(realm_s: str, name: str) -> None:
    """Forget one sealed value (the credential was deleted). Never raises."""
    try:
        data = _load()
        realm_records = data.get("records", {}).get(realm_s)
        if realm_records and realm_records.pop(name, None) is not None:
            if not realm_records:
                data["records"].pop(realm_s)
            _save(data)
    except (BackupError, OSError):
        pass


def _open(c: dict, private, realm_s: str, name: str, rec: dict) -> str:
    epk = _unb64(rec["epk"])
    pub_raw = _raw_public(c, private.public_key())
    key = _record_key(c, private.exchange(
        c["X25519PublicKey"].from_public_bytes(epk)), epk, pub_raw)
    return c["AESGCM"](key).decrypt(
        _unb64(rec["nonce"]), _unb64(rec["ct"]),
        f"{realm_s}|{name}".encode("utf-8")).decode("utf-8")


def _realm_target(realm_s: str) -> tuple:
    """``(scope, project_path)`` for a realm string, or ``("", "")``."""
    if realm_s == "global":
        home = cs.global_base()
        return ("global", str(home)) if home is not None else ("", "")
    if realm_s.startswith("proj|"):
        return "project", realm_s[len("proj|"):]
    return "", ""


def restore(passphrase: str, *, only=None) -> dict:
    """Write every backed-up value whose entry is registered but has lost its
    value back into the keychain. A value that still resolves is never
    overwritten, and an entry deleted since is not recreated.

    Returns name lists: ``restored``, ``present`` (value still there),
    ``orphaned`` (entry or project gone), ``failed``. Restored entries come
    back injection-only; see ``credential_store.restore_value``."""
    data = _load()
    if not data:
        raise BackupError("no vault backup found — nothing to restore from")
    c = _crypto()
    private = _unwrap(c, data, passphrase)
    wanted = set(only) if only else None
    out: dict = {"restored": [], "present": [], "orphaned": [], "failed": []}
    for realm_s, records in sorted(data.get("records", {}).items()):
        scope, project_path = _realm_target(realm_s)
        for name, rec in sorted(records.items()):
            if wanted is not None and name not in wanted:
                continue
            label = name if scope == "global" else f"{name} ({project_path})"
            if not scope or not Path(project_path).is_dir() \
                    or name not in cs._read_entries(scope, project_path):
                out["orphaned"].append(label)
                continue
            if cs.is_resolvable(name, project_path=project_path, scope=scope):
                out["present"].append(label)
                continue
            try:
                value = _open(c, private, realm_s, name, rec)
                cs.restore_value(name, value, scope=scope, project_path=project_path)
                out["restored"].append(label)
            except (c["InvalidTag"], cs.CredentialError, KeyError, ValueError) as exc:
                out["failed"].append(f"{label}: {type(exc).__name__}")
    return out


def _realm_entries(project_paths) -> list:
    """``(scope, project_path, realm, name)`` for every registered entry in the
    global vault and the given projects."""
    home = cs.global_base()
    targets = [("global", str(home))] if home is not None else []
    seen = set()
    for pp in project_paths or ():
        if not Path(pp, ".c3").is_dir() or cs._project_is_home(pp):
            continue
        realm_s = cs.realm("project", pp)
        if realm_s not in seen:
            seen.add(realm_s)
            targets.append(("project", pp))
    rows = []
    for scope, pp in targets:
        realm_s = cs.realm(scope, pp)
        for name in cs._read_entries(scope, pp):
            rows.append((scope, pp, realm_s, name))
    return rows


def sync(project_paths=()) -> dict:
    """Seal every value that still resolves in the global vault and the given
    projects. Returns ``{"saved": n, "missing": [names without a value],
    "failed": [...]}``."""
    if not is_enabled():
        raise BackupError("no vault backup configured — run `c3 creds backup init`")
    out: dict = {"saved": 0, "missing": [], "failed": []}
    for scope, pp, realm_s, name in _realm_entries(project_paths):
        raw = cs._get_raw(name, project_path=pp, scope=scope)
        label = name if scope == "global" else f"{name} ({pp})"
        if raw is None:
            out["missing"].append(label)
            continue
        result = seal(realm_s, name, raw)
        if result == "saved":
            out["saved"] += 1
        else:
            out["failed"].append(f"{label}: {result}")
    return out


def status(project_paths=(), presence=None) -> dict:
    """Counts only — never a value. ``not_backed_up`` lists registered entries
    with a live value but no sealed copy; ``restorable`` lists entries whose
    value is gone but whose copy is here.

    ``presence`` maps realm to a ``credential_store.value_presence`` result
    the caller already holds; any other vault is probed here, once."""
    data = _load()
    records = data.get("records", {}) if data else {}
    out: dict = {"enabled": bool(data), "path": str(backup_path() or ""),
                 "created": data.get("created", "") if data else "",
                 "backed_up": sum(len(r) for r in records.values()),
                 "restorable": [], "lost": [], "not_backed_up": []}
    known = dict(presence or {})
    for scope, pp, realm_s, name in _realm_entries(project_paths):
        label = name if scope == "global" else f"{name} ({pp})"
        has_copy = name in records.get(realm_s, {})
        if realm_s not in known:
            known[realm_s] = cs.value_presence(scope, pp)
        if known[realm_s].get(name, False):
            if not has_copy:
                out["not_backed_up"].append(label)
        elif has_copy:
            out["restorable"].append(label)
        else:
            out["lost"].append(label)
    return out
