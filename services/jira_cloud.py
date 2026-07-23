"""Jira Cloud backend — REST v3, Basic auth (email + API token), ADF bodies.

Search uses the enhanced ``/rest/api/3/search/jql`` endpoint with
``nextPageToken`` pagination (the legacy offset search API was removed on
Cloud). Rich-text bodies are Atlassian Document Format; plain text is wrapped
into a minimal single-doc ADF on write and flattened back to text on read.
"""
from __future__ import annotations

import base64
from typing import Any

from services.jira_client import (
    JiraTransport,
    normalize_comment,
    normalize_issue,
    normalize_transition,
)

_API = "/rest/api/3"

_DEFAULT_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,project,"
    "created,updated,labels"
)


# ── ADF helpers ───────────────────────────────────────────


def adf_from_text(text: str) -> dict:
    """Wrap plain text in a minimal ADF document (one paragraph per line)."""
    paragraphs = []
    for line in (text or "").split("\n"):
        content = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def text_from_adf(node: Any) -> str:
    """Flatten an ADF document to plain text. Strings pass through."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            node_type = n.get("type")
            if node_type == "text":
                out.append(n.get("text", ""))
            elif node_type == "hardBreak":
                out.append("\n")
            for child in n.get("content") or []:
                walk(child)
            if node_type in ("paragraph", "heading"):
                out.append("\n")
        elif isinstance(n, list):
            for child in n:
                walk(child)

    walk(node)
    return "".join(out).strip()


class CloudBackend:
    """Jira Cloud strategy behind the ``JiraClient`` facade."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        timeout: int = 30,
        verify_tls: bool = True,
        ca_bundle: str = "",
    ):
        raw = f"{email}:{api_token}".encode("utf-8")
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
        self._t = JiraTransport(
            base_url, auth, timeout=timeout, verify_tls=verify_tls, ca_bundle=ca_bundle
        )

    # Health / identity
    def server_info(self) -> dict:
        return self._t.request("GET", f"{_API}/serverInfo")

    def myself(self) -> dict:
        return self._t.request("GET", f"{_API}/myself")

    # Read
    def search(self, jql: str, *, fields: str = "", limit: int = 25, cursor: str = "") -> dict:
        params = {
            "jql": jql,
            "maxResults": limit,
            "fields": fields or _DEFAULT_FIELDS,
        }
        if cursor:
            params["nextPageToken"] = cursor
        page = self._t.request("GET", f"{_API}/search/jql", params=params)
        issues = [normalize_issue(i, text_from_adf) for i in page.get("issues", [])]
        return {"issues": issues, "next_cursor": page.get("nextPageToken", "") or ""}

    def get_issue(self, key: str, *, include_comments: bool = True) -> dict:
        fields = _DEFAULT_FIELDS + ",description"
        if include_comments:
            fields += ",comment"
        raw = self._t.request("GET", f"{_API}/issue/{key}", params={"fields": fields})
        return normalize_issue(raw, text_from_adf)

    def list_projects(self, *, query: str = "", limit: int = 50) -> list[dict]:
        page = self._t.request(
            "GET", f"{_API}/project/search",
            params={"query": query, "maxResults": limit},
        )
        return [
            {"key": p.get("key", ""), "name": p.get("name", ""), "id": p.get("id", "")}
            for p in page.get("values", [])
        ]

    def list_transitions(self, key: str) -> list[dict]:
        raw = self._t.request("GET", f"{_API}/issue/{key}/transitions")
        return [normalize_transition(t) for t in raw.get("transitions", [])]

    def get_create_metadata(self, project: str, issue_type: str) -> dict:
        """Two-step Cloud createmeta: resolve the issue-type id, then fetch
        its field metadata."""
        types_page = self._t.request(
            "GET", f"{_API}/issue/createmeta/{project}/issuetypes"
        )
        issue_types = types_page.get("issueTypes", types_page.get("values", []))
        match = next(
            (t for t in issue_types
             if (t.get("name") or "").lower() == issue_type.lower()),
            None,
        )
        if match is None:
            known = ", ".join(t.get("name", "?") for t in issue_types)
            return {"project": project, "issue_type": issue_type,
                    "error": f"unknown issue type; project has: {known}"}
        fields_page = self._t.request(
            "GET", f"{_API}/issue/createmeta/{project}/issuetypes/{match.get('id')}"
        )
        raw_fields = fields_page.get("fields", fields_page.get("values", []))
        required, optional = [], []
        for f in raw_fields:
            entry = {
                "id": f.get("fieldId", f.get("key", "")),
                "name": f.get("name", ""),
                "type": (f.get("schema") or {}).get("type", ""),
            }
            (required if f.get("required") else optional).append(entry)
        return {
            "project": project,
            "issue_type": issue_type,
            "required_fields": required,
            "optional_fields": [f["name"] for f in optional],
        }

    def search_users(self, query: str, *, limit: int = 10) -> list[dict]:
        users = self._t.request(
            "GET", f"{_API}/user/search", params={"query": query, "maxResults": limit}
        )
        return [
            {
                "id": u.get("accountId", ""),
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
            payload_fields["description"] = adf_from_text(description)
        if fields:
            payload_fields.update(fields)
        return self._t.request(
            "POST", f"{_API}/issue", body={"fields": payload_fields}, is_mutation=True
        )

    def add_comment(self, key: str, body: Any, *, body_format: str = "text") -> dict:
        adf = body if body_format == "adf" else adf_from_text(str(body))
        raw = self._t.request(
            "POST", f"{_API}/issue/{key}/comment", body={"body": adf}, is_mutation=True
        )
        return normalize_comment(raw, text_from_adf)

    def transition_issue(
        self, key: str, transition_id: str, *, comment: str = "", fields: dict | None = None
    ) -> dict:
        body: dict[str, Any] = {"transition": {"id": str(transition_id)}}
        if comment:
            body["update"] = {"comment": [{"add": {"body": adf_from_text(comment)}}]}
        if fields:
            body["fields"] = fields
        return self._t.request(
            "POST", f"{_API}/issue/{key}/transitions", body=body, is_mutation=True
        )

    def assign_issue(self, key: str, user_id: str) -> dict:
        return self._t.request(
            "PUT", f"{_API}/issue/{key}/assignee",
            body={"accountId": user_id}, is_mutation=True,
        )
