"""Basic Python fixture for the map-eval harness (hand-annotated in fixture_suite.jsonl)."""
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30.0


def retry(times: int):
    """Decorator factory; ``wrap`` is an inner function and must not be a symbol."""
    def wrap(fn):
        return fn
    return wrap


class Worker:
    """A worker that runs jobs."""

    def __init__(self, name: str, timeout: float = DEFAULT_TIMEOUT):
        self.name = name
        self.timeout = timeout

    @property
    def label(self) -> str:
        return f"worker:{self.name}"

    def run(self, job: dict) -> bool:
        """Run one job."""
        return bool(job)


class Scheduler:
    """Schedules workers."""

    def __init__(self, workers: list):
        self.workers = workers

    def run(self, jobs: list) -> int:
        """Run every job on the first worker."""
        return sum(1 for j in jobs if self.workers[0].run(j))

    @staticmethod
    def build(names: list) -> "Scheduler":
        return Scheduler([Worker(n) for n in names])


@retry(times=MAX_RETRIES)
def fetch(url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Fetch a URL (decorated)."""
    return url if timeout > 0 else None


async def gather_all(urls: list) -> list:
    """Fetch many URLs concurrently."""
    await asyncio.sleep(0)
    return [fetch(u) for u in urls]


def render(
    template: str,
    context: dict,
    strict: bool = False,
) -> str:
    """Multi-line signature with a return annotation."""
    return template.format(**context) if strict else template
