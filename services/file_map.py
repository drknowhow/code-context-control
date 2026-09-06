"""The canonical file map — ONE renderer for every map C3 serves.

A map is what the model reads to choose a `c3_read` target: one line per
symbol carrying kind, qualified name, the complete signature, the return
type when the source has one, and the exact line range. Nothing else — no
emoji, no column padding, no AI summary, no docstrings unless asked
(docs/file-map.md, C1 of the c3_compress remediation).

    # services/compressor.py (806L python)
    I 12 imports
    K LARGE_FILE_LINE_THRESHOLD [L77-L77]
    C CodeCompressor [L130-L806]
      M CodeCompressor.compress_file(self, filepath: str, mode: str = "structure") -> dict [L166-L188]
    F handle_compress(file_path: str, mode: str, svc, finalize, maybe_facts) -> str [L94-L114]

Every symbol line matches SYMBOL_LINE_RE, so a harness, a test, or the
model can parse the map back without knowing the language. Renders from a
FileMemoryStore record (the `sections` tree the parsers produce); parsers
that emit a flat list (Rust, Go) are nested here by line containment so an
impl's methods still read as `Type.method`.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from core import count_tokens

# Section type (as the parsers emit it) -> kind letter in the map.
KINDS = {
    "class": "C", "function": "F", "method": "M", "property": "P",
    "constant": "K", "variable": "V", "interface": "IF", "type": "T",
    "enum": "E", "struct": "S", "trait": "TR", "impl": "IM",
    "heading": "H", "section": "SEC", "import": "I",
}
# Section types that are never a symbol line of their own.
SKIP_TYPES = {"decorator", "comment", "content"}
# Kinds whose line carries a parameter list when the signature has one.
CALLABLE_KINDS = {"F", "M"}
# Container kinds: children (explicit or by containment) nest under them.
CONTAINER_KINDS = {"C", "IM", "TR", "S", "E", "IF"}
# More imports than this collapse to one count line.
IMPORTS_INLINE_MAX = 6
# Docstring excerpt cap when include_docs=True.
DOC_MAX_CHARS = 120

SYMBOL_LINE_RE = re.compile(
    r"^(?P<indent> *)(?P<kind>I|K|V|C|M|P|F|IF|T|E|S|TR|IM|H|SEC) "
    r"(?P<async>async )?(?P<name>.+?)(?:\((?P<params>.*)\))?"
    r"(?: -> (?P<ret>.+?))? \[L(?P<a>\d+)-L(?P<b>\d+)\]$")

_WS = re.compile(r"\s+")

# Signature grammars, most specific first. Each yields (params, ret, receiver).
_PY_DEF = re.compile(r"^(?:async\s+)?def\s+[\w.]+\s*\((?P<params>.*)\)\s*(?:->\s*(?P<ret>.+?))?\s*:?$")
_PY_CLASS = re.compile(r"^class\s+\w+\s*(?:\((?P<bases>.*)\))?\s*:?$")
_GO_FUNC = re.compile(r"^func\s+(?:\((?P<recv>[^)]*)\)\s*)?\w+\s*\((?P<params>.*?)\)\s*(?P<ret>[^{]*?)\s*\{?$")
_RS_FN = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+)*fn\s+\w+(?:<[^>]*>)?\s*\((?P<params>.*)\)\s*(?:->\s*(?P<ret>[^{]+?))?\s*(?:where\b.*)?\{?$")
_JS_FUNC = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*[\w$]*\s*(?:<[^>]*>)?\s*\((?P<params>.*)\)\s*(?::\s*(?P<ret>[^{]+?))?\s*\{?\s*$")
_JS_ARROW = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::[^=]+)?=\s*(?:async\s+)?\((?P<params>.*)\)\s*(?::\s*(?P<ret>[^=]+?))?\s*=>")
_JS_ARROW1 = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?(?P<params>\w+)\s*=>")
_JS_CLASS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+\w+(?:<[^>]*>)?\s*(?:extends\s+(?P<base>[\w.<>, ]+?))?\s*(?:implements\s+(?P<impl>[\w.<>, ]+?))?\s*\{?$")
_JS_METHOD = re.compile(r"^(?:(?:public|private|protected|static|readonly|async|get|set|override)\s+)*[\w$#]+\s*(?:<[^>]*>)?\s*\((?P<params>.*)\)\s*(?::\s*(?P<ret>[^{]+?))?\s*\{?$")


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _declaration_head(sig: str) -> str:
    """Cut a brace-language signature after its parameter list and return
    type: the text up to the body's `{`, an arrow, or a `;`. Parentheses
    are depth-matched, so a 400-char minified chunk holding several
    declarations yields only the first one's head."""
    i = sig.find("(")
    if i < 0:
        j = sig.find("{")
        return sig if j < 0 else sig[:j].rstrip()
    depth = 0
    close = -1
    for k in range(i, len(sig)):
        ch = sig[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close = k
                break
    if close < 0:
        return sig
    rest = sig[close + 1:]
    cut = len(rest)
    for stop in ("{", ";"):
        j = rest.find(stop)
        if 0 <= j < cut:
            cut = j
    j = rest.find("=>")
    if 0 <= j < cut:
        cut = j + 2   # keep the arrow: the arrow-function grammar needs it
    return (sig[:close + 1] + rest[:cut]).rstrip()


def parse_signature(signature: str, language: str) -> dict:
    """Split a raw signature into params / ret / receiver / bases.

    Whitespace-normalized (a multi-line def becomes one line); nothing is
    truncated. Returns {} when the text has no recognisable parameter list
    so the caller renders the bare name.
    """
    sig = _norm(signature)
    if not sig:
        return {}
    lang = (language or "").lower()
    if lang in ("javascript", "typescript", "go", "rust") and not sig.lstrip().startswith(
            ("class ", "export class", "export default class", "abstract class",
             "struct ", "pub struct", "enum ", "pub enum", "trait ", "pub trait", "impl ", "type ")):
        sig = _declaration_head(sig)
    out: dict = {}
    if lang == "python":
        m = _PY_DEF.match(sig)
        if m:
            out["params"] = _norm(m.group("params"))
            if m.group("ret"):
                out["ret"] = _norm(m.group("ret"))
            return out
        m = _PY_CLASS.match(sig)
        if m and m.group("bases"):
            out["bases"] = _norm(m.group("bases"))
        return out
    if lang == "go":
        m = _GO_FUNC.match(sig)
        if m:
            out["params"] = _norm(m.group("params"))
            ret = _norm(m.group("ret") or "")
            if ret:
                out["ret"] = ret.strip("()") if ret.startswith("(") and "," not in ret else ret
            recv = _norm(m.group("recv") or "")
            if recv:
                # `s *Server` / `Server` -> Server
                out["receiver"] = recv.split()[-1].lstrip("*")
        return out
    if lang == "rust":
        m = _RS_FN.match(sig)
        if m:
            out["params"] = _norm(m.group("params"))
            if m.group("ret"):
                out["ret"] = _norm(m.group("ret"))
        return out
    if lang in ("javascript", "typescript"):
        m = _JS_CLASS.match(sig)
        if m:
            bases = [b.strip() for b in (m.group("base") or "").split(",") if b.strip()]
            if bases:
                out["bases"] = ", ".join(bases)
            return out
        for rx in (_JS_FUNC, _JS_ARROW, _JS_METHOD):
            m = rx.match(sig)
            if m:
                out["params"] = _norm(m.group("params"))
                if m.groupdict().get("ret"):
                    out["ret"] = _norm(m.group("ret"))
                return out
        m = _JS_ARROW1.match(sig)
        if m:
            out["params"] = m.group("params")
        return out
    # Unknown language: a single balanced parenthesised group is a param list.
    m = re.match(r"^[^()]*\((?P<params>.*)\)[^()]*$", sig)
    if m and not sig.lstrip().startswith(("class ", "struct ", "enum ")):
        out["params"] = _norm(m.group("params"))
    return out


#: Languages whose `property` sections are top-level keys, not members.
DATA_LANGUAGES = {"json", "yaml", "toml", "ini"}


def _kind_for(section: dict, language: str) -> Optional[str]:
    stype = str(section.get("type") or "")
    if stype in SKIP_TYPES:
        return None
    if stype == "property" and (language or "").lower() in DATA_LANGUAGES:
        return "SEC"
    kind = KINDS.get(stype)
    if kind is None:
        return None
    if kind == "M" and _is_property(section):
        return "P"
    if kind == "F" and _is_property(section):
        return "P"
    return kind


def _is_property(section: dict) -> bool:
    decos = section.get("decorators") or []
    if any(d == "property" or d.endswith(".setter") or d.endswith(".getter")
           or d == "cached_property" or d.endswith(".cached_property") for d in decos):
        return True
    sig = str(section.get("signature") or "")
    return bool(re.match(r"^\s*(?:get|set)\s+[\w$#]+\s*\(", sig))


def _heading_level(section: dict) -> int:
    name = str(section.get("name") or "")
    m = re.match(r"^h([1-6]):", name)
    if m:
        return int(m.group(1))
    sig = str(section.get("signature") or "").lstrip()
    if sig.startswith("#"):
        return len(sig) - len(sig.lstrip("#"))
    return 1


def _heading_text(section: dict) -> str:
    name = str(section.get("name") or "")
    name = re.sub(r"^h[1-6]:\s*", "", name)
    return _norm(name)


def _first_sentence(doc: str) -> str:
    text = _norm(doc)
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    sent = m.group(1) if m else text
    if len(sent) > DOC_MAX_CHARS:
        sent = sent[:DOC_MAX_CHARS - 1].rstrip() + "…"
    return sent


def _nest_by_containment(sections: list) -> list:
    """Turn a flat section list into a tree by line containment.

    Parsers that never populate `children` (Rust, Go, the regex fallback)
    emit an impl/trait/class followed by its methods as siblings. A
    callable whose range lies inside the nearest preceding container becomes
    that container's child, so it renders qualified. Sections that already
    carry children are left as they are.
    """
    out: list = []
    stack: list = []   # open containers (dicts with a synthetic "_kids" list)
    for sec in sections:
        ls, le = int(sec.get("line_start") or 0), int(sec.get("line_end") or 0)
        while stack and not (stack[-1]["line_start"] <= ls and le <= stack[-1]["line_end"]):
            stack.pop()
        node = dict(sec)
        node["_kids"] = list(sec.get("children") or [])
        if stack and not sec.get("children") and node.get("type") in (
                "function", "method", "property", "constant", "variable", "class", "struct",
                "enum", "type", "interface"):
            stack[-1]["_kids"].append(node)
        else:
            out.append(node)
        if node.get("type") in ("class", "impl", "trait", "struct", "enum", "interface") \
                and le > ls:
            stack.append(node)
    return out


def _display_name(section: dict, kind: str, parent_name: str, language: str,
                  parsed: dict) -> str:
    name = _norm(section.get("name") or "")
    if kind == "IM":
        name = re.sub(r"^impl\s+", "", name)
        # `impl Trait for Type` -> Type
        m = re.match(r"^(?:<[^>]*>\s*)?(?:.+?\s+for\s+)?(?P<t>[\w:<>]+)", name)
        if m:
            name = m.group("t")
    if kind == "H":
        return _heading_text(section)
    if parent_name and kind in ("M", "P", "F", "K", "V", "C", "S", "E", "T", "IF") \
            and not name.startswith(parent_name + "."):
        return f"{parent_name}.{name}"
    if not parent_name and parsed.get("receiver") and kind in ("M", "F"):
        return f"{parsed['receiver']}.{name}"
    return name


def _render_symbol(section: dict, language: str, indent: int, parent_name: str,
                   include_docs: bool, lines: list) -> Optional[str]:
    kind = _kind_for(section, language)
    if kind is None:
        return None
    ls = int(section.get("line_start") or 0)
    le = int(section.get("line_end") or ls)
    if le < ls:
        le = ls
    sig = str(section.get("signature") or "")
    parsed = parse_signature(sig, language) if kind in CALLABLE_KINDS | {"C", "P"} else {}
    # Go: a receiver method is emitted flat; the receiver is the parent.
    if kind == "M" and not parent_name and parsed.get("receiver"):
        pass
    name = _display_name(section, kind, parent_name, language, parsed)
    is_async = bool(section.get("async")) or bool(re.match(r"^\s*(?:export\s+)?async\b", sig)) \
        or bool(re.match(r"^\s*(?:pub\s+)?async\b", sig)) \
        or (language == "python" and bool(re.match(r"^\s*(?:@.*\n)*\s*async\s+def", sig)))
    pad = " " * indent
    parts = [pad + kind + " " + ("async " if is_async else "") + name]
    if kind in CALLABLE_KINDS and "params" in parsed:
        parts[0] += f"({parsed['params']})"
        if parsed.get("ret"):
            parts[0] += f" -> {parsed['ret']}"
    elif kind == "C" and parsed.get("bases"):
        parts[0] += f"({parsed['bases']})"
    parts[0] += f" [L{ls}-L{le}]"
    lines.append(parts[0])
    if include_docs and section.get("doc"):
        sent = _first_sentence(str(section["doc"]))
        if sent:
            lines.append(f'{pad}  "{sent}"')
    return name


def render_map(record: dict, *, include_docs: bool = False,
               max_tokens: Optional[int] = None) -> str:
    """Render a FileMemoryStore record as the canonical map.

    `max_tokens` (search prefetch's inline cap) drops symbol lines from the
    END and appends `… <k> more symbols` — the renderer never changes shape
    to fit, it only shortens.
    """
    path = str(record.get("path") or "").replace("\\", "/")
    total_lines = int(record.get("lines") or 0)
    language = str(record.get("language") or "")
    sections = list(record.get("sections") or [])

    header = f"# {path} ({total_lines}L {language})".rstrip()
    body: list = []

    shape = record.get("shape") or {}
    parser = str(record.get("parser") or "")
    minified = bool(shape.get("minified"))
    if parser == "skipped" or shape.get("oversized"):
        body.append(f"[map:not mapped] {shape.get('bytes', 0):,} bytes on {total_lines} lines, "
                    f"longest line {shape.get('longest_line', 0):,} chars — read with lines=[a,b]")
        return "\n".join([header] + body)
    if parser == "lexical":
        body.append("[map:lexical-fallback] the parse overran its deadline; "
                    "symbols come from a line scan and may be incomplete")
    if minified:
        # A minified bundle's `var` soup is not structure; named declarations are.
        soup = [s for s in sections if s.get("type") in ("variable", "constant")]
        sections = [s for s in sections if s.get("type") not in ("variable", "constant")]
        body.append(f"[map:minified] longest line {shape.get('longest_line', 0):,} chars; "
                    f"{len(soup)} var bindings omitted")
    elif shape.get("generated"):
        body.append("[map:generated] the file says it is generated")

    imports = [s for s in sections if s.get("type") == "import"]
    others = [s for s in sections if s.get("type") != "import"]
    if imports:
        if len(imports) > IMPORTS_INLINE_MAX:
            body.append(f"I {len(imports)} imports")
        else:
            for imp in imports:
                ls = int(imp.get("line_start") or 0)
                le = int(imp.get("line_end") or ls)
                body.append(f"I {_norm(imp.get('signature') or imp.get('name'))} [L{ls}-L{le}]")

    tree = _nest_by_containment(others)

    def _walk(nodes: list, indent: int, parent_name: str) -> None:
        for node in nodes:
            kind = _kind_for(node, language)
            if kind is None:
                continue
            if kind == "H":
                level = max(1, _heading_level(node))
                name = _render_symbol(node, language, (level - 1) * 2, "", include_docs, body)
                continue
            name = _render_symbol(node, language, indent, parent_name, include_docs, body)
            kids = node.get("_kids")
            if kids is None:
                kids = list(node.get("children") or [])
            if kids and kind in CONTAINER_KINDS:
                _walk(kids, indent + 2, name or "")
            elif kids:
                _walk(kids, indent + 2, parent_name)

    _walk(tree, 0, "")

    text = "\n".join([header] + body)
    if max_tokens is not None and count_tokens(text) > max_tokens:
        kept = list(body)
        while kept and count_tokens("\n".join([header] + kept)) > max_tokens:
            kept.pop()
        dropped = len(body) - len(kept)
        text = "\n".join([header] + kept + [
            f"… {dropped} more symbols (map budget {max_tokens} tokens; "
            f"pass symbols=[…] or lines=[a,b] for the part you need)"])
    return text


def parse_map(text: str) -> list[dict[str, Any]]:
    """Parse a canonical map back into symbol dicts (name, kind, a, b, params, ret, async)."""
    out = []
    for line in text.splitlines():
        m = SYMBOL_LINE_RE.match(line)
        if not m:
            continue
        out.append({
            "kind": m.group("kind"),
            "name": m.group("name"),
            "params": m.group("params"),
            "ret": m.group("ret"),
            "async": bool(m.group("async")),
            "a": int(m.group("a")),
            "b": int(m.group("b")),
            "depth": len(m.group("indent")) // 2,
        })
    return out
