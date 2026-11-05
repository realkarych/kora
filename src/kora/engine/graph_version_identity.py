import hashlib
from dataclasses import dataclass
from typing import Never

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_values import (
    DecodedGraphValues,
    EncodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.db.reads import GraphVersionRow

DEPLOYMENT_START = b'{"ir":'
DEPLOYMENT_CONTRACT = b',"execution_contract":'
DEPLOYMENT_END = b"}"
HASH_CHUNK_BYTES = 4096


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
    __slots__ = ("_canonical_ir", "_values", "_pin")

    def __init__(
        self, canonical_ir: str, values: DecodedGraphValues, pin: GraphVersionPin
    ) -> None:
        self._canonical_ir: str = canonical_ir
        self._values: DecodedGraphValues = values
        self._pin: GraphVersionPin = pin

    @property
    def canonical_ir(self) -> str:
        return self._canonical_ir

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


def _checked_graph_version_identity(
    row: GraphVersionRow, pin: GraphVersionPin, limits: GraphVersionIdentityLimits
) -> CheckedGraphVersionIdentity:
    if (
        row.graph_id != pin.graph_id
        or row.id != pin.version_id
        or row.execution_contract_digest != pin.execution_contract_digest
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if type(row.value_encoding_version) is not int or row.value_encoding_version != 1:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    if type(row.ir) is not str or type(row.execution_contract) is not str:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    values = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=limits.max_encoded_bytes,
    )
    if not isinstance(values, DecodedGraphValues):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    stored = GraphVersionDigests(
        row.ir_digest, row.execution_contract_digest, row.deployment_digest
    )
    if (
        _canonical_digests(row.ir, row.execution_contract, limits.max_hash_bytes)
        != stored
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return CheckedGraphVersionIdentity(row.ir, values, pin)


def read_graph_version_identity(
    row: GraphVersionRow, pin: GraphVersionPin, *, limits: GraphVersionIdentityLimits
) -> CheckedGraphVersionIdentity:
    try:
        return _checked_graph_version_identity(row, pin, limits)
    except GraphValueRejected as rejection:
        reason = rejection.reason
    del row, pin
    raise_safe(GraphValueRejected(reason))
