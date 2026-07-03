import json
import tempfile
import unittest
from pathlib import Path

from services.memory import MemoryStore
from services.memory_distiller import (
    DISTILL_CATEGORIES,
    MINING_CATEGORIES,
    DistillerBreaker,
    MemoryDistiller,
)
from services.vector_store import VectorStore


def _config(**overrides):
    from core.config import MEMORY_LLM_DEFAULTS
    cfg = dict(MEMORY_LLM_DEFAULTS)
    cfg.update(overrides)
    return cfg


class FakeBridge:
    """Stands in for OllamaBridge: scripted auth status + responses."""

    def __init__(self, auth="ok", response=None):
        self.auth = auth
        self.response = response
        self.generate_calls = 0

    def check_auth(self, timeout=None):
        return self.auth

    def generate(self, prompt, **kwargs):
        self.generate_calls += 1
        return self.response


class FakeLocalClient:
    def __init__(self, available=True, response=None):
        self.available = available
        self.response = response
        self.generate_calls = 0

    def is_available(self, timeout=None):
        return self.available

    def generate(self, prompt, **kwargs):
        self.generate_calls += 1
        return self.response


def _good_json(*facts):
    return json.dumps({"facts": [
        {"fact": f, "category": "gotcha", "confidence": 0.9} for f in facts
    ]})


class DistillerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir(exist_ok=True)
        vector = VectorStore(str(self.project), config={"disable_vector_backend": True})
        self.memory = MemoryStore(str(self.project), vector_store=vector)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, config=None, ollama=None):
        return MemoryDistiller(str(self.project), self.memory,
                               config or _config(), ollama_client=ollama)

    def job(self, sid="20260703_120000"):
        distiller = MemoryDistiller(str(self.project), self.memory, _config())
        return distiller.queue.enqueue("session_digest", sid, {
            "started_at": "2026-07-03T12:00:00+00:00",
            "ended_at": "2026-07-03T13:00:00+00:00",
            "decisions": ["use tmp+os.replace for atomic writes"],
            "files_touched": ["services/memory.py"],
        })


class TestParsing(DistillerTestBase):
    def test_clean_json_parses(self):
        d = self.make()
        facts = d._parse(_good_json("subprocess with shell=True hangs forever on Windows; use Popen+taskkill"),
                         allowed=DISTILL_CATEGORIES)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["category"], "gotcha")
        self.assertAlmostEqual(facts[0]["confidence"], 0.9)

    def test_fenced_json_is_salvaged(self):
        d = self.make()
        raw = "```json\n" + _good_json("hooks must use the cmd.exe /c prefix on Windows paths") + "\n```"
        self.assertEqual(len(d._parse(raw, allowed=DISTILL_CATEGORIES)), 1)

    def test_prose_wrapped_json_is_salvaged(self):
        d = self.make()
        raw = "Here are the facts:\n" + _good_json("the delegate model resolver mangles cloud tags silently")
        self.assertEqual(len(d._parse(raw, allowed=DISTILL_CATEGORIES)), 1)

    def test_garbage_returns_empty(self):
        d = self.make()
        self.assertEqual(d._parse("no json here at all", allowed=DISTILL_CATEGORIES), [])
        self.assertEqual(d._parse("", allowed=DISTILL_CATEGORIES), [])
        self.assertEqual(d._parse('{"facts": "not-a-list"}', allowed=DISTILL_CATEGORIES), [])

    def test_line_salvage_recovers_individual_objects(self):
        d = self.make()
        raw = ('{"facts": [ broken json...\n'
               '{"fact": "always run c3_validate after editing project source files", '
               '"category": "convention", "confidence": 0.8}\n')
        facts = d._parse(raw, allowed=DISTILL_CATEGORIES)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["category"], "convention")

    def test_category_whitelist_and_clamps(self):
        d = self.make()
        raw = json.dumps({"facts": [
            {"fact": "x" * 500, "category": "auto:session", "confidence": 5.0},
            {"fact": "too short", "category": "gotcha", "confidence": 0.5},
        ]})
        facts = d._parse(raw, allowed=DISTILL_CATEGORIES)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["category"], "context")   # whitelisted fallback
        self.assertEqual(len(facts[0]["fact"]), 300)         # max_fact_chars clamp
        self.assertEqual(facts[0]["confidence"], 1.0)        # confidence clamp

    def test_max_facts_cap(self):
        d = self.make(_config(max_facts_per_session=2))
        raw = _good_json(*(f"durable fact number {i} about the project setup" for i in range(6)))
        self.assertEqual(len(d._parse(raw, allowed=DISTILL_CATEGORIES)), 2)


class TestGenerationChain(DistillerTestBase):
    def test_cloud_tier_used_when_configured(self):
        d = self.make(_config(cloud_enabled=True, api_key="k"))
        d._cloud, d._cloud_checked = FakeBridge(response="{}"), True
        text, tier = d._generate("p")
        self.assertEqual(tier, "cloud")

    def test_cloud_disabled_without_optin(self):
        d = self.make(_config(cloud_enabled=False, api_key="k"))
        self.assertFalse(d.cloud_usable())

    def test_no_key_no_local_proxy_means_no_cloud(self):
        from unittest.mock import patch
        d = self.make(_config(cloud_enabled=True, api_key="",
                              cloud_base_url="https://ollama.com"))
        import os
        old = os.environ.pop("OLLAMA_API_KEY", None)
        try:
            with patch("services.ollama_credentials.load_api_key", return_value=None):
                self.assertFalse(d.cloud_usable())
        finally:
            if old is not None:
                os.environ["OLLAMA_API_KEY"] = old

    def test_keyring_key_enables_cloud(self):
        from unittest.mock import patch
        d = self.make(_config(cloud_enabled=True, api_key="",
                              cloud_base_url="https://ollama.com"))
        import os
        old = os.environ.pop("OLLAMA_API_KEY", None)
        try:
            with patch("services.ollama_credentials.load_api_key",
                       return_value="ring-key-123"):
                self.assertTrue(d.cloud_usable())
                self.assertEqual(d._cloud.api_key, "ring-key-123")
        finally:
            if old is not None:
                os.environ["OLLAMA_API_KEY"] = old

    def test_localhost_proxy_needs_no_key(self):
        d = self.make(_config(cloud_enabled=True, api_key="",
                              cloud_base_url="http://localhost:11434"))
        import os
        old = os.environ.pop("OLLAMA_API_KEY", None)
        try:
            self.assertTrue(d.cloud_usable())
        finally:
            if old is not None:
                os.environ["OLLAMA_API_KEY"] = old

    def test_auth_failure_kills_cloud_for_process_and_falls_back(self):
        local = FakeLocalClient(response=_good_json("fallback fact from the local model tier"))
        d = self.make(_config(cloud_enabled=True, api_key="bad"), ollama=local)
        bridge = FakeBridge(auth="auth", response="never")
        d._cloud, d._cloud_checked = bridge, True
        text, tier = d._generate("p")
        self.assertEqual(tier, "local")
        self.assertEqual(bridge.generate_calls, 0)
        self.assertTrue(d._cloud_auth_dead)
        # second call: cloud not even probed
        d._generate("p")
        self.assertFalse(d.cloud_usable())

    def test_cloud_down_falls_back_to_local(self):
        local = FakeLocalClient(response=_good_json("fallback fact from the local model tier"))
        d = self.make(_config(cloud_enabled=True, api_key="k"), ollama=local)
        d._cloud, d._cloud_checked = FakeBridge(auth="down"), True
        text, tier = d._generate("p")
        self.assertEqual(tier, "local")
        self.assertFalse(d._cloud_auth_dead)  # transient, may heal next process

    def test_everything_down_returns_mechanical(self):
        d = self.make(_config(cloud_enabled=False), ollama=FakeLocalClient(available=False))
        text, tier = d._generate("p")
        self.assertEqual((text, tier), ("", "mechanical"))

    def test_breaker_opens_after_two_failures(self):
        breaker = DistillerBreaker(threshold=2, cooldown_sec=9999)
        self.assertTrue(breaker.allow("cloud"))
        breaker.record("cloud", False)
        self.assertTrue(breaker.allow("cloud"))
        breaker.record("cloud", False)
        self.assertFalse(breaker.allow("cloud"))
        breaker.record("cloud", True)  # success resets
        self.assertTrue(breaker.allow("cloud"))

    def test_generate_failures_trip_breaker(self):
        d = self.make(_config(cloud_enabled=True, api_key="k"))
        d._cloud, d._cloud_checked = FakeBridge(auth="ok", response=None), True
        d._generate("p")
        d._generate("p")
        self.assertFalse(d.breaker.allow("cloud"))


class TestStorage(DistillerTestBase):
    def test_facts_stored_with_provenance(self):
        d = self.make()
        stored = d._store([{"fact": "the MCP handshake must finish before any ML init runs",
                            "category": "gotcha", "confidence": 0.9}], "sid-1", "cloud")
        self.assertEqual(stored, 1)
        fact = self.memory.facts[-1]
        self.assertEqual(fact["source_quality"], "distilled")
        self.assertEqual(fact["category"], "gotcha")
        self.assertAlmostEqual(fact["confidence"], 0.9)

    def test_local_tier_gets_weighted_confidence_and_quality(self):
        d = self.make()
        d._store([{"fact": "background agents must never crash their polling loop on errors",
                   "category": "convention", "confidence": 1.0}], "sid-1", "local")
        fact = self.memory.facts[-1]
        self.assertEqual(fact["source_quality"], "distilled_local")
        self.assertAlmostEqual(fact["confidence"], 0.9)

    def test_near_duplicate_is_skipped(self):
        d = self.make()
        text = "the enforcement hook denies native tools until a c3 call happens first"
        self.memory.remember(text, "gotcha", "old-sess")
        stored = d._store([{"fact": text, "category": "gotcha", "confidence": 0.9}],
                          "sid-1", "cloud")
        self.assertEqual(stored, 0)

    def test_user_fact_never_rewritten_by_merge(self):
        d = self.make()
        original = "always use the cmd.exe prefix for hook commands because project paths break Git Bash"
        self.memory.remember(original, "convention", "old-sess")  # source_quality=user
        similar = "always use the cmd.exe prefix for hook commands since paths with parens break Git Bash badly"
        d._store([{"fact": similar, "category": "convention", "confidence": 0.9}], "sid-1", "cloud")
        user_fact = next(f for f in self.memory.facts if f["source_quality"] == "user")
        self.assertEqual(user_fact["fact"], original)

    def test_private_content_stripped_before_store(self):
        d = self.make()
        stored = d._store([{"fact": "deploy needs the staging flag <private>token=hunter2</private> set before restart",
                            "category": "gotcha", "confidence": 0.9}], "sid-1", "cloud")
        self.assertEqual(stored, 1)
        self.assertNotIn("hunter2", self.memory.facts[-1]["fact"])


class FakeConvoStore:
    def __init__(self, sessions):
        # sessions: {sid: [turn, ...]}
        self.sessions = sessions

    def list_sessions(self, limit=100):
        return [{"session_id": sid, "started": 1.0, "ended": 2.0}
                for sid in self.sessions][:limit]

    def get_session(self, sid, offset=0, limit=None):
        return list(self.sessions.get(sid, []))


def _turn(role, text, ts=1.5):
    return {"role": role, "text": text, "ts": ts}


class TestTranscriptMining(DistillerTestBase):
    def make_mining(self, convo, response=None, config=None):
        local = FakeLocalClient(response=response)
        d = self.make(config or _config(cloud_enabled=False), ollama=local)
        d.convo = convo
        return d

    def test_mines_user_signals_and_advances_hwm(self):
        convo = FakeConvoStore({"conv-1": [
            _turn("user", "no — always use the cmd.exe prefix for hooks on Windows"),
            _turn("assistant", "Understood, switching to cmd.exe /c."),
        ]})
        response = json.dumps({"facts": [{
            "fact": "always use the cmd.exe prefix for hook commands on Windows",
            "category": "preference", "confidence": 0.9}]})
        d = self.make_mining(convo, response=response)
        result = d.mine_transcripts()
        self.assertEqual(result, {"mined": 1, "stored": 1})
        fact = self.memory.facts[-1]
        self.assertEqual(fact["category"], "preference")
        # confidence capped at 0.8 for mined facts (0.9 * 0.9 local weight → 0.8 cap)
        self.assertLessEqual(fact["confidence"], 0.8)
        # second run: nothing new to mine
        self.assertEqual(d.mine_transcripts(), {"mined": 0, "stored": 0})

    def test_hwm_does_not_advance_when_llm_down(self):
        convo = FakeConvoStore({"conv-1": [_turn("user", "never push directly to main")]})
        d = self.make_mining(convo, response=None)  # local returns None
        d.ollama.available = False
        self.assertEqual(d.mine_transcripts(), {"mined": 0, "stored": 0})
        self.assertEqual(d._load_hwm(), {})  # untouched — retried next cycle

    def test_hwm_reset_when_store_rewritten_shorter(self):
        convo = FakeConvoStore({"conv-1": [_turn("user", "always squash-merge feature branches")]})
        response = json.dumps({"facts": [{
            "fact": "the user requires squash-merging for all feature branches",
            "category": "preference", "confidence": 0.9}]})
        d = self.make_mining(convo, response=response)
        d._save_hwm({"conv-1": {"last_turn_index": 99, "mined_at": "x"}})
        result = d.mine_transcripts()
        self.assertEqual(result["mined"], 1)
        self.assertEqual(d._load_hwm()["conv-1"]["last_turn_index"], 1)

    def test_assistant_only_sessions_are_skipped(self):
        convo = FakeConvoStore({"conv-1": [_turn("assistant", "I propose using Redis here.")]})
        d = self.make_mining(convo, response="should never be called")
        self.assertEqual(d.mine_transcripts(), {"mined": 0, "stored": 0})
        self.assertEqual(d.ollama.generate_calls, 0)

    def test_mining_disabled_by_config(self):
        convo = FakeConvoStore({"conv-1": [_turn("user", "always do the thing")]})
        d = self.make_mining(convo, response="x",
                             config=_config(cloud_enabled=False,
                                            transcript_mining_enabled=False))
        self.assertEqual(d.mine_transcripts(), {"mined": 0, "stored": 0})

    def test_mining_prompt_strips_private_and_categories_restricted(self):
        convo = FakeConvoStore({"conv-1": [
            _turn("user", "use the key <private>sk-secret-123</private> and always retry twice"),
        ]})
        response = json.dumps({"facts": [{
            "fact": "network calls in this project must always be retried twice on failure",
            "category": "gotcha", "confidence": 0.9}]})  # gotcha not allowed when mining
        d = self.make_mining(convo, response=response)
        prompt = d._mining_prompt(convo.get_session("conv-1"))
        self.assertNotIn("sk-secret-123", prompt)
        d.mine_transcripts()
        fact = self.memory.facts[-1]
        self.assertIn(fact["category"], MINING_CATEGORIES)


class TestJobLifecycle(DistillerTestBase):
    def test_process_job_success_marks_done(self):
        local = FakeLocalClient(response=_good_json(
            "session snapshots must run before /clear so context survives compaction"))
        d = self.make(_config(cloud_enabled=False), ollama=local)
        job = d.queue.enqueue("session_digest", "sid-1", {"decisions": ["d"]})
        result = d.process_job(job)
        self.assertEqual(result["tier"], "local")
        self.assertEqual(result["stored"], 1)
        self.assertEqual(d.queue.claim_pending(), [])

    def test_llm_unavailable_leaves_job_pending_until_max_attempts(self):
        d = self.make(_config(cloud_enabled=False, queue_max_attempts=3),
                      ollama=FakeLocalClient(available=False))
        job = d.queue.enqueue("session_digest", "sid-1", {})
        d.process_job(job)
        pending = d.queue.claim_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        d.process_job(pending[0])
        pending = d.queue.claim_pending()
        self.assertEqual(pending[0]["attempts"], 2)
        d.process_job(pending[0])   # third attempt → degrade
        self.assertEqual(d.queue.claim_pending(), [])

    def test_process_job_safe_swallows_exceptions(self):
        d = self.make()
        d._gather = lambda job: (_ for _ in ()).throw(RuntimeError("boom"))
        job = d.queue.enqueue("session_digest", "sid-1", {})
        self.assertIsNone(d.process_job_safe(job))
        # job re-queued as a failed attempt, not lost
        pending = d.queue.claim_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)

    def test_enqueue_session_requires_id_and_enabled(self):
        d = self.make(_config(enabled=False))
        self.assertIsNone(d.enqueue_session({"id": "sid-1"}))
        d = self.make()
        self.assertIsNone(d.enqueue_session(None))
        self.assertIsNone(d.enqueue_session({"id": ""}))
        job = d.enqueue_session({"id": "sid-1", "started": "2026-07-03T12:00:00+00:00",
                                 "decisions": [], "files_touched": []})
        self.assertEqual(job["status"], "pending")


if __name__ == "__main__":
    unittest.main()
