import asyncio
import os
import subprocess
import sys
from pathlib import Path


def get_c3_path():
    # Resolves to the root directory's cli/c3.py
    current_dir = Path(__file__).resolve().parent
    root_dir = current_dir.parent
    c3_path = root_dir / "cli" / "c3.py"
    return str(c3_path), str(root_dir)
async def run_cmd_async(*args):
    """Runs a c3 command asynchronously and returns stdout."""
    c3_path, root_dir = get_c3_path()

    env = os.environ.copy()
    env["PYTHONPATH"] = root_dir

    cmd = [sys.executable, c3_path] + list(args)

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        **kwargs
    )

    stdout, _ = await process.communicate()
    return stdout.decode("utf-8", errors="replace")

def run_cmd(*args):
    """Runs a c3 command synchronously."""
    c3_path, root_dir = get_c3_path()

    env = os.environ.copy()
    env["PYTHONPATH"] = root_dir

    cmd = [sys.executable, c3_path] + list(args)

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        **kwargs
    )
    return result.stdout
