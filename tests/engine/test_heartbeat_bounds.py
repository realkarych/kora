import json

from kora.db.worker_heartbeats import HEARTBEAT_VALUE_BYTES
from tests.engine.heartbeat_bounds import (
    maximal_closure,
    maximal_counters,
    maximal_recovery,
    producer_bounds,
)


def test_closed_producer_ascii_bounds_include_every_persisted_field() -> None:
    counters = maximal_counters()
    producers = (
        maximal_closure(),
        maximal_recovery(),
        counters,
        counters.usage_coverage,
    )

    bounds = producer_bounds()
    actual = tuple(
        len(json.dumps(item.model_dump(mode="json"), ensure_ascii=True).encode("ascii"))
        for item in producers
    )

    assert bounds == (4499, 6363, 4267, 3279)
    assert all(
        size <= bound <= HEARTBEAT_VALUE_BYTES
        for size, bound in zip(actual, bounds, strict=True)
    )
