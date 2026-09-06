"""Malformed module: an unclosed paren in ``broken`` splits the file in two.

Symbols before and after the error must still be listed.
"""
import os

LIMIT = 10


class Before:
    """Defined before the syntax error."""

    def ok(self, x: int) -> int:
        return x + LIMIT

    def twice(self, x: int) -> int:
        return self.ok(x) * 2


def broken(a, b):
    total = compute(a, (b + 1
    return total


def after(y: int) -> int:
    """Still a symbol after the syntax error."""
    return y * 2


class After:
    """Defined after the syntax error."""

    def fine(self) -> None:
        pass

    def also_fine(self, path: str) -> bool:
        return os.path.exists(path)
