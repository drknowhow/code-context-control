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

    p_mcp_install = subparsers.add_parser("mcp-install", help="Alias for install-mcp")
    p_mcp_install.add_argument("targets", nargs="*", help="Optional project path and/or IDE shorthand")
    p_mcp_install.add_argument("--ide", default="auto", type=parse_cli_ide_arg, metavar="{auto,claude,vscode,cursor,codex,gemini,antigravity}", help="Target IDE (default: auto-detect)")
    p_mcp_install.add_argument("--mcp-mode", choices=["direct", "proxy"], default="direct", help="MCP entrypoint mode (default: direct)")

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

    p_e2e = subparsers.add_parser("benchmark-e2e", help="Run end-to-end AI session benchmark (C3 vs baseline)")
    p_e2e.add_argument("project_path", nargs="?", default=".", help="Project path to benchmark")
    p_e2e.add_argument("--providers", default=None, help="Comma-separated: claude,gemini,codex (default: auto-detect)")
    p_e2e.add_argument("--models", default=None, help="Model overrides: claude=sonnet,gemini=gemini-2.5-flash,codex=o3")
    p_e2e.add_argument("--tasks", default="all", help="Task filter: all (default high-signal categories), or comma-separated: architecture,call_chain,code_review,bug_injection,multi_file_trace,explanation,file_discovery,etc.")
    p_e2e.add_argument("--max-tasks", type=int, default=1, help="Max tasks per category (default: 1)")
    p_e2e.add_argument("--timeout", type=int, default=120, help="Per-task timeout in seconds (default: 120)")
    p_e2e.add_argument("--no-parallel", action="store_true", help="Run providers sequentially instead of in parallel")
    p_e2e.add_argument("--judge", default=None, help="Enable AI-as-judge scoring with this CLI (e.g. claude, gemini)")
    p_e2e.add_argument("--judge-model", default=None, help="Model override for the judge CLI")
    p_e2e.add_argument("--json", action="store_true", help="Emit JSON report to stdout")
    p_e2e.add_argument("--output", help="Write JSON report to this path")
    p_e2e.add_argument("--html-output", help="Write HTML report to this path")
    p_e2e.add_argument("--dry-run", action="store_true", help="Show tasks and providers without running")
    p_e2e.add_argument("--verbose", action="store_true", help="Print each result as it completes")
    p_e2e.add_argument("--task-workers", type=int, default=1,
                       help="Run N tasks concurrently (default: 1). Higher values are faster but may hit rate limits.")
    p_e2e.add_argument("--no-cache", action="store_true",
                       help="Ignore cached results and re-run all tasks (cache is enabled by default, TTL=24h)")
    p_e2e.add_argument("--permission-mode", default="bypassPermissions",
                       help="Permission mode for AI CLI (default: bypassPermissions). Use 'plan' for read-only mode.")

    return parser
