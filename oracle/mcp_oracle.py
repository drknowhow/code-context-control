"""FastMCP HTTP/SSE server exposing the Oracle Discovery tools to external LLMs.

Serves the same :class:`~oracle.services.tool_registry.ToolRegistry` as the REST
surface, over MCP streamable-HTTP at ``http://<host>:<mcp_port>/mcp``, guarded by
the shared API-key (Bearer) auth. Runs in a daemon thread alongside the Oracle
Flask app — uvicorn with signal handlers disabled, since those only install on
the main thread.

All heavy imports (fastmcp, starlette, uvicorn) are lazy so importing this module
never hard-requires them; the Oracle degrades to REST-only if they are missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from oracle.services import api_auth

logger = logging.getLogger("oracle.mcp")

_INSTRUCTIONS = (
    "C3 Oracle Discovery — use C3's cross-project code & memory intelligence as tools.\n"
    "\n"
    "Recommended workflow:\n"
    "1. list_projects — see which C3 projects exist (names + absolute paths).\n"
    "2. Discover across ALL projects: search_facts (memory) or c3_search_cross (code).\n"
    "3. Narrow to one project using its path: c3_search to find code; c3_compress "
    "(mode='map') to see a file's shape before reading; c3_read for exact content; "
    "query_memory / read_graph / cross_insights for that project's memory.\n"
    "\n"
    "Notes: per-project tools REQUIRE a `project_path` taken from list_projects. Every tool "
    "returns JSON. suggest_action creates a PENDING suggestion for a human to approve (not a "
    "direct write); delegate_task runs a configured Oracle agent. Read + safe-action tiers "
    "only — no code-editing tools are exposed."
)


def _build_registry_tool_cls():
    """Build the RegistryTool subclass (lazy — needs fastmcp + pydantic)."""
    from fastmcp.tools.tool import Tool, ToolResult
    from pydantic import PrivateAttr

    class RegistryTool(Tool):
        """A FastMCP tool whose schema + dispatch come from the ToolRegistry."""

        _registry: Any = PrivateAttr(default=None)

        async def run(self, arguments: dict) -> ToolResult:  # type: ignore[override]
            try:
                result = self._registry.call_tool(self.name, arguments or {})
            except Exception as e:  # defensive: never surface a raw 500
                result = {"error": f"tool '{self.name}' failed: {e}"}
            return ToolResult(content=json.dumps(result, default=str))

    return RegistryTool


def build_mcp(registry, version: str = ""):
    """Create a FastMCP server with one MCP tool per available registry spec."""
    from fastmcp import FastMCP

    name = f"C3 Oracle Discovery v{version}" if version else "C3 Oracle Discovery"
    mcp = FastMCP(name, instructions=_INSTRUCTIONS)
    registry_tool_cls = _build_registry_tool_cls()
    for spec in registry.list_tools():
        tool = registry_tool_cls(
            name=spec["name"],
            description=spec["description"],
            parameters=spec["parameters"],
        )
        tool._registry = registry
        mcp.add_tool(tool)
    return mcp


class _BearerAuthMiddleware:
    """Pure-ASGI Bearer-token gate.

    Implemented at the ASGI layer (not Starlette's ``BaseHTTPMiddleware``) so it
    never buffers responses — MCP streamable-HTTP / SSE responses stream through
    intact. Rejects requests whose ``Authorization`` header fails ``api_auth.verify``.

    Enforcement is decided per request from live config (``api_require_auth``),
    so toggling auth in the dashboard takes effect without restarting the MCP
    transport — matching the Flask Discovery guard. ``require_auth`` is only the
    fallback default if config can't be read.
    """

    def __init__(self, app, require_auth: bool = True):
        self.app = app
        self.require_auth = require_auth

    def _enforce(self) -> bool:
        """Read live ``api_require_auth`` (falls back to the build-time value)."""
        try:
            from oracle.config import load_config
            return bool(load_config().get("api_require_auth", self.require_auth))
        except Exception:
            return self.require_auth

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and self._enforce():
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if not api_auth.verify(api_auth.extract_bearer(auth)):
                body = b'{"error": "unauthorized"}'
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class _HostGuardMiddleware:
    """Pure-ASGI Host-header allowlist.

    Rejects requests whose ``Host`` header is not loopback or the configured
    bind host — defeating DNS-rebinding against the MCP transport. Defense in
    depth on top of the Bearer gate (a rebound request would still need a valid
    token, but this stops it reaching the app at all).
    """

    def __init__(self, app, allowed: set):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            from core.web_security import _hostname
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1")
            if _hostname(host) not in self.allowed:
                body = b'{"error": "forbidden host"}'
                await send({
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_app(registry, version: str = "", require_auth: bool = True, path: str = "/mcp",
              host: str = "127.0.0.1", allowed_hosts=None):
    """Build the Starlette ASGI app for the MCP server (auth middleware attached)."""
    mcp = build_mcp(registry, version)
    app = mcp.http_app(path=path)
    # Always install the gate; it decides per request from live config whether to
    # enforce (so enabling auth in the dashboard works without a restart). The
    # build-time ``require_auth`` is passed as the fallback default only.
    app.add_middleware(_BearerAuthMiddleware, require_auth=require_auth)
    # Host-header allowlist (defense-in-depth vs DNS rebinding). Added last so it
    # is the outermost middleware and runs before the bearer check.
    from core.web_security import allowed_hostnames
    app.add_middleware(_HostGuardMiddleware, allowed=allowed_hostnames(host, allowed_hosts))
    return app


def serve_mcp(registry, host: str = "127.0.0.1", port: int = 3332,
              version: str = "", require_auth: bool = True, path: str = "/mcp",
              allowed_hosts=None) -> None:
    """Blocking: serve the MCP app with uvicorn. Safe to run off the main thread."""
    import uvicorn

    app = build_app(registry, version=version, require_auth=require_auth, path=path,
                    host=host, allowed_hosts=allowed_hosts)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    # uvicorn only installs signal handlers on the main thread; disable so this
    # can run inside a daemon thread under the Flask server.
    server.install_signal_handlers = lambda: None
    asyncio.run(server.serve())


def start_mcp_thread(registry, host: str = "127.0.0.1", port: int = 3332,
                     version: str = "", require_auth: bool = True,
                     path: str = "/mcp", allowed_hosts=None) -> threading.Thread:
    """Start :func:`serve_mcp` in a daemon thread and return the thread."""

    def _run():
        try:
            serve_mcp(registry, host=host, port=port, version=version,
                      require_auth=require_auth, path=path, allowed_hosts=allowed_hosts)
        except Exception:
            logger.exception("Oracle MCP server crashed")

    thread = threading.Thread(target=_run, daemon=True, name="oracle-mcp")
    thread.start()
    logger.info("Oracle MCP server thread started on http://%s:%s%s", host, port, path)
    return thread


def mcp_url(host: str, port: int, path: str = "/mcp") -> str:
    """Loopback-friendly URL for clients (collapses 0.0.0.0 to 127.0.0.1)."""
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"http://{display_host}:{port}{path}"
