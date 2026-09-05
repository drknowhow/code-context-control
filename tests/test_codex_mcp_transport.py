"""Real stdio handshake and tool call, without starting an AI client or model."""
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_stdio_host_session(tmp_path, host):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Implementation

    repo = Path(__file__).resolve().parents[1]
    project = tmp_path / "project with spaces"
    project.mkdir()
    c3 = project / ".c3"
    c3.mkdir()
    # Deliberately disagree with the caller: project preference cannot identify
    # the connecting client. Child configuration and all state are isolated.
    (c3 / "config.json").write_text(json.dumps({"ide": "claude-code" if host == "codex" else "codex",
        "hybrid": {"HYBRID_DISABLE_SLTM": True, "rag": {"enabled": False}},
        "delegate": {"codex_enabled": False, "gemini_enabled": False}}))
    (project / "example.py").write_text("def answer():\n    return 42\n")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(home / ".codex"),
                "C3_BENCHMARK_MODE": "1", "C3_TELEMETRY_OPT_IN": "0", "SENTRY_DSN": "",
                "CODEX_THREAD_ID": "synthetic-codex-thread", "CLAUDE_CODE_SESSION_ID": "synthetic-claude-thread"})
    params = StdioServerParameters(command=sys.executable,
        args=[str(repo / "cli/mcp_server.py"), "--project", str(project), "--host", host],
        cwd=str(project), env=env)

    async def exercise():
        with (tmp_path / "mcp-stderr.log").open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30),
                                         client_info=Implementation(name=host, version="test")) as client:
                    initialized = await client.initialize()
                    assert "C3" in initialized.serverInfo.name
                    tools = await client.list_tools()
                    assert "c3_read" in {t.name for t in tools.tools}
                    response = await client.call_tool("c3_read", {"file_path": "example.py", "lines": [1, 2]})
                    assert not response.isError
                    assert "42" in "\n".join(getattr(c, "text", "") for c in response.content)
                    await client.call_tool("c3_session", {"action": "save", "summary": "Transport smoke test"})
        sessions = [json.loads(p.read_text(encoding="utf-8")) for p in (c3 / "sessions").glob("session_*.json")]
        assert sessions
        assert sessions[-1]["source_ide"] == host
        assert sessions[-1]["host_session_id"] == ("synthetic-codex-thread" if host == "codex" else "synthetic-claude-thread")

    asyncio.run(asyncio.wait_for(exercise(), timeout=55))
