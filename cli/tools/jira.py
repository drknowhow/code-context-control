"""c3_jira tool — dispatch Jira actions through the JiraClient facade.

Mirrors cli/tools/bitbucket.py: account/token resolution from config +
OS keyring, mutating actions logged to the edit ledger, responses capped.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import count_tokens
from core.config import load_jira_config
from services import jira_credentials as jr_creds
from services.jira_client import JiraClient, JiraError

_TOOL = "c3_jira"
_RESPONSE_TOKEN_CAP = 2400

# Actions that change Jira state — flagged for ledger logging.
_MUTATING_ACTIONS = {
    "create_issue", "update_issue", "comment", "transition", "assign",
    "link_issues", "unlink_issues", "delete_issue",
    "move_to_sprint", "move_to_backlog", "add_worklog", "attach_file",
}

_VALID_ACTIONS = sorted([
    "status", "whoami", "search", "get_issue", "my_issues",
    "list_projects", "list_transitions", "get_create_metadata",
    "search_users", "list_link_types",
    "list_boards", "list_sprints", "list_worklogs",
    "create_issue", "update_issue", "comment", "transition", "assign",
    "link_issues", "unlink_issues", "delete_issue",
    "move_to_sprint", "move_to_backlog", "add_worklog", "attach_file",
])

# Attachment uploads are read fully into memory before the multipart POST;
# Jira's own default cap is 10MB — this local guard just prevents runaway
# reads before the server would reject the file anyway.
_ATTACH_MAX_BYTES = 20 * 1024 * 1024

# Required-field ids Jira satisfies from the create payload / auth context.
_AUTO_FIELD_IDS = {"project", "issuetype", "summary", "reporter"}


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
    head = "\n".join(lines[:20])
    if count_tokens(head) > _RESPONSE_TOKEN_CAP:
        head = head[:_RESPONSE_TOKEN_CAP * 4]
    return head + "\n[truncated]"


def _jql_quote(value: str) -> str:
    """Quote a literal for helper-built JQL. Raw `search` JQL is passed
    through untouched; helpers never string-concatenate unquoted input."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_client(svc, account: str = "") -> tuple[JiraClient | None, dict, str]:
    """Resolve account + token. Returns (client_or_None, account_entry, error)."""
    project_path = getattr(svc, "project_path", ".") or "."
    cfg = load_jira_config(project_path)
    name = (account or "").strip() or cfg.get("default_account", "")
    entry = (cfg.get("accounts") or {}).get(name)
    if not isinstance(entry, dict) or not entry.get("base_url"):
        return None, {}, (
            "[jira:no-account] No Jira account configured.\n"
            "Run: c3 jira login --url <JIRA_URL> --deployment cloud|data_center"
        )
    try:
        token = jr_creds.load_token(entry["base_url"], entry.get("username", ""))
    except RuntimeError as exc:
        return None, {}, f"[jira:keyring-error] {exc}"
    if not token:
        return None, {}, (
            f"[jira:no-token] No keyring entry for "
            f"{entry.get('username')}@{entry['base_url']}.\n"
            f"Run: c3 jira login --url {entry['base_url']}"
        )
    try:
        client = JiraClient(
            entry["base_url"],
            entry.get("username", ""),
            token,
            deployment=entry.get("deployment", "cloud"),
            verify_tls=bool(entry.get("verify_tls", True)),
            ca_bundle=entry.get("ca_bundle", ""),
        )
    except ValueError as exc:
        return None, {}, f"[jira:bad-config] {exc}"
    entry = dict(entry)
    entry["name"] = name
    return client, entry, ""


# ── Formatters ────────────────────────────────────────────


def _format_issue(i: dict) -> str:
    assignee = i.get("assignee") or "unassigned"
    return f"{i.get('key')} [{i.get('status')}] {i.get('summary')} — {assignee}"


def _format_issue_full(i: dict) -> str:
    lines = [
        f"{i.get('key')}: {i.get('summary')}",
        f"  type={i.get('issue_type')} status={i.get('status')} "
        f"({i.get('status_category')}) priority={i.get('priority')}",
        f"  project={i.get('project')} assignee={i.get('assignee') or 'unassigned'} "
        f"reporter={i.get('reporter')}",
        f"  created={i.get('created')} updated={i.get('updated')}",
    ]
    if i.get("labels"):
        lines.append(f"  labels: {', '.join(i['labels'])}")
    if i.get("parent"):
        lines.append(f"  parent: {i['parent']}")
    for link in i.get("links", []):
        status = f" [{link['status']}]" if link.get("status") else ""
        lines.append(
            f"  link: {link.get('description')} {link.get('issue')}{status} "
            f"(id={link.get('id')})"
        )
    for a in i.get("attachments", []):
        lines.append(
            f"  attachment: {a.get('filename')} ({a.get('size')} bytes, "
            f"{a.get('author')}) id={a.get('id')}"
        )
    if i.get("description"):
        lines.append("  description:")
        lines.extend(f"    {ln}" for ln in i["description"].splitlines())
    for c in i.get("comments", []):
        lines.append(f"  comment [{c.get('author')}] {c.get('created')}:")
        lines.extend(f"    {ln}" for ln in (c.get("body") or "").splitlines())
    return "\n".join(lines)


def _format_search(result: dict, label: str) -> str:
    issues = result.get("issues", [])
    if not issues:
        return f"[jira:{label}] 0 issues"
    lines = [f"[jira:{label}] {len(issues)} issue(s)"]
    lines.extend(f"  {_format_issue(i)}" for i in issues)
    if result.get("next_cursor"):
        lines.append(f"  next: pass cursor={result['next_cursor']}")
    return "\n".join(lines)


# ── Actions ───────────────────────────────────────────────


def _act_status(client: JiraClient | None, err: str, entry: dict, svc) -> str:
    project_path = getattr(svc, "project_path", ".") or "."
    accounts = load_jira_config(project_path).get("accounts") or {}
    lines = [f"[jira:status] {len(accounts)} account(s) configured"]
    for name, a in accounts.items():
        if isinstance(a, dict):
            lines.append(
                f"  {name}: {a.get('username')}@{a.get('base_url')} "
                f"({a.get('deployment')})"
            )
    if client is None:
        lines.append(err or "[jira:no-account]")
        return "\n".join(lines)
    try:
        info = client.server_info()
        me = client.myself()
        lines.append(
            f"  connected: Jira {info.get('version', '?')} "
            f"[{entry.get('deployment')}] as {me.get('displayName', '?')}"
        )
        if entry.get("default_project"):
            lines.append(f"  default_project: {entry['default_project']}")
    except JiraError as exc:
        lines.append(f"  connection FAILED: {exc}")
    return "\n".join(lines)


def _act_whoami(client: JiraClient) -> str:
    me = client.myself()
    user_id = me.get("accountId") or me.get("name") or ""
    return (
        f"[jira:whoami] {me.get('displayName', '?')} ({user_id}) "
        f"{me.get('emailAddress', '')}"
    ).rstrip()


def _act_search(client: JiraClient, kwargs: dict) -> str:
    jql = (kwargs.get("jql") or "").strip()
    if not jql:
        return "[jira:error] jql is required for search"
    result = client.search(
        jql,
        fields=kwargs.get("fields_list") or "",
        limit=int(kwargs.get("limit") or 25),
        cursor=kwargs.get("cursor") or "",
    )
    return _format_search(result, "search")


def _act_my_issues(client: JiraClient, entry: dict, kwargs: dict) -> str:
    clauses = ["assignee = currentUser()"]
    project = (kwargs.get("project") or "").strip() or entry.get("default_project", "")
    if project:
        clauses.append(f"project = {_jql_quote(project)}")
    category = (kwargs.get("status_category") or "").strip()
    if category:
        clauses.append(f"statusCategory = {_jql_quote(category)}")
    jql = " AND ".join(clauses) + " ORDER BY updated DESC"
    result = client.search(
        jql, limit=int(kwargs.get("limit") or 25), cursor=kwargs.get("cursor") or ""
    )
    return _format_search(result, "my_issues")


def _act_list_projects(client: JiraClient, kwargs: dict) -> str:
    projects = client.list_projects(
        query=kwargs.get("query") or "", limit=int(kwargs.get("limit") or 50)
    )
    if not projects:
        return "[jira:list_projects] 0 projects"
    lines = [f"[jira:list_projects] {len(projects)} project(s)"]
    lines.extend(f"  {p['key']}: {p['name']}" for p in projects)
    return "\n".join(lines)


def _act_list_transitions(client: JiraClient, issue: str) -> str:
    transitions = client.list_transitions(issue)
    if not transitions:
        return f"[jira:list_transitions] no transitions available for {issue}"
    lines = [f"[jira:list_transitions] {issue}:"]
    lines.extend(
        f"  {t['id']}: {t['name']} -> {t['to_status']}" for t in transitions
    )
    return "\n".join(lines)


# Jira's createmeta reports the field CONFIGURATION for a project + issue
# type, not the create screen — it can list fields (Epic Link is the classic
# case) that POST /issue rejects with "cannot be set. It is not on the
# appropriate screen". The caveat travels with every metadata response so an
# agent doesn't read "optional" as "settable on create".
_CREATEMETA_CAVEAT = (
    "Note: fields come from Jira's createmeta (the issue type's field "
    "configuration); the create screen may accept fewer. If create_issue "
    "rejects one with 'It is not on the appropriate screen', create without "
    "it, then set it via update_issue — the edit screen is configured "
    "separately. Pass field ids (not names) in the fields JSON."
)


def _act_create_metadata(client: JiraClient, entry: dict, kwargs: dict) -> str:
    project = (kwargs.get("project") or "").strip() or entry.get("default_project", "")
    issue_type = (kwargs.get("issue_type") or "").strip()
    if not project or not issue_type:
        return "[jira:error] project and issue_type are required for get_create_metadata"
    meta = client.get_create_metadata(project, issue_type)
    resp = f"[jira:create_metadata] {json.dumps(meta, indent=1)}"
    if not meta.get("error"):
        resp += f"\n{_CREATEMETA_CAVEAT}"
    return resp


def _act_search_users(client: JiraClient, kwargs: dict) -> str:
    query = (kwargs.get("query") or "").strip()
    if not query:
        return "[jira:error] query is required for search_users"
    users = client.search_users(query, limit=int(kwargs.get("limit") or 10))
    if not users:
        return f"[jira:search_users] no users match {query!r}"
    lines = [f"[jira:search_users] {len(users)} user(s)"]
    lines.extend(
        f"  {u['display_name']} id={u['id']} {u.get('email', '')}".rstrip()
        for u in users
    )
    return "\n".join(lines)


def _parse_extra_fields(kwargs: dict) -> tuple[dict | None, str]:
    raw = (kwargs.get("fields") or "").strip()
    if not raw:
        return None, ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"[jira:error] fields must be a JSON object: {exc}"
    if not isinstance(parsed, dict):
        return None, "[jira:error] fields must be a JSON object"
    return parsed, ""


def _act_create_issue(client: JiraClient, entry: dict, kwargs: dict) -> str:
    project = (kwargs.get("project") or "").strip() or entry.get("default_project", "")
    issue_type = (kwargs.get("issue_type") or "").strip()
    summary = (kwargs.get("summary") or "").strip()
    if not project or not issue_type or not summary:
        return "[jira:error] project, issue_type, and summary are required for create_issue"
    extra, err = _parse_extra_fields(kwargs)
    if err:
        return err
    description = kwargs.get("description") or ""

    # Best-effort createmeta validation: return machine-readable missing
    # required fields instead of guessing defaults. Skipped when the meta
    # endpoint itself fails — Jira's own 400 is authoritative then.
    try:
        meta = client.get_create_metadata(project, issue_type)
    except JiraError:
        meta = {"error": "createmeta unavailable"}
    if not meta.get("error"):
        provided = set(extra or {})
        if description:
            provided.add("description")
        missing = [
            f for f in meta.get("required_fields", [])
            if f.get("id") not in _AUTO_FIELD_IDS and f.get("id") not in provided
        ]
        if missing:
            return (
                f"[jira:missing-fields] {project}/{issue_type} requires fields "
                f"not provided — pass them via fields JSON:\n{json.dumps(missing, indent=1)}"
            )

    created = client.create_issue(
        project, issue_type, summary, description=description, fields=extra,
        parent=(kwargs.get("parent") or "").strip(),
    )
    return f"[jira:created] {created.get('key', '?')} — {summary}"


def _act_update_issue(client: JiraClient, issue: str, kwargs: dict) -> str:
    extra, err = _parse_extra_fields(kwargs)
    if err:
        return err
    summary = (kwargs.get("summary") or "").strip()
    description = kwargs.get("description") or ""
    parent = (kwargs.get("parent") or "").strip()
    if not summary and not description and not extra and not parent:
        return (
            "[jira:error] update_issue needs at least one of summary, "
            "description, parent (epic/parent key, 'none' clears), or "
            "fields JSON (field ids -> values)"
        )
    if summary or description or extra:
        client.update_issue(
            issue, summary=summary, description=description, fields=extra
        )
    if parent:
        # Separate call: the deployment backend maps `parent` to its native
        # field (Cloud parent field vs Data Center Epic Link customfield).
        client.set_parent(issue, parent)
    changed = [name for name, value in
               (("summary", summary), ("description", description)) if value]
    changed.extend(sorted(extra or {}))
    if parent:
        changed.append("parent" if parent.lower() not in {"none", "clear"}
                       else "parent (cleared)")
    return f"[jira:updated] {issue} — {', '.join(changed)}"


def _act_list_link_types(client: JiraClient) -> str:
    types = client.list_link_types()
    if not types:
        return "[jira:list_link_types] 0 link types"
    lines = [f"[jira:list_link_types] {len(types)} type(s)"]
    lines.extend(
        f"  {t['name']}: outward={t['outward']!r} inward={t['inward']!r}"
        for t in types
    )
    return "\n".join(lines)


def _act_link_issues(client: JiraClient, issue: str, kwargs: dict) -> str:
    link_type = (kwargs.get("link_type") or "").strip()
    target = (kwargs.get("target") or "").strip()
    if not link_type or not target:
        return (
            "[jira:error] link_type and target are required for link_issues — "
            "the link reads '<issue> <link_type> <target>' "
            "(e.g. issue=PROJ-1 link_type=blocks target=PROJ-2)"
        )
    # Resolve against the server's catalog: accept the type name or either
    # directional phrasing; an inward match ("is blocked by") flips the pair.
    inward, outward, name, description = issue, target, link_type, link_type
    try:
        types = client.list_link_types()
    except JiraError:
        types = []  # catalog unavailable — pass the raw name through
    if types:
        needle = link_type.lower()
        match = next(
            (t for t in types
             if needle in {t["name"].lower(), t["outward"].lower()}), None
        )
        if match is None:
            match = next(
                (t for t in types if needle == t["inward"].lower()), None
            )
            if match is not None:
                inward, outward = target, issue
        if match is None:
            catalog = "\n".join(
                f"  {t['name']}: outward={t['outward']!r} inward={t['inward']!r}"
                for t in types
            )
            return (
                f"[jira:error] unknown link type {link_type!r}. "
                f"Available:\n{catalog}"
            )
        name, description = match["name"], match["outward"]
    client.link_issues(name, inward, outward)
    return f"[jira:linked] {inward} {description} {outward}"


def _act_unlink_issues(client: JiraClient, kwargs: dict) -> str:
    link_id = str(kwargs.get("link_id") or "").strip()
    if not link_id:
        return (
            "[jira:error] link_id is required for unlink_issues — "
            "link ids appear on get_issue's link lines"
        )
    client.unlink_issues(link_id)
    return f"[jira:unlinked] link {link_id} removed"


def _act_delete_issue(client: JiraClient, issue: str, kwargs: dict) -> str:
    delete_subtasks = bool(kwargs.get("delete_subtasks"))
    client.delete_issue(issue, delete_subtasks=delete_subtasks)
    suffix = " (with subtasks)" if delete_subtasks else ""
    return f"[jira:deleted] {issue}{suffix} — permanent, not recoverable via the API"


def _split_issue_keys(issue: str) -> list[str]:
    return [k.strip() for k in issue.split(",") if k.strip()]


def _act_list_boards(client: JiraClient, entry: dict, kwargs: dict) -> str:
    project = (kwargs.get("project") or "").strip() or entry.get("default_project", "")
    boards = client.list_boards(
        project=project, limit=int(kwargs.get("limit") or 50)
    )
    if not boards:
        scope = f" for {project}" if project else ""
        return f"[jira:list_boards] 0 boards{scope}"
    lines = [f"[jira:list_boards] {len(boards)} board(s)"]
    lines.extend(
        f"  {b['id']}: {b['name']} ({b['type']}) {b.get('project', '')}".rstrip()
        for b in boards
    )
    return "\n".join(lines)


def _act_list_sprints(client: JiraClient, kwargs: dict) -> str:
    board_id = int(kwargs.get("board_id") or 0)
    if not board_id:
        return (
            "[jira:error] board_id is required for list_sprints — "
            "find it via list_boards"
        )
    sprints = client.list_sprints(
        board_id,
        state=(kwargs.get("sprint_state") or "").strip(),
        limit=int(kwargs.get("limit") or 50),
    )
    if not sprints:
        return f"[jira:list_sprints] board {board_id}: 0 sprints"
    lines = [f"[jira:list_sprints] board {board_id}: {len(sprints)} sprint(s)"]
    for s in sprints:
        window = f" {s['start'][:10]}..{s['end'][:10]}" \
            if s.get("start") or s.get("end") else ""
        goal = f" — {s['goal']}" if s.get("goal") else ""
        lines.append(f"  {s['id']}: {s['name']} [{s['state']}]{window}{goal}")
    return "\n".join(lines)


def _act_move_to_sprint(client: JiraClient, issue: str, kwargs: dict) -> str:
    sprint_id = int(kwargs.get("sprint_id") or 0)
    if not sprint_id:
        return (
            "[jira:error] sprint_id is required for move_to_sprint — "
            "find it via list_sprints"
        )
    keys = _split_issue_keys(issue)
    client.move_to_sprint(sprint_id, keys)
    return f"[jira:moved] {', '.join(keys)} -> sprint {sprint_id}"


def _act_move_to_backlog(client: JiraClient, issue: str) -> str:
    keys = _split_issue_keys(issue)
    client.move_to_backlog(keys)
    return f"[jira:moved] {', '.join(keys)} -> backlog"


def _act_add_worklog(client: JiraClient, issue: str, kwargs: dict) -> str:
    time_spent = (kwargs.get("time_spent") or "").strip()
    if not time_spent:
        return (
            "[jira:error] time_spent is required for add_worklog — "
            "Jira duration syntax, e.g. '2h 30m' or '1d'"
        )
    worklog = client.add_worklog(issue, time_spent, comment=kwargs.get("body") or "")
    return f"[jira:worklog-added] {issue} {time_spent} (id={worklog.get('id', '?')})"


def _act_list_worklogs(client: JiraClient, issue: str) -> str:
    logs = client.list_worklogs(issue)
    if not logs:
        return f"[jira:list_worklogs] {issue}: 0 worklogs"
    lines = [f"[jira:list_worklogs] {issue}: {len(logs)} worklog(s)"]
    for w in logs:
        comment = f" — {w['comment']}" if w.get("comment") else ""
        lines.append(
            f"  {w['id']}: {w['time_spent']} by {w['author']} "
            f"{(w.get('started') or '')[:16]}{comment}"
        )
    return "\n".join(lines)


def _act_attach_file(client: JiraClient, issue: str, kwargs: dict) -> str:
    file_path = (kwargs.get("file_path") or "").strip()
    if not file_path:
        return "[jira:error] file_path is required for attach_file"
    path = Path(file_path)
    if not path.is_file():
        return f"[jira:error] file not found: {file_path}"
    size = path.stat().st_size
    if size > _ATTACH_MAX_BYTES:
        return (
            f"[jira:error] {path.name} is {size} bytes — attach_file caps at "
            f"{_ATTACH_MAX_BYTES} bytes (Jira's own default limit is lower still)"
        )
    created = client.attach_file(issue, path.name, path.read_bytes())
    names = ", ".join(a.get("filename", "?") for a in created) or path.name
    return f"[jira:attached] {issue} <- {names} ({size} bytes)"


def _act_comment(client: JiraClient, issue: str, kwargs: dict) -> str:
    body = kwargs.get("body") or ""
    if not str(body).strip():
        return "[jira:error] body is required for comment"
    comment = client.add_comment(
        issue, body, body_format=kwargs.get("body_format") or "text"
    )
    return f"[jira:commented] {issue} comment id={comment.get('id', '?')}"


def _act_transition(client: JiraClient, issue: str, kwargs: dict) -> str:
    target = str(kwargs.get("transition") or "").strip()
    if not target:
        return "[jira:error] transition (id or name) is required"
    transition_id = target
    if not target.isdigit():
        available = client.list_transitions(issue)
        match = next(
            (t for t in available if t["name"].lower() == target.lower()), None
        )
        if match is None:
            names = ", ".join(f"{t['name']} ({t['id']})" for t in available)
            return (
                f"[jira:error] no transition named {target!r} on {issue}. "
                f"Available: {names or 'none'}"
            )
        transition_id = match["id"]
    extra, err = _parse_extra_fields(kwargs)
    if err:
        return err
    client.transition_issue(
        issue, transition_id, comment=kwargs.get("body") or "", fields=extra
    )
    return f"[jira:transitioned] {issue} via transition {transition_id}"


def _act_assign(client: JiraClient, issue: str, kwargs: dict) -> str:
    user = (kwargs.get("user") or "").strip()
    if not user:
        return (
            "[jira:error] user is required for assign — the deployment-native id "
            "(accountId on Cloud, username on Data Center); find it via search_users"
        )
    client.assign_issue(issue, user)
    return f"[jira:assigned] {issue} -> {user}"


# ── Ledger ────────────────────────────────────────────────


def _log_mutation(svc, action: str, issue: str, project: str,
                  kwargs: dict, response: str) -> None:
    """Append Jira mutations to the edit ledger. Never raises. Bodies and
    arbitrary field contents are deliberately NOT logged — only identifiers."""
    if action not in _MUTATING_ACTIONS:
        return
    if response.startswith(("[jira:error]", "[jira:missing-fields]")):
        return  # validation failed — nothing actually mutated
    if not getattr(svc, "edit_ledger", None):
        return
    ref = issue or project or "unknown"
    detail: dict[str, Any] = {
        "kind": "jira",
        "action": action,
        "issue": issue,
        "project": project,
    }
    try:
        svc.edit_ledger.log_edit(
            file=f"jira://{ref}",
            change_type=action,
            summary=response.splitlines()[0][:200] if response else f"jira {action}",
            tags=["jira", action],
            detail=detail,
        )
        if getattr(svc, "activity_log", None):
            svc.activity_log.log("jira_action", {
                "action": action, "issue": issue, "project": project,
            })
    except Exception:
        pass


# ── Main entrypoint ───────────────────────────────────────


def handle_jira(action: str, svc, finalize, **kwargs) -> str:
    """Dispatch a Jira action.

    ``svc`` provides ``project_path``, ``edit_ledger``, ``activity_log``.
    ``finalize(tool, args, response, summary)`` logs the call.
    """
    action = (action or "").strip().lower()
    args_for_log = {k: v for k, v in kwargs.items() if k not in {"token", "secret"}}
    args_for_log["action"] = action

    if not action:
        return finalize(_TOOL, args_for_log, "[jira:error] action is required", "error")

    client, entry, err = _build_client(svc, kwargs.get("account") or "")

    # `status` renders the config even without a working client.
    if action == "status":
        resp = _act_status(client, err, entry, svc)
        return finalize(_TOOL, args_for_log, _cap(resp), "status")

    if client is None:
        return finalize(_TOOL, args_for_log, err, "no-account")

    if action not in _VALID_ACTIONS:
        return finalize(
            _TOOL, args_for_log,
            f"[jira:unknown-action] '{action}'. Valid: {', '.join(_VALID_ACTIONS)}",
            "unknown-action",
        )

    issue = (kwargs.get("issue") or "").strip()
    if action in {"get_issue", "list_transitions", "update_issue",
                  "comment", "transition", "assign", "link_issues",
                  "delete_issue", "move_to_sprint", "move_to_backlog",
                  "add_worklog", "list_worklogs", "attach_file"} \
            and not issue:
        return finalize(
            _TOOL, args_for_log,
            f"[jira:error] issue key is required for {action}", "missing-arg",
        )
    project_for_log = (kwargs.get("project") or "").strip() \
        or entry.get("default_project", "")

    try:
        if action == "whoami":
            resp = _act_whoami(client)
        elif action == "search":
            resp = _act_search(client, kwargs)
        elif action == "my_issues":
            resp = _act_my_issues(client, entry, kwargs)
        elif action == "get_issue":
            resp = _format_issue_full(client.get_issue(issue))
        elif action == "list_projects":
            resp = _act_list_projects(client, kwargs)
        elif action == "list_transitions":
            resp = _act_list_transitions(client, issue)
        elif action == "get_create_metadata":
            resp = _act_create_metadata(client, entry, kwargs)
        elif action == "search_users":
            resp = _act_search_users(client, kwargs)
        elif action == "list_link_types":
            resp = _act_list_link_types(client)
        elif action == "link_issues":
            resp = _act_link_issues(client, issue, kwargs)
        elif action == "unlink_issues":
            resp = _act_unlink_issues(client, kwargs)
        elif action == "delete_issue":
            resp = _act_delete_issue(client, issue, kwargs)
        elif action == "list_boards":
            resp = _act_list_boards(client, entry, kwargs)
        elif action == "list_sprints":
            resp = _act_list_sprints(client, kwargs)
        elif action == "move_to_sprint":
            resp = _act_move_to_sprint(client, issue, kwargs)
        elif action == "move_to_backlog":
            resp = _act_move_to_backlog(client, issue)
        elif action == "add_worklog":
            resp = _act_add_worklog(client, issue, kwargs)
        elif action == "list_worklogs":
            resp = _act_list_worklogs(client, issue)
        elif action == "attach_file":
            resp = _act_attach_file(client, issue, kwargs)
        elif action == "create_issue":
            resp = _act_create_issue(client, entry, kwargs)
        elif action == "update_issue":
            resp = _act_update_issue(client, issue, kwargs)
        elif action == "comment":
            resp = _act_comment(client, issue, kwargs)
        elif action == "transition":
            resp = _act_transition(client, issue, kwargs)
        else:  # assign — action set is closed by the _VALID_ACTIONS gate above
            resp = _act_assign(client, issue, kwargs)
    except JiraError as exc:
        msg = f"[jira:api-error] {exc}"
        if "appropriate screen" in str(exc):
            if action == "create_issue":
                msg += (
                    "\nHint: createmeta lists the issue type's field "
                    "configuration, not the create screen. Retry without the "
                    "rejected field, then set it via update_issue — the edit "
                    "screen is configured separately."
                )
            elif action == "update_issue":
                msg += (
                    "\nHint: the field is not on the edit screen either — a "
                    "Jira admin must add it to the screen before the API can "
                    "set it."
                )
        return finalize(_TOOL, args_for_log, msg, f"http-{exc.status}")
    except Exception as exc:  # pragma: no cover — defensive
        return finalize(
            _TOOL, args_for_log,
            f"[jira:internal-error] {type(exc).__name__}: {exc}", "error",
        )

    _log_mutation(svc, action, issue, project_for_log, kwargs, resp)
    return finalize(_TOOL, args_for_log, _cap(resp), action)
