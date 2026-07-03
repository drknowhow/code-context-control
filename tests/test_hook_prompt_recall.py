import json
import tempfile
import unittest
from pathlib import Path

from cli.hook_dispatch import dispatch
from cli.hook_prompt_recall import run


def _fact(fid, text, category="gotcha", lifecycle="active"):
    return {"id": fid, "fact": text, "category": category, "lifecycle": lifecycle}


class TestHookPromptRecall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3" / "facts").mkdir(parents=True)
        self._write_facts([
            _fact("f1", "hook commands need the cmd.exe prefix because paths with parentheses break Git Bash"),
            _fact("f2", "the vector store must lazy-init or the MCP handshake times out", "decision"),
            _fact("f3", "archived fact about ancient history", lifecycle="archived"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _write_facts(self, facts):
        (self.project / ".c3" / "facts" / "facts.json").write_text(
            json.dumps(facts), encoding="utf-8")

    def _payload(self, prompt):
        return {"prompt": prompt, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit"}

    def test_injects_relevant_facts(self):
        out = run(self._payload("why do the hook commands use a cmd.exe prefix?"))
        self.assertIsNotNone(out)
        ctx = out["additionalContext"]
        self.assertIn("[c3:memory]", ctx)
        self.assertIn("cmd.exe prefix", ctx)

    def test_archived_facts_excluded(self):
        out = run(self._payload("tell me the archived fact about ancient history"))
        if out:  # other facts may weakly match; the archived one must not appear
            self.assertNotIn("ancient history", out["additionalContext"])

    def test_short_prompt_skipped(self):
        self.assertIsNone(run(self._payload("hi")))

    def test_no_facts_file_is_silent(self):
        (self.project / ".c3" / "facts" / "facts.json").unlink()
        self.assertIsNone(run(self._payload("why do hooks use the cmd.exe prefix?")))

    def test_corrupt_facts_file_is_silent(self):
        (self.project / ".c3" / "facts" / "facts.json").write_text("{corrupt", encoding="utf-8")
        self.assertIsNone(run(self._payload("why do hooks use the cmd.exe prefix?")))

    def test_flag_off_disables_injection(self):
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"memory_llm": {"prompt_inject_enabled": False}}), encoding="utf-8")
        self.assertIsNone(run(self._payload("why do hooks use the cmd.exe prefix?")))

    def test_no_project_is_silent(self):
        with tempfile.TemporaryDirectory() as bare:
            self.assertIsNone(run({"prompt": "a long enough prompt here", "cwd": bare}))

    def test_read_only_facts_file_untouched(self):
        facts_file = self.project / ".c3" / "facts" / "facts.json"
        before = facts_file.read_text(encoding="utf-8")
        run(self._payload("why do the hook commands use a cmd.exe prefix?"))
        self.assertEqual(facts_file.read_text(encoding="utf-8"), before)

    def test_output_respects_token_cap(self):
        many = [_fact(f"f{i}", f"gotcha number {i}: the cmd.exe prefix rule applies to hook command {i}")
                for i in range(50)]
        self._write_facts(many)
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"memory_llm": {"inject_max_tokens": 50, "inject_top_k": 40}}),
            encoding="utf-8")
        out = run(self._payload("what is the cmd.exe prefix rule for hook commands?"))
        self.assertIsNotNone(out)
        self.assertLessEqual(len(out["additionalContext"]), 50 * 4 + _line_slack())

    def test_dispatch_wraps_in_user_prompt_submit_shape(self):
        result = dispatch("prompt", self._payload(
            "why do the hook commands use a cmd.exe prefix?"))
        self.assertIsNotNone(result)
        hso = result.get("hookSpecificOutput")
        self.assertIsNotNone(hso)
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("[c3:memory]", hso["additionalContext"])
        self.assertNotIn("additionalContext", result)  # moved, not duplicated

    def test_dispatch_empty_prompt_returns_none(self):
        self.assertIsNone(dispatch("prompt", self._payload("hi")))


def _line_slack():
    # one line may finish just under the cap; allow a single line of overshoot
    return 220 + len("- (gotcha) ")


if __name__ == "__main__":
    unittest.main()
