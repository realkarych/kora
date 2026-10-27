import pkgutil

import pytest

import kora.protocols
from tests.support.observability import run_python

MODULES: list[str] = sorted(
    f"{kora.protocols.__name__}.{module.name}"
    for module in pkgutil.iter_modules(kora.protocols.__path__)
)


@pytest.mark.parametrize("module", MODULES)
def test_protocol_modules_import_in_any_order(module: str) -> None:
    arguments = ["-I", "-c", f"import {module}"]

    completed = run_python(arguments, {})

    assert completed.code == 0, completed.stderr
    assert completed.stderr == b""
