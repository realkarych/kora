import pytest

from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import (
    MAX_CLAIM_BYTES,
    ClaimPartition,
    ClaimWireRejected,
    claim_response,
    prepare_claim_part,
)


def at_limit(partition: ClaimPartition) -> JsonObject:
    return {partition.value: "a" * (partition.limit - len(partition.value) - 7)}


@pytest.mark.parametrize("partition", list(ClaimPartition))
def test_claim_partition_refuses_one_byte_over_its_own_budget(
    partition: ClaimPartition,
) -> None:
    value = at_limit(partition)
    text = value[partition.value]
    assert type(text) is str
    oversized = {partition.value: text + "b"}

    exact = prepare_claim_part(partition, value)
    with pytest.raises(ClaimWireRejected):
        prepare_claim_part(partition, oversized)

    assert len(exact.payload) == partition.limit


def test_claim_partition_join_counts_wrappers_without_borrowing() -> None:
    immutable, policy, system = (
        prepare_claim_part(partition, at_limit(partition))
        for partition in ClaimPartition
    )

    encoded = claim_response(immutable, policy, system)
    empty_policy = prepare_claim_part(ClaimPartition.POLICY, {})
    empty_system = prepare_claim_part(ClaimPartition.SYSTEM, {})
    smaller = claim_response(immutable, empty_policy, empty_system)
    value = at_limit(ClaimPartition.IMMUTABLE)
    text = value[ClaimPartition.IMMUTABLE.value]
    assert type(text) is str
    with pytest.raises(ClaimWireRejected):
        prepare_claim_part(ClaimPartition.IMMUTABLE, {"immutable": text + "b"})

    assert len(encoded) == MAX_CLAIM_BYTES
    assert ObjectReader(encoded.decode()).object()["claim"] == {
        **at_limit(ClaimPartition.IMMUTABLE),
        **at_limit(ClaimPartition.POLICY),
        **at_limit(ClaimPartition.SYSTEM),
    }
    assert len(smaller) < len(encoded)
    assert b'"immutable":' in smaller


def test_claim_partition_freezes_exact_values_before_response() -> None:
    nested: JsonObject = {"é\0": ["😀", "\ud800", 1 << 20_000, -0.0]}
    source: JsonObject = {"resolved_config": nested}
    original = prepare_claim_part(ClaimPartition.IMMUTABLE, source)
    policy = prepare_claim_part(ClaimPartition.POLICY, {"execution_policy": {}})
    system = prepare_claim_part(ClaimPartition.SYSTEM, {"attempt_id": "attempt"})

    nested.clear()
    source.clear()
    encoded = claim_response(original, policy, system)
    result = ObjectReader(encoded.decode()).object()["claim"]
    assert isinstance(result, dict)
    assert result["resolved_config"] == {"é\0": ["😀", "\ud800", 1 << 20_000, -0.0]}
    assert b'"attempt_id":"attempt"' in encoded
    assert "resolved_config" not in repr(original)


def test_claim_partition_join_refuses_duplicates_and_misplaced_parts() -> None:
    immutable = prepare_claim_part(ClaimPartition.IMMUTABLE, {"same": 1})
    policy = prepare_claim_part(ClaimPartition.POLICY, {"same": 2})
    distinct = prepare_claim_part(ClaimPartition.POLICY, {"policy": {}})
    system = prepare_claim_part(ClaimPartition.SYSTEM, {"attempt_id": "attempt"})

    with pytest.raises(ClaimWireRejected):
        claim_response(immutable, policy, system)
    with pytest.raises(ClaimWireRejected):
        claim_response(system, distinct, immutable)

    assert immutable.payload == b'{"same":1}'
    assert policy.payload == b'{"same":2}'


def test_claim_partition_join_refuses_equivalent_decoded_member_names() -> None:
    immutable = prepare_claim_part(ClaimPartition.IMMUTABLE, {"\ud83d\ude00": 1})
    policy = prepare_claim_part(ClaimPartition.POLICY, {"😀": 2})
    system = prepare_claim_part(ClaimPartition.SYSTEM, {})

    with pytest.raises(ClaimWireRejected):
        claim_response(immutable, policy, system)

    assert immutable.members == policy.members == frozenset({"😀"})
