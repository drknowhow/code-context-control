#!/usr/bin/env python3
"""
MCP Proxy - stdio proxy between Claude Code and the C3 MCP server.

Dynamically filters tools/list responses based on conversation context
and injects a sliding window context summary into tool responses.

Architecture:
    Claude Code <--stdio--> mcp_proxy.py <--subprocess stdio--> mcp_server.py

Usage:
    python cli/mcp_proxy.py --project <path>
"""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import load_proxy_config
from core.ide import get_profile
from services.ollama_client import OllamaClient
from services.proxy_state import ProxyState
from services.tool_classifier import ToolClassifier


class MCPProxy:
    """Bidirectional JSON-RPC proxy with optional tool filtering and context injection."""

    def __init__(self, project_path: str, host: str | None = None):
        self.project_path = project_path
        self.config = load_proxy_config(project_path)
        self.disabled = self.config.get("PROXY_DISABLE", False)
        self.filtering_enabled = bool(self.config.get("filter_tools", True)) and "all" not in self.config.get("always_visible", [])
        self.context_injection_enabled = bool(self.config.get("inject_context_summary", False))
        self.state_tracking_enabled = self.filtering_enabled or self.context_injection_enabled

        # Identify the IDE
        from core.host import resolve_host
        self.ide_name = resolve_host(project_path, host).provider
        self.ide_profile = get_profile(self.ide_name)

        # Initialize Ollama client (optional, for SLM classification)
        self.ollama = None
        if self.filtering_enabled and self.config.get("use_slm", True):
            try:
                self.ollama = OllamaClient()
            except Exception:
                pass

        # Tool classifier
        self.classifier = ToolClassifier(
            always_visible=self.config.get("always_visible", ["core"]),
            max_tools=self.config.get("max_tools", 30),
            use_slm=self.filtering_enabled and self.config.get("use_slm", True),
            slm_model=self.config.get("slm_model", "gemma3n:latest"),
            ollama=self.ollama,
        )

        # Sliding window state
        self.state = ProxyState(
            window_size=self.config.get("context_window_size", 10),
        )

        # Track pending tool calls: request_id -> {name, args}
        self._pending: dict[str | int, dict] = {}

        # Cache the full tool list from the server
        self._all_tools: list[dict] = []
        self._last_active_categories: list[str] = ["core"]
        self._last_visible_tool_names: tuple[str, ...] = ()
        self._last_classification_input: tuple[str, tuple[str, ...]] = ("", ())

        # Metrics
        self._metrics = {
            "started_at": time.time(),
            "messages_forwarded": 0,
            "tools_list_filtered": 0,
            "tools_calls_intercepted": 0,
            "context_injections": 0,
            "list_changed_sent": 0,
            "errors": 0,
        }

        # Subprocess
        self._process: asyncio.subprocess.Process | None = None

    async def run(self):
        """Main entry point - spawn subprocess and start forwarding."""
        server_script = str(Path(__file__).parent / "mcp_server.py")
        python_exe = sys.executable

        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._process = await asyncio.create_subprocess_exec(
            python_exe, server_script,
            "--project", self.project_path, "--host", self.ide_name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs
        )

        # Forward stderr from subprocess to our stderr
        stderr_task = asyncio.create_task(self._forward_stderr())

        # Bidirectional forwarding
        client_to_server = asyncio.create_task(self._read_client_forward_server())
        server_to_client = asyncio.create_task(self._read_server_forward_client())

        try:
            # Wait for either direction to finish (means subprocess died or stdin closed)
            done, pending = await asyncio.wait(
                [client_to_server, server_to_client, stderr_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel remaining tasks
            for task in pending:
                task.cancel()
        except asyncio.CancelledError:
            pass
        finally:
            self._write_metrics()
            if self._process and self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._process.kill()

    # ── Client → Server ────────────────────────────────────

    async def _read_client_forward_server(self):
        """Read from our stdin (Claude Code), forward to subprocess stdin.

        Uses a background thread for stdin reads because Windows ProactorEventLoop
        does not support connect_read_pipe on stdin.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _stdin_thread():
            """Blocking stdin reader running in a daemon thread."""
            try:
                while True:
                    line = sys.stdin.buffer.readline()
                    if not line:
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        t = threading.Thread(target=_stdin_thread, daemon=True)
        t.start()

        while True:
            line = await queue.get()
            if line is None:
                # Client disconnected
                if self._process and self._process.stdin:
                    self._process.stdin.close()
                break

            if not line.strip():
                continue

            try:
                msg = json.loads(line)
                msg = self._intercept_client_to_server(msg)
                out = json.dumps(msg, separators=(",", ":")) + "\n"
            except Exception:
                # Passthrough on parse/intercept failure
                out = line.decode("utf-8", errors="replace") + "\n"
                self._metrics["errors"] += 1

            self._metrics["messages_forwarded"] += 1
            if self._process and self._process.stdin:
                self._process.stdin.write(out.encode("utf-8"))
                await self._process.stdin.drain()

    # ── Server → Client ────────────────────────────────────

    async def _read_server_forward_client(self):
        """Read from subprocess stdout, forward to our stdout (Claude Code)."""
        stdout = self._process.stdout
        buffer = b""

        while True:
            chunk = await stdout.read(65536)
            if not chunk:
                break

            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue

                try:
                    msg = json.loads(line)
                    extra_msgs = []
                    msg, extra_msgs = self._intercept_server_to_client(msg)
                    out = json.dumps(msg, separators=(",", ":")) + "\n"

                    # Write the main message
                    sys.stdout.buffer.write(out.encode("utf-8"))
                    sys.stdout.buffer.flush()

                    # Write any extra messages (e.g., list_changed notifications)
                    for extra in extra_msgs:
                        extra_out = json.dumps(extra, separators=(",", ":")) + "\n"
                        sys.stdout.buffer.write(extra_out.encode("utf-8"))
                        sys.stdout.buffer.flush()

                    self._metrics["messages_forwarded"] += 1
                    continue
                except Exception:
                    self._metrics["errors"] += 1

                # NEVER passthrough non-JSON to stdout (it breaks MCP handshake in Codex/Claude)
                # Instead, redirect to our stderr so the user can see the log/error
                sys.stderr.buffer.write(b"[proxy:server_stdout] " + line + b"\n")
                sys.stderr.buffer.flush()

    # ── Stderr forwarding ──────────────────────────────────

    async def _forward_stderr(self):
        """Forward subprocess stderr to our stderr."""
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    # ── Interception ───────────────────────────────────────

    def _intercept_client_to_server(self, msg: dict) -> dict:
        """Intercept client→server messages. Track tools/call requests."""
        if self.disabled:
            return msg

        method = msg.get("method")
        if method == "tools/call":
            req_id = msg.get("id")
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            if req_id is not None:
                self._pending[req_id] = {"name": tool_name, "args": args}
            self._metrics["tools_calls_intercepted"] += 1

        return msg

    def _intercept_server_to_client(self, msg: dict) -> tuple[dict, list[dict]]:
        """Intercept server→client messages. Filter tools/list, inject context."""
        extra_msgs = []

        if self.disabled:
            return msg, extra_msgs

        # ── tools/list response ──────────────────────────
        # This is a response (has "result") to a tools/list request
        result = msg.get("result")
        if result and isinstance(result, dict) and "tools" in result:
            all_tools = result["tools"]
            # Cache the full tool list
            if len(all_tools) > len(self._all_tools):
                self._all_tools = all_tools

            if not self.filtering_enabled:
                return msg, extra_msgs

            # Classify and filter
            active = self.classifier.classify(
                self.state.get_recent_tool_names(),
                self.state.get_recent_text(),
            )
            self._last_active_categories = active
            filtered = self.classifier.filter_tools(all_tools, active)
            msg["result"] = {"tools": filtered}
            self._last_visible_tool_names = tuple(sorted(
                t.get("name", "") for t in filtered if isinstance(t, dict)
            ))
            self._last_classification_input = (self.state.get_recent_text(), tuple(self.state.get_recent_tool_names()))
            self._metrics["tools_list_filtered"] += 1
            return msg, extra_msgs

        # ── tools/call response ──────────────────────────
        req_id = msg.get("id")
        if req_id is not None and req_id in self._pending:
            call_info = self._pending.pop(req_id)
            tool_name = call_info["name"]
            args = call_info["args"]

            # Extract response text for state tracking
            response_text = ""
            if result:
                if isinstance(result, dict):
                    content = result.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                response_text = item.get("text", "")
                                break
                    elif isinstance(content, str):
                        response_text = content

            if self.state_tracking_enabled:
                self.state.record_tool_call(tool_name, args, response_text)

            # Inject context summary
            if self.context_injection_enabled and response_text:
                context_line = self.state.get_context_line()
                if context_line:
                    # Append context line to the response text
                    new_text = response_text + context_line
                    if result and isinstance(result, dict):
                        content = result.get("content", [])
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    item["text"] = new_text
                                    break
                    self._metrics["context_injections"] += 1

            if self.state_tracking_enabled:
                self._write_state()

            if self.filtering_enabled:
                # Avoid redundant re-classification if state hasn't changed
                current_text = self.state.get_recent_text()
                current_tools = tuple(self.state.get_recent_tool_names())
                current_input = (current_text, current_tools)

                if current_input != self._last_classification_input:
                    self._last_classification_input = current_input
                    next_active = self.classifier.classify(
                        list(current_tools),
                        current_text,
                    )
                    next_visible = tuple(sorted(
                        t
                        for t in {
                            tool.get("name", "")
                            for tool in self.classifier.filter_tools(self._all_tools, next_active)
                        }
                        if t
                    ))
                    if next_visible != self._last_visible_tool_names:
                        self._last_active_categories = next_active
                        self._last_visible_tool_names = next_visible
                        extra_msgs.append({
                            "jsonrpc": "2.0",
                            "method": "notifications/tools/list_changed",
                        })
                        self._metrics["list_changed_sent"] += 1

        return msg, extra_msgs

    # ── Live State ─────────────────────────────────────────

    def _write_state(self):
        """Write live proxy state to .c3/proxy_state.json for UI consumption."""
        try:
            state = {
                "last_updated": time.time(),
                "ide": self.ide_name,
                "ide_display": self.ide_profile.display_name,
                "current_goal": self.state.current_goal,
                "recent_files": list(self.state.recent_files),
                "recent_decisions": list(self.state.recent_decisions),
                "recent_tools": [
                    {"name": c["name"], "summary": c["summary"]}
                    for c in list(self.state.tool_calls)[-3:]
                ],
                "context_line": self.state.get_context_line().strip(),
                "classification_reasons": self.classifier.classification_reasons,
            }
            state_path = Path(self.project_path) / ".c3" / "proxy_state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    # ── Metrics ────────────────────────────────────────────

    def _write_metrics(self):
        """Write proxy metrics to .c3/proxy_metrics.json."""
        try:
            self._metrics["uptime_seconds"] = round(
                time.time() - self._metrics["started_at"], 1
            )
            self._metrics["total_tools_available"] = len(self._all_tools)
            self._metrics["active_categories"] = self._last_active_categories
            self._metrics["active_tool_count"] = self.classifier.get_active_tool_count(
                self._last_active_categories
            )

            metrics_path = Path(self.project_path) / ".c3" / "proxy_metrics.json"
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            with open(metrics_path, "w", encoding="utf-8") as f:
                json.dump(self._metrics, f, indent=2)
        except Exception:
            pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="C3 MCP Proxy")
    parser.add_argument("--project", required=True, help="Project root path")
    parser.add_argument("--host")
    args = parser.parse_args()
    proxy = MCPProxy(args.project, host=args.host)
    asyncio.run(proxy.run())


if __name__ == "__main__":
    main()
