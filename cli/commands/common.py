"""Shared lightweight CLI command handlers."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandDeps:
    load_config: object
    print_header: object
    print_savings: object
    count_tokens: object
    format_token_count: object
    CodeIndex: object
    CodeCompressor: object
    CompressionProtocol: object
    SessionManager: object
    HAS_RICH: bool
    Table: object
    console: object
    __file__: str


def cmd_index(args, deps: CommandDeps):
    """Rebuild the code index."""
    config = deps.load_config()
    project_path = config.get("project_path", ".")

    deps.print_header("Rebuilding Code Index")
    indexer = deps.CodeIndex(project_path)
    result = indexer.build_index(max_files=args.max_files or 500)

    if deps.HAS_RICH:
        table = deps.Table(title="Index Results")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Files Indexed", str(result["files_indexed"]))
        table.add_row("Chunks Created", str(result["chunks_created"]))
        table.add_row("Unique Symbols", str(result["unique_symbols"]))
        deps.console.print(table)
    else:
        print(f"  Files: {result['files_indexed']}, Chunks: {result['chunks_created']}, Symbols: {result['unique_symbols']}")


def cmd_compress(args, deps: CommandDeps):
    """Compress a file and show results."""
    config = deps.load_config()
    mode = args.mode or "smart"

    compressor = deps.CodeCompressor(str(Path(config.get("project_path", ".")) / ".c3/cache"))
    result = compressor.compress_file(args.file, mode)

    if "error" in result:
        print(f"Error: {result['error']}")
        return

    deps.print_header(f"Compressed: {args.file} (mode: {result.get('mode', mode)})")
    deps.print_savings(result)

    if args.output:
        print(f"\n--- Compressed Output ---\n{result['compressed']}")


def cmd_context(args, deps: CommandDeps):
    """Get relevant context for a query."""
    config = deps.load_config()
    project_path = config.get("project_path", ".")

    indexer = deps.CodeIndex(project_path)
    context = indexer.get_context(
        args.query,
        top_k=args.top_k or 5,
        max_tokens=args.max_tokens or 4000,
    )

    if args.pipe:
        print(context)
    else:
        deps.print_header(f"Context for: {args.query}")
        tokens = deps.count_tokens(context)
        print(f"  Context tokens: {deps.format_token_count(tokens)}")
        print(f"\n{context}")


def cmd_encode(args, deps: CommandDeps):
    """Encode text to compressed format."""
    config = deps.load_config()
    protocol = deps.CompressionProtocol(config.get("project_path", "."))

    text = " ".join(args.text)
    result = protocol.encode(text)

    if args.pipe:
        print(result["compressed"])
    else:
        deps.print_header("Compression Protocol - Encode")
        print(f"  Original:   {result['original']}")
        print(f"  Compressed: {result['compressed']}")
        deps.print_savings(result)


def cmd_decode(args, deps: CommandDeps):
    """Decode compressed text back to readable format."""
    config = deps.load_config()
    protocol = deps.CompressionProtocol(config.get("project_path", "."))

    text = " ".join(args.text)
    decoded = protocol.decode(text)
    print(decoded)


def cmd_session(args, deps: CommandDeps):
    """Session management commands."""
    config = deps.load_config()
    sm = deps.SessionManager(config.get("project_path", "."))

    if args.session_cmd == "start":
        desc = " ".join(args.extra) if args.extra else ""
        result = sm.start_session(desc)
        print(f"Session started: {result['session_id']}")

    elif args.session_cmd == "save":
        summary = " ".join(args.extra) if args.extra else ""
        result = sm.save_session(summary)
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            print(f"Session {result['session_id']} saved ({result['decisions']} decisions, {result['files']} files)")

    elif args.session_cmd == "load":
        session_id = args.extra[0] if args.extra else "latest"
        session = sm.load_session(session_id)
        if "error" in session:
            print(f"Error: {session['error']}")
        else:
            print(json.dumps(session, indent=2))

    elif args.session_cmd == "list":
        sessions = sm.list_sessions()
        if deps.HAS_RICH:
            table = deps.Table(title="Session History")
            table.add_column("ID", style="cyan")
            table.add_column("Date", style="green")
            table.add_column("Summary", style="white")
            table.add_column("Decisions", style="yellow")
            for session in sessions:
                table.add_row(session["id"], session["started"], session["summary"][:60], str(session["decisions"]))
            deps.console.print(table)
        else:
            for session in sessions:
                print(f"  {session['id']} | {session['started']} | {session['summary'][:50]} | {session['decisions']} decisions")

    elif args.session_cmd == "context":
        context = sm.get_session_context()
        print(context)


def cmd_claudemd(args, deps: CommandDeps):
    """Instructions file generation commands."""
    config = deps.load_config()
    project_path = config.get("project_path", ".")
    sm = deps.SessionManager(project_path)

    from core.ide import get_profile as _get_profile
    from core.ide import load_ide_config
    from services.claude_md import ClaudeMdManager
    from services.memory import MemoryStore

    ide_name = load_ide_config(project_path)
    profile = _get_profile(ide_name)
    instructions_file = profile.instructions_file or "CLAUDE.md"

    if args.claudemd_cmd in ("generate", "save"):
        indexer = deps.CodeIndex(project_path)
        indexer._load_index()
        memory = MemoryStore(project_path)
        claude_md = ClaudeMdManager(
            project_path,
            sm,
            indexer,
            memory,
            instructions_file=instructions_file,
            line_limit=profile.instructions_line_limit or 200,
            supports_hooks=profile.supports_hooks,
            supports_clear=profile.supports_clear,
        )

        mode = "nano" if getattr(args, "nano", False) else "compact"
        gen = claude_md.generate(mode=mode)
        content = gen.get("content", "")
        tokens = gen.get("tokens", 0)
        mode_label = " [nano]" if mode == "nano" else ""

        if args.claudemd_cmd == "generate":
            deps.print_header(f"Generated {instructions_file}{mode_label} ({deps.format_token_count(tokens)} tokens)")
            print(content)
        else:
            output_path = Path(project_path) / instructions_file
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                existing = output_path.read_text(encoding="utf-8", errors="replace")
                if "# User Notes" in existing:
                    user_section = existing[existing.index("# User Notes"):]
                    content += f"\n\n{user_section}"
            output_path.write_text(content, encoding="utf-8")
            print(f"{instructions_file} saved to {output_path} ({tokens} tokens)")

    elif args.claudemd_cmd == "check":
        indexer = deps.CodeIndex(project_path)
        indexer._load_index()
        memory = MemoryStore(project_path)
        claude_md = ClaudeMdManager(
            project_path,
            sm,
            indexer,
            memory,
            instructions_file=instructions_file,
            line_limit=profile.instructions_line_limit or 200,
            supports_hooks=profile.supports_hooks,
            supports_clear=profile.supports_clear,
        )
        result = claude_md.check_staleness()
        deps.print_header(f"{instructions_file} Health - {result['status'].upper()}")
        if "lines" in result:
            print(f"  Size: {result['lines']} lines, {result['tokens']} tokens")
        for issue in result.get("issues", []):
            icon = {"error": "[ERROR]", "warning": "[WARN]", "info": "[INFO]"}.get(issue["severity"], "[?]")
            print(f"  {icon} {issue['message']}")


def cmd_stats(args, deps: CommandDeps):
    """Show comprehensive stats."""
    config = deps.load_config()
    project_path = config.get("project_path", ".")

    deps.print_header("C3 Statistics")
    indexer = deps.CodeIndex(project_path)
    idx_stats = indexer.get_stats()
    protocol = deps.CompressionProtocol(project_path)
    proto_stats = protocol.get_stats()

    if deps.HAS_RICH:
        table = deps.Table(title="System Overview")
        table.add_column("Component", style="cyan")
        table.add_column("Metric", style="white")
        table.add_column("Value", style="green")

        table.add_row("Index", "Files Indexed", str(idx_stats.get("files_indexed", 0)))
        table.add_row("Index", "Total Chunks", str(idx_stats.get("total_chunks", 0)))
        table.add_row("Index", "Codebase Tokens", deps.format_token_count(idx_stats.get("total_tokens_in_codebase", 0)))
        table.add_row("Index", "Index Size", f"{idx_stats.get('index_size_kb', 0)} KB")
        table.add_row("Protocol", "Built-in Codes", str(proto_stats.get("built_in_actions", 0) + proto_stats.get("built_in_terms", 0)))
        table.add_row("Protocol", "Custom Terms", str(proto_stats.get("custom_terms", 0)))

        deps.console.print(table)
    else:
        print(f"  Index: {idx_stats.get('files_indexed', 0)} files, {idx_stats.get('total_chunks', 0)} chunks")
        print(f"  Codebase: {deps.format_token_count(idx_stats.get('total_tokens_in_codebase', 0))} tokens")
        print(f"  Protocol: {proto_stats.get('total_codes', 0)} compression codes")


def cmd_optimize(args, deps: CommandDeps):
    """Show optimization suggestions."""
    config = deps.load_config()
    sm = deps.SessionManager(config.get("project_path", "."))

    deps.print_header("Optimization Suggestions")
    suggestions = sm.get_optimization_suggestions()
    for i, suggestion in enumerate(suggestions, 1):
        print(f"  {i}. {suggestion}")


def cmd_pipe(args, deps: CommandDeps):
    """All-in-one pipeline: get context + output for piping to Claude."""
    config = deps.load_config()
    project_path = config.get("project_path", ".")
    query = " ".join(args.query)

    indexer = deps.CodeIndex(project_path)
    context = indexer.get_context(query, top_k=args.top_k or 5, max_tokens=args.max_tokens or 4000)

    sm = deps.SessionManager(project_path)
    session_context = sm.get_session_context(n_sessions=2)

    output_parts = []
    if session_context and "No previous" not in session_context:
        output_parts.append(session_context)

    output_parts.append(context)
    output_parts.append(f"\n## Task\n{query}")
    print("\n\n".join(output_parts))


def cmd_ui(args, deps: CommandDeps):
    """Launch the web UI."""
    server_path = Path(deps.__file__).parent / "server.py"
    spec = importlib.util.spec_from_file_location("server", str(server_path))
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    project_path = args.project_path or "."
    server.run_server(
        project_path,
        port=args.port,
        open_browser=not args.no_browser,
        silent=args.silent,
        nano=args.nano,
    )
