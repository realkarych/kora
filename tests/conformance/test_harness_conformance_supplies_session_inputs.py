import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.conformance.harness import HarnessCase, HarnessHost
from kora.ir.json import JsonObject
from kora.protocols.harness import SessionInputs, SessionSpec
from kora.protocols.vcs import WorkspaceTarget
from tests.conformance.harnesses import BrokenHarness
from tests.plugins.harness.fake.fake_support import DENIED_TOOL, FakeExercise

__all__ = ["kora_capture"]


class RecordingHarness(BrokenHarness):
    received: SessionInputs | None = None

    def build_session_spec(
        self,
        config: JsonObject,
        workspace: WorkspaceTarget,
        feedback: None,
        *,
        inputs: SessionInputs,
    ) -> SessionSpec:
        self.received = inputs
        return self.inner.build_session_spec(config, workspace, feedback, inputs=inputs)


@pytest.mark.parametrize("max_events", [3, None])
def test_harness_conformance_supplies_session_inputs(
    kora_capture: Capture, max_events: int | None
) -> None:
    adapter = RecordingHarness(
        FakeExercise().adapter(HarnessCase.MAX_EVENTS, context_for(kora_capture))
    )

    HarnessHost().run(adapter, DENIED_TOOL, max_events)

    assert adapter.received is not None
    if max_events is None:
        assert adapter.received.limits.model_dump(mode="json") == {}
    else:
        assert adapter.received.limits.max_events == max_events
