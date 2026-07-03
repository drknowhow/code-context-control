import tempfile
import unittest
from pathlib import Path

from cli.tools._helpers import maybe_related_facts
from services.memory import MemoryStore
from services.vector_store import VectorStore


class _Svc:
    def __init__(self, project_path, memory):
        self.project_path = project_path
        self.memory = memory


class TestMaybeRelatedFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir(exist_ok=True)
        vector = VectorStore(str(self.project), config={"disable_vector_backend": True})
        self.memory = MemoryStore(str(self.project), vector_store=vector)
        self.svc = _Svc(str(self.project), self.memory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_outside_read_context(self):
        self.memory.remember("memory distiller facts about services/memory_distiller.py", "gotcha")
        self.assertEqual(maybe_related_facts(self.svc, "memory_distiller.py"), "")
        self.assertEqual(maybe_related_facts(self.svc, "memory_distiller.py", context="search"), "")

    def test_read_context_surfaces_matching_fact(self):
        self.memory.remember(
            "services/memory_distiller.py owns the cloud LLM chain; never route it through delegate",
            "gotcha")
        out = maybe_related_facts(self.svc, "services/memory_distiller.py", context="read")
        self.assertIn("[c3:related]", out)
        self.assertIn("(gotcha)", out)

    def test_auto_categories_excluded(self):
        self.memory.remember(
            "Session summary about services/memory_distiller.py and other files", "auto:session")
        out = maybe_related_facts(self.svc, "services/memory_distiller.py", context="read")
        self.assertNotIn("auto:session", out)

    def test_no_match_returns_empty(self):
        self.memory.remember("completely unrelated fact about deployment quotas", "context")
        out = maybe_related_facts(self.svc, "services/watcher.py", context="read")
        self.assertEqual(out, "")

    def test_flag_off_returns_empty(self):
        import json
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"memory_llm": {"read_related_facts_enabled": False}}), encoding="utf-8")
        self.memory.remember(
            "services/memory_distiller.py owns the cloud LLM chain and its breaker", "gotcha")
        out = maybe_related_facts(self.svc, "services/memory_distiller.py", context="read")
        self.assertEqual(out, "")

    def test_line_cap_respected(self):
        self.memory.remember(
            "services/memory_distiller.py " + "very long fact body " * 30, "gotcha")
        out = maybe_related_facts(self.svc, "services/memory_distiller.py",
                                  context="read", width=80)
        for line in out.strip().splitlines():
            self.assertLessEqual(len(line), len("[c3:related] (gotcha) ") + 80)

    def test_broken_svc_is_silent(self):
        self.assertEqual(maybe_related_facts(object(), "x.py", context="read"), "")


if __name__ == "__main__":
    unittest.main()
