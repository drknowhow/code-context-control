"""
E2E Benchmark Task Library — dynamically generated tasks with verifiable ground truths.

Tasks are built from codebase analysis so they work on any project.
Each task has objectively verifiable ground truths derived from the index/file memory.

Task categories:
  - explanation (easy): Single-symbol explanation
  - file_discovery (easy): Locate where a symbol is defined
  - dependency_analysis (medium): Analyze file imports
  - architecture (medium/hard): Project structure understanding
  - call_chain (hard): Cross-file reference tracing
  - code_review (hard): Quality analysis of complex files
  - multi_file_trace (hard): Data flow across multiple files
  - large_file_needle (hard): Find specific detail in a large file
  - refactor_suggestion (expert): Cross-file duplication analysis
  - bug_injection (medium): Detect planted syntax/logic issues
"""

from __future__ import annotations

import random
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Difficulty weights — harder tasks count more in weighted scoring
DIFFICULTY_WEIGHTS = {
    "easy": 0.5,
    "medium": 1.0,
    "hard": 2.0,
    "expert": 3.0,
}


@dataclass
class GroundTruth:
    """Verifiable facts about the expected answer."""
    required_keywords: list[str] = field(default_factory=list)
    forbidden_keywords: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    expected_symbols: list[str] = field(default_factory=list)
    expected_answer_summary: str = ""
    # Factual claims that can be verified: list of (claim_text, is_true) tuples
    verifiable_claims: list[tuple[str, bool]] = field(default_factory=list)
    # Multi-part: list of sub-questions that should each be addressed
    required_aspects: list[str] = field(default_factory=list)
    scoring_weights: dict = field(default_factory=lambda: {
        "keyword": 0.15, "structural": 0.10, "file_mention": 0.15,
        "factual": 0.35, "completeness": 0.25,
    })


@dataclass
class E2ETask:
    """A single benchmark task with ground truth for scoring."""
    id: str
    category: str
    query: str
    ground_truth: GroundTruth
    difficulty: str = "medium"
    target_files: list[str] = field(default_factory=list)
    suggested_tools: list[str] = field(default_factory=list)
    multi_turn: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "query": self.query,
            "difficulty": self.difficulty,
            "target_files": self.target_files,
            "suggested_tools": self.suggested_tools,
            "multi_turn": self.multi_turn,
            "ground_truth": {
                "required_keywords": self.ground_truth.required_keywords,
                "forbidden_keywords": self.ground_truth.forbidden_keywords,
                "expected_files": self.ground_truth.expected_files,
                "expected_symbols": self.ground_truth.expected_symbols,
                "expected_answer_summary": self.ground_truth.expected_answer_summary,
                "verifiable_claims_count": len(self.ground_truth.verifiable_claims),
                "required_aspects": self.ground_truth.required_aspects,
            },
        }


TASK_PROMPT_TEMPLATE = (
    "Use C3 MCP tools (not native Read/Grep/Glob). "
    "Be concise, cite file paths and line numbers.\n\n"
    "Question: {query}"
)

# Categories included in benchmark runs.  Others still exist for ad-hoc use
# via --tasks but are excluded by default because they produce low or zero
# score delta between C3 and baseline.
BENCHMARK_CATEGORIES: set[str] = {
    "call_chain",
    "code_review",
    "bug_injection",
    "architecture",
    "multi_file_trace",
}

# Maps task category -> recommended C3 tools
_CATEGORY_TOOL_HINTS: dict[str, list[str]] = {
    "code_review": ["c3_compress(mode='bug_scan')", "c3_read"],
    "file_discovery": ["c3_search(action='files')"],
    "architecture": ["c3_compress(mode='map')", "c3_memory"],
    "bug_injection": ["c3_compress(mode='bug_scan')", "c3_validate"],
    "call_chain": ["c3_search(action='code')", "c3_read(symbols=[...])"],
    "explanation": ["c3_read(symbols=[...])", "c3_compress"],
    "refactor_suggestion": ["c3_compress(mode='map')", "c3_search"],
    "dependency_analysis": ["c3_search(action='code')", "c3_compress(mode='map')"],
    "multi_file_trace": ["c3_search(action='code')", "c3_read(symbols=[...])"],
    "large_file_needle": ["c3_compress(mode='dense_map')", "c3_read(lines=[...])"],
}


class TaskBuilder:
    """Generates benchmark tasks from codebase analysis."""

    def __init__(self, project_path: str, indexer=None, file_memory=None):
        self.project_path = Path(project_path).resolve()
        self.indexer = indexer
        self.file_memory = file_memory
        self._file_records: dict[str, dict] = {}
        self._all_symbols: list[tuple[str, str, dict]] = []  # (rel_path, symbol_name, section_info)

    def build_tasks(self, max_per_category: int = 1,
                    categories: set[str] | None = None) -> list[E2ETask]:
        """Build benchmark tasks, filtered to high-signal categories.

        Args:
            max_per_category: Max tasks per category (default 1).
            categories: Set of category names to include.
                        Defaults to BENCHMARK_CATEGORIES.
        """
        self._scan_files()
        if not self._all_symbols:
            return []

        include = categories or BENCHMARK_CATEGORIES

        # Map category name -> builder method
        all_builders = {
            "explanation": self._symbol_explanation_tasks,
            "file_discovery": self._file_discovery_tasks,
            "dependency_analysis": self._dependency_analysis_tasks,
            "architecture": self._architecture_tasks,
            "call_chain": self._call_chain_tasks,
            "code_review": self._code_review_tasks,
            "multi_file_trace": self._multi_file_trace_tasks,
            "large_file_needle": self._large_file_needle_tasks,
            "refactor_suggestion": self._refactor_suggestion_tasks,
            "bug_injection": self._bug_injection_tasks,
        }

        tasks = []
        for cat_name, builder_fn in all_builders.items():
            if cat_name not in include:
                continue
            try:
                category_tasks = builder_fn(max_per_category)
                tasks.extend(category_tasks)
            except Exception as exc:
                print(f"  [e2e_tasks] Warning: {builder_fn.__name__} failed: {exc}", file=sys.stderr)
                if __debug__:
                    traceback.print_exc(file=sys.stderr)
                continue
        return tasks

    def _scan_files(self):
        """Scan file memory to collect symbols and records."""
        if not self.file_memory:
            return
        all_files = self.file_memory.list_tracked()
        for rel_path in all_files:
            record = self.file_memory.get(rel_path)
            if not record or not record.get("sections"):
                continue
            self._file_records[rel_path] = record
            for section in record["sections"]:
                if section.get("type") in ("class", "function", "method"):
                    self._all_symbols.append((rel_path, section["name"], section))

    def _base_ground_truth(self, **kwargs) -> GroundTruth:
        """Create a GroundTruth pre-filled with common defaults."""
        return GroundTruth(**kwargs)

    def _pick_symbols(self, n: int, types: list[str] | None = None,
                      min_name_len: int = 0) -> list[tuple[str, str, dict]]:
        """Pick n random symbols, optionally filtered by type and name length."""
        pool = self._all_symbols
        if types:
            pool = [s for s in pool if s[2].get("type") in types]
        if min_name_len:
            pool = [s for s in pool if len(s[1]) >= min_name_len]
        if len(pool) <= n:
            return list(pool)
        return random.sample(pool, n)

    def _read_file_content(self, rel_path: str) -> str:
        """Read file content, return empty string on failure."""
        try:
            return (self.project_path / rel_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    # ── Original categories (enhanced) ──────────────────────────────────

    def _symbol_explanation_tasks(self, max_tasks: int) -> list[E2ETask]:
        """'What does function X do?' — enhanced with multi-hop questions for classes."""
        tasks = []
        picks = self._pick_symbols(max_tasks, types=["function", "class"])
        for rel_path, sym_name, section in picks:
            keywords = [sym_name]
            required_aspects = ["purpose"]

            if section.get("type") == "class":
                record = self._file_records.get(rel_path, {})
                methods = [s["name"] for s in record.get("sections", [])
                           if s.get("type") == "method" and s.get("parent") == sym_name][:3]
                keywords.extend(methods)
                required_aspects.extend(["methods", "usage"])

            docstring = section.get("docstring", "")
            if docstring:
                doc_words = [w for w in re.findall(r"[a-z_]{4,}", docstring.lower())
                             if w not in ("self", "this", "that", "with", "from", "none", "true", "false")][:3]
                keywords.extend(doc_words)

            # Build verifiable claims
            claims = [(f"{sym_name} is defined in {rel_path}", True)]
            params = section.get("params", "")
            if params and "self" not in params.split(",")[0]:
                claims.append((f"{sym_name} accepts parameters", True))

            tasks.append(E2ETask(
                id=f"explain_{sym_name}",
                category="explanation",
                query=(f"What does `{sym_name}` in `{rel_path}` do? "
                       f"Explain its purpose, parameters, and how it works."
                       + (" List its key methods and their roles." if section.get("type") == "class" else "")),
                target_files=[rel_path],
                difficulty="easy" if section.get("type") == "function" else "medium",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("explanation", []),
                ground_truth=self._base_ground_truth(
                    required_keywords=keywords,
                    expected_files=[rel_path],
                    expected_symbols=[sym_name],
                    expected_answer_summary=docstring or f"Explanation of {sym_name}",
                    verifiable_claims=claims,
                    required_aspects=required_aspects,
                ),
            ))
        return tasks

    def _file_discovery_tasks(self, max_tasks: int) -> list[E2ETask]:
        """'Which file contains class X?' — ground truth from index."""
        tasks = []
        picks = self._pick_symbols(max_tasks, types=["class"])
        if not picks:
            picks = self._pick_symbols(max_tasks, types=["function"])
        for rel_path, sym_name, section in picks:
            # Get other symbols in the same file for verification
            record = self._file_records.get(rel_path, {})
            other_symbols = [s["name"] for s in record.get("sections", [])
                             if s["name"] != sym_name and s.get("type") in ("class", "function")][:4]

            tasks.append(E2ETask(
                id=f"find_{sym_name}",
                category="file_discovery",
                query=f"Which file contains the `{sym_name}` {section.get('type', 'symbol')}? "
                      f"What other important symbols are defined in that same file?",
                target_files=[rel_path],
                difficulty="easy",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("file_discovery", []),
                ground_truth=self._base_ground_truth(
                    required_keywords=[sym_name],
                    expected_files=[rel_path],
                    expected_symbols=[sym_name] + other_symbols[:2],
                    expected_answer_summary=f"{sym_name} is defined in {rel_path}",
                    verifiable_claims=[
                        (f"{sym_name} is in {rel_path}", True),
                    ] + [(f"{s} is also in this file", True) for s in other_symbols[:2]],
                ),
            ))
        return tasks

    def _dependency_analysis_tasks(self, max_tasks: int) -> list[E2ETask]:
        """'What does file Y depend on?' — ground truth from imports."""
        tasks = []
        candidates = [(p, r) for p, r in self._file_records.items()
                       if r.get("imports")]
        if not candidates:
            return []
        picks = random.sample(candidates, min(max_tasks, len(candidates)))
        for rel_path, record in picks:
            imports = record.get("imports", [])
            import_keywords = []
            for imp in imports[:5]:
                mod = imp if isinstance(imp, str) else imp.get("module", "")
                if mod and not mod.startswith("__"):
                    import_keywords.append(mod.split(".")[-1])

            tasks.append(E2ETask(
                id=f"deps_{Path(rel_path).stem}",
                category="dependency_analysis",
                query=f"What are the key dependencies of `{rel_path}`? "
                      f"List the modules it imports and explain what each is used for.",
                target_files=[rel_path],
                difficulty="medium",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("dependency_analysis", []),
                ground_truth=self._base_ground_truth(
                    required_keywords=import_keywords[:4],
                    expected_files=[rel_path],
                    expected_answer_summary=f"Dependencies of {rel_path}: {', '.join(import_keywords)}",
                    verifiable_claims=[(f"{rel_path} imports {m}", True) for m in import_keywords[:3]],
                    required_aspects=["imports", "usage"],
                ),
            ))
        return tasks

    def _architecture_tasks(self, max_tasks: int) -> list[E2ETask]:
        """'How is the project structured?' — ground truth from directory analysis."""
        tasks = []
        top_dirs = set()
        for rel_path in self._file_records:
            parts = Path(rel_path).parts
            if len(parts) > 1:
                top_dirs.add(parts[0])

        if not top_dirs:
            return []

        dirs_list = sorted(top_dirs)
        tasks.append(E2ETask(
            id="architecture_overview",
            category="architecture",
            query="Describe the high-level architecture of this project. "
                  "What are the main directories/modules and what is each responsible for?",
            target_files=[],
            difficulty="medium",
            suggested_tools=_CATEGORY_TOOL_HINTS.get("architecture", []),
            ground_truth=self._base_ground_truth(
                required_keywords=dirs_list[:5],
                expected_answer_summary=f"Project has these main modules: {', '.join(dirs_list)}",
                required_aspects=["directories", "responsibilities", "relationships"],
            ),
        ))

        if len(dirs_list) >= 2:
            target_dir = random.choice(dirs_list[:3])
            dir_files = [p for p in self._file_records
                         if p.startswith(target_dir + "/") or p.startswith(target_dir + "\\")]
            file_keywords = [Path(f).stem for f in dir_files[:5]]

            tasks.append(E2ETask(
                id=f"architecture_{target_dir}",
                category="architecture",
                query=f"Explain the purpose and internal structure of the `{target_dir}/` module. "
                      f"What are the key files and how do they relate to each other?",
                target_files=dir_files[:5],
                difficulty="hard",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("architecture", []),
                multi_turn=True,
                ground_truth=self._base_ground_truth(
                    required_keywords=file_keywords[:4],
                    expected_files=dir_files[:3],
                    expected_answer_summary=f"The {target_dir} module contains: {', '.join(file_keywords)}",
                    required_aspects=["files", "purpose", "relationships"],
                ),
            ))

        return tasks[:max_tasks]

    def _call_chain_tasks(self, max_tasks: int) -> list[E2ETask]:
        """'What calls function X?' — ground truth from grep-verified call sites."""
        tasks = []
        picks = self._pick_symbols(max_tasks * 3, types=["function"], min_name_len=5)
        for rel_path, sym_name, section in picks:
            if len(tasks) >= max_tasks:
                break
            if sym_name.startswith("_"):
                continue

            call_sites = []
            _call_pat = re.compile(r"\b" + re.escape(sym_name) + r"\b")
            for other_path in self._file_records:
                if other_path == rel_path:
                    continue
                content = self._read_file_content(other_path)
                if _call_pat.search(content):
                    call_sites.append(other_path)

            if not call_sites:
                continue

            tasks.append(E2ETask(
                id=f"callers_{sym_name}",
                category="call_chain",
                query=f"Find all files that call or reference the function `{sym_name}` (defined in `{rel_path}`). "
                      f"For each caller, explain why it uses this function.",
                target_files=[rel_path] + call_sites[:3],
                difficulty="hard",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("call_chain", []),
                multi_turn=True,
                ground_truth=self._base_ground_truth(
                    required_keywords=[sym_name] + [Path(c).stem for c in call_sites[:2]],
                    expected_files=call_sites[:3],
                    expected_symbols=[sym_name],
                    expected_answer_summary=f"{sym_name} is called from: {', '.join(call_sites[:3])}",
                    verifiable_claims=[
                        (f"{Path(c).stem} calls {sym_name}", True) for c in call_sites[:3]
                    ],
                    required_aspects=["call_sites", "reasons"],
                ),
            ))
        return tasks

    def _code_review_tasks(self, max_tasks: int) -> list[E2ETask]:
        """'Review file X for issues' — enhanced with specific structural issues."""
        tasks = []
        complex_files = sorted(
            self._file_records.items(),
            key=lambda x: len(x[1].get("sections", [])),
            reverse=True,
        )[:max_tasks * 2]

        for rel_path, record in complex_files[:max_tasks]:
            symbols = [s["name"] for s in record.get("sections", [])
                       if s.get("type") in ("class", "function")][:5]
            line_count = record.get("line_count", 0)

            # Detect reviewable patterns from code structure
            review_aspects = ["error_handling", "organization"]
            claims = []
            if line_count > 500:
                review_aspects.append("file_length")
                claims.append((f"{rel_path} is {line_count} lines long", True))
            content = self._read_file_content(rel_path)
            if content:
                # Check for bare except
                if re.search(r"except\s*:", content):
                    review_aspects.append("bare_except")
                    claims.append((f"{rel_path} has bare except clauses", True))
                # Check for TODO/FIXME
                todo_count = len(re.findall(r"#\s*(TODO|FIXME|HACK|XXX)", content, re.IGNORECASE))
                if todo_count:
                    claims.append((f"{rel_path} has TODO/FIXME comments", True))
                # Check for long functions
                long_fns = [s["name"] for s in record.get("sections", [])
                            if s.get("line_end", 0) - s.get("line_start", 0) > 80]
                if long_fns:
                    review_aspects.append("long_functions")
                    claims.append((f"{rel_path} has long functions: {', '.join(long_fns[:2])}", True))

            tasks.append(E2ETask(
                id=f"review_{Path(rel_path).stem}",
                category="code_review",
                query=f"Review `{rel_path}` for code quality. Consider: error handling, "
                      f"code organization, naming conventions, and potential bugs. "
                      f"Be specific about what you find.",
                target_files=[rel_path],
                difficulty="hard",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("code_review", []),
                multi_turn=True,
                ground_truth=self._base_ground_truth(
                    required_keywords=symbols[:3],
                    expected_files=[rel_path],
                    expected_symbols=symbols[:3],
                    expected_answer_summary=f"Code review of {rel_path} ({line_count} lines, {len(symbols)} symbols)",
                    verifiable_claims=claims,
                    required_aspects=review_aspects,
                ),
            ))
        return tasks

    # ── New categories ──────────────────────────────────────────────────

    def _multi_file_trace_tasks(self, max_tasks: int) -> list[E2ETask]:
        """Trace data flow across multiple files — tests cross-file reasoning."""
        tasks = []
        # Find classes/functions imported and used across files
        for rel_path, record in self._file_records.items():
            if len(tasks) >= max_tasks:
                break
            imports = record.get("imports", [])
            if not imports:
                continue

            # Find a local import (within this project)
            for imp in imports:
                mod = imp if isinstance(imp, str) else imp.get("module", "")
                if not mod:
                    continue
                # Check if it's an internal module
                mod_parts = mod.replace(".", "/")
                matching_files = [f for f in self._file_records
                                  if mod_parts in f.replace("\\", "/")]
                if not matching_files:
                    continue

                source_file = matching_files[0]
                source_record = self._file_records.get(source_file, {})
                source_symbols = [s["name"] for s in source_record.get("sections", [])
                                  if s.get("type") in ("class", "function")][:3]
                if not source_symbols:
                    continue

                target_symbol = source_symbols[0]
                tasks.append(E2ETask(
                    id=f"trace_{Path(rel_path).stem}_to_{Path(source_file).stem}",
                    category="multi_file_trace",
                    query=(f"Trace how `{rel_path}` uses `{target_symbol}` from `{source_file}`. "
                           f"What data flows from the source to the consumer? "
                           f"What transformations happen along the way?"),
                    target_files=[rel_path, source_file],
                    difficulty="hard",
                    suggested_tools=_CATEGORY_TOOL_HINTS.get("multi_file_trace", []),
                    multi_turn=True,
                    ground_truth=self._base_ground_truth(
                        required_keywords=[target_symbol, Path(source_file).stem, Path(rel_path).stem],
                        expected_files=[rel_path, source_file],
                        expected_symbols=[target_symbol],
                        expected_answer_summary=(
                            f"{rel_path} imports {target_symbol} from {source_file} "
                            f"and uses it for data processing"
                        ),
                        verifiable_claims=[
                            (f"{rel_path} imports from {source_file}", True),
                            (f"{target_symbol} is used in {rel_path}", True),
                        ],
                        required_aspects=["import_chain", "data_flow", "transformations"],
                    ),
                ))
                break  # One trace per file

        return tasks[:max_tasks]

    def _large_file_needle_tasks(self, max_tasks: int) -> list[E2ETask]:
        """Find specific detail in a large file — tests surgical extraction."""
        tasks = []
        # Find files with many sections (large, complex files)
        large_files = sorted(
            self._file_records.items(),
            key=lambda x: x[1].get("line_count", 0),
            reverse=True,
        )

        for rel_path, record in large_files:
            if len(tasks) >= max_tasks:
                break
            line_count = record.get("line_count", 0)
            if line_count < 200:
                continue

            sections = record.get("sections", [])
            # Pick a function in the bottom half of the file (harder to find)
            bottom_half = [s for s in sections
                           if s.get("line_start", 0) > line_count // 2
                           and s.get("type") in ("function", "method")
                           and len(s.get("name", "")) >= 5]
            if not bottom_half:
                continue

            target = random.choice(bottom_half)
            target_name = target["name"]
            target_line = target.get("line_start", 0)
            docstring = target.get("docstring", "")
            params = target.get("params", "")

            claims = [
                (f"{target_name} starts around line {target_line}", True),
            ]
            if docstring:
                claims.append((f"{target_name} has a docstring", True))
            if params:
                claims.append((f"{target_name} accepts parameters", True))

            tasks.append(E2ETask(
                id=f"needle_{target_name}_in_{Path(rel_path).stem}",
                category="large_file_needle",
                query=(f"In `{rel_path}` ({line_count} lines), find the function `{target_name}`. "
                       f"What does it do, what are its parameters, and what line is it on?"),
                target_files=[rel_path],
                difficulty="hard",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("large_file_needle", []),
                multi_turn=True,
                ground_truth=self._base_ground_truth(
                    required_keywords=[target_name],
                    expected_files=[rel_path],
                    expected_symbols=[target_name],
                    expected_answer_summary=(
                        f"{target_name} at ~line {target_line} in {rel_path}: {docstring or 'no docstring'}"
                    ),
                    verifiable_claims=claims,
                    required_aspects=["location", "purpose", "parameters"],
                ),
            ))
        return tasks

    def _refactor_suggestion_tasks(self, max_tasks: int) -> list[E2ETask]:
        """Suggest refactoring for duplication — tests cross-file pattern detection."""
        tasks = []

        # Find files with similar names or in the same directory that might have duplication
        dir_groups: dict[str, list[str]] = {}
        for rel_path in self._file_records:
            parent = str(Path(rel_path).parent)
            dir_groups.setdefault(parent, []).append(rel_path)

        for parent_dir, files in dir_groups.items():
            if len(tasks) >= max_tasks:
                break
            if len(files) < 3:
                continue

            # Pick 2-3 files from the same directory
            sample = random.sample(files, min(3, len(files)))
            sample_symbols = {}
            for f in sample:
                rec = self._file_records.get(f, {})
                syms = [s["name"] for s in rec.get("sections", [])
                        if s.get("type") in ("function", "method")]
                sample_symbols[f] = syms

            all_syms = []
            for syms in sample_symbols.values():
                all_syms.extend(syms)
            file_stems = [Path(f).stem for f in sample]

            tasks.append(E2ETask(
                id=f"refactor_{parent_dir.replace('/', '_').replace(chr(92), '_')}",
                category="refactor_suggestion",
                query=(f"Analyze these files in `{parent_dir}/` for code duplication and refactoring opportunities: "
                       f"{', '.join('`' + f + '`' for f in sample)}. "
                       f"What patterns are repeated? How would you reduce the duplication?"),
                target_files=sample,
                difficulty="expert",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("refactor_suggestion", []),
                multi_turn=True,
                ground_truth=self._base_ground_truth(
                    required_keywords=file_stems[:3],
                    expected_files=sample,
                    expected_answer_summary=f"Refactoring analysis of {', '.join(file_stems)}",
                    required_aspects=["duplication_patterns", "refactoring_approach", "shared_abstractions"],
                ),
            ))
        return tasks

    def _bug_injection_tasks(self, max_tasks: int) -> list[E2ETask]:
        """Detect issues in code — tests analytical ability with planted hints."""
        tasks = []
        # Pick files and ask about specific patterns that are verifiable
        candidates = []
        for rel_path, record in self._file_records.items():
            content = self._read_file_content(rel_path)
            if not content or len(content) < 500:
                continue
            issues = []
            # Detect real patterns we can ask about
            if re.search(r"except\s*:", content):
                issues.append("bare_except")
            if re.search(r"except\s+Exception\s*:", content):
                issues.append("broad_except")
            if "# TODO" in content or "# FIXME" in content or "# HACK" in content:
                issues.append("todo_markers")
            if re.search(r"\.format\(", content) and "f\"" not in content[:2000]:
                issues.append("old_format_strings")
            if re.search(r"type\([\w]+\)\s*==", content):
                issues.append("type_comparison")
            if re.search(r"except.*pass\s*$", content, re.MULTILINE):
                issues.append("silent_exception")
            if issues:
                candidates.append((rel_path, record, content, issues))

        if not candidates:
            return []

        # Keyword synonym groups — each entry is a list so the evaluator matches
        # if ANY alternative is present.  Avoids fragile single-word requirements
        # (e.g. "overly" which models rarely write verbatim).
        _ISSUE_KEYWORDS: dict[str, list[str]] = {
            "bare_except": ["bare except", "except:", "catching all", "bare clause"],
            "broad_except": ["except Exception", "broad exception", "broad except", "swallow", "silent failure"],
            "todo_markers": ["TODO", "FIXME", "HACK", "tech debt"],
            "old_format_strings": [".format(", "f-string", "f\"", "f'"],
            "type_comparison": ["type()", "isinstance", "type comparison"],
            "silent_exception": ["except: pass", "swallow", "silent", "silently"],
        }
        _ISSUE_DESC: dict[str, str] = {
            "bare_except": "bare except clauses (catching all exceptions without specifying type)",
            "broad_except": "overly broad exception handling (catching base Exception)",
            "todo_markers": "unresolved TODO/FIXME/HACK comments",
            "old_format_strings": "use of .format() instead of f-strings",
            "type_comparison": "type comparison using type() == instead of isinstance()",
            "silent_exception": "silently swallowed exceptions (except: pass)",
        }

        for rel_path, record, content, issues in random.sample(candidates, min(max_tasks, len(candidates))):
            # Synonym groups per detected issue
            expected_keywords = [_ISSUE_KEYWORDS.get(i, [i]) for i in issues[:3]]
            claims = [(f"{rel_path} has {_ISSUE_DESC.get(i, i)}", True) for i in issues[:3]]

            tasks.append(E2ETask(
                id=f"bugs_{Path(rel_path).stem}",
                category="bug_injection",
                query=(f"Analyze `{rel_path}` for code quality issues, anti-patterns, and potential bugs. "
                       f"Focus on error handling, exception management, and code hygiene. "
                       f"List each issue with the specific line number or function name where it occurs, "
                       f"and suggest how to fix it."),
                target_files=[rel_path],
                difficulty="medium",
                suggested_tools=_CATEGORY_TOOL_HINTS.get("bug_injection", []),
                ground_truth=self._base_ground_truth(
                    required_keywords=expected_keywords,
                    expected_files=[rel_path],
                    expected_answer_summary=f"Issues in {rel_path}: {', '.join(issues)}",
                    verifiable_claims=claims,
                    required_aspects=["issues_found", "locations", "suggestions"],
                    # Bug reports benefit most from factual accuracy and completeness.
                    scoring_weights={
                        "keyword": 0.10,
                        "structural": 0.10,
                        "file_mention": 0.10,
                        "factual": 0.35,
                        "completeness": 0.35,
                    },
                ),
            ))
        return tasks


def build_prompt(task: E2ETask) -> str:
    """Build the full prompt string for a task."""
    prompt = TASK_PROMPT_TEMPLATE.format(query=task.query)
    tools = task.suggested_tools
    if tools:
        prompt += f"\n\nSuggested C3 tools for this task: {', '.join(tools)}"
    return prompt
