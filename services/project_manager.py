"""Global project registry and session manager for C3."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from services.activity_log import ActivityLog

_GLOBAL_C3_DIR = Path.home() / ".c3"
_PROJECTS_FILE = _GLOBAL_C3_DIR / "projects.json"
_REGISTRY_FILE = _GLOBAL_C3_DIR / "registry.json"
_SESSION_ACTIVITY_GRACE_SECONDS = 20 * 60
_REGISTRY_STARTUP_GRACE = 30  # seconds: keep registry entry while server is still starting


def _pythonw() -> str:
    """Return pythonw.exe when available for hidden-background launches on Windows."""
    candidate = Path(sys.executable).parent / "pythonw.exe"
    return str(candidate) if candidate.exists() else sys.executable


def _coerce_epoch(value) -> float | None:
    """Convert a value to epoch-seconds (float).

    Accepts epoch numbers, ISO-8601 strings, or None. Registry writes from
    _register_session() store a float; older writers and test fixtures may
    have stored an ISO string. Return None if we can't interpret it.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)  # numeric string
        except ValueError:
            pass
        try:
            # Handle trailing Z and offset-aware ISO
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


class ProjectManager:
    """Manages the global C3 project registry stored in ~/.c3/projects.json."""

    def __init__(self):
        _GLOBAL_C3_DIR.mkdir(parents=True, exist_ok=True)

    def _read_projects(self) -> list:
        try:
            if _PROJECTS_FILE.exists():
                with open(_PROJECTS_FILE, encoding="utf-8") as f:
                    return json.load(f).get("projects", [])
        except Exception:
            pass
        return []

    def _write_projects(self, projects: list):
        _GLOBAL_C3_DIR.mkdir(parents=True, exist_ok=True)
        with open(_PROJECTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"projects": projects}, f, indent=2)

    def _read_registry(self) -> list:
        try:
            if _REGISTRY_FILE.exists():
                with open(_REGISTRY_FILE, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _port_alive(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except Exception:
            return False

    def _verify_c3_session(self, port: int) -> bool:
        """Verify that a given port is actually a C3 session UI."""
        try:
            url = f"http://127.0.0.1:{port}/api/health"
            with urllib.request.urlopen(url, timeout=0.8) as r:
                data = json.loads(r.read().decode("utf-8"))
                is_c3 = data.get("service") in {"c3-ui", "c3-hub"}
                # Also accept if 'sources' key is present (for older versions or custom UIs)
                return is_c3 or "sources" in data
        except Exception:
            return False

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            normalized = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _read_project_config(self, path: str) -> dict:
        config_path = Path(path) / ".c3" / "config.json"
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _get_budget_info(self, path: str) -> dict | None:
        """Read the context budget from the project's .c3 directory."""
        budget_path = Path(path) / ".c3" / "context_budget.json"
        if budget_path.exists():
            try:
                with open(budget_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def _get_live_session_info(self, path: str) -> dict | None:
        """Return live session info inferred from the project's activity log."""
        try:
            activity = ActivityLog(path)

            # Use the public API — get_recent's scan_factor=100 for typed queries
            # handles sparse rare events (session_start can sit far behind many
            # tool_call entries). Going through the API also lets tests stub it.
            starts = activity.get_recent(limit=1, event_type="session_start")
            saves = activity.get_recent(limit=1, event_type="session_save")
            last_start = starts[0] if starts else None
            last_save = saves[0] if saves else None

            if last_start is None:
                return None
            session_id = last_start.get("session_id")
            started = last_start.get("timestamp")
            if not session_id or not started:
                return None

            # If the most recent save belongs to this session, it has ended.
            if last_save and last_save.get("session_id") == session_id:
                return None

            recent = activity.get_recent(limit=1, since=started)
            last_activity = recent[0].get("timestamp") if recent else started
            started_dt = self._parse_timestamp(started)
            last_activity_dt = self._parse_timestamp(last_activity)
            if not started_dt or not last_activity_dt:
                return None
            now = datetime.now(timezone.utc)
            idle_seconds = max(0, int((now - last_activity_dt).total_seconds()))
            if idle_seconds > _SESSION_ACTIVITY_GRACE_SECONDS:
                return None
            duration_seconds = max(0, int((now - started_dt).total_seconds()))
            return {
                "session_id": session_id,
                "started_at": started,
                "last_activity": last_activity,
                "description": last_start.get("description", ""),
                "duration_seconds": duration_seconds,
                "idle_seconds": idle_seconds,
            }
        except Exception:
            return None

    def _get_last_session_timestamp(
        self, path: str, stored_value: str | None = None, live_session: dict | None = None
    ) -> str | None:
        """Return the most recent known session timestamp for a project."""
        latest_dt = None
        latest_raw = None

        def remember(value: str | None):
            nonlocal latest_dt, latest_raw
            dt = self._parse_timestamp(value)
            if dt and (latest_dt is None or dt > latest_dt):
                latest_dt = dt
                latest_raw = value

        remember(stored_value)
        if live_session:
            remember(live_session.get("last_activity"))
            remember(live_session.get("started_at"))
        try:
            activity = ActivityLog(path)
            remember((activity.get_recent(limit=1, event_type="session_save") or [{}])[0].get("timestamp"))
            remember((activity.get_recent(limit=1, event_type="session_start") or [{}])[0].get("timestamp"))
        except Exception:
            pass
        return latest_raw

    def add_project(self, path: str, name: str = None, parent_path: str = None) -> dict:
        path = str(Path(path).resolve())
        projects = self._read_projects()
        for p in projects:
            if p["path"] == path:
                if parent_path is not None:
                    p["parent_path"] = str(Path(parent_path).resolve())
                    self._write_projects(projects)
                return p
        cfg = self._read_project_config(path)
        entry = {
            "name": name or Path(path).name,
            "path": path,
            "ide": cfg.get("ide", "unknown"),
            "added_at": datetime.utcnow().isoformat() + "Z",
            "last_session": None,
            "tags": [],
            "notes": "",
        }
        if parent_path:
            entry["parent_path"] = str(Path(parent_path).resolve())
        projects.append(entry)
        self._write_projects(projects)
        return entry

    def set_parent(self, path: str, parent_path: str | None) -> bool:
        """Set or clear a project's sub-project link in the registry."""
        path = str(Path(path).resolve())
        projects = self._read_projects()
        for p in projects:
            if p["path"] == path:
                if parent_path:
                    p["parent_path"] = str(Path(parent_path).resolve())
                else:
                    p.pop("parent_path", None)
                self._write_projects(projects)
                return True
        return False

    def remove_project(self, path: str) -> bool:
        path = str(Path(path).resolve())
        projects = self._read_projects()
        filtered = [p for p in projects if p["path"] != path]
        if len(filtered) < len(projects):
            self._write_projects(filtered)
            return True
        return False

    def sweep_registry(self):
        """Remove stale registry entries (dead ports). Call on hub startup."""
        registry = self._read_registry()
        if not registry:
            return
        valid = [e for e in registry if e.get("port") and self._port_alive(e["port"])]
        if len(valid) != len(registry):
            try:
                with open(_REGISTRY_FILE, "w", encoding="utf-8") as f:
                    json.dump(valid, f, indent=2)
            except Exception:
                pass

    def list_projects(self) -> list:
        projects = self._read_projects()
        registry = self._read_registry()
        ui_active_by_path: dict = {}
        valid_registry = []
        registry_changed = False

        for entry in registry:
            port = entry.get("port")
            proj_path = entry.get("project_path", "")
            if port and self._port_alive(port):
                if self._verify_c3_session(port):
                    ui_active_by_path[proj_path] = entry
                    valid_registry.append(entry)
                else:
                    registry_changed = True
            else:
                # Grace period: keep entries registered recently — the server may still be
                # starting up between _register_session() and app.run() binding the port.
                # started_at is written as epoch float by _register_session() but older
                # entries (or test fixtures) may store an ISO string; coerce defensively.
                started_epoch = _coerce_epoch(entry.get("started_at"))
                if started_epoch is not None and time.time() - started_epoch < _REGISTRY_STARTUP_GRACE:
                    valid_registry.append(entry)  # keep it; don't mark ui_active yet
                else:
                    registry_changed = True

        if registry_changed:
            try:
                with open(_REGISTRY_FILE, "w", encoding="utf-8") as f:
                    json.dump(valid_registry, f, indent=2)
            except Exception:
                pass

        result = []
        for p in projects:
            enriched = dict(p)
            path_accessible = Path(p["path"]).is_dir()
            enriched["accessible"] = path_accessible
            ui_active = ui_active_by_path.get(p["path"])
            live_session = self._get_live_session_info(p["path"]) if path_accessible else None
            enriched["ui_active"] = ui_active is not None
            enriched["session_active"] = live_session is not None
            # A project is active if either the web UI is live or the activity log shows a
            # currently running C3 session. The hub card should not fall back to "idle"
            # just because the session has no live UI port.
            enriched["active"] = enriched["ui_active"] or enriched["session_active"]
            enriched["port"] = ui_active["port"] if ui_active else None
            enriched["budget"] = self._get_budget_info(p["path"]) if enriched["active"] else None
            enriched["started_at"] = (
                ui_active["started_at"]
                if ui_active
                else (live_session["started_at"] if live_session else None)
            )
            enriched["live_session_id"] = (
                live_session["session_id"] if live_session else None
            )
            enriched["last_activity"] = (
                live_session["last_activity"] if live_session else None
            )
            enriched["session_description"] = (
                live_session["description"] if live_session else ""
            )
            enriched["last_session"] = (
                self._get_last_session_timestamp(p["path"], p.get("last_session"), live_session)
                if path_accessible else p.get("last_session")
            )
            cfg = self._read_project_config(p["path"]) if path_accessible else {}
            if cfg:
                enriched["ide"] = cfg.get("ide", p.get("ide", "unknown"))
                enriched["c3_version"] = cfg.get("version")
                try:
                    from core.ide import PROFILES
                    prof = PROFILES.get(enriched["ide"])
                    if prof:
                        if prof.config_path_global:
                            mf = Path.home() / prof.config_path
                        else:
                            mf = Path(p["path"]) / prof.config_path
                        enriched["mcp_installed"] = mf.exists()
                        mcp_cfg = cfg.get("mcp", {})
                        enriched["mcp_mode"] = mcp_cfg.get("mode") if isinstance(mcp_cfg, dict) else None
                except Exception:
                    pass
            result.append(enriched)
        return result

    def get_active_sessions(self) -> list:
        active = []
        for e in self._read_registry():
            port = e.get("port", 0)
            if port and self._port_alive(port) and self._verify_c3_session(port):
                active.append(e)

        for project in self.list_projects():
            if project.get("session_active") and not project.get("ui_active"):
                active.append(
                    {
                        "project_path": project["path"],
                        "project_name": project.get("name", Path(project["path"]).name),
                        "port": None,
                        "started_at": project.get("started_at"),
                        "live_session_id": project.get("live_session_id"),
                    }
                )
        return active

    def launch_session(self, path: str) -> dict:
        """Launch a C3 UI session for a project.

        Returns dict with 'launched' bool and optional 'error' string.
        """
        if not Path(path).is_dir():
            return {"launched": False, "error": f"Project path not accessible: {path}"}
        c3_py = Path(__file__).parent.parent / "cli" / "c3.py"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        kwargs = {}
        cmd = [sys.executable, str(c3_py), "ui", path, "--no-browser", "--silent"]
        log_dir = Path(path) / ".c3"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "ui.log"
        if sys.platform == "win32":
            cmd[0] = _pythonw()
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            kwargs["close_fds"] = True
            kwargs["stdout"] = open(log_file, "a", encoding="utf-8")
            kwargs["stderr"] = subprocess.STDOUT
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["start_new_session"] = True
            kwargs["stdout"] = open(log_file, "a", encoding="utf-8")
            kwargs["stderr"] = subprocess.STDOUT
        try:
            subprocess.Popen(cmd, cwd=path, env=env, **kwargs)
            return {"launched": True}
        except Exception as e:
            return {"launched": False, "error": str(e)}

    def stop_session(self, port: int) -> bool:
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32":
                result = subprocess.run(f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True, **kwargs)
                pids = set()
                for line in result.stdout.strip().splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.strip().split()
                        if len(parts) >= 5:
                            pids.add(parts[-1])
                for pid in pids:
                    subprocess.run(f"taskkill /PID {pid} /F", shell=True, capture_output=True, **kwargs)
            else:
                subprocess.run(f"lsof -ti:{port} | xargs kill -9", shell=True, capture_output=True)
            return True
        except Exception:
            return False

    def end_mcp_session(self, path: str) -> bool:
        """End an MCP-only session by writing a session_save event to the activity log."""
        live = self._get_live_session_info(path)
        if not live:
            return False
        activity = ActivityLog(path)
        activity.log("session_save", {"session_id": live["session_id"], "source": "hub"})
        return True

    def update_last_session(self, path: str):
        path = str(Path(path).resolve())
        projects = self._read_projects()
        for p in projects:
            if p["path"] == path:
                p["last_session"] = datetime.utcnow().isoformat() + "Z"
                break
        self._write_projects(projects)

    def get_project_details(self, path: str) -> dict:
        path = str(Path(path).resolve())
        cfg = self._read_project_config(path)
        ide = cfg.get("ide", "unknown")
        mcp_cfg = cfg.get("mcp", {})
        mcp_mode = mcp_cfg.get("mode", "unknown") if isinstance(mcp_cfg, dict) else "unknown"
        mcp_installed = False
        mcp_config_path = None
        mcp_servers: list = []
        try:
            from core.ide import PROFILES
            profile = PROFILES.get(ide)
            if profile:
                if profile.config_path_global:
                    mcp_file = Path.home() / profile.config_path
                else:
                    mcp_file = Path(path) / profile.config_path
                if mcp_file.exists():
                    mcp_installed = True
                    mcp_config_path = str(mcp_file)
                    if profile.config_format == "json":
                        with open(mcp_file, encoding="utf-8") as f:
                            mcp_data = json.load(f)
                        servers = mcp_data.get(profile.config_key, {})
                        if isinstance(servers, dict):
                            for name, conf in servers.items():
                                mcp_servers.append({
                                    "name": name,
                                    "command": conf.get("command", ""),
                                    "args": conf.get("args", []),
                                    "type": conf.get("type", ""),
                                    "env_keys": list((conf.get("env") or {}).keys()),
                                })
                    elif profile.config_format == "toml":
                        try:
                            import tomllib
                        except ImportError:
                            try:
                                import tomli as tomllib
                            except ImportError:
                                tomllib = None
                        if tomllib:
                            with open(mcp_file, "rb") as f:
                                toml_data = tomllib.load(f)
                            for name, conf in toml_data.get("mcp_servers", {}).items():
                                mcp_servers.append({
                                    "name": name,
                                    "command": conf.get("command", ""),
                                    "args": conf.get("args", []),
                                    "type": "",
                                    "env_keys": [],
                                })
        except Exception:
            pass
        return {
            "path": path,
            "c3_version": cfg.get("version"),
            "ide": ide,
            "mcp_mode": mcp_mode,
            "mcp_installed": mcp_installed,
            "mcp_config_path": mcp_config_path,
            "mcp_servers": mcp_servers,
            "initialized": bool(cfg),
        }

    def update_project(self, path: str, **fields) -> bool:
        """Update editable project fields: name, tags, notes."""
        path = str(Path(path).resolve())
        projects = self._read_projects()
        allowed = {"name", "tags", "notes", "autostart_ui"}
        for p in projects:
            if p["path"] == path:
                for k, v in fields.items():
                    if k in allowed:
                        p[k] = v
                self._write_projects(projects)
                return True
        return False

    def get_autostart_projects(self) -> list[str]:
        """Return paths of projects with autostart_ui=True that are accessible."""
        return [
            p["path"] for p in self._read_projects()
            if p.get("autostart_ui") and Path(p["path"]).is_dir()
        ]

    def rename_project(self, path: str, new_name: str) -> bool:
        path = str(Path(path).resolve())
        projects = self._read_projects()
        for p in projects:
            if p["path"] == path:
                p["name"] = new_name
                self._write_projects(projects)
                return True
        return False

    def transfer_project(self, old_path: str, new_path: str) -> dict:
        """Transfer a project registration from old_path to new_path.

        Validates the new location exists with a .c3/ directory and updates
        the registry to point there. Does not move files — the user handles
        the actual copy/move.
        """
        old_path = str(Path(old_path).resolve())
        new_path = str(Path(new_path).resolve())

        if old_path == new_path:
            return {"transferred": False, "error": "Paths are identical"}

        if not Path(new_path).is_dir():
            return {"transferred": False, "error": "New path does not exist"}

        if not (Path(new_path) / ".c3").is_dir():
            return {"transferred": False, "error": "New path has no .c3 directory"}

        projects = self._read_projects()

        entry = None
        for p in projects:
            if p["path"] == old_path:
                entry = p
                break
        if entry is None:
            return {"transferred": False, "error": "Project not registered"}

        for p in projects:
            if p["path"] == new_path:
                return {"transferred": False, "error": "New path already registered"}

        # Update entry
        old_dir_name = Path(old_path).name
        entry["path"] = new_path
        if entry.get("name") == old_dir_name:
            entry["name"] = Path(new_path).name

        # Re-read config from new location
        cfg = self._read_project_config(new_path)
        if cfg.get("ide"):
            entry["ide"] = cfg["ide"]

        self._write_projects(projects)

        # If the moved project designates sub-projects, repair their links
        # (child back-links + registry parent_path re-derived via rel_path).
        try:
            from services.subprojects import SubprojectManager
            SubprojectManager(new_path).reconcile(fix=True)
        except Exception:
            pass

        return {"transferred": True, "old_path": old_path, "new_path": new_path}

    def merge_projects(self, source_path: str, target_path: str, cleanup: str = "keep") -> dict:
        """Merge source project's memory/sessions/ledger into target.

        Combines facts (.c3/facts/facts.json), edit-ledger entries
        (.c3/edit_ledger.jsonl), conversation sessions (.c3/conversations/),
        and unions registry tags + appends notes. Skips file_memory and
        indices because their paths reference source files that don't
        exist in the target.

        Args:
            source_path: project being merged FROM.
            target_path: project being merged INTO.
            cleanup: "keep" leaves source untouched after the merge;
                     "clear" wipes source's .c3/, MCP configs and
                     instruction docs (equivalent to ``c3 init --clear``)
                     and removes its registry entry.

        Returns:
            ``{"merged": True, "source", "target", "cleanup", "stats": {...}}``
            on success or ``{"merged": False, "error": "..."}`` on validation
            failure.
        """
        import shutil
        import uuid

        if cleanup not in ("keep", "clear"):
            return {"merged": False, "error": "cleanup must be 'keep' or 'clear'"}

        src = Path(source_path).resolve()
        tgt = Path(target_path).resolve()

        if str(src) == str(tgt):
            return {"merged": False, "error": "Paths are identical"}
        if not src.is_dir():
            return {"merged": False, "error": "Source path does not exist"}
        if not tgt.is_dir():
            return {"merged": False, "error": "Target path does not exist"}
        if not (src / ".c3").is_dir():
            return {"merged": False, "error": "Source has no .c3 directory"}
        if not (tgt / ".c3").is_dir():
            return {"merged": False, "error": "Target has no .c3 directory"}

        projects = self._read_projects()
        src_entry = next((p for p in projects if p.get("path") == str(src)), None)
        tgt_entry = next((p for p in projects if p.get("path") == str(tgt)), None)
        if src_entry is None:
            return {"merged": False, "error": "Source project not registered"}
        if tgt_entry is None:
            return {"merged": False, "error": "Target project not registered"}

        src_name = src_entry.get("name") or src.name
        slug = "".join(c if c.isalnum() else "_" for c in src_name.lower())[:32] or "merged"
        merge_tag = f"merged:{slug}"
        merged_at = datetime.utcnow().isoformat() + "Z"

        stats = {"facts": 0, "ledger_entries": 0, "sessions": 0}
        warnings: list[str] = []

        # ── Facts ────────────────────────────────────────────────────
        src_facts_file = src / ".c3" / "facts" / "facts.json"
        tgt_facts_dir = tgt / ".c3" / "facts"
        tgt_facts_file = tgt_facts_dir / "facts.json"

        if src_facts_file.exists():
            try:
                with open(src_facts_file, encoding="utf-8") as f:
                    src_facts = json.load(f) or []
            except Exception as e:
                src_facts = []
                warnings.append(f"facts read failed: {e}")

            if src_facts:
                tgt_facts: list = []
                if tgt_facts_file.exists():
                    try:
                        with open(tgt_facts_file, encoding="utf-8") as f:
                            tgt_facts = json.load(f) or []
                    except Exception as e:
                        warnings.append(f"target facts read failed: {e}")
                        tgt_facts = []
                for fact in src_facts:
                    if not isinstance(fact, dict):
                        continue
                    new_fact = dict(fact)
                    new_id = uuid.uuid4().hex[:12]
                    new_fact["id"] = new_id
                    new_fact["vector_id"] = new_id
                    new_fact["merged_from"] = src_name
                    new_fact["merged_at"] = merged_at
                    tgt_facts.append(new_fact)
                    stats["facts"] += 1
                tgt_facts_dir.mkdir(parents=True, exist_ok=True)
                with open(tgt_facts_file, "w", encoding="utf-8") as f:
                    json.dump(tgt_facts, f, indent=2)

        # ── Edit ledger ──────────────────────────────────────────────
        src_ledger = src / ".c3" / "edit_ledger.jsonl"
        tgt_ledger = tgt / ".c3" / "edit_ledger.jsonl"
        if src_ledger.exists():
            tgt_ledger.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(tgt_ledger, "a", encoding="utf-8") as out:
                    for line in src_ledger.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        summary = entry.get("summary") or ""
                        entry["summary"] = f"[merged from {src_name}] {summary}".rstrip()
                        tags = list(entry.get("tags") or [])
                        if merge_tag not in tags:
                            tags.append(merge_tag)
                        entry["tags"] = tags
                        entry["merged_from"] = src_name
                        out.write(json.dumps(entry) + "\n")
                        stats["ledger_entries"] += 1
            except Exception as e:
                warnings.append(f"ledger merge failed: {e}")

        # ── Conversations ────────────────────────────────────────────
        src_conv = src / ".c3" / "conversations"
        tgt_conv = tgt / ".c3" / "conversations"
        if src_conv.is_dir():
            tgt_conv.mkdir(parents=True, exist_ok=True)
            renamed: dict[str, str] = {}  # old_id -> new_id

            # Load target sessions index up-front so we know what IDs collide.
            tgt_sessions: list = []
            tgt_sessions_file = tgt_conv / "sessions.json"
            if tgt_sessions_file.exists():
                try:
                    with open(tgt_sessions_file, encoding="utf-8") as f:
                        tgt_sessions = json.load(f) or []
                except Exception as e:
                    warnings.append(f"target sessions read failed: {e}")
                    tgt_sessions = []
            tgt_ids = {s.get("session_id") for s in tgt_sessions if s.get("session_id")}

            # Copy turn files (rename on collision).
            for entry_path in src_conv.iterdir():
                if not entry_path.is_file():
                    continue
                if entry_path.name == "sessions.json":
                    continue
                if entry_path.name.endswith(".jsonl.gz"):
                    base = entry_path.name[:-len(".jsonl.gz")]
                    ext = ".jsonl.gz"
                elif entry_path.suffix == ".jsonl":
                    base = entry_path.stem
                    ext = ".jsonl"
                else:
                    continue
                new_base = base
                if base in tgt_ids or (tgt_conv / (base + ext)).exists():
                    new_base = f"{base}_merged_{uuid.uuid4().hex[:6]}"
                    renamed[base] = new_base
                try:
                    shutil.copy2(entry_path, tgt_conv / (new_base + ext))
                except Exception as e:
                    warnings.append(f"session copy {entry_path.name} failed: {e}")

            # Merge sessions index.
            src_sessions_file = src_conv / "sessions.json"
            if src_sessions_file.exists():
                try:
                    with open(src_sessions_file, encoding="utf-8") as f:
                        src_sessions = json.load(f) or []
                except Exception as e:
                    src_sessions = []
                    warnings.append(f"source sessions read failed: {e}")
                for s in src_sessions:
                    if not isinstance(s, dict):
                        continue
                    new_s = dict(s)
                    sid = new_s.get("session_id", "")
                    if sid in renamed:
                        new_s["session_id"] = renamed[sid]
                    elif sid and sid in tgt_ids:
                        new_s["session_id"] = f"{sid}_merged_{uuid.uuid4().hex[:6]}"
                    new_s["merged_from"] = src_name
                    new_s["merged_at"] = merged_at
                    tgt_sessions.append(new_s)
                    stats["sessions"] += 1
                with open(tgt_sessions_file, "w", encoding="utf-8") as f:
                    json.dump(tgt_sessions, f, ensure_ascii=False, indent=2)
            else:
                # No index in source — count copied turn files as sessions.
                stats["sessions"] = len(list(tgt_conv.iterdir())) - (1 if tgt_sessions_file.exists() else 0)

        # ── Registry tags + notes ────────────────────────────────────
        for p in projects:
            if p.get("path") == str(tgt):
                tags = list(p.get("tags") or [])
                for t in (src_entry.get("tags") or []):
                    if t and t not in tags:
                        tags.append(t)
                p["tags"] = tags
                src_notes = (src_entry.get("notes") or "").strip()
                if src_notes:
                    tgt_notes = (p.get("notes") or "").strip()
                    sep = f"--- merged from {src_name} ---"
                    p["notes"] = f"{tgt_notes}\n\n{sep}\n{src_notes}".strip() if tgt_notes else f"{sep}\n{src_notes}"
                break
        self._write_projects(projects)

        # ── Cleanup ──────────────────────────────────────────────────
        if cleanup == "clear":
            try:
                # Lazy import to avoid services -> cli circular import at module load.
                from cli.c3 import _instruction_documents_for_project, _uninstall_mcp_all
                try:
                    # Project cleanup only — the machine-wide IDE registrations
                    # (~/.codex, Antigravity) serve every other C3 project.
                    _uninstall_mcp_all(str(src), include_global=False)
                except Exception as e:
                    warnings.append(f"uninstall_mcp failed: {e}")
                c3_dir = src / ".c3"
                if c3_dir.exists():
                    try:
                        shutil.rmtree(c3_dir)
                    except Exception as e:
                        warnings.append(f"rmtree .c3 failed: {e}")
                for filename, _ in _instruction_documents_for_project():
                    doc = src / filename
                    if doc.exists():
                        try:
                            doc.unlink()
                        except Exception as e:
                            warnings.append(f"delete {filename} failed: {e}")
            except Exception as e:
                warnings.append(f"cleanup helpers unavailable: {e}")
            self.remove_project(str(src))

        result = {
            "merged": True,
            "source": str(src),
            "target": str(tgt),
            "cleanup": cleanup,
            "stats": stats,
        }
        if warnings:
            result["warnings"] = warnings
        return result
