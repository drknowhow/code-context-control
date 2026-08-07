"""c3_validate — Deterministic syntax validation using native language parsers."""

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from cli.tools import _grants
from services import access_guard


async def _deep_type_check(full: Path, ext: str, timeout: int = 15) -> list[str]:
    """Run pyright (Python) or tsc (TypeScript) if available. Returns advisory type-warning strings."""
    warnings: list[str] = []

    def _popen_no_window() -> dict:
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        return kw

    def _run_with_timeout(cmd: list[str], cwd: str | None = None):
        """Popen + communicate(timeout); on timeout kill the whole tree.

        subprocess.run(timeout=...) only kills the direct child, orphaning
        `node` spawned by `npx tsc`. Popen + taskkill /F /T reaps the tree.
        Returns (stdout, stderr) — empty strings on timeout/error.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=cwd,
            **_popen_no_window(),
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return out or "", err or ""
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, stdin=subprocess.DEVNULL,
                    **_popen_no_window(),
                )
            else:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return "", ""

    if ext == ".py":
        exe = shutil.which("pyright")
        if not exe:
            return []
        try:
            proc_out, proc_err = await asyncio.to_thread(
                _run_with_timeout,
                [exe, "--outputjson", str(full)],
            )
            try:
                data = __import__("json").loads(proc_out)
                diags = data.get("generalDiagnostics", [])
                errors = [d for d in diags if d.get("severity") == "error"]
                warns  = [d for d in diags if d.get("severity") == "warning"]
                if errors or warns:
                    parts = []
                    if errors:
                        parts.append(f"{len(errors)} type error(s)")
                    if warns:
                        parts.append(f"{len(warns)} warning(s)")
                    warnings.append(f"pyright: {', '.join(parts)}")
                    for d in errors[:3]:
                        ln = (d.get("range") or {}).get("start", {}).get("line", "?")
                        warnings.append(f"  L{ln}: {d.get('message', '')[:80]}")
            except Exception:
                # Fallback: count error lines from plain text output
                lines = (proc_out + proc_err).splitlines()
                errs = [l for l in lines if " - error:" in l or " error " in l.lower()]
                if errs:
                    warnings.append(f"pyright: {len(errs)} issue(s) — run pyright {full.name} for details")
        except Exception:
            pass

    elif ext in (".ts", ".tsx"):
        exe = shutil.which("tsc")
        if not exe:
            exe = shutil.which("npx")
            tsc_args = [exe, "tsc", "--noEmit", "--strict", str(full)] if exe else []
        else:
            tsc_args = [exe, "--noEmit", "--strict", str(full)]
        if not tsc_args:
            return []
        try:
            proc_out, proc_err = await asyncio.to_thread(
                _run_with_timeout,
                tsc_args,
                str(full.parent),
            )
            output = proc_out + proc_err
            # tsc output: "file.ts(10,5): error TS2304: Cannot find..."
            errs = re.findall(r"error TS\d+:", output)
            warns = re.findall(r"warning TS\d+:", output)
            if errs or warns:
                parts = []
                if errs:
                    parts.append(f"{len(errs)} type error(s)")
                if warns:
                    parts.append(f"{len(warns)} warning(s)")
                warnings.append(f"tsc: {', '.join(parts)}")
                for line in output.splitlines()[:3]:
                    if "error TS" in line:
                        warnings.append(f"  {line.strip()[:100]}")
        except Exception:
            pass

    return warnings


async def handle_validate(file_path: str, svc, finalize) -> str:
    # Support comma-separated paths for batch validation
    paths = [p.strip() for p in file_path.split(",") if p.strip()]

    if len(paths) == 1:
        return await _validate_single(paths[0], svc, finalize)

    # Batch: validate all files in parallel
    cfg = (svc.hybrid_config or {}).get("agent_workflows", {})
    max_files = max(1, int(cfg.get("batch_validate_max_files", 10)))
    paths = paths[:max_files]

    tasks = [_validate_one(p, svc) for p in paths]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    pass_count = 0
    fail_count = 0
    skip_count = 0
    lines = []
    for fp, res in zip(paths, results):
        if isinstance(res, Exception):
            lines.append(f"? {fp} — ERROR: {res}")
            skip_count += 1
            continue
        status_word, detail = res
        if status_word == "PASS":
            lines.append(f"\u2713 {fp} \u2014 {detail}")
            pass_count += 1
        elif status_word == "FAIL":
            lines.append(f"\u2717 {fp} \u2014 {detail}")
            fail_count += 1
        else:
            lines.append(f"? {fp} \u2014 {detail}")
            skip_count += 1

    body = "\n".join(lines)
    summary = f"{len(paths)} files: {pass_count} pass, {fail_count} fail"
    if skip_count:
        summary += f", {skip_count} skip"
    return finalize("c3_validate", {"file_path": file_path, "batch": True}, body, summary)


async def _validate_one(file_path: str, svc) -> tuple:
    """Validate a single file and return (status_word, detail_line)."""
    # Access Guard: read verdict (docs/access-guard.md §3) — the S1 refusal
    # becomes the batch line for this member; other members are still served.
    denial = access_guard.check(file_path, "read", svc.project_path)
    if denial and not _grants.allow(svc, denial, tool="c3_validate",
                                    op="read", path=file_path):
        return ("SKIP", access_guard.refusal(denial, file_path, "read"))
    full = Path(svc.project_path) / file_path
    if not full.exists():
        full = Path(file_path)
    if not full.exists():
        return ("SKIP", f"NOT_FOUND: {file_path}")

    ext = full.suffix.lower()
    lang = ext.lstrip('.').upper() if ext else 'unknown'
    hybrid_cfg = svc.hybrid_config or {}
    timeout_seconds = max(1, int(hybrid_cfg.get("validate_timeout_seconds", 35) or 35))

    # Resolve relative path once (used for cache get/put)
    try:
        rel = str(full.resolve().relative_to(Path(svc.project_path).resolve()))
    except Exception:
        rel = file_path

    # Try cached result first
    vcache = getattr(svc, "validation_cache", None)
    cached_hit = False
    result = None
    if vcache:
        try:
            cached = vcache.get(rel)
            if cached is not None:
                result = cached
                cached_hit = True
        except Exception:
            pass

    if not cached_hit:
        try:
            content = await asyncio.to_thread(full.read_text, encoding="utf-8", errors="replace")
        except Exception as e:
            return ("SKIP", f"READ_ERROR {lang}: {e}")

        from services.parser import check_syntax_native_with_timeout

        try:
            result = await asyncio.to_thread(
                check_syntax_native_with_timeout, content, ext, timeout_seconds,
            )
        except Exception:
            result = {"status": "checker_failed", "checker": "native", "errors": [],
                      "detail": "Validation failed unexpectedly."}

        if vcache:
            try:
                st = os.stat(str(full))
                vcache.put(rel, result, st.st_mtime, st.st_size)
            except Exception:
                pass

    outcome = result.get("status", "checker_failed")
    checker = result.get("checker", "native")
    errors = result.get("errors", []) or []
    detail_text = result.get("detail", "")
    cache_tag = " [cached]" if cached_hit else ""

    if outcome == "clean":
        return ("PASS", f"PASS {lang}")
    elif outcome == "syntax_error":
        err_lines = "; ".join(f"L{e['line']}: {e['text']}" for e in errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        return ("FAIL", f"FAIL {lang}: {err_lines}{more}")
    elif outcome == "checker_unavailable":
        return ("SKIP", f"SKIP {lang}: {checker} not found{cache_tag}")
    elif outcome == "checker_timeout":
        return ("SKIP", f"TIMEOUT {lang}: exceeded {timeout_seconds}s{cache_tag}")
    elif outcome == "unsupported":
        return ("SKIP", f"SKIP {lang}: unsupported type{cache_tag}")
    else:
        return ("SKIP", f"ERROR {lang}: {detail_text}{cache_tag}")


async def _validate_single(file_path: str, svc, finalize) -> str:
    """Original single-file validation path."""
    # Access Guard: read verdict, checked before existence (R2 probe parity).
    denial = access_guard.check(file_path, "read", svc.project_path)
    if denial and not _grants.allow(svc, denial, tool="c3_validate",
                                    op="read", path=file_path):
        return finalize("c3_validate", {"file_path": file_path},
                        access_guard.refusal(denial, file_path, "read"),
                        "access-denied")
    full = Path(svc.project_path) / file_path
    if not full.exists():
        full = Path(file_path)
    if not full.exists():
        return f"Error: File not found: {file_path}"

    ext = full.suffix.lower()
    lang = ext.lstrip('.').upper() if ext else 'unknown'
    hybrid_cfg = svc.hybrid_config or {}
    timeout_seconds = max(1, int(hybrid_cfg.get("validate_timeout_seconds", 35) or 35))

    # Try cached result first (populated by background watcher).
    cached_hit = False
    vcache = getattr(svc, "validation_cache", None)
    if vcache:
        try:
            rel = str(full.resolve().relative_to(Path(svc.project_path).resolve()))
            cached = vcache.get(rel)
            if cached is not None:
                result = cached
                cached_hit = True
        except Exception:
            pass

    if not cached_hit:
        try:
            content = await asyncio.to_thread(full.read_text, encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error: Could not read {file_path}: {e}"

        from services.parser import check_syntax_native_with_timeout

        try:
            result = await asyncio.to_thread(
                check_syntax_native_with_timeout, content, ext, timeout_seconds,
            )
        except Exception:
            result = {"status": "checker_failed", "checker": "native", "errors": [],
                      "detail": "Validation failed unexpectedly."}

        # Store result in cache for future calls.
        if vcache:
            try:
                rel = str(full.resolve().relative_to(Path(svc.project_path).resolve()))
                st = os.stat(str(full))
                vcache.put(rel, result, st.st_mtime, st.st_size)
            except Exception:
                pass

    checker = result.get("checker", "native")
    detail = result.get("detail", "")
    errors = result.get("errors", []) or []
    outcome = result.get("status", "checker_failed")

    cache_tag = " [cached]" if cached_hit else ""

    if outcome == "clean":
        # LSP-inspired deep type check (advisory, never blocks PASS)
        type_warns = await _deep_type_check(full, ext)
        if type_warns:
            status = f"PASS {lang} [type-warnings]\n" + "\n".join(type_warns)
        else:
            status = f"PASS {lang}"
        summary = "pass"
    elif outcome == "syntax_error":
        status = f"FAIL {lang}:\n"
        for err in errors[:10]:
            status += f"- L{err['line']}, Col {err['column']}: {err['text']}\n"
        if len(errors) > 10:
            status += f"- ... and {len(errors) - 10} more errors.\n"
        if detail:
            status += f"[detail] {detail}"
        summary = f"validated syntax_error via {checker}"
    elif outcome == "checker_unavailable":
        status = f"SKIP {lang}: {checker} not found on PATH — install it to enable validation. [checker:{checker}]"
        if detail:
            status += f"\n[detail] {detail}"
        summary = f"validated checker_unavailable via {checker}"
    elif outcome == "checker_timeout":
        status = f"TIMEOUT {lang}: validation exceeded {timeout_seconds}s. [checker:{checker}]"
        if detail:
            status += f"\n[detail] {detail}"
        summary = f"validated checker_timeout via {checker}"
    elif outcome == "unsupported":
        status = f"SKIP {lang}: unsupported file type for native validation. [checker:{checker}]"
        if detail:
            status += f"\n[detail] {detail}"
        summary = f"validated unsupported via {checker}"
    else:
        status = f"ERROR {lang}: validator failed. [checker:{checker}]"
        if detail:
            status += f"\n[detail] {detail}"
        summary = f"validated checker_failed via {checker}"

    return finalize("c3_validate", {"file_path": file_path}, status, summary)
