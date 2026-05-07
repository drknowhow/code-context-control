"""c3_bitbucket — Bitbucket Data Center / Server integration.

Action-dispatch tool. Resolves credentials from the OS keyring (via
``services.bitbucket_credentials``) and the active account / default
project+repo from ``.c3/config.json``.

Long responses are capped at ``_RESPONSE_TOKEN_CAP`` to protect the
caller's context budget.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import count_tokens
from core.config import load_bitbucket_config
from services import bitbucket_credentials as bb_creds
from services.bitbucket_client import BitbucketDataCenterClient, BitbucketError

_TOOL = "c3_bitbucket"
_RESPONSE_TOKEN_CAP = 2400
_DIFF_PREVIEW_CHARS = 6000  # diffs above this are truncated

# Actions that change repo state — flagged for ledger logging.
_MUTATING_ACTIONS = {
    "create_pr", "comment_pr", "approve_pr", "unapprove_pr",
    "decline_pr", "merge_pr",
    "create_branch", "delete_branch",
    "update_repo_settings", "create_webhook", "delete_webhook",
}

# Actions that can fall back to default_project/default_repo from config.
_REPO_SCOPED = {
    "list_repos", "get_repo", "list_prs", "get_pr", "get_pr_diff",
    "get_pr_activities", "create_pr", "comment_pr", "approve_pr",
    "unapprove_pr", "decline_pr", "merge_pr", "list_branches",
    "create_branch", "delete_branch", "list_commits", "list_activity",
    "repo_settings", "update_repo_settings", "list_webhooks",
    "create_webhook", "delete_webhook", "list_permissions",
}


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
    return "\n".join(lines[:20]) + "\n[truncated]"


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


# ── Formatters ────────────────────────────────────────────


def _format_pr(pr: dict) -> str:
    state = pr.get("state", "?")
    pr_id = pr.get("id", "?")
    title = pr.get("title", "")
    author = (pr.get("author") or {}).get("user", {}).get("name", "?")
    src = (pr.get("fromRef") or {}).get("displayId", "?")
    dst = (pr.get("toRef") or {}).get("displayId", "?")
    return f"  #{pr_id} [{state:7}] {src} → {dst}  by {author}\n         {title}"


def _format_pr_full(pr: dict) -> str:
    lines = [
        f"PR #{pr.get('id')} [{pr.get('state')}] — {pr.get('title','')}",
        f"  {(pr.get('fromRef') or {}).get('displayId')} → {(pr.get('toRef') or {}).get('displayId')}",
        f"  Author: {(pr.get('author') or {}).get('user',{}).get('displayName','?')}",
        f"  Version: {pr.get('version')} | Open tasks: {pr.get('openTaskCount', 0)}",
    ]
    reviewers = pr.get("reviewers") or []
    if reviewers:
        rs = ", ".join(
            f"{r.get('user',{}).get('name','?')}({'✓' if r.get('approved') else '·'})"
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
    behind = b.get("metadata", {}).get(
        "com.atlassian.bitbucket.server.bitbucket-branch:ahead-behind-metadata-provider"
    )
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
        out.append(f"  Server : FAIL — {exc}")
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
        lines.append(f"  … +{len(projects) - 50} more")
    return "\n".join(lines)


def _act_list_repos(client: BitbucketDataCenterClient, project: str) -> str:
    repos = client.list_repos(project)
    if not repos:
        return f"[bitbucket:repos] {project}: (none)"
    lines = [f"[bitbucket:repos] {project} — {len(repos)} repo(s)"]
    for r in repos[:80]:
        lines.append(f"  {r.get('slug','?'):30} {r.get('name','?')}")
    if len(repos) > 80:
        lines.append(f"  … +{len(repos) - 80} more")
    return "\n".join(lines)


def _act_get_repo(client: BitbucketDataCenterClient, project: str, repo: str) -> str:
    r = client.get_repo(project, repo)
    return (
        f"[bitbucket:repo] {project}/{repo}\n"
        f"  name:    {r.get('name','?')}\n"
        f"  scmId:   {r.get('scmId','?')}\n"
        f"  public:  {r.get('public','?')}\n"
        f"  state:   {r.get('state','?')}\n"
        f"  links:   {[l.get('href') for l in (r.get('links',{}).get('clone') or [])]}"
    )


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
    out = [f"[bitbucket:prs] {project}/{repo} state={state} — {len(prs)} PR(s)"]
    for pr in prs:
        out.append(_format_pr(pr))
    return "\n".join(out)


def _act_get_pr(client, project: str, repo: str, pr_id: int) -> str:
    pr = client.get_pull_request(project, repo, pr_id)
    return f"[bitbucket:pr]\n{_format_pr_full(pr)}"


def _act_get_pr_diff(client, project: str, repo: str, pr_id: int, kwargs: dict) -> str:
    diff = client.get_pr_diff(project, repo, pr_id, context_lines=int(kwargs.get("context_lines", 3)))
    if len(diff) > _DIFF_PREVIEW_CHARS:
        diff = diff[:_DIFF_PREVIEW_CHARS] + "\n… [diff truncated]"
    return f"[bitbucket:diff] {project}/{repo}#{pr_id}\n{diff}"


def _act_get_pr_activities(client, project: str, repo: str, pr_id: int) -> str:
    acts = client.get_pr_activities(project, repo, pr_id)
    if not acts:
        return f"[bitbucket:pr-activity] {project}/{repo}#{pr_id}: (none)"
    out = [f"[bitbucket:pr-activity] {project}/{repo}#{pr_id} — {len(acts)} event(s)"]
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
    branches = client.list_branches(project, repo, filter_text=kwargs.get("filter", ""))
    if not branches:
        return f"[bitbucket:branches] {project}/{repo}: (none)"
    out = [f"[bitbucket:branches] {project}/{repo} — {len(branches)} branch(es)"]
    for b in branches[:80]:
        out.append(_format_branch(b))
    if len(branches) > 80:
        out.append(f"  … +{len(branches) - 80} more")
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
    out = [f"[bitbucket:commits] {project}/{repo} — {len(commits)} commit(s)"]
    for c in commits:
        out.append(_format_commit(c))
    return "\n".join(out)


def _act_list_activity(client, project: str, repo: str, kwargs: dict) -> str:
    acts = client.list_repo_activities(project, repo, limit=int(kwargs.get("limit", 30)))
    if not acts:
        return f"[bitbucket:activity] {project}/{repo}: (none)"
    out = [f"[bitbucket:activity] {project}/{repo} — {len(acts)} event(s)"]
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
    out = [f"[bitbucket:build_status] {commit[:12]} — {len(builds)} build(s)"]
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
    out = [f"[bitbucket:webhooks] {project}/{repo} — {len(hooks)} hook(s)"]
    for h in hooks:
        out.append(
            f"  #{h.get('id')} [{'on' if h.get('active') else 'off'}] {h.get('name','?')} → {h.get('url','?')}"
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
    return f"[bitbucket:webhook-created] {project}/{repo} #{res.get('id','?')} {name} → {url}"


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


# ── Ledger logging for mutations ──────────────────────────


def _log_mutation(svc, action: str, project: str, repo: str, kwargs: dict, response: str) -> None:
    """Append PR-merge / branch-delete events to the ledger so the audit trail
    reflects platform-side state changes too. Never raises."""
    if action not in _MUTATING_ACTIONS:
        return
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
    """
    action = (action or "").strip().lower()
    args_for_log = {k: v for k, v in kwargs.items() if k not in {"token", "secret"}}
    args_for_log["action"] = action

    if not action:
        return finalize(_TOOL, args_for_log,
                        "[bitbucket:error] action is required", "error")

    client, err = _build_client(svc)

    # `status` is special — it can render the config without a working client.
    if action == "status":
        resp = _act_status(client, err, svc)
        return finalize(_TOOL, args_for_log, _cap(resp), "status")

    if client is None:
        return finalize(_TOOL, args_for_log, err, "no-account")

    # Resolve project/repo for repo-scoped actions.
    project = repo = ""
    if action in _REPO_SCOPED:
        project, repo, scope_err = _resolve_repo(action, kwargs, svc)
        if scope_err:
            return finalize(_TOOL, args_for_log, scope_err, "missing-arg")

    pr_id = int(kwargs.get("pr_id") or kwargs.get("id") or 0)

    try:
        if action == "whoami":
            resp = _act_whoami(client)
        elif action == "list_projects":
            resp = _act_list_projects(client, kwargs)
        elif action == "list_repos":
            resp = _act_list_repos(client, project)
        elif action == "get_repo":
            resp = _act_get_repo(client, project, repo)
        elif action == "list_prs":
            resp = _act_list_prs(client, project, repo, kwargs)
        elif action == "get_pr":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_get_pr(client, project, repo, pr_id)
        elif action == "get_pr_diff":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_get_pr_diff(client, project, repo, pr_id, kwargs)
        elif action == "get_pr_activities":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_get_pr_activities(client, project, repo, pr_id)
        elif action == "create_pr":
            resp = _act_create_pr(client, project, repo, kwargs)
        elif action == "comment_pr":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_comment_pr(client, project, repo, pr_id, kwargs)
        elif action == "approve_pr":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_approve_pr(client, project, repo, pr_id)
        elif action == "unapprove_pr":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_unapprove_pr(client, project, repo, pr_id)
        elif action == "decline_pr":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_decline_pr(client, project, repo, pr_id)
        elif action == "merge_pr":
            if not pr_id:
                return finalize(_TOOL, args_for_log, "[bitbucket:error] pr_id required", "missing-arg")
            resp = _act_merge_pr(client, project, repo, pr_id, kwargs)
        elif action == "list_branches":
            resp = _act_list_branches(client, project, repo, kwargs)
        elif action == "create_branch":
            resp = _act_create_branch(client, project, repo, kwargs)
        elif action == "delete_branch":
            resp = _act_delete_branch(client, project, repo, kwargs)
        elif action == "list_commits":
            resp = _act_list_commits(client, project, repo, kwargs)
        elif action == "list_activity":
            resp = _act_list_activity(client, project, repo, kwargs)
        elif action == "build_status":
            resp = _act_build_status(client, kwargs)
        elif action == "repo_settings":
            resp = _act_repo_settings(client, project, repo)
        elif action == "update_repo_settings":
            resp = _act_update_repo_settings(client, project, repo, kwargs)
        elif action == "list_webhooks":
            resp = _act_list_webhooks(client, project, repo)
        elif action == "create_webhook":
            resp = _act_create_webhook(client, project, repo, kwargs)
        elif action == "delete_webhook":
            resp = _act_delete_webhook(client, project, repo, kwargs)
        elif action == "list_permissions":
            resp = _act_list_permissions(client, project, repo)
        else:
            valid = sorted([
                "status", "whoami",
                "list_projects", "list_repos", "get_repo",
                "list_prs", "get_pr", "get_pr_diff", "get_pr_activities",
                "create_pr", "comment_pr", "approve_pr", "unapprove_pr",
                "decline_pr", "merge_pr",
                "list_branches", "create_branch", "delete_branch",
                "list_commits", "list_activity", "build_status",
                "repo_settings", "update_repo_settings",
                "list_webhooks", "create_webhook", "delete_webhook",
                "list_permissions",
            ])
            return finalize(
                _TOOL, args_for_log,
                f"[bitbucket:unknown-action] '{action}'. Valid: {', '.join(valid)}",
                "unknown-action",
            )
    except BitbucketError as exc:
        return finalize(
            _TOOL, args_for_log,
            f"[bitbucket:api-error] {exc}", f"http-{exc.status}",
        )
    except Exception as exc:  # pragma: no cover — defensive
        return finalize(
            _TOOL, args_for_log,
            f"[bitbucket:internal-error] {type(exc).__name__}: {exc}", "error",
        )

    _log_mutation(svc, action, project, repo, kwargs, resp)
    return finalize(_TOOL, args_for_log, _cap(resp), action)
