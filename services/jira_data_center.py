"""Jira Data Center / Server backend — REST v2, PAT Bearer auth, text bodies.

Pagination is classic offset (``startAt``/``maxResults``/``total``); the
opaque cursor exposed by the facade is the stringified next offset. Rich-text
bodies are plain strings (wiki markup passes through untouched).
"""
from __future__ import annotations

from typing import Any

from services.jira_client import (
    JiraError,
    JiraTransport,
    normalize_comment,
    normalize_issue,
    normalize_transition,
)

_API = "/rest/api/2"

_DEFAULT_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,project,"
    "created,updated,labels"
)

# createmeta paging. DC caps maxResults server-side; _MAX_PAGES is a runaway
# guard, not an expected ceiling.
_PAGE_SIZE = 100
_MAX_PAGES = 20


def _text_of(value: Any) -> str:
    """DC bodies are already strings; tolerate anything else defensively."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _field_entry(field_id: str, f: dict) -> dict:
    return {
        "id": field_id,
        "name": f.get("name", ""),
        "type": (f.get("schema") or {}).get("type", ""),
    }


def _split_fields_list(raw_fields: list) -> tuple[list, list]:
    """DC 9.0+ shape: a list of field objects carrying their own ``fieldId``."""
    required, optional = [], []
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        entry = _field_entry(f.get("fieldId", f.get("key", "")), f)
        (required if f.get("required") else optional).append(entry)
    return required, optional


def _split_fields_map(raw_fields: dict) -> tuple[list, list]:
    """Legacy shape: a dict keyed by field id."""
    required, optional = [], []
    for field_id, f in raw_fields.items():
        entry = _field_entry(field_id, f)
        (required if f.get("required") else optional).append(entry)
    return required, optional


class DataCenterBackend:
    """Jira Data Center / Server strategy behind the ``JiraClient`` facade."""

    def __init__(
        self,
        base_url: str,
        username: str,
        token: str,
        *,
        timeout: int = 30,
        verify_tls: bool = True,
        ca_bundle: str = "",
    ):
        self._username = username
        self._t = JiraTransport(
            base_url, f"Bearer {token}",
            timeout=timeout, verify_tls=verify_tls, ca_bundle=ca_bundle,
        )

    # Health / identity
    def server_info(self) -> dict:
        return self._t.request("GET", f"{_API}/serverInfo")

    def myself(self) -> dict:
        return self._t.request("GET", f"{_API}/myself")

    # Read
    def search(self, jql: str, *, fields: str = "", limit: int = 25, cursor: str = "") -> dict:
        try:
            start_at = max(0, int(cursor)) if cursor else 0
        except ValueError:
            start_at = 0
        page = self._t.request(
            "GET", f"{_API}/search",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": limit,
                "fields": fields or _DEFAULT_FIELDS,
            },
        )
        issues = [normalize_issue(i, _text_of) for i in page.get("issues", [])]
        total = int(page.get("total", 0) or 0)
        next_offset = start_at + len(issues)
        next_cursor = str(next_offset) if issues and next_offset < total else ""
        return {"issues": issues, "next_cursor": next_cursor}

    def get_issue(self, key: str, *, include_comments: bool = True) -> dict:
        fields = _DEFAULT_FIELDS + ",description"
        if include_comments:
            fields += ",comment"
        raw = self._t.request("GET", f"{_API}/issue/{key}", params={"fields": fields})
        return normalize_issue(raw, _text_of)

    def list_projects(self, *, query: str = "", limit: int = 50) -> list[dict]:
        projects = self._t.request("GET", f"{_API}/project")
        rows = [
            {"key": p.get("key", ""), "name": p.get("name", ""), "id": p.get("id", "")}
            for p in (projects if isinstance(projects, list) else [])
        ]
        if query:
            needle = query.lower()
            rows = [
                r for r in rows
                if needle in r["key"].lower() or needle in r["name"].lower()
            ]
        return rows[:limit]

    def list_transitions(self, key: str) -> list[dict]:
        raw = self._t.request("GET", f"{_API}/issue/{key}/transitions")
        return [normalize_transition(t) for t in raw.get("transitions", [])]

    def get_create_metadata(self, project: str, issue_type: str) -> dict:
        """Required/optional create fields for one project + issue type.

        Jira DC 9.0 split the monolithic ``GET /issue/createmeta`` into a
        paginated pair and 11.x removed the original outright — it now 404s
        with "Issue Does Not Exist" for *any* project, which reads as a bad
        key rather than a dead endpoint. Try the split pair first and fall
        back to the legacy shape only for pre-9.0 servers.
        """
        try:
            return self._create_metadata_split(project, issue_type)
        except JiraError as exc:
            if exc.status != 404:
                raise
        try:
            return self._create_metadata_legacy(project, issue_type)
        except JiraError as exc:
            if exc.status == 404:
                raise JiraError(
                    f"createmeta unavailable for {project}/{issue_type}: the "
                    f"per-issue-type endpoints 404'd (project or issue type "
                    f"may not exist) and the legacy endpoint is removed on "
                    f"Jira 9.0+.",
                    status=404, path=f"{_API}/issue/createmeta",
                ) from exc
            raise

    def _create_metadata_split(self, project: str, issue_type: str) -> dict:
        """DC 9.0+ path: resolve the issue-type id, then page its fields."""
        types_page = self._t.request(
            "GET", f"{_API}/issue/createmeta/{project}/issuetypes",
            params={"maxResults": _PAGE_SIZE},
        )
        issue_types = types_page.get("values", types_page.get("issueTypes", []))
        match = next(
            (t for t in issue_types
             if (t.get("name") or "").lower() == issue_type.lower()),
            None,
        )
        if match is None:
            known = ", ".join(t.get("name", "?") for t in issue_types)
            return {"project": project, "issue_type": issue_type,
                    "error": f"unknown issue type; project has: {known}"}
        raw_fields = self._page_all(
            f"{_API}/issue/createmeta/{project}/issuetypes/{match.get('id')}"
        )
        required, optional = _split_fields_list(raw_fields)
        return {
            "project": project,
            "issue_type": issue_type,
            "required_fields": required,
            "optional_fields": [f["name"] for f in optional],
        }

    def _create_metadata_legacy(self, project: str, issue_type: str) -> dict:
        """Pre-9.0 path: one monolithic call, fields keyed by field id."""
        meta = self._t.request(
            "GET", f"{_API}/issue/createmeta",
            params={
                "projectKeys": project,
                "issuetypeNames": issue_type,
                "expand": "projects.issuetypes.fields",
            },
        )
        projects = meta.get("projects", [])
        issue_types = projects[0].get("issuetypes", []) if projects else []
        if not issue_types:
            return {"project": project, "issue_type": issue_type,
                    "error": "unknown project or issue type"}
        required, optional = _split_fields_map(
            issue_types[0].get("fields", {}) or {}
        )
        return {
            "project": project,
            "issue_type": issue_type,
            "required_fields": required,
            "optional_fields": [f["name"] for f in optional],
        }

    def _page_all(self, path: str) -> list[dict]:
        """Drain a DC paginated collection. Truncating required fields would
        silently under-report, so this pages rather than trusting one call."""
        out: list[dict] = []
        start_at = 0
        for _ in range(_MAX_PAGES):
            page = self._t.request(
                "GET", path,
                params={"startAt": start_at, "maxResults": _PAGE_SIZE},
            )
            values = page.get("values", page.get("fields", []))
            if isinstance(values, dict):
                # Some DC builds still key fields by id even on this route.
                out.extend(dict(f, fieldId=fid) for fid, f in values.items()
                           if isinstance(f, dict))
                break
            if not isinstance(values, list) or not values:
                break
            out.extend(values)
            total = page.get("total")
            if page.get("isLast"):
                break
            if isinstance(total, int) and len(out) >= total:
                break
            start_at += len(values)
        return out

    def search_users(self, query: str, *, limit: int = 10) -> list[dict]:
        users = self._t.request(
            "GET", f"{_API}/user/search",
            params={"username": query, "maxResults": limit},
        )
        return [
            {
                "id": u.get("name", ""),
                "display_name": u.get("displayName", ""),
                "email": u.get("emailAddress", ""),
                "active": bool(u.get("active", True)),
            }
            for u in (users if isinstance(users, list) else [])
        ]

    # Mutate
    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        *,
        description: str = "",
        fields: dict | None = None,
    ) -> dict:
        payload_fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
        }
        if description:
            payload_fields["description"] = description
        if fields:
            payload_fields.update(fields)
        return self._t.request(
            "POST", f"{_API}/issue", body={"fields": payload_fields}, is_mutation=True
        )

    def add_comment(self, key: str, body: Any, *, body_format: str = "text") -> dict:
        raw = self._t.request(
            "POST", f"{_API}/issue/{key}/comment",
            body={"body": str(body)}, is_mutation=True,
        )
        return normalize_comment(raw, _text_of)

    def transition_issue(
        self, key: str, transition_id: str, *, comment: str = "", fields: dict | None = None
    ) -> dict:
        body: dict[str, Any] = {"transition": {"id": str(transition_id)}}
        if comment:
            body["update"] = {"comment": [{"add": {"body": comment}}]}
        if fields:
            body["fields"] = fields
        return self._t.request(
            "POST", f"{_API}/issue/{key}/transitions", body=body, is_mutation=True
        )

    def assign_issue(self, key: str, user_id: str) -> dict:
        return self._t.request(
            "PUT", f"{_API}/issue/{key}/assignee",
            body={"name": user_id}, is_mutation=True,
        )
