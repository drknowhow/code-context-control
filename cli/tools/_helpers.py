"""Shared helpers for tool handlers."""

from pathlib import Path


def maybe_related_facts(svc, topic: str, top_k: int = 3, width: int = 100) -> str:
    """Append related facts if enabled. Currently disabled — adds noise."""
    return ""


# ── Response boilerplate diet (P6) ───────────────────────────────────────────

def show_token_ratios(svc) -> bool:
    """Debug flag: restore per-call "raw->optimized tok" ratio headers.

    Off by default — the ratio header was ~100-200 tokens/session of
    boilerplate the model does nothing with. Accounting no longer depends on
    the displayed header: migrated tools report (raw_tokens, optimized_tokens)
    structurally via finalize_with_tokens(). Set
    ``{"hybrid": {"show_token_ratios": true}}`` in .c3/config.json to see the
    old headers again (same convention as SHOW_SAVINGS_SUMMARY /
    show_savings_footer).
    """
    try:
        return bool((getattr(svc, "hybrid_config", None) or {}).get(
            "show_token_ratios", False))
    except Exception:
        return False


# ── Structured token accounting (honest measurement layer) ──────────────────

def finalize_with_tokens(finalize, svc, tool_name: str, args: dict,
                         response: str, summary: str = "", *,
                         raw_tokens=None, optimized_tokens=None,
                         duration_ms=None, **finalize_kwargs) -> str:
    """Finalize a tool response with STRUCTURED token accounting.

    This is the primary accounting path: tools pass explicitly measured
    (raw_tokens, optimized_tokens) values instead of encoding them in the
    summary string for SessionManager to regex-scrape
    (SessionManager._parse_summary_token_pair remains as a fallback for
    tools not yet migrated).

    Semantics — be honest about what these numbers mean:
      raw_tokens        full-read baseline: the token cost of ingesting the
                        entire un-optimized source. A counterfactual — the
                        model would not necessarily have read the whole file.
      optimized_tokens  what C3 actually returned for this operation.

    Savings derived from the pair are labeled
    ``estimated_saved_vs_full_read`` in session token_usage and in the
    per-tool telemetry JSONL (.c3/tool_telemetry.jsonl).

    Failure-safe: accounting errors never break the tool response. Any extra
    keyword args (e.g. response_tokens) are forwarded to ``finalize``.
    """
    try:
        session_mgr = getattr(svc, "session_mgr", None)
        if session_mgr is not None and (
                raw_tokens is not None or optimized_tokens is not None
                or duration_ms is not None):
            session_mgr.record_tool_tokens(
                tool_name, raw_tokens=raw_tokens,
                optimized_tokens=optimized_tokens, duration_ms=duration_ms)
    except Exception:
        pass
    return finalize(tool_name, args, response, summary, **finalize_kwargs)


# ── Ghost-file path validation ───────────────────────────────────────────────

# Python builtin / typing names that should never be file paths
_PYTHON_TYPE_NAMES = {
    "dict", "str", "int", "float", "bool", "list", "set", "tuple",
    "bytes", "bytearray", "complex", "frozenset", "memoryview",
    "type", "object", "range", "slice", "property", "classmethod",
    "staticmethod", "super", "None", "True", "False", "Ellipsis",
    "Any", "Union", "Optional", "List", "Dict", "Set", "Tuple",
    "Callable", "Iterator", "Generator", "Sequence", "Mapping",
}

# HEREDOC / here-string end-markers that leak as filenames when Bash or
# PowerShell misparses a `<<EOF` / `@'...'@` block. Mirror of
# cli/hook_ghost_files.py:_HEREDOC_MARKERS — kept duplicated to keep the
# hook script standalone (no cross-module imports).
_HEREDOC_MARKERS = {
    "EOF", "EOM", "EOL", "END", "STOP", "DONE",
    "MARK", "MARKER", "DELIM", "DELIMITER",
    "INPUT", "OUTPUT", "DATA", "BLOCK", "HEREDOC",
    "'@", "@'",
}

_GHOST_NAMES = _PYTHON_TYPE_NAMES | _HEREDOC_MARKERS


def validate_file_path(file_path: str) -> str | None:
    """Return an error message if file_path looks like a ghost-file path, else None.

    Catches common hallucinations where an LLM passes a Python type name,
    shell redirect artifact, or other non-path string as a file_path parameter.
    """
    if not file_path:
        return None

    # Support comma-separated paths: validate each individually
    paths = [p.strip() for p in file_path.split(",") if p.strip()]
    for p in paths:
        err = _check_single_path(p)
        if err:
            return err
    return None


def _check_single_path(p: str) -> str | None:
    """Validate a single file path segment."""
    name = Path(p).name

    # Bare Python type name or HEREDOC end-marker (e.g., file_path="dict" or "EOF")
    if name in _GHOST_NAMES:
        kind = "heredoc end-marker" if name in _HEREDOC_MARKERS else "Python type name"
        return (
            f"Rejected file_path={p!r} — looks like a {kind}, not a file path. "
            f"Pass the actual file path (e.g., 'src/models.py')."
        )

    # Partial type annotation (e.g., "tuple[float", "dict[str, int]")
    if "[" in name and not Path(p).suffix:
        return (
            f"Rejected file_path={p!r} — looks like a type annotation fragment, not a file path."
        )

    # Shell redirect artifact (e.g., "=3.0.0" from >=3.0.0)
    if name.startswith("=") or name.startswith(">") or name.startswith("<"):
        return (
            f"Rejected file_path={p!r} — looks like a shell redirect artifact, not a file path."
        )

    return None
