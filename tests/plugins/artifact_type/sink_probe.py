import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
from kora.ir.json import JsonValue
from kora.ir.structured_json import encode_snapshot
from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.artifact_type.agent_session import AgentSessionArtifactType
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.plugins.artifact_type.structured_output import StructuredOutputArtifactType
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.activity import NoActivity
from kora.protocols.artifact_type import ArtifactType
from kora.protocols.common import ProtocolContext
from kora.protocols.time import SystemClock
from tests.plugins.artifact_type import (
    agent_session_support,
    command_result_support,
    structured_output_support,
    support,
)
from tests.plugins.storage.sink_probe import fill

SINK_MARKER = "private-sink-marker"


@dataclass(frozen=True)
class SinkSubject:
    provider: ArtifactType
    value: JsonValue
    expected: JsonValue
    rejected: Callable[[], JsonValue]
    check_summary: Callable[["SinkSubject", ProtocolContext], None]


def private_field() -> JsonValue:
    return {"private": SINK_MARKER}


def structured_rejection() -> JsonValue:
    return structured_output_support.rejected(SINK_MARKER)


def equal_summary(subject: SinkSubject, context: ProtocolContext) -> None:
    assert subject.provider.summary(subject.value, context) == subject.expected


def snapshot_summary(subject: SinkSubject, context: ProtocolContext) -> None:
    summary = subject.provider.summary(subject.value, context)
    assert encode_snapshot(summary) == encode_snapshot(subject.expected)
    assert subject.provider.summary(None, context) is None


def vcs_ref() -> SinkSubject:
    provider = VcsRefArtifactType({}, support.binding())
    value = support.payload()
    return SinkSubject(provider, value, value, private_field, equal_summary)


def command_result() -> SinkSubject:
    provider = CommandResultArtifactType({}, command_result_support.binding())
    value = command_result_support.payload()
    return SinkSubject(provider, value, value, private_field, equal_summary)


def agent_session() -> SinkSubject:
    provider = AgentSessionArtifactType({}, agent_session_support.binding())
    value = agent_session_support.payload()
    expected = agent_session_support.expected_summary()
    return SinkSubject(provider, value, expected, private_field, equal_summary)


def structured_output() -> SinkSubject:
    provider = StructuredOutputArtifactType({}, structured_output_support.binding())
    value = structured_output_support.payload()
    return SinkSubject(provider, value, value, structured_rejection, snapshot_summary)


SUBJECTS: dict[str, Callable[[], SinkSubject]] = {
    "vcs_ref": vcs_ref,
    "command_result": command_result,
    "agent_session": agent_session,
    "structured_output": structured_output,
}


def run(name: str, sink: str) -> dict[str, object]:
    read_end, write_end = os.pipe()
    if sink == "blocked":
        fill(write_end)
    else:
        os.close(read_end)
    diagnostics = configure_process(
        CONFORMANCE_COMPONENT,
        LoggingSettings(level=Level.DEBUG, queue_records=16, queue_bytes=65536),
        "artifact-sink",
        utc_now,
        write_end,
    )
    context = ProtocolContext(
        namespace_id="artifact-sink",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(CONFORMANCE_COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=None,
        clock=SystemClock(),
        activity=NoActivity(),
    )
    subject = SUBJECTS[name]()
    for _ in range(150):
        assert subject.provider.validate(subject.value, context) == []
        subject.check_summary(subject, context)
        assert subject.provider.digest_fields(subject.value, context) == [""]
        assert subject.provider.validate(subject.rejected(), context)
    drained = diagnostics.shutdown(0.01 if sink == "blocked" else 1.0)
    state = diagnostics.snapshot()
    if drained:
        os.close(write_end)
    return {
        "completed": 150,
        "drained": drained,
        "sink_failed": state.sink_failed,
        "dropped": state.dropped_total(),
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(sys.argv[1], sys.argv[2])) + "\n")
