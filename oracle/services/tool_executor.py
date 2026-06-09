"""Standalone tool-dispatch adapter for the Oracle Discovery API.

The concrete tool implementations live on :class:`~oracle.services.chat_engine.ChatEngine`
because they share thread-local streaming machinery used by the chat UI (see
``_agent_tls`` in ``chat_engine.py``). Rather than duplicate that logic, this thin
adapter routes the external Discovery API through ``ChatEngine.run_tool`` so the
internal chat loop and the external API share exactly one dispatch path and can
never diverge.
"""

from __future__ import annotations

from typing import Any, Protocol


class ToolHost(Protocol):
    """Anything that can dispatch a tool by name (duck-typed ChatEngine)."""

    def run_tool(self, name: str, args: dict | None = ...) -> dict: ...


class ToolExecutor:
    """Adapter exposing ``execute(name, args)`` over a tool host."""

    def __init__(self, host: ToolHost):
        self._host = host

    def execute(self, name: str, args: dict | None = None) -> dict[str, Any]:
        """Dispatch a single tool call, returning the host's result dict."""
        return self._host.run_tool(name, args or {})
