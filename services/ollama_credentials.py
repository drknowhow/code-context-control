"""OS keyring wrapper for Ollama Cloud API keys.

Keys live in the OS-native secret store (Windows Credential Manager, macOS
Keychain, Linux Secret Service) via the ``keyring`` library. They are NEVER
written to ``.c3/config.json`` — that file is not gitignored by default, so
a plaintext key there could end up committed.

Resolution order used by MemoryDistiller._cloud_bridge:
  1. explicit ``memory_llm.api_key`` config value (hand-edited escape hatch)
  2. the ``api_key_env`` environment variable (default OLLAMA_API_KEY)
  3. this keyring entry (what the UIs and ``c3 init`` write)

The ``keyring`` import is lazy, mirroring services/bitbucket_credentials.py:
importing this module never fails on systems without the dep; only actual
credential operations do.
"""

from __future__ import annotations

import os
from typing import Optional

KEYRING_SERVICE = "c3-ollama"
DEFAULT_ACCOUNT = "https://ollama.com"


def _keyring_module():
    """Import keyring lazily so import-time failures don't crash unrelated code."""
    try:
        import keyring  # type: ignore[import-untyped]
        return keyring
    except ImportError as exc:  # pragma: no cover — only on broken installs
        raise RuntimeError(
            "The 'keyring' package is required for Ollama API key storage. "
            "Run: pip install keyring"
        ) from exc


def _account(base_url: str = "") -> str:
    return (base_url or DEFAULT_ACCOUNT).rstrip("/")


def save_api_key(key: str, base_url: str = "") -> None:
    """Store the API key in the OS keyring, keyed by endpoint base URL."""
    if not key or not key.strip():
        raise ValueError("api key is required")
    _keyring_module().set_password(KEYRING_SERVICE, _account(base_url), key.strip())


def load_api_key(base_url: str = "") -> Optional[str]:
    """Return the stored key for an endpoint, or None."""
    try:
        return _keyring_module().get_password(KEYRING_SERVICE, _account(base_url))
    except Exception:
        return None


def delete_api_key(base_url: str = "") -> bool:
    """Remove the stored key. Returns True if one existed."""
    try:
        keyring = _keyring_module()
        account = _account(base_url)
        if keyring.get_password(KEYRING_SERVICE, account) is None:
            return False
        keyring.delete_password(KEYRING_SERVICE, account)
        return True
    except Exception:
        return False


def api_key_available(base_url: str = "", api_key_env: str = "OLLAMA_API_KEY",
                      config_key: str = "") -> bool:
    """True if a usable key exists anywhere in the resolution chain."""
    return bool(
        (config_key or "").strip()
        or os.environ.get(api_key_env or "OLLAMA_API_KEY", "")
        or load_api_key(base_url)
    )
