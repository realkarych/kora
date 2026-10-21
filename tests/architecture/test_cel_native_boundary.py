import ast
from pathlib import Path

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
BINDING: Path = ROOT / "src/kora/cel_eval/binding.py"


def bypasses(source: str) -> list[int]:
    findings: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "native_compile":
            findings.append(node.lineno)
        if isinstance(node, ast.ImportFrom):
            findings.extend(
                node.lineno for alias in node.names if alias.name == "native_compile"
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"getattr", "vars"} and any(
                isinstance(argument, ast.Constant)
                and argument.value == "native_compile"
                for argument in node.args
            ):
                findings.append(node.lineno)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "native_compile":
                findings.append(node.lineno)
    return findings


def test_cel_native_compilation_requires_checked_expression() -> None:
    sources = sorted((ROOT / "src").rglob("*.py"))

    violations = {
        str(path.relative_to(ROOT)): bypasses(path.read_text())
        for path in sources
        if path != BINDING
    }

    assert not {path: lines for path, lines in violations.items() if lines}


@pytest.mark.parametrize(
    "source",
    [
        "binding.native_compile('raw')",
        "compile_alias = binding.native_compile",
        "from kora.cel_eval.binding import native_compile as alias",
        "getattr(binding, 'native_compile')('raw')",
        "vars(binding)['native_compile']('raw')",
    ],
)
def test_cel_native_boundary_detects_aliases(source: str) -> None:
    findings = bypasses(source)

    assert findings == [1]
