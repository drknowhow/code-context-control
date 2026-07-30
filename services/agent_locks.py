"""Agent Locks — leases that stop two agents doing overlapping work.

Implements docs/agent-locks.md Layer B. Distinct from Layer A (the
cross-process mutex inside ``cli/tools/edit.py``, which only stops torn
writes): a *lease* is held across many edits for minutes, carries a declared
intent, and is what makes a second agent back off a file someone is
mid-refactor on.

Like Access Guard, this is **cooperative coordination, not containment**. It
gates C3's own surfaces; a raw shell redirect is out of scope (spec §9).

State lives in the TARGET project's ``.c3/locks.json`` — never the caller's.
``c3_project(action='edit')`` lets an agent in project A mutate project B, and
caller-scoped state would hand the two agents different lock files and no
mutual exclusion at all.

Semantics are FleetDeck's (``fleetdeck/locks.py``), deliberately, so the two
systems name the same file the same way and can share one namespace:

* key ``(repo_id, relpath)`` — sha1 of the canonical root + casefolded relpath
* held by ``(agent_id, session_id)``, stamped with a monotonic fencing token
* acquisition is ALL-OR-NOTHING over a sorted path list, so two agents
  grabbing the same pair in opposite order cannot deadlock
* TTL is the real release mechanism — agents forget to release, so nothing
  here assumes they will; a crashed agent can never wedge a repo
* ``force_release`` bumps the fencing counter, making a returning holder stale
  by construction

Stdlib only, and no daemon: the state is a file guarded by an OS file lock, so
locking keeps working when nothing is running. That is the property that made
C3 rather than FleetDeck the right owner (spec §11).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from services.task_store import _FileLock

SCHEMA_VERSION = 1
DEFAULT_TTL_S = 900.0
MODES = ("advisory", "strict")

TAG_HELD = "[c3-lock:held]"
TAG_UNAVAILABLE = "[c3-lock:unavailable]"

_WSL_MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


class UnsupportedPathError(ValueError):
    """A path form we will not guess at.

    ``reason`` is machine-readable: ``unc``, ``outside_repo``, ``is_root``,
    ``empty``. Guessing would silently break mutual exclusion, which is worse
    than refusing — so callers surface the reason instead.
    """

    def __init__(self, path, reason: str) -> None:
        super().__init__(f"unsupported path ({reason}): {path!r}")
        self.path = str(path)
        self.reason = reason


# ── Path identity (mirrors fleetdeck/paths.py — purely lexical) ─────────────
# No filesystem access on purpose: a file that does not exist yet still
# normalizes, and the answer never depends on cwd or symlinks.

def _wsl_to_windows(path: str):
    m = _WSL_MNT_RE.match(path.strip())
    if m is None:
        return None
    return f"{m.group(1)}:/{m.group(2) or ''}"


def _lexical_norm(win_path: str) -> str:
    norm = os.path.normpath(win_path).replace("\\", "/").casefold()
    if _DRIVE_RE.match(norm) and len(norm) == 2:
        norm += "/"
    return norm


def canonical_root(root) -> str:
    raw = str(root).strip()
    if not raw:
        raise UnsupportedPathError(root, "empty")
    fwd = raw.replace("\\", "/")
    if fwd.startswith("//"):
        raise UnsupportedPathError(root, "unc")
    win = _wsl_to_windows(fwd)
    if win is None:
        win = fwd if _DRIVE_RE.match(fwd) else os.path.abspath(raw).replace("\\", "/")
    norm = _lexical_norm(win)
    return norm if norm.endswith(":/") else norm.rstrip("/")


def repo_id(root) -> str:
    return hashlib.sha1(canonical_root(root).encode("utf-8")).hexdigest()


def normalize_relpath(filepath, root) -> str:
    """Casefolded, forward-slash, repo-relative key for ``filepath``."""
    croot = canonical_root(root)
    raw = str(filepath).strip()
    if not raw:
        raise UnsupportedPathError(filepath, "empty")
    fwd = raw.replace("\\", "/")
    if fwd.startswith("//"):
        raise UnsupportedPathError(filepath, "unc")

    win = _wsl_to_windows(fwd)
    if win is None and _DRIVE_RE.match(fwd):
        win = fwd
    if win is not None:
        cand = _lexical_norm(win)
        prefix = croot if croot.endswith("/") else croot + "/"
        if cand == croot:
            raise UnsupportedPathError(filepath, "is_root")
        if not cand.startswith(prefix):
            raise UnsupportedPathError(filepath, "outside_repo")
        return cand[len(prefix):]

    rel = _lexical_norm(fwd.lstrip("/"))
    if rel in (".", ""):
        raise UnsupportedPathError(filepath, "is_root")
    if rel == ".." or rel.startswith("../"):
        raise UnsupportedPathError(filepath, "outside_repo")
    return rel


def agent_id_for(session_id: str) -> str:
    """FleetDeck's convention (fleetdeck/hook.py) so both name one agent alike."""
    sid = (session_id or "").strip()
    return f"claude-code:{sid[:8]}" if sid else "claude-code"


# ── Engine ──────────────────────────────────────────────────────────────────

class LockStore:
    """Leases for one project. Every mutation is load → mutate → atomic save
    under an in-process lock AND a cross-process file lock, the same guard
    ``task_store`` uses."""

    def __init__(self, project_path, ttl_s: float = DEFAULT_TTL_S,
                 clock=time.time):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / ".c3"
        self.state_file = self.data_dir / "locks.json"
        self.lock_file = self.data_dir / "locks.lock"
        self._ttl = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._repo_id = None

    # -- persistence -------------------------------------------------------

    def _empty(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "fencing": 0, "locks": []}

    def _load(self) -> dict:
        if not self.state_file.is_file():
            return self._empty()
        try:
            doc = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt state is NOT silently reset to empty: that would drop
            # live leases and let two agents into one file. Quarantine it and
            # let mode decide (strict callers fail closed on `corrupt`).
            try:
                self.state_file.replace(
                    self.state_file.with_suffix(".json.corrupt"))
            except Exception:
                pass
            doc = self._empty()
            doc["recovered"] = True
        if not isinstance(doc, dict) or not isinstance(doc.get("locks"), list):
            return self._empty()
        return doc

    def _save(self, doc: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _guard(self):
        return _FileLock(self.lock_file)

    def rid(self) -> str:
        if self._repo_id is None:
            self._repo_id = repo_id(self.project_path)
        return self._repo_id

    # -- internals ---------------------------------------------------------

    def _prune(self, doc: dict, now: float) -> list:
        live, expired = [], []
        for row in doc["locks"]:
            (live if row.get("expires_at", 0) > now else expired).append(row)
        doc["locks"] = live
        return expired

    def _rel(self, path) -> str:
        return normalize_relpath(path, self.project_path)

    # -- reads -------------------------------------------------------------

    def snapshot(self) -> dict:
        """Live leases. Read-only: never prunes, so a reader cannot mutate."""
        now = self._clock()
        doc = self._load()
        live = [dict(r) for r in doc["locks"] if r.get("expires_at", 0) > now]
        live.sort(key=lambda r: r.get("relpath", ""))
        for row in live:
            row["expires_in_s"] = round(row["expires_at"] - now, 1)
        return {"repo_id": self.rid(), "root": str(self.project_path),
                "fencing": doc.get("fencing", 0), "locks": live,
                "count": len(live)}

    def holder_of(self, path, session_id: str = ""):
        """The row blocking ``session_id`` from ``path``, or None.

        None also when this session already holds it — re-entrancy matters
        because one agent edits the same file many times in a row.
        """
        try:
            rel = self._rel(path)
        except UnsupportedPathError:
            return None
        now = self._clock()
        for row in self._load()["locks"]:
            if (row.get("relpath") == rel
                    and row.get("expires_at", 0) > now
                    and row.get("session_id") != session_id):
                return dict(row)
        return None

    # -- mutations ---------------------------------------------------------

    def acquire(self, paths, *, agent_id: str, session_id: str,
                intent: str = "", ttl_s: float = None) -> dict:
        """All-or-nothing over the sorted relpath list.

        Sorted so two agents requesting the same pair in opposite order cannot
        deadlock; all-or-nothing so a partial grab never leaves half a refactor
        holding half its files.
        """
        if not paths:
            return {"granted": False, "error": "paths is empty"}
        ttl = float(ttl_s) if ttl_s else self._ttl
        try:
            rels = sorted({self._rel(p) for p in paths})
        except UnsupportedPathError as exc:
            return {"granted": False, "error": "unsupported_path",
                    "reason": exc.reason, "detail": str(exc)}

        with self._lock, self._guard():
            doc = self._load()
            now = self._clock()
            self._prune(doc, now)
            held = {r["relpath"]: r for r in doc["locks"]}

            conflicts = [
                {"relpath": rel, "owner": held[rel]["agent_id"],
                 "intent": held[rel].get("intent", ""),
                 "expires_in_s": round(held[rel]["expires_at"] - now, 1)}
                for rel in rels
                if rel in held and held[rel].get("session_id") != session_id
            ]
            if conflicts:
                return {"granted": False, "repo_id": self.rid(),
                        "conflicts": conflicts}

            granted = []
            for rel in rels:
                existing = held.get(rel)
                if existing is not None:  # our own lease — extend, keep token
                    existing["expires_at"] = now + ttl
                    existing["intent"] = intent or existing.get("intent", "")
                    granted.append({"relpath": rel,
                                    "fencing_token": existing["fencing_token"],
                                    "expires_at": existing["expires_at"]})
                    continue
                doc["fencing"] = int(doc.get("fencing", 0)) + 1
                row = {"relpath": rel, "agent_id": agent_id,
                       "session_id": session_id,
                       "fencing_token": doc["fencing"], "intent": intent,
                       "acquired_at": now, "expires_at": now + ttl,
                       "lock_id": uuid.uuid4().hex}
                doc["locks"].append(row)
                granted.append({"relpath": rel,
                                "fencing_token": row["fencing_token"],
                                "expires_at": row["expires_at"]})
            self._save(doc)
            return {"granted": True, "repo_id": self.rid(), "locks": granted}

    def renew(self, paths, *, session_id: str, ttl_s: float = None) -> dict:
        ttl = float(ttl_s) if ttl_s else self._ttl
        with self._lock, self._guard():
            doc = self._load()
            now = self._clock()
            self._prune(doc, now)
            renewed, rejected = [], []
            wanted = set()
            for p in paths or []:
                try:
                    wanted.add(self._rel(p))
                except UnsupportedPathError:
                    rejected.append({"relpath": str(p),
                                     "reason": "unsupported_path"})
            for row in doc["locks"]:
                if row["relpath"] in wanted:
                    if row.get("session_id") != session_id:
                        rejected.append({"relpath": row["relpath"],
                                         "reason": "not_owner"})
                        continue
                    row["expires_at"] = now + ttl
                    renewed.append({"relpath": row["relpath"],
                                    "expires_at": row["expires_at"]})
            found = {r["relpath"] for r in renewed} | {
                r["relpath"] for r in rejected}
            rejected.extend({"relpath": rel, "reason": "expired_or_absent"}
                            for rel in sorted(wanted - found))
            if renewed:
                self._save(doc)
            return {"ok": not rejected, "renewed": renewed,
                    "rejected": rejected}

    def release(self, paths=None, *, session_id: str) -> dict:
        """Release this session's leases; all of them when ``paths`` is None."""
        with self._lock, self._guard():
            doc = self._load()
            self._prune(doc, self._clock())
            targets = None
            if paths:
                targets = set()
                for p in paths:
                    try:
                        targets.add(self._rel(p))
                    except UnsupportedPathError:
                        pass
            kept, released = [], []
            for row in doc["locks"]:
                mine = row.get("session_id") == session_id
                wanted = targets is None or row["relpath"] in targets
                if mine and wanted:
                    released.append(row["relpath"])
                else:
                    kept.append(row)
            doc["locks"] = kept
            if released:
                self._save(doc)
            return {"ok": True, "released": sorted(released),
                    "count": len(released)}

    def force_release(self, path, *, by: str = "human", note: str = "") -> dict:
        """Human override. Bumps the fencing counter so a returning holder is
        stale by construction, even if it still believes it holds the lease."""
        with self._lock, self._guard():
            doc = self._load()
            self._prune(doc, self._clock())
            try:
                rel = self._rel(path)
            except UnsupportedPathError as exc:
                return {"forced": False, "error": "unsupported_path",
                        "reason": exc.reason}
            before = len(doc["locks"])
            previous = next((r for r in doc["locks"] if r["relpath"] == rel), None)
            doc["locks"] = [r for r in doc["locks"] if r["relpath"] != rel]
            doc["fencing"] = int(doc.get("fencing", 0)) + 1
            self._save(doc)
            return {"forced": True, "relpath": rel,
                    "was_locked": len(doc["locks"]) != before,
                    "previous_owner": previous["agent_id"] if previous else None,
                    "by": by, "note": note}

    def sweep(self) -> dict:
        """Drop expired leases. Safe to call from anywhere; TTL is the real
        release mechanism, so this is what actually frees a crashed agent."""
        with self._lock, self._guard():
            doc = self._load()
            expired = self._prune(doc, self._clock())
            if expired:
                self._save(doc)
            return {"expired": [r["relpath"] for r in expired],
                    "count": len(expired)}


# ── Config ──────────────────────────────────────────────────────────────────

def config(project_path=".") -> dict:
    """``.c3/config.json`` section ``locks``. Unknown/missing → defaults."""
    out = {"mode": "advisory", "default_ttl_s": DEFAULT_TTL_S,
           "backend": "local", "enabled": True}
    cfg = Path(project_path) / ".c3" / "config.json"
    if not cfg.is_file():
        return out
    try:
        section = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("locks")
    except Exception:
        return out
    if not isinstance(section, dict):
        return out
    if section.get("mode") in MODES:
        out["mode"] = section["mode"]
    ttl = section.get("default_ttl_s")
    if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0:
        out["default_ttl_s"] = float(ttl)
    if isinstance(section.get("enabled"), bool):
        out["enabled"] = section["enabled"]
    # backend is read but not honoured yet: only the local backend exists.
    # It is NEVER chosen per call — see spec §10 (the namespace trap).
    if section.get("backend") in ("local", "fleetdeck"):
        out["backend"] = section["backend"]
    return out


def store_for(project_path=".") -> LockStore:
    return LockStore(project_path, ttl_s=config(project_path)["default_ttl_s"])


# ── Gate ────────────────────────────────────────────────────────────────────

def check(path, project_path=".", session_id: str = ""):
    """The row blocking this session from ``path``, or None.

    Mirrors ``access_guard.check``'s shape so the gate reads the same at the
    call site. Never raises: a lock system that breaks edits when its own
    state is unreadable is worse than one that lets an edit through.
    """
    try:
        cfg = config(project_path)
        if not cfg["enabled"]:
            return None
        return store_for(project_path).holder_of(path, session_id)
    except Exception:
        return None


def lease(path, project_path=".", session_id: str = "", intent: str = "") -> bool:
    """Best-effort implicit lease on one file, for c3_edit to call.

    Implicit rather than explicit by design (spec §14): an explicit
    c3_locks(acquire) yields better intent strings, but agents forget explicit
    steps and a lease nobody takes protects nobody. The summary the agent
    already wrote is a good enough intent.

    Never raises and never blocks: check() has already decided whether this
    edit may proceed, so a failure here costs coordination, not correctness.
    """
    try:
        if not config(project_path)["enabled"]:
            return False
        text = (intent or "").strip()[:120] or "editing via c3_edit"
        res = store_for(project_path).acquire(
            [path], agent_id=agent_id_for(session_id),
            session_id=session_id, intent=text)
        return bool(res.get("granted"))
    except Exception:
        return False


def refusal(holder: dict, path) -> str:
    """The agent-facing refusal. The last line is load-bearing: without an
    explicit 'do not route around this', models reach for c3_shell with a
    one-liner within about two turns (the lesson Mask Guard already taught)."""
    owner = holder.get("agent_id", "another agent")
    intent = (holder.get("intent") or "").strip()
    left = holder.get("expires_in_s")
    if left is None:
        left = max(0.0, holder.get("expires_at", 0) - time.time())
    mins, secs = divmod(int(max(0, left)), 60)
    detail = f'\n  intent: "{intent}"' if intent else ""
    return (
        f"{TAG_HELD} {path} is held by {owner}.{detail}\n"
        f"  lease expires in {mins}m{secs:02d}s.\n"
        f"  This is a policy block, not a transient error — do not route around "
        f"it via c3_shell, native Write, or another tool. Work on a different "
        f"file, or ask the holder to release."
    )
