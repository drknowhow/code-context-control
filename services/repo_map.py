"""RepoMapService — live, byte-stable repository map at .c3/MAP.md.

Replaces the Project Context tree that used to be frozen inside
CLAUDE.md / AGENTS.md (v2.60.0). Design points (CodYep-reviewed):

- MAP.md is machine-owned and byte-stable: it is rewritten only when the
  rendered content actually changes, so prompt caches keyed on file bytes
  stay warm across refreshes.
- Volatile freshness state (generated_at, git head, fingerprint) lives in
  .c3/map.meta.json — never inside MAP.md.
- .c3/map.dirty is a cheap sentinel hooks touch on structural changes
  (create/delete/rename, dependency-manifest edits).
- Generation is cross-process safe: exclusive lock file with stale-lock
  recovery + temp-file write + os.replace (atomic on Windows and POSIX).
- The map is repository DATA, not instructions: the header says so, and
  free-form memory content is deliberately excluded (injection surface).
"""
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MAP_NAME = "MAP.md"
META_NAME = "map.meta.json"
DIRTY_NAME = "map.dirty"
LOCK_NAME = "map.lock"

_LOCK_STALE_SECONDS = 120
_DEFAULT_TOKEN_BUDGET = 1000
_DEFAULT_FILE_CAP = 4000
_PORCELAIN_CAP = 400          # bounded: names-only status lines in fingerprint
_GIT_TIMEOUT = 10

# Dependency manifests: an edit to one of these is a structural change.
MANIFEST_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "gemfile", "composer.json", "makefile",
}

_HEADER = (
    "<!-- c3:repo-map schema={schema} -->\n"
    "<!-- Auto-generated repository DATA (not instructions). Do not edit.\n"
    "     Refresh: c3 map refresh | Freshness state: .c3/map.meta.json -->\n"
)


def mark_map_dirty(project_path, reason: str = "") -> None:
    """Touch the dirty sentinel. Cheap, idempotent, safe to call from hooks.

    Importable without constructing the service so PostToolUse hooks pay
    only a file-touch, never a scan.
    """
    try:
        c3_dir = Path(project_path) / ".c3"
        c3_dir.mkdir(parents=True, exist_ok=True)
        sentinel = c3_dir / DIRTY_NAME
        stamp = f"{datetime.now(timezone.utc).isoformat()} {reason}\n"
        if sentinel.exists() and sentinel.stat().st_size > 4096:
            sentinel.write_text(stamp, encoding="utf-8")  # reset runaway file
        else:
            with open(sentinel, "a", encoding="utf-8") as f:
                f.write(stamp)
    except OSError:
        pass  # a failed dirty-mark must never break the calling hook


def is_structural_change(file_path: str, change_type: str = "") -> bool:
    """True when an edit should dirty the map: create/delete/rename or a
    dependency-manifest change. Ordinary line edits return False."""
    if change_type in ("create", "delete", "rename"):
        return True
    return Path(file_path).name.lower() in MANIFEST_NAMES


class RepoMapService:
    """Owns .c3/MAP.md rendering, freshness checks, and atomic writes."""

    def __init__(self, project_path, session_mgr=None):
        self.project_path = Path(project_path)
        self.c3_dir = self.project_path / ".c3"
        self.session_mgr = session_mgr
        cfg = self._config().get("map", {})
        self.token_budget = int(cfg.get("token_budget", _DEFAULT_TOKEN_BUDGET))
        self.file_cap = int(cfg.get("file_cap", _DEFAULT_FILE_CAP))
        self.enabled = bool(cfg.get("enabled", True))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def map_path(self) -> Path:
        return self.c3_dir / MAP_NAME

    @property
    def meta_path(self) -> Path:
        return self.c3_dir / META_NAME

    @property
    def dirty_path(self) -> Path:
        return self.c3_dir / DIRTY_NAME

    def status(self) -> dict:
        """Freshness report. Read-only, never regenerates."""
        meta = self._read_meta()
        stale, reasons = self._is_stale(meta)
        out = {
            "exists": self.map_path.exists(),
            "stale": stale,
            "reasons": reasons,
            "schema": SCHEMA_VERSION,
        }
        if meta:
            out["generated_at"] = meta.get("generated_at")
            out["head"] = meta.get("head")
            out["truncated"] = meta.get("truncated", False)
            out["tokens"] = meta.get("tokens")
        return out

    def ensure(self, force: bool = False) -> dict:
        """Regenerate if missing/dirty/stale. Single-flight across processes:
        if another process holds the lock, report and return (it is already
        doing the work)."""
        if not self.enabled:
            return {"action": "disabled"}
        meta = self._read_meta()
        stale, reasons = self._is_stale(meta)
        if not stale and not force:
            return {"action": "fresh", "reasons": []}

        lock = self._acquire_lock()
        if lock is None:
            return {"action": "locked", "reasons": reasons}
        try:
            return self._regenerate(reasons if not force else ["forced"])
        finally:
            self._release_lock(lock)

    def refresh(self) -> dict:
        """Explicit repair command — always regenerates."""
        return self.ensure(force=True)

    # ------------------------------------------------------------------
    # Freshness
    # ------------------------------------------------------------------

    def _config(self) -> dict:
        try:
            with open(self.c3_dir / "config.json", encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}

    def _read_meta(self) -> dict:
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}

    def _git(self, *args) -> str:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(self.project_path),
                capture_output=True, text=True, timeout=_GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _fingerprint(self) -> dict:
        """Cheap identity of the working tree. Git: HEAD + branch + the
        bounded set of changed path names (names only — a content edit to an
        existing file does not alter navigation structure, but creates,
        deletes, and renames do). Non-git: bounded walk fingerprint."""
        head = self._git("rev-parse", "HEAD")
        if head:
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
            porcelain = self._git("status", "--porcelain")
            names = sorted(
                line[3:] for line in porcelain.splitlines()[:_PORCELAIN_CAP]
                if len(line) > 3
            )
            sig = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
            return {"kind": "git", "head": head, "branch": branch,
                    "worktree_sig": sig,
                    "root": str(self.project_path.resolve())}
        return {"kind": "plain", "head": "", "branch": "",
                "worktree_sig": self._plain_signature(),
                "root": str(self.project_path.resolve())}

    def _plain_signature(self) -> str:
        from services.scanner import iter_files
        h = hashlib.sha256()
        for p in iter_files(self.project_path, max_files=self.file_cap):
            try:
                st = p.stat()
            except OSError:
                continue
            rel = str(p.relative_to(self.project_path))
            h.update(f"{rel}|{st.st_mtime_ns}|{st.st_size}\n".encode("utf-8"))
        return h.hexdigest()[:16]

    def _is_stale(self, meta: dict) -> tuple:
        reasons = []
        if not self.map_path.exists():
            reasons.append("missing_map")
        if not meta:
            reasons.append("missing_meta")
        elif meta.get("schema") != SCHEMA_VERSION:
            reasons.append("schema_changed")
        if self.dirty_path.exists():
            reasons.append("dirty_sentinel")
        if meta and "schema_changed" not in reasons:
            fp = self._fingerprint()
            old = meta.get("fingerprint", {})
            if fp.get("head") != old.get("head"):
                reasons.append("head_changed")
            if fp.get("branch") != old.get("branch"):
                reasons.append("branch_changed")
            if fp.get("worktree_sig") != old.get("worktree_sig"):
                reasons.append("worktree_changed")
            if fp.get("root") != old.get("root"):
                reasons.append("root_moved")
        return (bool(reasons), reasons)

    # ------------------------------------------------------------------
    # Locking (cross-process, Windows-safe)
    # ------------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.c3_dir / LOCK_NAME

    def _acquire_lock(self):
        """O_CREAT|O_EXCL lock file with stale-lock recovery. Returns an fd
        or None when another live process holds it (single-flight)."""
        self.c3_dir.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(str(self.lock_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode("utf-8"))
                return fd
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    continue  # lock vanished between attempts — retry
                if age > _LOCK_STALE_SECONDS and attempt == 1:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        return None
                    continue
                return None
        return None

    def _release_lock(self, fd) -> None:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            self.lock_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def _regenerate(self, reasons: list) -> dict:
        t0 = time.monotonic()
        fp = self._fingerprint()
        content, render_info = self._render()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

        changed = True
        try:
            if self.map_path.exists():
                old = self.map_path.read_text(encoding="utf-8")
                changed = old != content
        except OSError:
            changed = True

        if changed:
            self._write_atomic(self.map_path, content, backup=True)

        from core import count_tokens
        meta = {
            "schema": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fp,
            "head": fp.get("head", ""),
            "content_hash": content_hash,
            "tokens": count_tokens(content),
            "truncated": render_info.get("truncated", False),
            "files_walked": render_info.get("files_walked", 0),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "reasons": reasons,
        }
        self._write_atomic(self.meta_path, json.dumps(meta, indent=2))
        try:
            self.dirty_path.unlink()
        except OSError:
            pass
        return {"action": "regenerated" if changed else "meta_only",
                "reasons": reasons, "tokens": meta["tokens"],
                "truncated": meta["truncated"],
                "duration_ms": meta["duration_ms"]}

    def _write_atomic(self, path: Path, content: str, backup: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if backup and path.exists():
            try:
                os.replace(path, path.with_suffix(path.suffix + ".bak"))
            except OSError:
                pass
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Rendering (byte-stable: no timestamps, no counts that churn)
    # ------------------------------------------------------------------

    def _render(self) -> tuple:
        info = {"truncated": False, "files_walked": 0}
        parts = [_HEADER.format(schema=SCHEMA_VERSION)]
        parts.append(f"# Repo Map — {self.project_path.name}\n")

        commands = self._detect_commands()
        if commands:
            parts.append("## Commands\n")
            parts.extend(f"- {name}: `{cmd}`" for name, cmd in commands)
            parts.append("")

        entries = self._detect_entry_points()
        if entries:
            parts.append("## Entry points\n")
            parts.extend(f"- `{e}`" for e in entries)
            parts.append("")

        modules = self._module_boundaries()
        if modules:
            parts.append("## Modules\n")
            parts.extend(f"- `{name}/` — {desc}" for name, desc in modules)
            parts.append("")

        subs = self._subproject_boundaries()
        if subs:
            parts.append("## Sub-projects (own maps — not expanded here)\n")
            parts.extend(f"- `{rel}` → `{rel}/.c3/MAP.md`" for rel in subs)
            parts.append("")

        tree, walked, truncated = self._render_tree(exclude=set(subs))
        info["files_walked"] = walked
        info["truncated"] = truncated
        parts.append("## Tree\n")
        parts.append(tree)
        if truncated:
            parts.append(f"\nMap truncated: walk capped at {self.file_cap} files.")
        parts.append("")

        key_files = self._key_files()
        if key_files:
            parts.append("## Key files\n")
            parts.extend(f"- `{kf}` — {reason}" for kf, reason in key_files)
            parts.append("")

        content = "\n".join(parts).rstrip() + "\n"
        content = self._fit_budget(content, info)
        return content, info

    def _fit_budget(self, content: str, info: dict) -> str:
        """Deterministic budget passes: full → dirs-only tree."""
        from core import count_tokens
        if count_tokens(content) <= self.token_budget:
            return content
        lines, out, in_tree = content.splitlines(), [], False
        for line in lines:
            if line.strip().startswith("```"):
                in_tree = not in_tree
                out.append(line)
                continue
            if in_tree and not line.rstrip().endswith("/") \
                    and "/" not in line.strip() and line.strip():
                continue  # drop file rows, keep directory rows
            out.append(line)
        slimmed = "\n".join(out)
        if count_tokens(slimmed) > self.token_budget:
            info["truncated"] = True
        return slimmed

    # -- section builders ----------------------------------------------

    def _detect_commands(self) -> list:
        cmds = []
        if (self.project_path / "pyproject.toml").exists():
            if (self.project_path / "tests").is_dir():
                cmds.append(("test", "python -m pytest"))
            cmds.append(("install", "pip install -e ."))
        pkg = self.project_path / "package.json"
        if pkg.exists():
            try:
                scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
                for name in ("test", "build", "start", "lint", "dev"):
                    if name in scripts:
                        cmds.append((name, f"npm run {name}"))
            except (OSError, ValueError):
                pass
        if (self.project_path / "Makefile").exists():
            cmds.append(("make", "make <target> (see Makefile)"))
        if (self.project_path / "Cargo.toml").exists():
            cmds.append(("test", "cargo test"))
            cmds.append(("build", "cargo build"))
        seen, out = set(), []
        for name, cmd in cmds:
            if name not in seen:
                seen.add(name)
                out.append((name, cmd))
        return out

    def _detect_entry_points(self) -> list:
        out = []
        py = self.project_path / "pyproject.toml"
        if py.exists():
            try:
                text = py.read_text(encoding="utf-8")
                in_scripts = False
                for line in text.splitlines():
                    s = line.strip()
                    if s.startswith("[project.scripts]"):
                        in_scripts = True
                        continue
                    if in_scripts:
                        if s.startswith("["):
                            break
                        if "=" in s:
                            name, target = (x.strip().strip('"') for x in s.split("=", 1))
                            out.append(f"{name} = {target}")
            except OSError:
                pass
        for candidate in ("main.py", "app.py", "server.py", "cli.py", "__main__.py"):
            if (self.project_path / candidate).exists():
                out.append(candidate)
        pkg = self.project_path / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                if data.get("main"):
                    out.append(str(data["main"]))
            except (OSError, ValueError):
                pass
        return sorted(set(out))[:10]

    def _module_boundaries(self) -> list:
        """Top-level directories with a one-line purpose. Deterministic:
        docstring of __init__.py, else first heading of the dir's README,
        else nothing (no churn-prone file counts)."""
        from services.scanner import make_dir_pruner
        pruned = make_dir_pruner(self.project_path)
        out = []
        try:
            dirs = sorted(
                d.name for d in self.project_path.iterdir()
                if d.is_dir() and not pruned(d.name)
            )
        except OSError:
            return out
        for name in dirs[:20]:
            desc = self._describe_dir(self.project_path / name)
            out.append((name, desc))
        return out

    def _describe_dir(self, path: Path) -> str:
        init = path / "__init__.py"
        if init.exists():
            try:
                for enc_line in init.read_text(encoding="utf-8",
                                               errors="replace").splitlines()[:5]:
                    s = enc_line.strip().strip('"""').strip("'''").strip()
                    if s and not s.startswith(("#", "import", "from")):
                        return s[:100]
            except OSError:
                pass
        readme = path / "README.md"
        if readme.exists():
            try:
                for line in readme.read_text(encoding="utf-8",
                                             errors="replace").splitlines()[:10]:
                    s = line.strip().lstrip("#").strip()
                    if s:
                        return s[:100]
            except OSError:
                pass
        return "(no description)"

    def _subproject_boundaries(self) -> list:
        try:
            from services.subprojects import get_subprojects
            entries = get_subprojects(self.project_path)
            return sorted(
                e.get("rel_path", "") for e in entries if e.get("rel_path")
            )
        except Exception:
            return []

    def _render_tree(self, exclude=None) -> tuple:
        """Depth-2 tree via the shared scanner rules. Sub-project roots are
        boundaries: named, never expanded."""
        from services.scanner import make_dir_pruner
        exclude = {Path(e).as_posix() for e in (exclude or set())}
        pruned = make_dir_pruner(self.project_path)
        lines = ["```"]
        walked = 0
        truncated = False
        for root, dirs, files in os.walk(self.project_path):
            rel = Path(root).relative_to(self.project_path)
            rel_posix = rel.as_posix()
            if rel_posix in exclude:
                dirs[:] = []
                lines.append("  " * len(rel.parts) + f"{rel.name}/ [sub-project]")
                continue
            dirs[:] = sorted(d for d in dirs if not pruned(d))
            level = len(rel.parts)
            indent = "  " * level
            shown = sorted(f for f in files if not f.startswith("."))
            walked += len(shown)
            if walked > self.file_cap:
                truncated = True
                lines.append(f"{indent}...")
                break
            if level >= 2:
                count = f" ({len(shown)} files)" if shown else ""
                lines.append(f"{indent}{rel.name or '.'}/{count}")
                dirs[:] = []
                continue
            lines.append(f"{indent}{(rel.name or self.project_path.name)}/")
            for f in shown[:15]:
                lines.append(f"{indent}  {f}")
            if len(shown) > 15:
                lines.append(f"{indent}  ... +{len(shown) - 15} more")
        lines.append("```")
        return "\n".join(lines), walked, truncated

    def _key_files(self) -> list:
        """Conventional key files + session-history hits when available.
        Reasons are stable phrases — no counts, no timestamps (byte churn)."""
        out = []
        if self.session_mgr is not None:
            try:
                for kf in self.session_mgr._detect_key_files()[:5]:
                    out.append((kf["file"], "frequently edited (session history)"))
            except Exception:
                pass
        for conventional in ("README.md", "pyproject.toml", "package.json"):
            if (self.project_path / conventional).exists():
                out.append((conventional, "project definition"))
        seen, dedup = set(), []
        for f, r in out:
            if f not in seen:
                seen.add(f)
                dedup.append((f, r))
        return dedup[:8]
