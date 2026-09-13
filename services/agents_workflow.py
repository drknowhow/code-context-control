"""The C3 block written into AGENTS.md.

AGENTS.md is read by every hooks-capable AGENTS.md host C3 installs (Codex and
Grok Build), and by Antigravity. The block is the same in every project, so a
regeneration never depends on which hosts happen to be installed; each host's
notes sit under their own heading.
"""
from services.codex_integration import CODEX_WORKFLOW
from services.grok_integration import GROK_WORKFLOW_NOTE

AGENTS_MD_WORKFLOW = CODEX_WORKFLOW.rstrip() + "\n\n" + GROK_WORKFLOW_NOTE
