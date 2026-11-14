import json
from typing import Literal

import pytest
from pydantic import ValidationError

from kora.client import ApiError, KoraClient
from kora.models.errors import (
    ApiError as ErrorRecord,
)
from kora.models.errors import (
    ApiErrorCode,
    ApiErrorEnvelope,
    DispatcherEvidenceDetails,
    RuntimeEvidenceDetails,
)
from tests.client.support import CREDENTIAL, Reader, Writer, peer, read_request, reply


@pytest.mark.asyncio
@pytest.mark.parametrize("item", ["runtime", "dispatchers"])
@pytest.mark.parametrize("reason", ["evidence_unbound", "evidence_conflict"])
async def test_client_preserves_exact_heartbeat_evidence_refusal(
    item: Literal["runtime", "dispatchers"],
    reason: Literal["evidence_unbound", "evidence_conflict"],
) -> None:
    details = (
        RuntimeEvidenceDetails(item="runtime", reason=reason)
        if item == "runtime"
        else DispatcherEvidenceDetails(
            item="dispatchers",
            reason=reason,
            index=127,
            dispatcher_id="submitted-dispatcher",
            lease_epoch=2**63 - 1,
        )
    )
    code = (
        ApiErrorCode.VALIDATION
        if reason == "evidence_unbound"
        else ApiErrorCode.CONFLICT
    )
    envelope = ApiErrorEnvelope(
        error=ErrorRecord(
            code=code,
            message="Heartbeat evidence was refused.",
            retryable=False,
            details=details,
            request_id="heartbeat-refusal",
        )
    )
    body = envelope.model_dump_json().encode()
    status = (
        b"HTTP/1.1 422 Unprocessable Entity"
        if code is ApiErrorCode.VALIDATION
        else b"HTTP/1.1 409 Conflict"
    )

    async def handle(reader: Reader, writer: Writer) -> None:
        await read_request(reader)
        reply(writer, body, status)

    async with peer(handle) as origin, KoraClient(origin.url, CREDENTIAL) as client:
        with pytest.raises(ApiError) as refusal:
            await client.openapi()
    wrong_code = (
        ApiErrorCode.CONFLICT
        if code is ApiErrorCode.VALIDATION
        else ApiErrorCode.VALIDATION
    )
    with pytest.raises(ValidationError):
        ErrorRecord.model_validate({**envelope.error.model_dump(), "code": wrong_code})

    assert refusal.value.code == code
    assert refusal.value.body == json.loads(body)
    assert refusal.value.content == body
    assert envelope.error.details.model_dump() == details.model_dump()


@pytest.mark.parametrize(
    "change",
    [
        {"index": -1},
        {"index": 128},
        {"index": True},
        {"process": {}},
        {"lease_epoch": -1},
        {"dispatcher_id": ""},
    ],
)
def test_dispatcher_evidence_error_rejects_invalid_identity_and_extra_content(
    change: dict[str, object],
) -> None:
    value: dict[str, object] = {
        "reason": "evidence_unbound",
        "item": "dispatchers",
        "index": 0,
        "dispatcher_id": "dispatcher",
        "lease_epoch": 1,
    }

    with pytest.raises(ValidationError):
        DispatcherEvidenceDetails.model_validate({**value, **change})
