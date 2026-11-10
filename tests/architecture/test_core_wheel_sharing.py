import ast
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
BUILD = "build_core_wheel"


def functions(relative: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse((REPOSITORY / relative).read_text())
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def fixture_scope(function: ast.FunctionDef) -> str | None:
    for decorator in function.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func) == "pytest.fixture"
            and not any(keyword.arg == "params" for keyword in decorator.keywords)
        ):
            for keyword in decorator.keywords:
                if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return None


def call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def called_names(function: ast.FunctionDef) -> frozenset[str]:
    return frozenset(
        name
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and (name := call_name(node)) is not None
    )


def build_sites() -> list[tuple[str, str]]:
    sites: list[tuple[str, str]] = []
    for path in sorted((REPOSITORY / "tests").rglob("*.py")):
        source = path.read_text()
        if BUILD not in source:
            continue
        tree = ast.parse(source)
        owners = {
            id(node): function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
        }
        sites.extend(
            (path.relative_to(REPOSITORY).as_posix(), owners.get(id(node), "<module>"))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and call_name(node) == BUILD
        )
    return sites


def test_core_wheel_is_built_once_per_session_and_installed_per_module() -> None:
    root = functions("tests/conftest.py")
    wheelhouse = functions("tests/cli/conftest.py")["provider_tooling_wheelhouse"]
    installed = functions("tests/support/configured_conformance.py")[
        "installed_conformance"
    ]

    sites = build_sites()

    assert sites == [("tests/conftest.py", "core_wheel")]
    assert fixture_scope(root["core_wheel"]) == "session"
    assert fixture_scope(wheelhouse) == "session"
    assert fixture_scope(installed) == "module"
    for consumer in (wheelhouse, installed):
        assert "core_wheel" in {argument.arg for argument in consumer.args.args}
        assert "copy_core_wheel" in called_names(consumer)
    assert "install_tooling" in called_names(installed)


def test_core_wheel_uses_the_private_archive_and_tooling_remains_direct() -> None:
    tooling = functions("tests/support/provider_tooling.py")
    fixture = functions("tests/conftest.py")["core_wheel"]
    core = tooling["build_core_wheel"]

    builds = [
        tuple(ast.unparse(argument) for argument in node.args)
        for node in ast.walk(core)
        if isinstance(node, ast.Call) and call_name(node) == "run_core_wheel_build"
    ]
    common = next(
        ast.unparse(node.value)
        for node in ast.walk(core)
        if isinstance(node, ast.Assign)
        and any(ast.unparse(target) == "arguments" for target in node.targets)
    )
    direct = next(
        ast.unparse(node.args[0])
        for node in ast.walk(tooling["build_wheel"])
        if isinstance(node, ast.Call) and call_name(node) == "run_setup"
    )

    shared_inputs = ("output", "variables", "deadline", "stop")
    assert builds == [
        ("[*arguments, '--sdist', str(REPOSITORY)]", *shared_inputs),
        ("[*arguments, '--wheel', str(archive)]", *shared_inputs),
    ]
    build_prefix = (
        "['uv', 'build', '--offline', '--no-build-isolation', "
        "'--python', sys.executable"
    )
    assert common == build_prefix + ", '--out-dir', str(output)]"
    assert (
        direct == build_prefix + ", '--wheel', '--out-dir', str(output), str(source)]"
    )
    assert "core_wheel_workspace" in called_names(fixture)
    assert "build_wheel" not in called_names(core)
    assert "build_wheel" in called_names(tooling["build_fixture_wheel"])
