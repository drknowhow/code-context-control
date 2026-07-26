"""c3_agent — Compound workflow agent for multi-step C3 pipelines in a single call."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from core import count_tokens

# ── Progress logging ─────────────────────────────────────────────────────────

def _progress_ticker(svc, message, interval=3.0):
    """Context manager: sends animated dot-progress ticks during a slow step.

    Emits one MCP log notification per interval with rotating dots and elapsed
    time, so the user sees activity instead of silence during long waits.

    Usage:
        _log_progress(svc, "[3/3] Waiting for Gemini...")
        with _progress_ticker(svc, "[3/3] Waiting for Gemini", interval=3.0):
            result = slow_call()
    """
    import threading
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        stop = threading.Event()
        dots_cycle = [".  ", ".. ", "...", " .."]

        def _tick():
            i = 0
            t0 = time.time()
            while not stop.wait(interval):
                elapsed = int(time.time() - t0)
                _log_progress(svc, f"{message}{dots_cycle[i % len(dots_cycle)]} {elapsed}s")
                i += 1

        t = threading.Thread(target=_tick, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=1)

    return _ctx()


def _log_progress(svc, message):
    """Emit workflow progress: MCP log notification + .c3/agent_progress.jsonl file."""
    # Live MCP notification (shown in Claude Code during tool execution)
    cb = getattr(svc, "_agent_progress_cb", None)
    if cb:
        try:
            cb(message)
        except Exception:
            pass
    # Persistent file log (for hub UI / external readers)
    try:
        import json as _json
        progress_file = Path(svc.project_path) / ".c3" / "agent_progress.jsonl"
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(progress_file, "a", encoding='utf-8') as f:
            f.write(_json.dumps({"ts": time.time(), "step": message}) + "\n")
    except Exception:
        pass


def _delegate_failure_reason(steps_log):
    """Extract last delegate failure reason from steps_log, if any."""
    for entry in reversed(steps_log):
        if "(skipped:" in entry or "(timeout" in entry or "(error" in entry or "(idle_timeout" in entry:
            return entry
    return None


# ── Workflow registry ─────────────────────────────────────────────────────────

AGENT_WORKFLOWS = {
    "review_changes": {
        "description": "Review staged/unstaged git changes with structural context",
        "steps": ["git_diff", "compress_changed", "delegate_review"],
    },
    "prepare_context": {
        "description": "Build a compressed context package for specified files/scope",
        "steps": ["resolve_files", "compress_all", "bundle"],
    },
    "investigate": {
        "description": "Investigate an error or issue using search + compress + activity",
        "steps": ["search_code", "compress_hits", "activity_context", "delegate_diagnose"],
    },
    "preflight": {
        "description": "Pre-edit check: validate + compress (parallel) + recent activity for target files",
        "steps": ["validate+compress(parallel)", "activity_summary"],
    },
    "validate_compress": {
        "description": "Validate and compress a file set in one call — both run in parallel",
        "steps": ["validate+compress(parallel)"],
    },
    "codex_review": {
        "description": "Review staged/unstaged git changes using Codex CLI for deeper analysis",
        "steps": ["git_diff", "compress_changed", "codex_delegate_review"],
    },
    "consensus_review": {
        "description": "Cross-model consensus: run Codex + Gemini in parallel, diff findings",
        "steps": ["git_diff", "compress_changed", "parallel(codex_review, gemini_review)", "merge_findings"],
    },
    "codex_test_gen": {
        "description": "Generate and run tests via Codex workspace-write sandbox",
        "steps": ["identify_changes", "compress_targets", "codex_generate_tests"],
    },
    "gemini_review": {
        "description": "Review staged/unstaged git changes using Gemini CLI",
        "steps": ["git_diff", "compress_changed", "gemini_delegate_review"],
    },
    "tri_consensus": {
        "description": "Cloud consensus: Codex + Gemini in parallel, merged findings",
        "steps": ["git_diff", "compress_changed", "parallel(codex, gemini)", "merge_findings"],
    },
}


def handle_agent(workflow: str, scope: str, context: str,
                 svc, finalize) -> str:
    """Execute a compound workflow."""
    if workflow == "available":
        wf_list = "\n".join(
            f"- {k}: {v['description']}" for k, v in AGENT_WORKFLOWS.items()
        )
        return finalize("c3_agent", {"workflow": "available"},
                        f"[agent:available] {len(AGENT_WORKFLOWS)} workflows\n{wf_list}",
                        f"{len(AGENT_WORKFLOWS)} workflows")

    wdef = AGENT_WORKFLOWS.get(workflow)
    if not wdef:
        available = ", ".join(AGENT_WORKFLOWS.keys())
        return finalize("c3_agent", {"workflow": workflow},
                        f"[agent:error] Unknown workflow '{workflow}'. Available: {available}",
                        "error")

    t0 = time.time()
    steps_log = []

    try:
        if workflow == "review_changes":
            result = _wf_review_changes(scope, context, svc, steps_log)
        elif workflow == "codex_review":
            result = _wf_codex_review(scope, context, svc, steps_log)
        elif workflow == "consensus_review":
            result = _wf_consensus_review(scope, context, svc, steps_log)
        elif workflow == "codex_test_gen":
            result = _wf_codex_test_gen(scope, context, svc, steps_log)
        elif workflow == "gemini_review":
            result = _wf_gemini_review(scope, context, svc, steps_log)
        elif workflow == "tri_consensus":
            result = _wf_tri_consensus(scope, context, svc, steps_log)
        elif workflow == "prepare_context":
            result = _wf_prepare_context(scope, context, svc, steps_log)
        elif workflow == "investigate":
            result = _wf_investigate(scope, context, svc, steps_log)
        elif workflow == "preflight":
            result = _wf_preflight(scope, context, svc, steps_log)
        elif workflow == "validate_compress":
            result = _wf_validate_compress(scope, context, svc, steps_log)
        else:
            result = f"[agent:error] Workflow '{workflow}' not implemented"
    except Exception as e:
        result = f"[agent:error] {workflow} failed: {e}"

    elapsed = time.time() - t0
    steps_summary = " → ".join(steps_log) if steps_log else "no steps"
    header = f"[agent:{workflow}] {elapsed:.1f}s | {steps_summary}\n\n"

    return finalize("c3_agent", {"workflow": workflow, "scope": scope},
                    header + result,
                    f"{workflow} {elapsed:.1f}s")


# ── Workflow implementations ──────────────────────────────────────────────────

def _wf_review_changes(scope, context, svc, steps_log):
    """Review git changes: diff → compress changed files → delegate review."""

    # Workflow-level deadline: abort if total time exceeds this.
    _workflow_deadline = time.time() + 90  # 90 seconds max for entire workflow

    def _check_deadline(step_name: str):
        if time.time() > _workflow_deadline:
            raise TimeoutError(f"[{step_name}] workflow deadline exceeded (90s)")

    try:
        return _wf_review_changes_inner(scope, context, svc, steps_log,
                                        _workflow_deadline, _check_deadline)
    except TimeoutError as e:
        _log_progress(svc, f"[timeout] {e}")
        return f"[workflow:timeout] {e}\nSteps completed: {' → '.join(steps_log)}"


def _wf_review_changes_inner(scope, context, svc, steps_log,
                              _workflow_deadline, _check_deadline):
    import subprocess

    # Step 1: Get git diff
    _log_progress(svc, "[1/3] Running git diff...")
    diff_cmd = ["git", "diff"]
    if scope == "staged":
        diff_cmd.append("--cached")
    elif scope == "HEAD":
        diff_cmd.extend(["HEAD~1", "HEAD"])
    # default: unstaged changes

    try:
        r = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL,
                           cwd=svc.project_path)
        diff_text = r.stdout.strip()
    except Exception as e:
        return f"[step:git_diff] Failed: {e}"

    if not diff_text:
        return "[step:git_diff] No changes found"
    steps_log.append(f"diff({len(diff_text)}ch)")

    # Step 2: Extract changed file paths
    changed_files = set()
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                changed_files.add(parts[-1])

    # Start Gemini subprocess early so its ~9s MCP startup overlaps with compress
    dcfg = svc.delegate_config or {}
    gemini_proc = None
    if dcfg.get("enabled", True) and dcfg.get("gemini_enabled", False):
        import cli.tools.delegate as _dm
        from cli.tools.delegate import GEMINI_MODELS, _is_gemini_on_path, _start_gemini_early
        # Prefer cached availability (avoids shutil.which on slow network-path systems)
        _gem_ok = (_dm._gemini_available is True) or (
            _dm._gemini_available is None and _is_gemini_on_path()
        )
        if _gem_ok and _dm._gemini_available is not False:
            gdef = GEMINI_MODELS.get("review", GEMINI_MODELS.get("ask", {}))
            model = dcfg.get("gemini_default_model") or gdef.get("model", "gemini-2.5-flash")
            _log_progress(svc, f"[2/3] Compressing {len(changed_files)} file(s) + starting Gemini...")
            gemini_proc = _start_gemini_early(
                model=model,
                timeout=int(dcfg.get("gemini_timeout", 45)),
                idle_timeout=15,
                cwd=str(svc.project_path),
            )
        else:
            _log_progress(svc, "[2/3] Compressing changed files...")
    else:
        _log_progress(svc, "[2/3] Compressing changed files...")

    with _progress_ticker(svc, "[2/3] Compressing", interval=1.5):
        maps = _parallel_compress(list(changed_files)[:5], svc, steps_log)

    _check_deadline("compress")

    # Step 3: Feed prompt to already-running Gemini (MCP startup overlapped with compress)
    # Clamp delegate timeout to remaining workflow time to prevent overshoot.
    _remaining = max(10, int(_workflow_deadline - time.time()))
    review_context = f"Git diff:\n{diff_text[:3000]}\n\nStructural maps:\n{maps}"
    if context:
        review_context += f"\n\nUser context: {context}"

    if gemini_proc is not None:
        _log_progress(svc, "[3/3] Sending prompt to Gemini (MCP ready)...")
        from cli.tools.delegate import _finish_gemini_early
        with _progress_ticker(svc, "[3/3] Waiting for Gemini", interval=3.0):
            output, ok, _ = _finish_gemini_early(
                gemini_proc, "Review these changes for issues", review_context,
                timeout=min(int(dcfg.get("gemini_timeout", 45)), _remaining),
                idle_timeout=15,
            )
        if ok and output:
            steps_log.append("gemini(review,early_start)")
            delegate_result = output
        else:
            if output and output.startswith("["):
                steps_log.append(f"gemini({output.split(']')[0]}])".replace("[", ""))
            else:
                steps_log.append("gemini(error:no_output)")
            delegate_result = ""
    else:
        _log_progress(svc, "[3/3] Delegating review to Gemini...")
        with _progress_ticker(svc, "[3/3] Waiting for Gemini", interval=3.0):
            delegate_result = _try_delegate(svc, "review", "Review these changes for issues",
                                            review_context, steps_log, prefer_gemini=True)

    parts = [f"--- Changed files ({len(changed_files)}) ---"]
    for f in sorted(changed_files):
        parts.append(f"  {f}")
    parts.append(f"\n--- Diff ({count_tokens(diff_text)}tok) ---")
    # Truncate diff for response
    if count_tokens(diff_text) > 800:
        parts.append(diff_text[:2000] + "\n... [truncated]")
    else:
        parts.append(diff_text)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")
    if delegate_result:
        parts.append(f"\n--- AI Review ---\n{delegate_result}")
    else:
        reason = _delegate_failure_reason(steps_log)
        parts.append(f"\n--- AI Review skipped: {reason or 'no backend available'} ---")

    return "\n".join(parts)


def _wf_codex_review(scope, context, svc, steps_log):
    """Review git changes using Codex CLI for deeper analysis."""
    import subprocess

    # Step 1: Get git diff (same as review_changes)
    _log_progress(svc, "[1/3] Running git diff...")
    diff_cmd = ["git", "diff"]
    if scope == "staged":
        diff_cmd.append("--cached")
    elif scope == "HEAD":
        diff_cmd.extend(["HEAD~1", "HEAD"])

    try:
        r = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10,
                           cwd=svc.project_path)
        diff_text = r.stdout.strip()
    except Exception as e:
        return f"[step:git_diff] Failed: {e}"

    if not diff_text:
        return "[step:git_diff] No changes found"
    steps_log.append(f"diff({len(diff_text)}ch)")

    # Step 2: Compress changed files
    changed_files = set()
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                changed_files.add(parts[-1])

    _log_progress(svc, f"[2/3] Compressing {len(changed_files)} changed files...")
    with _progress_ticker(svc, "[2/3] Compressing", interval=1.5):
        maps = _parallel_compress(list(changed_files)[:5], svc, steps_log)

    # Step 3: Delegate to Codex (prefer_codex=True)
    _log_progress(svc, "[3/3] Delegating review to Codex...")
    review_context = f"Git diff:\n{diff_text[:6000]}\n\nStructural maps:\n{maps}"
    if context:
        review_context += f"\n\nUser context: {context}"

    with _progress_ticker(svc, "[3/3] Waiting for Codex", interval=3.0):
        delegate_result = _try_delegate(svc, "review", "Review these changes for issues, regressions, and missing tests",
                                        review_context, steps_log, prefer_codex=True)

    parts = [f"--- Changed files ({len(changed_files)}) ---"]
    for f in sorted(changed_files):
        parts.append(f"  {f}")
    parts.append(f"\n--- Diff ({count_tokens(diff_text)}tok) ---")
    if count_tokens(diff_text) > 800:
        parts.append(diff_text[:2000] + "\n... [truncated]")
    else:
        parts.append(diff_text)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")
    if delegate_result:
        parts.append(f"\n--- Codex Review ---\n{delegate_result}")
    else:
        reason = _delegate_failure_reason(steps_log)
        parts.append(f"\n--- Codex Review skipped: {reason or 'no backend available'} ---")

    return "\n".join(parts)


def _wf_consensus_review(scope, context, svc, steps_log):
    """Cross-model consensus: run Ollama + Codex review in parallel, merge findings."""
    import subprocess

    # Step 1: Get git diff
    _log_progress(svc, "[1/4] Running git diff...")
    diff_cmd = ["git", "diff"]
    if scope == "staged":
        diff_cmd.append("--cached")
    elif scope == "HEAD":
        diff_cmd.extend(["HEAD~1", "HEAD"])

    try:
        r = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10,
                           cwd=svc.project_path)
        diff_text = r.stdout.strip()
    except Exception as e:
        return f"[step:git_diff] Failed: {e}"

    if not diff_text:
        return "[step:git_diff] No changes found"
    steps_log.append(f"diff({len(diff_text)}ch)")

    # Step 2: Compress changed files
    changed_files = set()
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                changed_files.add(parts[-1])
    _log_progress(svc, f"[2/4] Compressing {len(changed_files)} changed files...")
    with _progress_ticker(svc, "[2/4] Compressing", interval=1.5):
        maps = _parallel_compress(list(changed_files)[:5], svc, steps_log)

    review_context = f"Git diff:\n{diff_text[:4000]}\n\nStructural maps:\n{maps}"
    if context:
        review_context += f"\n\nUser context: {context}"
    review_task = "Review these changes for bugs, regressions, and missing tests. Be concise."

    # Step 3: Run Codex and Gemini in parallel
    _log_progress(svc, "[3/4] Delegating to Codex + Gemini in parallel...")
    codex_result = ""
    gemini_result = ""
    codex_steps = []
    gemini_steps = []

    def _run_codex():
        return _try_delegate(svc, "review", review_task, review_context, codex_steps, prefer_codex=True)

    def _run_gemini():
        return _try_delegate(svc, "review", review_task, review_context, gemini_steps, prefer_gemini=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_codex = pool.submit(_run_codex)
        fut_gemini = pool.submit(_run_gemini)
        try:
            codex_result = fut_codex.result(timeout=100)
            steps_log.extend(codex_steps)
        except Exception:
            codex_result = "[timeout/error]"
        try:
            gemini_result = fut_gemini.result(timeout=100)
            steps_log.extend(gemini_steps)
        except Exception:
            gemini_result = "[timeout/error]"

    # Step 4: Merge findings
    parts = [f"--- Changed files ({len(changed_files)}) ---"]
    for f in sorted(changed_files):
        parts.append(f"  {f}")
    parts.append(f"\n--- Diff ({count_tokens(diff_text)}tok) ---")
    if count_tokens(diff_text) > 800:
        parts.append(diff_text[:2000] + "\n... [truncated]")
    else:
        parts.append(diff_text)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")

    parts.append("\n" + "=" * 60)
    parts.append("CONSENSUS REVIEW — Cloud backend analyses")
    parts.append("=" * 60)

    backends = [
        ("Codex (cloud/GPT)", codex_result),
        ("Gemini (cloud/Google)", gemini_result),
    ]
    available_count = 0
    for label, result in backends:
        if result and result != "[timeout/error]":
            parts.append(f"\n--- {label} ---\n{result}")
            available_count += 1
        else:
            parts.append(f"\n--- {label} ---\n[unavailable or no response]")

    _log_progress(svc, "[4/4] Merging consensus results...")
    if available_count == 2:
        parts.append("\n--- Consensus ---")
        parts.append("Both models provided reviews. Compare findings above for agreement/divergence.")
    elif available_count == 1:
        parts.append("\n--- Note: Only one backend responded ---")
    else:
        reasons = [_delegate_failure_reason(codex_steps), _delegate_failure_reason(gemini_steps)]
        detail = ", ".join(r for r in reasons if r) or "check delegate configuration"
        parts.append(f"\n--- No backends available: {detail} ---")

    return "\n".join(parts)


def _wf_codex_test_gen(scope, context, svc, steps_log):
    """Generate tests via Codex CLI with workspace-write sandbox."""
    from cli.tools.delegate import _codex_available, _run_codex, check_codex

    # Verify Codex
    if _codex_available is None:
        check_codex()
    from cli.tools.delegate import _codex_available as avail
    if not avail:
        return "[codex_test_gen:error] Codex CLI not available"

    dcfg = svc.delegate_config or {}
    if not dcfg.get("codex_enabled", False):
        return "[codex_test_gen:error] Codex not enabled in config"

    # Resolve target files from scope
    files = []
    if scope and ("," in scope or scope.endswith((".py", ".js", ".ts", ".go", ".rs", ".java"))):
        files = [f.strip() for f in scope.split(",") if f.strip()]
    else:
        # Use edit ledger to find recently edited files
        try:
            recent = svc.edit_ledger.get_history(limit=5)
            files = list(dict.fromkeys(e.get("file", "") for e in recent if e.get("file")))[:5]
        except Exception:
            pass

    if not files:
        return "[codex_test_gen:error] No target files found. Provide file paths in scope or edit files first."

    steps_log.append(f"targets({len(files)})")

    # Compress targets for context
    maps = _parallel_compress(files, svc, steps_log)

    # Build test generation prompt
    file_list = ", ".join(files)
    task = (
        f"Generate focused unit tests for the following files: {file_list}. "
        "Write tests that maximize defect coverage with minimal redundancy. "
        "Use the project's existing test framework and conventions. "
        "Write the test files to disk."
    )
    gen_context = f"Target files:\n{file_list}\n\nStructural maps:\n{maps}"
    if context:
        gen_context += f"\n\nAdditional context: {context}"

    model = dcfg.get("codex_default_model", "")
    timeout = int(dcfg.get("codex_timeout", 180))

    steps_log.append("codex_generate")
    output, ok = _run_codex(
        task=task, context=gen_context,
        model=model, sandbox="workspace-write",
        reasoning="high", timeout=timeout,
        cwd=str(svc.project_path),
    )

    parts = [f"--- Test generation for {len(files)} file(s) ---"]
    parts.append(f"Files: {file_list}")
    parts.append("Sandbox: workspace-write")
    parts.append(f"Model: {model or 'cli-default'}")
    if ok:
        parts.append(f"\n--- Codex output ---\n{output}")
    else:
        parts.append(f"\n--- Error ---\n{output}")

    return "\n".join(parts)


def _wf_gemini_review(scope, context, svc, steps_log):
    """Review git changes using Gemini CLI."""
    import subprocess

    _log_progress(svc, "[1/3] Running git diff...")
    diff_cmd = ["git", "diff"]
    if scope == "staged":
        diff_cmd.append("--cached")
    elif scope == "HEAD":
        diff_cmd.extend(["HEAD~1", "HEAD"])

    try:
        r = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10,
                           cwd=svc.project_path)
        diff_text = r.stdout.strip()
    except Exception as e:
        return f"[step:git_diff] Failed: {e}"

    if not diff_text:
        return "[step:git_diff] No changes found"
    steps_log.append(f"diff({len(diff_text)}ch)")

    changed_files = set()
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                changed_files.add(parts[-1])

    _log_progress(svc, f"[2/3] Compressing {len(changed_files)} changed files...")
    with _progress_ticker(svc, "[2/3] Compressing", interval=1.5):
        maps = _parallel_compress(list(changed_files)[:5], svc, steps_log)

    review_context = f"Git diff:\n{diff_text[:6000]}\n\nStructural maps:\n{maps}"
    if context:
        review_context += f"\n\nUser context: {context}"

    _log_progress(svc, "[3/3] Delegating review to Gemini...")
    with _progress_ticker(svc, "[3/3] Waiting for Gemini", interval=3.0):
        delegate_result = _try_delegate(svc, "review", "Review these changes for issues, regressions, and missing tests",
                                        review_context, steps_log, prefer_gemini=True)

    parts = [f"--- Changed files ({len(changed_files)}) ---"]
    for f in sorted(changed_files):
        parts.append(f"  {f}")
    parts.append(f"\n--- Diff ({count_tokens(diff_text)}tok) ---")
    if count_tokens(diff_text) > 800:
        parts.append(diff_text[:2000] + "\n... [truncated]")
    else:
        parts.append(diff_text)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")
    if delegate_result:
        parts.append(f"\n--- Gemini Review ---\n{delegate_result}")
    else:
        reason = _delegate_failure_reason(steps_log)
        parts.append(f"\n--- Gemini Review skipped: {reason or 'no backend available'} ---")

    return "\n".join(parts)


def _wf_tri_consensus(scope, context, svc, steps_log):
    """Three-way consensus: Ollama + Codex + Gemini in parallel, merge findings."""
    import subprocess

    _log_progress(svc, "[1/4] Running git diff...")
    diff_cmd = ["git", "diff"]
    if scope == "staged":
        diff_cmd.append("--cached")
    elif scope == "HEAD":
        diff_cmd.extend(["HEAD~1", "HEAD"])

    try:
        r = subprocess.run(diff_cmd, capture_output=True, text=True, timeout=10,
                           cwd=svc.project_path)
        diff_text = r.stdout.strip()
    except Exception as e:
        return f"[step:git_diff] Failed: {e}"

    if not diff_text:
        return "[step:git_diff] No changes found"
    steps_log.append(f"diff({len(diff_text)}ch)")

    changed_files = set()
    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                changed_files.add(parts[-1])
    _log_progress(svc, f"[2/4] Compressing {len(changed_files)} changed files...")
    with _progress_ticker(svc, "[2/4] Compressing", interval=1.5):
        maps = _parallel_compress(list(changed_files)[:5], svc, steps_log)

    review_context = f"Git diff:\n{diff_text[:4000]}\n\nStructural maps:\n{maps}"
    if context:
        review_context += f"\n\nUser context: {context}"
    review_task = "Review these changes for bugs, regressions, and missing tests. Be concise."

    # Run cloud backends in parallel (Codex + Gemini)
    _log_progress(svc, "[3/4] Delegating to Codex + Gemini in parallel...")
    codex_steps = []
    gemini_steps = []

    def _run_codex():
        return _try_delegate(svc, "review", review_task, review_context, codex_steps, prefer_codex=True)

    def _run_gemini():
        return _try_delegate(svc, "review", review_task, review_context, gemini_steps, prefer_gemini=True)

    codex_result = gemini_result = ""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_codex = pool.submit(_run_codex)
        fut_gemini = pool.submit(_run_gemini)
        try:
            codex_result = fut_codex.result(timeout=100)
            steps_log.extend(codex_steps)
        except Exception:
            codex_result = "[timeout/error]"
        try:
            gemini_result = fut_gemini.result(timeout=100)
            steps_log.extend(gemini_steps)
        except Exception:
            gemini_result = "[timeout/error]"

    # Merge findings
    parts = [f"--- Changed files ({len(changed_files)}) ---"]
    for f in sorted(changed_files):
        parts.append(f"  {f}")
    parts.append(f"\n--- Diff ({count_tokens(diff_text)}tok) ---")
    if count_tokens(diff_text) > 800:
        parts.append(diff_text[:2000] + "\n... [truncated]")
    else:
        parts.append(diff_text)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")

    parts.append("\n" + "=" * 60)
    parts.append("CONSENSUS REVIEW -- Cloud backend analyses")
    parts.append("=" * 60)

    backends = [
        ("Codex (cloud/GPT)", codex_result),
        ("Gemini (cloud/Google)", gemini_result),
    ]
    available_count = 0
    for label, result in backends:
        if result and result != "[timeout/error]":
            parts.append(f"\n--- {label} ---\n{result}")
            available_count += 1
        else:
            parts.append(f"\n--- {label} ---\n[unavailable or no response]")

    _log_progress(svc, "[4/4] Merging consensus results...")
    parts.append(f"\n--- Consensus ({available_count}/2 backends responded) ---")
    if available_count == 2:
        parts.append("Both models provided reviews. Compare findings above for agreement/divergence.")
    elif available_count == 1:
        parts.append("Only one backend responded. Enable the other for true consensus.")
    else:
        reasons = [_delegate_failure_reason(codex_steps), _delegate_failure_reason(gemini_steps)]
        detail = ", ".join(r for r in reasons if r) or "check delegate configuration"
        parts.append(f"No backends available: {detail}")

    return "\n".join(parts)


def _wf_prepare_context(scope, context, svc, steps_log):
    """Build compressed context package for files matching scope."""
    # scope can be: comma-separated file paths, or a search query
    files = []
    if "," in scope or scope.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java")):
        # Treat as file paths
        files = [f.strip() for f in scope.split(",") if f.strip()]
        steps_log.append(f"resolve({len(files)} explicit)")
    else:
        # Treat as search query
        results = svc.indexer.search(scope, top_k=5, include_content=False)
        files = list(dict.fromkeys(r["file"] for r in results))[:5]
        steps_log.append(f"search({len(files)} hits)")

    if not files:
        return "[step:resolve] No files found"

    maps = _parallel_compress(files, svc, steps_log)
    return f"--- Context package ({len(files)} files) ---\n{maps}"


def _wf_investigate(scope, context, svc, steps_log):
    """Investigate an error: search → compress → activity → diagnose."""
    query = scope or context or "error"

    # Step 1: Search for relevant code
    _log_progress(svc, f"[1/4] Searching for '{query[:40]}'...")
    results = svc.indexer.search(query, top_k=5, include_content=True, max_tokens=600)
    steps_log.append(f"search({len(results)}r)")

    if not results:
        return f"[step:search] No results for '{query}'"

    # Step 2: Compress top hit files in parallel
    hit_files = list(dict.fromkeys(r["file"] for r in results))[:3]
    _log_progress(svc, f"[2/4] Compressing {len(hit_files)} files...")
    with _progress_ticker(svc, "[2/4] Compressing", interval=1.5):
        maps = _parallel_compress(hit_files, svc, steps_log)

    # Step 3: Recent activity
    activity = ""
    try:
        recent = svc.activity_log.get_recent(limit=10)
        if recent:
            activity = "\n".join(
                f"[{e.get('timestamp', '').split('T')[-1][:8]}] {e.get('tool', '')} → {e.get('summary', '')[:60]}"
                for e in reversed(recent)
            )
            steps_log.append(f"activity({len(recent)})")
    except Exception:
        pass

    # Step 4: Delegate diagnosis if available
    diag_context = f"Query: {query}\n\nSearch results:\n"
    for r in results[:3]:
        diag_context += f"  {r['file']}:L{r['lines']} ({r['type']})\n"
    if maps:
        diag_context += f"\nMaps:\n{maps}"
    if activity:
        diag_context += f"\nRecent activity:\n{activity}"
    if context:
        diag_context += f"\nUser context: {context}"

    _log_progress(svc, "[4/4] Delegating diagnosis to Gemini...")
    with _progress_ticker(svc, "[4/4] Waiting for Gemini", interval=3.0):
        delegate_result = _try_delegate(svc, "diagnose", f"Investigate: {query}",
                                        diag_context, steps_log, prefer_gemini=True)

    parts = [f"--- Search hits for '{query}' ---"]
    for r in results[:5]:
        name = f" {r['name']}" if r.get('name') else ""
        parts.append(f"  {r['file']}:L{r['lines']}{name} ({r['type']}, s={r.get('score', 0):.3f})")
        if r.get("content"):
            # Show first few lines of content
            content_lines = r["content"].split("\n")[:5]
            for cl in content_lines:
                parts.append(f"    {cl}")
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")
    if activity:
        parts.append(f"\n--- Recent activity ---\n{activity}")
    if delegate_result:
        parts.append(f"\n--- AI Diagnosis ---\n{delegate_result}")

    return "\n".join(parts)


def _wf_preflight(scope, context, svc, steps_log):
    """Pre-edit check: validate + compress (parallel) + recent activity."""
    files = [f.strip() for f in scope.split(",") if f.strip()]
    if not files:
        return "[step:resolve] No files specified"

    # Step 1: Validate AND compress all files concurrently
    val_results, maps = _parallel_validate_and_compress(files, svc, steps_log)

    # Step 2: Recent activity summary
    activity = ""
    try:
        recent = svc.activity_log.get_recent(limit=5)
        if recent:
            activity = "\n".join(
                f"[{e.get('timestamp', '').split('T')[-1][:8]}] {e.get('tool', '')} → {e.get('summary', '')[:60]}"
                for e in reversed(recent)
            )
            steps_log.append(f"activity({len(recent)})")
    except Exception:
        pass

    parts = [f"--- Preflight ({len(files)} files) ---"]
    for fp in files:
        status, detail = val_results.get(fp, ("UNKNOWN", ""))
        marker = "✓" if status == "PASS" else "✗" if status == "FAIL" else "?"
        line = f"  {marker} {fp} — {status}"
        if detail:
            line += f": {detail}"
        parts.append(line)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")
    if activity:
        parts.append(f"\n--- Recent activity ---\n{activity}")

    return "\n".join(parts)


def _wf_validate_compress(scope, context, svc, steps_log):
    """Validate and compress a file set in one call — both run in parallel."""
    files = [f.strip() for f in scope.split(",") if f.strip()]
    if not files:
        return "[step:resolve] No files specified"

    val_results, maps = _parallel_validate_and_compress(files, svc, steps_log)

    parts = [f"--- Validate + Compress ({len(files)} files) ---"]
    for fp in files:
        status, detail = val_results.get(fp, ("UNKNOWN", ""))
        marker = "✓" if status == "PASS" else "✗" if status == "FAIL" else "?"
        line = f"  {marker} {fp} — {status}"
        if detail:
            line += f": {detail}"
        parts.append(line)
    if maps:
        parts.append(f"\n--- Structural maps ---\n{maps}")

    return "\n".join(parts)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parallel_compress(files: list, svc, steps_log: list,
                       per_file_timeout: float = 10.0) -> str:
    """Compress multiple files in parallel, return combined maps."""
    if not files:
        return ""

    maps = {}

    def compress_one(fp):
        full_path = str(Path(svc.project_path) / fp)
        try:
            res = svc.compressor.compress_file(full_path, "map")
            if isinstance(res, dict) and res.get("compressed"):
                return fp, res["compressed"]
        except Exception:
            pass
        return fp, None

    with ThreadPoolExecutor(max_workers=min(len(files), 4)) as pool:
        futures = {pool.submit(compress_one, f): f for f in files}
        for fut in as_completed(futures, timeout=per_file_timeout * len(files)):
            try:
                fp, compressed = fut.result(timeout=per_file_timeout)
            except (FuturesTimeout, Exception):
                continue
            if compressed:
                maps[fp] = compressed

    steps_log.append(f"compress({len(maps)}/{len(files)})")

    parts = []
    for fp in files:
        if fp in maps:
            parts.append(f"## {fp}\n{maps[fp]}")
    return "\n\n".join(parts)


def _parallel_validate_and_compress(files: list, svc, steps_log: list):
    """Run validation and compression for all files concurrently.
    Returns (val_results_dict, maps_str)."""
    from services.parser import check_syntax_native_with_timeout
    hybrid_cfg = svc.hybrid_config or {}
    timeout_s = max(1, int(hybrid_cfg.get("validate_timeout_seconds", 35) or 35))

    val_results = {}
    compress_results = {}

    def validate_one(fp):
        full = Path(svc.project_path) / fp
        if not full.exists():
            return "validate", fp, "NOT_FOUND", ""
        ext = full.suffix.lower()
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
            result = check_syntax_native_with_timeout(content, ext, timeout_s)
            status = result.get("status", "checker_failed")
            errors = result.get("errors", [])
            if status == "clean":
                return "validate", fp, "PASS", ""
            elif status == "syntax_error":
                err_lines = "; ".join(f"L{e['line']}: {e['text']}" for e in errors[:3])
                return "validate", fp, "FAIL", err_lines
            else:
                return "validate", fp, status.upper(), result.get("detail", "")
        except Exception as e:
            return "validate", fp, "ERROR", str(e)

    def compress_one(fp):
        full_path = str(Path(svc.project_path) / fp)
        try:
            res = svc.compressor.compress_file(full_path, "map")
            if isinstance(res, dict) and res.get("compressed"):
                return "compress", fp, res["compressed"]
        except Exception:
            pass
        return "compress", fp, None

    # Submit ALL tasks (validate + compress) into one pool
    with ThreadPoolExecutor(max_workers=min(len(files) * 2, 8)) as pool:
        futures = []
        for fp in files:
            futures.append(pool.submit(validate_one, fp))
            futures.append(pool.submit(compress_one, fp))

        for fut in as_completed(futures):
            result = fut.result()
            if result[0] == "validate":
                _, fp, status, detail = result
                val_results[fp] = (status, detail)
            else:
                _, fp, compressed = result
                if compressed:
                    compress_results[fp] = compressed

    steps_log.append(f"validate+compress({len(files)}files,{len(compress_results)}maps)")

    map_parts = []
    for fp in files:
        if fp in compress_results:
            map_parts.append(f"## {fp}\n{compress_results[fp]}")
    maps_str = "\n\n".join(map_parts)

    return val_results, maps_str


def _try_delegate(svc, task_type: str, task: str, context: str, steps_log: list,
                  prefer_codex: bool = False, prefer_gemini: bool = False) -> str:
    """Try to delegate to a specific backend. Returns empty string if unavailable.

    When prefer_codex or prefer_gemini is set, ONLY that backend is tried —
    no fallback to Ollama. This prevents parallel consensus workflows from
    running duplicate Ollama calls when a preferred backend is unavailable.
    """
    try:
        dcfg = svc.delegate_config or {}
        if not dcfg.get("enabled", True):
            steps_log.append("delegate(skipped:disabled)")
            return ""

        # --- Codex-only path (no fallthrough) ------------------------------
        if prefer_codex:
            if not dcfg.get("codex_enabled", False):
                steps_log.append("codex(skipped:disabled)")
                return ""
            import cli.tools.delegate as _dm
            from cli.tools.delegate import CODEX_MODELS, _is_codex_on_path, _run_codex
            if not _is_codex_on_path():
                steps_log.append("codex(skipped:not_on_path)")
                return ""
            # Preflight: cached health check
            if _dm._codex_available is None:
                from cli.tools.delegate import check_codex
                check_codex()
            if _dm._codex_available is False:
                steps_log.append("codex(skipped:health_check_failed)")
                return ""
            cdef = CODEX_MODELS.get(task_type, CODEX_MODELS.get("ask", {}))
            model = dcfg.get("codex_default_model") or cdef.get("model", "")
            sandbox = dcfg.get("codex_default_sandbox") or cdef.get("sandbox", "read-only")
            reasoning = dcfg.get("codex_reasoning_effort") or cdef.get("reasoning", "high")
            timeout = int(dcfg.get("codex_timeout", 90))

            max_ctx = max(200, int(dcfg.get("codex_max_context_tokens", 4000) or 4000))
            ctx_text = context[:max_ctx * 4] if count_tokens(context) > max_ctx else context

            _log_progress(svc, f"[delegate] Running Codex ({model}, {sandbox})...")
            output, ok = _run_codex(
                task=task, context=ctx_text,
                model=model, sandbox=sandbox,
                reasoning=reasoning, timeout=timeout,
                cwd=str(svc.project_path),
            )
            if ok and output:
                _log_progress(svc, f"[delegate] Codex done ({task_type})")
                steps_log.append(f"codex({task_type},{model})")
                return output
            # Log the specific failure from output
            if output and output.startswith("["):
                steps_log.append(f"codex({output.split(']')[0]}])".replace("[", ""))
            else:
                steps_log.append("codex(error:no_output)")
            return ""

        # --- Gemini-only path (no fallthrough) -----------------------------
        if prefer_gemini:
            if not dcfg.get("gemini_enabled", False):
                steps_log.append("gemini(skipped:disabled)")
                return ""
            import cli.tools.delegate as _dm
            from cli.tools.delegate import GEMINI_MODELS, _is_gemini_on_path, _run_gemini
            if not _is_gemini_on_path():
                steps_log.append("gemini(skipped:not_on_path)")
                return ""
            # Preflight: cached health check (gemini --version, 5s timeout)
            if _dm._gemini_available is None:
                from cli.tools.delegate import check_gemini
                check_gemini()
            if _dm._gemini_available is False:
                steps_log.append("gemini(skipped:health_check_failed)")
                return ""
            gdef = GEMINI_MODELS.get(task_type, GEMINI_MODELS.get("ask", {}))
            model = dcfg.get("gemini_default_model") or gdef.get("model", "gemini-2.5-flash")
            timeout = int(dcfg.get("gemini_timeout", 45))

            max_ctx = max(200, int(dcfg.get("gemini_max_context_tokens", 8000) or 8000))
            ctx_text = context[:max_ctx * 4] if count_tokens(context) > max_ctx else context

            _log_progress(svc, f"[delegate] Running Gemini ({model})...")
            output, ok, _ = _run_gemini(
                task=task, context=ctx_text,
                model=model, timeout=timeout,
                cwd=str(svc.project_path),
            )
            if ok and output:
                steps_log.append(f"gemini({task_type},{model})")
                return output
            # Log the specific failure from output
            if output and output.startswith("["):
                steps_log.append(f"gemini({output.split(']')[0]}])".replace("[", ""))
            else:
                steps_log.append("gemini(error:no_output)")
            return ""

        # --- Ollama path (default) -----------------------------------------
        # For heavy tasks, prefer a cloud CLI if one is available on the system.
        # Ollama can take 30-90s per call; cloud CLIs are faster for reviews/diagnose.
        _LIGHT_TASKS = {"ask", "explain", "summarize", "docstring"}
        if task_type not in _LIGHT_TASKS:
            import cli.tools.delegate as _dm_auto
            from cli.tools.delegate import (
                CODEX_MODELS,
                GEMINI_MODELS,
                _is_codex_on_path,
                _is_gemini_on_path,
                _run_codex,
                _run_gemini,
            )
            # Gemini first (prefer; enabled in config for this project)
            _gem_ok = (_dm_auto._gemini_available is True) or (
                _dm_auto._gemini_available is None and _is_gemini_on_path()
            )
            if _gem_ok and _dm_auto._gemini_available is not False:
                gdef = GEMINI_MODELS.get(task_type, GEMINI_MODELS.get("ask", {}))
                g_model = dcfg.get("gemini_default_model") or gdef.get("model", "gemini-2.5-flash")
                g_timeout = int(dcfg.get("gemini_timeout", 45))
                g_max = max(200, int(dcfg.get("gemini_max_context_tokens", 8000) or 8000))
                g_ctx = context[:g_max * 4] if count_tokens(context) > g_max else context
                _log_progress(svc, f"[auto] Routing {task_type} → Gemini (skipping slow Ollama)...")
                g_out, g_ok, _ = _run_gemini(
                    task=task, context=g_ctx, model=g_model,
                    timeout=g_timeout, cwd=str(svc.project_path),
                )
                if g_ok and g_out:
                    steps_log.append(f"gemini({task_type},{g_model},auto)")
                    return g_out
            # Codex second
            _codex_ok = (_dm_auto._codex_available is True) or (
                _dm_auto._codex_available is None and _is_codex_on_path()
            )
            if _codex_ok and _dm_auto._codex_available is not False:
                cdef = CODEX_MODELS.get(task_type, CODEX_MODELS.get("ask", {}))
                c_model = dcfg.get("codex_default_model") or cdef.get("model", "")
                c_sandbox = dcfg.get("codex_default_sandbox") or cdef.get("sandbox", "read-only")
                c_reason = dcfg.get("codex_reasoning_effort") or cdef.get("reasoning", "high")
                c_timeout = int(dcfg.get("codex_timeout", 90))
                c_max = max(200, int(dcfg.get("codex_max_context_tokens", 4000) or 4000))
                c_ctx = context[:c_max * 4] if count_tokens(context) > c_max else context
                _log_progress(svc, f"[auto] Routing {task_type} → Codex (skipping slow Ollama)...")
                c_out, c_ok = _run_codex(
                    task=task, context=c_ctx, model=c_model,
                    sandbox=c_sandbox, reasoning=c_reason,
                    timeout=c_timeout, cwd=str(svc.project_path),
                )
                if c_ok and c_out:
                    steps_log.append(f"codex({task_type},{c_model},auto)")
                    return c_out

        ollama = svc.ollama_client
        if not ollama or not ollama.is_available():
            return ""

        from cli.tools.delegate import DELEGATE_TASKS, _fallback_model_order, resolve_model_name

        tdef = DELEGATE_TASKS.get(task_type)
        if not tdef:
            return ""

        req_model = dcfg.get(f"{task_type}_model") or dcfg.get("preferred_model") or tdef["default_model"]
        avail = ollama.list_models() or []
        model = resolve_model_name(req_model, avail)
        if not model:
            for cand in _fallback_model_order(task_type) + avail:
                model = resolve_model_name(cand, avail)
                if model:
                    break
        if not model:
            return ""

        # Truncate context
        max_ctx = max(200, int(dcfg.get("max_context_tokens", 1400) or 1400))
        if count_tokens(context) > max_ctx:
            context = context[:max_ctx * 4]

        response = ollama.generate(
            model=model,
            prompt=f"{task}\n\nContext:\n{context}",
            system=tdef.get("system_prompt", ""),
            max_tokens=int(dcfg.get("max_tokens", 512)),
            temperature=float(dcfg.get("temperature", 0.3)),
        )
        steps_log.append(f"delegate({task_type},{model})")
        return response.strip() if response else ""
    except Exception as e:
        steps_log.append(f"delegate(error:{type(e).__name__})")
        return ""
