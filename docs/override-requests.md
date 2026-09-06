# Override Requests — v1 Design Spec (implementation contract)

Status: **FROZEN 2026-08-07. P1 + P2 + P2a + P3 shipped (v2.69.0 / v2.70.0 /
v2.72.0 / v2.71.0). P4 built and installed on the phone; its first live run
found the P2a gap, which is what v2.72.0 closes. P5 approval surface shipped
v2.98.0 (Hub Access tab: cross-project pending cards, decide routes with
`decided_by="desktop"`, typed-glob challenge, read-only policy matrix —
see docs/confirm-guard.md §7). Still outstanding from P5: the Settings UI
for `override.layers` and the "this rule is costing you" nudge.**
Written 2026-08-07 from a survey of the live blocking layers, the Oracle mobile
API, and the c3-mobile client. Changes from here need a documented reason in
the PR description (same rule as `access-guard.md`).

Implementation status per phase (§14):

| Phase | State | Where |
|---|---|---|
| P0 spike | folded into P1 | cross-process lock + policy short-circuit |
| P1 grant primitive | **shipped v2.69.0** | `services/override_policy.py`, `services/override_grants.py`, both PreToolUse hooks, `c3 override` |
| P2 agent surface (`c3_override`) | **shipped v2.70.0** | `services/override_requests.py`, `cli/tools/override.py`, refusal offer line, `c3 override requests\|approve\|deny` |
| **P2a grants on the `c3_*` surfaces** | **shipped v2.72.0** | `cli/tools/_grants.py` (one gate, one session id), wired into `c3_read`, `c3_edit`, `c3_compress`, `c3_filter`, `c3_impact`, `c3_validate`; `tests/test_override_grants_mcp.py` |
| P3 Oracle routes | **shipped v2.71.0** | `oracle/services/mobile_api.py` (6 routes + 2 capabilities), `oracle/config.py` switches, mute store in `services/override_requests.py` |
| P4 mobile Requests pane | **built — awaiting live end-to-end** | separate repo `c3-mobile` (local, no remote): `50f0c49` + `5c1769b` — `src/api/{types,queries,mutations}.ts`, `src/components/guard/overrides.tsx`, `src/notifications/{routing,route-map}.ts`, 43 node:test cases. arm64 release APK delivered 2026-08-07 |
| **P4a wake on decide + long-poll delivery** | **shipped v2.73.0** | `services/override_wake.py`, `override.wake` policy key, `/api/mobile/feed?wait=` (`feed_wait` capability, api_version 3), `c3-mobile` live-push loop |
| P5 desktop parity | **approval surface shipped v2.98.0** (Hub Access tab + `/api/hub/overrides` routes; layers Settings UI + cost nudge still open) | `cli/hub_server.py`, `cli/hub_ui/components/hub_access.js`, `tests/test_hub_override_routes.py` |

**Resolved deviation (P1 → P2).** §10's `c3 override approve <id>` / `deny
<id>` needed the request store; both shipped in P2 alongside `requests`. The
P1 grant-centric verbs (`grant`, `check`, `revoke`, `sweep`) remain — they are
the surface for approving something no agent asked for.

**Gap found while building P2, recorded rather than papered over.** §13 claims
grants are honoured on the `c3_*` MCP tools. They are not: P1 wired the gate
into the two PreToolUse hooks only, so an approved grant unblocks native
`Read`/`Edit` but not `c3_read`/`c3_edit`. Until P2a closes that, the refusal
offer line is emitted on the **hook surface only** — an offer that promises a
human "yes" will work must not appear where the retry would still refuse.

**Deviations recorded while building P3, not papered over.**

1. **Test file name.** §14 names `tests/test_override_routes.py`; the file
   shipped as **`tests/test_mobile_override_routes.py`**, matching the
   surrounding `test_mobile_*` convention. These are the *mobile* gateway's
   routes; the desktop dashboard routes (P5) will want the unqualified name.
2. **The mute store is a new file, not a new key.** §8 asks for "deny +
   suppress identical requests for this session" and §3.3 freezes
   `override_requests.json` as a JSON array of request rows. Those cannot both
   hold, so mutes live in a sibling **`~/.c3/oracle/override_mutes.json`**,
   written by the same load-all/mutate/save-all helper. Suppression key is the
   tuple `create()` already used for duplicate detection —
   `(project, session, layer, rule, tool, op, path_key)` — so mute is
   duplicate suppression that outlives the pending row. It fails **open**
   (corrupt ⇒ the agent may ask again), deliberately the opposite of the grant
   store's fail-closed read: a lost mute costs one notification, a lost-open
   grant would be a capability.
3. **Session grants needed a second challenge.** §8 lists `mode: 'session'` as
   requiring confirmation but does not say with what. Reusing the rule glob
   would let one set of keystrokes answer two different questions ("I accept
   this access layer" and "I accept an unlimited-uses grant"), so a session
   grant on a non-typed-confirm layer challenges with the literal string
   `session`. On an `access_deny`/`access_builtin` layer the rule glob still
   governs — one typed confirmation per tap, never two.
4. **Policy writes are project-scope only.** §3.1 allows a global `override`
   section, but `POST /overrides/policy` writes the project's
   `.c3/config.json` and nothing else — the same call `mobile_enforcement_set`
   makes about machine-wide discipline. A phone has no affordance for
   reviewing a change that re-governs every project on the machine.
5. **`decide()` did not notify.** P2's `create()` appended to the feed but
   `decide()` did not, so the feed showed questions and never answers. §8
   requires the decision entry, so `_notify_decision` was added to the service
   (not the route) — the CLI gets it too.

**Addition (P1).** `override_grants.json` and `overrides.jsonl` join the
never-writable target list alongside the vault files: an approved `**/.c3/**`
write must not be convertible into the agent minting its own grants. §11
threat 3 argued this from `BUILTIN_WRITE_DENY`; P1 also enforces it at the
grant layer, so it holds even if that builtin is ever switched off.

**Deviation recorded 2026-09-06 (C3 Desk D0a, v2.125.0) — `decided_by` gains
a fourth value, and the gateway gains per-client tokens.** §3.3 freezes
`decided_by` as `"mobile"|"desktop"|"cli"`, and §8 hard-coded `"mobile"` on
the gateway routes because the phone was the gateway's only client. The
desk tray client (C3 Desk) now decides through the same `/api/mobile`
routes, and auditing its taps as a phone would be false. So:

1. **`decided_by` is the authenticated principal's kind**, never a body
   field: `"desk"` for a request bearing a `desk` client token, `"mobile"`
   for a `mobile` client token *or* the legacy Discovery token (every phone
   paired before this release keeps its attribution byte-for-byte). Hub
   stays `"desktop"`, the CLI stays `"cli"`. The row schema, field order and
   allowlist serialiser are untouched.
2. **Per-client tokens** (`oracle/services/client_tokens.py`,
   `~/.c3/oracle/clients.json`, hashes only, owner-only ACL). A gateway
   request authenticates with the Discovery token OR a live client token;
   the principal rides on `flask.g.c3_client = {kind, client_id}`. Routes:
   `POST /api/mobile/clients` (no Bearer — local address + the on-disk
   bootstrap key, constant-time, security-bucket throttled; 201 with the
   token exactly once), `GET /api/mobile/clients` (rows without hashes,
   `current` marks the caller), `DELETE /api/mobile/clients/<id>` (revoke;
   the token fails on its next request). `/info` reports `client` and the
   `clients` capability; `api_version` is 5. The Settings-tab QR now carries
   a per-device `mobile` token instead of the Discovery token, so rotating
   the Discovery token no longer un-pairs every phone.
3. **Loopback listener.** A `bind_host` naming a specific non-loopback
   address (the Tailscale IP) also serves the same app on `127.0.0.1`
   (`loopback_listener`, default true; `oracle/listeners.py`), so the desk
   client and `curl 127.0.0.1:3331` are no longer refused on the Oracle's
   own machine. Same app object, same Host allowlist and Bearer gates.
4. **Long-poll budget** (§7.1 / P4a): `_MAX_WAITERS` 4 → 8 with a per-kind
   cap of 4, counted on the principal's kind, so a phone cannot take the
   desk's slots or vice versa. Past either cap `wait` still degrades to an
   immediate answer.

Tests: `tests/test_mobile_clients.py`, `tests/test_oracle_loopback.py`, and
the waiter-cap cases in `tests/test_mobile_api.py`.

**Deviation (2026-09-06, v2.126.0, C3 Desk D0b): `channel` is consumed by
the desktop client as of 2.126.0.** §3.1's `override.channel`
(`mobile | desktop | both`) was validated, merged (last scope with an
opinion wins) and printed by `c3 override policy`, and consumed by nothing.
The desktop tray client now reads it to decide how a request notification is
presented — `desktop` / `both` → toast, `mobile` → popover only. To reach it
the gateway exposes the value in three places, all additive to the §3.3
contract: a `channel` field on every row of `GET /api/mobile/overrides`
(resolved once per distinct project in the page) and on the `request` object
of `GET /api/mobile/overrides/<id>`, and a top-level `channel` on
`GET /api/mobile/overrides/policy` beside the `policy.channel` it already
carried. The phone ignores the field; §9 is unchanged. Nothing about the
merge or the default (`mobile`) moved.

Companion specs: `access-guard.md`, `mask-guard.md`, `agent-locks.md`.

---

## 0. The problem, precisely

C3 blocks in seven places (§13). Every one of those blocks is **terminal**, and
every un-block is a human sitting at the desktop typing `c3 enforce advisory`
or `c3 access remove`. There is no third option today — the code was checked
and no allow-once, lease, escalation, or approval path exists anywhere in the
repo.

Two bad outcomes follow:

1. The agent stalls until the user is back at a keyboard. Away-from-desk work
   (which is the entire reason c3-mobile exists) dies at the first denial.
2. Worse, and more common: to avoid (1) the user pre-loosens policy —
   `enforcement.mode = advisory` globally, `access.deny` rules deleted rather
   than suspended. A guard that spends its life in `advisory` is not a guard.

Override Requests add the missing third option: **the agent asks, the phone
answers, and the answer is a narrow, time-boxed, single-use grant — not a
policy change.** The rule survives the grant.

Non-goal: this is not a way to make C3 permissive, and it is not containment.
Same posture as Access Guard — a cooperative mistake/prompt-injection guard.
An agent with a raw shell on the same machine is out of scope (§13).

---

## 1. Vocabulary

| Term | Meaning |
|---|---|
| **Denial** | Existing. A blocking layer refuses a tool call and returns a refusal string. |
| **Request** | New. An agent-initiated ask carrying the denial's identity plus a justification. Has no power. |
| **Grant** | New. A user-approved token that makes **one** retry of **one** tool call on **one** path succeed, once, soon. |
| **Escalatable** | A layer the user has explicitly opted into. Default: nothing is escalatable. |

The load-bearing distinction: a Request is a message, a Grant is a capability.
Creating a Request is cheap and agent-initiated. Creating a Grant is expensive
and human-only.

---

## 2. Which layers are escalatable

| Layer | Denial source | Escalatable? | Default |
|---|---|---|---|
| **Discipline** (native write before `c3_*`) | `hook_pretool_enforce` | yes | **off** — the honest fix is to call `c3_edit` |
| **Access `read_only`** | `hook_access_guard` / `access_guard.check` | yes | **off** |
| **Access `deny`** (user/global rules) | same | yes, typed confirm required | **off** |
| **Access builtins, Tier 1** (`*.pem`, `**/.env*`, `**/.git/**`, …) | same | yes, typed confirm required | **off** |
| **Mask read-only / unsupported surface** | `access_guard` mask verdict | yes, read-only lift only | **off** |
| **`c3_shell` soft-warn** | `cli/tools/shell.py` | yes | **off** |
| **`c3_shell` catastrophic block** | `cli/tools/shell.py` `_BLOCKED` | **never** | — |
| **Vault write guard** (`.c3/config.json`, `secrets.enc`, `cred_state.json`) | `hook_pretool_enforce._vault_denial` | **never** | — |
| **Access Tier-0 absolute deny** (`**/.c3/secrets.enc`, `**/.c3/cred_state.json`) | `access_guard.BUILTIN_ABSOLUTE_DENY` | **never** | — |
| **Credential `agent_readable` gate** | `credential_store.verify_agent_readable` | **never** | — |
| **Dispatcher fail-closed deny** | `hook_dispatch._fail_closed_deny` | **never** | — |
| **`c3_project` `allow_write`** | cross-project tool | **never** | — |

The "never" rows are hardcoded. **No config value, no grant, and no approval
can reach them** — a request against one of them is refused at creation time
with `[c3-override:not-escalatable]` and never reaches the phone. That
constraint exists so the phone can never become a one-tap path to the
credential vault.

---

## 3. Schema

### 3.1 Policy — project `.c3/config.json`, new top-level `override` section

```json
{
  "override": {
    "enabled": false,
    "channel": "mobile",
    "layers": {
      "discipline":     false,
      "access_readonly": false,
      "access_deny":     false,
      "access_builtin":  false,
      "mask":            false,
      "shell_warn":      false
    },
    "max_ttl_s": 900,
    "default_uses": 1,
    "request_ttl_s": 600,
    "max_pending_per_session": 3,
    "max_requests_per_hour": 20,
    "notify_severity": "critical",
    "allow_session_grants": false,
    "allow_rule_grants": false,
    "rule_grant_ttl_s": 14400,
    "rule_grant_idle_s": 1800,
    "wake": null
  }
}
```

`allow_rule_grants`, `rule_grant_ttl_s` and `rule_grant_idle_s` are §4.1.
All three are widenings: the boolean AND-merges like `allow_session_grants`,
and both integers min-merge and clamp to `HARD_MAX_RULE_TTL_S` /
`HARD_MAX_RULE_IDLE_S`. A longer TTL or a longer idle window means a rule
grant outlives more of the conversation, so the mobile policy route counts
either as a loosening and demands the same challenge.

`wake` is §7.1. `null` (the default) means nobody is told when a request is
decided — see there for why that was the bug and not the design.

Rules, mirroring `access`:

- **Unknown keys are a hard config error.** (`access-guard.md` §1 already
  reserves this behaviour so a future grant schema can never be silently
  no-op'd by an old C3. This is that schema; it lives in its own top-level
  section, *not* under `access`, because `access` is frozen at exactly two
  keys.)
- Global `~/.c3/config.json` may set the same section. **Project and global
  merge by tightening only**: a layer is escalatable iff *both* scopes allow
  it; `max_ttl_s` / `default_uses` / rate limits take the *smaller* value.
  A project can never widen what global forbids.
- Corrupt/unparseable `override` section ⇒ the feature evaluates as
  `enabled: false` with a loud warning. Fail-closed.
- Mutations are **human-only** (desktop Settings UI, `c3 override` CLI, or the
  mobile Guard tab if `override_write` is granted). No agent-facing mutation
  surface — same as `access`. `.c3/**` is already in `BUILTIN_WRITE_DENY`, so
  the agent cannot edit this file even with native tools.

### 3.2 Oracle switches — `~/.c3/oracle/config.json`

```json
{
  "mobile_override_enabled": false,
  "mobile_override_write":   false
}
```

Gate capabilities `override` (read pending list) and `override_write` (decide).
Follows the existing `mobile_access_enabled` / `mobile_access_write` pattern in
`oracle/config.py`, and disabled ⇒ **404**, not 403, so old clients and
switched-off features take the same code path.

### 3.3 Requests — `~/.c3/oracle/override_requests.json`

Oracle-owned, JSON array, house style of `oracle/services/memory_writer.py`
(`suggestions.json`): load-all / mutate / save-all, `indent=2`.

```jsonc
{
  "id": "ovr_9f3c1a20b4d7",
  "project_path": "U:/1. Projects/.../c3-mobile",
  "session_id": "abc123",                // Claude Code session that asked
  "created_at": "2026-08-07T20:41:03Z",
  "expires_at": "2026-08-07T20:51:03Z",  // request_ttl_s
  "status": "pending",                   // pending|approved|denied|expired|withdrawn
  "layer": "access",                     // access|discipline|mask|shell
  "rule": "**/.env*",                    // exact rule token that denied
  "rule_class": "access_builtin",        // maps to override.layers key
  "scope": "builtin",
  "tool": "Read",
  "op": "read",
  "path": "U:/.../.env.local",           // access_guard.canonicalize output
  "refusal": "[c3-access:denied] ...",   // verbatim denial the agent received
  "justification": "…agent-supplied…",   // UNTRUSTED, ≤400 chars, rendered quoted
  "resolved_at": null,
  "decided_by": null,                    // "mobile"|"desktop"|"cli"|"desk" (desk added v2.125.0, see deviation at top)
  "decision_note": null
}
```

`justification` is **agent-supplied and therefore untrusted input**. It is
rendered on the phone inside a quoted block under the label *"The agent wrote
this. It may be repeating text it read from a file."* It is never parsed, never
used for matching, and never interpolated into a shell or path.

### 3.4 Grants — `.c3/override_grants.json` (project-local runtime state)

Deliberately **not** config. Ephemeral, gitignored with the rest of `.c3/`,
written with the same atomic-write helper as `enforcement_state.json`
(`cli/_hook_utils`).

```jsonc
{
  "grants": [
    {
      "id": "grt_44a1…",
      "request_id": "ovr_9f3c1a20b4d7",
      "session_id": "abc123",
      "layer": "access",
      "rule": "**/.env*",
      "tool": "Read",
      "op": "read",
      "path_key": "<canonical_key(path)>",
      "expires_at": "2026-08-07T20:56:03Z",
      "uses_remaining": 1,
      "granted_at": "2026-08-07T20:41:44Z",
      "granted_by": "mobile"
    }
  ]
}
```

Fail-closed: unparseable file ⇒ **zero grants**, everything stays denied, warning
emitted via `_hook_utils.drain_state_warnings()`.

Writers are privileged internal services (Oracle, `c3 override` CLI) using the
same tool-layer bypass as `credential_store` — the audit/state writer never
consults the evaluator.

### 3.5 Audit — `.c3/overrides.jsonl`

Append-only, rotated at 512 KB like `denials.jsonl`. One line per lifecycle
event: `requested`, `approved`, `denied`, `expired`, `consumed`,
`consumed_after_expiry_attempt`. Never deletable from any agent surface. A
grant that is never used still leaves `requested` + `approved` + `expired`.

---

## 4. Grant matching (the whole security surface in one place)

A grant authorises a retry **iff all of these hold**:

1. `project_path` identical,
2. `session_id` identical (a grant never crosses sessions),
3. `layer` identical,
4. `rule` string-identical to the rule that is about to deny,
5. `tool` identical (exact tool name, not a class),
6. `op` identical,
7. `path_key == access_guard.canonicalize(target)` — the *canonical* key, so
   `.\src\..\.env` and `.env` are the same grant and neither is a new one,
8. `now < expires_at`,
9. `uses_remaining > 0`.

Any mismatch ⇒ ordinary denial, and the near-miss is recorded so the phone can
show *"the agent retried with a different path than you approved."*

**Consumption is atomic and happens at ALLOW time in the PreToolUse hook**
(read-modify-write under the existing atomic helper). If the tool then fails
for an unrelated reason the use is burned; re-requesting is cheap and this is
strictly safer than consuming in PostToolUse, where a crash would leave a live
grant behind.

---

## 4.1 Grant scope — `call` and `rule` (v2.118.0)

§4 above describes a `scope="call"` grant, which is still the default and
still what every grant minted before v2.118.0 is read as (a missing `scope`
field means `call`).

The problem it left: an approval covers **one file**. A confirm rule over
`**/.mcp.json`-class paths, or a `deny` over a whole directory, produces one
card per file, and a user working through five of them taps five times for a
decision they made once. The `session` *mode* did not solve this — it lifts
the use count, not the path binding.

`scope="rule"` is the shape that does. It relaxes **exactly two** of the nine
conditions:

- **5 (tool)** — from the exact tool to a declared **op-class**
  (`override_policy.TOOL_CLASSES` / `same_tool_class`). Exact match always
  qualifies; beyond that both tools must resolve to the same declared class.
  A tool in no class is never widened, so an unrecognised or future tool
  cannot inherit an existing grant. `c3_artifacts` (restore writes a whole
  prior version) and `c3_project` (writes into a *different* project) are
  deliberately unclassed.
- **7 (path)** — from the exact canon key to *"inside the path set the rule
  glob describes"*, evaluated by `override_grants.rule_covers` against the
  **same** `(canon, rel)` pair and the **same** compiler
  (`access_guard._compile`) that produced the denial. A rule grant therefore
  covers exactly the files that rule would have blocked — never one more.

Everything else is unchanged, and three things replace what it drops:

- **`uses_remaining` is `None`** (unlimited). `HARD_MAX_USES` does not apply.
- **Its own TTL ceiling.** `rule_grant_ttl_s` (default 4 h, hard ceiling
  `HARD_MAX_RULE_TTL_S` = 8 h) instead of `max_ttl_s` / `HARD_MAX_TTL_S`. A
  `call` grant keeps its 15-minute ceiling untouched.
- **An idle window.** `idle_s` (default 30 min, ceiling
  `HARD_MAX_RULE_IDLE_S` = 1 h) measured from `last_used_at`, else
  `granted_at`. An unreadable or absent-but-required window reads as expired,
  never as eternal.

The idle window exists because **there is no session-END signal to hang a
long grant on.** The MCP surface's `host_session_id` is snapshotted once at
`start_session()` and goes stale across `/clear`, while the hook surface
reads the live payload field — so after `/clear` a long grant would keep
matching MCP-surface calls in what is, to the user, a new conversation. A
15-minute grant made that harmless; a 4-hour one would not. Rather than
rewrite session identity (a shared `.c3/session.host` file is
last-writer-wins across two windows in the same project — worse than the
problem), a rule grant dies on its own the moment the conversation stops
exercising it.

**What still cannot be reached.** The widened path condition is the only
route by which a grant could grow toward the vault or the policy files, and
it does not: `find()` and `consume()` refuse `override_policy.forbidden_target()`
on **every call**, not only at mint. So a rule grant over `**/.c3/**` covers
`.c3/notes.txt` and never `override_grants.json`, `overrides.jsonl`,
`config.json`, `secrets.enc` or `cred_state.json` (§11 threat 3). Tier-0
absolute denies never reach the store at all.

**Minting one requires all of:** `override.allow_rule_grants` (default
`false`, AND-merged across scopes and counted as a *widening* policy edit),
a **globbable** rule — the synthetic `<discipline:native-write-block>` and
`<shell:soft-warn>` tokens are refused, they name a behaviour and have no
path set — a rule that actually covers the refusal being approved, and the
**rule glob retyped by hand** on *every* layer, including `access_confirm`,
which stays one-tap for `once`.

**It must be visible and killable.** `GET /api/hub/grants` and the Access
tab's *Active grants* strip list live grants with their real reach;
`DELETE /api/hub/grants/<id>` and `c3 override revoke <id>` end one
immediately. `granted_context()` says *"covers every path &lt;rule&gt; matches,
this session"* rather than a use count, so a standing capability never reads
in the transcript like a single approval.

**Near-misses are scoped too:** a rule grant is *supposed* to span tools and
paths, so those differences are not reported for it — only a rule or op
mismatch is, which keeps the "you approved X, the agent tried Y" signal out
of the noise the user deliberately allowed.

---

## 5. Gate placement

Order matters: `hook_dispatch._routes()` runs `hook_access_guard` **before**
`hook_pretool_enforce`, and deny beats allow in the merge. So the grant check
must be inserted in **both**, each checking only its own layer:

- `cli/hook_access_guard.py` — after `access_guard.check()` returns a `Denial`,
  before `_deny()`: consult grants for `layer="access"` / `layer="mask"`.
  Tier-0 and vault rules skip the lookup entirely.
- `cli/hook_pretool_enforce.py` — after the strict-mode write-block decision,
  before `_deny()`: consult grants for `layer="discipline"`. **After**
  `_vault_denial()`, which stays unconditional.
- `cli/tools/shell.py` — soft-warn suppression only; `_BLOCKED` never consults
  grants.

Cost: one extra small-JSON read per PreToolUse event, in the same subprocess
that already reads `enforcement_state.json`. Short-circuit before the read when
`override.enabled` is false — which is the default — so the hot path for every
existing user is one dict lookup.

An allowed-by-grant call emits `additionalContext`:
`[c3-override:granted] Allowed by override grt_44a1 (approved on mobile 20:41Z, 0 uses left). The rule **/.env* is still in force.`

---

## 6. Refusal contract

Existing refusal strings gain **one** appended line, and only when the layer is
escalatable and `override.enabled` is true:

```
[c3-access:denied] Reading `.env.local` is blocked by rule `**/.env*` (builtin).
This is a policy decision, not a transient error — do not route around it.
[c3-override] You may ask the user to allow this once:
  c3_override(action='request', path='.env.local', tool='Read', op='read',
              why='<one sentence, concrete>')
```

When the layer is *not* escalatable the line is **absent** — an agent must
never learn that a request surface exists for the vault. Silence beats
`[c3-override:not-escalatable]` in the refusal, which is reserved for the
`c3_override` tool's own response when an agent guesses.

---

## 7. Agent surface — `c3_override` (new MCP tool)

| action | Effect |
|---|---|
| `request` | Create a request. Returns `{id, status:'pending', expires_at}`. Refuses non-escalatable layers, refuses if `override.enabled` false, refuses over rate limits. |
| `status` | Poll one request by id. |
| `wait` | Block **inside the MCP server** (long-running process — safe) polling every 1 s, `timeout_s` clamped to ≤180. Returns on decision or timeout. |
| `list` | The agent's own pending requests for this session. |
| `withdraw` | Cancel its own pending request (e.g. it found another way). |

There is **no `approve` action, and never will be.** The tool is registered in
the MCP surface the agent already has; the decide route is not.

**Rejected design: blocking the PreToolUse hook until the phone answers.**
Claude Code's per-hook `timeout` field is generous enough to make it
technically possible ([hooks reference](https://docs.claude.com/en/docs/claude-code/hooks)),
but a blocked hook freezes the whole turn with no spinner, no explanation and
no cancel. `c3_override(action='wait')` gets the same UX from a process that is
allowed to be slow.

Rate limits enforced at `request`: `max_pending_per_session` (3),
`max_requests_per_hour` per project (20), and **duplicate suppression** — an
identical `(layer, rule, tool, op, path_key)` while one is pending returns the
existing id rather than minting a second card.

### 7.1 Wake on decide — `override.wake` (shipped v2.73.0)

**The gap this closes, measured.** 2026-08-08, live run: an agent asked at
00:36Z, ended its turn, the user approved from the phone at 00:42:32Z, and the
grant expired unused at 00:57:32Z. Both halves worked and nothing connected
them. Before this, the only ways an agent learned of a decision were `wait`
(≤180 s, and gone the moment the turn ends) or `status` (which requires
something to have already woken it). Grants are capped at 900 s, so an idle
agent misses the window by construction, and the user's tap looks broken while
being entirely correct.

On every decision — approve **and** deny — C3 runs one configured command:

```json
"wake": {
  "command": ["python", "-m", "my_agent.notify", "{message}"],
  "cwd": "/path/to/the/orchestrator",
  "timeout_s": 10,
  "on": ["approved", "denied"]
}
```

Placeholders, substituted per argv element: `{request_id}` `{session_id}`
`{status}` `{decided_by}` `{tool}` `{op}` `{path}` `{path_key}` `{rule}`
`{rule_class}` `{layer}` `{grant_id}` `{project}` `{project_name}`
`{message}`. `{message}` is the pre-built line for the agent — it names the
decision, the grant, and exactly one next step ("retry the SAME call once").

**Why a command and not an integration.** C3 has no idea what runs the agent —
a chat daemon, a queue, a webhook. One argv covers all three; a backend per
orchestrator would rot in this repo.

Four constraints, each load-bearing:

- **argv, never a shell string.** `command` must be a list; there is no
  `shell=True` and no string form. A placeholder carrying a quote or a
  semicolon is an argument, not syntax. Substitution happens per element after
  the list is fixed, so it can never add an argument.
- **Config-only, never remote.** Set it in `.c3/config.json` on the desktop.
  `POST /api/mobile/overrides/policy` returns **403** for this key, `widen`
  confirmation included: a bearer token is authentication, not physical
  presence. Agents cannot write the file either (`**/.c3/**` is builtin
  read-only, and `forbidden_target` refuses to let any grant cover it), so it
  cannot be self-approved.
- **Synchronous, bounded.** `timeout_s` defaults to 10, hard max 60.
  Backgrounding it would look kinder and silently lose every wake: `c3
  override approve` exits the instant `decide()` returns.
- **A wake that fails never unwinds a decision.** Exit code, timeout and
  missing binary all land in `.c3/overrides.jsonl` as `wake_failed`; the grant
  stands and `action='status'` still works. A wake is a shortcut past waiting,
  not the mechanism.

A `wake` section that does not validate makes the **whole `override` section**
read as corrupt — i.e. the feature disables itself loudly. Degrading a typo
into "no wake, carry on" would restore the exact silence this exists to fix.

`GET /overrides/policy` reports `wake_configured: true|false` and never the
argv, which can name a conversation id or a path a policy screen has no reason
to see.

---

## 8. Oracle surface

**Shipped v2.71.0.** Blueprint routes under the existing `/api/mobile` prefix
(`oracle/services/mobile_api.py`), Bearer-auth on every method including GET,
`_security_gate()` rate budget on the mutating ones:

| Route | Purpose |
|---|---|
| `GET /api/mobile/overrides` | Pending + recently-decided, newest first. Params `project` (optional — omit for all projects), `status`, `limit`. |
| `GET /api/mobile/overrides/<id>` | One request, full context. |
| `POST /api/mobile/overrides/<id>/decide` | `{decision: 'approve'\|'deny', mode?: 'once'\|'session', uses?, ttl_s?, note?, confirm?}` |
| `POST /api/mobile/overrides/<id>/mute` | Deny + suppress identical requests for this session. |
| `GET /api/mobile/overrides/policy` | The effective `override` section for a project (read-only view of §3.1). |
| `POST /api/mobile/overrides/policy` | Edit it, gated on `override_write` + typed confirm for any widening. |

Capabilities added to `mobile_api.CAPABILITIES`: `override`, `override_write`,
mapped to `mobile_override_enabled` / `mobile_override_write`.

Confirmation follows the existing challenge protocol — the server answers
`{needs_confirmation: true, confirm_with: "<string>"}` and the client re-sends
with `confirm`. Required for:

- approving any `access_deny` / `access_builtin` request — challenge is the
  **rule glob itself**, typed by hand;
- `mode: 'session'` grants (only if `allow_session_grants` is true);
- widening `override.layers` in the policy route.

`ttl_s` is clamped server-side to `min(requested, override.max_ttl_s)`. A client
asking for a week gets 15 minutes and is told so — the approve response carries
`clamped: true` and a `clamped_note` naming the ceiling that did it, because a
silent clamp leaves the phone displaying a grant that does not exist.

On approval the Oracle: writes the grant to `.c3/override_grants.json`,
appends to `.c3/overrides.jsonl`, flips request status, and appends an
acknowledgeable entry to `.c3/notifications.jsonl` so the existing feed shows
the decision.

**As built (v2.71.0).** Request rows are serialised through an explicit field
allowlist — the §3.3 names exactly — rather than the raw dict, so an internal
field added later cannot silently start crossing the network. `path_key` is
withheld for that reason: it is a canonical local-filesystem identity used for
grant matching, and the client already has the human-readable `path`. The
cross-project listing additionally drops rows whose project the gateway's
scanner does not serve; the store is one file for the whole machine, and a
token's reach must not exceed the projects it can enumerate.

See the deviation list at the top of this document for the four places the
implementation had to decide something §8 left open.

---

## 9. Mobile surface

**Placement:** a fourth segment in the existing Guard tab — `Credentials |
Access | Discipline | **Requests**` — gated on `useCapability(CAP_OVERRIDE)`.
Not a new tab: it inherits the project picker, and an approval inbox that is
empty 95% of the time does not earn permanent bottom-bar real estate. A count
badge on the Guard tab icon carries the urgency.

**Data:** `useOverrides()` in `src/api/queries.ts`, key
`['overrides', baseUrl, project, status]`, `refetchInterval` tied to
`feedIntervalMs` (default 15 s) — pending approvals are the one thing in the
app where polling latency is felt.

**Delivery latency (v2.73.0).** `GET /api/mobile/feed` accepts `wait`
(seconds, 0–30, requires `since` and no `before`) and holds the request open
until something matches — advertised as the `feed_wait` capability. The app
keeps one such request in flight while it is alive, so a request lands in
about a second instead of on the next 15 s tick, and the local notification
fires from the same path the background poll uses. Server-side the hold
watches `.c3` mtimes and only rebuilds the feed when one moves; waiters are
capped (4) and degrade to an immediate answer past the cap.

**This is not push, and the distinction matters.** A process Android has
frozen cannot hold a socket, so with the app killed the floor is still
WorkManager's ~15 minutes. That is why `request_ttl_s` must stay well above
it — 3600 on this box. Closing the closed-app gap needs FCM (a Firebase
project + credentials) or an Android foreground service; both are real work,
neither is in v2.73.0.

**Card** (reuse `Card` + left-border pattern from `feed.tsx` / `denials.tsx`):

```
┌───────────────────────────────────────────┐
│ c3-mobile · 40s ago            [ACCESS]   │
│ Read  .env.local                          │
│ blocked by  **/.env*   (builtin)          │
│                                           │
│ ❝ Need the Supabase URL to wire the       │
│   pairing test. ❞                         │
│   ↳ the agent wrote this; it may be       │
│     repeating text from a file            │
│                                           │
│ [ Approve once ]  [ Deny ]  [ Deny+mute ] │
│  Fix it properly →                        │
└───────────────────────────────────────────┘
```

- **Approve once** — `ConfirmSheet` `mode='typed'`, `challenge=<rule glob>`,
  `weakens` for access layers; `mode='simple'` for discipline/shell-warn.
- **Deny** / **Deny + mute** — no confirmation, instant, no penalty for reflex.
  Deny must always be the cheaper gesture than approve.
- **Fix it properly →** — deep-links to the existing Access or Discipline pane
  so the answer to a recurring request is a policy edit, not a habit.
- Expired/decided requests grey out in place with the outcome, then drop from
  the list on next refresh.

**Notification:** the request writes to `.c3/notifications.jsonl` at
`notify_severity` (default `critical`), so it rides the existing feed →
`pollOnce()` → `Notifications.scheduleNotificationAsync()` path in
`src/notifications/background.ts` with `data: {kind:'override', id}`, and the
tap handler routes to the Requests segment. **Known latency:** foreground ≈ the
poll interval; Android background is floored at 15 min by WorkManager. v1
accepts this and pairs it with `c3_override(action='wait')` so the agent parks
for ≤3 min and then moves on rather than idling. v2 options in §15.

---

## 10. Desktop surface

Parity is required, not optional — the desktop is where the user is when the
phone is charging.

- `c3 override list` / `approve <id> [--ttl 300] [--uses 1]` / `deny <id>` /
  `policy` — full CLI.
- Oracle dashboard: a Requests card on the project view using the same routes.
- Both are authorised by the existing dashboard session cookie / bootstrap
  flow; neither is reachable from an MCP tool.

---

## 11. Security analysis

**Threat 1 — prompt injection farms approvals.** A hostile repo tells the agent
to request `**/.env*`, the user taps approve out of habit, secrets leave.
Mitigations: Tier-0 unreachable at any price; every access-layer approval needs
the rule glob **typed by hand** (`ConfirmSheet mode='typed'` already exists and
is already used for `enforcement mode=off`); the justification is labelled
untrusted; deny is one tap and approve is four; rate limits and duplicate
suppression cap the spam; `overrides.jsonl` is permanent.

**Threat 2 — grant reuse.** Mitigated by single-use + TTL ≤ 15 min +
session-binding + exact canonical path match (§4). The approved path is shown
on the card in full; a retry against any other path is a fresh request.

**Threat 3 — the agent grants itself.** `.c3/**` is in `BUILTIN_WRITE_DENY`, so
neither native tools nor `c3_edit` can write `override_grants.json`;
`c3_override` has no approve action; the decide route needs the mobile Bearer
token (phone secure store) or the desktop session cookie.

**Threat 4 — an agent with a shell.** It can read the Oracle API key from the
OS keyring and self-approve. **This is not defended against, by design** —
identical to the Access Guard posture (`access-guard.md`: cooperative guard,
not containment). Say it out loud in the docs rather than implying a boundary
that isn't there.

**Threat 5 — approval fatigue.** The real one. Defaults are `enabled: false`
and every layer `false`, so the feature does not exist until the user turns on
exactly the layer that is actually costing them. "Fix it properly" is on every
card. If a rule generates more than N requests per week the Requests pane
surfaces it as *"this rule is costing you — edit it or accept it."*

---

## 12. Failure modes to design for

1. **Grants file corrupt** ⇒ zero grants, everything denied, loud warning.
2. **Oracle not running** when the agent requests ⇒ `c3_override(request)`
   writes the request file directly (same on-disk store) and returns
   `{delivered: false}`; the phone sees it whenever the Oracle comes back. The
   agent is told delivery failed and should ask in chat instead.
3. **Approval arrives after the agent moved on** ⇒ grant sits until TTL, is
   never consumed, expires, `overrides.jsonl` records `expired`. Never an
   error.
4. **Two sessions blocked on the same rule** ⇒ two requests, session-bound
   grants, no cross-talk. The phone groups them visually by rule.
5. **Clock skew** ⇒ not a threat; requester, grantor and consumer are all the
   same machine's clock.
6. **User approves, then the agent's plan changed** ⇒ near-miss is logged and
   surfaced: *"approved Read on X, agent then tried Write on Y."*
7. **Request expires while the phone is showing it** ⇒ decide route returns
   409 with the current status; the card refreshes to `expired` rather than
   silently minting a grant.
8. **`override.enabled` flipped off while grants are live** ⇒ live grants are
   voided immediately; the check reads policy before grants.

---

## 13. Coverage matrix

| Surface | Denial enforced | Grant honoured |
|---|---|---|
| Claude Code native tools (hooks installed) | yes | yes |
| `c3_*` MCP tools | yes | **yes (v2.72.0).** All six content tools consult `cli/tools/_grants.allow` before refusing, which delegates to the same `override_grants.gate_access` the hooks call — policy first, grants second. **Masked paths are excluded on purpose:** a mask is not a refusal to be lifted but a different view being served, so "approve once" has no meaning for it; only the `denial` branch of `verdict()` consults a grant. |
| Codex (hooks wired manually) | yes — the dispatcher speaks Codex's wire format as of v2.78.0 | yes, same path as Claude Code |
| IDEs without hooks installed (Codex out of the box, Antigravity, VS Code, Cursor) | discipline is advisory only today | n/a — nothing to override |
| Raw shell / direct file API | **no** | **no** |
| `c3_project` cross-project writes | `allow_write` unchanged | never |

Unchanged from Access Guard: the guard covers cooperative surfaces. Anything
that can bypass the denial does not need the grant.

---

## 14. Phasing

**P0 — Spike (½ day).** Measure the added PreToolUse cost of one extra JSON
read on Windows; confirm the short-circuit when `override.enabled` is false is
free. Confirm `_hook_utils` atomic write survives two hook subprocesses racing
a grant consumption. Gate: numbers in the PR body.

**P1 — Grant primitive, no UI.** `services/override_grants.py` (schema,
matching §4, atomic consume, audit), `override` config section + tightening
merge + hard-error on unknown keys, gate insertion in `hook_access_guard` and
`hook_pretool_enforce`, `c3 override` CLI as the only approval path. Tests:
grant matching truth table (all 9 conditions, each negated), Tier-0/vault
unreachable, corrupt-file fail-closed, single-use consumption under a simulated
race, TTL expiry, session isolation.

**P2 — Agent surface.** `c3_override` MCP tool (`request`/`status`/`wait`/
`list`/`withdraw`), refusal-string append, rate limits + duplicate suppression.
Tests: no `approve` action exists; non-escalatable layers refused at creation;
refusal line absent for non-escalatable layers.

**P3 — Oracle. Shipped v2.71.0.** Routes, capabilities, config switches,
notification emission, typed-confirm challenges, TTL clamping. Tests in
`tests/test_mobile_override_routes.py` (renamed — see deviation 1), including
the endpoint sweep proving no route ever mints a grant without a valid decide
call, and a wire-contract test pinning the §3.3 field names the mobile client
reads.

**Ship gate not yet met.** The suite is green, but the live end-to-end run —
request from a real blocked call, approve on the real phone, watch the retry
succeed — needs P4's client. Until then P3 is verified only against Flask's
test client.

**P4 — Mobile.** `CAP_OVERRIDE` constants, `useOverrides` / `useDecideOverride`,
Requests segment, badge, notification tap-through, "Fix it properly" deep-link.

**P5 — Desktop parity + polish.** Dashboard Requests card, Settings UI for
`override.layers`, the "this rule is costing you" nudge, optional session
grants.

Ship gate for each phase: green suite **and** a live end-to-end run — request
from a real blocked tool call, approve on the real phone, watch the retry
succeed and the audit line land. A merged PR is not a shipped feature.

---

## 15. Open questions

1. **Should `discipline` be escalatable at all?** Its correct fix is to call
   `c3_edit`, which costs nothing. Making it one-tap-approvable may exist
   purely to train tap-fatigue that later gets spent on an `access_deny` card.
   Current proposal: ship the layer, default it off, and watch whether it is
   ever turned on.
2. **Latency.** Is the Android 15-minute background floor acceptable, or does
   v1 need SSE from the Oracle? The plumbing exists (`GET /api/chat` already
   streams `text/event-stream`) and a LAN/Tailscale SSE connection avoids any
   third-party push service, which the self-hosted principle would otherwise
   forbid. Cost is a held connection and reconnect logic on the client.
3. **Session grants.** `allow_session_grants` is specified but defaults off.
   Does an unlimited-uses, 1-hour grant have a legitimate use, or is it just a
   slow `c3 enforce advisory`?
4. **Store choice.** Reuse the `suggestions.json` approve/dismiss machinery, or
   a parallel store? Proposal: parallel — suggestions are memory-write
   proposals with no TTL, no session binding and no security consequence.
5. **Multi-project inbox.** `GET /api/mobile/overrides` without `project`
   returns everything; does the phone default to that, or to the currently
   picked project? Proposal: everything, because the point is answering while
   away from the desk.
