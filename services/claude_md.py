"""
CLAUDE.md Management Service

Provides intelligent CLAUDE.md lifecycle tools:
- generate: Create CLAUDE.md from live project data + session/memory insights
- check_staleness: Detect drift between CLAUDE.md and actual project state
- compact: Reduce bloated CLAUDE.md while preserving critical info
- get_promotion_candidates: Surface high-value facts/patterns for inclusion

All methods are read-only — they return content/reports but never write to disk.
"""
import json
import re
from pathlib import Path
from typing import Optional

from core import count_tokens

# Default truncation limit (Claude Code truncates after 200 lines)
TRUNCATION_LIMIT = 200

C3_COMPACT_WORKFLOW = """\
## C3 Tools — MANDATORY (enforced by hooks)
Native tools (Read, Grep, Glob, Edit, Write) are **blocked by PreToolUse hooks** unless a c3_* tool \
was called first. Do NOT attempt native tools without prior c3_* usage — they will be denied.

**Native tools are permitted ONLY when:**
1. The c3_* tool failed or returned an error
2. The c3_* tool returned insufficient scope for a targeted follow-up
When falling back, state which c3_* tool was attempted and why it was insufficient.

## Workflow (follow this order — do not skip steps)
1. **RECALL**: `c3_memory(action='recall')` — before any multi-step or context-dependent task. Large memory stores: use `index` first (compact list), then `fetch` for specific IDs
2. **SEARCH FIRST**: `c3_search(action='code|files|semantic')` — before ANY file discovery or content search. Never start with Grep/Glob
3. **MAP before READ**: `c3_compress(mode='map')` then `c3_read(symbols=...|lines=...)` — for ANY file read. Never start with native Read. Use `mode='ast'` for knowledge-graph overview (requires codebase-memory-mcp)
4. **IMPACT** (shared symbols): `c3_impact(target='symbol')` — blast-radius check before editing any function/class used across files
5. **EDIT via C3**: `c3_edit(file_path, old_string, new_string, summary)` — for ALL edits. Parallel across files; `edits=[]` batch for same file
6. **FILTER**: `c3_filter(text=...)` — for terminal output >10 lines or log files
6.5. **SHELL via C3**: `c3_shell(cmd, cwd='', timeout=60)` — for tests, git, build, scripts. Returns structured `{exit_code, stdout, stderr, duration_ms}`. Auto-filters stdout >30 lines; auto-logs git-mutating commands (commit/add/merge/rebase/reset/restore/checkout) to the edit ledger. Blocks fork bombs and `rm -rf /` or `~`; soft-warns on `--force`, `--no-verify`, `reset --hard`. Native Bash remains the fallback for interactive/TTY commands
7. **VALIDATE**: `c3_validate(file_path)` — after edits or before reporting done. Runs deep type check (pyright/tsc) automatically if installed
8. **LOG**: `c3_session(action='log')` for decisions. `c3_session(action='snapshot')` before /clear
9. **DELEGATE**: `c3_delegate(task, backend='ollama|codex|gemini|claude|auto')` or `c3_agent(workflow=...)` for multi-model pipelines
10. **BITBUCKET** (when configured, v2.30.0+): `c3_bitbucket(action='...')` — for self-hosted enterprise Bitbucket Data Center / Server: PRs, branches, builds, repo admin. Tokens live in the OS keyring (set up via `c3 bitbucket login`). Read actions are safe in plan mode; write actions (`merge_pr`, `create_branch`, etc.) are auto-logged to the edit ledger.

## Plan mode
In plan mode, all c3_* read tools (search, read, compress, filter, validate, status) work normally — skip edit/delegate steps.

## Anti-patterns (DO NOT do these)
- Starting with native file search/read/grep without a prior c3_* call
- Using native Edit when c3_edit is available
- Reading entire files when c3_compress + c3_read would be more surgical
- Skipping c3_validate after making edits"""

# Ultra-compact workflow for nano mode (~250 tokens vs ~800 for full)
C3_NANO_WORKFLOW = """\
## C3 Tools — MANDATORY
Native tools BLOCKED unless c3_* called first. State reason when falling back.
1. c3_search(action='code|files|semantic') — BEFORE any search/grep/glob
2. c3_compress(mode='map') then c3_read(symbols=...|lines=...) — BEFORE any file read
3. c3_edit(file_path, old_string, new_string, summary) — for ALL edits; edits=[{...}] batch
4. c3_filter(text='...') — output >10 lines
5. c3_validate(file_path) — after edits
6. c3_session(action='log'|'snapshot') — decisions / before /clear
Plan mode: all c3_* read tools work normally — skip edit/delegate steps.
DO NOT: start with native Read/Grep/Glob/Edit, skip c3_validate, read full files without c3_compress."""


class ClaudeMdManager:
    """Manages instructions file generation, analysis, compaction, and insight promotion.

    Supports multiple IDEs — instructions_file determines the output filename
    (e.g. CLAUDE.md for Claude Code, .github/copilot-instructions.md for VS Code).
    """

    def __init__(self, project_path: str, session_mgr, indexer, memory,
                 instructions_file: str = "CLAUDE.md", line_limit: int = 200,
                 supports_hooks: bool = True, supports_clear: bool = True,
                 nano_mode: bool = False):
        self.project_path = Path(project_path)
        self.session_mgr = session_mgr
        self.indexer = indexer
        self.memory = memory
        self.instructions_file = instructions_file
        self.line_limit = line_limit
        self.supports_hooks = supports_hooks
        self.supports_clear = supports_clear
        self._nano_mode = nano_mode

    # ── Public API (one per MCP tool) ────────────────────────

    def _build_c3_workflow(self, nano: bool = False) -> str:
        """Build C3 workflow section.

        nano=True: ~250 tokens (vs ~800 full). Use for IDEs where instructions space is limited.
        Filters out hooks/snapshot/transcript lines for IDEs that don't support them.
        """
        if nano:
            workflow = C3_NANO_WORKFLOW
        else:
            workflow = C3_COMPACT_WORKFLOW

        # Strip features unsupported by this IDE to reduce irrelevant instruction tokens
        if not self.supports_clear:
            # Remove /clear and snapshot/restore references
            lines = workflow.splitlines()
            lines = [l for l in lines if '/clear' not in l and 'snapshot' not in l.lower()]
            workflow = '\n'.join(lines)
        if not self.supports_hooks:
            # Remove hook-specific log lines (hooks are Claude Code / Gemini only)
            lines = workflow.splitlines()
            lines = [l for l in lines if 'PostToolUse' not in l and 'AfterTool' not in l]
            workflow = '\n'.join(lines)

        return workflow

    def generate(self, include_sessions: bool = True, mode: str = "compact") -> dict:
        """Generate token-efficient CLAUDE.md from live project data.

        mode='compact' (default): full workflow + project tree + key facts (~2,000 tokens)
        mode='nano': minimal mandate only (~250 tokens) — project tree/facts served via c3_memory

        Optimized for minimal per-turn overhead:
        - Compact C3 tool reference (~7 lines vs ~16)
        - No session history (use c3_memory recall instead)
        - Top 5 learned facts only (rest available via c3_memory)
        - No shortcuts section (low value, costs tokens every turn)
        """
        if mode == "nano":
            self._nano_mode = True

        use_nano = getattr(self, '_nano_mode', False)

        # Nano mode: return minimal mandate only — project tree/facts served via c3_memory on demand
        if use_nano:
            content = self._build_c3_workflow(nano=True)
            metrics = self._count_metrics(content)
            return {
                "content": content,
                "lines": metrics["lines"],
                "tokens": metrics["tokens"],
                "mode": "nano",
                "truncation_warning": None,
            }

        parts = []

        # C3 workflow instructions (compact)
        parts.append(self._build_c3_workflow(nano=False))

        # Project structure
        parts.append("\n# Project Context\n")
        parts.append(self.session_mgr._scan_project_structure())

        # Tech stack
        parts.append("\n## Tech Stack\n")
        parts.append(self.session_mgr._detect_tech_stack())

        # Key files (compact)
        key_files = self._detect_key_files()
        if key_files:
            parts.append("\n## Key Files\n")
            for kf in key_files[:5]:
                parts.append(f"- `{kf['file']}` — {kf['reason']}")

        # Top learned facts only (rest available via c3_memory recall)
        promoted_facts = [
            f for f in self.memory.facts
            if f.get("relevance_count", 0) >= 3
        ]
        if promoted_facts:
            parts.append("\n## Key Facts (use c3_memory for more)\n")
            for f in promoted_facts[:5]:
                parts.append(f"- {f['fact'][:120]}")

        content = '\n'.join(parts)
        metrics = self._count_metrics(content)

        # Enforce line budget: progressively prune rather than silently truncate Key Facts
        if self.line_limit and metrics["lines"] > self.line_limit:
            # Pass 1: prune project structure to depth 1
            pruned_parts = []
            for part in parts:
                if part.strip().startswith("```") and "\n" in part:
                    part = self._prune_structure_depth(part, max_depth=1)
                pruned_parts.append(part)
            content = '\n'.join(pruned_parts)
            metrics = self._count_metrics(content)

        if self.line_limit and metrics["lines"] > self.line_limit:
            # Pass 2: drop key facts to 3
            rebuilt = []
            in_facts = False
            facts_shown = 0
            for line in content.splitlines():
                if line.startswith("## Key Facts"):
                    in_facts = True
                    rebuilt.append(line)
                    continue
                if in_facts and line.startswith("- "):
                    if facts_shown < 3:
                        rebuilt.append(line)
                        facts_shown += 1
                    continue
                if in_facts and line.startswith("## "):
                    in_facts = False
                rebuilt.append(line)
            content = '\n'.join(rebuilt)
            metrics = self._count_metrics(content)

        return {
            "content": content,
            "lines": metrics["lines"],
            "tokens": metrics["tokens"],
            "truncation_warning": (
                f"Content is {metrics['lines']} lines — exceeds limit of {self.line_limit}. "
                "Run `c3 claudemd compact` to reduce further."
            ) if self.line_limit and metrics["lines"] > self.line_limit else None,
        }

    def check_staleness(self) -> dict:
        """Check existing CLAUDE.md for staleness and drift."""
        current = self._read_current()
        if current is None:
            return {
                "status": "missing",
                "issues": [{
                    "severity": "error",
                    "message": f"No {self.instructions_file} found. Use CLI `c3 claudemd generate` to create one.",
                }],
            }

        issues = []
        sections = self._parse_sections(current)
        metrics = self._count_metrics(current)

        # Size warning (only if line_limit is set)
        if self.line_limit and metrics["lines"] > self.line_limit:
            issues.append({
                "severity": "warning",
                "message": (
                    f"{self.instructions_file} is {metrics['lines']} lines ({metrics['tokens']} tokens). "
                    f"Truncation may occur after {self.line_limit} lines. "
                    "Use CLI `c3 claudemd compact` to reduce."
                ),
            })

        # Structure drift
        structure_issues = self._diff_structure(current)
        issues.extend(structure_issues)

        # Tech stack drift
        tech_issues = self._diff_tech_stack(current)
        issues.extend(tech_issues)

        # Session staleness
        session_files = sorted(
            (self.project_path / ".c3" / "sessions").glob("session_*.json"),
            reverse=True,
        ) if (self.project_path / ".c3" / "sessions").exists() else []

        session_section = sections.get("Session History (Compressed)", "")
        if session_files:
            # Count sessions mentioned in CLAUDE.md
            mentioned_ids = set(re.findall(r'Session:\s*(\d{8}_\d{6})', session_section))
            total_sessions = len(session_files)
            unmentioned = total_sessions - len(mentioned_ids)
            if unmentioned > 3:
                issues.append({
                    "severity": "info",
                    "message": f"{unmentioned} sessions not reflected in CLAUDE.md. Consider regenerating.",
                })

        if not issues:
            issues.append({
                "severity": "info",
                "message": "CLAUDE.md looks up to date.",
            })

        return {
            "status": "ok" if all(i["severity"] == "info" for i in issues) else "stale",
            "lines": metrics["lines"],
            "tokens": metrics["tokens"],
            "issues": issues,
        }

    def compact(self, target_lines: int = 150) -> dict:
        """Compact existing CLAUDE.md to fit within target line count."""
        current = self._read_current()
        if current is None:
            return {"error": f"No {self.instructions_file} found on disk. Use CLI `c3 claudemd generate` to preview, then `c3 claudemd save` to persist before compacting."}

        original_metrics = self._count_metrics(current)
        sections = self._parse_sections(current)
        lines = current.split('\n')

        # If already under target, no compaction needed
        if original_metrics["lines"] <= target_lines:
            return {
                "content": current,
                "original_lines": original_metrics["lines"],
                "compacted_lines": original_metrics["lines"],
                "original_tokens": original_metrics["tokens"],
                "compacted_tokens": original_metrics["tokens"],
                "actions": ["Already under target — no compaction needed."],
            }

        actions = []

        # Step 1: Compress session history — keep last 3, one-line summaries
        if "Session History (Compressed)" in sections:
            session_text = sections["Session History (Compressed)"]
            compressed = self._compress_sessions(session_text, max_sessions=3)
            if len(compressed.split('\n')) < len(session_text.split('\n')):
                sections["Session History (Compressed)"] = compressed
                actions.append("Trimmed session history to last 3 sessions with one-line summaries")

        # Step 2: Deduplicate — remove exact duplicate lines (excluding blank lines and headers)
        seen_lines = set()
        deduped_sections = {}
        for name, text in sections.items():
            if name in ("User Notes", "C3 — Token-Saving Workflow (MUST FOLLOW)"):
                deduped_sections[name] = text
                continue
            new_lines = []
            for line in text.split('\n'):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    new_lines.append(line)
                elif stripped not in seen_lines:
                    seen_lines.add(stripped)
                    new_lines.append(line)
            deduped_sections[name] = '\n'.join(new_lines)
        dup_removed = sum(
            len(sections[k].split('\n')) - len(deduped_sections[k].split('\n'))
            for k in sections
        )
        if dup_removed > 0:
            actions.append(f"Removed {dup_removed} duplicate lines")
            sections = deduped_sections

        # Step 3: Prune structure tree depth if still over target
        content = self._reassemble_sections(sections)
        current_lines = len(content.split('\n'))
        if current_lines > target_lines and "Project Context (Auto-generated by C3)" in sections:
            ctx_section = sections["Project Context (Auto-generated by C3)"]
            pruned = self._prune_structure_depth(ctx_section, max_depth=2)
            if len(pruned.split('\n')) < len(ctx_section.split('\n')):
                sections["Project Context (Auto-generated by C3)"] = pruned
                actions.append("Reduced project structure tree depth")

        # Reassemble
        content = self._reassemble_sections(sections)
        compacted_metrics = self._count_metrics(content)

        if not actions:
            actions.append("No compaction opportunities found.")

        return {
            "content": content,
            "original_lines": original_metrics["lines"],
            "compacted_lines": compacted_metrics["lines"],
            "original_tokens": original_metrics["tokens"],
            "compacted_tokens": compacted_metrics["tokens"],
            "actions": actions,
        }

    def get_promotion_candidates(self, min_relevance: int = 2) -> dict:
        """Find facts and patterns worth promoting into CLAUDE.md."""
        current = self._read_current()
        current_text = current or ""
        candidates = {
            "Code Patterns & Conventions": [],
            "Quick Reference Shortcuts": [],
            "Key Files": [],
            "Project Roadmap & Active Plans": [],
        }

        # High-relevance facts
        for fact in self.memory.facts:
            if fact.get("relevance_count", 0) < min_relevance:
                continue
            # Skip if already in CLAUDE.md
            if fact["fact"] in current_text:
                continue

            category = fact.get("category", "general")
            target = "Code Patterns & Conventions"
            if category in ("shortcut", "reference", "alias"):
                target = "Quick Reference Shortcuts"
            elif category in ("file", "path", "entry_point"):
                target = "Key Files"
            elif category in ("plan", "roadmap", "todo"):
                target = "Project Roadmap & Active Plans"

            candidates[target].append({
                "fact": fact["fact"],
                "category": category,
                "relevance_count": fact["relevance_count"],
                "snippet": f"- [{category}] {fact['fact']}",
            })

        # Recurring decisions and plans from sessions
        session_dir = self.project_path / ".c3" / "sessions"
        if session_dir.exists():
            decision_keywords = {}  # keyword -> [session_ids]
            active_plans = []  # List of unique plan strings
            for sf in sorted(session_dir.glob("session_*.json"), reverse=True)[:20]:
                try:
                    with open(sf, encoding='utf-8') as f:
                        s = json.load(f)
                    sid = s.get("id", "unknown")
                    for d in s.get("decisions", []):
                        text = d.get("decision", "")
                        # Plan detection
                        if "PLAN:" in text.upper():
                            plan_text = text.split("PLAN:", 1)[1].strip()
                            if plan_text and not any(p["fact"] == plan_text for p in active_plans):
                                active_plans.append({
                                    "fact": plan_text,
                                    "category": "active_plan",
                                    "relevance_count": 1,
                                    "snippet": f"- [PLAN] {plan_text}"
                                })

                        # Decision keyword extraction (5+ chars)
                        words = set(re.findall(r'[a-zA-Z]{5,}', text.lower()))
                        for w in words:
                            if w not in decision_keywords:
                                decision_keywords[w] = []
                            if sid not in decision_keywords[w]:
                                decision_keywords[w].append(sid)
                except Exception:
                    continue

            # Add unique plans to roadmap
            for p in active_plans:
                if p["fact"] not in current_text:
                    candidates["Project Roadmap & Active Plans"].append(p)

            # Keywords appearing in 2+ sessions
            recurring = {k: v for k, v in decision_keywords.items() if len(v) >= 2}
            for keyword, session_ids in sorted(recurring.items(), key=lambda x: -len(x[1]))[:5]:
                snippet = f"- Recurring decision keyword: \"{keyword}\" (across {len(session_ids)} sessions)"
                if snippet not in current_text:
                    candidates["Code Patterns & Conventions"].append({
                        "fact": f"Recurring decision keyword: \"{keyword}\"",
                        "category": "recurring_decision",
                        "relevance_count": len(session_ids),
                        "snippet": snippet,
                    })

        # Filter out empty groups
        candidates = {k: v for k, v in candidates.items() if v}

        total = sum(len(v) for v in candidates.values())
        return {
            "total_candidates": total,
            "candidates": candidates,
            "message": (
                f"Found {total} promotion candidates across {len(candidates)} sections."
                if total > 0
                else "No promotion candidates found. Build more session history and facts first."
            ),
        }

    # ── Shared helpers ───────────────────────────────────────

    def _read_current(self) -> Optional[str]:
        """Read existing instructions file from project root."""
        path = self.project_path / self.instructions_file
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _parse_sections(self, content: str) -> dict:
        """Split CLAUDE.md into named sections by # or ## headers."""
        sections = {}
        current_name = "_preamble"
        current_lines = []

        for line in content.split('\n'):
            header_match = re.match(r'^(#{1,3})\s+(.+)', line)
            if header_match:
                # Save previous section
                if current_lines or current_name != "_preamble":
                    sections[current_name] = '\n'.join(current_lines)
                current_name = header_match.group(2).strip()
                current_lines = []
            else:
                current_lines.append(line)

        # Save last section
        if current_lines or current_name != "_preamble":
            sections[current_name] = '\n'.join(current_lines)

        return sections

    def _reassemble_sections(self, sections: dict) -> str:
        """Reassemble sections into CLAUDE.md content."""
        parts = []
        for name, body in sections.items():
            if name == "_preamble":
                if body.strip():
                    parts.append(body)
            else:
                # Determine header level from body context (default ##)
                level = "#"
                if name in ("Project Context (Auto-generated by C3)",
                            "Session History (Compressed)", "User Notes"):
                    level = "#"
                else:
                    level = "##"
                parts.append(f"{level} {name}\n{body}")
        return '\n\n'.join(parts)

    def _count_metrics(self, content: str) -> dict:
        """Count lines and tokens."""
        lines = len(content.split('\n'))
        tokens = count_tokens(content)
        return {"lines": lines, "tokens": tokens}

    # ── Generate helpers ─────────────────────────────────────

    def _detect_enhanced_patterns(self) -> list:
        """Detect patterns beyond what SessionManager finds — linting, test frameworks, monorepo."""
        patterns = []
        p = self.project_path

        # Base patterns from session manager
        base = self.session_mgr._detect_patterns()
        if base and base != "No patterns auto-detected":
            for line in base.split('\n'):
                line = line.strip().lstrip('- ')
                if line:
                    patterns.append(line)

        # Linting / formatting
        linting_indicators = {
            ".eslintrc": "ESLint", ".eslintrc.js": "ESLint", ".eslintrc.json": "ESLint",
            ".eslintrc.yml": "ESLint", "eslint.config.js": "ESLint (flat config)",
            ".prettierrc": "Prettier", ".prettierrc.json": "Prettier",
            "prettier.config.js": "Prettier",
            ".flake8": "Flake8", "setup.cfg": "Python config (setup.cfg)",
            "ruff.toml": "Ruff", ".ruff.toml": "Ruff",
            ".stylelintrc": "Stylelint",
            "biome.json": "Biome",
        }
        for filename, tool in linting_indicators.items():
            if (p / filename).exists():
                patterns.append(f"Uses {tool}")

        # Check pyproject.toml for tool configs
        pyproject = p / "pyproject.toml"
        if pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8")
                if "[tool.ruff" in text:
                    patterns.append("Uses Ruff (via pyproject.toml)")
                if "[tool.black" in text:
                    patterns.append("Uses Black formatter")
                if "[tool.pytest" in text or "[tool.pytest.ini_options" in text:
                    patterns.append("Uses pytest")
                if "[tool.mypy" in text:
                    patterns.append("Uses mypy type checking")
            except Exception:
                pass

        # Test frameworks
        if (p / "jest.config.js").exists() or (p / "jest.config.ts").exists():
            patterns.append("Uses Jest for testing")
        if (p / "vitest.config.ts").exists() or (p / "vitest.config.js").exists():
            patterns.append("Uses Vitest for testing")
        if (p / "pytest.ini").exists() or (p / "conftest.py").exists():
            patterns.append("Uses pytest")

        # Monorepo indicators
        if (p / "lerna.json").exists():
            patterns.append("Monorepo (Lerna)")
        if (p / "pnpm-workspace.yaml").exists():
            patterns.append("Monorepo (pnpm workspaces)")
        if (p / "turbo.json").exists():
            patterns.append("Monorepo (Turborepo)")
        pkg = p / "package.json"
        if pkg.exists():
            try:
                with open(pkg, encoding='utf-8') as f:
                    data = json.load(f)
                if "workspaces" in data:
                    patterns.append("Monorepo (npm/yarn workspaces)")
            except Exception:
                pass

        # Deduplicate
        seen = set()
        unique = []
        for pat in patterns:
            key = pat.lower()
            if key not in seen:
                seen.add(key)
                unique.append(pat)

        return unique

    def _detect_key_files(self) -> list:
        """Identify key files from session history and conventional entry points."""
        key_files = []
        seen = set()

        # Hot files from session history
        session_dir = self.project_path / ".c3" / "sessions"
        if session_dir.exists():
            file_counts = {}
            for sf in sorted(session_dir.glob("session_*.json"), reverse=True)[:20]:
                try:
                    with open(sf, encoding='utf-8') as f:
                        s = json.load(f)
                    for ft in s.get("files_touched", []):
                        fname = ft.get("file", "")
                        if fname:
                            file_counts[fname] = file_counts.get(fname, 0) + 1
                except Exception:
                    continue

            for fname, count in sorted(file_counts.items(), key=lambda x: -x[1])[:5]:
                if count >= 2 and fname not in seen:
                    key_files.append({"file": fname, "reason": f"edited in {count} sessions"})
                    seen.add(fname)

        # Conventional entry points
        entry_points = [
            ("main.py", "Python entry point"),
            ("app.py", "Application entry point"),
            ("index.ts", "TypeScript entry point"),
            ("index.js", "JavaScript entry point"),
            ("src/index.ts", "Source entry point"),
            ("src/index.js", "Source entry point"),
            ("src/main.ts", "Source entry point"),
            ("src/App.tsx", "React app root"),
            ("cli/mcp_server.py", "MCP server entry"),
        ]
        for filepath, reason in entry_points:
            if (self.project_path / filepath).exists() and filepath not in seen:
                key_files.append({"file": filepath, "reason": reason})
                seen.add(filepath)

        return key_files

    # ── Check helpers ────────────────────────────────────────

    def _diff_structure(self, current_content: str) -> list:
        """Find dirs mentioned in CLAUDE.md that don't exist, and new dirs not mentioned."""
        issues = []

        # Extract dir-like references from the code block
        mentioned_dirs = set()
        in_code_block = False
        for line in current_content.split('\n'):
            if line.strip().startswith('```'):
                in_code_block = not in_code_block
                continue
            if in_code_block and line.strip().endswith('/'):
                dirname = line.strip().rstrip('/')
                if dirname:
                    mentioned_dirs.add(dirname)

        # Scan actual top-level dirs
        skip = {'node_modules', '.git', '__pycache__', '.c3', 'venv',
                'env', '.venv', 'dist', 'build', '.next', '.cache', '.claude'}
        actual_dirs = set()
        for item in self.project_path.iterdir():
            if item.is_dir() and item.name not in skip and not item.name.startswith('.'):
                actual_dirs.add(item.name)

        # Compare (use base names only)
        mentioned_basenames = {d.split('/')[-1] for d in mentioned_dirs if d}

        missing_in_fs = mentioned_basenames - actual_dirs
        new_in_fs = actual_dirs - mentioned_basenames

        for d in missing_in_fs:
            # Skip the project root name
            if d == self.project_path.name:
                continue
            issues.append({
                "severity": "warning",
                "message": f"Directory '{d}' mentioned in CLAUDE.md but not found on disk.",
            })

        for d in new_in_fs:
            issues.append({
                "severity": "info",
                "message": f"New directory '{d}' exists but is not in CLAUDE.md.",
            })

        return issues

    def _diff_tech_stack(self, current_content: str) -> list:
        """Compare tech stack in CLAUDE.md vs detected."""
        issues = []
        detected = self.session_mgr._detect_tech_stack()

        if detected == "Could not auto-detect":
            return issues

        detected_set = {t.strip().lower() for t in detected.split(',')}

        # Find the tech stack line in CLAUDE.md
        sections = self._parse_sections(current_content)
        claimed_text = sections.get("Tech Stack", "")
        claimed_set = set()
        for line in claimed_text.split('\n'):
            line = line.strip().lstrip('- ')
            if line:
                for item in line.split(','):
                    item = item.strip().lower()
                    if item:
                        claimed_set.add(item)

        new_tech = detected_set - claimed_set
        for tech in new_tech:
            issues.append({
                "severity": "warning",
                "message": f"Detected '{tech}' in project but not listed in CLAUDE.md Tech Stack.",
            })

        return issues

    # ── Compact helpers ──────────────────────────────────────

    def _compress_sessions(self, session_text: str, max_sessions: int = 3) -> str:
        """Trim session history to last N sessions with one-line summaries."""
        # Split into individual session blocks (## Session: ...)
        blocks = re.split(r'(?=## Session:)', session_text)
        blocks = [b.strip() for b in blocks if b.strip()]

        if len(blocks) <= max_sessions:
            return session_text

        # Keep only last max_sessions, compress each to one line
        kept = blocks[:max_sessions]
        compressed_lines = []
        for block in kept:
            lines = block.split('\n')
            header = lines[0] if lines else ""
            # Extract summary if present
            summary = ""
            for line in lines[1:]:
                if line.startswith("**Summary:**"):
                    summary = line.replace("**Summary:**", "").strip()
                    break
                elif line.startswith("**When:**"):
                    date = line.replace("**When:**", "").strip()
                    summary = f"({date}) {summary}"
            if summary:
                compressed_lines.append(f"{header}\n**Summary:** {summary}\n")
            else:
                compressed_lines.append(f"{header}\n")

        return '\n'.join(compressed_lines)

    def _prune_structure_depth(self, section_text: str, max_depth: int = 2) -> str:
        """Reduce project structure tree depth."""
        lines = section_text.split('\n')
        pruned = []
        in_code_block = False

        for line in lines:
            if line.strip().startswith('```'):
                in_code_block = not in_code_block
                pruned.append(line)
                continue

            if in_code_block:
                # Count indent level (2 spaces per level)
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                depth = indent // 2
                if depth <= max_depth:
                    pruned.append(line)
            else:
                pruned.append(line)

        return '\n'.join(pruned)
