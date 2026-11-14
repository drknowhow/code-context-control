"""Second copy of every credential value, outside the OS keychain.

The OS keychain is the vault's only copy of a value, and Windows Credential
Manager can lose every entry at once. ``~/.c3/vault_backup.json`` keeps a
copy that does not depend on the keychain:

* each value is sealed to an X25519 public key (ephemeral ECDH, HKDF-SHA256,
  AES-GCM with ``realm|name`` as associated data). Sealing needs no secret,
  so every value write updates the backup without a prompt;
* the matching private key is stored wrapped, up to twice:

  - ``device_key`` (Windows): protected with DPAPI, for the signed-in user
    or, when Windows cannot use the user's key, for the computer (see
    :func:`_device_protect`). It opens with no prompt, so a value the
    keychain lost is put back the next time something reads it
    (:func:`heal`). On Windows this wrap is created automatically the first
    time a value is stored (:func:`ensure_default`), so the backup is on by
    default there.
  - ``wrapped_key``: under a key derived from a passphrase the user chose
    (scrypt). It depends on neither the keychain nor DPAPI, and is the only
    way back when the Windows profile itself is lost.

Only :func:`restore` and :func:`heal` decrypt, and they write each value
straight back into the keychain; no function here returns a value.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
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
_DEVICE_ENTROPY = (FORMAT + "|device-key").encode("ascii")
# Set to off/0/false/no to keep C3 from creating a backup on its own.
AUTO_ENV = "C3_VAULT_BACKUP"
# heal() reads the vault, and vault reads call heal(): one level only.
_HEALING = threading.local()


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


def _dpapi(raw: bytes, *, protect: bool, machine: bool = False) -> Optional[bytes]:
    """CryptProtectData / CryptUnprotectData. ``machine`` protects with the
    computer's key instead of the signed-in user's; unprotecting detects the
    scope from the blob. None when this is not Windows or Windows refuses
    (no prompt is shown)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        def blob(data: bytes):
            buf = ctypes.create_string_buffer(data, len(data))
            return _Blob(len(data), ctypes.cast(
                buf, ctypes.POINTER(ctypes.c_char))), buf

        source, _source_buf = blob(raw)
        entropy, _entropy_buf = blob(_DEVICE_ENTROPY)
        out = _Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if protect and machine:
            flags |= 0x4  # CRYPTPROTECT_LOCAL_MACHINE
        if not call(ctypes.byref(source), None, ctypes.byref(entropy), None,
                    None, flags, ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
    except Exception:
        return None


def device_available() -> bool:
    """Whether this platform can hold a device key (Windows DPAPI)."""
    return sys.platform == "win32"


def _device_protect(raw: bytes) -> Optional[tuple]:
    """``(scheme, blob)``. The user's own DPAPI key when Windows can use it.
    A profile whose DPAPI master key is unreadable is also one whose
    Credential Manager cannot save to disk, so that case falls back to the
    computer's key: weaker (any administrator, or anyone holding the
    unencrypted disk, can open it) but it is the case the backup exists for."""
    blob = _dpapi(raw, protect=True)
    if blob:
        return "dpapi", blob
    blob = _dpapi(raw, protect=True, machine=True)
    return ("dpapi-machine", blob) if blob else None


def _device_unprotect(blob: bytes) -> Optional[bytes]:
    return _dpapi(blob, protect=False)


def auto_enabled() -> bool:
    return os.environ.get(AUTO_ENV, "").strip().lower() not in (
        "off", "0", "false", "no")


def _private_raw(c: dict, private) -> bytes:
    ser = c["serialization"]
    return private.private_bytes(
        ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())


def _device_wrap(private_raw: bytes) -> Optional[dict]:
    if not device_available():
        return None
    wrapped = _device_protect(private_raw)
    if not wrapped:
        return None
    return {"scheme": wrapped[0], "blob": _b64(wrapped[1]), "at": _now()}


def _device_private(c: dict, data: dict):
    """The private key from the device wrap, or None when there is no wrap,
    Windows no longer opens it, or it is not this backup's key."""
    wrap = data.get("device_key")
    if not isinstance(wrap, dict) or not device_available():
        return None
    try:
        raw = _device_unprotect(_unb64(str(wrap["blob"])))
        if not raw or len(raw) != 32:
            return None
        private = c["X25519PrivateKey"].from_private_bytes(raw)
        if _b64(_raw_public(c, private.public_key())) != data.get("public_key"):
            return None
        return private
    except (KeyError, ValueError):
        return None


def has_passphrase() -> bool:
    """Whether the backup can be opened with a passphrase. False when off."""
    try:
        return "wrapped_key" in _load()
    except BackupError:
        return False


def device_opens() -> bool:
    """Whether the backup opens on this device right now, with no prompt."""
    try:
        data = _load()
        return bool(data) and _device_private(_crypto(), data) is not None
    except BackupError:
        return False


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
    wrapped = data.get("wrapped_key")
    if not isinstance(wrapped, dict) or "kdf" not in data:
        raise BackupError(
            "this vault backup has no passphrase; it opens only on the Windows "
            "account that created it")
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


def _create(passphrase: Optional[str]) -> None:
    if is_enabled():
        raise BackupError(f"a backup already exists at {backup_path()}")
    c = _crypto()
    private = c["X25519PrivateKey"].generate()
    private_raw = _private_raw(c, private)
    data = {"format": FORMAT, "created": _now(),
            "public_key": _b64(_raw_public(c, private.public_key()))}
    device = _device_wrap(private_raw)
    if device:
        data["device_key"] = device
    if passphrase is not None:
        data["kdf"], data["wrapped_key"] = _wrap(c, private_raw, passphrase)
    elif not device:
        raise BackupError(
            "no device key is available on this system; run `c3 creds backup "
            "init` to protect the backup with a passphrase")
    data["records"] = {}
    _save(data)


def init(passphrase: str) -> None:
    """Create the backup file, locked with a passphrase (and, on Windows, a
    device key as well). Refuses to replace one that exists."""
    _check_passphrase(passphrase)
    _create(passphrase)


def init_device() -> None:
    """Create a backup that opens only on this Windows account, with no
    passphrase. Refuses to replace one that exists."""
    _create(None)


def ensure_default(project_paths=()) -> bool:
    """Turn the backup on where that needs no question: Windows, no backup
    file yet, not opted out with ``C3_VAULT_BACKUP=off``. Copies every value
    that currently resolves. True when this call created the backup."""
    if is_enabled() or not device_available() or not auto_enabled():
        return False
    try:
        init_device()
        sync(project_paths)
    except (BackupError, OSError):
        return False
    return True


def _refresh_device_key(c: dict, private) -> None:
    """Give the backup a device wrap that opens here. Best effort."""
    try:
        data = _load()
        if not data or _device_private(c, data) is not None:
            return
        device = _device_wrap(_private_raw(c, private))
        if device:
            data["device_key"] = device
            _save(data)
    except (BackupError, OSError):
        pass


def _unlock(c: dict, data: dict, passphrase: Optional[str]):
    """The private key, from the passphrase when one is given, else from the
    device key."""
    if passphrase is not None:
        return _unwrap(c, data, passphrase)
    private = _device_private(c, data)
    if private is not None:
        return private
    if "wrapped_key" in data:
        raise BackupError(
            "the vault backup does not open on this device; its passphrase "
            "is needed")
    raise BackupError(
        "the vault backup no longer opens on this device and has no "
        "passphrase, so its values cannot be recovered. Delete "
        f"{backup_path()} and re-enter the values")


def change_passphrase(old: Optional[str], new: str) -> None:
    """Set or replace the passphrase; the sealed records stay as they are.

    ``old`` is the current passphrase. A backup that has none yet is opened
    with its device key instead, and ``old`` is ignored."""
    _check_passphrase(new)
    data = _load()
    if not data:
        raise BackupError("no vault backup to change")
    c = _crypto()
    private = _unlock(c, data, old if "wrapped_key" in data else None)
    data["kdf"], data["wrapped_key"] = _wrap(c, _private_raw(c, private), new)
    _save(data)
    _refresh_device_key(c, private)


def seal(realm_s: str, name: str, value: str) -> str:
    """Add or replace one value's sealed copy.

    Returns ``"saved"``, ``"off"`` (no backup configured) or
    ``"failed: <reason>"``. Never raises: the keychain write it follows has
    already succeeded, and a set must not fail because its copy did."""
    try:
        data = _load()
        if not data:
            scope, project_path = _realm_target(realm_s)
            if not ensure_default((project_path,) if scope == "project" else ()):
                return "off"
            data = _load()
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


def _restore_with(c: dict, data: dict, private, *, only=None,
                  realm_only: str = "") -> tuple:
    """``(report, [(realm, name) restored])``. ``_HEALING`` is held so the
    vault reads made here do not start a heal of their own."""
    wanted = set(only) if only else None
    out: dict = {"restored": [], "present": [], "orphaned": [], "failed": []}
    done: list = []
    was_healing = getattr(_HEALING, "on", False)
    _HEALING.on = True
    try:
        for realm_s, records in sorted(data.get("records", {}).items()):
            if realm_only and realm_s != realm_only:
                continue
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
                    cs.restore_value(name, value, scope=scope,
                                     project_path=project_path)
                    out["restored"].append(label)
                    done.append((realm_s, name))
                except (c["InvalidTag"], cs.CredentialError, KeyError,
                        ValueError) as exc:
                    out["failed"].append(f"{label}: {type(exc).__name__}")
    finally:
        _HEALING.on = was_healing
    return out, done


def restore(passphrase: Optional[str] = None, *, only=None) -> dict:
    """Write every backed-up value whose entry is registered but has lost its
    value back into the keychain. A value that still resolves is never
    overwritten, and an entry deleted since is not recreated.

    With no passphrase the backup is opened with its device key, and a
    :class:`BackupError` says so when that does not work here.

    Returns name lists: ``restored``, ``present`` (value still there),
    ``orphaned`` (entry or project gone), ``failed``. Restored entries come
    back injection-only; see ``credential_store.restore_value``."""
    data = _load()
    if not data:
        raise BackupError("no vault backup found — nothing to restore from")
    c = _crypto()
    private = _unlock(c, data, passphrase)
    out, _done = _restore_with(c, data, private, only=only)
    if passphrase is not None:
        _refresh_device_key(c, private)
    return out


def heal(realm_s: str, names) -> list:
    """Put back the values of ``names`` in one realm that the keychain lost,
    using the device key. Returns the names restored. No prompt, never
    raises, and does nothing when the backup cannot be opened here.

    Called by the vault's own read paths, so a wipe repairs itself the first
    time a value is listed or used."""
    if getattr(_HEALING, "on", False) or not names:
        return []
    try:
        if not is_enabled():
            return []
        data = _load()
        held = data.get("records", {}).get(realm_s, {})
        wanted = [n for n in names if n in held]
        if not wanted or "device_key" not in data:
            return []
        c = _crypto()
        private = _device_private(c, data)
        if private is None:
            return []
        out, done = _restore_with(c, data, private, only=wanted,
                                  realm_only=realm_s)
    except (BackupError, OSError, KeyError, ValueError):
        return []
    if out["restored"]:
        _log_heal(out["restored"])
    return [name for _realm, name in done]


def _log_heal(labels: list) -> None:
    """Names only, to the global activity log that `c3 creds audit` reads."""
    try:
        from services.activity_log import ActivityLog
        home = cs.global_base()
        if home is not None:
            ActivityLog(str(home)).log("cred_action", {
                "kind": "creds", "action": "restore", "names": labels,
                "via": "auto"})
    except (ImportError, OSError, ValueError):
        pass


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
    if not is_enabled() and not ensure_default(project_paths):
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
                 "passphrase": "wrapped_key" in data,
                 "device": bool(data) and _device_private(_crypto(), data) is not None,
                 "device_scheme": str((data.get("device_key") or {}).get("scheme", "")),
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
