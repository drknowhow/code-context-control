"""Guard: no hardcoded Codex/OpenAI model pins in production code.

A pinned model name goes stale and then hard-fails accounts that don't
support it (ChatGPT-plan Codex logins reject retired models with a 400).
Discovered live 2026-07-26: a generated .codex/config.toml pinning
"o4-mini" and CODEX_MODELS pinning "gpt-5.3-codex-spark" both broke every
Codex delegation on this box. Model resolution must fall through:
.c3 config codex_default_model -> user's Codex CLI default (no -m flag).

Same class of guard as tests/test_version_sync.py: when a value can drift
out from under us, CI asserts it can't come back.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Production files that build Codex invocations.
PRODUCTION_FILES = [
    "cli/tools/delegate.py",
    "cli/tools/agent.py",
    "services/agents.py",
]

# Any OpenAI-style model literal: gpt-4o, gpt-5.x[-suffix], o3/o4-mini, etc.
MODEL_LITERAL = re.compile(
    r"[\"'](?:gpt-\d[\w.\-]*|o\d[\w.\-]*-mini[\w.\-]*)[\"']"
)


class TestNoStaleModelPins(unittest.TestCase):
    def test_no_hardcoded_codex_models_in_production(self):
        offenders = []
        for rel in PRODUCTION_FILES:
            text = (REPO / rel).read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if MODEL_LITERAL.search(line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "Hardcoded model pins found (resolution must fall through to "
            "config or the Codex CLI default):\n" + "\n".join(offenders),
        )

    def test_codex_models_table_has_no_model_keys(self):
        from cli.tools.delegate import CODEX_MODELS
        pinned = {k: v["model"] for k, v in CODEX_MODELS.items() if "model" in v}
        self.assertEqual(pinned, {}, "CODEX_MODELS must not pin model names")

    def test_codex_cmd_omits_m_flag_when_model_empty(self):
        from cli.tools.delegate import _codex_cmd
        cmd = _codex_cmd("prompt", "", "read-only", "high")
        self.assertNotIn("-m", cmd)
        self.assertIn("--sandbox", cmd)

    def test_codex_cmd_passes_explicit_model(self):
        from cli.tools.delegate import _codex_cmd
        cmd = _codex_cmd("prompt", "my-model", "read-only", "high")
        self.assertEqual(cmd[cmd.index("-m") + 1], "my-model")
