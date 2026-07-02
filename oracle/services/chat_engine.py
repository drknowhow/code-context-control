"""Chat engine for Oracle — tool-calling loop with streaming."""

import concurrent.futures
import json
import queue
import re
import threading
import time
import urllib.error
import uuid
from pathlib import Path

# Thread-local used to hand the active agent-event sink + parent tool_id
# into worker threads running _execute_tool, so nested sub-agent loops
# (_tool_delegate_task) can emit lifecycle events back to the main chat()
# generator without threading them through every tool signature.
_agent_tls = threading.local()

from oracle.config import load_config
from oracle.services.c3_bridge import validate_project_path
from oracle.services.chat_store import ChatStore
from oracle.services.cross_memory import CrossMemory
from oracle.services.health_checker import HealthChecker
from oracle.services.insight_engine import InsightEngine
from oracle.services.memory_reader import MemoryReader
from oracle.services.memory_writer import MemoryWriter
from oracle.services.ollama_bridge import OllamaBridge
from oracle.services.project_scanner import ProjectScanner

# ── Tool definitions (embedded in system prompt) ──────────

_TOOL_DEFS = """
Available tools — call ONE at a time by outputting exactly:
<tool_call>{"name": "tool_name", "args": {…}}</tool_call>

After you see the result, continue your response using the data.
Do NOT call a tool if you can answer from context or prior results.

Tools:
1. list_projects()
   Returns all registered C3 projects with fact counts and paths.

2. query_memory(project_path, query?, category?, limit=10)
   Search or list memory facts from a specific project.
   - project_path (required): full path to the project
   - query (optional): keyword filter on fact text
   - category (optional): filter by category
   - limit: max results (default 10)

3. search_facts(query, limit=20)
   Search facts across ALL projects. Returns matches with project source.

4. project_health(project_path)
   Run a health check on a project's memory. Returns status, issues, stats.

5. analyze_project(project_path)
   Deep LLM-powered analysis of a project's memory patterns and themes.

6. cross_insights(project_path?)
   Get cross-project insights. If project_path given, filter to that project.

7. suggest_action(project_path, action, fact_ids, reason)
   Create a pending suggestion (merge_facts, archive_facts, or add_fact).
   - action: "merge_facts" | "archive_facts" | "add_fact"
   - fact_ids: list of fact IDs involved
   - reason: explanation string

8. read_graph(project_path)
   Get memory graph statistics for a project (nodes, edges, types).

--- C3 Code Intelligence Tools (require project_path) ---

9. c3_search(project_path, query, action="code", top_k=3, max_tokens=1200)
   Code intelligence search within a project.
   action: code|exact|files|semantic|transcript

10. c3_read(project_path, file_path, symbols=null, lines=null)
    Read file contents with optional symbol or line-range extraction.

11. c3_edits(project_path, action="history", file="", limit=50, since="", tag="")
    Query the edit ledger: history, versions, stats.

12. c3_edits_cross(action="history", tag="", limit=20, scope="")
    Query edit ledgers across ALL projects. No project_path needed.
    scope: ""=all, "top"=top-level only, or a project name/path = it + its sub-projects.

13. c3_memory_query(project_path, action="query", query="", category="", top_k=10)
    Query project memory (read-only: recall, query, list, score, graph, trends).

14. c3_compress(project_path, file_path, mode="map")
    Token-efficient file summary. mode: map|dense_map|smart|diff|bug_scan

15. c3_validate(project_path, file_path)
    Syntax validation on a file.

16. c3_status(project_path, view="health", detailed=false)
    Project health/budget/sessions overview. view: budget|health|sessions

17. c3_search_cross(query, action="code", top_k=3, scope="")
    Search code across ALL projects. No project_path needed.
    scope: ""=all, "top"=top-level only, or a project name/path = it + its sub-projects.

18. delegate_task(agent_id, task)
    Delegate a specific sub-task to a specialized agent.
    - agent_id: The ID of the active agent to use.
    - task: A detailed prompt explaining what the agent needs to do.

19. activity_report(date?, since?, until?, project_path?, narrate=false)
    Cross-project daily activity digest: sessions, tool calls, edits, git
    mutations, and token/cost across ALL projects (or one, via project_path).
    Defaults to today (UTC). narrate=true adds a prose summary.

20. c3_project(action="list", project="", ...)
    Cross-project ops by registered project NAME or path. Read-only actions:
    list|info|subprojects|search|read|compress|status|memory|impact|edits|validate.
    Extra args mirror the underlying tool (query, file_path, mode, view,
    mem_action, edits_action, top_k, limit, target).

21. c3_artifacts(project_path, action="list", artifact="", version=0, against=0)
    Agent-config artifact tracking (read-only): list|history|show|diff|status.
    Version history for CLAUDE.md/settings/MCP configs/skills in a project.
"""

_SYSTEM_BASE = """You are Oracle, an AI assistant specializing in cross-project code intelligence and memory analysis.
You have access to memory facts, project health data, cross-project insights,
AND full C3 code intelligence (code search, file reading, edit history, validation)
for all registered C3 projects. You help developers understand patterns across
their projects, investigate code, trace edit history, and maintain healthy memory.

When the user asks about projects, code, memory, patterns, or needs analysis — use your
tools to retrieve real data before answering. Always ground responses in actual
project data.

For code-level investigation, use c3_search/c3_read/c3_compress to explore files.
For edit history, use c3_edits or c3_edits_cross to trace changes across projects.
Use list_projects first to discover project paths before calling project-specific tools.
"""

_DEPTH_INSTRUCTIONS = {
    "brief": "\nBe very concise. Use bullet points. Max 3 sentences per answer. Only use a tool if you truly cannot answer without it — prefer answering from context.\n",
    "normal": "\nBe concise and specific. Use a tool only when the user asks about specific data you don't have in context. Limit yourself to one tool call when possible.\n",
    "deep": "\nProvide thorough, detailed analysis with examples, data, and recommendations. Use multiple tool calls to gather comprehensive data when needed.\n",
}

_SYSTEM_RULES = """
Important rules:
- You can call multiple tools at once by outputting multiple `<tool_call>...</tool_call>` blocks.
- Call tools in parallel when tasks are independent.
- If the user's question can be answered from conversation context, do NOT call a tool.
- After receiving tool results, synthesize them into a clear, helpful answer.
- Format your answers with markdown for readability.
"""

# Native tool-calling mode: tools are declared via Ollama's tools= API, so the
# prompt carries only behavioral guidance — no <tool_call> syntax, no catalog.
_SYSTEM_RULES_NATIVE = """
Important rules:
- Use the provided tools when the user asks about specific data you don't have in context; call independent tools in parallel in one turn.
- Do NOT call a tool if the question can be answered from conversation context or prior results.
- After receiving tool results, synthesize them into a clear, helpful answer.
- Use list_projects first to discover project paths before calling project-specific tools.
- Format your answers with markdown for readability.
"""

# ── Slash command registry ────────────────────────────────

COMMANDS = {
    "project": {"args": "<name...> | clear", "desc": "Focus on specific projects"},
    "model":   {"args": "<model-name>",      "desc": "Switch LLM model for this conversation"},
    "depth":   {"args": "brief | normal | deep", "desc": "Set response detail level"},
    "health":  {"args": "[project-name]",    "desc": "Quick health check (no LLM)"},
    "clear":   {"args": "",                  "desc": "Clear conversation history"},
    "help":    {"args": "",                  "desc": "Show available commands"},
    "tools":   {"args": "",                  "desc": "List available Oracle tools"},
    "team":    {"args": "",                  "desc": "Show active agents and their specializations"},
}

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_MAX_TOOL_ROUNDS = 8
# Rounds = LLM calls. One tool use needs 2 rounds (call + response synthesis).
_DEPTH_MAX_ROUNDS = {"brief": 2, "normal": 4, "deep": 8}
_MAX_HISTORY_MESSAGES = 40
_MAX_TOOL_RESULT_CHARS = 3000
_VISIBLE_RETRY_PROMPT = (
    "Your previous response contained only hidden reasoning and no user-visible "
    "assistant content. Now provide the visible response. If you need a tool, "
    "output exactly one <tool_call>{...}</tool_call> block in assistant content; "
    "otherwise answer the user directly."
)
_VISIBLE_RETRY_PROMPT_NATIVE = (
    "Your previous response contained only hidden reasoning and no user-visible "
    "assistant content. Now provide the visible response. If you need a tool, "
    "use a native tool call; otherwise answer the user directly."
)

_TC_OPEN = "<tool_call>"
_TC_CLOSE = "</tool_call>"
# If the pre-strip visible answer in round 0 is at least this many chars, we
# treat any trailing <tool_call> as speculative and do NOT regenerate. Prevents
# the "correct answer then wrong answer on next round" failure mode.
_TRUST_ANSWER_MIN_CHARS = 120


_NATIVE_TOOLS_CACHE: list[dict] | None = None


def _native_tool_defs() -> list[dict]:
    """Ollama native ``tools`` array built from TOOL_SPECS (single source of
    truth). Internal chat exposes ALL specs regardless of ``api_max_tier`` —
    that cap applies to external Discovery callers only."""
    global _NATIVE_TOOLS_CACHE
    if _NATIVE_TOOLS_CACHE is None:
        from oracle.services.tool_registry import TOOL_SPECS
        _NATIVE_TOOLS_CACHE = [
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["parameters"],
                },
            }
            for s in TOOL_SPECS
        ]
    return _NATIVE_TOOLS_CACHE


def _classify_no_tools(exc: Exception) -> tuple[bool, bool]:
    """Classify a streaming error raised while native tools were attached.

    Returns ``(fallback_to_legacy, negative_cache_model)``. Any HTTP 400
    triggers fallback (retrying without tools is one cheap request); the
    model's capability is negative-cached only when the server names tools as
    the problem, so an unrelated 400 doesn't permanently demote the model.
    """
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 400:
        return False, False
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return True, "tool" in body.lower()


class _ToolCallStripper:
    """Streaming-friendly stripper for <tool_call>...</tool_call> blocks.

    Buffers chunks so partial open/close tags that straddle chunk boundaries
    are never leaked to the UI. feed() returns only visible (stripped) text;
    flush() emits any trailing buffer that is not inside a tool_call.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_call = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._in_call:
                end = self._buf.find(_TC_CLOSE)
                if end == -1:
                    return "".join(out)
                self._buf = self._buf[end + len(_TC_CLOSE):]
                self._in_call = False
                continue
            start = self._buf.find(_TC_OPEN)
            if start == -1:
                hold = 0
                for i in range(1, len(_TC_OPEN)):
                    if self._buf.endswith(_TC_OPEN[:i]):
                        hold = i
                if hold:
                    out.append(self._buf[:-hold])
                    self._buf = self._buf[-hold:]
                else:
                    out.append(self._buf)
                    self._buf = ""
                return "".join(out)
            out.append(self._buf[:start])
            self._buf = self._buf[start + len(_TC_OPEN):]
            self._in_call = True

    def flush(self) -> str:
        if self._in_call:
            return ""
        tail = self._buf
        self._buf = ""
        return tail


def _build_system_prompt(state: dict, native: bool = False) -> str:
    """Build system prompt dynamically based on conversation state.

    ``native=True`` omits the <tool_call> catalog/syntax (tools are declared
    via Ollama's native API instead), keeping only behavioral guidance.
    """
    parts = [_SYSTEM_BASE]

    # Active Sub-Agents (Supervisor Role)
    cfg = load_config()
    active_agents = [a for a in cfg.get("agents", []) if a.get("active")]
    if active_agents:
        parts.append("\n\nYou are the Oracle Supervisor. You lead a team of specialized agents. You can delegate specific sub-tasks to them using the `delegate_task` tool. If a task requires deep specialization, delegate it.\n**Active Agents:**\n")
        for agent in active_agents:
            parts.append(f"- `{agent.get('id')}`: {agent.get('description', '')}\n")

    # Depth
    depth = state.get("depth", "normal")
    parts.append(_DEPTH_INSTRUCTIONS.get(depth, _DEPTH_INSTRUCTIONS["normal"]))

    # Project focus
    focused = state.get("focused_projects", [])
    if focused:
        names = ", ".join(f'"{p["name"]}" ({p["path"]})' for p in focused)
        parts.append(
            f"\nYou are currently focused on these projects: {names}.\n"
            "Prefer querying these projects first. When the user says 'this project' "
            "or 'my project', they mean one of the focused projects.\n"
        )

    if native:
        parts.append(_SYSTEM_RULES_NATIVE)
    else:
        parts.append(_TOOL_DEFS)
        parts.append(_SYSTEM_RULES)
    return "".join(parts)


class ChatEngine:
    """Orchestrates chat with tool-calling loop and streaming."""

    def __init__(
        self,
        bridge: OllamaBridge,
        reader: MemoryReader,
        writer: MemoryWriter,
        cross_memory: CrossMemory,
        health_checker: HealthChecker,
        insight_engine: InsightEngine,
        scanner: ProjectScanner,
        store: ChatStore,
        c3_bridge=None,
        activity_reporter=None,
    ):
        self.bridge = bridge
        self.reader = reader
        self.writer = writer
        self.cross_memory = cross_memory
        self.health_checker = health_checker
        self.insight_engine = insight_engine
        self.scanner = scanner
        self.store = store
        self.c3_bridge = c3_bridge
        self.activity_reporter = activity_reporter

    # ── Shared streaming drain ────────────────────────────

    def _drain_stream(self, messages: list[dict], model: str,
                      stripper: "_ToolCallStripper | None" = None,
                      tools: list[dict] | None = None, think: bool | None = True):
        """Stream one LLM round, yielding SSE event dicts as they arrive.

        The single streamer behind the main chat loop, the visible-response
        retry, and the delegate sub-agent loop. Returns (via StopIteration
        value — use ``yield from``) a summary dict: ``text`` (raw, including
        any <tool_call> markup in legacy mode), ``thinking``, ``tool_calls``
        (native protocol), ``stats``, ``chunks``, ``visible_chars``.

        In legacy mode pass a ``_ToolCallStripper`` so <tool_call> blocks are
        withheld from the emitted text events; native mode emits text as-is
        (the content channel is clean) and collects structured tool calls
        without emitting SSE for them — the executor emits the canonical
        ``tool_call`` event with its tool_id.
        """
        full_text = ""
        thinking_text = ""
        tool_calls: list[dict] = []
        stats: dict = {}
        chunks = 0
        visible_chars = 0
        for item in self.bridge.stream_chat(messages, model=model, think=think,
                                            tools=tools):
            if isinstance(item, tuple):
                kind, chunk = item
            else:
                kind, chunk = "text", item
            chunks += 1
            if kind == "thinking":
                thinking_text += chunk
                yield {"type": "thinking", "content": chunk}
            elif kind == "stats":
                stats = chunk
            elif kind == "tool_call":
                tool_calls.append(chunk)
            else:
                full_text += chunk
                visible = stripper.feed(chunk) if stripper is not None else chunk
                if visible:
                    visible_chars += len(visible)
                    yield {"type": "text", "content": visible}
        if stripper is not None:
            tail = stripper.flush()
            if tail:
                visible_chars += len(tail)
                yield {"type": "text", "content": tail}
        return {"text": full_text, "thinking": thinking_text,
                "tool_calls": tool_calls, "stats": stats, "chunks": chunks,
                "visible_chars": visible_chars}

    # ── Main chat generator ───────────────────────────────

    def chat(self, conv_id: str | None, user_message: str):
        """
        Generator yielding SSE event dicts:
          {"type": "meta", ...}
          {"type": "status", "message": ..., "detail": ...}
          {"type": "text", "content": "..."}
          {"type": "tool_call", "name": ..., "args": ..., "tool_id": ...}
          {"type": "tool_result", "tool_id": ..., "name": ..., "result": ..., "duration_ms": ...}
          {"type": "done", "conv_id": ..., "stats": ...}
          {"type": "error", "message": ...}
        """
        turn_start = time.time()
        total_tokens = 0
        thinking_chars = 0
        response_chars = 0
        tool_calls_count = 0
        ollama_stats = {}

        # Ensure conversation exists
        if not conv_id:
            conv_id = self.store.create_conversation()

        # Load conversation state (project focus, model, depth)
        state = self.store.get_state(conv_id)
        use_model = state.get("model") or self.bridge.model
        focused = state.get("focused_projects", [])
        focus_label = ", ".join(p["name"] for p in focused) if focused else "all projects"

        yield {
            "type": "meta", "conv_id": conv_id, "model": use_model,
            "state": state,
        }
        yield {"type": "status", "message": "Preparing context", "detail": f"Focus: {focus_label}"}

        # Save user message
        self.store.append_message(conv_id, {"role": "user", "content": user_message})

        # Native tool-calling mode: capability probe returns True/False, or
        # None (unknown) → attempt native and fall back on live rejection.
        probe = self.bridge.supports_tools(use_model) if self.bridge else False
        use_native = probe is not False
        tools = _native_tool_defs() if use_native else None

        # Build messages for LLM
        history = self.store.get_conversation(conv_id)
        llm_messages = self._build_llm_messages(history, state, native=use_native)
        context_msgs = len(llm_messages) - 1  # exclude system prompt
        yield {"type": "status", "message": "Context ready", "detail": f"{context_msgs} messages in context"}

        # Tool-calling loop — depth controls max rounds
        depth = state.get("depth", "normal")
        max_rounds = _DEPTH_MAX_ROUNDS.get(depth, 2)
        round_messages = []  # messages generated in this turn
        try:
            for _round in range(max_rounds):
                round_label = f"Round {_round + 1}" if _round > 0 else ""
                yield {"type": "status", "message": f"Streaming from {use_model}", "detail": round_label or "Generating response"}

                stream_start = time.time()
                try:
                    result = yield from self._drain_stream(
                        llm_messages, use_model,
                        stripper=None if use_native else _ToolCallStripper(),
                        tools=tools,
                    )
                except Exception as e:
                    fallback_ok, cache_neg = (
                        _classify_no_tools(e) if (use_native and tools) else (False, False)
                    )
                    if not fallback_ok:
                        yield {"type": "error", "message": f"LLM error: {e}"}
                        break
                    # Mid-turn fallback: the model rejected native tools —
                    # demote to the legacy text protocol and rerun this round.
                    if cache_neg and self.bridge:
                        self.bridge.set_tools_support(use_model, False)
                    use_native = False
                    tools = None
                    llm_messages[0] = {
                        "role": "system",
                        "content": _build_system_prompt(state, native=False),
                    }
                    yield {"type": "status", "message": "Falling back to text tool protocol",
                           "detail": f"{use_model} rejected native tool calling"}
                    try:
                        result = yield from self._drain_stream(
                            llm_messages, use_model, stripper=_ToolCallStripper(),
                        )
                    except Exception as e2:
                        yield {"type": "error", "message": f"LLM error: {e2}"}
                        break

                full_text = result["text"]
                thinking_text = result["thinking"]
                native_calls = result["tool_calls"]
                thinking_chars += len(thinking_text)
                response_chars += result["visible_chars"]
                chunk_count = result["chunks"]
                if result["stats"]:
                    ollama_stats = result["stats"]

                if not full_text.strip() and not native_calls and thinking_text.strip():
                    yield {
                        "type": "status",
                        "message": "Retrying visible response",
                        "detail": "Model returned thinking without assistant content",
                    }
                    retry_prompt = (
                        _VISIBLE_RETRY_PROMPT_NATIVE if use_native else _VISIBLE_RETRY_PROMPT
                    )
                    retry_messages = llm_messages + [{"role": "user", "content": retry_prompt}]
                    try:
                        retry = yield from self._drain_stream(
                            retry_messages, use_model, tools=tools, think=False,
                        )
                        full_text += retry["text"]
                        thinking_chars += len(retry["thinking"])
                        response_chars += retry["visible_chars"]
                        native_calls.extend(retry["tool_calls"])
                        chunk_count += retry["chunks"]
                        if retry["stats"]:
                            ollama_stats = retry["stats"]
                    except Exception as e:
                        yield {"type": "error", "message": f"Visible response retry failed: {e}"}

                if not full_text.strip() and not native_calls and thinking_text.strip():
                    fallback = (
                        f"{use_model} returned hidden reasoning but no visible response. "
                        "I retried with thinking disabled and still did not receive assistant content. "
                        "Try again or switch to another model with /model."
                    )
                    full_text = fallback
                    response_chars += len(fallback)
                    yield {"type": "text", "content": fallback}

                stream_ms = int((time.time() - stream_start) * 1000)
                total_tokens += chunk_count
                yield {"type": "status", "message": "Response received", "detail": f"{chunk_count} chunks in {stream_ms}ms"}

                # Determine tool calls for this round.
                if use_native:
                    # Structured calls from the native protocol; content is clean.
                    valid_calls = [
                        {"name": c.get("name", "unknown"), "args": c.get("arguments") or {}}
                        for c in native_calls if c.get("name")
                    ]
                    visible_text = full_text.strip()
                    final_text = visible_text
                else:
                    tool_matches = list(_TOOL_CALL_RE.finditer(full_text))
                    visible_text = _TOOL_CALL_RE.sub("", full_text).strip()
                    final_text = visible_text or full_text
                    valid_calls = []
                    for match in tool_matches:
                        try:
                            valid_calls.append(json.loads(match.group(1)))
                        except json.JSONDecodeError:
                            continue

                if not valid_calls:
                    # No tool call — final answer
                    round_messages.append({"role": "assistant", "content": final_text})
                    break

                # Legacy-only heuristic: if the model already produced a
                # substantive answer alongside speculative <tool_call> blocks,
                # trust the answer and stop — regenerating with tool results
                # usually corrupts it. Native mode needs no such guard: models
                # emitting structured calls expect their results.
                if not use_native and len(visible_text) >= _TRUST_ANSWER_MIN_CHARS:
                    round_messages.append({"role": "assistant", "content": visible_text})
                    yield {
                        "type": "status",
                        "message": "Answer finalized",
                        "detail": "Speculative tool calls skipped — answer already provided",
                    }
                    break

                # Record the assistant response before tool results (stripped so
                # the next LLM round sees clean context without <tool_call> noise).
                round_messages.append({"role": "assistant", "content": final_text})
                if use_native:
                    # Echo the structured calls back so the model can pair the
                    # role:tool results that follow.
                    llm_messages.append({
                        "role": "assistant",
                        "content": full_text,
                        "tool_calls": [
                            {"function": {"name": c["name"], "arguments": c.get("args") or {}}}
                            for c in valid_calls
                        ],
                    })
                else:
                    llm_messages.append({"role": "assistant", "content": final_text})

                # Execute all tools in parallel
                tool_calls_count += len(valid_calls)

                # Per-turn sink for sub-agent lifecycle events emitted from
                # worker threads (e.g. _tool_delegate_task). Drained in the
                # future-polling loop below and yielded to the HTTP stream.
                agent_event_sink: queue.Queue = queue.Queue()

                def _run_tool(tool_name, tool_args, tool_id):
                    _agent_tls.agent_sink = agent_event_sink
                    _agent_tls.parent_tool_id = tool_id
                    t0 = time.perf_counter_ns()
                    try:
                        result = self._execute_tool(tool_name, tool_args)
                    finally:
                        _agent_tls.agent_sink = None
                        _agent_tls.parent_tool_id = None
                    dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
                    return result, dur_ms

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(valid_calls), 5)) as executor:
                    # Prepare tasks
                    future_to_call = {}
                    for call in valid_calls:
                        tool_name = call.get("name", "unknown")
                        tool_args = call.get("args", {})
                        tool_id = uuid.uuid4().hex[:8]

                        yield {"type": "tool_call", "name": tool_name, "args": tool_args, "tool_id": tool_id}

                        future = executor.submit(_run_tool, tool_name, tool_args, tool_id)
                        future_to_call[future] = (tool_name, tool_args, tool_id, call)

                    # Poll futures with short timeout so we can drain the
                    # agent event sink between poll ticks. This interleaves
                    # sub-agent events with tool_result events in real time.
                    pending = set(future_to_call.keys())
                    while pending:
                        while True:
                            try:
                                ev = agent_event_sink.get_nowait()
                            except queue.Empty:
                                break
                            yield ev
                        done_now, pending = concurrent.futures.wait(
                            pending,
                            timeout=0.05,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        for future in done_now:
                            tool_name, tool_args, tool_id, call = future_to_call[future]
                            try:
                                result, dur_ms = future.result()
                            except Exception as e:
                                result, dur_ms = {"error": str(e)}, 0

                            # Drain any remaining sub-agent events for this
                            # tool before emitting its tool_result so the UI
                            # sees agent_done before tool completion.
                            while True:
                                try:
                                    ev = agent_event_sink.get_nowait()
                                except queue.Empty:
                                    break
                                yield ev

                            result_str = json.dumps(result, default=str)
                            truncated = result_str[:_MAX_TOOL_RESULT_CHARS]
                            if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                                truncated += "... (truncated)"

                            yield {
                                "type": "tool_result", "tool_id": tool_id, "name": tool_name,
                                "result": result, "duration_ms": dur_ms,
                            }

                            # Record in round messages
                            round_messages.append({
                                "role": "tool_call", "content": json.dumps(call),
                                "tool_name": tool_name, "tool_id": tool_id,
                            })
                            round_messages.append({
                                "role": "tool_result", "content": truncated,
                                "tool_name": tool_name, "tool_id": tool_id,
                            })

                            # Extend next LLM context: native protocol feeds a
                            # role:tool message; legacy re-injects as user text.
                            if use_native:
                                llm_messages.append({
                                    "role": "tool", "tool_name": tool_name,
                                    "content": truncated,
                                })
                            else:
                                llm_messages.append({
                                    "role": "user",
                                    "content": f"<tool_result name=\"{tool_name}\">\n{truncated}\n</tool_result>",
                                })

                    # Final drain after all tools complete
                    while True:
                        try:
                            ev = agent_event_sink.get_nowait()
                        except queue.Empty:
                            break
                        yield ev

                if not use_native:
                    # Legacy models tend to restart their answer after inlined
                    # tool results; the nudge counters that. Native mode must
                    # NOT inject a user message between tool_calls and their
                    # role:tool results — the protocol handles continuation.
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "The tool results above contain the data you requested. "
                            "Now write your final answer to the user based on those results. "
                            "Do NOT output any <tool_call> blocks. Do NOT restart or repeat "
                            "your previous message — the user has already seen it. "
                            "Write only the remaining, finalized answer."
                        ),
                    })
                yield {"type": "status", "message": "Finalizing answer", "detail": f"After {len(valid_calls)} parallel results"}
            else:
                # Exhausted rounds without a final answer — synthesize one
                fallback = (
                    f"I gathered data using {tool_calls_count} tool call(s) but reached "
                    f"the {max_rounds}-round limit before producing a final answer. "
                    f"Try increasing depth with /depth deep, or rephrase your question."
                )
                yield {"type": "text", "content": fallback}
                round_messages.append({"role": "assistant", "content": fallback})

        except Exception as e:
            yield {"type": "error", "message": str(e)}

        total_ms = int((time.time() - turn_start) * 1000)
        # Build token stats from Ollama if available
        token_stats = {}
        if ollama_stats:
            # Ollama durations are in nanoseconds
            eval_count = ollama_stats.get("eval_count", 0)
            eval_ns = ollama_stats.get("eval_duration", 0)
            prompt_count = ollama_stats.get("prompt_eval_count", 0)
            token_stats = {
                "eval_tokens": eval_count,
                "prompt_tokens": prompt_count,
                "tokens_per_sec": round(eval_count / (eval_ns / 1e9), 1) if eval_ns else 0,
            }

        rounds_used = min(_round + 1, max_rounds) if round_messages else 0

        # Attach per-turn metadata to the final assistant message so it
        # survives conversation reload. The UI renders a footer from this.
        assistant_metadata = {
            "model": use_model,
            "duration_ms": total_ms,
            "rounds": rounds_used,
            "tool_calls": tool_calls_count,
            "thinking_chars": thinking_chars,
            "response_chars": response_chars,
            **token_stats,
        }
        for msg in reversed(round_messages):
            if msg.get("role") == "assistant":
                msg["metadata"] = assistant_metadata
                break

        # Persist all round messages
        if round_messages:
            self.store.append_messages(conv_id, round_messages)

        yield {
            "type": "done", "conv_id": conv_id,
            "model": use_model,
            "stats": {
                "model": use_model,
                "total_ms": total_ms,
                "chunks": total_tokens,
                "thinking_chars": thinking_chars,
                "response_chars": response_chars,
                "tool_calls": tool_calls_count,
                "rounds": rounds_used,
                **token_stats,
            },
        }

    # ── LLM message building ─────────────────────────────

    def _build_llm_messages(self, history: list[dict], state: dict | None = None,
                            native: bool = False) -> list[dict]:
        """Convert stored history to Ollama chat messages with sliding window.

        Historical tool results are re-injected as ``<tool_result>`` user
        messages in BOTH modes: safe for all models, and avoids orphaned
        ``role:tool`` messages (no paired tool_calls) on conversation reload.
        """
        system_prompt = _build_system_prompt(state or {}, native=native)
        messages = [{"role": "system", "content": system_prompt}]

        # Take last N messages, skip tool_call/tool_result (they were inlined)
        recent = history[-_MAX_HISTORY_MESSAGES:]
        for msg in recent:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
            elif role == "tool_result":
                # Re-inject as user message so LLM has context
                name = msg.get("tool_name", "tool")
                messages.append({
                    "role": "user",
                    "content": f"<tool_result name=\"{name}\">\n{content}\n</tool_result>",
                })

        return messages

    # ── Slash commands ────────────────────────────────────

    @staticmethod
    def get_commands() -> dict:
        """Return command registry for the frontend autocomplete."""
        return COMMANDS

    def execute_command(self, conv_id: str | None, command_str: str) -> dict:
        """Parse and execute a slash command. Returns result dict."""
        command_str = command_str.strip()
        if command_str.startswith("/"):
            command_str = command_str[1:]

        parts = command_str.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd not in COMMANDS:
            return {"ok": False, "command": cmd, "message": f"Unknown command: /{cmd}"}

        # Ensure conversation exists
        if not conv_id:
            conv_id = self.store.create_conversation()

        match cmd:
            case "project":
                return self._cmd_project(conv_id, args)
            case "model":
                return self._cmd_model(conv_id, args)
            case "depth":
                return self._cmd_depth(conv_id, args)
            case "health":
                return self._cmd_health(conv_id, args)
            case "clear":
                return self._cmd_clear(conv_id)
            case "help":
                return self._cmd_help()
            case "tools":
                return self._cmd_tools()
            case "team":
                return self._cmd_team()
            case _:
                return {"ok": False, "command": cmd, "message": f"Unknown command: /{cmd}"}

    def _cmd_project(self, conv_id: str, args: str) -> dict:
        if args.lower() == "clear" or not args:
            self.store.update_state(conv_id, focused_projects=[])
            return {
                "ok": True, "command": "project",
                "message": "Project focus cleared. Now querying all projects.",
                "state": self.store.get_state(conv_id),
            }

        # Fuzzy-match project names
        projects = self.scanner.discover()
        names = [n.strip() for n in args.replace(",", " ").split()]
        matched = []
        not_found = []
        for name in names:
            name_lower = name.lower()
            found = None
            for p in projects:
                p_name = Path(p.get("path", "")).name.lower()
                if name_lower == p_name or name_lower in p_name:
                    found = {"name": Path(p["path"]).name, "path": p["path"]}
                    break
            if found:
                matched.append(found)
            else:
                not_found.append(name)

        if not matched:
            available = ", ".join(Path(p.get("path", "")).name for p in projects[:10])
            return {
                "ok": False, "command": "project",
                "message": f"No projects matched: {', '.join(not_found)}. Available: {available}",
            }

        self.store.update_state(conv_id, focused_projects=matched)
        msg = f"Focused on: {', '.join(m['name'] for m in matched)}"
        if not_found:
            msg += f"\nNot found: {', '.join(not_found)}"
        return {
            "ok": True, "command": "project", "message": msg,
            "state": self.store.get_state(conv_id),
        }

    def _cmd_model(self, conv_id: str, args: str) -> dict:
        if not args:
            state = self.store.get_state(conv_id)
            current = state.get("model") or self.bridge.model
            return {"ok": True, "command": "model", "message": f"Current model: {current}"}

        # Validate model
        if self.bridge.has_model(args):
            self.store.update_state(conv_id, model=args)
            return {
                "ok": True, "command": "model",
                "message": f"Model switched to: {args}",
                "state": self.store.get_state(conv_id),
            }
        else:
            models = self.bridge.list_models() or []
            available = ", ".join(models[:10]) if models else "(none found)"
            return {
                "ok": False, "command": "model",
                "message": f"Model '{args}' not found. Available: {available}",
            }

    def _cmd_depth(self, conv_id: str, args: str) -> dict:
        level = args.lower().strip()
        if level not in ("brief", "normal", "deep"):
            return {
                "ok": False, "command": "depth",
                "message": f"Invalid depth: '{args}'. Use: brief, normal, or deep",
            }
        self.store.update_state(conv_id, depth=level)
        labels = {"brief": "Concise bullet points", "normal": "Standard detail", "deep": "Thorough analysis"}
        return {
            "ok": True, "command": "depth",
            "message": f"Response depth set to: {level} ({labels[level]})",
            "state": self.store.get_state(conv_id),
        }

    def _cmd_health(self, conv_id: str, args: str) -> dict:
        state = self.store.get_state(conv_id)
        focused = state.get("focused_projects", [])

        if args:
            # Find matching project
            projects = self.scanner.discover()
            target = None
            for p in projects:
                if args.lower() in Path(p.get("path", "")).name.lower():
                    target = p.get("path")
                    break
            if not target:
                return {"ok": False, "command": "health", "message": f"Project not found: {args}"}
            results = [self.health_checker.check(target)]
        elif focused:
            results = [self.health_checker.check(p["path"]) for p in focused]
        else:
            projects = self.scanner.discover()
            results = [self.health_checker.check(p["path"]) for p in projects[:5]]

        return {"ok": True, "command": "health", "results": results}

    def _cmd_clear(self, conv_id: str) -> dict:
        # Delete messages but keep state
        state = self.store.get_state(conv_id)
        path = self.store._conv_path(conv_id)
        path.write_text("[]", "utf-8")
        self.store._touch_index(conv_id, message_count=0)
        return {
            "ok": True, "command": "clear",
            "message": "Conversation cleared. State preserved.",
            "state": state,
        }

    def _cmd_help(self) -> dict:
        lines = ["**Available commands:**\n"]
        for cmd, info in COMMANDS.items():
            arg_str = f" `{info['args']}`" if info["args"] else ""
            lines.append(f"- **/{cmd}**{arg_str} — {info['desc']}")
        return {"ok": True, "command": "help", "message": "\n".join(lines)}

    def _cmd_tools(self) -> dict:
        return {"ok": True, "command": "tools", "message": _TOOL_DEFS.strip()}

    def _cmd_team(self) -> dict:
        cfg = load_config()
        agents = cfg.get("agents", [])
        active = [a for a in agents if a.get("active")]
        if not active:
            return {"ok": True, "command": "team", "message": "No specialized agents are currently active. You can activate them in the **Team / Agents** tab."}

        lines = ["**Active Specialized Agents:**\n"]
        for a in active:
            lines.append(f"- **{a['name']}** (`{a['id']}`): {a.get('description', 'No description')}")

        return {"ok": True, "command": "team", "message": "\n".join(lines)}

    # ── Tool execution ────────────────────────────────────

    def run_tool(self, name: str, args: dict | None = None) -> dict:
        """Public entry point for dispatching a single tool call.

        Shared by the internal chat loop and the external Discovery API so both
        route through one dispatch path and can never diverge. ``args`` may be
        None/empty for no-argument tools.
        """
        return self._execute_tool(name, args or {})

    def _execute_tool(self, name: str, args: dict) -> dict:
        """Dispatch tool call to the appropriate service."""
        try:
            match name:
                case "list_projects":
                    return self._tool_list_projects()
                case "query_memory":
                    return self._tool_query_memory(**args)
                case "search_facts":
                    return self._tool_search_facts(**args)
                case "project_health":
                    return self._tool_project_health(**args)
                case "analyze_project":
                    return self._tool_analyze_project(**args)
                case "cross_insights":
                    return self._tool_cross_insights(**args)
                case "suggest_action":
                    return self._tool_suggest_action(**args)
                case "read_graph":
                    return self._tool_read_graph(**args)
                case "activity_report":
                    return self._tool_activity_report(**args)
                case "delegate_task":
                    return self._tool_delegate_task(**args)
                # ── C3 code intelligence tools ──
                case "c3_search" | "c3_read" | "c3_edits" | "c3_edits_cross" | \
                     "c3_memory_query" | "c3_compress" | "c3_validate" | \
                     "c3_status" | "c3_search_cross" | "c3_project" | "c3_artifacts":
                    return self._dispatch_c3(name, args)
                case _:
                    return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            return {"error": f"Tool '{name}' failed: {e}"}

    # ── C3 bridge dispatch ─────────────────────────────────

    def _dispatch_c3(self, name: str, args: dict) -> dict:
        """Dispatch a C3 tool call through the bridge."""
        if self.c3_bridge is None:
            return {"error": "C3 bridge not configured. C3 code intelligence is unavailable."}
        # Map tool names to bridge methods.
        _C3_METHODS = {
            "c3_search": self.c3_bridge.c3_search,
            "c3_read": self.c3_bridge.c3_read,
            "c3_edits": self.c3_bridge.c3_edits,
            "c3_edits_cross": self.c3_bridge.c3_edits_cross,
            "c3_memory_query": self.c3_bridge.c3_memory,
            "c3_compress": self.c3_bridge.c3_compress,
            "c3_validate": self.c3_bridge.c3_validate,
            "c3_status": self.c3_bridge.c3_status,
            "c3_search_cross": self.c3_bridge.c3_search_cross,
            "c3_project": self.c3_bridge.c3_project,
            "c3_artifacts": self.c3_bridge.c3_artifacts,
        }
        method = _C3_METHODS.get(name)
        if not method:
            return {"error": f"Unknown C3 tool: {name}"}
        try:
            return method(**args)
        except Exception as e:
            return {"error": f"C3 tool '{name}' failed: {e}"}

    # ── Tool implementations ──────────────────────────────

    def _tool_list_projects(self) -> dict:
        projects = self.scanner.discover()
        return {
            "count": len(projects),
            "projects": [
                {
                    "name": p.get("name", Path(p.get("path", "")).name),
                    "path": p.get("path", ""),
                    "facts_count": p.get("facts_count", 0),
                    "has_c3": p.get("has_c3", False),
                    "is_subproject": p.get("is_subproject", False),
                    "parent_path": p.get("parent_path", ""),
                    "subproject_count": p.get("subproject_count", 0),
                }
                for p in projects
            ],
        }

    def _tool_query_memory(
        self, project_path: str, query: str = "", category: str = "", limit: int = 10
    ) -> dict:
        project_path = validate_project_path(self.scanner, project_path)
        facts = self.reader.read_facts(project_path)
        if category:
            facts = [f for f in facts if f.get("category", "") == category]
        if query:
            query_lower = query.lower()
            terms = query_lower.split()
            facts = [
                f for f in facts
                if any(t in f.get("fact", "").lower() for t in terms)
            ]
        # Sort by relevance_count descending
        facts.sort(key=lambda f: int(f.get("relevance_count", 0)), reverse=True)
        top = facts[:limit]
        return {
            "project": project_path,
            "total_matching": len(facts),
            "returned": len(top),
            "facts": [
                {
                    "id": f.get("id", ""),
                    "category": f.get("category", "general"),
                    "fact": f.get("fact", "")[:300],
                    "lifecycle": f.get("lifecycle", "active"),
                    "relevance": f.get("relevance_count", 0),
                }
                for f in top
            ],
        }

    def _tool_search_facts(self, query: str, limit: int = 20) -> dict:
        """Search facts across all projects."""
        projects = self.scanner.discover()
        all_matches = []
        query_lower = query.lower()
        terms = query_lower.split()
        for p in projects:
            path = p.get("path", "")
            facts = self.reader.read_facts(path)
            for f in facts:
                text = f.get("fact", "").lower()
                score = sum(1 for t in terms if t in text)
                if score > 0:
                    all_matches.append({
                        "project": Path(path).name,
                        "project_path": path,
                        "id": f.get("id", ""),
                        "category": f.get("category", "general"),
                        "fact": f.get("fact", "")[:300],
                        "score": score,
                    })
        all_matches.sort(key=lambda m: m["score"], reverse=True)
        top = all_matches[:limit]
        return {"query": query, "total_matches": len(all_matches), "results": top}

    def _tool_project_health(self, project_path: str) -> dict:
        project_path = validate_project_path(self.scanner, project_path)
        return self.health_checker.check(project_path)

    def _tool_analyze_project(self, project_path: str) -> dict:
        project_path = validate_project_path(self.scanner, project_path)
        return self.insight_engine.analyze_project(project_path)

    def _tool_cross_insights(self, project_path: str = "") -> dict:
        if project_path:
            insights = self.cross_memory.get_for_project(project_path)
        else:
            insights = self.cross_memory.get_all_insights()
        stats = self.cross_memory.stats()
        return {
            "insights": [
                {
                    "id": i.get("id", ""),
                    "type": i.get("type", ""),
                    "text": i.get("text", "")[:400],
                    "source_projects": i.get("source_projects", []),
                    "confidence": i.get("confidence", 0),
                }
                for i in insights[:20]
            ],
            "stats": stats,
        }

    def _tool_suggest_action(
        self, project_path: str, action: str, fact_ids: list, reason: str
    ) -> dict:
        project_path = validate_project_path(self.scanner, project_path)
        data = {"fact_ids": fact_ids, "reason": reason}
        suggestion = self.writer.suggest(project_path, action, data)
        return {"suggestion_id": suggestion.get("id"), "status": "pending", "type": action}

    def _tool_read_graph(self, project_path: str) -> dict:
        project_path = validate_project_path(self.scanner, project_path)
        return self.reader.get_graph_stats(project_path)

    def _tool_activity_report(self, date: str = "", since: str = "", until: str = "",
                              project_path: str = "", narrate: bool = False) -> dict:
        if self.activity_reporter is None:
            return {"error": "Activity reporter not configured."}
        return self.activity_reporter.report(
            date=date, since=since, until=until,
            project_path=project_path, narrate=narrate,
        )

    def _tool_delegate_task(self, agent_id: str, task: str) -> dict:
        """Execute a sub-agent loop for the delegated task.

        Pushes lifecycle events onto the thread-local _agent_tls.agent_sink
        (set by the main chat() worker wrapper) so the UI can stream live
        sub-agent thinking, nested tool calls, and response tokens.
        """
        sink = getattr(_agent_tls, "agent_sink", None)
        parent_tool_id = getattr(_agent_tls, "parent_tool_id", None)

        def _emit(ev_type: str, **payload):
            if sink is not None and parent_tool_id is not None:
                sink.put({"type": ev_type, "tool_id": parent_tool_id, **payload})

        cfg = load_config()
        agent = next((a for a in cfg.get("agents", []) if a.get("id") == agent_id and a.get("active")), None)
        if not agent:
            _emit("agent_done", agent_id=agent_id, error="not_active")
            return {"error": f"Agent '{agent_id}' is not active or does not exist."}

        model = agent.get("model") or self.bridge.model
        # Sub-agents pick their tool protocol from their OWN model.
        probe = self.bridge.supports_tools(model) if self.bridge else False
        use_native = probe is not False
        tools = _native_tool_defs() if use_native else None

        def _sys_prompt(native: bool) -> str:
            rules = _SYSTEM_RULES_NATIVE if native else f"{_TOOL_DEFS}\n{_SYSTEM_RULES}"
            return f"{agent.get('system_prompt', '')}\n\n{rules}"

        llm_messages = [
            {"role": "system", "content": _sys_prompt(use_native)},
            {"role": "user", "content": task}
        ]

        rounds = 0
        max_rounds = 6
        agent_start_ns = time.perf_counter_ns()
        total_result_chars = 0
        full_text = ""

        _emit("agent_start", agent_id=agent_id, task=task, model=model)

        while rounds < max_rounds:
            rounds += 1
            _emit("agent_round", agent_id=agent_id, round=rounds)
            gen = self._drain_stream(llm_messages, model, tools=tools)
            result = None
            try:
                while True:
                    try:
                        ev = next(gen)
                    except StopIteration as stop:
                        result = stop.value
                        break
                    if ev.get("type") == "text":
                        _emit("agent_text", content=ev["content"])
                    elif ev.get("type") == "thinking":
                        _emit("agent_thinking", content=ev["content"])
                    # stats chunks are ignored for sub-agents
            except Exception as e:
                if use_native and tools:
                    fallback_ok, cache_neg = _classify_no_tools(e)
                    if fallback_ok:
                        # Model rejected native tools — demote to the legacy
                        # text protocol and rerun this round.
                        if cache_neg and self.bridge:
                            self.bridge.set_tools_support(model, False)
                        use_native = False
                        tools = None
                        llm_messages[0] = {"role": "system", "content": _sys_prompt(False)}
                        rounds -= 1
                        continue
                _emit("agent_done", agent_id=agent_id, rounds=rounds, error=str(e))
                return {"error": f"Agent '{agent_id}' encountered LLM error: {e}"}

            full_text = result["text"]
            native_calls = result["tool_calls"]

            if not full_text.strip() and not native_calls:
                _emit("agent_done", agent_id=agent_id, rounds=rounds, error="empty_response")
                return {"error": f"Agent '{agent_id}' returned empty response."}

            if use_native:
                if not native_calls:
                    total_result_chars = len(full_text)
                    dur_ms = (time.perf_counter_ns() - agent_start_ns) // 1_000_000
                    _emit("agent_done", agent_id=agent_id, rounds=rounds,
                          result_chars=total_result_chars, duration_ms=dur_ms)
                    return {"agent": agent_id, "result": full_text}
                # Sub-agents run one tool per round; take the first call.
                first = native_calls[0]
                call = {"name": first.get("name", "unknown"), "args": first.get("arguments") or {}}
            else:
                tool_match = _TOOL_CALL_RE.search(full_text)
                if not tool_match:
                    total_result_chars = len(full_text)
                    dur_ms = (time.perf_counter_ns() - agent_start_ns) // 1_000_000
                    _emit("agent_done", agent_id=agent_id, rounds=rounds,
                          result_chars=total_result_chars, duration_ms=dur_ms)
                    return {"agent": agent_id, "result": full_text}

                try:
                    call = json.loads(tool_match.group(1))
                except json.JSONDecodeError:
                    total_result_chars = len(full_text)
                    dur_ms = (time.perf_counter_ns() - agent_start_ns) // 1_000_000
                    _emit("agent_done", agent_id=agent_id, rounds=rounds,
                          result_chars=total_result_chars, duration_ms=dur_ms)
                    return {"agent": agent_id, "result": full_text}

            tool_name = call.get("name", "unknown")
            tool_args = call.get("args", {})
            sub_tool_id = uuid.uuid4().hex[:8]

            _emit("agent_tool_call", sub_tool_id=sub_tool_id, name=tool_name, args=tool_args)

            t0 = time.perf_counter_ns()
            if tool_name == "delegate_task":
                tool_result = {"error": "Sub-agents cannot delegate tasks."}
            else:
                tool_result = self._execute_tool(tool_name, tool_args)
            sub_dur_ms = (time.perf_counter_ns() - t0) // 1_000_000

            _emit("agent_tool_result", sub_tool_id=sub_tool_id, name=tool_name,
                  result=tool_result, duration_ms=sub_dur_ms)

            result_str = json.dumps(tool_result, default=str)
            truncated = result_str[:_MAX_TOOL_RESULT_CHARS]
            if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                truncated += "... (truncated)"

            if use_native:
                llm_messages.append({
                    "role": "assistant", "content": full_text,
                    "tool_calls": [{"function": {"name": tool_name, "arguments": tool_args}}],
                })
                llm_messages.append({"role": "tool", "tool_name": tool_name, "content": truncated})
            else:
                llm_messages.append({"role": "assistant", "content": full_text})
                llm_messages.append({
                    "role": "user",
                    "content": f"<tool_result name=\"{tool_name}\">\n{truncated}\n</tool_result>\nContinue your response using this data."
                })

        dur_ms = (time.perf_counter_ns() - agent_start_ns) // 1_000_000
        _emit("agent_done", agent_id=agent_id, rounds=rounds,
              result_chars=len(full_text), duration_ms=dur_ms,
              error="max_rounds_reached")
        return {"agent": agent_id, "error": "Agent reached max tool rounds.", "partial_result": full_text}

