import json
from pathlib import Path

import pytest

from tests.support.observability import run_python

SOURCE: Path = Path(__file__).resolve().parents[2] / "src"

HEAVY: tuple[str, ...] = (
    "jsonschema",
    "referencing",
    "pydantic",
    "kora.protocols",
    "kora.observability",
    "structlog",
    "kora.plugins",
    "kora.testing",
)
FORBIDDEN_FOR_CASES: tuple[str, ...] = ("kora.plugins", "kora.testing")
BUNDLE_MODULES: tuple[str, ...] = (
    "runtime",
    "secrets",
    "policy",
    "harness",
    "vcs",
    "artifact_storage",
)
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


@pytest.mark.parametrize("protocol", BUNDLE_MODULES)
def test_bridge_loads_only_the_selected_bundle(protocol: str) -> None:
    source = (
        "import json, sys\n"
        "from kora.conformance.bridge import ConformanceSelection\n"
        "from kora.protocols.metadata import ProtocolName, ProviderMetadata\n"
        "def constructor():\n"
        "    raise AssertionError('constructor must not be called')\n"
        "ConformanceSelection(ProviderMetadata(ProtocolName("
        f"{protocol!r}), 'fixture', constructor, {{'type':'object'}}, '1'))\n"
        f"names = {{'kora.conformance.' + name for name in {BUNDLE_MODULES!r}}}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m in names "
        "or m.startswith(('kora.plugins', 'kora.testing')))))\n"
    )

    result = run_python(["-I", "-c", source], {})

    assert result.code == 0, result.stderr
    assert json.loads(result.stdout) == [f"kora.conformance.{protocol}"]


def test_bridge_public_types_keep_other_suites_unloaded() -> None:
    source = (
        "import json, sys\n"
        "from kora.conformance import ConformanceAdapter, ConstructorArguments, "
        "PreparedConformance, ProtocolExerciseBundle\n"
        f"names = {{'kora.conformance.' + name for name in {BUNDLE_MODULES!r}}}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m in names)))\n"
    )

    result = run_python(["-I", "-c", source], {})

    assert result.code == 0, result.stderr
    assert json.loads(result.stdout) == []


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


def test_fixed_lifetime_helper_imports_no_parent_lifetime_or_launch_models() -> None:
    parent_modules = (
        "kora.ir.json",
        "kora.protocols.identity",
        "kora.protocols.lifetime",
        "kora.runner.processes",
        "kora.runner.gate",
    )
    found = loaded(
        "kora.conformance.io_helper",
        parent_modules,
    )

    assert set(found).isdisjoint(parent_modules)


@pytest.mark.parametrize(
    "module",
    [
        "kora.conformance.common",
        "kora.conformance.secrets",
        "kora.conformance.artifact_storage",
        "kora.conformance.vcs",
        "kora.conformance.inputs",
    ],
)
def test_case_modules_import_no_fakes(module: str) -> None:
    found = loaded(module, FORBIDDEN_FOR_CASES)

    assert found == []


def test_conformance_requires_extra() -> None:
    source = (
        f"import sys\nsys.path.insert(0, {str(SOURCE)!r})\nimport kora.conformance\n"
    )

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


def test_lifetime_leaf_records_preserve_all_canonical_public_exports() -> None:
    source = (
        "from kora import conformance\n"
        "from kora.protocols import identity as protocol_identity, identity_records\n"
        "from kora.protocols import lifetime, lifetime_records\n"
        "from kora.runner import identity, processes\n"
        "for name in ('InvocationId', 'ResourceId', 'OperationId', 'Revision', "
        "'Generation', 'Uint64', 'Uint32', 'ResourceKind', 'ResourceState', "
        "'LifetimeReason', 'OriginalOutcome', 'LocalRuntimeBinding', "
        "'ProcessRegistration', 'LocalRuntimeRegistration', "
        "'TemporaryTreeRegistration', 'BorrowedServiceRegistration', "
        "'ResourceRegistration'):\n"
        "    assert getattr(lifetime, name) is getattr(lifetime_records, name)\n"
        "for name in ('ResourceKind', 'ResourceState', 'LifetimeReason', "
        "'OriginalOutcome', 'LocalRuntimeBinding', 'ProcessRegistration', "
        "'LocalRuntimeRegistration', 'TemporaryTreeRegistration', "
        "'BorrowedServiceRegistration'):\n"
        "    assert getattr(conformance, name) is getattr(lifetime_records, name)\n"
        "for name in ('Identifier', 'MAX_PID', 'MAX_COUNT', 'Pid', 'Count', "
        "'ProcessIdentity'):\n"
        "    assert getattr(protocol_identity, name) is "
        "getattr(identity_records, name)\n"
        "assert lifetime_records.ProcessIdentity is identity_records.ProcessIdentity\n"
        "assert processes.complete_identity is identity.complete_identity\n"
    )

    result = run_python(["-I", "-c", source], {})

    assert result.code == 0, result.stderr
    assert result.stdout == b""
