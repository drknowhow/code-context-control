"""c3_read — Surgical symbol/line extraction from files.
Supports comma-separated paths with parallel extraction via ThreadPoolExecutor."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from cli.tools import _grants
from cli.tools._helpers import finalize_with_tokens, maybe_related_facts
from cli.tools.compress import map_detail
from core import count_tokens
from services import access_guard


def _coerce_list(val: Any) -> list[str] | None:
    """Coerce symbols from string/JSON to list. MCP clients sometimes serialize lists as strings."""
    if val is None:
        return None
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("["):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed]
            except (json.JSONDecodeError, ValueError):
                pass
        if val:
            # Comma-separated symbols ("a,b,c") -> multiple targets. Function/class
            # names never contain commas, and regex anchors (^foo$) have none either.
            if "," in val:
                return [s.strip() for s in val.split(",") if s.strip()]
            return [val]
    return None


def _coerce_lines(val: Any):
    """Coerce `lines` from MCP's string serialization into an int or list.

    MCP clients sometimes serialize numbers/lists as strings (the same reason
    `_coerce_list` exists for `symbols`). Without this, a JSON-string such as
    "[22, 193]" or "22" falls through handle_read's range logic and the tool
    silently returns the file *map* instead of the requested source lines.
    """
    if val is None or isinstance(val, (int, list, tuple)):
        return val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        if val.startswith("["):
            try:
                parsed = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return None
            return parsed if isinstance(parsed, list) else None
        # Comma-separated integers like "1,2,3" → list of line numbers.
        if "," in val:
            try:
                nums = [int(p.strip()) for p in val.split(",") if p.strip()]
            except ValueError:
                return None
            return nums or None
        try:
            return int(val)
        except ValueError:
            # "start-end" like "22-40". Only treat as a range when both ends
            # are non-negative ints — a leading "-5" is a (rejected) negative,
            # not a range. partition on the FIRST "-" preserves that.
            if "-" in val:
                a, _, b = val.partition("-")
                try:
                    return [int(a.strip()), int(b.strip())]
                except ValueError:
                    return None
    return None


def handle_read(file_path: str, symbols: Any = None, lines: Any = None,
                include_docstrings: bool = True, svc=None, finalize=None) -> str:
    symbols = _coerce_list(symbols)
    lines = _coerce_lines(lines)
    # Multi-file dispatch (parallel)
    if "," in file_path:
        paths = [p.strip() for p in file_path.split(",") if p.strip()]
        if len(paths) == 1:
            return handle_read(paths[0], symbols=symbols, lines=lines,
                               include_docstrings=include_docstrings,
                               svc=svc, finalize=finalize)
        # Nested ThreadPoolExecutor workers don't inherit MCP's request_context
        # contextvar, so calling the real `finalize` inside a worker raises
        # 'NoneType'.lifespan_context. Use a no-op in workers and run the real
        # finalize once on the combined result (still on the outer to_thread,
        # which does propagate contextvars).
        def _worker_finalize(name, args, resp, summ, **kw):
            return resp

        results = {}
        with ThreadPoolExecutor(max_workers=min(len(paths), 4)) as pool:
            futures = {
                pool.submit(handle_read, p, symbols, lines,
                            include_docstrings, svc, _worker_finalize): p
                for p in paths
            }
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    results[p] = fut.result()
                except Exception as e:
                    results[p] = f"[read:error] {p}: {e}"
        combined = "\n\n".join(results[p] for p in paths)
        if finalize is None:
            return combined
        combined_tokens = count_tokens(combined)
        return finalize("c3_read",
                        {"file": file_path, "symbols": symbols},
                        combined,
                        f"multi:{len(paths)}files->{combined_tokens}tok",
                        response_tokens=combined_tokens)

    # Access Guard: read verdict per file (docs/access-guard.md §3), checked
    # before existence so probes get the same refusal whether or not the
    # target exists (R2). Batch members hit this via the per-file dispatch,
    # so allowed members are served and denied ones carry the S1 line inline.
    # A live grant (P2a) is consulted only on the DENIAL branch. The masked
    # branch below is untouched on purpose: a mask is not a refusal to be
    # lifted, it is a different view being served, and "approve once" has no
    # meaning for it. Widening that is a separate decision, not a side effect.
    _verdict = access_guard.verdict(file_path, "read", svc.project_path)
    if _verdict.denial and not _grants.allow(svc, _verdict.denial,
                                             tool="c3_read", op="read",
                                             path=file_path):
        # A confirm hold (a builtin downgraded to mode=confirm — user rules
        # never confirm reads) files its own request so S8 can name it.
        rid, note = _grants.confirm_request(svc, _verdict.denial,
                                            tool="c3_read", op="read",
                                            path=file_path)
        resp = access_guard.refusal(_verdict.denial, file_path, "read",
                                    request_id=rid, request_note=note)
        if finalize is None:
            return resp
        return finalize("c3_read", {"file": file_path}, resp, "access-denied")

    # Mask Guard: serve the materialized view instead of the file's bytes
    # (docs/mask-guard.md §2). Line/symbol slicing is deliberately NOT applied
    # to a masked view — line numbers in a transformed file do not correspond
    # to the original, and handing the agent coordinates that look real is
    # exactly the class of quiet wrongness masking exists to prevent.
    if _verdict.masked:
        from services import mask_mirror
        try:
            view = mask_mirror.render_for_path(file_path, svc.project_path)
        except mask_mirror.MaskUnavailable as exc:
            resp = f"{access_guard.TAG_MASK_UNSUPPORTED} {exc.message}"
            if finalize is None:
                return resp
            return finalize("c3_read", {"file": file_path}, resp,
                            "mask-unavailable")
        resp = view.with_header(file_path)
        if lines is not None or symbols:
            resp += ("\n\n[c3-mask:note] line and symbol selection was "
                     "ignored: positions in a transformed view do not map to "
                     "the original file. The whole view is shown.")
        if finalize is None:
            return resp
        return finalize("c3_read", {"file": file_path}, resp,
                        f"masked:{view.preset}")

    full = Path(svc.project_path) / file_path
    if not full.exists():
        full = Path(file_path)
    if not full.exists():
        return f"[read:error] File not found: {file_path}"

    rel_path = str(full.resolve().relative_to(Path(svc.project_path).resolve())).replace("\\", "/")

    if full.is_dir():
        # A directory maps to one line per file, budgeted
        # (docs/file-map.md § Directories). `lines=<int>` sets the budget.
        from services.dir_map import DEFAULT_MAX_TOKENS, render_directory_map
        budget = DEFAULT_MAX_TOKENS
        if isinstance(lines, int) and not isinstance(lines, bool) and lines > 0:
            budget = lines
        text, dir_detail = render_directory_map(svc, rel_path, max_tokens=budget)
        tok = count_tokens(text)
        if finalize is None:
            return text
        return finalize_with_tokens(
            finalize, svc, "c3_read", {"file": file_path}, text,
            f"dir:{dir_detail['files']}files->{tok}tok",
            optimized_tokens=tok, response_tokens=tok, detail=dir_detail)

    # Resolve ranges
    ranges = []

    def _add_range(start: int, end: int) -> None:
        # Swap reversed ranges (e.g. "40-22") and drop non-positive line
        # numbers — lines are 1-based, so a negative/zero spec is invalid
        # rather than a relative offset.
        if start > end:
            start, end = end, start
        if end < 1:
            return
        start = max(start, 1)
        ranges.append((start, end))

    if lines is not None:
        if isinstance(lines, int):
            line_specs = [lines]
        elif isinstance(lines, (list, tuple)) and len(lines) == 2 and all(isinstance(x, int) for x in lines):
            line_specs = [lines]
        elif isinstance(lines, (list, tuple)):
            line_specs = lines
        else:
            line_specs = []

        for spec in line_specs:
            if isinstance(spec, int):
                _add_range(spec, spec)
            elif isinstance(spec, (list, tuple)) and len(spec) >= 2:
                _add_range(int(spec[0]), int(spec[1]))
            elif isinstance(spec, (list, tuple)) and len(spec) == 1:
                _add_range(int(spec[0]), int(spec[0]))

    # Ensure file_memory index is fresh.
    # When the watcher is running, it handles updates in the background —
    # only force-update if file_memory has no record at all (first access).
    try:
        watcher_active = (hasattr(svc, "watcher") and svc.watcher._observer.is_alive())
        if watcher_active:
            if not svc.file_memory.get(rel_path):
                svc.file_memory.update(rel_path)
        elif svc.file_memory.needs_update(rel_path):
            svc.file_memory.update(rel_path)
    except Exception:
        pass

    raw_text = full.read_text(encoding="utf-8", errors="replace")
    # EOL-normalize exactly the way c3_edit's matcher does (\r\n and \r → \n),
    # then split on \n ONLY. splitlines() also breaks on \x0c/\u2028/\x85 etc.,
    # which rendered those in-line chars as line breaks — an old_string copied
    # from that output (with \n) could never match the actual file bytes.
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    content_lines = raw_text.split("\n")
    if content_lines and content_lines[-1] == "":
        content_lines.pop()  # trailing \n would otherwise add a phantom empty line
    # Lazy: only count full file tokens when needed for the summary string
    _full_tok_cache = [None]

    def full_file_tokens():
        if _full_tok_cache[0] is None:
            _full_tok_cache[0] = count_tokens(raw_text)
        return _full_tok_cache[0]

    if symbols:
        matches = svc.file_memory.get_symbol_ranges(rel_path, symbols, return_matches=True)

        # Check for ambiguity
        disambiguation_msgs = []
        for target in symbols:
            if target.startswith('^') or target in ('<main>', '<globals>', '<imports>'):
                continue
            target_matches = [m for m in matches if m["target"] == target.lower() or m["target"] == target]
            unique_names = set(m["match"] for m in target_matches)
            if len(unique_names) > 1:
                exact = [m for m in target_matches if m["match"].lower() == target.lower()]
                if exact:
                    matches = [m for m in matches
                               if m["target"] != target and m["target"] != target.lower()
                               or m["match"].lower() == target.lower()]
                else:
                    options = ", ".join(
                        f"{m['match']} (L{m['range'][0]}-L{m['range'][1]})" for m in target_matches)
                    disambiguation_msgs.append(
                        f"Ambiguous symbol '{target}'. Did you mean: {options}?")

        if disambiguation_msgs:
            resp = (f"[read:error] Ambiguous symbols found in {file_path}:\n"
                    + "\n".join(disambiguation_msgs)
                    + "\nTry using exact regex (e.g., '^symbol_name$') or the specific symbol name.")
            resp_tok = count_tokens(resp)
            return finalize_with_tokens(
                finalize, svc, "c3_read", {"file": file_path, "symbols": symbols},
                resp, f"{full_file_tokens()}->{resp_tok}tok",
                raw_tokens=full_file_tokens(), optimized_tokens=resp_tok,
                response_tokens=resp_tok)

        for m in matches:
            ranges.append(m["range"])

        if '<main>' in symbols or '<globals>' in symbols:
            record = svc.file_memory.get(rel_path)
            if record and "sections" in record:
                covered = set()

                def _mark(secs):
                    for s in secs:
                        covered.update(range(s["line_start"], s["line_end"] + 1))
                        if "children" in s:
                            _mark(s["children"])

                _mark(record["sections"])
                main_ranges = []
                current_start = None
                for i in range(1, len(content_lines) + 1):
                    if i not in covered:
                        if current_start is None:
                            current_start = i
                    else:
                        if current_start is not None:
                            main_ranges.append((current_start, i - 1))
                            current_start = None
                if current_start is not None:
                    main_ranges.append((current_start, len(content_lines)))
                ranges.extend(main_ranges)

    if not ranges and symbols:
        map_cached = not svc.file_memory.needs_update(rel_path)
        file_map = svc.file_memory.get_or_build_map(rel_path)
        resp = f"[read:{file_path}] symbols not found: {symbols}. Showing file map:\n{file_map}"
        map_tok = count_tokens(file_map)
        return finalize_with_tokens(
            finalize, svc, "c3_read", {"file": file_path, "symbols": symbols},
            resp, f"{full_file_tokens()}->{map_tok}tok",
            raw_tokens=full_file_tokens(), optimized_tokens=map_tok,
            detail=map_detail(svc, rel_path, "read", cache_hit=map_cached,
                              symbols=len(symbols), fallback="symbols_not_found"))

    if not ranges:
        map_cached = not svc.file_memory.needs_update(rel_path)
        budget = svc.file_memory.MAP_TOKEN_BUDGET
        if isinstance(lines, int) and not isinstance(lines, bool) and lines > 0:
            budget = lines   # lines=<int> with no symbols = map budget (C4)
        file_map = svc.file_memory.get_or_build_map(rel_path, max_tokens=budget)
        if count_tokens(file_map) >= full_file_tokens():
            # A map that costs more than the file is worth nothing: serve the
            # file (docs/file-map.md § Small files).
            file_map = (f"[read:{file_path}] whole file — smaller than its map\n"
                        + raw_text)
        resp = (file_map
                + "\n[map only — pass lines=[start,end] or symbols=[...] for exact source]"
                + maybe_related_facts(svc, rel_path, top_k=3, context="read"))
        map_tok = count_tokens(resp)
        return finalize_with_tokens(
            finalize, svc, "c3_read", {"file": file_path},
            resp, f"{full_file_tokens()}->{map_tok}tok",
            raw_tokens=full_file_tokens(), optimized_tokens=map_tok,
            response_tokens=map_tok,
            detail=map_detail(svc, rel_path, "read", cache_hit=map_cached,
                              fallback="map_only"))
    else:
        # Sort and merge overlapping ranges
        ranges.sort()
        merged = []
        if ranges:
            curr_start, curr_end = ranges[0]
            for next_start, next_end in ranges[1:]:
                if next_start <= curr_end + 1:
                    curr_end = max(curr_end, next_end)
                else:
                    merged.append((curr_start, curr_end))
                    curr_start, curr_end = next_start, next_end
            merged.append((curr_start, curr_end))
        ranges = merged
        header = f"[read:{file_path}]"

    parts = []
    prev_end = None
    for start, end in ranges:
        s_idx = max(0, start - 1)
        e_idx = min(len(content_lines), end)
        chunk = content_lines[s_idx:e_idx]
        if len(ranges) > 1:
            # ⟦…⟧ markers are tool chrome, not file content. The gap note makes
            # the discontinuity explicit so a copied old_string never spans it.
            if prev_end is None:
                parts.append(f"⟦L{start}-L{end}⟧")
            else:
                parts.append(
                    f"⟦L{start}-L{end} — {start - prev_end - 1} lines "
                    f"(L{prev_end + 1}-L{start - 1}) omitted; blocks are NOT "
                    f"contiguous, never span this marker in a c3_edit old_string⟧")
        parts.extend(chunk)
        prev_end = end

    final_content = "\n".join(parts)
    resp = final_content + maybe_related_facts(svc, rel_path, top_k=3, context="read")
    tokens = count_tokens(resp)
    summary = f"{full_file_tokens()}->{tokens}tok" if tokens < full_file_tokens() else f"{tokens}tok"
    # C0 measurement: what the read asked for and how much source it took.
    read_detail = {
        "backend": "source",
        "symbols": len(symbols) if symbols else 0,
        "by_lines": bool(lines),
        "ranges": len(ranges),
        "lines_served": sum(e - s_ + 1 for s_, e in ranges),
        "file_lines": len(content_lines),
    }
    return finalize_with_tokens(
        finalize, svc, "c3_read", {"file": file_path, "symbols": symbols}, resp, summary,
        raw_tokens=full_file_tokens(), optimized_tokens=tokens,
        response_tokens=tokens, detail=read_detail)
