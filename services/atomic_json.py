"""One durable same-directory replace for the small JSON stores C3 writes.

Why this module exists
----------------------
C3 keeps a handful of tiny JSON files that more than one writer touches: the
agent lock store, the per-scope ``config.json`` written by the CLI, the hub
and the mobile API, and the shell job store. Every one of them had grown the
same three-line publish step by copy — ``<name>.tmp`` then a single
``os.replace`` — and on Windows that shape is a live bug rather than a style
nit:

1. **A shared temp name races on the TEMP, not on the target.**
   ``os.replace`` is atomic on the destination; nothing at all protects
   ``<name>.tmp``. Two writers of one path both create it, so one publishes
   a file the other is still writing, or the replace raises
   ``PermissionError: [WinError 5] Access is denied`` because the loser still
   holds a handle to it. Observed for real in the shell job store, where it
   crashed a supervisor and reported "failed before running" for a command
   that was fine (fixed there in 2.118.1, PR #157). The other copies had the
   identical shape and no demonstrated failure only because they are written
   far less often — the lock store especially is a coin that had not landed
   on its edge yet.
2. **A pid alone is not a unique suffix.** Threads share a pid, and the hub,
   the Oracle and the lock store are all written from threaded servers. Six
   threads are enough to drive a ``.tmp<pid>`` implementation into both
   ``PermissionError`` and ``FileNotFoundError`` within a second, so the
   suffix carries per-WRITE randomness as well.
3. **One attempt turns a transient sharing violation into a lost write.** An
   AV scanner or the Search indexer holding the target open for a few
   milliseconds is ordinary on Windows and clears on its own; a single
   ``os.replace`` turns it into an exception at the call site.

``cli/_hook_utils._atomic_write_json`` deliberately does NOT import this. A
PreToolUse hook is a separate, short-lived, single-threaded process that
imports nothing from ``services`` on purpose, so pid-only is genuinely
enough there. That one duplication stays.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

# Eight attempts across ~2.5s of doubling backoff. Four attempts over 0.14s
# was sized for a scanner or indexer holding a handle; it was not enough for
# the third case, several threads replacing the same destination at once.
# Two concurrent MoveFileEx calls on one target can also answer WinError 5,
# and on a loaded CI runner the loser can wait longer than 0.14s for its turn
# (windows-latest hit it in two of four runs of the six-thread test). The
# budget only costs time on the failure path; a failure that survives it is
# real and is re-raised.
REPLACE_ATTEMPTS = 8
REPLACE_BACKOFF_S = 0.02


def atomic_tmp_path(path: Path) -> Path:
    """``<name>.tmp<pid>-<random>`` beside ``path`` — unique per write."""
    return path.with_name(f"{path.name}.tmp{os.getpid()}-{secrets.token_hex(4)}")


def write_text_atomic(path, text: str, *, encoding: str = "utf-8",
                      fsync: bool = True) -> None:
    """Publish ``text`` at ``path``: unique temp, then a retried ``os.replace``.

    ``fsync`` flushes the temp's bytes before the replace, so a crash or power
    loss between the two cannot publish a zero-length or half-written file.
    Durable state wants that: a truncated ``config.json`` makes the Access
    Guard read ``<corrupt-config>`` and fail closed on every subsequent call,
    which wedges the whole session. Callers whose file is ephemeral spill
    state pass ``fsync=False`` rather than pay a disk flush per update.

    Newline translation matches ``Path.write_text`` (the default), so this is
    a drop-in for the ``write_text`` + ``os.replace`` pairs it replaces and
    does not churn line endings in files already on disk.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic_tmp_path(path)
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        last_exc: OSError | None = None
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last_exc = exc
                if attempt < REPLACE_ATTEMPTS - 1:
                    time.sleep(REPLACE_BACKOFF_S * (2 ** attempt))
        if last_exc is not None:
            raise last_exc
    finally:
        # A successful replace already consumed tmp, so this only fires on the
        # failure paths — otherwise abandoned temps accumulate next to the
        # file, and for `.c3/` that means they get indexed and committed.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_json_atomic(path, data, *, indent: int = 2,
                      ensure_ascii: bool = True,
                      trailing_newline: bool = True,
                      fsync: bool = True) -> None:
    """``write_text_atomic`` over ``json.dumps``.

    The serialisation knobs exist so each caller keeps the exact bytes it
    wrote before this module: the point of the change is the publish step,
    not a reformat of five JSON files that other tools and diffs already read.
    """
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    if trailing_newline:
        text += "\n"
    write_text_atomic(path, text, fsync=fsync)
