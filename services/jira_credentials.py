"""OS keyring wrapper for Jira credentials (Cloud API tokens / DC PATs).

Secrets are stored in the OS-native secret store (Windows Credential Manager,
macOS Keychain, Linux Secret Service) via the ``keyring`` library. They are
NEVER written to ``.c3/config.json`` — only the non-secret registry of named
accounts and the default-account pointer live there.

Accounts are *named* (e.g. "work", "internal") and each entry carries its
credential-bound identity: ``base_url``, ``username`` (email on Cloud),
``deployment`` ("cloud" | "data_center"), plus per-account TLS settings.
Tokens are keyed in the keyring by ``(base_url, username)`` — a config file
that rewrites an account's ``base_url`` can therefore never retrieve the
token that was stored for the original server.

The ``keyring`` import is intentionally lazy so importing this module never
fails on systems that haven't installed the dep yet (e.g. during initial
``c3 init``); the failure surface only triggers when a credential operation
is actually requested.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

KEYRING_SERVICE = "c3-jira"

VALID_DEPLOYMENTS = ("cloud", "data_center")


def _keyring_module():
    """Import keyring lazily so import-time failures don't crash unrelated code."""
    try:
        import keyring  # type: ignore[import-untyped]
        return keyring
    except ImportError as exc:  # pragma: no cover — only triggered on broken installs
        raise RuntimeError(
            "The 'keyring' package is required for Jira credential storage. "
            "Run: pip install keyring"
        ) from exc


class JiraCredentialError(RuntimeError):
    """Raised when keyring or config I/O fails, or validation rejects input."""


def account_id(base_url: str, username: str) -> str:
    """Stable keyring account-id for a (base_url, username) pair."""
    return f"{base_url.rstrip('/')}|{username}"


def validate_base_url(base_url: str, *, allow_insecure: bool = False) -> str:
    """Normalize and validate a Jira base URL; returns it without a trailing slash.

    Rejects non-HTTPS URLs unless ``allow_insecure`` is set — storing a token
    against a plaintext endpoint invites credential interception.
    """
    base_norm = (base_url or "").strip().rstrip("/")
    if not base_norm:
        raise JiraCredentialError("base_url is required")
    if base_norm.startswith("https://"):
        return base_norm
    if base_norm.startswith("http://") and allow_insecure:
        return base_norm
    raise JiraCredentialError(
        "Jira base_url must use https:// (allow_insecure is intended only for "
        "local development servers)"
    )


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


def _jira_section(config: dict) -> dict:
    section = config.get("jira")
    if not isinstance(section, dict):
        section = {}
    section.setdefault("default_account", "")
    accounts = section.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    section["accounts"] = accounts
    return section


def _write_jira_section(project_path: str, section: dict) -> None:
    config = _load_config(project_path)
    config["jira"] = section
    _save_config(project_path, config)


# ── Public API ────────────────────────────────────────────


def save_credentials(
    name: str,
    base_url: str,
    username: str,
    token: str,
    *,
    deployment: str,
    project_path: str = ".",
    set_default: bool = True,
    default_project: str = "",
    verify_tls: bool = True,
    ca_bundle: str = "",
    allow_insecure: bool = False,
) -> None:
    """Store the secret in the OS keyring and register the named account."""
    if not name or not username or not token:
        raise JiraCredentialError("name, username, and token are required")
    if deployment not in VALID_DEPLOYMENTS:
        raise JiraCredentialError(
            f"deployment must be one of {VALID_DEPLOYMENTS}, got {deployment!r}"
        )
    base_norm = validate_base_url(base_url, allow_insecure=allow_insecure)
    keyring = _keyring_module()
    try:
        keyring.set_password(KEYRING_SERVICE, account_id(base_norm, username), token)
    except Exception as exc:
        raise JiraCredentialError(f"keyring write failed: {exc}") from exc

    config = _load_config(project_path)
    section = _jira_section(config)
    section["accounts"][name] = {
        "base_url": base_norm,
        "username": username,
        "deployment": deployment,
        "default_project": default_project,
        "verify_tls": bool(verify_tls),
        "ca_bundle": ca_bundle,
    }
    if set_default or not section["default_account"]:
        section["default_account"] = name
    config["jira"] = section
    _save_config(project_path, config)


def load_token(base_url: str, username: str) -> Optional[str]:
    """Return the secret for an account, or None if not stored."""
    keyring = _keyring_module()
    try:
        return keyring.get_password(KEYRING_SERVICE, account_id(base_url, username))
    except Exception:
        return None


def delete_credentials(name: str, *, project_path: str = ".") -> bool:
    """Remove the named account's secret and registry entry. Returns True
    if anything was removed."""
    config = _load_config(project_path)
    section = _jira_section(config)
    entry = section["accounts"].get(name)

    removed_keyring = False
    if isinstance(entry, dict):
        keyring = _keyring_module()
        try:
            keyring.delete_password(
                KEYRING_SERVICE,
                account_id(entry.get("base_url", ""), entry.get("username", "")),
            )
            removed_keyring = True
        except Exception:
            # keyring.errors.PasswordDeleteError when not present
            pass

    removed_config = name in section["accounts"]
    section["accounts"].pop(name, None)
    if section["default_account"] == name:
        remaining = sorted(section["accounts"])
        section["default_account"] = remaining[0] if remaining else ""
    config["jira"] = section
    _save_config(project_path, config)
    return removed_keyring or removed_config


def list_accounts(project_path: str = ".") -> dict:
    """Registered accounts as ``{name: {base_url, username, deployment, ...}}``.
    Tokens not included."""
    config = _load_config(project_path)
    section = _jira_section(config)
    return {n: dict(a) for n, a in section["accounts"].items() if isinstance(a, dict)}


def get_account(name: str = "", *, project_path: str = ".") -> dict:
    """Return the named (or default) account entry with its name included,
    or ``{}`` when unresolved. Reads the project config only — cross-file
    project→home resolution lives in ``core.config.load_jira_config``."""
    config = _load_config(project_path)
    section = _jira_section(config)
    target = name or section["default_account"]
    entry = section["accounts"].get(target)
    if not isinstance(entry, dict):
        return {}
    out = dict(entry)
    out["name"] = target
    return out


def set_default_account(name: str, *, project_path: str = ".") -> None:
    config = _load_config(project_path)
    section = _jira_section(config)
    if name not in section["accounts"]:
        raise JiraCredentialError(f"unknown Jira account {name!r} — run `c3 jira login` first")
    section["default_account"] = name
    config["jira"] = section
    _save_config(project_path, config)


def set_default_project(project_key: str, *, name: str = "", project_path: str = ".") -> None:
    """Pin a default Jira project key on the named (or default) account."""
    config = _load_config(project_path)
    section = _jira_section(config)
    target = name or section["default_account"]
    entry = section["accounts"].get(target)
    if not isinstance(entry, dict):
        raise JiraCredentialError(
            "no Jira account to set a default project on — run `c3 jira login` first"
        )
    entry["default_project"] = project_key
    config["jira"] = section
    _save_config(project_path, config)


def get_active_token(project_path: str = ".", *, name: str = "") -> Optional[str]:
    """Convenience: return the secret for the default (or named) account, or None."""
    entry = get_account(name, project_path=project_path)
    if not entry.get("base_url") or not entry.get("username"):
        return None
    return load_token(entry["base_url"], entry["username"])
