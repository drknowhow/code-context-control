"""LLM-powered memory distillation: session logs → durable project facts.

Degradation chain per job:
  1. cloud  — OllamaBridge against Ollama Cloud (Sonnet-class model,
              Bearer key from config/env, or a signed-in local daemon
              proxying `-cloud` tags)
  2. local  — the runtime's OllamaClient with a small local model
  3. mechanical — give up on the LLM; the regex AutoMemory path already
              captured what it could, the job is marked done_degraded

Never routes through cli/tools/delegate.py: its resolve_model_name()
silently substitutes local models for cloud tags.

Distilled facts are written with source_quality='distilled' (cloud) or
'distilled_local' and never use 'auto:*' categories, which are subject
to rolling-window pruning.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from services.auto_memory import _jaccard, _merge_texts, _strip_private
from services.memory_queue import MemoryQueue
from services.ollama_bridge import OllamaBridge

log = logging.getLogger("c3.memory_distiller")

DISTILL_CATEGORIES = {"decision", "gotcha", "convention", "preference", "feedback", "context"}
MINING_CATEGORIES = {"preference", "feedback", "decision"}

_SYSTEM = (
    "You distill a coding session's logs into durable project memory for "
    "future AI sessions. Respond with STRICT JSON only — no prose, no "
    "markdown fences."
)

_DIGEST_RULES = """## Task
Extract 3-7 facts worth remembering in FUTURE sessions of this project.

RULES:
- SKIP anything derivable by reading the code or git history: file contents,
  function signatures, what was changed, test results. Git already remembers those.
- KEEP: WHY a choice was made, constraints discovered the hard way, environment
  quirks (OS/tooling gotchas), user preferences and corrections, approaches that
  FAILED and must not be retried, naming/style conventions the user enforced.
- Each fact must stand alone without session context, fit in {max_chars} characters,
  and name concrete files/tools/commands where relevant.
- If nothing durable was learned, return an empty list. Do not pad.

OUTPUT JSON SCHEMA:
{{"facts": [{{"fact": "<string>",
            "category": "decision|gotcha|convention|preference|context",
            "confidence": <float 0.0-1.0, how explicitly this was established>}}]}}"""

_MINING_RULES = """## Task
The excerpts above are USER messages from a coding conversation, each with
minimal assistant context. Extract ONLY signals originating from the USER:
- explicit corrections ("no, don't X", "that's wrong, use Y")
- standing preferences ("always/never ...", tone/format/workflow demands)
- decisions or approvals the user stated or confirmed
IGNORE: assistant proposals the user did not confirm, questions, one-off task
instructions with no future relevance, anything derivable from code or git.
Max 5 facts, each within {max_chars} characters. Empty list if none.

OUTPUT JSON SCHEMA:
{{"facts": [{{"fact": "<string>", "category": "preference|feedback|decision",
            "confidence": <float 0.0-1.0>}}]}}"""

_TURN_CHAR_CAP = 1200
_TURNS_CHAR_BUDGET = 12000
_MINING_CHAR_BUDGET = 8000
_EVENT_LINE_CAP = 160
_EVENTS_CHAR_BUDGET = 4000
_WINDOW_SLACK_SEC = 300


def _to_epoch(value) -> float:
    """Best-effort ISO string / epoch number → epoch seconds (0.0 on failure)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


class DistillerBreaker:
    """Per-tier circuit breaker: opens after N consecutive failures,
    half-opens after a cooldown. In-process state only — a stuck-open
    breaker resets with the server."""

    def __init__(self, threshold: int = 2, cooldown_sec: int = 300):
        self.threshold = max(1, int(threshold))
        self.cooldown = max(0, int(cooldown_sec))
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def allow(self, tier: str) -> bool:
        if self._failures.get(tier, 0) < self.threshold:
            return True
        return (time.time() - self._opened_at.get(tier, 0.0)) >= self.cooldown

    def record(self, tier: str, ok: bool):
        if ok:
            self._failures[tier] = 0
            self._opened_at.pop(tier, None)
        else:
            self._failures[tier] = self._failures.get(tier, 0) + 1
            if self._failures[tier] >= self.threshold:
                self._opened_at[tier] = time.time()


class MemoryDistiller:
    """Turns session material into durable facts via the LLM chain."""

    def __init__(self, project_path: str, memory_store, config: dict,
                 ollama_client=None, convo_store=None, activity_log=None):
        self.project = Path(project_path)
        self.memory = memory_store
        self.config = dict(config or {})
        self.ollama = ollama_client
        self.convo = convo_store
        self.activity = activity_log
        self.queue = MemoryQueue(project_path)
        self.breaker = DistillerBreaker(
            threshold=2, cooldown_sec=self.config.get("breaker_cooldown_sec", 300))
        self._cloud: OllamaBridge | None = None
        self._cloud_checked = False
        # Set on 401/402/403/429 — auth problems don't heal within a process,
        # so never re-probe the cloud tier once this trips.
        self._cloud_auth_dead = False

    # ── Cloud tier plumbing ─────────────────────────────────────────

    def _cloud_bridge(self) -> OllamaBridge | None:
        if self._cloud_checked:
            return self._cloud
        self._cloud_checked = True
        cfg = self.config
        base = (cfg.get("cloud_base_url") or "https://ollama.com").strip()
        # Resolution chain: explicit config value → env var → OS keyring
        # (the keyring entry is what the UIs and `c3 init` write).
        key = cfg.get("api_key") or os.environ.get(cfg.get("api_key_env") or "OLLAMA_API_KEY", "")
        if not key:
            try:
                from services.ollama_credentials import load_api_key
                key = load_api_key(base) or ""
            except Exception:
                key = ""
        local_proxy = "localhost" in base or "127.0.0.1" in base
        if not key and not local_proxy:
            return None  # no way to authenticate against the cloud endpoint
        self._cloud = OllamaBridge(
            base_url=base,
            model=cfg.get("cloud_model") or "glm-4.6:cloud",
            api_key=key,
            cache_dir=Path.home() / ".c3" / "cache" / "memory_llm",
        )
        return self._cloud

    def cloud_usable(self) -> bool:
        return (bool(self.config.get("cloud_enabled"))
                and not self._cloud_auth_dead
                and self._cloud_bridge() is not None)

    # ── Job lifecycle ───────────────────────────────────────────────

    def enqueue_session(self, session: dict | None) -> dict | None:
        """Durably enqueue a digest job for a finished session (fast, ~1 file write)."""
        if not session or not self.config.get("enabled", True):
            return None
        sid = session.get("id") or ""
        if not sid:
            return None
        payload = {
            "session_file": str(self.project / ".c3" / "sessions" / f"session_{sid}.json"),
            "started_at": session.get("started", ""),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "description": session.get("description", ""),
            # Compact inline snapshot so the job is self-contained even if
            # the session file is never written (crash before save_session).
            "decisions": list(session.get("decisions", []))[-8:],
            "files_touched": list(session.get("files_touched", []))[:15],
        }
        return self.queue.enqueue("session_digest", sid, payload)

    def process_job_safe(self, job: dict | None) -> dict | None:
        """Exception-proof wrapper — safe as a daemon-thread target."""
        try:
            return self.process_job(job)
        except Exception as exc:  # never take down shutdown or the agent loop
            log.warning("distill job failed: %s", exc)
            try:
                if job:
                    self._retry_or_degrade(job, str(exc))
            except Exception:
                pass
            return None

    def process_job(self, job: dict | None) -> dict | None:
        if not job or not self.config.get("enabled", True):
            return None
        material = self._gather(job)
        prompt = self._digest_prompt(material)
        text, tier = self._generate(prompt)
        if not text:
            self._retry_or_degrade(job, "no LLM tier available")
            return {"stored": 0, "tier": "mechanical", "session_id": job.get("session_id")}
        facts = self._parse(text, allowed=DISTILL_CATEGORIES)
        stored = self._store(facts, job.get("session_id", ""), tier)
        self.queue.mark(job, "done", tier_used=tier)
        log.info("distilled session %s via %s: %d facts stored",
                 job.get("session_id"), tier, stored)
        return {"stored": stored, "tier": tier, "session_id": job.get("session_id")}

    def _retry_or_degrade(self, job: dict, error: str):
        max_attempts = int(self.config.get("queue_max_attempts", 3))
        if int(job.get("attempts", 0)) + 1 >= max_attempts:
            # The regex AutoMemory path already captured what it could.
            self.queue.mark(job, "done_degraded", error=error, tier_used="mechanical")
        else:
            self.queue.mark(job, "pending", error=error)

    # ── Material gathering ──────────────────────────────────────────

    def _gather(self, job: dict) -> dict:
        payload = job.get("payload", {}) or {}
        session = {}
        session_file = payload.get("session_file", "")
        if session_file:
            try:
                session = json.loads(Path(session_file).read_text(encoding="utf-8"))
            except Exception:
                session = {}
        started = payload.get("started_at") or session.get("started", "")
        ended = payload.get("ended_at") or ""
        events = []
        if self.activity:
            try:
                events = self.activity.get_recent(
                    limit=80, since=started or None, until=ended or None)
            except Exception:
                events = []
        return {
            "session_id": job.get("session_id", ""),
            "description": payload.get("description") or session.get("description", ""),
            "decisions": session.get("decisions") or payload.get("decisions") or [],
            "files": session.get("files_touched") or payload.get("files_touched") or [],
            "events": events,
            "turns": self._turns_in_window(started, ended),
        }

    def _turns_in_window(self, started, ended) -> list[dict]:
        """Conversation turns whose timestamps overlap the session window.

        There is NO join key between a C3 session (timestamp id) and its
        IDE transcript (UUID filename) — the time-window join is the only
        available bridge. Turns are private-stripped here; the raw store
        is unscrubbed.
        """
        if not self.convo:
            return []
        start = _to_epoch(started)
        end = _to_epoch(ended) or time.time()
        if not start:
            return []
        lo, hi = start - _WINDOW_SLACK_SEC, end + _WINDOW_SLACK_SEC
        picked: list[dict] = []
        try:
            for meta in self.convo.list_sessions(limit=20):
                s0 = _to_epoch(meta.get("started", 0))
                s1 = _to_epoch(meta.get("ended", 0)) or s0
                if s1 < lo or s0 > hi:
                    continue
                for turn in self.convo.get_session(meta.get("session_id", "")):
                    ts = _to_epoch(turn.get("ts", 0))
                    if lo <= ts <= hi and (turn.get("text") or "").strip():
                        picked.append(turn)
        except Exception:
            return []
        picked.sort(key=lambda t: _to_epoch(t.get("ts", 0)))
        return self._budget_turns(picked)

    @staticmethod
    def _budget_turns(turns: list[dict]) -> list[dict]:
        """Fit turns into the char budget, keeping user turns preferentially."""
        users = [t for t in turns if t.get("role") == "user"]
        others = [t for t in turns if t.get("role") != "user"]
        kept, used = [], 0
        for turn in users + others:
            text = _strip_private(turn.get("text", ""))[:_TURN_CHAR_CAP]
            if not text.strip():
                continue
            if used + len(text) > _TURNS_CHAR_BUDGET:
                break
            kept.append({"role": turn.get("role", "?"), "ts": turn.get("ts", 0), "text": text})
            used += len(text)
        kept.sort(key=lambda t: _to_epoch(t.get("ts", 0)))
        return kept

    # ── Prompt building ─────────────────────────────────────────────

    def _digest_prompt(self, material: dict) -> str:
        parts = [f"Project: {self.project.name}",
                 f"Session: {material.get('session_id', '?')}"]
        if material.get("description"):
            parts.append(f"Description: {str(material['description'])[:200]}")
        files = material.get("files") or []
        if files:
            names = ", ".join(self._file_label(f) for f in files[:15])
            parts.append(f"Files touched: {names}")
        decisions = material.get("decisions") or []
        if decisions:
            parts.append("\n## Logged decisions")
            parts.extend(f"- {self._as_line(d, 200)}" for d in decisions[-8:])
        events = material.get("events") or []
        if events:
            parts.append("\n## Activity tail (tool calls)")
            used = 0
            for event in events:
                line = self._event_line(event)
                if not line:
                    continue
                if used + len(line) > _EVENTS_CHAR_BUDGET:
                    break
                parts.append(f"- {line}")
                used += len(line)
        turns = material.get("turns") or []
        if turns:
            parts.append("\n## Conversation excerpts (private content already removed)")
            for turn in turns:
                parts.append(f"{turn['role'].upper()}: {turn['text']}")
        parts.append("\n" + _DIGEST_RULES.format(
            max_chars=int(self.config.get("max_fact_chars", 300))))
        return "\n".join(parts)

    @staticmethod
    def _file_label(entry) -> str:
        if isinstance(entry, dict):
            return str(entry.get("path") or entry.get("file") or entry)[:120]
        return str(entry)[:120]

    @staticmethod
    def _as_line(entry, cap: int) -> str:
        if isinstance(entry, dict):
            entry = entry.get("decision") or entry.get("text") or json.dumps(entry, default=str)
        return " ".join(str(entry).split())[:cap]

    @staticmethod
    def _event_line(event: dict) -> str:
        etype = event.get("type", "")
        if etype == "tool_call":
            tool = event.get("tool", "?")
            summary = event.get("result_summary") or event.get("summary") or ""
            return f"{tool}: {' '.join(str(summary).split())}"[:_EVENT_LINE_CAP]
        if etype in ("decision", "plan", "file_change"):
            payload = {k: v for k, v in event.items() if k not in ("timestamp", "type")}
            return f"{etype}: {json.dumps(payload, default=str)}"[:_EVENT_LINE_CAP]
        return ""

    # ── Generation chain ────────────────────────────────────────────

    def _generate(self, prompt: str) -> tuple[str, str]:
        """Run the degradation chain. Returns (text, tier) — ('', 'mechanical') on failure."""
        cfg = self.config
        if self.cloud_usable() and self.breaker.allow("cloud"):
            bridge = self._cloud_bridge()
            status = bridge.check_auth(timeout=5)
            if status == "auth":
                self._cloud_auth_dead = True
                self.breaker.record("cloud", False)
                log.warning("cloud tier disabled for this process: auth/quota rejected")
            elif status == "down":
                self.breaker.record("cloud", False)
            else:
                text = bridge.generate(
                    prompt, system=_SYSTEM, model=cfg.get("cloud_model"),
                    temperature=0.2, max_tokens=1200, timeout=90)
                self.breaker.record("cloud", bool(text))
                if text:
                    return text, "cloud"
        if self.ollama and self.breaker.allow("local"):
            try:
                available = self.ollama.is_available()
            except Exception:
                available = False
            if available:
                try:
                    text = self.ollama.generate(
                        prompt=prompt, model=cfg.get("local_model") or "gemma3n:latest",
                        system=_SYSTEM, temperature=0.2, max_tokens=1200, timeout=90)
                except Exception:
                    text = None
                self.breaker.record("local", bool(text))
                if text:
                    return text, "local"
            else:
                self.breaker.record("local", False)
        return "", "mechanical"

    # ── Transcript mining ───────────────────────────────────────────

    def _hwm_path(self) -> Path:
        return self.queue.dir / "transcript_hwm.json"

    def _load_hwm(self) -> dict:
        try:
            data = json.loads(self._hwm_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_hwm(self, hwm: dict):
        tmp = self._hwm_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hwm, indent=2), encoding="utf-8")
        os.replace(tmp, self._hwm_path())

    def mine_transcripts(self, limit_sessions: int = 2) -> dict:
        """Extract user corrections/preferences/decisions from unmined turns.

        Tracks a per-conversation high-water mark (turn index) so each turn
        is mined at most once. The mark advances only AFTER facts are
        persisted — a crash re-mines the batch, and the Jaccard dedup in
        _store makes the re-run a near-no-op.
        """
        if not (self.convo and self.config.get("enabled", True)
                and self.config.get("transcript_mining_enabled", True)):
            return {"mined": 0, "stored": 0}
        hwm = self._load_hwm()
        mined = stored = 0
        try:
            sessions = self.convo.list_sessions(limit=30)
        except Exception:
            return {"mined": 0, "stored": 0}
        for meta in sessions:
            if mined >= limit_sessions:
                break
            sid = meta.get("session_id") or ""
            if not sid:
                continue
            last_idx = int((hwm.get(sid) or {}).get("last_turn_index", 0))
            try:
                turns = self.convo.get_session(sid)
            except Exception:
                continue
            if last_idx > len(turns):
                # Store file was rewritten shorter (corruption recovery) —
                # the old mark is meaningless, re-mine from the start.
                last_idx = 0
            fresh = turns[last_idx:]
            if not any(t.get("role") == "user" and (t.get("text") or "").strip()
                       for t in fresh):
                continue
            prompt = self._mining_prompt(fresh)
            if not prompt:
                continue
            text, tier = self._generate(prompt)
            if not text:
                break  # LLM chain down — retry the same sessions next cycle
            facts = self._parse(text, allowed=MINING_CATEGORIES)
            stored += self._store(facts, sid, tier, confidence_cap=0.8)
            mined += 1
            hwm[sid] = {"last_turn_index": len(turns),
                        "mined_at": datetime.now(timezone.utc).isoformat()}
            self._save_hwm(hwm)
        if mined:
            log.info("transcript mining: %d session(s), %d fact(s) stored", mined, stored)
        return {"mined": mined, "stored": stored}

    def _mining_prompt(self, turns: list[dict]) -> str:
        """User turns + one assistant neighbor each, private-stripped, budgeted."""
        keep_idx: set[int] = set()
        for i, turn in enumerate(turns):
            if turn.get("role") == "user" and (turn.get("text") or "").strip():
                keep_idx.add(i)
                if i + 1 < len(turns) and turns[i + 1].get("role") == "assistant":
                    keep_idx.add(i + 1)
        lines, used, has_user = [], 0, False
        for i in sorted(keep_idx):
            turn = turns[i]
            text = " ".join(_strip_private(turn.get("text", "")).split())[:_TURN_CHAR_CAP]
            if not text:
                continue
            role = str(turn.get("role", "?")).upper()
            line = f"[{i}] {role}: {text}"
            if used + len(line) > _MINING_CHAR_BUDGET:
                break
            lines.append(line)
            used += len(line)
            has_user = has_user or role == "USER"
        if not has_user:
            return ""
        return (f"Project: {self.project.name}\n\n"
                "## Conversation excerpts (private content already removed)\n"
                + "\n".join(lines) + "\n\n"
                + _MINING_RULES.format(max_chars=int(self.config.get("max_fact_chars", 300))))

    # ── Parsing & storage ───────────────────────────────────────────

    def _parse(self, raw: str, allowed: set[str]) -> list[dict]:
        text = (raw or "").strip()
        if not text:
            return []
        candidates: list = []
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                if isinstance(data, dict) and isinstance(data.get("facts"), list):
                    candidates = data["facts"]
            except json.JSONDecodeError:
                candidates = []
        if not candidates:
            # Salvage: pick out individual {"fact": ...} objects line-wise.
            for match in re.finditer(r'\{[^{}]*"fact"[^{}]*\}', text, re.DOTALL):
                try:
                    candidates.append(json.loads(match.group(0)))
                except json.JSONDecodeError:
                    pass
        max_facts = int(self.config.get("max_facts_per_session", 7))
        max_chars = int(self.config.get("max_fact_chars", 300))
        out = []
        for cand in candidates:
            if len(out) >= max_facts:
                break
            if not isinstance(cand, dict):
                continue
            fact = " ".join(str(cand.get("fact") or "").split())
            if len(fact) < 25:
                continue
            category = str(cand.get("category") or "context").strip().lower()
            if category not in allowed:
                category = "context" if "context" in allowed else next(iter(allowed))
            try:
                confidence = float(cand.get("confidence", 0.7))
            except (TypeError, ValueError):
                confidence = 0.7
            out.append({
                "fact": fact[:max_chars],
                "category": category,
                "confidence": max(0.0, min(1.0, confidence)),
            })
        return out

    def _store(self, facts: list[dict], session_id: str, tier: str,
               confidence_cap: float = 1.0) -> int:
        """Dedup against existing memory and persist. Returns facts stored."""
        source_quality = "distilled" if tier == "cloud" else "distilled_local"
        tier_weight = 1.0 if tier == "cloud" else 0.9
        stored = 0
        for item in facts:
            text = _strip_private(item["fact"]).strip()
            if len(text) < 25:
                continue
            try:
                hits = self.memory.recall(text, top_k=3)
            except Exception:
                hits = []
            duplicate = False
            for hit in hits:
                sim = _jaccard(text, hit.get("fact", ""))
                if sim >= 0.85:
                    duplicate = True
                    break
                if sim > 0.55:
                    # Merge into non-user facts only; a user-authored fact is
                    # authoritative and must never be rewritten by the LLM.
                    if hit.get("source_quality") != "user":
                        try:
                            self.memory.update_fact(
                                hit["id"], fact=_merge_texts(hit.get("fact", ""), text))
                        except Exception:
                            pass
                    duplicate = True
                    break
            if duplicate:
                continue
            confidence = round(min(confidence_cap, item["confidence"] * tier_weight), 3)
            self.memory.remember(text, item["category"], session_id,
                                 confidence=confidence, source_quality=source_quality)
            stored += 1
        return stored
