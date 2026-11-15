from dataclasses import replace

from kora.api.graph_deployment_limits import READ
from kora.cel.cost import DATA_DEPTH
from kora.db.graph_resource_owner_reads import MAX_OWNER_ROWS
from kora.db.sqlite_graph_version_bounds import (
    SQLiteGraphVersionWidths,
    bound_sqlite_graph_version_record,
)
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.start_preparation import (
    StartPreparationLimits,
    start_preparation_reservation,
)
from kora.engine.start_records import MAX_PARAMS_BYTES

PAIR = READ.identity.max_encoded_bytes
PHYSICAL = READ.physical_input_bytes
METADATA = 4096
NATIVE = max(
    1024 * (PAIR + PHYSICAL + METADATA + 1),
    *(
        bound_sqlite_graph_version_record(
            SQLiteGraphVersionWidths(2 * PAIR, 2 * PAIR, 2 * PHYSICAL, 8192, 512),
            encoding="utf-16le",
            page_size=1 << shift,
        ).leaf_and_overflow_bytes
        for shift in range(9, 17)
    ),
)
OWNERS = 2 * (
    READ.resources.logical.ir_bytes
    + MAX_OWNER_ROWS * (PHYSICAL + 128 + len("expiring"))
)
CUT_SOURCE = READ.projection.section_bytes
CUT_SCRATCH = (
    16 * (DATA_DEPTH + 2) * (3 * DATA_DEPTH + 4) * (CUT_SOURCE + 1)
    + 64 * MAX_PARAMS_BYTES
)
_LIMITS = StartPreparationLimits(
    GraphSnapshotLimits(READ, NATIVE, OWNERS),
    32 * (MAX_PARAMS_BYTES + CUT_SOURCE + 1) ** 2,
    CUT_SCRATCH,
    READ.projection.cel.typed_work,
    1,
    1,
)
_RETAINED, _WORK = start_preparation_reservation(_LIMITS)
START_PREPARATION = replace(_LIMITS, retained=_RETAINED, work=_WORK)
