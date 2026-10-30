import json
import os
import sys

from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
from kora.observability import Level, LoggingSettings, configure_process
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.time import SystemClock
from tests.plugins.artifact_type.command_result_support import binding, payload
from tests.plugins.storage.sink_probe import fill


def run(sink: str) -> dict[str, object]:
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
    provider = CommandResultArtifactType({}, binding())
    value = payload()
    for _ in range(150):
        assert provider.validate(value, context) == []
        assert provider.summary(value, context) == value
        assert provider.digest_fields(value, context) == [""]
        assert provider.validate({"private": "private-sink-marker"}, context)
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
    sys.stdout.write(json.dumps(run(sys.argv[1])) + "\n")
