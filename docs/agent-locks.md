# Agent Locks — Design Spec (DRAFT, pre-implementation)

Status: **DRAFT.** Not frozen. Written 2026-07-30 after reading FleetDeck's
lock engine (`U:\1. Projects\AgentSync\fleetdeck\locks.py`, `paths.py`,
`hook.py`) and C3's existing concurrency surface. Nothing here is built yet.
Freeze this document before Phase 3 starts.

Agent Locks let multiple agents work in one repo — or across several C3
projects — without clobbering each other. Like Access Guard, it is a
**cooperative coordination layer, not containment**: it protects against
agents racing each other, not against a hostile process. See §9 Coverage
matrix for exactly what is and is not covered.

---

## 1. The problem, precisely

Two distinct failures get conflated. They need different mechanisms.

**Layer A — torn writes.** Two `c3_edit` calls interleave read → replace →
write on one file; one edit is silently lost. Today `cli/tools/edit.py:20`
guards this with a `threading.Lock`, which is per-process. Every Claude Code
session spawns its own `c3-mcp` stdio server, so across sessions this is
**unguarded**. There is also no mtime/hash staleness check anywhere in
`edit.py`.

**Layer B — overlapping work.** Two agents both refactor `services/router.py`
over ten minutes. No per-call locking helps. This needs a *lease* an agent
holds across many edits, with a declared intent.

Layer A is the correctness floor. Layer B is the feature.

### 1.1 Why FleetDeck alone does not cover this

FleetDeck installs a user-level PreToolUse hook with matcher
`Edit|Write|MultiEdit|NotebookEdit`, and `fleetdeck/hook.py:23` hard-codes the
same set as `EDIT_TOOLS`. C3's own PreToolUse hook **blocks those tools** and
routes edits through `mcp__c3__c3_edit`. In a C3 project, FleetDeck's gate
therefore never fires. The two systems currently cancel out.

---

## 2. Schema

Lock state is a per-project file, in the **target** project — never the
caller's. `c3_project(action='edit', allow_write=true)` lets an agent in
project A mutate project B; caller-scoped state would put the two agents in
different files and neither would see the other.

`.c3/locks.json` (project scope only; no global scope):

```json
{
  "version": 1,
  "fencing": 47,
  "mode": "advisory",
  "locks": [
    {
      "relpath": "services/router.py",
      "agent_id": "claude-code:a3f19c2b",
      "session_id": "a3f19c2b-...",
      "fencing_token": 47,
      "intent": "refactor retry backoff",
      "acquired_at": 1785432100.4,
      "expires_at": 1785433000.4,
      "lock_id": "9f2c..."
    }
  ]
}
```

Mutual exclusion on the file itself uses the cross-process `_FileLock` already
in `services/task_store.py:71` (msvcrt `LK_NBLCK` on Windows, `flock` on
POSIX, 30 s bounded acquire, OS-released if the holder dies). Same
load → mutate → atomic-save transaction pattern as `task_store` and
`time_tracker`.

Config lives in `.c3/config.json`, section `locks`:

```json
{
  "locks": {
    "mode": "advisory",
    "default_ttl_s": 900,
    "backend": "local"
  }
}
```

- `mode`: `advisory` (default) or `strict`. Strict fails **closed** when lock
  state cannot be read or the backend is unreachable.
- `backend`: `local` (default) or `fleetdeck`. See §10 — this is chosen
  per repo at config time and is **never** switched at call time.
- Mutations are human-only (`c3 locks` CLI / Hub UI), except acquire/release,
  which are the agent's normal working verbs.

---

## 3. Lock identity

Key is `(repo_id, relpath)` — adopt FleetDeck's scheme verbatim from
`fleetdeck/paths.py` so both systems name the same file the same way:

- `repo_id` = sha1 of the canonical repo root (absolute, casefolded,
  forward-slash Windows form).
- `relpath` = casefolded, forward-slash, repo-relative.
- Normalization is **purely lexical** — no filesystem access. Files that do
  not exist yet still normalize, and the result never depends on cwd or
  symlinks.
- `/mnt/u/repo/src/api.py` (WSL) and `U:\repo\SRC\API.PY` (Windows) collide on
  one key.
- Unsupported forms are rejected with a machine-readable reason —
  `unc`, `outside_repo`, `is_root`, `empty` — never guessed. A wrong guess
  silently breaks mutual exclusion, which is worse than refusing.

`agent_id` follows FleetDeck's convention: `claude-code:<session_id[:8]>`
(`fleetdeck/hook.py:57`). Same agent, same name, both systems.

---

## 4. Lease lifecycle

Adopt FleetDeck's semantics (`fleetdeck/locks.py`). They are correct and
matching them is what makes the two systems interoperate rather than fight.

| Op | Contract |
|---|---|
| `acquire` | **All-or-nothing** over a sorted relpath list. Deterministic ordering means two agents grabbing the same pair in opposite order cannot deadlock. Returns `{granted, locks[]}` or `{granted: false, conflicts[]}`. |
| `renew` | Extends TTL. Rejects if the fencing token is stale or the caller is not the holder. |
| `release` | By session, optionally scoped to paths. Idempotent. |
| `validate` | Non-mutating: does this session still hold these locks with current tokens? The strict-mode primitive. |
| `force_release` | Human override. **Bumps the fencing counter** so a returning holder is stale by construction. Audited. |
| `sweep` | Expires leases past TTL. Every expiry is an event. |

Four properties to preserve:

1. **TTL is the real release mechanism.** Agents forget to release. Design as
   if `release` never happens; default 900 s, renewable. A crashed agent can
   never wedge a repo.
2. **Fencing tokens** make `force_release` safe.
3. **All-or-nothing** multi-path acquire — required because `c3_edit`
   encourages parallel calls across files.
4. **The engine never lies.** An advisory repo is badged advisory. We never
   render "protected" for a repo we cannot actually protect.

---

## 5. Gate placement

C3 already has the right seam. `cli/tools/edit.py:259`:

```python
op = "write" if path.exists() else "create"
denial = access_guard.check(str(path), op, svc.project_path)
if denial:
    return finalize("c3_edit", {"file": file_path},
                    access_guard.refusal(denial, file_path, op), "access-denied")
```

The lock gate is a structurally identical second evaluator on the next line —
`agent_locks.check(path, op, project_path) -> Held | None` plus a
`refusal()` renderer. Ordering: vault guard → Access Guard → **Agent Locks**.
Policy denials outrank contention; never tell an agent a file is locked when
it was never allowed to write it.

Seams to gate:

| Surface | File | Notes |
|---|---|---|
| `c3_edit` | `cli/tools/edit.py:259` | create, single, and batch modes alike |
| `c3_shell` | `cli/tools/shell.py` | only the git-mutating commands C3 already detects for the ledger — see §9 |
| `c3_project(action='edit')` | `cli/tools/project.py` | proxies to `handle_edit`, so inherited — verify the target project's lock file is used, not the caller's |
| native `Edit`/`Write` | `cli/hook_pretool_enforce.py` | C3 already runs a PreToolUse hook here; a lock check is a natural addition. Needed because `hook_edit_unlock.py` legitimately unlocks native Edit after a `c3_read`. |

**Layer A is separate and unconditional.** Replace the `threading.Lock` at
`edit.py:20` with `_FileLock` regardless of whether leases ship. Held for the
duration of one read → replace → write, it gives genuine mutual exclusion for
`c3_edit`-vs-`c3_edit` with no daemon, no TTL, no recovery path.

---

## 6. Refusal contract

Reuse the established C3 refusal idiom. Tag `[c3-lock:held]`, matching
`[c3-mask:transformed]` / `[c3-mask:unsupported]`, which the instruction docs
already teach agents not to route around.

```
[c3-lock:held] services/router.py is held by claude-code:a3f19c2b
  intent: "refactor retry backoff"   expires in 6m12s
This is a policy block, not a transient error. Do not retry via c3_shell,
native Write, or another tool. Work on a different file, or ask the holder
to release.
```

The last sentence is load-bearing. Without an explicit "do not route around
this", models reach for `c3_shell` with a Python one-liner within about two
turns. This lesson is already banked from Mask Guard.

Strict-mode backend failure is a distinct tag — `[c3-lock:unavailable]` —
so an agent can tell contention from infrastructure.

---

## 7. Surfaces

- **`c3_locks(action='list|acquire|release|renew|status')`** — MCP tool.
  Mirrors the REST verbs. `force_release` is human-only.
- **`c3 locks list | release --all | force-release <path>`** — CLI. The hammer
  for when an agent is gone and TTL has not fired.
- **Hub UI tab** — per-repo lock table: holder, intent, draining lease ring,
  honest `ADVISORY` / `ENFORCED` badge, force-release button. C3's Hub is
  already cross-project, so this is a fleet-wide view without a fleet daemon.
- **Events** — denials to `.c3/notifications.jsonl`; acquire/release/expire as
  cheap ledger events. Denials are the signal that tells you whether leases
  were worth building.

---

## 8. Granularity

File-level for v1. Symbol-level is tempting — `c3_impact` already resolves
symbol blast radius — but it roughly doubles the state model and the failure
modes (overlapping ranges, edits that move symbol boundaries, renames). The
known pain is coarse locks on large files like `cli/hub_server.py` (~1500
lines). Accept that in v1; revisit only if the denial log shows it dominating.

---

## 9. Coverage matrix

Honest scope. Nothing here is containment.

| Path to a file mutation | Covered | How |
|---|---|---|
| `c3_edit` (create / single / batch) | **Yes** | gate at `edit.py:259` + `_FileLock` |
| `c3_project(action='edit')` | **Yes** | proxies `handle_edit`; target-project lock file |
| native `Edit` / `Write` / `MultiEdit` in Claude Code | **Yes** | `hook_pretool_enforce.py` |
| `c3_shell` running git mutations (`checkout`, `reset`, `restore`, `merge`) | **Partial** | only commands C3 already parses for the ledger |
| `c3_shell` running arbitrary code that writes files (`sed`, a test writing fixtures, a build step) | **No** | paths are not declarable; genuinely uncoverable |
| A non-Claude agent (Codex / Gemini / Ollama) editing directly | **No** | needs FleetDeck, or that agent calling `c3 locks acquire` |
| A human in an editor | **No** | out of scope |
| A repo with no `.c3/` | **No** | no lock file, no coordination |
| Two machines on a shared drive | **No** | neither C3 nor FleetDeck handles this |

The Hub badge must reflect this matrix. A repo where agents mostly work
through `c3_shell` is not meaningfully protected and should not look like it
is.

---

## 10. Backends, and the namespace trap

The obvious design — *try FleetDeck, fall back to `.c3/locks.json` if the
daemon is down* — **is a correctness bug.** If the daemon flaps, agent A lands
in the FleetDeck namespace and agent B in the local one. Neither sees the
other, and the badge still says protected. Silent collisions are worse than no
locking, because you stop watching for them.

Rule: **backend is chosen per repo, at config time, deterministically.** When
the chosen backend is unreachable, behaviour is governed by `mode` —
`advisory` fails open, `strict` fails closed — but the namespace **never**
switches.

Default is `local`. C3 ships as a product (see `commercial/`) and cannot
depend on personal infrastructure. The local backend needs no daemon, which
for a lock system is a real correctness advantage: it cannot fail open because
a process died.

---

## 11. Relationship to FleetDeck

**Recommended direction: C3 owns locks; FleetDeck owns the fleet.**

This inverts the more obvious "C3 as a FleetDeck client" arrangement. The
deciding factor is that C3's lock state is files on disk with OS-level
locking, so it works with **no daemon running** — whereas FleetDeck's engine
fails open when its daemon is down. For a mutual-exclusion primitive, that
asymmetry decides it. C3 also has the depth: it is the only thing that sees
`c3_edit`, `c3_shell`, `c3_project`, Access Guard verdicts and masked paths.

So the split is:

| | C3 Agent Locks | FleetDeck |
|---|---|---|
| Scope | per project, any number of projects | machine-wide |
| Substrates | Claude Code (MCP tools + hook) | all — Claude, Codex, Gemini, Ollama, CLI sessions |
| Needs a daemon | no | yes |
| Owns | lock state, leases, fencing | presence, heartbeats, task leases, messaging, cockpit |

Integration, once both exist: FleetDeck **reads** each registered repo's
`.c3/locks.json` to render its per-repo lock tables, and its non-Claude
wrappers acquire through `c3 locks acquire` or C3's REST surface. One
namespace, C3 authoritative, FleetDeck as the cross-substrate front door and
the cockpit.

### Do you need to run FleetDeck?

**No — not for locking**, once Phases 1–3 land. Every Claude Code session,
interactive or subagent, across every c3-installed project on the machine, is
covered by C3 alone with nothing running in the background.

**Yes — for these**, which C3 does not do and should not grow:

- A non-Claude substrate (a Codex, Gemini, or Ollama daemon) editing the same
  repos. C3's gate lives inside C3's own tools and hook; it cannot see them.
- Presence — "which agents are alive right now, on what substrate, doing
  what." C3 has per-project sessions, not a machine-wide live roster.
- Cross-agent messaging / broadcast into each agent's inbox.
- Repos with no `.c3/`.

Until a non-Claude agent shares a repo with a Claude session, FleetDeck is
optional for this purpose.

### Immediate stopgap, before any of this is built

Add `mcp__c3__c3_edit` to FleetDeck's PreToolUse matcher **and** to
`EDIT_TOOLS` in `fleetdeck/hook.py:23`. Claude Code matchers regex against MCP
tool names, and `_PATH_KEYS` already leads with `file_path`, which is
`c3_edit`'s parameter. Two lines, restores coverage today, and starts
producing the denial data that tells you whether Phase 3 is worth building.

---

## 12. Failure modes to design for

| Mode | Handling |
|---|---|
| Agent never releases | TTL. Assume release never happens. |
| Agent crashes mid-edit | `_FileLock` is OS-released on process death; the lease expires on TTL. |
| `.c3/locks.json` corrupt | Advisory: log and proceed unlocked. Strict: refuse all writes with `[c3-lock:unavailable]`. Never silently reset to empty. |
| Two agents, opposite acquisition order | All-or-nothing over a sorted list makes deadlock impossible. |
| Clock skew | Single machine; `time.time()` is fine. Revisit if state is ever shared across hosts. |
| Lease held by a session that ended | Stop-hook release, plus TTL as backstop. |
| Agent routes around a denial | Explicit refusal wording (§6) + the same instruction-doc treatment masked paths get. Cooperative, not enforced. |

---

## 13. Phasing

1. **`_FileLock` in `edit.py`.** Cross-process torn-write safety. ~15 LOC +
   tests. No new concepts, no daemon, ships standalone. Do this regardless of
   everything below.
2. **FleetDeck two-line matcher fix** (§11). Restores coverage today and
   answers the empirical question: *do these agents actually collide, and
   where?*
3. **Lease service + `c3_locks` + gates + refusal strings.** ~400 LOC. Local
   backend only.
4. **Hub tab + FleetDeck read integration.** ~200 LOC.

Phases 1–2 are an afternoon. Run them, read the denial log, and let that
decide whether 3 and 4 are worth building.

---

## 14. Open questions

- Should acquiring a lease be **implicit** (first `c3_edit` on a file takes a
  lease) or **explicit** (`c3_locks(action='acquire')` with a declared
  intent)? Implicit is invisible and always correct; explicit produces far
  better intent strings for the other agent to read. Probably implicit-with-
  auto-intent derived from the edit summary, explicit as an override.
- Should a lease block **reads**? Almost certainly not — but an agent reading
  a file another agent is mid-refactor on will act on stale content. A
  read-side *warning* (not a block) may be worth more than the lock.
- Does `c3_impact` belong in the acquire path — locking a symbol's callers
  alongside the symbol? Powerful, and a good way to turn one edit into a
  twenty-file lease that wedges everyone. Defer.
- Sub-projects (`services/subprojects.py`): does a child branch share the
  parent's lock namespace, or keep its own? Follow whatever the federated
  memory scope already does.
