"""External benchmark adapters."""

from services.bench.external.aider_polyglot import (
    AiderPolyglotBenchmark,
    AiderPolyglotResult,
    detect_aider,
    find_polyglot_repo,
)
from services.bench.external.swe_bench import (
    SWEBenchAdapter,
    SWEBenchTask,
    SWEBenchResult,
    SWEBenchReport,
    load_tasks,
    evaluate_with_docker,
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
