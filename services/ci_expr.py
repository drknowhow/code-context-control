"""AgentCI — a GitHub Actions expression evaluator, for `if:` conditions.

Before this existed, `if:` was parsed and ignored, so every guarded step ran
locally whether or not CI would have run it. That over-runs rather than
under-runs — it cannot manufacture a false green — but it wastes time and
reports failures for steps that were never going to execute.

Three rules keep the evaluator honest, and they are the whole design:

1. **An expression we cannot parse blocks the job.** Not "assume true" (which
   silently runs steps CI skips) and not "assume false" (which silently skips
   steps CI runs). Both are guesses; a blocker is a fact.

2. **A reference we cannot resolve blocks the job.** `github.event_name` has no
   honest local value — there is no event. Rather than inventing `"push"` and
   evaluating a condition against fiction, an unknown reference raises, and the
   caller can pass `--event pull_request` to state what it is simulating.

3. **Status functions are runtime, not parse-time.** `success()` depends on
   what already ran, so validation happens when the DAG is built and evaluation
   happens in the runner, with a context that knows results.

Grammar implemented (the subset that appears in real workflows):

    expr    := or
    or      := and ( '||' and )*
    and     := cmp ( '&&' cmp )*
    cmp     := unary ( ('=='|'!='|'<'|'<='|'>'|'>=') unary )?
    unary   := '!' unary | primary
    primary := literal | path | call | '(' expr ')'

Functions: success, failure, always, cancelled, contains, startsWith,
endsWith, format, join, fromJSON, toJSON.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Context roots we can populate. A reference to anything else — `secrets`,
# `vars`, `inputs` — is unresolvable locally and must block rather than
# evaluate to empty, which is how a job passes here and fails in CI.
KNOWN_ROOTS = {"github", "env", "matrix", "runner", "job", "needs", "steps",
               "strategy"}


class ExprError(ValueError):
    """The expression could not be parsed."""


class UnknownRef(ValueError):
    """The expression referenced something we cannot honestly resolve."""


# ── Tokenizer ───────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>'(?:[^']|'')*')
  | (?P<num>-?\d+(?:\.\d+)?)
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>!(),.\[\]])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*)
""", re.VERBOSE)


@dataclass
class Token:
    kind: str
    value: str
    pos: int


def tokenize(src: str) -> list:
    tokens: list = []
    i = 0
    while i < len(src):
        m = _TOKEN_RE.match(src, i)
        if not m:
            raise ExprError(f"unexpected character {src[i]!r} at {i}")
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        tokens.append(Token(kind, m.group(), m.start()))
    return tokens


# ── Parser → a tiny AST of tuples ───────────────────────────────────────────
# ("lit", value) ("path", [parts]) ("call", name, [args])
# ("not", node) ("bin", op, left, right)

class _Parser:
    def __init__(self, tokens: list):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, value: str = ""):
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        if value and tok.value != value:
            raise ExprError(f"expected {value!r} but found {tok.value!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.parse_or()
        if self.peek() is not None:
            raise ExprError(f"trailing input at {self.peek().value!r}")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.peek() and self.peek().value == "||":
            self.take()
            node = ("bin", "||", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_cmp()
        while self.peek() and self.peek().value == "&&":
            self.take()
            node = ("bin", "&&", node, self.parse_cmp())
        return node

    def parse_cmp(self):
        node = self.parse_unary()
        tok = self.peek()
        if tok and tok.value in ("==", "!=", "<", "<=", ">", ">="):
            self.take()
            node = ("bin", tok.value, node, self.parse_unary())
        return node

    def parse_unary(self):
        tok = self.peek()
        if tok and tok.value == "!":
            self.take()
            return ("not", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")

        if tok.value == "(":
            self.take("(")
            node = self.parse_or()
            self.take(")")
            return node

        if tok.kind == "str":
            self.take()
            # GitHub escapes a single quote by doubling it.
            return ("lit", tok.value[1:-1].replace("''", "'"))

        if tok.kind == "num":
            self.take()
            text = tok.value
            return ("lit", float(text) if "." in text else int(text))

        if tok.kind == "ident":
            self.take()
            low = tok.value.lower()
            if low in ("true", "false"):
                return ("lit", low == "true")
            if low == "null":
                return ("lit", None)
            nxt = self.peek()
            if nxt and nxt.value == "(":
                return self.parse_call(tok.value)
            return self.parse_path(tok.value)

        raise ExprError(f"unexpected token {tok.value!r}")

    def parse_call(self, name: str):
        self.take("(")
        args: list = []
        if self.peek() and self.peek().value != ")":
            args.append(self.parse_or())
            while self.peek() and self.peek().value == ",":
                self.take(",")
                args.append(self.parse_or())
        self.take(")")
        return ("call", name.lower(), args)

    def parse_path(self, root: str):
        parts = [root]
        while self.peek() and self.peek().value in (".", "["):
            tok = self.take()
            if tok.value == ".":
                nxt = self.take()
                if nxt.kind not in ("ident", "num"):
                    raise ExprError(f"bad property name {nxt.value!r}")
                parts.append(nxt.value)
            else:
                inner = self.parse_or()
                self.take("]")
                if inner[0] != "lit":
                    raise ExprError("only literal index access is supported")
                parts.append(str(inner[1]))
        return ("path", parts)


def parse(src: str):
    return _Parser(tokenize(src)).parse()


# ── Evaluation ──────────────────────────────────────────────────────────────

@dataclass
class EvalContext:
    """Everything an `if:` may legitimately read, locally.

    `statuses` carries the runtime facts that make status functions meaningful:
    whether anything has failed so far in the relevant scope.
    """
    values: dict = field(default_factory=dict)   # github/env/matrix/... roots
    failed: bool = False                         # something upstream failed
    cancelled: bool = False                      # never true locally


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, dict)):
        return True
    return bool(value)


def _coerce_pair(left, right) -> tuple:
    """GitHub compares loosely. Numbers stay numbers; otherwise compare as
    lower-cased strings, which is what `== 'Push'` vs `'push'` relies on."""
    if isinstance(left, bool) or isinstance(right, bool):
        return _truthy(left), _truthy(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left, right
    return (str(left).lower() if left is not None else "",
            str(right).lower() if right is not None else "")


def _resolve(parts: list, ctx: EvalContext):
    root = parts[0]
    if root not in KNOWN_ROOTS:
        raise UnknownRef(
            f"`{'.'.join(parts)}` — the `{root}` context is not available "
            "locally, so this condition cannot be evaluated honestly")
    value = ctx.values.get(root)
    if value is None:
        raise UnknownRef(f"`{root}` context is not populated for this run")
    for part in parts[1:]:
        if isinstance(value, dict):
            if part not in value:
                raise UnknownRef(
                    f"`{'.'.join(parts)}` — `{part}` is unknown locally. "
                    "If this is the event, pass the event you are simulating.")
            value = value[part]
        else:
            raise UnknownRef(f"`{'.'.join(parts)}` — `{part}` is not a mapping")
    return value


def _call(name: str, args: list, ctx: EvalContext):
    if name == "success":
        return not ctx.failed and not ctx.cancelled
    if name == "failure":
        return ctx.failed and not ctx.cancelled
    if name == "always":
        return True
    if name == "cancelled":
        return ctx.cancelled

    if name == "contains":
        haystack, needle = args[0], args[1]
        if isinstance(haystack, list):
            return any(_coerce_pair(item, needle)[0] == _coerce_pair(item, needle)[1]
                       for item in haystack)
        return str(needle).lower() in str(haystack).lower()
    if name == "startswith":
        return str(args[0]).lower().startswith(str(args[1]).lower())
    if name == "endswith":
        return str(args[0]).lower().endswith(str(args[1]).lower())
    if name == "format":
        out = str(args[0])
        for idx, val in enumerate(args[1:]):
            out = out.replace("{" + str(idx) + "}", str(val))
        return out
    if name == "join":
        items = args[0] if isinstance(args[0], list) else [args[0]]
        sep = str(args[1]) if len(args) > 1 else ","
        return sep.join(str(i) for i in items)
    if name == "tojson":
        return json.dumps(args[0])
    if name == "fromjson":
        try:
            return json.loads(str(args[0]))
        except json.JSONDecodeError as exc:
            raise ExprError(f"fromJSON: {exc}") from exc

    raise ExprError(f"unsupported function `{name}()`")


def _eval(node, ctx: EvalContext):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        return _resolve(node[1], ctx)
    if kind == "call":
        return _call(node[1], [_eval(a, ctx) for a in node[2]], ctx)
    if kind == "not":
        return not _truthy(_eval(node[1], ctx))
    if kind == "bin":
        op = node[1]
        # Short-circuit, so `x && y.z` does not blow up on an unknown y.z when
        # x is already false — same as GitHub, and it keeps honest conditions
        # evaluable even when a branch references something we lack.
        if op == "&&":
            left = _eval(node[2], ctx)
            return _eval(node[3], ctx) if _truthy(left) else left
        if op == "||":
            left = _eval(node[2], ctx)
            return left if _truthy(left) else _eval(node[3], ctx)
        left, right = _eval(node[2], ctx), _eval(node[3], ctx)
        a, b = _coerce_pair(left, right)
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        try:
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            if op == ">=":
                return a >= b
        except TypeError as exc:
            raise ExprError(f"cannot compare {left!r} and {right!r}") from exc
    raise ExprError(f"cannot evaluate node {node!r}")


# ── Public surface ──────────────────────────────────────────────────────────

_STATUS_FNS = ("success(", "failure(", "always(", "cancelled(")


def normalize(src: str) -> str:
    """An `if:` value is already an expression; `${{ }}` around it is optional.

    GitHub also implies `success() &&` unless the condition names a status
    function itself, which is why `if: always()` is the documented way to run a
    step after a failure.
    """
    text = str(src or "").strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    if not text:
        return ""
    if not any(fn in text.replace(" ", "").lower() for fn in _STATUS_FNS):
        text = f"success() && ({text})"
    return text


def validate(src: str, github_fields: set = None) -> str:
    """Parse-check an `if:` at DAG-build time. Returns '' or a blocker reason.

    Checks syntax, reference roots, and — because it is knowable now — which
    `github.*` fields we can actually supply. Values that legitimately vary at
    runtime (`needs.*.result`, `steps.*`) are accepted here and resolved later.

    `github.event_name` is the field this exists for: there is no event
    locally, so a condition on it is unjudgeable until the caller declares
    which event they are simulating.
    """
    text = normalize(src)
    if not text:
        return ""
    try:
        node = parse(text)
    except ExprError as exc:
        return f"cannot parse `if:` condition ({exc})"

    bad_roots: list = []
    bad_fields: list = []
    _collect_refs(node, bad_roots, bad_fields, github_fields or set())
    if bad_roots:
        return (f"`if:` condition reads the `{sorted(set(bad_roots))[0]}` "
                "context, which is not available locally")
    if bad_fields:
        field = sorted(set(bad_fields))[0]
        hint = (" — pass the event you are simulating (e.g. --event push)"
                if field == "event_name" else "")
        return (f"`if:` condition reads `github.{field}`, which has no honest "
                f"local value{hint}")
    return ""


def _collect_refs(node, bad_roots: list, bad_fields: list,
                  github_fields: set) -> None:
    kind = node[0]
    if kind == "path":
        parts = node[1]
        if parts[0] not in KNOWN_ROOTS:
            bad_roots.append(parts[0])
        elif parts[0] == "github" and len(parts) > 1:
            if parts[1] not in github_fields:
                bad_fields.append(parts[1])
    elif kind == "call":
        for arg in node[2]:
            _collect_refs(arg, bad_roots, bad_fields, github_fields)
    elif kind == "not":
        _collect_refs(node[1], bad_roots, bad_fields, github_fields)
    elif kind == "bin":
        _collect_refs(node[2], bad_roots, bad_fields, github_fields)
        _collect_refs(node[3], bad_roots, bad_fields, github_fields)


def evaluate(src: str, ctx: EvalContext) -> bool:
    """True when the guarded job/step should run. Raises on an honest unknown."""
    text = normalize(src)
    if not text:
        return True
    return _truthy(_eval(parse(text), ctx))
