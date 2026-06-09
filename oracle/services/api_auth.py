"""API-key authentication for the Oracle Discovery API.

Stores a single Bearer token in the OS keyring (Windows Credential Manager /
macOS Keychain / Linux Secret Service), mirroring ``services/bitbucket_credentials.py``.

An optional ``C3_ORACLE_API_KEY`` environment variable overrides the keyring — useful
for headless/CI/containers where no keyring backend exists. Env-provided keys are
never written back to the keyring.
"""

from __future__ import annotations

import os
import secrets

KEYRING_SERVICE = "c3-oracle-api"
KEYRING_ACCOUNT = "discovery-api"
ENV_OVERRIDE = "C3_ORACLE_API_KEY"


class ApiAuthError(RuntimeError):
    """Raised when the keyring backend is unavailable or a write fails."""


def _keyring_module():
    """Import keyring lazily so import-time failures don't crash unrelated code."""
    try:
        import keyring  # type: ignore[import-untyped]

        return keyring
    except ImportError as exc:  # pragma: no cover — only on broken installs
        raise ApiAuthError(
            "The 'keyring' package is required for Oracle API key storage. "
            "Run: pip install keyring"
        ) from exc


def _env_key() -> str | None:
    val = os.environ.get(ENV_OVERRIDE, "").strip()
    return val or None


def _generate() -> str:
    return secrets.token_urlsafe(32)


def _store(key: str) -> None:
    # Keys supplied via the environment are authoritative and not persisted.
    if _env_key():
        return
    try:
        _keyring_module().set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key)
    except ApiAuthError:
        raise
    except Exception as exc:  # pragma: no cover — backend-specific failures
        raise ApiAuthError(f"keyring write failed: {exc}") from exc


def peek() -> str | None:
    """Return the stored API key, or ``None`` if none is set. Env override wins."""
    env = _env_key()
    if env:
        return env
    try:
        return _keyring_module().get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except ApiAuthError:
        raise
    except Exception:  # pragma: no cover — backend read failures are non-fatal
        return None


def get_or_create_key() -> str:
    """Return the existing key, generating and persisting a new one if absent."""
    existing = peek()
    if existing:
        return existing
    key = _generate()
    _store(key)
    return key


def rotate() -> str:
    """Generate, store, and return a fresh key, replacing any existing one."""
    key = _generate()
    _store(key)
    return key


def clear() -> bool:
    """Delete the stored key from the keyring. Returns True if one was removed."""
    try:
        _keyring_module().delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        return True
    except ApiAuthError:
        raise
    except Exception:  # no key stored, or backend lacks the entry
        return False


def verify(token: str | None) -> bool:
    """Constant-time comparison of a presented token against the stored key."""
    if not token:
        return False
    stored = peek()
    if not stored:
        return False
    return secrets.compare_digest(token, stored)


def extract_bearer(header_value: str | None) -> str | None:
    """Pull the token from an ``Authorization: Bearer <token>`` header value.

    Tolerates a bare token (no scheme) for clients that omit ``Bearer``.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return header_value.strip() or None
