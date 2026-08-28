# Confirm Rules — a declarative "pause for a human" access mode

Status: FROZEN for the parts shipped in 2.97.0 (§1–§6). §7 records the
phases that build on this and are NOT yet shipped; they freeze when they
land. Companion specs: `docs/access-guard.md` (the evaluator this extends),
`docs/override-requests.md` (the request/grant machinery this reuses),
`docs/mask-guard.md` (the precedence neighbour).

## 0. Why

Access Guard v1 gave every path exactly two governed outcomes: refused
(`deny`, `read_only`, `mask`) or ungoverned. Override Requests then made a
refusal *escalatable* — but only reactively: the agent hits a wall, decides
to ask, and the user must have pre-enabled the whole override feature.

What was missing is the middle mode users actually reach for: **"the agent
may change this, but each change goes past me first."** A `confirm` rule is
that mode, declaratively. The write refuses softly, C3 itself files the
Override Request (the agent cannot forget to ask, and cannot word the ask),
and one tap approves exactly one retry through the existing grant machinery.
The rule survives every grant, unchanged.

Non-goal, same as every guard layer: this is cooperative mistake/injection
containment for agents working through C3 surfaces, not an OS sandbox.

## 1. Rule format

A fourth glob-list kind in the `access` section, both scopes:

```json
{ "access": { "confirm": ["infra/**", "*.tf"] } }
```

Everything about glob storage, canonicalization, scope UNION, and
tightening-only merge is inherited from access-guard §1 verbatim. The
unknown-key rule is inherited too, and is the compatibility story: **a
pre-2.97 C3 reading `access.confirm` treats the scope as corrupt and
evaluates deny-all** — loud and strict, never silently permissive.
Mutation stays human-only (`c3 access add --kind confirm`, the Access tab,
`POST /api/access`); every mutation is ledger-logged (`access://<glob>`).

## 2. Semantics

- `confirm` gates the **write class only**: `write`, `create`, `delete`.
  Reads, enumeration, search, indexing are untouched — a confirm path stays
  fully visible. This is permanent for user rules; there is deliberately no
  read-confirm knob, because "pause on read" already exists as `deny` plus
  the `access_deny` override layer. (An internal all-ops variant —
  `Rule.confirm_ops == "all"` — is reserved for builtin mode downgrades of
  full-deny builtins, where confirm must cover reads or it would silently
  become allow-read. It is unreachable from config.)
- Precedence: **`deny` > `mask` > `read_only` > `confirm`**. Confirm is the
  loosest non-allow outcome — a hold can be approved into a write, a
  `read_only` cannot — so under the scopes-only-tighten invariant everything
  stricter wins. Two consequences are load-bearing: a user's `read_only`
  rule is never softened into a pause by a builtin confirm tier beside it,
  and a user `confirm` glob over `**/.c3/**` does not outrank the builtin
  write-deny (the sanctioned route to a confirm-mode builtin is
  `builtin_mode`, §7). Mask beats confirm because an edit expressed against
  a transformed view cannot be trusted against the real file (mask-guard §3).
  *(Corrected in v2.100.0 — 2.97.0 briefly shipped confirm above read_only,
  which let the agent-config tier shadow a stricter user rule; the artifact
  restore wiring tests caught it.)*
- The verdict kind is `confirm`; the `Denial` carries `kind="confirm"` with
  the matched glob. `check()` returns that denial, so **every surface that
  was never taught about confirm refuses** (fail closed): filter, validate,
  impact, delegate, artifact restore, the c3_project proxy.

## 3. The auto-file protocol

A confirm hold files the Override Request itself, at the enforcement site,
via one shared helper (`override_requests.auto_file`):

- Sites that file: the PreToolUse hook (`hook_access_guard.run`, native
  tools) and `c3_edit` (via `cli/tools/_grants.confirm_request`). Both
  consult grants FIRST — an approved retry never re-files.
- Sites that refuse without filing (S8 says so and points to a filing
  surface): every other c3_* tool.
- `auto_file` never raises. It returns the pending row (the existing one on
  a duplicate — dedup, mutes, and both rate limits are `create()`'s,
  unchanged) or a short reason S8 embeds.
- Auto-filed rows carry an **empty justification**: the card renders from
  the denial's trusted identity (tool, op, path, rule) and nothing the agent
  composed. Filing and consuming happen under the same session id on the
  same surface.
- Never inside `verdict()`/`check()` — the evaluator stays pure and the
  hook hot path stays one config read.
- Request creation still refuses `forbidden_target()` paths outright: even
  under a user confirm rule covering `.c3/**`, the vault and the override
  policy/grant/audit files can never become a request, so they can never
  become a grant.

## 4. Refusal string S8 (pinned, verbatim)

Machine tag: `[c3-access:confirm]`. Base:

> `[c3-access:confirm] {operation} for {path} is held for human confirmation
> by Access Guard rule '{glob}' ({scope} scope). This is a pause, not a
> refusal — a human must approve this exact {operation}. Do not retry until
> a decision arrives, and do not route around the hold via the shell or
> another tool.{tail} Rules: ``c3 access list`` or the Access tab.`

`{tail}`, one of three:

- filed: ` Confirmation request {id} is pending — wait with
  c3_override(action='wait', request_id='{id}'), then retry this exact call
  once if approved.`
- not filed: ` A confirmation request could not be filed ({reason}) — ask
  the user in chat.`
- refuse-only surface: ` No confirmation request was filed from this
  surface — retry via c3_read, c3_edit, or a native tool (those file
  one), or ask the user in chat.`

S8 never carries the override-requests §6 offer line — it would be a second,
contradictory invitation on a refusal that already names the filed request.

## 5. The `access_confirm` layer — the one default-ON exception

Confirm approvals ride the existing request→grant pipeline under a new
`override.layers` key, `access_confirm`, with one deliberate amendment to
override-requests §3.1 property 1 ("default off, everywhere"):

- **`access_confirm` defaults `True` and does not require
  `override.enabled`.** A confirm rule exists only because a human wrote
  one; that authorship is the opt-in, and the request machinery is the
  rule's *mechanism*, not an escalation of it.
- The escape hatch is explicit: `override.layers.access_confirm: false` in
  either scope (tightening-only AND-merge) forces confirm rules to refuse
  without filing.
- **Fail closed on corrupt config:** `escalatable()` returns `False` for
  every layer — including this one — whenever any override scope is
  corrupt. Without that guard, the layers dict's `True` default would leave
  confirm escalatable exactly when the config could not be read.
- Grant minting honours the same carve-out: `decide()` passes the request's
  `rule_class` to `mint(layers_key=...)`, which checks
  `escalatable(layers_key)` instead of the bare `enabled` flag. Direct CLI
  mints without a `layers_key` keep the legacy `enabled` requirement.
- `access_confirm` is NOT in `TYPED_CONFIRM_LAYERS`: one-tap approve is the
  intended UX. The rule means "ask me", not "override my deny" — the
  typed-glob challenge stays where it was, on `access_deny` and
  `access_builtin`. Rate limits, request TTL, grant TTL/use ceilings, mutes,
  and the nine-condition grant matcher apply to confirm identically.

## 6. Coverage

Enforced everywhere the access evaluator runs: c3_* tools, native tools via
hooks, the shell scan (a confirm rule never hard-denies a shell read —
write-class only). Requests are approved from `c3 override approve`, the
mobile Guard tab, and (planned, §7) the Hub. Residual risks are
access-guard §6's, unchanged.

## 7. Phases building on this

1. **Hub approval surface** — SHIPPED v2.98.0. Cross-project pending-request
   cards + decide routes (`decided_by="desktop"`, typed-glob challenge
   re-enforced server-side in `decide()`), read-only per-project rules and
   override-layer matrix. `GET/POST /api/hub/overrides`, `GET
   /api/hub/access`, `cli/hub_ui/components/hub_access.js`. Audit mirrors
   the mobile route: identifiers only, never the justification.

2. **`builtin_mode`** — SHIPPED v2.99.0. Per-builtin mode
   `deny | confirm | allow` (plus `default`, the reset verb), GLOBAL scope
   only, in `access.builtin_mode: {glob: mode}`. Two-key attested exactly
   like the opt-out it generalises: keyring account `builtin_mode|<glob>`
   must hold the SAME mode string, written attestation-first; every failure
   path enforces the shipped default. A mode never widens the op class the
   builtin governs:

   | Tier | default | deny | confirm | allow |
   |---|---|---|---|---|
   | `BUILTIN_DENY` (`**/.env*`) — all ops | deny-all | deny-all | confirm ALL ops (`confirm_ops="all"` — a write-only confirm would silently become allow-read) | off |
   | `BUILTIN_WRITE_DENY` (`**/.c3/**`, `**/.claude/settings*.json`, `**/.git/**`) | read_only | full deny (tightens) | confirm-write | off |

   Tier-0 vault globs take no mode at any price (`set_builtin_mode`
   raises), and even under a confirm-mode `.c3/**` the `forbidden_target()`
   files can never become a request. `disable_builtin` survives as the
   legacy spelling of `allow` (`set_builtin_disabled` is a shim); a glob
   named in BOTH spellings makes the global scope corrupt (deny-all), and
   `set_builtin_mode` lazily retires the legacy entry so the state cannot
   arise through the API. Project-scope `builtin_mode` ⇒ corrupt. Reads on
   a confirm-held builtin file a request from the hook and `c3_read`;
   enumeration surfaces exclude and never auto-file (no existence leak).
   Surfaces: `c3 access builtin mode <glob> <mode>` (typed-glob confirm for
   the widening modes), `POST /api/access/builtin_mode` + a mode selector in
   the Access tab, `list_rules()["builtin"]["modes"]`.

3. **Agent-config confirm tier** — SHIPPED v2.100.0. `BUILTIN_CONFIRM_WRITE`
   = `**/.mcp.json`, `**/claude.md`, `**/agents.md`, `**/gemini.md`,
   `**/.claude/{hooks,skills,agents,commands}/**` — the previously
   UNGUARDED agent-config surfaces (an agent could add an MCP server or
   rewrite a hook body silently; only artifact capture saw it, after the
   fact). Default is a pause, not a block: writes hold for one-tap approval,
   reads stay open — an agent must always be able to read its own
   instructions. Mode-governable: this tier governs writes only, so `deny`
   hardens to a write-deny (never a full deny) and `allow` restores the
   pre-2.100 behaviour. The `settings*.json` write-deny is deliberately NOT
   in this tier — hook REGISTRATION stays hard while hook BODIES pause,
   because registration decides code execution. `artifact_store.restore()`
   exempts builtin confirm exactly as it exempts builtin read_only: restore
   writes back a version the store itself captured, on an audited
   human-triggerable path.

Not yet shipped, not yet frozen:

4. **`shell_warn` grant wiring** — suppress the c3_shell soft-warn once per
   approved use; `_BLOCKED` never consults grants.
