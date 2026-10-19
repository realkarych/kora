import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Path = REPOSITORY_ROOT / "src"
ROOT_PACKAGE: str = "kora"
TABLES_MODULE: str = "kora.db.tables"
OWNING_MODULES: frozenset[str] = frozenset(
    {
        "kora.engine",
        "kora.db.migrations",
        "kora.db.maintenance",
        "kora.scheduler.retention",
    }
)
GUARDED_TABLES: frozenset[str] = frozenset(
    {"run", "node_instance", "attempt", "wait", "approval_request", "artifact_version"}
)
WRITE_FUNCTIONS: frozenset[str] = frozenset({"insert", "update", "delete"})
MAPPING_FUNCTIONS: frozenset[str] = frozenset({"map_imperatively", "mapper"})
RAW_WRITE: re.Pattern[str] = re.compile(
    r"\b(insert\s+into|update|delete\s+from)\s+(\"?\w+\"?\.)?\"?("
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


def resolve_relative(module: SourceModule, level: int, name: str) -> str:
    parts = module.name.split(".")
    package_parts = parts if module.path.stem == "__init__" else parts[:-1]
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    return ".".join([*base_parts, name] if name else base_parts)


def import_bindings(module: SourceModule, tree: ast.Module) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    bindings[head] = head
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if node.level > 0:
                source = resolve_relative(module, node.level, source)
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{source}.{alias.name}"
    return bindings


def dotted(node: ast.expr, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = dotted(node.value, bindings)
        return None if owner is None else f"{owner}.{node.attr}"
    return None


def guarded_table_name(node: ast.expr, bindings: dict[str, str]) -> bool:
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        key = node.slice
        return (
            node.value.attr == "tables"
            and isinstance(key, ast.Constant)
            and key.value in GUARDED_TABLES
        )
    name = dotted(node, bindings)
    if name is None or not name.startswith(f"{TABLES_MODULE}."):
        return False
    return name.removeprefix(f"{TABLES_MODULE}.") in GUARDED_TABLES


def is_write_function(node: ast.Call, bindings: dict[str, str]) -> bool:
    name = dotted(node.func, bindings)
    return name is not None and name.split(".")[-1] in WRITE_FUNCTIONS


def core_write(node: ast.Call, bindings: dict[str, str]) -> bool:
    if not is_write_function(node, bindings):
        return False
    if node.args and guarded_table_name(node.args[0], bindings):
        return True
    function = node.func
    return isinstance(function, ast.Attribute) and guarded_table_name(
        function.value, bindings
    )


def raw_sql_reason(node: ast.Call, bindings: dict[str, str]) -> str | None:
    name = dotted(node.func, bindings)
    leaf = None if name is None else name.split(".")[-1]
    if leaf == "exec_driver_sql":
        return "executes driver SQL outside kora.engine"
    if leaf != "text" or not node.args:
        return None
    first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return "builds SQL text dynamically outside kora.engine"
    if RAW_WRITE.search(first.value) is not None:
        return "writes an execution table in raw SQL"
    return None


def mapping_reason(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.ClassDef):
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
            value = statement.value
            names_guarded = (
                isinstance(value, ast.Constant) and value.value in GUARDED_TABLES
            ) or guarded_table_name(value, bindings)
            if names_guarded and {"__tablename__", "__table__"} & set(targets):
                return f"maps {node.name} to an execution table through the ORM"
    if isinstance(node, ast.Call):
        name = dotted(node.func, bindings)
        if name is not None and name.split(".")[-1] in MAPPING_FUNCTIONS:
            if any(guarded_table_name(argument, bindings) for argument in node.args):
                return "maps an execution table through the ORM"
    return None


def star_import_reason(node: ast.AST, module: SourceModule) -> str | None:
    if not isinstance(node, ast.ImportFrom):
        return None
    source = node.module or ""
    if node.level > 0:
        source = resolve_relative(module, node.level, source)
    if source == TABLES_MODULE and any(alias.name == "*" for alias in node.names):
        return "star-imports the execution tables"
    return None


def module_violations(module: SourceModule, owner: bool) -> list[Violation]:
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    bindings = import_bindings(module, tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        reason = mapping_reason(node, bindings)
        if reason is None and not owner:
            reason = star_import_reason(node, module)
            if reason is None and isinstance(node, ast.Call):
                if core_write(node, bindings):
                    reason = "writes an execution table outside kora.engine"
                else:
                    reason = raw_sql_reason(node, bindings)
        if reason is not None:
            line = getattr(node, "lineno", 1)
            violations.append(Violation(module.path, line, reason))
    return violations


def find_violations(source_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for module in source_modules(source_root):
        violations.extend(module_violations(module, is_owner(module.name)))
    return sorted(violations, key=lambda v: (str(v.path), v.line))


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
        "kora.api.forms",
        "from sqlalchemy.dialects.postgresql import insert as pg_insert\n"
        "from ..db.tables import run\n"
        "import kora.db as d\n"
        "from kora import db\n"
        "pg_insert(run)\n"
        "d.tables.attempt.delete()\n"
        "db.tables.wait.update()\n"
        "update(metadata.tables['node_instance'])\n",
    )
    write_module(
        tmp_path,
        "kora.plugins.approval.cli",
        "from kora.db import tables as t\nt.approval_request.update()\n",
    )
    write_module(
        tmp_path,
        "kora.scheduler.passes",
        "from sqlalchemy import text\n"
        'text("UPDATE wait SET status = 1")\n'
        'text("delete from public.run")\n'
        'text(f"UPDATE {name} SET x = 1")\n'
        'connection.exec_driver_sql("SELECT 1")\n',
    )
    write_module(tmp_path, "kora.worker.cache", "from kora.db.tables import *\n")

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/api/forms.py:5: writes an execution table outside kora.engine",
        "kora/api/forms.py:6: writes an execution table outside kora.engine",
        "kora/api/forms.py:7: writes an execution table outside kora.engine",
        "kora/api/forms.py:8: writes an execution table outside kora.engine",
        "kora/api/routes.py:3: writes an execution table outside kora.engine",
        "kora/api/routes.py:4: writes an execution table outside kora.engine",
        "kora/plugins/approval/cli.py:2: writes an execution table outside kora.engine",
        "kora/scheduler/passes.py:2: writes an execution table in raw SQL",
        "kora/scheduler/passes.py:3: writes an execution table in raw SQL",
        "kora/scheduler/passes.py:4: builds SQL text dynamically outside kora.engine",
        "kora/scheduler/passes.py:5: executes driver SQL outside kora.engine",
        "kora/worker/cache.py:1: star-imports the execution tables",
    ]


def test_checker_flags_orm_mapping_of_execution_tables(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.db.orm",
        "from kora.db.tables import attempt\n"
        "class Attempt(Base):\n"
        "    __table__ = attempt\n"
        "class Run(Base):\n"
        '    __tablename__ = "run"\n'
        "class Token(Base):\n"
        '    __tablename__ = "api_token"\n'
        "registry.map_imperatively(Wait, attempt)\n",
    )

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == [
        "kora/db/orm.py:2: maps Attempt to an execution table through the ORM",
        "kora/db/orm.py:4: maps Run to an execution table through the ORM",
        "kora/db/orm.py:8: maps an execution table through the ORM",
    ]


def test_checker_allows_owning_modules_and_unguarded_tables(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.engine.attempts",
        "from sqlalchemy import update, text\nfrom kora.db.tables import attempt\n"
        'update(attempt)\ntext("UPDATE attempt SET x = 1")\n',
    )
    write_module(
        tmp_path,
        "kora.db.migrations.revision_0001",
        "from sqlalchemy import insert\nfrom kora.db.tables import run\ninsert(run)\n",
    )
    write_module(
        tmp_path,
        "kora.db.maintenance",
        "from kora.db.tables import artifact_version\nartifact_version.update()\n",
    )
    write_module(
        tmp_path,
        "kora.scheduler.retention",
        "from kora.db.tables import artifact_version\nartifact_version.update()\n",
    )
    write_module(
        tmp_path,
        "kora.api.tokens",
        "from sqlalchemy import insert, select, text\n"
        "from kora.db.tables import api_token, run\n"
        'insert(api_token)\nselect(run)\nitems.add(run)\ntext("SELECT * FROM run")\n',
    )

    violations = find_violations(tmp_path)

    assert describe_all(violations, tmp_path) == []
