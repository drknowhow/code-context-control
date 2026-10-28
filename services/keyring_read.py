"""Read one keyring secret without importing ``keyring`` where that is exact.

A hook process pays ``keyring`` about 250 ms before its first answer: about
150 ms of imports, then about 100 ms reading every installed package's entry
points to choose a backend. On Windows, with no backend named in the
environment or in keyring's config file and no other package registering
one, that choice is ``WinVaultKeyring``, and its read is one or two
``CredReadW`` calls — about 9 ms through ctypes. ``get_password`` makes those
calls the way ``WinVaultKeyring.get_password`` does and returns what they
find, ``None`` included. Every other case goes to ``keyring`` itself:

- any system other than Windows;
- a process that already imported ``keyring`` (it may have chosen or been
  given another backend, and tests install fakes there);
- ``PYTHON_KEYRING_BACKEND`` naming any other backend, or a
  ``keyringrc.cfg`` in keyring's config root;
- another installed package registering a keyring backend (keyring then
  chains every viable backend by priority, so another one may answer
  first), and an install missing keyring itself or the ``win32cred`` that
  WinVaultKeyring reads through;
- any ctypes error.

The registered backends are the expensive part to find out, so that answer
is cached on disk; see ``_keyrings_own_backends_only``.

Callers keep their own failure handling: whatever ``keyring.get_password``
would raise, this raises too.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_WINVAULT = "keyring.backends.Windows.WinVaultKeyring"
_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168
_BACKENDS_CACHE_MAX = 8  # sys.path shapes kept: hooks, MCP server, hub, CLI, ...
_RACY_S = 2.0  # as in FileMemo: an mtime this recent may hide a later change

_api = None  # (CredReadW, CredFree, CREDENTIALW pointer type), loaded once


def get_password(service: str, username: str) -> str | None:
    """``keyring.get_password(service, username)``, without keyring when exact."""
    if _vault_is_keyrings_choice():
        try:
            return _vault_get(service, username)
        except Exception:
            pass  # keyring reads it again, and raises what it raises
    import keyring  # noqa: PLC0415 — the general path, and the fallback
    return keyring.get_password(service, username)


def _vault_is_keyrings_choice() -> bool:
    """True when keyring would read this process's secrets from WinVault."""
    if sys.platform != "win32" or "keyring" in sys.modules:
        return False
    if os.environ.get("PYTHON_KEYRING_BACKEND", _WINVAULT) != _WINVAULT:
        return False
    # keyring.util.platform_.config_root() on Windows, then core._config_path().
    root = os.environ.get("LOCALAPPDATA", os.environ.get("ProgramData", "."))
    if (Path(root) / "Python Keyring" / "keyringrc.cfg").exists():
        return False
    return _keyrings_own_backends_only()


def _keyrings_own_backends_only() -> bool:
    """True when keyring and a ``win32cred`` are installed and no other
    package registers a keyring backend.

    Reading every installed distribution's entry points costs about 100 ms,
    so the answer is cached per sys.path in ``~/.c3/cache/keyring_backends.json``
    against the mtime of each sys.path entry. importlib.metadata keys its own
    directory listings on those mtimes, and installing, upgrading or removing
    a package adds or removes a directory in one of them. The cache does not
    see an ``entry_points.txt`` rewritten inside an existing distribution,
    which only an in-place ``setup.py egg_info`` of a keyring plugin does.
    An answer is not stored while any entry changed in the last ``_RACY_S``:
    a second change inside the same mtime tick would not show.
    """
    started = time.time()
    try:
        entries = [entry or os.getcwd() for entry in sys.path]
    except OSError:  # the working directory was deleted
        return False
    mtimes = [_mtime_ns(entry) for entry in entries]
    key = "\n".join(entries)
    path = _backends_cache_file()
    data = _read_backends_cache(path)
    hit = data.get(key)
    if isinstance(hit, dict) and hit.get("mtimes") == mtimes \
            and isinstance(hit.get("own_only"), bool):
        return hit["own_only"]
    try:
        own_only = _scan_backends()
    except Exception:
        return False
    if max((m for m in mtimes if m is not None), default=0) < (started - _RACY_S) * 1e9:
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and isinstance(v.get("at"), (int, float))}
        data[key] = {"mtimes": mtimes, "own_only": own_only, "at": started}
        if len(data) > _BACKENDS_CACHE_MAX:
            keep = sorted(data, key=lambda k: data[k]["at"])[-_BACKENDS_CACHE_MAX:]
            data = {k: data[k] for k in keep}
        try:
            from services.atomic_json import write_json_atomic  # noqa: PLC0415
            write_json_atomic(path, data, fsync=False)
        except Exception:
            pass  # the next process scans again
    return own_only


def _scan_backends() -> bool:
    """keyring's own entry points are the only ``keyring.backends`` ones, its
    Windows backend among them, and win32ctypes or pywin32 can be imported."""
    import importlib.util  # noqa: PLC0415 — a cache miss only
    from importlib import metadata  # noqa: PLC0415

    values = set()
    for ep in metadata.entry_points(group="keyring.backends"):
        dist = getattr(ep, "dist", None)
        if (getattr(dist, "name", None) or "").lower() != "keyring":
            return False
        values.add(ep.value)
    if "keyring.backends.Windows" not in values:
        return False
    return any(importlib.util.find_spec(name) is not None
               for name in ("win32ctypes", "win32cred"))


def _backends_cache_file() -> Path:
    home = os.environ.get("C3_HOME") or os.path.expanduser("~")
    return Path(home) / ".c3" / "cache" / "keyring_backends.json"


def _read_backends_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mtime_ns(path: str):
    try:
        return os.stat(path).st_mtime_ns
    except (OSError, ValueError):
        return None


def _vault_get(service: str, username: str) -> str | None:
    """WinVaultKeyring.get_password: the target ``service`` first, then the
    compound ``username@service`` when that entry is missing or belongs to
    another user name (keyring moves older entries there on a collision)."""
    found = _cred_read(service)
    if found is None or (username and found[0] != username):
        found = _cred_read(f"{username}@{service}")
    if found is None:
        return None
    blob = found[1]
    try:
        return blob.decode("utf-16")  # keyring's DecodingCredential.value
    except UnicodeDecodeError:
        return blob.decode("utf-8")


def _cred_read(target: str):
    """``(UserName, CredentialBlob)`` of a generic credential, or None."""
    read, free, pcred = _load_api()
    import ctypes  # noqa: PLC0415 — loaded with the API above

    cred = pcred()
    if not read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise OSError(err, f"CredReadW failed for {target!r}")
    try:
        c = cred.contents
        blob = (ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
                if c.CredentialBlobSize else b"")
        return c.UserName, blob
    finally:
        free(cred)


def _load_api():
    global _api
    if _api is None:
        import ctypes  # noqa: PLC0415 — Windows only, first read only
        from ctypes import wintypes  # noqa: PLC0415

        class _CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        pcred = ctypes.POINTER(_CREDENTIALW)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        read = advapi32.CredReadW
        read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         ctypes.POINTER(pcred)]
        read.restype = wintypes.BOOL
        free = advapi32.CredFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        _api = (read, free, pcred)
    return _api
