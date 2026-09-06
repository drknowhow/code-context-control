"""c3_compress — the canonical file map (services/file_map.render_map), one file or a batch.

There is one mode, `map`, and it is the same text c3_read(file_path) serves.
The modes that existed before 2.122.0 (dense_map, smart, diff, bug_scan,
ast, structure, outline) were called twice in 3,932 tool calls; a request
for one of them is answered with the map plus a one-line notice so an old
prompt keeps working while it says so (docs/file-map.md § Retired modes).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cli.tools import _grants
from cli.tools._helpers import finalize_with_tokens, show_token_ratios
from core import count_tokens
from services import access_guard

#: Every mode name c3_compress ever accepted, and what to say about it now.
RETIRED_MODES = {
    "dense_map": "the map is dense now",
    "smart": "the map carries full signatures",
    "structure": "the map carries full signatures",
    "outline": "the map carries full signatures",
    "diff": "use `git diff` or the edit ledger (c3_edits)",
    "bug_scan": "use c3_search(action='exact', query='except ') for handler hotspots",
    "ast": "project architecture: codebase-memory-mcp `cli get_architecture`, or c3_status",
}
RETIRED_IN = "2.122.0"


def deprecation_line(requested: str) -> str:
    hint = RETIRED_MODES.get(requested, "")
    return (f"[compress:deprecated] mode '{requested}' retired in {RETIRED_IN} — "
            f"map is the only mode ({hint}); see docs/file-map.md")


def map_detail(svc, rel: str, requested_mode: str, *, cache_hit: bool,
               symbols: int = 0, fallback: str = "") -> dict:
    """Telemetry `detail` for a map served from file_memory (C0, 2.120.0).

    backend names the code path, parser the extractor that built the record
    (tree_sitter | regex | generic, "" for a pre-2.120.0 record), cache_hit
    whether the record was already fresh. `symbols` is how many the caller
    asked for and `fallback` why a read served a map instead of source.
    `deprecated_mode` (2.122.0) is set when a retired mode name was asked for.
    Flat so telemetry.aggregate_tool_telemetry can fold it without a schema.
    """
    record = None
    try:
        record = svc.file_memory.get(rel)
    except Exception:
        record = None
    if not isinstance(record, dict):
        record = None
    detail = {
        "requested_mode": requested_mode,
        "backend": "file_memory",
        "parser": str((record or {}).get("parser") or ""),
        "cache_hit": bool(cache_hit),
        "sections": len((record or {}).get("sections") or []),
    }
    if symbols:
        detail["symbols"] = int(symbols)
    if fallback:
        detail["fallback"] = fallback
    if requested_mode not in ("map", "read"):
        detail["deprecated_mode"] = requested_mode
    return detail


def handle_compress(file_path: str, mode: str, svc,
                    finalize, maybe_facts) -> str:
    requested = (mode or "map").strip() or "map"
    if requested != "map" and requested not in RETIRED_MODES:
        return finalize("c3_compress", {"file_path": file_path, "mode": requested},
                        f"[compress:error] Unknown mode '{requested}'. The only mode is 'map'.",
                        "error")
    note = deprecation_line(requested) if requested != "map" else ""

    # Batch dispatch: comma-separated paths
    if "," in file_path:
        paths = [p.strip() for p in file_path.split(",") if p.strip()]
        return _compress_batch(paths, requested, note, svc, finalize)

    return _compress_single(file_path, requested, note, svc, finalize)


def _build_map(svc, rel: str) -> str:
    """The canonical map for `rel`, draining any queued watcher updates first."""
    queued = svc.file_memory.drain_queue()
    completed = []
    failed = []
    for qp in queued[:10]:
        try:
            if svc.file_memory.update(qp):
                completed.append(qp)
            else:
                failed.append(qp)
        except Exception:
            failed.append(qp)
    if completed:
        svc.file_memory.complete_updates(completed)
    if failed:
        svc.file_memory.complete_updates(failed, failed=True)
    return svc.file_memory.get_or_build_map(rel, max_tokens=svc.file_memory.MAP_TOKEN_BUDGET)


def _compress_single(file_path: str, requested: str, note: str, svc, finalize) -> str:
    """Map a single file."""
    # Access Guard: read verdict up front (docs/access-guard.md §3).
    _verdict = access_guard.verdict(file_path, "read", svc.project_path)
    if _verdict.denial and not _grants.allow(svc, _verdict.denial,
                                             tool="c3_compress", op="read",
                                             path=file_path):
        return finalize("c3_compress", {"file_path": file_path, "mode": requested},
                        access_guard.refusal(_verdict.denial, file_path, "read"),
                        "access-denied")
    # Mask Guard: the map is built through file_memory, which reads the RAW
    # file — a masked path is served from its view instead
    # (docs/mask-guard.md §6, row 7).
    if _verdict.masked:
        from services import mask_mirror
        try:
            view = mask_mirror.render_for_path(file_path, svc.project_path)
        except mask_mirror.MaskUnavailable as exc:
            return finalize("c3_compress",
                            {"file_path": file_path, "mode": requested},
                            f"{access_guard.TAG_MASK_UNSUPPORTED} {exc.message}",
                            "mask-unavailable")
        return finalize("c3_compress", {"file_path": file_path, "mode": requested},
                        view.with_header(file_path), f"masked:{view.preset}")

    full = Path(svc.project_path) / file_path
    if not full.exists():
        full = Path(file_path)
    if not full.exists():
        return ("[file_map:error] not found. To create a new file use "
                "c3_edit(file_path=..., old_string='', new_string=<content>).")
    rel = str(full.resolve().relative_to(Path(svc.project_path).resolve())).replace("\\", "/")
    # C0 measurement: was the record already fresh before this call?
    cache_hit = not svc.file_memory.needs_update(rel)
    res = _build_map(svc, rel)
    raw_tokens = None
    map_tokens = 0
    try:
        raw_text = full.read_text(encoding="utf-8", errors="replace")
        raw_tokens = count_tokens(raw_text)
        map_tokens = count_tokens(res)
        if map_tokens >= raw_tokens and not res.startswith("[file_map]"):
            # A map that costs more than the file is worth nothing
            # (docs/file-map.md § Small files).
            res = f"[compress:{file_path}] whole file — smaller than its map\n" + raw_text
            map_tokens = count_tokens(res)
        summary = "map"
    except Exception:
        summary = "mapped"
    if note:
        res = note + "\n" + res
        map_tokens = count_tokens(res)
    return finalize_with_tokens(
        finalize, svc, "c3_compress", {"file_path": file_path, "mode": requested},
        res, summary,
        raw_tokens=raw_tokens, optimized_tokens=map_tokens or None,
        response_tokens=map_tokens,
        detail=map_detail(svc, rel, requested, cache_hit=cache_hit))


def _compress_batch(paths: list, requested: str, note: str, svc, finalize) -> str:
    """Map several files in parallel, return one combined report."""
    max_files = 10
    paths = paths[:max_files]

    results = {}

    def _do_one(fp):
        """Returns (fp, map_text, raw_tokens, map_tokens, error)."""
        try:
            # Access Guard: per-member read verdict — the S1 refusal becomes
            # this member's batch line; other members are still served.
            _v = access_guard.verdict(fp, "read", svc.project_path)
            if _v.denial:
                return fp, None, None, None, access_guard.refusal(_v.denial, fp,
                                                                  "read")
            if _v.masked:
                from services import mask_mirror
                try:
                    view = mask_mirror.render_for_path(fp, svc.project_path)
                except mask_mirror.MaskUnavailable as exc:
                    return fp, None, None, None, \
                        f"{access_guard.TAG_MASK_UNSUPPORTED} {exc.message}"
                text = view.with_header(fp)
                return fp, text, None, count_tokens(text), None
            full = Path(svc.project_path) / fp
            if not full.exists():
                full = Path(fp)
            if not full.exists():
                return fp, None, None, None, "not found"
            rel = str(full.resolve().relative_to(
                Path(svc.project_path).resolve())).replace("\\", "/")
            res = svc.file_memory.get_or_build_map(
                rel, max_tokens=svc.file_memory.MAP_TOKEN_BUDGET)
            try:
                raw_tok = count_tokens(full.read_text(encoding="utf-8", errors="replace"))
                map_tok = count_tokens(res)
                return fp, res, raw_tok, map_tok, None
            except Exception:
                return fp, res, None, None, None
        except Exception as e:
            return fp, None, None, None, str(e)

    with ThreadPoolExecutor(max_workers=min(len(paths), 8)) as pool:
        futures = {pool.submit(_do_one, fp): fp for fp in paths}
        for fut in as_completed(futures):
            fp, text, raw_tok, opt_tok, err = fut.result()
            results[fp] = (text, raw_tok, opt_tok, err)

    ratios = show_token_ratios(svc)
    parts = []
    total_ok = 0
    total_raw = 0
    total_opt = 0
    measured = 0
    for fp in paths:
        text, raw_tok, opt_tok, err = results.get(fp, (None, None, None, "unknown"))
        if text:
            tag = ""
            if raw_tok is not None and opt_tok is not None:
                measured += 1
                total_raw += raw_tok
                total_opt += opt_tok
                if ratios:
                    tag = f" ({raw_tok}->{opt_tok}tok)"
            parts.append(f"## {fp}{tag}\n{text}")
            total_ok += 1
        else:
            parts.append(f"## {fp} — ERROR: {err}")

    header = f"[compress:batch] {total_ok}/{len(paths)} files (map)"
    if note:
        header = note + "\n" + header
    body = header + "\n\n" + "\n\n".join(parts)
    detail = {"requested_mode": requested, "backend": "batch",
              "files": len(paths), "ok": total_ok}
    if requested != "map":
        detail["deprecated_mode"] = requested
    return finalize_with_tokens(
        finalize, svc, "c3_compress",
        {"file_path": ",".join(paths), "mode": requested, "batch": True},
        body, f"batch {total_ok}/{len(paths)}",
        raw_tokens=total_raw if measured else None,
        optimized_tokens=total_opt if measured else None,
        detail=detail)
