"""Orchestrate a full README screenshot run.

    build demo world -> swap hub registry -> start servers -> seed
    -> assert freshness -> capture -> ALWAYS restore the registry

The registry swap is the dangerous part: ~/.c3/projects.json holds every real
project on the machine, and the Hub renders all of their names and absolute
paths. It is backed up before anything else happens and restored in a finally
block so an exception mid-run cannot leave a developer's registry replaced by
the demo one.

Usage:
    python -m scripts.screenshots.run                  # full run
    python -m scripts.screenshots.run --keep-demo      # leave the demo world
    python -m scripts.screenshots.run --restore-only   # emergency restore
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
C3_HOME = Path.home() / ".c3"
REGISTRY = C3_HOME / "projects.json"
HUB_CONFIG = C3_HOME / "hub_config.json"
BAK_SUFFIX = ".PRE-SCREENSHOT-BAK"

# Single source of truth for the demo location — demo_world picks a root whose
# absolute path is safe to publish (no username in it).
from scripts.screenshots.demo_world import PRIMARY as _PRIMARY_NAME  # noqa: E402
from scripts.screenshots.demo_world import ROOT as DEMO_ROOT  # noqa: E402

PRIMARY = DEMO_ROOT / _PRIMARY_NAME

BANNER_RE = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d+)")


# --------------------------------------------------------------------------
# Registry backup / restore
# --------------------------------------------------------------------------

def backup_state() -> None:
    for path in (REGISTRY, HUB_CONFIG):
        bak = path.with_name(path.name + BAK_SUFFIX)
        if path.exists() and not bak.exists():
            shutil.copy2(path, bak)
            print(f"  backed up {path.name} -> {bak.name}")
        elif bak.exists():
            print(f"  backup already present: {bak.name} (left as-is)")


def restore_state() -> None:
    for path in (REGISTRY, HUB_CONFIG):
        bak = path.with_name(path.name + BAK_SUFFIX)
        if bak.exists():
            shutil.copy2(bak, path)
            bak.unlink()
            print(f"  restored {path.name}")


def swap_registry_to_demo() -> None:
    """Leave only the demo projects visible to the Hub."""
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    projects = data.get("projects", [])
    demo = [p for p in projects if str(DEMO_ROOT) in str(p.get("path", ""))]
    if not demo:
        raise RuntimeError(
            "no demo projects in the registry — run demo_world.build() first")
    data["projects"] = demo
    REGISTRY.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  registry: {len(projects)} entries -> {len(demo)} demo entries")

    cfg = json.loads(HUB_CONFIG.read_text(encoding="utf-8")) if HUB_CONFIG.exists() else {}
    # sidebar_group matters: it defaults to "active", which filters the list
    # down to projects with a live session — one row and a mostly empty page.
    cfg.update({"theme": "dark", "projects_view": "list",
                "main_view": "projects", "sidebar_group": "all",
                "sidebar_collapsed": False})
    HUB_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------

def kill_port_owners(ports: list[int]) -> None:
    """Windows: pkill/kill are unreliable against native processes, and
    SO_REUSEADDR lets a second listener bind while the first still answers.
    Kill by the OS's own record of who owns the port."""
    ps = ("Get-NetTCPConnection -State Listen -LocalPort " +
          ",".join(str(p) for p in ports) +
          " -ErrorAction SilentlyContinue | "
          "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force "
          "-ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=False, capture_output=True)


def start_server(args: list[str], label: str, timeout: int = 120) -> tuple[subprocess.Popen, str]:
    """Launch a server and parse the port from its own banner.

    Never assume the requested port was the one bound: find_free_port()
    (cli/server.py:4211) silently walks upward when a port is taken.
    """
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "cli.c3", *args],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"{label} exited before binding a port")
            continue
        match = BANNER_RE.search(line)
        if match:
            url = f"http://127.0.0.1:{match.group(1)}"
            print(f"  {label} bound {url}")
            return proc, url
    raise TimeoutError(f"{label}: no banner within {timeout}s")


def wait_http(url: str, timeout: int = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                if res.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise TimeoutError(f"{url} never answered 200")


def stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/screenshots/2026-07")
    ap.add_argument("--keep-demo", action="store_true",
                    help="leave the demo world on disk afterwards")
    ap.add_argument("--skip-build", action="store_true",
                    help="reuse an existing demo world")
    ap.add_argument("--restore-only", action="store_true",
                    help="restore the registry backup and exit")
    args = ap.parse_args()

    if args.restore_only:
        print("restoring backed-up state")
        restore_state()
        return 0

    from scripts.screenshots import demo_world, seed, seed_sessions

    ui_proc = hub_proc = None
    try:
        print("[1/7] backing up real state")
        backup_state()

        if not args.skip_build:
            print("[2/7] building demo world")
            demo_world.build()
        else:
            print("[2/7] reusing existing demo world")

        print("[3/7] swapping hub registry to demo-only")
        swap_registry_to_demo()

        # Must precede server start: the server holds current_session in
        # memory, so a session opened afterwards by another process is invisible.
        print("[4/7] seeding session history")
        seed_sessions.main(str(PRIMARY))

        print("[5/7] starting servers")
        kill_port_owners([3330, 3333, 3334, 3335])
        time.sleep(1.5)
        ui_proc, ui_url = start_server(
            ["ui", str(PRIMARY), "--no-browser", "--silent"], "ui")
        hub_proc, hub_url = start_server(["hub", "--no-browser", "--silent"], "hub")
        wait_http(ui_url)
        wait_http(hub_url)

        print("[6/7] seeding demo data")
        seed.main(ui_url)
        # Last, so its events are the most recent: they drive both the Current
        # Session card and the Recent Activity feed.
        seed_sessions.seed_current_session(str(PRIMARY))

        print("[7/7] capturing")
        rc = subprocess.run(
            [sys.executable, "-m", "scripts.screenshots.capture",
             "--ui-url", ui_url, "--hub-url", hub_url, "--out", args.out],
            cwd=str(REPO)).returncode
        if rc != 0:
            print("\ncapture failed", file=sys.stderr)
            return rc
    finally:
        print("\ncleanup")
        stop(ui_proc)
        stop(hub_proc)
        restore_state()
        if not args.keep_demo and not args.skip_build:
            demo_world_mod = sys.modules.get("scripts.screenshots.demo_world")
            if demo_world_mod:
                demo_world_mod.clean()

    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
