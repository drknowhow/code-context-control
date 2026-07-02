"""ArtifactStore — version history for agent-affecting files.

Tracks the files that shape an AI coding agent's behavior (instruction docs,
settings/hooks, MCP configs, skills/agents/commands/plugins) with content-
addressed version history, diffs, and restore. Provider-agnostic via the
pattern table in services/artifact_defs.py.

Storage (.c3/agent_artifacts/):
  manifest.json   current inventory AND per-artifact version index — restore
                  depends only on this file plus blobs, never on log retention
  history.jsonl   append-only audit log (rotates freely, no tombstones)
  pending.jsonl   fast capture signals from hooks / C3's own writers
  blobs/<sha256>.gz  gzip of raw file bytes, deduped by write-if-absent

Concurrency model mirrors services/task_store.py: no in-memory cache,
load -> mutate -> atomic save under one threading.Lock. No git subprocesses
anywhere — content hashes, not commits, are identity.
"""
from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from services.artifact_defs import (
    ArtifactUnit,
    classify_path,
    discover_units,
    _norm,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_VERSIONS = 20
MAX_MEMBER_BYTES = 512_000  # larger members: hash-tracked, not blobbed
_C3_BLOCK_MARK = "C3:BEGIN"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _unit_hash(members: list) -> str:
    lines = sorted(f"{m['path']}\n{m['sha256']}" for m in members)
    return _sha256("\n".join(lines).encode("utf-8"))


class ArtifactStore:
    """Inventory + history + diff + restore for agent-affecting artifacts."""

    def __init__(self, project_path: str, ide_name: str = "claude-code"):
        self.project_path = Path(project_path)
        self.ide_name = ide_name
        self.data_dir = self.project_path / ".c3" / "agent_artifacts"
        self.manifest_file = self.data_dir / "manifest.json"
        self.history_file = self.data_dir / "history.jsonl"
        self.pending_file = self.data_dir / "pending.jsonl"
        self.blob_dir = self.data_dir / "blobs"
        self._lock = threading.Lock()
        self._seq = 0

    # ── Persistence ────────────────────────────────────────────────

    def _empty_manifest(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "updated_at": "",
                "last_scan": "", "artifacts": {}}

    def _load_manifest(self) -> dict:
        if not self.manifest_file.exists():
            return self._empty_manifest()
        try:
            doc = json.loads(self.manifest_file.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("artifacts"), dict):
                raise ValueError("malformed manifest.json")
            doc.setdefault("schema_version", SCHEMA_VERSION)
            return doc
        except Exception:
            # Corrupt file: preserve for inspection, start empty (artifacts
            # re-baseline on next scan; blobs stay valid on disk).
            for n in range(1, 100):
                target = self.manifest_file.with_name(f"manifest.json.corrupt-{n}")
                if not target.exists():
                    try:
                        os.replace(self.manifest_file, target)
                    except OSError:
                        pass
                    break
            return self._empty_manifest()

    def _save_manifest(self, doc: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        doc["updated_at"] = _now()
        tmp = self.manifest_file.with_name("manifest.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.manifest_file)

    def _write_blob(self, data: bytes) -> str:
        sha = _sha256(data)
        path = self.blob_dir / f"{sha}.gz"
        if not path.exists():
            self.blob_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{sha}.tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return sha

    def _read_blob(self, sha: str) -> Optional[bytes]:
        path = self.blob_dir / f"{sha}.gz"
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rb") as f:
                return f.read()
        except Exception:
            return None

    def _next_event_id(self) -> str:
        self._seq = (self._seq % 999) + 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"art_{stamp}_{self._seq:03d}_{uuid.uuid4().hex[:4]}"

    def _append_history(self, event: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── Scanning ───────────────────────────────────────────────────

    def _snapshot_unit(self, unit: ArtifactUnit) -> tuple:
        """Hash + blob every member of a discovered unit.

        Returns (manifest_members, version_members). Oversized members are
        hash-tracked with blob=None (unrestorable); unreadable files skipped.
        """
        man_members, ver_members = [], []
        for rel in unit.members:
            fp = self.project_path / rel
            try:
                stat = fp.stat()
                data = fp.read_bytes()
            except OSError:
                continue
            sha = _sha256(data)
            binary = _is_binary(data)
            blob = None
            if len(data) <= MAX_MEMBER_BYTES:
                blob = self._write_blob(data)
            man_members.append({"path": rel, "sha256": sha, "size": len(data),
                                "mtime": stat.st_mtime, "binary": binary})
            ver_members.append({"path": rel, "blob": blob, "size": len(data),
                                "binary": binary})
        return man_members, ver_members

    def _emit(self, manifest: dict, unit: ArtifactUnit, event_type: str,
              attribution: dict, ver_members: list, man_members: list) -> dict:
        entry = manifest["artifacts"].get(unit.id)
        now = _now()
        prev_version = entry["current_version"] if entry else 0
        prev_hash = entry["unit_hash"] if entry else ""
        new_hash = _unit_hash(man_members) if man_members else ""
        version = prev_version + 1 if event_type != "deleted" else prev_version

        event = {
            "id": self._next_event_id(),
            "ts": now,
            "artifact_id": unit.id,
            "class": unit.cls,
            "provider": unit.provider,
            "event": event_type,
            "source": attribution.get("source", "scan"),
            "session_id": attribution.get("session_id", ""),
            "summary": attribution.get("summary", ""),
            "version": version,
            "prev_version": prev_version,
            "unit_hash": new_hash,
            "prev_unit_hash": prev_hash,
            "changed": self._changed_members(entry, man_members, ver_members),
        }
        if attribution.get("restored_from") is not None:
            event["restored_from"] = attribution["restored_from"]

        if event_type == "deleted":
            entry["exists"] = False
            entry["last_changed"] = now
            entry["members"] = []
        else:
            if entry is None:
                entry = {"id": unit.id, "class": unit.cls, "provider": unit.provider,
                         "root": unit.root, "scope": "project",
                         "roles": list(unit.roles), "first_seen": now,
                         "versions": []}
                manifest["artifacts"][unit.id] = entry
            entry.update({"exists": True, "unit_hash": new_hash,
                          "current_version": version, "last_changed": now,
                          "members": man_members})
            entry["versions"].append({
                "v": version, "ts": now, "unit_hash": new_hash,
                "event_id": event["id"], "source": event["source"],
                "members": ver_members,
            })
            entry["versions"] = entry["versions"][-DEFAULT_MAX_VERSIONS:]

        self._append_history(event)
        return event

    @staticmethod
    def _changed_members(entry: Optional[dict], man_members: list,
                         ver_members: list) -> list:
        old = {m["path"]: m["sha256"] for m in (entry or {}).get("members", [])}
        blobs = {m["path"]: m.get("blob") for m in ver_members}
        changed = []
        for m in man_members:
            if m["path"] not in old:
                changed.append({"path": m["path"], "change": "added",
                                "blob": blobs.get(m["path"])})
            elif old[m["path"]] != m["sha256"]:
                changed.append({"path": m["path"], "change": "modified",
                                "blob": blobs.get(m["path"])})
        new_paths = {m["path"] for m in man_members}
        for path in old:
            if path not in new_paths:
                changed.append({"path": path, "change": "removed", "blob": None})
        return changed

    def scan(self, paths: Optional[list] = None,
             attribution: Optional[dict] = None) -> dict:
        """Compare disk state vs manifest; emit history events for changes.

        Idempotent by unit_hash comparison — rescanning an unchanged tree
        emits nothing. `paths` limits the scan to artifacts touching those
        project-relative paths (targeted rescan from pending signals).
        """
        attribution = attribution or {}
        with self._lock:
            return self._scan_locked(paths, attribution)

    def _scan_locked(self, paths: Optional[list], attribution: dict) -> dict:
        manifest = self._load_manifest()
        units = discover_units(self.project_path)

        if paths is not None:
            wanted = set()
            for p in paths:
                ref = classify_path(_norm(p))
                if ref is not None:
                    wanted.add(ref.id)
            units = [u for u in units if u.id in wanted]
            considered = wanted
        else:
            considered = None  # everything

        added, modified, deleted, events = [], [], [], []
        unchanged = 0
        seen_ids = set()

        for unit in units:
            seen_ids.add(unit.id)
            man_members, ver_members = self._snapshot_unit(unit)
            if not man_members:
                continue
            new_hash = _unit_hash(man_members)
            entry = manifest["artifacts"].get(unit.id)
            if entry is None or not entry.get("exists", True):
                events.append(self._emit(manifest, unit, "created",
                                         attribution, ver_members, man_members))
                added.append(unit.id)
            elif entry.get("unit_hash") != new_hash:
                events.append(self._emit(manifest, unit, "modified",
                                         attribution, ver_members, man_members))
                modified.append(unit.id)
            else:
                unchanged += 1

        for aid, entry in manifest["artifacts"].items():
            if not entry.get("exists", True) or aid in seen_ids:
                continue
            if considered is not None and aid not in considered:
                continue
            ghost = ArtifactUnit(id=aid, cls=entry["class"], name=aid.split(":", 1)[1],
                                 provider=entry["provider"], root=entry["root"],
                                 roles=tuple(entry.get("roles", [])), members=[])
            events.append(self._emit(manifest, ghost, "deleted",
                                     attribution, [], []))
            deleted.append(aid)

        if paths is None:
            manifest["last_scan"] = _now()
        if events or paths is None:
            self._save_manifest(manifest)
        return {"added": added, "modified": modified, "deleted": deleted,
                "unchanged": unchanged, "events": events}

    def consume_pending(self) -> dict:
        """Process hook/self-report signals: targeted rescan per touched path
        with the signal's attribution. Signals whose artifact is unchanged
        (already captured by an interleaved scan) drop silently."""
        with self._lock:
            if not self.pending_file.exists():
                return {"consumed": 0, "events": []}
            try:
                raw = self.pending_file.read_text(encoding="utf-8")
                self.pending_file.unlink()
            except OSError:
                return {"consumed": 0, "events": []}

            by_path: dict[str, dict] = {}
            count = 0
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    sig = json.loads(line)
                except Exception:
                    continue
                count += 1
                by_path[sig.get("path", "")] = sig  # last signal wins

            events = []
            for path, sig in by_path.items():
                if not path:
                    continue
                attribution = {"source": sig.get("source", "hook"),
                               "session_id": sig.get("session_id", ""),
                               "summary": sig.get("summary", "")}
                events.extend(self._scan_locked([path], attribution)["events"])
            return {"consumed": count, "events": events}

    def note_write(self, rel_path: str, source: str, session_id: str = "",
                   summary: str = "") -> None:
        """Synchronous in-process capture (the c3_edit path). Best-effort."""
        try:
            self.scan(paths=[rel_path],
                      attribution={"source": source, "session_id": session_id,
                                   "summary": summary})
        except Exception:
            pass

    # ── Queries ────────────────────────────────────────────────────

    def resolve(self, ref: str) -> Optional[dict]:
        """Artifact by exact id, unique id prefix, or path."""
        ref = (ref or "").strip()
        if not ref:
            return None
        manifest = self._load_manifest()
        artifacts = manifest["artifacts"]
        if ref in artifacts:
            return artifacts[ref]
        cp = classify_path(ref)
        if cp is not None and cp.id in artifacts:
            return artifacts[cp.id]
        matches = [a for aid, a in artifacts.items() if aid.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        return None

    def list_artifacts(self, cls: str = "", provider: str = "") -> list:
        manifest = self._load_manifest()
        out = []
        for aid in sorted(manifest["artifacts"]):
            a = manifest["artifacts"][aid]
            if cls and a.get("class") != cls:
                continue
            if provider and a.get("provider") != provider:
                continue
            out.append({
                "id": aid, "class": a.get("class"), "provider": a.get("provider"),
                "root": a.get("root"), "exists": a.get("exists", True),
                "roles": a.get("roles", []),
                "version": a.get("current_version", 0),
                "unit_hash": (a.get("unit_hash") or "")[:7],
                "last_changed": a.get("last_changed", ""),
                "files": len(a.get("members", [])),
                "versions_kept": len(a.get("versions", [])),
            })
        return out

    def get_history(self, artifact: str = "", limit: int = 50) -> list:
        target_id = ""
        if artifact:
            entry = self.resolve(artifact)
            if entry is None:
                return []
            target_id = entry["id"]
        if not self.history_file.exists():
            return []
        events = []
        try:
            with open(self.history_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if target_id and ev.get("artifact_id") != target_id:
                        continue
                    events.append(ev)
        except OSError:
            return []
        return events[-max(1, int(limit)):][::-1]  # newest first

    def _find_version(self, entry: dict, version: int) -> Optional[dict]:
        for v in entry.get("versions", []):
            if v["v"] == version:
                return v
        return None

    def get_version(self, artifact: str, version: int = 0) -> dict:
        """Content at a version. version=0 -> live files on disk."""
        entry = self.resolve(artifact)
        if entry is None:
            return {"error": f"no artifact matches: {artifact}"}
        members = []
        if version == 0:
            for m in entry.get("members", []):
                fp = self.project_path / m["path"]
                try:
                    data = fp.read_bytes()
                except OSError:
                    continue
                members.append(self._member_view(m["path"], data))
            label = "live"
        else:
            ver = self._find_version(entry, version)
            if ver is None:
                kept = [v["v"] for v in entry.get("versions", [])]
                return {"error": f"version v{version} not kept for {entry['id']} "
                                 f"(kept: {kept})"}
            for m in ver["members"]:
                data = self._read_blob(m["blob"]) if m.get("blob") else None
                members.append(self._member_view(m["path"], data,
                                                 size=m.get("size", 0),
                                                 binary=m.get("binary", False)))
            label = f"v{version}"
        return {"id": entry["id"], "version": label, "members": members}

    @staticmethod
    def _member_view(path: str, data: Optional[bytes], size: int = 0,
                     binary: bool = False) -> dict:
        if data is None:
            return {"path": path, "size": size, "binary": binary, "text": None}
        binary = _is_binary(data)
        return {"path": path, "size": len(data), "binary": binary,
                "text": None if binary else data.decode("utf-8", errors="replace")}

    def diff(self, artifact: str, v_from: int, v_to: Optional[int] = None) -> dict:
        """Unified diff between two versions (v_to None -> live files)."""
        entry = self.resolve(artifact)
        if entry is None:
            return {"error": f"no artifact matches: {artifact}"}
        old = self.get_version(entry["id"], v_from)
        if "error" in old:
            return old
        new = self.get_version(entry["id"], v_to if v_to is not None else 0)
        if "error" in new:
            return new
        old_by = {m["path"]: m for m in old["members"]}
        new_by = {m["path"]: m for m in new["members"]}
        chunks, plus, minus = [], 0, 0
        for path in sorted(set(old_by) | set(new_by)):
            om, nm = old_by.get(path), new_by.get(path)
            if om and nm and om.get("text") == nm.get("text") \
                    and om.get("text") is not None:
                continue
            if (om and om.get("binary")) or (nm and nm.get("binary")):
                chunks.append(f"--- {path} ---\n[binary member — no text diff]")
                continue
            if (om and om.get("text") is None) or (nm and nm.get("text") is None):
                chunks.append(f"--- {path} ---\n[member too large at capture — no diff]")
                continue
            a = (om["text"] if om else "").splitlines(keepends=True)
            b = (nm["text"] if nm else "").splitlines(keepends=True)
            lines = list(difflib.unified_diff(
                a, b, fromfile=f"{path}@{old['version']}",
                tofile=f"{path}@{new['version']}", n=3))
            if lines:
                plus += sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
                minus += sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
                chunks.append("".join(lines).rstrip("\n"))
        return {"id": entry["id"], "from": old["version"], "to": new["version"],
                "plus": plus, "minus": minus,
                "diff": "\n".join(chunks) if chunks else "(no differences)"}

    # ── Restore ────────────────────────────────────────────────────

    def restore(self, artifact: str, version: int, session_id: str = "") -> dict:
        """Write a previous version's bytes back to disk.

        Forward-only: appends a NEW manifest version + history event
        (event=restored); history is never rewritten. Removes currently
        tracked members absent from the target version — never touches
        untracked strays.
        """
        with self._lock:
            manifest = self._load_manifest()
            entry = None
            probe = self.resolve(artifact)
            if probe is not None:
                entry = manifest["artifacts"].get(probe["id"])
            if entry is None:
                return {"error": f"no artifact matches: {artifact}"}
            ver = self._find_version(entry, version)
            if ver is None:
                kept = [v["v"] for v in entry.get("versions", [])]
                return {"error": f"version v{version} not kept for {entry['id']} "
                                 f"(kept: {kept})"}

            warnings, written, removed = [], [], []
            target_paths = {m["path"] for m in ver["members"]}

            for m in ver["members"]:
                if not m.get("blob"):
                    warnings.append(f"{m['path']}: not restorable "
                                    f"(exceeded {MAX_MEMBER_BYTES}B at capture)")
                    continue
                data = self._read_blob(m["blob"])
                if data is None:
                    warnings.append(f"{m['path']}: blob missing — not restored")
                    continue
                fp = self.project_path / m["path"]
                fp.parent.mkdir(parents=True, exist_ok=True)
                tmp = fp.with_name(fp.name + ".c3tmp")
                tmp.write_bytes(data)
                os.replace(tmp, fp)
                written.append(m["path"])
                if not m.get("binary") and _C3_BLOCK_MARK in data.decode(
                        "utf-8", errors="replace"):
                    warnings.append(
                        f"{m['path']}: contains a C3-managed block — the block "
                        "is regenerated on the next install-mcp / claudemd save")

            for m in entry.get("members", []):
                if m["path"] in target_paths:
                    continue
                fp = self.project_path / m["path"]
                try:
                    fp.unlink()
                    removed.append(m["path"])
                except OSError:
                    warnings.append(f"{m['path']}: could not remove")

            if entry.get("class") == "settings" or "settings" in entry.get("roles", []):
                warnings.append(
                    "settings/hooks load at session start — a live agent "
                    "session keeps its current hooks until restarted")

            unit = ArtifactUnit(id=entry["id"], cls=entry["class"],
                                name=entry["id"].split(":", 1)[1],
                                provider=entry["provider"], root=entry["root"],
                                roles=tuple(entry.get("roles", [])),
                                members=sorted(target_paths))
            man_members, ver_members = self._snapshot_unit(unit)
            event = self._emit(manifest, unit, "restored",
                               {"source": "restore", "session_id": session_id,
                                "summary": f"restored to v{version}",
                                "restored_from": version},
                               ver_members, man_members)
            self._save_manifest(manifest)
            return {"restored": True, "id": entry["id"], "from_version": version,
                    "new_version": event["version"], "files_written": written,
                    "files_removed": removed, "warnings": warnings}

    # ── Health / retention ─────────────────────────────────────────

    def status(self) -> dict:
        manifest = self._load_manifest()
        by_class: dict[str, int] = {}
        missing = 0
        for a in manifest["artifacts"].values():
            by_class[a.get("class", "?")] = by_class.get(a.get("class", "?"), 0) + 1
            if not a.get("exists", True):
                missing += 1
        recent = self.get_history(limit=200)
        out_of_band = sum(1 for e in recent
                          if e.get("source") == "scan" and e.get("event") != "created")
        pending = 0
        try:
            if self.pending_file.exists():
                pending = sum(1 for l in
                              self.pending_file.read_text(encoding="utf-8").splitlines()
                              if l.strip())
        except OSError:
            pass
        return {"tracked": len(manifest["artifacts"]), "by_class": by_class,
                "missing": missing, "last_scan": manifest.get("last_scan", ""),
                "pending_signals": pending, "out_of_band_recent": out_of_band}

    def prune(self, max_versions: int = DEFAULT_MAX_VERSIONS,
              blob_orphan_days: int = 7) -> dict:
        """Cap version lists and GC orphan blobs (age-guarded: other processes
        — hub server, MCP server — may hold un-saved manifests referencing a
        fresh blob, so only blobs older than the guard are deleted)."""
        with self._lock:
            manifest = self._load_manifest()
            trimmed = 0
            referenced = set()
            for a in manifest["artifacts"].values():
                versions = a.get("versions", [])
                if len(versions) > max_versions:
                    trimmed += len(versions) - max_versions
                    a["versions"] = versions[-max_versions:]
                for v in a["versions"]:
                    for m in v["members"]:
                        if m.get("blob"):
                            referenced.add(m["blob"])
            if trimmed:
                self._save_manifest(manifest)

            deleted = 0
            cutoff = time.time() - blob_orphan_days * 86400
            if self.blob_dir.is_dir():
                for bf in self.blob_dir.glob("*.gz"):
                    if bf.stem in referenced:
                        continue
                    try:
                        if bf.stat().st_mtime < cutoff:
                            bf.unlink()
                            deleted += 1
                    except OSError:
                        pass
            return {"versions_trimmed": trimmed, "blobs_deleted": deleted}
