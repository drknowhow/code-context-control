"""Mask Guard transform engines — deterministic, versioned, no LLM.

Implements docs/mask-guard.md §4. Every preset here is a pure function of
(text, params, salt): the same input must render identically on every read and
through every surface, or an agent can recover the original by differencing
surfaces (§2). Nothing in this module may call a model, consult the clock, or
use unseeded randomness.

Failure is always closed: an unsupported language, an unparseable table, a
binary blob, or a residual secret after rendering raises ``MaskRenderError``
and the read refuses. Serving a half-transformed view is the one outcome worse
than refusing.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

# Bumping this invalidates every materialized view (it is in the view hash).
TRANSFORMER_VERSION = 1

# Placeholder syntax: deliberately NOT valid in any language we support, so a
# placeholder that somehow reaches a real file trips a linter/compiler
# immediately instead of being silently committed (docs/mask-guard.md §5).
PLACEHOLDER = "«c3:redacted:{kind}»"

_MAX_BYTES = 8 * 1024 * 1024  # refuse to render anything larger


class MaskRenderError(Exception):
    """A preset could not render this input. The read must refuse."""


@dataclass(frozen=True)
class RenderResult:
    text: str
    preset: str
    stats: dict          # per-preset counters, safe to show (no raw values)


# ── Secret detectors ────────────────────────────────────────────────────────
# Ordered: the first pattern that matches a span wins. Keep patterns anchored
# and specific — a greedy pattern that eats a whole line is a correctness bug,
# not a safety win, because the agent then cannot reason about the file at all.

_SECRET_PATTERNS = (
    ("private_key",
     re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
                re.DOTALL)),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")),
    ("connection_string",
     re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s:@/]+@[^\s/]+", re.IGNORECASE)),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}")),
    # Assignment form: KEY = "value" / KEY: value — redact the VALUE only, so
    # the agent still sees which setting exists. Group 1 is kept verbatim.
    ("assigned_secret", re.compile(
        r"(?im)^([ \t]*[\"']?[A-Za-z_][A-Za-z0-9_]*"
        r"(?:SECRET|PASSWD|PASSWORD|TOKEN|APIKEY|API_KEY|ACCESS_KEY|"
        r"PRIVATE_KEY|CLIENT_SECRET|AUTH)[A-Za-z0-9_]*[\"']?"
        r"[ \t]*[:=][ \t]*)"
        r"[\"']?([^\s\"'#,]{6,})[\"']?")),
)

# Entropy sweep: long opaque tokens no named pattern claimed. Bounded charset
# and length so ordinary code (hashes in lockfiles, base64 icons) is not
# shredded — those are handled by keeping the threshold high.
_ENTROPY_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")
_ENTROPY_THRESHOLD = 4.2


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _redact_secrets(text: str, params: dict, salt: str) -> RenderResult:
    hits = {}

    def _bump(kind: str) -> str:
        hits[kind] = hits.get(kind, 0) + 1
        return PLACEHOLDER.format(kind=kind)

    out = text
    for kind, pattern in _SECRET_PATTERNS:
        if kind == "assigned_secret":
            out = pattern.sub(lambda m: m.group(1) + _bump("secret"), out)
        else:
            out = pattern.sub(lambda m, k=kind: _bump(k), out)

    def _entropy_sub(m):
        token = m.group(0)
        if token.startswith("«c3:"):
            return token
        if _shannon(token) < _ENTROPY_THRESHOLD:
            return token
        return _bump("high_entropy")

    out = _ENTROPY_CANDIDATE.sub(_entropy_sub, out)
    return RenderResult(out, "redact_secrets", {"redacted": hits})


# ── Tabular helpers ─────────────────────────────────────────────────────────

def _sniff_delimiter(header: str) -> str:
    """Tab wins when present — TSV headers rarely contain literal tabs else."""
    return "\t" if "\t" in header else ","


def _split_lines(text: str) -> tuple:
    """(lines, line_ending) preserving the file's dominant ending."""
    ending = "\r\n" if "\r\n" in text else "\n"
    return text.split(ending), ending


def _pseudonym(column: str, value: str, salt: str) -> str:
    """Stable, salted, one-way pseudonym for one cell.

    Same real value -> same pseudonym WITHIN a project, so joins, uniqueness
    and cardinality survive masking (the property Table Parser's injective
    dictionary bought with a stored reverse map — bought here with a salt
    instead, so there is no reverse map to protect).

    The salt is project-local and never enters model context; without it the
    digest cannot be brute-forced back to a low-cardinality domain.
    """
    digest = hashlib.blake2b(
        f"{salt}\x00{column}\x00{value}".encode("utf-8"), digest_size=5
    ).hexdigest()
    return f"{column.strip().lower().replace(' ', '_')[:24]}_{digest}"


def _redact_columns(text: str, params: dict, salt: str) -> RenderResult:
    lines, ending = _split_lines(text)
    if not lines or not lines[0].strip():
        raise MaskRenderError("redact_columns: file has no header row")
    delim = _sniff_delimiter(lines[0])
    header = [h.strip() for h in lines[0].split(delim)]
    wanted = [c.strip() for c in params["columns"]]
    lower = {h.lower(): i for i, h in enumerate(header)}
    idx = []
    missing = []
    for col in wanted:
        if col.lower() in lower:
            idx.append(lower[col.lower()])
        else:
            missing.append(col)
    if missing:
        # Fail closed: a typo'd column name would silently leave real values
        # exposed while the UI reports the file as masked.
        raise MaskRenderError(
            f"redact_columns: column(s) not found in header: "
            f"{', '.join(missing)} (header: {', '.join(header)})")

    out, replaced = [lines[0]], 0
    for line in lines[1:]:
        if not line.strip():
            out.append(line)
            continue
        cells = line.split(delim)
        for i in idx:
            if i < len(cells) and cells[i].strip():
                cells[i] = _pseudonym(header[i], cells[i].strip(), salt)
                replaced += 1
        out.append(delim.join(cells))
    return RenderResult(ending.join(out), "redact_columns",
                        {"columns": wanted, "cells_replaced": replaced})


def _sample_rows(text: str, params: dict, salt: str) -> RenderResult:
    count = int(params["count"])
    strategy = params["strategy"]
    lines, ending = _split_lines(text)
    if not lines:
        raise MaskRenderError("sample_rows: empty file")
    body = [ln for ln in lines[1:] if ln.strip()]
    total = len(body)
    kept = body[:count] if strategy == "first" else body[max(0, total - count):]
    note = (f"# {PLACEHOLDER.format(kind='rows')} {total - len(kept)} of "
            f"{total} data rows withheld by mask policy "
            f"(sample_rows:{strategy}:{count})")
    out = [lines[0]] + kept + [note]
    return RenderResult(ending.join(out), "sample_rows",
                        {"rows_total": total, "rows_kept": len(kept),
                         "strategy": strategy})


def _signatures_only(text: str, params: dict, salt: str, *,
                     path: str = "", compressor=None) -> RenderResult:
    """Structure-only view. Reuses the compressor — the CROP family is a
    re-wiring of machinery C3 already has (docs/mask-guard.md §1).

    ``compressor`` is injected by the mirror rather than imported here:
    compressor imports the mirror, which imports this module, so a
    module-level import would cycle.
    """
    from pathlib import Path as _Path

    ext = _Path(path).suffix.lower() if path else ""
    if not ext:
        raise MaskRenderError(
            "signatures_only: cannot determine language without a file "
            "extension")
    if compressor is None:
        raise MaskRenderError(
            "signatures_only: no structural extractor supplied — the caller "
            "must inject a CodeCompressor")
    try:
        rendered = compressor._extract_structure(text, ext, "structure")
    except Exception as exc:
        raise MaskRenderError(
            f"signatures_only: cannot parse {path or 'input'} ({exc})") from exc
    if not rendered or not rendered.strip():
        raise MaskRenderError(
            f"signatures_only: no structure extracted from {path or 'input'} "
            "— unsupported language or syntax")
    return RenderResult(rendered, "signatures_only",
                        {"lines_in": text.count("\n") + 1,
                         "lines_out": rendered.count("\n") + 1})


_ENGINES = {
    "redact_secrets": _redact_secrets,
    "redact_columns": _redact_columns,
    "sample_rows": _sample_rows,
    "signatures_only": _signatures_only,
}


# ── Protected Mode ──────────────────────────────────────────────────────────

def residual_secrets(text: str) -> list:
    """Secret kinds still present in *rendered* output. Empty list is the
    only acceptable result (docs/mask-guard.md §7)."""
    found = []
    for kind, pattern in _SECRET_PATTERNS:
        if kind == "assigned_secret":
            # The assignment form legitimately survives as `KEY = «c3:...»`.
            for m in pattern.finditer(text):
                if not m.group(2).startswith("«c3:"):
                    found.append(kind)
                    break
            continue
        if pattern.search(text):
            found.append(kind)
    return found


def render(text: str, preset: str, params: dict, *, salt: str = "",
           path: str = "", protected: bool = True,
           compressor=None) -> RenderResult:
    """Render *text* through *preset*. Raises MaskRenderError to refuse.

    ``protected`` runs the post-render residual scan (Protected Mode). It is
    on by default and callers should leave it on: it converts the worst class
    of bug — a detector that silently missed something — from a quiet leak
    into a loud refusal.
    """
    engine = _ENGINES.get(preset)
    if engine is None:
        raise MaskRenderError(f"unknown mask preset '{preset}'")
    if len(text.encode("utf-8", errors="ignore")) > _MAX_BYTES:
        raise MaskRenderError(
            f"input exceeds the {_MAX_BYTES // (1024 * 1024)}MB mask render "
            "cap — narrow the glob or use a crop preset upstream")
    if "\x00" in text[:4096]:
        raise MaskRenderError("input looks binary — refusing to render")

    if preset == "signatures_only":
        result = engine(text, params, salt, path=path, compressor=compressor)
    else:
        result = engine(text, params, salt)

    if protected:
        residual = residual_secrets(result.text)
        if residual:
            raise MaskRenderError(
                "Protected Mode: rendered view still contains detectable "
                f"secret material ({', '.join(sorted(set(residual)))}). "
                "Refusing to serve a partially-masked view — add a "
                "redact_secrets rule for this path or tighten the preset.")
    return result
