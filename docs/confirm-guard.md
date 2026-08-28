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
- Precedence: **`deny` > `mask` > `confirm` > `read_only`**. Mask beats
  confirm deliberately: masked content is read-only *with no override*
  (mask-guard §3) because an edit expressed against a transformed view
  cannot be trusted against the real file — no confirmation flow may
  authorise it.
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
  surface — perform the change via c3_edit or a native tool (both file
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

Not yet shipped, not yet frozen:

2. **`builtin_mode`** — per-builtin downgrade `deny|confirm|allow`
   (global-only, two-key attested, Tier-0 and forbidden targets excluded;
   `disable_builtin` becomes the legacy alias of `allow`).
3. **Agent-config confirm tier** — `.mcp.json`, CLAUDE.md/AGENTS.md,
   `.claude/hooks|skills|agents|commands/**` as builtin confirm-write.
4. **`shell_warn` grant wiring** — suppress the c3_shell soft-warn once per
   approved use; `_BLOCKED` never consults grants.
