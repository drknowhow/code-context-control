"""AgentCI — deterministic job fingerprints and cached result reuse (Phase 4).

The loop is only useful if it is fast. Re-running a job whose inputs have not
moved since it last passed is the cheapest thing to stop doing — but a cache
that is wrong is worse than no cache, because it returns a green for code that
was never checked.

So the fingerprint is **conservative first, precise on request**, and it reuses
the mapping Phase 5 already needs:

* A job with a `ci.required_map` entry is fingerprinted over **only the paths
  that map names**, plus its own definition. Edits elsewhere cannot invalidate
  it, and cannot silently be covered by it either — the map is the repository's
  own statement about what this job reads.
* A job with **no** mapping is fingerprinted over the **whole tree**. Any edit
  anywhere invalidates it. That yields few hits on an active repo, which is the
  correct outcome: with no declared inputs, there is no honest basis for
  claiming a change could not have affected the job.

A reused result is recorded as `cached`, not as a fresh pass, and the run
report always says how many jobs came from cache. `no_cache` forces execution.

What is deliberately NOT built, with reasons rather than silence:

* **Workflow compilation cache** (spec §15). Parsing this repository's two
  workflows takes single-digit milliseconds; a cache would add invalidation
  bugs to buy nothing measurable.
* **Image cache.** Docker already layer-caches images, and act reuses them —
  the first `catthehacker` pull is ~1 GB and every later run is instant. There
  is nothing here for C3 to add.
* **Actions cache.** act clones actions into its own cache directory. Same
  reasoning.

`actions/cache` steps ARE handled, because that is a *dependency* cache the
workflow explicitly asks for and nothing else provides locally.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CACHE_DIR = ".c3/ci/cache"
RESULTS_FILE = "results.jsonl"
DEPS_DIR = "deps"
MAX_RESULTS = 500


@dataclass
class CacheHit:
    fingerprint: str
    run_id: str
    at: str
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {"fingerprint": self.fingerprint, "run_id": self.run_id,
                "at": self.at, "duration_ms": self.duration_ms}


def _sha(*parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def _is_c3_internal(path: str) -> bool:
    """C3's own state, which changes on every run and is never a job input.

    `lstrip("./")` is NOT prefix removal — it strips any leading '.' or '/'
    characters, so `.c3/` became `c3/` and this guard silently never matched.
    Git also collapses an untracked directory to a single `.c3/` entry, which
    is why the trailing-slash form has to be handled at all.
    """
    norm = str(path or "").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/").rstrip("/")
    return norm in (".c3", ".git") or norm.startswith((".c3/", ".git/"))


def _git(project, *args) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(project), capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {}),
        )
        return out.stdout or ""
    except Exception:
        return ""


# ── Fingerprint ─────────────────────────────────────────────────────────────

def _job_identity(inst, engine: str, image: str) -> str:
    """Everything about the job itself that changes what it would do."""
    steps = [(s.index, s.run, s.uses, s.if_, sorted((s.env or {}).items()),
              s.working_directory) for s in inst.steps]
    return _sha(inst.key, inst.runs_on, sorted(inst.needs),
                sorted((inst.matrix or {}).items()),
                sorted((inst.env or {}).items()), inst.if_, steps,
                engine, image)


def _tree_state(project, scope: list = None) -> str:
    """Content state of the inputs a job could read.

    With a scope, only those paths count — `git hash-object` for tracked
    content, plus a hash of the bytes for anything untracked or modified.
    Without one, the whole tree counts, which is the conservative default.
    """
    project = Path(project)
    if not scope:
        head = _git(project, "rev-parse", "HEAD").strip()
        # Dirty content is not in HEAD, so hash it explicitly — otherwise an
        # uncommitted edit would reuse a result computed before it existed.
        dirty = _git(project, "status", "--porcelain")
        dirty_hash = ""
        for line in dirty.splitlines():
            path = line[3:].strip().strip('"') if len(line) > 3 else ""
            if not path or " -> " in path:
                path = path.split(" -> ")[-1] if " -> " in path else path
            # C3's own bookkeeping is not an input to the repository's CI.
            # Without this the first run writes .c3/ci/runs/... , the tree
            # state changes, and every run invalidates the cache it just
            # wrote — a cache that can never hit.
            if _is_c3_internal(path):
                continue
            full = project / path
            try:
                dirty_hash = _sha(dirty_hash, path,
                                  full.read_bytes() if full.is_file() else "gone")
            except OSError:
                dirty_hash = _sha(dirty_hash, path, "unreadable")
        return _sha(head, dirty_hash)

    parts: list = []
    for pattern in sorted(scope):
        for match in sorted(project.glob(pattern)):
            if not match.is_file():
                continue
            try:
                if _is_c3_internal(match.relative_to(project).as_posix()):
                    continue
            except ValueError:
                pass
            try:
                rel = match.relative_to(project).as_posix()
                parts.append(_sha(rel, match.read_bytes()))
            except (OSError, ValueError):
                parts.append(_sha(str(match), "unreadable"))
    return _sha(*parts) if parts else _sha("empty-scope")


def job_fingerprint(project, inst, engine: str = "native", image: str = "",
                    scope: list = None) -> str:
    """Stable id for "this exact job against these exact inputs"."""
    return _sha(_job_identity(inst, engine, image), _tree_state(project, scope))


def scope_for(inst, rules: dict) -> list:
    """The path globs a job declares, or None for 'the whole tree'."""
    import fnmatch
    for pattern, globs in (rules or {}).items():
        if fnmatch.fnmatch(inst.key, pattern) or fnmatch.fnmatch(inst.job_id, pattern):
            # The job's own definition is always an input.
            extra = [Path(inst.workflow_path).name] if inst.workflow_path else []
            return list(globs) + [f".github/workflows/{n}" for n in extra]
    return None


# ── Result store ────────────────────────────────────────────────────────────

def _results_path(project) -> Path:
    return Path(project) / CACHE_DIR / RESULTS_FILE


def lookup(project, fingerprint: str) -> CacheHit | None:
    """The most recent PASS recorded for this fingerprint, if any."""
    path = _results_path(project)
    if not fingerprint or not path.is_file():
        return None
    hit = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("fingerprint") == fingerprint:
                hit = CacheHit(fingerprint=fingerprint,
                               run_id=row.get("run_id", ""),
                               at=row.get("at", ""),
                               duration_ms=int(row.get("duration_ms") or 0))
    except OSError:
        return None
    return hit


def record(project, fingerprint: str, run_id: str, at: str,
           duration_ms: int = 0) -> None:
    """Remember that this fingerprint passed. Only ever called for a PASS."""
    if not fingerprint:
        return
    path = _results_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"fingerprint": fingerprint, "run_id": run_id,
                                 "at": at, "duration_ms": duration_ms}) + "\n")
        _trim(path)
    except OSError:
        pass


def _trim(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_RESULTS * 2:
            path.write_text("\n".join(lines[-MAX_RESULTS:]) + "\n",
                            encoding="utf-8")
    except OSError:
        pass


def clear(project) -> int:
    """Drop every cached result and dependency cache. Returns bytes freed."""
    base = Path(project) / CACHE_DIR
    if not base.is_dir():
        return 0
    size = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
    shutil.rmtree(base, ignore_errors=True)
    return size


def stats(project) -> dict:
    path = _results_path(project)
    entries = 0
    if path.is_file():
        try:
            entries = sum(1 for ln in path.read_text(encoding="utf-8").splitlines()
                          if ln.strip())
        except OSError:
            entries = 0
    deps = Path(project) / CACHE_DIR / DEPS_DIR
    dep_keys = len([d for d in deps.iterdir()]) if deps.is_dir() else 0
    return {"results": entries, "dependency_keys": dep_keys,
            "dir": str(Path(project) / CACHE_DIR)}


# ── actions/cache — the one cache nothing else provides locally ─────────────

def dep_cache_dir(project, key: str) -> Path:
    return Path(project) / CACHE_DIR / DEPS_DIR / _sha(key)[:32]


def restore_dependency(project, key: str, paths: list) -> bool:
    """Restore a previously saved `actions/cache` entry. True when it hit."""
    store = dep_cache_dir(project, key)
    manifest = store / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for rel in entries.get("paths", []):
        src = store / "payload" / _sha(rel)[:24]
        dest = Path(project) / rel
        if not src.exists():
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
        except OSError:
            return False
    return True


def save_dependency(project, key: str, paths: list) -> bool:
    """Save paths under an `actions/cache` key. Immutable, like GitHub's."""
    store = dep_cache_dir(project, key)
    if (store / "manifest.json").is_file():
        return False            # GitHub cache keys never overwrite
    saved: list = []
    try:
        (store / "payload").mkdir(parents=True, exist_ok=True)
        for rel in paths:
            src = Path(project) / rel
            if not src.exists():
                continue
            dest = store / "payload" / _sha(rel)[:24]
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
            saved.append(rel)
        (store / "manifest.json").write_text(
            json.dumps({"key": key, "paths": saved}), encoding="utf-8")
    except OSError:
        return False
    return bool(saved)
