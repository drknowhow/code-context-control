"""Shared helpers for tool handlers."""

from pathlib import Path


def maybe_related_facts(svc, topic: str, top_k: int = 3, width: int = 100) -> str:
    """Append related facts if enabled. Currently disabled — adds noise."""
    return ""


# ── Ghost-file path validation ───────────────────────────────────────────────

# Python builtin / typing names that should never be file paths
_GHOST_NAMES = {
    "dict", "str", "int", "float", "bool", "list", "set", "tuple",
    "bytes", "bytearray", "complex", "frozenset", "memoryview",
    "type", "object", "range", "slice", "property", "classmethod",
    "staticmethod", "super", "None", "True", "False", "Ellipsis",
    "Any", "Union", "Optional", "List", "Dict", "Set", "Tuple",
    "Callable", "Iterator", "Generator", "Sequence", "Mapping",
}


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

    # Bare Python type name (e.g., file_path="dict")
    if name in _GHOST_NAMES:
        return (
            f"Rejected file_path={p!r} — looks like a Python type name, not a file path. "
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
