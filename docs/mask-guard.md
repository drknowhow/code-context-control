# Mask Guard — v1 Design Spec (implementation contract)

Status: **FROZEN**, shipped in v2.63.0. Sibling review: Vi (gemYep) and Cod
(CodYep), both 2026-07-27; Cod's review overturned rev 1's architecture (see
Reconciliation). Changes to this spec after implementation require a
documented reason in the PR description.

**Implementation map**

| Concern | Module |
|---|---|
| verdict, precedence, rule store, refusals | `services/access_guard.py` |
| deterministic transforms + Protected Mode | `services/mask_presets.py` |
| content-addressed views, manifest, staleness | `services/mask_mirror.py` |
| transactional purge/build/rebuild | `services/mask_activation.py` |
| fact provenance | `services/memory.py`, `services/auto_memory.py` |
| human surfaces | `cli/server.py` (`/api/access/*`), `cli/c3.py` (`c3 access mask`), `cli/ui/components/access.js` |
| tests | `tests/test_mask_guard.py`, `test_mask_surfaces.py`, `test_mask_routes.py` |

> ### Reconciliation after sibling review (rev 1 → rev 2)
>
> **Cod's review overturned rev 1's central architecture decision, and he was
> right.** Recorded here rather than silently rewritten, because the reasoning
> matters more than the conclusion.
>
> Rev 1 chose **transform-on-read** and proposed **post-filtering `c3_shell`
> stdout** through the redact engine. Cod chose a **content-addressed
> materialized mirror** and **blocking** shell/git/validate/delegate over
> masked scope rather than sanitizing their output.
>
> These are a package, and rev 1's package was the weaker one:
>
> 1. Rev 1's objection to a mirror was *coherence* — the agent reads the twin
>    while `c3_shell` runs pytest against the real tree. That objection
>    dissolves once shell content-ops over masked scope are blocked: there is
>    no divergence left to be incoherent about.
> 2. **Post-filtering stdout cannot work for crops, only for redactions.** You
>    can regex a secret out of `cat config.py`. You cannot post-filter
>    `cat data.csv` into `sample_rows(20)` — that would require reconstructing
>    which rows were supposed to be visible. Rev 1 §6 row 4 was wrong, and it
>    was wrong in the specific way this project has been burned by before:
>    a best-effort mitigation written in language that reads as a guarantee.
> 3. **Protected Mode needs an object to validate.** A materialized artifact
>    means the validation verdict is recorded in a manifest and the exact
>    validated bytes are what gets served. Transform-on-read has to re-render
>    and re-trust on every read.
>
> Also adopted from Cod: transactional activation as a state machine (§6),
> `redact_columns` as a preset, explicit exclusion of `summarize`, versioned
> presets that never silently upgrade, and rejecting overlapping rules instead
> of inventing a precedence order (rev 1 invented one — a source of surprise).
>
> **Held against Cod:** the vault stays on the v2 roadmap. He argues to never
> build it because a reverse dictionary is another privileged content surface.
> That is a real cost, but re-identifying the *agent's output for the human*
> is the feature that makes masked work reviewable. Deferred, not dropped, and
> it must justify itself against his objection when it comes up.
>
> **Vi's contribution:** independently named the write-back corruption as the
> ship-blind failure (§3), and the builtin secret-pattern library (§4).
> Her "mask at the MCP output serializer" is rejected — see §2.
>
> **Verified, not assumed** (`services/auto_memory.py`): facts extracted from
> file content are stored via `memory.remember(fact_text, category,
> session_id)` — **session provenance only, no file provenance**. Cod's named
> ship-blind failure is therefore live in this codebase today: on mask
> activation there is no way to locate or purge the facts derived from the
> now-masked file. Fact-provenance is a **prerequisite of v1**, not a
> follow-up. See §9.

Predecessor: `docs/access-guard.md` (v2.62.0, FROZEN). Mask Guard is an
**extension of Access Guard, not a parallel system** — same config section,
same canonicalizer, same evaluator, same precedence machinery, same UI tab.

Design inputs: `U:\1. Projects\SafeMirror` (classify → policy → transform →
validate → artifact; mirror-gated MCP reads; Protected Mode) and
`U:\1. Projects\Table Parser` (deterministic injective real→fake dictionary;
encrypted vault; free-text re-identification; row sample/scramble; recipes).

---

## 0. The one idea that drives every decision

Access Guard's `deny` is a **predicate**. Masking is a **function**.

| | predicate (`deny`) | function (`mask`) |
|---|---|---|
| evaluated | anywhere, cheaply, repeatedly | exactly once per byte-to-text boundary |
| failure mode | no output | **wrong output that looks right** |
| derived artifacts | trivially safe (nothing to leak) | every cache, index, diff and log becomes a channel |
| determinism | irrelevant | **mandatory** — same file must render identically on every read and through every surface |

Everything below follows from that table.

---

## 1. Two families, not one

Dimitri's ask — "crop/render/adjust their contents so they are masked" —
is actually two different jobs, and separating them is what makes v1
shippable in one release.

**REDACT (value-level).** Sensitive *values* inside otherwise-shareable
files. Secrets, PII, customer names, internal hostnames. Genuinely new
engine. Deterministic pattern + entropy scan → inert placeholder.

**CROP (shape-level).** The file is not sensitive, it is *too much* or
*too raw*: a 2 GB CSV where 20 rows would do, a vendor SDK that should read
as an API surface not source, a 400 MB log. **C3 already has this engine** —
`compressor` modes (`structure` / `outline` / `smart`) are exactly
shape-level rendering. v1 CROP is a re-wiring, not a build.

The token-economy value of CROP is why this belongs in C3 at all rather than
staying in SafeMirror: masking and compression are the same operation
pointed at two different goals.

---

## 2. Architecture: transform-on-read, service layer, content-addressed cache

**Rejected: materialized mirror** (the SafeMirror model — write a safe twin,
gate reads to it). For a *code repository* it fails on coherence, not on
security: the agent reads the twin, then `c3_shell` runs pytest against the
real tree, and every error message it gets back is in real coordinates while
its mental model is in masked coordinates. Two sources of truth on disk,
staleness on every write, and (in SafeMirror's mirror-in-place form) git
sees the mask as a diff and the build compiles against fakes.

**Adopted: transform-on-read at the service layer.** Nothing on disk
changes. Git stays clean, builds and tests run on real code, and the render
is a pure function — cacheable with the key shape the compressor already
uses (`{content_hash}_{mode}{ext}.json`), extended with the rule digest.

**Rejected: Vi's "exclusively at the MCP output serializer."** Right
instinct (disk untouched), wrong seam. Serializer-only masking misses the
indexer, `c3_delegate` subprocesses, and the PreToolUse hook path — and it
contradicts the rule Access Guard already established and tested
(`docs/access-guard.md` §3: *service layer, never MCP wrappers*). Two
enforcement philosophies in one codebase is how one of them rots.

```
ONE exported function, mirroring canonicalize()'s role in the guard:

    mask.render(path, text, *, surface) -> RenderResult(text, applied, rules)
```

Every place raw bytes become model-visible text calls it. A CI meta-test
enforces that — the direct analogue of the existing *"CI meta-test:
file-touching call sites route through the guard."*

**Why one function is a security property, not just hygiene:** if `c3_read`,
`c3_compress`, a search snippet and `c3_impact` render the same file
differently, the agent recovers the original by **differencing the
surfaces**. Consistent rendering is the whole game.

---

## 3. Write-back: `mask` implies write-deny. Non-negotiable, no override.

This is the crux, and it is the failure Vi named unprompted: the agent reads
`DB_PASSWORD = "«c3:redacted»"`, later edits an adjacent line, and `c3_edit`
faithfully writes the placeholder into the real file. Auth breaks in
production; C3 reports success with zero warnings.

Reverse-mapping the edit (`old_string`/`new_string` through the dictionary
before applying) is **unsound in general**, and its unsoundness is the
dangerous kind — it usually works:

- **Cropped content has no inverse.** `sample-rows(20)` discarded rows; a
  reverse map cannot restore what was never emitted.
- **Invented identifiers.** The agent writes a *new* symbol that has no real
  counterpart. Nothing to map it back to; it lands as-is.
- **Partial tokens.** The agent copies half a placeholder, or reflows it
  across a line break. Match fails, raw placeholder is written.
- **Non-injective collisions.** Table Parser needed deterministic
  perturbation to keep fakes injective *within a column*; across a whole
  repo, injectivity is far harder, and a collision silently rewrites the
  wrong symbol.
- **Semantically right in masked space, wrong in real space.** The agent
  reasons correctly over 20 sampled rows and writes a fix that is wrong for
  the other 40 million.

Applying to a twin and re-projecting (option c) inherits all of the above
and adds a merge.

The decision costs nothing to build: `read_only` already exists in the
schema with a shipped, agent-legible refusal string (S2). `mask` ⇒
`read_only` reuses proven machinery.

**Where reversibility does belong — the other side of the glass.** Table
Parser's fake→real dictionary is valuable, just not for write-back. Its home
is **re-identifying the agent's OUTPUT for the human**: the agent proposes a
patch mentioning `Acme_Corp_7`, and the UI shows Dimitri the re-identified
version. The mapping never enters model context and is never used to mutate
a file. That is the encrypted-vault feature, and it is **v2**.

---

## 4. Schema — extend `access`, do not create a parallel section

```json
{
  "access": {
    "deny":      ["**/payroll/**", "*.pem"],
    "read_only": ["migrations/**"],
    "mask": [
      { "glob": "**/*.csv",         "render": "sample-rows(20)" },
      { "glob": "data/clients/**",  "render": "redact:pii" },
      { "glob": "vendor/**",        "render": "signatures-only" },
      { "glob": "**/*.log",         "render": "redact:secrets,head(200)" }
    ]
  }
}
```

**Access Guard's strictest design choice pays off here.** Its
*"any unrecognized key — especially `allow` — is a hard config error"* rule
means an old C3 reading a config with `mask` **hard-errors instead of
silently serving unmasked content**. That is precisely the future this rule
was written for, and it should be called out in the PR.

### Precedence and composition

- Verdict order: **`deny` > `mask` > `read_only`**. `mask` implies
  write-deny (§3) and raw-read-deny.
- Scopes UNION; merging only ever tightens (unchanged from Access Guard).
- **Multiple matching mask rules compose in a fixed canonical order, never
  in rule order**: all `redact:*` first, then all crops. Redaction before
  cropping is the only safe order — cropping after redaction cannot
  reintroduce a redacted value, whereas the reverse can emit raw bytes that
  the crop happened to keep. Rule-file order must never affect output, or
  the config stops being diffable.

### v1 render kinds (all deterministic, zero LLM)

| kind | engine | notes |
|---|---|---|
| `redact:secrets` | new: builtin pattern library + Shannon-entropy scan | `ghp_*`, `sk-*`, `AKIA*`, PEM blocks, JWTs, bearer tokens, connection strings, `KEY=` assignments |
| `redact:pii` | new: pattern library | email, phone, SSN, IBAN, card, street address |
| `redact:custom` | new: user regex list | authored in the UI, validated on save |
| `head(n)` / `tail(n)` | trivial | logs |
| `sample-rows(n)` | tabular parser | CSV/TSV; header always preserved |
| `schema-only` | tabular/JSON | columns + inferred types + row count, zero values |
| `signatures-only` | **existing** `compressor` structure mode | vendor trees, large modules |

### Dropped from v1: the classifier-driven policy

SafeMirror's local-LLM label→action pipeline does not ship in v1. An LLM
pass per read is unaffordable at agent read rates, and **non-determinism in
a security-adjacent transform is disqualifying** — the same file must render
identically on every read or the agent's cached reasoning goes incoherent
and the differencing attack in §2 opens up.

**But the classifier is not wasted — it moves to authoring time.** A
"Suggest rules" button in the UI runs the SafeMirror-style scan (Ollama, or
regex fallback) across the project and *proposes globs and render kinds for
the human to accept*. Classification is probabilistic and slow: do it once,
with a human in the loop. Execution is deterministic and hot: do it on every
read. This split is the reconciliation of the two source systems and is the
strongest single idea in this proposal.

---

## 5. Honesty contract: loud at rule level, silent at content level

Access Guard's principle is that the agent is **told** it was blocked, so it
never silently produces wrong work. Masking needs the same, and the
silent-vs-loud framing is a false binary. The third option:

**Tell the agent THAT content is transformed and BY WHICH RULE. Never tell
it what was removed or where the real value lives.**

1. **Inline header on every rendered payload:**
   > `[c3-mask:rendered] {path} — rule '{glob}' render '{kind}' ({scope}
   > scope). Content is transformed: values may be synthetic, redacted, or
   > truncated. Do not treat literals here as real, do not copy them into
   > code, and do not report conclusions about data volume or completeness
   > from this view. This file is not writable — `c3_edit` will refuse.`

2. **Inert, self-describing placeholders:** `«c3:redacted:aws_key»` —
   deliberately chosen so it is **not valid in any language**. This is
   defense-in-depth against exactly Vi's failure mode: even if write-deny
   were somehow bypassed, the placeholder cannot be silently committed —
   a linter, type-checker, or compile trips on it immediately. A
   `[REDACTED]` string literal would not.

3. **Search footer** `[c3-mask:limited]`, same shape as the shipped
   `[c3-access:limited]`, emitted whenever any mask rule is active for the
   scope (not only when something was actually masked — no presence oracle).

**Why loud wins the argument.** The counter-case is that loudness hands a
prompt-injected agent a target list. It is weak here for two independent
reasons: (a) Access Guard already establishes that *rule text is not secret*
and shows matched globs in refusals and `c3_status`; (b) unlike `deny`, a
masked file's **existence, name and structure are visible by design** — the
agent is reading it. Loudness therefore costs almost no information the
agent does not already hold, while silence costs correctness. The asymmetry
is decisive: silent masking fails as *"agent ships code built on a lie and
neither party knows"*; loud masking fails as *"adversarial agent learns
which files the user cares about"* — which it learns anyway.

---

## 6. Leak channels — ranked by likelihood of being missed

`deny` never had to produce output, so derived artifacts were trivially
safe. `mask` produces output, so every derived artifact is a channel. This
is the section most likely to be under-built.

| # | Channel | Why it leaks | Fix | Miss risk |
|---|---|---|---|---|
| 1 | **Render/compression cache** | keyed on content hash alone → rule changes, content doesn't, stale unmasked entry served | cache key = `hash(content) + hash(effective_rules)` | **HIGH** |
| 2 | **TF-IDF / vector index built before the rule** | Access Guard's index exclusion is *forward-only* (documented residual). For mask this is worse: **search snippets are model-visible content** | stamp the rule digest in index metadata; mismatch ⇒ forced re-index of matching paths on rule change | **HIGH** |
| 3 | **`c3_validate` / pyright / tsc output** | quotes the offending source line verbatim; does not look like a file read, so it gets forgotten | route checker output through `render()` | **HIGH** |
| 4 | **`c3_shell` stdout** | `type`, `cat`, `grep`, or a pytest assertion printing real values. The shell reads real bytes by design; the existing scan is *advisory* | post-filter stdout through the redact pass (cheap, deterministic, reuses the engine) | **MED-HIGH** |
| 5 | **`c3_delegate`** | hands a path/CWD to a subprocess backend that opens the real file itself. Guard pins `--sandbox read-only` — **useless here, read-only IS the leak** | when mask rules are active: pass rendered content inline, or block the masked subtree | **MED** |
| 6 | **Edit-ledger stored diffs** | patches recorded before the rule existed, replayed to the agent via `c3_edits` | render on ledger read-out | **MED** |
| 7 | **Auto-memory / `file_memory` facts** | summaries recorded pre-rule | render on recall; flag pre-rule facts on rule creation | **MED** |
| 8 | **Tracebacks / exception strings** | Python quoting source lines; error text echoing `old_string` | render exception payloads at the tool boundary | **LOW-MED** |
| 9 | **`MAP.md` / repo map** | module one-liners derived from docstrings | regenerate on rule change | **LOW** |
| 10 | **git objects** | `git show` / `git diff` through the shell — total bypass | documented residual; not solvable in v1 | **LOW freq / TOTAL** |

**Two channels not on anyone's list:**

- **The agent's own prior context.** A rule added mid-session cannot unread
  what is already in the transcript. Masking is **forward-only within a
  session**. The UI must say so on rule creation and offer *"takes effect
  for new reads — clear the session for full effect."*
- **Cross-surface differencing.** Covered in §2; the reason `render()` must
  be a single function.

---

## 7. Protected Mode (borrowed from SafeMirror, and it belongs in v1)

After rendering, re-scan the **output**. If it still matches a secret
pattern, **fail closed** — refuse the read with the S1-shaped refusal rather
than serve a partially-masked payload. Cheap to build, and it converts the
worst class of bug (redaction silently missed something) from a silent leak
into a loud refusal. This is the single most valuable idea to steal from
SafeMirror and it costs one function call.

---

## 8. Surfaces

**UI — one tab, two sections.** Keep it inside the existing **Access** tab
(`cli/ui/components/access.js`) rather than a new tab: precedence between
`deny` / `mask` / `read_only` is the thing users must reason about, and
splitting it across tabs hides exactly that. Additions:

- a **Masking** section listing mask rules per scope (builtin / global /
  project), same visual grammar as the rule table;
- the existing **"Test a path"** probe extended to return
  `allowed | masked | read_only | denied`, and — for `masked` — a **live
  side-by-side before/after preview**. *This is the feature that sells the
  system:* the user sees exactly what the AI will see, on their real file,
  before committing the rule;
- **"Suggest rules"** (§4) — scan, propose, human accepts;
- a forward-only warning banner on rule creation (§6).

**REST:** `/api/access` grows a `mask` array; new `POST /api/access/preview`
returns `{before, after, applied_rules}` for the probe. Mutations
human-only and ledger-logged, unchanged.

**CLI:** `c3 access mask add <glob> --render <kind> [--scope]`,
`c3 access mask rm`, `c3 access list` shows all three kinds.

**Coverage matrix** (§5 of the guard spec, single source for the UI tab,
`c3_status` and the guide) gains a mask row stating plainly: *transform-on-
read means the real bytes remain on disk; anything outside C3's surfaces —
your editor, a raw shell, a non-Claude agent — sees them unmasked. Mask
Guard is context hygiene, not containment.*

---

## 9. Scope split

**v1 — SHIPPED in v2.63.0**
`mask` verdict + precedence + write-deny · fail-closed `check()` so
un-migrated surfaces refuse instead of leaking · content-addressed mirror
with staleness refusal · the four presets in §4 · Protected Mode ·
fact provenance + transactional activation · view-aware indexing so masked
files stay searchable from the view · `[c3-mask:transformed]` header,
`«c3:redacted:*»` placeholders, `[c3-mask:limited]` footer · UI section +
before/after preview · `c3 access mask` CLI · REST · coverage matrix + guide ·
81 tests across three files including the wiring meta-test.

Two bugs the tests caught during the build, both of the exact class this
design exists to prevent:

1. **CRLF round-trip in the mirror.** Artifacts were written with
   `newline=""` but read back through universal-newline translation, so a
   CRLF view differed between a fresh render and a cache hit — a
   differencing oracle. Fixed by `mask_mirror._read_view`.
2. **Preset schema was not JSON-serializable.** `/api/access` exposed the
   validation types directly; `preset_catalog()` now names them on the wire.

**v2**
Encrypted vault + fake→real dictionary for **re-identifying agent output to
the human** (Table Parser) · Faker-style synthetic values where structure
must survive redaction · LLM-assisted rule authoring (SafeMirror classifier,
authoring-time only) · cross-file linkage consistency · shareable rules
(needs the audit-until-human-ack model already deferred by the guard).

---

## 10. Residual risks (state them in the PR, do not discover them later)

Real bytes stay on disk and every non-C3 reader sees them · git history ·
pre-rule index/memory/ledger content (mitigated by forced re-index, not
eliminated) · shell as a rendering bypass (post-filter is best-effort, same
honesty as the guard's advisory scan) · mid-session rules are forward-only ·
redaction pattern libraries have false negatives by nature — Protected Mode
converts most of those to refusals, not all · a determined agent can still
difference surfaces if any call site skips `render()`, which is why the CI
meta-test is load-bearing rather than nice-to-have.
