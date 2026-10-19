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
    {"kora.__main__", "kora.standalone", "kora.testing", "kora.conformance"}
)
NAMESPACE_MODULES: frozenset[str] = frozenset({"kora.plugins"})
PROVIDER_LOADING_MODULE: str = "kora.protocols.loading"
CEL_BINDING: str = "cel"
CEL_BINDING_OWNER: str = "cel_eval"
DYNAMIC_IMPORT_CALLS: frozenset[str] = frozenset(
    {"importlib.import_module", "import_module", "__import__", "pkgutil.resolve_name"}
)

BASE: frozenset[str] = frozenset({"ir", "observability"})
SUPERVISOR_PROVIDER_IMPORTS: frozenset[str] = BASE | {"runner", "executor", "protocols"}
PLAIN_PROVIDER_IMPORTS: frozenset[str] = BASE | {"protocols"}
ROW_READING_PROVIDER_IMPORTS: frozenset[str] = BASE | {"db", "protocols"}

LAYERS: dict[str, frozenset[str]] = {
    "kora": frozenset(),
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
    "dsl": BASE | {"cel", "protocols"},
    "cli": BASE | {"client", "models", "dsl", "cel", "protocols"},
    "mcp": BASE | {"client", "models", "protocols"},
    "mcp.stdio": BASE | {"mcp", "client", "models", "dsl", "cel", "protocols"},
    "plugins.runtime": SUPERVISOR_PROVIDER_IMPORTS,
    "plugins.harness": SUPERVISOR_PROVIDER_IMPORTS,
    "plugins.vcs": BASE | {"executor", "protocols"},
    "plugins.policy": PLAIN_PROVIDER_IMPORTS,
    "plugins.secrets": PLAIN_PROVIDER_IMPORTS,
    "plugins.authentication": ROW_READING_PROVIDER_IMPORTS,
    "plugins.approval": ROW_READING_PROVIDER_IMPORTS,
    "plugins.sensor": PLAIN_PROVIDER_IMPORTS,
    "plugins.storage": PLAIN_PROVIDER_IMPORTS,
    "plugins.artifact_type": PLAIN_PROVIDER_IMPORTS,
    "plugins.exporter": PLAIN_PROVIDER_IMPORTS,
}

SERVER_FREE: frozenset[str] = frozenset(
    {
        "observability",
        "cel",
        "ir",
        "protocols",
        "models",
        "client",
        "executor",
        "runner",
        "worker",
        "dsl",
        "cli",
        "mcp",
        "mcp.stdio",
        "plugins.runtime",
        "plugins.harness",
        "plugins.vcs",
        "plugins.policy",
        "plugins.secrets",
    }
)
SERVER: frozenset[str] = frozenset(
    {
        "db",
        "engine",
        "api",
        "scheduler",
        "cel_eval",
        "plugins.authentication",
        "plugins.approval",
    }
)
SERVER_LIBRARIES: frozenset[str] = frozenset(
    {"fastapi", "starlette", "uvicorn", "sqlalchemy", "alembic", "asyncpg", "aiosqlite"}
)
SERVER_LIBRARY_FREE: frozenset[str] = frozenset(
    {
        "observability",
        "cel",
        "ir",
        "protocols",
        "models",
        "client",
        "executor",
        "runner",
        "worker",
    }
)


@dataclass(frozen=True)
class ImportedModule:
    line: int
    target: str
    dynamic: bool


@dataclass(frozen=True)
class SourceModule:
    path: Path
    name: str


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def describe(self, source_root: Path) -> str:
        return f"{self.path.relative_to(source_root)}:{self.line}: {self.reason}"


def is_within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def layer_of(module: str) -> str | None:
    if module == ROOT_PACKAGE:
        return ROOT_PACKAGE
    if not is_within(module, ROOT_PACKAGE):
        return None
    relative = module.removeprefix(f"{ROOT_PACKAGE}.")
    listed = [layer for layer in LAYERS if layer != ROOT_PACKAGE]
    matches = [layer for layer in listed if is_within(relative, layer)]
    return max(matches, key=len, default=None)


def is_composition_root(module: str) -> bool:
    return any(is_within(module, root) for root in COMPOSITION_ROOTS)


def source_modules(source_root: Path) -> list[SourceModule]:
    modules: list[SourceModule] = []
    for path in sorted((source_root / ROOT_PACKAGE).rglob("*.py")):
        parts = list(path.relative_to(source_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules.append(SourceModule(path, ".".join(parts)))
    return modules


def resolve_relative(module: SourceModule, level: int, name: str) -> str:
    parts = module.name.split(".")
    package_parts = parts if module.path.name == "__init__.py" else parts[:-1]
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    return ".".join([*base_parts, name] if name else base_parts)


def call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return f"{function.value.id}.{function.attr}"
    return None


def literal_first_argument(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def imported_modules(
    module: SourceModule, known: frozenset[str]
) -> Iterator[ImportedModule]:
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ImportedModule(node.lineno, alias.name, dynamic=False)
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if node.level > 0:
                source = resolve_relative(module, node.level, source)
            for alias in node.names:
                candidate = f"{source}.{alias.name}"
                target = candidate if candidate in known else source
                yield ImportedModule(node.lineno, target, dynamic=False)
        elif isinstance(node, ast.Call) and call_name(node) in DYNAMIC_IMPORT_CALLS:
            target = literal_first_argument(node)
            if target is not None and is_within(target, ROOT_PACKAGE):
                yield ImportedModule(node.lineno, target, dynamic=True)


def third_party_violation(importer_layer: str, target: str) -> str | None:
    if is_within(target, CEL_BINDING) and importer_layer != CEL_BINDING_OWNER:
        return f"only {CEL_BINDING_OWNER} may import the CEL binding {target}"
    library = target.split(".")[0]
    if library in SERVER_LIBRARIES and importer_layer in SERVER_LIBRARY_FREE:
        return f"{importer_layer} may not import the server library {library}"
    return None


def kora_violation(importer_layer: str, target: str) -> str | None:
    if is_composition_root(target):
        return f"imports the composition root {target}"
    target_layer = layer_of(target)
    if target_layer is None:
        return f"imports {target}, which is outside the layer table"
    if target_layer == importer_layer or target_layer in LAYERS[importer_layer]:
        return None
    return f"{importer_layer} may not import {target_layer} ({target})"


def import_violation(
    module: SourceModule, importer_layer: str, imported: ImportedModule
) -> str | None:
    if imported.dynamic:
        if is_within(module.name, PROVIDER_LOADING_MODULE):
            return None
        return f"dynamically imports {imported.target}"
    if not is_within(imported.target, ROOT_PACKAGE):
        return third_party_violation(importer_layer, imported.target)
    return kora_violation(importer_layer, imported.target)


def find_violations(source_root: Path) -> list[Violation]:
    modules = source_modules(source_root)
    known = frozenset(module.name for module in modules)
    violations: list[Violation] = []
    for module in modules:
        if is_composition_root(module.name):
            continue
        importer_layer = layer_of(module.name)
        if module.name in NAMESPACE_MODULES:
            importer_layer = ROOT_PACKAGE
        if importer_layer is None:
            reason = f"{module.name} is outside the table"
            violations.append(Violation(module.path, 1, reason))
            continue
        for imported in imported_modules(module, known):
            reason = import_violation(module, importer_layer, imported)
            if reason is not None:
                violations.append(Violation(module.path, imported.line, reason))
    return violations


def package_import_graph(source_root: Path) -> dict[str, set[str]]:
    modules = source_modules(source_root)
    known = frozenset(module.name for module in modules)
    graph: dict[str, set[str]] = {}
    for module in modules:
        importer_layer = layer_of(module.name)
        if is_composition_root(module.name) or importer_layer is None:
            continue
        edges = graph.setdefault(importer_layer, set())
        for imported in imported_modules(module, known):
            target_layer = layer_of(imported.target)
            if target_layer is not None and target_layer != importer_layer:
                edges.add(target_layer)
    return graph


def reachable_packages(graph: dict[str, set[str]], start: str) -> set[str]:
    reached: set[str] = set()
    pending = list(graph.get(start, set()))
    while pending:
        package = pending.pop()
        if package not in reached:
            reached.add(package)
            pending.extend(graph.get(package, set()))
    return reached


def server_reach(source_root: Path) -> dict[str, list[str]]:
    graph = package_import_graph(source_root)
    reach: dict[str, list[str]] = {}
    for package in sorted(SERVER_FREE):
        reached_server = sorted(reachable_packages(graph, package) & SERVER)
        if reached_server:
            reach[package] = reached_server
    return reach


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


def write_packages(source_root: Path, packages: list[str]) -> None:
    for package in packages:
        write_module(source_root, f"{package}.__init__", "")


def describe_all(violations: list[Violation], source_root: Path) -> list[str]:
    return [violation.describe(source_root) for violation in violations]


def test_source_tree_respects_package_layers() -> None:
    source_root = SOURCE_ROOT

    module_names = [module.name for module in source_modules(source_root)]
    violations = find_violations(source_root)

    assert ROOT_PACKAGE in module_names
    assert describe_all(violations, source_root) == []


def test_layer_table_matches_documentation() -> None:
    document = LAYER_DOCUMENT.read_text(encoding="utf-8")

    layers = documented_layers(document)

    assert layers == LAYERS


def test_server_free_packages_never_reach_server_packages() -> None:
    source_root = SOURCE_ROOT

    reach = server_reach(source_root)

    assert reach == {}


def test_checker_flags_forbidden_import_forms(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.db", "kora.runner"])
    write_module(tmp_path, "kora.runner.absolute", "import kora.db\n")
    write_module(tmp_path, "kora.runner.from_module", "from kora.db import tables\n")
    write_module(tmp_path, "kora.runner.from_root", "from kora import db\n")
    write_module(tmp_path, "kora.runner.relative", "from .. import db\n")
    write_module(
        tmp_path,
        "kora.runner.function_level",
        "def load() -> None:\n    import kora.db\n",
    )
    write_module(
        tmp_path,
        "kora.runner.type_only",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import kora.db\n",
    )
    write_module(tmp_path, "kora.runner.composition", "import kora.testing\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/runner/absolute.py:1: runner may not import db (kora.db)",
        "kora/runner/composition.py:1: imports the composition root kora.testing",
        "kora/runner/from_module.py:1: runner may not import db (kora.db)",
        "kora/runner/from_root.py:1: runner may not import db (kora.db)",
        "kora/runner/function_level.py:2: runner may not import db (kora.db)",
        "kora/runner/relative.py:1: runner may not import db (kora.db)",
        "kora/runner/type_only.py:3: runner may not import db (kora.db)",
    ]


def test_checker_flags_unlisted_package(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.unlisted", "kora.worker"])
    write_module(tmp_path, "kora.worker.claims", "import kora.unlisted\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/unlisted/__init__.py:1: kora.unlisted is outside the table",
        "kora/worker/claims.py:1: imports kora.unlisted, "
        "which is outside the layer table",
    ]


def test_checker_flags_dynamic_kora_import(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.protocols", "kora.worker"])
    dynamic_source = 'import importlib\nimportlib.import_module("kora.db")\n'
    write_module(tmp_path, "kora.worker.loader", dynamic_source)
    write_module(tmp_path, "kora.protocols.loading", dynamic_source)

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/worker/loader.py:2: dynamically imports kora.db",
    ]


def test_checker_flags_server_library_in_server_free_package(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.client", "kora.db"])
    write_module(tmp_path, "kora.client.transport", "import sqlalchemy\n")
    write_module(tmp_path, "kora.db.tables", "import sqlalchemy\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/client/transport.py:1: client may not import the server library "
        "sqlalchemy",
    ]


def test_only_cel_eval_imports_cel_binding(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.cel", "kora.cel_eval", "kora.ir"])
    write_module(tmp_path, "kora.ir.conditions", "import cel\nimport kora.cel\n")
    write_module(tmp_path, "kora.cel_eval.binding", "import cel\nfrom cel import x\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/ir/conditions.py:1: only cel_eval may import the CEL binding cel",
    ]


def test_checker_flags_transitive_reach_through_widened_row(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.db", "kora.executor", "kora.worker"])
    write_module(tmp_path, "kora.executor.plans", "import kora.db\n")
    write_module(tmp_path, "kora.worker.claims", "import kora.executor\n")

    reach = server_reach(tmp_path)

    assert reach == {"executor": ["db"], "worker": ["db"]}
