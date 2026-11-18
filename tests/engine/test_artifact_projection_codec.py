import hashlib
import json
from dataclasses import replace
from typing import cast

import pytest

from kora.cel_eval.artifact_projection_codec import (
    MAX_RECORD_BYTES,
    ProjectionFailure,
    ProjectionRejected,
)
from kora.conformance.common import context_for
from kora.engine.artifact_projection import (
    check_artifact_projection,
    prepare_artifact_projection,
)
from kora.ir.json import JsonValue
from tests.engine.artifact_projection_cases import (
    LIMITS,
    PATH,
    metadata,
    projection_capture,
    projection_graph,
)
from tests.protocols.effective_artifact_cases import installed


def malformed(content: bytes, kind: str) -> bytes:
    if kind == "duplicate":
        return content.replace(b'"original":', b'"original":null,"origi\\u006eal":')
    if kind == "extra":
        return content[:-1] + b',"extra":null}'
    if kind == "missing":
        return content.replace(b',"original":null', b"")
    if kind == "reordered":
        return content.replace(
            b'"original":null,"converted":null', b'"converted":null,"original":null'
        )
    if kind == "unknown":
        return content.replace(b"projection.v1", b"projection.v2")
    if kind == "whitespace":
        return content + b"\n"
    if kind == "nonfinite":
        return content.replace(b'"original":null', b'"original":1e999')
    if kind == "integer_token":
        return content.replace(b'"original":null', b'"original":' + b"9" * 311)
    if kind == "float_token":
        return content.replace(b'"original":null', b'"original":0.' + b"0" * 32)
    if kind == "deep":
        return content.replace(
            b'"original":null', b'"original":' + b"[" * 35 + b"0" + b"]" * 35
        )
    if kind == "occurrences":
        return content.replace(
            b'"original":null', b'"original":[' + b"0," * 999 + b"0]"
        )
    if kind == "surrogate":
        return content.replace(b'"original":null', b'"original":"\\ud800"')
    if kind == "conversion":
        return content.replace(b'"converted":null', b'"converted":false')
    if kind == "escaped":
        return content.replace(b'"format"', b'"for\\u006dat"')
    if kind == "invalid_utf8":
        return content[:-1] + b"\xff}"
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "extra",
        "missing",
        "reordered",
        "unknown",
        "whitespace",
        "nonfinite",
        "integer_token",
        "float_token",
        "deep",
        "occurrences",
        "surrogate",
        "conversion",
        "escaped",
        "invalid_utf8",
    ],
)
def test_projection_checks_malformed_bytes_after_matching_external_digest(
    kind: str,
) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, None, installed(), context_for(capture), limits=LIMITS
        )
    content = malformed(prepared.encoded(), kind)
    owner = replace(
        metadata(prepared),
        content_digest=hashlib.sha256(content).hexdigest(),
        content_bytes=len(content),
    )

    with pytest.raises(ProjectionRejected) as refused:
        check_artifact_projection(checked, PATH, content, owner, limits=LIMITS)

    assert refused.value.reason in (
        ProjectionFailure.BINDING,
        ProjectionFailure.ENCODING,
        ProjectionFailure.VALUE,
    )
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


class NativeBytesSubclass(bytes):
    pass


@pytest.mark.parametrize(
    "kind", ["subclass", "text", "digest", "length", "bool_length", "oversized"]
)
def test_projection_checks_native_shape_and_complete_record_identity(kind: str) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, None, installed(), context_for(capture), limits=LIMITS
        )
    content: object = prepared.encoded()
    owner = metadata(prepared)
    if kind == "subclass":
        content = NativeBytesSubclass(prepared.encoded())
    elif kind == "text":
        content = prepared.encoded().decode()
    elif kind == "digest":
        owner = replace(owner, content_digest="f" * 64)
    elif kind == "length":
        owner = replace(owner, content_bytes=owner.content_bytes - 1)
    elif kind == "bool_length":
        owner = replace(owner, content_bytes=True)
    else:
        content = b" " * (MAX_RECORD_BYTES + 1)
        owner = replace(
            owner,
            content_bytes=len(content),
            content_digest=hashlib.sha256(content).hexdigest(),
        )

    with pytest.raises(ProjectionRejected) as refused:
        check_artifact_projection(checked, PATH, content, owner, limits=LIMITS)

    assert refused.value.reason is ProjectionFailure.BINDING


@pytest.mark.parametrize("kind", ["original_bytes", "converted_bytes", "occurrences"])
def test_projection_original_converted_and_record_limits_are_independent(
    kind: str,
) -> None:
    checked = projection_graph()
    value: JsonValue = "x" * 4094
    if kind == "original_bytes":
        value = "x" * 4095
    elif kind == "converted_bytes":
        value = {"a": cast(list[JsonValue], [1] * 250), "b": "x" * 3500}
    elif kind == "occurrences":
        value = cast(list[JsonValue], [0] * 256)

    with projection_capture() as capture:
        with pytest.raises(ProjectionRejected) as refused:
            prepare_artifact_projection(
                checked, PATH, value, installed(), context_for(capture), limits=LIMITS
            )

    assert refused.value.reason is ProjectionFailure.VALUE


def test_projection_admits_the_complete_maximum_record() -> None:
    checked = projection_graph()
    value = "x" * 4094

    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, value, installed(), context_for(capture), limits=LIMITS
        )
        restored = check_artifact_projection(
            checked, PATH, prepared.encoded(), metadata(prepared), limits=LIMITS
        )

    assert len(restored.encoded()) == MAX_RECORD_BYTES == 8515
    assert json.loads(restored.encoded())["original"] == value
