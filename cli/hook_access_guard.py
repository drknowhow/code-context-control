"""PreToolUse access-guard sub-hook — yielded FIRST from the pretool route.

Evaluates native tool targets against services.access_guard BEFORE the
c3-usage/sticky-unlock logic in hook_pretool_enforce ever runs; the
dispatcher's deny-beats-allow merge makes a deny here final. hook_dispatch
treats this module as _FAIL_CLOSED: if it fails to import or raises, write-
class native tools are denied instead of sailing through.

Bash coverage is a best-effort token scan — advisory by design
(docs/access-guard.md §3/§6); it catches an agent naming a denied path, not
an adversary hiding one. Two passes: every path-shaped token as a READ
(existence-gated), then the command's WRITE targets as writes (v2.102.0), so
a redirect into a confirm-held or read-only file holds like any other write.
"""
import os
import re
import sys
from pathlib import Path

_CLI_DIR = Path(__file__).resolve().parent
for _p in (str(_CLI_DIR.parent), str(_CLI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _hook_utils import normalize_tool_name  # noqa: E402

from services import access_guard as ag  # noqa: E402

try:
    from services import access_telemetry
except Exception:  # pragma: no cover — telemetry is never load-bearing
    access_telemetry = None

# The same write-target extractor the edit ledger uses after the fact. Its
# absence degrades the shell branch to the read-only scan, never to a crash.
try:
    from _shell_writes import shell_write_targets
except Exception:  # pragma: no cover — package-style import (dispatcher, tests)
    try:
        from cli._shell_writes import shell_write_targets
    except Exception:
        shell_write_targets = None

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
#
# UNANCHORED, and that is the fix rather than an oversight. `^…://` only ever
# recognised a token that BEGINS with a scheme, which is the same
# whitelist-a-shape mistake #50 made and this module's own header calls out —
# and the `_SYNTAX_CHARS` generalisation does not cover the gap, because a
# token can carry a URL and no syntax at all. Observed 2026-08-11: the single
# argument of
#     printf '\n---\n\nhttps://claude.ai/code/session_01Hs28…\n' >> body.md
# strips to `\n---\n\nhttps://claude.ai/…`, which has a separator (`\` and
# `/`), no syntax characters, no `::`, and no leading scheme. It reached
# ag.check, canonicalised to a residual colon, and hard-denied a command whose
# only file argument was an ordinary markdown path. Third logged instance of
# this class in five days (two on 2026-08-07 were Windows paths inside Python
# string literals).
#
# A token containing `://` anywhere is a network literal, not an NTFS stream:
# a real stream is `file:name:$TYPE`, and no spelling of it contains `//`.
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
# IPv6 literal or CIDR: >=2 colons, hex groups only, optional [] and /prefix.
# 'C:/x/f.txt:stream' has one colon and non-hex parts, so it never matches.
_IPV6_RE = re.compile(
    r"^\[?[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,}\]?(?:/\d{1,3})?$",
    re.IGNORECASE,
)

# Characters that make a token a compound expression rather than a path
# argument: what Windows forbids in a filename outright (< > " | ? *) plus the
# bracketing and quoting that marks structure ({} [] () ' ` &).
#
# NOT in this set, deliberately: ':' is the thing under test; '$' and '=' are
# legal in a real path AND in a real ADS spelling — './notes.txt:$DATA' is the
# canonical default-stream syntax, and dropping it here silently deleted an
# existing negative control the first time this was written. Every genuinely
# dangerous shell use of '$' arrives with '(' or '{' anyway, which are covered.
#
# This is the general form of the fix #50 attempted with _is_network_token, and
# the reason that one was not enough: it whitelisted two *shapes* — a token
# STARTING with a scheme, or a bare IPv6 — so a URL that was not the whole token
# still tripped, and syntax carrying no URL at all was never covered:
#
#     echo "see [#1](https://example.com/a/b)"   -> token '[#1](https://…' , scheme not at position 0
#     echo '{a:.x,b:("/"+.y)}'                   -> a jq filter, no URL anywhere
#     python -c "… ['src/a.py','src/b.py'] …"    -> a list literal
#
# All three contain '/', so the token gate admitted them; all three canonicalize
# to something with a residual colon, and <ads> is exempt from existence-gating,
# so each became a hard deny on text that names no file at all.
_SYNTAX_CHARS = frozenset("{}[]()<>\"'`|&*?")

# `pkg/mod.py::TestThing`, `ns::Type` — a pytest node id or a C++/Rust scope.
# NTFS spells a stream `file:name:$TYPE`, and the doubled-colon default-stream
# form `file::$DATA` is real, so "contains ::" is NOT safe on its own. What is
# safe is the type slot: stream types are `$`-prefixed system constants, so a
# `::` NOT followed by `$` cannot be a stream spelling.
_DOUBLE_COLON_RE = re.compile(r"::(?!\$)")

# `<rev>:<path>` — how git names a blob: `git show HEAD:setup.py`,
# `git cat-file -p origin/main:src/a.py`.
_GIT_REVSPEC_RE = re.compile(r"^(?P<rev>[^:\s]+):(?P<path>[^:\s]+)$")
_GIT_CMD_RE = re.compile(r"^\s*(?:[\w.\-]*[/\\])?git(?:\.exe)?\b", re.IGNORECASE)

# Where one command ends and the next begins. Anchoring "is this git?" to the
# start of the whole string was wrong in the most ordinary way possible:
# `cd /repo && git show origin/main:pyproject.toml` starts with `cd`, so the
# revspec rewrite never fired for the shape people actually type. Caught by a
# live probe after release, not by the unit tests — which passed the command as
# `git show …` because that is how the test author writes it, not how a shell
# call arrives.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")

# `cd <dir>` — the segment that moves the ground under every later one. Handles
# a quoted target, since project paths on this platform contain spaces, and the
# Windows `/d` flag.
_CD_RE = re.compile(
    r"""^\s*cd\s+(?:/d\s+)?(?P<dir>"[^"]+"|'[^']+'|\S+)\s*$""",
    re.IGNORECASE,
)


def _is_network_token(tok: str) -> bool:
    """True for URLs and IPv6 literals/CIDRs — never for an ADS spelling.

    ``search`` for the scheme (a URL anywhere in the token disqualifies it as
    a path), ``match`` for IPv6 — that pattern is a whole-token shape and
    anchoring is what keeps `C:/x/f.txt:stream` from matching it.
    """
    return bool(_SCHEME_RE.search(tok) or _IPV6_RE.match(tok))


def _is_scope_token(tok: str) -> bool:
    """True for `a/b.py::Thing` and `ns::Type` — never an ADS spelling."""
    return bool(_DOUBLE_COLON_RE.search(tok))


def _git_revspec_path(cmd: str, tok: str):
    """The path half of a git ``<rev>:<path>`` token, or None.

    Returned so the caller can check **the path** rather than the whole token,
    which is the fix and a tightening at once. Today every git revspec is denied
    by accident: ``<rev>:<path>`` canonicalizes to something with a residual
    colon and trips ``<ads>``, so ``git show origin/main:pyproject.toml`` — an
    everyday command naming nothing sensitive — is refused.

    Whitelisting the shape outright would go too far the other way, because
    ``git show HEAD:.env`` really does print a denied file's contents, and right
    now the only thing stopping it is the false positive. Checking the path half
    gets both right: `.env` is denied on its own rule, `pyproject.toml` is
    allowed on its own merits, and neither answer depends on a rule misfiring.

    Gated on the command actually being ``git``, since ``<word>:<word>`` means
    something else almost everywhere else.
    """
    if not _GIT_CMD_RE.match(cmd or ""):
        return None
    if len(tok) > 1 and tok[1] == ":":
        return None  # a drive letter is not a revspec separator
    m = _GIT_REVSPEC_RE.match(tok)
    if not m:
        return None
    path = m.group("path")
    # A `$`-prefixed suffix is a stream type, not a path. Do not reinterpret it.
    return None if path.startswith("$") else path


def _looks_like_a_path(tok: str) -> bool:
    """False for tokens that are shell syntax rather than a path argument.

    Deliberately asymmetric, because the two errors are not equal. Skipping a
    token that WAS a path costs a missed advisory flag in a best-effort scanner
    — the real gates (the Read/Write/Edit path check above, and every c3 tool)
    still refuse it. Flagging a token that was NOT a path costs a hard,
    unappealable deny on a command that touches nothing, which is what #50 was.
    So this errs toward skipping.
    """
    return not (_SYNTAX_CHARS & set(tok))


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _record(denial, tool: str, operation: str, path: str,
            base: str, session_id: str = "") -> None:
    """Log a path-policy denial for `c3 access stats`. Best-effort."""
    if access_telemetry is None or denial is None:
        return
    try:
        access_telemetry.record(
            layer=access_telemetry.LAYER_ACCESS,
            rule=getattr(denial, "rule", ""),
            scope=getattr(denial, "scope", ""),
            tool=tool, operation=operation, path=path,
            session_id=session_id, project_path=base,
        )
    except Exception:
        pass


def _override_allows(base: str, denial, *, tool: str, op: str, path: str,
                     session_id: str, peek: bool = False):
    """A live override grant covering this exact call, or None (spec §5).

    Lazy import and policy-before-grants ordering keep the hot path free: a
    project that never turned overrides on pays one dict lookup and never
    opens the grants file. Any failure returns None — the denial stands.

    ``peek`` looks without consuming; see ``_granted`` for why.
    """
    try:
        from services import override_grants as og  # noqa: PLC0415 — lazy
        return og.gate_access(base, denial, tool=tool, op=op, path=path,
                              session_id=session_id, peek=peek)
    except Exception:
        return None  # fail closed: no grant, ordinary denial


def _granted(line: str, defer: bool, consume) -> dict:
    """The allow-by-grant output.

    Under the dispatcher (``defer`` True) the use is NOT burned here: the
    provisional line goes out with an ``_on_allow`` callable, and
    hook_dispatch runs it only once every PreToolUse sub-hook has voted
    allow. Before v2.102.0 this hook consumed first and a strict-mode
    discipline deny from hook_pretool_enforce then won the merge — grant
    spent, nothing written, and the user asked twice for one edit. Run
    standalone (``defer`` False) the use was already consumed by the peek-
    less lookup, so the line is final.
    """
    out = {"additionalContext": line}
    if defer:
        out["_on_allow"] = consume
    return out


def _confirm_request(base: str, denial, *, tool: str, op: str, path: str,
                     session_id: str) -> tuple:
    """Auto-file an Override Request for a ``confirm`` denial (S8).

    ``(request_id, note)`` — one of them is always ''. Lazy import, never
    raises: a broken request store degrades to S8's "could not be filed"
    tail, and the hold stands either way (docs/confirm-guard.md §3).
    """
    try:
        from services import override_requests as orq  # noqa: PLC0415 — lazy
        row, reason = orq.auto_file(base, denial=denial, tool=tool, op=op,
                                    path=path, session_id=session_id)
        if row:
            return str(row.get("id") or ""), ""
        return "", reason
    except Exception as exc:
        return "", f"request store unavailable ({exc})"


def _target(tool_input: dict) -> str:
    return str(
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
        or ""
    )


def _cd_target(segment: str, cwd: str) -> str | None:
    """The directory a `cd` segment moves to, resolved against ``cwd``."""
    m = _CD_RE.match(segment or "")
    if not m:
        return None
    target = m.group("dir").strip("\"'")
    if not target or target.startswith("-"):
        return None
    try:
        return os.path.abspath(os.path.join(cwd, os.path.expanduser(target)))
    except (OSError, ValueError):
        return None


def _scan_shell(cmd: str, base: str):
    """(denial, token) for the first confident deny-rule hit, else (None, '').

    Confident = the token resolves to an EXISTING path that a ``deny`` rule
    covers (existence-gating keeps regex/pattern arguments like '\\.env'
    from false-denying), or the evaluator flags the spelling itself.

    Tokens that are shell syntax rather than path arguments are skipped
    outright, as are URLs and IPv6 literals: all of them trip the ADS spelling
    check, which is exempt from existence-gating, so a token naming nothing on
    disk would otherwise hard-deny (#50).

    Scanning is per **command segment**, not per whole string. A segment is what
    sits between `&&`, `||`, `;`, `|` or a newline, and each one answers "am I a
    git command?" for itself — so the revspec rewrite applies to the tokens of
    the git segment and to no others. `cat notes.txt:hidden && git status` must
    not have its first token reinterpreted just because a later segment is git.
    """
    budget = _MAX_TOKENS
    cwd = base
    for segment in _SEGMENT_SPLIT.split(cmd or ""):
        if budget <= 0:
            break
        is_git = bool(_GIT_CMD_RE.match(segment))
        for raw in _TOKEN_SPLIT.split(segment)[:budget]:
            budget -= 1
            tok = raw.strip("\"'`;,()")
            if not tok or tok.startswith("-"):
                continue
            # A revspec's path half need not contain a separator — `HEAD:.env`
            # has none, so it used to be dropped here before any rule saw it.
            if ("/" not in tok and "\\" not in tok and not tok.startswith(".")
                    and not (is_git and _GIT_REVSPEC_RE.match(tok))):
                continue
            # Order matters only for readability: a URL is also syntax-free, so
            # either check alone would skip 'https://x'. Both are kept because
            # they answer different questions — "is this a network literal" and
            # "is this a path at all" — and the second is the one that
            # generalizes.
            if not _looks_like_a_path(tok) or _is_network_token(tok):
                continue
            # `tests/x.py::TestThing` is a node id, not a stream spelling.
            if _is_scope_token(tok):
                continue
            # `git show <rev>:<path>` — judge the PATH, not the whole token.
            # This is the one place the scan rewrites what it checks rather than
            # skipping it, because skipping would let `git show HEAD:.env`
            # through.
            revspec_path = _git_revspec_path(segment, tok) if is_git else None
            from_revspec = revspec_path is not None
            if from_revspec:
                tok = revspec_path
            if re.match(r"^/[a-z]/", tok):  # MSYS /c/foo → C:/foo
                tok = f"{tok[1]}:{tok[2:]}"
            # Resolve against the cwd the command will actually run in, not the
            # session root. `cd /elsewhere && cat .env` used to compute the
            # denial correctly and then throw it away, because the existence
            # gate looked for `.env` under the project root where it does not
            # live (#82). The rule base stays `base` — policy is the project's;
            # only the path being judged follows the shell.
            probe = tok
            if not os.path.isabs(probe):
                try:
                    probe = os.path.join(cwd, probe)
                except (OSError, ValueError):
                    probe = tok
            denial = ag.check(probe, "read", base)
            if denial and denial.kind == "deny":
                try:
                    exists = Path(probe).exists()
                except OSError:
                    exists = False
                # A revspec names a path in HISTORY, so the working tree is the
                # wrong place to ask whether it is real. `git show HEAD~5:.env`
                # reads a denied file whether or not that file exists today, and
                # existence-gating it would be a hole this rewrite opened.
                if exists or from_revspec or denial.rule.startswith("<"):
                    return denial, tok
        # AFTER the segment's own tokens: `cd x` moves the ground for what
        # follows, not for its own argument.
        moved = _cd_target(segment, cwd)
        if moved is not None:
            cwd = moved
    return None, ""


def _scan_shell_writes(cmd: str, base: str, session_id: str,
                       defer: bool) -> dict | None:
    """WRITE-class verdict for the files a shell command writes (v2.102.0).

    ``_scan_shell`` evaluates every token as a READ. Confirm holds, read_only
    rules and the builtin write-denies are all write-class, so ``cat >>
    CLAUDE.md``, ``sed -i`` on .mcp.json or a heredoc into a hook body sailed
    through with no hold — the one bypass docs/confirm-guard.md could only
    ask the agent not to use. Targets come from the extractor the edit
    ledger already trusts after the fact (cli/_shell_writes), with the same
    best-effort caveat: a missed target costs a missed hold, a false one
    costs a hold the human clears in one tap. Synthetic spelling denials are
    skipped exactly as the read scan skips them (a token that is not a path
    must not veto the command); the corrupt-config deny-all still refuses.

    Grant identity is ``tool="Bash", op="write", path=<target>`` — bound to
    the file, not the command text, the same stated limitation as the
    shell_warn layer. Several held targets need several grants; the command
    runs only when every one is covered, and the uses are burned together
    (or not at all) by the dispatcher's settle step.
    """
    if shell_write_targets is None:
        return None
    lines, consumers = [], []
    for target in shell_write_targets(cmd, base):
        denial = ag.check(target, "write", base)
        if denial is None:
            continue
        if denial.rule.startswith("<") and denial.rule != "<corrupt-config>":
            continue
        granted = _override_allows(base, denial, tool="Bash", op="write",
                                   path=target, session_id=session_id,
                                   peek=defer)
        if granted:
            lines.append(granted)
            if defer:
                consumers.append(lambda t=target, d=denial: _override_allows(
                    base, d, tool="Bash", op="write", path=t,
                    session_id=session_id))
            continue
        _record(denial, "Bash", "write", target, base, session_id)
        if denial.kind == "confirm":
            rid, note = _confirm_request(base, denial, tool="Bash", op="write",
                                         path=target, session_id=session_id)
            return _deny(ag.refusal(denial, target, "write", surface="hook",
                                    tool="Bash", request_id=rid,
                                    request_note=note)
                         + " (best-effort shell scan)")
        return _deny(ag.refusal(denial, target, "write", surface="hook",
                                tool="Bash") + " (best-effort shell scan)")
    if not lines:
        return None

    def _settle():
        got = [fn() for fn in consumers]
        return "\n".join(got) if all(got) else None

    return _granted("\n".join(lines), bool(consumers), _settle)


def run(payload: dict, project_path: Path | None = None,
        defer_consume: bool = False) -> dict | None:
    tool = normalize_tool_name(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {}) or {}
    base = str(project_path if project_path is not None else Path.cwd())
    session_id = str(payload.get("session_id") or "")

    if tool in _WRITE_TOOLS or tool in _READ_TOOLS:
        fp = _target(tool_input)
        if not fp:
            return None
        op = "write" if tool in _WRITE_TOOLS else "read"
        denial = ag.check(fp, op, base)
        if denial:
            granted = _override_allows(base, denial, tool=tool, op=op,
                                       path=fp, session_id=session_id,
                                       peek=defer_consume)
            if granted:
                return _granted(granted, defer_consume,
                                lambda: _override_allows(
                                    base, denial, tool=tool, op=op, path=fp,
                                    session_id=session_id))
            _record(denial, tool, op, fp, base, session_id)
            if denial.kind == "confirm":
                # A confirm hold files its own request (grants were consulted
                # above, so an approved retry lands in the granted branch).
                rid, note = _confirm_request(base, denial, tool=tool, op=op,
                                             path=fp, session_id=session_id)
                return _deny(ag.refusal(denial, fp, op, surface="hook",
                                        tool=tool, request_id=rid,
                                        request_note=note))
            return _deny(ag.refusal(denial, fp, op, surface="hook", tool=tool))
        return None

    if tool in _SEARCH_TOOLS:
        # Hard-deny only an EXPLICIT path argument inside a denied subtree;
        # rootless/broad searches stay advisory (docs/access-guard.md §3).
        root = str(tool_input.get("path") or "")
        if root:
            denial = ag.check(root, "read", base)
            if denial and denial.kind == "deny":
                _record(denial, tool, "read", root, base, session_id)
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
            _record(denial, tool, "read", tok, base, session_id)
            return _deny(ag.refusal(denial, tok, "read",
                                    surface="hook", tool=tool)
                         + " (best-effort shell scan)")
        return _scan_shell_writes(cmd, base, session_id, defer_consume)

    return None
