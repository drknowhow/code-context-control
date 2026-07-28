# Sub-projects

A sub-project is a nested repository — a service in a monorepo, a vendored tool
— promoted to a first-class child with its own `.c3`. The parent's index
excludes the child's subtree, so nothing is indexed twice, and retrieval fans
out across children only when you ask for it.

```text
c3_search(query='retry policy', scope='all')          # parent + every child
c3_search(query='retry policy', scope='billing-svc')  # one child by name
```

`c3_memory` federates the same way, with memory roll-up configurable per
parent.

## Managing the hierarchy

Everything below is available from a project card in the Hub or the drill-in
**Sub-projects** tab.

- **Designate** — folder picker with upfront validation, an adopt-vs-initialize
  preview, and IDE choice. An existing top-level project that already sits
  physically inside a parent can be re-linked with **"Make sub-project of…"**.
- **Link health is passive.** Parent cards surface a red "N link issues" badge
  and children show their link status automatically. **Reconcile** shows
  exactly what is broken and repairs it on confirm.
- **Cascade** update / reindex / health across all children — cancellable, and
  optionally including the parent itself.
- **Promote** a child back to top-level, or **de-initialize** it entirely
  behind a typed-name confirmation.
- **Change parent…** moves a child between parents. Folders must physically
  nest, so the wizard stages the move, validates each step, and never leaves a
  half-applied state.

Federation behaviour per parent — memory roll-up, search fan-out, and how many
children are consulted per query — is editable in the project's config editor.

## CLI

```bash
c3 sub list                            # show the hierarchy (default subcommand)
c3 sub add ./services/billing          # designate a child
c3 sub check --fix                     # link health, and repair what's broken
c3 sub run update --include-parent     # cascade update across children
c3 sub run reindex                     # cascade reindex
c3 sub remove billing                  # unlink a child
```

Promoting a child back to top-level and re-parenting are Hub actions — they
involve staged filesystem moves and typed confirmations, so they are
deliberately not exposed as one-shot CLI flags.
