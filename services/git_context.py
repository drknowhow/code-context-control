"""GitContext — single source of truth for git working-tree state.

Centralizes git-root detection and branch / HEAD / dirty queries so the rest
of C3 does not re-implement subprocess plumbing (previously duplicated in
``EditLedger`` and ``VersionTracker``). State is cached for a short TTL because
several callers — the edit ledger, context snapshots, the branch watcher — ask
for it in quick succession.

All git calls use list-arg ``subprocess.run`` (no shell) with a timeout and the
Windows ``CREATE_NO_WINDOW`` flag. The ``shell=True`` hang documented for the
ledger's combined command does not apply to non-shell invocations, so the
simple ``run(..., timeout=...)`` form is safe here.
"""

import subprocess
import sys
import time
from pathlib import Path


def _git_kwargs() -> dict:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


class GitContext:
    """Cached accessor for the git working-tree state of a project path.

    Branch awareness is deliberately content/ref-derived rather than persisted:
    the index follows whatever HEAD currently points at. See ``state()`` for the
    shape returned to callers.
    """

    def __init__(self, project_path, ttl: float = 3.0):
        self.project_path = Path(project_path).resolve()
        self._ttl = ttl
        self._git_root = self._detect_git_root()
        self._cache: dict | None = None
        self._cache_time: float = 0.0

    # ── git root ──────────────────────────────────────────────────────
    @property
    def git_root(self) -> Path | None:
        return self._git_root

    @property
    def available(self) -> bool:
        return self._git_root is not None

    def _run(self, args: list, timeout: float = 3.0) -> tuple:
        """Run a git command rooted at git_root (project_path fallback).

        Returns (returncode, stdout, stderr); (None, '', '') on failure/timeout.
        """
        cwd = self._git_root or self.project_path
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
                **_git_kwargs(),
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except Exception:
            return None, "", ""

    def _detect_git_root(self) -> Path | None:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(self.project_path),
                capture_output=True, text=True, timeout=3,
                stdin=subprocess.DEVNULL,
                **_git_kwargs(),
            )
            if proc.returncode == 0:
                root = (proc.stdout or "").strip()
                if root:
                    return Path(root).resolve()
        except Exception:
            pass
        return None

    # ── working-tree state ────────────────────────────────────────────
    @staticmethod
    def _empty_state() -> dict:
        return {
            "available": False, "git_root": None, "branch": None,
            "detached": False, "head_sha": "", "upstream": None,
            "ahead": 0, "behind": 0, "dirty": False,
        }

    def state(self, force: bool = False) -> dict:
        """Return cached working-tree state.

        Keys: ``available``, ``git_root``, ``branch`` (None when detached),
        ``detached``, ``head_sha`` (short), ``upstream``, ``ahead``, ``behind``,
        ``dirty``. Cached for ``ttl`` seconds; pass ``force=True`` to refresh.
        """
        now = time.time()
        if not force and self._cache is not None and (now - self._cache_time) < self._ttl:
            return self._cache
        self._cache = self._compute_state()
        self._cache_time = now
        return self._cache

    def _compute_state(self) -> dict:
        st = self._empty_state()
        if not self._git_root:
            return st
        st["available"] = True
        st["git_root"] = str(self._git_root)

        rc, out, _ = self._run(["status", "--branch", "--porcelain=v2"])
        if rc != 0:
            # porcelain=v2 unsupported or command failed — degrade gracefully.
            return self._fallback_state(st)

        dirty = False
        for line in out.splitlines():
            if line.startswith("# branch.oid "):
                sha = line[len("# branch.oid "):].strip()
                if sha and sha != "(initial)":
                    st["head_sha"] = sha[:12]
            elif line.startswith("# branch.head "):
                head = line[len("# branch.head "):].strip()
                if head == "(detached)":
                    st["detached"] = True
                else:
                    st["branch"] = head
            elif line.startswith("# branch.upstream "):
                st["upstream"] = line[len("# branch.upstream "):].strip()
            elif line.startswith("# branch.ab "):
                for token in line[len("# branch.ab "):].split():
                    try:
                        if token.startswith("+"):
                            st["ahead"] = int(token[1:])
                        elif token.startswith("-"):
                            st["behind"] = int(token[1:])
                    except ValueError:
                        pass
            elif line and not line.startswith("#"):
                dirty = True
        st["dirty"] = dirty
        return st

    def _fallback_state(self, st: dict) -> dict:
        """Best-effort branch/HEAD for git versions without porcelain=v2."""
        rc, out, _ = self._run(["rev-parse", "HEAD"])
        if rc == 0 and out.strip():
            st["head_sha"] = out.strip()[:12]
        rc, out, _ = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        if rc == 0:
            head = out.strip()
            if head == "HEAD":
                st["detached"] = True
            elif head:
                st["branch"] = head
        rc, out, _ = self._run(["status", "--porcelain"])
        if rc == 0:
            st["dirty"] = bool(out.strip())
        return st

    # ── convenience accessors ─────────────────────────────────────────
    def branch(self) -> str | None:
        return self.state().get("branch")

    def head_sha(self) -> str:
        return self.state().get("head_sha", "")

    def label(self) -> str:
        """Human-readable 'branch @ shortsha' (or '(detached) @ sha')."""
        st = self.state()
        if not st["available"]:
            return "no-git"
        name = st["branch"] or "(detached)"
        sha = (st["head_sha"] or "")[:8]
        return f"{name} @ {sha}" if sha else name

    # ── change queries (scoped re-index support) ──────────────────────
    def _to_project_rel(self, git_rel: str) -> str | None:
        """Convert a git-root-relative path to project-relative POSIX form.

        Returns None when the path lies outside ``project_path`` (e.g. a sibling
        subdirectory of the repo, or another worktree) so callers never queue
        files they do not track.
        """
        if not git_rel or not self._git_root:
            return None
        abs_path = (self._git_root / git_rel).resolve()
        try:
            return abs_path.relative_to(self.project_path).as_posix()
        except Exception:
            return None

    def changed_files(self, old_sha: str, new_sha: str) -> list:
        """Project-relative paths that differ between two commits.

        Empty list if either sha is missing or the diff fails (e.g. the old
        commit is no longer reachable) — callers should fall back to
        ``dirty_files()`` in that case.
        """
        if not (old_sha and new_sha) or not self._git_root:
            return []
        rc, out, _ = self._run(["diff", "--name-only", f"{old_sha}..{new_sha}"])
        if rc != 0:
            return []
        result = []
        for line in out.splitlines():
            rel = self._to_project_rel(line.strip())
            if rel:
                result.append(rel)
        return result

    def dirty_files(self) -> list:
        """Project-relative paths with uncommitted working-tree changes.

        Includes untracked files. Catches edits made outside C3 (other editors,
        rebases, ``git restore``) that mtime-on-access would only notice later.
        """
        if not self._git_root:
            return []
        rc, out, _ = self._run(["status", "--porcelain"])
        if rc != 0:
            return []
        result = []
        for line in out.splitlines():
            if len(line) <= 3:
                continue
            path = line[3:].strip()
            # Renames render as 'old -> new'; index the new path.
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip()
            # git quotes paths containing special characters.
            if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            rel = self._to_project_rel(path)
            if rel:
                result.append(rel)
        return result
