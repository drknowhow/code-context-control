import tempfile
import unittest
from pathlib import Path

from services.context_snapshot import ContextSnapshot
from services.conversation_store import ConversationStore
from services.file_memory import FileMemoryStore
from services.memory import MemoryStore
from services.retrieval_broker import MemoryRetrievalBroker
from services.vector_store import VectorStore


class _StubSessionManager:
    def __init__(self):
        self.current_session = {
            "id": "sess-1",
            "decisions": [{"decision": "Use broker retrieval", "reasoning": "single query path"}],
            "files_touched": [{"file": "src/demo.py", "type": "code", "summary": "demo file"}],
            "context_notes": ["important note"],
            "context_budget": {"response_tokens": 12, "call_count": 1, "compression_level": 0},
        }


class TestMemorySystem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir(exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_fact_removes_vector_record(self):
        vector = VectorStore(str(self.project), config={"disable_vector_backend": True})
        memory = MemoryStore(str(self.project), vector_store=vector)

        stored = memory.remember("broker delete consistency term", "general", "sess-1")
        fact_id = stored["id"]
        self.assertTrue(any(result["id"] == fact_id for result in vector.search("consistency term", top_k=5)))

        deleted = memory.delete_fact(fact_id)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(any(result["id"] == fact_id for result in vector.search("consistency term", top_k=5)))

    def test_retrieval_broker_combines_sources(self):
        vector = VectorStore(str(self.project), config={"disable_vector_backend": True})
        memory = MemoryStore(str(self.project), vector_store=vector)
        convo = ConversationStore(str(self.project))
        file_memory = FileMemoryStore(str(self.project))
        snapshots = ContextSnapshot(str(self.project))
        broker = MemoryRetrievalBroker(str(self.project), memory, convo, file_memory, snapshots)
        memory.set_retrieval_broker(broker)

        memory.remember("alpha architecture fact", "design_docs", "sess-1")
        convo.add_turn("chat-1", "user", "conversation alpha retrieval context")

        src = self.project / "src"
        src.mkdir()
        demo = src / "demo.py"
        demo.write_text("def alpha_handler():\n    return 'alpha'\n", encoding="utf-8")
        file_memory.update("src/demo.py", ai_summary="alpha file summary")

        snapshots.capture(_StubSessionManager(), memory, task_description="alpha checkpoint", working_files=["src/demo.py"])

        result = broker.search("alpha", top_k=5)
        self.assertTrue(result["facts"])
        self.assertTrue(result["conversations"])
        self.assertTrue(result["files"])
        self.assertTrue(result["snapshots"])
        self.assertTrue(any(hit["kind"] == "fact" for hit in result["results"]))
        self.assertTrue(any(hit["kind"] == "conversation" for hit in result["results"]))

    def test_file_memory_queue_uses_claim_and_complete(self):
        file_memory = FileMemoryStore(str(self.project))
        file_memory.queue_for_update("a.py")
        file_memory.queue_for_update("a.py")
        file_memory.queue_for_update("b.py")

        claimed = file_memory.drain_queue()
        self.assertEqual(claimed, ["a.py", "b.py"])
        self.assertEqual(file_memory.drain_queue(), ["a.py", "b.py"])

        file_memory.complete_updates(["a.py"])
        self.assertEqual(file_memory.drain_queue(), ["b.py"])

        file_memory.complete_updates(["b.py"], failed=True)
        self.assertEqual(file_memory.drain_queue(), ["b.py"])

    def test_memory_count_invariant(self):
        """Invariant: status-visible totals must match what recall/list see.

        Regression test for the category='general' default bug that made
        c3_memory(action='list') report 0 facts while status reported 84.
        """
        vector = VectorStore(str(self.project), config={"disable_vector_backend": True})
        memory = MemoryStore(str(self.project), vector_store=vector)

        memory.remember("arch: uses x", "architecture", "s1")
        memory.remember("arch: uses y", "architecture", "s1")
        memory.remember("conv: do z", "convention", "s1")
        memory.remember("default fact", "", "s1")  # empty -> handled by tool; raw store stays
        memory.remember("archive me", "architecture", "s1")
        memory.delete_fact(memory.facts[-1]["id"])  # delete one

        all_facts = memory.facts
        active = [f for f in all_facts if f.get("lifecycle") != "archived"]

        # Single source of truth: raw list length
        self.assertEqual(len(all_facts), len(active),
                         "no archived facts created, but active != total")

        # Every recall result must be in the active set
        hits = memory.recall("arch", top_k=10)
        for h in hits:
            self.assertEqual(h.get("lifecycle", "active"), "active")
            self.assertIn(h["id"], {f["id"] for f in active})

        # Category breakdown must sum to active total
        by_cat = {}
        for f in active:
            by_cat[f.get("category", "general")] = by_cat.get(f.get("category", "general"), 0) + 1
        self.assertEqual(sum(by_cat.values()), len(active))

    def test_memory_list_tool_respects_category(self):
        """handle_memory(list) must not silently filter when category=''."""
        from cli.tools.memory import handle_memory

        class _Svc:
            def __init__(self, mem):
                self.memory = mem
                self.session_mgr = _StubSessionManager()
                self.vector_store = None
                self.memory_graph = None
                self.memory_scorer = None

        memory = MemoryStore(str(self.project))
        memory.remember("arch fact", "architecture", "s1")
        memory.remember("conv fact", "convention", "s1")
        svc = _Svc(memory)

        def fin(name, args, resp, summ, **kw):
            return resp

        # No category -> see all
        out_all = handle_memory("list", "", "", "", 5, svc, fin)
        self.assertIn("2 fact(s)", out_all)
        self.assertIn("[architecture]", out_all)
        self.assertIn("[convention]", out_all)

        # Specific category -> filter
        out_arch = handle_memory("list", "", "", "architecture", 5, svc, fin)
        self.assertIn("1 fact(s)", out_arch)
        self.assertIn("[architecture]", out_arch)
        self.assertNotIn("[convention]", out_arch)

        # Unknown category -> 0 with helpful hint
        out_none = handle_memory("list", "", "", "nonexistent", 5, svc, fin)
        self.assertIn("0 facts", out_none)
        self.assertIn("active categories:", out_none)

    def test_snapshot_restore_exposes_machine_state(self):
        memory = MemoryStore(str(self.project))
        memory.remember("snapshot state fact", "general", "sess-1")
        snapshots = ContextSnapshot(str(self.project))

        captured = snapshots.capture(_StubSessionManager(), memory, task_description="stateful restore", working_files=["src/demo.py"])
        restored = snapshots.restore(captured["snapshot_id"], level=1)
        state = snapshots.restore_state(captured["snapshot_id"])

        self.assertIn("state", restored)
        self.assertEqual(state["state"]["task_description"], "stateful restore")
        self.assertEqual(state["state"]["working_files"], ["src/demo.py"])


if __name__ == "__main__":
    unittest.main()
