"""Builders for Claude Code transcripts and C3 session artefacts (2.143.0).

Transcripts are written the way Claude Code writes them: one compact JSON
object per line (``separators=(",", ":")``) — the catalog's tail scan looks
for unescaped ``"type":"ai-title"`` markers, so a spaced dump would hide
exactly what the tests exercise.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"
# Shares U3's first 8 chars, so an 8-char prefix is ambiguous between them.
U3B = "33333333-9999-4999-8999-999999999999"


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


class SessionFixture:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.claude = self.root / "claude-home"          # CLAUDE_CONFIG_DIR
        (self.claude / "projects").mkdir(parents=True, exist_ok=True)

    def project(self, name: str, init: bool = True) -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        if init:
            (p / ".c3").mkdir(exist_ok=True)
        return p

    def tdir(self, project) -> Path:
        slug = re.sub(r"[^a-zA-Z0-9]", "-", str(Path(project).resolve()))
        d = self.claude / "projects" / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def transcript(self, project, sid: str, *, prompt: str = "Fix the login bug",
                   title: str | None = "Login bug", last_prompt: str | None = "and the tests",
                   bridge: str | None = None, branch: str = "main",
                   started: str = "2026-09-01T10:00:00.000Z",
                   ended: str = "2026-09-01T11:00:00.000Z",
                   cwd=None, filler_kb: int = 0, title_after_filler: bool = False,
                   in_dir: Path | None = None, mtime: float | None = None) -> Path:
        cwd = str(cwd if cwd is not None else Path(project).resolve())
        base = {"cwd": cwd, "sessionId": sid, "version": "2.1.0", "gitBranch": branch}
        rows = [
            dict(base, type="user", isMeta=True, timestamp=started,
                 message={"role": "user", "content": "Caveat: local command output"}),
            dict(base, type="user", timestamp=started,
                 message={"role": "user", "content": prompt}),
            dict(base, type="assistant", timestamp=started,
                 message={"role": "assistant", "content": [
                     {"type": "text", "text": "On it."},
                     {"type": "tool_use", "name": "Bash", "input": {}}]}),
            dict(base, type="user", timestamp=started,
                 message={"role": "user", "content": [
                     {"type": "tool_result", "content": "ok"}]}),
        ]
        tail = []
        if title and not title_after_filler:
            rows.append({"type": "ai-title", "aiTitle": "draft " + title, "sessionId": sid})
            tail.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
        filler = [dict(base, type="attachment", timestamp=started, attachment="x" * 1000)
                  for _ in range(filler_kb)]
        after = []
        if title and title_after_filler:
            after = [{"type": "ai-title", "aiTitle": title, "sessionId": sid}]
            # Push the title more than one tail step (256 KB) from the end.
            after += [dict(base, type="attachment", timestamp=started, attachment="y" * 1000)
                      for _ in range(300)]
        if last_prompt:
            tail.append({"type": "last-prompt", "lastPrompt": last_prompt, "sessionId": sid})
        if bridge:
            tail.append({"type": "bridge-session", "bridgeSessionId": bridge, "sessionId": sid})
        tail.append(dict(base, type="user", timestamp=ended,
                         message={"role": "user", "content": last_prompt or "done"}))
        tail.append(dict(base, type="assistant", timestamp=ended,
                         message={"role": "assistant", "content": [
                             {"type": "text", "text": "Done."}]}))
        d = in_dir if in_dir is not None else self.tdir(project)
        path = d / f"{sid}.jsonl"
        path.write_text("\n".join(dumps(r) for r in rows + filler + after + tail) + "\n",
                        encoding="utf-8")
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def record(project, c3_id: str, host_id: str, *, system: str = "claude",
               decisions=(), description: str = "MCP server session",
               started: str = "2026-09-01T10:00:00+00:00") -> Path:
        d = Path(project) / ".c3" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"session_{c3_id}.json"
        path.write_text(json.dumps({
            "id": c3_id, "host_session_id": host_id, "started": started,
            "ended": started, "description": description, "summary": "Touched 1 file",
            "source_system": system, "source_ide": system,
            "git": {"branch": "main"},
            "decisions": [{"decision": d, "reasoning": "", "timestamp": started}
                          for d in decisions],
        }), encoding="utf-8")
        return path

    @staticmethod
    def snapshot(project, c3_id: str, task: str, notes: str,
                 created: str = "2026-09-01T10:30:00+00:00") -> Path:
        d = Path(project) / ".c3" / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"snap_{c3_id[:15]}.json"
        path.write_text(json.dumps({"session_id": c3_id, "created": created,
                                    "task_description": task, "custom_notes": notes}),
                        encoding="utf-8")
        return path

    @staticmethod
    def tasks(project, tasks: list[dict]) -> None:
        d = Path(project) / ".c3" / "pm"
        d.mkdir(parents=True, exist_ok=True)
        (d / "pm.json").write_text(json.dumps({"schema_version": 1, "rev": 1, "tasks": tasks,
                                               "milestones": [], "notes": []}),
                                   encoding="utf-8")

    @staticmethod
    def heartbeat(project, c3_id: str, host_id: str) -> None:
        d = Path(project) / ".c3" / "live"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{c3_id}.json").write_text(json.dumps({
            "session_id": c3_id, "host_session_id": host_id, "ide": "claude-code",
            "pid": 1, "ts": time.time()}), encoding="utf-8")

    @staticmethod
    def activity(project, rows: list[dict]) -> None:
        path = Path(project) / ".c3" / "activity_log.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
