import ast
import hashlib
import io
import json
import re
import tokenize
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeGuard, cast

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[2]
PYPROJECT: str = "pyproject.toml"
GENERATED_FILES: tuple[str, ...] = ("uv.lock", "web/pnpm-lock.yaml")
SKIPPED_PATHS: frozenset[str] = frozenset(
    {
        "measurements",
        "dist",
        "build",
        "web/dist",
        "web/build",
        "web/node_modules",
        "src/kora/api/static",
    }
)
SKIPPED_DIRECTORY_NAMES: frozenset[str] = frozenset({"__pycache__", "node_modules"})
SHADOW_NAMES: frozenset[str] = frozenset(
    {
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyrightconfig.json",
        "ruff.toml",
        ".ruff.toml",
        PYPROJECT,
    }
)
ESLINT_CONFIG: str = "web/eslint.config.mjs"
ESLINT_CONFIG_PREFIXES: tuple[str, ...] = (".eslintrc", "eslint.config.")
WEB_PACKAGE: str = "web/package.json"
WEB_SCRIPTS: dict[str, str] = {
    "dev": "vite",
    "typecheck": "tsc -b --pretty false",
    "lint": "eslint . --max-warnings 0",
    "build": "pnpm typecheck && vite build",
    "test:browser": "node tests/browser/run.ts",
}
STRICT_TSCONFIGS: tuple[str, ...] = (
    "web/tsconfig.app.json",
    "web/tsconfig.node.json",
    "web/tsconfig.browser.json",
)
SHARED_COMPILER_OPTIONS: dict[str, object] = {
    "module": "ESNext",
    "moduleResolution": "bundler",
    "verbatimModuleSyntax": True,
    "moduleDetection": "force",
    "noEmit": True,
    "strict": True,
    "noUnusedLocals": True,
    "noUnusedParameters": True,
    "erasableSyntaxOnly": True,
    "noFallthroughCasesInSwitch": True,
    "noUncheckedIndexedAccess": True,
    "noUncheckedSideEffectImports": True,
}
TSCONFIG_TABLES: dict[str, dict[str, object]] = {
    "web/tsconfig.json": {
        "files": [],
        "references": [
            {"path": "./tsconfig.app.json"},
            {"path": "./tsconfig.node.json"},
            {"path": "./tsconfig.browser.json"},
        ],
    },
    "web/tsconfig.app.json": {
        "compilerOptions": {
            "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.app.tsbuildinfo",
            "target": "ES2022",
            "lib": ["ES2022", "DOM", "DOM.Iterable"],
            "types": ["vite/client"],
            "jsx": "react-jsx",
            **SHARED_COMPILER_OPTIONS,
        },
        "include": ["src"],
    },
    "web/tsconfig.node.json": {
        "compilerOptions": {
            "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.node.tsbuildinfo",
            "target": "ES2023",
            "lib": ["ES2023"],
            "types": ["node"],
            **SHARED_COMPILER_OPTIONS,
        },
        "include": ["vite.config.ts"],
    },
    "web/tsconfig.browser.json": {
        "compilerOptions": {
            "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.browser.tsbuildinfo",
            "target": "ES2023",
            "lib": ["ES2023", "DOM", "DOM.Iterable"],
            "types": ["node"],
            **SHARED_COMPILER_OPTIONS,
            "allowImportingTsExtensions": True,
        },
        "include": ["tests/browser"],
    },
}
WEB_CONFIGURATION: tuple[str, ...] = (*TSCONFIG_TABLES, WEB_PACKAGE, ESLINT_CONFIG)
SHEBANG: str = "#!"
ENV_PROGRAM: str = "env"
SHELL_INTERPRETERS: frozenset[str] = frozenset({"sh", "bash", "zsh", "ksh"})
PYTHON_INTERPRETER: str = "python"
VERSION_CHARACTERS: str = "0123456789."
DIRECTIVE: re.Pattern[str] = re.compile(
    r"noqa|type\s*:\s*ignore|pyright\s*:|pragma\s*:|fmt\s*:|ruff\s*:|isort\s*:"
    r"|mypy\s*:|pylint\s*:|flake8\s*:|coverage\s*:|nosec"
    r"|eslint-(disable|enable|env)|@ts-(ignore|expect-error|nocheck|check)"
    r"|prettier-ignore|biome-ignore",
    re.IGNORECASE,
)
QUOTED: re.Pattern[str] = re.compile(
    r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\\n])*`"
)
SHELL_COMMENT_PRECEDERS: str = " \t;()&|"
MOCK_FIXTURES: frozenset[str] = frozenset({"monkeypatch", "mocker"})
STRING_MARKER_CALLS: frozenset[str] = frozenset({"add_marker", "applymarker"})
SKIP_METHOD_CALLS: frozenset[str] = frozenset({"skipTest"})
PACKAGE_KEYWORD: str = "package"
RUNTIME_CONFIGURATION_CALLS: frozenset[str] = frozenset({"addinivalue_line"})
WARNING_FILTER_CALLS: frozenset[str] = frozenset({"catch_warnings"})
PLUGIN_MANAGER: str = "pluginmanager"
TESTS_DIRECTORY: str = "tests/"
PLUGIN_MANAGER_DIRECTORIES: tuple[str, ...] = (TESTS_DIRECTORY, "src/kora/conformance/")
GETATTR: str = "getattr"
MODULES_ATTRIBUTE: str = "modules"
BANNED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "skip",
        "xfail",
        "importorskip",
        "exit",
        "skipif",
        "filterwarnings",
        "simplefilter",
        "catch_warnings",
        "resetwarnings",
        "filters",
        "skipTest",
        "SkipTest",
        "MonkeyPatch",
        "suppress",
        "_exit",
        "adverse_outcomes",
        "fixtures_adverse",
        "skip_fixture",
        "xfail_fixture",
    }
)
PYTEST_ENTRY_POINTS: str = "pytest11"
DYNAMIC_IMPORT_CALLS: frozenset[str] = frozenset(
    {"__import__", "import_module", "resolve_name"}
)
GUARDED_DYNAMIC_MODULES: frozenset[str] = frozenset(
    {"pytest", "_pytest", "warnings", "unittest", "kora_tooling_fixture"}
)
BROAD_EXCEPTIONS: frozenset[str] = frozenset({"Exception", "BaseException"})
COLLECTION_OVERRIDES: frozenset[str] = frozenset(
    {"collect_ignore", "collect_ignore_glob", "pytest_plugins"}
)
SCOPE_NODES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.Try,
    ast.TryStar,
)
HOOK_PREFIX: str = "pytest_"
WARNINGS_MODULE: str = "warnings"

ALLOWED_BLIND_HANDLERS: dict[str, int] = {
    "src/kora/__main__.py": 1,
    "src/kora/observability/bootstrap.py": 2,
    "src/kora/cel_eval/thread.py": 1,
    "src/kora/observability/sink.py": 1,
}
WARNING_FILTERS_MODULE: str = "tests/support/warning_filters.py"
ALLOWED_WARNING_NAMES: dict[str, frozenset[str]] = {
    WARNING_FILTERS_MODULE: frozenset(
        {"catch_warnings", "filterwarnings", "simplefilter"}
    )
}
WARNING_FILTERS_DIGEST: str = (
    "b5f9ff40d88dc8de988005de79a4af635e844a3af8637b7066389ff91c4b8105"
)
ALLOWED_WARNING_DIGESTS: dict[str, str] = {
    WARNING_FILTERS_MODULE: WARNING_FILTERS_DIGEST
}
ALLOWED_WARNING_DEFINITIONS: dict[str, frozenset[str]] = {
    WARNING_FILTERS_MODULE: frozenset(
        {
            "EXPRESSION_INDEX_WARNING",
            "expression_index_warnings_ignored",
            "every_warning_raised",
        }
    )
}
ALWAYS_ACTION: str = "always"
SIMPLE_FILTER: str = "simplefilter"
MESSAGE_FILTER: str = "filterwarnings"
CONTEXT_FILTER: str = "catch_warnings"
MESSAGE_FILTER_ARGUMENTS: int = 3
EXPRESSION_INDEX_PATTERN_NAME: str = "EXPRESSION_INDEX_WARNING"
EXPRESSION_INDEX_PATTERN: str = ".*expression-based index.*"
MATCH_EVERYTHING: str = ".*"
ALLOWED_WARNING_CATEGORIES: frozenset[str] = frozenset({"SAWarning", "UserWarning"})
ALLOWED_WARNING_IMPORTS: dict[str, frozenset[str]] = {
    WARNING_FILTERS_MODULE: frozenset(
        {
            WARNINGS_MODULE,
            "collections.abc.Generator",
            "contextlib.contextmanager",
            "sqlalchemy.exc.SAWarning",
        }
    )
}
ALLOWED_HOOKS: dict[str, frozenset[str]] = {
    "tests/conftest.py": frozenset({"pytest_report_header", "pytest_generate_tests"}),
    "tests/observability/conftest.py": frozenset(),
}

ADVERSE_DIRECTORY: str = "tests/fixtures/provider_tooling/src/kora_tooling_fixture/"
ADVERSE_HELPER: str = ADVERSE_DIRECTORY + "adverse_outcomes.py"
ADVERSE_CONSUMER: str = ADVERSE_DIRECTORY + "fixtures_adverse.py"
ADVERSE_NAMES: frozenset[str] = frozenset(
    {"adverse_outcomes", "fixtures_adverse", "skip_fixture", "xfail_fixture"}
)
ADVERSE_SOURCE: str = (
    "from typing import NoReturn\n"
    "import pytest\n"
    "def skip_fixture() -> NoReturn:\n"
    '    pytest.skip("intentional installed fixture skip")\n'
    "def xfail_fixture() -> NoReturn:\n"
    '    pytest.xfail("intentional installed fixture xfail")\n'
)

BANNED_API: dict[str, dict[str, str]] = {
    "pytest.skip": {"msg": "AGENTS.md Testing: no skipped tests"},
    "pytest.xfail": {"msg": "AGENTS.md Testing: no expected failures"},
    "pytest.exit": {"msg": "AGENTS.md Testing: a test process reports its result"},
    "pytest.importorskip": {"msg": "AGENTS.md Testing: no skipped tests"},
    "pytest.mark.skip": {"msg": "AGENTS.md Testing: no skipped tests"},
    "pytest.mark.skipif": {"msg": "AGENTS.md Testing: no skipped tests"},
    "pytest.mark.xfail": {"msg": "AGENTS.md Testing: no expected failures"},
    "pytest.mark.filterwarnings": {"msg": "AGENTS.md Testing: warnings are errors"},
    "pytest.MonkeyPatch": {"msg": "AGENTS.md Testing: no mocks or patching"},
    "_pytest": {"msg": "AGENTS.md Testing: no pytest internals"},
    "unittest.SkipTest": {"msg": "AGENTS.md Testing: no skipped tests"},
    "unittest.case": {"msg": "AGENTS.md Testing: no skipped tests"},
    "unittest.skip": {"msg": "AGENTS.md Testing: no skipped tests"},
    "unittest.skipIf": {"msg": "AGENTS.md Testing: no skipped tests"},
    "unittest.skipUnless": {"msg": "AGENTS.md Testing: no skipped tests"},
    "unittest.expectedFailure": {"msg": "AGENTS.md Testing: no expected failures"},
    "unittest.mock": {"msg": "AGENTS.md Testing: no mocks"},
    "mock": {"msg": "AGENTS.md Testing: no mocks"},
    "pytest_mock": {"msg": "AGENTS.md Testing: no mocks"},
    "warnings.filterwarnings": {"msg": "AGENTS.md Testing: warnings are errors"},
    "warnings.simplefilter": {"msg": "AGENTS.md Testing: warnings are errors"},
    "warnings.catch_warnings": {"msg": "AGENTS.md Testing: warnings are errors"},
    "warnings.resetwarnings": {"msg": "AGENTS.md Testing: warnings are errors"},
    "warnings.filters": {"msg": "AGENTS.md Testing: warnings are errors"},
    "contextlib.suppress": {"msg": "AGENTS.md Code: fail fast, no blind suppression"},
    "os._exit": {"msg": "AGENTS.md Testing: a test process reports its result"},
}
RUFF_TABLE: dict[str, object] = {
    "target-version": "py311",
    "respect-gitignore": False,
    "extend-exclude": [".claude"],
    "include": [
        "*.py",
        "*.pyi",
        "*.ipynb",
        "**/pyproject.toml",
        "**/ruff.toml",
        "**/.ruff.toml",
    ],
    "line-length": 88,
    "lint": {
        "select": [
            "E",
            "F",
            "I",
            "UP",
            "B",
            "ANN",
            "S",
            "BLE",
            "PT",
            "PGH",
            "ERA",
            "T20",
            "TID",
            "RUF100",
        ],
        "per-file-ignores": {
            "hatch_build.py": ["S603"],
            "src/kora/executor/launcher.py": ["S603"],
            "src/kora/plugins/vcs/git/decoder.py": ["S603"],
            "src/kora/plugins/vcs/git/export.py": ["S603"],
            "src/kora/plugins/vcs/git/tree_build.py": ["S603"],
            "tests/**": ["S101", "S603"],
            "src/kora/__main__.py": ["BLE001"],
            "src/kora/observability/bootstrap.py": ["BLE001"],
            "src/kora/cel_eval/thread.py": ["BLE001"],
            "src/kora/observability/sink.py": ["BLE001"],
            "src/kora/runner/gate.py": ["S606"],
            "tests/support/warning_filters.py": ["TID251"],
            ADVERSE_HELPER: ["TID251"],
        },
        "flake8-tidy-imports": {"banned-api": BANNED_API},
    },
}
PYRIGHT_TABLE: dict[str, object] = {
    "include": ["src", "hatch_build.py", "tests"],
    "typeCheckingMode": "strict",
    "pythonVersion": "3.11",
    "venvPath": ".",
    "venv": ".venv",
    "enableTypeIgnoreComments": False,
}
PYTEST_TABLE: dict[str, object] = {
    "testpaths": ["tests"],
    "addopts": [
        "--strict-config",
        "--strict-markers",
        "--import-mode=importlib",
        "-p",
        "kora.conformance.plugin",
        "-m",
        "not measurement",
    ],
    "filterwarnings": ["error"],
    "empty_parameter_set_mark": "fail_at_collect",
    "markers": [
        "measurement: serial performance and resource-lifetime measurement",
        "kora_conformance: a protocol conformance case of kora.conformance",
    ],
    "asyncio_mode": "strict",
    "asyncio_default_fixture_loop_scope": "function",
}
STRICT_ADDOPTS: frozenset[str] = frozenset({"--strict-config", "--strict-markers"})
WARNINGS_AS_ERRORS: str = "error"


class Language(Enum):
    PYTHON = "python"
    SHELL = "shell"
    SQL = "sql"
    TEMPLATE = "template"
    SCRIPT = "script"


LANGUAGES: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".sh": Language.SHELL,
    ".bash": Language.SHELL,
    ".zsh": Language.SHELL,
    ".ksh": Language.SHELL,
    ".sql": Language.SQL,
    ".psql": Language.SQL,
    ".pgsql": Language.SQL,
    ".mako": Language.TEMPLATE,
    ".ts": Language.SCRIPT,
    ".tsx": Language.SCRIPT,
    ".mts": Language.SCRIPT,
    ".cts": Language.SCRIPT,
    ".js": Language.SCRIPT,
    ".jsx": Language.SCRIPT,
    ".mjs": Language.SCRIPT,
    ".cjs": Language.SCRIPT,
}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str
    language: Language


@dataclass(frozen=True)
class Violation:
    relative: str
    line: int
    reason: str

    def describe(self) -> str:
        return f"{self.relative}:{self.line}: {self.reason}"


def relative_to(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def is_skipped(root: Path, directory: Path) -> bool:
    name = directory.name
    if name.startswith(".") or name in SKIPPED_DIRECTORY_NAMES:
        return True
    return relative_to(root, directory) in SKIPPED_PATHS


def walk_files(root: Path) -> Iterator[Path]:
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if not is_skipped(root, entry):
                    pending.append(entry)
            elif entry.is_file():
                yield entry


def shebang_program(head: str) -> str:
    tokens = head[len(SHEBANG) :].split()
    if not tokens:
        return ""
    program = Path(tokens[0]).name
    if program != ENV_PROGRAM:
        return program
    for token in tokens[1:]:
        if not token.startswith("-"):
            return Path(token).name
    return ""


def shebang_language(path: Path) -> Language | None:
    with path.open("rb") as handle:
        head = handle.readline(256)
    if not head.startswith(SHEBANG.encode("ascii")):
        return None
    program = shebang_program(head.decode("utf-8", errors="replace"))
    if program.startswith(PYTHON_INTERPRETER):
        return Language.PYTHON
    if program.rstrip(VERSION_CHARACTERS) in SHELL_INTERPRETERS:
        return Language.SHELL
    return None


def classify(path: Path) -> Language | None:
    if path.suffix:
        return LANGUAGES.get(path.suffix)
    return shebang_language(path)


def source_files(root: Path) -> list[SourceFile]:
    generated = set(GENERATED_FILES)
    found: list[SourceFile] = []
    for path in walk_files(root):
        relative = relative_to(root, path)
        if relative in generated:
            continue
        language = classify(path)
        if language is not None:
            found.append(SourceFile(path, relative, language))
    return found


def is_shebang(line: int, column: int, text: str) -> bool:
    return line == 1 and column == 0 and text.startswith(SHEBANG)


def comment_reason(text: str) -> str:
    return "tool directive" if DIRECTIVE.search(text) else "comment"


def python_comment_violations(source: SourceFile, text: str) -> Iterator[Violation]:
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        if is_shebang(line, column, token.string):
            continue
        yield Violation(source.relative, line, comment_reason(token.string))


def is_string_statement(node: ast.AST) -> TypeGuard[ast.Expr]:
    if not isinstance(node, ast.Expr):
        return False
    value = node.value
    if isinstance(value, ast.JoinedStr):
        return True
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def call_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return ""


def string_value(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [string_value(value) for value in node.values]
        if all(part is not None for part in parts):
            return "".join(part for part in parts if part is not None)
    return None


def positional_or_keyword(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(node.args) > index:
        return node.args[index]
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return None


def first_string_argument(node: ast.Call) -> str | None:
    return string_value(node.args[0]) if node.args else None


def top_level_module(name: str) -> str:
    return re.split(r"[.:]", name.lstrip("."), maxsplit=1)[0]


def guarded_dynamic_import(node: ast.Call) -> str | None:
    target = first_string_argument(node)
    package = string_value(positional_or_keyword(node, 1, PACKAGE_KEYWORD))
    for candidate in (package, target):
        if candidate is not None and top_level_module(candidate) in (
            GUARDED_DYNAMIC_MODULES
        ):
            return candidate
    return None


def keyword_names(node: ast.Call) -> set[str]:
    return {keyword.arg for keyword in node.keywords if keyword.arg is not None}


def is_broad_exception(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Tuple):
        return any(is_broad_exception(element) for element in node.elts)
    if isinstance(node, ast.Name):
        return node.id in BROAD_EXCEPTIONS
    if isinstance(node, ast.Attribute):
        return node.attr in BROAD_EXCEPTIONS
    return False


def raises_within(node: ast.AST) -> Iterator[ast.Raise]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Raise):
            yield child
        elif not isinstance(child, SCOPE_NODES):
            yield from raises_within(child)


def names_handler(node: ast.expr | None, handler: ast.ExceptHandler) -> bool:
    return isinstance(node, ast.Name) and node.id == handler.name


def reraises(handler: ast.ExceptHandler) -> bool:
    for statement in raises_within(handler):
        if statement.exc is None:
            return True
        if names_handler(statement.exc, handler):
            return True
        if names_handler(statement.cause, handler):
            return True
    return False


def is_blind_handler(node: ast.AST) -> TypeGuard[ast.ExceptHandler]:
    return (
        isinstance(node, ast.ExceptHandler)
        and is_broad_exception(node.type)
        and not reraises(node)
    )


def call_violations(source: SourceFile, node: ast.Call) -> Iterator[Violation]:
    name = call_name(node)
    line = node.lineno
    if name in STRING_MARKER_CALLS and first_string_argument(node) is not None:
        yield Violation(source.relative, line, "string marker")
    if name in SKIP_METHOD_CALLS:
        yield Violation(source.relative, line, "unittest skip method")
    if name in RUNTIME_CONFIGURATION_CALLS:
        yield Violation(source.relative, line, "runtime configuration change")
    if (
        name in WARNING_FILTER_CALLS
        and "action" in keyword_names(node)
        and source.relative not in ALLOWED_WARNING_NAMES
    ):
        yield Violation(source.relative, line, "warning filter")
    if name in DYNAMIC_IMPORT_CALLS:
        target = guarded_dynamic_import(node)
        if target is not None:
            yield Violation(source.relative, line, f"dynamic import of {target}")
    if name == "get" and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        module = first_string_argument(node)
        if (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == MODULES_ATTRIBUTE
            and module is not None
            and top_level_module(module) in GUARDED_DYNAMIC_MODULES
        ):
            yield Violation(source.relative, line, f"dynamic import of {module}")
    if name == GETATTR and len(node.args) > 1:
        attribute = string_value(node.args[1])
        if attribute in BANNED_ATTRIBUTES:
            yield Violation(source.relative, line, f"dynamic access to {attribute}")


def guarded_modules_lookup(node: ast.Subscript) -> str | None:
    if not (
        isinstance(node.value, ast.Attribute) and node.value.attr == MODULES_ATTRIBUTE
    ):
        return None
    key = string_value(node.slice)
    if key is not None and top_level_module(key) in GUARDED_DYNAMIC_MODULES:
        return key
    return None


def function_violations(
    source: SourceFile, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> Iterator[Violation]:
    arguments = node.args
    for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
        if argument.arg in MOCK_FIXTURES:
            yield Violation(
                source.relative, argument.lineno, f"mock fixture {argument.arg}"
            )


def blind_handler_violations(
    source: SourceFile, tree: ast.Module
) -> Iterator[Violation]:
    handlers = [node for node in ast.walk(tree) if is_blind_handler(node)]
    allowed = ALLOWED_BLIND_HANDLERS.get(source.relative)
    if allowed is None:
        for handler in handlers:
            yield Violation(source.relative, handler.lineno, "blind exception handler")
    elif len(handlers) != allowed:
        yield Violation(
            source.relative,
            1,
            f"{len(handlers)} blind exception handlers, {allowed} allowed",
        )


def imported_names(node: ast.stmt) -> Iterator[tuple[str, str | None]]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name, alias.asname
    if isinstance(node, ast.ImportFrom):
        dots = "." * node.level
        source = f"{dots}{node.module}." if node.module else dots
        for alias in node.names:
            yield f"{source}{alias.name}", alias.asname


def warning_import_violations(
    source: SourceFile, tree: ast.Module
) -> Iterator[Violation]:
    allowed = ALLOWED_WARNING_IMPORTS.get(source.relative)
    if allowed is None:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for qualified, alias in imported_names(node):
            if qualified not in allowed or alias is not None:
                yield Violation(
                    source.relative,
                    node.lineno,
                    f"import of {qualified} is not allowed",
                )


def is_narrow_message(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == EXPRESSION_INDEX_PATTERN_NAME
    message = string_value(node)
    return message not in (None, "", MATCH_EVERYTHING)


def is_allowed_category(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in ALLOWED_WARNING_CATEGORIES


def is_blanket_filter(node: ast.Call) -> bool:
    name = call_name(node)
    if name == CONTEXT_FILTER:
        return bool(node.args or node.keywords)
    if name == SIMPLE_FILTER:
        return first_string_argument(node) != ALWAYS_ACTION
    if name == MESSAGE_FILTER:
        if len(node.args) < MESSAGE_FILTER_ARGUMENTS or node.keywords:
            return True
        return not (
            is_narrow_message(node.args[1]) and is_allowed_category(node.args[2])
        )
    return False


def changes_pattern(node: ast.stmt) -> bool:
    if not isinstance(node, ast.AnnAssign | ast.Assign) or node.value is None:
        return False
    if EXPRESSION_INDEX_PATTERN_NAME not in bound_names(node):
        return False
    return string_value(node.value) != EXPRESSION_INDEX_PATTERN


def warning_definition_violations(
    source: SourceFile, tree: ast.Module
) -> Iterator[Violation]:
    allowed = ALLOWED_WARNING_DEFINITIONS.get(source.relative, frozenset())
    for node in module_scope_statements(tree.body):
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for name in bound_names(node):
            if name not in allowed:
                yield Violation(
                    source.relative, node.lineno, f"definition of {name} is not allowed"
                )
        if changes_pattern(node):
            yield Violation(source.relative, node.lineno, "filter pattern changed")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_blanket_filter(node):
            yield Violation(source.relative, node.lineno, "blanket warning filter")


def warning_name_violations(
    source: SourceFile, tree: ast.Module
) -> Iterator[Violation]:
    allowed = ALLOWED_WARNING_NAMES.get(source.relative)
    if allowed is None:
        return
    digest = hashlib.sha256(source.path.read_bytes()).hexdigest()
    if digest != ALLOWED_WARNING_DIGESTS[source.relative]:
        yield Violation(source.relative, 1, "content differs from the recorded digest")
    yield from warning_import_violations(source, tree)
    yield from warning_definition_violations(source, tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == WARNINGS_MODULE
            and node.attr not in allowed
        ):
            yield Violation(
                source.relative,
                node.lineno,
                f"warnings.{node.attr} is not allowed",
            )


def target_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from target_names(element)
    elif isinstance(target, ast.Starred):
        yield from target_names(target.value)


def expression_bound_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.NamedExpr):
        return node.target.id
    if isinstance(node, ast.MatchAs | ast.MatchStar):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def walrus_names(node: ast.stmt) -> Iterator[str]:
    pending: list[ast.AST] = [
        child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)
    ]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Lambda | ast.stmt):
            continue
        name = expression_bound_name(current)
        if name is not None:
            yield name
        pending.extend(ast.iter_child_nodes(current))


def bound_names(node: ast.stmt) -> list[str]:
    return [*direct_bound_names(node), *walrus_names(node)]


def direct_bound_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in target_names(target)]
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return list(target_names(node.target))
    if isinstance(node, ast.For | ast.AsyncFor):
        return list(target_names(node.target))
    if isinstance(node, ast.With | ast.AsyncWith):
        return [
            name
            for item in node.items
            if item.optional_vars is not None
            for name in target_names(item.optional_vars)
        ]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.ImportFrom):
        return [alias.asname or alias.name for alias in node.names]
    if isinstance(node, ast.Import):
        return [alias.asname for alias in node.names if alias.asname is not None]
    return []


def nested_blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    if isinstance(statement, ast.If | ast.For | ast.AsyncFor | ast.While):
        return [statement.body, statement.orelse]
    if isinstance(statement, ast.With | ast.AsyncWith):
        return [statement.body]
    if isinstance(statement, ast.Try | ast.TryStar):
        handler_bodies = [handler.body for handler in statement.handlers]
        return [statement.body, *handler_bodies, statement.orelse, statement.finalbody]
    if isinstance(statement, ast.Match):
        return [case.body for case in statement.cases]
    return []


def module_scope_statements(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    for statement in statements:
        yield statement
        for block in nested_blocks(statement):
            yield from module_scope_statements(block)


def module_bindings(tree: ast.Module) -> Iterator[tuple[ast.AST, str]]:
    for statement in module_scope_statements(tree.body):
        for name in bound_names(statement):
            yield statement, name
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            for name in node.names:
                yield node, name


def hook_violations(source: SourceFile, tree: ast.Module) -> Iterator[Violation]:
    allowed = ALLOWED_HOOKS.get(source.relative, frozenset())
    for node, name in module_bindings(tree):
        line = getattr(node, "lineno", 1)
        if name in COLLECTION_OVERRIDES:
            yield Violation(source.relative, line, f"collection override {name}")
        elif name.startswith(HOOK_PREFIX) and name not in allowed:
            yield Violation(source.relative, line, f"unlisted pytest hook {name}")


def adverse_fixture_violations(
    source: SourceFile, tree: ast.Module
) -> Iterator[Violation]:
    if source.relative == ADVERSE_HELPER:
        if ast.dump(tree) != ast.dump(ast.parse(ADVERSE_SOURCE)):
            yield Violation(source.relative, 1, "adverse helper structure changed")
        return
    if source.relative == ADVERSE_CONSUMER:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for qualified, _alias in imported_names(node):
                if ADVERSE_NAMES.intersection(qualified.split(".")) or (
                    qualified == "kora_tooling_fixture"
                ):
                    yield Violation(source.relative, node.lineno, "adverse import")
        if isinstance(node, ast.Name) and node.id in ADVERSE_NAMES:
            yield Violation(source.relative, node.lineno, "adverse reference")
        if isinstance(node, ast.Attribute) and node.attr in ADVERSE_NAMES:
            yield Violation(source.relative, node.lineno, "adverse reference")


def python_violations(source: SourceFile, text: str) -> Iterator[Violation]:
    tree = ast.parse(text, filename=source.relative)
    yield from python_comment_violations(source, text)
    for node in ast.walk(tree):
        if is_string_statement(node):
            yield Violation(source.relative, node.lineno, "string statement")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == PLUGIN_MANAGER
            and source.relative.startswith(PLUGIN_MANAGER_DIRECTORIES)
        ):
            yield Violation(source.relative, node.lineno, "plugin manager access")
        if isinstance(node, ast.Subscript):
            module = guarded_modules_lookup(node)
            if module is not None:
                yield Violation(
                    source.relative, node.lineno, f"dynamic import of {module}"
                )
        if isinstance(node, ast.Call):
            yield from call_violations(source, node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from function_violations(source, node)
    yield from blind_handler_violations(source, tree)
    yield from warning_name_violations(source, tree)
    yield from adverse_fixture_violations(source, tree)
    yield from hook_violations(source, tree)


def strip_quotes(line: str) -> str:
    return QUOTED.sub("", line)


def shell_comment(line: str) -> str | None:
    stripped = strip_quotes(line)
    for index, character in enumerate(stripped):
        if character != "#":
            continue
        if index == 0 or stripped[index - 1] in SHELL_COMMENT_PRECEDERS:
            return stripped[index:]
    return None


def marker_comment(line: str, markers: tuple[str, ...]) -> str | None:
    stripped = strip_quotes(line)
    positions = [stripped.find(marker) for marker in markers]
    found = [position for position in positions if position >= 0]
    if not found:
        return None
    return stripped[min(found) :]


def line_comment(language: Language, line: str) -> str | None:
    if language is Language.SHELL:
        return shell_comment(line)
    if language is Language.SQL:
        return marker_comment(line, ("--", "/*"))
    if language is Language.TEMPLATE:
        return marker_comment(line, ("#", "<%doc>"))
    return marker_comment(line, ("//", "/*"))


def line_violations(source: SourceFile, text: str) -> Iterator[Violation]:
    for number, line in enumerate(text.splitlines(), start=1):
        if is_shebang(number, 0, line):
            continue
        comment = line_comment(source.language, line)
        if comment is None:
            continue
        reason = comment_reason(comment)
        if source.language is Language.SCRIPT and reason == "comment":
            continue
        yield Violation(source.relative, number, reason)


def file_violations(source: SourceFile) -> Iterator[Violation]:
    text = source.path.read_text(encoding="utf-8-sig")
    if source.language is Language.PYTHON:
        yield from python_violations(source, text)
    else:
        yield from line_violations(source, text)


def describe(violations: set[Violation]) -> list[str]:
    ordered = sorted(violations, key=lambda v: (v.relative, v.line, v.reason))
    return [violation.describe() for violation in ordered]


def source_violations(root: Path) -> list[str]:
    return describe(
        {
            violation
            for source in source_files(root)
            for violation in file_violations(source)
        }
    )


def as_table(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def table(document: dict[str, object], *keys: str) -> dict[str, object]:
    current = document
    for key in keys:
        current = as_table(current.get(key))
    return current


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [item for item in items if isinstance(item, str)]


def pyproject_violations(root: Path) -> Iterator[Violation]:
    path = root / PYPROJECT
    if not path.is_file():
        yield Violation(PYPROJECT, 1, "missing")
        return
    with path.open("rb") as handle:
        document: dict[str, object] = tomllib.load(handle)
    ruff = table(document, "tool", "ruff")
    pyright = table(document, "tool", "pyright")
    pytest_options = table(document, "tool", "pytest", "ini_options")
    if ruff != RUFF_TABLE:
        yield Violation(PYPROJECT, 1, "[tool.ruff] differs from the recorded table")
    if string_list(table(ruff, "lint").get("ignore")):
        yield Violation(PYPROJECT, 1, "[tool.ruff.lint] ignore is not empty")
    if pyright != PYRIGHT_TABLE:
        yield Violation(PYPROJECT, 1, "[tool.pyright] differs from the recorded table")
    if pyright.get("typeCheckingMode") != "strict":
        yield Violation(PYPROJECT, 1, "[tool.pyright] typeCheckingMode is not strict")
    if pyright.get("enableTypeIgnoreComments") is not False:
        yield Violation(PYPROJECT, 1, "[tool.pyright] honours type: ignore comments")
    if pytest_options != PYTEST_TABLE:
        yield Violation(
            PYPROJECT, 1, "[tool.pytest.ini_options] differs from the recorded table"
        )
    for entry in string_list(pytest_options.get("filterwarnings")):
        if entry != WARNINGS_AS_ERRORS and not entry.startswith(
            f"{WARNINGS_AS_ERRORS}:"
        ):
            yield Violation(PYPROJECT, 1, f"filterwarnings {entry!r} is not error")
    missing = STRICT_ADDOPTS - set(string_list(pytest_options.get("addopts")))
    for option in sorted(missing):
        yield Violation(PYPROJECT, 1, f"addopts lacks {option}")
    if PYTEST_ENTRY_POINTS in table(document, "project", "entry-points"):
        yield Violation(PYPROJECT, 1, "registers pytest plugins through entry points")


def shadows_eslint_config(relative: str) -> bool:
    name = Path(relative).name
    return relative != ESLINT_CONFIG and name.startswith(ESLINT_CONFIG_PREFIXES)


def shadow_violations(root: Path) -> Iterator[Violation]:
    for path in walk_files(root):
        relative = relative_to(root, path)
        if path.name in SHADOW_NAMES and relative != PYPROJECT:
            yield Violation(relative, 1, "shadows pyproject.toml")
        if shadows_eslint_config(relative):
            yield Violation(relative, 1, "shadows eslint.config.mjs")


def json_document(path: Path) -> dict[str, object]:
    return as_table(json.loads(path.read_text(encoding="utf-8")))


def web_configuration_violations(root: Path) -> Iterator[Violation]:
    for relative, recorded in TSCONFIG_TABLES.items():
        path = root / relative
        if not path.is_file():
            yield Violation(relative, 1, "missing")
            continue
        document = json_document(path)
        if document != recorded:
            yield Violation(relative, 1, "differs from the recorded document")
        if relative in STRICT_TSCONFIGS and (
            table(document, "compilerOptions").get("strict") is not True
        ):
            yield Violation(relative, 1, "compilerOptions.strict is not true")
    if not (root / ESLINT_CONFIG).is_file():
        yield Violation(ESLINT_CONFIG, 1, "missing")
    package = root / WEB_PACKAGE
    if not package.is_file():
        yield Violation(WEB_PACKAGE, 1, "missing")
    elif table(json_document(package), "scripts") != WEB_SCRIPTS:
        yield Violation(WEB_PACKAGE, 1, "scripts differ from the recorded table")


def generated_file_violations(root: Path) -> Iterator[Violation]:
    for relative in GENERATED_FILES:
        if not (root / relative).is_file():
            yield Violation(relative, 1, "listed generated file is missing")
    for relative in (*ALLOWED_WARNING_DIGESTS, ADVERSE_HELPER):
        if not (root / relative).is_file():
            yield Violation(relative, 1, "listed allowance file is missing")


def configuration_violations(root: Path) -> list[str]:
    return describe(
        {
            *pyproject_violations(root),
            *shadow_violations(root),
            *web_configuration_violations(root),
            *generated_file_violations(root),
        }
    )


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def replaced(text: str, target: str, replacement: str) -> str:
    result = text.replace(target, replacement)
    assert result != text, target
    return result


def copy_configuration(root: Path) -> None:
    for relative in (
        PYPROJECT,
        *WEB_CONFIGURATION,
        *ALLOWED_WARNING_DIGESTS,
        ADVERSE_HELPER,
    ):
        write(root, relative, (REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))
    for relative in GENERATED_FILES:
        write(root, relative, "# noqa\n")


def test_hand_written_sources_carry_no_suppression() -> None:
    root = REPOSITORY_ROOT

    violations = source_violations(root)

    assert violations == []


def test_adverse_fixture_allowance_is_confined(tmp_path: Path) -> None:
    cases = {
        "ordinary": (
            "tests/test_case.py",
            "from kora_tooling_fixture.adverse_outcomes import skip_fixture\n"
            "skip_fixture()\n",
        ),
        "production": (
            "src/kora/module.py",
            "import kora_tooling_fixture.fixtures_adverse\n",
        ),
        "alias": (
            "tests/test_case.py",
            "from kora_tooling_fixture.adverse_outcomes import xfail_fixture as f\n"
            "f()\n",
        ),
        "parent": (
            "tests/test_case.py",
            "import kora_tooling_fixture as f\nf.adverse_outcomes.skip_fixture()\n",
        ),
        "relative": (
            ADVERSE_DIRECTORY + "other.py",
            "from . import adverse_outcomes as f\nf.skip_fixture()\n",
        ),
        "dynamic": (
            "tests/test_case.py",
            "from importlib import import_module\n"
            "module = import_module('kora_tooling_fixture.adverse_outcomes')\n"
            "getattr(module, 'skip_fixture')()\n",
        ),
        "extra_call": (ADVERSE_HELPER, ADVERSE_SOURCE + "pytest.skip('extra')\n"),
        "extra_import": (ADVERSE_HELPER, ADVERSE_SOURCE + "import os\n"),
    }
    for name, (relative, source) in cases.items():
        write(tmp_path / name, relative, source)
    write(tmp_path / "allowed", ADVERSE_HELPER, ADVERSE_SOURCE)
    write(
        tmp_path / "allowed",
        ADVERSE_CONSUMER,
        "from kora_tooling_fixture.adverse_outcomes import skip_fixture\n"
        "skip_fixture()\n",
    )

    results = {name: source_violations(tmp_path / name) for name in cases}
    allowed = source_violations(tmp_path / "allowed")

    assert all(results.values()), results
    assert any(
        "dynamic import of kora_tooling_fixture.adverse_outcomes" in finding
        for finding in results["dynamic"]
    )
    assert any(
        "dynamic access to skip_fixture" in finding for finding in results["dynamic"]
    )
    assert allowed == []


def test_check_configuration_is_not_weakened() -> None:
    root = REPOSITORY_ROOT

    violations = configuration_violations(root)

    assert violations == []


def test_checker_flags_directives_in_every_language(tmp_path: Path) -> None:
    write(
        tmp_path,
        "src/kora/module.py",
        "import os  # noqa: F401\n"
        "value: int = 'x'  # type: ignore[assignment]\n"
        "other = value  #pyright: ignore[reportUnknownVariableType]\n"
        "def f() -> None:  # pragma: no cover\n"
        "    return None\n"
        "# fmt: off\n"
        "# ruff: noqa: E501\n"
        "# isort: skip_file\n"
        "x = os.system('ls')  # nosec\n"
        "# mypy: ignore-errors\n"
        "text = '# noqa inside a string is fine'\n",
    )
    write(tmp_path, "scripts/run.sh", "#!/bin/sh\nset -e # shellcheck disable=SC1\n")
    write(tmp_path, "scripts/deploy", "#!/usr/bin/env bash\necho ok # noqa\n")
    write(tmp_path, "scripts/strict", "#!/usr/bin/env -S bash -eu\necho ok # noqa\n")
    write(tmp_path, "scripts/plain", "#!/bin/sh -e\necho ok # noqa\n")
    write(tmp_path, "scripts/tool", "#!/usr/bin/env python3.11\nx = 1  # noqa\n")
    write(tmp_path, "db/query.sql", "SELECT 1; -- noqa\n/* nosec */ SELECT 2;\n")
    write(tmp_path, "db/script.py.mako", "import sqlalchemy as sa  # noqa\n")
    write(
        tmp_path,
        "web/src/App.tsx",
        "// eslint-disable-next-line no-console\n"
        "/* @ts-ignore */\n"
        "const a = 1 // prettier-ignore\n"
        "const b = '@ts-ignore'\n"
        "const c = /https?:\\/\\//\n"
        "const d = `// eslint-disable`\n"
        "export { a, b, c, d }\n",
    )
    write(tmp_path, "web/vite.config.mts", "// @ts-expect-error\nexport default {}\n")
    write(tmp_path, "web/src/Old.jsx", "// a comment\n// eslint-disable\n")

    violations = source_violations(tmp_path)

    assert violations == [
        "db/query.sql:1: tool directive",
        "db/query.sql:2: tool directive",
        "db/script.py.mako:1: tool directive",
        "scripts/deploy:2: tool directive",
        "scripts/plain:2: tool directive",
        "scripts/run.sh:2: comment",
        "scripts/strict:2: tool directive",
        "scripts/tool:2: tool directive",
        "src/kora/module.py:1: tool directive",
        "src/kora/module.py:2: tool directive",
        "src/kora/module.py:3: tool directive",
        "src/kora/module.py:4: tool directive",
        "src/kora/module.py:6: tool directive",
        "src/kora/module.py:7: tool directive",
        "src/kora/module.py:8: tool directive",
        "src/kora/module.py:9: tool directive",
        "src/kora/module.py:10: tool directive",
        "web/src/App.tsx:1: tool directive",
        "web/src/App.tsx:2: tool directive",
        "web/src/App.tsx:3: tool directive",
        "web/src/Old.jsx:2: tool directive",
        "web/vite.config.mts:1: tool directive",
    ]


def test_checker_flags_comments_docstrings_and_mock_fixtures(tmp_path: Path) -> None:
    write(
        tmp_path,
        "src/kora/module.py",
        '"""Module docstring."""\n'
        "import importlib\n"
        "import pkgutil\n"
        "import warnings\n"
        "# a comment\n"
        "class Thing:\n"
        "    'class docstring'\n"
        "    value: int = 1\n"
        "    'attribute docstring'\n"
        "def f(monkeypatch: object, *, mocker: object) -> None:\n"
        '    """Function docstring."""\n'
        "    f'{monkeypatch} as a statement'\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        return None\n"
        "    try:\n"
        "        pass\n"
        "    except (ValueError, BaseException) as error:\n"
        "        raise error\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "    __import__('pytest').skip('x')\n"
        "    importlib.import_module('warnings').simplefilter('ignore')\n"
        "    importlib.import_module('.outcomes', package='_pytest')\n"
        "    pkgutil.resolve_name('pytest:skip')\n"
        "    getattr(pytest, 'skip')('x')\n"
        "    sys.modules['pytest'].skip('x')\n"
        "    sys.modules.get('pytest').skip('x')\n"
        "    with warnings.catch_warnings(action='ignore'):\n"
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as error:\n"
        "        def later() -> None:\n"
        "            raise error\n"
        "        return None\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as error:\n"
        "        try:\n"
        "            pass\n"
        "        except ValueError:\n"
        "            raise\n"
        "        return None\n",
    )
    write(
        tmp_path,
        "tests/conftest.py",
        "import pytest\n"
        "from tests.hooks import configure as pytest_configure\n"
        "from tests.hooks import plugins as pytest_plugins\n"
        "collect_ignore = ['slow_test.py']\n"
        "def pytest_report_header() -> str:\n"
        "    return 'ok'\n"
        "def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:\n"
        "    for item in items:\n"
        "        item.add_marker('skip')\n"
        "        item.add_marker(f'xfail')\n"
        "def _report() -> None:\n"
        "    return None\n"
        "pytest_runtest_makereport = _report\n"
        "def pytest_sessionstart(session: pytest.Session) -> None:\n"
        "    session.config.addinivalue_line('filterwarnings', 'ignore')\n"
        "    session.config.pluginmanager.register(object())\n"
        "if True:\n"
        "    def pytest_runtest_setup() -> None:\n"
        "        return None\n"
        "    pytest_runtest_call, other = _report, None\n"
        "import tests.hooks as pytest_runtest_teardown\n"
        "(pytest_runtest_logreport := _report)\n"
        "def helper(_: object = (pytest_collection_finish := _report)) -> None:\n"
        "    global pytest_sessionfinish\n"
        "    pytest_sessionfinish = _report\n"
        "with nullcontext(pytest_runtest_protocol := _report):\n"
        "    pass\n"
        "match _report:\n"
        "    case pytest_keyboard_interrupt:\n"
        "        pass\n",
    )
    write(
        tmp_path,
        "tests/test_things.py",
        "def test_it(request: object) -> None:\n"
        "    request.applymarker('xfail')\n"
        "    request.applymarker(marker)\n"
        "    request.skipTest('x')\n"
        "pytest_plugins = ['tests.helpers.report']\n"
        "def pytest_runtest_makereport() -> None:\n"
        "    return None\n",
    )
    write(
        tmp_path,
        "src/kora/__main__.py",
        "try:\n    pass\nexcept Exception:\n    pass\n"
        "try:\n    pass\nexcept BaseException:\n    pass\n",
    )
    write(tmp_path, "src/kora/observability/sink.py", "x = 1\n")
    write(
        tmp_path,
        WARNING_FILTERS_MODULE,
        "import warnings\n"
        "import pytest\n"
        "import warnings as w\n"
        "from warnings import resetwarnings\n"
        "from contextlib import suppress\n"
        "warnings.resetwarnings()\n"
        "warnings.filters.clear()\n"
        "def quietly() -> None:\n"
        "    import unittest\n"
        "    from . import helpers\n"
        "    warnings.simplefilter('ignore')\n"
        "    warnings.filterwarnings('ignore')\n"
        "    warnings.filterwarnings('ignore', '', Warning)\n"
        "    warnings.filterwarnings('ignore', '.*', SAWarning)\n"
        "    warnings.filterwarnings('ignore', 'x', Warning)\n"
        "    with warnings.catch_warnings(action='ignore'):\n"
        "        pass\n"
        "EXPRESSION_INDEX_WARNING: str = ''\n",
    )
    write(tmp_path, "tests/measurements/test_slow.py", "# measured\nx = 1\n")
    write(tmp_path, "tests/bom.py", "\ufeff# after a byte order mark\nx = 1\n")
    write(
        tmp_path,
        "scripts/run.sh",
        '#!/bin/bash\n# a comment\necho "# not" ;# yes\ncase $1 in a)# tail\nesac\n',
    )
    write(tmp_path, "db/view.sql", "SELECT 1; -- a comment\nSELECT '-- not';\n")
    write(tmp_path, "db/script.py.mako", "## template comment\n<%doc>block</%doc>\n")

    violations = source_violations(tmp_path)

    assert violations == [
        "db/script.py.mako:1: comment",
        "db/script.py.mako:2: comment",
        "db/view.sql:1: comment",
        "scripts/run.sh:2: comment",
        "scripts/run.sh:3: comment",
        "scripts/run.sh:4: comment",
        "src/kora/__main__.py:1: 2 blind exception handlers, 1 allowed",
        "src/kora/module.py:1: string statement",
        "src/kora/module.py:5: comment",
        "src/kora/module.py:7: string statement",
        "src/kora/module.py:9: string statement",
        "src/kora/module.py:10: mock fixture mocker",
        "src/kora/module.py:10: mock fixture monkeypatch",
        "src/kora/module.py:11: string statement",
        "src/kora/module.py:12: string statement",
        "src/kora/module.py:15: blind exception handler",
        "src/kora/module.py:23: blind exception handler",
        "src/kora/module.py:25: dynamic import of pytest",
        "src/kora/module.py:26: dynamic import of warnings",
        "src/kora/module.py:27: dynamic import of _pytest",
        "src/kora/module.py:28: dynamic import of pytest:skip",
        "src/kora/module.py:29: dynamic access to skip",
        "src/kora/module.py:30: dynamic import of pytest",
        "src/kora/module.py:31: dynamic import of pytest",
        "src/kora/module.py:32: warning filter",
        "src/kora/module.py:36: blind exception handler",
        "src/kora/module.py:42: blind exception handler",
        "src/kora/observability/sink.py:1: 0 blind exception handlers, 1 allowed",
        "tests/bom.py:1: comment",
        "tests/conftest.py:2: unlisted pytest hook pytest_configure",
        "tests/conftest.py:3: collection override pytest_plugins",
        "tests/conftest.py:4: collection override collect_ignore",
        "tests/conftest.py:7: unlisted pytest hook pytest_collection_modifyitems",
        "tests/conftest.py:9: string marker",
        "tests/conftest.py:10: string marker",
        "tests/conftest.py:13: unlisted pytest hook pytest_runtest_makereport",
        "tests/conftest.py:14: unlisted pytest hook pytest_sessionstart",
        "tests/conftest.py:15: runtime configuration change",
        "tests/conftest.py:16: plugin manager access",
        "tests/conftest.py:18: unlisted pytest hook pytest_runtest_setup",
        "tests/conftest.py:20: unlisted pytest hook pytest_runtest_call",
        "tests/conftest.py:21: unlisted pytest hook pytest_runtest_teardown",
        "tests/conftest.py:22: unlisted pytest hook pytest_runtest_logreport",
        "tests/conftest.py:23: unlisted pytest hook pytest_collection_finish",
        "tests/conftest.py:24: unlisted pytest hook pytest_sessionfinish",
        "tests/conftest.py:26: unlisted pytest hook pytest_runtest_protocol",
        "tests/conftest.py:28: unlisted pytest hook pytest_keyboard_interrupt",
        "tests/measurements/test_slow.py:1: comment",
        f"{WARNING_FILTERS_MODULE}:1: content differs from the recorded digest",
        f"{WARNING_FILTERS_MODULE}:2: import of pytest is not allowed",
        f"{WARNING_FILTERS_MODULE}:3: import of warnings is not allowed",
        f"{WARNING_FILTERS_MODULE}:4: import of warnings.resetwarnings is not allowed",
        f"{WARNING_FILTERS_MODULE}:5: import of contextlib.suppress is not allowed",
        f"{WARNING_FILTERS_MODULE}:6: warnings.resetwarnings is not allowed",
        f"{WARNING_FILTERS_MODULE}:7: warnings.filters is not allowed",
        f"{WARNING_FILTERS_MODULE}:8: definition of quietly is not allowed",
        f"{WARNING_FILTERS_MODULE}:9: import of unittest is not allowed",
        f"{WARNING_FILTERS_MODULE}:10: import of .helpers is not allowed",
        f"{WARNING_FILTERS_MODULE}:11: blanket warning filter",
        f"{WARNING_FILTERS_MODULE}:12: blanket warning filter",
        f"{WARNING_FILTERS_MODULE}:13: blanket warning filter",
        f"{WARNING_FILTERS_MODULE}:14: blanket warning filter",
        f"{WARNING_FILTERS_MODULE}:15: blanket warning filter",
        f"{WARNING_FILTERS_MODULE}:16: blanket warning filter",
        f"{WARNING_FILTERS_MODULE}:18: filter pattern changed",
        "tests/test_things.py:2: string marker",
        "tests/test_things.py:4: unittest skip method",
        "tests/test_things.py:5: collection override pytest_plugins",
        "tests/test_things.py:6: unlisted pytest hook pytest_runtest_makereport",
    ]


def test_checker_allows_shebangs_and_listed_generated_files(tmp_path: Path) -> None:
    copy_configuration(tmp_path)
    write(tmp_path, "scripts/run.py", "#!/usr/bin/env python3\nprint('ok')\n")
    write(tmp_path, "scripts/marked.py", "\ufeff#!/usr/bin/env python3\nprint('ok')\n")
    write(tmp_path, "scripts/run.sh", "#!/bin/sh\necho \"${x#y}\" $# '#' $(cmd)\n")
    write(tmp_path, "scripts/deploy", "#!/usr/bin/env bash\necho ok\n")
    write(tmp_path, "scripts/notes", "plain text # not scanned\n")
    write(tmp_path, "scripts/other", "#!/usr/bin/env perl\n# perl is not scanned\n")
    write(
        tmp_path,
        "src/kora/module.py",
        "text = '# noqa'\n"
        'other = """\n# type: ignore\n"""\n'
        "def f() -> None:\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        raise\n"
        "    try:\n"
        "        pass\n"
        "    except BaseException as error:\n"
        "        raise error\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as error:\n"
        "        raise RuntimeError('wrapped') from error\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as error:\n"
        "        if error:\n"
        "            raise\n"
        "    request.applymarker(marker)\n"
        "    importlib.import_module('kora.plugins', package='kora')\n",
    )
    write(
        tmp_path,
        "src/kora/__main__.py",
        "try:\n    pass\nexcept Exception:\n    pass\n",
    )
    write(
        tmp_path,
        "src/kora/observability/bootstrap.py",
        "try:\n    pass\nexcept Exception:\n    pass\n"
        "try:\n    pass\nexcept Exception:\n    pass\n",
    )
    write(
        tmp_path,
        "src/kora/observability/sink.py",
        "try:\n    pass\nexcept BaseException:\n    pass\n",
    )
    write(
        tmp_path,
        WARNING_FILTERS_MODULE,
        (REPOSITORY_ROOT / WARNING_FILTERS_MODULE).read_text(encoding="utf-8"),
    )
    write(
        tmp_path,
        "tests/conftest.py",
        "import pytest_asyncio\n"
        "def pytest_report_header() -> str:\n    return 'ok'\n"
        "def pytest_generate_tests(metafunc: object) -> None:\n    return None\n",
    )
    write(tmp_path, "tests/observability/conftest.py", "x = 1\n")
    write(tmp_path, "db/view.sql", "SELECT '-- not a comment', \"/* nor this */\";\n")
    write(tmp_path, "db/script.py.mako", "revision: str = ${repr(up_revision)}\n")
    write(
        tmp_path,
        "web/src/App.tsx",
        "const a = /https?:\\/\\/ noqa/\n"
        "const b = '@ts-ignore'\n"
        "const c = <a>http://example.test</a>\n"
        "export { a, b, c }\n",
    )
    write(tmp_path, "measurements/out.py", "# generated output\n")
    write(tmp_path, "web/node_modules/pkg/index.js", "// eslint-disable\n")
    write(tmp_path, "web/dist/bundle.js", "/* @ts-nocheck */\n")
    write(tmp_path, "src/kora/api/static/app.js", "// @ts-ignore\n")
    write(tmp_path, ".venv/lib/site.py", "# noqa\n")
    write(tmp_path, ".claude/worktrees/copy/module.py", "# noqa\n")
    write(tmp_path, "src/kora/__pycache__/cached.py", "# noqa\n")

    violations = [*source_violations(tmp_path), *configuration_violations(tmp_path)]

    assert violations == []


def test_checker_flags_weakened_configuration(tmp_path: Path) -> None:
    copy_configuration(tmp_path)
    pyproject = (tmp_path / PYPROJECT).read_text(encoding="utf-8")
    pyproject = replaced(pyproject, '    "BLE",\n', "")
    pyproject = replaced(
        pyproject,
        "[tool.ruff.lint.per-file-ignores]\n",
        '[tool.ruff.lint.extend-per-file-ignores]\n"tests/**" = ["BLE001"]\n\n'
        "[tool.ruff.lint.per-file-ignores]\n",
    )
    pyproject = replaced(
        pyproject,
        'filterwarnings = ["error"]\n',
        'filterwarnings = ["error", "ignore::DeprecationWarning"]\n',
    )
    pyproject = replaced(
        pyproject, '    "--strict-markers",\n', '    "-p",\n    "no:warnings",\n'
    )
    pyproject = replaced(
        pyproject,
        'typeCheckingMode = "strict"\nenableTypeIgnoreComments = false\n',
        'typeCheckingMode = "basic"\nenableTypeIgnoreComments = true\n',
    )
    pyproject += '\n[project.entry-points.pytest11]\nquiet = "tests.quiet"\n'
    write(tmp_path, PYPROJECT, pyproject)
    write(tmp_path, "pytest.ini", "[pytest]\nfilterwarnings = ignore\n")
    write(tmp_path, "tests/.pytest.ini", "[pytest]\n")
    write(tmp_path, "tests/db/tox.ini", "[pytest]\n")
    write(tmp_path, "src/setup.cfg", "[tool:pytest]\n")
    write(tmp_path, "pyrightconfig.json", '{"typeCheckingMode": "basic"}\n')
    write(tmp_path, "src/ruff.toml", "[lint]\nselect = []\n")
    write(tmp_path, "src/kora/.ruff.toml", "[lint]\nselect = []\n")
    write(tmp_path, "tests/pyproject.toml", "[tool.ruff]\n")
    write(tmp_path, "web/.eslintrc.json", "{}\n")
    write(tmp_path, "web/eslint.config.js", "export default []\n")
    write(tmp_path, "web/src/eslint.config.mjs", "export default []\n")
    write(tmp_path, "eslint.config.js", "export default []\n")
    (tmp_path / ESLINT_CONFIG).unlink()
    write(
        tmp_path,
        "web/tsconfig.node.json",
        '{"compilerOptions": {"strict": false}, "include": ["vite.config.ts"]}\n',
    )
    app = json_document(tmp_path / "web/tsconfig.app.json")
    table(app, "compilerOptions")["strictNullChecks"] = False
    write(tmp_path, "web/tsconfig.app.json", json.dumps(app))
    write(tmp_path, "web/tsconfig.json", '{"files": [], "references": []}\n')
    package = json_document(tmp_path / WEB_PACKAGE)
    table(package, "scripts")["lint"] = "eslint src/App.tsx"
    write(tmp_path, WEB_PACKAGE, json.dumps(package))
    (tmp_path / "web/pnpm-lock.yaml").unlink()
    (tmp_path / WARNING_FILTERS_MODULE).unlink()

    violations = configuration_violations(tmp_path)

    assert violations == [
        "eslint.config.js:1: shadows eslint.config.mjs",
        "pyproject.toml:1: [tool.pyright] differs from the recorded table",
        "pyproject.toml:1: [tool.pyright] honours type: ignore comments",
        "pyproject.toml:1: [tool.pyright] typeCheckingMode is not strict",
        "pyproject.toml:1: [tool.pytest.ini_options] differs from the recorded table",
        "pyproject.toml:1: [tool.ruff] differs from the recorded table",
        "pyproject.toml:1: addopts lacks --strict-markers",
        "pyproject.toml:1: filterwarnings 'ignore::DeprecationWarning' is not error",
        "pyproject.toml:1: registers pytest plugins through entry points",
        "pyrightconfig.json:1: shadows pyproject.toml",
        "pytest.ini:1: shadows pyproject.toml",
        "src/kora/.ruff.toml:1: shadows pyproject.toml",
        "src/ruff.toml:1: shadows pyproject.toml",
        "src/setup.cfg:1: shadows pyproject.toml",
        "tests/.pytest.ini:1: shadows pyproject.toml",
        "tests/db/tox.ini:1: shadows pyproject.toml",
        "tests/pyproject.toml:1: shadows pyproject.toml",
        f"{WARNING_FILTERS_MODULE}:1: listed allowance file is missing",
        "web/.eslintrc.json:1: shadows eslint.config.mjs",
        "web/eslint.config.js:1: shadows eslint.config.mjs",
        "web/eslint.config.mjs:1: missing",
        "web/package.json:1: scripts differ from the recorded table",
        "web/pnpm-lock.yaml:1: listed generated file is missing",
        "web/src/eslint.config.mjs:1: shadows eslint.config.mjs",
        "web/tsconfig.app.json:1: differs from the recorded document",
        "web/tsconfig.json:1: differs from the recorded document",
        "web/tsconfig.node.json:1: compilerOptions.strict is not true",
        "web/tsconfig.node.json:1: differs from the recorded document",
    ]


def test_process_replacement_is_confined_to_the_fixed_gate() -> None:
    root = REPOSITORY_ROOT

    violations = replacement_violations(root)

    assert violations == []


def test_replacement_guard_accepts_only_the_designated_direct_call(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "src/kora/runner/gate.py",
        "import os\ndef execute_workload(config: object) -> None:\n"
        "    os.execve(config.executable, config.argv, config.environment)\n",
    )

    violations = replacement_violations(tmp_path)

    assert violations == []


def unapproved_gate_reference(line: int) -> str:
    return f"{REPLACEMENT_GATE}:{line}: unapproved process replacement"


def test_replacement_guard_refuses_missing_and_changed_call_sites(
    tmp_path: Path,
) -> None:
    cases = {
        "missing": None,
        "zero": "import os\ndef execute_workload() -> None:\n    pass\n",
        "twice": "import os\ndef execute_workload() -> None:\n"
        "    os.execve('x', (), {})\n    os.execve('x', (), {})\n",
        "wrong_function": "import os\ndef other() -> None:\n"
        "    os.execve('x', (), {})\n",
        "wrong_api": "import os\ndef execute_workload() -> None:\n"
        "    os.execv('x', ())\n",
        "module_alias": "import os as native\ndef execute_workload() -> None:\n"
        "    native.execve('x', (), {})\n",
        "import_alias": "from os import execve as replace\n"
        "def execute_workload() -> None:\n    replace('x', (), {})\n",
        "value_alias": "import os\nreplace = os.execve\n"
        "def execute_workload() -> None:\n    replace('x', (), {})\n",
        "default": "import os\ndef execute_workload(replace=os.execve) -> None:\n"
        "    replace('x', (), {})\n",
        "decorator": "import os\n@os.execve\ndef execute_workload() -> None:\n"
        "    pass\n",
        "dynamic": "import os\ndef execute_workload() -> None:\n"
        "    getattr(os, 'execve')('x', (), {})\n",
        "dictionary": "import os\ndef execute_workload() -> None:\n"
        "    os.__dict__['execve']('x', (), {})\n",
        "rebound": "import os\nos = replacement\n"
        "def execute_workload() -> None:\n    os.execve('x', (), {})\n",
        "argument_shadow": "import os\ndef execute_workload(os: object) -> None:\n"
        "    os.execve('x', (), {})\n",
        "other_exec": "import os\ndef execute_workload() -> None:\n"
        "    os.execve('x', (), {})\ndef other() -> None:\n    os.execl('x', 'x')\n",
    }
    missing = f"{REPLACEMENT_GATE}:1: fixed launch gate is missing"
    count = f"{REPLACEMENT_GATE}:1: fixed os.execve call count is not one"
    expected = {
        "missing": [missing, count],
        "zero": [count],
        "twice": [count],
        "wrong_function": [count, unapproved_gate_reference(3)],
        "wrong_api": [count, unapproved_gate_reference(3)],
        "module_alias": [count, unapproved_gate_reference(3)],
        "import_alias": [count, unapproved_gate_reference(1)],
        "value_alias": [count, unapproved_gate_reference(2)],
        "default": [count, unapproved_gate_reference(2)],
        "decorator": [count, unapproved_gate_reference(2)],
        "dynamic": [count, unapproved_gate_reference(3)],
        "dictionary": [count, unapproved_gate_reference(3)],
        "rebound": [count, unapproved_gate_reference(4)],
        "argument_shadow": [count, unapproved_gate_reference(3)],
        "other_exec": [unapproved_gate_reference(5)],
    }
    roots = {name: tmp_path / name for name in cases}
    for name, source in cases.items():
        roots[name].mkdir()
        if source is not None:
            write(roots[name], "src/kora/runner/gate.py", source)

    results = {name: replacement_violations(root) for name, root in roots.items()}

    assert results == expected


def test_replacement_guard_refuses_the_same_call_in_another_file(
    tmp_path: Path,
) -> None:
    source = "import os\ndef execute_workload() -> None:\n    os.execve('x', (), {})\n"
    write(tmp_path, REPLACEMENT_GATE, source)
    write(tmp_path, "src/kora/runner/other.py", source)

    violations = replacement_violations(tmp_path)

    assert violations == ["src/kora/runner/other.py:3: unapproved process replacement"]


def test_gate_rule_cannot_expand_the_pinned_ruff_configuration(tmp_path: Path) -> None:
    original = (REPOSITORY_ROOT / PYPROJECT).read_text(encoding="utf-8")
    allowance = '"src/kora/runner/gate.py" = ["S606"]'
    variations = {
        "path": replaced(original, allowance, '"src/kora/runner/*.py" = ["S606"]'),
        "extra_rule": replaced(original, allowance, allowance[:-1] + ', "S603"]'),
        "global": replaced(
            original, "[tool.ruff.lint]\n", '[tool.ruff.lint]\nignore = ["S606"]\n'
        ),
        "extend": original
        + '\n[tool.ruff.lint.extend-per-file-ignores]\n"*.py" = ["S606"]\n',
        "adverse_fixture_tid251_path": replaced(
            original,
            "[tool.ruff.lint.per-file-ignores]\n",
            '[tool.ruff.lint.per-file-ignores]\n"tests/other.py" = ["TID251"]\n',
        ),
    }
    for name, source in variations.items():
        copy_configuration(tmp_path / name)
        write(tmp_path / name, PYPROJECT, source)

    results = {name: configuration_violations(tmp_path / name) for name in variations}

    assert all(
        "pyproject.toml:1: [tool.ruff] differs from the recorded table" in findings
        for findings in results.values()
    ), results


REPLACEMENT_GATE: str = "src/kora/runner/gate.py"
REPLACEMENT_APIS: frozenset[str] = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "_execvpe",
    }
)


def replacement_reference(
    node: ast.AST,
    getters: set[str],
) -> TypeGuard[ast.Attribute | ast.ImportFrom | ast.Call | ast.Subscript]:
    if isinstance(node, ast.Attribute):
        return node.attr in REPLACEMENT_APIS
    if isinstance(node, ast.ImportFrom):
        return node.module == "os" and any(
            alias.name in REPLACEMENT_APIS or alias.name == "*" for alias in node.names
        )
    if isinstance(node, ast.Call):
        name = call_name(node)
        if name in getters:
            return (
                string_value(positional_or_keyword(node, 1, "name")) in REPLACEMENT_APIS
            )
        if name in {"get", "__getitem__", "__getattribute__", "attrgetter"}:
            return first_string_argument(node) in REPLACEMENT_APIS
    if isinstance(node, ast.Subscript):
        return string_value(node.slice) in REPLACEMENT_APIS
    return False


def binds_os(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "os" and isinstance(node.ctx, ast.Store)
    if isinstance(node, ast.arg):
        return node.arg == "os"
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name == "os"
    if isinstance(node, ast.Import):
        return any(alias.asname == "os" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return any((alias.asname or alias.name) == "os" for alias in node.names)
    return False


def getter_names(nodes: tuple[ast.AST, ...]) -> set[str]:
    names = {GETATTR}
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module == "builtins":
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == GETATTR
            )
    changed = True
    while changed:
        previous = len(names)
        for node in nodes:
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            value = node.value
            if (
                isinstance(value, ast.Name)
                and value.id in names
                or isinstance(value, ast.Attribute)
                and value.attr == GETATTR
            ):
                names.update(bound_names(node))
        changed = len(names) != previous
    return names


def allowed_replacement_reference(
    source: SourceFile,
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    unaliased_import: bool,
    rebound: bool,
) -> bool:
    if (
        source.relative != REPLACEMENT_GATE
        or not unaliased_import
        or rebound
        or not isinstance(node, ast.Attribute)
        or node.attr != "execve"
        or not isinstance(node.value, ast.Name)
        or node.value.id != "os"
    ):
        return False
    parent = parents.get(node)
    if not isinstance(parent, ast.Call) or parent.func is not node:
        return False
    current: ast.AST | None = parent
    while current is not None and not isinstance(
        current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ):
        current = parents.get(current)
    return (
        isinstance(current, ast.FunctionDef)
        and current.name == "execute_workload"
        and isinstance(parents.get(current), ast.Module)
        and not current.decorator_list
    )


def replacement_violations(root: Path) -> list[str]:
    violations: set[Violation] = set()
    allowed_count = 0
    if not (root / REPLACEMENT_GATE).is_file():
        violations.add(Violation(REPLACEMENT_GATE, 1, "fixed launch gate is missing"))
    for source in source_files(root):
        if source.language is not Language.PYTHON:
            continue
        tree = ast.parse(source.path.read_text(encoding="utf-8"))
        nodes = tuple(ast.walk(tree))
        parents = {
            child: parent for parent in nodes for child in ast.iter_child_nodes(parent)
        }
        unaliased = any(
            isinstance(node, ast.Import)
            and any(alias.name == "os" and alias.asname is None for alias in node.names)
            for node in tree.body
        )
        rebound = any(binds_os(node) for node in nodes)
        getters = getter_names(nodes)
        for node in nodes:
            if not replacement_reference(node, getters):
                continue
            if allowed_replacement_reference(source, node, parents, unaliased, rebound):
                allowed_count += 1
            else:
                violations.add(
                    Violation(
                        source.relative, node.lineno, "unapproved process replacement"
                    )
                )
    if allowed_count != 1:
        violations.add(
            Violation(REPLACEMENT_GATE, 1, "fixed os.execve call count is not one")
        )
    return describe(violations)


def test_replacement_guard_rejects_aliases_beside_the_approved_call(
    tmp_path: Path,
) -> None:
    approved = (
        "import os\ndef execute_workload() -> None:\n    os.execve('x', (), {})\n"
    )
    aliases = {
        "module_value": "native = os\nnative.execve('x', (), {})\n",
        "from_import": "from os import execvp as replace\nreplace('x', ())\n",
        "assignment": "replace = os.execve\nreplace('x', (), {})\n",
        "default": "def other(replace=os.execve) -> None:\n    replace('x', (), {})\n",
        "decorator": "@os.execve\ndef other() -> None:\n    pass\n",
        "getter_alias": "getter = getattr\ngetter(os, 'execve')('x', (), {})\n",
        "getter_import": "from builtins import getattr as getter\n"
        "getter(os, 'execve')('x', (), {})\n",
        "module_dictionary": "os.__dict__.get('execve')('x', (), {})\n",
        "attribute_getter": "import operator\n"
        "operator.attrgetter('execve')(os)('x', (), {})\n",
    }
    for name, extra in aliases.items():
        write(tmp_path / name, REPLACEMENT_GATE, approved + extra)

    results = {name: replacement_violations(tmp_path / name) for name in aliases}

    assert all(
        any("unapproved process replacement" in finding for finding in findings)
        for findings in results.values()
    ), results
