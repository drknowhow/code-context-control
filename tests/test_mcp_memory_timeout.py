"""Request-level timeout coverage for c3_memory retrieval actions."""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cli import mcp_server


class TestMemoryToolTimeout(unittest.TestCase):
    def test_recall_timeout_returns_without_tool_error(self):
        svc = SimpleNamespace(hybrid_config={"memory_retrieval_timeout_seconds": 0.1})
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=svc))

        def slow_memory(*args, **kwargs):
            time.sleep(0.25)
            return "late"

        with patch.object(mcp_server, "handle_memory", side_effect=slow_memory):
            result = asyncio.run(mcp_server.c3_memory("recall", query="test", ctx=ctx))

        self.assertIn("[memory:timeout]", result)
        self.assertIn("other tools remain available", result)

    def test_non_retrieval_action_is_not_time_limited(self):
        svc = SimpleNamespace(hybrid_config={"memory_retrieval_timeout_seconds": 0.1})
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=svc))
        with patch.object(mcp_server, "handle_memory", return_value="remembered"):
            result = asyncio.run(mcp_server.c3_memory(
                "add", fact="durable fact", ctx=ctx))
        self.assertEqual(result, "remembered")


if __name__ == "__main__":
    unittest.main()
