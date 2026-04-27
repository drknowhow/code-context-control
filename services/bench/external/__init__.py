"""External benchmark adapters."""

from services.bench.external.aider_polyglot import (
    AiderPolyglotBenchmark,
    AiderPolyglotResult,
    detect_aider,
    find_polyglot_repo,
)
from services.bench.external.swe_bench import (
    SWEBenchAdapter,
    SWEBenchReport,
    SWEBenchResult,
    SWEBenchTask,
    evaluate_with_docker,
    load_tasks,
)

__all__ = [
    "AiderPolyglotBenchmark",
    "AiderPolyglotResult",
    "detect_aider",
    "find_polyglot_repo",
    "SWEBenchAdapter",
    "SWEBenchTask",
    "SWEBenchResult",
    "SWEBenchReport",
    "load_tasks",
    "evaluate_with_docker",
]
