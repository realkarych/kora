from sqlalchemy import (
    BigInteger,
    Executable,
    LargeBinary,
    Text,
    and_,
    bindparam,
    case,
    cast,
    false,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.sqltypes import NullType

from kora.db.graph_stage_promotion_custody_v1 import MAX_PROMOTION_DOCUMENT_BYTES_V1
from kora.db.identifiers import is_identifier
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, SQLITE

MAX_PROMOTION_PAIR_BYTES: int = 2 * MAX_PROMOTION_DOCUMENT_BYTES_V1
SQLITE_RELATIONS = text(
    """
    (SELECT CAST(:stage_id AS TEXT) AS stage_id) AS selected
    LEFT JOIN main.graph_deployment_stage_promotion_intent AS intent
        INDEXED BY sqlite_autoindex_graph_deployment_stage_promotion_intent_1
        ON intent.stage_id COLLATE BINARY = selected.stage_id
    LEFT JOIN main.graph_deployment_stage_promotion_settlement AS settlement
        INDEXED BY sqlite_autoindex_graph_deployment_stage_promotion_settlement_1
        ON settlement.stage_id COLLATE BINARY = selected.stage_id
    """
)
POSTGRES_RELATIONS = text(
    """
    (SELECT CAST(:stage_id AS pg_catalog.text) AS stage_id) AS selected
    LEFT JOIN ONLY graph_deployment_stage_promotion_intent AS intent
        ON intent.stage_id OPERATOR(pg_catalog.=) selected.stage_id
    LEFT JOIN ONLY graph_deployment_stage_promotion_settlement AS settlement
        ON settlement.stage_id OPERATOR(pg_catalog.=) selected.stage_id
    """
)
INTENT_KEY: ColumnElement[str] = literal_column("intent.stage_id", Text())
SETTLEMENT_KEY: ColumnElement[str] = literal_column("settlement.stage_id", Text())
REQUEST: ColumnElement[bytes] = literal_column("intent.request_bytes", LargeBinary())
OBSERVATION: ColumnElement[bytes] = literal_column(
    "settlement.observation_bytes", LargeBinary()
)
_POSTGRES_EXPECTED_WIDTH = text("CAST(:expected_width AS pg_catalog.int8)")
_POSTGRES_EXPECTED_BYTES = text("CAST(:expected_bytes AS pg_catalog.bytea)")


class PromotionDocumentProjectionRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph stage promotion document projection rejected")


def _document_width(document: ColumnElement[bytes], sqlite: bool) -> ColumnElement[int]:
    if sqlite:
        return case(
            (
                func.typeof(document) == "blob",
                func.length(document, type_=BigInteger),
            )
        )
    return cast(func.pg_catalog.octet_length(document), BigInteger())


def _within_document_width(
    width: ColumnElement[int], sqlite: bool
) -> ColumnElement[bool]:
    limit = MAX_PROMOTION_DOCUMENT_BYTES_V1
    if sqlite:
        return width.between(1, limit)
    return and_(
        width.bool_op("OPERATOR(pg_catalog.>=)")(1),
        width.bool_op("OPERATOR(pg_catalog.<=)")(limit),
    )


def _projection(dialect: str, stage_id: str, max_document_bytes: int) -> Executable:
    sqlite = dialect == SQLITE
    relations = SQLITE_RELATIONS if sqlite else POSTGRES_RELATIONS
    intent_present = INTENT_KEY.is_not(None)
    settlement_present = SETTLEMENT_KEY.is_not(None)
    intent_width = _document_width(REQUEST, sqlite)
    settlement_width = _document_width(OBSERVATION, sqlite)
    optional_width = func.coalesce(settlement_width, 0, type_=BigInteger)
    if sqlite:
        within_allowance = intent_width + optional_width <= max_document_bytes
    else:
        pair_width = intent_width.op("OPERATOR(pg_catalog.+)")(optional_width)
        within_allowance = pair_width.bool_op("OPERATOR(pg_catalog.<=)")(
            max_document_bytes
        )
    admitted = and_(
        _within_document_width(intent_width, sqlite),
        or_(SETTLEMENT_KEY.is_(None), _within_document_width(settlement_width, sqlite)),
        within_allowance,
    )
    return select(
        intent_present.label("intent_present"),
        settlement_present.label("settlement_present"),
        intent_width.label("intent_width"),
        settlement_width.label("settlement_width"),
        case((admitted, REQUEST)).label("request_bytes"),
        case((admitted, OBSERVATION)).label("observation_bytes"),
    ).select_from(relations.bindparams(stage_id=stage_id))


def graph_stage_promotion_document_statement(
    dialect: str, stage_id: str, *, max_document_bytes: int
) -> Executable:
    if (
        type(dialect) is str
        and dialect in (SQLITE, POSTGRESQL)
        and type(stage_id) is str
        and is_identifier(stage_id)
        and type(max_document_bytes) is int
        and 0 <= max_document_bytes <= MAX_PROMOTION_PAIR_BYTES
    ):
        return _projection(dialect, stage_id, max_document_bytes)
    del dialect, stage_id, max_document_bytes
    raise_private_metadata_error(PromotionDocumentProjectionRejected())


def _bounded_expected_document(document: bytes | None) -> bool:
    return document is None or (
        type(document) is bytes and 0 < len(document) <= MAX_PROMOTION_DOCUMENT_BYTES_V1
    )


def _document_comparison(
    stored: ColumnElement[bytes], expected: bytes, sqlite: bool
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    width = _document_width(stored, sqlite)
    if sqlite:
        return width == len(expected), stored == expected
    expected_width = _POSTGRES_EXPECTED_WIDTH.bindparams(
        bindparam("expected_width", len(expected), type_=NullType(), unique=True)
    )
    expected_bytes = _POSTGRES_EXPECTED_BYTES.bindparams(
        bindparam("expected_bytes", expected, type_=NullType(), unique=True)
    )
    return (
        width.bool_op("OPERATOR(pg_catalog.=)")(expected_width),
        stored.bool_op("OPERATOR(pg_catalog.=)")(expected_bytes),
    )


def _comparison(
    dialect: str,
    stage_id: str,
    request_bytes: bytes | None,
    observation_bytes: bytes | None,
) -> Executable:
    sqlite = dialect == SQLITE
    relations = SQLITE_RELATIONS if sqlite else POSTGRES_RELATIONS
    if request_bytes is None:
        matches = and_(INTENT_KEY.is_(None), SETTLEMENT_KEY.is_(None))
    else:
        request_width, request_equal = _document_comparison(
            REQUEST, request_bytes, sqlite
        )
        if observation_bytes is None:
            admitted = and_(
                INTENT_KEY.is_not(None), request_width, SETTLEMENT_KEY.is_(None)
            )
            equal = request_equal
        else:
            observation_width, observation_equal = _document_comparison(
                OBSERVATION, observation_bytes, sqlite
            )
            admitted = and_(
                INTENT_KEY.is_not(None),
                request_width,
                SETTLEMENT_KEY.is_not(None),
                observation_width,
            )
            equal = and_(request_equal, observation_equal)
        matches = case((admitted, equal), else_=false())
    return select(matches.label("matches")).select_from(
        relations.bindparams(stage_id=stage_id)
    )


def graph_stage_promotion_document_comparison_statement(
    dialect: str,
    stage_id: str,
    *,
    request_bytes: bytes | None,
    observation_bytes: bytes | None,
) -> Executable:
    if (
        type(dialect) is str
        and dialect in (SQLITE, POSTGRESQL)
        and type(stage_id) is str
        and is_identifier(stage_id)
        and _bounded_expected_document(request_bytes)
        and _bounded_expected_document(observation_bytes)
        and (request_bytes is not None or observation_bytes is None)
    ):
        return _comparison(dialect, stage_id, request_bytes, observation_bytes)
    del dialect, stage_id, request_bytes, observation_bytes
    raise_private_metadata_error(PromotionDocumentProjectionRejected())
