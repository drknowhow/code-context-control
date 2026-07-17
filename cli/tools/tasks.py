"""c3_task — durable per-project tasks, milestones, and decision notes."""

READ_ACTIONS = {"list", "get", "board", "history", "milestone_list", "note_list"}

_TASK_ACTIONS = ("add, update, done, list, get, board, history, archive, link, unlink, "
                 "milestone_add, milestone_update, milestone_list, milestone_archive, "
                 "note_add, note_list")


def _fmt_task(t) -> str:
    due = f" due:{t['due_date']}" if t.get("due_date") else ""
    tags = (" #" + " #".join(t["tags"])) if t.get("tags") else ""
    ms = " [ms]" if t.get("milestone_id") else ""
    return f"  {t['id'][:8]} [{t['status']:<11}] ({t['priority']}) {t['title']}{ms}{due}{tags}"


def handle_task(action, svc, finalize, *, title="", task_id="", status="",
                priority="", due_date="", tags="", description="", milestone="",
                note="", kind="", link_type="", ref="", label="", name="",
                target_date="", query="", limit=50) -> str:
    action = (action or "").strip().lower()

    def done_resp(resp, summ="ok"):
        return finalize("c3_task", {"action": action}, resp, summ)

    store = getattr(svc, "task_store", None)
    if store is None:
        return done_resp("[task:error] task store unavailable on this runtime", "error")

    pm_cfg = (getattr(svc, "hybrid_config", None) or {}).get("pm") or {}
    if not pm_cfg.get("enabled", True):
        return done_resp("[task:disabled] PM is off (hybrid.pm.enabled=false)", "disabled")

    if not action:
        return done_resp(f"[task:error] action required. Actions: {_TASK_ACTIONS}.", "error")

    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

    def _milestone_id():
        """Resolve the milestone param to an id. Returns (id|None, error|None)."""
        if not (milestone or "").strip():
            return None, None
        ms = store.resolve_milestone(milestone)
        if ms is None:
            return None, f"[task:error] no milestone matches: {milestone}"
        return ms["id"], None

    # ── Tasks ──────────────────────────────────────────────────────
    if action == "add":
        if not (title or "").strip():
            return done_resp("[task:error] add requires title.", "error")
        ms_id, err = _milestone_id()
        if err:
            return done_resp(err, "error")
        session = getattr(getattr(svc, "session_mgr", None), "current_session", None) or {}
        res = store.create_task(
            title, description=description, status=status or "backlog",
            priority=priority or "p2", due_date=due_date or None,
            tags=tag_list, milestone_id=ms_id,
            created_by="mcp", origin_session=session.get("id", ""))
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(
            f"[task:added] {res['id']} \"{res['title']}\" ({res['priority']}, {res['status']})",
            res["id"][:8])

    if action in ("update", "done"):
        if not task_id:
            return done_resp(f"[task:error] {action} requires task_id.", "error")
        fields = {}
        if action == "done":
            fields["status"] = "done"
        else:
            if status:
                fields["status"] = status
            if priority:
                fields["priority"] = priority
            if due_date:
                fields["due_date"] = due_date
            if description:
                fields["description"] = description
            if title:
                fields["title"] = title
            if tags:
                fields["tags"] = tag_list
            if milestone:
                ms_id, err = _milestone_id()
                if err:
                    return done_resp(err, "error")
                fields["milestone_id"] = ms_id
            if not fields:
                return done_resp("[task:error] update requires at least one field "
                                 "(status/priority/due_date/description/title/tags/milestone).",
                                 "error")
        res = store.update_task(task_id, actor="mcp", **fields)
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        mark = "done" if res["status"] == "done" else "updated"
        return done_resp(f"[task:{mark}] {res['id'][:8]} \"{res['title']}\" "
                         f"({res['priority']}, {res['status']})", mark)

    if action == "archive":
        if not task_id:
            return done_resp("[task:error] archive requires task_id.", "error")
        res = store.archive_task(task_id, actor="mcp")
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(f"[task:archived] {res['id'][:8]} \"{res['title']}\"", "archived")

    if action == "get":
        if not task_id:
            return done_resp("[task:error] get requires task_id.", "error")
        t = store.get_task(task_id)
        if t is None:
            return done_resp(f"[task:error] no task matches: {task_id}", "error")
        lines = [f"[task:{t['id']}] {t['title']}",
                 f"  status:{t['status']} priority:{t['priority']} "
                 f"due:{t.get('due_date') or '—'} lifecycle:{t['lifecycle']}"]
        if t.get("tags"):
            lines.append(f"  tags: {', '.join(t['tags'])}")
        if t.get("milestone_id"):
            lines.append(f"  milestone: {t['milestone_id']}")
        if t.get("description"):
            lines.append(f"  {t['description'][:400]}")
        for link in t.get("links", []):
            lines.append(f"  link[{link['type']}]: {link['ref']}")
        return done_resp("\n".join(lines), "1 task")

    if action == "list":
        ms_id, err = _milestone_id()
        if err:
            return done_resp(err, "error")
        rows = store.list_tasks(status=status or None, milestone_id=ms_id,
                                tag=(tag_list[0] if tag_list else None),
                                priority=priority or None, query=query,
                                limit=max(1, int(limit)))
        if not rows:
            return done_resp("[task:list] 0 tasks", "0")
        body = "\n".join(_fmt_task(t) for t in rows)
        return done_resp(f"[task:list] {len(rows)} task(s)\n{body}", f"{len(rows)}t")

    if action == "board":
        board = store.board()
        s = board["stats"]
        lines = []
        rec = board.get("recovery")
        if rec:
            src = ("restored from pm.json.bak" if rec.get("restored_from_backup")
                   else "no backup — started empty")
            lines.append(f"[task:warning] corrupt pm.json quarantined as "
                         f"{rec.get('quarantined') or '?'} ({src})")
        lines.append(f"[task:board] open:{s['open']} overdue:{s['overdue']} "
                     f"done:{s['done_total']} rev:{board.get('rev', 0)}")
        for col, rows in board["columns"].items():
            lines.append(f"  {col} ({len(rows)}):")
            for t in rows[:10]:
                lines.append("  " + _fmt_task(t))
        for ms in board["milestones"]:
            p = ms["progress"]
            lines.append(f"  milestone {ms['id'][:8]} \"{ms['name']}\" "
                         f"{p['done']}/{p['total']} ({p['pct']}%)")
        return done_resp("\n".join(lines), f"{s['open']} open")

    if action == "history":
        rows = store.history(item_id=task_id or None, limit=max(1, int(limit)))
        if not rows:
            return done_resp("[task:history] 0 events", "0")
        lines = [f"[task:history] {len(rows)} event(s), newest first"]
        for ev in rows:
            when = (ev.get("ts") or "")[:16].replace("T", " ")
            who = f" by {ev['actor']}" if ev.get("actor") else ""
            changes = ev.get("patch") or {}
            if ev.get("op") == "create":
                changes = {"title": (ev.get("data") or {}).get("title", "")}
            elif not changes:
                changes = ev.get("data") or {}
            detail = ", ".join(
                f"{k}: {v[0]}->{v[1]}" if isinstance(v, list) and len(v) == 2
                else f"{k}={v}"
                for k, v in list(changes.items())[:4])
            lines.append(f"  {when} r{ev.get('rev', '?')} {ev.get('entity')}."
                         f"{ev.get('op')} {(ev.get('id') or '')[:8]}{who} {detail}")
        return done_resp("\n".join(lines), f"{len(rows)}e")

    if action in ("link", "unlink"):
        if not task_id or not link_type or not ref:
            return done_resp(f"[task:error] {action} requires task_id, link_type "
                             "(file|commit|edit), and ref.", "error")
        res = (store.add_link(task_id, link_type, ref, label=label)
               if action == "link" else store.remove_link(task_id, link_type, ref))
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(f"[task:{action}ed] {res['id'][:8]} now has "
                         f"{len(res.get('links', []))} link(s)", action)

    # ── Milestones ─────────────────────────────────────────────────
    if action == "milestone_add":
        if not (name or "").strip():
            return done_resp("[task:error] milestone_add requires name.", "error")
        res = store.create_milestone(name, description=description,
                                     target_date=target_date or None)
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(f"[milestone:added] {res['id']} \"{res['name']}\"", res["id"][:8])

    if action == "milestone_update":
        if not milestone:
            return done_resp("[task:error] milestone_update requires milestone.", "error")
        ms = store.resolve_milestone(milestone)
        if ms is None:
            return done_resp(f"[task:error] no milestone matches: {milestone}", "error")
        fields = {}
        if name:
            fields["name"] = name
        if target_date:
            fields["target_date"] = target_date
        if description:
            fields["description"] = description
        if not fields:
            return done_resp("[task:error] milestone_update requires "
                             "name/target_date/description.", "error")
        res = store.update_milestone(ms["id"], **fields)
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(f"[milestone:updated] {res['id'][:8]} \"{res['name']}\"", "updated")

    if action == "milestone_archive":
        if not milestone:
            return done_resp("[task:error] milestone_archive requires milestone.", "error")
        ms = store.resolve_milestone(milestone)
        if ms is None:
            return done_resp(f"[task:error] no milestone matches: {milestone}", "error")
        res = store.archive_milestone(ms["id"])
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(f"[milestone:archived] \"{res['name']}\" "
                         f"({res['detached_tasks']} task(s) detached)", "archived")

    if action == "milestone_list":
        rows = store.list_milestones()
        if not rows:
            return done_resp("[milestone:list] 0 milestones", "0")
        lines = [f"[milestone:list] {len(rows)} milestone(s)"]
        for ms in rows:
            p = ms["progress"]
            target = f" target:{ms['target_date']}" if ms.get("target_date") else ""
            lines.append(f"  {ms['id'][:8]} \"{ms['name']}\" {p['done']}/{p['total']} "
                         f"({p['pct']}%){target}")
        return done_resp("\n".join(lines), f"{len(rows)}m")

    # ── Notes ──────────────────────────────────────────────────────
    if action == "note_add":
        if not (note or "").strip():
            return done_resp("[task:error] note_add requires note.", "error")
        res = store.add_note(note, kind=kind or "note", tags=tag_list,
                             task_id=task_id or None, author="mcp")
        if "error" in res:
            return done_resp(f"[task:error] {res['error']}", "error")
        return done_resp(f"[note:added] {res['id']} ({res['kind']})", res["id"][:8])

    if action == "note_list":
        rows = store.list_notes(kind=(kind if kind in ("note", "decision") else None),
                                limit=max(1, int(limit)))
        if not rows:
            return done_resp("[note:list] 0 notes", "0")
        lines = [f"[note:list] {len(rows)} note(s)"]
        for n in rows:
            date = (n.get("created_at") or "")[:10]
            lines.append(f"  {n['id'][:8]} [{n['kind']}] {date} {n['text'][:120]}")
        return done_resp("\n".join(lines), f"{len(rows)}n")

    return done_resp(f"[task:error] Unknown action '{action}'. Actions: {_TASK_ACTIONS}.",
                     "error")
