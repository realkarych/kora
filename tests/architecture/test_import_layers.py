import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Path = REPOSITORY_ROOT / "src"
LAYER_DOCUMENT: Path = REPOSITORY_ROOT / "knowledge" / "architecture" / "detailed.md"
LAYER_SECTION_HEADING: str = "## Package layers"

ROOT_PACKAGE: str = "kora"
COMPOSITION_ROOTS: frozenset[str] = frozenset(
    {"kora.__main__", "kora.standalone", "kora.testing"}
)
NAMESPACE_MODULES: frozenset[str] = frozenset({"kora", "kora.plugins"})
CEL_BINDING: str = "cel"
CEL_BINDING_OWNER: str = "cel_eval"

BASE: frozenset[str] = frozenset({"ir", "observability"})
EXECUTION_PROVIDER_IMPORTS: frozenset[str] = BASE | {"runner", "executor", "protocols"}
CONTROL_PLANE_PROVIDER_IMPORTS: frozenset[str] = BASE | {"db", "protocols"}
THIN_CLIENT_IMPORTS: frozenset[str] = BASE | {
    "client",
    "models",
    "dsl",
    "cel",
    "protocols",
}

LAYERS: dict[str, frozenset[str]] = {
    "observability": frozenset(),
    "cel": frozenset({"observability"}),
    "ir": frozenset({"cel", "observability"}),
    "protocols": BASE,
    "models": BASE | {"protocols"},
    "client": BASE | {"models", "protocols"},
    "executor": BASE | {"protocols"},
    "runner": BASE | {"executor", "protocols"},
    "db": BASE,
    "cel_eval": BASE | {"cel"},
    "engine": BASE | {"db", "cel_eval", "protocols"},
    "api": BASE | {"engine", "db", "models", "cel_eval", "cel", "protocols"},
    "scheduler": BASE | {"engine", "db", "cel_eval", "protocols"},
    "worker": BASE | {"client", "executor", "models", "protocols"},
    "dsl": BASE | {"cel"},
    "cli": THIN_CLIENT_IMPORTS,
    "mcp": THIN_CLIENT_IMPORTS,
    "plugins.runtime": EXECUTION_PROVIDER_IMPORTS,
    "plugins.harness": EXECUTION_PROVIDER_IMPORTS,
    "plugins.vcs": EXECUTION_PROVIDER_IMPORTS,
    "plugins.policy": EXECUTION_PROVIDER_IMPORTS,
    "plugins.secrets": EXECUTION_PROVIDER_IMPORTS,
    "plugins.authentication": CONTROL_PLANE_PROVIDER_IMPORTS,
    "plugins.approval": CONTROL_PLANE_PROVIDER_IMPORTS,
    "plugins.sensor": CONTROL_PLANE_PROVIDER_IMPORTS,
    "plugins.storage": CONTROL_PLANE_PROVIDER_IMPORTS,
    "plugins.artifact_type": CONTROL_PLANE_PROVIDER_IMPORTS,
    "plugins.exporter": CONTROL_PLANE_PROVIDER_IMPORTS,
}


@dataclass(frozen=True)
class ImportedModule:
    line: int
    target: str


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def describe(self, source_root: Path) -> str:
        return f"{self.path.relative_to(source_root)}:{self.line}: {self.reason}"


def module_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def layer_of(module: str) -> str | None:
    if not is_within(module, ROOT_PACKAGE) or module == ROOT_PACKAGE:
        return None
    relative = module.removeprefix(f"{ROOT_PACKAGE}.")
    matches = [layer for layer in LAYERS if is_within(relative, layer)]
    return max(matches, key=len, default=None)


def is_composition_root(module: str) -> bool:
    return any(is_within(module, root) for root in COMPOSITION_ROOTS)


def resolve_relative(module: str, is_package: bool, level: int, name: str) -> str:
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    return ".".join([*base_parts, name] if name else base_parts)


def imported_modules(path: Path, module: str) -> Iterator[ImportedModule]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportedModule(node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if node.level > 0:
                source = resolve_relative(module, is_package, node.level, source)
            if source in NAMESPACE_MODULES:
                for alias in node.names:
                    yield ImportedModule(node.lineno, f"{source}.{alias.name}")
            else:
                yield ImportedModule(node.lineno, source)


def binding_violation(importer_layer: str | None, target: str) -> str | None:
    if is_within(target, CEL_BINDING) and importer_layer != CEL_BINDING_OWNER:
        return f"only {CEL_BINDING_OWNER} may import the CEL binding {target}"
    return None


def import_violation(importer_layer: str | None, target: str) -> str | None:
    binding = binding_violation(importer_layer, target)
    if binding is not None:
        return binding
    if not is_within(target, ROOT_PACKAGE):
        return None
    if target == ROOT_PACKAGE:
        return "imports the bare package root"
    if is_composition_root(target):
        return f"imports the composition root {target}"
    target_layer = layer_of(target)
    if target_layer is None:
        return f"imports {target}, which is outside the layer table"
    if importer_layer is None:
        return f"imports {target} from a namespace module"
    if target_layer == importer_layer or target_layer in LAYERS[importer_layer]:
        return None
    return f"{importer_layer} may not import {target_layer} ({target})"


def composition_root_violation(target: str) -> str | None:
    return binding_violation(None, target)


def find_violations(source_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted((source_root / ROOT_PACKAGE).rglob("*.py")):
        module = module_name(path, source_root)
        check = composition_root_violation if is_composition_root(module) else None
        importer_layer = layer_of(module)
        if check is None and importer_layer is None and module not in NAMESPACE_MODULES:
            violations.append(Violation(path, 1, f"{module} is outside the table"))
            continue
        for imported in imported_modules(path, module):
            if check is None:
                reason = import_violation(importer_layer, imported.target)
            else:
                reason = check(imported.target)
            if reason is not None:
                violations.append(Violation(path, imported.line, reason))
    return violations


def backticked_names(cell: str) -> frozenset[str]:
    return frozenset(re.findall(r"`([a-z_.]+)`", cell))


def documented_layers(document: str) -> dict[str, frozenset[str]]:
    section = document.split(f"\n{LAYER_SECTION_HEADING}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    layers: dict[str, frozenset[str]] = {}
    for row in rows:
        packages_cell, _, allowed_cell = row.strip("|").split(" | ")
        for package in backticked_names(packages_cell):
            layers[package] = backticked_names(allowed_cell)
    return layers


def write_module(source_root: Path, module: str, source: str) -> None:
    path = source_root.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_every_source_import_follows_the_layer_table() -> None:
    source_root = SOURCE_ROOT

    violations = find_violations(source_root)

    assert [v.describe(source_root) for v in violations] == []


def test_layer_table_matches_the_architecture_document() -> None:
    document = LAYER_DOCUMENT.read_text(encoding="utf-8")

    layers = documented_layers(document)

    assert layers == LAYERS


def test_checker_reports_forbidden_imports(tmp_path: Path) -> None:
    write_module(tmp_path, "kora.__init__", "")
    write_module(tmp_path, "kora.runner.__init__", "import kora.db\n")
    write_module(tmp_path, "kora.ir.__init__", "import cel\n")
    write_module(tmp_path, "kora.worker.claims", "from ..engine import transitions\n")
    write_module(tmp_path, "kora.cel_eval.__init__", "import cel\nimport kora.cel\n")
    write_module(tmp_path, "kora.unlisted", "")
    write_module(
        tmp_path,
        "kora.executor.plans",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import kora.api\n",
    )
    write_module(tmp_path, "kora.dsl.__init__", "from kora import cel, cel_eval\n")
    write_module(tmp_path, "kora.plugins.__init__", "")
    write_module(tmp_path, "kora.plugins.runtime.local", "import kora.db\n")
    write_module(
        tmp_path,
        "kora.plugins.storage.local",
        "from kora.plugins import storage, runtime\n",
    )
    write_module(tmp_path, "kora.client.__init__", "import kora\nimport kora.testing\n")
    write_module(tmp_path, "kora.standalone", "import kora.api\nimport cel\n")

    violations = find_violations(tmp_path)

    assert [v.describe(tmp_path) for v in violations] == [
        "kora/client/__init__.py:1: imports the bare package root",
        "kora/client/__init__.py:2: imports the composition root kora.testing",
        "kora/dsl/__init__.py:1: dsl may not import cel_eval (kora.cel_eval)",
        "kora/executor/plans.py:3: executor may not import api (kora.api)",
        "kora/ir/__init__.py:1: only cel_eval may import the CEL binding cel",
        "kora/plugins/runtime/local.py:1: plugins.runtime may not import db (kora.db)",
        "kora/plugins/storage/local.py:1: "
        "plugins.storage may not import plugins.runtime (kora.plugins.runtime)",
        "kora/runner/__init__.py:1: runner may not import db (kora.db)",
        "kora/standalone.py:2: only cel_eval may import the CEL binding cel",
        "kora/unlisted.py:1: kora.unlisted is outside the table",
        "kora/worker/claims.py:1: worker may not import engine (kora.engine)",
    ]
