"""Directory maps — one line per file, budgeted, for `c3_read('<dir>')`.

    # services/ (58 files, 41,203L)
    compressor.py (809L python) — CodeCompressor; compress_file; STRUCTURE_PATTERNS
    file_map.py (280L python) — render_map; parse_signature; parse_map; KINDS
    …
    … 12 more files

Files are ranked the way a reader would want them: recently edited first
(the edit ledger), then by how much structure they hold (symbol count),
then by path. The map is filled to a token budget and closes with how
many files it left out. Traversal is bounded — the scanner's SKIP_DIRS
and .gitignore pruning, never following symlinks, a hard file cap — and
structure comes from file_memory records, extracting on the fly only
for a bounded number of code files (docs/file-map.md § Directories).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from core import count_tokens
from services.file_memory import CODE_EXTENSIONS, LANG_MAP
from services.scanner import iter_files

DEFAULT_MAX_TOKENS = 1500
MAX_FILES = 400            # hard traversal cap
MAX_EXTRACT_FILES = 120    # code files parsed on the fly, per call
EXTRACT_DEADLINE_S = 3.0   # then the rest show line counts only
MAX_EXTRACT_BYTES = 200 * 1024
SYMBOLS_PER_FILE = 6
LEDGER_LOOKBACK = 300      # recent ledger entries consulted for ranking


def _recent_edit_ranks(svc) -> dict:
    """path -> recency rank (0 = most recent) from the edit ledger."""
    ledger = getattr(svc, "edit_ledger", None)
    if ledger is None:
        return {}
    try:
        entries = ledger.get_history(limit=LEDGER_LOOKBACK)
    except Exception:
        return {}
    ranks: dict = {}
    for entry in entries or []:
        f = str((entry or {}).get("file") or "").replace("\\", "/")
        if f and f not in ranks:
            ranks[f] = len(ranks)
    return ranks


#: What a reader wants to see first on a one-line summary of a file.
_SYMBOL_PRIORITY = {"class": 0, "interface": 0, "struct": 0, "trait": 0, "enum": 0,
                    "function": 1, "method": 1, "type": 2, "heading": 2, "section": 2,
                    "constant": 3, "variable": 3}
_SKIP_SYMBOL_TYPES = {"import", "decorator", "comment", "content", "property", "impl"}


def _structural_sections(record: Optional[dict]) -> list:
    return [s for s in (record or {}).get("sections") or []
            if s.get("type") not in _SKIP_SYMBOL_TYPES
            and str(s.get("name") or "").strip() not in ("", "(full file)")]


def _top_symbols(record: Optional[dict]) -> list:
    """Up to SYMBOLS_PER_FILE names: classes first, then functions, then the
    rest, constants last — each group in source order."""
    picked = sorted(enumerate(_structural_sections(record)),
                    key=lambda iv: (_SYMBOL_PRIORITY.get(iv[1].get("type"), 2),
                                    str(iv[1].get("name") or "").startswith("_"),
                                    iv[0]))
    names = []
    for _, s in picked[:SYMBOLS_PER_FILE]:
        name = str(s.get("name") or "").strip()
        if s.get("type") == "heading":
            name = name.split(":", 1)[-1].strip()
        names.append(name)
    return names


def _line_count(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def render_directory_map(svc, rel_dir: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple:
    """Return (text, detail) for the directory `rel_dir` (project-relative, posix)."""
    root = Path(svc.project_path).resolve()
    rel_dir = str(rel_dir).replace("\\", "/").strip("/")
    target = (root / rel_dir).resolve() if rel_dir else root
    label = (rel_dir + "/") if rel_dir else "./"

    files = []
    capped = False
    for fp in iter_files(target, max_files=MAX_FILES + 1):
        if len(files) >= MAX_FILES:
            capped = True
            break
        try:
            files.append(fp.resolve().relative_to(root).as_posix())
        except ValueError:
            continue

    store = getattr(svc, "file_memory", None)
    recent = _recent_edit_ranks(svc)
    started = time.monotonic()
    extracted = 0
    rows = []
    total_lines = 0
    for rel in files:
        record = None
        if store is not None:
            try:
                record = store.get(rel)
            except Exception:
                record = None
            ext = Path(rel).suffix.lower()
            if (record is None and ext in CODE_EXTENSIONS
                    and extracted < MAX_EXTRACT_FILES
                    and time.monotonic() - started < EXTRACT_DEADLINE_S):
                try:
                    if (root / rel).stat().st_size <= MAX_EXTRACT_BYTES:
                        record = store.update(rel)
                        extracted += 1
                except Exception:
                    record = None
        lines = int((record or {}).get("lines") or 0) or _line_count(root / rel)
        total_lines += lines
        ext = Path(rel).suffix.lower()
        lang = str((record or {}).get("language") or LANG_MAP.get(ext) or ext.lstrip(".") or "")
        symbols = _top_symbols(record)
        rows.append({
            "rel": rel,
            "name": rel[len(rel_dir) + 1:] if rel_dir and rel.startswith(rel_dir + "/") else rel,
            "lines": lines,
            "lang": lang,
            "symbols": symbols,
            "recent": recent.get(rel, 10 ** 6),
            "nsym": len(_structural_sections(record)),
        })

    rows.sort(key=lambda r: (r["recent"], -r["nsym"], r["rel"]))

    header = f"# {label} ({len(files)}{'+' if capped else ''} files, {total_lines:,}L)"
    body = []
    for r in rows:
        line = f"{r['name']} ({r['lines']}L {r['lang']})".rstrip()
        if r["symbols"]:
            line += " — " + "; ".join(r["symbols"])
        body.append(line)

    kept = list(body)
    while kept and count_tokens("\n".join([header] + kept)) > max_tokens:
        kept.pop()
    dropped = len(body) - len(kept)
    parts = [header] + kept
    if dropped:
        parts.append(f"… {dropped} more files")
    if capped:
        parts.append(f"[dir_map] traversal capped at {MAX_FILES} files; map a subdirectory")
    # In source order the reader gets recency first; say so once.
    parts.append("[dir_map] recently edited first, then by structure; "
                 "c3_read('<file>') maps one file")
    text = "\n".join(parts)
    detail = {
        "backend": "directory",
        "files": len(files),
        "listed": len(kept),
        "extracted": extracted,
        "capped": capped,
    }
    return text, detail
