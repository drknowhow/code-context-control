"""AgentCI — workflow discovery, normalization, and the job DAG.

The governing constraint (AgentCI spec §6.1) is that C3 does NOT invent a
second CI configuration language. `.github/workflows/*.yml` stays the single
source of truth; this module reads it and produces a normalized intermediate
representation that the runner can execute and the Hub can draw.

The honesty rule that shapes every decision here: **a job we cannot faithfully
reproduce must say so, not quietly run a subset of itself.** A local "PASS"
that skipped the half of a job it did not understand is worse than no local CI
at all, because it is indistinguishable from a real pass at exactly the moment
someone is deciding whether to push. So every construct we do not implement
lands in `JobInstance.blockers` and makes the job UNSUPPORTED — never SKIPPED,
never silently narrowed.
"""
from __future__ import annotations

import itertools
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from services import ci_expr

WORKFLOW_DIR = ".github/workflows"
_WORKFLOW_GLOBS = ("*.yml", "*.yaml")

# ── Runner → host OS ────────────────────────────────────────────────────────
# `runs-on` names a GitHub-hosted image. Locally we can only honestly run the
# ones matching this host: a `macos-latest` job executed on Windows is not that
# job. Reporting it as passed is the exact failure mode this module exists to
# prevent, so a foreign runner is a first-class "not runnable here" state.
_RUNNER_OS = {
    "ubuntu": "Linux", "linux": "Linux",
    "macos": "Darwin", "mac": "Darwin",
    "windows": "Windows", "win": "Windows",
}


def host_os() -> str:
    return platform.system()


def runner_os(runs_on: str) -> str:
    """Best-effort map of a `runs-on` label to an OS name. '' when unknown."""
    label = str(runs_on or "").strip().lower()
    for key, osname in _RUNNER_OS.items():
        if key in label:
            return osname
    return ""


# ── Steps we can stand in for ───────────────────────────────────────────────
# A `uses:` step runs somebody else's JavaScript in a GitHub runtime we do not
# have. A small number of them are pure environment setup whose local
# equivalent is "nothing, because the condition already holds" — those get a
# documented shim. Everything else blocks the job. The shim list is
# deliberately short and explicit; growing it is a decision, not a default.
#
#   checkout      — the working tree IS the checkout. No-op.
#   setup-python  — we run on the interpreter we already have. No-op, but the
#                   requested version is recorded so a mismatch is visible.
#   setup-node    — same reasoning as setup-python.
#   cache         — a pure optimization; skipping it changes timing, not result.
#   upload-artifact / download-artifact — handled by the runner against a local
#                   artifact directory rather than GitHub storage.
SHIMMED_ACTIONS = {
    "actions/checkout": "working tree is already the checkout",
    "actions/setup-python": "using the interpreter C3 is running on",
    "actions/setup-node": "using the node on PATH",
    "actions/setup-go": "using the go on PATH",
    "actions/cache": "cache is an optimization; skipped locally",
    "actions/upload-artifact": "artifacts copied to the local run directory",
    "actions/download-artifact": "artifacts read from the local run directory",
}


def action_name(uses: str) -> str:
    """`actions/checkout@v6` -> `actions/checkout` (docker:// and ./local kept whole)."""
    return str(uses or "").split("@", 1)[0].strip()


# ── Expressions ─────────────────────────────────────────────────────────────
# GitHub expressions are a language. We implement the contexts a workflow needs
# to be *executable* — matrix, env, and a handful of github.* fields — and treat
# every other expression as a blocker rather than substituting something
# plausible. `${{ secrets.X }}` silently becoming "" is how a job passes locally
# and fails in CI.
_EXPR_RE = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


@dataclass
class ExprResult:
    text: str
    unresolved: list = field(default_factory=list)


def substitute(text: str, contexts: dict) -> ExprResult:
    """Resolve `${{ ... }}` against *contexts*; report what we could not."""
    if not text or "${{" not in text:
        return ExprResult(text or "", [])

    unresolved: list = []

    def _one(match: re.Match) -> str:
        expr = match.group(1).strip()
        # Only bare dotted lookups are supported. Anything with an operator,
        # function call, or literal is a real expression and we do not have an
        # evaluator for it.
        if not re.fullmatch(r"[A-Za-z_][\w-]*(\.[\w-]+)+", expr):
            unresolved.append(expr)
            return match.group(0)
        root, _, rest = expr.partition(".")
        scope = contexts.get(root)
        if not isinstance(scope, dict):
            unresolved.append(expr)
            return match.group(0)
        value = scope
        for part in rest.split("."):
            if not isinstance(value, dict) or part not in value:
                unresolved.append(expr)
                return match.group(0)
            value = value[part]
        if isinstance(value, (dict, list)):
            unresolved.append(expr)
            return match.group(0)
        return "" if value is None else str(value)

    return ExprResult(_EXPR_RE.sub(_one, text), unresolved)


# ── Normalized IR ───────────────────────────────────────────────────────────

@dataclass
class Step:
    index: int
    name: str
    run: str = ""
    uses: str = ""
    shell: str = ""
    env: dict = field(default_factory=dict)
    working_directory: str = ""
    if_: str = ""
    with_: dict = field(default_factory=dict)
    # Set during instantiation when the step is a `uses:` we stand in for.
    shim: str = ""

    @property
    def is_run(self) -> bool:
        return bool(self.run)

    def to_dict(self) -> dict:
        return {
            "index": self.index, "name": self.name, "run": self.run,
            "uses": self.uses, "shim": self.shim, "if": self.if_,
        }


@dataclass
class JobInstance:
    """One concretely runnable job — a job after matrix expansion.

    Identity is two-level on purpose. `id` is what a person types
    (`test (windows-latest, 3.12)`); `key` is globally unique because two
    workflows may each define a job called `build`, and `needs: [build]` in one
    of them must never resolve to the other's.
    """
    id: str                      # matrix-qualified, e.g. "test (windows-latest, 3.12)"
    job_id: str                  # the YAML key, e.g. "test"
    name: str                    # display name with expressions resolved
    runs_on: str
    needs: list = field(default_factory=list)      # job_ids, scoped to this workflow
    steps: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    matrix: dict = field(default_factory=dict)
    if_: str = ""
    blockers: list = field(default_factory=list)   # why NATIVE cannot run it
    act_blockers: list = field(default_factory=list)  # why ACT cannot either
    workflow: str = ""
    workflow_path: str = ""      # act needs -W to disambiguate job names

    @property
    def key(self) -> str:
        """Globally unique handle: `<workflow>::<id>`."""
        return f"{self.workflow}::{self.id}"

    @property
    def supported(self) -> bool:
        return not self.blockers

    def supported_by(self, engine: str) -> bool:
        """act runs real actions and honours container:/services:, so those
        stop being blockers when it is the engine. A missing secret still
        is one — no engine can reproduce a job whose input does not exist."""
        return not (self.act_blockers if engine == "act" else self.blockers)

    def blockers_for(self, engine: str) -> list:
        return list(self.act_blockers if engine == "act" else self.blockers)

    @property
    def foreign_runner(self) -> bool:
        target = runner_os(self.runs_on)
        return bool(target) and target != host_os()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "key": self.key, "job_id": self.job_id,
            "name": self.name,
            "runs_on": self.runs_on, "needs": list(self.needs),
            "matrix": dict(self.matrix), "workflow": self.workflow,
            "workflow_path": self.workflow_path,
            "supported": self.supported, "blockers": list(self.blockers),
            "act_blockers": list(self.act_blockers),
            "act_could_run": not self.act_blockers,
            "foreign_runner": self.foreign_runner,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class Workflow:
    name: str
    path: str
    on: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    jobs: dict = field(default_factory=dict)   # raw job_id -> raw dict
    error: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path,
                "triggers": sorted(self.on.keys()) if self.on else [],
                "jobs": sorted(self.jobs.keys()), "error": self.error}


# ── Discovery + parse ───────────────────────────────────────────────────────

def discover_workflows(project_path) -> list:
    """Every workflow file under .github/workflows, sorted for stable output."""
    root = Path(project_path) / WORKFLOW_DIR
    if not root.is_dir():
        return []
    found: list = []
    for pattern in _WORKFLOW_GLOBS:
        found.extend(root.glob(pattern))
    return sorted(found, key=lambda p: p.name)


def parse_workflow(path) -> Workflow:
    """Parse one workflow file into the normalized IR.

    A malformed file becomes a Workflow carrying `error` rather than raising:
    one unparseable workflow in a repo must not make the whole repo
    un-inspectable, and the error has to survive all the way to the report.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return Workflow(name=path.stem, path=str(path),
                        error=f"{type(exc).__name__}: {exc}")
    if not isinstance(raw, dict):
        return Workflow(name=path.stem, path=str(path),
                        error="workflow root is not a mapping")

    # `on:` is the YAML 1.1 boolean True — a genuine footgun, and the reason a
    # naive parser reports "no triggers" for every workflow ever written.
    on_raw = raw.get("on", raw.get(True, {}))
    if isinstance(on_raw, str):
        on = {on_raw: {}}
    elif isinstance(on_raw, list):
        on = {str(k): {} for k in on_raw}
    elif isinstance(on_raw, dict):
        on = {str(k): (v or {}) for k, v in on_raw.items()}
    else:
        on = {}

    jobs = raw.get("jobs")
    return Workflow(
        name=str(raw.get("name") or path.stem),
        path=str(path),
        on=on,
        env=dict(raw.get("env") or {}),
        jobs=dict(jobs) if isinstance(jobs, dict) else {},
        error="" if isinstance(jobs, dict) and jobs else "no jobs defined",
    )


# ── Matrix expansion ────────────────────────────────────────────────────────

def expand_matrix(strategy: dict) -> list:
    """Cartesian product of `strategy.matrix`, honouring include/exclude.

    Returns `[{}]` for a job with no matrix so callers have one code path.
    """
    if not isinstance(strategy, dict):
        return [{}]
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict) or not matrix:
        return [{}]

    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    axes = {k: v for k, v in matrix.items()
            if k not in ("include", "exclude") and isinstance(v, list)}

    combos: list = []
    if axes:
        keys = list(axes)
        for values in itertools.product(*(axes[k] for k in keys)):
            combos.append({k: v for k, v in zip(keys, values)})
    if not combos:
        combos = [{}]

    def _matches(combo: dict, spec: dict) -> bool:
        return all(str(combo.get(k)) == str(v) for k, v in spec.items())

    if isinstance(exclude, list):
        combos = [c for c in combos
                  if not any(isinstance(e, dict) and _matches(c, e) for e in exclude)]

    # `include` both extends matching combos and appends standalone ones. The
    # real GitHub semantics are subtler; this covers the shapes that appear in
    # practice and anything stranger shows up as an extra combo rather than a
    # missing one — over-running is visible, under-running is not.
    if isinstance(include, list):
        for entry in include:
            if not isinstance(entry, dict):
                continue
            overlay_keys = {k for k in entry if k in axes}
            attached = False
            if overlay_keys:
                for combo in combos:
                    if all(str(combo.get(k)) == str(entry[k]) for k in overlay_keys):
                        combo.update({k: v for k, v in entry.items()
                                      if k not in overlay_keys})
                        attached = True
            if not attached:
                combos.append(dict(entry))

    return combos or [{}]


def _matrix_suffix(combo: dict) -> str:
    if not combo:
        return ""
    return " (" + ", ".join(str(combo[k]) for k in sorted(combo)) + ")"


# ── Instantiation ───────────────────────────────────────────────────────────

def git_context(project_path) -> dict:
    """Real branch/sha for the `github` context — read, never invented.

    Only facts we can actually observe go in here. `event_name` is deliberately
    absent unless the caller declares one: there is no event locally, and
    guessing "push" would make `if: github.event_name == 'push'` evaluate
    against fiction. An absent key raises UnknownRef, which blocks the job with
    a message telling the user to declare the event they mean.
    """
    import subprocess

    def _git(*args) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=str(project_path), capture_output=True,
                text=True, timeout=15, stdin=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if os.name == "nt" else {}),
            )
            return (out.stdout or "").strip()
        except Exception:
            return ""

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    sha = _git("rev-parse", "HEAD")
    ctx: dict = {}
    if branch:
        ctx["ref"] = f"refs/heads/{branch}"
        ctx["ref_name"] = branch
    if sha:
        ctx["sha"] = sha
    return ctx


def _github_context(workflow: Workflow, event: str = "",
                    git: dict = None) -> dict:
    ctx = {"workflow": workflow.name}
    ctx.update(git or {})
    if event:
        ctx["event_name"] = event
    return ctx


def instantiate(workflow: Workflow, event: str = "", git: dict = None) -> list:
    """Expand a parsed workflow into concrete, runnable JobInstances."""
    instances: list = []
    for job_id, raw in (workflow.jobs or {}).items():
        if not isinstance(raw, dict):
            continue
        # A `uses:` at job level is a reusable workflow — a whole feature we do
        # not implement. It gets an instance so it is VISIBLE in inspect output,
        # carrying the blocker that keeps it out of any run.
        needs = raw.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        needs = [str(n) for n in needs if n]

        job_env = dict(raw.get("env") or {})
        steps_raw = raw.get("steps")

        for combo in expand_matrix(raw.get("strategy") or {}):
            github_ctx = _github_context(workflow, event, git)
            ctx = {
                "matrix": {k: v for k, v in combo.items()},
                "env": {**workflow.env, **job_env},
                "github": github_ctx,
            }
            blockers: list = []
            act_blockers: list = []   # survives even with a real action runner
            github_fields = set(github_ctx)

            runs_on_raw = raw.get("runs-on") or ""
            if isinstance(runs_on_raw, dict):       # {group:, labels:}
                runs_on_raw = " ".join(str(v) for v in runs_on_raw.values())
            elif isinstance(runs_on_raw, list):
                runs_on_raw = " ".join(str(v) for v in runs_on_raw)
            runs_on = substitute(str(runs_on_raw), ctx)
            if runs_on.unresolved:
                msg = f"unresolved runs-on expression: {runs_on.unresolved[0]}"
                blockers.append(msg)
                act_blockers.append(msg)

            name = substitute(str(raw.get("name") or job_id), ctx).text

            if raw.get("uses"):
                msg = f"reusable workflow ({raw['uses']}) — not supported locally"
                blockers.append(msg)
                act_blockers.append(msg)

            # container: / services: are native-only limits. act runs both.
            if raw.get("container"):
                blockers.append("job-level `container:` needs the act engine")
            if raw.get("services"):
                blockers.append("job-level `services:` needs the act engine")

            # `if:` is validated here and evaluated in the runner: whether it
            # holds depends on results that do not exist yet, but whether it
            # CAN ever be evaluated is knowable now, and a condition we could
            # never judge must block rather than be quietly ignored.
            job_if = str(raw.get("if") or "")
            if job_if:
                problem = ci_expr.validate(job_if, github_fields)
                if problem:
                    blockers.append(f"job-level {problem}")

            steps: list = []
            if not isinstance(steps_raw, list):
                blockers.append("job has no steps")
                act_blockers.append("job has no steps")
            else:
                for idx, sraw in enumerate(steps_raw):
                    if not isinstance(sraw, dict):
                        continue
                    step, sblockers, s_act = _build_step(
                        idx, sraw, ctx, github_fields)
                    steps.append(step)
                    blockers.extend(sblockers)
                    act_blockers.extend(s_act)

            inst = JobInstance(
                id=f"{job_id}{_matrix_suffix(combo)}",
                job_id=job_id,
                name=name,
                runs_on=runs_on.text,
                needs=needs,
                steps=steps,
                env={**workflow.env, **job_env},
                matrix=dict(combo),
                if_=str(raw.get("if") or ""),
                blockers=blockers,
                act_blockers=act_blockers,
                workflow=workflow.name,
                workflow_path=workflow.path,
            )
            instances.append(inst)
    return instances


def _build_step(index: int, raw: dict, ctx: dict,
                github_fields: set = None) -> tuple:
    """One step, with expressions resolved.

    Returns (Step, native_blockers, act_blockers). The two lists differ only
    where an engine genuinely changes what is reproducible — an unknown
    `uses:` is fatal to the native shell and routine for act.
    """
    blockers: list = []
    act_blockers: list = []
    step_if = str(raw.get("if") or "")
    if step_if:
        problem = ci_expr.validate(step_if, github_fields or set())
        if problem:
            # act evaluates `if:` itself, but a condition reading a value that
            # does not exist locally is unjudgeable on any engine.
            blockers.append(f"step {index} {problem}")
            act_blockers.append(f"step {index} {problem}")
    uses = str(raw.get("uses") or "").strip()
    run_res = substitute(str(raw.get("run") or ""), ctx)
    name = substitute(str(raw.get("name") or ""), ctx).text

    shim = ""
    if uses:
        base = action_name(uses)
        if base in SHIMMED_ACTIONS:
            shim = SHIMMED_ACTIONS[base]
        else:
            # Native has no way to execute somebody else's action; act does,
            # which is the single biggest reason that engine exists.
            blockers.append(
                f"step {index} uses `{uses}` — the native engine cannot run "
                "actions (the act engine can)")
    elif not run_res.text.strip():
        blockers.append(f"step {index} has neither `run` nor `uses`")
        act_blockers.append(f"step {index} has neither `run` nor `uses`")

    if run_res.unresolved:
        # Executing a command with a literal ${{ }} in it would run something
        # other than what CI runs. Refuse the job instead. act substitutes the
        # same expressions, but a missing secret is missing on either engine.
        msg = (f"step {index} has unresolved expression(s): "
               + ", ".join(sorted(set(run_res.unresolved))))
        blockers.append(msg)
        act_blockers.append(msg)

    env_res = {}
    for key, value in (raw.get("env") or {}).items():
        sub = substitute(str(value), ctx)
        if sub.unresolved:
            msg = (f"step {index} env {key} has unresolved expression(s): "
                   + ", ".join(sorted(set(sub.unresolved))))
            blockers.append(msg)
            act_blockers.append(msg)
        env_res[str(key)] = sub.text

    step = Step(
        index=index,
        name=name or (uses or (run_res.text.strip().splitlines() or [""])[0][:60]),
        run=run_res.text,
        uses=uses,
        shell=str(raw.get("shell") or ""),
        env=env_res,
        working_directory=substitute(str(raw.get("working-directory") or ""), ctx).text,
        if_=str(raw.get("if") or ""),
        with_=dict(raw.get("with") or {}),
        shim=shim,
    )
    return step, blockers, act_blockers


# ── DAG ─────────────────────────────────────────────────────────────────────

class CycleError(ValueError):
    """`needs` forms a cycle. GitHub rejects these too; we say which jobs."""


@dataclass
class Dag:
    instances: list = field(default_factory=list)

    @property
    def by_key(self) -> dict:
        return {i.key: i for i in self.instances}

    def _workflows(self) -> list:
        """Workflow names in first-seen order."""
        seen: list = []
        for inst in self.instances:
            if inst.workflow not in seen:
                seen.append(inst.workflow)
        return seen

    def in_workflow(self, workflow: str) -> list:
        return [i for i in self.instances if i.workflow == workflow]

    def deps_of(self, inst: JobInstance) -> list:
        """Instances this one waits on — every matrix cell of each needed job.

        Scoped to the SAME workflow: two workflows may both define `build`, and
        a cross-workflow edge would be invented, not read.
        """
        return [i for i in self.instances
                if i.workflow == inst.workflow and i.job_id in inst.needs]

    def dependents(self, inst: JobInstance) -> list:
        """Instances that wait on *inst*'s job, within its workflow."""
        return [i for i in self.instances
                if i.workflow == inst.workflow and inst.job_id in i.needs]

    def topo_order(self) -> list:
        """Instances in dependency order; siblings keep declaration order.

        Ordering is over JOB KEYS within a workflow, not over instances:
        `build: needs: [test]` waits on every matrix cell of `test`, which is
        what GitHub does. Separate workflows are independent and simply
        concatenate in first-seen order.
        """
        ordered_all: list = []
        for wf in self._workflows():
            members = self.in_workflow(wf)
            job_ids: list = []
            for inst in members:
                if inst.job_id not in job_ids:
                    job_ids.append(inst.job_id)
            needs_map: dict = {}
            for inst in members:
                needs_map.setdefault(inst.job_id, set()).update(
                    n for n in inst.needs if n in job_ids)

            ordered: list = []
            permanent: set = set()
            temporary: set = set()

            def visit(jid: str, trail: list):
                if jid in permanent:
                    return
                if jid in temporary:
                    cycle = " -> ".join(trail + [jid])
                    raise CycleError(f"`needs` cycle in workflow '{wf}': {cycle}")
                temporary.add(jid)
                for dep in sorted(needs_map.get(jid, ())):
                    visit(dep, trail + [jid])
                temporary.discard(jid)
                permanent.add(jid)
                ordered.append(jid)

            for jid in job_ids:
                visit(jid, [])

            rank = {jid: n for n, jid in enumerate(ordered)}
            ordered_all.extend(
                sorted(members, key=lambda i: (rank[i.job_id], i.id)))
        return ordered_all

    def unknown_needs(self) -> list:
        """`needs:` entries naming a job that does not exist in that workflow."""
        known: dict = {}
        for inst in self.instances:
            known.setdefault(inst.workflow, set()).add(inst.job_id)
        missing: list = []
        for inst in self.instances:
            for need in inst.needs:
                if need not in known.get(inst.workflow, set()):
                    missing.append({"job": inst.key, "missing_need": need})
        return missing

    def resolve(self, selector: str) -> list:
        """Instances matching a user-typed selector.

        Accepts a full `key`, an `id`, or a bare `job_id` (all matrix cells).
        Returns every match so an ambiguous short form can be reported rather
        than silently resolving to whichever came first.
        """
        sel = (selector or "").strip()
        if not sel:
            return []
        exact = [i for i in self.instances if i.key == sel]
        if exact:
            return exact
        by_id = [i for i in self.instances if i.id == sel]
        if by_id:
            return by_id
        return [i for i in self.instances if i.job_id == sel]


def build_dag(workflows, event: str = "", git: dict = None) -> Dag:
    """Instances for one or many workflows, as a single DAG."""
    if isinstance(workflows, Workflow):
        workflows = [workflows]
    instances: list = []
    for wf in workflows:
        instances.extend(instantiate(wf, event=event, git=git))
    return Dag(instances=instances)


def inspect_project(project_path, event: str = "") -> dict:
    """Everything `c3 ci inspect` needs, as plain data."""
    files = discover_workflows(project_path)
    workflows = [parse_workflow(p) for p in files]
    dag = build_dag([w for w in workflows if not w.error],
                    event=event, git=git_context(project_path))

    cycle = ""
    try:
        ordered = dag.topo_order()
    except CycleError as exc:
        ordered, cycle = dag.instances, str(exc)

    return {
        "project": str(project_path),
        "host_os": host_os(),
        "workflows": [w.to_dict() for w in workflows],
        "jobs": [i.to_dict() for i in ordered],
        "order": [i.key for i in ordered],
        "cycle": cycle,
        "unknown_needs": dag.unknown_needs(),
        "runnable": [i.key for i in ordered
                     if i.supported and not i.foreign_runner],
        "unsupported": [{"key": i.key, "blockers": i.blockers}
                        for i in ordered if not i.supported],
        "foreign": [{"key": i.key, "runs_on": i.runs_on}
                    for i in ordered if i.supported and i.foreign_runner],
    }
