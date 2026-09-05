"""Bounded, project-checked Codex rollout adapter. No private reasoning import."""
import json
import os
from datetime import datetime
from pathlib import Path


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _records(path: Path):
    # Bound individual lines, not the entire transcript in memory.
    with path.open(encoding="utf-8", errors="replace") as stream:
        while True:
            line = stream.readline(2 * 1024 * 1024 + 1)
            if not line:
                return
            if len(line) > 2 * 1024 * 1024:
                while line and not line.endswith("\n"):
                    line = stream.readline(2 * 1024 * 1024 + 1)
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    yield record
            except ValueError:
                continue


def read_rollout(path: Path, project: Path, session_id: str = "",
                 max_turns: int = 2000, max_text: int = 24000) -> dict | None:
    """Require matching session metadata before accepting any content or usage."""
    records = _records(path)
    metadata = next(records, None)
    if not metadata or metadata.get("type") != "session_meta":
        return None
    meta = metadata.get("payload") or {}
    if not isinstance(meta, dict) or not meta.get("cwd") or not meta.get("id"):
        return None
    if Path(meta["cwd"]).resolve() != project.resolve():
        return None
    if session_id and meta["id"] != session_id:
        return None
    turns, usage, model = [], None, ""
    for record in records:
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            model = str(payload.get("model") or model)
        elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(total, dict):
                # Totals are cumulative; summing events double-counts the session.
                keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
                usage = {k: max(0, v) for k in keys
                         if isinstance((v := total.get(k)), int) and not isinstance(v, bool)}
        elif record.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role not in ("user", "assistant") or payload.get("phase") == "analysis":
                continue
            content = payload.get("content") or []
            if not isinstance(content, list):
                continue
            text = "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict)
                             and p.get("type") in ("input_text", "output_text", "text"))[:max_text]
            if text and len(turns) < max_turns:
                try:
                    ts = datetime.fromisoformat(record.get("timestamp", "").replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    ts = 0.0
                turns.append({"id": f"t{len(turns)+1:04d}", "ts": ts, "role": role,
                              "text": text, "source": "codex"})
    return {"session_id": str(meta["id"]), "turns": turns, "usage": usage,
            "usage_available": usage is not None and all(k in usage for k in ("input_tokens", "output_tokens")),
            "model": model}
