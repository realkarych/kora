import json
from collections.abc import Generator
from contextlib import contextmanager

from kora.conformance.capture import Capture, CaptureOverflow, CaptureTimeout
from kora.protocols.errors import KoraProtocolError
from tests.measurements.test_local_runtime_cost import last_retirement_observation


@contextmanager
def retirement_failure_note(capture: Capture) -> Generator[None]:
    try:
        yield
    except KoraProtocolError as error:
        if error.code != "runner_stop_unconfirmed":
            raise
        try:
            records = capture.finish()
        except (CaptureTimeout, CaptureOverflow, json.JSONDecodeError) as failure:
            error.add_note("last observed runtime.local.retire: null (unverified)")
            raise error from failure
        error.add_note(
            json.dumps(
                {
                    "last_observed_runtime.local.retire": last_retirement_observation(
                        records
                    )
                },
                sort_keys=True,
            )
        )
        raise
