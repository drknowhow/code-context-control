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
    encode_multipart_file,
    normalize_attachment,
    normalize_board,
    normalize_comment,
    normalize_issue,
    normalize_sprint,
    normalize_transition,
    normalize_worklog,
)

_API = "/rest/api/2"
_AGILE = "/rest/agile/1.0"

_DEFAULT_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,project,"
    "created,updated,labels"
)

# createmeta paging. DC caps maxResults server-side; _MAX_PAGES is a runaway
# guard, not an expected ceiling.
_PAGE_SIZE = 100
_MAX_PAGES = 20

# Epic Link custom-field id per base_url. DC has no first-class epic parent —
# the link lives in a per-instance custom field ("Epic Link") whose id only
# GET /field reveals. Discovered once per server per process; "" (no such
# field) is a valid cached answer.
_EPIC_FIELD_CACHE: dict[str, str] = {}


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
        self._base_url = base_url
        self._t = JiraTransport(
            base_url, f"Bearer {token}",
            timeout=timeout, verify_tls=verify_tls, ca_bundle=ca_bundle,
        )

    def _epic_link_field_id(self) -> str:
        cached = _EPIC_FIELD_CACHE.get(self._base_url)
        if cached is not None:
            return cached
        try:
            fields = self._t.request("GET", f"{_API}/field")
        except JiraError:
            return ""  # transient failure — retry on the next call
        field_id = next(
            (f.get("id", "") for f in (fields if isinstance(fields, list) else [])
             if (f.get("name") or "").strip().lower() == "epic link"),
            "",
        )
        _EPIC_FIELD_CACHE[self._base_url] = field_id
        return field_id

    def _parent_fields(self, parent: str) -> dict:
        """Field payload linking an issue under *parent*: the Epic Link
        custom field when the parent is an Epic, the ``parent`` field
        (subtasks) otherwise."""
        parent_type = ""
        try:
            parent_type = self.get_issue(
                parent, include_comments=False
            ).get("issue_type", "")
        except JiraError:
            pass  # bad key — the write's own error is authoritative
        if parent_type.lower() == "epic":
            field_id = self._epic_link_field_id()
            if field_id:
                return {field_id: parent}
        return {"parent": {"key": parent}}

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
        fields = _DEFAULT_FIELDS + ",description,parent,issuelinks,attachment"
        epic_field = self._epic_link_field_id()
        if epic_field:
            fields += f",{epic_field}"
        if include_comments:
            fields += ",comment"
        raw = self._t.request("GET", f"{_API}/issue/{key}", params={"fields": fields})
        dto = normalize_issue(raw, _text_of)
        if epic_field and not dto.get("parent"):
            epic_key = (raw.get("fields") or {}).get(epic_field)
            if isinstance(epic_key, str) and epic_key:
                dto["parent"] = epic_key
        return dto

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
            "optional_fields": optional,
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
            "optional_fields": optional,
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
        parent: str = "",
    ) -> dict:
        payload_fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
        }
        if description:
            payload_fields["description"] = description
        if parent:
            payload_fields.update(self._parent_fields(parent))
        if fields:
            payload_fields.update(fields)
        return self._t.request(
            "POST", f"{_API}/issue", body={"fields": payload_fields}, is_mutation=True
        )

    def update_issue(
        self,
        key: str,
        *,
        summary: str = "",
        description: str = "",
        fields: dict | None = None,
    ) -> dict:
        payload_fields: dict[str, Any] = {}
        if summary:
            payload_fields["summary"] = summary
        if description:
            payload_fields["description"] = description
        if fields:
            payload_fields.update(fields)
        return self._t.request(
            "PUT", f"{_API}/issue/{key}",
            body={"fields": payload_fields}, is_mutation=True,
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

    def list_link_types(self) -> list[dict]:
        raw = self._t.request("GET", f"{_API}/issueLinkType")
        return [
            {
                "id": t.get("id", ""),
                "name": t.get("name", ""),
                "inward": t.get("inward", ""),
                "outward": t.get("outward", ""),
            }
            for t in raw.get("issueLinkTypes", [])
        ]

    def link_issues(self, link_type: str, inward_key: str, outward_key: str) -> dict:
        return self._t.request(
            "POST", f"{_API}/issueLink",
            body={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
            is_mutation=True,
        )

    def set_parent(self, key: str, parent: str) -> dict:
        if parent.lower() in {"none", "clear"}:
            field_id = self._epic_link_field_id()
            cleared = {field_id: None} if field_id else {"parent": None}
            return self._t.request(
                "PUT", f"{_API}/issue/{key}",
                body={"fields": cleared}, is_mutation=True,
            )
        return self._t.request(
            "PUT", f"{_API}/issue/{key}",
            body={"fields": self._parent_fields(parent)}, is_mutation=True,
        )

    def unlink_issues(self, link_id: str) -> dict:
        return self._t.request(
            "DELETE", f"{_API}/issueLink/{link_id}", is_mutation=True
        )

    def delete_issue(self, key: str, *, delete_subtasks: bool = False) -> dict:
        return self._t.request(
            "DELETE", f"{_API}/issue/{key}",
            params={"deleteSubtasks": "true" if delete_subtasks else "false"},
            is_mutation=True,
        )

    def add_worklog(self, key: str, time_spent: str, *, comment: str = "") -> dict:
        body: dict[str, Any] = {"timeSpent": time_spent}
        if comment:
            body["comment"] = comment
        raw = self._t.request(
            "POST", f"{_API}/issue/{key}/worklog", body=body, is_mutation=True
        )
        return normalize_worklog(raw, _text_of)

    def list_worklogs(self, key: str) -> list[dict]:
        raw = self._t.request("GET", f"{_API}/issue/{key}/worklog")
        return [normalize_worklog(w, _text_of) for w in raw.get("worklogs", [])]

    def attach_file(self, key: str, filename: str, content: bytes) -> list[dict]:
        data, content_type = encode_multipart_file(filename, content)
        raw = self._t.request(
            "POST", f"{_API}/issue/{key}/attachments",
            raw_data=data, content_type=content_type,
            extra_headers={"X-Atlassian-Token": "no-check"},
            is_mutation=True,
        )
        return [normalize_attachment(a)
                for a in (raw if isinstance(raw, list) else [])]

    def list_boards(self, *, project: str = "", limit: int = 50) -> list[dict]:
        raw = self._t.request(
            "GET", f"{_AGILE}/board",
            params={"projectKeyOrId": project, "maxResults": limit},
        )
        return [normalize_board(b) for b in raw.get("values", [])]

    def list_sprints(self, board_id: int, *, state: str = "",
                     limit: int = 50) -> list[dict]:
        raw = self._t.request(
            "GET", f"{_AGILE}/board/{board_id}/sprint",
            params={"state": state, "maxResults": limit},
        )
        return [normalize_sprint(s) for s in raw.get("values", [])]

    def move_to_sprint(self, sprint_id: int, issue_keys: list[str]) -> dict:
        return self._t.request(
            "POST", f"{_AGILE}/sprint/{sprint_id}/issue",
            body={"issues": issue_keys}, is_mutation=True,
        )

    def move_to_backlog(self, issue_keys: list[str]) -> dict:
        return self._t.request(
            "POST", f"{_AGILE}/backlog/issue",
            body={"issues": issue_keys}, is_mutation=True,
        )
