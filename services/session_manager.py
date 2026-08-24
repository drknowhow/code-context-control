"""
Session State Manager

Maintains compressed state between Claude Code sessions:
- Auto-saves decisions, changes, and context
- Generates optimized CLAUDE.md files
- Tracks token usage patterns for optimization suggestions
- Provides session continuity without re-explaining everything
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import count_tokens
from core.ide import detect_ide, load_ide_config
from services.git_context import GitContext
from services.telemetry import append_telemetry_record


class SessionManager:
    """Manages session state and generates CLAUDE.md files."""

    _SOURCE_BY_IDE = {
        "claude-code": "claude",
        "vscode": "vscode",
        "cursor": "cursor",
        "codex": "codex",
        "gemini": "gemini",
        "antigravity": "antigravity",
    }
    _IDE_BY_SOURCE = {
        "claude": "claude-code",
        "vscode": "vscode",
        "cursor": "cursor",
        "codex": "codex",
        "gemini": "gemini",
        "antigravity": "antigravity",
    }

    # Default context budget threshold (overridable via .c3/config.json "context_budget" key)
    DEFAULT_BUDGET_THRESHOLDS = {
        "threshold": 35000,
    }

    def __init__(self, project_path: str, data_dir: str = ".c3/sessions", ollama_client=None):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.current_session = None
        self.ollama_client = ollama_client
        self._git = None

        # Analytics now in a dedicated directory
        analytics_dir = self.project_path / ".c3" / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        self.analytics_file = analytics_dir / "analytics.json"

        # Migrate legacy analytics if it exists in the sessions folder
        legacy_analytics = self.data_dir / "analytics.json"
        if legacy_analytics.exists() and not self.analytics_file.exists():
            try:
                legacy_analytics.replace(self.analytics_file)
            except Exception:
                pass

        self._budget_file = self.project_path / ".c3" / "context_budget.json"
        self._budget_thresholds = self._load_budget_thresholds()

        # Per-tool-call accounting handoff. A tool call's structured token
        # data (record_tool_tokens) and its args-derived action are stashed
        # here between log_tool_call() and track_response(), which run
        # sequentially on the same handler thread. Thread-local keeps
        # concurrent tool calls from clobbering each other.
        self._tool_call_local = threading.local()

    @staticmethod
    def _normalize_source_system(source_system: Optional[str]) -> Optional[str]:
        """Normalize caller-system labels to canonical values."""
        if not source_system:
            return None
        raw = str(source_system).strip().lower()
        aliases = {
            "claude-code": "claude",
            "claude": "claude",
            "vscode": "vscode",
            "copilot": "vscode",
            "vs-code": "vscode",
            "cursor": "cursor",
            "codex": "codex",
            "openai-codex": "codex",
            "gemini": "gemini",
            "antigravity": "antigravity",
        }
        return aliases.get(raw, raw)

    def _detect_ide_name(self) -> str:
        """Infer current IDE from saved config, then project markers."""
        ide_name = load_ide_config(str(self.project_path))
        if ide_name == "claude-code":
            # If no explicit config exists, marker-based detection can refine this.
            ide_name = detect_ide(str(self.project_path))
        return ide_name or "claude-code"

    def _git_state(self) -> dict:
        """Current git branch/HEAD for stamping on the session (lazy, cached)."""
        try:
            if self._git is None:
                self._git = GitContext(self.project_path)
            st = self._git.state()
            return {
                "branch": st.get("branch"),
                "head_sha": st.get("head_sha", ""),
                "detached": st.get("detached", False),
            }
        except Exception:
            return {"branch": None, "head_sha": "", "detached": False}

    def start_session(self, description: str = "", source_system: Optional[str] = None) -> dict:
        """Start a new session."""
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        source_ide = self._detect_ide_name()
        normalized_source = self._normalize_source_system(source_system)
        if normalized_source:
            source_ide = self._IDE_BY_SOURCE.get(normalized_source, source_ide)
        source_system_value = normalized_source or self._SOURCE_BY_IDE.get(source_ide, "manual")
        self.current_session = {
            "id": session_id,
            "started": datetime.now(timezone.utc).isoformat(),
            "description": description,
            "source_system": source_system_value,
            "source_ide": source_ide,
            "git": self._git_state(),
            "decisions": [],
            "files_touched": [],
            "key_changes": [],
            "context_notes": [],
            "tool_calls": [],
            # estimated_saved_vs_full_read is an ESTIMATE against a
            # full-file-read baseline (what ingesting the whole source would
            # have cost) — not measured against real agent behavior.
            "token_usage": {"estimated_saved_vs_full_read": 0, "estimated_used": 0, "measured_ops": 0},
            "context_budget": {
                "response_tokens": 0,
                "call_count": 0,
                "peak_tokens": 0,
                "by_tool": {},
                "c3_calls": 0,
                "native_calls": 0,
                "auto_snapshot_fired": False,
            },
        }
        return {
            "session_id": session_id,
            "status": "started",
            "source_system": source_system_value,
            "source_ide": source_ide,
        }

    def log_decision(self, decision: str, reasoning: str = ""):
        """Log a decision made during the session."""
        if not self.current_session:
            self.start_session()
        self.current_session["decisions"].append({
            "decision": decision,
            "reasoning": reasoning,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    def log_file_change(self, filepath: str, change_type: str, summary: str = ""):
        """Log a file change."""
        if not self.current_session:
            self.start_session()
        self.current_session["files_touched"].append({
            "file": filepath,
            "type": change_type,  # created, modified, deleted
            "summary": summary,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    def log_tool_call(self, tool_name: str, args: dict, result_summary: str = ""):
        """Log an MCP tool invocation to the current session."""
        if not self.current_session:
            self.start_session()
        self.current_session["tool_calls"].append({
            "tool": tool_name,
            "args": args,
            "result_summary": result_summary[:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Stash the sub-action and target for the telemetry record emitted
        # later in track_response() (same handler thread).
        try:
            if isinstance(args, dict):
                action = str(args.get("action") or args.get("mode") or "")
                target = self._derive_target(args)
            else:
                action = ""
                target = ""
            self._tool_call_local.action = action
            self._tool_call_local.target = target
        except Exception:
            pass
        # Legacy fallback: heuristic savings estimate scraped from summaries
        # like "122288->5085tok". Skipped when the tool already reported
        # structured tokens via record_tool_tokens() for this call.
        self._update_token_usage_estimate(tool_name, result_summary)

    # ─── Token accounting (estimates vs full-read baseline) ──

    def record_tool_tokens(self, tool_name: str, raw_tokens: Optional[int] = None,
                           optimized_tokens: Optional[int] = None,
                           duration_ms: Optional[float] = None) -> None:
        """Structured per-call token accounting — the PRIMARY path.

        Tools call this (via cli.tools._helpers.finalize_with_tokens) with
        explicitly measured values instead of encoding them in summary
        strings for regex-scraping (_parse_summary_token_pair remains only
        as a fallback for tools not yet migrated).

        raw_tokens is a full-read baseline: the token cost of ingesting the
        entire un-optimized source. optimized_tokens is what C3 actually
        returned. The difference is accumulated as
        token_usage["estimated_saved_vs_full_read"] — an estimate against
        that counterfactual baseline, not a measurement of real agent
        behavior.
        """
        if not self.current_session:
            self.start_session()
        raw = self._coerce_token_count(raw_tokens)
        optimized = self._coerce_token_count(optimized_tokens)
        if raw is not None and optimized is not None:
            self._accumulate_token_usage(raw, optimized)
        # Hand off to track_response() for the telemetry record. Keyed by
        # tool name so a stale entry from an aborted call can't be
        # mis-attributed to a different tool on the same thread.
        self._tool_call_local.pending = {
            "tool": tool_name,
            "raw_tokens": raw,
            "optimized_tokens": optimized,
            "duration_ms": duration_ms,
            "source": "structured",
        }

    @staticmethod
    def _coerce_token_count(value) -> Optional[int]:
        """Coerce to a non-negative int, or None."""
        if value is None or isinstance(value, bool):
            return None
        try:
            n = int(value)
        except (ValueError, TypeError):
            return None
        return n if n >= 0 else None

    @staticmethod
    def _parse_summary_token_pair(result_summary: str) -> tuple[int, int] | None:
        """LEGACY fallback: parse "raw->optimized tok" out of a summary string.

        Returns (raw_tokens, optimized_tokens). Only used for tools that have
        not migrated to the structured record_tool_tokens() path.
        """
        if not result_summary:
            return None
        m = re.search(r"(\d+)\s*->\s*(\d+)\s*tok\b", result_summary, re.IGNORECASE)
        if not m:
            return None
        try:
            raw = int(m.group(1))
            optimized = int(m.group(2))
        except Exception:
            return None
        if raw < 0 or optimized < 0:
            return None
        return raw, optimized

    def _accumulate_token_usage(self, raw: int, optimized: int) -> None:
        """Accumulate one measured (raw, optimized) pair into token_usage.

        "estimated_saved_vs_full_read" is labeled as such because raw is a
        full-file-read baseline — a counterfactual, not observed behavior.
        """
        if not self.current_session:
            return
        token_usage = self.current_session.setdefault("token_usage", {})
        token_usage["estimated_saved_vs_full_read"] = (
            int(token_usage.get("estimated_saved_vs_full_read", 0)) + max(0, raw - optimized))
        token_usage["estimated_used"] = int(token_usage.get("estimated_used", 0)) + optimized
        token_usage["measured_ops"] = int(token_usage.get("measured_ops", 0)) + 1

    def _update_token_usage_estimate(self, tool_name: str, result_summary: str) -> None:
        """Fallback token accounting scraped from tool result summaries.

        Skipped when structured values were already recorded for this call
        via record_tool_tokens() (prevents double-counting).
        """
        if not self.current_session:
            return
        pending = getattr(self._tool_call_local, "pending", None)
        if pending is not None:
            if pending.get("tool") == tool_name and pending.get("source") == "structured":
                # Structured accounting already ran for this call.
                return
            # Stale entry (aborted call / different tool) — discard.
            self._tool_call_local.pending = None
        pair = self._parse_summary_token_pair(result_summary)
        if not pair:
            return
        raw, optimized = pair
        self._accumulate_token_usage(raw, optimized)
        # Still surface the parsed pair to telemetry, marked as
        # summary-scraped so downstream consumers know the provenance.
        self._tool_call_local.pending = {
            "tool": tool_name,
            "raw_tokens": raw,
            "optimized_tokens": optimized,
            "duration_ms": None,
            "source": "summary",
        }

    def reset_budget(self, initial_tokens: int = 0) -> None:
        """Reset the current session's context budget (typically after /clear)."""
        if not self.current_session:
            return
        budget = self.current_session["context_budget"]
        budget["response_tokens"] = initial_tokens
        budget["call_count"] = 0
        budget["peak_tokens"] = initial_tokens
        budget["by_tool"] = {}
        self._persist_budget()

    def is_over_budget(self) -> bool:
        """Return True if cumulative response tokens exceed the threshold."""
        if not self.current_session:
            return False
        total = self.current_session["context_budget"]["response_tokens"]
        return total >= self._budget_thresholds["threshold"]

    def add_context_note(self, note: str):
        """Add a context note for future sessions."""
        if not self.current_session:
            self.start_session()
        self.current_session["context_notes"].append(note)

    def save_session(self, summary: str = "") -> dict:
        """Save current session to disk."""
        if not self.current_session:
            return {"error": "No active session"}

        self.current_session["ended"] = datetime.now(timezone.utc).isoformat()

        # Determine summary: user provided > AI generated > Heuristic auto
        if summary:
            self.current_session["summary"] = summary
        elif self.ollama_client and self.ollama_client.is_available():
            self.current_session["summary"] = self._ai_summarize()
        else:
            self.current_session["summary"] = self._auto_summarize()

        # Compute duration
        try:
            started = datetime.fromisoformat(self.current_session["started"])
            ended = datetime.fromisoformat(self.current_session["ended"])
            duration_seconds = int((ended - started).total_seconds())
        except (ValueError, KeyError):
            duration_seconds = 0
        self.current_session["duration_seconds"] = duration_seconds
        self.current_session["duration"] = self._format_duration(duration_seconds)

        session_file = self.data_dir / f"session_{self.current_session['id']}.json"
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(self.current_session, f, indent=2)

        # Update analytics
        self._update_analytics()

        # Retention: cap .c3/sessions to the newest N files (older ones are
        # gzip-archived). Snapshot restore uses .c3/snapshots — unaffected.
        self._enforce_session_cap()

        result = {
            "session_id": self.current_session["id"],
            "saved_to": str(session_file),
            "decisions": len(self.current_session["decisions"]),
            "files_touched": len(self.current_session["files_touched"]),
            "duration_seconds": duration_seconds,
            "duration": self.current_session["duration"],
        }
        self.current_session = None
        return result

    def _enforce_session_cap(self) -> None:
        """Keep only the newest ``retention.sessions_max_files`` session files.

        Older session_*.json files are gzip-archived into .c3/archive (or
        deleted when ``sessions_archive`` is False). Failure-safe: retention
        problems never break session saving.
        """
        try:
            from services.retention import (
                archive_dir_for,
                cap_session_files,
                load_retention_config,
            )
            cfg = load_retention_config(str(self.project_path))
            if not cfg.get("enabled", True):
                return
            max_files = int(cfg.get("sessions_max_files", 50))
            if max_files <= 0:
                return
            dest = (archive_dir_for(str(self.project_path))
                    if cfg.get("sessions_archive", True) else None)
            cap_session_files(self.data_dir, max_files=max_files, archive_dir=dest)
        except Exception:
            pass

    def load_session(self, session_id: str = "latest") -> dict:
        """Load a previous session's context."""
        if session_id == "latest":
            sessions = sorted(self.data_dir.glob("session_*.json"), reverse=True)
            if not sessions:
                return {"error": "No previous sessions found"}
            session_file = sessions[0]
        else:
            session_file = self.data_dir / f"session_{session_id}.json"

        if not session_file.exists():
            return {"error": f"Session not found: {session_id}"}

        with open(session_file, encoding='utf-8') as f:
            session = json.load(f)

        return session

    def get_session_context(self, n_sessions: int = 3) -> str:
        """Get compressed context from recent sessions, ready for Claude."""
        sessions = sorted(self.data_dir.glob("session_*.json"), reverse=True)[:n_sessions]

        if not sessions:
            return "No previous session history."

        context_parts = ["# Session History (Compressed)\n"]

        for sf in sessions:
            with open(sf, encoding='utf-8') as f:
                s = json.load(f)

            part = f"## Session: {s.get('id', 'unknown')}\n"
            part += f"**When:** {s.get('started', 'unknown')[:10]}\n"
            if s.get('summary'):
                part += f"**Summary:** {s['summary']}\n"

            if s.get('decisions'):
                part += "**Decisions:**\n"
                for d in s['decisions'][:5]:
                    part += f"- {d['decision']}\n"

            if s.get('files_touched'):
                files = [f"{ft['type']}: {ft['file']}" for ft in s['files_touched'][:10]]
                part += f"**Files:** {', '.join(files)}\n"

            if s.get('context_notes'):
                part += "**Notes:**\n"
                for note in s['context_notes'][:3]:
                    part += f"- {note}\n"

            context_parts.append(part)

        return '\n'.join(context_parts)

    def list_sessions(self, n: int = 10) -> list:
        """List recent sessions."""
        sessions = sorted(self.data_dir.glob("session_*.json"), reverse=True)[:n]
        result = []
        for sf in sessions:
            with open(sf, encoding='utf-8') as f:
                s = json.load(f)
            # Compute duration if missing from stored session
            duration_seconds = s.get("duration_seconds", 0)
            duration = s.get("duration", "")
            if not duration and s.get("started") and s.get("ended"):
                try:
                    started = datetime.fromisoformat(s["started"])
                    ended = datetime.fromisoformat(s["ended"])
                    duration_seconds = int((ended - started).total_seconds())
                    duration = self._format_duration(duration_seconds)
                except (ValueError, KeyError):
                    pass

            budget = s.get("context_budget", {})
            result.append({
                "id": s.get("id"),
                "started": s.get("started", ""),
                "ended": s.get("ended", ""),
                "summary": s.get("summary", "")[:100],
                "description": (s.get("description", "") or "")[:80],
                "source_system": s.get("source_system", ""),
                "source_ide": s.get("source_ide", ""),
                "decisions": len(s.get("decisions", [])),
                "files": len(s.get("files_touched", [])),
                "tool_calls": len(s.get("tool_calls", [])),
                "context_notes": len(s.get("context_notes", [])),
                "duration": duration,
                "duration_seconds": duration_seconds,
                "response_tokens": budget.get("response_tokens", 0),
                "by_tool": budget.get("by_tool", {}),
            })
        return result

    def _repo_map_enabled(self) -> bool:
        """Live repo map is default-on; map.enabled=false restores the
        legacy embedded tree (mirrors ClaudeMdManager._repo_map_enabled)."""
        try:
            with open(self.project_path / ".c3" / "config.json",
                      encoding="utf-8") as f:
                cfg = json.load(f) or {}
            return bool(cfg.get("map", {}).get("enabled", True))
        except (OSError, ValueError):
            return True

    def generate_claude_md(self, include_sessions: bool = True) -> str:
        """Auto-generate token-efficient project context for instructions files."""
        parts = []

        # Project context: stable pointer to the live repo map when enabled
        # (v2.60.0), legacy embedded tree otherwise.
        parts.append("# Project Context\n")
        if self._repo_map_enabled():
            from services.claude_md import MAP_POINTER_BLOCK
            parts.append(MAP_POINTER_BLOCK)
        else:
            parts.append(self._scan_project_structure())

            # Tech stack detection
            parts.append("\n## Tech Stack\n")
            parts.append(self._detect_tech_stack())

            # Key files (conventional entry points + session history)
            key_files = self._detect_key_files()
            if key_files:
                parts.append("\n## Key Files\n")
                for kf in key_files[:5]:
                    parts.append(f"- `{kf['file']}` — {kf['reason']}")

        # Key facts from memory store (if wired in)
        memory_store = getattr(self, '_memory_store', None)
        if memory_store is not None:
            promoted = [
                f for f in getattr(memory_store, 'facts', [])
                if f.get("relevance_count", 0) >= 3
            ]
            if promoted:
                parts.append("\n## Key Facts (use c3_memory for more)\n")
                for f in promoted[:5]:
                    parts.append(f"- {f['fact'][:120]}")

        return '\n'.join(parts)

    def _detect_key_files(self) -> list:
        """Identify key files from session history and conventional entry points."""
        key_files = []
        seen = set()

        # Hot files from session history
        session_dir = self.project_path / ".c3" / "sessions"
        if session_dir.exists():
            file_counts = {}
            for sf in sorted(session_dir.glob("session_*.json"), reverse=True)[:20]:
                try:
                    with open(sf, encoding='utf-8') as f:
                        s = json.load(f)
                    for ft in s.get("files_touched", []):
                        fname = ft.get("file", "")
                        if fname:
                            file_counts[fname] = file_counts.get(fname, 0) + 1
                except Exception:
                    continue

            for fname, count in sorted(file_counts.items(), key=lambda x: -x[1])[:5]:
                if count >= 2 and fname not in seen:
                    key_files.append({"file": fname, "reason": f"edited in {count} sessions"})
                    seen.add(fname)

        # Conventional entry points
        entry_points = [
            ("main.py", "Python entry point"),
            ("app.py", "Application entry point"),
            ("index.ts", "TypeScript entry point"),
            ("index.js", "JavaScript entry point"),
            ("src/index.ts", "Source entry point"),
            ("src/index.js", "Source entry point"),
            ("src/main.ts", "Source entry point"),
            ("src/App.tsx", "React app root"),
            ("cli/mcp_server.py", "MCP server entry"),
        ]
        for filepath, reason in entry_points:
            if (self.project_path / filepath).exists() and filepath not in seen:
                key_files.append({"file": filepath, "reason": reason})
                seen.add(filepath)

        return key_files

    def save_claude_md(self, instructions_file: str = "CLAUDE.md", template: str = "") -> dict:
        """Generate and save instructions file to the project root.

        Args:
            instructions_file: Target filename, e.g. "CLAUDE.md",
                ".github/copilot-instructions.md", ".cursorrules".
            template: Optional static instructions to prepend.
        """
        auto_content = self.generate_claude_md()

        if template:
            # Merge template with auto-generated context
            content = template.rstrip() + "\n\n---\n\n" + auto_content.lstrip()
        else:
            content = auto_content

        # Wrap C3-generated content in the managed block so regenerating the
        # doc never clobbers user-written content outside it (mirrors global
        # ~/.claude/CLAUDE.md). Legacy files and bare user files are preserved.
        from services.claude_md import write_c3_instruction_doc

        output_path = self.project_path / instructions_file
        final = write_c3_instruction_doc(output_path, content,
                                         project_path=self.project_path)
        tokens = count_tokens(final)

        return {
            "path": str(output_path),
            "tokens": tokens,
            "status": "saved"
        }

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Return a human-readable duration string (e.g., '2m 34s', '1h 5m')."""
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {secs}s" if secs else f"{minutes}m"
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins}m" if mins else f"{hours}h"

    def _auto_summarize(self) -> str:
        """Auto-generate session summary."""
        parts = []
        if self.current_session.get("description"):
            parts.append(self.current_session["description"])

        files = self.current_session.get("files_touched", [])
        if files:
            parts.append(f"Touched {len(files)} files")

        decisions = self.current_session.get("decisions", [])
        if decisions:
            parts.append(f"Made {len(decisions)} decisions")
            if decisions:
                parts.append(decisions[-1]["decision"])  # Most recent

        tool_calls = self.current_session.get("tool_calls", [])
        if tool_calls:
            parts.append(f"{len(tool_calls)} tool calls")

        return ". ".join(parts) if parts else "Session with no recorded activity"

    def _ai_summarize(self, model: str = "gemma3n:latest") -> str:
        """Use local AI to generate a semantic summary of the session."""
        if not self.ollama_client:
            return self._auto_summarize()

        heuristic = self._auto_summarize()
        # Extract last few tool calls for context
        calls = self.current_session.get("tool_calls", [])
        history = []
        for c in calls[-10:]:
            history.append(f"Tool: {c.get('tool')} Args: {json.dumps(c.get('args', {}))} Result: {c.get('result_summary')}")

        prompt = (
            "Summarize this coding session in one clear, technical sentence. "
            "Focus on the 'why' and the primary outcome.\n\n"
            f"Heuristic Data: {heuristic}\n"
            "Recent Activity:\n" + "\n".join(history) + "\n\n"
            "Summary:"
        )

        try:
            summary = self.ollama_client.generate(
                prompt=prompt,
                model=model,
                system="You are a senior developer writing a git-style summary of a task.",
                max_tokens=64,
                temperature=0.3
            )
            return summary.strip() if summary else heuristic
        except Exception:
            return heuristic

    # Known extensionless files that are legitimate
    _KNOWN_NO_EXT = {
        'Makefile', 'Dockerfile', 'Procfile', 'Vagrantfile', 'Gemfile',
        'Rakefile', 'Guardfile', 'Brewfile', 'Justfile', 'Taskfile',
        'LICENSE', 'LICENCE', 'CODEOWNERS', 'CACHEDIR.TAG',
    }

    @staticmethod
    def _is_valid_filename(name: str) -> bool:
        """Filter out junk/artifact files that shouldn't appear in the project tree."""
        # Must have at least one alphanumeric character
        if not any(c.isalnum() for c in name):
            return False
        # Must start with a letter, digit, dot, or underscore
        if not name[0].isalpha() and name[0] not in '.0123456789_':
            return False
        # Reject names that are purely numeric (likely artifacts)
        if name.replace('.', '').replace('_', '').isdigit():
            return False
        # Reject names with shell/template metacharacters
        if any(c in name for c in '{}()$`'):
            return False
        # Reject extensionless files unless they're known valid names
        if '.' not in name and name not in SessionManager._KNOWN_NO_EXT:
            return False
        return True

    def _scan_project_structure(self) -> str:
        """Scan project and generate compressed structure.

        Prunes via the shared scanner rules (SKIP_DIRS + root .gitignore,
        including simple globs like *.egg-info/), so the tree shipped in
        instruction docs shows only what a clean distribution contains.
        """
        from services.scanner import make_dir_pruner
        pruned = make_dir_pruner(self.project_path)

        structure = ["```"]
        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = sorted(d for d in dirs if not pruned(d))
            level = len(Path(root).relative_to(self.project_path).parts)

            indent = "  " * level
            dirname = os.path.basename(root)
            valid_files = [f for f in sorted(files) if self._is_valid_filename(f)]

            if level >= 2:
                # Deep subdirs: show name + file count only, no individual file listing
                count_str = f" ({len(valid_files)} files)" if valid_files else ""
                structure.append(f"{indent}{dirname}/{count_str}")
                dirs[:] = []  # stop os.walk from recursing deeper
                continue

            structure.append(f"{indent}{dirname}/")
            for f in valid_files[:15]:
                structure.append(f"{indent}  {f}")
            if len(valid_files) > 15:
                structure.append(f"{indent}  ... +{len(valid_files) - 15} more")

        structure.append("```")
        return '\n'.join(structure)

    def _detect_tech_stack(self) -> str:
        """Detect tech stack from project files."""
        indicators = {
            "package.json": "Node.js",
            "tsconfig.json": "TypeScript",
            "requirements.txt": "Python",
            "Pipfile": "Python (Pipenv)",
            "pyproject.toml": "Python (Modern)",
            "Cargo.toml": "Rust",
            "go.mod": "Go",
            "DESCRIPTION": "R Package",
            "app.R": "R Shiny",
            "server.R": "R Shiny",
            "docker-compose.yml": "Docker",
            "Dockerfile": "Docker",
            ".env": "Environment vars",
            "next.config.js": "Next.js",
            "vite.config.ts": "Vite",
            "tailwind.config.js": "Tailwind CSS",
        }

        detected = []
        for filename, tech in indicators.items():
            if (self.project_path / filename).exists():
                detected.append(tech)

        # Check package.json for frameworks
        pkg_json = self.project_path / "package.json"
        if pkg_json.exists():
            try:
                with open(pkg_json, encoding='utf-8') as f:
                    pkg = json.load(f)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                for dep, tech in [("react", "React"), ("vue", "Vue"), ("angular", "Angular"),
                                  ("express", "Express"), ("fastify", "Fastify")]:
                    if dep in deps:
                        detected.append(tech)
            except Exception:
                pass

        return ', '.join(detected) if detected else "Could not auto-detect"

    def _detect_patterns(self) -> str:
        """Detect coding patterns and conventions."""
        patterns = []

        # Check for common patterns
        src_files = list(self.project_path.rglob("*.py"))[:20]
        src_files += list(self.project_path.rglob("*.ts"))[:20]
        src_files += list(self.project_path.rglob("*.js"))[:20]

        has_tests = any(self.project_path.rglob("test_*")) or any(self.project_path.rglob("*.test.*"))
        has_types = any(self.project_path.rglob("*.d.ts")) or any(self.project_path.rglob("types.*"))
        has_ci = (self.project_path / ".github" / "workflows").exists()

        if has_tests:
            patterns.append("Has test files")
        if has_types:
            patterns.append("Uses TypeScript types")
        if has_ci:
            patterns.append("Has CI/CD (GitHub Actions)")

        return '\n'.join(f"- {p}" for p in patterns) if patterns else "No patterns auto-detected"

    def _generate_shortcuts(self) -> str:
        """Generate token-efficient shortcut references."""
        shortcuts = [
            "When referencing this project, use these shortcuts:",
            "- `SRC` = main source directory",
            "- `TESTS` = test directory",
            "- `CFG` = configuration files",
            "- `DEPS` = dependencies (package.json / requirements.txt)",
        ]
        return '\n'.join(shortcuts)

    def _update_analytics(self):
        """Update session analytics."""
        analytics = {}
        if self.analytics_file.exists():
            try:
                with open(self.analytics_file, encoding='utf-8') as f:
                    analytics = json.load(f)
            except Exception:
                analytics = {}

        analytics["total_sessions"] = analytics.get("total_sessions", 0) + 1
        analytics["last_session"] = datetime.now(timezone.utc).isoformat()

        total_decisions = analytics.get("total_decisions", 0)
        total_decisions += len(self.current_session.get("decisions", []))
        analytics["total_decisions"] = total_decisions

        with open(self.analytics_file, 'w', encoding='utf-8') as f:
            json.dump(analytics, f, indent=2)

    def parse_claude_session_tokens(self, project_path: str = "", detailed: bool = False) -> dict:
        """Read Claude Code's session JSONL files for real token usage stats.

        Scopes to the current project directory to avoid summing tokens
        from unrelated projects.

        When detailed=True, also returns a 'sessions' list with per-session breakdown
        and timestamps.
        """
        import re
        home = Path.home()
        results = {"sessions_found": 0, "total_input_tokens": 0, "total_output_tokens": 0,
                    "cache_creation_tokens": 0, "cache_read_tokens": 0}
        if detailed:
            results["sessions"] = []
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.exists():
            return results

        proj_path = Path(project_path or self.project_path).resolve()
        proj_str = str(proj_path)

        # Claude Code slugifies paths by replacing every non-alphanumeric char with '-'.
        slug_primary = re.sub(r'[^a-zA-Z0-9]', '-', proj_str).lstrip('-')
        # Keep legacy variant (old C3 algorithm) for backwards compatibility.
        slug_legacy = proj_str.replace("\\", "--").replace("/", "--").replace(":", "").lstrip("-")

        candidate_dirs = []
        for slug in (slug_primary, slug_legacy):
            d = projects_dir / slug
            if d.is_dir() and d not in candidate_dirs:
                candidate_dirs.append(d)

        # If direct slug lookup misses, try a constrained name match rather than
        # summing all projects (which would mix unrelated token usage).
        if not candidate_dirs:
            project_name = proj_path.name.lower()
            for d in projects_dir.iterdir():
                if not d.is_dir():
                    continue
                name = d.name.lower()
                if name.endswith(f"--{project_name}") or f"--{project_name}--" in name:
                    candidate_dirs.append(d)

        for project_dir in candidate_dirs:
            for session_file in project_dir.glob("*.jsonl"):
                try:
                    with open(session_file, encoding="utf-8", errors="replace") as f:
                        found_usage = False
                        s_inp = s_out = s_cache_create = s_cache_read = 0
                        s_first_ts = s_last_ts = None
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            entry = json.loads(line)
                            # Usage is nested at entry.message.usage for assistant messages
                            msg = entry.get("message", {})
                            usage = msg.get("usage", {})
                            inp = usage.get("input_tokens", 0)
                            out = usage.get("output_tokens", 0)
                            cache_create = usage.get("cache_creation_input_tokens", 0)
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            if inp or out or cache_create or cache_read:
                                # total_input_tokens = non-cached + cache_creation + cache_read
                                # This matches Claude Code's reported token usage
                                results["total_input_tokens"] += inp + cache_create + cache_read
                                results["total_output_tokens"] += out
                                results["cache_creation_tokens"] += cache_create
                                results["cache_read_tokens"] += cache_read
                                found_usage = True
                                if detailed:
                                    s_inp += inp + cache_create + cache_read
                                    s_out += out
                                    s_cache_create += cache_create
                                    s_cache_read += cache_read
                            if detailed:
                                ts = entry.get("timestamp")
                                if ts:
                                    if s_first_ts is None:
                                        s_first_ts = ts
                                    s_last_ts = ts
                        if found_usage:
                            results["sessions_found"] += 1
                            if detailed:
                                results["sessions"].append({
                                    "session_id": session_file.stem,
                                    "input_tokens": s_inp,
                                    "output_tokens": s_out,
                                    "cache_creation_tokens": s_cache_create,
                                    "cache_read_tokens": s_cache_read,
                                    "started": s_first_ts,
                                    "ended": s_last_ts,
                                })
                except Exception:
                    continue
        if detailed:
            results["sessions"].sort(key=lambda x: x.get("started") or "", reverse=True)
        return results

    # ─── Hook-captured Session Stats ─────────────────────────

    def get_session_stats(self, limit: int = 50) -> list:
        """Read hook-captured session stats from .c3/session_stats.jsonl.

        Each entry: {ts, session_id, stop_reason, cost_usd, input_tokens,
                     output_tokens, cache_creation_tokens, cache_read_tokens}
        """
        stats_path = Path(self.project_path) / ".c3" / "session_stats.jsonl"
        if not stats_path.exists():
            return []
        entries = []
        try:
            with open(stats_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
        return entries[-limit:]

    def get_live_session_tokens(self) -> dict:
        """Read running token counts from the most recently modified Claude transcript file.

        Claude Code appends to transcript JSONL files after each exchange, so this
        gives per-turn live visibility into the active session's token usage.
        Returns: {session_id, input_tokens, output_tokens, cache_creation_tokens,
                  cache_read_tokens, turns, last_modified}
        """
        import re as _re
        home = Path.home()
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.exists():
            return {}

        proj_path = Path(self.project_path).resolve()
        proj_str = str(proj_path)
        slug_primary = _re.sub(r"[^a-zA-Z0-9]", "-", proj_str).lstrip("-")
        slug_legacy = proj_str.replace("\\", "--").replace("/", "--").replace(":", "").lstrip("-")

        candidate_dirs: list[Path] = []
        for slug in (slug_primary, slug_legacy):
            d = projects_dir / slug
            if d.is_dir() and d not in candidate_dirs:
                candidate_dirs.append(d)

        if not candidate_dirs:
            project_name = proj_path.name.lower()
            for d in projects_dir.iterdir():
                if not d.is_dir():
                    continue
                name = d.name.lower()
                if name.endswith(f"--{project_name}") or f"--{project_name}--" in name:
                    candidate_dirs.append(d)

        # Find the most recently modified JSONL file across all candidate dirs
        most_recent: Optional[Path] = None
        most_recent_mtime = 0.0
        for d in candidate_dirs:
            for f in d.glob("*.jsonl"):
                try:
                    mtime = f.stat().st_mtime
                    if mtime > most_recent_mtime:
                        most_recent_mtime = mtime
                        most_recent = f
                except Exception:
                    pass

        if not most_recent:
            return {}

        result: dict = {
            "session_id": most_recent.stem,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "turns": 0,
            "last_modified": most_recent_mtime,
        }
        try:
            with open(most_recent, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        msg = entry.get("message", {})
                        usage = msg.get("usage", {})
                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        if inp or out or cache_create or cache_read:
                            result["input_tokens"] += inp + cache_create + cache_read
                            result["output_tokens"] += out
                            result["cache_creation_tokens"] += cache_create
                            result["cache_read_tokens"] += cache_read
                            result["turns"] += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return result

    # ─── Context Budget ──────────────────────────────────────

    def _load_budget_thresholds(self) -> dict:
        """Load thresholds from .c3/config.json or use defaults.

        Migrates old multi-threshold keys: if 'threshold' is not set but
        'nudge' exists, uses 'nudge' as the threshold value.
        """
        config_file = self.project_path / ".c3" / "config.json"
        thresholds = dict(self.DEFAULT_BUDGET_THRESHOLDS)
        if config_file.exists():
            try:
                with open(config_file, encoding='utf-8') as f:
                    cfg = json.load(f)
                overrides = cfg.get("context_budget", {})
                if "threshold" in overrides:
                    thresholds["threshold"] = int(overrides["threshold"])
                elif "nudge" in overrides:
                    # Migrate old nudge → threshold
                    thresholds["threshold"] = int(overrides["nudge"])
            except Exception:
                pass
        return thresholds

    # Tools classified as c3 vs native for adoption tracking
    _C3_TOOLS = {"c3_search", "c3_compress", "c3_read", "c3_filter",
                 "c3_validate", "c3_session", "c3_memory", "c3_status",
                 "c3_delegate", "c3_edit", "c3_edits", "c3_agent"}
    _NATIVE_TOOLS = {"Read", "Grep", "Glob", "Bash", "Edit", "Write",
                     "FindFiles", "SearchText"}
    # C3 infra tools: their tokens are overhead, not content delivery
    _C3_INFRA_TOOLS = {"c3_session", "c3_memory", "c3_status", "c3_edits"}

    def track_response(self, tool_name: str, response_text: str,
                       response_tokens: int = 0) -> None:
        """Count tokens on response, accumulate in budget.
        Pass response_tokens to skip redundant count_tokens() call."""
        if not self.current_session:
            return
        budget = self.current_session["context_budget"]
        tokens = response_tokens or count_tokens(response_text)
        budget["response_tokens"] += tokens
        budget["call_count"] += 1
        if tokens > budget["peak_tokens"]:
            budget["peak_tokens"] = tokens
        budget["by_tool"][tool_name] = budget["by_tool"].get(tool_name, 0) + tokens
        # Track c3 vs native adoption
        if tool_name in self._C3_TOOLS:
            budget["c3_calls"] = budget.get("c3_calls", 0) + 1
        elif tool_name in self._NATIVE_TOOLS:
            budget["native_calls"] = budget.get("native_calls", 0) + 1
        # Track infra overhead separately
        if tool_name in self._C3_INFRA_TOOLS:
            budget["infra_tokens"] = budget.get("infra_tokens", 0) + tokens
        # Per-tool telemetry JSONL (failure-safe; never breaks the response)
        self._emit_tool_telemetry(tool_name, tokens)
        # Persist every 5 calls
        if budget["call_count"] % 5 == 0:
            self._persist_budget()

    #: Arg names that name a FILE, most specific first. Deliberately excludes
    #: `query`/`target` on c3_impact (a symbol, not a path) and anything that
    #: could carry a value rather than a location — telemetry is a measurement
    #: log, not somewhere to accidentally start recording payloads.
    _TARGET_ARG_KEYS = ("file_path", "filepath", "path", "file")
    _TARGET_MAX = 200

    def _derive_target(self, args: dict) -> str:
        """What a tool call was ABOUT, as a project-relative path.

        Empty when the args name no file — a search by query or a session
        action has no target, and inventing one would make the "top files"
        view a mix of paths and unrelated strings.
        """
        for key in self._TARGET_ARG_KEYS:
            raw = args.get(key)
            if not raw or not isinstance(raw, str):
                continue
            value = raw.strip()
            if not value:
                continue
            try:
                path = Path(value)
                if path.is_absolute():
                    value = self._relativize(path)
            except (OSError, ValueError):
                pass
            return value.replace("\\", "/")[:self._TARGET_MAX]
        return ""

    def _relativize(self, path) -> str:
        """An absolute path as project-relative, or its bare name.

        Resolve BOTH sides or neither. Resolving only the root compares a
        symlink-expanded root against an unexpanded candidate, which never
        matches: on macOS a temp dir is /var/... while its resolved form is
        /private/var/..., and Windows has the same problem with 8.3 names and
        substituted drives. The failure is quiet and costly — every path
        degrades to a bare filename, so `cli/server.py` and `ui/server.py`
        merge into one "server.py" row in the by-file view.

        Both orders are tried because resolve() can diverge in either
        direction depending on which side the symlink is on.
        """
        root = Path(self.project_path)
        try:
            root_resolved = root.resolve()
        except (OSError, ValueError):
            root_resolved = root
        try:
            path_resolved = path.resolve()
        except (OSError, ValueError):
            path_resolved = path
        for candidate, base in ((path_resolved, root_resolved),
                                (path, root),
                                (path_resolved, root),
                                (path, root_resolved)):
            try:
                return str(candidate.relative_to(base))
            except (ValueError, OSError):
                continue
        return path.name

    def _emit_tool_telemetry(self, tool_name: str, response_tokens: int) -> None:
        """Append one per-tool-call record to .c3/tool_telemetry.jsonl.

        Consumes the pending structured/summary token data stashed on this
        thread by record_tool_tokens() / log_tool_call(). Failure-safe:
        telemetry errors are swallowed so they can never break a tool
        response.
        """
        try:
            pending = getattr(self._tool_call_local, "pending", None)
            if pending is not None and pending.get("tool") != tool_name:
                pending = None  # stale entry from an aborted call
            action = getattr(self._tool_call_local, "action", "") or ""
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": (self.current_session or {}).get("id", ""),
                "tool": tool_name,
                "action": action,
                "response_tokens": int(response_tokens),
                "raw_tokens": pending.get("raw_tokens") if pending else None,
                "optimized_tokens": pending.get("optimized_tokens") if pending else None,
                "duration_ms": pending.get("duration_ms") if pending else None,
                "source": pending.get("source") if pending else None,
                "target": getattr(self._tool_call_local, "target", "") or "",
            }
            append_telemetry_record(self.project_path, record)
        except Exception:
            pass
        finally:
            try:
                self._tool_call_local.pending = None
                self._tool_call_local.action = ""
                self._tool_call_local.target = ""
            except Exception:
                pass

    def _persist_budget(self) -> None:
        """Write current budget snapshot to .c3/context_budget.json."""
        if not self.current_session:
            return
        budget = self.current_session["context_budget"]
        self._budget_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._budget_file, 'w', encoding='utf-8') as f:
                json.dump(budget, f, indent=2)
        except Exception:
            pass

    def mark_auto_snapshot_fired(self) -> None:
        """Mark that auto-snapshot has been triggered for this session."""
        if self.current_session:
            self.current_session["context_budget"]["auto_snapshot_fired"] = True
            self._persist_budget()

    def get_context_nudge(self) -> str:
        """Return a budget nudge if over threshold, else empty string."""
        if not self.current_session:
            return ""
        budget = self.current_session["context_budget"]
        total = budget["response_tokens"]
        threshold = self._budget_thresholds["threshold"]
        if total < threshold:
            return ""
        pct = round(total / threshold * 100) if threshold > 0 else 0
        return (f"\n[ctx:{pct}%|high] Token budget exceeded threshold. "
                "Run c3_session(action='compact') soon, then ask user to /clear and restore.")

    def get_budget_snapshot(self) -> dict:
        """Return budget stats for c3_status. Cached by call_count."""
        if not self.current_session:
            return {"error": "no active session"}
        budget = self.current_session["context_budget"]
        # Cache: reuse last snapshot if call_count hasn't changed
        cc = budget["call_count"]
        if hasattr(self, "_snap_cache") and self._snap_cache[0] == cc:
            return self._snap_cache[1]
        total = budget["response_tokens"]
        infra = budget.get("infra_tokens", 0)
        content = total - infra
        calls = budget["call_count"]
        avg = round(total / calls) if calls > 0 else 0
        by_tool = budget.get("by_tool", {})
        top = sorted(by_tool.items(), key=lambda x: -x[1])[:5]
        c3 = budget.get("c3_calls", 0)
        native = budget.get("native_calls", 0)
        total_classified = c3 + native
        adoption_pct = round(c3 / total_classified * 100) if total_classified > 0 else 100
        snap = {
            "response_tokens": total,
            "content_tokens": content,
            "infra_tokens": infra,
            "call_count": calls,
            "peak_tokens": budget["peak_tokens"],
            "avg_tokens_per_call": avg,
            "top_consumers": [{"tool": t, "tokens": n} for t, n in top],
            "threshold": self._budget_thresholds["threshold"],
            "over_budget": total >= self._budget_thresholds["threshold"],
            "by_tool": by_tool,
            "c3_calls": c3,
            "native_calls": native,
            "c3_adoption_pct": adoption_pct,
            "auto_snapshot_fired": budget.get("auto_snapshot_fired", False),
        }
        self._snap_cache = (cc, snap)
        return snap
