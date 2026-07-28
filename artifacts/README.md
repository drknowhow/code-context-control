# Artifacts

Published explainer and reference pages that travel with the repository.

These are **standalone visual documents** — the kind of thing you hand to
someone (or to yourself, six months later) to understand a feature without
reading the source. They live here so they version alongside the code they
describe: when a feature changes, its artifact shows up in the same diff.

Not to be confused with:

- **`cli/guide/*.html`** — the in-app guide, served by the running server at
  `/guide/`. That ships inside the package and is the canonical user
  documentation.
- **`docs/*.md`** — design specs and implementation contracts, written for
  whoever changes the code next.
- **`c3_artifacts` (the MCP tool)** — unrelated. That's version history for
  agent-config files (CLAUDE.md, hooks, MCP configs). Same word, different
  thing.

## Index

| File | What it explains | Published |
|---|---|---|
| [`mask-guard-explainer.html`](mask-guard-explainer.html) | Mask Guard (v2.63.0) — before/after examples, the four presets, why masked means read-only | [claude.ai/code/artifact/88ac037d](https://claude.ai/code/artifact/88ac037d-f36c-4e70-b7f0-82733d37bb6e) · 2026-07-27 |

## Conventions

**Naming.** `<feature>-<kind>.html`, lowercase and hyphenated —
`mask-guard-explainer.html`, not `MaskGuard_v2.html`. The filename is the
artifact's identity: republishing the same path updates the same URL, so
renaming a file orphans its published page.

**Content is real.** Every example in an artifact should be actual output
from a verified run, not illustrative invention. The Mask Guard explainer's
before/after pairs are copied verbatim from the v2.63.0 release verification.
An artifact that shows made-up output is worse than no artifact — it teaches
something false with the authority of a screenshot.

**These files are publish sources, not standalone pages.** They deliberately
omit `<!doctype>`, `<html>`, `<head>` and `<body>` because the publisher wraps
them. A browser will still render one opened directly from disk, but without
the theme toggle the hosted version has. Don't "fix" this by adding the
wrapper tags — that breaks publishing.

**Theme-aware.** Palettes are defined as custom properties on `:root`,
redefined under `@media (prefers-color-scheme: dark)` and again under
`:root[data-theme="dark"]` / `:root[data-theme="light"]` so the viewer's
toggle wins in both directions.

**Self-contained.** A strict CSP blocks every external host — no CDN scripts,
no webfont URLs, no remote images. Inline the CSS, and embed any asset as a
`data:` URI.

## Updating a published artifact

The URL is bound to the file path, and it is only re-targetable if you have
the URL. Editing the file and republishing **the same path** keeps the URL.
From a different session, pass the recorded URL explicitly — otherwise a new
one is minted and the link in the index above goes stale while still
resolving to the old content, which is the worst of both.

That is why the index records the URL. Keep it current.
