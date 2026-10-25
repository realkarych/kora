import pytest

from tests.support.observability import run_python

MODULES: tuple[str, ...] = (
    "kora.protocols.deadlines",
    "kora.protocols.common",
    "kora.protocols.time",
    "kora.protocols.identity",
    "kora.protocols.evidence",
    "kora.protocols.report",
    "kora.protocols.history",
    "kora.protocols.secret_value",
    "kora.protocols.vcs",
    "kora.protocols.policy",
    "kora.protocols.harness",
    "kora.protocols.runtime",
    "kora.protocols.secrets",
)


@pytest.mark.parametrize("module", MODULES)
def test_protocol_modules_import_in_any_order(module: str) -> None:
    arguments = ["-I", "-c", f"import {module}"]

    completed = run_python(arguments, {})

    assert completed.code == 0, completed.stderr
    assert completed.stderr == b""
