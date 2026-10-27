import ast
import re
from collections.abc import Iterator
from pathlib import Path

from tests.support.ast_modules import (
    SourceModule,
    Violation,
    is_within,
    resolve_relative,
    source_modules,
    write_module,
)

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
    {
        "exec_driver_sql",
        "driver_connection",
        "get_raw_connection",
        "raw_connection",
        "dbapi_connection",
    }
)
METADATA_ATTRIBUTES: frozenset[str] = frozenset({"tables", "sorted_tables"})
CONFINING_PACKAGES: tuple[str, ...] = (DB_PACKAGE, "kora.engine")
DATABASE_DRIVERS: frozenset[str] = frozenset(
    {"sqlite3", "asyncpg", "aiosqlite", "psycopg", "psycopg2"}
)
PRIVATE_SQLITE_OWNER: str = "kora.plugins.storage.state"
TABLE_HANDLES: frozenset[str] = GUARDED_TABLES | {"tables", "metadata"}
SQL_COMMENT: re.Pattern[str] = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
RAW_WRITE: re.Pattern[str] = re.compile(
    r"\b(insert(\s+or\s+\w+)?\s+into|replace\s+into|merge\s+into|update(\s+only)?"
    r"|delete\s+from(\s+only)?|truncate(\s+table)?(\s+only)?)\s+(\"?\w+\"?\.)?\"?("
    + "|".join(sorted(GUARDED_TABLES))
    + r")\b",
    re.IGNORECASE,
)


def is_owner(module: str) -> bool:
    return any(is_within(module, owner) for owner in OWNING_MODULES)


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


def under_confining_package(name: str) -> bool:
    return any(is_within(name, package) for package in CONFINING_PACKAGES)


def names_guarded_table(node: ast.expr, bindings: dict[str, str]) -> bool:
    if isinstance(node, ast.Subscript):
        receiver = node.value
        return (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "tables"
            and constant_string(node.slice) in GUARDED_TABLES
        )
    if isinstance(node, ast.Call):
        name = dotted(node.func, bindings)
        leaf = None if name is None else name.split(".")[-1]
        if leaf in TABLE_CONSTRUCTORS:
            return (
                constant_string(node.args[0] if node.args else None) in GUARDED_TABLES
            )
        function = node.func
        if leaf == "get" and isinstance(function, ast.Attribute):
            receiver = function.value
            return (
                isinstance(receiver, ast.Attribute)
                and receiver.attr == "tables"
                and constant_string(node.args[0] if node.args else None)
                in GUARDED_TABLES
            )
        if leaf == "getattr" and len(node.args) > 1:
            owner = dotted(node.args[0], bindings)
            return owner is not None and under_confining_package(owner)
        return False
    name = dotted(node, bindings)
    if name is None or not under_confining_package(name):
        return False
    return name.split(".")[-1] in GUARDED_TABLES


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
    sqlalchemy_text = is_sqlalchemy_call(node, bindings, frozenset({"text"}))
    driver_statement = isinstance(node.func, ast.Attribute) and node.func.attr in {
        "execute",
        "executemany",
        "executescript",
    }
    if not sqlalchemy_text and not driver_statement:
        return None
    arguments = call_arguments(node, "text" if sqlalchemy_text else "sql")
    if not arguments:
        return None
    statement = constant_string(arguments[0])
    if statement is None:
        return (
            "builds SQL text dynamically outside kora.engine"
            if sqlalchemy_text
            else None
        )
    normalized = SQL_COMMENT.sub(" ", statement)
    for quote in "`[]":
        normalized = normalized.replace(quote, "")
    if RAW_WRITE.search(normalized) is not None:
        return "writes an execution table in raw SQL"
    return None


def guarded_driver(source: str, module: SourceModule) -> bool:
    driver = source.split(".")[0]
    return driver in DATABASE_DRIVERS and not (
        module.name == PRIVATE_SQLITE_OWNER and driver == "sqlite3"
    )


def guarded_import_reasons(node: ast.AST, module: SourceModule) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if is_within(alias.name, TABLES_MODULE):
                yield "imports kora.db.tables"
            elif guarded_driver(alias.name, module):
                yield f"imports the database driver {alias.name}"
        return
    if not isinstance(node, ast.ImportFrom):
        return
    source = import_source(module, node)
    if guarded_driver(source, module):
        yield f"imports the database driver {source}"
    elif is_within(source, TABLES_MODULE):
        yield "imports from kora.db.tables"
    elif under_confining_package(source):
        for alias in node.names:
            if alias.name == "*":
                yield f"star-imports {source}"
            elif alias.name in TABLE_HANDLES:
                yield f"imports {alias.name} from {source}"


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
    if isinstance(node, ast.Attribute) and node.attr in METADATA_ATTRIBUTES:
        return "accesses table metadata"
    if isinstance(node, ast.Attribute) and names_guarded_table(node, bindings):
        return "references an execution table"
    if isinstance(node, ast.Subscript) and names_guarded_table(node, bindings):
        return "looks up an execution table"
    if isinstance(node, ast.Call) and names_guarded_table(node, bindings):
        return "builds or looks up an execution table"
    if isinstance(node, ast.Call) and dynamic_tables_import(node, bindings):
        return "imports kora.db.tables dynamically"
    return None


def dynamic_tables_import(node: ast.Call, bindings: dict[str, str]) -> bool:
    name = dotted(node.func, bindings)
    if name is None or name.split(".")[-1] not in {"import_module", "__import__"}:
        return False
    target = constant_string(node.args[0]) if node.args else None
    return target is not None and is_within(target, TABLES_MODULE)


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
    for module in source_modules(source_root, ROOT_PACKAGE, (".py",)):
        violations.extend(module_violations(module))
    described = {violation.describe(source_root) for violation in violations}
    return sorted(described)


def test_transitions_only_through_shared_functions() -> None:
    source_root = SOURCE_ROOT

    violations = find_violations(source_root)

    assert violations == []


def test_private_control_sqlite_does_not_grant_metadata_authority(
    tmp_path: Path,
) -> None:
    write_module(
        tmp_path,
        "kora.plugins.storage.state",
        "import sqlite3\n"
        "from sqlite3 import Connection\n"
        "import asyncpg\n"
        "from kora.db.tables import run\n"
        'connection.execute("UPDATE run SET status=1")\n'
        'connection.executemany("DELETE FROM wait WHERE id=?", ())\n'
        'connection.executescript("INSERT INTO attempt VALUES (1)")\n',
    )
    write_module(tmp_path, "kora.plugins.storage.state_helper", "import sqlite3\n")
    write_module(tmp_path, "kora.plugins.storage.staging", "import sqlite3\n")

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/plugins/storage/staging.py:1: imports the database driver sqlite3",
        "kora/plugins/storage/state.py:3: imports the database driver asyncpg",
        "kora/plugins/storage/state.py:4: imports from kora.db.tables",
        "kora/plugins/storage/state.py:5: writes an execution table in raw SQL",
        "kora/plugins/storage/state.py:6: writes an execution table in raw SQL",
        "kora/plugins/storage/state.py:7: writes an execution table in raw SQL",
        "kora/plugins/storage/state_helper.py:1: imports the database driver sqlite3",
    ]


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
        "importlib.import_module('kora.db.tables')\n",
    )
    write_module(
        tmp_path,
        "kora.worker.helpers",
        "from kora.db.tables import metadata\n"
        "import kora.db.tables\n"
        "from kora.db import tables\n"
        "import sqlite3\n",
    )
    write_module(
        tmp_path,
        "kora.api.reexports",
        "from kora.db.reads import run\n"
        "import kora.db.reads as reads\n"
        "from kora.engine import attempt\n"
        "reads.wait.update()\n",
    )

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/api/reexports.py:1: imports run from kora.db.reads",
        "kora/api/reexports.py:3: imports attempt from kora.engine",
        "kora/api/reexports.py:4: references an execution table",
        "kora/api/reexports.py:4: writes an execution table outside kora.engine",
        "kora/api/routes.py:1: imports from kora.db.tables",
        "kora/api/routes.py:2: imports attempt from kora.db",
        "kora/api/routes.py:3: imports from kora.db.tables",
        "kora/api/routes.py:5: accesses table metadata",
        "kora/api/routes.py:5: references an execution table",
        "kora/scheduler/passes.py:2: builds or looks up an execution table",
        "kora/scheduler/passes.py:3: builds or looks up an execution table",
        "kora/scheduler/passes.py:4: accesses table metadata",
        "kora/scheduler/passes.py:4: looks up an execution table",
        "kora/scheduler/passes.py:5: accesses table metadata",
        "kora/scheduler/passes.py:5: builds or looks up an execution table",
        "kora/scheduler/passes.py:6: imports kora.db.tables dynamically",
        "kora/worker/helpers.py:1: imports from kora.db.tables",
        "kora/worker/helpers.py:2: imports kora.db.tables",
        "kora/worker/helpers.py:3: imports tables from kora.db",
        "kora/worker/helpers.py:4: imports the database driver sqlite3",
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
        "engine.raw_connection().cursor()\n"
        "c.connection.dbapi_connection\n"
        'text("UPDATE/**/`run` SET x = 1")\n'
        'text("UPDATE [wait] SET x = 1")\n'
        'text("SELECT * FROM run")\n'
        "widget.text(value)\n",
    )

    violations = find_violations(tmp_path)

    assert violations == [
        "kora/db/reports.py:10: writes an execution table in raw SQL",
        "kora/db/reports.py:11: writes an execution table in raw SQL",
        "kora/db/reports.py:2: writes an execution table in raw SQL",
        "kora/db/reports.py:3: writes an execution table in raw SQL",
        "kora/db/reports.py:4: writes an execution table in raw SQL",
        "kora/db/reports.py:5: builds SQL text dynamically outside kora.engine",
        "kora/db/reports.py:6: executes driver-level SQL outside kora.engine",
        "kora/db/reports.py:7: executes driver-level SQL outside kora.engine",
        "kora/db/reports.py:8: executes driver-level SQL outside kora.engine",
        "kora/db/reports.py:9: executes driver-level SQL outside kora.engine",
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
        "from kora.db.reads import load_run\n"
        "body.get('run')\n"
        "body['attempt']\n"
        "getattr(payload, 'wait')\n",
    )

    violations = find_violations(tmp_path)

    assert violations == []
