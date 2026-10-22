import json

import pytest

from kora.observability import LoggingSettings
from kora.observability.redaction import MASK
from tests.protocols.conftest import StartCapture
from tests.protocols.secret_support import ESCAPED, PLAIN, Holder

ENCODE: dict[str, object] = {"secret_transfer": True}


@pytest.mark.parametrize("value", [PLAIN, ESCAPED])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_secret_transfer_field_is_masked(
    start_capture: StartCapture, value: str, ascii_only: bool
) -> None:
    capture = start_capture(LoggingSettings())
    holder = Holder.model_validate_json(
        json.dumps({"credential": value}), context={"secret_scope": capture.scope}
    )
    encoded = holder.model_dump_json(context=ENCODE)
    body = json.dumps(value, ensure_ascii=ascii_only)
    embedded = json.dumps({"note": f"prefix {value} suffix"}, ensure_ascii=ascii_only)

    masked = [
        capture.diagnostics.registry.mask(text) for text in (encoded, body, embedded)
    ]

    for text in masked:
        assert MASK in text
        assert value not in text
        assert body[1:-1] not in text
