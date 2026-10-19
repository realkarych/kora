import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Path = REPOSITORY_ROOT / "src"
LAYER_DOCUMENT: Path = REPOSITORY_ROOT / "knowledge" / "architecture" / "detailed.md"
LAYER_SECTION_HEADING: str = "## Package layers"
DECISION_RECORD: Path = (
    REPOSITORY_ROOT / "docs" / "decisions" / "0026-package-layers.md"
)

ROOT_PACKAGE: str = "kora"
COMPOSITION_ROOTS: frozenset[str] = frozenset(
    {"kora.__main__", "kora.standalone", "kora.testing", "kora.conformance"}
)
NAMESPACE_MODULES: frozenset[str] = frozenset({"kora.plugins"})
PROVIDER_LOADING_MODULE: str = "kora.protocols.loading"
CEL_BINDING: str = "cel"
CEL_BINDING_OWNER: str = "cel_eval"
DYNAMIC_IMPORT_CALLS: frozenset[str] = frozenset(
    {"importlib.import_module", "builtins.__import__", "pkgutil.resolve_name"}
)
BUILTIN_NAMES: dict[str, str] = {"__import__": "builtins.__import__"}

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


def dotted_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def source_modules(source_root: Path) -> list[SourceModule]:
    package_root = source_root / ROOT_PACKAGE
    paths = [*package_root.rglob("*.py"), *package_root.rglob("*.pyi")]
    return [
        SourceModule(path, dotted_name(path, source_root)) for path in sorted(paths)
    ]


def known_modules(source_root: Path) -> frozenset[str]:
    package_root = source_root / ROOT_PACKAGE
    directories = [path for path in package_root.rglob("*") if path.is_dir()]
    names = {dotted_name(path / "__init__.py", source_root) for path in directories}
    names.add(ROOT_PACKAGE)
    names.update(module.name for module in source_modules(source_root))
    return frozenset(names)


def resolve_relative(module: SourceModule, level: int, name: str) -> str:
    parts = module.name.split(".")
    package_parts = parts if module.path.stem == "__init__" else parts[:-1]
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    return ".".join([*base_parts, name] if name else base_parts)


def local_bindings(tree: ast.Module) -> dict[str, str]:
    bindings = dict(BUILTIN_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def call_name(node: ast.Call, bindings: dict[str, str]) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return bindings.get(function.id, function.id)
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        owner = bindings.get(function.value.id, function.value.id)
        return f"{owner}.{function.attr}"
    return None


def literal_argument(node: ast.Call, position: int, keyword: str) -> str | None:
    candidates = [node.args[position]] if len(node.args) > position else []
    candidates += [item.value for item in node.keywords if item.arg == keyword]
    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
    return None


def dynamic_target(node: ast.Call) -> str | None:
    name = literal_argument(node, 0, "name")
    if name is None or not name.startswith("."):
        return name
    package = literal_argument(node, 1, "package")
    if package is None:
        return None
    level = len(name) - len(name.lstrip("."))
    base = package.split(".")[: len(package.split(".")) - (level - 1)]
    remainder = name.lstrip(".")
    return ".".join([*base, remainder] if remainder else base)


def module_level_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            pending.extend(ast.iter_child_nodes(node))


def imported_modules(
    module: SourceModule, known: frozenset[str]
) -> Iterator[ImportedModule]:
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    bindings = local_bindings(tree)
    nodes = module_level_nodes(tree) if module.name == ROOT_PACKAGE else ast.walk(tree)
    for node in nodes:
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
        elif (
            isinstance(node, ast.Call)
            and call_name(node, bindings) in DYNAMIC_IMPORT_CALLS
        ):
            target = dynamic_target(node)
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


def composition_root_violations(
    module: SourceModule, known: frozenset[str]
) -> Iterator[Violation]:
    for imported in imported_modules(module, known):
        if is_within(imported.target, CEL_BINDING):
            reason = f"only {CEL_BINDING_OWNER} may import the CEL binding cel"
            yield Violation(module.path, imported.line, reason)


def find_violations(source_root: Path) -> list[Violation]:
    modules = source_modules(source_root)
    known = known_modules(source_root)
    violations: list[Violation] = []
    for module in modules:
        if is_composition_root(module.name):
            violations.extend(composition_root_violations(module, known))
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
    known = known_modules(source_root)
    graph: dict[str, set[str]] = {}
    for module in modules:
        importer_layer = layer_of(module.name)
        if is_composition_root(module.name) or importer_layer is None:
            continue
        edges = graph.setdefault(importer_layer, set())
        for imported in imported_modules(module, known):
            target_layer = layer_of(imported.target)
            if imported.dynamic or target_layer in (None, importer_layer):
                continue
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


def decision_record_set(document: str, label: str) -> frozenset[str]:
    line = next(line for line in document.splitlines() if f"**{label}**" in line)
    return backticked_names(line.split(" is ", 1)[1])


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


def test_invariant_sets_match_the_decision_record() -> None:
    document = DECISION_RECORD.read_text(encoding="utf-8")

    server_free = decision_record_set(document, "server-free set")
    server = decision_record_set(document, "server set")
    roots_line = next(
        line for line in document.splitlines() if "**Composition roots.**" in line
    )
    roots = backticked_names(roots_line)

    assert server_free == SERVER_FREE
    assert server == SERVER
    assert roots == COMPOSITION_ROOTS
    assert not SERVER_FREE & SERVER
    assert SERVER_FREE | SERVER <= LAYERS.keys()


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
    write_module(tmp_path, "kora.runner.bare_root", "import kora\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/runner/absolute.py:1: runner may not import db (kora.db)",
        "kora/runner/bare_root.py:1: runner may not import kora (kora)",
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
    write_module(
        tmp_path,
        "kora.worker.aliased",
        "import importlib as il\n"
        "from importlib import import_module as load\n"
        "import builtins\n"
        'il.import_module("kora.db")\n'
        'load(name="kora.engine")\n'
        'builtins.__import__("kora.api")\n'
        'importlib.import_module(".scheduler", "kora")\n',
    )

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/worker/aliased.py:4: dynamically imports kora.db",
        "kora/worker/aliased.py:5: dynamically imports kora.engine",
        "kora/worker/aliased.py:6: dynamically imports kora.api",
        "kora/worker/aliased.py:7: dynamically imports kora.scheduler",
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
    write_module(tmp_path, "kora.__main__", "import cel\nimport kora.db\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/__main__.py:1: only cel_eval may import the CEL binding cel",
        "kora/ir/conditions.py:1: only cel_eval may import the CEL binding cel",
    ]


def test_checker_flags_transitive_reach_independent_of_the_table(
    tmp_path: Path,
) -> None:
    write_packages(tmp_path, ["kora", "kora.db", "kora.executor", "kora.worker"])
    write_module(tmp_path, "kora.executor.plans", "import kora.db\n")
    write_module(tmp_path, "kora.worker.claims", "import kora.executor\n")
    write_module(
        tmp_path,
        "kora.protocols.loading",
        'import importlib\nimportlib.import_module("kora.db")\n',
    )

    reach = server_reach(tmp_path)

    assert reach == {"executor": ["db"], "worker": ["db"]}


def test_checker_resolves_namespace_subpackages_and_root_scope(tmp_path: Path) -> None:
    write_packages(tmp_path, ["kora", "kora.mcp", "kora.db"])
    stdio_directory = tmp_path / "kora" / "mcp" / "stdio"
    stdio_directory.mkdir(parents=True)
    write_module(tmp_path, "kora.mcp.stdio.tools", "")
    write_module(tmp_path, "kora.mcp.tools", "from kora.mcp import stdio\n")
    write_module(
        tmp_path,
        "kora.__init__",
        "def load() -> None:\n    import kora.db\nimport kora.db\n",
    )
    stub = tmp_path / "kora" / "runner" / "__init__.pyi"
    stub.parent.mkdir(parents=True)
    stub.write_text("from . import plans\nfrom .. import db\n", encoding="utf-8")
    write_module(tmp_path, "kora.runner.plans", "")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/__init__.py:3: kora may not import db (kora.db)",
        "kora/mcp/tools.py:1: mcp may not import mcp.stdio (kora.mcp.stdio)",
        "kora/runner/__init__.pyi:2: runner may not import db (kora.db)",
    ]
