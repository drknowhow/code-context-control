"""Bitbucket Data Center / Server REST client.

Stdlib-only HTTP via urllib.request, Bearer-token (PAT) auth.

Targets the on-prem 'Data Center' / 'Server' product, NOT Bitbucket Cloud
(which uses a different host, auth model, and API path).

Surface:
  * Read       — projects, repos, PRs, branches, commits, builds, activity
  * PR writes  — create / comment / approve / unapprove / decline / merge
  * Branch     — create / delete
  * Admin      — repo settings, webhooks, permissions

All methods raise ``BitbucketError`` on transport failure or non-2xx
response, with the HTTP status, request path, and parsed error message
preserved on the exception.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

_TIMEOUT = 30  # seconds
_API = "/rest/api/1.0"
_BUILD_API = "/rest/build-status/1.0"
_BRANCH_UTILS = "/rest/branch-utils/1.0"

try:
    from importlib.metadata import version as _pkg_version

    _C3_VERSION = _pkg_version("code-context-control")
except Exception:  # pragma: no cover - package metadata may be unavailable
    _C3_VERSION = "0.0.0"

_USER_AGENT = f"c3-bitbucket/{_C3_VERSION}"


class BitbucketError(RuntimeError):
    """Raised on non-2xx responses or transport failures."""

    def __init__(self, message: str, *, status: int = 0, path: str = "", body: Any = None):
        super().__init__(message)
        self.status = status
        self.path = path
        self.body = body


class BitbucketDataCenterClient:
    """Minimal Bitbucket Data Center / Server REST client."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int = _TIMEOUT,
        verify_tls: bool = True,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._verify_tls = verify_tls

    # ── Internal HTTP ─────────────────────────────────────

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self._verify_tls:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict | None = None,
        api_root: str = _API,
        raw: bool = False,
        accept: str = "application/json",
        return_headers: bool = False,
    ) -> Any:
        full_path = f"{api_root}{path}"
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""}
            )
            if qs:
                full_path = f"{full_path}?{qs}"
        url = f"{self.base_url}{full_path}"

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "User-Agent": _USER_AGENT,
        }
        data: bytes | None = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif isinstance(body, bytes):
                data = body
            else:
                data = str(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ssl_context()) as resp:
                payload = resp.read()
                resp_headers = {k: v for k, v in resp.headers.items()}
                if raw:
                    body_out: Any = payload
                elif not payload:
                    body_out = {}
                else:
                    try:
                        body_out = json.loads(payload.decode("utf-8"))
                    except json.JSONDecodeError:
                        body_out = {"raw": payload.decode("utf-8", errors="replace")}
                if return_headers:
                    return body_out, resp_headers
                return body_out
        except urllib.error.HTTPError as exc:
            try:
                err_payload = json.loads(exc.read().decode("utf-8"))
                msg = err_payload.get("errors", [{}])[0].get("message") or str(exc)
            except Exception:
                err_payload = None
                msg = str(exc)
            raise BitbucketError(
                f"HTTP {exc.code} on {method.upper()} {full_path}: {msg}",
                status=exc.code,
                path=full_path,
                body=err_payload,
            ) from exc
        except urllib.error.URLError as exc:
            raise BitbucketError(
                f"Transport failure on {method.upper()} {full_path}: {exc.reason}",
                status=0,
                path=full_path,
            ) from exc

    def _paged(
        self,
        path: str,
        *,
        params: dict | None = None,
        api_root: str = _API,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> Iterator[dict]:
        """Yield items across paged ``values`` arrays. Caps at ``max_pages``."""
        start = 0
        for _ in range(max_pages):
            page_params = dict(params or {})
            page_params.setdefault("limit", page_size)
            page_params["start"] = start
            page = self._request("GET", path, params=page_params, api_root=api_root)
            for item in page.get("values", []):
                yield item
            if page.get("isLastPage", True):
                return
            start = page.get("nextPageStart")
            if start is None:
                return

    # ── Health / identity ─────────────────────────────────

    def application_properties(self) -> dict:
        """Server version + identity. Useful for connection tests."""
        return self._request("GET", "/application-properties")

    def whoami(self) -> dict:
        """The authenticated user (PAT owner).

        Bitbucket Data Center / Server has no ``/users/me`` endpoint (that is a
        Cloud convention; DC treats ``me`` as a literal username and 404s).
        Resolve the account from the ``X-AUSERNAME`` header that rides on every
        authenticated response, then enrich with the user record when possible.
        """
        _body, headers = self._request(
            "GET", "/application-properties", return_headers=True
        )
        slug = ""
        for key, value in (headers or {}).items():
            if key.lower() == "x-ausername":
                slug = urllib.parse.unquote(value or "")
                break
        if not slug:
            return {}
        try:
            return self._request("GET", f"/users/{urllib.parse.quote(slug)}")
        except BitbucketError:
            return {"name": slug, "slug": slug, "displayName": slug}

    # ── Projects & repos (read) ───────────────────────────

    def list_projects(self, *, name: str = "") -> list[dict]:
        return list(self._paged("/projects", params={"name": name} if name else None))

    def list_repos(self, project_key: str) -> list[dict]:
        return list(self._paged(f"/projects/{project_key}/repos"))

    def get_repo(self, project_key: str, repo_slug: str) -> dict:
        return self._request("GET", f"/projects/{project_key}/repos/{repo_slug}")

    def get_default_branch(self, project_key: str, repo_slug: str) -> dict:
        return self._request(
            "GET", f"/projects/{project_key}/repos/{repo_slug}/default-branch"
        )

    def list_branches(
        self, project_key: str, repo_slug: str, *, filter_text: str = ""
    ) -> list[dict]:
        params = {"details": "true"}
        if filter_text:
            params["filterText"] = filter_text
        return list(
            self._paged(
                f"/projects/{project_key}/repos/{repo_slug}/branches", params=params
            )
        )

    def list_commits(
        self,
        project_key: str,
        repo_slug: str,
        *,
        branch: str = "",
        until: str = "",
        path: str = "",
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if branch:
            params["until"] = branch
        if until:
            params["until"] = until
        if path:
            params["path"] = path
        page = self._request(
            "GET", f"/projects/{project_key}/repos/{repo_slug}/commits", params=params
        )
        return page.get("values", [])

    def list_repo_activities(
        self, project_key: str, repo_slug: str, *, limit: int = 50
    ) -> list[dict]:
        """Recent activity feed (commits + PR events) — uses commits as the
        portable signal; full activity feeds require add-ons on some
        DC versions."""
        return self.list_commits(project_key, repo_slug, limit=limit)

    # ── Pull requests (read) ──────────────────────────────

    def list_pull_requests(
        self,
        project_key: str,
        repo_slug: str,
        *,
        state: str = "OPEN",
        order: str = "NEWEST",
        author: str = "",
        reviewer: str = "",
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "state": state,
            "order": order,
            "limit": limit,
        }
        if author:
            params["role.1"] = "AUTHOR"
            params["username.1"] = author
        if reviewer:
            params["role.2"] = "REVIEWER"
            params["username.2"] = reviewer
        page = self._request(
            "GET",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests",
            params=params,
        )
        return page.get("values", [])

    def get_pull_request(
        self, project_key: str, repo_slug: str, pr_id: int
    ) -> dict:
        return self._request(
            "GET",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}",
        )

    def get_pr_diff(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        *,
        context_lines: int = 3,
    ) -> str:
        """Return the unified diff for a PR (text)."""
        payload = self._request(
            "GET",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/diff",
            params={"contextLines": context_lines},
            raw=True,
            accept="text/plain",
        )
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)

    def get_pr_activities(
        self, project_key: str, repo_slug: str, pr_id: int, *, limit: int = 50
    ) -> list[dict]:
        page = self._request(
            "GET",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/activities",
            params={"limit": limit},
        )
        return page.get("values", [])

    # ── Pull requests (write) ─────────────────────────────

    def create_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        *,
        title: str,
        from_branch: str,
        to_branch: str,
        description: str = "",
        reviewers: list[str] | None = None,
    ) -> dict:
        body = {
            "title": title,
            "description": description,
            "fromRef": {
                "id": from_branch if from_branch.startswith("refs/") else f"refs/heads/{from_branch}",
                "repository": {"slug": repo_slug, "project": {"key": project_key}},
            },
            "toRef": {
                "id": to_branch if to_branch.startswith("refs/") else f"refs/heads/{to_branch}",
                "repository": {"slug": repo_slug, "project": {"key": project_key}},
            },
            "reviewers": [{"user": {"name": u}} for u in (reviewers or [])],
        }
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests",
            body=body,
        )

    def update_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        *,
        version: int,
        title: str,
        description: str = "",
        to_branch: str = "",
        reviewers: list[str] | None = None,
    ) -> dict:
        """Edit an open PR (title/description/reviewers/target branch).

        Bitbucket's PUT replaces the record, so callers pass the FULL desired
        state — title is always required, and ``reviewers`` is the complete
        reviewer set (``None`` = field omitted entirely, which Bitbucket
        treats as "leave unchanged" only for toRef, not reviewers — the tool
        layer merges current values before calling). ``version`` is the PR's
        current version for optimistic locking."""
        body: dict[str, Any] = {
            "version": version,
            "title": title,
            "description": description,
        }
        if reviewers is not None:
            body["reviewers"] = [{"user": {"name": u}} for u in reviewers]
        if to_branch:
            body["toRef"] = {
                "id": to_branch if to_branch.startswith("refs/") else f"refs/heads/{to_branch}",
                "repository": {"slug": repo_slug, "project": {"key": project_key}},
            }
        return self._request(
            "PUT",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}",
            body=body,
        )

    def comment_on_pr(
        self, project_key: str, repo_slug: str, pr_id: int, *, text: str
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/comments",
            body={"text": text},
        )

    def get_pr_commits(
        self, project_key: str, repo_slug: str, pr_id: int
    ) -> list[dict]:
        return list(self._paged(
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/commits"
        ))

    def get_pr_comment(
        self, project_key: str, repo_slug: str, pr_id: int, comment_id: int
    ) -> dict:
        return self._request(
            "GET",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            f"/comments/{comment_id}",
        )

    def update_pr_comment(
        self, project_key: str, repo_slug: str, pr_id: int, comment_id: int, *,
        version: int, text: str,
    ) -> dict:
        """Edit a comment's text. ``version`` is the comment's current
        version (optimistic locking, same scheme as PRs)."""
        return self._request(
            "PUT",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            f"/comments/{comment_id}",
            body={"text": text, "version": version},
        )

    def delete_pr_comment(
        self, project_key: str, repo_slug: str, pr_id: int, comment_id: int, *,
        version: int,
    ) -> dict:
        return self._request(
            "DELETE",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            f"/comments/{comment_id}",
            params={"version": version},
        )

    # PR tasks. Since Bitbucket 7.2 a task IS a comment with severity
    # BLOCKER; the standalone /tasks API was removed. Listing uses the
    # dedicated blocker-comments endpoint, resolution is a comment-state
    # transition.
    def list_pr_tasks(
        self, project_key: str, repo_slug: str, pr_id: int, *, state: str = ""
    ) -> list[dict]:
        params = {"state": state} if state else None
        return list(self._paged(
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            f"/blocker-comments",
            params=params,
        ))

    def create_pr_task(
        self, project_key: str, repo_slug: str, pr_id: int, *, text: str
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/comments",
            body={"text": text, "severity": "BLOCKER"},
        )

    def set_pr_task_state(
        self, project_key: str, repo_slug: str, pr_id: int, comment_id: int, *,
        version: int, state: str,
    ) -> dict:
        """``state`` is ``RESOLVED`` or ``OPEN``."""
        return self._request(
            "PUT",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            f"/comments/{comment_id}",
            body={"state": state, "version": version},
        )

    def approve_pr(
        self, project_key: str, repo_slug: str, pr_id: int
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/approve",
        )

    def unapprove_pr(
        self, project_key: str, repo_slug: str, pr_id: int
    ) -> dict:
        return self._request(
            "DELETE",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/approve",
        )

    def set_pr_reviewer_status(
        self, project_key: str, repo_slug: str, pr_id: int, *,
        user_slug: str, status: str,
    ) -> dict:
        """Set the AUTHENTICATED user's review verdict on a PR —
        ``UNAPPROVED`` / ``NEEDS_WORK`` / ``APPROVED``. Bitbucket only lets a
        user set their own status, so ``user_slug`` must be the caller's."""
        return self._request(
            "PUT",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            f"/participants/{urllib.parse.quote(user_slug)}",
            body={"status": status},
        )

    def decline_pr(
        self, project_key: str, repo_slug: str, pr_id: int, *, version: int
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/decline",
            params={"version": version},
        )

    def merge_pr(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        *,
        version: int,
        message: str = "",
    ) -> dict:
        body: dict[str, Any] = {}
        if message:
            body["message"] = message
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/merge",
            params={"version": version},
            body=body or None,
        )

    # ── Branch writes ─────────────────────────────────────

    def create_branch(
        self,
        project_key: str,
        repo_slug: str,
        *,
        name: str,
        start_point: str,
        message: str = "",
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/branches",
            api_root=_BRANCH_UTILS,
            body={"name": name, "startPoint": start_point, "message": message},
        )

    def delete_branch(
        self, project_key: str, repo_slug: str, *, name: str, dry_run: bool = False
    ) -> dict:
        return self._request(
            "DELETE",
            f"/projects/{project_key}/repos/{repo_slug}/branches",
            api_root=_BRANCH_UTILS,
            body={"name": name, "dryRun": dry_run},
        )

    # ── Build status ──────────────────────────────────────

    def get_build_status(self, commit_id: str) -> list[dict]:
        page = self._request(
            "GET", f"/commits/{commit_id}", api_root=_BUILD_API
        )
        return page.get("values", []) if isinstance(page, dict) else []

    # ── Repo administration ───────────────────────────────

    def get_repo_settings(self, project_key: str, repo_slug: str) -> dict:
        return self.get_repo(project_key, repo_slug)

    def update_repo_settings(
        self, project_key: str, repo_slug: str, *, settings: dict
    ) -> dict:
        return self._request(
            "PUT", f"/projects/{project_key}/repos/{repo_slug}", body=settings
        )

    def list_webhooks(self, project_key: str, repo_slug: str) -> list[dict]:
        return list(self._paged(f"/projects/{project_key}/repos/{repo_slug}/webhooks"))

    def create_webhook(
        self,
        project_key: str,
        repo_slug: str,
        *,
        name: str,
        url: str,
        events: list[str],
        active: bool = True,
        secret: str = "",
    ) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "url": url,
            "events": events,
            "active": active,
        }
        if secret:
            body["configuration"] = {"secret": secret}
        return self._request(
            "POST",
            f"/projects/{project_key}/repos/{repo_slug}/webhooks",
            body=body,
        )

    def delete_webhook(
        self, project_key: str, repo_slug: str, *, webhook_id: int
    ) -> dict:
        return self._request(
            "DELETE",
            f"/projects/{project_key}/repos/{repo_slug}/webhooks/{webhook_id}",
        )

    def list_repo_user_permissions(
        self, project_key: str, repo_slug: str
    ) -> list[dict]:
        return list(
            self._paged(
                f"/projects/{project_key}/repos/{repo_slug}/permissions/users"
            )
        )

    def list_repo_group_permissions(
        self, project_key: str, repo_slug: str
    ) -> list[dict]:
        return list(
            self._paged(
                f"/projects/{project_key}/repos/{repo_slug}/permissions/groups"
            )
        )
