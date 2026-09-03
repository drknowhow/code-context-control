"""Lexical search engine for CodeIndex: SQLite FTS5 (BM25) with a code tokenizer.

Why this exists (docs/search-eval.md, plan phase P2): the hand-rolled TF-IDF
scored every chunk on every query with a tokenizer that dropped digits
(``sha256`` became ``sha``, ``v2`` and ``S256`` vanished), applied stacked
multiplicative boosts, and carried a synonym map written for C3's own
vocabulary. This module gives the index:

* :func:`tokenize_code` — identifiers kept verbatim AND split into their
  camelCase/snake_case parts, digits preserved, no stemming. ``parseIso8601``
  yields ``parseiso8601 parse iso8601``; a query for ``iso8601`` or the whole
  name both hit.
* :class:`Filters` — ``path`` / ``lang`` / ``kind`` narrowing, shared by every
  search action.
* :func:`doc_kind` / :func:`lang_of` — the classification the filters and the
  intent priors use.
* :func:`fts5_available` — FTS5 is a compile-time option of SQLite; probed
  once. The FTS5 table itself lives in :mod:`services.index_store` beside the
  documents and chunks (2.107.0); when FTS5 is missing ``CodeIndex`` keeps its
  TF-IDF path, with the same tokenizer, so the fallback still finds ``v2``.
"""

from __future__ import annotations

import fnmatch
import re
import sqlite3

_IDENT_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")

_fts5_probe: bool | None = None


# ── Tokenizer ───────────────────────────────────────────────────────────────


def split_identifier(ident: str) -> list[str]:
    """``parseIso8601`` -> ['parse', 'iso8601']; ``sha256_digest`` -> ['sha256', 'digest'].

    Splits on underscores and camelCase boundaries only. A letter/digit
    boundary is NOT a split: ``sha256``, ``utf8``, ``oauth2`` and ``v2`` are
    the tokens people type.
    """
    spaced = _CAMEL_2.sub(r"\1 \2", ident)
    spaced = _CAMEL_1.sub(r"\1 \2", spaced)
    return [p.lower() for p in spaced.replace("_", " ").split() if p]


def _keep(token: str) -> bool:
    # Two characters, or one character with a digit (``v2``, ``h2``): a lone
    # letter is noise, a lone digit-bearing token is an identifier.
    return len(token) >= 2 or any(ch.isdigit() for ch in token)


def tokenize_code(text: str, *, dedupe: bool = False) -> list[str]:
    """Verbatim identifiers plus their parts, lowercased, digits kept.

    ``dedupe=True`` (queries) keeps first occurrences only; index text keeps
    repeats so term frequency survives.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _IDENT_RE.finditer(text or ""):
        ident = m.group(0)
        parts = split_identifier(ident)
        candidates = []
        low = ident.lower()
        if len(parts) > 1 and _keep(low):
            candidates.append(low)
        candidates.extend(p for p in parts if _keep(p))
        for tok in candidates:
            if dedupe:
                if tok in seen:
                    continue
                seen.add(tok)
            out.append(tok)
    return out


# ── Classification ──────────────────────────────────────────────────────────

_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "testing"}
_DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc", ".tex"}
_CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env.example", ".xml", ".properties"}
_CONFIG_DIRS = {"configs", "config", ".github", "conf", "settings"}
_TEST_FILE_RE = re.compile(
    r"(^test_.*|.*_test\.(py|go|rb|rs|php|ex|exs)$|.*\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$"
    r"|.*Tests?\.(java|kt|cs|swift|scala)$|^conftest\.py$)")

_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".cs": "csharp", ".fs": "fsharp", ".vb": "vb",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".pm": "perl", ".lua": "lua",
    ".r": "r", ".R": "r", ".jl": "julia", ".swift": "swift", ".m": "objc", ".mm": "objc",
    ".dart": "dart", ".c": "c", ".h": "c", ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp",
    ".hpp": "cpp", ".hxx": "cpp", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".fish": "shell", ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css",
    ".less": "css", ".vue": "vue", ".svelte": "svelte", ".md": "markdown", ".mdx": "markdown",
    ".rst": "rst", ".tex": "tex", ".adoc": "asciidoc", ".txt": "text",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".xml": "xml", ".csv": "csv", ".sql": "sql", ".graphql": "graphql",
    ".gql": "graphql", ".prisma": "prisma", ".proto": "protobuf", ".tf": "terraform",
    ".hcl": "hcl", ".nix": "nix", ".zig": "zig", ".nim": "nim", ".hs": "haskell",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".clj": "clojure", ".cljs": "clojure",
    ".elm": "elm", ".ml": "ocaml", ".mli": "ocaml",
}


def _ext_of(rel: str) -> str:
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".env.example"):
        return ".env.example"
    return ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""


def lang_of(rel: str) -> str:
    """Language name for a relative path ('' when unknown)."""
    ext = _ext_of(rel)
    return _LANG_BY_EXT.get(ext) or _LANG_BY_EXT.get(ext.lower(), "")


def doc_kind(rel: str) -> str:
    """One of ``test`` | ``doc`` | ``config`` | ``source`` for a relative path."""
    norm = rel.replace("\\", "/")
    parts = norm.lower().split("/")
    name = parts[-1]
    dirs = set(parts[:-1])
    ext = _ext_of(norm)
    if dirs & _TEST_DIRS or _TEST_FILE_RE.match(name):
        return "test"
    if ext in _DOC_EXTS or "docs" in dirs or "doc" in dirs:
        return "doc"
    if ext in _CONFIG_EXTS or dirs & _CONFIG_DIRS:
        return "config"
    return "source"


def _split_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = str(value).split(",")
    return [v.strip().lower() for v in items if v and v.strip()]


class Filters:
    """Normalised ``path`` / ``lang`` / ``kind`` filters.

    ``path``: fnmatch globs against the relative path (``src/**``, ``*.go``,
    ``services/indexer.py``); a bare name also matches as a substring.
    ``lang``: language names or bare extensions (``python``, ``py``).
    ``kind``: doc kinds (``source``, ``test``, ``doc``, ``config``) and/or
    chunk types (``function``, ``class``, ``method``, ``heading`` ...).
    """

    def __init__(self, path=None, lang=None, kind=None):
        self.paths = _split_list(path)
        langs = _split_list(lang)
        self.langs = set()
        for item in langs:
            ext = item if item.startswith(".") else "." + item
            self.langs.add(_LANG_BY_EXT.get(ext, item))
        self.kinds = set(_split_list(kind))

    def __bool__(self) -> bool:
        return bool(self.paths or self.langs or self.kinds)

    def key(self) -> tuple:
        return (tuple(self.paths), tuple(sorted(self.langs)), tuple(sorted(self.kinds)))

    def path_ok(self, rel: str) -> bool:
        if not self.paths:
            return True
        norm = rel.replace("\\", "/").lower()
        base = norm.rsplit("/", 1)[-1]
        for pat in self.paths:
            p = pat.replace("\\", "/")
            if fnmatch.fnmatchcase(norm, p) or fnmatch.fnmatchcase(base, p) \
                    or fnmatch.fnmatchcase(norm, "*/" + p) or p in norm:
                return True
        return False

    def lang_ok(self, rel: str) -> bool:
        return not self.langs or lang_of(rel) in self.langs

    def kind_ok(self, rel: str, chunk_type: str) -> bool:
        if not self.kinds:
            return True
        return doc_kind(rel) in self.kinds or (chunk_type or "").lower() in self.kinds

    def doc_ok(self, rel: str) -> bool:
        return self.path_ok(rel) and self.lang_ok(rel)

    def chunk_ok(self, rel: str, chunk_type: str) -> bool:
        return self.doc_ok(rel) and self.kind_ok(rel, chunk_type)


# ── Intent priors ───────────────────────────────────────────────────────────

_TEST_INTENT = {"test", "tests", "testing", "spec", "specs", "pytest", "unittest"}
_DOC_INTENT_LEAD = {"how", "where", "what", "why", "when", "which", "guide", "docs", "documentation"}
_DOC_INTENT_ANY = {"configure", "configuring", "setup", "install", "installing", "deploy",
                   "deploying", "usage", "tutorial", "readme"}


def intent_prior(query_tokens: list[str], kind: str) -> float:
    """Small additive prior for a chunk's doc kind given the query's shape.

    A query that names tests wants tests; a how-to question wants docs. No
    penalty for anything: tests are the right answer for test queries, and
    without an intent signal source and tests compete on content alone.
    """
    toks = [t for t in query_tokens if t]
    if not toks:
        return 0.0
    if kind == "test" and any(t in _TEST_INTENT for t in toks):
        return 0.15
    if kind == "doc" and (toks[0] in _DOC_INTENT_LEAD or any(t in _DOC_INTENT_ANY for t in toks)):
        return 0.15
    return 0.0


# ── FTS5 store ──────────────────────────────────────────────────────────────


def fts5_available() -> bool:
    """Probe once whether this Python's SQLite was compiled with FTS5."""
    global _fts5_probe
    if _fts5_probe is None:
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            conn.close()
            _fts5_probe = True
        except Exception:
            _fts5_probe = False
    return _fts5_probe
