from dataclasses import dataclass
from typing import Literal, TypeAlias

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.types import JsonObject, JsonValue


@dataclass(frozen=True)
class EncodedGraphValues:
    ir: str
    execution_contract: str
    value_encoding_version: Literal[1] = 1


@dataclass(frozen=True)
class DecodedGraphValues:
    ir: JsonObject
    execution_contract: JsonObject


@dataclass(frozen=True)
class LegacyGraphValues:
    ir: JsonValue
    execution_contract: JsonValue


StoredGraphValues: TypeAlias = DecodedGraphValues | LegacyGraphValues


def prepare_graph_values(
    ir: object, execution_contract: object, *, max_encoded_bytes: int
) -> EncodedGraphValues:
    budget = TextBudget(max_encoded_bytes)
    encoded = EncodedGraphValues(
        render_object(ir, budget), render_object(execution_contract, budget)
    )
    read_graph_values(
        encoded.value_encoding_version,
        encoded.ir,
        encoded.execution_contract,
        max_encoded_bytes=max_encoded_bytes,
    )
    return encoded


def read_graph_values(
    version: object,
    ir: JsonValue,
    execution_contract: JsonValue,
    *,
    max_encoded_bytes: int,
) -> StoredGraphValues:
    budget = TextBudget(max_encoded_bytes)
    if version is None:
        return LegacyGraphValues(ir, execution_contract)
    if type(version) is not int or version != 1:
        raise GraphValueRejected(GraphValueFailure.VERSION)
    if type(ir) is not str or type(execution_contract) is not str:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(len(ir) + len(execution_contract))
    if not ir.isascii() or not execution_contract.isascii():
        raise GraphValueRejected(GraphValueFailure.INVALID)
    decoded = DecodedGraphValues(
        ObjectReader(ir).object(), ObjectReader(execution_contract).object()
    )
    canonical_budget = TextBudget(max_encoded_bytes)
    if (
        render_object(decoded.ir, canonical_budget) != ir
        or render_object(decoded.execution_contract, canonical_budget)
        != execution_contract
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return decoded
