"""c3_bitbucket — Bitbucket Data Center / Server integration.

Action-dispatch tool. Resolves credentials from the OS keyring (via
``services.bitbucket_credentials``) and the active account / default
project+repo from ``.c3/config.json``.

Long responses are capped at ``_RESPONSE_TOKEN_CAP`` to protect the
caller's context budget. ``format="json"`` returns normalized JSON for
programs instead (see cli/tools/_json_reply.py).
"""
from __future__ import annotations

import json
from typing import Any

from cli.tools import _json_reply as jr
from core import count_tokens
from core.config import load_bitbucket_config
from services import bitbucket_credentials as bb_creds
from services.bitbucket_client import BitbucketDataCenterClient, BitbucketError

_TOOL = "c3_bitbucket"
_RESPONSE_TOKEN_CAP = 2400
_DIFF_PREVIEW_CHARS = 6000  # diffs above this are truncated

# Actions that change repo state — flagged for ledger logging.
_MUTATING_ACTIONS = {
    "create_pr", "update_pr", "comment_pr", "approve_pr", "unapprove_pr",
    "needs_work_pr", "decline_pr", "merge_pr",
    "update_pr_comment", "delete_pr_comment",
    "create_pr_task", "resolve_pr_task",
    "create_branch", "delete_branch",
    "update_repo_settings", "create_webhook", "delete_webhook",
}

# Actions that can fall back to default_project/default_repo from config.
_REPO_SCOPED = {
    "list_repos", "get_repo", "list_prs", "get_pr", "get_pr_diff",
    "get_pr_activities", "get_pr_commits", "create_pr", "update_pr",
    "comment_pr", "update_pr_comment", "delete_pr_comment", "approve_pr",
    "unapprove_pr", "needs_work_pr", "decline_pr", "merge_pr",
    "list_pr_tasks", "create_pr_task", "resolve_pr_task", "list_branches",
    "create_branch", "delete_branch", "list_commits", "list_activity",
    "repo_settings", "update_repo_settings", "list_webhooks",
    "create_webhook", "delete_webhook", "list_permissions",
    "readme", "get_file", "pr_issues",
}

# Actions that need a pr_id.
_PR_ACTIONS = {
    "get_pr", "get_pr_diff", "get_pr_activities", "get_pr_commits",
    "pr_issues", "update_pr", "comment_pr", "update_pr_comment",
    "delete_pr_comment", "list_pr_tasks", "create_pr_task",
    "resolve_pr_task", "approve_pr", "unapprove_pr", "needs_work_pr",
    "decline_pr", "merge_pr",
}

_VALID_ACTIONS = sorted(
    {"status", "whoami", "list_projects", "build_status"} | _REPO_SCOPED
)

# Branch metadata keys Bitbucket adds with details=true (plugins permitting).
_META = "com.atlassian.bitbucket.server."
_META_AHEAD_BEHIND = _META + "bitbucket-branch:ahead-behind-metadata-provider"
_META_LAST_COMMIT = _META + "bitbucket-branch:latest-commit-metadata"
_META_JIRA = _META + "bitbucket-jira:branch-list-jira-issues"
_META_OUTGOING_PR = _META + "bitbucket-ref-metadata:outgoing-pull-request-metadata"
_META_BUILDS = _META + "bitbucket-build:build-status-metadata"


def _cap(resp: str) -> str:
    """Truncate response if over the token cap (matches cli/tools/search.py)."""
    if count_tokens(resp) <= _RESPONSE_TOKEN_CAP:
        return resp
    lines = resp.split("\n")
    while len(lines) > 1:
        lines = lines[:len(lines) * 3 // 4]
        candidate = "\n".join(lines) + "\n[truncated]"
        if count_tokens(candidate) <= _RESPONSE_TOKEN_CAP:
            return candidate
    # A single over-long line never splits above; hard-clamp by characters as a
    # final guard (~4 chars/token) so one huge line can't blow past the cap.
    head = "\n".join(lines[:20])
    if count_tokens(head) > _RESPONSE_TOKEN_CAP:
        head = head[:_RESPONSE_TOKEN_CAP * 4]
    return head + "\n[truncated]"


def _build_client(svc) -> tuple[BitbucketDataCenterClient | None, str]:
    """Resolve active account + token. Returns (client_or_None, error_msg)."""
    project_path = getattr(svc, "project_path", ".") or "."
    cfg = load_bitbucket_config(project_path)
    active = cfg.get("active") or {}
    base_url = active.get("base_url")
    username = active.get("username")
    if not base_url or not username:
        return None, (
            "[bitbucket:no-account] No active Bitbucket account.\n"
            "Run: c3 bitbucket login --url <BITBUCKET_URL>"
        )
    try:
        token = bb_creds.load_token(base_url, username)
    except RuntimeError as exc:
        return None, f"[bitbucket:keyring-error] {exc}"
    if not token:
        return None, (
            f"[bitbucket:no-token] No keyring entry for {username}@{base_url}.\n"
            f"Run: c3 bitbucket login --url {base_url} --username {username}"
        )
    client = BitbucketDataCenterClient(
        base_url=base_url,
        token=token,
        verify_tls=bool(cfg.get("verify_tls", True)),
    )
    return client, ""


def _resolve_repo(action: str, kwargs: dict, svc) -> tuple[str, str, str]:
    """Resolve project_key + repo_slug, falling back to config defaults.
    Returns (project, repo, error_msg)."""
    project_path = getattr(svc, "project_path", ".") or "."
    cfg = load_bitbucket_config(project_path)
    project = (kwargs.get("project") or "").strip() or cfg.get("default_project", "")
    repo = (kwargs.get("repo") or "").strip() or cfg.get("default_repo", "")
    if action in {"list_repos"}:
        if not project:
            return "", "", "[bitbucket:missing-arg] project key required (or set default via `c3 bitbucket set-default`)"
        return project, "", ""
    if not project or not repo:
        return "", "", (
            "[bitbucket:missing-arg] project and repo are required.\n"
            "Pass project=<KEY>, repo=<SLUG> or run `c3 bitbucket set-default --project K --repo R`."
        )
    return project, repo, ""


def _href(links: dict | None, rel: str = "self") -> str:
    """The first href under ``links[rel]`` of a Bitbucket entity."""
    for link in (links or {}).get(rel) or []:
        if isinstance(link, dict) and link.get("href"):
            return link["href"]
    return ""


def _filter_text(kwargs: dict) -> str:
    return kwargs.get("filter_text") or kwargs.get("filter") or ""


# ── Formatters ────────────────────────────────────────────


def _format_pr(pr: dict) -> str:
    state = pr.get("state", "?")
    pr_id = pr.get("id", "?")
    title = pr.get("title", "")
    author = (pr.get("author") or {}).get("user", {}).get("name", "?")
    src = (pr.get("fromRef") or {}).get("displayId", "?")
    dst = (pr.get("toRef") or {}).get("displayId", "?")
    return f"  #{pr_id} [{state:7}] {src} -> {dst}  by {author}\n         {title}"


def _format_pr_full(pr: dict) -> str:
    lines = [
        f"PR #{pr.get('id')} [{pr.get('state')}] -- {pr.get('title','')}",
        f"  {(pr.get('fromRef') or {}).get('displayId')} -> {(pr.get('toRef') or {}).get('displayId')}",
        f"  Author: {(pr.get('author') or {}).get('user',{}).get('displayName','?')}",
        f"  Version: {pr.get('version')} | Open tasks: {pr.get('openTaskCount', 0)}",
    ]
    reviewers = pr.get("reviewers") or []
    if reviewers:
        rs = ", ".join(
            f"{r.get('user',{}).get('name','?')}({'[x]' if r.get('approved') else '[ ]'})"
            for r in reviewers
        )
        lines.append(f"  Reviewers: {rs}")
    desc = (pr.get("description") or "").strip()
    if desc:
        lines.append("")
        lines.append(desc)
    return "\n".join(lines)


def _format_branch(b: dict) -> str:
    name = b.get("displayId", b.get("id", "?"))
    latest = b.get("latestCommit", "")[:8]
    is_default = " [default]" if b.get("isDefault") else ""
    behind = b.get("metadata", {}).get(_META_AHEAD_BEHIND)
    ab = ""
    if isinstance(behind, dict):
        ab = f"  +{behind.get('ahead',0)}/-{behind.get('behind',0)}"
    return f"  {name:40} {latest}{ab}{is_default}"


def _format_commit(c: dict) -> str:
    ch = (c.get("id") or "")[:8]
    msg = (c.get("message") or "").splitlines()[0] if c.get("message") else ""
    author = (c.get("author") or {}).get("displayName", "?")
    return f"  {ch}  {author:24} {msg}"


def _format_build(b: dict) -> str:
    return f"  [{b.get('state','?'):7}] {b.get('key','?'):20} {b.get('description','') or b.get('name','')}"


def _format_activity(act: dict) -> str:
    typ = act.get("action") or act.get("type") or "?"
    user = (act.get("user") or {}).get("name") or (act.get("user") or {}).get("displayName", "?")
    created = act.get("createdDate", "")
    return f"  [{typ}] by {user} @ {created}"


# ── Action handlers ───────────────────────────────────────


def _act_status(client: BitbucketDataCenterClient | None, err: str, svc) -> str:
    project_path = getattr(svc, "project_path", ".") or "."
    cfg = load_bitbucket_config(project_path)
    active = cfg.get("active") or {}
    accounts = cfg.get("accounts") or []
    out = ["[bitbucket:status]"]
    out.append(f"  Active : {active.get('username','-')}@{active.get('base_url','-')}")
    out.append(f"  Defaults: project={cfg.get('default_project') or '-'} repo={cfg.get('default_repo') or '-'}")
    out.append(f"  Accounts: {len(accounts)} configured")
    for a in accounts:
        marker = "*" if a == active else "-"
        out.append(f"    {marker} {a.get('username','?')}@{a.get('base_url','?')}")
    if client is None:
        out.append("")
        out.append(err or "[no client]")
        return "\n".join(out)
    try:
        props = client.application_properties()
        version = props.get("version", "?")
        out.append(f"  Server : OK (version {version})")
    except BitbucketError as exc:
        out.append(f"  Server : FAIL -- {exc}")
    return "\n".join(out)


def _act_whoami(client: BitbucketDataCenterClient) -> str:
    user = client.whoami()
    return (
        "[bitbucket:whoami]\n"
        f"  name:        {user.get('name','?')}\n"
        f"  displayName: {user.get('displayName','?')}\n"
        f"  email:       {user.get('emailAddress','?')}\n"
        f"  active:      {user.get('active', '?')}"
    )


def _act_list_projects(client: BitbucketDataCenterClient, kwargs: dict) -> str:
    name = kwargs.get("name", "") or kwargs.get("query", "")
    projects = client.list_projects(name=name)
    if not projects:
        return "[bitbucket:projects] (none)"
    lines = [f"[bitbucket:projects] {len(projects)} project(s)"]
    for p in projects[:50]:
        lines.append(f"  {p.get('key','?'):16} {p.get('name','?')}")
    if len(projects) > 50:
        lines.append(f"  ... +{len(projects) - 50} more")
    return "\n".join(lines)


def _act_list_repos(client: BitbucketDataCenterClient, project: str) -> str:
    repos = client.list_repos(project)
    if not repos:
        return f"[bitbucket:repos] {project}: (none)"
    lines = [f"[bitbucket:repos] {project} - {len(repos)} repo(s)"]
    for r in repos[:80]:
        lines.append(f"  {r.get('slug','?'):30} {r.get('name','?')}")
    if len(repos) > 80:
        lines.append(f"  ... +{len(repos) - 80} more")
    return "\n".join(lines)


def _default_branch(client, project: str, repo: str) -> str:
    """The default branch name; empty for an empty repo or on any error."""
    try:
        return (client.get_default_branch(project, repo) or {}).get("displayId", "")
    except BitbucketError:
        return ""


def _act_get_repo(client: BitbucketDataCenterClient, project: str, repo: str) -> str:
    r = client.get_repo(project, repo)
    lines = [
        f"[bitbucket:repo] {project}/{repo}",
        f"  name:    {r.get('name','?')}",
    ]
    if r.get("description"):
        lines.append(f"  description: {r['description']}")
    lines += [
        f"  default: {_default_branch(client, project, repo) or '-'}",
        f"  url:     {_href(r.get('links')) or '-'}",
        f"  scmId:   {r.get('scmId','?')}",
        f"  public:  {r.get('public','?')}",
        f"  state:   {r.get('state','?')}",
        f"  links:   {[l.get('href') for l in (r.get('links',{}).get('clone') or [])]}",
    ]
    return "\n".join(lines)


def _act_readme(client, project: str, repo: str, kwargs: dict) -> str:
    f = client.readme(project, repo, at=kwargs.get("at") or "")
    if not f.get("found"):
        return f"[bitbucket:readme] {project}/{repo}: no README at the root"
    return _format_file("readme", project, repo, f)


def _act_get_file(client, project: str, repo: str, kwargs: dict) -> str:
    path = (kwargs.get("path") or "").strip()
    if not path:
        return "[bitbucket:get_file:error] path is required"
    f = client.get_file(project, repo, path, at=kwargs.get("at") or "")
    return _format_file("file", project, repo, f)


def _format_file(tag: str, project: str, repo: str, f: dict) -> str:
    where = f"{f.get('path')}@{f['at']}" if f.get("at") else f.get("path")
    head = f"[bitbucket:{tag}] {project}/{repo} {where} ({f.get('size', 0)} bytes"
    if f.get("binary"):
        return f"{head}): binary file, not shown"
    if f.get("truncated"):
        head += ", truncated"
    return f"{head})\n{f.get('text', '')}"


def _act_pr_issues(client, project: str, repo: str, pr_id: int) -> str:
    issues = client.pr_issues(project, repo, pr_id)
    if not issues:
        return f"[bitbucket:pr_issues] {project}/{repo}#{pr_id}: 0 issue(s)"
    out = [f"[bitbucket:pr_issues] {project}/{repo}#{pr_id}: {len(issues)} issue(s)"]
    out.extend(f"  {i.get('key', '?'):16} {i.get('url', '')}" for i in issues)
    return "\n".join(out)


def _act_list_prs(client, project: str, repo: str, kwargs: dict) -> str:
    state = (kwargs.get("state") or "OPEN").upper()
    prs = client.list_pull_requests(
        project, repo, state=state,
        author=kwargs.get("author", ""),
        reviewer=kwargs.get("reviewer", ""),
        limit=int(kwargs.get("limit", 50)),
    )
    if not prs:
        return f"[bitbucket:prs] {project}/{repo} state={state}: (none)"
    out = [f"[bitbucket:prs] {project}/{repo} state={state} - {len(prs)} PR(s)"]
    for pr in prs:
        out.append(_format_pr(pr))
    return "\n".join(out)


def _act_get_pr(client, project: str, repo: str, pr_id: int) -> str:
    pr = client.get_pull_request(project, repo, pr_id)
    return f"[bitbucket:pr]\n{_format_pr_full(pr)}"


def _act_get_pr_diff(client, project: str, repo: str, pr_id: int, kwargs: dict) -> str:
    diff = client.get_pr_diff(project, repo, pr_id, context_lines=int(kwargs.get("context_lines", 3)))
    if len(diff) > _DIFF_PREVIEW_CHARS:
        diff = diff[:_DIFF_PREVIEW_CHARS] + "\n... [diff truncated]"
    return f"[bitbucket:diff] {project}/{repo}#{pr_id}\n{diff}"


def _act_get_pr_activities(client, project: str, repo: str, pr_id: int) -> str:
    acts = client.get_pr_activities(project, repo, pr_id)
    if not acts:
        return f"[bitbucket:pr-activity] {project}/{repo}#{pr_id}: (none)"
    out = [f"[bitbucket:pr-activity] {project}/{repo}#{pr_id} - {len(acts)} event(s)"]
    for a in acts[:50]:
        out.append(_format_activity(a))
    return "\n".join(out)


def _act_create_pr(client, project: str, repo: str, kwargs: dict) -> str:
    title = kwargs.get("title") or ""
    from_b = kwargs.get("from_branch") or kwargs.get("source") or ""
    to_b = kwargs.get("to_branch") or kwargs.get("target") or ""
    if not title or not from_b or not to_b:
        return "[bitbucket:create_pr:error] title, from_branch, to_branch are required"
    reviewers = kwargs.get("reviewers") or []
    if isinstance(reviewers, str):
        reviewers = [r.strip() for r in reviewers.split(",") if r.strip()]
    pr = client.create_pull_request(
        project, repo,
        title=title, from_branch=from_b, to_branch=to_b,
        description=kwargs.get("description", "") or kwargs.get("body", ""),
        reviewers=reviewers,
    )
    return f"[bitbucket:created]\n{_format_pr_full(pr)}"


def _act_update_pr(client, project: str, repo: str, pr_id: int, kwargs: dict) -> str:
    title = kwargs.get("title") or ""
    description = kwargs.get("description", "") or kwargs.get("body", "")
    to_b = kwargs.get("to_branch") or kwargs.get("target") or ""
    reviewers_raw = kwargs.get("reviewers")
    if not title and not description and not to_b and not reviewers_raw:
        return (
            "[bitbucket:update_pr:error] nothing to change — pass at least one "
            "of title, description, to_branch, reviewers"
        )
    # PUT replaces the PR record, so merge unchanged fields from the live PR.
    pr = client.get_pull_request(project, repo, pr_id)
    reviewers: list[str] | None = None
    if isinstance(reviewers_raw, str) and reviewers_raw.strip():
        reviewers = [r.strip() for r in reviewers_raw.split(",") if r.strip()]
    elif isinstance(reviewers_raw, list) and reviewers_raw:
        reviewers = [str(r) for r in reviewers_raw]
    else:
        reviewers = [
            ((r.get("user") or {}).get("name") or "")
            for r in pr.get("reviewers") or []
        ]
        reviewers = [r for r in reviewers if r]
    updated = client.update_pull_request(
        project, repo, pr_id,
        version=pr.get("version", 0),
        title=title or pr.get("title", ""),
        description=description or pr.get("description", ""),
        to_branch=to_b,
        reviewers=reviewers,
    )
    return f"[bitbucket:updated]\n{_format_pr_full(updated)}"


def _act_comment_pr(client, project: str, repo: str, pr_id: int, kwargs: dict) -> str:
    text = kwargs.get("body") or kwargs.get("text") or ""
    if not text:
        return "[bitbucket:comment_pr:error] body is required"
    res = client.comment_on_pr(project, repo, pr_id, text=text)
    return f"[bitbucket:commented] {project}/{repo}#{pr_id} comment-id={res.get('id','?')}"


def _act_approve_pr(client, project: str, repo: str, pr_id: int) -> str:
    client.approve_pr(project, repo, pr_id)
    return f"[bitbucket:approved] {project}/{repo}#{pr_id}"


def _act_unapprove_pr(client, project: str, repo: str, pr_id: int) -> str:
    client.unapprove_pr(project, repo, pr_id)
    return f"[bitbucket:unapproved] {project}/{repo}#{pr_id}"


def _act_get_pr_commits(client, project: str, repo: str, pr_id: int) -> str:
    commits = client.get_pr_commits(project, repo, pr_id)
    if not commits:
        return f"[bitbucket:pr_commits] {project}/{repo}#{pr_id}: 0 commits"
    lines = [
        f"[bitbucket:pr_commits] {project}/{repo}#{pr_id}: "
        f"{len(commits)} commit(s)"
    ]
    lines.extend(f"  {_format_commit(c)}" for c in commits)
    return "\n".join(lines)


def _act_update_pr_comment(client, project: str, repo: str, pr_id: int,
                           kwargs: dict) -> str:
    comment_id = int(kwargs.get("comment_id") or 0)
    text = kwargs.get("body") or kwargs.get("text") or ""
    if not comment_id or not text:
        return "[bitbucket:update_pr_comment:error] comment_id and body are required"
    current = client.get_pr_comment(project, repo, pr_id, comment_id)
    res = client.update_pr_comment(
        project, repo, pr_id, comment_id,
        version=current.get("version", 0), text=text,
    )
    return (
        f"[bitbucket:comment-updated] {project}/{repo}#{pr_id} "
        f"comment-id={res.get('id', comment_id)}"
    )


def _act_delete_pr_comment(client, project: str, repo: str, pr_id: int,
                           kwargs: dict) -> str:
    comment_id = int(kwargs.get("comment_id") or 0)
    if not comment_id:
        return "[bitbucket:delete_pr_comment:error] comment_id is required"
    current = client.get_pr_comment(project, repo, pr_id, comment_id)
    client.delete_pr_comment(
        project, repo, pr_id, comment_id, version=current.get("version", 0)
    )
    return (
        f"[bitbucket:comment-deleted] {project}/{repo}#{pr_id} "
        f"comment-id={comment_id}"
    )


def _format_task(c: dict) -> str:
    author = ((c.get("author") or {}).get("displayName")
              or (c.get("author") or {}).get("name") or "?")
    first_line = (c.get("text") or "").splitlines()[0][:120] \
        if c.get("text") else ""
    return f"[{c.get('state', 'OPEN')}] id={c.get('id', '?')} ({author}) {first_line}"


def _act_list_pr_tasks(client, project: str, repo: str, pr_id: int,
                       kwargs: dict) -> str:
    state = (kwargs.get("state") or "").strip()
    tasks = client.list_pr_tasks(project, repo, pr_id, state=state)
    label = f" (state={state})" if state else ""
    if not tasks:
        return f"[bitbucket:pr_tasks] {project}/{repo}#{pr_id}: 0 task(s){label}"
    lines = [
        f"[bitbucket:pr_tasks] {project}/{repo}#{pr_id}: "
        f"{len(tasks)} task(s){label}"
    ]
    lines.extend(f"  {_format_task(t)}" for t in tasks)
    return "\n".join(lines)


def _act_create_pr_task(client, project: str, repo: str, pr_id: int,
                        kwargs: dict) -> str:
    text = kwargs.get("body") or kwargs.get("text") or ""
    if not text:
        return "[bitbucket:create_pr_task:error] body is required"
    res = client.create_pr_task(project, repo, pr_id, text=text)
    return (
        f"[bitbucket:task-created] {project}/{repo}#{pr_id} "
        f"task-id={res.get('id', '?')}"
    )


def _act_resolve_pr_task(client, project: str, repo: str, pr_id: int,
                         kwargs: dict) -> str:
    comment_id = int(kwargs.get("comment_id") or 0)
    if not comment_id:
        return (
            "[bitbucket:resolve_pr_task:error] comment_id (the task id from "
            "list_pr_tasks) is required"
        )
    current = client.get_pr_comment(project, repo, pr_id, comment_id)
    client.set_pr_task_state(
        project, repo, pr_id, comment_id,
        version=current.get("version", 0), state="RESOLVED",
    )
    return f"[bitbucket:task-resolved] {project}/{repo}#{pr_id} task-id={comment_id}"


def _act_needs_work_pr(client, project: str, repo: str, pr_id: int) -> str:
    me = client.whoami()
    slug = me.get("slug") or me.get("name") or ""
    if not slug:
        return (
            "[bitbucket:needs_work_pr:error] could not resolve the "
            "authenticated user's slug (whoami returned no identity)"
        )
    client.set_pr_reviewer_status(
        project, repo, pr_id, user_slug=slug, status="NEEDS_WORK"
    )
    return f"[bitbucket:needs-work] {project}/{repo}#{pr_id} marked by {slug}"


def _act_decline_pr(client, project: str, repo: str, pr_id: int) -> str:
    pr = client.get_pull_request(project, repo, pr_id)
    version = pr.get("version", 0)
    client.decline_pr(project, repo, pr_id, version=version)
    return f"[bitbucket:declined] {project}/{repo}#{pr_id}"


def _act_merge_pr(client, project: str, repo: str, pr_id: int, kwargs: dict) -> str:
    pr = client.get_pull_request(project, repo, pr_id)
    version = pr.get("version", 0)
    res = client.merge_pr(
        project, repo, pr_id,
        version=version,
        message=kwargs.get("message", "") or kwargs.get("commit_message", ""),
    )
    return (
        f"[bitbucket:merged] {project}/{repo}#{pr_id} state={res.get('state','?')}\n"
        f"  Merged into: {(res.get('toRef') or {}).get('displayId','?')}"
    )


def _act_list_branches(client, project: str, repo: str, kwargs: dict) -> str:
    branches = client.list_branches(
        project, repo, filter_text=_filter_text(kwargs),
        order=kwargs.get("order") or "",
    )
    if not branches:
        return f"[bitbucket:branches] {project}/{repo}: (none)"
    out = [f"[bitbucket:branches] {project}/{repo} - {len(branches)} branch(es)"]
    for b in branches[:80]:
        out.append(_format_branch(b))
    if len(branches) > 80:
        out.append(f"  ... +{len(branches) - 80} more")
    return "\n".join(out)


def _act_create_branch(client, project: str, repo: str, kwargs: dict) -> str:
    name = kwargs.get("name") or kwargs.get("branch") or ""
    start = kwargs.get("start_point") or kwargs.get("from") or ""
    if not name or not start:
        return "[bitbucket:create_branch:error] name and start_point are required"
    res = client.create_branch(
        project, repo, name=name, start_point=start,
        message=kwargs.get("message", ""),
    )
    return f"[bitbucket:branch-created] {project}/{repo} {res.get('displayId', name)} from {start}"


def _act_delete_branch(client, project: str, repo: str, kwargs: dict) -> str:
    name = kwargs.get("name") or kwargs.get("branch") or ""
    if not name:
        return "[bitbucket:delete_branch:error] name is required"
    client.delete_branch(project, repo, name=name, dry_run=bool(kwargs.get("dry_run", False)))
    return f"[bitbucket:branch-deleted] {project}/{repo} {name}"


def _act_list_commits(client, project: str, repo: str, kwargs: dict) -> str:
    commits = client.list_commits(
        project, repo,
        branch=kwargs.get("branch", ""),
        path=kwargs.get("path", ""),
        limit=int(kwargs.get("limit", 30)),
    )
    if not commits:
        return f"[bitbucket:commits] {project}/{repo}: (none)"
    out = [f"[bitbucket:commits] {project}/{repo} - {len(commits)} commit(s)"]
    for c in commits:
        out.append(_format_commit(c))
    return "\n".join(out)


def _act_list_activity(client, project: str, repo: str, kwargs: dict) -> str:
    acts = client.list_repo_activities(project, repo, limit=int(kwargs.get("limit", 30)))
    if not acts:
        return f"[bitbucket:activity] {project}/{repo}: (none)"
    out = [f"[bitbucket:activity] {project}/{repo} - {len(acts)} event(s)"]
    for a in acts:
        out.append(_format_commit(a))
    return "\n".join(out)


def _act_build_status(client, kwargs: dict) -> str:
    commit = kwargs.get("commit") or kwargs.get("commit_hash") or ""
    if not commit:
        return "[bitbucket:build_status:error] commit hash is required"
    builds = client.get_build_status(commit)
    if not builds:
        return f"[bitbucket:build_status] {commit[:12]}: (none)"
    out = [f"[bitbucket:build_status] {commit[:12]} - {len(builds)} build(s)"]
    for b in builds:
        out.append(_format_build(b))
    return "\n".join(out)


def _act_repo_settings(client, project: str, repo: str) -> str:
    s = client.get_repo_settings(project, repo)
    return f"[bitbucket:repo-settings] {project}/{repo}\n{json.dumps(s, indent=2)}"


def _act_update_repo_settings(client, project: str, repo: str, kwargs: dict) -> str:
    settings = kwargs.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except json.JSONDecodeError as exc:
            return f"[bitbucket:update_repo_settings:error] settings JSON parse failed: {exc}"
    if not isinstance(settings, dict):
        return "[bitbucket:update_repo_settings:error] settings must be an object"
    res = client.update_repo_settings(project, repo, settings=settings)
    return f"[bitbucket:repo-settings-updated] {project}/{repo}\n{json.dumps(res, indent=2)}"


def _act_list_webhooks(client, project: str, repo: str) -> str:
    hooks = client.list_webhooks(project, repo)
    if not hooks:
        return f"[bitbucket:webhooks] {project}/{repo}: (none)"
    out = [f"[bitbucket:webhooks] {project}/{repo} - {len(hooks)} hook(s)"]
    for h in hooks:
        out.append(
            f"  #{h.get('id')} [{'on' if h.get('active') else 'off'}] {h.get('name','?')} -> {h.get('url','?')}"
        )
        evs = h.get("events") or []
        if evs:
            out.append(f"    events: {', '.join(evs)}")
    return "\n".join(out)


def _act_create_webhook(client, project: str, repo: str, kwargs: dict) -> str:
    name = kwargs.get("name") or ""
    url = kwargs.get("url") or ""
    events = kwargs.get("events") or []
    if isinstance(events, str):
        events = [e.strip() for e in events.split(",") if e.strip()]
    if not name or not url or not events:
        return "[bitbucket:create_webhook:error] name, url, and events are required"
    res = client.create_webhook(
        project, repo,
        name=name, url=url, events=events,
        active=bool(kwargs.get("active", True)),
        secret=kwargs.get("secret", ""),
    )
    return f"[bitbucket:webhook-created] {project}/{repo} #{res.get('id','?')} {name} -> {url}"


def _act_delete_webhook(client, project: str, repo: str, kwargs: dict) -> str:
    wh_id = int(kwargs.get("webhook_id") or kwargs.get("id") or 0)
    if not wh_id:
        return "[bitbucket:delete_webhook:error] webhook_id is required"
    client.delete_webhook(project, repo, webhook_id=wh_id)
    return f"[bitbucket:webhook-deleted] {project}/{repo} #{wh_id}"


def _act_list_permissions(client, project: str, repo: str) -> str:
    users = client.list_repo_user_permissions(project, repo)
    groups = client.list_repo_group_permissions(project, repo)
    out = [f"[bitbucket:permissions] {project}/{repo}"]
    out.append(f"  Users ({len(users)}):")
    for u in users[:50]:
        out.append(
            f"    {u.get('user',{}).get('name','?'):24} {u.get('permission','?')}"
        )
    out.append(f"  Groups ({len(groups)}):")
    for g in groups[:50]:
        out.append(f"    {g.get('group',{}).get('name','?'):24} {g.get('permission','?')}")
    return "\n".join(out)


# ── JSON replies (format="json") ──────────────────────────


class _UsageError(Exception):
    """A missing or bad argument found while building a JSON reply."""


def _user_dto(u: dict | None) -> dict:
    u = u or {}
    return {"name": u.get("name") or u.get("slug") or "",
            "display_name": u.get("displayName") or u.get("name") or ""}


def _reviewer_dto(p: dict) -> dict:
    return {**_user_dto(p.get("user")), "approved": bool(p.get("approved")),
            "status": p.get("status", "")}


def _ref_repo(ref: dict) -> str:
    """``PROJ/slug`` of the repository a PR ref points into."""
    repo = (ref or {}).get("repository") or {}
    key = (repo.get("project") or {}).get("key", "")
    slug = repo.get("slug", "")
    return f"{key}/{slug}" if key and slug else ""


def _project_dto(p: dict) -> dict:
    return {"key": p.get("key", ""), "name": p.get("name", ""),
            "description": p.get("description", ""),
            "public": bool(p.get("public")), "url": _href(p.get("links"))}


def _repo_dto(r: dict) -> dict:
    proj = r.get("project") or {}
    return {
        "project": proj.get("key", ""),
        "project_name": proj.get("name", ""),
        "slug": r.get("slug", ""),
        "name": r.get("name", ""),
        "description": r.get("description", ""),
        "state": r.get("state", ""),
        "scm_id": r.get("scmId", ""),
        "public": bool(r.get("public")),
        "forkable": bool(r.get("forkable")),
        "url": _href(r.get("links")),
        "clone": [{"name": c.get("name", ""), "href": c.get("href", "")}
                  for c in (r.get("links") or {}).get("clone") or []
                  if isinstance(c, dict)],
    }


def _pr_dto(pr: dict) -> dict:
    src = pr.get("fromRef") or {}
    dst = pr.get("toRef") or {}
    props = pr.get("properties") or {}
    return {
        "id": pr.get("id"),
        "version": pr.get("version"),
        "title": pr.get("title", ""),
        "description": pr.get("description", ""),
        "state": pr.get("state", ""),
        "draft": bool(pr.get("draft")),
        "author": _user_dto((pr.get("author") or {}).get("user")),
        "reviewers": [_reviewer_dto(r) for r in pr.get("reviewers") or []],
        "source_branch": src.get("displayId", ""),
        "source_commit": src.get("latestCommit", ""),
        "source_repository": _ref_repo(src),
        "destination_branch": dst.get("displayId", ""),
        "destination_commit": dst.get("latestCommit", ""),
        "repository": _ref_repo(dst),
        "created": pr.get("createdDate"),
        "updated": pr.get("updatedDate"),
        "closed": pr.get("closedDate"),
        "url": _href(pr.get("links")),
        "open_tasks": props.get("openTaskCount", pr.get("openTaskCount", 0)),
        "resolved_tasks": props.get("resolvedTaskCount", 0),
        "comment_count": props.get("commentCount", 0),
        "merge_result": (props.get("mergeResult") or {}).get("outcome", ""),
    }


def _commit_dto(c: dict) -> dict:
    keys = (c.get("properties") or {}).get("jira-key") or []
    return {
        "id": c.get("id", ""),
        "display_id": c.get("displayId") or (c.get("id") or "")[:11],
        "message": c.get("message", ""),
        "author": _user_dto(c.get("author")),
        "author_timestamp": c.get("authorTimestamp"),
        "committer": _user_dto(c.get("committer")),
        "committer_timestamp": c.get("committerTimestamp"),
        "parents": [p.get("id", "") for p in c.get("parents") or []
                    if isinstance(p, dict)],
        "jira_keys": [k for k in keys if isinstance(k, str)]
        if isinstance(keys, list) else [],
    }


def _branch_dto(b: dict) -> dict:
    meta = b.get("metadata") or {}
    ab = meta.get(_META_AHEAD_BEHIND)
    last = meta.get(_META_LAST_COMMIT)
    issues = meta.get(_META_JIRA)
    outgoing = meta.get(_META_OUTGOING_PR)
    builds = meta.get(_META_BUILDS)
    pr = outgoing.get("pullRequest") if isinstance(outgoing, dict) else None
    return {
        "name": b.get("displayId", ""),
        "id": b.get("id", ""),
        "latest_commit": b.get("latestCommit", ""),
        "is_default": bool(b.get("isDefault")),
        "ahead": ab.get("ahead") if isinstance(ab, dict) else None,
        "behind": ab.get("behind") if isinstance(ab, dict) else None,
        "last_commit": _commit_dto(last) if isinstance(last, dict) else None,
        "jira_keys": [i["key"] for i in issues
                      if isinstance(i, dict) and i.get("key")]
        if isinstance(issues, list) else [],
        "pull_request": {"id": pr.get("id"), "state": pr.get("state", ""),
                         "title": pr.get("title", "")}
        if isinstance(pr, dict) else None,
        "builds": builds if isinstance(builds, dict) else None,
    }


def _build_dto(b: dict) -> dict:
    return {"state": b.get("state", ""), "key": b.get("key", ""),
            "name": b.get("name", ""), "url": b.get("url", ""),
            "description": b.get("description", ""),
            "date_added": b.get("dateAdded")}


def _comment_dto(c: dict, depth: int = 0) -> dict:
    replies = (c.get("comments") or []) if depth < 5 else []
    return {
        "id": c.get("id"),
        "text": c.get("text", ""),
        "author": _user_dto(c.get("author")),
        "created": c.get("createdDate"),
        "updated": c.get("updatedDate"),
        "severity": c.get("severity", ""),
        "state": c.get("state", ""),
        "replies": [_comment_dto(r, depth + 1) for r in replies
                    if isinstance(r, dict)],
    }


def _activity_dto(a: dict) -> dict:
    out = {"id": a.get("id"), "action": a.get("action", ""),
           "created": a.get("createdDate"), "user": _user_dto(a.get("user"))}
    if isinstance(a.get("comment"), dict):
        out["comment_action"] = a.get("commentAction", "")
        out["comment"] = _comment_dto(a["comment"])
        anchor = a.get("commentAnchor") or {}
        if anchor:
            out["anchor"] = {"path": anchor.get("path", ""),
                             "line": anchor.get("line"),
                             "line_type": anchor.get("lineType", "")}
    return out


def _task_dto(t: dict) -> dict:
    return {"id": t.get("id"), "text": t.get("text", ""),
            "state": t.get("state", ""), "severity": t.get("severity", ""),
            "author": _user_dto(t.get("author")),
            "created": t.get("createdDate")}


def _json_read(client, action: str, project: str, repo: str, pr_id: int,
               kwargs: dict) -> dict | None:
    """The structured data behind a read action, or None for an action
    with no structured form (writes, admin), which the caller wraps as text."""
    limit = int(kwargs.get("limit") or 0)
    at = kwargs.get("at") or ""
    if action == "whoami":
        u = client.whoami()
        return {"name": u.get("name", ""), "slug": u.get("slug", ""),
                "display_name": u.get("displayName", ""),
                "email": u.get("emailAddress", ""),
                "active": u.get("active")}
    if action == "list_projects":
        name = kwargs.get("name", "") or kwargs.get("query", "")
        return {"projects": [_project_dto(p) for p in client.list_projects(name=name)]}
    if action == "list_repos":
        return {"project": project,
                "repos": [_repo_dto(r) for r in client.list_repos(project)]}
    if action == "get_repo":
        return {**_repo_dto(client.get_repo(project, repo)),
                "default_branch": _default_branch(client, project, repo)}
    if action == "readme":
        return {"project": project, "repo": repo,
                **client.readme(project, repo, at=at)}
    if action == "get_file":
        path = (kwargs.get("path") or "").strip()
        if not path:
            raise _UsageError("path is required for get_file")
        return {"project": project, "repo": repo,
                **client.get_file(project, repo, path, at=at)}
    if action == "list_prs":
        state = (kwargs.get("state") or "OPEN").upper()
        prs = client.list_pull_requests(
            project, repo, state=state,
            author=kwargs.get("author", ""),
            reviewer=kwargs.get("reviewer", ""),
            limit=limit or 50,
        )
        return {"project": project, "repo": repo, "state": state,
                "pull_requests": [_pr_dto(pr) for pr in prs]}
    if action == "get_pr":
        return _pr_dto(client.get_pull_request(project, repo, pr_id))
    if action == "get_pr_diff":
        return {"id": pr_id, "diff": client.get_pr_diff(
            project, repo, pr_id,
            context_lines=int(kwargs.get("context_lines", 3)))}
    if action == "get_pr_activities":
        acts = client.get_pr_activities(project, repo, pr_id, limit=limit or 50)
        # Bitbucket lists newest first; oldest first lets trimming keep the newest.
        return {"id": pr_id, "activities": [_activity_dto(a) for a in reversed(acts)]}
    if action == "get_pr_commits":
        return {"id": pr_id, "commits": [
            _commit_dto(c) for c in client.get_pr_commits(project, repo, pr_id)]}
    if action == "pr_issues":
        return {"id": pr_id, "issues": [
            {"key": i.get("key", ""), "url": i.get("url", "")}
            for i in client.pr_issues(project, repo, pr_id)]}
    if action == "list_pr_tasks":
        state = (kwargs.get("state") or "").strip()
        return {"id": pr_id, "tasks": [
            _task_dto(t) for t in client.list_pr_tasks(project, repo, pr_id, state=state)]}
    if action == "list_branches":
        branches = client.list_branches(
            project, repo, filter_text=_filter_text(kwargs),
            order=kwargs.get("order") or "",
        )
        if limit:
            branches = branches[:limit]
        return {"project": project, "repo": repo,
                "branches": [_branch_dto(b) for b in branches]}
    if action == "list_commits":
        commits = client.list_commits(
            project, repo, branch=kwargs.get("branch", ""),
            path=kwargs.get("path", ""), limit=limit or 30,
        )
        return {"project": project, "repo": repo,
                "commits": [_commit_dto(c) for c in commits]}
    if action == "build_status":
        commit = kwargs.get("commit") or kwargs.get("commit_hash") or ""
        if not commit:
            raise _UsageError("commit hash is required for build_status")
        return {"commit": commit,
                "builds": [_build_dto(b) for b in client.get_build_status(commit)]}
    return None


def _status_data(client, err: str, svc) -> dict:
    project_path = getattr(svc, "project_path", ".") or "."
    cfg = load_bitbucket_config(project_path)
    active = cfg.get("active") or {}
    data: dict[str, Any] = {
        "base_url": active.get("base_url", ""),
        "username": active.get("username", ""),
        "default_project": cfg.get("default_project") or "",
        "default_repo": cfg.get("default_repo") or "",
        "accounts": [{"username": a.get("username", ""),
                      "base_url": a.get("base_url", "")}
                     for a in cfg.get("accounts") or [] if isinstance(a, dict)],
        "connected": False,
        "formats": ["text", "json"],
    }
    if client is None:
        data["error"] = err or "[bitbucket:no-account]"
        return data
    try:
        props = client.application_properties()
        data.update(connected=True, version=props.get("version", ""))
    except BitbucketError as exc:
        data["error"] = str(exc)
    return data


def _dispatch_text(client, action: str, project: str, repo: str, pr_id: int,
                   kwargs: dict) -> str:
    """Run *action* and return its text reply."""
    if action == "whoami":
        return _act_whoami(client)
    if action == "list_projects":
        return _act_list_projects(client, kwargs)
    if action == "list_repos":
        return _act_list_repos(client, project)
    if action == "get_repo":
        return _act_get_repo(client, project, repo)
    if action == "readme":
        return _act_readme(client, project, repo, kwargs)
    if action == "get_file":
        return _act_get_file(client, project, repo, kwargs)
    if action == "list_prs":
        return _act_list_prs(client, project, repo, kwargs)
    if action == "get_pr":
        return _act_get_pr(client, project, repo, pr_id)
    if action == "get_pr_diff":
        return _act_get_pr_diff(client, project, repo, pr_id, kwargs)
    if action == "get_pr_activities":
        return _act_get_pr_activities(client, project, repo, pr_id)
    if action == "get_pr_commits":
        return _act_get_pr_commits(client, project, repo, pr_id)
    if action == "pr_issues":
        return _act_pr_issues(client, project, repo, pr_id)
    if action == "create_pr":
        return _act_create_pr(client, project, repo, kwargs)
    if action == "update_pr":
        return _act_update_pr(client, project, repo, pr_id, kwargs)
    if action == "comment_pr":
        return _act_comment_pr(client, project, repo, pr_id, kwargs)
    if action == "update_pr_comment":
        return _act_update_pr_comment(client, project, repo, pr_id, kwargs)
    if action == "delete_pr_comment":
        return _act_delete_pr_comment(client, project, repo, pr_id, kwargs)
    if action == "list_pr_tasks":
        return _act_list_pr_tasks(client, project, repo, pr_id, kwargs)
    if action == "create_pr_task":
        return _act_create_pr_task(client, project, repo, pr_id, kwargs)
    if action == "resolve_pr_task":
        return _act_resolve_pr_task(client, project, repo, pr_id, kwargs)
    if action == "approve_pr":
        return _act_approve_pr(client, project, repo, pr_id)
    if action == "unapprove_pr":
        return _act_unapprove_pr(client, project, repo, pr_id)
    if action == "needs_work_pr":
        return _act_needs_work_pr(client, project, repo, pr_id)
    if action == "decline_pr":
        return _act_decline_pr(client, project, repo, pr_id)
    if action == "merge_pr":
        return _act_merge_pr(client, project, repo, pr_id, kwargs)
    if action == "list_branches":
        return _act_list_branches(client, project, repo, kwargs)
    if action == "create_branch":
        return _act_create_branch(client, project, repo, kwargs)
    if action == "delete_branch":
        return _act_delete_branch(client, project, repo, kwargs)
    if action == "list_commits":
        return _act_list_commits(client, project, repo, kwargs)
    if action == "list_activity":
        return _act_list_activity(client, project, repo, kwargs)
    if action == "build_status":
        return _act_build_status(client, kwargs)
    if action == "repo_settings":
        return _act_repo_settings(client, project, repo)
    if action == "update_repo_settings":
        return _act_update_repo_settings(client, project, repo, kwargs)
    if action == "list_webhooks":
        return _act_list_webhooks(client, project, repo)
    if action == "create_webhook":
        return _act_create_webhook(client, project, repo, kwargs)
    if action == "delete_webhook":
        return _act_delete_webhook(client, project, repo, kwargs)
    # list_permissions — the action set is closed by the _VALID_ACTIONS gate
    return _act_list_permissions(client, project, repo)


# ── Ledger logging for mutations ──────────────────────────


def _log_mutation(svc, action: str, project: str, repo: str, kwargs: dict, response: str) -> None:
    """Append PR-merge / branch-delete events to the ledger so the audit trail
    reflects platform-side state changes too. Never raises."""
    if action not in _MUTATING_ACTIONS:
        return
    if ":error]" in response.split("\n", 1)[0]:
        return  # validation failed — nothing actually mutated (matches jira)
    if not getattr(svc, "edit_ledger", None):
        return
    detail: dict[str, Any] = {
        "kind": "bitbucket",
        "action": action,
        "project": project,
        "repo": repo,
        "kwargs": {k: v for k, v in kwargs.items() if k not in {"token", "secret"}},
    }
    rel = f"bitbucket://{project}/{repo}"
    try:
        svc.edit_ledger.log_edit(
            file=rel,
            change_type=action,
            summary=response.splitlines()[0][:200] if response else f"bitbucket {action}",
            tags=["bitbucket", action],
            detail=detail,
        )
        if getattr(svc, "activity_log", None):
            svc.activity_log.log("bitbucket_action", {
                "action": action,
                "project": project,
                "repo": repo,
            })
    except Exception:
        pass


# ── Main entrypoint ───────────────────────────────────────


def handle_bitbucket(action: str, svc, finalize, **kwargs) -> str:
    """Dispatch a Bitbucket Data Center action.

    ``svc`` provides ``project_path``, ``edit_ledger``, ``activity_log``,
    ``session_mgr``. ``finalize(tool, args, response, summary)`` logs the call.
    ``format="json"`` returns ``{"ok", "action", "data"}`` JSON instead of
    the capped text reply (see cli/tools/_json_reply.py).
    """
    action = (action or "").strip().lower()
    args_for_log = {k: v for k, v in kwargs.items() if k not in {"token", "secret"}}
    args_for_log["action"] = action
    want_json = jr.wants_json(kwargs)

    def fail(msg: str, summary: str, *, status: int = 0) -> str:
        if want_json:
            return finalize(_TOOL, args_for_log,
                            jr.error(action, msg, status=status, code=summary),
                            summary, ok=False)
        return finalize(_TOOL, args_for_log, msg, summary)

    if not action:
        return fail("[bitbucket:error] action is required", "error")

    client, err = _build_client(svc)

    # `status` is special — it can render the config without a working client.
    if action == "status":
        if want_json:
            data = _status_data(client, err, svc)
            return finalize(_TOOL, args_for_log, jr.ok("status", data), "status")
        resp = _act_status(client, err, svc)
        return finalize(_TOOL, args_for_log, _cap(resp), "status")

    if client is None:
        return fail(err, "no-account")

    if action not in _VALID_ACTIONS:
        return fail(
            f"[bitbucket:unknown-action] '{action}'. Valid: {', '.join(_VALID_ACTIONS)}",
            "unknown-action",
        )

    # Resolve project/repo for repo-scoped actions.
    project = repo = ""
    if action in _REPO_SCOPED:
        project, repo, scope_err = _resolve_repo(action, kwargs, svc)
        if scope_err:
            return fail(scope_err, "missing-arg")

    pr_id = int(kwargs.get("pr_id") or kwargs.get("id") or 0)
    if action in _PR_ACTIONS and not pr_id:
        return fail("[bitbucket:error] pr_id required", "missing-arg")

    data = None
    try:
        if want_json:
            data = _json_read(client, action, project, repo, pr_id, kwargs)
        if data is None:
            resp = _dispatch_text(client, action, project, repo, pr_id, kwargs)
    except _UsageError as exc:
        return fail(f"[bitbucket:error] {exc}", "missing-arg")
    except BitbucketError as exc:
        return fail(f"[bitbucket:api-error] {exc}", f"http-{exc.status}",
                    status=exc.status)
    except Exception as exc:  # pragma: no cover — defensive
        return fail(f"[bitbucket:internal-error] {type(exc).__name__}: {exc}", "error")

    if data is not None:
        return finalize(_TOOL, args_for_log, jr.ok(action, data), action)
    _log_mutation(svc, action, project, repo, kwargs, resp)
    if want_json:
        if ":error]" in resp.split("\n", 1)[0]:
            return fail(resp, "error")
        return finalize(_TOOL, args_for_log, jr.text(action, resp), action)
    return finalize(_TOOL, args_for_log, _cap(resp), action)
