"""
Pruned Filesystem Scanner

Shared walker for every C3 index build (code index, doc index, compression
dictionary, directory compression). Replaces the sorted(Path.rglob('*'))
pattern, which enumerated every entry under node_modules/.git/venv before
filtering and materialized the whole tree up front - on large projects that
meant minutes of stat calls before the first file was even considered.

os.walk with in-place dirnames pruning never descends into skipped
directories, yields files in deterministic order, exits as soon as the
caller stops consuming, and reports progress so long scans are visible.
"""
import fnmatch
import os
from pathlib import Path
from typing import Callable, Iterator, Optional, Set, Tuple

# Superset of the historical per-service skip lists.
# Matched against directory names exactly (never file names).
SKIP_DIRS = {
    # original shared set
    'node_modules', '.git', '__pycache__', '.c3', 'venv', 'env', '.venv',
    'dist', 'build', '.next', '.cache', 'coverage', '.pytest_cache',
    # heavyweights the old lists missed
    'target', '.tox', '.nox', '.eggs', '.mypy_cache', '.ruff_cache',
    '.gradle', 'Pods', 'obj', '.idea', '.vs', '.svn', '.hg',
    'bower_components', '.terraform', '.parcel-cache', '.turbo',
    '.nuxt', '.yarn', '.pnpm-store',
}


def gitignore_dir_patterns(root) -> tuple:
    """(literal_names, glob_patterns) for directory pruning from .gitignore.

    Single-segment entries only - a bare name, ``/name/``, or a simple glob
    like ``*.egg-info/`` - never negations or nested paths, so pruning can
    only be narrower than the ignore file itself. Both sets are matched
    against directory basenames. Data/log/build directories are exactly
    what makes large-project scans hang, and they are almost always
    single-segment entries.
    """
    names: set = set()
    patterns: list = []
    try:
        text = (Path(root) / '.gitignore').read_text(errors='replace')
    except OSError:
        return names, patterns
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('!'):
            continue
        cleaned = line.strip('/')
        if not cleaned or '/' in cleaned or '\\' in cleaned:
            continue
        if any(ch in cleaned for ch in '*?['):
            patterns.append(cleaned)
        else:
            names.add(cleaned)
    return names, patterns


def gitignore_dir_names(root) -> set:
    """Literal directory names from the root .gitignore (best-effort)."""
    return gitignore_dir_patterns(root)[0]


def make_dir_pruner(root, extra_skip=(), respect_gitignore: bool = True):
    """Predicate ``dirname -> bool`` (True = prune this directory).

    Combines SKIP_DIRS, ``extra_skip``, and the root .gitignore (literal
    names plus single-segment globs like ``*.egg-info/``). Shared by the
    index scanners and the project-tree doc generator so every surface
    agrees on what a distributable project looks like.
    """
    skip = set(SKIP_DIRS) | set(extra_skip)
    patterns: list = []
    if respect_gitignore:
        names, patterns = gitignore_dir_patterns(root)
        skip |= names

    def pruned(dirname: str) -> bool:
        return (dirname in skip
                or any(fnmatch.fnmatch(dirname, p) for p in patterns))

    return pruned


def iter_files(
    root,
    exts: Optional[Set[str]] = None,
    skip_dirs: Optional[Set[str]] = None,
    exclude_parts: Optional[Callable[[Tuple[str, ...]], bool]] = None,
    max_files: Optional[int] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    respect_gitignore: bool = True,
) -> Iterator[Path]:
    """Yield candidate files under ``root`` with directory-level pruning.

    Args:
        exts: lowercase suffix allowlist (None = every file).
        skip_dirs: directory names to prune (default: SKIP_DIRS).
        exclude_parts: predicate over project-relative path parts; True
            skips a file, and prunes whole directories before descent
            (used for sub-project exclusion).
        max_files: stop traversal entirely after yielding this many.
        on_progress: called as ``on_progress(entries_seen, files_yielded)``
            at directory granularity - cheap enough to wire straight to a
            TTY progress line (which should throttle by time).
        respect_gitignore: also prune literal directory names listed in
            the root .gitignore.
    """
    root = Path(root)
    skip = set(SKIP_DIRS if skip_dirs is None else skip_dirs)
    glob_skips: list = []
    if respect_gitignore:
        names, glob_skips = gitignore_dir_patterns(root)
        skip |= names

    yielded = 0
    seen = 0
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True,
                                                followlinks=False):
        try:
            rel_parts = Path(dirpath).relative_to(root).parts
        except ValueError:
            rel_parts = ()

        kept = []
        for d in sorted(dirnames):
            if d in skip:
                continue
            if glob_skips and any(fnmatch.fnmatch(d, p) for p in glob_skips):
                continue
            if exclude_parts is not None and exclude_parts(rel_parts + (d,)):
                continue
            kept.append(d)
        dirnames[:] = kept
        seen += len(filenames) + len(kept)

        for fname in sorted(filenames):
            if exts is not None:
                if os.path.splitext(fname)[1].lower() not in exts:
                    continue
            if exclude_parts is not None and exclude_parts(rel_parts + (fname,)):
                continue
            yield Path(dirpath) / fname
            yielded += 1
            if max_files is not None and yielded >= max_files:
                if on_progress is not None:
                    on_progress(seen, yielded)
                return

        if on_progress is not None:
            on_progress(seen, yielded)
