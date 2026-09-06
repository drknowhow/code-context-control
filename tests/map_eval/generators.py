"""Deterministic fixture generators for the ``c3 map-eval`` suite.

Two fixtures are too large to check in as literals — a 20 KB single-line
minified bundle and a 250 KB Python module with 400 functions — so they are
built here from a seed, the way ``tests/shell_eval/generators.py`` builds
its streams. Because the generator lays the file out itself, it also knows
the gold: every planted symbol's name, parameters, return annotation and
exact line range. That gold is computed from the layout, never from a
parser, so it cannot be circular.

Determinism rules: all randomness goes through ``random.Random(seed)``; the
seed is ``params.seed`` or a CRC32 of the case id (never ``hash()``).
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field


@dataclass
class Generated:
    filename: str
    content: str
    expected: list[dict] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)


def seed_for(case_id: str, params: dict | None = None) -> int:
    params = params or {}
    if "seed" in params:
        return int(params["seed"])
    return zlib.crc32(case_id.encode("utf-8")) & 0xFFFFFFFF


# ── js_minified ─────────────────────────────────────────────────────────────


def js_minified(params: dict, rng: random.Random) -> Generated:
    """One line of at least ``min_bytes`` (default 20 KiB): ``named`` planted
    top-level function declarations (the gold) buried in ``var`` soup of
    anonymous function expressions, the shape webpack/terser output takes.

    Gold: the planted functions, all on line 1. The filler ``var`` bindings
    are deliberately NOT gold — a map that lists hundreds of one-letter
    bindings on a single line is noise, which is what ``max_map_tokens``
    in the suite line measures.
    """
    named = int(params.get("named", 6))
    min_bytes = int(params.get("min_bytes", 20 * 1024))
    pieces: list[str] = []
    expected: list[dict] = []
    forbidden: list[str] = []

    def filler(i: int) -> str:
        k = rng.randint(2, 97)
        name = f"v{i}"
        forbidden.append(name)
        return f"var {name}=function(t){{return t*{k}+{i}}};"

    slots = max(named, 1)
    per_slot = 6
    i = 0
    for n in range(slots):
        for _ in range(per_slot):
            pieces.append(filler(i))
            i += 1
        fn = f"c3Plant{n}"
        pieces.append(f"function {fn}(a,b){{return a+b+{n}}}")
        expected.append({"kind": "F", "name": fn, "params": "a,b", "line_start": 1, "line_end": 1})
    while sum(len(p) for p in pieces) < min_bytes:
        pieces.append(filler(i))
        i += 1
    content = "".join(pieces) + "\n"
    return Generated("js_minified.js", content, expected, forbidden[:5])


# ── py_large_generated ──────────────────────────────────────────────────────


def py_large_generated(params: dict, rng: random.Random) -> Generated:
    """A Python module of ``functions`` top-level functions (default 400)
    padded to at least ``min_bytes`` (default 250 KiB). Every function has
    the same signature shape ``fn_NNNN(a: int, b: int) -> int`` and a
    docstring; the body length varies with the seed so ranges are not
    arithmetic. Gold: every function with its exact range."""
    n_funcs = int(params.get("functions", 400))
    min_bytes = int(params.get("min_bytes", 250 * 1024))
    per_func_target = max(1, min_bytes // n_funcs)

    lines: list[str] = ['"""Generated large module for the map-eval harness."""', "", "SCALE = 3", ""]
    expected: list[dict] = [{"kind": "K", "name": "SCALE", "line_start": 3, "line_end": 3}]
    forbidden: list[str] = []

    for n in range(n_funcs):
        name = f"fn_{n:04d}"
        start = len(lines) + 1
        body = [
            f"def {name}(a: int, b: int) -> int:",
            f'    """Generated function {n}; the local helper must not be a symbol."""',
            "    total = a + b * SCALE",
        ]
        # Pad with distinct statements until the function is long enough.
        j = 0
        while sum(len(x) + 1 for x in body) < per_func_target:
            k = rng.randint(1, 999)
            body.append(f"    total = (total * {k} + {j}) % 1_000_003  # step {j}")
            j += 1
        if n % 50 == 0:
            helper = f"inner_{n:04d}"
            forbidden.append(helper)
            body.append(f"    def {helper}(x: int) -> int:")
            body.append("        return x + total")
            body.append(f"    total = {helper}(total)")
        body.append("    return total")
        lines.extend(body)
        end = len(lines)
        expected.append({"kind": "F", "name": name, "params": "a: int, b: int", "ret": "int",
                         "line_start": start, "line_end": end})
        lines.extend(["", ""])
    content = "\n".join(lines).rstrip("\n") + "\n"
    return Generated("py_large_generated.py", content, expected, forbidden)


GENERATORS = {
    "js_minified": js_minified,
    "py_large_generated": py_large_generated,
}


def generate(name: str, params: dict | None = None, *, case_id: str = "") -> Generated:
    if name not in GENERATORS:
        raise KeyError(f"unknown generator {name!r}; known: {sorted(GENERATORS)}")
    params = dict(params or {})
    rng = random.Random(seed_for(case_id or name, params))
    return GENERATORS[name](params, rng)
