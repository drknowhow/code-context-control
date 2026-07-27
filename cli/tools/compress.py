"""c3_compress — Token-efficient file summaries (6 modes: map, dense_map, smart, diff, bug_scan, ast).
Supports comma-separated paths for batch compression with parallel execution."""

import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cli.tools._helpers import finalize_with_tokens, show_token_ratios
from core import count_tokens
from services import access_guard


def _run_memory_mcp_cli(args: list, cwd: str, timeout: int = 30) -> tuple:
    """Run codebase-memory-mcp CLI and return (success, output_or_error)."""
    binary = shutil.which("codebase-memory-mcp")
    if not binary:
        return False, "not_installed"
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [binary] + args,
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=cwd, **kwargs,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True, result.stdout.strip()
        return False, result.stderr.strip() or f"exit {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def _compress_ast(file_path: str, svc, finalize, maybe_facts) -> str:
    """Use codebase-memory-mcp binary for AST knowledge-graph structural analysis."""
    cwd = str(svc.project_path)
    ok, out = _run_memory_mcp_cli(["cli", "get_architecture"], cwd)

    if not ok and out == "not_installed":
        return finalize(
            "c3_compress", {"file_path": file_path, "mode": "ast"},
            "[compress:ast] codebase-memory-mcp not installed.\n"
            "Install (macOS/Linux): curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash\n"
            "Install (Windows): see https://github.com/DeusData/codebase-memory-mcp\n"
            "Tip: use c3_compress(mode='map') for C3's built-in structural analysis.",
            "not_installed",
        )
    if not ok:
        hint = "Run: codebase-memory-mcp index" if not out or "not indexed" in out.lower() else out
        return finalize(
            "c3_compress", {"file_path": file_path, "mode": "ast"},
            f"[compress:ast] {hint}\nTip: say 'Index this project' with codebase-memory-mcp active.",
            "not_indexed",
        )

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return finalize("c3_compress", {"file_path": file_path, "mode": "ast"},
                        f"[compress:ast] Unexpected output — try: codebase-memory-mcp index\n{out[:200]}",
                        "error")

    lines = ["[compress:ast] codebase-memory-mcp knowledge graph"]
    if data.get("languages"):
        lines.append(f"Languages: {', '.join(str(l) for l in data['languages'])}")
    if data.get("entry_points"):
        eps = [str(e) for e in data["entry_points"][:6]]
        lines.append(f"Entry points: {', '.join(eps)}")
    if data.get("packages"):
        pkgs = [str(p) for p in data["packages"][:12]]
        lines.append(f"Packages: {', '.join(pkgs)}")
    if data.get("layers"):
        layers = [str(l) for l in data["layers"][:8]]
        lines.append(f"Layers: {', '.join(layers)}")
    if data.get("hotspots"):
        lines.append("Hotspots (high-churn):")
        for h in data["hotspots"][:6]:
            lines.append(f"  {h}")
    if data.get("clusters"):
        lines.append(f"Clusters: {len(data['clusters'])} functional modules detected")
    if data.get("routes"):
        lines.append(f"Routes: {len(data['routes'])} HTTP endpoints")

    return finalize("c3_compress", {"file_path": file_path, "mode": "ast"},
                    "\n".join(lines), "ast")


def handle_compress(file_path: str, mode: str, svc,
                    finalize, maybe_facts) -> str:
    # Validate mode
    valid_modes = ("map", "dense_map", "smart", "diff", "bug_scan", "ast")
    if mode == "ast":
        return _compress_ast(file_path, svc, finalize, maybe_facts)
    if mode not in valid_modes:
        # Graceful migration for removed modes
        if mode in ("structure", "outline"):
            mode = "map"
        else:
            return finalize("c3_compress", {"file_path": file_path, "mode": mode},
                            f"[compress:error] Unknown mode '{mode}'. Use: {', '.join(valid_modes)}",
                            "error")

    # Batch dispatch: comma-separated paths
    if "," in file_path:
        paths = [p.strip() for p in file_path.split(",") if p.strip()]
        return _compress_batch(paths, mode, svc, finalize, maybe_facts)

    return _compress_single(file_path, mode, svc, finalize, maybe_facts)


def _compress_single(file_path: str, mode: str, svc, finalize, maybe_facts) -> str:
    """Compress a single file."""
    # Access Guard: read verdict up front — covers the map/dense_map paths
    # that never reach compressor.compress_file (docs/access-guard.md §3).
    denial = access_guard.check(file_path, "read", svc.project_path)
    if denial:
        return finalize("c3_compress", {"file_path": file_path, "mode": mode},
                        access_guard.refusal(denial, file_path, "read"),
                        "access-denied")
    full = Path(svc.project_path) / file_path
    if not full.exists():
        full = Path(file_path)

    if mode in ("map", "dense_map"):
        if not full.exists():
            return "[file_map:error] not found"
        rel = str(full.resolve().relative_to(Path(svc.project_path).resolve())).replace("\\", "/")
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
        res = (svc.file_memory.get_or_build_dense_map(rel)
               if mode == "dense_map"
               else svc.file_memory.get_or_build_map(rel))
        # Structured accounting: pass the (full-read baseline, map) pair via
        # record_tool_tokens() instead of a "raw->maptok" summary for the
        # legacy regex fallback to scrape.
        raw_tokens = None
        map_tokens = 0
        try:
            raw_tokens = count_tokens(full.read_text(encoding="utf-8", errors="replace"))
            map_tokens = count_tokens(res)
            summary = mode
        except Exception:
            summary = "mapped"
        return finalize_with_tokens(
            finalize, svc, "c3_compress", {"file_path": file_path, "mode": mode},
            res, summary,
            raw_tokens=raw_tokens, optimized_tokens=map_tokens or None,
            response_tokens=map_tokens)

    try:
        res = svc.compressor.compress_file(str(full), mode)
    except access_guard.AccessDenied as exc:
        return finalize("c3_compress", {"file_path": file_path, "mode": mode},
                        exc.message, "access-denied")
    if "error" in res:
        return f"Error: {res['error']}"
    resp = res['compressed']
    return finalize_with_tokens(
        finalize, svc, "c3_compress", {"file_path": file_path},
        resp + maybe_facts(svc, Path(file_path).name), mode,
        raw_tokens=res.get('original_tokens'),
        optimized_tokens=res.get('compressed_tokens'))


def _compress_batch(paths: list, mode: str, svc, finalize, maybe_facts) -> str:
    """Compress multiple files in parallel, return combined report."""
    max_files = 10
    paths = paths[:max_files]

    results = {}

    def _do_one(fp):
        """Returns (fp, compressed_text, raw_tokens, optimized_tokens, error)."""
        try:
            # Access Guard: per-member read verdict — the S1 refusal becomes
            # this member's batch line; other members are still served.
            denial = access_guard.check(fp, "read", svc.project_path)
            if denial:
                return fp, None, None, None, access_guard.refusal(denial, fp, "read")
            full = Path(svc.project_path) / fp
            if not full.exists():
                full = Path(fp)
            if not full.exists():
                return fp, None, None, None, "not found"

            if mode in ("map", "dense_map"):
                rel = str(full.resolve().relative_to(
                    Path(svc.project_path).resolve())).replace("\\", "/")
                res = (svc.file_memory.get_or_build_dense_map(rel)
                       if mode == "dense_map"
                       else svc.file_memory.get_or_build_map(rel))
                try:
                    raw_tok = count_tokens(full.read_text(encoding="utf-8", errors="replace"))
                    map_tok = count_tokens(res)
                    return fp, res, raw_tok, map_tok, None
                except Exception:
                    return fp, res, None, None, None
            else:
                res = svc.compressor.compress_file(str(full), mode)
                if "error" in res:
                    return fp, None, None, None, res["error"]
                return (fp, res["compressed"], res.get("original_tokens"),
                        res.get("compressed_tokens"), None)
        except Exception as e:
            return fp, None, None, None, str(e)

    with ThreadPoolExecutor(max_workers=min(len(paths), 8)) as pool:
        futures = {pool.submit(_do_one, fp): fp for fp in paths}
        for fut in as_completed(futures):
            fp, compressed, raw_tok, opt_tok, err = fut.result()
            results[fp] = (compressed, raw_tok, opt_tok, err)

    ratios = show_token_ratios(svc)
    parts = []
    total_ok = 0
    total_raw = 0
    total_opt = 0
    measured = 0
    for fp in paths:
        compressed, raw_tok, opt_tok, err = results.get(fp, (None, None, None, "unknown"))
        if compressed:
            tag = ""
            if raw_tok is not None and opt_tok is not None:
                measured += 1
                total_raw += raw_tok
                total_opt += opt_tok
                if ratios:
                    tag = f" ({raw_tok}->{opt_tok}tok)"
            parts.append(f"## {fp}{tag}\n{compressed}")
            total_ok += 1
        else:
            parts.append(f"## {fp} — ERROR: {err}")

    header = f"[compress:batch] {total_ok}/{len(paths)} files ({mode})"
    body = header + "\n\n" + "\n\n".join(parts)
    return finalize_with_tokens(
        finalize, svc, "c3_compress",
        {"file_path": ",".join(paths), "mode": mode, "batch": True},
        body, f"batch {total_ok}/{len(paths)}",
        raw_tokens=total_raw if measured else None,
        optimized_tokens=total_opt if measured else None)
