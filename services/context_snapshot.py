"""
Context Snapshot — Capture/restore working context across /clear boundaries.

Saves session state (decisions, files, notes, facts) before /clear,
provides compact briefings to reinstate context after /clear.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core import count_tokens
from services.git_context import GitContext

# Max characters per file structural map stored in snapshot
_FILE_MAP_MAX_CHARS = 600


class ContextSnapshot:
    """Snapshot/restore for clear-and-recall workflow."""

    def __init__(self, project_path: str, data_dir: str = ".c3/snapshots"):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._git = GitContext(self.project_path)

    def capture(self, session_mgr, memory_store,
                task_description: str = "",
                working_files: list = None,
                custom_notes: str = "",
                compressor=None) -> dict:
        """Capture current working context to a snapshot file.

        Returns {snapshot_id, path, token_count}.
        """
        session = session_mgr.current_session or {}
        session_id = session.get("id", "")

        # Collect decisions from current session
        decisions = session.get("decisions", [])

        # Collect files touched
        files_touched = session.get("files_touched", [])

        # Collect context notes
        context_notes = session.get("context_notes", [])

        # Session-scoped facts (added this session)
        session_facts = [
            f for f in memory_store.facts
            if f.get("source_session") == session_id and session_id
        ]

        # Also recall top relevant facts from full memory store (cross-session)
        relevant_facts = []
        if task_description:
            try:
                recalled = memory_store.recall(task_description, top_k=8)
                session_fact_texts = {f["fact"] for f in session_facts}
                relevant_facts = [
                    {"fact": r["fact"], "category": r.get("category", "general")}
                    for r in recalled
                    if r["fact"] not in session_fact_texts
                ][:6]
            except Exception:
                pass

        # Auto-populate working_files from files_touched when not explicitly provided
        if not working_files and files_touched:
            working_files = [ft["file"] for ft in files_touched[:8]]

        # Capture structural maps of working files for immediate context on restore
        file_maps = {}
        if compressor and working_files:
            for fp in working_files[:5]:
                try:
                    abs_fp = str(self.project_path / fp) if not Path(fp).is_absolute() else fp
                    result = compressor.compress_file(abs_fp, mode="structure")
                    if result and not result.get("error"):
                        file_maps[fp] = result.get("compressed", "")[:_FILE_MAP_MAX_CHARS]
                except Exception:
                    pass

        # Extract plan decisions separately so they are surfaced prominently on restore
        plans = [
            d for d in decisions
            if d.get("decision", "").startswith("PLAN:")
        ]

        # Context budget snapshot
        budget = session.get("context_budget", {})

        # Git working-tree state — lets restore flag a branch change.
        try:
            gstate = self._git.state(force=True)
            git_info = {
                "branch": gstate.get("branch"),
                "head_sha": gstate.get("head_sha", ""),
                "detached": gstate.get("detached", False),
            }
        except Exception:
            git_info = {"branch": None, "head_sha": "", "detached": False}

        snapshot = {
            "schema_version": 3,
            "snapshot_id": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
            "created": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "git": git_info,
            "task_description": task_description,
            "working_files": working_files or [],
            "custom_notes": custom_notes,
            "decisions": decisions,
            "plans": plans,
            "files_touched": files_touched,
            "context_notes": context_notes,
            "session_facts": [
                {"fact": f["fact"], "category": f["category"]}
                for f in session_facts
            ],
            "relevant_facts": relevant_facts,
            "file_maps": file_maps,
            "context_budget": {
                "response_tokens": budget.get("response_tokens", 0),
                "call_count": budget.get("call_count", 0),
            },
            "state": {
                "task_description": task_description,
                "working_files": working_files or [],
                "decisions": decisions,
                "files_touched": files_touched,
                "context_notes": context_notes,
                "session_facts": [
                    {"fact": f["fact"], "category": f["category"], "id": f.get("id", "")}
                    for f in session_facts
                ],
                "context_budget": {
                    "response_tokens": budget.get("response_tokens", 0),
                    "call_count": budget.get("call_count", 0),
                    },
            },
        }

        path = self.data_dir / f"snap_{snapshot['snapshot_id']}.json"
        # Atomic write: a partial snapshot (crash mid-dump) is the newest file
        # and would make _load_snapshot("latest") raise. temp + os.replace
        # guarantees the snapshot file is either absent or complete.
        tmp = path.with_suffix(f".json.tmp-{os.getpid()}-{int(time.time() * 1000)}")
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

        token_count = count_tokens(json.dumps(snapshot))
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "path": str(path),
            "token_count": token_count,
        }

    def restore(self, snapshot_id: str = "latest", memory_store=None, level: int = 0) -> dict:
        """Restore a snapshot as a full markdown briefing.

        Args:
            snapshot_id: Snapshot ID or 'latest'.
            memory_store: Optional MemoryStore for live recall enrichment.
            level: 0=full briefing, 1=compact briefing (for auto-restore notifications).

        Returns {snapshot_id, briefing, tokens}.
        """
        snap = self._load_snapshot(snapshot_id)
        if "error" in snap:
            return snap

        # Flag when the working tree has moved off the branch this was taken on.
        snap_git = snap.get("git") or {}
        if snap_git.get("branch"):
            try:
                cur = self._git.state(force=True)
                if (cur.get("available") and cur.get("branch")
                        and cur["branch"] != snap_git["branch"]):
                    snap["_branch_warning"] = (
                        f"snapshot taken on '{snap_git['branch']}', "
                        f"now on '{cur['branch']}'"
                    )
            except Exception:
                pass

        # Enrich with live memory recall so cross-session facts are surfaced immediately
        if memory_store and snap.get("task_description"):
            try:
                recalled = memory_store.recall(snap["task_description"], top_k=6)
                existing_texts = (
                    {f["fact"] for f in snap.get("session_facts", [])}
                    | {f["fact"] for f in snap.get("relevant_facts", [])}
                )
                snap["_live_recall"] = [
                    {"fact": r["fact"], "category": r.get("category", "general")}
                    for r in recalled
                    if r["fact"] not in existing_texts
                ][:5]
            except Exception:
                pass

        sid = snap["snapshot_id"]
        briefing = self._compact_briefing(snap) if level > 0 else self._full_briefing(snap)

        return {
            "snapshot_id": sid,
            "briefing": briefing,
            "tokens": count_tokens(briefing),
            "state": snap.get("state", {}),
        }

    def list_snapshots(self, n: int = 10) -> list:
        """List recent snapshots."""
        files = sorted(self.data_dir.glob("snap_*.json"), reverse=True)[:n]
        results = []
        for sf in files:
            try:
                with open(sf, encoding='utf-8') as f:
                    snap = json.load(f)
                results.append({
                    "id": snap["snapshot_id"],
                    "created": snap.get("created", ""),
                    "task_description": snap.get("task_description", "")[:80],
                    "decisions_count": len(snap.get("decisions", [])),
                    "files_count": len(snap.get("files_touched", [])),
                })
            except Exception:
                continue
        return results

    def restore_state(self, snapshot_id: str = "latest") -> dict:
        snap = self._load_snapshot(snapshot_id)
        if "error" in snap:
            return snap
        return {
            "snapshot_id": snap["snapshot_id"],
            "state": snap.get("state", {}),
        }

    def search(self, query: str, top_k: int = 5) -> list:
        query_l = (query or "").lower().strip()
        if not query_l:
            return []
        results = []
        for item in self.list_snapshots(50):
            snap = self._load_snapshot(item["id"])
            haystack = " ".join([
                snap.get("task_description", ""),
                snap.get("custom_notes", ""),
                " ".join(note for note in snap.get("context_notes", [])),
                " ".join(d.get("decision", "") for d in snap.get("decisions", [])),
                " ".join(f.get("fact", "") for f in snap.get("session_facts", [])),
            ]).lower()
            if query_l in haystack:
                results.append({
                    "snapshot_id": snap["snapshot_id"],
                    "task_description": snap.get("task_description", ""),
                    "created": snap.get("created", ""),
                    "score": 1.0 if query_l in snap.get("task_description", "").lower() else 0.6,
                })
            if len(results) >= top_k:
                break
        return results

    def _load_snapshot(self, snapshot_id: str) -> dict:
        """Load a snapshot by ID or 'latest'.

        For 'latest', skip past any corrupt/partial files to the next-newest
        good snapshot instead of raising.
        """
        if snapshot_id == "latest":
            files = sorted(self.data_dir.glob("snap_*.json"), reverse=True)
            if not files:
                return {"error": "No snapshots found"}
            last_error = None
            for path in files:
                try:
                    with open(path, encoding='utf-8') as f:
                        return json.load(f)
                except Exception as exc:  # corrupt/partial — try next-newest
                    last_error = exc
                    continue
            return {"error": f"No readable snapshots found: {last_error}"}

        path = self.data_dir / f"snap_{snapshot_id}.json"
        if not path.exists():
            return {"error": f"Snapshot not found: {snapshot_id}"}

        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception as exc:
            return {"error": f"Snapshot is corrupt: {snapshot_id}: {exc}"}

    def _full_briefing(self, snap: dict) -> str:
        """Level 0: Full briefing with all details."""
        parts = [f"# Context Restore: {snap.get('task_description', 'N/A')}"]
        parts.append(f"Snapshot: {snap['snapshot_id']} | Session: {snap.get('session_id', '?')}")

        git = snap.get("git") or {}
        if git.get("head_sha"):
            label = (git.get("branch") or "(detached)") + " @ " + git["head_sha"][:8]
            parts.append(f"Branch: {label}")
        if snap.get("_branch_warning"):
            parts.append(f"\n⚠️ Branch changed since snapshot — {snap['_branch_warning']}")

        if snap.get("custom_notes"):
            parts.append(f"\n## Notes\n{snap['custom_notes']}")

        # Plans first — highest priority context
        plans = snap.get("plans", [])
        if plans:
            parts.append("\n## Plans")
            for d in plans:
                text = d["decision"].removeprefix("PLAN:").strip()
                if d.get("reasoning"):
                    text += f" — {d['reasoning']}"
                parts.append(f"- {text}")

        # Live-recalled facts (cross-session, surfaced at restore time)
        live_recall = snap.get("_live_recall", [])
        if live_recall:
            parts.append("\n## Relevant Memory (live recall)")
            for fact in live_recall:
                parts.append(f"- [{fact['category']}] {fact['fact']}")

        # Relevant facts captured at snapshot time (cross-session)
        relevant_facts = snap.get("relevant_facts", [])
        if relevant_facts:
            parts.append("\n## Relevant Memory (at snapshot)")
            for fact in relevant_facts:
                parts.append(f"- [{fact['category']}] {fact['fact']}")

        decisions = snap.get("decisions", [])
        non_plan_decisions = [d for d in decisions if not d.get("decision", "").startswith("PLAN:")]
        if non_plan_decisions:
            parts.append("\n## Decisions")
            for d in non_plan_decisions:
                line = f"- {d['decision']}"
                if d.get("reasoning"):
                    line += f" — {d['reasoning']}"
                parts.append(line)

        files = snap.get("files_touched", [])
        if files:
            parts.append("\n## Files Touched")
            for ft in files:
                summary = f" — {ft['summary']}" if ft.get("summary") else ""
                parts.append(f"- {ft.get('type', '?')}: {ft['file']}{summary}")

        # File structural maps — skip re-reading after restore
        file_maps = snap.get("file_maps", {})
        if file_maps:
            parts.append("\n## File Structures (skip re-reading)")
            for fp, fmap in file_maps.items():
                parts.append(f"\n### {fp}\n```\n{fmap}\n```")

        ctx_notes = snap.get("context_notes", [])
        if ctx_notes:
            parts.append("\n## Context Notes")
            for note in ctx_notes:
                parts.append(f"- {note}")

        facts = snap.get("session_facts", [])
        if facts:
            parts.append("\n## Session Facts")
            for fact in facts:
                parts.append(f"- [{fact['category']}] {fact['fact']}")

        working = snap.get("working_files", [])
        if working:
            parts.append(f"\n## Working Files\n{', '.join(working)}")

        budget = snap.get("context_budget", {})
        if budget.get("response_tokens"):
            parts.append(f"\n## Budget\n{budget['response_tokens']}tok / {budget['call_count']} calls")

        return "\n".join(parts)

    def _compact_briefing(self, snap: dict) -> str:
        """Level 1: Compact briefing — top decisions + file list."""
        parts = [f"[restore:{snap['snapshot_id']}] {snap.get('task_description', '')}"]
        if snap.get("_branch_warning"):
            parts.append(f"⚠️ branch changed — {snap['_branch_warning']}")

        plans = snap.get("plans", [])
        if plans:
            for d in plans:
                text = d["decision"].removeprefix("PLAN:").strip()
                parts.append(f"[plan] {text[:80]}")

        decisions = snap.get("decisions", [])
        non_plan = [d for d in decisions if not d.get("decision", "").startswith("PLAN:")]
        if non_plan:
            top = non_plan[-3:]  # Most recent 3
            for d in top:
                parts.append(f"- {d['decision'][:80]}")

        files = snap.get("files_touched", [])
        if files:
            file_list = ", ".join(ft["file"] for ft in files[:10])
            parts.append(f"files: {file_list}")

        facts = snap.get("session_facts", [])
        relevant = snap.get("relevant_facts", [])
        total_facts = len(facts) + len(relevant)
        if total_facts:
            parts.append(f"facts: {total_facts} available")

        return "\n".join(parts)
