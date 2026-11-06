import hashlib
from dataclasses import dataclass
from typing import Never

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    decoded_string,
)
from kora.db.graph_values import (
    DecodedGraphValues,
    EncodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.graph_version_reads import (
    DOUBLE_WIDTH,
    AcquiredGraphVersion,
    transport_limit,
)
from kora.db.reads import GraphVersionRow
from kora.db.types import DIGEST_LENGTH

DEPLOYMENT_START = b'{"ir":'
DEPLOYMENT_CONTRACT = b',"execution_contract":'
DEPLOYMENT_END = b"}"
HASH_CHUNK_BYTES = 4096
JSON_WHITESPACE = " \t\n\r"
HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class GraphVersionIdentityLimits:
    max_encoded_bytes: int
    max_hash_bytes: int

    def __post_init__(self) -> None:
        if any(
            type(limit) is not int or limit <= 0
            for limit in (self.max_encoded_bytes, self.max_hash_bytes)
        ):
            raise GraphValueRejected(GraphValueFailure.BUDGET)


@dataclass(frozen=True, slots=True)
class GraphVersionDigests:
    ir_digest: str
    execution_contract_digest: str
    deployment_digest: str


@dataclass(frozen=True, slots=True)
class GraphVersionPin:
    graph_id: str
    version_id: str
    execution_contract_digest: str


class PreparedGraphVersionIdentity:
    __slots__ = ("_values", "_digests")

    def __init__(
        self, values: EncodedGraphValues, digests: GraphVersionDigests
    ) -> None:
        self._values: EncodedGraphValues = values
        self._digests: GraphVersionDigests = digests

    @property
    def values(self) -> EncodedGraphValues:
        return self._values

    @property
    def digests(self) -> GraphVersionDigests:
        return self._digests

    def __getstate__(self) -> Never:
        raise TypeError("graph version identity requires explicit access")


class CheckedGraphVersionIdentity:
    __slots__ = ("_canonical_ir", "_canonical_contract", "_values", "_pin")

    def __init__(
        self,
        canonical_ir: str,
        canonical_contract: str,
        values: DecodedGraphValues,
        pin: GraphVersionPin,
    ) -> None:
        self._canonical_ir: str = canonical_ir
        self._canonical_contract: str = canonical_contract
        self._values: DecodedGraphValues = values
        self._pin: GraphVersionPin = pin

    @property
    def canonical_ir(self) -> str:
        return self._canonical_ir

    @property
    def canonical_contract(self) -> str:
        return self._canonical_contract

    @property
    def values(self) -> DecodedGraphValues:
        return self._values

    @property
    def pin(self) -> GraphVersionPin:
        return self._pin

    def __getstate__(self) -> Never:
        raise TypeError("graph version identity requires explicit access")


def _canonical_digests(
    ir: str, contract: str, max_hash_bytes: int
) -> GraphVersionDigests:
    required = (
        2 * (len(ir) + len(contract))
        + len(DEPLOYMENT_START)
        + len(DEPLOYMENT_CONTRACT)
        + len(DEPLOYMENT_END)
    )
    if required > max_hash_bytes:
        raise GraphValueRejected(GraphValueFailure.BUDGET)

    deployment = hashlib.sha256(DEPLOYMENT_START)

    def leaf_digest(text: str) -> str:
        leaf = hashlib.sha256()
        for start in range(0, len(text), HASH_CHUNK_BYTES):
            chunk = text[start : start + HASH_CHUNK_BYTES].encode("ascii")
            leaf.update(chunk)
            deployment.update(chunk)
        return leaf.hexdigest()

    ir_digest = leaf_digest(ir)
    deployment.update(DEPLOYMENT_CONTRACT)
    contract_digest = leaf_digest(contract)
    deployment.update(DEPLOYMENT_END)
    return GraphVersionDigests(ir_digest, contract_digest, deployment.hexdigest())


def prepare_graph_version_identity(
    ir: object, execution_contract: object, *, limits: GraphVersionIdentityLimits
) -> PreparedGraphVersionIdentity:
    pair: EncodedGraphValues | None = None
    try:
        pair = prepare_graph_values(
            ir, execution_contract, max_encoded_bytes=limits.max_encoded_bytes
        )
        digests = _canonical_digests(
            pair.ir, pair.execution_contract, limits.max_hash_bytes
        )
        return PreparedGraphVersionIdentity(pair, digests)
    except GraphValueRejected as rejection:
        reason = rejection.reason
    del ir, execution_contract, pair
    raise_safe(GraphValueRejected(reason))


def _transport_string(value: bytes, encoding: str) -> str:
    text = value.decode(encoding).strip(JSON_WHITESPACE)
    inner, end = decoded_string(text, 0)
    if end != len(text) or not inner.isascii():
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return inner


def _transport_digest(value: bytes, encoding: str) -> str:
    digest = value.decode(encoding)
    if len(digest) != DIGEST_LENGTH or any(point not in HEX_DIGITS for point in digest):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return digest


def _acquired_values(
    row: AcquiredGraphVersion, pin: GraphVersionPin, limits: GraphVersionIdentityLimits
) -> tuple[EncodedGraphValues, GraphVersionDigests]:
    width = 2 if row.encoding in DOUBLE_WIDTH else 1
    first, second = row.payloads
    if any(len(value) != width * DIGEST_LENGTH for value in row.digests):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if len(first) + len(second) > transport_limit(limits.max_encoded_bytes, width):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    ir_digest, contract_digest, deployment_digest = row.digests
    try:
        stored = GraphVersionDigests(
            _transport_digest(ir_digest, row.encoding),
            _transport_digest(contract_digest, row.encoding),
            _transport_digest(deployment_digest, row.encoding),
        )
        if stored.execution_contract_digest != pin.execution_contract_digest:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        return EncodedGraphValues(
            _transport_string(first, row.encoding),
            _transport_string(second, row.encoding),
        ), stored
    except UnicodeDecodeError:
        raise GraphValueRejected(GraphValueFailure.INVALID) from None


def _row_values(
    row: GraphVersionRow, pin: GraphVersionPin
) -> tuple[EncodedGraphValues, GraphVersionDigests]:
    if row.execution_contract_digest != pin.execution_contract_digest:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if type(row.value_encoding_version) is not int or row.value_encoding_version != 1:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    if type(row.ir) is not str or type(row.execution_contract) is not str:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return EncodedGraphValues(row.ir, row.execution_contract), GraphVersionDigests(
        row.ir_digest, row.execution_contract_digest, row.deployment_digest
    )


def _checked_graph_version_identity(
    row: GraphVersionRow | AcquiredGraphVersion,
    pin: GraphVersionPin,
    limits: GraphVersionIdentityLimits,
) -> CheckedGraphVersionIdentity:
    if row.graph_id != pin.graph_id or row.id != pin.version_id:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    pair, stored = (
        _acquired_values(row, pin, limits)
        if isinstance(row, AcquiredGraphVersion)
        else _row_values(row, pin)
    )
    values = read_graph_values(
        pair.value_encoding_version,
        pair.ir,
        pair.execution_contract,
        max_encoded_bytes=limits.max_encoded_bytes,
    )
    if not isinstance(values, DecodedGraphValues):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    if (
        _canonical_digests(pair.ir, pair.execution_contract, limits.max_hash_bytes)
        != stored
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return CheckedGraphVersionIdentity(pair.ir, pair.execution_contract, values, pin)


def read_graph_version_identity(
    row: GraphVersionRow | AcquiredGraphVersion,
    pin: GraphVersionPin,
    *,
    limits: GraphVersionIdentityLimits,
) -> CheckedGraphVersionIdentity:
    try:
        return _checked_graph_version_identity(row, pin, limits)
    except GraphValueRejected as rejection:
        reason = rejection.reason
    del row, pin
    raise_safe(GraphValueRejected(reason))
