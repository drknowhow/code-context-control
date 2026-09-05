"""Per-process host identity, separate from a project's preferred editor.

Installers pass --host explicitly. Environment discovery is a compatibility
fallback for older launch configurations; hook payloads use their own identity.
"""
import os
from dataclasses import dataclass

from core.ide import get_profile, load_ide_config, normalize_ide_name


@dataclass(frozen=True)
class HostContext:
    provider: str
    host_session_id: str = ""

    @property
    def capabilities(self):
        return get_profile(self.provider)


def resolve_host(project_path: str = "", explicit_host: str | None = None,
                 environ=None) -> HostContext:
    env = os.environ if environ is None else environ
    provider = explicit_host or env.get("C3_HOST")
    if not provider:
        # A Claude child can inherit CODEX_THREAD_ID from its parent.
        if env.get("CLAUDE_CODE_SESSION_ID"):
            provider = "claude-code"
        elif env.get("CODEX_THREAD_ID"):
            provider = "codex"
        else:
            provider = load_ide_config(project_path)
    provider = normalize_ide_name(provider)
    session_env = {"claude-code": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID"}
    session = str(env.get(session_env.get(provider, ""), "") or "").strip()
    return HostContext(provider, session)
