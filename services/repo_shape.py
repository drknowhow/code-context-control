"""Repo shape: is this project a codebase, or a pile of prose?

C3's differentiated tools — symbol-aware ``c3_compress`` / ``c3_read``,
``c3_impact`` blast radius, ``c3_validate`` type checks, ``c3_ci`` — act on
programming-language source. On a project that is Markdown and ``.docx``
with next to no source, every one of them has nothing to act on, while
strict tool discipline is still paid on every turn (field report,
2026-08-22: 636 files, ~95% prose, net negative for the whole session, C3
uninstalled at the end). ``c3 init`` should say which kind of project it is
looking at, and default the prose kind to ``advisory`` instead of letting
the user discover the mismatch one blocked write at a time.

This module only measures and recommends. ``cli.c3`` decides what to do
with the answer, and an explicit user choice always wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Programming-language source. Deliberately NOT the indexer's ``code_exts``
#: (which includes Markdown, JSON and YAML because they are worth indexing) —
#: the question here is what the symbol tools can act on.
SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".pyx",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".c", ".h", ".cpp", ".cxx", ".cc", ".hpp", ".hxx",
    ".rs", ".go", ".java", ".kt", ".kts", ".scala", ".cs", ".fs", ".vb",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".rb", ".pl", ".pm", ".lua", ".php", ".r", ".jl",
    ".sql", ".graphql", ".gql", ".prisma",
    ".hs", ".ex", ".exs", ".erl", ".clj", ".cljs", ".elm", ".ml", ".mli",
    ".swift", ".m", ".mm", ".dart",
    ".tf", ".hcl", ".nix", ".proto", ".thrift", ".zig", ".nim",
})

#: Prose and office documents — what a documentation project is made of.
PROSE_EXTS = frozenset({
    ".md", ".mdx", ".rst", ".tex", ".adoc", ".txt", ".org",
    ".docx", ".doc", ".odt", ".rtf", ".pdf", ".epub",
    ".pptx", ".ppt", ".odp", ".xlsx", ".xls", ".ods", ".csv",
})

KIND_CODE = "code"
KIND_MIXED = "mixed"
KIND_PROSE = "prose"
KIND_EMPTY = "empty"      # too few files to have an opinion

#: Below this many source+prose files the shape is ``empty``: a fresh repo
#: with a README and one script is not "a documentation project".
MIN_FILES = 20
#: Source share of (source + prose) at or below which the repo is prose.
PROSE_MAX_SOURCE_SHARE = 0.10
#: Source share at or above which the repo is code.
CODE_MIN_SOURCE_SHARE = 0.50
#: Traversal cap — counting is all this does, and a ratio does not need
#: every file of a 200k-file monorepo to be right.
DEFAULT_MAX_FILES = 50_000


@dataclass(frozen=True)
class RepoShape:
    total: int          # every file the scanner yielded
    source: int
    prose: int
    other: int          # config, data, images, binaries — neither side
    kind: str
    capped: bool = False

    @property
    def judged(self) -> int:
        return self.source + self.prose

    @property
    def source_share(self) -> float:
        return (self.source / self.judged) if self.judged else 0.0

    def describe(self) -> str:
        pct = f"{self.source_share * 100:.0f}%"
        cap = " (first {0:,} files)".format(self.total) if self.capped else ""
        if self.kind == KIND_EMPTY:
            return f"{self.total} files — too few to judge{cap}"
        return (f"{self.prose:,} prose/doc files, {self.source:,} source files "
                f"({pct} of the judged set), {self.other:,} other — {self.kind}{cap}")


def classify(path) -> str:
    """``source`` / ``prose`` / ``other`` by extension."""
    ext = Path(path).suffix.lower()
    if ext in SOURCE_EXTS:
        return "source"
    if ext in PROSE_EXTS:
        return "prose"
    return "other"


def kind_for(source: int, prose: int) -> str:
    judged = source + prose
    if judged < MIN_FILES:
        return KIND_EMPTY
    share = source / judged
    if share <= PROSE_MAX_SOURCE_SHARE:
        return KIND_PROSE
    if share >= CODE_MIN_SOURCE_SHARE:
        return KIND_CODE
    return KIND_MIXED


def assess(project_path, max_files: int = DEFAULT_MAX_FILES) -> RepoShape:
    """Walk the project with the same pruning the indexer uses and count."""
    from services.scanner import SKIP_DIRS, iter_files

    root = Path(project_path)
    counts = {"source": 0, "prose": 0, "other": 0}
    total = 0
    skip = set(SKIP_DIRS) | {".c3"}
    for f in iter_files(root, exts=None, skip_dirs=skip, max_files=max_files):
        total += 1
        counts[classify(f)] += 1
    return RepoShape(
        total=total, source=counts["source"], prose=counts["prose"],
        other=counts["other"], kind=kind_for(counts["source"], counts["prose"]),
        capped=(max_files is not None and total >= max_files),
    )


def recommended_mode(shape: RepoShape) -> str | None:
    """The tool-discipline mode the shape argues for, or None for no opinion."""
    return "advisory" if shape.kind == KIND_PROSE else None
