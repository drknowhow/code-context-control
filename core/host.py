"""Per-process host identity, separate from a project's preferred editor.

Installers pass --host explicitly. Environment discovery is a compatibility
fallback for older launch configurations; hook payloads use their own identity.
"""
import os
from dataclasses import dataclass

from core.ide import get_profile, load_ide_config, normalize_ide_name

# The environment variable each host puts its session id in. The MCP server
# reads it once at start; after a /clear it is REWRITTEN in-process by
# cli/mcp_server._rebind_host_session so that runtimes built later in the same
# process (the c3_project proxy) inherit the new id — see services.host_handoff.
SESSION_ENV = {"claude-code": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID",
               "grok": "GROK_SESSION_ID"}


def session_env_var(provider: str | None) -> str:
    """The env var carrying ``provider``'s session id, or ``""``."""
    return SESSION_ENV.get(normalize_ide_name(provider or ""), "")


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
    # Grok is never inferred from GROK_SESSION_ID: a grok launched from a Claude
    # Code terminal also inherits CLAUDE_CODE_SESSION_ID, so its MCP entry and
    # hooks name the host explicitly (--host grok) instead.
    session = str(env.get(SESSION_ENV.get(provider, ""), "") or "").strip()
    return HostContext(provider, session)
