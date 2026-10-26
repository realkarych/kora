import ast
import sys
from pathlib import Path

import pytest

HELPERS: Path = Path(__file__).parents[3] / "src" / "kora" / "plugins" / "vcs" / "git"


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add((node.module or "").split(".")[0])
    return roots


@pytest.mark.parametrize("helper", ["tree_build.py", "decoder.py", "export.py"])
def test_tree_build_imports_only_the_standard_library(helper: str) -> None:
    roots = imported_roots(HELPERS / helper)

    assert roots <= set(sys.stdlib_module_names)
