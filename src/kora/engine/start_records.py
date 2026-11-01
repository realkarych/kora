import hashlib
import math
from dataclasses import dataclass
from typing import Annotated, Literal, Never, TypeAlias, cast

from pydantic import AfterValidator, ConfigDict, Field, StrictInt, StrictStr

from kora.db.identifiers import is_identifier
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity import Sha256

MAX_PARAMS_BYTES: int = 4_194_304
MAX_PARAMS_DEPTH: int = 32
MAX_IDENTITY_BYTES: int = MAX_PARAMS_BYTES + 1024
MAX_RECEIPT_BYTES: int = 4096
MIN_INTEGER: int = -(2**63)
MAX_INTEGER: int = 2**63 - 1


class StartRecordRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("the manual start record is invalid")


def refuse() -> Never:
    raise StartRecordRejected()


def identifier(value: str) -> str:
    if not is_identifier(value):
        refuse()
    return value


StoredIdentifier: TypeAlias = Annotated[StrictStr, AfterValidator(identifier)]


class StartRecord(BoundaryModel):
    model_config = ConfigDict(strict=True, revalidate_instances="always")


class StartScope(StartRecord):
    namespace_id: StoredIdentifier
    graph_id: StoredIdentifier


class ManualStartIdentity(StartScope):
    request_digest: Sha256


class AcceptedStart(StartRecord):
    outcome: Literal["accepted"]
    run_id: StoredIdentifier
    graph_version_id: StoredIdentifier
    status: Literal["pending"]


class SkippedStart(StartRecord):
    outcome: Literal["skipped"]
    reason: Literal["overlap"]
    run_id: None


StartOutcome: TypeAlias = AcceptedStart | SkippedStart


class StartReceipt(ManualStartIdentity):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    kind: Literal["manual"]
    response: Annotated[StartOutcome, Field(discriminator="outcome")]


@dataclass
class JsonBudget:
    remaining: int
    max_depth: int

    def spend(self, size: int) -> None:
        if size > self.remaining:
            refuse()
        self.remaining -= size

    def string(self, value: str) -> None:
        self.spend(len(value) + 2)
        for char in value:
            point = ord(char)
            if 0xD800 <= point <= 0xDFFF:
                refuse()
            if char in '"\\\b\f\n\r\t':
                extra = 1
            elif point < 32:
                extra = 5
            else:
                extra = (
                    0
                    if point < 128
                    else 1
                    if point < 2048
                    else 2
                    if point < 65536
                    else 3
                )
            self.spend(extra)

    def visit(self, value: object, depth: int) -> None:
        if type(value) is str:
            self.string(value)
        elif value is None or type(value) is bool:
            self.spend(5 if value is False else 4)
        elif type(value) is int:
            if not MIN_INTEGER <= value <= MAX_INTEGER:
                refuse()
            self.spend(len(str(value)))
        elif type(value) is float:
            if not math.isfinite(value):
                refuse()
            self.spend(len(repr(value)))
        elif type(value) is dict:
            if depth > self.max_depth:
                refuse()
            mapping = cast(dict[object, object], value)
            self.spend(2 + max(0, len(mapping) - 1) + len(mapping))
            for key, child in mapping.items():
                if type(key) is not str:
                    refuse()
                self.string(key)
                self.visit(child, depth + 1)
        elif type(value) is list:
            if depth > self.max_depth:
                refuse()
            sequence = cast(list[object], value)
            self.spend(2 + max(0, len(sequence) - 1))
            for child in sequence:
                self.visit(child, depth + 1)
        else:
            refuse()


def bounded_object(value: object, max_bytes: int, max_depth: int) -> JsonObject:
    if type(value) is not dict:
        refuse()
    JsonBudget(max_bytes, max_depth).visit(cast(dict[object, object], value), 0)
    return cast(JsonObject, value)


def prepare_manual_start(
    namespace_id: str,
    graph_id: str,
    params: object,
    *,
    graph_version_id: str | None = None,
    allow_unenforced_secrets: bool = False,
) -> ManualStartIdentity:
    try:
        scope = StartScope(namespace_id=namespace_id, graph_id=graph_id)
        if type(allow_unenforced_secrets) is not bool:
            refuse()
        selector: JsonObject = {"kind": "latest"}
        if graph_version_id is not None:
            if type(graph_version_id) is not str:
                refuse()
            selector = {"kind": "version", "id": identifier(graph_version_id)}
        admitted = bounded_object(params, MAX_PARAMS_BYTES, MAX_PARAMS_DEPTH)
        envelope: JsonObject = {
            "format": "manual-start/1",
            "namespace_id": scope.namespace_id,
            "graph_id": scope.graph_id,
            "graph_version": selector,
            "params": admitted,
            "allow_unenforced_secrets": allow_unenforced_secrets,
        }
        encoded = canonical_bytes(envelope)
        if len(encoded) > MAX_IDENTITY_BYTES:
            refuse()
        return ManualStartIdentity(
            namespace_id=scope.namespace_id,
            graph_id=scope.graph_id,
            request_digest=hashlib.sha256(encoded).hexdigest(),
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise StartRecordRejected() from None


def decode_start_receipt(value: object) -> StartReceipt:
    try:
        return StartReceipt.model_validate(bounded_object(value, MAX_RECEIPT_BYTES, 2))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise StartRecordRejected() from None


def start_receipt_document(receipt: StartReceipt) -> dict[str, JsonValue]:
    try:
        document = bounded_object(receipt.model_dump(mode="json"), MAX_RECEIPT_BYTES, 2)
        decode_start_receipt(document)
        return document
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise StartRecordRejected() from None
