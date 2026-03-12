"""
E2E Benchmark Engine — runs real AI sessions comparing C3-augmented vs baseline workflows.

Mode 2: Full agent with tool access.
  - C3 run: MCP tools available (.mcp.json present)
  - Baseline run: No C3 MCP (strict config override)
  - Both can use native CLI tools (Read, Grep, Bash, etc.)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services.e2e_evaluator import EvalScore, Evaluator
from services.e2e_tasks import E2ETask, TaskBuilder, build_prompt, DIFFICULTY_WEIGHTS


def _unicode_safe() -> bool:
    """True if the terminal can render Unicode box-drawing/check characters."""
    try:
        enc = getattr(sys.stdout, "encoding", "") or ""
        "─✓✗".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError, AttributeError):
        return False


_UNI = _unicode_safe()


def _fmt_duration(seconds: float) -> str:
    """Format seconds as '1m23s' or '45s'."""
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


# ---------------------------------------------------------------------------
# CLI Provider
# ---------------------------------------------------------------------------

@dataclass
class ToolUsage:
    """Tool usage statistics from a single CLI run."""
    tool_counts: dict[str, int] = field(default_factory=dict)  # tool_name -> call_count
    tool_categories: dict[str, list[str]] = field(default_factory=dict)  # category -> [tool_names]
    total_tool_calls: int = 0
    unique_tools: int = 0
    # C3-specific tools detected
    c3_tool_calls: int = 0
    native_tool_calls: int = 0

    def to_dict(self) -> dict:
        return {
            "tool_counts": self.tool_counts,
            "tool_categories": self.tool_categories,
            "total_tool_calls": self.total_tool_calls,
            "unique_tools": self.unique_tools,
            "c3_tool_calls": self.c3_tool_calls,
            "native_tool_calls": self.native_tool_calls,
        }


# C3 MCP tools — detect to separate C3 tools from native tools
# Anthropic API pricing ($ per million tokens) — used for cost consistency checks.
# Keys are model ID substrings; first match wins.
_MODEL_PRICING = {
    "claude-opus-4":     {"input": 15.0,  "output": 75.0,  "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-3-5":   {"input": 15.0,  "output": 75.0,  "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4":   {"input": 3.0,   "output": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-3-5": {"input": 3.0,   "output": 15.0,  "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4":    {"input": 0.80,  "output": 4.0,   "cache_write": 1.0,   "cache_read": 0.08},
    "claude-haiku-3-5":  {"input": 0.80,  "output": 4.0,   "cache_write": 1.0,   "cache_read": 0.08},
}

def _get_pricing(model_id: str) -> dict | None:
    mid = (model_id or "").lower()
    for key, rates in _MODEL_PRICING.items():
        if key in mid:
            return rates
    return None

def _compute_expected_cost(inp: int, out: int, cw: int, cr: int, model_id: str) -> float | None:
    """Re-derive cost from token breakdown. Returns None if model pricing is unknown."""
    rates = _get_pricing(model_id)
    if not rates:
        return None
    return (inp * rates["input"] + out * rates["output"] +
            cw * rates["cache_write"] + cr * rates["cache_read"]) / 1_000_000

_C3_TOOLS = {"c3_compress", "c3_read", "c3_search", "c3_filter", "c3_validate",
             "c3_memory", "c3_session", "c3_status", "c3_delegate"}

# Native Claude Code tools
_NATIVE_TOOLS = {"Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent",
                 "WebSearch", "WebFetch", "NotebookEdit", "TodoRead", "TodoWrite"}

# Tool category mapping
_TOOL_CATEGORIES = {
    "c3_mcp": list(_C3_TOOLS),
    "file_ops": ["Read", "Write", "Edit", "Glob", "c3_read", "c3_compress"],
    "search": ["Grep", "Glob", "c3_search", "WebSearch"],
    "execution": ["Bash", "Agent", "c3_delegate"],
    "analysis": ["c3_validate", "c3_filter", "c3_status"],
    "context": ["c3_memory", "c3_session"],
}


@dataclass
class CLIResponse:
    """Result from a single CLI invocation."""
    text: str = ""
    latency_ms: float = 0.0
    exit_code: int = -1
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    model_used: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    error: str = ""
    # Rich timing from Claude JSON
    duration_ms: float = 0.0
    duration_api_ms: float = 0.0
    # Cache economics
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Model metadata
    model_id: str = ""
    context_window: int = 0
    # Full response text for side-by-side comparison
    response_text: str = ""
    # Tool usage analysis
    tool_usage: ToolUsage = field(default_factory=ToolUsage)
    # Token count reliability: "modelUsage", "usage", or "partial" (cost/token mismatch)
    token_count_source: str = ""
    # Estimated peak context window fill % (proxy via cache_read growth across turns)
    context_pressure_pct: float = 0.0

    def to_dict(self) -> dict:
        total_tokens = self.input_tokens + self.output_tokens + self.cache_creation_tokens + self.cache_read_tokens
        computed = _compute_expected_cost(
            self.input_tokens, self.output_tokens,
            self.cache_creation_tokens, self.cache_read_tokens,
            self.model_id or self.model_used,
        )
        d = {
            "text_length": len(self.text),
            "response_text": self.response_text[:800] + "…" if len(self.response_text) > 800 else self.response_text,
            "latency_ms": round(self.latency_ms, 1),
            "duration_ms": round(self.duration_ms, 1),
            "duration_api_ms": round(self.duration_api_ms, 1),
            "exit_code": self.exit_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "computed_cost_usd": round(computed, 6) if computed is not None else None,
            "token_count_source": self.token_count_source,
            "context_pressure_pct": round(self.context_pressure_pct, 1),
            "num_turns": self.num_turns,
            "model_used": self.model_used,
            "model_id": self.model_id,
            "context_window": self.context_window,
            "error": self.error,
            "tool_usage": self.tool_usage.to_dict(),
        }
        return d


@dataclass
class CLIProvider:
    """Wraps an AI CLI for non-interactive prompt execution."""
    name: str
    executable: str = ""
    model: str | None = None
    available: bool = False
    permission_mode: str = "bypassPermissions"

    def detect(self) -> bool:
        """Check if CLI is installed and accessible."""
        exe = self.executable or self.name
        # shutil.which resolves .cmd/.bat wrappers on Windows (e.g. gemini.CMD, codex.CMD)
        resolved = shutil.which(exe)
        if not resolved:
            self.available = False
            return False
        try:
            result = subprocess.run(
                [resolved, "--version"], capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            self.available = result.returncode == 0
            if self.available:
                self.executable = resolved
        except Exception:
            self.available = False
        return self.available

    def run(self, prompt: str, cwd: str, with_c3: bool = True,
            timeout: int = 180, multi_turn: bool = False) -> CLIResponse:
        """Execute prompt through CLI, return structured response."""
        response = CLIResponse()

        if multi_turn and with_c3 and self.name == "claude":
            return self._run_multi_turn(prompt, cwd, timeout)

        cmd = self._build_command(prompt, with_c3)

        env = os.environ.copy()
        for block_var in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT",
                         "GEMINI_CLI", "CODEX_CLI"):
            env.pop(block_var, None)
        # Prevent C3 MCP server subprocesses from auto-restoring snapshots between tasks,
        # which would carry over accumulated budget from the previous task's session end.
        env["C3_BENCHMARK_MODE"] = "1"

        t0 = time.perf_counter()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                env=env, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            response.latency_ms = (time.perf_counter() - t0) * 1000
            response.exit_code = result.returncode
            response.raw_stdout = result.stdout or ""
            response.raw_stderr = result.stderr or ""
            self._parse_output(response)

        except subprocess.TimeoutExpired:
            response.latency_ms = timeout * 1000
            response.error = f"Timeout after {timeout}s"
        except Exception as e:
            response.latency_ms = (time.perf_counter() - t0) * 1000
            response.error = str(e)

        # Store full response for comparison view
        response.response_text = response.text

        # Extract tool usage
        response.tool_usage = self._extract_tool_usage(response, with_c3=with_c3)

        return response

    def _build_command(self, prompt: str, with_c3: bool) -> list[str]:
        """Build CLI command for non-interactive execution."""
        exe = self.executable or self.name

        if self.name == "claude":
            cmd = [exe, "-p", prompt, "--output-format", "json",
                   "--permission-mode", self.permission_mode]
            if self.model:
                cmd += ["--model", self.model]
            if not with_c3:
                cmd += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
            return cmd

        elif self.name == "gemini":
            # --approval-mode yolo: auto-approve all tool calls (required for
            # non-interactive benchmark runs; "plan" falls back to "default"
            # which prompts interactively and causes timeout).
            cmd = [exe, "-p", prompt, "--output-format", "json",
                   "--approval-mode", "yolo"]
            if self.model:
                cmd += ["-m", self.model]
            if not with_c3:
                # Pass a dummy server name to bypass all configured MCP servers.
                cmd += ["--allowed-mcp-server-names", "__none__"]
            return cmd

        elif self.name == "codex":
            cmd = [exe, "exec", prompt, "--json"]
            if self.model:
                cmd += ["--model", self.model]
            if not with_c3:
                cmd += ["-c", "mcp_servers={}"]
            return cmd

        raise ValueError(f"Unknown provider: {self.name}")

    def _run_multi_turn(self, prompt: str, cwd: str, timeout: int) -> CLIResponse:
        """Two-prompt flow: explore first, then answer with --resume."""
        response = CLIResponse()
        exe = self.executable or self.name

        env = os.environ.copy()
        for block_var in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT",
                         "GEMINI_CLI", "CODEX_CLI"):
            env.pop(block_var, None)
        env["C3_BENCHMARK_MODE"] = "1"
        _cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

        explore_prompt = (
            f"Using C3 MCP tools, explore the codebase to understand the following. "
            f"Do NOT answer yet — just gather context using c3_memory, c3_search, "
            f"c3_compress, and c3_read.\n\nQuestion: {prompt}"
        )
        answer_prompt = (
            f"Based on your exploration, now answer the question. "
            f"Be specific with file paths, function names, and line numbers. "
            f"Keep your answer concise (under 500 words).\n\nQuestion: {prompt}"
        )

        t0 = time.perf_counter()

        try:
            # Turn 1: Explore
            cmd1 = [exe, "-p", explore_prompt, "--output-format", "json",
                    "--permission-mode", self.permission_mode]
            if self.model:
                cmd1 += ["--model", self.model]

            result1 = subprocess.run(
                cmd1, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                env=env, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, creationflags=_cflags,
            )

            # Extract session ID from turn 1 for safe --resume
            # (--continue would race under concurrent task_workers)
            session_id = None
            try:
                data1_parsed = json.loads(result1.stdout or "{}")
                session_id = data1_parsed.get("session_id") or data1_parsed.get("sessionId")
            except (json.JSONDecodeError, TypeError):
                pass

            # Turn 2: Answer — use --resume if we got a session ID, else --continue
            cmd2 = [exe, "-p", answer_prompt, "--output-format", "json",
                    "--permission-mode", self.permission_mode]
            if session_id:
                cmd2 += ["--resume", session_id]
            else:
                cmd2 += ["--continue"]
            if self.model:
                cmd2 += ["--model", self.model]

            result2 = subprocess.run(
                cmd2, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                env=env, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, creationflags=_cflags,
            )

            response.latency_ms = (time.perf_counter() - t0) * 1000
            response.exit_code = result2.returncode
            # Use turn 2 output as the main response (it has the answer)
            response.raw_stdout = result2.stdout or ""
            # Combine stderr from both turns for tool detection
            response.raw_stderr = (result1.stderr or "") + "\n" + (result2.stderr or "")
            self._parse_output(response)

            # Merge cost/tokens from turn 1 if available
            try:
                data1 = json.loads(result1.stdout or "{}")
                usage1 = data1.get("usage", data1.get("result", {}).get("usage", {}))
                if usage1:
                    response.input_tokens += usage1.get("input_tokens", 0)
                    response.output_tokens += usage1.get("output_tokens", 0)
                    response.cache_creation_tokens += usage1.get("cache_creation_input_tokens", 0)
                    response.cache_read_tokens += usage1.get("cache_read_input_tokens", 0)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        except subprocess.TimeoutExpired:
            response.latency_ms = timeout * 2 * 1000
            response.error = f"Multi-turn timeout after {timeout}s"
        except Exception as e:
            response.latency_ms = (time.perf_counter() - t0) * 1000
            response.error = str(e)

        response.response_text = response.text
        response.tool_usage = self._extract_tool_usage(response, with_c3=True)
        return response

    def _parse_output(self, response: CLIResponse):
        """Parse raw output into structured response fields."""
        stdout = response.raw_stdout.strip()

        if self.name == "claude":
            self._parse_claude_json(response, stdout)
        elif self.name == "gemini":
            self._parse_gemini_output(response, stdout)
        elif self.name == "codex":
            self._parse_codex_output(response, stdout)
        else:
            response.text = stdout

    def _parse_claude_json(self, response: CLIResponse, stdout: str):
        """Parse Claude's --output-format json — extract all available fields."""
        try:
            data = json.loads(stdout)
            response.text = data.get("result", stdout)

            # Plan mode puts the actual answer in messages[], not in result.
            # If result is very short, scan for the last substantial assistant text.
            if len(response.text.strip()) < 300:
                for msg in reversed(data.get("messages", [])):
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        candidate = content.strip()
                    elif isinstance(content, list):
                        candidate = "\n".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()
                    else:
                        continue
                    if len(candidate) > len(response.text.strip()):
                        response.text = candidate
                        break
            response.cost_usd = data.get("total_cost_usd", 0) or data.get("cost_usd", 0) or 0
            response.num_turns = data.get("num_turns", 0) or 0
            response.duration_ms = data.get("duration_ms", 0) or 0
            response.duration_api_ms = data.get("duration_api_ms", 0) or 0

            # Usage block (may be last-turn only when modelUsage is absent)
            usage = data.get("usage", {})
            if usage:
                response.input_tokens = usage.get("input_tokens", 0) or 0
                response.output_tokens = usage.get("output_tokens", 0) or 0
                response.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
                response.cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
                response.token_count_source = "usage"

            # Model usage block — has model ID, context window, and cumulative token totals.
            # Preferred over `usage` when present because it is always session-cumulative.
            model_usage = data.get("modelUsage", {})
            if model_usage:
                for model_id, model_data in model_usage.items():
                    response.model_id = model_id
                    response.model_used = model_id
                    response.context_window = model_data.get("contextWindow", 0) or 0
                    if model_data.get("inputTokens"):
                        response.input_tokens = model_data["inputTokens"]
                    if model_data.get("outputTokens"):
                        response.output_tokens = model_data["outputTokens"]
                    if model_data.get("cacheCreationInputTokens"):
                        response.cache_creation_tokens = model_data["cacheCreationInputTokens"]
                    if model_data.get("cacheReadInputTokens"):
                        response.cache_read_tokens = model_data["cacheReadInputTokens"]
                    break  # Take first model
                response.token_count_source = "modelUsage"
            elif not response.model_used:
                response.model_used = self.model or ""

            # Consistency check: re-derive cost from token breakdown and compare to
            # total_cost_usd.  A delta > $0.01 means the token counts are partial
            # (likely last-turn only from the `usage` block).
            computed = _compute_expected_cost(
                response.input_tokens, response.output_tokens,
                response.cache_creation_tokens, response.cache_read_tokens,
                response.model_id or response.model_used,
            )
            if computed is not None and abs(computed - response.cost_usd) > 0.01:
                response.token_count_source = "partial"

            # Context pressure: estimate peak context fill % across the session.
            # Approximation: cache_read grows each turn; the last turn reads ~
            # 2*total_cache_read/(num_turns+1) tokens from cache.
            if response.context_window > 0 and response.num_turns > 0:
                peak_ctx = 2 * response.cache_read_tokens / (response.num_turns + 1)
                response.context_pressure_pct = min(peak_ctx / response.context_window * 100, 100.0)

        except (json.JSONDecodeError, TypeError):
            response.text = stdout

    def _parse_gemini_output(self, response: CLIResponse, stdout: str):
        """Parse Gemini CLI output.

        Gemini CLI may prefix stdout with status lines like
        "Server 'c3' supports tool updates. Listening for changes..."
        before the JSON blob, so we locate the first '{' and parse from there.

        Token structure (as of gemini-cli 0.32+):
          stats.models.<model_id>.tokens.{input, candidates, cached, total}
        """
        # Strip any non-JSON prefix lines printed before the JSON object
        json_start = stdout.find("{")
        if json_start > 0:
            stdout = stdout[json_start:]

        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            response.text = stdout
            return

        if not isinstance(data, dict):
            if isinstance(data, list):
                texts = [msg.get("text", msg.get("content", ""))
                         for msg in data if isinstance(msg, dict)]
                response.text = "\n".join(t for t in texts if t)
            else:
                response.text = str(data)
            return

        response.text = data.get("response", data.get("text", data.get("result", stdout)))

        # Token & model data live under stats.models.<model_id>.tokens
        stats = data.get("stats", {})
        models = stats.get("models", {})
        if models:
            # Aggregate across all model entries (Gemini may split across models)
            total_input = total_output = total_cached = total_req = 0
            for model_id, mdata in models.items():
                if not response.model_used:
                    response.model_used = model_id
                
                api_stats = mdata.get("api", {})
                total_req += api_stats.get("totalRequests", 0) or 0
                
                tok = mdata.get("tokens", {})
                total_input += tok.get("input", 0) or 0
                # "candidates" = generated/output tokens
                total_output += tok.get("candidates", 0) or 0
                total_cached += tok.get("cached", 0) or 0
            
            response.num_turns = total_req
            response.input_tokens = total_input
            response.output_tokens = total_output
            response.cache_read_tokens = total_cached

        if not response.model_used:
            response.model_used = data.get("model", self.model or "")

    def _parse_codex_output(self, response: CLIResponse, stdout: str):
        """Parse Codex exec --json JSONL output.

        Event schema:
          thread.started          — session opened
          turn.started            — new turn
          error                   — transient (reconnect/transport) or fatal
          item.completed          — item.type='agent_message' has the text;
                                    item.type='error' is a non-fatal item error
          turn.completed          — final event; .usage has token counts

        A session is considered terminated if no turn.completed is emitted.
        """
        response.model_used = self.model or ""
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass

        if not events:
            # No JSONL at all — old codex version or plain text output
            response.text = stdout
            return

        texts = []
        fatal_errors = []
        turn_completed = False

        _TRANSIENT = ("reconnecting", "falling back", "stream disconnected",
                      "websocket", "https transport")

        for ev in events:
            t = ev.get("type", "")

            if t == "error":
                msg = ev.get("message", "")
                if not any(pat in msg.lower() for pat in _TRANSIENT):
                    fatal_errors.append(msg)

            elif t == "item.completed":
                item = ev.get("item", {})
                if item.get("type") == "agent_message":
                    texts.append(item.get("text", ""))
                elif item.get("type") == "error":
                    msg = item.get("message", "")
                    if not any(pat in msg.lower() for pat in _TRANSIENT):
                        fatal_errors.append(msg)

            elif t == "turn.completed":
                turn_completed = True
                usage = ev.get("usage", {})
                response.input_tokens = usage.get("input_tokens", 0) or 0
                response.cache_read_tokens = usage.get("cached_input_tokens", 0) or 0
                response.output_tokens = usage.get("output_tokens", 0) or 0

        response.text = "\n".join(t for t in texts if t)

        if not turn_completed:
            # Session was killed before completing — surface a clear error
            termination_msg = "; ".join(fatal_errors) if fatal_errors else "session terminated before turn.completed"
            response.error = f"[codex:terminated] {termination_msg}"
        elif fatal_errors and not response.text:
            response.error = f"[codex:error] {'; '.join(fatal_errors)}"

    def _extract_tool_usage(self, response: CLIResponse, with_c3: bool = True) -> ToolUsage:
        """Extract tool usage from CLI response using JSON data + text heuristics."""
        usage = ToolUsage()
        counts: dict[str, int] = {}

        # Source 1: Claude JSON — parse tool_use messages if present
        if self.name == "claude" and response.raw_stdout:
            try:
                data = json.loads(response.raw_stdout)
                # Claude may include messages array with tool_use blocks
                messages = data.get("messages", [])
                for msg in messages:
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    name = block.get("name", "unknown")
                                    counts[name] = counts.get(name, 0) + 1
                        # Also check role=tool_use pattern
                        if msg.get("role") == "assistant" and msg.get("type") == "tool_use":
                            name = msg.get("name", "unknown")
                            counts[name] = counts.get(name, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

        # Source 1c: Claude JSON — parse top-level 'result' array
        if self.name == "claude" and response.raw_stdout and not counts:
            try:
                data = json.loads(response.raw_stdout)
                result_list = data.get("result", [])
                if isinstance(result_list, list):
                    for block in result_list:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "unknown")
                            counts[name] = counts.get(name, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

        # Source 1b: Gemini JSON — parse stats.tools if present
        if self.name == "gemini" and response.raw_stdout:
            stdout = response.raw_stdout
            json_start = stdout.find("{")
            if json_start >= 0:
                try:
                    data = json.loads(stdout[json_start:])
                    stats = data.get("stats", {})
                    tools = stats.get("tools", {})
                    by_name = tools.get("byName", {})
                    for tool_name, tool_data in by_name.items():
                        count = tool_data.get("count", 0)
                        if count > 0:
                            # Normalize tool name (e.g., mcp_c3_c3_search -> c3_search)
                            norm_name = tool_name
                            if norm_name.startswith("mcp_c3_"):
                                norm_name = norm_name[7:]
                            counts[norm_name] = counts.get(norm_name, 0) + count
                except (json.JSONDecodeError, TypeError):
                    pass

        # Source 2: Heuristic — detect tool patterns from response text.
        # Supplements (not replaces) JSON-based counts: adds tools found in text
        # that weren't captured by JSON parsing (e.g. when messages[] is absent).
        # c3_* patterns are skipped for baseline runs to avoid false positives when
        # the response quotes source files that mention c3_* tool names.
        text = response.text or ""
        heuristic_counts = _detect_tools_from_text(text, include_c3=with_c3)
        if not counts:
            counts = heuristic_counts
        else:
            # Supplement: add heuristic detections for tools not captured by JSON
            for name, count in heuristic_counts.items():
                if name not in counts:
                    counts[name] = count

        # Source 3: Parse stderr for tool call patterns
        if not counts and response.raw_stderr:
            import re
            stderr_tools = re.findall(r'(?:Tool|tool_use|Calling)[\s:]+(\w+)', response.raw_stderr)
            for tool_name in stderr_tools:
                if tool_name.startswith("mcp__c3__"):
                    tool_name = tool_name[9:]
                elif tool_name.startswith("mcp_c3_"):
                    tool_name = tool_name[7:]
                if tool_name and tool_name != "unknown":
                    counts[tool_name] = counts.get(tool_name, 0) + 1

        # If we have num_turns but no tool counts, estimate from turns
        if not counts and response.num_turns > 1:
            # Each turn beyond the first likely involved tool use
            # We can't know which tools, but we know there were tool interactions
            counts["_unknown_tools"] = max(0, response.num_turns - 1)

        # Classify tools
        c3_calls = 0
        native_calls = 0
        for name, count in counts.items():
            if name in _C3_TOOLS or name.startswith("c3_"):
                c3_calls += count
            elif name in _NATIVE_TOOLS:
                native_calls += count
            else:
                native_calls += count  # default to native

        # Build categories
        categories: dict[str, list[str]] = {}
        for cat_name, cat_tools in _TOOL_CATEGORIES.items():
            matched = [t for t in counts if t in cat_tools]
            if matched:
                categories[cat_name] = matched

        usage.tool_counts = counts
        usage.tool_categories = categories
        usage.total_tool_calls = sum(counts.values())
        usage.unique_tools = len(counts)
        usage.c3_tool_calls = c3_calls
        usage.native_tool_calls = native_calls

        return usage


def _detect_tools_from_text(text: str, include_c3: bool = True) -> dict[str, int]:
    """Heuristic tool detection from response text patterns."""
    counts: dict[str, int] = {}
    text_lower = text.lower()

    # Pattern-based detection for common tool signatures
    _patterns = []

    # C3 MCP tools — only when MCP is enabled (baseline runs may mention c3_* in quoted code)
    if include_c3:
        _patterns += [
            (r'\bc3_compress\b', "c3_compress"),
            (r'\bc3_read\b', "c3_read"),
            (r'\bc3_search\b', "c3_search"),
            (r'\bc3_filter\b', "c3_filter"),
            (r'\bc3_validate\b', "c3_validate"),
            (r'\bc3_memory\b', "c3_memory"),
            (r'\bc3_session\b', "c3_session"),
            (r'\bc3_status\b', "c3_status"),
        ]
    # Native tool signatures in response text
    _patterns += [
        (r'(?:i\'ll |let me |i will )read (?:the |this )?file', "Read"),
        (r'(?:reading|read) `[^`]+`', "Read"),
        (r'(?:i\'ll |let me )search', "Grep"),
        (r'(?:searching|grep|search)(?:ing)? for', "Grep"),
        (r'(?:i\'ll |let me )(?:run|execute)', "Bash"),
        (r'(?:running|ran) (?:the )?command', "Bash"),
        (r'(?:i\'ll |let me )edit', "Edit"),
        (r'(?:i\'ll |let me )write', "Write"),
        (r'(?:looking for|finding|glob) files', "Glob"),
    ]

    for pattern, tool_name in _patterns:
        found = len(re.findall(pattern, text_lower))
        if found:
            counts[tool_name] = counts.get(tool_name, 0) + found

    return counts


# ---------------------------------------------------------------------------
# Task Result
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    """Result of running one task against one provider."""
    task_id: str
    task_category: str
    task_difficulty: str = "medium"
    provider: str = ""
    c3_response: CLIResponse = field(default_factory=CLIResponse)
    baseline_response: CLIResponse = field(default_factory=CLIResponse)
    c3_score: EvalScore = field(default_factory=EvalScore)
    baseline_score: EvalScore = field(default_factory=EvalScore)

    @property
    def c3_wins(self) -> bool:
        c3 = self.c3_score.combined_score
        base = self.baseline_score.combined_score
        if c3 != base:
            return c3 > base
        # Tiebreaker: faster (lower latency) wins
        return self.c3_response.latency_ms < self.baseline_response.latency_ms

    @property
    def score_delta(self) -> float:
        return self.c3_score.combined_score - self.baseline_score.combined_score

    @property
    def difficulty_weight(self) -> float:
        return DIFFICULTY_WEIGHTS.get(self.task_difficulty, 1.0)

    def efficiency(self) -> dict:
        """Compute per-task efficiency metrics (time, cost, tokens saved)."""
        c3 = self.c3_response
        base = self.baseline_response
        c3_total_tok = c3.input_tokens + c3.output_tokens + c3.cache_creation_tokens + c3.cache_read_tokens
        base_total_tok = base.input_tokens + base.output_tokens + base.cache_creation_tokens + base.cache_read_tokens

        def _pct(saved, total):
            return round(saved / total * 100, 1) if total else 0.0

        time_saved = base.latency_ms - c3.latency_ms
        cost_saved = base.cost_usd - c3.cost_usd
        tokens_saved = base_total_tok - c3_total_tok
        turns_saved = base.num_turns - c3.num_turns

        c3_qpd = c3.cost_usd and (self.c3_score.combined_score / c3.cost_usd) or 0
        base_qpd = base.cost_usd and (self.baseline_score.combined_score / base.cost_usd) or 0

        return {
            "time_saved_ms": round(time_saved, 1),
            "time_saved_pct": _pct(time_saved, base.latency_ms),
            "cost_saved_usd": round(cost_saved, 6),
            "cost_saved_pct": _pct(cost_saved, base.cost_usd),
            "tokens_saved": tokens_saved,
            "tokens_saved_pct": _pct(tokens_saved, base_total_tok),
            "turns_saved": turns_saved,
            "quality_per_dollar_c3": round(c3_qpd, 2),
            "quality_per_dollar_baseline": round(base_qpd, 2),
        }

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_category": self.task_category,
            "task_difficulty": self.task_difficulty,
            "difficulty_weight": self.difficulty_weight,
            "provider": self.provider,
            "c3_response": self.c3_response.to_dict(),
            "baseline_response": self.baseline_response.to_dict(),
            "c3_score": self.c3_score.to_dict(),
            "baseline_score": self.baseline_score.to_dict(),
            "c3_wins": self.c3_wins,
            "score_delta": round(self.score_delta, 3),
            "efficiency": self.efficiency(),
        }


# ---------------------------------------------------------------------------
# Benchmark Engine
# ---------------------------------------------------------------------------

def detect_providers(
    requested: list[str] | None = None,
    model_overrides: dict[str, str] | None = None,
    permission_mode: str = "bypassPermissions",
) -> list[CLIProvider]:
    """Detect available AI CLIs on the system."""
    all_providers = [
        CLIProvider(name="claude"),
        CLIProvider(name="gemini"),
        CLIProvider(name="codex"),
    ]

    for p in all_providers:
        p.permission_mode = permission_mode
        p.detect()
        if model_overrides and p.name in model_overrides:
            p.model = model_overrides[p.name]

    if requested:
        all_providers = [p for p in all_providers if p.name in requested]

    return [p for p in all_providers if p.available]


# ---------------------------------------------------------------------------
# Result cache helpers
# ---------------------------------------------------------------------------

def _task_cache_key(task: "E2ETask", providers: list["CLIProvider"]) -> str:
    """Stable cache key from task id, query, and provider identities."""
    import hashlib
    _CACHE_VERSION = "v2"  # Bump when prompt template or scoring changes
    provider_sig = ",".join(f"{p.name}:{p.model or ''}" for p in sorted(providers, key=lambda p: p.name))
    raw = f"{_CACHE_VERSION}|{task.id}|{task.query}|{provider_sig}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _task_result_to_cache_dict(tr: "TaskResult") -> dict:
    """Minimal serialisation of a TaskResult for the cache."""
    return {
        "task_id": tr.task_id,
        "task_category": tr.task_category,
        "task_difficulty": tr.task_difficulty,
        "provider": tr.provider,
        "c3_score": tr.c3_score.to_dict(),
        "baseline_score": tr.baseline_score.to_dict(),
        "c3_response": tr.c3_response.to_dict(),
        "baseline_response": tr.baseline_response.to_dict(),
    }


def _load_result_cache(project_path: str) -> dict:
    cache_path = Path(project_path) / ".c3" / "e2e_benchmark" / "result_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_result_cache(project_path: str, cache: dict) -> None:
    cache_path = Path(project_path) / ".c3" / "e2e_benchmark" / "result_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


class E2EBenchmark:
    """Orchestrates end-to-end benchmark runs across providers and tasks."""

    def __init__(
        self,
        project_path: str,
        providers: list[CLIProvider],
        tasks: list[E2ETask],
        evaluator: Evaluator,
        timeout: int = 120,
        parallel: bool = True,
        verbose: bool = False,
        on_progress: callable = None,
        task_workers: int = 1,
        cache: bool = True,
        permission_mode: str = "bypassPermissions",
    ):
        self.project_path = str(Path(project_path).resolve())
        self.providers = providers
        self.tasks = tasks
        self.evaluator = evaluator
        self.timeout = timeout
        self.parallel = parallel
        self.verbose = verbose
        self.on_progress = on_progress
        self.task_workers = max(1, task_workers)
        self.cache = cache
        self.permission_mode = permission_mode
        self._result_cache = _load_result_cache(project_path) if cache else {}
        self.results: list[TaskResult] = []
        self._sandbox_path = self.project_path

    def run_all(self) -> list[TaskResult]:
        """Run all tasks against all providers, return results.

        Task-level parallelism is controlled by self.task_workers (default 1).
        Setting task_workers > 1 runs multiple tasks concurrently — useful when
        benchmarking many tasks and Anthropic rate limits allow it.
        """
        self.results = []
        total = len(self.tasks) * len(self.providers)
        completed = 0
        self._wins = 0  # rolling C3 win count for verbose scoreboard
        self._task_elapsed: list[float] = []  # per-task wall times for ETA
        run_start = time.perf_counter()
        SEP = "─" if _UNI else "-"
        _lock = __import__("threading").Lock()

        # --- Worktree sandbox ---
        sandbox_path = self.project_path
        worktree_dir = None
        if self.permission_mode != "plan":
            git_dir = os.path.join(self.project_path, ".git")
            if os.path.isdir(git_dir):
                suffix = f"_c3bench_{os.getpid()}"
                worktree_dir = os.path.join(os.path.dirname(self.project_path), f".c3_bench{suffix}")
                try:
                    subprocess.run(
                        ["git", "worktree", "add", worktree_dir, "HEAD", "--detach"],
                        cwd=self.project_path, capture_output=True, text=True, timeout=30,
                    )
                    # Selective .c3/ copy
                    src_c3 = os.path.join(self.project_path, ".c3")
                    dst_c3 = os.path.join(worktree_dir, ".c3")
                    if os.path.isdir(src_c3):
                        os.makedirs(dst_c3, exist_ok=True)
                        for item in ("index", "doc_index"):
                            s = os.path.join(src_c3, item)
                            d = os.path.join(dst_c3, item)
                            if os.path.isdir(s):
                                shutil.copytree(s, d, dirs_exist_ok=True)
                        for item in ("facts.json", "config.json"):
                            s = os.path.join(src_c3, item)
                            if os.path.isfile(s):
                                shutil.copy2(s, os.path.join(dst_c3, item))

                    # Copy CLAUDE.md — required for C3 tool mandate
                    for md_file in ("CLAUDE.md",):
                        s = os.path.join(self.project_path, md_file)
                        if os.path.isfile(s):
                            shutil.copy2(s, os.path.join(worktree_dir, md_file))

                    # Copy .mcp.json — registers C3 MCP server with Claude CLI
                    src_mcp = os.path.join(self.project_path, ".mcp.json")
                    if os.path.isfile(src_mcp):
                        shutil.copy2(src_mcp, os.path.join(worktree_dir, ".mcp.json"))

                    # Copy .claude/ settings (contains MCP hooks, local config)
                    src_claude = os.path.join(self.project_path, ".claude")
                    dst_claude = os.path.join(worktree_dir, ".claude")
                    if os.path.isdir(src_claude) and not os.path.isdir(dst_claude):
                        shutil.copytree(src_claude, dst_claude, dirs_exist_ok=True)

                    if not os.path.isfile(os.path.join(worktree_dir, "CLAUDE.md")):
                        print("  !! Warning: CLAUDE.md not found in worktree — C3 instructions may be missing")
                    if not os.path.isfile(os.path.join(worktree_dir, ".mcp.json")):
                        print("  !! Warning: .mcp.json not found in worktree — MCP tools won't be available")
                    sandbox_path = worktree_dir
                    if self.verbose:
                        print(f"  Sandbox: {worktree_dir}")
                except Exception as e:
                    print(f"  !! Worktree creation failed ({e}), running in-place")
                    worktree_dir = None
            else:
                if self.verbose:
                    print("  !! Not a git repo — skipping worktree sandbox")

        self._sandbox_path = sandbox_path

        def _run_task_with_stats(task_idx_task):
            task_idx, task = task_idx_task
            if self.verbose:
                with _lock:
                    label = f" Task {task_idx+1}/{len(self.tasks)} | [{task.category}] {task.id} ({task.difficulty}) "
                    print(f"\n  {SEP*3}{label}{SEP * max(0, 68 - len(label))}", flush=True)
                    print(f"  Q: {task.query[:120]}{'...' if len(task.query) > 120 else ''}", flush=True)

            task_start = time.perf_counter()

            # Check cache first
            cached = self._get_cached_results(task)
            if cached is not None:
                if self.verbose:
                    with _lock:
                        print(f"  >> [{task.id}] using cached results", flush=True)
                return time.perf_counter() - task_start, cached

            if self.parallel and len(self.providers) > 1:
                task_results = self._run_task_parallel(task)
            else:
                task_results = self._run_task_sequential(task)

            if self.cache:
                self._save_cached_results(task, task_results)

            return time.perf_counter() - task_start, task_results

        indexed_tasks = list(enumerate(self.tasks))

        try:
            if self.task_workers > 1:
                with ThreadPoolExecutor(max_workers=self.task_workers) as pool:
                    futures = {pool.submit(_run_task_with_stats, it): it for it in indexed_tasks}
                    for future in as_completed(futures):
                        try:
                            elapsed, task_results = future.result()
                        except Exception as e:
                            it = futures[future]
                            task_results = []
                            elapsed = 0.0
                            if self.verbose:
                                print(f"  !! Task {it[1].id} failed: {e}", flush=True)

                        with _lock:
                            self._task_elapsed.append(elapsed)
                            for tr in task_results:
                                self.results.append(tr)
                                completed += 1
                                if tr.c3_wins:
                                    self._wins += 1
                                if self.on_progress:
                                    self.on_progress(completed, total, tr)
                                if self.verbose:
                                    self._print_result(tr, completed, total)
            else:
                for task_idx, task in indexed_tasks:
                    if self.verbose and self._task_elapsed:
                        avg_s = sum(self._task_elapsed) / len(self._task_elapsed)
                        remaining_tasks = len(self.tasks) - task_idx
                        eta = f"  ~{_fmt_duration(avg_s * remaining_tasks)} left"
                        label = f" Task {task_idx+1}/{len(self.tasks)} | [{task.category}] {task.id} ({task.difficulty}){eta} "
                        print(f"\n  {SEP*3}{label}{SEP * max(0, 68 - len(label))}", flush=True)
                        print(f"  Q: {task.query[:120]}{'...' if len(task.query) > 120 else ''}", flush=True)
                    elif self.verbose:
                        label = f" Task {task_idx+1}/{len(self.tasks)} | [{task.category}] {task.id} ({task.difficulty}) "
                        print(f"\n  {SEP*3}{label}{SEP * max(0, 68 - len(label))}", flush=True)
                        print(f"  Q: {task.query[:120]}{'...' if len(task.query) > 120 else ''}", flush=True)

                    elapsed, task_results = _run_task_with_stats((task_idx, task))
                    self._task_elapsed.append(elapsed)
                    for tr in task_results:
                        self.results.append(tr)
                        completed += 1
                        if tr.c3_wins:
                            self._wins += 1
                        if self.on_progress:
                            self.on_progress(completed, total, tr)
                        if self.verbose:
                            self._print_result(tr, completed, total)

            if self.cache:
                _save_result_cache(self.project_path, self._result_cache)

            if self.verbose:
                elapsed = time.perf_counter() - run_start
                c3_wins = sum(1 for r in self.results if r.c3_wins)
                avg_c3 = sum(r.c3_score.combined_score for r in self.results) / max(len(self.results), 1)
                avg_base = sum(r.baseline_score.combined_score for r in self.results) / max(len(self.results), 1)
                cached_count = sum(1 for r in self.results if getattr(r, "_from_cache", False))
                cache_note = f"  ({cached_count} cached)" if cached_count else ""
                print(f"\n  {SEP*72}")
                print(f"  Done in {_fmt_duration(elapsed)}{cache_note}  |  C3 won {c3_wins}/{total} tasks "
                      f"({100*c3_wins/max(total,1):.1f}%)  |  "
                      f"avg score: C3={avg_c3:.3f}  Base={avg_base:.3f}", flush=True)

            return self.results
        finally:
            if worktree_dir and os.path.isdir(worktree_dir):
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", worktree_dir, "--force"],
                        cwd=self.project_path, capture_output=True, text=True, timeout=30,
                    )
                except Exception:
                    pass
                if os.path.isdir(worktree_dir):
                    try:
                        shutil.rmtree(worktree_dir, ignore_errors=True)
                    except Exception:
                        pass

    def _get_cached_results(self, task: E2ETask) -> list[TaskResult] | None:
        """Return cached TaskResults if valid cache entry exists, else None."""
        key = _task_cache_key(task, self.providers)
        entry = self._result_cache.get(key)
        if not entry:
            return None
        # Simple TTL: 24 hours
        if time.time() - entry.get("ts", 0) > 86400:
            del self._result_cache[key]
            return None
        try:
            results = []
            for r in entry["results"]:
                tr = TaskResult(
                    task_id=r["task_id"],
                    task_category=r["task_category"],
                    task_difficulty=r.get("task_difficulty", "medium"),
                    provider=r.get("provider", ""),
                )
                # Restore scores from cached dicts
                for field, cls in (("c3_score", EvalScore), ("baseline_score", EvalScore)):
                    d = r.get(field, {})
                    score = cls()
                    for k, v in d.items():
                        if hasattr(score, k):
                            setattr(score, k, v)
                    setattr(tr, field, score)
                tr._from_cache = True
                results.append(tr)
            return results
        except Exception:
            return None

    def _save_cached_results(self, task: E2ETask, results: list[TaskResult]) -> None:
        """Persist task results to the in-memory cache dict."""
        key = _task_cache_key(task, self.providers)
        self._result_cache[key] = {
            "ts": time.time(),
            "results": [_task_result_to_cache_dict(r) for r in results],
        }

    def _run_task_parallel(self, task: E2ETask) -> list[TaskResult]:
        results = []
        prompt = build_prompt(task)

        with ThreadPoolExecutor(max_workers=len(self.providers)) as pool:
            futures = {}
            for provider in self.providers:
                future = pool.submit(self._run_single, task, provider, prompt)
                futures[future] = provider

            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    provider = futures[future]
                    tr = TaskResult(
                        task_id=task.id,
                        task_category=task.category,
                        task_difficulty=task.difficulty,
                        provider=provider.name,
                    )
                    tr.c3_response.error = str(e)
                    tr.baseline_response.error = str(e)
                    results.append(tr)

        return results

    def _run_task_sequential(self, task: E2ETask) -> list[TaskResult]:
        results = []
        prompt = build_prompt(task)

        for provider in self.providers:
            try:
                result = self._run_single(task, provider, prompt)
                results.append(result)
            except Exception as e:
                tr = TaskResult(
                    task_id=task.id,
                    task_category=task.category,
                    task_difficulty=task.difficulty,
                    provider=provider.name,
                )
                tr.c3_response.error = str(e)
                results.append(tr)

        return results

    def _run_single(self, task: E2ETask, provider: CLIProvider,
                    prompt: str) -> TaskResult:
        tr = TaskResult(
            task_id=task.id,
            task_category=task.category,
            task_difficulty=task.difficulty,
            provider=provider.name,
        )

        if self.verbose:
            print(f"  >> {provider.name:>7} | C3 + BASE | starting in parallel...", flush=True)

        # Run C3 and baseline concurrently — halves wall time per task
        # Always single-turn: multi-turn doubles wall time and causes timeouts
        with ThreadPoolExecutor(max_workers=2) as pool:
            c3_future = pool.submit(provider.run, prompt, self._sandbox_path, True, self.timeout, False)
            base_future = pool.submit(provider.run, prompt, self._sandbox_path, False, self.timeout, False)
            tr.c3_response = c3_future.result()
            tr.baseline_response = base_future.result()

        if self.verbose:
            self._print_call_result(provider.name, "C3  ", tr.c3_response)
            self._print_call_result(provider.name, "BASE", tr.baseline_response)

        if tr.c3_response.text:
            tr.c3_score = self.evaluator.score(tr.c3_response.text, task.ground_truth)
        if tr.baseline_response.text:
            tr.baseline_score = self.evaluator.score(tr.baseline_response.text, task.ground_truth)

        return tr

    def _print_call_result(self, provider: str, label: str, resp: CLIResponse):
        lat = f"{resp.latency_ms/1000:.1f}s"
        tok = resp.input_tokens + resp.output_tokens + resp.cache_read_tokens + resp.cache_creation_tokens
        tok_str = f"{tok:,} tok" if tok else "? tok"
        if resp.error:
            status = f"ERROR: {resp.error[:70]}"
        else:
            parts = [f"done {lat:>6}", f"{tok_str:>12}"]
            if resp.num_turns:
                parts.append(f"{resp.num_turns} turn{'s' if resp.num_turns != 1 else ''}")
            if resp.cost_usd:
                parts.append(f"${resp.cost_usd:.4f}")
            if resp.model_used:
                parts.append(f"[{resp.model_used[:20]}]")
            status = "  ".join(parts)
        print(f"  >> {provider:>7} | {label:<4} | {status}", flush=True)

    def _print_result(self, tr: TaskResult, current: int, total: int):
        eff = tr.efficiency()
        delta = tr.score_delta
        wins = getattr(self, "_wins", "?")
        c3_err = " [C3-ERR]" if tr.c3_response.error else ""
        base_err = " [BASE-ERR]" if tr.baseline_response.error else ""

        if _UNI:
            winner_str = "C3 \u2713 wins" if tr.c3_wins else "BASE  wins"
        else:
            winner_str = "C3 WINS  " if tr.c3_wins else "BASE WINS"

        time_saved_s = eff["time_saved_ms"] / 1000
        cost_saved = eff["cost_saved_usd"]

        print(
            f"  [{current:>2}/{total}] {winner_str} | {tr.provider} | "
            f"C3={tr.c3_score.combined_score:.3f}  Base={tr.baseline_score.combined_score:.3f}  "
            f"delta={delta:+.3f} | "
            f"time {time_saved_s:+.0f}s  cost {cost_saved:+.4f} | "
            f"[{wins}/{current} wins]{c3_err}{base_err}",
            flush=True,
        )

        # On C3 loss, show which dimension hurt most
        if not tr.c3_wins and not tr.c3_response.error:
            dims = {
                "file_mention":  tr.c3_score.file_mention_score  - tr.baseline_score.file_mention_score,
                "completeness":  tr.c3_score.completeness_score  - tr.baseline_score.completeness_score,
                "structural":    tr.c3_score.structural_score    - tr.baseline_score.structural_score,
                "keyword":       tr.c3_score.keyword_score       - tr.baseline_score.keyword_score,
            }
            worst_dim, worst_gap = min(dims.items(), key=lambda x: x[1])
            if worst_gap < -0.05:
                c3_v = getattr(tr.c3_score, f"{worst_dim}_score", 0)
                base_v = getattr(tr.baseline_score, f"{worst_dim}_score", 0)
                print(f"           weak dim: {worst_dim}  C3={c3_v:.2f}  Base={base_v:.2f}", flush=True)


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------
# Run History & Trends
# ---------------------------------------------------------------------------

def load_run_history(project_path: str, max_runs: int = 20) -> list[dict]:
    """Load past benchmark runs from .c3/e2e_benchmark/runs/ sorted newest-first."""
    runs_dir = Path(project_path) / ".c3" / "e2e_benchmark" / "runs"
    if not runs_dir.exists():
        return []

    run_files = sorted(runs_dir.glob("e2e_*.json"), reverse=True)[:max_runs]
    history = []
    for f in run_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = str(f)
            history.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return history


def compute_trends(current: dict, history: list[dict]) -> dict:
    """Compute trend data from current run + historical runs.

    Returns sparkline arrays, since-last-run deltas, and moving averages.
    """
    if not history:
        return {"available": False}

    # Build timeline: history (oldest-first) + current
    timeline = list(reversed(history)) + [current]

    # Extract key metrics across runs
    win_rates = []
    avg_deltas = []
    avg_c3_scores = []
    avg_base_scores = []
    total_costs_c3 = []
    total_costs_base = []
    timestamps = []
    mcp_ratios = []

    for run in timeline:
        sc = run.get("scorecard", {})
        eff = run.get("efficiency_summary", {})
        win_rates.append(sc.get("c3_win_rate", 0))
        avg_deltas.append(sc.get("avg_score_delta", 0))
        avg_c3_scores.append(sc.get("avg_score_c3", 0))
        avg_base_scores.append(sc.get("avg_score_baseline", 0))
        total_costs_c3.append(eff.get("total_cost_c3_usd", 0))
        total_costs_base.append(eff.get("total_cost_baseline_usd", 0))
        timestamps.append(run.get("timestamp", ""))
        mcp_ratios.append(run.get("mcp_ratio", run.get("tool_analysis", {}).get("summary", {}).get("mcp_ratio", 0)))

    # Since-last-run deltas (compare current vs most recent past run)
    prev = history[0]  # newest past run (history is newest-first)
    prev_sc = prev.get("scorecard", {})
    prev_eff = prev.get("efficiency_summary", {})
    cur_sc = current.get("scorecard", {})
    cur_eff = current.get("efficiency_summary", {})

    since_last = {
        "win_rate_delta": round(cur_sc.get("c3_win_rate", 0) - prev_sc.get("c3_win_rate", 0), 1),
        "score_delta_delta": round(
            cur_sc.get("avg_score_delta", 0) - prev_sc.get("avg_score_delta", 0), 3
        ),
        "avg_c3_delta": round(
            cur_sc.get("avg_score_c3", 0) - prev_sc.get("avg_score_c3", 0), 3
        ),
        "cost_saved_delta": round(
            cur_eff.get("total_cost_saved_usd", 0) - prev_eff.get("total_cost_saved_usd", 0), 4
        ),
        "token_saved_delta": (
            cur_eff.get("total_tokens_saved", 0) - prev_eff.get("total_tokens_saved", 0)
        ),
        "mcp_ratio_delta": round(
            current.get("mcp_ratio", 0) - prev.get("mcp_ratio", prev.get("tool_analysis", {}).get("summary", {}).get("mcp_ratio", 0)), 1
        ),
        "prev_timestamp": prev.get("timestamp", "unknown"),
        "prev_total_tasks": prev.get("total_results", 0),
    }

    # Per-category trends
    cur_cats = current.get("category_stats", {})
    prev_cats = prev.get("category_stats", {})
    category_trends = {}
    for cat in set(list(cur_cats.keys()) + list(prev_cats.keys())):
        cur_wr = cur_cats.get(cat, {}).get("win_rate_c3", 0)
        prev_wr = prev_cats.get(cat, {}).get("win_rate_c3", 0)
        cur_d = cur_cats.get(cat, {}).get("avg_score_delta", 0)
        prev_d = prev_cats.get(cat, {}).get("avg_score_delta", 0)
        category_trends[cat] = {
            "win_rate_delta": round(cur_wr - prev_wr, 1),
            "score_delta_delta": round(cur_d - prev_d, 3),
            "improving": cur_d > prev_d,
        }

    # Moving averages (3-run window)
    def _ma(arr, window=3):
        if len(arr) < window:
            return arr[-1] if arr else 0
        return round(sum(arr[-window:]) / window, 3)

    return {
        "available": True,
        "run_count": len(timeline),
        "sparklines": {
            "win_rates": [round(x, 1) for x in win_rates],
            "avg_deltas": [round(x, 3) for x in avg_deltas],
            "avg_c3_scores": [round(x, 3) for x in avg_c3_scores],
            "avg_base_scores": [round(x, 3) for x in avg_base_scores],
            "costs_c3": [round(x, 4) for x in total_costs_c3],
            "costs_base": [round(x, 4) for x in total_costs_base],
            "mcp_ratios": [round(x, 1) for x in mcp_ratios],
            "timestamps": timestamps,
        },
        "since_last": since_last,
        "category_trends": category_trends,
        "moving_averages": {
            "win_rate_3run": _ma(win_rates),
            "delta_3run": _ma(avg_deltas),
            "c3_score_3run": _ma(avg_c3_scores),
        },
    }


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_e2e_report(
    project_path: str,
    results: list[TaskResult],
    providers: list[CLIProvider],
    tasks: list[E2ETask],
) -> dict:
    """Generate comprehensive JSON report from benchmark results."""
    # Per-provider aggregation
    provider_stats = {}
    for p in providers:
        p_results = [r for r in results if r.provider == p.name]
        if not p_results:
            continue

        c3_wins = sum(1 for r in p_results if r.c3_wins)
        total = len(p_results)
        avg_c3 = sum(r.c3_score.combined_score for r in p_results) / total
        avg_base = sum(r.baseline_score.combined_score for r in p_results) / total
        avg_delta = sum(r.score_delta for r in p_results) / total

        # Weighted win rate
        weighted_c3_wins = sum(r.difficulty_weight for r in p_results if r.c3_wins)
        weighted_total = sum(r.difficulty_weight for r in p_results)

        total_c3_tokens = sum(
            r.c3_response.input_tokens + r.c3_response.output_tokens +
            r.c3_response.cache_creation_tokens + r.c3_response.cache_read_tokens
            for r in p_results
        )
        total_base_tokens = sum(
            r.baseline_response.input_tokens + r.baseline_response.output_tokens +
            r.baseline_response.cache_creation_tokens + r.baseline_response.cache_read_tokens
            for r in p_results
        )
        total_c3_cost = sum(r.c3_response.cost_usd for r in p_results)
        total_base_cost = sum(r.baseline_response.cost_usd for r in p_results)
        total_c3_latency = sum(r.c3_response.latency_ms for r in p_results)
        total_base_latency = sum(r.baseline_response.latency_ms for r in p_results)

        provider_stats[p.name] = {
            "model": p.model or "default",
            "tasks_run": total,
            "c3_wins": c3_wins,
            "baseline_wins": total - c3_wins,
            "win_rate_c3": round(c3_wins / total * 100, 1),
            "weighted_win_rate_c3": round(weighted_c3_wins / weighted_total * 100, 1) if weighted_total else 0,
            "avg_score_c3": round(avg_c3, 3),
            "avg_score_baseline": round(avg_base, 3),
            "avg_score_delta": round(avg_delta, 3),
            "total_tokens_c3": total_c3_tokens,
            "total_tokens_baseline": total_base_tokens,
            "total_cost_c3_usd": round(total_c3_cost, 4),
            "total_cost_baseline_usd": round(total_base_cost, 4),
            "avg_latency_c3_ms": round(total_c3_latency / total, 1),
            "avg_latency_baseline_ms": round(total_base_latency / total, 1),
        }

    # Per-category aggregation
    categories = sorted(set(r.task_category for r in results))
    category_stats = {}
    for cat in categories:
        cat_results = [r for r in results if r.task_category == cat]
        total = len(cat_results)
        c3_wins = sum(1 for r in cat_results if r.c3_wins)
        avg_delta = sum(r.score_delta for r in cat_results) / total
        avg_c3 = sum(r.c3_score.combined_score for r in cat_results) / total
        avg_base = sum(r.baseline_score.combined_score for r in cat_results) / total

        category_stats[cat] = {
            "tasks_run": total,
            "c3_wins": c3_wins,
            "win_rate_c3": round(c3_wins / total * 100, 1),
            "avg_score_c3": round(avg_c3, 3),
            "avg_score_baseline": round(avg_base, 3),
            "avg_score_delta": round(avg_delta, 3),
            "difficulty": cat_results[0].task_difficulty if cat_results else "unknown",
        }

    # Global scorecard
    total_results = len(results)
    total_c3_wins = sum(1 for r in results if r.c3_wins)
    global_avg_c3 = sum(r.c3_score.combined_score for r in results) / total_results if total_results else 0
    global_avg_base = sum(r.baseline_score.combined_score for r in results) / total_results if total_results else 0

    # Weighted win rate
    weighted_wins = sum(r.difficulty_weight for r in results if r.c3_wins)
    weighted_total = sum(r.difficulty_weight for r in results)
    weighted_win_rate = round(weighted_wins / weighted_total * 100, 1) if weighted_total else 0

    # Efficiency summary
    total_time_c3 = sum(r.c3_response.latency_ms for r in results)
    total_time_base = sum(r.baseline_response.latency_ms for r in results)
    total_cost_c3 = sum(r.c3_response.cost_usd for r in results)
    total_cost_base = sum(r.baseline_response.cost_usd for r in results)
    total_tokens_c3 = sum(
        r.c3_response.input_tokens + r.c3_response.output_tokens +
        r.c3_response.cache_creation_tokens + r.c3_response.cache_read_tokens
        for r in results
    )
    total_tokens_base = sum(
        r.baseline_response.input_tokens + r.baseline_response.output_tokens +
        r.baseline_response.cache_creation_tokens + r.baseline_response.cache_read_tokens
        for r in results
    )

    def _pct(saved, total):
        return round(saved / total * 100, 1) if total else 0.0

    efficiency_summary = {
        "total_time_c3_s": round(total_time_c3 / 1000, 1),
        "total_time_baseline_s": round(total_time_base / 1000, 1),
        "total_time_saved_s": round((total_time_base - total_time_c3) / 1000, 1),
        "time_saved_pct": _pct(total_time_base - total_time_c3, total_time_base),
        "avg_time_per_task_c3_s": round(total_time_c3 / total_results / 1000, 1) if total_results else 0,
        "avg_time_per_task_baseline_s": round(total_time_base / total_results / 1000, 1) if total_results else 0,
        "total_cost_c3_usd": round(total_cost_c3, 4),
        "total_cost_baseline_usd": round(total_cost_base, 4),
        "total_cost_saved_usd": round(total_cost_base - total_cost_c3, 4),
        "cost_saved_pct": _pct(total_cost_base - total_cost_c3, total_cost_base),
        "total_tokens_c3": total_tokens_c3,
        "total_tokens_baseline": total_tokens_base,
        "total_tokens_saved": total_tokens_base - total_tokens_c3,
        "tokens_saved_pct": _pct(total_tokens_base - total_tokens_c3, total_tokens_base),
        # Projections (assuming 5 sessions/day, 22 days/month)
        "projected_daily_cost_saved_usd": round((total_cost_base - total_cost_c3) * 5, 4),
        "projected_monthly_cost_saved_usd": round((total_cost_base - total_cost_c3) * 5 * 22, 2),
    }

    # Score breakdown by dimension (averaged across all results)
    dimensions = ["keyword_score", "structural_score", "file_mention_score",
                   "factual_score", "completeness_score"]
    dimension_breakdown = {}
    for dim in dimensions:
        c3_vals = [getattr(r.c3_score, dim, 0) for r in results]
        base_vals = [getattr(r.baseline_score, dim, 0) for r in results]
        dimension_breakdown[dim] = {
            "avg_c3": round(sum(c3_vals) / len(c3_vals), 3) if c3_vals else 0,
            "avg_baseline": round(sum(base_vals) / len(base_vals), 3) if base_vals else 0,
            "delta": round(
                (sum(c3_vals) / len(c3_vals) - sum(base_vals) / len(base_vals)), 3
            ) if c3_vals and base_vals else 0,
        }

    # Tool usage analysis
    tool_analysis = _build_tool_analysis(results)

    # Tool adoption: how many C3-mode runs actually used MCP tools?
    tasks_using_mcp = sum(1 for r in results if r.c3_response.tool_usage.c3_tool_calls > 0)
    unique_mcp_tools_used = set()
    for r in results:
        for tool_name in r.c3_response.tool_usage.tool_counts:
            if tool_name in _C3_TOOLS or tool_name.startswith("c3_"):
                unique_mcp_tools_used.add(tool_name)
    tool_adoption = {
        "tasks_using_mcp": tasks_using_mcp,
        "total_tasks": total_results,
        "adoption_rate": round(tasks_using_mcp / total_results * 100, 1) if total_results else 0,
        "unique_mcp_tools": sorted(unique_mcp_tools_used),
        "unique_mcp_tool_count": len(unique_mcp_tools_used),
    }

    report_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_path": project_path,
        "benchmark_type": "e2e_agent",
        "total_tasks": len(tasks),
        "total_results": total_results,
        "providers_tested": [p.name for p in providers],
        "scorecard": {
            "c3_win_rate": round(total_c3_wins / total_results * 100, 1) if total_results else 0,
            "weighted_win_rate": weighted_win_rate,
            "c3_wins": total_c3_wins,
            "baseline_wins": total_results - total_c3_wins,
            "avg_score_c3": round(global_avg_c3, 3),
            "avg_score_baseline": round(global_avg_base, 3),
            "avg_score_delta": round(global_avg_c3 - global_avg_base, 3),
        },
        "efficiency_summary": efficiency_summary,
        "dimension_breakdown": dimension_breakdown,
        "tool_analysis": tool_analysis,
        "provider_stats": provider_stats,
        "category_stats": category_stats,
        "tasks": [t.to_dict() for t in tasks],
        "results": [r.to_dict() for r in results],
    }

    # Promote mcp_ratio for easy trend access
    report_data["mcp_ratio"] = tool_analysis.get("summary", {}).get("mcp_ratio", 0)
    report_data["tool_adoption"] = tool_adoption

    # Generate insights from the assembled report
    report_data["insights"] = _build_insights(report_data)

    # Load history and compute trends
    history = load_run_history(project_path)
    report_data["trends"] = compute_trends(report_data, history)

    return report_data


def _build_tool_analysis(results: list[TaskResult]) -> dict:
    """Aggregate tool usage data across all results."""
    # Global tool counts
    c3_tool_totals: dict[str, int] = {}
    base_tool_totals: dict[str, int] = {}

    # Per-category tool usage
    category_tools: dict[str, dict] = {}

    # Aggregate totals
    total_c3_calls = 0
    total_base_calls = 0
    total_c3_unique = 0
    total_base_unique = 0
    total_c3_mcp = 0
    total_c3_native = 0
    total_base_native = 0

    for r in results:
        c3_tu = r.c3_response.tool_usage
        base_tu = r.baseline_response.tool_usage

        total_c3_calls += c3_tu.total_tool_calls
        total_base_calls += base_tu.total_tool_calls
        total_c3_unique += c3_tu.unique_tools
        total_base_unique += base_tu.unique_tools
        total_c3_mcp += c3_tu.c3_tool_calls
        total_c3_native += c3_tu.native_tool_calls
        total_base_native += base_tu.native_tool_calls

        for tool, count in c3_tu.tool_counts.items():
            c3_tool_totals[tool] = c3_tool_totals.get(tool, 0) + count
        for tool, count in base_tu.tool_counts.items():
            base_tool_totals[tool] = base_tool_totals.get(tool, 0) + count

        cat = r.task_category
        if cat not in category_tools:
            category_tools[cat] = {
                "c3_total_calls": 0, "base_total_calls": 0,
                "c3_mcp_calls": 0, "c3_native_calls": 0,
                "base_native_calls": 0,
            }
        category_tools[cat]["c3_total_calls"] += c3_tu.total_tool_calls
        category_tools[cat]["base_total_calls"] += base_tu.total_tool_calls
        category_tools[cat]["c3_mcp_calls"] += c3_tu.c3_tool_calls
        category_tools[cat]["c3_native_calls"] += c3_tu.native_tool_calls
        category_tools[cat]["base_native_calls"] += base_tu.native_tool_calls

    n = len(results) or 1

    # Top tools ranked by usage
    all_tools = sorted(
        set(list(c3_tool_totals.keys()) + list(base_tool_totals.keys())),
        key=lambda t: c3_tool_totals.get(t, 0) + base_tool_totals.get(t, 0),
        reverse=True,
    )
    tool_comparison = [
        {
            "tool": t,
            "c3_calls": c3_tool_totals.get(t, 0),
            "baseline_calls": base_tool_totals.get(t, 0),
            "is_c3_tool": t in _C3_TOOLS or t.startswith("c3_"),
            "delta": c3_tool_totals.get(t, 0) - base_tool_totals.get(t, 0),
        }
        for t in all_tools[:20]  # top 20
    ]

    # Tool diversity score: unique tools per task (higher = more diverse strategy)
    c3_diversity = round(total_c3_unique / n, 1)
    base_diversity = round(total_base_unique / n, 1)

    return {
        "summary": {
            "total_c3_tool_calls": total_c3_calls,
            "total_baseline_tool_calls": total_base_calls,
            "avg_c3_calls_per_task": round(total_c3_calls / n, 1),
            "avg_baseline_calls_per_task": round(total_base_calls / n, 1),
            "c3_mcp_calls": total_c3_mcp,
            "c3_native_calls": total_c3_native,
            "baseline_native_calls": total_base_native,
            "c3_tool_diversity": c3_diversity,
            "baseline_tool_diversity": base_diversity,
            "mcp_ratio": round(total_c3_mcp / total_c3_calls * 100, 1) if total_c3_calls else 0,
        },
        "tool_comparison": tool_comparison,
        "category_breakdown": category_tools,
        "c3_tool_totals": c3_tool_totals,
        "baseline_tool_totals": base_tool_totals,
    }


# ---------------------------------------------------------------------------
# Insights Engine
# ---------------------------------------------------------------------------

_INSIGHT_SEVERITY = {"strength": 0, "info": 1, "warning": 2, "critical": 3}


def _build_insights(report: dict) -> dict:
    """Analyze benchmark results and generate actionable insights."""
    findings: list[dict] = []
    sc = report.get("scorecard", {})
    eff = report.get("efficiency_summary", {})
    dims = report.get("dimension_breakdown", {})
    cats = report.get("category_stats", {})
    ta = report.get("tool_analysis", {}).get("summary", {})
    results = report.get("results", [])

    # --- Overall performance ---
    win_rate = sc.get("c3_win_rate", 0)
    weighted_wr = sc.get("weighted_win_rate", 0)
    delta = sc.get("avg_score_delta", 0)

    if win_rate >= 75:
        findings.append({
            "severity": "strength", "area": "overall",
            "title": "Strong C3 advantage",
            "detail": f"C3 wins {win_rate:.0f}% of tasks with an average score delta of {delta:+.3f}.",
            "action": "C3 MCP tools provide consistent quality improvements across tasks.",
        })
    elif win_rate >= 50:
        findings.append({
            "severity": "info", "area": "overall",
            "title": "Moderate C3 advantage",
            "detail": f"C3 wins {win_rate:.0f}% of tasks. Weighted win rate: {weighted_wr:.0f}%.",
            "action": "C3 helps on harder tasks. Consider which task categories benefit most.",
        })
    elif win_rate > 0:
        findings.append({
            "severity": "warning", "area": "overall",
            "title": "Baseline competitive",
            "detail": f"C3 only wins {win_rate:.0f}% of tasks (delta: {delta:+.3f}).",
            "action": "Review category breakdown — C3 may excel in specific areas but not globally.",
        })

    # Weighted vs unweighted divergence
    if abs(weighted_wr - win_rate) > 15:
        if weighted_wr > win_rate:
            findings.append({
                "severity": "strength", "area": "difficulty",
                "title": "C3 excels on harder tasks",
                "detail": f"Weighted win rate ({weighted_wr:.0f}%) significantly exceeds raw ({win_rate:.0f}%).",
                "action": "C3 tools provide more value on complex tasks where tool assistance matters most.",
            })
        else:
            findings.append({
                "severity": "warning", "area": "difficulty",
                "title": "C3 struggles on harder tasks",
                "detail": f"Weighted win rate ({weighted_wr:.0f}%) is below raw ({win_rate:.0f}%).",
                "action": "Investigate hard/expert task results — C3 tools may need optimization for complex queries.",
            })

    # --- Efficiency insights ---
    cost_pct = eff.get("cost_saved_pct", 0)
    token_pct = eff.get("tokens_saved_pct", 0)
    time_pct = eff.get("time_saved_pct", 0)
    monthly = eff.get("projected_monthly_cost_saved_usd", 0)

    if cost_pct > 20:
        findings.append({
            "severity": "strength", "area": "cost",
            "title": f"Significant cost reduction ({cost_pct:.0f}%)",
            "detail": f"C3 saves ${eff.get('total_cost_saved_usd', 0):.4f} per run. Projected monthly: ${monthly:.2f}.",
            "action": "Cost savings compound at scale — C3 pays for itself quickly.",
        })
    elif cost_pct < -20:
        findings.append({
            "severity": "warning", "area": "cost",
            "title": f"C3 increases cost ({abs(cost_pct):.0f}% more)",
            "detail": "C3 MCP tool calls add token overhead that exceeds native tool efficiency.",
            "action": "Consider if quality gains justify the cost. Review which C3 tools add most overhead.",
        })

    if token_pct > 15:
        findings.append({
            "severity": "strength", "area": "tokens",
            "title": f"Token efficient ({token_pct:.0f}% fewer tokens)",
            "detail": f"C3 uses {eff.get('total_tokens_saved', 0):,} fewer tokens across all tasks.",
            "action": "C3 compress/read tools reduce context window usage effectively.",
        })
    elif token_pct < -15:
        findings.append({
            "severity": "warning", "area": "tokens",
            "title": f"Higher token usage ({abs(token_pct):.0f}% more)",
            "detail": "C3 tool calls introduce additional tokens from MCP overhead.",
            "action": "Check if c3_compress is being used — it should reduce token consumption.",
        })

    if time_pct > 20:
        findings.append({
            "severity": "strength", "area": "speed",
            "title": f"Faster responses ({time_pct:.0f}% time saved)",
            "detail": f"C3 saves {eff.get('total_time_saved_s', 0):.0f}s total across all tasks.",
            "action": "C3 tools help the AI find answers faster with fewer tool-use turns.",
        })
    elif time_pct < -20:
        findings.append({
            "severity": "warning", "area": "speed",
            "title": f"Slower responses ({abs(time_pct):.0f}% slower)",
            "detail": "MCP tool round-trips add latency.",
            "action": "C3 tool startup overhead may dominate on simple tasks. Focus C3 on complex queries.",
        })

    # --- Dimension analysis ---
    for dim_name, dim_data in dims.items():
        d = dim_data.get("delta", 0)
        label = dim_name.replace("_score", "").replace("_", " ").title()
        if d > 0.1:
            findings.append({
                "severity": "strength", "area": "dimension",
                "title": f"Strong in {label} (+{d:.3f})",
                "detail": f"C3: {dim_data['avg_c3']:.3f} vs Baseline: {dim_data['avg_baseline']:.3f}.",
                "action": f"C3 tools enhance {label.lower()} — a clear differentiator.",
            })
        elif d < -0.1:
            findings.append({
                "severity": "warning", "area": "dimension",
                "title": f"Weak in {label} ({d:+.3f})",
                "detail": f"C3: {dim_data['avg_c3']:.3f} vs Baseline: {dim_data['avg_baseline']:.3f}.",
                "action": f"Investigate why C3 underperforms on {label.lower()}. May need tool improvements.",
            })

    # --- Category analysis ---
    weak_cats = []
    strong_cats = []
    for cat_name, cat_data in cats.items():
        wr = cat_data.get("win_rate_c3", 50)
        cat_label = cat_name.replace("_", " ").title()
        if wr >= 80:
            strong_cats.append(cat_label)
        elif wr <= 20:
            weak_cats.append((cat_label, cat_data.get("avg_score_delta", 0)))

    if strong_cats:
        findings.append({
            "severity": "strength", "area": "category",
            "title": f"Dominates in {', '.join(strong_cats)}",
            "detail": f"C3 wins 80%+ of tasks in these categories.",
            "action": "These are C3's sweet spots — consider marketing/documentation around these strengths.",
        })
    if weak_cats:
        cats_str = ", ".join(f"{c} ({d:+.3f})" for c, d in weak_cats)
        findings.append({
            "severity": "critical" if len(weak_cats) > 2 else "warning",
            "area": "category",
            "title": f"Weak categories: {', '.join(c for c, _ in weak_cats)}",
            "detail": f"C3 wins 20% or less: {cats_str}.",
            "action": "Priority improvement areas. Analyze response comparisons for these categories.",
        })

    # --- Tool usage insights ---
    mcp_ratio = ta.get("mcp_ratio", 0)
    c3_diversity = ta.get("c3_tool_diversity", 0)
    base_diversity = ta.get("baseline_tool_diversity", 0)

    if mcp_ratio > 0 and mcp_ratio < 20:
        findings.append({
            "severity": "warning", "area": "tools",
            "title": f"Low MCP tool utilization ({mcp_ratio:.0f}%)",
            "detail": "C3 MCP tools are available but rarely used by the AI.",
            "action": "The AI may not know about C3 tools. Check CLAUDE.md instructions and tool descriptions.",
        })
    elif mcp_ratio >= 50:
        findings.append({
            "severity": "strength", "area": "tools",
            "title": f"Heavy MCP utilization ({mcp_ratio:.0f}%)",
            "detail": "The AI actively leverages C3 MCP tools over native alternatives.",
            "action": "Good adoption. C3 tools are being discovered and preferred.",
        })

    # --- Tool adoption rate ---
    adoption = report.get("tool_adoption", {})
    adoption_rate = adoption.get("adoption_rate", 0)
    if adoption_rate < 50:
        findings.append({
            "severity": "warning", "area": "adoption",
            "title": f"Low C3 tool adoption ({adoption_rate:.0f}% of tasks)",
            "detail": f"Only {adoption.get('tasks_using_mcp', 0)}/{adoption.get('total_tasks', 0)} C3-mode runs used any MCP tools.",
            "action": "Strengthen prompt instructions or check if CLAUDE.md C3 mandate is being loaded.",
        })
    elif adoption_rate >= 80:
        findings.append({
            "severity": "strength", "area": "adoption",
            "title": f"High C3 tool adoption ({adoption_rate:.0f}%)",
            "detail": f"{adoption.get('tasks_using_mcp', 0)}/{adoption.get('total_tasks', 0)} tasks used C3 MCP tools. {adoption.get('unique_mcp_tool_count', 0)} unique tools.",
            "action": "Good adoption across tasks.",
        })

    if c3_diversity > base_diversity + 1:
        findings.append({
            "severity": "info", "area": "tools",
            "title": "C3 enables broader tool strategy",
            "detail": f"C3 mode uses {c3_diversity:.1f} unique tools/task vs {base_diversity:.1f} baseline.",
            "action": "More diverse tool usage suggests C3 provides richer exploration capabilities.",
        })

    # --- Context pressure warnings ---
    # Flag any task where peak estimated context fill exceeded 70 % in either run.
    _PRESSURE_WARN = 70.0
    high_pressure = []
    for r in results:
        for label, resp_key in (("c3", "c3_response"), ("baseline", "baseline_response")):
            resp = r.get(resp_key, {})
            pct = resp.get("context_pressure_pct", 0)
            if pct >= _PRESSURE_WARN:
                high_pressure.append((r.get("task_id", "?"), label, pct,
                                      resp.get("num_turns", 0),
                                      resp.get("total_tokens", 0)))
    if high_pressure:
        worst = max(high_pressure, key=lambda x: x[2])
        task_list = ", ".join(f"{t} ({l}, {p:.0f}%)" for t, l, p, *_ in high_pressure)
        findings.append({
            "severity": "warning" if worst[2] < 90 else "critical",
            "area": "context_pressure",
            "title": f"{len(high_pressure)} task run(s) under high context pressure",
            "detail": (
                f"Peak context fill ≥{_PRESSURE_WARN:.0f}% in: {task_list}. "
                f"Worst: '{worst[0]}' ({worst[1]}) at ~{worst[2]:.0f}% over {worst[3]} turns "
                f"({worst[4]:,} cumulative tokens)."
            ),
            "action": (
                "High context pressure degrades output quality in later turns. "
                "Consider splitting long tasks, enabling C3 snapshots mid-task, "
                "or adding a max-turn limit to the benchmark runner."
            ),
        })

    # --- Token count reliability ---
    partial_runs = []
    for r in results:
        for label, resp_key in (("c3", "c3_response"), ("baseline", "baseline_response")):
            resp = r.get(resp_key, {})
            if resp.get("token_count_source") == "partial":
                reported = resp.get("cost_usd", 0)
                computed = resp.get("computed_cost_usd") or 0
                partial_runs.append((r.get("task_id", "?"), label, reported, computed))
    if partial_runs:
        run_list = "; ".join(
            f"{t} ({l}): reported ${rep:.4f} vs computed ${cmp:.4f}"
            for t, l, rep, cmp in partial_runs
        )
        findings.append({
            "severity": "warning",
            "area": "data_quality",
            "title": f"Token counts unreliable for {len(partial_runs)} run(s)",
            "detail": (
                f"cost_usd (cumulative) and token counts (partial) disagree by >$0.01. "
                f"Affected: {run_list}. Likely cause: 'usage' block reflects last turn only "
                f"while total_cost_usd is session-cumulative. Token savings % may be understated."
            ),
            "action": (
                "Token savings comparisons for affected baseline runs are undercounted. "
                "True C3 token savings are likely higher than reported. "
                "Fix: ensure 'modelUsage' block is present in Claude JSON output, "
                "or sum per-turn usage blocks in the benchmark runner."
            ),
        })

    # --- Per-result outlier detection ---
    if results:
        biggest_c3_win = max(results, key=lambda r: r.get("score_delta", 0))
        biggest_base_win = min(results, key=lambda r: r.get("score_delta", 0))

        if biggest_c3_win.get("score_delta", 0) > 0.2:
            findings.append({
                "severity": "info", "area": "outlier",
                "title": f"Biggest C3 win: {biggest_c3_win['task_id']}",
                "detail": f"Delta: {biggest_c3_win['score_delta']:+.3f} ({biggest_c3_win['task_category']}).",
                "action": "Expand response comparison to understand what C3 did differently.",
            })
        if biggest_base_win.get("score_delta", 0) < -0.2:
            findings.append({
                "severity": "warning", "area": "outlier",
                "title": f"Biggest C3 loss: {biggest_base_win['task_id']}",
                "detail": f"Delta: {biggest_base_win['score_delta']:+.3f} ({biggest_base_win['task_category']}).",
                "action": "Review this task — C3 tools may have misled the AI or added noise.",
            })

    # Sort: critical first, then warning, info, strength
    findings.sort(key=lambda f: _INSIGHT_SEVERITY.get(f.get("severity", "info"), 1), reverse=True)

    # Summary verdict
    n_critical = sum(1 for f in findings if f["severity"] == "critical")
    n_warnings = sum(1 for f in findings if f["severity"] == "warning")
    n_strengths = sum(1 for f in findings if f["severity"] == "strength")

    if n_critical > 0:
        verdict = "C3 has critical weak spots that need attention before production use."
    elif n_warnings > n_strengths:
        verdict = "C3 shows mixed results. Focus on weak categories and dimensions."
    elif n_strengths > 0 and n_warnings == 0:
        verdict = "C3 provides clear, consistent improvements across the board."
    elif n_strengths > n_warnings:
        verdict = "C3 is net positive with some areas for improvement."
    else:
        verdict = "Results are inconclusive. Consider running more tasks or harder categories."

    return {
        "verdict": verdict,
        "findings": findings,
        "counts": {
            "critical": n_critical,
            "warnings": n_warnings,
            "strengths": n_strengths,
            "info": sum(1 for f in findings if f["severity"] == "info"),
        },
    }


# ---------------------------------------------------------------------------
# HTML Report
# ---------------------------------------------------------------------------

def render_e2e_html(report: dict) -> str:
    """Render a comprehensive visual HTML report for the E2E benchmark."""
    sc = report["scorecard"]
    eff = report.get("efficiency_summary", {})
    dims = report.get("dimension_breakdown", {})
    providers = report.get("provider_stats", {})
    categories = report.get("category_stats", {})
    results = report.get("results", [])
    timestamp = report.get("timestamp", "")
    tool_analysis = report.get("tool_analysis", {})
    insights = report.get("insights", {})
    trends = report.get("trends", {})

    provider_names = list(providers.keys())
    c3_scores = [providers[p]["avg_score_c3"] for p in provider_names]
    base_scores = [providers[p]["avg_score_baseline"] for p in provider_names]
    win_rates = [providers[p]["win_rate_c3"] for p in provider_names]

    cat_names = list(categories.keys())
    cat_deltas = [categories[c]["avg_score_delta"] for c in cat_names]
    cat_c3 = [categories[c].get("avg_score_c3", 0) for c in cat_names]
    cat_base = [categories[c].get("avg_score_baseline", 0) for c in cat_names]

    dim_names = list(dims.keys())
    dim_c3 = [dims[d]["avg_c3"] for d in dim_names]
    dim_base = [dims[d]["avg_baseline"] for d in dim_names]
    dim_labels = [d.replace("_score", "").replace("_", " ").title() for d in dim_names]

    # Efficiency cards
    eff_time_saved = eff.get("total_time_saved_s", 0)
    eff_cost_saved = eff.get("total_cost_saved_usd", 0)
    eff_tokens_saved = eff.get("total_tokens_saved", 0)

    # Tool usage data
    ta_summary = tool_analysis.get("summary", {})
    ta_comparison = tool_analysis.get("tool_comparison", [])
    ta_categories = tool_analysis.get("category_breakdown", {})

    # Build tool comparison table rows
    tool_rows = ""
    for tc in ta_comparison:
        is_c3 = tc.get("is_c3_tool", False)
        badge = '<span class="c3-badge">C3</span>' if is_c3 else ""
        delta = tc.get("delta", 0)
        delta_class = "positive" if delta > 0 else ("negative" if delta < 0 else "")
        tool_rows += f"""
        <tr>
          <td>{tc['tool']} {badge}</td>
          <td>{tc['c3_calls']}</td>
          <td>{tc['baseline_calls']}</td>
          <td class="{delta_class}">{delta:+d}</td>
        </tr>"""

    # Tool category chart data
    ta_cat_names = list(ta_categories.keys())
    ta_cat_c3_mcp = [ta_categories[c].get("c3_mcp_calls", 0) for c in ta_cat_names]
    ta_cat_c3_native = [ta_categories[c].get("c3_native_calls", 0) for c in ta_cat_names]
    ta_cat_base_native = [ta_categories[c].get("base_native_calls", 0) for c in ta_cat_names]

    # Insights HTML
    insights_html = ""
    for finding in insights.get("findings", []):
        sev = finding.get("severity", "info")
        insights_html += f"""
        <div class="insight {sev}">
          <div class="insight-title">{_html_escape(finding.get('title', ''))}</div>
          <div class="insight-detail">{_html_escape(finding.get('detail', ''))}</div>
          <div class="insight-action">{_html_escape(finding.get('action', ''))}</div>
        </div>"""

    verdict = insights.get("verdict", "")
    ic = insights.get("counts", {})
    verdict_counts = f"{ic.get('strengths', 0)} strengths, {ic.get('warnings', 0)} warnings, {ic.get('critical', 0)} critical"

    # Trend data
    has_trends = trends.get("available", False)
    sl = trends.get("since_last", {})
    sparklines = trends.get("sparklines", {})
    cat_trends = trends.get("category_trends", {})

    # Since-last-run delta badges for scorecard
    def _delta_badge(val, fmt="+.1f", suffix="", invert=False):
        """Generate an HTML delta badge. invert=True means lower is better."""
        if not has_trends or val == 0:
            return ""
        good = val < 0 if invert else val > 0
        cls = "positive" if good else "negative"
        return f'<div class="delta-badge {cls}">{val:{fmt}}{suffix}</div>'

    wr_delta_badge = _delta_badge(sl.get("win_rate_delta", 0), "+.1f", "pp")
    delta_delta_badge = _delta_badge(sl.get("score_delta_delta", 0), "+.3f")
    c3_delta_badge = _delta_badge(sl.get("avg_c3_delta", 0), "+.3f")
    cost_delta_badge = _delta_badge(sl.get("cost_saved_delta", 0), "+.4f", "")

    # Category trend arrows
    cat_trend_arrows = {}
    for cat_name, ct in cat_trends.items():
        d = ct.get("score_delta_delta", 0)
        if d > 0.01:
            cat_trend_arrows[cat_name] = '<span class="trend-up">&#9650;</span>'
        elif d < -0.01:
            cat_trend_arrows[cat_name] = '<span class="trend-down">&#9660;</span>'
        else:
            cat_trend_arrows[cat_name] = '<span class="trend-flat">&#9654;</span>'

    # Pre-build trend HTML section (avoids nested f-string issues on Python <3.12)
    trend_section_html = ""
    trend_charts_js = ""
    if has_trends:
        run_count = trends.get("run_count", 0)
        sl_ts = sl.get("prev_timestamp", "?")
        sl_wr = sl.get("win_rate_delta", 0)
        sl_sd = sl.get("score_delta_delta", 0)
        sl_cs = sl.get("cost_saved_delta", 0)
        sl_ts_saved = sl.get("token_saved_delta", 0)
        trend_section_html = (
            f'<h2 class="section-title">Trend Analysis ({run_count} runs)</h2>\n'
            f'<div class="since-last">\n'
            f'  Since last run ({sl_ts}):\n'
            f'  Win rate {sl_wr:+.1f}pp |\n'
            f'  Score delta {sl_sd:+.3f} |\n'
            f'  Cost saved {sl_cs:+.4f} USD |\n'
            f'  Tokens saved {sl_ts_saved:+,d} |\n'
            f'  MCP ratio {sl.get("mcp_ratio_delta", 0):+.1f}pp\n'
            f'</div>\n'
            f'<div class="trend-section">\n'
            f'  <div class="sparkline-grid">\n'
            f'    <div class="card"><h3>Win Rate Over Time</h3><canvas id="trendWinRate"></canvas></div>\n'
            f'    <div class="card"><h3>Score Delta Over Time</h3><canvas id="trendDelta"></canvas></div>\n'
            f'    <div class="card"><h3>Avg Scores Over Time</h3><canvas id="trendScores"></canvas></div>\n'
            f'    <div class="card"><h3>Cost Per Run Over Time</h3><canvas id="trendCost"></canvas></div>\n'
            f'    <div class="card"><h3>MCP Ratio Over Time</h3><canvas id="trendMcpRatio"></canvas></div>\n'
            f'  </div>\n'
            f'</div>\n'
        )
        sp_ts = json.dumps(sparklines.get("timestamps", []))
        sp_wr = json.dumps(sparklines.get("win_rates", []))
        sp_ad = json.dumps(sparklines.get("avg_deltas", []))
        sp_c3 = json.dumps(sparklines.get("avg_c3_scores", []))
        sp_bs = json.dumps(sparklines.get("avg_base_scores", []))
        sp_cc = json.dumps(sparklines.get("costs_c3", []))
        sp_cb = json.dumps(sparklines.get("costs_base", []))
        sp_mr = json.dumps(sparklines.get("mcp_ratios", []))
        trend_charts_js = (
            f"const trendLabels = {sp_ts}.map(t => t ? t.slice(5,16) : '');\n"
            f"const sparkOpts = {{ ...chartOpts, plugins:{{ legend:{{ display:false }} }}, "
            f"scales:{{ x:{{ display:false }}, y:{{ grid:{{ color:'#1e1e2e' }} }} }}, "
            f"elements:{{ point:{{ radius:2 }}, line:{{ tension:0.3 }} }} }};\n\n"
            f"new Chart(document.getElementById('trendWinRate'), {{\n"
            f"  type:'line', data:{{ labels:trendLabels,\n"
            f"    datasets:[{{ data:{sp_wr}, borderColor:C3, borderWidth:2, fill:false }}]\n"
            f"  }}, options:{{ ...sparkOpts, scales:{{ ...sparkOpts.scales, y:{{ ...sparkOpts.scales.y, min:0, max:100 }} }} }}\n"
            f"}});\n\n"
            f"new Chart(document.getElementById('trendDelta'), {{\n"
            f"  type:'line', data:{{ labels:trendLabels,\n"
            f"    datasets:[{{ data:{sp_ad}, borderColor:ACCENT, borderWidth:2, fill:true, backgroundColor:ACCENT+'22' }}]\n"
            f"  }}, options:sparkOpts\n"
            f"}});\n\n"
            f"new Chart(document.getElementById('trendScores'), {{\n"
            f"  type:'line', data:{{ labels:trendLabels,\n"
            f"    datasets:[\n"
            f"      {{ label:'C3', data:{sp_c3}, borderColor:C3, borderWidth:2, fill:false }},\n"
            f"      {{ label:'Baseline', data:{sp_bs}, borderColor:BASE, borderWidth:2, fill:false }}\n"
            f"    ]\n"
            f"  }}, options:{{ ...sparkOpts, plugins:{{ legend:{{ display:true, labels:{{ color:'#888' }} }} }} }}\n"
            f"}});\n\n"
            f"new Chart(document.getElementById('trendCost'), {{\n"
            f"  type:'line', data:{{ labels:trendLabels,\n"
            f"    datasets:[\n"
            f"      {{ label:'C3', data:{sp_cc}, borderColor:C3, borderWidth:2, fill:false }},\n"
            f"      {{ label:'Baseline', data:{sp_cb}, borderColor:BASE, borderWidth:2, fill:false }}\n"
            f"    ]\n"
            f"  }}, options:{{ ...sparkOpts, plugins:{{ legend:{{ display:true, labels:{{ color:'#888' }} }} }} }}\n"
            f"}});\n\n"
            f"new Chart(document.getElementById('trendMcpRatio'), {{\n"
            f"  type:'line', data:{{ labels:trendLabels,\n"
            f"    datasets:[{{ data:{sp_mr}, borderColor:ACCENT, borderWidth:2, fill:true, backgroundColor:ACCENT+'22' }}]\n"
            f"  }}, options:{{ ...sparkOpts, scales:{{ ...sparkOpts.scales, y:{{ ...sparkOpts.scales.y, min:0, max:100 }} }} }}\n"
            f"}});\n"
        )

    # Results table
    result_rows = ""
    for r in results:
        c3_s = r["c3_score"]["combined_score"]
        base_s = r["baseline_score"]["combined_score"]
        delta = r["score_delta"]
        winner = "C3" if r["c3_wins"] else "Baseline"
        winner_class = "c3-win" if r["c3_wins"] else "base-win"
        e = r.get("efficiency", {})
        time_saved = e.get("time_saved_ms", 0) / 1000
        cost_saved = e.get("cost_saved_usd", 0)

        c3_tu = r.get("c3_response", {}).get("tool_usage", {})
        base_tu = r.get("baseline_response", {}).get("tool_usage", {})
        c3_tools = c3_tu.get("total_tool_calls", 0)
        base_tools = base_tu.get("total_tool_calls", 0)

        result_rows += f"""
        <tr class="{winner_class}">
          <td>{r['provider']}</td>
          <td>{r['task_id']}</td>
          <td>{r['task_category']}</td>
          <td><span class="diff-badge">{r.get('task_difficulty','?')}</span></td>
          <td>{c3_s:.3f}</td>
          <td>{base_s:.3f}</td>
          <td>{delta:+.3f}</td>
          <td>{winner}</td>
          <td>{time_saved:+.1f}s</td>
          <td>${cost_saved:+.3f}</td>
          <td>{c3_tools}/{base_tools}</td>
        </tr>"""

    # Provider detail cards
    provider_cards = ""
    for pname, pdata in providers.items():
        provider_cards += f"""
        <div class="card provider-card">
          <h3>{pname.title()}</h3>
          <div class="stat-row"><span class="label">Model</span><span class="value">{pdata['model']}</span></div>
          <div class="stat-row"><span class="label">Win Rate</span><span class="value highlight">{pdata['win_rate_c3']:.1f}%</span></div>
          <div class="stat-row"><span class="label">Weighted Win Rate</span><span class="value">{pdata.get('weighted_win_rate_c3', 0):.1f}%</span></div>
          <div class="stat-row"><span class="label">Avg Score (C3/Base)</span><span class="value">{pdata['avg_score_c3']:.3f} / {pdata['avg_score_baseline']:.3f}</span></div>
          <div class="stat-row"><span class="label">Score Delta</span><span class="value {'positive' if pdata['avg_score_delta'] >= 0 else 'negative'}">{pdata['avg_score_delta']:+.3f}</span></div>
          <div class="stat-row"><span class="label">Tokens (C3/Base)</span><span class="value">{pdata['total_tokens_c3']:,} / {pdata['total_tokens_baseline']:,}</span></div>
          <div class="stat-row"><span class="label">Cost (C3/Base)</span><span class="value">${pdata['total_cost_c3_usd']:.4f} / ${pdata['total_cost_baseline_usd']:.4f}</span></div>
          <div class="stat-row"><span class="label">Avg Latency (C3/Base)</span><span class="value">{pdata['avg_latency_c3_ms']/1000:.1f}s / {pdata['avg_latency_baseline_ms']/1000:.1f}s</span></div>
        </div>"""

    # Response comparison (expandable details)
    comparison_html = ""
    for r in results:
        c3_text = r["c3_response"].get("response_text", "")
        base_text = r["baseline_response"].get("response_text", "")
        c3_s = r["c3_score"]["combined_score"]
        base_s = r["baseline_score"]["combined_score"]
        delta = r["score_delta"]
        tag = "c3-win" if r["c3_wins"] else "base-win"
        if c3_text or base_text:
            # Truncate for display
            c3_display = (c3_text[:2000] + "...") if len(c3_text) > 2000 else c3_text
            base_display = (base_text[:2000] + "...") if len(base_text) > 2000 else base_text
            e = r.get("efficiency", {})
            comparison_html += f"""
        <details class="comparison-item">
          <summary class="{tag}">
            <strong>{r['task_id']}</strong> ({r['provider']}) —
            C3: {c3_s:.3f} vs Base: {base_s:.3f} ({delta:+.3f})
            | Time: {e.get('time_saved_ms',0)/1000:+.1f}s | Cost: ${e.get('cost_saved_usd',0):+.3f}
          </summary>
          <div class="comparison-grid">
            <div class="comparison-col">
              <h4 style="color:var(--c3)">C3 Response ({r['c3_response']['latency_ms']/1000:.1f}s, ${r['c3_response']['cost_usd']:.4f})</h4>
              <pre>{_html_escape(c3_display)}</pre>
              <div class="score-details">
                Keyword: {r['c3_score']['keyword_score']:.2f} |
                Structural: {r['c3_score']['structural_score']:.2f} |
                Files: {r['c3_score']['file_mention_score']:.2f} |
                Factual: {r['c3_score']['factual_score']:.2f} |
                Complete: {r['c3_score']['completeness_score']:.2f}
              </div>
            </div>
            <div class="comparison-col">
              <h4 style="color:var(--base)">Baseline Response ({r['baseline_response']['latency_ms']/1000:.1f}s, ${r['baseline_response']['cost_usd']:.4f})</h4>
              <pre>{_html_escape(base_display)}</pre>
              <div class="score-details">
                Keyword: {r['baseline_score']['keyword_score']:.2f} |
                Structural: {r['baseline_score']['structural_score']:.2f} |
                Files: {r['baseline_score']['file_mention_score']:.2f} |
                Factual: {r['baseline_score']['factual_score']:.2f} |
                Complete: {r['baseline_score']['completeness_score']:.2f}
              </div>
            </div>
          </div>
        </details>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C3 E2E Benchmark Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{
    --bg: #0a0a0f; --surface: #12121a; --border: #1e1e2e;
    --text: #e0e0e0; --dim: #888; --accent: #6c5ce7;
    --c3: #00b894; --base: #e17055; --neutral: #636e72;
    --positive: #00b894; --negative: #e17055;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 24px; max-width: 1400px; margin: 0 auto; }}
  .header {{ text-align: center; padding: 32px 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }}
  .header h1 {{ font-size: 28px; font-weight: 300; color: var(--accent); }}
  .header .meta {{ color: var(--dim); font-size: 13px; margin-top: 8px; }}

  .scorecard {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .scorecard .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; text-align: center; }}
  .scorecard .card .big {{ font-size: 32px; font-weight: 700; }}
  .scorecard .card .label {{ font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}

  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card h3 {{ font-size: 15px; font-weight: 500; margin-bottom: 12px; color: var(--accent); }}
  .card canvas {{ max-height: 260px; }}

  .provider-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .provider-card .stat-row {{ display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 13px; }}
  .provider-card .stat-row .label {{ color: var(--dim); }}
  .provider-card .highlight {{ color: var(--accent); font-weight: 700; }}
  .positive {{ color: var(--positive); }}
  .negative {{ color: var(--negative); }}

  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid var(--border); color: var(--dim); text-transform: uppercase; font-size: 10px; letter-spacing: 1px; }}
  td {{ padding: 6px; border-bottom: 1px solid var(--border); }}
  tr.c3-win td:nth-child(8) {{ color: var(--c3); font-weight: 600; }}
  tr.base-win td:nth-child(8) {{ color: var(--base); font-weight: 600; }}
  tr:hover {{ background: rgba(108, 92, 231, 0.05); }}
  .diff-badge {{ font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--border); }}

  .section-title {{ font-size: 18px; font-weight: 400; margin: 24px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }}

  .tool-summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }}
  .tool-summary .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; text-align: center; }}
  .tool-summary .stat .num {{ font-size: 24px; font-weight: 700; }}
  .tool-summary .stat .lbl {{ font-size: 10px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }}
  .c3-badge {{ font-size: 9px; padding: 1px 5px; border-radius: 3px; background: var(--c3); color: #000; font-weight: 700; vertical-align: middle; margin-left: 4px; }}
  .tool-table {{ margin-top: 12px; }}

  .insight {{ padding: 12px 16px; border-radius: 8px; margin-bottom: 8px; border-left: 4px solid; }}
  .insight.critical {{ background: rgba(225,112,85,0.1); border-color: #e17055; }}
  .insight.warning {{ background: rgba(253,203,110,0.1); border-color: #fdcb6e; }}
  .insight.strength {{ background: rgba(0,184,148,0.1); border-color: #00b894; }}
  .insight.info {{ background: rgba(108,92,231,0.1); border-color: #6c5ce7; }}
  .insight .insight-title {{ font-weight: 600; font-size: 14px; margin-bottom: 4px; }}
  .insight.critical .insight-title {{ color: #e17055; }}
  .insight.warning .insight-title {{ color: #fdcb6e; }}
  .insight.strength .insight-title {{ color: #00b894; }}
  .insight.info .insight-title {{ color: #6c5ce7; }}
  .insight .insight-detail {{ font-size: 12px; color: var(--dim); }}
  .insight .insight-action {{ font-size: 12px; color: var(--text); margin-top: 4px; font-style: italic; }}
  .verdict {{ background: var(--surface); border: 2px solid var(--accent); border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; text-align: center; }}
  .verdict .verdict-text {{ font-size: 16px; font-weight: 400; color: var(--accent); }}
  .verdict .verdict-counts {{ font-size: 12px; color: var(--dim); margin-top: 6px; }}

  .guide {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 24px; }}
  .guide summary {{ cursor: pointer; padding: 14px 20px; font-size: 15px; font-weight: 500; color: var(--accent); }}
  .guide .guide-content {{ padding: 0 20px 16px; font-size: 13px; line-height: 1.7; color: var(--dim); }}
  .guide .guide-content h4 {{ color: var(--text); margin: 12px 0 4px; font-size: 14px; }}
  .guide .guide-content dt {{ color: var(--text); font-weight: 600; margin-top: 8px; }}
  .guide .guide-content dd {{ margin-left: 16px; margin-bottom: 4px; }}

  .delta-badge {{ font-size: 10px; padding: 1px 6px; border-radius: 4px; margin-top: 4px; display: inline-block; }}
  .delta-badge.positive {{ background: rgba(0,184,148,0.15); color: var(--positive); }}
  .delta-badge.negative {{ background: rgba(225,112,85,0.15); color: var(--negative); }}
  .trend-up {{ color: var(--positive); font-size: 10px; }}
  .trend-down {{ color: var(--negative); font-size: 10px; }}
  .trend-flat {{ color: var(--dim); font-size: 10px; }}
  .trend-section {{ margin-bottom: 24px; }}
  .trend-section .sparkline-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
  .trend-section canvas {{ max-height: 140px; }}
  .since-last {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 16px; margin-bottom: 12px; font-size: 13px; color: var(--dim); }}

  .comparison-item {{ margin-bottom: 8px; }}
  .comparison-item summary {{ cursor: pointer; padding: 8px 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; font-size: 13px; }}
  .comparison-item summary.c3-win {{ border-left: 3px solid var(--c3); }}
  .comparison-item summary.base-win {{ border-left: 3px solid var(--base); }}
  .comparison-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; }}
  .comparison-col {{ background: var(--bg); border-radius: 8px; padding: 12px; overflow: hidden; }}
  .comparison-col h4 {{ font-size: 13px; margin-bottom: 8px; }}
  .comparison-col pre {{ font-size: 11px; white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow-y: auto; color: var(--dim); }}
  .score-details {{ font-size: 10px; color: var(--dim); margin-top: 8px; padding-top: 6px; border-top: 1px solid var(--border); }}
</style>
</head>
<body>
<div class="header">
  <h1>C3 End-to-End Benchmark</h1>
  <div class="meta">{timestamp} | {report.get('project_path', '')} | Providers: {', '.join(report.get('providers_tested', []))}</div>
</div>

<details class="guide">
  <summary>How to Read This Report</summary>
  <div class="guide-content">
    <h4>What This Benchmark Measures</h4>
    <p>Each task is run twice against each AI provider: once with C3 MCP tools enabled, once without (baseline).
    Both modes have full native tool access (Read, Grep, Bash, etc.). The only difference is whether C3's
    specialized tools (c3_search, c3_compress, c3_read, etc.) are available.</p>

    <h4>Key Metrics</h4>
    <dl>
      <dt>Win Rate</dt>
      <dd>Percentage of tasks where C3 mode scored higher than baseline. >60% = good, >75% = strong.</dd>
      <dt>Weighted Win Rate</dt>
      <dd>Same as win rate but harder tasks count more (easy=0.5x, medium=1x, hard=2x, expert=3x). If this exceeds raw win rate, C3 is better at hard tasks.</dd>
      <dt>Score Delta</dt>
      <dd>Average difference between C3 and baseline scores. Positive = C3 better. >+0.05 is meaningful.</dd>
      <dt>Efficiency Metrics</dt>
      <dd>Time, cost, and token savings. Positive values mean C3 used fewer resources.</dd>
    </dl>

    <h4>Scoring Dimensions (0.0 - 1.0)</h4>
    <dl>
      <dt>Keyword</dt><dd>Required terms present in response, forbidden terms absent.</dd>
      <dt>Structural</dt><dd>Code blocks, file references, line numbers, organized structure.</dd>
      <dt>File Mention</dt><dd>Expected files and symbols referenced correctly.</dd>
      <dt>Hallucination</dt><dd>1.0 = no fabricated file paths or symbols. Lower = invented references.</dd>
      <dt>Factual</dt><dd>Verifiable claims about the codebase matched against ground truth.</dd>
      <dt>Completeness</dt><dd>All required aspects of the question addressed.</dd>
    </dl>

    <h4>Tool Usage Analysis</h4>
    <p>Shows which tools each mode used. "MCP Ratio" is the percentage of C3 tool calls that used C3-specific
    tools. Higher MCP ratio + higher score = C3 tools are being used effectively. Low MCP ratio may mean
    the AI isn't discovering C3 tools.</p>

    <h4>Reading the Insights</h4>
    <p>Insights are auto-generated from the data. Colors indicate severity:
    <span style="color:#00b894">green = strength</span>,
    <span style="color:#6c5ce7">purple = info</span>,
    <span style="color:#fdcb6e">yellow = warning</span>,
    <span style="color:#e17055">red = critical</span>.
    Each insight includes an actionable recommendation.</p>
  </div>
</details>

<div class="verdict">
  <div class="verdict-text">{_html_escape(verdict)}</div>
  <div class="verdict-counts">{verdict_counts}</div>
</div>

<div class="scorecard">
  <div class="card">
    <div class="big" style="color: var(--c3)">{sc['c3_win_rate']:.0f}%</div>
    <div class="label">C3 Win Rate</div>
    {wr_delta_badge}
  </div>
  <div class="card">
    <div class="big">{sc.get('weighted_win_rate', sc['c3_win_rate']):.0f}%</div>
    <div class="label">Weighted Win Rate</div>
  </div>
  <div class="card">
    <div class="big">{sc['c3_wins']} / {sc['c3_wins'] + sc['baseline_wins']}</div>
    <div class="label">C3 Wins / Total</div>
  </div>
  <div class="card">
    <div class="big">{sc['avg_score_c3']:.2f}</div>
    <div class="label">Avg C3 Score</div>
    {c3_delta_badge}
  </div>
  <div class="card">
    <div class="big {'positive' if sc['avg_score_delta'] >= 0 else 'negative'}">{sc['avg_score_delta']:+.3f}</div>
    <div class="label">Score Delta</div>
    {delta_delta_badge}
  </div>
  <div class="card">
    <div class="big {'positive' if eff_time_saved >= 0 else 'negative'}">{eff_time_saved:+.0f}s</div>
    <div class="label">Time Saved</div>
  </div>
  <div class="card">
    <div class="big {'positive' if eff_cost_saved >= 0 else 'negative'}">${eff_cost_saved:+.3f}</div>
    <div class="label">Cost Saved</div>
  </div>
  <div class="card">
    <div class="big">{eff.get('projected_monthly_cost_saved_usd', 0):+.1f}</div>
    <div class="label">$/mo Projected</div>
  </div>
  <div class="card">
    <div class="big" style="color: var(--accent)">{(report.get('tool_adoption') or {}).get('adoption_rate', 0):.0f}%</div>
    <div class="label">MCP Adoption</div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h3>Provider: C3 vs Baseline Scores</h3>
    <canvas id="providerChart"></canvas>
  </div>
  <div class="card">
    <h3>Score Dimensions (C3 vs Baseline)</h3>
    <canvas id="dimChart"></canvas>
  </div>
  <div class="card">
    <h3>Category: C3 vs Baseline</h3>
    <canvas id="catChart"></canvas>
  </div>
  <div class="card">
    <h3>Win Rate by Provider</h3>
    <canvas id="winRateChart"></canvas>
  </div>
</div>

{trend_section_html}

<h2 class="section-title">Insights &amp; Recommendations</h2>
{insights_html}

<h2 class="section-title">Provider Details</h2>
<div class="provider-cards">{provider_cards}</div>

<h2 class="section-title">Tool Usage Analysis</h2>
<div class="tool-summary">
  <div class="stat"><div class="num" style="color:var(--c3)">{ta_summary.get('avg_c3_calls_per_task', 0)}</div><div class="lbl">Avg C3 Tools/Task</div></div>
  <div class="stat"><div class="num" style="color:var(--base)">{ta_summary.get('avg_baseline_calls_per_task', 0)}</div><div class="lbl">Avg Base Tools/Task</div></div>
  <div class="stat"><div class="num" style="color:var(--accent)">{ta_summary.get('c3_mcp_calls', 0)}</div><div class="lbl">C3 MCP Calls</div></div>
  <div class="stat"><div class="num">{ta_summary.get('mcp_ratio', 0):.0f}%</div><div class="lbl">MCP Ratio</div></div>
  <div class="stat"><div class="num">{ta_summary.get('c3_tool_diversity', 0)}</div><div class="lbl">C3 Diversity</div></div>
  <div class="stat"><div class="num">{ta_summary.get('baseline_tool_diversity', 0)}</div><div class="lbl">Base Diversity</div></div>
</div>
<div class="grid">
  <div class="card">
    <h3>Tool Calls by Category</h3>
    <canvas id="toolCatChart"></canvas>
  </div>
  <div class="card">
    <h3>Top Tools: C3 vs Baseline</h3>
    <canvas id="toolCompChart"></canvas>
  </div>
</div>
<div class="card tool-table" style="overflow-x: auto;">
  <h3>Tool Comparison Detail</h3>
  <table>
    <thead><tr><th>Tool</th><th>C3 Calls</th><th>Base Calls</th><th>Delta</th></tr></thead>
    <tbody>{tool_rows}</tbody>
  </table>
</div>

<h2 class="section-title">All Results</h2>
<div class="card" style="overflow-x: auto;">
  <table>
    <thead><tr>
      <th>Provider</th><th>Task</th><th>Category</th><th>Diff</th>
      <th>C3</th><th>Base</th><th>Delta</th><th>Winner</th>
      <th>Time Saved</th><th>Cost Saved</th><th>Tools (C3/Base)</th>
    </tr></thead>
    <tbody>{result_rows}</tbody>
  </table>
</div>

<h2 class="section-title">Response Comparison</h2>
{comparison_html}

<script>
const C3='#00b894', BASE='#e17055', ACCENT='#6c5ce7';
const chartOpts = {{ responsive:true, plugins:{{ legend:{{ labels:{{ color:'#888' }} }} }} }};

new Chart(document.getElementById('providerChart'), {{
  type:'bar', data:{{ labels:{json.dumps(provider_names)},
    datasets:[{{ label:'C3', data:{json.dumps(c3_scores)}, backgroundColor:C3 }},
              {{ label:'Baseline', data:{json.dumps(base_scores)}, backgroundColor:BASE }}]
  }}, options:{{ ...chartOpts, scales:{{ y:{{ beginAtZero:true, max:1, grid:{{ color:'#1e1e2e' }} }} }} }}
}});

new Chart(document.getElementById('dimChart'), {{
  type:'radar', data:{{ labels:{json.dumps(dim_labels)},
    datasets:[{{ label:'C3', data:{json.dumps(dim_c3)}, borderColor:C3, backgroundColor:C3+'33' }},
              {{ label:'Baseline', data:{json.dumps(dim_base)}, borderColor:BASE, backgroundColor:BASE+'33' }}]
  }}, options:{{ ...chartOpts, scales:{{ r:{{ beginAtZero:true, max:1, grid:{{ color:'#1e1e2e' }}, pointLabels:{{ color:'#888' }} }} }} }}
}});

new Chart(document.getElementById('catChart'), {{
  type:'bar', data:{{ labels:{json.dumps([c.replace('_',' ').title() for c in cat_names])},
    datasets:[{{ label:'C3', data:{json.dumps(cat_c3)}, backgroundColor:C3 }},
              {{ label:'Baseline', data:{json.dumps(cat_base)}, backgroundColor:BASE }}]
  }}, options:{{ ...chartOpts, scales:{{ y:{{ beginAtZero:true, max:1, grid:{{ color:'#1e1e2e' }} }} }} }}
}});

new Chart(document.getElementById('winRateChart'), {{
  type:'bar', data:{{ labels:{json.dumps(provider_names)},
    datasets:[{{ label:'Win Rate %', data:{json.dumps(win_rates)}, backgroundColor:ACCENT }}]
  }}, options:{{ ...chartOpts, indexAxis:'y', scales:{{ x:{{ beginAtZero:true, max:100, grid:{{ color:'#1e1e2e' }} }} }}, plugins:{{ legend:{{ display:false }} }} }}
}});

// Tool Usage Charts
new Chart(document.getElementById('toolCatChart'), {{
  type:'bar', data:{{ labels:{json.dumps([c.replace('_',' ').title() for c in ta_cat_names])},
    datasets:[
      {{ label:'C3 MCP', data:{json.dumps(ta_cat_c3_mcp)}, backgroundColor:'#6c5ce7' }},
      {{ label:'C3 Native', data:{json.dumps(ta_cat_c3_native)}, backgroundColor:C3 }},
      {{ label:'Baseline Native', data:{json.dumps(ta_cat_base_native)}, backgroundColor:BASE }}
    ]
  }}, options:{{ ...chartOpts, scales:{{ x:{{ stacked:true }}, y:{{ stacked:true, grid:{{ color:'#1e1e2e' }} }} }} }}
}});

const topTools = {json.dumps([t['tool'] for t in ta_comparison[:10]])};
const topC3 = {json.dumps([t['c3_calls'] for t in ta_comparison[:10]])};
const topBase = {json.dumps([t['baseline_calls'] for t in ta_comparison[:10]])};
new Chart(document.getElementById('toolCompChart'), {{
  type:'bar', data:{{ labels:topTools,
    datasets:[{{ label:'C3', data:topC3, backgroundColor:C3 }},
              {{ label:'Baseline', data:topBase, backgroundColor:BASE }}]
  }}, options:{{ ...chartOpts, indexAxis:'y', scales:{{ x:{{ grid:{{ color:'#1e1e2e' }} }} }} }}
}});

// Trend sparkline charts
{trend_charts_js}
</script>
</body>
</html>"""


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for pre blocks."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
