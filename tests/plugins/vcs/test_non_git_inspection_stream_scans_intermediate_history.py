from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from tests.plugins.vcs.git_support import make_world

LEAKED_VALUE: str = "kora-intermediate-secret-77d2"


def test_non_git_inspection_stream_scans_intermediate_history(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    world.commit(checkout, {"notes.txt": f"{LEAKED_VALUE}\n"}, "intermediate")
    world.commit(checkout, {"notes.txt": "scrubbed\n"}, "scrubbed")
    secret = world.capture.scope.admit(LEAKED_VALUE)

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout, secrets=(secret,))

    assert raised.value.failure_class is ErrorClass.CREDENTIAL_LEAK
    assert raised.value.code == "secret_leak"
