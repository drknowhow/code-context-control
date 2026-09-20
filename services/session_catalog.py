"""Past agent sessions of one project, one row each (2.143.0).

Why this exists
---------------
Every Claude Code conversation is kept as a transcript
(``~/.claude/projects/<slug>/<uuid>.jsonl``) and C3 keeps its own session
records, snapshots and tasks beside the project. Nothing joined them, so
"which session was I doing X in, is it worth going back to, and how do I get
back" had no answer short of opening JSONL files by hand. And nothing could
say that a session was a dead end.

This module builds that join. It never parses a whole transcript: the head
(first prompt, start, cwd) and the tail (title, last prompt, remote bridge,
last activity) are enough to orient, and a per-file cache keyed on
size + mtime means a transcript is read once per change.

Stale is a FLAG, not a guess. The agent (``c3_session(action='stale')``) or a
person (Hub / Desk) sets it, with a reason and optionally the session that
superseded it. Heuristics (idle for weeks, ended by /clear, branch deleted,
very short) only ever produce ``hints``; they never set ``stale``.

Marks live in ``.c3/session_marks.jsonl``, append-only and folded last-wins.
Several MCP processes and the Hub can write at once, and an append never
loses a concurrent writer's row the way a read-modify-write of one JSON file
would. The file doubles as the audit trail.

Resume is built here, never from caller input: the id must be a UUID, its
transcript must exist, and the command is a fixed argv
``["claude", "--resume", <id>]``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "claude_projects_root", "claude_transcript_dir", "scan_transcript",
    "list_sessions", "get_session", "overview", "mark", "resolve_id",
    "resume_spec", "remote_url", "MARK_OPS",
]

# Bump when scan_transcript's output changes, so cached metas are re-read.
CACHE_VERSION = 2
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The claude.ai/code bridge id is ``cse_<x>`` and the web session lives at
# ``/code/session_<x>``. Inferred from one sample (2026-09-19); a bridge id in
# any other shape gets no link rather than a guessed one.
_BRIDGE_RE = re.compile(r"^cse_([A-Za-z0-9]+)$")
_REMOTE_URL = "https://claude.ai/code/session_{}"

HEAD_BYTES = 64 * 1024
HEAD_MAX_BYTES = 512 * 1024
TAIL_STEPS = (256 * 1024, 1024 * 1024, 4 * 1024 * 1024)
MAX_TRANSCRIPTS = 200            # newest N per project; older ones are not listed
FIRST_PROMPT_CHARS = 240
PREVIEW_TURNS = 8
PREVIEW_CHARS = 600
DEFAULT_IDLE_DAYS = 14
SHORT_TRANSCRIPT_BYTES = 16 * 1024
CLEAR_CHAIN_WINDOW_S = 120
_BRANCH_TTL_S = 60.0

MARKS_FILE = "session_marks.jsonl"
MARK_OPS = ("stale", "unstale", "note")
_MARKED_BY = ("agent", "user")

_TS_RE = re.compile(rb'"timestamp":"([^"]+)"')
_BRANCH_RE = re.compile(rb'"gitBranch":"([^"]*)"')
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TAG_RE = re.compile(r"<(command-name|command-message|command-args)>(.*?)</\1>", re.S)
# A `!cmd` typed into Claude Code is stored as <bash-input>, its output as
# <bash-stdout>/<bash-stderr>; show the command, drop the output.
_BASH_IN_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
_BASH_OUT_RE = re.compile(r"<bash-(stdout|stderr)>.*?</bash-\1>", re.S)
_WS_RE = re.compile(r"\s+")
# Task labels C3 writes on its own (MCP start, Stop hook, budget checkpoint).
# A snapshot carrying one says nothing about the work, so it never becomes the
# row's note — the last prompt is more useful than "auto-snapshot on stop".
_MACHINE_TASK_RE = re.compile(
    r"^(|mcp server session|auto[-_ ].*|automated_restart|budget_restart.*)$", re.I)

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_branch_cache: dict[str, tuple[float, set]] = {}


def _project_lock(project) -> threading.Lock:
    """One lock per project: two readers of one cache serialize, readers of
    different projects (the Hub's all-projects view) run in parallel."""
    key = os.path.normcase(str(project))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# ── Where transcripts live ─────────────────────────────────────────────────

def claude_projects_root() -> Path:
    """``$CLAUDE_CONFIG_DIR/projects`` when set (Claude Code honours it), else
    ``~/.claude/projects``."""
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _bare(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def claude_transcript_dir(project) -> Path | None:
    """The transcript folder Claude Code uses for ``project``, or None.

    Exact slug first (every non-alphanumeric becomes ``-``), then the same slug
    with leading dashes stripped, then an exact bare-alphanumeric match. There
    is deliberately no "folder name contains the project name" fallback: a
    wrong folder here would list another project's sessions and resume them
    in the wrong place.
    """
    root = claude_projects_root()
    if not root.is_dir():
        return None
    project_str = str(Path(project).resolve())
    slug = re.sub(r"[^a-zA-Z0-9]", "-", project_str)
    for name in (slug, slug.lstrip("-")):
        cand = root / name
        if name and cand.is_dir():
            return cand
    target = _bare(project_str)
    try:
        for d in root.iterdir():
            if d.is_dir() and _bare(d.name) == target:
                return d
    except OSError:
        return None
    return None


def _same_dir(a: str, b) -> bool:
    try:
        ra, rb = os.path.realpath(a), os.path.realpath(str(b))
    except Exception:
        ra, rb = str(a), str(b)
    return os.path.normcase(os.path.normpath(ra)) == os.path.normcase(os.path.normpath(rb))


# ── Reading one transcript ─────────────────────────────────────────────────

def _clean(text: str, limit: int) -> str:
    text = _REMINDER_RE.sub(" ", text or "")
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _prompt_text(rec: dict, limit: int = FIRST_PROMPT_CHARS) -> str:
    """The human prompt in a transcript row, or '' when the row is not one
    (tool results, meta rows, local command output, sidechains)."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return ""
    msg = rec.get("message") or {}
    if not isinstance(msg, dict) or msg.get("role", "user") != "user":
        return ""
    content = msg.get("content")
    if isinstance(content, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return ""
        content = " ".join(str(p.get("text") or "") for p in content
                           if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(content, str):
        return ""
    raw = _BASH_OUT_RE.sub("", content).strip()
    if not raw or raw.startswith("<local-command") or raw.startswith("Caveat:"):
        return ""
    raw = _BASH_IN_RE.sub(lambda m: "! " + m.group(1).strip(), raw)
    if raw.startswith("<command-"):
        parts = {m.group(1): m.group(2).strip() for m in _TAG_RE.finditer(raw)}
        name = parts.get("command-name", "")
        if not name:
            return ""
        raw = f"{name} {parts.get('command-args', '')}".strip()
    return _clean(raw, limit)


def _read_range(path: Path, start: int, length: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(max(0, start))
        return fh.read(max(0, length))


def _lines(block: bytes, *, drop_first: bool, drop_last: bool) -> list[bytes]:
    lines = block.split(b"\n")
    if drop_first and lines:
        lines = lines[1:]
    if drop_last and lines:
        lines = lines[:-1]
    return [ln for ln in lines if ln.strip()]


def _loads(raw: bytes):
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _typed(raw: bytes, kind: str) -> dict:
    rec = _loads(raw)
    return rec if rec is not None and rec.get("type") == kind else {}


def scan_transcript(path) -> dict:
    """Orienting metadata for one transcript, from its head and tail only."""
    path = Path(path)
    st = path.stat()
    meta = {
        "id": path.stem, "size": st.st_size, "mtime": st.st_mtime,
        "started": "", "last_ts": "", "cwd": "", "branch": "", "version": "",
        "first_prompt": "", "last_prompt": "", "title": "", "title_source": "",
        "bridge": "", "prompts_in_head": 0,
    }
    first_title = ""
    # Head: the first real prompt usually sits in the first few KB, but a
    # pasted wall of text can push it further, so widen once before giving up.
    for limit in (HEAD_BYTES, HEAD_MAX_BYTES):
        head = _read_range(path, 0, min(limit, st.st_size))
        truncated = len(head) < st.st_size
        for raw in _lines(head, drop_first=False, drop_last=truncated):
            rec = _loads(raw)
            if rec is None:
                continue
            if not meta["started"] and rec.get("timestamp"):
                meta["started"] = str(rec["timestamp"])
            if not meta["cwd"] and rec.get("cwd"):
                meta["cwd"] = str(rec["cwd"])
            if not meta["version"] and rec.get("version"):
                meta["version"] = str(rec["version"])
            if not meta["branch"] and rec.get("gitBranch"):
                meta["branch"] = str(rec["gitBranch"])
            if rec.get("type") == "ai-title" and not first_title:
                first_title = str(rec.get("aiTitle") or "")
            if rec.get("type") == "bridge-session" and not meta["bridge"]:
                meta["bridge"] = str(rec.get("bridgeSessionId") or "")
            text = _prompt_text(rec)
            if text:
                meta["prompts_in_head"] += 1
                if not meta["first_prompt"]:
                    meta["first_prompt"] = text
        if meta["first_prompt"] or not truncated:
            break

    # Tail: newest values win, so walk backwards and stop once the title is
    # found. A title can sit behind megabytes of tool output; widen stepwise.
    custom = ai = summary = last_prompt = bridge = last_ts = branch = ""
    for step in TAIL_STEPS:
        start = max(0, st.st_size - step)
        tail = _read_range(path, start, st.st_size - start)
        for raw in reversed(_lines(tail, drop_first=start > 0, drop_last=False)):
            if not last_ts:
                m = _TS_RE.search(raw)
                if m:
                    last_ts = m.group(1).decode("utf-8", "replace")
            if not branch:
                m = _BRANCH_RE.search(raw)
                if m:
                    branch = m.group(1).decode("utf-8", "replace")
            # Markers are matched unescaped, so a tool result that merely
            # QUOTES a title row (its quotes are escaped) never matches; the
            # parsed type is checked as well.
            if not custom and b'"type":"custom-title"' in raw:
                rec = _typed(raw, "custom-title")
                custom = str(rec.get("customTitle") or rec.get("title") or "")
            elif not ai and b'"type":"ai-title"' in raw:
                ai = str(_typed(raw, "ai-title").get("aiTitle") or "")
            elif not summary and b'"type":"summary"' in raw:
                summary = str(_typed(raw, "summary").get("summary") or "")
            elif not last_prompt and b'"type":"last-prompt"' in raw:
                last_prompt = str(_typed(raw, "last-prompt").get("lastPrompt") or "")
            elif not bridge and b'"type":"bridge-session"' in raw:
                bridge = str(_typed(raw, "bridge-session").get("bridgeSessionId") or "")
            if (custom or ai) and last_prompt and last_ts and branch and bridge:
                break
        if custom or ai or start == 0:
            break

    meta["last_ts"] = last_ts
    if branch:
        meta["branch"] = branch
    if bridge:
        meta["bridge"] = bridge
    meta["last_prompt"] = _clean(last_prompt, FIRST_PROMPT_CHARS)
    for text, source in ((custom, "custom"), (ai, "ai"), (first_title, "ai"),
                         (summary, "summary")):
        if text.strip():
            meta["title"], meta["title_source"] = _clean(text, 120), source
            break
    if not meta["title"] and meta["first_prompt"]:
        meta["title"], meta["title_source"] = _clean(meta["first_prompt"], 120), "first_prompt"
    return meta


def remote_url(bridge_id: str) -> str | None:
    m = _BRIDGE_RE.match(str(bridge_id or ""))
    return _REMOTE_URL.format(m.group(1)) if m else None


# ── Cache ──────────────────────────────────────────────────────────────────

def _c3(project) -> Path:
    return Path(project) / ".c3"


def _cache_path(project) -> Path:
    return _c3(project) / "cache" / "session_catalog.json"


def _load_cache(project) -> dict:
    try:
        data = json.loads(_cache_path(project).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            for key in ("transcripts", "records", "snapshots"):
                data.setdefault(key, {})
            return data
    except Exception:
        pass
    return {"version": CACHE_VERSION, "transcripts": {}, "records": {}, "snapshots": {}}


def _save_cache(project, cache: dict) -> None:
    if not _c3(project).is_dir():
        return  # never create .c3 in a project C3 does not manage
    try:
        from services.atomic_json import write_json_atomic
        path = _cache_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, cache, indent=None, fsync=False)
    except Exception:
        pass  # a cold cache costs time, never correctness


def _cached(section: dict, path: Path, build, dirty: list):
    """``build(path)`` unless ``section`` holds a result for this size+mtime."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = path.name
    hit = section.get(key)
    if isinstance(hit, dict) and hit.get("_size") == st.st_size and hit.get("_mtime") == st.st_mtime:
        return hit
    try:
        value = build(path)
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    value["_size"], value["_mtime"] = st.st_size, st.st_mtime
    section[key] = value
    dirty.append(key)
    return value


# ── The other sources ──────────────────────────────────────────────────────

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _record(path: Path) -> dict:
    s = _read_json(path) or {}
    decisions = [str(d.get("decision") or "") for d in (s.get("decisions") or [])
                 if isinstance(d, dict) and d.get("decision")]
    return {
        "id": str(s.get("id") or ""), "host_session_id": str(s.get("host_session_id") or ""),
        "started": str(s.get("started") or ""), "ended": str(s.get("ended") or ""),
        "description": str(s.get("description") or ""), "summary": str(s.get("summary") or ""),
        "source_system": str(s.get("source_system") or ""),
        "source_ide": str(s.get("source_ide") or ""),
        "branch": str(((s.get("git") or {}) if isinstance(s.get("git"), dict) else {}).get("branch") or ""),
        "decisions": decisions[-10:], "decision_count": len(decisions),
    }


def _snapshot(path: Path) -> dict:
    s = _read_json(path) or {}
    return {"session_id": str(s.get("session_id") or ""), "created": str(s.get("created") or ""),
            "task": _clean(str(s.get("task_description") or ""), 400),
            "next_steps": _clean(str(s.get("custom_notes") or ""), 400)}


def _links(project) -> list[dict]:
    """``.c3/host_sessions/<provider>/*.link.json`` — host id ↔ C3 id."""
    out = []
    base = _c3(project) / "host_sessions"
    try:
        for f in base.glob("*/*.link.json"):
            data = _read_json(f)
            if isinstance(data, dict) and data.get("host_session_id") and data.get("session_id"):
                out.append(data)
    except OSError:
        pass
    return out


def _tasks(project) -> list[dict]:
    doc = _read_json(_c3(project) / "pm" / "pm.json") or {}
    return [t for t in doc.get("tasks", []) if isinstance(t, dict)
            and t.get("lifecycle", "active") == "active"]


def _read_marks(project) -> list[dict]:
    path = _c3(project) / MARKS_FILE
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("id") and row.get("op") in MARK_OPS:
                    out.append(row)
    except OSError:
        pass
    return out


def _fold_marks(rows: list[dict]) -> dict[str, dict]:
    """Last-wins per id, kept separately for the stale flag and the note."""
    state: dict[str, dict] = {}
    for row in rows:
        cur = state.setdefault(row["id"], {"stale": None, "note": None})
        if row["op"] == "stale":
            cur["stale"] = {"by": row.get("by", ""), "reason": row.get("reason", ""),
                            "successor": row.get("successor", ""), "at": row.get("ts", "")}
        elif row["op"] == "unstale":
            cur["stale"] = None
        elif row["op"] == "note":
            cur["note"] = {"summary": row.get("summary", ""), "next_steps": row.get("next_steps", ""),
                           "at": row.get("ts", ""), "by": row.get("by", ""), "source": "mark"}
    return state


def _lifecycle(project) -> tuple[set, dict, dict]:
    """(ended_by_clear, predecessor, successor) from session_open/session_end
    activity rows. A ``session_end(reason=clear)`` followed within
    ``CLEAR_CHAIN_WINDOW_S`` by ``session_open(start_source=clear)`` is one
    conversation continued in a fresh session."""
    path = _c3(project) / "activity_log.jsonl"
    ends, opens = [], []
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"session_end"' not in raw and b'"session_open"' not in raw:
                    continue
                rec = _loads(raw)
                if not rec:
                    continue
                if rec.get("type") == "session_end" and rec.get("reason") == "clear":
                    ends.append(rec)
                elif rec.get("type") == "session_open" and rec.get("start_source") == "clear":
                    opens.append(rec)
    except OSError:
        pass
    cleared = {str(e.get("host_session_id") or "") for e in ends} - {""}
    pred: dict[str, str] = {}
    succ: dict[str, str] = {}
    for o in opens:
        b = str(o.get("host_session_id") or "")
        tb = _epoch(o.get("timestamp"))
        best = None
        for e in ends:
            a = str(e.get("host_session_id") or "")
            ta = _epoch(e.get("timestamp"))
            if a and a != b and tb and ta and 0 <= tb - ta <= CLEAR_CHAIN_WINDOW_S:
                if best is None or ta > best[1]:
                    best = (a, ta)
        if b and best:
            pred[b], succ[best[0]] = best[0], b
    return cleared, pred, succ


def _epoch(iso) -> float:
    if not iso:
        return 0.0
    try:
        text = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch else ""


def _local_branches(project) -> set | None:
    """Local branch names, cached briefly; None when git cannot say."""
    key = str(project)
    now = time.monotonic()
    hit = _branch_cache.get(key)
    if hit and now - hit[0] < _BRANCH_TTL_S:
        return hit[1]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        res = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=key, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=5, **kwargs)
        branches = ({ln.strip() for ln in res.stdout.splitlines() if ln.strip()}
                    if res.returncode == 0 else None)
    except Exception:
        branches = None
    _branch_cache[key] = (now, branches)
    return branches


def _idle_days(project) -> int:
    cfg = _read_json(_c3(project) / "config.json") or {}
    try:
        return max(1, int(((cfg.get("sessions") or {}) if isinstance(cfg.get("sessions"), dict)
                           else {}).get("idle_days", DEFAULT_IDLE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_IDLE_DAYS


def _provider(system: str) -> str:
    s = (system or "").lower()
    if s in ("claude", "claude-code", ""):
        return "claude"
    if s in ("gemini", "antigravity"):
        return "gemini"
    return s


# ── Building rows ──────────────────────────────────────────────────────────

def _transcript_files(tdir: Path | None, limit: int | None = MAX_TRANSCRIPTS) -> list[Path]:
    """UUID-named transcripts, newest first (``limit=None``: all of them)."""
    if tdir is None:
        return []
    stamped = []
    try:
        for f in tdir.glob("*.jsonl"):
            if UUID_RE.match(f.stem):
                try:
                    stamped.append((f.stat().st_mtime, f))
                except OSError:
                    continue
    except OSError:
        return []
    stamped.sort(key=lambda pair: pair[0], reverse=True)
    files = [f for _, f in stamped]
    return files if limit is None else files[:limit]


def _find_elsewhere(host_id: str) -> Path | None:
    """A Claude transcript for ``host_id`` in ANY project folder — a session
    run from a worktree lives under the worktree's slug, not the project's."""
    if not UUID_RE.match(host_id or ""):
        return None
    root = claude_projects_root()
    try:
        for d in root.iterdir():
            cand = d / f"{host_id}.jsonl"
            if cand.is_file():
                return cand
    except OSError:
        return None
    return None


def _build(project, name: str = "") -> tuple[list[dict], dict]:
    """Every row for one project (unfiltered) plus a lookup context."""
    project = Path(project)
    name = name or project.name
    with _project_lock(project):
        cache = _load_cache(project)
        dirty: list = []
        tdir = claude_transcript_dir(project)
        metas = {}
        for f in _transcript_files(tdir):
            meta = _cached(cache["transcripts"], f, scan_transcript, dirty)
            if not meta:
                continue
            if meta.get("cwd") and not _same_dir(meta["cwd"], project):
                continue  # a folder that only looks like ours
            metas[meta["id"]] = dict(meta, _path=str(f))
        records = []
        sdir = _c3(project) / "sessions"
        if sdir.is_dir():
            for f in sorted(sdir.glob("session_*.json"), reverse=True):
                rec = _cached(cache["records"], f, _record, dirty)
                if rec and rec.get("id"):
                    records.append(rec)
        snaps = []
        pdir = _c3(project) / "snapshots"
        if pdir.is_dir():
            for f in sorted(pdir.glob("snap_*.json"), reverse=True):
                snap = _cached(cache["snapshots"], f, _snapshot, dirty)
                if snap and snap.get("session_id"):
                    snaps.append(snap)
        if dirty:
            _save_cache(project, cache)

    # C3 id → host id, from every place that states it.
    c3_to_host: dict[str, str] = {}
    for link in _links(project):
        c3_to_host[str(link["session_id"])] = str(link["host_session_id"])
    for rec in records:
        if rec["host_session_id"]:
            c3_to_host[rec["id"]] = rec["host_session_id"]
    try:
        from services.session_live import live_sessions
        live_rows = live_sessions(project, prune=False)
    except Exception:
        live_rows = []
    live_ids = set()
    for hb in live_rows:
        host, c3id = str(hb.get("host_session_id") or ""), str(hb.get("session_id") or "")
        if host:
            live_ids.add(host)
            if c3id:
                c3_to_host[c3id] = host
        elif c3id:
            live_ids.add(c3id)

    def row_id_for(c3id: str) -> str:
        return c3_to_host.get(c3id, c3id)

    by_row_records: dict[str, list] = {}
    for rec in records:
        by_row_records.setdefault(row_id_for(rec["id"]), []).append(rec)
    by_row_snaps: dict[str, list] = {}
    for snap in snaps:
        by_row_snaps.setdefault(row_id_for(snap["session_id"]), []).append(snap)
    by_row_tasks: dict[str, list] = {}
    for task in _tasks(project):
        keys = set()
        if task.get("origin_session"):
            keys.add(row_id_for(str(task["origin_session"])))
        for link in task.get("links") or []:
            if isinstance(link, dict) and link.get("type") == "session" and link.get("ref"):
                keys.add(str(link["ref"]))
        for key in keys:
            by_row_tasks.setdefault(key, []).append(
                {"id": task.get("id", ""), "title": task.get("title", ""),
                 "status": task.get("status", "")})

    # Rows that exist only as C3 records (other hosts, or a Claude session
    # whose transcript is not in this project's folder).
    for rid, recs in by_row_records.items():
        if rid in metas:
            continue
        rec0 = recs[0]
        if _provider(rec0["source_system"]) == "claude" and UUID_RE.match(rid):
            found = _find_elsewhere(rid)
            if found is not None:
                try:
                    metas[rid] = dict(scan_transcript(found), _path=str(found), _elsewhere=True)
                    continue
                except Exception:
                    pass
        metas[rid] = {"id": rid, "_c3_only": True, "provider": _provider(rec0["source_system"])}

    marks = _fold_marks(_read_marks(project))
    cleared, pred, succ = _lifecycle(project)
    idle_days = _idle_days(project)
    now = time.time()
    branches = None
    rows = []
    for rid, meta in metas.items():
        recs = by_row_records.get(rid, [])
        c3_only = bool(meta.get("_c3_only"))
        provider = meta.get("provider") or "claude"
        started = meta.get("started") or min((r["started"] for r in recs if r["started"]), default="")
        last_epoch = max(_epoch(meta.get("last_ts")), float(meta.get("mtime") or 0),
                         max((_epoch(r["ended"] or r["started"]) for r in recs), default=0.0))
        title, title_source = meta.get("title", ""), meta.get("title_source", "")
        if not title:
            for rec in recs:
                text = rec["description"] if rec["description"] not in ("", "MCP server session") \
                    else rec["summary"]
                if text:
                    title, title_source = _clean(text, 120), "c3"
                    break
        branch = meta.get("branch") or next((r["branch"] for r in recs if r["branch"]), "")
        mark_state = marks.get(rid, {})
        note = mark_state.get("note")
        rsnaps = sorted(by_row_snaps.get(rid, []), key=lambda s: s["created"], reverse=True)
        told = [s for s in rsnaps if not _MACHINE_TASK_RE.match(s["task"].strip())]
        if not note and told:
            note = {"summary": told[0]["task"], "next_steps": told[0]["next_steps"],
                    "at": told[0]["created"], "by": "agent", "source": "snapshot"}
        live = rid in live_ids
        stale = mark_state.get("stale")
        hints = []
        if not live and not stale:
            idle = (now - last_epoch) / 86400 if last_epoch else 0
            if idle >= idle_days:
                hints.append(f"idle {int(idle)}d")
            if rid in cleared:
                hints.append("ended by /clear")
            if branch and branch not in ("HEAD", ""):
                if branches is None:
                    branches = _local_branches(project) or set()
                if branches and branch not in branches:
                    hints.append("branch gone")
            if not c3_only and int(meta.get("size") or 0) < SHORT_TRANSCRIPT_BYTES:
                hints.append("short")
        successor = (stale or {}).get("successor") or succ.get(rid, "")
        rows.append({
            "id": rid, "short": rid[:8], "provider": provider,
            "project": {"name": name, "path": str(project)},
            "title": title or "(untitled)", "title_source": title_source or "none",
            "first_prompt": meta.get("first_prompt", ""), "last_prompt": meta.get("last_prompt", ""),
            "started": started, "last_active": _iso(last_epoch), "branch": branch,
            "live": live, "stale": stale, "hints": hints, "note": note,
            "links": {
                "c3_sessions": [r["id"] for r in recs],
                "snapshots": len(rsnaps), "tasks": len(by_row_tasks.get(rid, [])),
                "decisions": sum(r["decision_count"] for r in recs),
                "predecessor": pred.get(rid, ""), "successor": successor,
            },
            "resume": _resume_view(rid, provider, meta, live, project),
        })
    rows.sort(key=lambda r: r["last_active"], reverse=True)
    ctx = {"metas": metas, "records": by_row_records, "snaps": by_row_snaps,
           "tasks": by_row_tasks}
    return rows, ctx


def _resume_view(rid: str, provider: str, meta: dict, live: bool, project) -> dict:
    command = f"claude --resume {rid}" if provider == "claude" and UUID_RE.match(rid) else ""
    why = ""
    if provider != "claude":
        why = "resume is only wired for Claude Code sessions so far"
    elif meta.get("_c3_only") or not UUID_RE.match(rid):
        why = "no transcript found for this session"
    elif live:
        why = "this session is open right now; continue it there"
    cwd = meta.get("cwd") or str(project)
    return {"command": command, "cwd": cwd, "remote_url": remote_url(meta.get("bridge", "")),
            "can_launch": not why, "why_not": why}


# ── Public API ─────────────────────────────────────────────────────────────

def _matches(row: dict, q: str) -> bool:
    if not q:
        return True
    hay = " ".join([row["id"], row["title"], row["first_prompt"], row["last_prompt"],
                    row["branch"], ((row.get("note") or {}).get("summary") or ""),
                    ((row.get("stale") or {}).get("reason") or "")]).lower()
    return all(word in hay for word in q.lower().split())


def filter_rows(rows: list[dict], *, stale: str = "hide", q: str = "",
                limit: int = 50, before: str = "") -> dict:
    """Apply the list filters to already-built rows (newest first)."""
    stale = (stale or "hide").lower()
    out = []
    for row in rows:
        if stale == "hide" and row["stale"]:
            continue
        if stale == "only" and not row["stale"]:
            continue
        if stale == "likely" and (row["stale"] or not row["hints"]):
            continue
        if before and row["last_active"] >= before:
            continue
        if not _matches(row, q):
            continue
        out.append(row)
    limit = max(1, min(int(limit or 50), 500))
    page = out[:limit]
    return {"sessions": page,
            "next_before": page[-1]["last_active"] if len(out) > limit and page else ""}


def list_sessions(project, *, name: str = "", stale: str = "hide", q: str = "",
                  limit: int = 50, before: str = "") -> dict:
    """``{"sessions": [row...], "next_before": iso|""}`` for one project.

    stale: ``hide`` (default), ``only``, ``likely`` (unmarked rows with hints)
    or ``all``.
    """
    rows, _ = _build(project, name)
    return filter_rows(rows, stale=stale, q=q, limit=limit, before=before)


def list_many(projects: list[dict], *, stale: str = "hide", q: str = "",
              limit: int = 50, before: str = "", workers: int = 8) -> dict:
    """Rows from several projects (``[{name, path}]``) merged newest first.

    Projects build in parallel; one unreadable project is reported under
    ``errors`` and never empties the list.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(p):
        path = str(p.get("path") or "")
        if not path or not (Path(path) / ".c3").is_dir():
            return path, [], ""
        try:
            rows, _ = _build(path, str(p.get("name") or ""))
            return path, rows, ""
        except Exception as exc:  # pragma: no cover - defensive
            return path, [], str(exc)

    merged, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for path, rows, err in pool.map(one, projects):
            merged.extend(rows)
            if err:
                errors.append({"path": path, "error": err})
    merged.sort(key=lambda r: r["last_active"], reverse=True)
    result = filter_rows(merged, stale=stale, q=q, limit=limit, before=before)
    result["errors"] = errors
    return result


def resolve_id(project, ref: str, rows: list[dict] | None = None) -> tuple[str, str]:
    """``(id, "")`` for an exact id or a unique prefix of 8+ chars, else
    ``("", reason)``."""
    ref = str(ref or "").strip()
    if not ref:
        return "", "a session id is required"
    if rows is None:
        rows, _ = _build(project)
    ids = [r["id"] for r in rows]
    if ref in ids:
        return ref, ""
    if len(ref) < 8:
        return "", f"'{ref}' is too short to match a session; use 8+ characters"
    hits = [i for i in ids if i.startswith(ref)]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return "", f"no session in this project matches '{ref}'"
    return "", f"'{ref}' matches {len(hits)} sessions; use more characters"


def get_session(project, ref: str, *, name: str = "") -> dict:
    """One row plus decisions, snapshot, tasks and a transcript preview."""
    rows, ctx = _build(project, name)
    rid, err = resolve_id(project, ref, rows)
    if err:
        return {"error": err}
    row = next(r for r in rows if r["id"] == rid)
    decisions = []
    for rec in ctx["records"].get(rid, []):
        decisions.extend(rec["decisions"])
    snaps = sorted(ctx["snaps"].get(rid, []), key=lambda s: s["created"], reverse=True)
    snaps = [s for s in snaps if not _MACHINE_TASK_RE.match(s["task"].strip())] or snaps
    meta = ctx["metas"].get(rid, {})
    preview = []
    if meta.get("_path"):
        try:
            preview = transcript_preview(Path(meta["_path"]))
        except Exception:
            preview = []
    return dict(row, decisions=decisions[-10:],
                snapshot=({"task": snaps[0]["task"], "next_steps": snaps[0]["next_steps"],
                           "at": snaps[0]["created"]} if snaps else None),
                tasks=ctx["tasks"].get(rid, []), preview=preview)


def _tool_name(name: str) -> str:
    """``mcp__c3__c3_shell`` -> ``c3_shell``; built-in tool names unchanged."""
    parts = str(name or "tool").split("__")
    return parts[-1] if len(parts) >= 3 and parts[0] == "mcp" else (name or "tool")


def _render_parts(parts: list[tuple[str, str]]) -> str:
    """Text parts verbatim; each run of tool calls as one line,
    ``⚙ PowerShell ×5 · Read ×2``."""
    lines: list[str] = []
    run: dict[str, int] = {}

    def flush():
        if run:
            names = [f"{n} ×{c}" if c > 1 else n for n, c in run.items()]
            more = f" · +{len(names) - 6} more" if len(names) > 6 else ""
            lines.append("⚙ " + " · ".join(names[:6]) + more)
            run.clear()

    for kind, value in parts:
        if kind == "tool":
            run[value] = run.get(value, 0) + 1
        else:
            flush()
            lines.append(value)
    flush()
    return "\n".join(lines)


def transcript_preview(path: Path, turns: int = PREVIEW_TURNS) -> list[dict]:
    """The last ``turns`` human/assistant turns as short text. Each run of
    tool calls is one ``⚙`` line and system reminders are removed; a long
    agent turn keeps its END (the answer), a long prompt its start."""
    size = path.stat().st_size
    items: list[dict] = []
    for step in TAIL_STEPS:
        start = max(0, size - step)
        tail = _read_range(path, start, size - start)
        items = []
        for raw in _lines(tail, drop_first=start > 0, drop_last=False):
            rec = _loads(raw)
            if not rec or rec.get("isSidechain"):
                continue
            role, parts = "", []
            if rec.get("type") == "user":
                text = _prompt_text(rec, PREVIEW_CHARS)
                if text:
                    role, parts = "user", [("text", text)]
            elif rec.get("type") == "assistant":
                content = (rec.get("message") or {}).get("content")
                for p in content if isinstance(content, list) else []:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and str(p.get("text") or "").strip():
                        parts.append(("text", str(p["text"]).strip()))
                    elif p.get("type") == "tool_use":
                        parts.append(("tool", _tool_name(p.get("name"))))
                if parts:
                    role = "assistant"
            if not role:
                continue
            ts = str(rec.get("timestamp") or "")
            if items and items[-1]["role"] == role:
                items[-1]["parts"].extend(parts)
                items[-1]["ts"] = ts or items[-1]["ts"]
            else:
                items.append({"role": role, "parts": list(parts), "ts": ts})
        if len([i for i in items if i["role"] == "user"]) >= turns // 2 or start == 0:
            break
    out = []
    for item in items[-turns:]:
        text = _REMINDER_RE.sub(" ", _render_parts(item["parts"])).strip()
        if len(text) > PREVIEW_CHARS:
            text = ("…" + text[-(PREVIEW_CHARS - 1):].lstrip() if item["role"] == "assistant"
                    else text[: PREVIEW_CHARS - 1].rstrip() + "…")
        out.append({"role": item["role"], "text": text, "ts": item["ts"]})
    return out


def overview(project, *, name: str = "") -> dict:
    """Cheap per-project counts for a picker: stats files, parses nothing but
    the newest transcript."""
    project = Path(project)
    files = _transcript_files(claude_transcript_dir(project), limit=None)
    marks = _fold_marks(_read_marks(project))
    stale_ids = {k for k, v in marks.items() if v.get("stale")}
    try:
        from services.session_live import live_sessions
        live = {str(h.get("host_session_id") or h.get("session_id") or "")
                for h in live_sessions(project, prune=False)} - {""}
    except Exception:
        live = set()
    idle_days = _idle_days(project)
    now = time.time()
    idle = 0
    last = 0.0
    for f in files:
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        last = max(last, mt)
        if f.stem not in stale_ids and f.stem not in live and (now - mt) / 86400 >= idle_days:
            idle += 1
    newest = None
    if files:
        with _project_lock(project):
            cache = _load_cache(project)
            dirty: list = []
            meta = _cached(cache["transcripts"], files[0], scan_transcript, dirty)
            if dirty:
                _save_cache(project, cache)
        if meta:
            newest = {"id": meta["id"], "title": meta.get("title") or "(untitled)"}
    return {"name": name or project.name, "path": str(project),
            "counts": {"total": len(files), "live": len(live),
                       "stale": len(stale_ids & {f.stem for f in files}), "idle": idle},
            "last_active": _iso(last), "newest": newest,
            "transcripts": bool(files)}


def mark(project, ref: str, op: str, *, reason: str = "", successor: str = "",
         summary: str = "", next_steps: str = "", by: str = "agent",
         by_session: str = "") -> dict:
    """Append one mark. Returns ``{"mark": row, "row": <updated row>}`` or
    ``{"error": reason}``."""
    op = str(op or "").strip().lower()
    if op not in MARK_OPS:
        return {"error": f"op must be one of {list(MARK_OPS)}", "status": 400}
    if not _c3(project).is_dir():
        return {"error": "C3 is not initialized in this project", "status": 409}
    by = by if by in _MARKED_BY else "agent"
    rows, _ = _build(project)
    rid, err = resolve_id(project, ref, rows)
    if err:
        return {"error": err, "status": 404}
    succ_id = ""
    if op == "stale" and str(successor or "").strip():
        succ_id, err = resolve_id(project, successor, rows)
        if err:
            return {"error": f"successor: {err}", "status": 404}
        if succ_id == rid:
            return {"error": "a session cannot supersede itself", "status": 400}
    if op == "note" and not (str(summary or "").strip() or str(next_steps or "").strip()):
        return {"error": "a note needs a summary or next steps", "status": 400}
    row = next(r for r in rows if r["id"] == rid)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "provider": row["provider"],
             "id": rid, "op": op, "by": by, "by_session": str(by_session or "")}
    if op == "stale":
        entry.update(reason=_clean(str(reason or ""), 400), successor=succ_id)
    elif op == "note":
        entry.update(summary=_clean(str(summary or ""), 600),
                     next_steps=_clean(str(next_steps or ""), 600))
    path = _c3(project) / MARKS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        return {"error": f"could not write {MARKS_FILE}: {exc}", "status": 500}
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(project)).log("session_mark", {
            "op": op, "host_session_id": rid, "by": by, "by_session": entry["by_session"],
            **({"reason": entry.get("reason", "")} if op == "stale" else {})})
    except Exception:
        pass
    rows, _ = _build(project)
    updated = next((r for r in rows if r["id"] == rid), row)
    return {"mark": entry, "row": updated, "warning": (
        "this session is live right now" if op == "stale" and updated["live"] else "")}


def resume_spec(project, ref: str) -> dict:
    """The fixed command that resumes ``ref``, or ``{"error", "status"}``.

    ``status`` is an HTTP-shaped hint: 404 unknown/unresumable id, 409 live.
    """
    rows, ctx = _build(project)
    rid, err = resolve_id(project, ref, rows)
    if err:
        return {"error": err, "status": 404}
    row = next(r for r in rows if r["id"] == rid)
    view = row["resume"]
    if not view["can_launch"]:
        return {"error": view["why_not"], "status": 409 if row["live"] else 404,
                "row": row}
    meta = ctx["metas"].get(rid, {})
    if not UUID_RE.match(rid) or not meta.get("_path") or not Path(meta["_path"]).is_file():
        return {"error": "no transcript found for this session", "status": 404}
    cwd = Path(view["cwd"])
    if not cwd.is_dir():
        cwd = Path(project)
    return {"id": rid, "argv": ["claude", "--resume", rid], "command": view["command"],
            "cwd": str(cwd), "row": row}
