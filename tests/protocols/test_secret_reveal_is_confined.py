import ast
from pathlib import Path

ROOT: Path = Path(__file__).parents[2]
SOURCE: Path = ROOT / "src"
REVEAL: frozenset[str] = frozenset({"reveal"})
PRIVATE: frozenset[str] = frozenset({"_kora_secret", "_SecretValue__kora_secret"})
REVEAL_ALLOWED: tuple[str, ...] = (
    "src/kora/runner/",
    "src/kora/executor/",
    "src/kora/plugins/runtime/",
    "src/kora/plugins/harness/",
    "src/kora/plugins/vcs/",
    "src/kora/plugins/secrets/",
    "src/kora/protocols/secret_value.py",
    "src/kora/conformance/secrets.py",
)
PRIVATE_ALLOWED: tuple[str, ...] = ("src/kora/protocols/secret_value.py",)


def names_one_of(node: ast.expr, names: frozenset[str]) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in names
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(part in names for part in node.value.split("."))
    return False


def violations(tree: ast.AST, names: frozenset[str]) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.expr) and names_one_of(node, names)
    ]


def check(text: str, names: frozenset[str]) -> list[int]:
    return violations(ast.parse(text), names)


def offenders(names: frozenset[str], allowed: tuple[str, ...]) -> dict[str, list[int]]:
    return {
        relative: lines
        for path in SOURCE.rglob("*.py")
        if not (relative := str(path.relative_to(ROOT))).startswith(allowed)
        and (lines := check(path.read_text(), names))
    }


def test_secret_reveal_is_confined() -> None:
    reveal_offenders = offenders(REVEAL, REVEAL_ALLOWED)
    private_offenders = offenders(PRIVATE, PRIVATE_ALLOWED)

    assert reveal_offenders == {}
    assert private_offenders == {}


def test_the_check_flags_every_access_form() -> None:
    reveal_sources = [
        "value.reveal()",
        'getattr(value, "reveal")',
        'operator.methodcaller("reveal")(value)',
        'value.__getattribute__("reveal")',
    ]
    private_sources = [
        "value._kora_secret",
        "value._SecretValue__kora_secret",
        'operator.attrgetter("_kora_secret")(value)',
        'operator.attrgetter("holder._kora_secret")(value)',
        'inspect.getattr_static(value, "_kora_secret")',
        'type(value).__dict__["_kora_secret"]',
    ]

    flagged = [check(source, REVEAL) for source in reveal_sources] + [
        check(source, PRIVATE) for source in private_sources
    ]

    assert all(flagged)


def transfer_carriers() -> frozenset[str]:
    carriers: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef) and "SecretTransfer" in ast.unparse(node):
                carriers.add(node.name)
    return frozenset(carriers)


def constructions(tree: ast.AST, carriers: frozenset[str]) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "model_construct"
        and isinstance(node.value, ast.Name)
        and node.value.id in carriers
    ]


def test_no_transfer_carrier_skips_validation() -> None:
    carriers = transfer_carriers()

    found = {
        str(path.relative_to(ROOT)): lines
        for path in SOURCE.rglob("*.py")
        if (lines := constructions(ast.parse(path.read_text()), carriers))
    }

    assert {
        "IssuedCredential",
        "PublicationAuthorization",
        "TransferHolder",
    } <= carriers
    assert found == {}


def test_the_construction_check_flags_a_carrier() -> None:
    source = "IssuedCredential.model_construct(value=secret)"

    lines = constructions(ast.parse(source), frozenset({"IssuedCredential"}))

    assert lines == [1]


def updating_copies(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_copy"
        and any(keyword.arg == "update" for keyword in node.keywords)
    ]


def test_no_source_copies_a_model_with_updates() -> None:
    found = {
        str(path.relative_to(ROOT)): lines
        for path in SOURCE.rglob("*.py")
        if (lines := updating_copies(ast.parse(path.read_text())))
    }

    flagged = updating_copies(ast.parse("value.model_copy(update={'a': 1})"))

    assert found == {}
    assert flagged == [1]
