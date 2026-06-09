"""Argument parser construction for the C3 CLI."""

from __future__ import annotations

import argparse


def build_parser(version: str, parse_cli_ide_arg):
    parser = argparse.ArgumentParser(
        prog="c3",
        description="Claude Code Companion - Reduce token usage with local intelligence",
    )
    parser.add_argument("--version", "-v", action="version", version=f"c3 version {version}")
    subparsers = parser.add_subparsers(dest="command")

    p_init = subparsers.add_parser("init", help="Initialize C3 for a project")
    p_init.add_argument("project_path", nargs="?", default=".")
    p_init.add_argument("--force", action="store_true", help="Skip prompts and apply update non-interactively")
    p_init.add_argument("--clear", action="store_true", help="Remove all C3 files and exit without rebuilding")
    p_init.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,gemini,antigravity}", help="Target IDE for MCP config (default: auto-detect)")
    p_init.add_argument("--mcp-mode", choices=["direct", "proxy"], default="direct", help="Default MCP mode if install is selected during init (default: direct)")
    p_init.add_argument("--git", action="store_true", help="Initialize a local Git repository during init/update")
    p_init.add_argument("--permissions", choices=["read-only", "c3-strict", "standard", "permissive"], default=None, help="Apply Claude Code permission tier (Claude Code only, used with --force)")
    p_init.add_argument("--include-mcp-wildcard", action="store_true", help="Add mcp__* wildcard so non-C3 MCP servers don't prompt per-call")

    p_index = subparsers.add_parser("index", help="Rebuild code index")
    p_index.add_argument("--max-files", type=int, default=500)

    p_compress = subparsers.add_parser("compress", help="Compress a file")
    p_compress.add_argument("file", help="File to compress")
    p_compress.add_argument("--mode", choices=["map", "dense_map", "smart", "diff"], default="smart")
    p_compress.add_argument("--output", "-o", action="store_true", help="Show compressed output")

    p_context = subparsers.add_parser("context", help="Get relevant context for a query")
    p_context.add_argument("query", help="What you want to do")
    p_context.add_argument("--top-k", type=int, default=5)
    p_context.add_argument("--max-tokens", type=int, default=4000)
    p_context.add_argument("--pipe", action="store_true", help="Raw output for piping")

    p_encode = subparsers.add_parser("encode", help="Encode text to compressed format")
    p_encode.add_argument("text", nargs="+")
    p_encode.add_argument("--pipe", action="store_true")

    p_decode = subparsers.add_parser("decode", help="Decode compressed format")
    p_decode.add_argument("text", nargs="+")

    p_session = subparsers.add_parser("session", help="Session management")
    p_session.add_argument("session_cmd", choices=["start", "save", "load", "list", "context"])
    p_session.add_argument("extra", nargs="*")

    p_claudemd = subparsers.add_parser("claudemd", help="CLAUDE.md management")
    p_claudemd.add_argument("claudemd_cmd", choices=["generate", "save", "check"])
    p_claudemd.add_argument("--nano", action="store_true", help="Generate nano mode (~250 tokens) instead of full compact mode")

    subparsers.add_parser("stats", help="Show statistics")

    p_benchmark = subparsers.add_parser("benchmark", help="Run with/without-C3 workflow benchmark")
    p_benchmark.add_argument("project_path", nargs="?", default=".")
    p_benchmark.add_argument("--sample-size", type=int, default=25, help="Number of files for compression benchmark")
    p_benchmark.add_argument("--min-tokens", type=int, default=200, help="Prefer files with at least this many tokens")
    p_benchmark.add_argument("--top-k", type=int, default=5, help="Top-k files for retrieval benchmarks")
    p_benchmark.add_argument("--max-tokens", type=int, default=4000, help="Max tokens in C3 retrieval context")
    p_benchmark.add_argument("--json", action="store_true", help="Emit JSON report to stdout")
    p_benchmark.add_argument("--output", help="Write JSON report to this path (relative to project)")
    p_benchmark.add_argument("--html-output", help="Write HTML report to this path (relative to project)")
    p_benchmark.add_argument("--no-html", action="store_true", help="Do not generate the HTML benchmark report")
    p_benchmark.add_argument("--system-name", help="System/AI identifier for this benchmark run (e.g. codex, claude, cursor)")
    p_benchmark.add_argument("--system-label", help="Display label for the benchmark system (e.g. OpenAI Codex)")
    p_benchmark.add_argument("--system-version", help="Optional system version/build label for the benchmark output")

    p_session_bench = subparsers.add_parser("session-benchmark", help="Run real-world session workflow benchmark")
    p_session_bench.add_argument("project_path", nargs="?", default=".")
    p_session_bench.add_argument("--sample-size", type=int, default=15, help="Number of files to sample")
    p_session_bench.add_argument("--min-tokens", type=int, default=200, help="Prefer files with at least this many tokens")
    p_session_bench.add_argument("--json", action="store_true", help="Emit JSON report to stdout")
    p_session_bench.add_argument("--output", help="Write JSON report to this path")
    p_session_bench.add_argument("--html-output", help="Write HTML report to this path")

    subparsers.add_parser("optimize", help="Show optimization suggestions")

    p_pipe = subparsers.add_parser("pipe", help="All-in-one pipeline for Claude")
    p_pipe.add_argument("query", nargs="+")
    p_pipe.add_argument("--top-k", type=int, default=5)
    p_pipe.add_argument("--max-tokens", type=int, default=4000)

    p_install_mcp = subparsers.add_parser("install-mcp", help="Generate MCP config for your IDE")
    p_install_mcp.add_argument("targets", nargs="*", help="Optional project path and/or IDE shorthand (for example: `claude` or `. codex`)")
    p_install_mcp.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,gemini,antigravity}", help="Target IDE (default: auto-detect)")
    p_install_mcp.add_argument("--mcp-mode", choices=["direct", "proxy"], default="direct", help="MCP entrypoint mode (default: direct)")
    p_install_mcp.add_argument("--permissions", choices=["read-only", "c3-strict", "standard", "permissive"], default=None, help="Apply Claude Code permission tier (Claude Code only)")
    p_install_mcp.add_argument("--include-mcp-wildcard", action="store_true", help="Add mcp__* wildcard so non-C3 MCP servers don't prompt per-call")

    p_mcp_install = subparsers.add_parser("mcp-install", help="Alias for install-mcp")
    p_mcp_install.add_argument("targets", nargs="*", help="Optional project path and/or IDE shorthand")
    p_mcp_install.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,gemini,antigravity}", help="Target IDE (default: auto-detect)")
    p_mcp_install.add_argument("--mcp-mode", choices=["direct", "proxy"], default="direct", help="MCP entrypoint mode (default: direct)")
    p_mcp_install.add_argument("--permissions", choices=["read-only", "c3-strict", "standard", "permissive"], default=None, help="Apply Claude Code permission tier (Claude Code only)")
    p_mcp_install.add_argument("--include-mcp-wildcard", action="store_true", help="Add mcp__* wildcard so non-C3 MCP servers don't prompt per-call")

    p_mcp_remove = subparsers.add_parser("mcp-remove", help="Remove an MCP server from your IDE config")
    p_mcp_remove.add_argument("name", help="Name of the MCP server to remove (e.g. 'c3')")
    p_mcp_remove.add_argument("project_path", nargs="?", default=".", help="Project path to resolve IDE and config")
    p_mcp_remove.add_argument("--ide", default="auto", type=parse_cli_ide_arg, help="Target IDE (default: auto-detect)")

    p_ui = subparsers.add_parser("ui", help="Launch the web dashboard")
    p_ui.add_argument("project_path", nargs="?", default=".")
    p_ui.add_argument("--port", type=int, default=3333)
    p_ui.add_argument("--no-browser", action="store_true")
    p_ui.add_argument("--silent", action="store_true", help="Hide API request logs in terminal")
    p_ui.add_argument("--nano", action="store_true", help="Launch minimal mission-control UI")

    p_hub = subparsers.add_parser("hub", help="Launch the Project Hub web dashboard")
    p_hub.add_argument("--port", type=int, default=3330, help="Port to listen on (default: 3330)")
    p_hub.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    p_hub.add_argument("--silent", action="store_true", help="Disable browser auto-open and suppress request logs")
    p_hub.add_argument("--extra-silent", action="store_true", help="Also suppress hub startup banner output")
    p_hub.add_argument("--install", action="store_true", help="Register as a login/startup service")
    p_hub.add_argument("--uninstall", action="store_true", help="Remove startup service registration")
    p_hub.add_argument("--status", action="store_true", help="Show startup service status")

    p_projects = subparsers.add_parser("projects", help="Manage registered C3 projects (CLI)")
    p_projects.add_argument(
        "projects_cmd",
        nargs="?",
        choices=["list", "add", "remove", "start", "sessions"],
        default="list",
        help="Sub-command (default: list)",
    )
    p_projects.add_argument(
        "project_path",
        nargs="?",
        default=None,
        help="Project path (required for add, remove, start)",
    )
    p_projects.add_argument("--name", default=None, help="Display name (for add)")

    p_perms = subparsers.add_parser("permissions",
        help="Manage Claude Code permissions — show | preview <tier> | diff | clean | <tier>")
    p_perms.add_argument("tier", nargs="?", default="show",
        help="Action (show/preview/diff/clean) or tier (read-only, c3-strict, standard, permissive). Aliases: strict, unrestricted, readonly.")
    p_perms.add_argument("target", nargs="?", default=None,
        help="Target tier for 'preview' or 'diff' subcommands")
    p_perms.add_argument("--include-mcp-wildcard", action="store_true",
        help="Include mcp__* wildcard so non-C3 MCP servers don't prompt per-call")
    p_perms.add_argument("--project-path", default=".",
        help="Project path (default: current directory)")

    p_e2e = subparsers.add_parser("benchmark-e2e", help="Run end-to-end AI session benchmark (C3 vs baseline)")
    p_e2e.add_argument("project_path", nargs="?", default=".", help="Project path to benchmark")

    p_e2e_common = p_e2e.add_argument_group("common options")
    p_e2e_common.add_argument("--providers", default=None, help="Comma-separated: claude,gemini,codex (default: auto-detect)")
    p_e2e_common.add_argument("--models", default=None, help="Model overrides: claude=sonnet,gemini=gemini-2.5-flash,codex=o3")
    p_e2e_common.add_argument("--tasks", default="all", help="Task filter: all (default), or comma-separated categories")
    p_e2e_common.add_argument("--max-tasks", type=int, default=1, help="Max tasks per category (default: 1)")
    p_e2e_common.add_argument("--timeout", type=int, default=120, help="Per-task timeout in seconds (default: 120)")
    p_e2e_common.add_argument("--dry-run", action="store_true", help="Show tasks and providers without running")
    p_e2e_common.add_argument("--verbose", action="store_true", help="Print each result as it completes")
    p_e2e_common.add_argument("--json", action="store_true", help="Emit JSON report to stdout")
    p_e2e_common.add_argument("--output", help="Write JSON report to this path")
    p_e2e_common.add_argument("--html-output", help="Write HTML report to this path")

    p_e2e_adv = p_e2e.add_argument_group("advanced options")
    p_e2e_adv.add_argument("--no-parallel", action="store_true", help="Run providers sequentially instead of in parallel")
    p_e2e_adv.add_argument("--judge", default=None, help="Enable AI-as-judge scoring with this CLI (e.g. claude, gemini)")
    p_e2e_adv.add_argument("--judge-model", default=None, help="Model override for the judge CLI")
    p_e2e_adv.add_argument("--task-workers", type=int, default=1,
                           help="Run N tasks concurrently (default: 1). Higher values are faster but may hit rate limits.")
    p_e2e_adv.add_argument("--no-cache", action="store_true",
                           help="Ignore cached results and re-run all tasks (cache is enabled by default, TTL=24h)")
    p_e2e_adv.add_argument("--permission-mode", default="bypassPermissions",
                           help="Permission mode for AI CLI (default: bypassPermissions). Use 'plan' for read-only mode.")
    p_e2e_adv.add_argument("--delegate-benchmark", action="store_true",
                           help="Run delegate backend comparison (Ollama vs Codex) instead of normal e2e benchmark")
    p_e2e_adv.add_argument("--delegate-types", default=None,
                           help="Comma-separated delegate task types to benchmark (default: all). E.g. review,diagnose")

    p_terse = subparsers.add_parser("terse", help="Manage the terse-advisor nudge state")
    p_terse.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["dismiss", "later", "reset", "status"],
        help="dismiss=silence forever, later=snooze 24h, reset=clear state, status=show state (default)",
    )

    # Unified `c3 bench <tier>` — wraps benchmark / session-benchmark / benchmark-e2e
    # Legacy commands above kept for backward compatibility.
    p_bench = subparsers.add_parser(
        "bench",
        help="Run C3 benchmarks (unified: quick | session | e2e | delegate | all | dashboard)",
    )
    bench_sub = p_bench.add_subparsers(dest="bench_tier")

    p_bq = bench_sub.add_parser("quick", help="Local synthetic benchmark (no AI calls) [Synthetic]")
    p_bq.add_argument("project_path", nargs="?", default=".")
    p_bq.add_argument("--sample-size", type=int, default=25)
    p_bq.add_argument("--min-tokens", type=int, default=200)
    p_bq.add_argument("--top-k", type=int, default=5)
    p_bq.add_argument("--max-tokens", type=int, default=4000)
    p_bq.add_argument("--json", action="store_true")
    p_bq.add_argument("--output")
    p_bq.add_argument("--html-output")
    p_bq.add_argument("--no-html", action="store_true")
    p_bq.add_argument("--system-name")
    p_bq.add_argument("--system-label")
    p_bq.add_argument("--system-version")

    p_bs = bench_sub.add_parser("session", help="Workflow scenario benchmark (6 scenarios) [Synthetic]")
    p_bs.add_argument("project_path", nargs="?", default=".")
    p_bs.add_argument("--sample-size", type=int, default=15)
    p_bs.add_argument("--min-tokens", type=int, default=200)
    p_bs.add_argument("--json", action="store_true")
    p_bs.add_argument("--output")
    p_bs.add_argument("--html-output")

    p_be = bench_sub.add_parser("e2e", help="End-to-end AI benchmark (real claude/gemini/codex) [Live AI]")
    p_be.add_argument("project_path", nargs="?", default=".")
    p_be.add_argument("--providers", default=None, help="Comma-separated: claude,gemini,codex (default: auto-detect)")
    p_be.add_argument("--models", default=None, help="Model overrides: claude=sonnet,gemini=gemini-2.5-flash,codex=o3")
    p_be.add_argument("--tasks", default="all", help="Task filter: all (default), or comma-separated categories")
    p_be.add_argument("--max-tasks", type=int, default=1)
    p_be.add_argument("--timeout", type=int, default=120)
    p_be.add_argument("--dry-run", action="store_true")
    p_be.add_argument("--verbose", action="store_true")
    p_be.add_argument("--json", action="store_true")
    p_be.add_argument("--output")
    p_be.add_argument("--html-output")
    p_be.add_argument("--no-parallel", action="store_true")
    p_be.add_argument("--judge", default=None)
    p_be.add_argument("--judge-model", default=None)
    p_be.add_argument("--task-workers", type=int, default=1)
    p_be.add_argument("--no-cache", action="store_true")
    p_be.add_argument("--permission-mode", default="bypassPermissions")

    p_bd = bench_sub.add_parser("delegate", help="Delegate backend comparison (Ollama vs Codex) [Live AI]")
    p_bd.add_argument("project_path", nargs="?", default=".")
    p_bd.add_argument("--delegate-types", default=None, help="Comma-separated delegate task types (default: all)")
    p_bd.add_argument("--verbose", action="store_true")
    p_bd.add_argument("--json", action="store_true")
    p_bd.add_argument("--output")

    p_ba = bench_sub.add_parser("all", help="Run full benchmark suite (quick + session + e2e + dashboard)")
    p_ba.add_argument("project_path", nargs="?", default=".")
    p_ba.add_argument("--skip-e2e", action="store_true", help="Skip the e2e benchmark (slow; requires AI CLIs)")
    p_ba.add_argument("--sample-size", type=int, default=15)
    p_ba.add_argument("--min-tokens", type=int, default=200)
    p_ba.add_argument("--max-tasks", type=int, default=1)
    p_ba.add_argument("--timeout", type=int, default=120)
    p_ba.add_argument("--providers", default=None)

    p_bdash = bench_sub.add_parser("dashboard", help="Regenerate unified HTML dashboard")
    p_bdash.add_argument("project_path", nargs="?", default=".")
    p_bdash.add_argument("--open", action="store_true", help="Open in browser after generating")

    p_bext = bench_sub.add_parser("external",
        help="External benchmark suites (Aider Polyglot / SWE-bench) [External]")
    p_bext.add_argument("project_path", nargs="?", default=".")
    p_bext.add_argument("--suite", choices=["aider-polyglot", "swe-bench-lite"],
        default="aider-polyglot",
        help="External benchmark suite (default: aider-polyglot)")
    p_bext.add_argument("--path", default=None,
        help="Path to the benchmark corpus (aider-polyglot: repo dir; swe-bench-lite: unused)")
    p_bext.add_argument("--dataset", default=None,
        help="swe-bench-lite: path to swe_bench_lite.jsonl or HF dataset id "
             "(default: princeton-nlp/SWE-bench_Lite)")
    p_bext.add_argument("--agent", choices=["aider"], default="aider",
        help="swe-bench-lite: agent to generate patches (default: aider)")
    p_bext.add_argument("--languages", default="python",
        help="aider-polyglot: python,javascript,go,rust,java,cpp (default: python)")
    p_bext.add_argument("--max-exercises", type=int, default=5,
        help="aider-polyglot: max exercises per language (default: 5)")
    p_bext.add_argument("--max-tasks", type=int, default=5,
        help="swe-bench-lite: max instances to run (default: 5)")
    p_bext.add_argument("--model", default="gpt-4o-mini",
        help="Model ID to pass to the agent (default: gpt-4o-mini)")
    p_bext.add_argument("--timeout", type=int, default=300,
        help="Per-task agent timeout in seconds (default: 300)")
    p_bext.add_argument("--docker-eval", action="store_true",
        help="swe-bench-lite: run the official Docker-based evaluation after patch generation")
    p_bext.add_argument("--verbose", action="store_true",
        help="Print each task result as it completes")
    p_bext.add_argument("--dry-run", action="store_true",
        help="Validate setup (CLIs, datasets) without running the agent")

    # ── Bitbucket Data Center / Server (v2.30.0) ─────────────────────────
    p_bitbucket = subparsers.add_parser(
        "bitbucket",
        help="Bitbucket Data Center / Server credential + workspace management",
    )
    bb_subs = p_bitbucket.add_subparsers(dest="bitbucket_cmd")

    bb_login = bb_subs.add_parser(
        "login",
        help="Authenticate with a Bitbucket Data Center server (interactive PAT prompt)",
    )
    bb_login.add_argument("--url", required=True, help="Bitbucket server base URL (e.g. https://bitbucket.example.com)")
    bb_login.add_argument("--username", help="Bitbucket username (prompted if omitted)")
    bb_login.add_argument("--token", help="Personal Access Token (prompted via getpass if omitted — preferred)")
    bb_login.add_argument("--no-set-active", action="store_true", help="Do not switch the active account to this one")
    bb_login.add_argument("--insecure", action="store_true", help="Disable TLS verification (self-signed certs)")
    bb_login.add_argument("project_path", nargs="?", default=".")

    bb_logout = bb_subs.add_parser("logout", help="Remove a Bitbucket account from keyring + config")
    bb_logout.add_argument("--url", help="Bitbucket server base URL (defaults to active account)")
    bb_logout.add_argument("--username", help="Username to log out (defaults to active account)")
    bb_logout.add_argument("project_path", nargs="?", default=".")

    bb_status = bb_subs.add_parser("status", help="Show configured Bitbucket accounts and connectivity")
    bb_status.add_argument("project_path", nargs="?", default=".")

    bb_use = bb_subs.add_parser("use", help="Switch the active Bitbucket account")
    bb_use.add_argument("--url", required=True)
    bb_use.add_argument("--username", required=True)
    bb_use.add_argument("project_path", nargs="?", default=".")

    bb_default = bb_subs.add_parser(
        "set-default",
        help="Set the default project key + repo slug for this C3 project",
    )
    bb_default.add_argument("--project", required=True, help="Bitbucket project key (e.g. PROJ)")
    bb_default.add_argument("--repo", required=True, help="Repository slug")
    bb_default.add_argument("project_path", nargs="?", default=".")

    # ── Oracle Discovery API (v2.32.0) ──────────────────────────────────
    p_oracle = subparsers.add_parser(
        "oracle",
        help="Oracle Discovery API key + connection management",
    )
    or_subs = p_oracle.add_subparsers(dest="oracle_cmd")
    or_api = or_subs.add_parser(
        "api",
        help="Show connection info / manage the Discovery API key",
    )
    or_api.add_argument(
        "action",
        nargs="?",
        default="info",
        choices=["info", "key", "rotate", "clear"],
        help="info (default): print REST+MCP URLs and a .mcp.json snippet; "
             "key: print the token; rotate: replace it; clear: delete it",
    )
    or_api.add_argument("--port", type=int, default=None, help="Override REST port in printed info")
    or_api.add_argument("--mcp-port", type=int, default=None, help="Override MCP port in printed info")

    return parser
