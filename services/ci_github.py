"""AgentCI — publishing a local run back to GitHub (Phase 8 / PRD 5, partial).

The plan asks for a GitHub App posting check runs. This is the honest slice
that can exist without one: a **commit status** posted through the `gh` CLI's
existing authentication. No App to register, no callback URL, no secret for C3
to hold — and `gh` already knows who you are.

Three refusals, because a status is a claim other people act on:

1. **A dirty tree cannot be published.** The status attaches to a commit sha,
   and if the working tree differs from that commit then the thing that ran is
   not the thing being labelled. This is the single most important guard here.
2. **A partial result cannot be published as success.** `PARTIAL_PASS` means
   something did not run; on GitHub the available states are success / failure
   / pending / error, none of which mean "we checked some of it". Rather than
   pick a misleading one, C3 refuses unless explicitly forced, and then posts
   `pending` with the reason in the description.
3. **An unpushed commit cannot be published.** The sha must exist on the
   remote or the status has nothing to attach to.

The context defaults to `agentci/local` so nobody mistakes a laptop run for the
hosted CI, and the description always says which host and engine produced it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_CONTEXT = "agentci/local"


def _run(args: list, cwd, timeout: int = 60) -> tuple:
    try:
        out = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {}),
        )
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def availability(project) -> dict:
    """Can a status be published from here, and if not, why not."""
    info = {"ok": False, "gh": shutil.which("gh") or "", "reason": ""}
    if not info["gh"]:
        info["reason"] = ("the `gh` CLI is not installed — C3 publishes through "
                          "your existing gh authentication rather than holding "
                          "a token of its own")
        return info
    code, out = _run([info["gh"], "auth", "status"], project)
    if code != 0:
        info["reason"] = f"`gh auth status` failed: {out.strip()[:160]}"
        return info
    code, out = _run([info["gh"], "repo", "view", "--json", "nameWithOwner",
                      "-q", ".nameWithOwner"], project)
    if code != 0 or not out.strip():
        info["reason"] = "this directory has no GitHub remote gh can resolve"
        return info
    info["repo"] = out.strip().splitlines()[-1]
    info["ok"] = True
    return info


def _git(project, *args) -> str:
    code, out = _run(["git", *args], project)
    return out.strip() if code == 0 else ""


def preflight(project, run: dict) -> dict:
    """Everything that must be true before a status may be posted."""
    checks: dict = {"ok": False, "reason": "", "sha": "", "state": "",
                    "description": ""}

    fingerprint = run.get("fingerprint") or {}
    if fingerprint.get("dirty"):
        checks["reason"] = (
            f"the working tree had {fingerprint.get('dirty_files')} uncommitted "
            "file(s) when this ran, so the commit does not describe what was "
            "actually verified. Commit first, then re-run.")
        return checks

    sha = fingerprint.get("sha") or _git(project, "rev-parse", "HEAD")
    if not sha:
        checks["reason"] = "no commit sha to attach a status to"
        return checks
    checks["sha"] = sha

    verdict = run.get("verdict", "")
    if verdict == "FULL_CI_PASS":
        checks["state"] = "success"
    elif verdict == "FAIL":
        checks["state"] = "failure"
    else:
        checks["state"] = "pending"
        checks["reason"] = (
            f"{verdict} means something did not run. GitHub has no state for "
            "'partially verified', so publishing it would overstate the result. "
            "Pass force=True to post it as `pending` with the reason attached.")
        return checks

    host = run.get("host_os", "?")
    engines = {j.get("engine") for j in run.get("jobs", []) if j.get("engine")}
    checks["description"] = (
        f"local {verdict} on {host} "
        f"({'+'.join(sorted(engines)) or 'native'}) — run {run.get('run_id', '')}"
    )[:140]
    checks["ok"] = True
    return checks


def publish(project, run: dict, context: str = DEFAULT_CONTEXT,
            force: bool = False, dry_run: bool = False) -> dict:
    """Post a commit status for *run*. Returns what happened, never raises."""
    project = Path(project)
    avail = availability(project)
    if not avail["ok"]:
        return {"published": False, "reason": avail["reason"]}

    checks = preflight(project, run)
    if not checks["ok"]:
        if not (force and checks["state"] == "pending"):
            return {"published": False, "reason": checks["reason"],
                    "sha": checks["sha"]}
        checks["description"] = (
            f"local {run.get('verdict')} — NOT a full pass: "
            f"{run.get('note', '')}")[:140]

    # A status on a sha the remote has never seen goes nowhere useful.
    if not _git(project, "branch", "-r", "--contains", checks["sha"]):
        return {"published": False, "sha": checks["sha"],
                "reason": ("this commit is not on any remote branch yet — push "
                           "it before publishing a status for it")}

    payload = ["-f", f"state={checks['state']}",
               "-f", f"context={context}",
               "-f", f"description={checks['description']}"]
    endpoint = f"repos/{avail['repo']}/statuses/{checks['sha']}"

    if dry_run:
        return {"published": False, "dry_run": True, "endpoint": endpoint,
                "state": checks["state"], "sha": checks["sha"],
                "description": checks["description"]}

    code, out = _run([avail["gh"], "api", "-X", "POST", endpoint, *payload],
                     project, timeout=90)
    if code != 0:
        return {"published": False, "sha": checks["sha"],
                "reason": f"gh api failed: {out.strip()[:200]}"}
    try:
        body = json.loads(out)
        url = body.get("url", "")
    except json.JSONDecodeError:
        url = ""
    return {"published": True, "state": checks["state"], "sha": checks["sha"],
            "context": context, "description": checks["description"], "url": url}
