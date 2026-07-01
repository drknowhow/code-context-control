"""EditLedger — persistent, git-integrated audit trail of AI edits.

Storage: .c3/edit_ledger.jsonl (append-only)

Performance: in-memory caches for version map and seq counter avoid
repeated full-file scans.  Git info is captured via a single combined
command instead of 3 separate subprocesses.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.git_context import GitContext

# Marker key on version-tombstone lines written by rotate_if_needed().
# Tombstones carry {"file", "version"} so version numbering survives
# archival of a file's entries (both this module's cache and the
# hook's ledger scan pick them up); every other reader skips them.
ROTATION_KEY = "_c3_rotation"


def _is_rotation_marker(entry: dict) -> bool:
    """True for rotation/version-tombstone lines (not real ledger records)."""
    return ROTATION_KEY in entry


class EditLedger:
    """Tracks every AI edit with version numbering and git context."""

    def __init__(self, project_path: str):
        self.project_path = Path(project_path).resolve()
        self.ledger_file = self.project_path / ".c3" / "edit_ledger.jsonl"
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self._git = GitContext(self.project_path)
        self._git_root = self._git.git_root
        # In-memory caches — loaded lazily on first use, updated on writes
        self._version_cache: dict[str, int] | None = None  # {file: max_version}
        self._total_count: int | None = None
        self._seq_counter: int = 0  # monotonic within process lifetime
        self._write_lock = threading.Lock()

    # ── Cache management ──────────────────────────────────────────────

    def _ensure_cache(self):
        """Load version cache from ledger file (once per process)."""
        if self._version_cache is not None:
            return
        self._version_cache = {}
        self._total_count = 0
        if not self.ledger_file.exists():
            return
        for line in self.ledger_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if _is_rotation_marker(entry):
                # Version tombstone: max version of a file whose entries
                # were archived — merge so numbering continues from there.
                f = entry.get("file", "")
                try:
                    v_num = int(str(entry.get("version", "v0")).lstrip("v"))
                    if f:
                        self._version_cache[f] = max(self._version_cache.get(f, 0), v_num)
                except (ValueError, AttributeError):
                    pass
                continue
            if "target_id" in entry:  # skip patch entries
                continue
            self._total_count += 1
            f = entry.get("file", "")
            v_str = entry.get("version", "v0")
            try:
                v_num = int(v_str.lstrip("v"))
                self._version_cache[f] = max(self._version_cache.get(f, 0), v_num)
            except (ValueError, AttributeError):
                pass

    # ── Public API ────────────────────────────────────────────────────

    def log_edit(self, file: str, change_type: str, summary: str,
                 lines_changed: list = None, tags: list = None,
                 session_id: str = None, include_git: bool = True,
                 detail: dict = None) -> dict:
        """Record an edit. Returns the entry dict.

        Args:
            include_git: if False, skips git subprocess calls (faster).
        """
        self._ensure_cache()
        rel = file.replace("\\", "/")
        now = datetime.now(timezone.utc)
        self._seq_counter += 1

        # Git info — single combined command when enabled
        git_info = {"commit": "", "author": "", "subject": "", "dirty": False,
                    "branch": None, "head_sha": ""}
        diff_summary = ""
        if include_git and self._git_root:
            git_info, diff_summary = self._git_combined(rel)

        # Version from cache — O(1)
        cur = self._version_cache.get(rel, 0)
        new_v = cur + 1
        self._version_cache[rel] = new_v

        entry = {
            "id": f"edit_{now.strftime('%Y%m%d_%H%M%S')}_{self._seq_counter:03d}_{uuid.uuid4().hex[:4]}",
            "timestamp": now.isoformat(),
            "session_id": session_id or "",
            "file": rel,
            "change_type": change_type,
            "summary": summary,
            "lines_changed": lines_changed,
            "version": f"v{new_v}",
            "git": git_info,
            "diff_summary": diff_summary,
            "tags": tags or [],
        }
        if detail:
            entry["detail"] = detail
        with self._write_lock:
            with open(self.ledger_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        if self._total_count is not None:
            self._total_count += 1
        return entry

    def get_history(self, file: str = None, limit: int = 50,
                    since: str = None, branch: str = None) -> list:
        """Query edits, optionally filtered by file, time, and/or branch."""
        results = self._load_merged(file_filter=file, since_filter=since,
                                    branch_filter=branch)
        return results[-limit:]

    def get_file_versions(self, file: str) -> list:
        """All version entries for a specific file."""
        return self.get_history(file=file, limit=10000)

    def get_stats(self) -> dict:
        """Summary: total edits, files edited, by change_type."""
        if not self.ledger_file.exists():
            return {"total": 0, "by_type": {}, "files": 0, "most_edited": []}
        type_counts = Counter()
        file_counts = Counter()
        total = 0
        with open(self.ledger_file, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "target_id" in entry or _is_rotation_marker(entry):
                    continue  # skip patch entries and rotation tombstones
                total += 1
                type_counts[entry.get("change_type", "unknown")] += 1
                file_counts[entry.get("file", "unknown")] += 1
        return {
            "total": total,
            "by_type": dict(type_counts),
            "files": len(file_counts),
            "most_edited": [
                {"file": f, "count": c}
                for f, c in file_counts.most_common(10)
            ],
        }

    def get_version(self, file: str) -> str:
        """Current version string for a file (e.g. 'v3')."""
        self._ensure_cache()
        rel = file.replace("\\", "/")
        v = self._version_cache.get(rel, 0)
        return f"v{v}" if v > 0 else "v0"

    def tag_edit(self, edit_id: str, tag: str) -> bool:
        """Add a tag to an existing edit by appending a patch entry.

        Append-only: rather than rewriting the whole file (which races with
        concurrent appends from the hook process and enrich/validate), this
        writes a ``{"target_id": id, "tags_add": [tag]}`` patch that
        ``_load_merged`` applies. Returns True if the target edit exists.
        """
        if not self.ledger_file.exists():
            return False
        # Confirm the target base entry exists before appending a patch.
        found = False
        try:
            for line in self.ledger_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if "target_id" not in entry and entry.get("id") == edit_id:
                    found = True
                    break
        except Exception:
            return False
        if not found:
            return False
        patch = {
            "target_id": edit_id,
            "tags_add": [tag],
            "tagged_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._write_lock:
            try:
                with open(self.ledger_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(patch) + "\n")
            except Exception:
                return False
        return True

    # ── Retention (P5) ────────────────────────────────────────────────

    def rotate_if_needed(self, max_bytes: int, archive_dir,
                         keep_days: int = 14) -> dict | None:
        """Archive old, fully-enriched entries once the ledger exceeds ``max_bytes``.

        Audit integrity: the ledger is NEVER truncated silently. Eligible
        records (base entries older than ``keep_days`` that are not awaiting
        the enricher, plus every patch targeting them) are written to a gzip
        archive in ``archive_dir`` FIRST; only after the archive exists is
        the live file rewritten without them. Entries still carrying
        ``git_pending`` with no git patch are always retained, so the
        EditLedgerEnricherAgent's pending-patch flow is unaffected.

        Version continuity: for any file whose entries were all archived, a
        tombstone line ``{"_c3_rotation": 1, "file": ..., "version": "vN"}``
        is kept at the top of the live file. Both ``_ensure_cache`` and the
        PostToolUse hook's ledger scan read ``file``/``version`` pairs, so
        the next edit becomes vN+1 instead of restarting at v1.

        Concurrency: runs under the write lock (in-process appends are
        serialized); hook processes append-open per write, and any bytes a
        hook appends while we rebuild are carried over verbatim before the
        atomic ``os.replace``. If the final rewrite fails, the just-written
        archive is removed again so no records are ever duplicated or lost.

        Returns a summary dict, or None when no rotation happened. Never raises.
        """
        try:
            if max_bytes <= 0 or not self.ledger_file.exists():
                return None
            if self.ledger_file.stat().st_size < max_bytes:
                return None
        except OSError:
            return None
        try:
            from services.retention import write_archive_lines
        except Exception:
            return None

        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(0, keep_days))).isoformat()
        with self._write_lock:
            try:
                raw = self.ledger_file.read_bytes()
            except OSError:
                return None
            read_size = len(raw)
            parsed = []  # (line, entry-or-None)
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        entry = None
                except (json.JSONDecodeError, ValueError):
                    entry = None
                parsed.append((line, entry))

            # Pass 1: classify — which base ids exist, which have git patches.
            bases: dict = {}
            git_patched: set = set()
            for _, entry in parsed:
                if entry is None or _is_rotation_marker(entry):
                    continue
                if "target_id" in entry:
                    if "git" in entry:
                        git_patched.add(entry["target_id"])
                    continue
                eid = entry.get("id")
                if eid:
                    bases[eid] = entry

            def _is_pending(e: dict) -> bool:
                return bool(e.get("git_pending")) and e.get("id") not in git_patched

            archive_ids = {
                eid for eid, e in bases.items()
                if e.get("timestamp", "") and e["timestamp"] < cutoff
                and not _is_pending(e)
            }
            if not archive_ids:
                return None

            # Pass 2: split lines, track per-file max versions + live files.
            archived_lines: list = []
            retained_lines: list = []
            versions: dict = {}      # file → max version int (all bases + old tombstones)
            retained_files: set = set()

            def _bump(fname, v_str):
                try:
                    v = int(str(v_str or "v0").lstrip("v"))
                except (ValueError, AttributeError):
                    return
                if fname:
                    versions[fname] = max(versions.get(fname, 0), v)

            for line, entry in parsed:
                if entry is None:
                    retained_lines.append(line)   # unparseable — keep, conservative
                    continue
                if _is_rotation_marker(entry):
                    # Old tombstone: fold its version into the new set, then archive it.
                    _bump(entry.get("file", ""), entry.get("version"))
                    archived_lines.append(line)
                    continue
                if "target_id" in entry:
                    tid = entry["target_id"]
                    if tid in archive_ids:
                        archived_lines.append(line)
                    elif tid in bases:
                        retained_lines.append(line)
                    else:
                        # Orphaned patch (target already archived / missing).
                        pts = (entry.get("enriched_at") or entry.get("validated_at")
                               or entry.get("tagged_at") or "")
                        if pts and pts < cutoff:
                            archived_lines.append(line)
                        else:
                            retained_lines.append(line)
                    continue
                fname = entry.get("file", "")
                _bump(fname, entry.get("version"))
                if entry.get("id") in archive_ids:
                    archived_lines.append(line)
                else:
                    retained_lines.append(line)
                    if fname:
                        retained_files.add(fname)

            # Archive FIRST — records exist in two places before any removal.
            gz = write_archive_lines(archive_dir, "edit_ledger", archived_lines)
            if gz is None:
                return None

            now_iso = datetime.now(timezone.utc).isoformat()
            tombstones = [
                json.dumps({ROTATION_KEY: 1, "file": fname, "version": f"v{v}",
                            "timestamp": now_iso, "archived_to": gz.name})
                for fname, v in sorted(versions.items())
                if fname not in retained_files
            ]

            tmp = self.ledger_file.with_name(
                f"{self.ledger_file.name}.tmp.{os.getpid()}")
            try:
                content = "\n".join(tombstones + retained_lines)
                if content:
                    content += "\n"
                # Cross-process append guard: carry over anything a hook
                # appended past our original read offset while we worked.
                try:
                    with open(self.ledger_file, "rb") as fh:
                        fh.seek(read_size)
                        extra = fh.read().decode("utf-8", errors="replace")
                    if extra.strip():
                        content += extra if extra.endswith("\n") else extra + "\n"
                except OSError:
                    pass
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, self.ledger_file)
            except Exception:
                # Rewrite failed: live file untouched — remove the archive so
                # the next attempt can't duplicate records.
                for p in (gz, tmp):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                return None

            # Versions are unchanged (tombstones preserve maxima); only the
            # live base-entry count shrank.
            if self._total_count is not None:
                self._total_count = max(0, self._total_count - len(archive_ids))
            return {
                "archived_entries": len(archive_ids),
                "archived_lines": len(archived_lines),
                "tombstones": len(tombstones),
                "archive": str(gz),
            }

    # ── Private helpers ───────────────────────────────────────────────

    def _tail_entries(self, limit: int) -> list:
        """Read the last `limit` entries from the ledger efficiently."""
        try:
            raw = self.ledger_file.read_bytes()
        except Exception:
            return []
        # Scan backwards for enough newlines
        lines_found = []
        pos = len(raw)
        while pos > 0 and len(lines_found) < limit + 5:  # extra buffer for blank lines
            nl = raw.rfind(b"\n", 0, pos - 1)
            chunk = raw[nl + 1:pos]
            if chunk.strip():
                lines_found.append(chunk)
            pos = nl if nl >= 0 else 0
        # Parse in forward order
        results = []
        for raw_line in reversed(lines_found):
            try:
                results.append(json.loads(raw_line))
            except (json.JSONDecodeError, ValueError):
                continue
        return results[-limit:]

    def _git_combined(self, rel_path: str) -> tuple:
        """Capture git info + diff in a single subprocess call.

        Returns (git_info_dict, diff_summary_str).
        """
        info = {"commit": "", "author": "", "subject": "", "dirty": False,
                "branch": None, "head_sha": ""}
        diff_summary = ""
        abs_path = (self.project_path / rel_path).resolve()
        try:
            git_rel = str(abs_path.relative_to(self._git_root))
        except Exception:
            return info, diff_summary

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # Single shell command: status + log + diff
        # Using && chaining so we get all info in one subprocess
        sep = "---C3SEP---"
        cmd = (
            f'git status --porcelain -- "{git_rel}" && '
            f'echo {sep} && '
            f'git log -1 --format="%H%x1f%an%x1f%s" -- "{git_rel}" && '
            f'echo {sep} && '
            f'git diff --numstat -- "{git_rel}"'
        )
        try:
            proc = subprocess.Popen(
                cmd, shell=True,
                cwd=self._git_root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True, **kwargs,
            )
            try:
                stdout, _ = proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, **kwargs,
                    )
                else:
                    proc.kill()
                proc.communicate()
                return info, diff_summary
            parts = (stdout or "").split(sep)
            # Part 0: status --porcelain output
            if len(parts) > 0:
                info["dirty"] = bool(parts[0].strip())
            # Part 1: log output
            if len(parts) > 1:
                log_line = parts[1].strip()
                fields = log_line.split("\x1f")
                if len(fields) == 3:
                    info["commit"] = fields[0][:12]
                    info["author"] = fields[1]
                    info["subject"] = fields[2]
            # Part 2: diff --numstat output
            if len(parts) > 2:
                diff_line = parts[2].strip()
                if diff_line:
                    nums = diff_line.split("\t")
                    if len(nums) >= 2:
                        diff_summary = f"+{nums[0]} -{nums[1]}"
        except Exception:
            pass

        # Branch + HEAD from the cached GitContext (cheap; shared TTL cache).
        try:
            gstate = self._git.state()
            info["branch"] = gstate.get("branch")
            info["head_sha"] = gstate.get("head_sha", "")
        except Exception:
            pass

        return info, diff_summary

    # ── Async enrichment ──────────────────────────────────────────────

    def _load_merged(self, file_filter: str = None, since_filter: str = None,
                     branch_filter: str = None) -> list:
        """Read all base entries with any appended patches merged in.

        Patch entries are identified by having a 'target_id' field.
        Returns entries sorted by timestamp ascending.
        """
        if not self.ledger_file.exists():
            return []
        base: dict = {}    # id → entry dict
        patches: dict = {}  # target_id → list of patch dicts
        try:
            for line in self.ledger_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if _is_rotation_marker(entry):
                    continue  # version tombstones are not history entries
                if "target_id" in entry:
                    patches.setdefault(entry["target_id"], []).append(entry)
                else:
                    eid = entry.get("id")
                    if eid:
                        base[eid] = entry
        except Exception:
            return []

        # Apply patches — each patch carries git enrichment, validation, and/or tags
        orphaned: list = []
        for target_id, patch_list in patches.items():
            if target_id not in base:
                orphaned.append(target_id)
                continue
            for patch in patch_list:
                if "git" in patch:
                    base[target_id]["git"] = patch["git"]
                    base[target_id]["diff_summary"] = patch.get("diff_summary", "")
                    base[target_id].pop("git_pending", None)
                if "valid" in patch:
                    base[target_id]["valid"] = patch["valid"]
                    if patch.get("errors"):
                        base[target_id]["lint_errors"] = patch["errors"]
                if patch.get("tags_add"):
                    tags = base[target_id].get("tags") or []
                    for t in patch["tags_add"]:
                        if t not in tags:
                            tags.append(t)
                    base[target_id]["tags"] = tags
        if orphaned:
            # Best-effort diagnostics — never let logging break the read path.
            try:
                print(
                    f"[edit_ledger] {len(orphaned)} orphaned patch target_id(s) "
                    f"with no base entry: {orphaned[:10]}",
                    file=sys.stderr,
                )
            except Exception:
                pass

        norm_file = file_filter.replace("\\", "/") if file_filter else None
        results = []
        for entry in base.values():
            if norm_file and entry.get("file") != norm_file:
                continue
            if since_filter and entry.get("timestamp", "") < since_filter:
                continue
            if branch_filter and (entry.get("git") or {}).get("branch") != branch_filter:
                continue
            results.append(entry)
        results.sort(key=lambda e: e.get("timestamp", ""))
        return results

    def enrich_pending(self, batch: int = 10) -> int:
        """Find hook-logged entries with git_pending=True and append git patches.

        Called by EditLedgerEnricherAgent on a background timer.
        Returns the number of entries enriched this cycle.
        """
        if not self.ledger_file.exists() or not self._git_root:
            return 0
        pending = []
        already_patched: set = set()
        try:
            for line in self.ledger_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if _is_rotation_marker(entry):
                    continue
                if "target_id" in entry and "git" in entry:
                    already_patched.add(entry["target_id"])
                elif "target_id" not in entry and entry.get("git_pending") and entry.get("file"):
                    pending.append(entry)
        except Exception:
            return 0

        to_enrich = [e for e in pending if e["id"] not in already_patched][:batch]
        if not to_enrich:
            return 0

        patches = []
        for entry in to_enrich:
            git_info, diff_summary = self._git_combined(entry["file"])
            patches.append({
                "target_id": entry["id"],
                "git": git_info,
                "diff_summary": diff_summary,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
            })

        with self._write_lock:
            try:
                with open(self.ledger_file, "a", encoding="utf-8") as f:
                    for patch in patches:
                        f.write(json.dumps(patch) + "\n")
            except Exception:
                return 0
        return len(patches)

    def validate_pending(self, batch: int = 5, validation_cache=None) -> list:
        """Find recently-edited files without validation results and validate them.

        Appends validate patches to the ledger and returns result dicts.
        Called by EditLedgerEnricherAgent on a background timer.
        """
        if not self.ledger_file.exists() or not validation_cache:
            return []
        pending = []
        already_validated: set = set()
        try:
            for line in self.ledger_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if _is_rotation_marker(entry):
                    continue  # tombstones carry "file" but are not edits
                if "target_id" in entry and "valid" in entry:
                    already_validated.add(entry["target_id"])
                elif "target_id" not in entry and entry.get("file"):
                    pending.append(entry)
        except Exception:
            return []

        # Most recent first, skip already validated
        to_validate = [e for e in reversed(pending) if e["id"] not in already_validated][:batch]
        if not to_validate:
            return []

        results = []
        patches = []
        for entry in to_validate:
            try:
                result = validation_cache.validate_file(entry["file"])
            except Exception:
                result = None
            if result is None:
                continue
            patch = {
                "target_id": entry["id"],
                "valid": result.get("valid", True),
                "errors": result.get("errors", []),
                "validated_at": datetime.now(timezone.utc).isoformat(),
            }
            patches.append(patch)
            results.append({"id": entry["id"], "file": entry["file"], **patch})

        if patches:
            with self._write_lock:
                try:
                    with open(self.ledger_file, "a", encoding="utf-8") as f:
                        for patch in patches:
                            f.write(json.dumps(patch) + "\n")
                except Exception:
                    pass
        return results
