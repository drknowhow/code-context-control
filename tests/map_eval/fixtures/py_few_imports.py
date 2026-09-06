import os
import sys
from pathlib import Path

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/cache"))


def build_index(root: Path) -> dict:
    """Walk root and index files; ``visit`` is local and must not be a symbol."""
    index = {}

    def visit(path: Path) -> None:
        if path.is_file():
            index[path.name] = path.stat().st_size
        else:
            for child in path.iterdir():
                visit(child)

    visit(root)
    return index


class Registry:
    """Holds handlers keyed by name."""

    class Entry:
        """A single registration (nested class)."""

        def __init__(self, name: str, handler):
            self.name = name
            self.handler = handler

        def call(self, *args):
            return self.handler(*args)

    def __init__(self):
        self.entries = {}

    def register(self, name: str, handler) -> "Registry.Entry":
        entry = Registry.Entry(name, handler)
        self.entries[name] = entry
        return entry

    def lookup(self, name: str):
        return self.entries.get(name)


def main(argv: list = sys.argv) -> int:
    print(build_index(CACHE_DIR), argv)
    return 0
