import ast
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

from tests.support.ast_modules import SourceModule, parse_module, source_modules

CLIENT_MODULE = "kora.client.transport"
CLIENT_CLASS = "KoraClient"
REQUEST_METHOD = "_request_json"
CLIENT_RECEIVER = "client"
PARAMETER = re.compile(r"\{[^}]*\}")
EXCLUDED_OPERATIONS: frozenset[tuple[str, str]] = frozenset({("GET", "/healthz")})


def normalized(path: str) -> str:
    return PARAMETER.sub("{}", path)


def schema_operations(paths: Mapping[str, object]) -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    for path, item in paths.items():
        assert isinstance(item, dict)
        methods = cast("dict[str, object]", item)
        for method in methods:
            operations.add((method.upper(), normalized(path)))
    return operations


def module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.target.id] = node.value.value
    return constants


def request_path(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return request_path(node.func.value, constants)
    return None


def method_requests(
    method: ast.AsyncFunctionDef, constants: dict[str, str]
) -> Iterator[tuple[str, str]]:
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == REQUEST_METHOD
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            path = request_path(node.args[1], constants)
            if path is not None:
                yield node.args[0].value, normalized(path)


def client_operations(source_root: Path) -> dict[str, set[tuple[str, str]]]:
    module = SourceModule(
        source_root.joinpath(*CLIENT_MODULE.split(".")).with_suffix(".py"),
        CLIENT_MODULE,
    )
    tree = parse_module(module)
    constants = module_constants(tree)
    operations: dict[str, set[tuple[str, str]]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == CLIENT_CLASS:
            for member in node.body:
                if isinstance(
                    member, ast.AsyncFunctionDef
                ) and not member.name.startswith("_"):
                    operations[member.name] = set(method_requests(member, constants))
    return operations


def is_client_receiver(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == CLIENT_RECEIVER) or (
        isinstance(node, ast.Attribute) and node.attr == CLIENT_RECEIVER
    )


def called_attributes(modules: list[SourceModule]) -> set[str]:
    names: set[str] = set()
    for module in modules:
        for node in ast.walk(parse_module(module)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and is_client_receiver(node.func.value)
            ):
                names.add(node.func.attr)
    return names


def surface_modules(source_root: Path, surface: str) -> list[SourceModule]:
    if surface == "cli":
        entry = source_root / "kora" / "__main__.py"
        return [
            *source_modules(source_root, "kora/cli", (".py",)),
            SourceModule(entry, "kora.__main__"),
        ]
    return source_modules(source_root, "kora/mcp", (".py",))


def uncovered_operations(paths: Mapping[str, object], source_root: Path) -> list[str]:
    operations = schema_operations(paths) - EXCLUDED_OPERATIONS
    clients = client_operations(source_root)
    uncovered: list[str] = []
    for surface in ("cli", "mcp"):
        called = called_attributes(surface_modules(source_root, surface))
        reached = {
            operation
            for name, requests in clients.items()
            if name in called
            for operation in requests
        }
        uncovered.extend(
            f"{surface}: {method} {path}"
            for method, path in sorted(operations - reached)
        )
    return uncovered
