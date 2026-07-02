"""c3_search — Code, file, and transcript discovery."""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cli.tools._helpers import finalize_with_tokens, show_token_ratios
from core import count_tokens

# Hard cap: responses above this are truncated to avoid filling context.
_RESPONSE_TOKEN_CAP = 2400


def _approx_tokens(text: str) -> int:
    """Fast token estimate (~4 chars/token). Use for budget checks where exact
    tiktoken counts are wasted work — the difference is <5% and noise."""
    return len(text) >> 2


def _cap_response(resp: str) -> str:
    """Truncate response if it exceeds the token cap."""
    tok = count_tokens(resp)
    if tok <= _RESPONSE_TOKEN_CAP:
        return resp
    # Binary-ish search: cut lines from the end until under budget
    lines = resp.split("\n")
    while len(lines) > 1:
        lines = lines[:len(lines) * 3 // 4]  # drop ~25% each iteration
        candidate = "\n".join(lines) + "\n[truncated]"
        if count_tokens(candidate) <= _RESPONSE_TOKEN_CAP:
            return candidate
    return "\n".join(lines[:20]) + "\n[truncated]"


def handle_search(query: str, action: str, top_k: int, max_tokens: int,
                  svc, finalize, maybe_facts, prefetch: bool = False,
                  scope: str = "") -> str:
    top_k = max(1, min(int(top_k), 10))
    max_tokens = min(max(200, int(max_tokens)), _RESPONSE_TOKEN_CAP)

    # Sub-project fan-out: scope='all' (parent + children) or '<child name>'.
    scope = (scope or "").strip()
    if scope and scope not in ("project", "self"):
        try:
            from core.config import load_hybrid_config
            sub_cfg = load_hybrid_config(getattr(svc, "project_path", "")).get("subprojects") or {}
            fanout_enabled = bool(sub_cfg.get("search_fanout", True))
        except Exception:
            fanout_enabled = True
        if fanout_enabled:
            from cli.tools.federate import federated_search
            return federated_search(query, action, top_k, max_tokens, svc,
                                    finalize, maybe_facts, scope)

    if action == "exact":
        return _exact_search(query, top_k, max_tokens, svc, finalize)

    if action == "files":
        resp = _files_search(query, top_k, svc, finalize)
        if prefetch:
            resp = _append_prefetch(resp, query, top_k, svc)
        return _cap_response(resp)

    if action == "transcript":
        return _transcript_search(query, top_k, max_tokens, svc, finalize)

    if action == "semantic":
        return _semantic_search(query, top_k, max_tokens, svc, finalize, maybe_facts)

    # Default: Code Search
    resp = _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts)
    if prefetch:
        resp = _append_prefetch(resp, query, top_k, svc)
    return _cap_response(resp)


def _exact_search(query, top_k, max_tokens, svc, finalize):
    try:
        pat = re.compile(query)
    except Exception as e:
        return finalize("c3_search", {"action": "exact"},
                        f"[search:exact:error] Invalid regex: {e}", "error")

    tracked = svc.file_memory.list_tracked()

    def _scan_file(rel):
        """Scan a single file for regex matches. Returns (rel, matches) or None."""
        full = Path(svc.project_path) / rel
        if not full.exists():
            return None
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None
        file_matches = []
        for i, line in enumerate(lines):
            if pat.search(line):
                start = max(0, i - 1)
                end = min(len(lines), i + 2)
                for j in range(start, end):
                    marker = ">" if j == i else " "
                    entry = f"{marker}L{j+1}: {lines[j][:200]}"
                    if entry not in file_matches:
                        file_matches.append(entry)
                if file_matches and file_matches[-1] != "---":
                    file_matches.append("---")
        return (rel, file_matches) if file_matches else None

    # Parallel file scanning
    matched_parts = []
    file_count = 0
    total_tokens = 0
    with ThreadPoolExecutor(max_workers=min(len(tracked), 8)) as pool:
        for result in pool.map(_scan_file, tracked):
            if result is None:
                continue
            rel, file_matches = result
            chunk = f"--- {rel} ---\n" + "\n".join(file_matches)
            chunk_tokens = count_tokens(chunk)
            if total_tokens + chunk_tokens > max_tokens and matched_parts:
                break
            file_count += 1
            total_tokens += chunk_tokens
            matched_parts.append(chunk)
            if file_count >= top_k:
                break

    if not matched_parts:
        return finalize("c3_search", {"action": "exact"},
                        f"[search:exact:{query}] 0 results", "0")

    resp = "\n".join(matched_parts)
    return finalize("c3_search", {"action": "exact"}, resp, f"{file_count}f",
                    response_tokens=total_tokens)


def _files_search(query, top_k, svc, finalize):
    res = svc.indexer.search(query, top_k=top_k, include_content=False)
    if not res:
        return finalize("c3_search", {"action": "files"},
                        f"[search:files:{query}] 0 results", "0")
    parts = []
    _MAP_TOKEN_CAP = 600  # cap inline file map to avoid bloating response
    for r in res:
        meta = f"- {r['file']} (L{r['lines']})"
        if r.get('name'):
            meta += f" — contains {r['type']} '{r['name']}'"
        if len(parts) == 0:
            try:
                rel = r['file'].replace("\\", "/")
                watcher_active = (hasattr(svc, "watcher") and svc.watcher._observer.is_alive())
                if not watcher_active and svc.file_memory.needs_update(rel):
                    svc.file_memory.update(rel)
                fmap = svc.file_memory.get_or_build_map(rel)
                if fmap and count_tokens(fmap) <= _MAP_TOKEN_CAP:
                    meta += f"\n  {fmap.replace(chr(10), chr(10) + '  ')}"
                elif fmap:
                    # Truncate large maps: keep first N lines
                    fmap_lines = fmap.split("\n")
                    truncated = []
                    tok = 0
                    for fl in fmap_lines:
                        tok += count_tokens(fl)
                        if tok > _MAP_TOKEN_CAP:
                            break
                        truncated.append(fl)
                    truncated.append("  [map truncated]")
                    meta += f"\n  {chr(10).join(truncated).replace(chr(10), chr(10) + '  ')}"
            except Exception:
                pass
        parts.append(meta)
    return finalize("c3_search", {"action": "files"},
                    "\n".join(parts),
                    f"{len(res)}f")


def _transcript_search(query, top_k, max_tokens, svc, finalize):
    sync_result = svc.convo_store.sync(source="all")
    available = sync_result.get("available_sources", {})
    available_names = [name for name, present in available.items() if present]
    if not available_names:
        resp = ("[transcript:unavailable] No supported transcript sources found for this project. "
                "Supported sources: Claude Code, Gemini CLI, and imported transcripts under .c3/conversations/imports.")
        return finalize("c3_search", {"action": "transcript"}, resp, "unavailable")

    results = svc.convo_store.search(query, limit=max(top_k * 3, top_k))
    if not results:
        srcs = ",".join(sorted(available_names))
        return finalize("c3_search", {"action": "transcript"},
                        f"[transcript:{query}] 0 results sources:{srcs}", "0")
    ratios = show_token_ratios(svc)
    parts = []
    total_tokens = 0
    emitted = 0
    for r in results:
        tokens = int(r.get("tokens", 0) or count_tokens(r.get("text", "")))
        if total_tokens + tokens > max_tokens and parts:
            break
        total_tokens += tokens
        ts_raw = r.get("ts", 0)
        try:
            ts_str = time.strftime("%Y-%m-%d", time.localtime(float(ts_raw))) if ts_raw else ""
        except Exception:
            ts_str = ""
        source = r.get("source") or r.get("turn_source") or "manual"
        role = r.get("role", "")
        session_id = str(r.get("session_id", ""))
        if ratios:
            # Debug view: full session id + relevance score (old header).
            header = f"--- {source}:{session_id} [{ts_str}] role:{role} score:{r['score']}"
        else:
            # Minimal per-item header — full UUIDs and scores were ~40 tokens
            # of boilerplate per result the model does nothing with.
            header = f"--- {source}:{session_id[:8]} {ts_str} {role}".rstrip()
        text = r.get("text", "")
        parts.extend([header, text])
        emitted += 1
        if emitted >= top_k:
            break
    head = f"[transcript:{query}] {emitted}r"
    if ratios:
        head += f",{total_tokens}tok"
    resp = head + "\n" + "\n".join(parts)
    return finalize("c3_search", {"action": "transcript"}, resp, f"{emitted}r")


def _semantic_search(query, top_k, max_tokens, svc, finalize, maybe_facts):
    ei = getattr(svc, "embedding_index", None)
    if not ei or not ei.ready:
        # Fallback to TF-IDF code search when embeddings unavailable
        return _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts)

    results = ei.search(query, top_k=top_k, max_tokens=max_tokens)
    if not results:
        return finalize("c3_search", {"query": query, "action": "semantic"},
                        f"[semantic:{query}] 0 results (falling back to code search)",
                        "0→fallback")

    lines = []
    total_tokens = 0
    for r in results:
        name = f" {r['name']}" if r.get('name') else ""
        ref = f"--- {r['file']}:L{r['lines']}{name} ({r['type']})"
        lines.extend([ref, r['content']] if r.get('content') else [ref])
        total_tokens += r['tokens']

    resp = "\n".join(lines)
    resp += maybe_facts(svc, query, top_k=2)
    return finalize_with_tokens(
        finalize, svc, "c3_search", {"query": query, "action": "semantic"}, resp,
        f"{len(results)}r",
        optimized_tokens=total_tokens, response_tokens=total_tokens)


def _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts):
    results = svc.indexer.search(query, top_k=max(top_k + 1, top_k * 2),
                                 max_tokens=max_tokens, include_content=True)
    if not results:
        return finalize("c3_search", {"query": query}, f"[search:{query}] 0 results", "0")

    best_score = max((r.get("score", 0.0) for r in results), default=0.0)
    if best_score > 0:
        results = [r for r in results if r.get("score", 0.0) >= (best_score * 0.2)]

    deduped = []
    seen = set()
    for r in results:
        key = (r.get("file"), r.get("lines"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
            if len(deduped) >= top_k:
                break

    lines = []
    total_tokens = 0
    for r in deduped:
        name = f" {r['name']}" if r['name'] else ""
        ref = f"--- {r['file']}:L{r['lines']}{name} ({r['type']})"
        lines.extend([ref, r['content']] if r.get('content') else [ref])
        total_tokens += r['tokens']

    resp = "\n".join(lines)
    resp += maybe_facts(svc, query, top_k=2)
    # Structured accounting: the (full-read baseline, returned) pair flows via
    # record_tool_tokens() instead of being regex-scraped from the summary.
    full_tokens = sum(r.get("file_tokens", r["tokens"]) for r in deduped)
    return finalize_with_tokens(
        finalize, svc, "c3_search", {"query": query, "top_k": top_k}, resp,
        f"{len(deduped)}r",
        raw_tokens=full_tokens, optimized_tokens=total_tokens,
        response_tokens=total_tokens)


def _append_prefetch(resp: str, query: str, top_k: int, svc) -> str:
    """Auto-compress top result files in parallel and append structural maps."""
    # Extract file paths from the response
    files = []
    for line in resp.split("\n"):
        if line.startswith("--- ") and ":L" in line:
            # Pattern: --- path/file.py:L10-20 name (type,tok,s=0.123)
            path = line[4:].split(":L")[0].strip()
            if path and path not in files:
                files.append(path)
        elif line.startswith("- ") and " (L" in line:
            # Pattern: - path/file.py (L123)
            path = line[2:].split(" (L")[0].strip()
            if path and path not in files:
                files.append(path)

    cfg = (svc.hybrid_config or {}).get("agent_workflows", {})
    max_files = max(1, int(cfg.get("prefetch_max_files", 3)))
    files = files[:max_files]

    if not files:
        return resp

    maps = {}
    uncached = []

    # Fast path: check in-memory + file_memory map cache before spawning threads
    for fp in files:
        try:
            fmap = svc.file_memory.get_map(fp.replace("\\", "/"))
            if fmap:
                maps[fp] = fmap
                continue
        except Exception:
            pass
        uncached.append(fp)

    def compress_one(fp):
        try:
            full = str(Path(svc.project_path) / fp)
            result = svc.compressor.compress_file(full, "map")
            if isinstance(result, dict) and result.get("compressed"):
                return fp, result["compressed"]
        except Exception:
            pass
        return fp, None

    if uncached:
        with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
            futures = {pool.submit(compress_one, f): f for f in uncached}
            for fut in as_completed(futures):
                fp, compressed = fut.result()
                if compressed:
                    maps[fp] = compressed

    if not maps:
        return resp

    # Budget: prefetch maps share remaining token headroom. Uses fast approximation —
    # exact tiktoken cost is wasted work here (we only need an upper-bound check).
    resp_tokens = _approx_tokens(resp)
    remaining = max(400, _RESPONSE_TOKEN_CAP - resp_tokens)

    prefetch_parts = [f"\n\n--- prefetched maps ({len(maps)} files) ---"]
    used = 0
    for fp in files:
        if fp in maps:
            m = maps[fp]
            m_tok = _approx_tokens(m)
            if used + m_tok > remaining:
                break
            prefetch_parts.append(f"## {fp}\n{m}")
            used += m_tok
    return resp + "\n".join(prefetch_parts)
