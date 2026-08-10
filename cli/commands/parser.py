"""Argument parser construction for the C3 CLI."""

from __future__ import annotations

import argparse


def build_parser(version: str, parse_cli_ide_arg):
    parser = argparse.ArgumentParser(
        prog="c3",
        description="Claude Code Companion - Reduce token usage with local intelligence",
        epilog="Support C3 development: https://github.com/sponsors/drknowhow",
    )
    parser.add_argument(
        "--version", "-v", action="version",
        version=f"c3 version {version} | support C3: https://github.com/sponsors/drknowhow",
    )
    subparsers = parser.add_subparsers(dest="command")

    p_init = subparsers.add_parser("init", help="Initialize C3 for a project")
    p_init.add_argument("project_path", nargs="?", default=".")
    p_init.add_argument("--force", action="store_true", help="Skip prompts and apply update non-interactively")
    p_init.add_argument("--clear", action="store_true", help="Remove all C3 files and exit without rebuilding")
    p_init.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,antigravity}", help="Target IDE for MCP config (default: auto-detect)")
    p_init.add_argument("--mcp-mode", choices=["direct", "proxy"], default="direct", help="Default MCP mode if install is selected during init (default: direct)")
    p_init.add_argument("--git", action="store_true", help="Initialize a local Git repository during init/update")
    p_init.add_argument("--no-embed", action="store_true", help="Skip building the semantic embedding index during init")
    p_init.add_argument("--permissions", choices=["read-only", "c3-strict", "standard", "permissive"], default=None, help="Apply Claude Code permission tier (Claude Code only, used with --force)")
    p_init.add_argument("--enforcement", choices=["strict", "advisory", "off"], default=None, help="Tool-discipline mode. Omit to derive from --permissions (standard->advisory, permissive->off, strict/read-only->strict)")
    p_init.add_argument("--include-mcp-wildcard", action="store_true", help="Add mcp__* wildcard so non-C3 MCP servers don't prompt per-call")

    p_upgrade = subparsers.add_parser("upgrade", help="Upgrade C3 to the latest PyPI release")
    p_upgrade.add_argument("--check", action="store_true",
                           help="Only report whether a newer version exists; don't install")

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

    p_map = subparsers.add_parser("map", help="Live repo map (.c3/MAP.md) management")
    p_map.add_argument("map_cmd", choices=["status", "ensure", "refresh"])
    p_map.add_argument("--json", action="store_true", help="Emit JSON result")

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
    p_install_mcp.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,antigravity}", help="Target IDE (default: auto-detect)")
    p_install_mcp.add_argument("--mcp-mode", choices=["direct", "proxy"], default="direct", help="MCP entrypoint mode (default: direct)")
    p_install_mcp.add_argument("--permissions", choices=["read-only", "c3-strict", "standard", "permissive"], default=None, help="Apply Claude Code permission tier (Claude Code only)")
    p_install_mcp.add_argument("--include-mcp-wildcard", action="store_true", help="Add mcp__* wildcard so non-C3 MCP servers don't prompt per-call")

    p_mcp_install = subparsers.add_parser("mcp-install", help="Alias for install-mcp")
    p_mcp_install.add_argument("targets", nargs="*", help="Optional project path and/or IDE shorthand")
    p_mcp_install.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,antigravity}", help="Target IDE (default: auto-detect)")
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

    p_sub = subparsers.add_parser("sub", help="Manage sub-projects (linked child .c3 branches)")
    p_sub.add_argument(
        "sub_cmd",
        nargs="?",
        choices=["add", "list", "remove", "run", "check"],
        default="list",
        help="Sub-command (default: list)",
    )
    p_sub.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Folder (add), sub-project name/path (remove), or operation update|reindex|health (run)",
    )
    p_sub.add_argument("--parent", default=".", help="Parent project path (default: current directory)")
    p_sub.add_argument("--name", default=None, help="Display name for the sub-project (add)")
    p_sub.add_argument("--ide", default=None, type=parse_cli_ide_arg, help="IDE for the sub-project init (add)")
    p_sub.add_argument("--no-reindex-parent", action="store_true", help="Skip the parent reindex after add/remove")
    p_sub.add_argument("--no-init", action="store_true", help="Link only; skip running init in the folder (add)")
    p_sub.add_argument("--clear", action="store_true", help="Also wipe the sub-project's .c3 and unregister it (remove; default keeps .c3)")
    p_sub.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    p_sub.add_argument("--include-parent", action="store_true", help="Also run the operation on the parent (run)")
    p_sub.add_argument("--mcp", action="store_true", help="Also reinstall MCP config on update (run update)")
    p_sub.add_argument("--fix", action="store_true", help="Repair links from the parent config (check)")
    p_sub.add_argument("--prune", action="store_true", help="With --fix: drop entries whose folder is gone (check)")
    p_sub.add_argument("--json", action="store_true", help="Emit JSON output")

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
    bb_login.add_argument("--global", dest="use_global", action="store_true", help="Store the account in the global ~/.c3/config.json so it is reusable in every C3 project")
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

    # ── Jira Cloud / Data Center (v2.56.0) ──────────────────────────────
    p_jira = subparsers.add_parser(
        "jira",
        help="Jira Cloud / Data Center credential + workspace management",
    )
    jira_subs = p_jira.add_subparsers(dest="jira_cmd")

    jr_login = jira_subs.add_parser(
        "login",
        help="Authenticate with a Jira site (interactive token prompt)",
    )
    jr_login.add_argument("--url", required=True, help="Jira base URL (e.g. https://yoursite.atlassian.net)")
    jr_login.add_argument("--deployment", choices=["cloud", "data_center"], default="", help="Deployment type (inferred as 'cloud' for *.atlassian.net URLs; required otherwise)")
    jr_login.add_argument("--name", default="", help="Account name in the registry (default: derived from URL host)")
    jr_login.add_argument("--username", help="Email (Cloud) or username (Data Center); prompted if omitted")
    jr_login.add_argument("--token", help="API token (Cloud) / PAT (Data Center); prompted via getpass if omitted — preferred")
    jr_login.add_argument("--no-set-default", action="store_true", help="Do not make this the default account")
    jr_login.add_argument("--no-verify-login", action="store_true", help="Skip the connection probe (store credentials offline)")
    jr_login.add_argument("--insecure", action="store_true", help="Disable TLS verification / allow http:// (local dev only)")
    jr_login.add_argument("--ca-bundle", default="", help="Path to a custom CA bundle for self-signed enterprise certs")
    jr_login.add_argument("--global", dest="use_global", action="store_true", help="Store the account in the global ~/.c3/config.json so it is reusable in every C3 project")
    jr_login.add_argument("project_path", nargs="?", default=".")

    jr_logout = jira_subs.add_parser("logout", help="Remove a Jira account from keyring + config")
    jr_logout.add_argument("--name", default="", help="Account name (defaults to the default account)")
    jr_logout.add_argument("project_path", nargs="?", default=".")

    jr_status = jira_subs.add_parser("status", help="Show configured Jira accounts and connectivity")
    jr_status.add_argument("project_path", nargs="?", default=".")

    jr_use = jira_subs.add_parser("use", help="Switch the default Jira account")
    jr_use.add_argument("--name", required=True, help="Registered account name")
    jr_use.add_argument("project_path", nargs="?", default=".")

    jr_default = jira_subs.add_parser(
        "set-default",
        help="Set the default Jira project key on an account",
    )
    jr_default.add_argument("--project", required=True, help="Jira project key (e.g. PROJ)")
    jr_default.add_argument("--name", default="", help="Account name (defaults to the default account)")
    jr_default.add_argument("project_path", nargs="?", default=".")

    # ── Credential vault (v2.58.0) ──────────────────────────────────────
    p_creds = subparsers.add_parser(
        "creds",
        help="Credential vault — named secrets for agents (global + per-project)",
    )
    creds_subs = p_creds.add_subparsers(dest="creds_cmd")

    cr_set = creds_subs.add_parser(
        "set", help="Create or update a credential (value via hidden prompt)"
    )
    cr_set.add_argument("name", help="Entry name (env-var safe: [A-Za-z_][A-Za-z0-9_]*)")
    cr_set.add_argument("--value", default="", help="Secret value (prompted via getpass if omitted — preferred)")
    cr_set.add_argument("--stdin", action="store_true", help="Read the value from stdin (piped/multiline values)")
    cr_set.add_argument("--type", dest="ctype", choices=["token", "env", "multiline"], default="token", help="Entry type")
    cr_set.add_argument("--desc", default="", help="Human description shown in list/UI")
    cr_set.add_argument("--env-var", default="", help="Env var name used at injection (default: entry name)")
    cr_set.add_argument("--agent-readable", action="store_true", help="Allow the agent to reveal the decoded value into its context (default: injection-only)")
    cr_set.add_argument("--inject", action="store_true", help="Auto-inject into every c3_shell run")
    cr_set.add_argument("--global", dest="use_global", action="store_true", help="Store in the global scope (~/.c3) so every C3 project can use it")
    # --path (not a trailing positional): a second positional after options
    # breaks argparse on py<3.12 ("unrecognized arguments").
    cr_set.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    cr_get = creds_subs.add_parser("get", help="Show entry metadata (masked; --show prints the value)")
    cr_get.add_argument("name")
    cr_get.add_argument("--show", action="store_true", help="Print the decoded value to the terminal")
    cr_get.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    cr_list = creds_subs.add_parser("list", help="List credentials visible to this project")
    cr_list.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    cr_rm = creds_subs.add_parser("rm", help="Delete a credential (value + registry entry)")
    cr_rm.add_argument("name")
    cr_rm.add_argument("--global", dest="use_global", action="store_true", help="Delete from the global scope")
    cr_rm.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    cr_import = creds_subs.add_parser("import", help="Import KEY=VALUE lines from a .env file")
    cr_import.add_argument("env_file", help="Path to the .env file")
    cr_import.add_argument("--global", dest="use_global", action="store_true", help="Import into the global scope")
    cr_import.add_argument("--overwrite", action="store_true", help="Replace entries already registered in the target scope")
    cr_import.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    # ── Agent Locks (docs/agent-locks.md) ───────────────────────────────
    # force-release is human-only: it bumps the fencing counter so a holder
    # that comes back is stale by construction. Agents get c3_locks instead,
    # which deliberately has no force action.
    p_locks = subparsers.add_parser(
        "locks", help="Agent leases — see who holds which file, release a stuck one")
    locks_subs = p_locks.add_subparsers(dest="locks_cmd")

    lk_list = locks_subs.add_parser("list", help="Show active leases")
    lk_list.add_argument("--path", dest="project_path", default=".")

    lk_rel = locks_subs.add_parser("release", help="Release leases held by a session")
    lk_rel.add_argument("--session", required=True, help="session_id to release")
    lk_rel.add_argument("--path", dest="project_path", default=".")

    lk_force = locks_subs.add_parser(
        "force-release", help="Break one lease regardless of holder (audited)")
    lk_force.add_argument("file", help="Project-relative path to unlock")
    lk_force.add_argument("--note", default="", help="Why — recorded in the ledger")
    lk_force.add_argument("--path", dest="project_path", default=".")

    lk_sweep = locks_subs.add_parser("sweep", help="Drop expired leases now")
    lk_sweep.add_argument("--path", dest="project_path", default=".")

    # ── AgentCI — local CI execution (docs/agent-ci.md) ─────────────────
    # Reads .github/workflows/*.yml as the source of truth. The CLI mirrors
    # the c3_ci tool so a human can drive the same loop an agent does.
    p_ci = subparsers.add_parser(
        "ci", help="Run this repo's real CI locally instead of pushing for feedback")
    ci_subs = p_ci.add_subparsers(dest="ci_cmd")

    def _ci_common(sub):
        sub.add_argument("--path", dest="project_path", default=".",
                         help="Project directory (default: current)")
        sub.add_argument("--json", action="store_true", help="Machine-readable output")
        sub.add_argument("--event", default="",
                         help="GitHub event to simulate (push, pull_request) — "
                              "only needed when an `if:` reads github.event_name")
        return sub

    _ci_common(ci_subs.add_parser(
        "inspect", help="Show workflows, the job DAG, and what runs on this host"))

    ci_run = _ci_common(ci_subs.add_parser("run", help="Execute jobs locally"))
    ci_run.add_argument("--job", default="",
                        help="Job id, matrix cell, or workflow::job (default: all)")
    ci_run.add_argument("--workflow", default="", help="Limit to one workflow by name")
    ci_run.add_argument("--allow-foreign", action="store_true",
                        help="Also run jobs targeting another OS (labelled cross-OS; "
                             "can never yield FULL_CI_PASS)")
    ci_run.add_argument("--timeout", type=int, default=0,
                        help="Per-step timeout in seconds (default 900)")
    ci_run.add_argument("--engine", choices=["auto", "native", "act"],
                        default="auto",
                        help="auto (default) uses act for Linux jobs when act + "
                             "Docker are present, native otherwise")
    ci_run.add_argument("--allow-side-effects", action="store_true",
                        help="Let the act engine run jobs that look like they "
                             "publish or deploy. Refused by default.")
    ci_run.add_argument("--required", action="store_true",
                        help="Run only the jobs a change could have broken "
                             "(conservative; see `c3 ci plan`)")
    ci_run.add_argument("--base", default="",
                        help="Diff against this ref instead of the working tree")
    ci_run.add_argument("--allow-host-mutation", action="store_true",
                        help="Permit native steps that reconfigure this machine "
                             "(pip/npm -g/apt install). Refused by default.")
    ci_run.add_argument("--no-cache", action="store_true",
                        help="Ignore cached results and execute every "
                             "selected job")
    ci_run.add_argument("--network", default="",
                        help="Container network for the act engine (e.g. `none` "
                             "to cut egress). Default: act's own.")

    ci_rerun = _ci_common(ci_subs.add_parser(
        "rerun", help="Re-run only the jobs that failed in the last run"))
    ci_rerun.add_argument("--run", dest="run_id", default="", help="Run id (default: latest)")
    ci_rerun.add_argument("--allow-foreign", action="store_true")
    ci_rerun.add_argument("--timeout", type=int, default=0)

    ci_status = _ci_common(ci_subs.add_parser("status", help="Last run's verdict and jobs"))
    ci_status.add_argument("--run", dest="run_id", default="")

    ci_fail = _ci_common(ci_subs.add_parser(
        "failures", help="Structured failures from the last run"))
    ci_fail.add_argument("--run", dest="run_id", default="")

    ci_logs = _ci_common(ci_subs.add_parser("logs", help="Tail one job's log"))
    ci_logs.add_argument("job", nargs="?", default="", help="Job key")
    ci_logs.add_argument("--run", dest="run_id", default="")
    ci_logs.add_argument("--tail", type=int, default=200)

    _ci_common(ci_subs.add_parser("runs", help="Recent local CI runs"))
    ci_plan = _ci_common(ci_subs.add_parser(
        "plan", help="Show which jobs a change requires, and why"))
    ci_plan.add_argument("--base", default="",
                         help="Diff against this ref instead of the working tree")
    ci_cache_p = _ci_common(ci_subs.add_parser(
        "cache", help="Cached-result store: size, or --clear it"))
    ci_cache_p.add_argument("--clear", action="store_true",
                            help="Drop every cached result and dependency cache")
    _ci_common(ci_subs.add_parser(
        "doctor", help="Which execution engines are available on this machine"))

    # ── Override Requests (v2.69.0, docs/override-requests.md) ──────────
    # Human-only approval surface. A grant is single-use, session-bound,
    # path-exact and TTL-capped, and it never edits policy: the rule that
    # denied the call is still in force the moment the grant is spent.
    # There is no agent-facing path to any verb in this command.
    p_override = subparsers.add_parser(
        "override",
        help="Override Requests — allow one blocked call once, without weakening the rule",
    )
    override_subs = p_override.add_subparsers(dest="override_cmd")

    ov_policy = override_subs.add_parser(
        "policy", help="Show the effective `override` policy and which layers are escalatable"
    )
    ov_policy.add_argument("--path", dest="project_path", default=".",
                           help="Project directory (default: current)")

    ov_grant = override_subs.add_parser(
        "grant", help="Mint a single-use grant for one blocked call (human approval)"
    )
    ov_grant.add_argument("target", help="The exact path that was blocked")
    ov_grant.add_argument("--session", dest="session_id", required=True,
                          help="Claude Code session id the grant is bound to — a grant never crosses sessions")
    ov_grant.add_argument("--op", choices=["read", "write"], default="read",
                          help="Operation to allow (default: read)")
    ov_grant.add_argument("--tool", default=None,
                          help="Exact tool name (default: Read for --op read, Edit for --op write)")
    ov_grant.add_argument("--layer", choices=["access", "discipline", "mask", "shell"],
                          default=None,
                          help="Force a layer. Default: derive it from the denial the path actually produces")
    ov_grant.add_argument("--ttl", type=int, default=None, dest="ttl_s",
                          help="Seconds until the grant expires (clamped to override.max_ttl_s, hard ceiling 900)")
    ov_grant.add_argument("--uses", type=int, default=None,
                          help="Uses allowed (default 1; >1 needs override.allow_session_grants)")
    ov_grant.add_argument("--confirm", default=None,
                          help="Required for deny/builtin rules: retype the rule glob by hand")
    ov_grant.add_argument("--path", dest="project_path", default=".",
                          help="Project directory (default: current)")

    ov_list = override_subs.add_parser("list", help="Live grants for this project")
    ov_list.add_argument("--session", dest="session_id", default="",
                         help="Only grants bound to this session id")
    ov_list.add_argument("--audit", type=int, default=0, metavar="N",
                         help="Also print the last N lines of .c3/overrides.jsonl")
    ov_list.add_argument("--path", dest="project_path", default=".",
                         help="Project directory (default: current)")

    ov_revoke = override_subs.add_parser("revoke", help="Drop a live grant before it is used")
    ov_revoke.add_argument("grant_id", help="Grant id (grt_…)")
    ov_revoke.add_argument("--path", dest="project_path", default=".",
                           help="Project directory (default: current)")

    ov_check = override_subs.add_parser(
        "check", help="Would a grant cover this call right now? (never consumes one)"
    )
    ov_check.add_argument("target", help="Path to test")
    ov_check.add_argument("--session", dest="session_id", required=True)
    ov_check.add_argument("--op", choices=["read", "write"], default="read")
    ov_check.add_argument("--tool", default=None)
    ov_check.add_argument("--layer", choices=["access", "discipline", "mask", "shell"],
                          default=None)
    ov_check.add_argument("--path", dest="project_path", default=".",
                          help="Project directory (default: current)")

    ov_sweep = override_subs.add_parser("sweep", help="Drop expired / spent grants now")
    ov_sweep.add_argument("--path", dest="project_path", default=".",
                          help="Project directory (default: current)")

    # Requests: what agents have ASKED for. Deciding is human-only and lives
    # here (and, later, on the phone) — never on the c3_override agent tool.
    ov_requests = override_subs.add_parser(
        "requests", help="Override requests agents have asked for"
    )
    ov_requests.add_argument("--status", default="",
                             help="pending | approved | denied | expired | withdrawn")
    ov_requests.add_argument("--all", dest="all_projects", action="store_true",
                             help="Every project, not just this one")
    ov_requests.add_argument("--path", dest="project_path", default=".",
                             help="Project directory (default: current)")

    ov_approve = override_subs.add_parser(
        "approve", help="Approve one request — mints a single-use grant"
    )
    ov_approve.add_argument("request_id", help="Request id (ovr_…)")
    ov_approve.add_argument("--ttl", type=int, default=None, dest="ttl_s",
                            help="Seconds until the grant expires (clamped to override.max_ttl_s)")
    ov_approve.add_argument("--uses", type=int, default=None,
                            help="Uses allowed (default 1)")
    ov_approve.add_argument("--note", default="",
                            help="Note recorded with the decision")
    ov_approve.add_argument("--confirm", default=None,
                            help="Required for deny/builtin rules: retype the rule glob by hand")
    ov_approve.add_argument("--path", dest="project_path", default=".",
                            help="Project directory (default: current)")

    ov_deny = override_subs.add_parser("deny", help="Refuse one request")
    ov_deny.add_argument("request_id", help="Request id (ovr_…)")
    ov_deny.add_argument("--note", default="",
                         help="Note the agent will see with the refusal")
    ov_deny.add_argument("--path", dest="project_path", default=".",
                         help="Project directory (default: current)")

    # ── Tool discipline / enforcement mode (v2.66.0) ────────────────────
    # LAYER C: how hard C3 pushes the agent toward c3_* tools. Distinct from
    # `c3 access` (path policy — a security boundary) and from
    # `c3 permissions` (the IDE's own allow/deny lists).
    p_enforce = subparsers.add_parser(
        "enforce",
        help="Tool discipline — strict | advisory | off (show current if omitted)",
    )
    p_enforce.add_argument(
        "mode", nargs="?", default=None,
        choices=["strict", "advisory", "off"],
        help="strict = block native Edit/Write until a c3_* call; "
             "advisory = allow with a nudge (ledger still logs); "
             "off = no nudging. Omit to show the current mode.",
    )
    p_enforce.add_argument("--global", dest="use_global", action="store_true",
                           help="Write to the global scope (~/.c3) as the default for every project")
    p_enforce.add_argument("--signal-ttl", type=int, default=None, dest="signal_ttl",
                           help="Seconds a c3_* call keeps native tools unlocked (default 600)")
    p_enforce.add_argument("--path", dest="project_path", default=".",
                           help="Project directory (default: current)")

    # ── Access Guard (v2.62.0) ──────────────────────────────────────────
    # Human-only mutation surface (frozen spec docs/access-guard.md §1):
    # rule changes happen here or in the UI tab, never via an agent tool.
    p_access = subparsers.add_parser(
        "access",
        help="Access Guard — path deny/read-only rules the agent must respect",
    )
    access_subs = p_access.add_subparsers(dest="access_cmd")

    ac_list = access_subs.add_parser(
        "list", help="Show rules per scope (builtin + global + project) and coverage"
    )
    ac_list.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    ac_add = access_subs.add_parser("add", help="Add a rule to the project (or --global) scope")
    ac_add.add_argument("glob", help="POSIX glob (** crosses directories), e.g. 'secrets/**' or '*.pem'")
    ac_add.add_argument("--kind", choices=["deny", "read_only"], required=True,
                        help="deny = no read/write/enumerate; read_only = no write")
    ac_add.add_argument("--global", dest="use_global", action="store_true",
                        help="Store in the global scope (~/.c3) so every C3 project enforces it")
    ac_add.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    ac_remove = access_subs.add_parser("remove", help="Remove a rule from the project (or --global) scope")
    ac_remove.add_argument("glob", help="The stored glob to remove (case-insensitive match)")
    ac_remove.add_argument("--kind", choices=["deny", "read_only"], required=True,
                           help="Which rule list the glob lives in")
    ac_remove.add_argument("--global", dest="use_global", action="store_true",
                           help="Remove from the global scope (~/.c3)")
    ac_remove.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    ac_check = access_subs.add_parser("check", help="Test a path: verdict + matched rule + refusal string")
    ac_check.add_argument("target", help="Path to test (absolute or project-relative)")
    ac_check.add_argument("--op", choices=["read", "write"], default="read",
                          help="Operation to evaluate (default: read)")
    ac_check.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    # ── Denial telemetry (v2.66.0, docs/access-guard.md §3) ─────────────
    # Answers "which rule is actually costing me time" with hit counts, and
    # names the lever that would clear each one.
    ac_stats = access_subs.add_parser(
        "stats", help="What got denied, how often, and how to unblock it")
    ac_stats.add_argument("--path", dest="project_path", default=".",
                          help="Project directory (default: current)")
    ac_stats.add_argument("--session", default="",
                          help="Limit to one session id (default: all retained)")
    ac_stats.add_argument("--limit", type=int, default=15,
                          help="Max rows to show (default: 15)")
    ac_stats.add_argument("--clear", action="store_true",
                          help="Delete the retained denial log and exit")
    ac_stats.add_argument("--json", dest="as_json", action="store_true",
                          help="Emit the aggregate as JSON")

    # ── Builtin opt-out (two-key) ───────────────────────────────────────
    # Builtins are on by default. Switching one off needs a config entry AND
    # a keyring attestation, so an agent that writes config.json alone still
    # cannot loosen the guard. Global scope only — project scopes may only
    # ever tighten. The credential-vault builtins are never disableable.
    ac_builtin = access_subs.add_parser(
        "builtin", help="Disable or re-enable a built-in guard (global, needs confirmation)")
    builtin_subs = ac_builtin.add_subparsers(dest="builtin_cmd")
    for _name, _help in (("disable", "Stop enforcing a built-in guard"),
                         ("enable", "Re-enforce a built-in guard")):
        _p = builtin_subs.add_parser(_name, help=_help)
        _p.add_argument("glob", help="One of: **/.env*, **/.c3/**, **/.claude/settings*.json, **/.git/**")
        _p.add_argument("--path", dest="project_path", default=".",
                        help="Project directory used for the audit log (default: current)")
        if _name == "disable":
            _p.add_argument("--yes", action="store_true",
                            help="Skip the typed confirmation (scripts/CI)")

    # ── Mask Guard (v2.63.0, docs/mask-guard.md) ────────────────────────
    # Masking exposes a path but transforms what the agent sees. Rules are
    # human-only, like the rest of Access Guard.
    ac_mask = access_subs.add_parser(
        "mask", help="Mask Guard — expose a path but transform what the agent sees")
    mask_subs = ac_mask.add_subparsers(dest="mask_cmd")

    am_add = mask_subs.add_parser("add", help="Add or replace a mask rule")
    am_add.add_argument("glob", help="POSIX glob, e.g. 'data/**' or '*.csv'")
    am_add.add_argument("--preset", required=True,
                        choices=["redact_secrets", "redact_columns",
                                 "sample_rows", "signatures_only"],
                        help="Deterministic transform applied to matching files")
    am_add.add_argument("--params", default="",
                        help="Preset params, e.g. 'count=20,strategy=first' "
                             "or 'columns=email,name'")
    am_add.add_argument("--global", dest="use_global", action="store_true",
                        help="Store in the global scope (~/.c3)")
    am_add.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    am_rm = mask_subs.add_parser("rm", help="Remove a mask rule")
    am_rm.add_argument("glob", help="The stored glob to remove")
    am_rm.add_argument("--global", dest="use_global", action="store_true",
                       help="Remove from the global scope (~/.c3)")
    am_rm.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    am_status = mask_subs.add_parser(
        "status", help="Activation state — whether masking is actually in effect")
    am_status.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    am_act = mask_subs.add_parser(
        "activate",
        help="Purge pre-mask derived artifacts, then build + validate views")
    am_act.add_argument("--reindex", action="store_true",
                        help="Also rebuild the code index in the same pass")
    am_act.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    am_prev = mask_subs.add_parser(
        "preview", help="Show exactly what the agent sees for one path")
    am_prev.add_argument("target", help="Path to preview (absolute or project-relative)")
    am_prev.add_argument("--path", dest="project_path", default=".", help="Project directory (default: current)")

    # ── Oracle Discovery API (v2.32.0) ──────────────────────────────────
    p_oracle = subparsers.add_parser(
        "oracle",
        help="Oracle dashboard server + Discovery API key management",
    )
    or_subs = p_oracle.add_subparsers(dest="oracle_cmd")
    or_serve = or_subs.add_parser(
        "serve",
        aliases=["start"],
        help="Launch the Oracle dashboard server (REST + MCP discovery endpoints)",
    )
    or_serve.add_argument("--port", type=int, default=None,
                          help="Server port (default: config 'port', 3331)")
    or_serve.add_argument("--no-browser", action="store_true",
                          help="Don't open the browser")
    or_open = or_subs.add_parser(
        "open",
        help="Sign in to a running dashboard via a single-use URL",
    )
    or_open.add_argument("--port", type=int, default=None,
                         help="Server port (default: config 'port', 3331)")
    or_open.add_argument("--no-browser", action="store_true",
                         help="Print the URL without opening a browser")
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
