"""c3_edits — AI-tracked edit ledger: log, query, and version file changes."""


def handle_edits(action: str, file: str, change_type: str, summary: str,
                 lines_changed: str, tags: str, limit: int, since: str,
                 edit_id: str, tag: str, svc, finalize) -> str:
    """Route c3_edits actions."""
    ledger = svc.edit_ledger
    if ledger is None:
        return finalize("c3_edits", {"action": action}, "Edit ledger not available", "ledger disabled")

    if action == "log":
        if not file:
            return finalize("c3_edits", {"action": "log"}, "file is required", "missing file")
        # Parse lines_changed: "120,145" → [120, 145]
        lc = None
        if lines_changed:
            try:
                lc = [int(x.strip()) for x in lines_changed.split(",") if x.strip()]
            except ValueError:
                lc = None
        # Parse tags: "tag1,tag2" → ["tag1", "tag2"]
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        session_id = ""
        if svc.session_mgr and hasattr(svc.session_mgr, "current_session"):
            cs = svc.session_mgr.current_session
            if cs:
                session_id = cs.get("id", "")

        entry = ledger.log_edit(
            file=file,
            change_type=change_type or "modified",
            summary=summary or "Edit logged",
            lines_changed=lc,
            tags=tag_list,
            session_id=session_id,
        )

        # Cross-log to activity_log
        if svc.activity_log:
            svc.activity_log.log("file_change", {
                "file": entry["file"],
                "change_type": entry["change_type"],
                "summary": entry["summary"],
                "edit_id": entry["id"],
            })

        # Cross-log to session_mgr
        if svc.session_mgr and hasattr(svc.session_mgr, "log_file_change"):
            svc.session_mgr.log_file_change(entry["file"], entry["change_type"])

        body = (f"[edit:{entry['id']}] {entry['file']} {entry['version']}\n"
                f"  type: {entry['change_type']}\n"
                f"  summary: {entry['summary']}")
        if entry.get("diff_summary"):
            body += f"\n  diff: {entry['diff_summary']}"
        if entry.get("git", {}).get("commit"):
            body += f"\n  git: {entry['git']['commit']} ({entry['git']['subject']})"
        return finalize("c3_edits", {"action": "log", "file": file}, body,
                        f"{entry['file']} → {entry['version']}")

    elif action == "history":
        entries = ledger.get_history(
            file=file or None,
            limit=limit or 50,
            since=since or None,
        )
        if not entries:
            return finalize("c3_edits", {"action": "history"}, "No edits found", "0 edits")
        lines = [f"[edits:history] {len(entries)} entries" + (f" for {file}" if file else "")]
        for e in entries:
            ln = f"  {e['timestamp'][:19]} | {e['file']} {e['version']} | {e['change_type']} | {e['summary']}"
            if e.get("tags"):
                ln += f" [{','.join(e['tags'])}]"
            lines.append(ln)
        return finalize("c3_edits", {"action": "history", "file": file},
                        "\n".join(lines), f"{len(entries)} edits")

    elif action == "versions":
        if not file:
            return finalize("c3_edits", {"action": "versions"}, "file is required", "missing file")
        versions = ledger.get_file_versions(file)
        if not versions:
            return finalize("c3_edits", {"action": "versions", "file": file},
                            f"No versions found for {file}", "0 versions")
        lines = [f"[edits:versions] {file} — {len(versions)} versions"]
        for v in versions:
            ln = f"  {v['version']} | {v['timestamp'][:19]} | {v['change_type']} | {v['summary']}"
            lines.append(ln)
        current = versions[-1]["version"] if versions else "v0"
        return finalize("c3_edits", {"action": "versions", "file": file},
                        "\n".join(lines), f"{file} current: {current}")

    elif action == "stats":
        stats = ledger.get_stats()
        lines = [
            f"[edits:stats] {stats['total']} total edits across {stats['files']} files",
            f"  by type: {stats['by_type']}",
        ]
        if stats.get("most_edited"):
            lines.append("  most edited:")
            for m in stats["most_edited"][:5]:
                lines.append(f"    {m['file']}: {m['count']} edits")
        return finalize("c3_edits", {"action": "stats"},
                        "\n".join(lines), f"{stats['total']} edits, {stats['files']} files")

    elif action == "tag":
        if not edit_id or not tag:
            return finalize("c3_edits", {"action": "tag"},
                            "edit_id and tag are required", "missing params")
        ok = ledger.tag_edit(edit_id, tag)
        msg = f"Tagged {edit_id} with '{tag}'" if ok else f"Edit {edit_id} not found"
        return finalize("c3_edits", {"action": "tag"}, msg, msg)

    else:
        return finalize("c3_edits", {"action": action},
                        f"Unknown action: {action}. Use: log, history, versions, stats, tag",
                        "unknown action")
