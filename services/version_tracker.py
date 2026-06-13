"""VersionTracker - Git-aware version tracking for key project files."""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.ide import get_profile
from services.git_context import GitContext


class VersionTracker:
    """Track key file versions with Git metadata when available."""

    DEFAULT_MAX_KEY_FILES = 10
    HISTORY_LIMIT = 12

    def __init__(self, project_path: str, ide_name: str = "claude-code"):
        self.project_path = Path(project_path).resolve()
        self.ide_name = ide_name
        self.store_path = self.project_path / ".c3" / "version_tracker.json"
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._git = GitContext(self.project_path)
        self._git_root = self._git.git_root
        self.state = self._load_state()

    def scan(self, agent: str = "current", max_files: int | None = None) -> dict:
        target = self.ide_name if agent in ("", "current", None) else str(agent)
        key_files = self.discover_key_files(agent=target, max_files=max_files)
        tracked = {}
        changed = []
        now = datetime.now(timezone.utc).isoformat()

        for item in key_files:
            rel_path = item["file"]
            record = self._build_record(rel_path, item.get("reason", ""))
            tracked[rel_path] = record
            prev = (self.state.get("files") or {}).get(rel_path)
            if prev and self._record_signature(prev) != self._record_signature(record):
                change = {
                    "file": rel_path,
                    "reason": record.get("reason", ""),
                    "exists": record.get("exists", False),
                    "git": record.get("git", {}),
                    "previous_hash": prev.get("sha256", ""),
                    "current_hash": record.get("sha256", ""),
                }
                record["history"] = ([change] + prev.get("history", []))[:self.HISTORY_LIMIT]
                changed.append(change)
            elif prev:
                record["history"] = prev.get("history", [])[:self.HISTORY_LIMIT]
            else:
                record["history"] = []

        self.state = {
            "updated_at": now,
            "ide_name": self.ide_name,
            "agent": target,
            "git_root": str(self._git_root) if self._git_root else "",
            "files": tracked,
        }
        self._save_state()
        return {
            "agent": target,
            "updated_at": now,
            "git_available": bool(self._git_root),
            "files": list(tracked.values()),
            "changed": changed,
        }

    def get_status(self, agent: str = "current", changed_only: bool = False, max_files: int | None = None) -> dict:
        result = self.scan(agent=agent, max_files=max_files)
        if changed_only:
            changed_paths = {item["file"] for item in result["changed"]}
            result["files"] = [item for item in result["files"] if item["file"] in changed_paths]
        return result

    def discover_key_files(self, agent: str = "current", max_files: int | None = None) -> list[dict]:
        profile = get_profile(agent if agent not in ("", "current", None) else self.ide_name)
        seen = set()
        files: list[dict] = []

        def add(rel_path: str, reason: str) -> None:
            rel = str(Path(rel_path)).replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            if not rel or rel in seen:
                return
            files.append({"file": rel, "reason": reason})
            seen.add(rel)

        add(".c3/config.json", "C3 project configuration")
        if profile.instructions_file:
            add(profile.instructions_file, f"{profile.display_name} instructions")
        if profile.config_path and not profile.config_path_global:
            add(profile.config_path, f"{profile.display_name} MCP config")
        if profile.settings_path:
            add(profile.settings_path, f"{profile.display_name} settings")

        for rel_path, reason in self._hot_files():
            add(rel_path, reason)

        conventional = [
            ("README.md", "Project overview"),
            ("cli/c3.py", "CLI entry point"),
            ("cli/mcp_server.py", "MCP server entry"),
            ("services/agents.py", "Background agent logic"),
            ("core/config.py", "Shared defaults"),
        ]
        for rel_path, reason in conventional:
            if (self.project_path / rel_path).exists():
                add(rel_path, reason)

        limit = max(1, int(max_files or self.DEFAULT_MAX_KEY_FILES))
        return files[:limit]

    def _hot_files(self) -> list[tuple[str, str]]:
        session_dir = self.project_path / ".c3" / "sessions"
        if not session_dir.exists():
            return []
        counts: dict[str, int] = {}
        for sf in sorted(session_dir.glob("session_*.json"), reverse=True)[:20]:
            try:
                with open(sf, encoding="utf-8") as f:
                    session = json.load(f)
                for ft in session.get("files_touched", []):
                    rel_path = str(ft.get("file", "") or "").replace("\\", "/")
                    if rel_path:
                        counts[rel_path] = counts.get(rel_path, 0) + 1
            except Exception:
                continue
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [(path, f"edited in {count} sessions") for path, count in ranked if count >= 2][:5]

    def _build_record(self, rel_path: str, reason: str) -> dict:
        path = self.project_path / rel_path
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        mtime = path.stat().st_mtime if exists else 0
        return {
            "file": rel_path,
            "reason": reason,
            "exists": exists,
            "size": size,
            "mtime": mtime,
            "sha256": self._sha256(path) if exists and path.is_file() else "",
            "git": self._git_info(rel_path),
        }

    def _record_signature(self, record: dict) -> str:
        git = record.get("git", {}) or {}
        return "|".join([
            str(int(bool(record.get("exists")))),
            str(record.get("size", 0)),
            str(record.get("mtime", 0)),
            record.get("sha256", ""),
            git.get("commit", ""),
            str(int(bool(git.get("dirty")))),
        ])

    def _sha256(self, path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except Exception:
            return ""

    def _git_info(self, rel_path: str) -> dict:
        info = {
            "available": bool(self._git_root),
            "tracked": False,
            "dirty": False,
            "commit": "",
            "author": "",
            "timestamp": 0,
            "subject": "",
            "branch": None,
        }
        if not self._git_root:
            return info
        abs_path = (self.project_path / rel_path).resolve()
        try:
            git_rel = abs_path.relative_to(self._git_root)
        except Exception:
            return info
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(git_rel)],
                cwd=self._git_root,
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
                **kwargs
            )
            info["tracked"] = True
        except Exception:
            return info
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            status = subprocess.run(
                ["git", "status", "--porcelain", "--", str(git_rel)],
                cwd=self._git_root,
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
                **kwargs
            )
            info["dirty"] = bool((status.stdout or "").strip())
        except Exception:
            pass
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            log = subprocess.run(
                ["git", "log", "-1", "--format=%H%x1f%ct%x1f%an%x1f%s", "--", str(git_rel)],
                cwd=self._git_root,
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
                **kwargs
            )
            parts = (log.stdout or "").strip().split("\x1f")
            if len(parts) == 4:
                info["commit"] = parts[0]
                try:
                    info["timestamp"] = int(parts[1])
                except Exception:
                    info["timestamp"] = 0
                info["author"] = parts[2]
                info["subject"] = parts[3]
        except Exception:
            pass
        try:
            info["branch"] = self._git.state().get("branch")
        except Exception:
            pass
        return info

    def _load_state(self) -> dict:
        if not self.store_path.exists():
            return {"files": {}}
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("files", {})
                return data
        except Exception:
            pass
        return {"files": {}}

    def _save_state(self) -> None:
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
