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


def gitignore_dir_names(root) -> set:
    """Literal directory names from the root .gitignore (best-effort).

    Only unambiguous entries are used - a bare name or ``/name/`` with no
    wildcard, negation, or nested separator - so pruning can never be
    broader than the ignore file itself. Data/log directories are exactly
    what makes large-project scans hang, and they are almost always
    plain-name entries.
    """
    names = set()
    try:
        text = (Path(root) / '.gitignore').read_text(errors='replace')
    except OSError:
        return names
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('!'):
            continue
        if any(ch in line for ch in '*?['):
            continue
        cleaned = line.strip('/')
        if cleaned and '/' not in cleaned and '\\' not in cleaned:
            names.add(cleaned)
    return names


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
    if respect_gitignore:
        skip |= gitignore_dir_names(root)

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
