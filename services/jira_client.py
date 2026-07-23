"""Jira REST client — facade over Cloud and Data Center backends.

``JiraClient`` picks the backend from the account's ``deployment`` field
("cloud" | "data_center"); all divergence (API version, auth scheme, rich-text
format, pagination) lives inside ``services/jira_cloud.py`` and
``services/jira_data_center.py``. Callers see one normalized surface:
plain-text bodies, DTO dicts, and an opaque pagination cursor.

Transport policy: stdlib urllib (no new dependencies), one bounded retry on
HTTP 429 honoring ``Retry-After`` for NON-mutating requests only — mutations
are never auto-retried because the first attempt may have succeeded.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from services.jira_credentials import VALID_DEPLOYMENTS

_TIMEOUT = 30  # seconds
_RETRY_AFTER_CAP = 30  # never sleep longer than this on a 429

try:
    from importlib.metadata import version as _pkg_version

    _C3_VERSION = _pkg_version("code-context-control")
except Exception:  # pragma: no cover - package metadata may be unavailable
    _C3_VERSION = "0.0.0"

_USER_AGENT = f"c3-jira/{_C3_VERSION}"


class JiraError(RuntimeError):
    """Raised on non-2xx responses or transport failures."""

    def __init__(self, message: str, *, status: int = 0, path: str = "", body: Any = None):
        super().__init__(message)
        self.status = status
        self.path = path
        self.body = body


def _error_message(payload: Any) -> str:
    """Best-effort human message from a Jira error body."""
    if isinstance(payload, dict):
        messages = payload.get("errorMessages")
        if isinstance(messages, list) and messages:
            return "; ".join(str(m) for m in messages)
        errors = payload.get("errors")
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {v}" for k, v in errors.items())
    return ""


class JiraTransport:
    """Shared HTTP layer. Auth header is injected by the backend factory."""

    def __init__(
        self,
        base_url: str,
        auth_header: str,
        *,
        timeout: int = _TIMEOUT,
        verify_tls: bool = True,
        ca_bundle: str = "",
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not auth_header:
            raise ValueError("auth_header is required")
        self.base_url = base_url.rstrip("/")
        self._auth_header = auth_header
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self._ca_bundle:
            return ssl.create_default_context(cafile=self._ca_bundle)
        if self._verify_tls:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict | None = None,
        is_mutation: bool | None = None,
    ) -> Any:
        """One HTTP round-trip. ``is_mutation`` defaults to ``method != GET``;
        non-mutating requests get one bounded retry on 429."""
        if is_mutation is None:
            is_mutation = method.upper() != "GET"
        full_path = path
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""}
            )
            if qs:
                full_path = f"{full_path}?{qs}"
        url = f"{self.base_url}{full_path}"

        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempts = 0
        while True:
            attempts += 1
            req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(
                    req, timeout=self._timeout, context=self._ssl_context()
                ) as resp:
                    payload = resp.read()
                    if not payload:
                        return {}
                    try:
                        return json.loads(payload.decode("utf-8"))
                    except json.JSONDecodeError:
                        return {"raw": payload.decode("utf-8", errors="replace")}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and not is_mutation and attempts == 1:
                    retry_after = 1.0
                    try:
                        retry_after = float(exc.headers.get("Retry-After", "1"))
                    except Exception:
                        pass
                    time.sleep(min(retry_after, _RETRY_AFTER_CAP))
                    continue
                try:
                    err_payload = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    err_payload = None
                msg = _error_message(err_payload) or str(exc)
                raise JiraError(
                    f"HTTP {exc.code} on {method.upper()} {full_path}: {msg}",
                    status=exc.code,
                    path=full_path,
                    body=err_payload,
                ) from exc
            except urllib.error.URLError as exc:
                raise JiraError(
                    f"Transport failure on {method.upper()} {full_path}: {exc.reason}",
                    status=0,
                    path=full_path,
                ) from exc


# ── Shared normalization ──────────────────────────────────


def normalize_issue(raw: dict, text_of: Callable[[Any], str]) -> dict:
    """Flatten a Jira issue JSON into the C3 DTO. ``text_of`` converts the
    backend's rich-text format (ADF dict on Cloud, string on DC) to text."""
    f = raw.get("fields") or {}
    status = f.get("status") or {}
    category = status.get("statusCategory") or {}
    assignee = f.get("assignee") or {}
    dto = {
        "key": raw.get("key", ""),
        "id": raw.get("id", ""),
        "summary": f.get("summary", ""),
        "status": status.get("name", ""),
        "status_category": category.get("key", ""),
        "issue_type": (f.get("issuetype") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "assignee": assignee.get("displayName", ""),
        "assignee_id": assignee.get("accountId") or assignee.get("name") or "",
        "reporter": (f.get("reporter") or {}).get("displayName", ""),
        "project": (f.get("project") or {}).get("key", ""),
        "created": f.get("created", ""),
        "updated": f.get("updated", ""),
        "labels": f.get("labels") or [],
    }
    if "description" in f:
        dto["description"] = text_of(f.get("description"))
    comment_block = f.get("comment")
    if isinstance(comment_block, dict):
        dto["comments"] = [
            normalize_comment(c, text_of) for c in comment_block.get("comments", [])
        ]
    return dto


def normalize_comment(raw: dict, text_of: Callable[[Any], str]) -> dict:
    return {
        "id": raw.get("id", ""),
        "author": (raw.get("author") or {}).get("displayName", ""),
        "created": raw.get("created", ""),
        "body": text_of(raw.get("body")),
    }


def normalize_transition(raw: dict) -> dict:
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "to_status": (raw.get("to") or {}).get("name", ""),
    }


# ── Facade ────────────────────────────────────────────────


class JiraClient:
    """Deployment-agnostic Jira client. All methods return normalized DTOs;
    ``search`` uses an opaque string cursor for pagination on both backends."""

    def __init__(
        self,
        base_url: str,
        username: str,
        token: str,
        *,
        deployment: str,
        timeout: int = _TIMEOUT,
        verify_tls: bool = True,
        ca_bundle: str = "",
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not username:
            raise ValueError("username is required")
        if not token:
            raise ValueError("token is required")
        if deployment not in VALID_DEPLOYMENTS:
            raise ValueError(f"deployment must be one of {VALID_DEPLOYMENTS}")
        self.base_url = base_url.rstrip("/")
        self.deployment = deployment

        # Imported here to keep module import cheap and avoid cycles.
        from services.jira_cloud import CloudBackend
        from services.jira_data_center import DataCenterBackend

        if deployment == "cloud":
            self._backend: Any = CloudBackend(
                self.base_url, username, token,
                timeout=timeout, verify_tls=verify_tls, ca_bundle=ca_bundle,
            )
        else:
            self._backend = DataCenterBackend(
                self.base_url, username, token,
                timeout=timeout, verify_tls=verify_tls, ca_bundle=ca_bundle,
            )

    # Health / identity
    def server_info(self) -> dict:
        return self._backend.server_info()

    def myself(self) -> dict:
        return self._backend.myself()

    # Issues — read
    def search(self, jql: str, *, fields: str = "", limit: int = 25, cursor: str = "") -> dict:
        """Run JQL. Returns ``{"issues": [dto...], "next_cursor": str}`` —
        pass ``next_cursor`` back in to fetch the next page ("" = done)."""
        return self._backend.search(jql, fields=fields, limit=limit, cursor=cursor)

    def get_issue(self, key: str, *, include_comments: bool = True) -> dict:
        return self._backend.get_issue(key, include_comments=include_comments)

    def list_projects(self, *, query: str = "", limit: int = 50) -> list[dict]:
        return self._backend.list_projects(query=query, limit=limit)

    def list_transitions(self, key: str) -> list[dict]:
        return self._backend.list_transitions(key)

    def get_create_metadata(self, project: str, issue_type: str) -> dict:
        return self._backend.get_create_metadata(project, issue_type)

    def search_users(self, query: str, *, limit: int = 10) -> list[dict]:
        return self._backend.search_users(query, limit=limit)

    # Issues — mutate (never auto-retried at the transport layer)
    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        *,
        description: str = "",
        fields: dict | None = None,
    ) -> dict:
        return self._backend.create_issue(
            project, issue_type, summary, description=description, fields=fields
        )

    def add_comment(self, key: str, body: Any, *, body_format: str = "text") -> dict:
        return self._backend.add_comment(key, body, body_format=body_format)

    def transition_issue(
        self, key: str, transition_id: str, *, comment: str = "", fields: dict | None = None
    ) -> dict:
        return self._backend.transition_issue(
            key, transition_id, comment=comment, fields=fields
        )

    def assign_issue(self, key: str, user_id: str) -> dict:
        """Assign by the deployment-native id: ``accountId`` on Cloud,
        username on Data Center."""
        return self._backend.assign_issue(key, user_id)
