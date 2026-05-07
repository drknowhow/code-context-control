"""OS keyring wrapper for Bitbucket Personal Access Tokens.

Tokens are stored in the OS-native secret store (Windows Credential Manager,
macOS Keychain, Linux Secret Service) via the ``keyring`` library. They are
NEVER written to ``.c3/config.json`` — only the non-secret index of known
``(base_url, username)`` accounts and the active-account pointer live there.

The ``keyring`` import is intentionally lazy so importing this module never
fails on systems that haven't installed the dep yet (e.g. during initial
``c3 init``); the failure surface only triggers when a credential operation
is actually requested.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

KEYRING_SERVICE = "c3-bitbucket"


def _keyring_module():
    """Import keyring lazily so import-time failures don't crash unrelated code."""
    try:
        import keyring  # type: ignore[import-untyped]
        return keyring
    except ImportError as exc:  # pragma: no cover — only triggered on broken installs
        raise RuntimeError(
            "The 'keyring' package is required for Bitbucket credential storage. "
            "Run: pip install keyring"
        ) from exc


class BitbucketCredentialError(RuntimeError):
    """Raised when keyring or config I/O fails."""


def account_id(base_url: str, username: str) -> str:
    """Stable keyring account-id for a (base_url, username) pair."""
    return f"{base_url.rstrip('/')}|{username}"


def _config_path(project_path: str) -> Path:
    return Path(project_path) / ".c3" / "config.json"


def _load_config(project_path: str) -> dict:
    path = _config_path(project_path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(project_path: str, config: dict) -> None:
    path = _config_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _bitbucket_section(config: dict) -> dict:
    section = config.get("bitbucket")
    if not isinstance(section, dict):
        section = {}
    section.setdefault("active", {"base_url": "", "username": ""})
    section.setdefault("accounts", [])
    section.setdefault("default_project", "")
    section.setdefault("default_repo", "")
    section.setdefault("verify_tls", True)
    return section


def _write_bitbucket_section(project_path: str, section: dict) -> None:
    config = _load_config(project_path)
    config["bitbucket"] = section
    _save_config(project_path, config)


# ── Public API ────────────────────────────────────────────


def save_credentials(
    base_url: str,
    username: str,
    token: str,
    *,
    project_path: str = ".",
    set_active: bool = True,
) -> None:
    """Store the PAT in the OS keyring and add the account to the config index."""
    if not base_url or not username or not token:
        raise BitbucketCredentialError("base_url, username, and token are required")
    keyring = _keyring_module()
    try:
        keyring.set_password(KEYRING_SERVICE, account_id(base_url, username), token)
    except Exception as exc:
        raise BitbucketCredentialError(f"keyring write failed: {exc}") from exc

    config = _load_config(project_path)
    section = _bitbucket_section(config)
    base_norm = base_url.rstrip("/")
    account = {"base_url": base_norm, "username": username}
    if account not in section["accounts"]:
        section["accounts"].append(account)
    if set_active or not section["active"].get("base_url"):
        section["active"] = account
    config["bitbucket"] = section
    _save_config(project_path, config)


def load_token(base_url: str, username: str) -> Optional[str]:
    """Return the PAT for an account, or None if not stored."""
    keyring = _keyring_module()
    try:
        return keyring.get_password(KEYRING_SERVICE, account_id(base_url, username))
    except Exception:
        return None


def delete_credentials(
    base_url: str,
    username: str,
    *,
    project_path: str = ".",
) -> bool:
    """Remove credentials from keyring and from the config index. Returns True
    if anything was removed."""
    keyring = _keyring_module()
    removed_keyring = False
    try:
        keyring.delete_password(KEYRING_SERVICE, account_id(base_url, username))
        removed_keyring = True
    except Exception:
        # keyring.errors.PasswordDeleteError when not present
        pass

    config = _load_config(project_path)
    section = _bitbucket_section(config)
    base_norm = base_url.rstrip("/")
    before = len(section["accounts"])
    section["accounts"] = [
        a for a in section["accounts"]
        if not (a.get("base_url") == base_norm and a.get("username") == username)
    ]
    removed_config = len(section["accounts"]) != before
    if (
        section["active"].get("base_url") == base_norm
        and section["active"].get("username") == username
    ):
        section["active"] = (
            section["accounts"][0] if section["accounts"] else {"base_url": "", "username": ""}
        )
    config["bitbucket"] = section
    _save_config(project_path, config)
    return removed_keyring or removed_config


def list_accounts(project_path: str = ".") -> list[dict]:
    """Known accounts as `[{base_url, username}, ...]`. Tokens not included."""
    config = _load_config(project_path)
    section = _bitbucket_section(config)
    return list(section["accounts"])


def get_active_account(project_path: str = ".") -> dict:
    config = _load_config(project_path)
    section = _bitbucket_section(config)
    return dict(section["active"])


def set_active_account(
    base_url: str, username: str, *, project_path: str = "."
) -> None:
    config = _load_config(project_path)
    section = _bitbucket_section(config)
    base_norm = base_url.rstrip("/")
    section["active"] = {"base_url": base_norm, "username": username}
    if not any(
        a.get("base_url") == base_norm and a.get("username") == username
        for a in section["accounts"]
    ):
        section["accounts"].append({"base_url": base_norm, "username": username})
    config["bitbucket"] = section
    _save_config(project_path, config)


def set_default_repo(
    project_key: str, repo_slug: str, *, project_path: str = "."
) -> None:
    config = _load_config(project_path)
    section = _bitbucket_section(config)
    section["default_project"] = project_key
    section["default_repo"] = repo_slug
    config["bitbucket"] = section
    _save_config(project_path, config)


def set_verify_tls(verify: bool, *, project_path: str = ".") -> None:
    config = _load_config(project_path)
    section = _bitbucket_section(config)
    section["verify_tls"] = bool(verify)
    config["bitbucket"] = section
    _save_config(project_path, config)


def get_active_token(project_path: str = ".") -> Optional[str]:
    """Convenience: return the PAT for the currently-active account, or None."""
    active = get_active_account(project_path)
    if not active.get("base_url") or not active.get("username"):
        return None
    return load_token(active["base_url"], active["username"])
