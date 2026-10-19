import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
SOURCE_ROOT: Path = REPOSITORY_ROOT / "src"
ROOT_PACKAGE: str = "kora"
DB_PACKAGE: str = "kora.db"
TABLES_MODULE: str = "kora.db.tables"
SQLALCHEMY: str = "sqlalchemy"
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
WRITE_FUNCTIONS: frozenset[str] = frozenset(
    {"insert", "update", "delete", "Insert", "Update", "Delete"}
)
TABLE_CONSTRUCTORS: frozenset[str] = frozenset({"table", "Table"})
MAPPING_FUNCTIONS: frozenset[str] = frozenset({"map_imperatively", "mapper"})
MAPPING_ATTRIBUTES: frozenset[str] = frozenset({"__tablename__", "__table__"})
RAW_CONNECTION_ATTRIBUTES: frozenset[str] = frozenset(
    {"exec_driver_sql", "driver_connection", "get_raw_connection"}
)
RAW_WRITE: re.Pattern[str] = re.compile(
    r"\b(insert(\s+or\s+\w+)?\s+into|replace\s+into|merge\s+into|update(\s+only)?"
    r"|delete\s+from(\s+only)?|truncate(\s+table)?(\s+only)?)\s+(\"?\w+\"?\.)?\"?("
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


def import_source(module: SourceModule, node: ast.ImportFrom) -> str:
    source = node.module or ""
    if node.level > 0:
        source = resolve_relative(module, node.level, source)
    return source


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
            source = import_source(module, node)
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


def constant_string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def call_arguments(node: ast.Call, keyword: str) -> list[ast.expr]:
    return [*node.args[:1], *[k.value for k in node.keywords if k.arg == keyword]]


def is_sqlalchemy_call(
    node: ast.Call, bindings: dict[str, str], names: frozenset[str]
) -> bool:
    name = dotted(node.func, bindings)
    if name is None:
        return False
    return is_within(name, SQLALCHEMY) and name.split(".")[-1] in names


def names_guarded_table(node: ast.expr, bindings: dict[str, str]) -> bool:
    if isinstance(node, ast.Subscript):
        return constant_string(node.slice) in GUARDED_TABLES
    if isinstance(node, ast.Call):
        name = dotted(node.func, bindings)
        leaf = None if name is None else name.split(".")[-1]
        if leaf in TABLE_CONSTRUCTORS or leaf in {"get", "getattr"}:
            arguments = node.args[1:2] if leaf == "getattr" else node.args[:1]
            return any(constant_string(a) in GUARDED_TABLES for a in arguments)
        return False
    name = dotted(node, bindings)
    if name is None:
        return False
    for prefix in (TABLES_MODULE, DB_PACKAGE):
        if (
            name.startswith(f"{prefix}.")
            and name.removeprefix(f"{prefix}.") in GUARDED_TABLES
        ):
            return True
    return False


def write_targets(node: ast.Call, bindings: dict[str, str]) -> list[ast.expr]:
    function = node.func
    if is_sqlalchemy_call(node, bindings, WRITE_FUNCTIONS):
        return call_arguments(node, "table")
    if isinstance(function, ast.Attribute) and function.attr in {
        "insert",
        "update",
        "delete",
    }:
        return [function.value]
    return []


def raw_sql_reason(node: ast.Call, bindings: dict[str, str]) -> str | None:
    if not is_sqlalchemy_call(node, bindings, frozenset({"text"})):
        return None
    arguments = call_arguments(node, "text")
    if not arguments:
        return None
    statement = constant_string(arguments[0])
    if statement is None:
        return "builds SQL text dynamically outside kora.engine"
    if RAW_WRITE.search(statement) is not None:
        return "writes an execution table in raw SQL"
    return None


def guarded_import_reasons(node: ast.AST, module: SourceModule) -> Iterator[str]:
    if not isinstance(node, ast.ImportFrom):
        return
    source = import_source(module, node)
    if not (source == TABLES_MODULE or source == DB_PACKAGE):
        return
    for alias in node.names:
        if alias.name == "*":
            yield "star-imports the execution tables"
        elif alias.name in GUARDED_TABLES:
            yield f"imports the execution table {alias.name}"


def mapping_reason(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.ClassDef):
        for statement in node.body:
            if names_mapped_table(statement, bindings):
                return f"maps {node.name} to an execution table through the ORM"
    if isinstance(node, ast.Call):
        name = dotted(node.func, bindings)
        if name is not None and name.split(".")[-1] in MAPPING_FUNCTIONS:
            arguments = [*node.args, *[k.value for k in node.keywords]]
            if any(names_guarded_value(a, bindings) for a in arguments):
                return "maps an execution table through the ORM"
    return None


def names_guarded_value(node: ast.expr, bindings: dict[str, str]) -> bool:
    return constant_string(node) in GUARDED_TABLES or names_guarded_table(
        node, bindings
    )


def names_mapped_table(statement: ast.stmt, bindings: dict[str, str]) -> bool:
    if isinstance(statement, ast.Assign):
        targets = {t.id for t in statement.targets if isinstance(t, ast.Name)}
        value: ast.expr | None = statement.value
    elif isinstance(statement, ast.AnnAssign) and isinstance(
        statement.target, ast.Name
    ):
        targets = {statement.target.id}
        value = statement.value
    elif (
        isinstance(statement, ast.FunctionDef) and statement.name in MAPPING_ATTRIBUTES
    ):
        returns = [s.value for s in ast.walk(statement) if isinstance(s, ast.Return)]
        return any(r is not None and names_guarded_value(r, bindings) for r in returns)
    else:
        return False
    return (
        bool(targets & MAPPING_ATTRIBUTES)
        and value is not None
        and names_guarded_value(value, bindings)
    )


def outside_db_reason(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Attribute) and names_guarded_table(node, bindings):
        return "references an execution table"
    if isinstance(node, ast.Call) and names_guarded_table(node, bindings):
        return "builds or looks up an execution table"
    return None


def call_reason(node: ast.Call, bindings: dict[str, str]) -> str | None:
    if any(
        names_guarded_table(target, bindings)
        for target in write_targets(node, bindings)
    ):
        return "writes an execution table outside kora.engine"
    return raw_sql_reason(node, bindings)


def module_violations(module: SourceModule) -> list[Violation]:
    tree = ast.parse(module.path.read_text(encoding="utf-8"), filename=str(module.path))
    bindings = import_bindings(module, tree)
    owner = is_owner(module.name)
    inside_db = is_within(module.name, DB_PACKAGE)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        reasons: list[str] = []
        mapping = mapping_reason(node, bindings)
        if mapping is not None:
            reasons.append(mapping)
        if not owner:
            if not inside_db:
                reasons.extend(guarded_import_reasons(node, module))
                outside = outside_db_reason(node, bindings)
                if outside is not None:
                    reasons.append(outside)
            if isinstance(node, ast.Call):
                call = call_reason(node, bindings)
                if call is not None:
                    reasons.append(call)
            if (
                isinstance(node, ast.Attribute)
                and node.attr in RAW_CONNECTION_ATTRIBUTES
            ):
                reasons.append("executes driver-level SQL outside kora.engine")
        line = getattr(node, "lineno", 1)
        violations.extend(Violation(module.path, line, reason) for reason in reasons)
    return violations


def find_violations(source_root: Path) -> list[str]:
    violations: list[Violation] = []
    for module in source_modules(source_root):
        violations.extend(module_violations(module))
    described = {violation.describe(source_root) for violation in violations}
    return sorted(described)


def write_module(source_root: Path, module: str, source: str) -> None:
    path = source_root.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_transitions_only_through_shared_functions() -> None:
    source_root = SOURCE_ROOT

    violations = find_violations(source_root)

    assert violations == []


def test_checker_refuses_execution_tables_outside_db_and_engine(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.api.routes",
        "from kora.db.tables import run\n"
        "from ..db import attempt\n"
        "from kora.db.tables import *\n"
        "import kora.db as d\n"
        "d.tables.wait\n",
    )
    write_module(
        tmp_path,
        "kora.scheduler.passes",
        "import sqlalchemy as sa\n"
        "sa.table('run', sa.column('status'))\n"
        "sa.Table('attempt', sa.MetaData())\n"
        "metadata.tables['node_instance']\n"
        "metadata.tables.get('wait')\n"
        "getattr(tables, 'approval_request')\n",
    )

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/api/routes.py:1: imports the execution table run",
        "kora/api/routes.py:2: imports the execution table attempt",
        "kora/api/routes.py:3: star-imports the execution tables",
        "kora/api/routes.py:5: references an execution table",
        "kora/scheduler/passes.py:2: builds or looks up an execution table",
        "kora/scheduler/passes.py:3: builds or looks up an execution table",
        "kora/scheduler/passes.py:5: builds or looks up an execution table",
        "kora/scheduler/passes.py:6: builds or looks up an execution table",
    ]


def test_checker_flags_writes_inside_db(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.db.repository",
        "from sqlalchemy import insert, Update\n"
        "from sqlalchemy.dialects.postgresql import insert as pg_insert\n"
        "from kora.db.tables import run, attempt\n"
        "insert(run)\n"
        "pg_insert(attempt)\n"
        "Update(run)\n"
        "insert(table=attempt)\n"
        "run.delete()\n"
        "seen.update(run)\n",
    )

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/db/repository.py:4: writes an execution table outside kora.engine",
        "kora/db/repository.py:5: writes an execution table outside kora.engine",
        "kora/db/repository.py:6: writes an execution table outside kora.engine",
        "kora/db/repository.py:7: writes an execution table outside kora.engine",
        "kora/db/repository.py:8: writes an execution table outside kora.engine",
    ]


def test_checker_flags_raw_and_driver_sql(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.db.reports",
        "from sqlalchemy import text\n"
        'text("UPDATE ONLY wait SET status = 1")\n'
        'text("INSERT OR REPLACE INTO run VALUES (1)")\n'
        'text(text="TRUNCATE TABLE attempt")\n'
        'text(f"UPDATE {name} SET x = 1")\n'
        "connection.exec_driver_sql(statement)\n"
        "raw.driver_connection.execute(statement)\n"
        'text("SELECT * FROM run")\n'
        "widget.text(value)\n",
    )

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/db/reports.py:2: writes an execution table in raw SQL",
        "kora/db/reports.py:3: writes an execution table in raw SQL",
        "kora/db/reports.py:4: writes an execution table in raw SQL",
        "kora/db/reports.py:5: builds SQL text dynamically outside kora.engine",
        "kora/db/reports.py:6: executes driver-level SQL outside kora.engine",
        "kora/db/reports.py:7: executes driver-level SQL outside kora.engine",
    ]


def test_checker_flags_orm_mapping_of_execution_tables(tmp_path: Path) -> None:
    write_module(
        tmp_path,
        "kora.db.orm",
        "from sqlalchemy import Table\n"
        "from kora.db.tables import attempt\n"
        "class Attempt(Base):\n"
        "    __table__ = attempt\n"
        "class Run(Base):\n"
        '    __tablename__: str = "run"\n'
        "class Wait(Base):\n"
        "    __table__ = Table('wait', Base.metadata)\n"
        "class Node(Base):\n"
        "    @declared_attr\n"
        "    def __tablename__(cls) -> str:\n"
        "        return 'node_instance'\n"
        "class Token(Base):\n"
        '    __tablename__ = "api_token"\n'
        "registry.map_imperatively(Item, local_table=attempt)\n",
    )

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/db/orm.py:15: maps an execution table through the ORM",
        "kora/db/orm.py:3: maps Attempt to an execution table through the ORM",
        "kora/db/orm.py:5: maps Run to an execution table through the ORM",
        "kora/db/orm.py:7: maps Wait to an execution table through the ORM",
        "kora/db/orm.py:9: maps Node to an execution table through the ORM",
    ]


def test_checker_allows_owning_modules_and_reads_in_db(tmp_path: Path) -> None:
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
        "kora.db.tables",
        "from sqlalchemy import Table, MetaData\nrun = Table('run', MetaData())\n",
    )
    write_module(
        tmp_path,
        "kora.db.reads",
        "from sqlalchemy import select, insert\n"
        "from kora.db.tables import run, api_token\n"
        "select(run)\ninsert(api_token)\n",
    )
    write_module(
        tmp_path,
        "kora.api.tokens",
        "from kora.db.reads import load_run\nfrom kora.db.tables import api_token\n",
    )

    violations = find_violations(tmp_path)

    assert violations == []
