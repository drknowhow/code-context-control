"""docstrings-only: every public function and method has a docstring, and the
code is otherwise identical to the fixture. argv: <work copy> <fixture>."""
import ast
import sys
from pathlib import Path


def without_docstrings(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)


work, fixture = Path(sys.argv[1]), Path(sys.argv[2])
new = (work / "inv" / "orders.py").read_text(encoding="utf-8")
old = (fixture / "inv" / "orders.py").read_text(encoding="utf-8")
if without_docstrings(new) != without_docstrings(old):
    sys.exit("code other than docstrings changed")
missing = [n.name for n in ast.walk(ast.parse(new))
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
           and not ast.get_docstring(n)]
if missing:
    sys.exit(f"no docstring: {missing}")
