"""PreToolUse access-guard sub-hook — yielded FIRST from the pretool route.

Evaluates native tool targets against services.access_guard BEFORE the
c3-usage/sticky-unlock logic in hook_pretool_enforce ever runs; the
dispatcher's deny-beats-allow merge makes a deny here final. hook_dispatch
treats this module as _FAIL_CLOSED: if it fails to import or raises, write-
class native tools are denied instead of sailing through.

Bash coverage is a best-effort, existence-gated token scan — advisory by
design (docs/access-guard.md §3/§6); it catches an agent naming a denied
path, not an adversary hiding one.
"""
import re
import sys
from pathlib import Path

_CLI_DIR = Path(__file__).resolve().parent
for _p in (str(_CLI_DIR.parent), str(_CLI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _hook_utils import normalize_tool_name  # noqa: E402

from services import access_guard as ag  # noqa: E402

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_READ_TOOLS = {"Read", "SearchText"}
_SEARCH_TOOLS = {"Grep", "Glob", "FindFiles"}
_SHELL_TOOLS = {"Bash"}  # normalize_tool_name folds run_shell_command in

_TOKEN_SPLIT = re.compile(r"\s+")
_MAX_TOKENS = 200

# A colon in a shell token overwhelmingly means "URL scheme" or "IPv6", not
# "NTFS alternate data stream". The ADS check stays strict for real path
# arguments (_WRITE_TOOLS/_READ_TOOLS); here it only produced false denies on
# commit messages, curl, and gh (#50).
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
# IPv6 literal or CIDR: >=2 colons, hex groups only, optional [] and /prefix.
# 'C:/x/f.txt:stream' has one colon and non-hex parts, so it never matches.
_IPV6_RE = re.compile(
    r"^\[?[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,}\]?(?:/\d{1,3})?$",
    re.IGNORECASE,
)


def _is_network_token(tok: str) -> bool:
    """True for URLs and IPv6 literals/CIDRs — never for an ADS spelling."""
    return bool(_SCHEME_RE.match(tok) or _IPV6_RE.match(tok))


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _target(tool_input: dict) -> str:
    return str(
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
        or ""
    )


def _scan_shell(cmd: str, base: str):
    """(denial, token) for the first confident deny-rule hit, else (None, '').

    Confident = the token resolves to an EXISTING path that a ``deny`` rule
    covers (existence-gating keeps regex/pattern arguments like '\\.env'
    from false-denying), or the evaluator flags the spelling itself.

    URLs and IPv6 literals are skipped outright: they trip the ADS spelling
    check, which is exempt from existence-gating, so a token naming nothing
    on disk would otherwise hard-deny (#50).
    """
    for raw in _TOKEN_SPLIT.split(cmd)[:_MAX_TOKENS]:
        tok = raw.strip("\"'`;,()")
        if not tok or tok.startswith("-"):
            continue
        if "/" not in tok and "\\" not in tok and not tok.startswith("."):
            continue
        if _is_network_token(tok):
            continue
        if re.match(r"^/[a-z]/", tok):  # MSYS /c/foo → C:/foo
            tok = f"{tok[1]}:{tok[2:]}"
        denial = ag.check(tok, "read", base)
        if denial and denial.kind == "deny":
            try:
                p = Path(tok)
                exists = (p if p.is_absolute() else Path(base) / p).exists()
            except OSError:
                exists = False
            if exists or denial.rule.startswith("<"):
                return denial, tok
    return None, ""


def run(payload: dict, project_path: Path | None = None) -> dict | None:
    tool = normalize_tool_name(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {}) or {}
    base = str(project_path if project_path is not None else Path.cwd())

    if tool in _WRITE_TOOLS or tool in _READ_TOOLS:
        fp = _target(tool_input)
        if not fp:
            return None
        op = "write" if tool in _WRITE_TOOLS else "read"
        denial = ag.check(fp, op, base)
        if denial:
            return _deny(ag.refusal(denial, fp, op, surface="hook", tool=tool))
        return None

    if tool in _SEARCH_TOOLS:
        # Hard-deny only an EXPLICIT path argument inside a denied subtree;
        # rootless/broad searches stay advisory (docs/access-guard.md §3).
        root = str(tool_input.get("path") or "")
        if root:
            denial = ag.check(root, "read", base)
            if denial and denial.kind == "deny":
                return _deny(ag.refusal(denial, root, "read",
                                        surface="hook", tool=tool))
        footer = ag.search_footer(base)
        return {"additionalContext": footer} if footer else None

    if tool in _SHELL_TOOLS:
        cmd = str(tool_input.get("command") or "")
        if not cmd:
            return None
        denial, tok = _scan_shell(cmd, base)
        if denial:
            return _deny(ag.refusal(denial, tok, "read",
                                    surface="hook", tool=tool)
                         + " (best-effort shell scan)")
        return None

    return None
