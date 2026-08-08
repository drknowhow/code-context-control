# Access Guard — v1 Design Spec (implementation contract)

Status: FROZEN before implementation (board review wf_b5381a5d-56a, chair
verdict endorse_with_changes; refusal strings critiqued by Cod 2026-07-27).
Changes to this spec after implementation starts require a documented reason
in the PR description.

Access Guard lets the user mark paths the agent must not read, or must not
write, and makes every C3 surface enforce it. It is a **cooperative
mistake/prompt-injection guard, not containment**: a non-Claude agent's raw
shell or direct file API is out of scope (see Coverage matrix).

## 1. Schema

`.c3/config.json` (project scope) and `~/.c3/config.json` (global scope),
section `access`:

```json
{
  "access": {
    "deny":      ["**/payroll/**", "*.pem"],
    "read_only": ["docs/legal/**", "migrations/**"]
  }
}
```

- Exactly two keys. **Any unrecognized key — especially `allow` — is a hard
  config error** (guarantees a future grant schema can never be silently
  no-op'd by an old C3). No `mode` field: the guard always enforces.
- Globs are stored POSIX forward-slash canonical; backslashes normalized on
  input. Case-insensitive matching.
- Scopes UNION; merging only ever tightens. `deny` beats `read_only`.
- v1 rules are LOCAL-ONLY: `.c3/` is gitignored, committed rules cannot
  arrive via clone. (Shareable rules are v2 and require the
  audit-until-human-ack model.)
- ALL rule mutations are human-only (UI / `c3 access` CLI), ledger-logged.
  No agent-facing mutation surface exists. The agent's read view is
  `c3_status`; its runtime interface is the refusal string.

### Semantics

- `deny` = no read, no write, **no create** (R1), **no enumerate** (R2).
- `read_only` = no write/create/delete/rename-target. Reads are evaluated
  separately (another rule may deny them).
- R1 deny-CREATE is checked against the canonicalized, post-processed leaf:
  trailing-dot/space/ADS spellings of a denied name are denied. Write
  denial applies to create, delete, rename and link where a surface can see
  them — both rename endpoints need write. (Shell `mv` is a documented v1
  residual.)
- R2 deny-ENUMERATE: listings, search results, maps, and prefetch never
  leak existence or filenames of denied paths. Direct probes get the same
  refusal whether or not the target exists.
- Rule text is NOT secret: refusals and `c3_status` show the matched glob.
  The guard protects file contents and existence, not the rule registry —
  a user who wants an unlabeled protection names the glob accordingly.

### Builtins (hardcoded, always-on, fail-closed)

- deny (read+write): `**/.env*`, `**/.c3/secrets.enc`, `**/.c3/cred_state.json`
- write-deny (agent surfaces): `**/.c3/**`, `~/.c3/**`,
  `**/.claude/settings*.json`, the C3 install directory
  (`Path(cli.__file__).parent`), `**/.git/**` (reads stay open)
- Removable DEFAULT rules seeded in global scope on first run (user may
  delete them): `*.pem`, `id_rsa*`, `*.key`
- Spelling rules (`<name>`) deny how a path is WRITTEN rather than where it
  points, so they have no glob. A refusal cites them by name, so they are
  listed by `c3 access list` alongside the globs — the canonical set lives in
  `access_guard.SYNTHETIC_RULES`:
  `<unc>`, `<unresolvable>`, `<empty-component>`, `<8.3-alias>`,
  `<ads>` (Windows only). Because these flag a spelling, they are exempt
  from the shell scanner's existence gate; the scanner therefore skips URL
  and IPv6 tokens outright, since a colon there means "scheme" or "IPv6",
  not "alternate data stream" (#50).
- Corrupt/unparseable access section ⇒ that scope evaluates deny-all with a
  loud warning. Builtins apply even with no config at all.
- Internal service writes (credential_store, the guard's own state, ledger,
  activity log) use privileged paths that bypass tool-layer checks — the
  audit writer never consults the evaluator.

## 2. Canonicalization (ONE exported function)

`access_guard.canonicalize(path, project_root)` — every consumer (evaluator,
hooks, unlock map, shell scanner) uses this and nothing else:

1. Pre-strip `\\?\UNC\` → `\\`, then `\\?\` and `\\.\` (resolve() preserves
   them; files open fine under them).
2. Hard-deny anything still UNC-form (config escape hatch, off by default).
3. `resolve()` with nearest-existing-parent for non-existent targets.
4. On residual non-existent components: strip trailing `.` and ` ` until
   stable.
5. Reject `:` outside drive position (named ADS survives resolve()).
6. Casefold the full comparison string.
7. 8.3 predicate: any residual component matching `~digit` → expand via
   GetLongPathNameW or deny.

## 3. Enforcement inventory (service layer, never MCP wrappers)

| Surface | Check |
|---|---|
| compressor.compress_file + read service | read verdict (delegate + indexer inherit) |
| handle_edit | write verdict (create/edit/batch; both endpoints on any move) |
| handle_shell | cwd deny + advisory token scan (cmd AND exec_cmd, post-credential-expansion, MSYS `/x/`→`X:\`) |
| each search action | per-action pre-filter before dedup/top_k/map-build; `[c3-access:limited]` footer whenever ANY rules are active for the scope (not only on actual filtering — no presence oracle); defense-in-depth in `_append_prefetch` |
| c3_validate, c3_filter(file_path=), c3_impact | read verdict |
| ArtifactStore.restore | write verdict per member path |
| scanner.iter_files | index-time exclusion (denied paths never enter TF-IDF/vector index, MAP.md, file_memory) |
| c3_project | rules = global ∪ caller ∪ containing-realm of RESOLVED path; registration required beyond list/scan/register |
| c3_delegate | inherits read guard; pins codex `--sandbox read-only` when rules exist; autonomous backends behind user opt-in |
| PreToolUse hooks | new `hook_access_guard.py` FIRST in pretool routes; `_FAIL_CLOSED` synthesized deny on exception/import failure; verdict before `_PREREQS` early-return and before `_check_c3_used`; Bash + run_shell_command matchers installed same release (advisory scan); native Grep/Glob hard-denied only on explicit path args inside denied subtrees |

Typed `AccessDenied` exception carries (verdict, rule, scope, reason) and
must re-raise through except-Exception-continue loops (delegate).

Denial logging: coalesced per (rule, tool, session) with a hit counter; the
enforcement hook's tail scan counts only `type=='tool_call'` lines so a
denial storm cannot evict its evidence window.

> **Implementation note (v2.66.0).** Denial logging shipped in
> `services/access_telemetry.py`, surfaced as `c3 access stats`. It covers this
> guard *and* the tool-discipline hook, tagging each event with a `layer`
> (`access` | `discipline`) so the user picks the right lever. Coalescing
> happens at read time rather than write time — hooks are concurrent
> short-lived subprocesses, so a shared counter file would race. This is an
> implementation choice within §3, not a change to the spec.
>
> Tool discipline itself (the `hook_pretool_enforce` native-write block) is a
> separate layer with its own user-facing knob; see `docs/enforcement.md`. It
> is deliberately outside this spec: relaxing it does not relax any rule
> defined here, and the guard's evaluation never consults it.

## 4. Refusal strings (VERBATIM — implementation copies these exactly)

Interpolations `{path}`, `{project}`, `{tool}` are length-capped (200 chars,
middle-ellipsized) before insertion; `{operation}` ∈ read|write|create|delete.
Machine tags are stable API: `[c3-access:denied]`, `[c3-access:read_only]`,
`[c3-access:limited]`.

**S1 — MCP deny:**
> [c3-access:denied] {operation} denied for {path} by Access Guard rule
> '{glob}' ({scope} scope). This is a policy decision, not a transient error
> — do not retry or route around it. Mark the affected step blocked and
> continue with unaffected files; report the skip to the user. Rules:
> `c3 access list` or the Access tab.

**S2 — MCP read-only:**
> [c3-access:read_only] {operation} denied for {path} by Access Guard rule
> '{glob}' ({scope} scope). The effective policy is read-only; reads are
> evaluated separately. Do not retry the {operation}. Mark the affected step
> blocked and continue with unaffected files; report the skip. Rules:
> `c3 access list` or the Access tab.

`{operation}` is the operation the CALLER named, not the literal word
"write". A read-only rule blocks the whole write class, and `c3_edit` names
that operation `create` when the file does not exist yet. This string said
"write" until v2.74.0, which taught agents to request an override for
`op='write'`; the grant matcher compares `op` exactly, so an approval the
user had already given was minted for one operation, spent against another,
and refused.

**S3 — hook deny (native tool):**
> [c3-access:denied] native {tool} {operation} denied for {path} by Access
> Guard rule '{glob}' ({scope} scope). This is a policy decision, not a
> transient error — do not retry through another tool or the shell. Mark
> the affected step blocked and continue with unaffected files. Rules:
> `c3 access list`.

**S4 — search annotation (whenever any rules are active for the scope):**
> [c3-access:limited] results are limited to paths permitted by Access
> Guard. Absence is not evidence a path does not exist — check
> `c3 access list` before concluding missing work, and report the
> limitation if required work appears to be missing.

**S5 — c3_project proxy deny:**
> [c3-access:denied] {operation} denied for {path} through project
> '{project}' by that project's Access Guard rule '{glob}' ({scope} scope).
> The target project's effective policy governs proxied access — do not
> retry. Mark the affected step blocked and continue with unaffected work.

## 5. Coverage matrix (ships in UI tab, c3_status, guide)

> Enforced: C3 MCP tools (all agents using C3) · Claude Code native tools
> (hooks) · c3_shell (best-effort scan, advisory).
> NOT enforced: non-Claude agents' raw shell, direct file APIs, editors.

## 6. Residual risks (named, documented)

Rename/move via shell; TOCTOU between evaluation and operation; non-Claude
agents outside the MCP layer; pre-existing vector-index/auto-memory content
recorded before a rule existed (index-time exclusion is forward-only in v1).

## 7. Test matrix (minimum)

corrupt-config-must-deny · injected-evaluator-exception-must-deny ·
sticky-unlocked-still-denied · c3_bridge + c3_project bypass ·
denial-storm (30 denials don't flip `_check_c3_used`) · Windows matrix on
NOT-YET-EXISTING targets + `\\?\`, `\\?\UNC\`, plain UNC spellings ·
deny-CREATE trailing dot/space/ADS · 3-file-plan zero-retry acceptance ·
CI meta-test: file-touching call sites route through the guard ·
lint guard banning direct `Path.resolve()` in enforcement-adjacent code.
