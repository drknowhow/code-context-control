"""Tests for the MCP transport Host-header allowlist (oracle/mcp_oracle.py).

Defense-in-depth against DNS rebinding on the :3332 streamable-HTTP transport,
on top of the Bearer gate.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.web_security import allowed_hostnames  # noqa: E402
from oracle.mcp_oracle import _HostGuardMiddleware  # noqa: E402


class TestMcpHostGuard(unittest.TestCase):
    def _drive(self, host_header: bytes) -> dict:
        state = {"passed": False, "sent": []}

        async def inner(scope, receive, send):
            state["passed"] = True

        async def send(message):
            state["sent"].append(message)

        async def receive():
            return {"type": "http.request"}

        mw = _HostGuardMiddleware(inner, allowed_hostnames("127.0.0.1"))
        scope = {"type": "http", "headers": [(b"host", host_header)]}
        asyncio.run(mw(scope, receive, send))
        return state

    def test_blocks_rebinding_host(self):
        s = self._drive(b"evil.com")
        self.assertFalse(s["passed"])
        self.assertEqual(s["sent"][0]["status"], 403)

    def test_blocks_empty_host(self):
        s = self._drive(b"")
        self.assertFalse(s["passed"])

    def test_allows_loopback_ip(self):
        self.assertTrue(self._drive(b"127.0.0.1:3332")["passed"])

    def test_allows_localhost(self):
        self.assertTrue(self._drive(b"localhost:3332")["passed"])


if __name__ == "__main__":
    unittest.main()
