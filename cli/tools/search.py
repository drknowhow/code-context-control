"""c3_search — Code, file, and transcript discovery."""

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cli.tools._helpers import finalize_with_tokens, show_token_ratios
from core import count_tokens
from services import access_guard
from services.indexer import DEFAULT_CODE_EXTS
from services.lexical_index import Filters
from services.scanner import SKIP_DIRS, iter_files

# Hard cap: responses above this are truncated to avoid filling context.
_RESPONSE_TOKEN_CAP = 2400
# `exact`: after this many matching lines in one file the rest are counted,
# not printed. A single file with hundreds of hits used to consume the whole
# token budget and hide every other file.
_EXACT_MAX_MATCHES_PER_FILE = 20
_RG_TIMEOUT_SECONDS = 60


def _read_denied(path, svc) -> bool:
    """True when Access Guard denies reading `path` (fails closed on errors).

    Used as the per-action pre-filter (docs/access-guard.md §3): denied paths
    are dropped BEFORE dedup/top_k/map-building so they never appear in
    search output (R2 deny-ENUMERATE) and never consume result slots.
    """
    try:
        # Masked paths are NOT dropped: masking exposes a file in transformed
        # form, so it stays discoverable. Its indexed content is the view
        # (services/indexer._masked_content), so snippets are already safe.
        v = access_guard.verdict(path, "read", svc.project_path)
        return v.denial is not None
    except Exception:
        return True


def _with_access_footer(resp: str, svc) -> str:
    """Append the S4/mask limitation notices exactly once (self-gating)."""
    footers = []
    for fn in (access_guard.search_footer, access_guard.mask_footer):
        try:
            footer = fn(svc.project_path)
        except Exception:
            footer = ""
        if footer and resp and footer not in resp:
            footers.append(footer)
    return resp + "\n" + "\n".join(footers) if footers else resp


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
                  scope: str = "", ignore_case: bool = False,
                  path: str = "", lang: str = "", kind: str = "") -> str:
    top_k = max(1, min(int(top_k), 10))
    max_tokens = min(max(200, int(max_tokens)), _RESPONSE_TOKEN_CAP)
    filters = Filters(path=path, lang=lang, kind=kind)

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
                                    finalize, maybe_facts, scope, ignore_case=ignore_case,
                                    path=path, lang=lang, kind=kind)

    if action == "exact":
        resp = _exact_search(query, top_k, max_tokens, svc, finalize,
                             ignore_case=ignore_case, filters=filters)
    elif action == "files":
        resp = _files_search(query, top_k, svc, finalize, filters=filters)
        if prefetch:
            resp = _append_prefetch(resp, query, top_k, svc)
        resp = _cap_response(resp)
    elif action == "transcript":
        resp = _transcript_search(query, top_k, max_tokens, svc, finalize)
    elif action == "semantic":
        resp = _semantic_search(query, top_k, max_tokens, svc, finalize, maybe_facts,
                                filters=filters)
    else:
        # Default: Code Search
        resp = _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts,
                            filters=filters)
        if prefetch:
            resp = _append_prefetch(resp, query, top_k, svc)
        resp = _cap_response(resp)
    # S4: one Access Guard limitation footer per response (self-gated).
    return _with_access_footer(resp, svc)


# ── Search universe ─────────────────────────────────────────────────────────


def _search_manifest(svc) -> list:
    """Relative POSIX paths of every file c3 indexes.

    The same pruned walk ``CodeIndex.build_index`` uses — Access Guard prunes
    denied subtrees inside it and sub-project folders are excluded — so
    ``exact`` and ``files`` see exactly the code-search universe. Before
    2.105.0 ``exact`` iterated ``file_memory.list_tracked()``: the files an
    agent had happened to read, 427 of 513 indexed on this repo.
    """
    project = Path(svc.project_path)
    indexer = getattr(svc, "indexer", None)
    exts = getattr(indexer, "code_exts", None)
    if not isinstance(exts, (set, frozenset)) or not exts:
        exts = DEFAULT_CODE_EXTS
    exclude_parts = None
    prefixes = getattr(indexer, "exclude_prefixes", None)
    is_excluded = getattr(indexer, "_is_excluded", None)
    if isinstance(prefixes, list) and prefixes and callable(is_excluded):
        def exclude_parts(parts, _c=is_excluded, _p=prefixes):
            return _c(parts, _p)
    rels = []
    for fpath in iter_files(project, exts=exts, skip_dirs=set(SKIP_DIRS),
                            exclude_parts=exclude_parts):
        try:
            rels.append(fpath.relative_to(project).as_posix())
        except ValueError:
            continue
    return rels


def _guard_rules_active(svc) -> bool:
    """True when any user access rule (deny/read_only/mask) applies — or when
    that cannot be determined. Fails closed: ripgrep reads raw bytes, so it
    only runs on projects with no rules at all."""
    try:
        return bool(access_guard.has_active_rules(svc.project_path))
    except Exception:
        return True


def _ripgrep_path(svc):
    """Explicit config/env first, then PATH. None disables the fast path."""
    cfg = getattr(svc, "hybrid_config", None) or {}
    for cand in (cfg.get("ripgrep_path"), os.environ.get("C3_RIPGREP")):
        if cand and Path(str(cand)).exists():
            return str(cand)
    return shutil.which("rg")


def _rg_candidate_files(rg: str, project: Path, pattern: str, ignore_case: bool):
    """Files under ``project`` where ripgrep finds ``pattern``; None on any failure.

    Only a pre-filter: every candidate is then re-scanned in Python through
    the guard/mask view, so the output is identical to the pure-Python path —
    ripgrep just shrinks 2000 files to the few that can match.
    """
    cmd = [rg, "--json", "--no-messages", "--no-ignore", "--hidden",
           "--max-count", "1", "--max-filesize", "4M"]
    if ignore_case:
        cmd.append("-i")
    for d in sorted(SKIP_DIRS):
        cmd += ["-g", f"!{d}"]
    cmd += ["-e", pattern, "--", str(project)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=_RG_TIMEOUT_SECONDS, cwd=str(project))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode not in (0, 1):  # 2 = error (unsupported regex syntax, ...)
        return None
    root = project.resolve()
    found = []
    seen = set()
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "match":
            continue
        text = ((obj.get("data") or {}).get("path") or {}).get("text")
        if not text:
            continue
        try:
            rel = Path(text).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if rel not in seen:
            seen.add(rel)
            found.append(rel)
    return found


# ── exact ───────────────────────────────────────────────────────────────────


def _exact_search(query, top_k, max_tokens, svc, finalize, ignore_case: bool = False,
                  filters: Filters | None = None):
    try:
        pat = re.compile(query, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return finalize("c3_search", {"action": "exact"},
                        f"[search:exact:error] Invalid regex: {e}", "error")

    project = Path(svc.project_path)
    manifest = _search_manifest(svc)
    if filters:
        manifest = [rel for rel in manifest if filters.doc_ok(rel) and filters.kind_ok(rel, "")]
    targets = manifest
    rg = _ripgrep_path(svc)
    if rg and manifest and not _guard_rules_active(svc):
        candidates = _rg_candidate_files(rg, project, query, ignore_case)
        if candidates is not None:
            allowed = set(candidates)
            targets = [rel for rel in manifest if rel in allowed]

    def _scan_file(rel):
        """Scan one file for regex matches. Returns (rel, lines) or None."""
        full = project / rel
        if not full.exists():
            return None
        # Mask Guard: regex-scan the VIEW, never the raw bytes. An exact
        # search that matched real content and printed it verbatim would be a
        # complete bypass of the mask (docs/mask-guard.md §6).
        try:
            from services.indexer import _masked_content
            text = _masked_content(full, svc.project_path)
            if text is None:
                return None
            lines = text.splitlines()
        except Exception:
            return None
        file_matches = []
        n_matches = 0
        for i, line in enumerate(lines):
            if not pat.search(line):
                continue
            n_matches += 1
            if n_matches > _EXACT_MAX_MATCHES_PER_FILE:
                continue  # keep counting, stop printing
            start = max(0, i - 1)
            end = min(len(lines), i + 2)
            for j in range(start, end):
                marker = ">" if j == i else " "
                entry = f"{marker}L{j+1}: {lines[j][:200]}"
                if entry not in file_matches:
                    file_matches.append(entry)
            if file_matches and file_matches[-1] != "---":
                file_matches.append("---")
        if n_matches > _EXACT_MAX_MATCHES_PER_FILE:
            file_matches.append(
                f"[+{n_matches - _EXACT_MAX_MATCHES_PER_FILE} more matching lines in this file; "
                "narrow the pattern]")
        return (rel, file_matches) if file_matches else None

    matched_parts = []
    file_count = 0
    total_tokens = 0
    if targets:
        with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as pool:
            for result in pool.map(_scan_file, targets):
                if result is None:
                    continue
                rel, file_matches = result
                if _read_denied(rel, svc):
                    continue  # R2: denied paths never appear in results
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
                        f"[search:exact:{query}] 0 results in {len(manifest)} files", "0")

    resp = "\n".join(matched_parts)
    return finalize("c3_search", {"action": "exact"}, resp, f"{file_count}f",
                    response_tokens=total_tokens)


# ── files ───────────────────────────────────────────────────────────────────


def _rank_filenames(query: str, docs: dict) -> list:
    """Rank indexed paths by how the query names them.

    Tiers: exact basename/stem (0), glob (1), basename substring (2), path
    substring (3). Within a tier shorter paths first. Case-insensitive.
    Before 2.105.0 ``files`` was the content search with the content hidden:
    ``limit`` could not find ``limiter.go`` and ``Invoi`` found nothing.
    """
    q = (query or "").strip()
    ql = q.lower().replace("\\", "/")
    if not ql:
        return []
    is_glob = any(ch in ql for ch in "*?[")
    out = []
    for doc_id, meta in docs.items():
        rel = str(doc_id).replace("\\", "/")
        rl = rel.lower()
        base = rl.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        tier, reason = None, ""
        if is_glob:
            if (fnmatch.fnmatchcase(rl, ql) or fnmatch.fnmatchcase(base, ql)
                    or fnmatch.fnmatchcase(rl, "*/" + ql)):
                tier, reason = 1, f"matches {q}"
        elif base == ql or stem == ql:
            tier, reason = 0, "exact name"
        elif ql in base:
            tier, reason = 2, f"name contains '{q}'"
        elif ql in rl:
            tier, reason = 3, f"path contains '{q}'"
        if tier is None:
            continue
        n_lines = int((meta or {}).get("lines") or 0) if isinstance(meta, dict) else 0
        out.append({"file": str(doc_id), "lines": f"1-{n_lines}", "tier": tier, "reason": reason})
    out.sort(key=lambda r: (r["tier"], len(r["file"]), r["file"].lower()))
    return out


def _indexed_documents(svc):
    indexer = getattr(svc, "indexer", None)
    docs = getattr(indexer, "documents", None)
    if isinstance(docs, dict) and not docs:
        loader = getattr(indexer, "_load_index", None)
        if callable(loader):
            try:
                loader()
                docs = getattr(indexer, "documents", None)
            except Exception:
                docs = None
    return docs if isinstance(docs, dict) and docs else None


def _first_file_map(rel: str, svc) -> str:
    """Structural map of the top hit, capped, or '' when unavailable."""
    _MAP_TOKEN_CAP = 600  # cap inline file map to avoid bloating response
    try:
        rel = rel.replace("\\", "/")
        watcher_active = (hasattr(svc, "watcher") and svc.watcher._observer.is_alive())
        if not watcher_active and svc.file_memory.needs_update(rel):
            svc.file_memory.update(rel)
        fmap = svc.file_memory.get_or_build_map(rel)
        if not fmap:
            return ""
        if count_tokens(fmap) <= _MAP_TOKEN_CAP:
            return f"\n  {fmap.replace(chr(10), chr(10) + '  ')}"
        # Truncate large maps: keep first N lines
        truncated = []
        tok = 0
        for fl in fmap.split("\n"):
            tok += count_tokens(fl)
            if tok > _MAP_TOKEN_CAP:
                break
            truncated.append(fl)
        truncated.append("  [map truncated]")
        return f"\n  {chr(10).join(truncated).replace(chr(10), chr(10) + '  ')}"
    except Exception:
        return ""


def _files_search(query, top_k, svc, finalize, filters: Filters | None = None):
    docs = _indexed_documents(svc)
    ranked = _rank_filenames(query, docs) if docs else []
    if filters:
        ranked = [r for r in ranked if filters.doc_ok(r["file"]) and filters.kind_ok(r["file"], "")]
    # Access Guard pre-filter before any map-building (R2 deny-ENUMERATE).
    ranked = [r for r in ranked if not _read_denied(r["file"], svc)]
    if ranked:
        parts = []
        for r in ranked[:top_k]:
            meta = f"- {r['file']} (L{r['lines']}) — {r['reason']}"
            if not parts:
                meta += _first_file_map(r["file"], svc)
            parts.append(meta)
        return finalize("c3_search", {"action": "files"}, "\n".join(parts),
                        f"{len(parts)}f")

    # No path names the query: fall back to content terms (path tokens are
    # part of the TF-IDF vocabulary, so `docker compose` still finds the file).
    res = svc.indexer.search(query, top_k=top_k, include_content=False,
                             **_filter_kwargs(filters))
    res = [r for r in res if not _read_denied(r.get("file", ""), svc)]
    if not res:
        return finalize("c3_search", {"action": "files"},
                        f"[search:files:{query}] 0 results", "0")
    parts = []
    for r in res:
        meta = f"- {r['file']} (L{r['lines']})"
        if r.get('name'):
            meta += f" — contains {r['type']} '{r['name']}'"
        if not parts:
            meta += _first_file_map(r["file"], svc)
        parts.append(meta)
    return finalize("c3_search", {"action": "files"}, "\n".join(parts), f"{len(res)}f")


# ── transcript / semantic / code ────────────────────────────────────────────


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


def _filter_kwargs(filters: Filters | None) -> dict:
    """``path``/``lang``/``kind`` kwargs for ``CodeIndex.search`` (empty when unset)."""
    if not filters:
        return {}
    return {"path": ",".join(filters.paths), "lang": ",".join(sorted(filters.langs)),
            "kind": ",".join(sorted(filters.kinds))}


def _semantic_search(query, top_k, max_tokens, svc, finalize, maybe_facts,
                     filters: Filters | None = None):
    ei = getattr(svc, "embedding_index", None)
    if not ei or not ei.ready:
        # Fallback to TF-IDF code search when embeddings unavailable
        return _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts, filters=filters)

    # Over-fetch when filtering: the embedding index knows nothing about
    # paths or kinds, so the cut happens here.
    fetch = top_k * 4 if filters else top_k
    results = ei.search(query, top_k=fetch, max_tokens=max_tokens * (4 if filters else 1))
    if filters:
        results = [r for r in results
                   if filters.chunk_ok(r.get("file", ""), r.get("type") or "")][:top_k]
    # Access Guard pre-filter before result assembly (R2 deny-ENUMERATE).
    results = [r for r in results if not _read_denied(r.get("file", ""), svc)]
    if not results:
        # The header used to promise this fallback and then return without
        # it; now it happens.
        fallback = _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts,
                                filters=filters)
        return f"[semantic:{query}] 0 results; code search instead:\n{fallback}"

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


def _code_search(query, top_k, max_tokens, svc, finalize, maybe_facts,
                 filters: Filters | None = None):
    results = svc.indexer.search(query, top_k=max(top_k + 1, top_k * 2),
                                 max_tokens=max_tokens, include_content=True,
                                 **_filter_kwargs(filters))
    # Access Guard pre-filter BEFORE dedup/top_k (R2 deny-ENUMERATE).
    results = [r for r in results if not _read_denied(r.get("file", ""), svc)]
    if not results:
        return finalize("c3_search", {"query": query}, f"[search:{query}] 0 results", "0")

    best_score = max((r.get("score", 0.0) for r in results), default=0.0)
    if best_score > 0:
        # The relative filter drops long-tail noise. It must never drop the
        # definition of the symbol the query named: a windowed class scores a
        # fraction of a small test class that merely mentions it (CodeIndex vs
        # _FakeCodeIndex on this repo: 8.8 vs 58.9).
        results = [r for r in results
                   if r.get("exact_symbol") or r.get("score", 0.0) >= (best_score * 0.2)]

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

    # Defense-in-depth: never prefetch a map for an access-denied path (R2).
    files = [f for f in files if not _read_denied(f, svc)]

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
