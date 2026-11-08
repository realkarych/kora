from dataclasses import dataclass

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.types import DIGEST_LENGTH, IDENTIFIER_LENGTH, TIMESTAMP_TEXT_LENGTH

ENCODINGS = ("utf-8", "utf-16le", "utf-16be")
RECORD_COLUMNS = 17
MAX_VARINT_BYTES = 9
FIXED_INTEGER_BYTES = 16
FIXED_ASCII_CHARACTERS = (
    2 * IDENTIFIER_LENGTH
    + 4 * DIGEST_LENGTH
    + 3 * TIMESTAMP_TEXT_LENGTH
    + len("retired")
)
MAX_RECORD_BYTES = (1 << 31) - 3
MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 65_536
MAX_RESERVED_BYTES = 255
MIN_USABLE_BYTES = 480
TABLE_LEAF_PAYLOAD_MARGIN = 35
OVERFLOW_POINTER_BYTES = 4


@dataclass(frozen=True, slots=True)
class SQLiteGraphVersionWidths:
    ir_bytes: int
    execution_contract_bytes: int
    resources_bytes: int
    resolved_digests_bytes: int
    deployed_by_bytes: int


@dataclass(frozen=True, slots=True)
class SQLiteGraphVersionFootprint:
    record_payload_bytes: int
    leaf_and_overflow_bytes: int


def bound_sqlite_graph_version_record(
    widths: SQLiteGraphVersionWidths, *, encoding: str, page_size: int
) -> SQLiteGraphVersionFootprint:
    if (
        type(widths) is not SQLiteGraphVersionWidths
        or type(encoding) is not str
        or encoding not in ENCODINGS
        or type(page_size) is not int
        or not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE
        or page_size & (page_size - 1)
    ):
        del widths, encoding, page_size
        raise GraphValueRejected(GraphValueFailure.INVALID)
    encoding_width = 1 if encoding == "utf-8" else 2
    values = (
        widths.ir_bytes,
        widths.execution_contract_bytes,
        widths.resources_bytes,
        widths.resolved_digests_bytes,
        widths.deployed_by_bytes,
    )
    if any(
        type(value) is not int or value < minimum
        for value, minimum in zip(values, (1, 1, 1, 1, 0), strict=True)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if any(value > MAX_RECORD_BYTES for value in values):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    if any(value % encoding_width for value in values):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    header_bound = (RECORD_COLUMNS + 1) * MAX_VARINT_BYTES
    payload = (
        sum(values)
        + header_bound
        + FIXED_INTEGER_BYTES
        + FIXED_ASCII_CHARACTERS * encoding_width
    )
    if payload > MAX_RECORD_BYTES:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    usable = max(MIN_USABLE_BYTES, page_size - MAX_RESERVED_BYTES)
    overflow_capacity = usable - OVERFLOW_POINTER_BYTES
    overflow = (
        0
        if payload <= usable - TABLE_LEAF_PAYLOAD_MARGIN
        else (payload + overflow_capacity - 1) // overflow_capacity
    )
    return SQLiteGraphVersionFootprint(payload, (1 + overflow) * page_size)
