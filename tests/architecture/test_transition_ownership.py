import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Path = REPOSITORY_ROOT / "src"
ROOT_PACKAGE: str = "kora"
TABLES_MODULE: str = "kora.db.tables"
OWNING_MODULES: frozenset[str] = frozenset(
    {"kora.engine", "kora.db.migrations", "kora.scheduler.retention"}
)
GUARDED_TABLES: frozenset[str] = frozenset(
    {"run", "node_instance", "attempt", "wait", "approval_request", "artifact_version"}
)
GUARDED_CLASSES: frozenset[str] = frozenset(
    {"Run", "NodeInstance", "Attempt", "Wait", "ApprovalRequest", "ArtifactVersion"}
)
WRITE_FUNCTIONS: frozenset[str] = frozenset({"insert", "update", "delete"})
SESSION_WRITES: frozenset[str] = frozenset({"add", "add_all", "merge", "delete"})
RAW_WRITE: re.Pattern[str] = re.compile(
    r"\b(insert\s+into|update|delete\s+from)\s+\"?("
    + "|".join(sorted(GUARDED_TABLES))
    + r")\b",
    re.IGNORECASE,
)


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


def source_modules(source_root: Path) -> list[SourceModule]:
    modules: list[SourceModule] = []
    for path in sorted((source_root / ROOT_PACKAGE).rglob("*.py")):
        parts = list(path.relative_to(source_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules.append(SourceModule(path, ".".join(parts)))
    return modules


def is_owner(module: str) -> bool:
    return any(is_within(module, owner) for owner in OWNING_MODULES)


def table_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    tables: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == TABLES_MODULE:
            for alias in node.names:
                if alias.name in GUARDED_TABLES:
                    tables.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "kora.db":
            for alias in node.names:
                if alias.name == "tables":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == TABLES_MODULE and alias.asname:
                    modules.add(alias.asname)
    return frozenset(tables), frozenset(modules)


def names_guarded_table(
    node: ast.expr, tables: frozenset[str], modules: frozenset[str]
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in tables
    if isinstance(node, ast.Attribute) and node.attr in GUARDED_TABLES:
        value = node.value
        if isinstance(value, ast.Name):
            return value.id in modules
        return ast.unparse(value) == TABLES_MODULE
    return False


def callee_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def core_write(node: ast.Call, tables: frozenset[str], modules: frozenset[str]) -> bool:
    name = callee_name(node)
    if name not in WRITE_FUNCTIONS:
        return False
    if node.args and names_guarded_table(node.args[0], tables, modules):
        return True
    function = node.func
    return isinstance(function, ast.Attribute) and names_guarded_table(
        function.value, tables, modules
    )


def raw_write(node: ast.Call) -> bool:
    if callee_name(node) != "text" or not node.args:
        return False
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return False
    return RAW_WRITE.search(first.value) is not None


def session_write(node: ast.Call) -> bool:
    if callee_name(node) not in SESSION_WRITES or not node.args:
        return False
    candidates = (
        node.args[0].elts
        if isinstance(node.args[0], ast.List | ast.Tuple)
        else [node.args[0]]
    )
    for candidate in candidates:
        if (
            isinstance(candidate, ast.Call)
            and callee_name(candidate) in GUARDED_CLASSES
        ):
            return True
        if isinstance(candidate, ast.Name) and candidate.id in GUARDED_CLASSES:
            return True
    return False


def module_violations(module: SourceModule) -> list[Violation]:
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    tables, modules = table_bindings(tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if core_write(node, tables, modules):
            reason = "writes an execution table outside kora.engine"
            violations.append(Violation(module.path, node.lineno, reason))
        elif raw_write(node):
            reason = "writes an execution table in raw SQL"
            violations.append(Violation(module.path, node.lineno, reason))
        elif session_write(node):
            reason = "writes an execution table through an ORM session"
            violations.append(Violation(module.path, node.lineno, reason))
    return violations


def find_violations(source_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for module in source_modules(source_root):
        if not is_owner(module.name):
            violations.extend(module_violations(module))
    return violations


def write_module(source_root: Path, module: str, source: str) -> None:
    path = source_root.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def describe_all(violations: list[Violation], source_root: Path) -> list[str]:
    return [violation.describe(source_root) for violation in violations]


def test_transitions_only_through_shared_functions() -> None:
    source_root = SOURCE_ROOT

    violations = find_violations(source_root)

    assert describe_all(violations, source_root) == []


def test_checker_flags_execution_table_writes_outside_engine(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.api.routes",
        "from sqlalchemy import insert, update\n"
        "from kora.db.tables import run, attempt as attempts\n"
        "insert(run)\n"
        "update(attempts)\n",
    )
    write_module(
        tmp_path,
        "kora.plugins.approval.cli",
        "from kora.db import tables as t\nt.approval_request.update()\n",
    )
    write_module(
        tmp_path,
        "kora.scheduler.passes",
        'from sqlalchemy import text\ntext("UPDATE wait SET status = 1")\n',
    )
    write_module(
        tmp_path,
        "kora.worker.cache",
        "session.add(Attempt(id=1))\nsession.add_all([Run(id=1)])\n",
    )

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/api/routes.py:3: writes an execution table outside kora.engine",
        "kora/api/routes.py:4: writes an execution table outside kora.engine",
        "kora/plugins/approval/cli.py:2: writes an execution table outside kora.engine",
        "kora/scheduler/passes.py:2: writes an execution table in raw SQL",
        "kora/worker/cache.py:1: writes an execution table through an ORM session",
        "kora/worker/cache.py:2: writes an execution table through an ORM session",
    ]


def test_checker_allows_owning_modules_and_unguarded_tables(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.engine.attempts",
        "from sqlalchemy import update\nfrom kora.db.tables import attempt\n"
        "update(attempt)\n",
    )
    write_module(
        tmp_path,
        "kora.db.migrations.revision_0001",
        "from sqlalchemy import insert\nfrom kora.db.tables import run\ninsert(run)\n",
    )
    write_module(
        tmp_path,
        "kora.scheduler.retention",
        "from kora.db.tables import artifact_version\nartifact_version.update()\n",
    )
    write_module(
        tmp_path,
        "kora.api.tokens",
        "from sqlalchemy import insert, select\n"
        "from kora.db.tables import api_token, run\n"
        "insert(api_token)\nselect(run)\nitems.add(run)\n",
    )

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == []
