"""AgentCI — impact analysis and required-mode planning (spec §14, PRD 3).

Full mode runs everything. Required mode runs the jobs a change could
plausibly have broken, so the edit→validate loop is short enough that an agent
actually uses it.

**The planner is conservative by construction, and that is the whole design.**
A job wrongly skipped is a false green delivered at the moment someone decides
to push — the exact failure this module exists to prevent. A job wrongly run
costs seconds. So the default answer for anything the planner cannot reason
about is RUN, and precision is opt-in: workflow `paths:` filters are honoured
automatically, and `.c3/config.json → ci.required_map` lets a repository state
its own mapping.

Out of the box on a repo with neither, required mode selects everything and
says why for each job. That is not a disappointing V1 — it is the only honest
one. Guessing which tests a change cannot affect, with no dependency graph and
no coverage data, is how a skipped job becomes a shipped bug.

Every decision carries a reason (spec §14), so `plan` output can be read and
argued with rather than trusted.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MODE_FULL = "full"
MODE_REQUIRED = "required"
MODE_JOB = "job"
MODES = (MODE_FULL, MODE_REQUIRED, MODE_JOB)

RUN = "run"
SKIP = "skip"


@dataclass
class Decision:
    job: str
    decision: str
    reason: str

    def to_dict(self) -> dict:
        return {"job": self.job, "decision": self.decision, "reason": self.reason}


@dataclass
class Plan:
    mode: str
    changed: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    base: str = ""
    note: str = ""

    @property
    def selected(self) -> list:
        return [d.job for d in self.decisions if d.decision == RUN]

    @property
    def skipped(self) -> list:
        return [d.job for d in self.decisions if d.decision == SKIP]

    def to_dict(self) -> dict:
        return {"mode": self.mode, "base": self.base, "note": self.note,
                "changed": list(self.changed),
                "selected": self.selected, "skipped": self.skipped,
                "decisions": [d.to_dict() for d in self.decisions]}


# ── Changed files ───────────────────────────────────────────────────────────

def _git(project, *args, strip: bool = True) -> str:
    """Run git. `strip=False` matters for `status --porcelain`.

    Porcelain encodes the status in the FIRST TWO COLUMNS, and an unstaged
    modification is a leading space (` M path`). Stripping the whole output
    eats that space on the first line only, shifting the slice by one and
    silently yielding `ervices/ci_runner.py`. A path that loses a character
    matches no rule, so the planner would quietly mis-decide one job per run.
    """
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(project), capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {}),
        )
        text = out.stdout or ""
        return text.strip() if strip else text
    except Exception:
        return ""


# `XY path`, tolerating either column being a space.
_PORCELAIN = re.compile(r"^(?P<xy>..) (?P<path>.+)$")


def changed_paths(project, base: str = "") -> list:
    """Repo-relative paths that differ from *base* (default: the working tree).

    With no base this reports the uncommitted work — staged, unstaged and
    untracked — because that is what an agent mid-edit is actually validating.
    With a base (`origin/main`) it reports the branch's whole diff, which is
    what a pre-push or pre-PR check wants.
    """
    project = Path(project)
    paths: set = set()

    if base:
        merge_base = _git(project, "merge-base", base, "HEAD") or base
        for line in _git(project, "diff", "--name-only", merge_base).splitlines():
            if line.strip():
                paths.add(line.strip())

    # Uncommitted work always counts: it is present in the tree the run uses.
    for line in _git(project, "status", "--porcelain", strip=False).splitlines():
        match = _PORCELAIN.match(line)
        if not match:
            continue
        entry = match.group("path").strip()
        if " -> " in entry:            # a rename reports both sides
            old, new = entry.split(" -> ", 1)
            paths.update({old.strip().strip('"'), new.strip().strip('"')})
        elif entry:
            paths.add(entry.strip('"'))

    return sorted(p.replace("\\", "/") for p in paths if p)


# ── Repository mapping rules ────────────────────────────────────────────────

def load_rules(project) -> dict:
    """`.c3/config.json → ci.required_map`: {job pattern: [path globs]}.

    A job matched by a pattern runs only when a changed path matches one of its
    globs. A job named by no pattern is never narrowed — see the module
    docstring.
    """
    cfg = Path(project) / ".c3" / "config.json"
    if not cfg.is_file():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ci = data.get("ci") if isinstance(data, dict) else None
    mapping = (ci or {}).get("required_map") if isinstance(ci, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {str(k): [str(g) for g in v] if isinstance(v, list) else [str(v)]
            for k, v in mapping.items()}


def _matches_any(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.rstrip("/") + "/*")
               for g in globs)


# ── Workflow path filters ───────────────────────────────────────────────────

def _workflow_filters(workflow_on: dict, event: str) -> tuple:
    """(paths, paths_ignore) declared for *event*, or ([], []) when absent."""
    if not isinstance(workflow_on, dict):
        return [], []
    candidates = [workflow_on.get(event)] if event else list(workflow_on.values())
    for spec in candidates:
        if isinstance(spec, dict):
            paths = spec.get("paths") or []
            ignore = spec.get("paths-ignore") or []
            if paths or ignore:
                return ([str(p) for p in paths], [str(p) for p in ignore])
    return [], []


# ── The planner ─────────────────────────────────────────────────────────────

def plan_required(project, instances, workflows, changed: list = None,
                  base: str = "", event: str = "") -> Plan:
    """Decide run/skip for every instance, with a reason for each."""
    changed = changed_paths(project, base) if changed is None else list(changed)
    rules = load_rules(project)
    plan = Plan(mode=MODE_REQUIRED, changed=changed, base=base)

    by_name = {w.name: w for w in (workflows or [])}

    if not changed:
        plan.note = ("no changed files — nothing to validate. Use full mode to "
                     "run anyway.")
        for inst in instances:
            plan.decisions.append(Decision(
                inst.key, SKIP, "no files changed relative to "
                                f"{base or 'the last commit'}"))
        return plan

    for inst in instances:
        wf = by_name.get(inst.workflow)
        wf_rel = ""
        if wf and wf.path:
            try:
                wf_rel = str(Path(wf.path).resolve().relative_to(
                    Path(project).resolve())).replace("\\", "/")
            except ValueError:
                wf_rel = ""

        # 1. The workflow definition itself changed — everything in it is suspect.
        if wf_rel and wf_rel in changed:
            plan.decisions.append(Decision(
                inst.key, RUN, f"{wf_rel} changed — the job definition itself moved"))
            continue

        # 2. Workflow path filters are the repository's own statement about
        #    what this workflow cares about, so they are authoritative.
        paths, ignore = _workflow_filters(getattr(wf, "on", {}) or {}, event)
        if paths:
            hits = [c for c in changed if _matches_any(c, paths)]
            if not hits:
                plan.decisions.append(Decision(
                    inst.key, SKIP,
                    f"no changed path matches {inst.workflow}'s `paths:` filter"))
                continue
            plan.decisions.append(Decision(
                inst.key, RUN,
                f"{hits[0]} matches {inst.workflow}'s `paths:` filter"))
            continue
        if ignore and all(_matches_any(c, ignore) for c in changed):
            plan.decisions.append(Decision(
                inst.key, SKIP,
                f"every changed path matches {inst.workflow}'s `paths-ignore:`"))
            continue

        # 3. An explicit repository mapping, if one names this job.
        matched_rule = next(
            (pat for pat in rules
             if fnmatch.fnmatch(inst.key, pat) or fnmatch.fnmatch(inst.job_id, pat)),
            "")
        if matched_rule:
            globs = rules[matched_rule]
            hits = [c for c in changed if _matches_any(c, globs)]
            if hits:
                plan.decisions.append(Decision(
                    inst.key, RUN,
                    f"{hits[0]} matches required_map['{matched_rule}']"))
            else:
                plan.decisions.append(Decision(
                    inst.key, SKIP,
                    f"no changed path matches required_map['{matched_rule}']"))
            continue

        # 4. Nothing said this job is unaffected, so run it. Conservative by
        #    design: the cost of being wrong here is seconds, and the cost of
        #    being wrong the other way is a false green.
        plan.decisions.append(Decision(
            inst.key, RUN,
            "no path filter or required_map rule covers this job — running it, "
            "because skipping on a guess risks a false pass"))

    if not rules and not any(d.reason.startswith("no changed path matches")
                             for d in plan.decisions):
        plan.note = (
            f"{len(plan.selected)} of {len(instances)} job(s) selected. This "
            "repository declares no `paths:` filters and no "
            "`ci.required_map`, so required mode is as broad as full mode — "
            "add a map in .c3/config.json to narrow it.")
    return plan
