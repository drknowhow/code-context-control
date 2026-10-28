"""Keep OpenBLAS to one thread in C3's own long-lived processes.

numpy's bundled OpenBLAS starts one worker per core when numpy loads and
commits a buffer for each: on a 20-core machine ``import numpy`` added
652 MB of commit charge and 19 threads to every MCP and UI server, for BLAS
work C3 never does (``OPENBLAS_NUM_THREADS=1``: 41 MB, tool latency
unchanged). Commit charge counts against the machine's commit limit even
when the pages are never touched.

``limit()`` must run before numpy is imported. Commands C3 runs for the user
(``c3_shell``, ``c3_shell_job``, local CI) get the user's own setting back
through ``user_env()``.
"""
import os

_VAR = "OPENBLAS_NUM_THREADS"
#: Set beside _VAR only when C3 chose the value, so user_env() can tell a
#: limit C3 added from one the user set themselves.
_MARK = "C3_SET_OPENBLAS_NUM_THREADS"


def limit() -> None:
    """Pin OpenBLAS to one thread unless the environment already sets it."""
    if _VAR not in os.environ:
        os.environ[_VAR] = "1"
        os.environ[_MARK] = "1"


def user_env(env: dict) -> dict:
    """Drop the limit C3 added from *env* (in place) and return it."""
    if env.pop(_MARK, None) is not None and env.get(_VAR) == "1":
        env.pop(_VAR, None)
    return env
