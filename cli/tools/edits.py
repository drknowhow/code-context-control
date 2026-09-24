"""c3_edits — AI-tracked edit ledger: log, query, version and revert file changes."""

from pathlib import Path

from cli.tools.edit import _edit_lock, _log_to_ledger, _write_gate
from cli.tools.edit_verify import verify as _verify
from services import edit_blobs
from services.atomic_json import write_bytes_atomic

_ALL_ROWS = 10 ** 9


def _later_edits(ledger, row: dict) -> list:
    return [e["id"] for e in ledger.get_history(file=row["file"], limit=_ALL_ROWS)
            if e.get("timestamp", "") > row.get("timestamp", "")]


def _revert(edit_id: str, svc, finalize) -> str:
    """Put a file back to the pre-image a ledger row recorded.

    Refuses when the row has no pre-image, or when the file's current bytes
    are not the row's post-image. Goes through the same write gates and
    `_edit_lock` as c3_edit, and logs its own `reverted` row carrying its own
    pre/post images, so a revert is itself revertible.
    """
    args = {"action": "revert", "edit_id": edit_id}
    if not edit_id:
        return finalize("c3_edits", args, "edit_id is required", "missing edit_id")
    ledger = svc.edit_ledger
    row = next((e for e in ledger.get_history(limit=_ALL_ROWS)
                if e.get("id") == edit_id), None)
    if row is None:
        return finalize("c3_edits", args, f"No ledger row {edit_id}", "not found")
    detail = row.get("detail") or {}
    rel = row["file"]
    if "post_sha256" not in detail:
        return finalize(
            "c3_edits", args,
            f"[c3-revert:no-image] {edit_id} ({rel}) has no recorded pre/post "
            f"image. Only c3_edit writes record one; native Edit/Write (hook), "
            f"shell writes and older rows do not. Use git to undo it.",
            "no image")

    target_sha = detail.get("pre_sha256")
    target = None
    if target_sha is not None:
        target = edit_blobs.get(svc.project_path, target_sha)
        if target is None:
            why = detail.get("blob", "")
            why = why if why.startswith("skipped:") else "evicted or missing"
            return finalize(
                "c3_edits", args,
                f"[c3-revert:no-image] {edit_id} ({rel}): the pre-image is not "
                f"stored ({why}). Use git to undo it.",
                "no image")

    path = (Path(svc.project_path) / rel).resolve()
    if target is None:
        op = "delete"
    else:
        op = "write" if path.exists() else "create"
    refusal = _write_gate(svc, path, rel, rel, op, "c3_edits",
                          f"revert {edit_id}", finalize)
    if refusal is not None:
        return refusal

    try:
        with _edit_lock(path):
            try:
                current = path.read_bytes() if path.exists() else None
            except OSError as exc:
                return finalize("c3_edits", args, f"Read error: {exc}",
                                "read error")
            current_sha = (edit_blobs.sha256(current)
                           if current is not None else None)
            if current_sha != detail["post_sha256"]:
                later = _later_edits(ledger, row)
                hint = (f"\n  Later ledger edits to this file (revert newest "
                        f"first): {', '.join(reversed(later))}" if later else
                        "\n  No later ledger edit explains it: it was changed "
                        "outside C3.")
                return finalize(
                    "c3_edits", args,
                    f"[c3-revert:changed] {rel} has changed since {edit_id}; "
                    f"reverting it would discard those changes.{hint}",
                    "changed since")
            try:
                if target is None:
                    path.unlink()
                else:
                    write_bytes_atomic(path, target)
            except OSError as exc:
                return finalize("c3_edits", args, f"Write error: {exc}",
                                "write error")
            images = edit_blobs.record(svc.project_path, path, current, target)
    except TimeoutError:
        return finalize(
            "c3_edits", args,
            f"[c3-lock:busy] {rel} is held by another C3 process and did not "
            f"free up in time. Retry, or revert later.", "lock busy")

    verb = "deleted" if target is None else "restored"
    summary = f"Revert {edit_id}"
    deferred = _log_to_ledger(rel, summary, None, svc,
                              detail={"reverts": edit_id, **images},
                              change_type="reverted")
    return finalize("c3_edits", args,
                    f"✓ {rel} {verb} to its state before {edit_id}" + deferred,
                    f"{rel} reverted")


def handle_edits(action: str, file: str, change_type: str, summary: str,
                 lines_changed: str, tags: str, limit: int, since: str,
                 edit_id: str, tag: str, svc, finalize, branch: str = "",
                 old_string: str = "", new_string: str = "",
                 edits: str = "") -> str:
    """Route c3_edits actions."""
    ledger = svc.edit_ledger

    # verify runs BEFORE the ledger-availability gate. It leads on the file, and
    # the file is readable whether or not the ledger is: answering "did my edit
    # land" with "ledger disabled" would withhold the evidence that matters most
    # at the one moment a caller has no other way to find out (#74).
    if action == "verify":
        body, summ = _verify(file, old_string, new_string, edits, svc,
                             limit=limit or 200)
        return finalize("c3_edits", {"action": "verify", "file": file},
                        body, summ)

    if ledger is None:
        return finalize("c3_edits", {"action": action}, "Edit ledger not available", "ledger disabled")

    if action == "revert":
        return _revert(edit_id, svc, finalize)

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
            branch=branch or None,
        )
        if not entries:
            return finalize("c3_edits", {"action": "history"}, "No edits found", "0 edits")
        scope = (f" for {file}" if file else "") + (f" on {branch}" if branch else "")
        lines = [f"[edits:history] {len(entries)} entries" + scope]
        for e in entries:
            ln = f"  {e['timestamp'][:19]} | {e['file']} {e['version']} | {e['change_type']} | {e['summary']}"
            br = (e.get("git") or {}).get("branch")
            if br:
                ln += f" @{br}"
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
                        f"Unknown action: {action}. "
                        "Use: log, history, versions, stats, tag, verify, revert",
                        "unknown action")
