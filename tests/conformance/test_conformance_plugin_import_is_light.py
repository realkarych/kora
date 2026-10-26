import json

import pytest

from tests.support.observability import run_python

HEAVY: tuple[str, ...] = (
    "pydantic",
    "kora.protocols",
    "kora.observability",
    "structlog",
    "kora.plugins",
    "kora.testing",
)
FORBIDDEN_FOR_CASES: tuple[str, ...] = ("kora.plugins", "kora.testing")
PROBE: str = (
    "import json, sys\n"
    "import {module}\n"
    "print(json.dumps(sorted(m for m in sys.modules if m.startswith({prefixes!r}))))\n"
)


def loaded(module: str, prefixes: tuple[str, ...]) -> list[str]:
    result = run_python(
        ["-I", "-c", PROBE.format(module=module, prefixes=prefixes)], {}
    )
    assert result.code == 0, result.stderr
    found: list[str] = json.loads(result.stdout)
    return found


def test_conformance_plugin_import_is_light() -> None:
    found = loaded("kora.conformance.plugin", HEAVY)

    assert found == []


def test_fixed_lifetime_helper_imports_no_graph_models() -> None:
    found = loaded(
        "kora.conformance.io_helper",
        ("kora.ir.config", "kora.ir.topology", "kora.ir.document", "kora.ir.export"),
    )

    assert found == []


def test_fixed_lifetime_helper_imports_no_provider_context_or_logging() -> None:
    found = loaded(
        "kora.conformance.io_helper",
        ("kora.protocols.common", "kora.observability", "structlog", "rich"),
    )

    assert found == []


def test_fixed_lifetime_helper_imports_no_cel_checker() -> None:
    found = loaded("kora.conformance.io_helper", ("kora.cel",))

    assert found == []


@pytest.mark.parametrize(
    "module",
    [
        "kora.conformance.common",
        "kora.conformance.secrets",
        "kora.conformance.artifact_storage",
        "kora.conformance.vcs",
    ],
)
def test_case_modules_import_no_fakes(module: str) -> None:
    found = loaded(module, FORBIDDEN_FOR_CASES)

    assert found == []


def test_conformance_requires_extra() -> None:
    source = "import sys\nsys.path.insert(0, 'src')\nimport kora.conformance\n"

    result = run_python(["-I", "-S", "-c", source], {})

    assert result.code != 0
    assert b"ConformanceExtraMissing" in result.stderr
    assert b"kora[conformance]" in result.stderr


def test_lifetime_public_records_share_protocol_identity_without_runtime_imports() -> (
    None
):
    source = (
        "import json, sys\n"
        "from kora.conformance import LocalRuntimeBinding, LocalRuntimeRegistration\n"
        "from kora.protocols import lifetime\n"
        "assert LocalRuntimeBinding is lifetime.LocalRuntimeBinding\n"
        "assert LocalRuntimeRegistration is lifetime.LocalRuntimeRegistration\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith("
        "('kora.plugins', 'kora.testing', 'kora.conformance.lifetime', "
        "'kora.conformance.transport')))))\n"
    )

    result = run_python(["-I", "-c", source], {})

    assert result.code == 0, result.stderr
    assert json.loads(result.stdout) == []
