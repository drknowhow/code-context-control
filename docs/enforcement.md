# Tool Discipline (Enforcement Modes)

Added in v2.66.0. This documents **Layer C** — how hard C3 pushes the agent
toward `c3_*` tools — and how to turn it down when it gets in the way.

## The four layers, and which one is bothering you

C3 has four independent gates. They are easy to confuse, and picking the wrong
lever either does nothing or weakens something you wanted kept.

| Layer | What it governs | Where it lives | Lever |
|---|---|---|---|
| **A** Permission tier | which tools the IDE will call at all | `.claude/settings.local.json` | `c3 permissions <tier>` |
| **B** Access Guard | which **paths** the agent may read/write | `access` in `config.json` | `c3 access …` |
| **C** Tool discipline | whether native `Edit`/`Write` are blocked in favour of `c3_edit` | `enforcement` in `config.json` | `c3 enforce <mode>` |
| **D** Agent locks | file leases between concurrent agents | `locks` in `config.json` | `c3 locks …` |

If work feels slow because edits keep getting refused with `[c3:enforce]`, that
is **Layer C**. If a specific path is refused with `[c3-access:denied]`, that is
**Layer B**. Run `c3 access stats` — it labels each denial with its layer and
names the exact command that clears it.

## Modes

```
c3 enforce             # show the current mode and what it blocks
c3 enforce advisory    # the usual fix for friction
c3 enforce strict
c3 enforce off
c3 enforce --global advisory     # default for every project on this machine
c3 enforce --signal-ttl 1800     # keep native tools unlocked longer
```

| Mode | Native `Edit`/`Write` without a prior `c3_*` call | Edit ledger |
|---|---|---|
| `strict` | **denied** | full, including c3_edit's pre-edit snapshot |
| `advisory` | allowed, with a one-line hint | full — `hook_edit_ledger` runs PostToolUse regardless |
| `off` | allowed silently | full |

The ledger is captured by a **PostToolUse** hook, so it does not depend on this
setting. What `strict` buys you over `advisory` is the pre-edit snapshot that
`c3_edit` takes, which is what makes a clean revert possible. That is the whole
trade-off.

## Scope: the project root

The hook governs the project's own files. A native Edit/Write/Read aimed at
a path outside the project root — a scratch file in `/tmp`, a file in
another checkout — passes through untouched in every mode: no block, no
hint. There is no ledger here for that file, so there is nothing to protect.
Relative paths resolve against the project root.

## Shell commands

`Bash` is never blocked by tool discipline — blocking shell writes outright
would break far more legitimate work than it protects. What the hook does
instead: it reads the command for files it probably writes (`cli/_shell_writes.py`
— redirects, `tee`, `cp`/`mv`/`touch`/`rm`, `sed -i`, inline Python
`open(...,'w')`, following `cd`) and, when any of them is inside the project
and `c3_edit` has not just run, adds an advisory hint naming them. After the
command runs, files it named that exist, are editable and changed within the
last two minutes are written to the edit ledger as `change_type: "shell"`
rows carrying the command — after the fact, with no pre-edit snapshot.

## Failed calls

A `c3_*` call that failed (a response beginning `Error` / `[tool:error]`, the
masked-path refusal, or an MCP `isError`) counts for nothing: no enforcement
signal, no sticky unlock, and the activity log records it with `ok: false` so
the hook's scan skips it. To create a new file use
`c3_edit(file_path=..., old_string='', new_string=...)`.

## What never changes, at any mode

Loosening tool discipline is safe precisely because it cannot reach these:

- **Access Guard path policy** — `deny` / `read_only` / `mask` rules, and the
  builtins. `hook_access_guard` runs *first* in the PreToolUse route and its
  deny wins the merge.
- **The credential vault** — `.c3/secrets.enc`, `.c3/cred_state.json`,
  `.c3/config.json` are never writable by a native tool. This guard keys off a
  fixed tool set, so neither `off` nor a `blocked_tools: []` override reaches it.
- **Agent locks** — leases are governed by the `locks` section.

Tests in `tests/test_enforcement_policy.py::TestSecurityBoundariesSurviveEveryMode`
assert exactly this, for every mode.

## Defaults and upgrades

Resolution order is **project → global → `strict`**.

An install with no `enforcement` section resolves to `strict`, which is the
pre-v2.66 behavior. Nothing is derived at read time, so **upgrading C3 never
changes how an existing project behaves.** A mode is written only when you run
`c3 init`, `c3 permissions <tier>`, or `c3 enforce`.

When a mode *is* written from a tier, it follows this table — which resolves the
old contradiction where the `permissive` tier ("all tools pre-approved") still
had every native `Edit` hard-denied by the hook:

| Permission tier | Derived discipline |
|---|---|
| `read-only` | `strict` |
| `c3-strict` | `strict` |
| `standard` | `advisory` |
| `permissive` | `off` |

### Provenance (`set_by`)

- `set_by: "repo-shape"` — written by `c3 init` when the project is a
  documentation repo (see *`c3 init`* below). A default with a stated
  reason: it defers to `user` exactly as `tier` does, and a later tier
  choice overrides it.
- `set_by: "tier"` — written as a side effect of choosing a permission tier.
  A later tier change may overwrite it.
- `set_by: "user"` — set explicitly with `c3 enforce`. A tier change **defers**
  to it and prints that it did, so an explicit choice is never silently undone.

## Config reference

```jsonc
// .c3/config.json  (or ~/.c3/config.json for the machine-wide default)
{
  "enforcement": {
    "mode": "advisory",        // strict | advisory | off
    "set_by": "user",          // tier | user  — see provenance above
    "signal_ttl_s": 600,       // how long one c3_* call keeps natives unlocked
    "blocked_tools": ["Edit", "Write", "MultiEdit"]   // optional narrowing
  }
}
```

Everything fails **closed**. An unknown mode, a malformed section, unparseable
JSON, or a `blocked_tools` entry naming a tool this policy does not govern all
resolve to `strict` and surface a `[c3:enforcement-config]` warning to the
agent rather than silently relaxing enforcement.

`signal_ttl_s` is clamped to 30…86400 **at read time** (a hand-edited file gets
a warning, not a crash); the write surfaces — `c3 enforce --signal-ttl` and the
UI/Hub routes — reject an out-of-range value instead of adjusting it. Raise it
when a long single-file refactor keeps outrunning the 10-minute default and
getting re-blocked.

Since v2.67 `signal_ttl_s` and `blocked_tools` are also editable without
touching the mode (the Hub TTL/tools editors use this): a mode-less write goes
through `enforcement_policy.set_fields`, which never touches `mode`/`set_by` —
so a TTL tweak cannot turn a tier-derived choice into a `user` one. It also
**refuses to create** an `enforcement` section: resolution stops at the first
scope whose section exists, and a mode-less section coerces to `strict`, so a
TTL-only section would silently shadow an inherited `advisory`. Pick a mode at
that scope first.

## Project UI — the Discipline tab

`c3 ui` has a **Discipline** tab next to Access Guard. It covers this project
only, and is where you go when work is being blocked right now:

- Three mode cards — pick one to apply it. `off` confirms first.
- Provenance: the active mode, where it came from, the signal TTL, and what
  the permission tier implies. If your explicit choice differs from the tier,
  it says so and that a tier change will not undo it.
- A panel listing what stays enforced at every mode, so `off` is an informed
  choice rather than a guess.
- The denial table: hits, rule, tool, layer, and the command that clears it.

Routes: `GET|POST /api/enforcement`, `DELETE /api/enforcement/denials`.

## Hub UI — the Discipline tab

The Hub (`c3 hub`) has a **Discipline** tab alongside Projects / Tasks /
Credentials / Locks. It is the same knob as `c3 enforce`, across every
registered project at once:

- One row per project with its current mode, where that mode came from
  (`project` / `global` / never set), its permission tier, its signal TTL and
  its `blocked_tools` count.
- **Search, filter, sort** (v2.67): a free-text filter over name / path /
  mode / tier (`/` focuses it), chips for `Strict` / `Advisory` / `Off` /
  `Has denials` / `Attention` (warnings, unreadable policies, tier drift),
  and sorting by name, denial count, or mode. The list polls every 5 s —
  never while you are typing or a dialog is open.
- A three-way mode picker per row. Switching to `off` asks for confirmation
  and spells out both what stays enforced and what you give up.
- A **Global default card** (v2.67) for the `~/.c3` fallback: its own mode
  picker and TTL editor, `NOT SET` when no global section exists. Projects
  with their own setting are unaffected, and the card says so.
- **TTL and blocked-tools editors** per row (v2.67), enabled only when the
  row's policy actually comes from the project scope — an inherited policy
  says why it is disabled instead of writing a section that would shadow the
  inheritance. Both post mode-less bodies, so `set_by` is untouched.
- **Bulk apply** (v2.67): `select` puts the list in checkbox mode; a sticky
  bar applies one mode to every selected project after a single confirm.
  Each write is still audited per project.
- Denial counts per project. Expand a row for the ranked breakdown — hits,
  rule, tool, last hit, session count, and the exact command that clears it —
  plus a counter reset and a **raw event search** (v2.67): substring over
  path/rule/tool, layer chips, click a session id to filter to that session,
  `all events` to browse the log newest-first.
- Projects that are unreadable or have no `.c3` are listed under **Not
  reporting** rather than silently shown as `strict`. "We don't know" and
  "running strict" are different claims and the tab keeps them apart.
- A malformed `enforcement` section raises a banner saying those projects
  resolve to `strict` and will not honour the mode displayed.
- Clicking a project name opens the drill panel on its **Discipline** tab
  (v2.67) — the same controls scoped to one project, with the full 12-row
  denial aggregate and the event search.

It is deliberately a separate tab from Access Guard, for the same reason
`c3 enforce` is a separate command: path policy is a security boundary, tool
discipline is a workflow preference, and one tab for both invites the mistake
of loosening the wrong one.

Routes: `GET /api/hub/enforcement/overview`,
`GET|POST /api/projects/enforcement` (POST body
`{path?, scope?, mode?, signal_ttl_s?, blocked_tools?}`; `scope: "global"`
writes `~/.c3` and needs no path),
`GET /api/projects/enforcement/denials/search`,
`DELETE /api/projects/enforcement/denials`. Project-scope mutations are
ledger- and activity-logged on the target project; mode changes are always
recorded as `set_by: "user"`, so a Hub change survives a later tier change.
Global-scope writes have no target project to audit into — the
`~/.c3/config.json` write is itself the record. The `enforcement` section is
deliberately excluded from the generic Config editor's write whitelist: the
dedicated route is the only write path, so validation and provenance rules
cannot be bypassed.

## Denial telemetry

`docs/access-guard.md` §3 specified denial logging; it landed in v2.66.0.

```
c3 access stats                 # ranked denials + the lever for each
c3 access stats --session <id>  # just this session
c3 access stats --json          # machine-readable
c3 access stats --clear         # reset counters
```

Events append to `.c3/denials.jsonl` (rotated at 512 KB) and are coalesced per
`(layer, rule, tool)` at read time — hooks are concurrent short-lived
subprocesses, so a shared counter file would race, while a single-line append
is the pattern `edit_ledger.jsonl` already relies on.

Since v2.67 the raw events are also searchable without coalescing:
`access_telemetry.search_events` backs
`GET /api/enforcement/denials/search` (project UI) and
`GET /api/projects/enforcement/denials/search?path=…` (Hub). Filters: `q`
(AND'd case-insensitive substrings over path/rule/tool), `layer`, `tool`
(exact), `session` (prefix, so 8-char short ids work), `since` (ISO-8601),
`limit` (default 200, cap 500). Newest first; `matched` keeps counting past
the cap so truncation is visible. The rotated `.jsonl.1` is included. The
aggregate endpoints also accept `?session=` to narrow to one session.

The log is local, gitignored, and records the denied path. Clear it with
`--clear` if that matters for a given repo.

## `c3 init`

Interactive init asks for tool discipline as **Step 5/5**, right after the
permission tier, defaulting to the tier-derived mode. Non-interactive:

```
c3 init . --force --permissions standard --enforcement advisory
```

Omit `--enforcement` to take the tier's derived value.

`c3 init` on an existing project prints a `Disc :` line with the active mode,
its provenance, and a note when it disagrees with what the stored tier implies.

### Repo shape

A new install also prints a `Repo shape:` line — how many source files
against how many prose/office documents (`.md`, `.rst`, `.docx`, `.pdf`, …;
config, data and images count for neither side). Below 20 judged files it
has no opinion. At 10% source or less the project is `prose`: C3's
symbol-aware tools have little to act on in a documentation repo, so
interactive Step 5/5 suggests `advisory` and says why, and `--force` without
`--enforcement` writes `advisory` with `set_by: repo-shape` and prints the
way back. It never overrides a `c3 enforce` choice or an explicit
`--enforcement`. `services.repo_shape` holds the thresholds.

## Sub-agents without C3 tools

A Claude Code sub-agent whose definition lists `tools:` gets only those
tools. If none of them reaches the `c3` MCP server (`mcp__c3__c3_edit`,
`mcp__c3`, or `*`), a strict deny telling it to use `c3_edit` cannot be
followed. The hook reads `agent_type` from the payload, looks the agent up
in `.claude/agents/` (project, then `~/.claude/agents/`), and for such an
agent degrades the block to the advisory nudge — the edit ledger still
records the write. An agent with no `tools:` line inherits every tool and
stays strict. To keep strict for a tool-listed agent, add `mcp__c3__c3_edit`
to its grant or drop the `tools:` line.

## Troubleshooting

**Blocks continue after `c3 enforce advisory`.** Check `c3 enforce` output — a
global-scope entry cannot override a project one (project wins), and the
permission tier (Layer A) can deny `Edit` independently. `c3 permissions show`
covers that layer.

**`[c3:hook-error] enforcement_state: corrupted …`.** Pre-v2.66,
`_atomic_write_json` had no `fsync` and no cleanup on a failed `os.replace`,
which could publish a truncated state file and orphan `.c3/*.tmp<pid>` files. A
corrupt state loads empty, which *drops sticky unlocks* and makes enforcement
feel more aggressive, not less. v2.66 fsyncs before publishing, retries the
replace on Windows sharing violations, and always removes the temp file.
`c3 init` sweeps orphaned temp files whose owning PID is gone.
