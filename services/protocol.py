"""
Compression Protocol

A custom encoding/decoding scheme for prompts and context that reduces token usage
by converting natural language into compressed shorthand.

Features:
- Action shorthand (READ -> R, FIX -> FX, CREATE -> CR, etc.)
- Path abbreviation
- Common phrase compression
- Project-specific dictionary building
- Reversible encoding
"""
import json
import re
from pathlib import Path

from core import measure_savings

# Core action dictionary
ACTION_CODES = {
    # File operations
    "read": "R", "write": "W", "create": "CR", "delete": "DEL",
    "edit": "ED", "modify": "MOD", "update": "UPD", "rename": "RN",
    "move": "MV", "copy": "CP",
    # Code operations
    "fix": "FX", "debug": "DBG", "refactor": "RFT", "optimize": "OPT",
    "test": "TST", "add": "ADD", "remove": "RM", "implement": "IMP",
    "extract": "EXT", "inline": "INL", "wrap": "WRP",
    # Analysis
    "explain": "EXP", "analyze": "ANL", "review": "REV",
    "find": "FND", "search": "SRCH", "list": "LST", "show": "SHW",
    "compare": "CMP", "check": "CHK",
    # Common qualifiers
    "the": "", "this": "", "that": "", "a": "", "an": "",
    "please": "", "can you": "", "could you": "", "would you": "",
    "i want to": "", "i need to": "", "i'd like to": "",
    "help me": "", "go ahead and": "",
}

# Common programming terms
TERM_CODES = {
    "function": "fn", "variable": "var", "constant": "const",
    "class": "cls", "method": "mth", "property": "prop",
    "interface": "ifc", "type": "typ", "enum": "enm",
    "component": "cmp", "module": "mod", "package": "pkg",
    "import": "imp", "export": "exp", "default": "def",
    "parameter": "param", "argument": "arg", "return": "ret",
    "async": "asc", "await": "awt", "promise": "prom",
    "error": "err", "exception": "exc", "warning": "warn",
    "database": "db", "query": "qry", "schema": "sch",
    "request": "req", "response": "res", "middleware": "mw",
    "authentication": "auth", "authorization": "authz",
    "configuration": "cfg", "environment": "env",
    "typescript": "TS", "javascript": "JS", "python": "PY",
    "react": "RCT", "node": "ND",
    "line": "L", "file": "F", "directory": "D",
    "string": "str", "number": "num", "boolean": "bool",
    "array": "arr", "object": "obj", "null": "nil",
    "undefined": "undef",
}

# Common phrase patterns
PHRASE_PATTERNS = [
    (r"please read the file (.+?) and", r"R:\1"),
    (r"can you (?:please )?look at (.+)", r"R:\1"),
    (r"fix the (?:bug|error|issue) (?:in|on|at) (.+?)(?:\s+(?:where|on|at)\s+line\s+(\d+))?", r"FX:\1 L\2"),
    (r"create a (?:new )?(.+?) (?:file|component|module) (?:called|named) (.+)", r"CR:\2.\1"),
    (r"add (.+?) to (.+)", r"ADD:\1 IN:\2"),
    (r"remove (.+?) from (.+)", r"RM:\1 FROM:\2"),
    (r"refactor (.+?) to (.+)", r"RFT:\1 TO:\2"),
    (r"move (.+?) to (.+)", r"MV:\1 TO:\2"),
    (r"rename (.+?) to (.+)", r"RN:\1 TO:\2"),
    (r"implement (.+?) in (.+)", r"IMP:\1 IN:\2"),
    (r"there(?:'s| is) (?:a |an )?(?:bug|error|issue|problem) (?:in|with|on) (.+)", r"FX:\1"),
    (r"on line (\d+)", r"L\1"),
    (r"the (.+?) (?:is|are) (?:not working|broken|failing)", r"FX:\1"),
    (r"(?:the |)(.+?) (?:doesn't|does not|isn't|is not) (?:work|working)", r"FX:\1"),
]

# Reverse lookup for decoding
REVERSE_ACTIONS = {v: k for k, v in ACTION_CODES.items() if v}
REVERSE_TERMS = {v: k for k, v in TERM_CODES.items()}


class CompressionProtocol:
    """Encode/decode natural language prompts to compressed shorthand."""

    def __init__(self, project_path: str = "", custom_dict_path: str = ".c3/dictionary.json"):
        self.project_path = Path(project_path) if project_path else Path(".")
        self.dict_path = self.project_path / custom_dict_path
        self.custom_dict = self._load_custom_dict()
        self.path_aliases = {}

    def encode(self, text: str) -> dict:
        """Encode natural language to compressed format."""
        original = text
        compressed = text.lower().strip()

        # Step 1: Apply phrase patterns
        for pattern, replacement in PHRASE_PATTERNS:
            compressed = re.sub(pattern, replacement, compressed, flags=re.IGNORECASE)

        # Step 2: Remove filler words
        for word in ["please", "can you", "could you", "would you", "i want to",
                     "i need to", "i'd like to", "help me", "go ahead and",
                     "the", "this", "that"]:
            compressed = re.sub(rf'\b{re.escape(word)}\b', '', compressed, flags=re.IGNORECASE)

        # Step 3: Apply action codes
        for word, code in ACTION_CODES.items():
            if code:  # Skip empty replacements (already removed filler words)
                compressed = re.sub(rf'\b{re.escape(word)}\b', code, compressed, flags=re.IGNORECASE)

        # Step 4: Apply term codes
        for word, code in TERM_CODES.items():
            compressed = re.sub(rf'\b{re.escape(word)}\b', code, compressed, flags=re.IGNORECASE)

        # Step 5: Apply custom dictionary
        for word, code in self.custom_dict.items():
            compressed = re.sub(rf'\b{re.escape(word)}\b', code, compressed, flags=re.IGNORECASE)

        # Step 6: Compress paths
        compressed = self._compress_paths(compressed)

        # Step 7: Clean up whitespace
        compressed = re.sub(r'\s+', ' ', compressed).strip()

        savings = measure_savings(original, compressed)
        savings["original"] = original
        savings["compressed"] = compressed

        return savings

    def decode(self, compressed: str) -> str:
        """Decode compressed format back to readable text."""
        text = compressed

        # Decode action codes (R: -> Read file)
        action_patterns = {
            r'\bR:': "Read file ",
            r'\bW:': "Write to ",
            r'\bCR:': "Create ",
            r'\bDEL:': "Delete ",
            r'\bED:': "Edit ",
            r'\bFX:': "Fix ",
            r'\bDBG:': "Debug ",
            r'\bRFT:': "Refactor ",
            r'\bOPT:': "Optimize ",
            r'\bTST:': "Test ",
            r'\bADD:': "Add ",
            r'\bRM:': "Remove ",
            r'\bIMP:': "Implement ",
            r'\bEXP:': "Explain ",
            r'\bANL:': "Analyze ",
            r'\bREV:': "Review ",
            r'\bFND:': "Find ",
            r'\bSRCH:': "Search for ",
            r'\bMV:': "Move ",
            r'\bRN:': "Rename ",
            r'\bCMP:': "Compare ",
            r'\bCHK:': "Check ",
        }

        for pattern, replacement in action_patterns.items():
            text = re.sub(pattern, replacement, text)

        # Decode term codes
        for code, term in REVERSE_TERMS.items():
            text = re.sub(rf'\b{re.escape(code)}\b', term, text)

        # Decode line references
        text = re.sub(r'\bL(\d+)', r'on line \1', text)

        # Decode modifiers
        text = re.sub(r'\bIN:', 'in ', text)
        text = re.sub(r'\bTO:', 'to ', text)
        text = re.sub(r'\bFROM:', 'from ', text)

        # Clean up
        text = re.sub(r'\s+', ' ', text).strip()
        text = text[0].upper() + text[1:] if text else text

        return text

    def _compress_paths(self, text: str) -> str:
        """Compress file paths using aliases."""
        # Auto-detect common path prefixes
        path_pattern = r'(?:src|lib|app|components|pages|utils|hooks|services|api|config)(?:/\w+)+'
        paths = re.findall(path_pattern, text)

        for path in paths:
            parts = path.split('/')
            if len(parts) > 2:
                # Keep first and last parts
                compressed_path = f"{parts[0]}/../{parts[-1]}"
                text = text.replace(path, compressed_path)

        return text

    def _load_custom_dict(self) -> dict:
        """Load project-specific custom dictionary."""
        if self.dict_path.exists():
            try:
                with open(self.dict_path, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_custom_dict(self):
        """Save custom dictionary to disk."""
        self.dict_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dict_path, 'w', encoding='utf-8') as f:
            json.dump(self.custom_dict, f, indent=2)

    def add_custom_term(self, term: str, code: str):
        """Add a project-specific term to the dictionary."""
        self.custom_dict[term.lower()] = code
        self.save_custom_dict()

    def build_project_dictionary(self) -> dict:
        """Auto-build a project-specific dictionary from codebase analysis."""
        if not self.project_path.exists():
            return {}

        # Find commonly used terms in the project
        term_freq = {}
        skip_dirs = {'node_modules', '.git', '__pycache__', '.c3', 'venv'}
        code_exts = {'.py', '.js', '.ts', '.tsx', '.jsx', '.r', '.R'}

        for fpath in self.project_path.rglob('*'):
            if not fpath.is_file() or fpath.suffix not in code_exts:
                continue
            if any(skip in fpath.parts for skip in skip_dirs):
                continue

            try:
                content = fpath.read_text(errors='replace')
            except Exception:
                continue

            # Extract identifiers
            identifiers = re.findall(r'\b[a-zA-Z_]\w{5,}\b', content)
            for ident in identifiers:
                lower = ident.lower()
                if lower not in ACTION_CODES and lower not in TERM_CODES:
                    term_freq[lower] = term_freq.get(lower, 0) + 1

        # Generate codes for frequent terms
        frequent = sorted(term_freq.items(), key=lambda x: x[1], reverse=True)[:30]
        new_entries = {}

        for term, freq in frequent:
            if freq >= 5:  # Only for terms appearing 5+ times
                # Generate abbreviation
                if len(term) > 6:
                    code = term[:3].upper()
                    # Ensure uniqueness
                    suffix = 1
                    while code in TERM_CODES.values() or code in new_entries.values():
                        code = term[:3].upper() + str(suffix)
                        suffix += 1
                    new_entries[term] = code

        # Merge with existing custom dict
        self.custom_dict.update(new_entries)
        self.save_custom_dict()

        return new_entries

    def get_protocol_header(self) -> str:
        """
        Generate a compression protocol header to include in system prompt.
        This tells Claude how to interpret compressed messages.
        """
        header = """# C3 Compression Protocol
When you see compressed shorthand, decode using:
## Actions: R=Read W=Write CR=Create FX=Fix DBG=Debug RFT=Refactor OPT=Optimize TST=Test ADD=Add RM=Remove IMP=Implement
## Modifiers: L=Line F=File D=Directory IN=in TO=to FROM=from
## Terms: fn=function cls=class cmp=component mod=module cfg=config auth=authentication db=database
## Format: ACTION:target [MODIFIER:value] [context]
## Example: "FX:src/auth.ts L47 TS err missing onClick prop" = "Fix the TypeScript error on line 47 of src/auth.ts where the onClick prop is missing"
"""

        # Add custom dictionary if exists
        if self.custom_dict:
            custom_section = "## Project-specific: " + ' '.join(
                f"{v}={k}" for k, v in list(self.custom_dict.items())[:20]
            )
            header += custom_section + "\n"

        return header

    def batch_encode(self, texts: list) -> list:
        """Encode multiple texts at once."""
        return [self.encode(t) for t in texts]

    def get_stats(self) -> dict:
        """Get compression protocol statistics."""
        return {
            "built_in_actions": len(ACTION_CODES),
            "built_in_terms": len(TERM_CODES),
            "custom_terms": len(self.custom_dict),
            "total_codes": len(ACTION_CODES) + len(TERM_CODES) + len(self.custom_dict),
        }
