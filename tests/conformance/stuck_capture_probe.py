import json
import sys

from kora.conformance.capture import (
    LEFTOVERS,
    Capture,
    CaptureLeftover,
    CaptureTimeout,
    Pipe,
    captured,
)
from kora.conformance.common import CONFORMANCE_COMPONENT, utc_now
from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    LoggingSettings,
    configure_process,
)
from kora.protocols.secret_value import SecretScope

FLOOD: EventSpec = CATALOG.register(
    EventSpec(
        "conformance.probe.flood",
        CONFORMANCE_COMPONENT,
        Level.INFO,
        "a record that fills the pipe",
        {"index": FieldKind.INTEGER},
    )
)


def outcome() -> dict[str, object]:
    pipe = Pipe.open()
    diagnostics = configure_process(
        CONFORMANCE_COMPONENT, LoggingSettings(), "stuck", utc_now, pipe.write_end
    )
    logger = diagnostics.logger(CONFORMANCE_COMPONENT)
    for index in range(4096):
        logger.emit(FLOOD, index=index)
    capture = Capture(diagnostics, SecretScope(diagnostics), pipe, 0.2)
    timed_out = False
    try:
        capture.release()
    except CaptureTimeout:
        timed_out = True
    refused = False
    try:
        with captured(CONFORMANCE_COMPONENT, LoggingSettings(), "next", utc_now):
            pass
    except CaptureLeftover:
        refused = True
    return {"timed_out": timed_out, "refused": refused, "held": len(LEFTOVERS.held)}


sys.stdout.write(json.dumps(outcome()))
