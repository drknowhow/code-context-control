# Sub-projects

A sub-project is a C3 project declared a child of another one — a service in a
monorepo, a vendored tool, or a second checkout that happens to sit somewhere
else entirely. It keeps its own `.c3` (index, memory, ledger, config), and
retrieval fans out across the hierarchy only when you ask for it.

```text
c3_search(query='retry policy', scope='all')          # parent + every descendant
c3_search(query='retry policy', scope='billing-svc')  # one of them by name
```

`c3_memory` federates the same way, with memory roll-up configurable per
parent.

## Two kinds of link

The difference is where the child lives, and the only thing it changes is
indexing.

**Nested** — the child folder is inside the parent's. The parent's index,
doc-index, dictionary and watcher exclude that subtree, so nothing is indexed
twice. This is the monorepo case.

**Linked by path** — the child lives anywhere: a sibling folder, another drive.
Nothing is excluded from the parent's index, because the child was never inside
the tree the parent scans.

C3 picks the kind for you from the path you give it. You do not choose.

## Depth

Hierarchy is a **strict tree**: one parent per project, as many children as you
like, nested up to **8 levels**. A sub-project can have sub-projects of its own.

A project cannot become its own ancestor. When the child had to live inside the
parent that was impossible by construction; now that a child can live anywhere,
the link is checked against the ancestor chain and refused if it would close a
loop.

## Managing the hierarchy

Everything below is available from a project card in the Hub or the drill-in
**Sub-projects** tab, at every level of the tree.

- **Link project by path** — point at any folder. C3 inspects it first and
  tells you what is there before anything happens: whether it is a C3 project,
  its version, how much is in it, whether the hub already knows it, who already
  claims it as a child, and what it would bring with it. If it is not
  registered, one confirm registers and links it.
- **Designate** — the folder picker, fenced to the parent's own subtree, for
  promoting a folder that is already inside it. Initializes a `.c3` if the
  folder does not have one.
- **Detected, not linked** — when C3 finds a nested `.c3` under a folder you
  are inspecting, it says so and offers to link it. It is a suggestion. C3
  never creates a link you did not ask for, and a rescan never overwrites one
  you did.
- **Link health is passive.** Parent cards surface a red "N link issues" badge
  and children show their link status automatically. **Reconcile** shows what
  is broken and repairs it on confirm.
- **Cascade** update / reindex / health across the whole subtree — cancellable,
  and optionally including the parent itself.
- **Promote** a child back to top-level, or **de-initialize** it entirely
  behind a typed-name confirmation.
- **Change parent…** moves a child between parents. Since folders no longer
  have to nest, this is a configuration change and no files move.

Federation behaviour per parent — memory roll-up, search fan-out, and how many
children are consulted per query — is editable in the project's config editor.
The per-query cap drops the most distant relatives first, so a direct child is
never dropped in favour of a grandchild.

## CLI

```bash
c3 sub list                            # direct children (default subcommand)
c3 sub tree                            # the whole hierarchy
c3 sub tree --depth 2                  # stop after two levels
c3 sub inspect <path>                  # read-only: what is there, who claims it
c3 sub link <path>                     # link an existing project, anywhere on disk
c3 sub link <path> --init              # ...initializing it first if it isn't one
c3 sub add ./services/billing          # designate a folder (initializes it)
c3 sub check --fix                     # link health, and repair what's broken
c3 sub run update --include-parent     # cascade update across the subtree
c3 sub run reindex --depth 1           # cascade, direct children only
c3 sub remove billing                  # unlink a child
```

`inspect` mutates nothing — pointing it at an unrelated folder is safe.
`link` refuses a folder that is not already a C3 project unless you pass
`--init`; `add` initializes one for you. That is the whole difference between
them.

Promoting a child back to top-level and de-initializing are Hub actions —
they involve typed confirmations, so they are deliberately not exposed as
one-shot CLI flags.

## Where the link is recorded

Three places, and `c3 sub check` cross-checks them:

| Store | Field |
|---|---|
| parent `.c3/config.json` | `subprojects: [{name, rel_path \| path, added_at}]` |
| child `.c3/config.json` | `parent: {name, path, rel_path?}` |
| `~/.c3/projects.json` | `parent_path` on the child's row |

A nested entry carries `rel_path`; a linked one carries an absolute `path`.
The child's back-link `rel_path` is best-effort and absent when the two sides
are on different Windows drives, where a relative path does not exist.

The parent config is authoritative — `c3 sub check --fix` repairs the other two
from it. Both `subprojects` and `parent` are refused by the Hub's config editor;
they are managed through the actions above, never hand-edited.
