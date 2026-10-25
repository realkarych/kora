import json
from collections.abc import Callable, Mapping

import pytest
from pydantic import ValidationError

from kora.ir.vcs_ref_json import MAX_JSON_BYTES, canonical_bytes
from kora.protocols.evidence import (
    MAX_ENVELOPE_BYTES,
    ClosureEvidence,
    DispatcherClosure,
)
from kora.protocols.history import (
    MAX_OBSERVATION_METADATA_BYTES,
    MAX_PAGE_ROWS,
    AcknowledgeRead,
    ClosurePosition,
    ClosureSnapshot,
    GrantHistoryPage,
    GrantHistoryPosition,
    GrantHistoryProgress,
    GrantHistorySnapshot,
    HistoryEnvelopeExceeded,
    RecoveryEvidence,
    RecoveryPage,
    RecoveryProgress,
    grant_page_size,
    observation_metadata_size,
    read_grant_history_page,
    read_recovery_read,
    record_bytes,
    recovery_page_size,
)
from kora.protocols.identity import GrantBinding
from tests.protocols import runtime_documents as documents
from tests.protocols.runtime_documents import (
    WIDEST,
    WIDEST_SCOPE,
    widest_closure,
    widest_grant,
)

POSITION_LIMIT: int = 16_384
RECORD_LIMIT: int = 16_384


def widest_snapshot() -> dict[str, object]:
    return {
        "version": 1,
        "namespace_id": WIDEST,
        "attempt_id": WIDEST,
        "execution_domain_id": WIDEST,
        "upper_epoch": 2**63 - 1,
        "count": 2**63 - 1,
        "digest": documents.SHA,
    }


def encoded_size(model: GrantBinding | DispatcherClosure) -> int:
    return len(record_bytes(model))


def history(count: int) -> list[dict[str, object]]:
    return [documents.grant(epoch) for epoch in range(1, count + 1)]


def json_size(document: Mapping[str, object]) -> int:
    return len(canonical_bytes(json.loads(json.dumps(document))))


def test_worst_case_records_fit_their_allowances() -> None:
    grant = GrantBinding.model_validate(widest_grant(1))
    closure = DispatcherClosure.model_validate(widest_closure(1))

    grant_bytes = encoded_size(grant)
    closure_bytes = encoded_size(closure)
    page = GrantHistoryPage.model_validate(
        documents.grant_page([widest_grant(1)], 0, 1, WIDEST_SCOPE)
    )

    assert grant_bytes < 4096
    assert closure_bytes < 5120
    assert max(grant_bytes, closure_bytes) < RECORD_LIMIT
    assert page.complete is True


def test_worst_case_selectors_fit_their_limits() -> None:
    snapshot = widest_snapshot()
    position = {
        "after_epoch": 2**63 - 1,
        "received_count": 2**63 - 1,
        "prefix_digest": documents.SHA,
    }
    closure_snapshot = {
        **{key: value for key, value in snapshot.items() if key != "upper_epoch"},
        "generation": 2**63 - 1,
        "grant_snapshot": snapshot,
    }

    parsed = [
        GrantHistorySnapshot.model_validate(snapshot),
        GrantHistoryPosition.model_validate(position),
        ClosureSnapshot.model_validate(closure_snapshot),
    ]
    sizes = [json_size(item) for item in (snapshot, position, closure_snapshot)]

    assert len(parsed) == 3
    assert max(sizes) < POSITION_LIMIT


def test_worst_case_acknowledge_fits_the_message_limit() -> None:
    closures = documents.dispatcher_closures(MAX_PAGE_ROWS)
    request = documents.acknowledge(closures)
    documents.member(request, "evidence_snapshot")["generation"] = 2**63 - 1
    request["generation"] = 2**63 - 1
    for disposition in documents.members(request, "dispositions"):
        disposition.update(status="refused", reason="evidence_conflict")

    parsed = AcknowledgeRead.model_validate(request)

    assert len(parsed.dispositions) == MAX_PAGE_ROWS
    assert json_size(request) < MAX_ENVELOPE_BYTES


def test_worst_case_observation_metadata_fits_its_limit() -> None:
    observation = documents.recovery_evidence(
        [],
        attempt_id=WIDEST,
        execution_domain_id=WIDEST,
        handle=documents.handle(
            runtime=WIDEST,
            attempt_id=WIDEST,
            execution_domain_id=WIDEST,
            host_hint=WIDEST,
        ),
        termination=documents.termination(
            attempt_id=WIDEST,
            execution_domain_id=WIDEST,
            retirement_id=WIDEST,
            evidence_ref=WIDEST,
        ),
        normal_completion=documents.normal_completion(
            attempt_id=WIDEST, execution_domain_id=WIDEST, record_ref=WIDEST
        ),
        publication_outcome=documents.outcome(
            state="failed", operation_id=WIDEST, code="c" * 64
        ),
    )
    observation["closed_dispatchers"] = {
        **documents.member(observation, "closed_dispatchers"),
        "attempt_id": WIDEST,
        "execution_domain_id": WIDEST,
        "namespace_id": WIDEST,
        "generation": 2**63 - 1,
        "count": 2**63 - 1,
        "digest": documents.SHA,
        "grant_snapshot": widest_snapshot(),
    }

    parsed = RecoveryEvidence.model_validate_json(json.dumps(observation))

    assert observation_metadata_size(parsed) < MAX_OBSERVATION_METADATA_BYTES


def changed_last(page: dict[str, object]) -> dict[str, object]:
    grants = documents.members(page, "grants")
    grants[-1] = {**grants[-1], "worker_id": "worker-2"}
    return {**page, "grants": grants}


def reordered(page: dict[str, object]) -> dict[str, object]:
    return {**page, "grants": list(reversed(documents.members(page, "grants")))}


GRANTS: list[dict[str, object]] = history(3)
GRANT_PAGE: dict[str, object] = documents.grant_page(GRANTS, 1, 3)
REFUSED_GRANT_PAGES: list[dict[str, object]] = [
    changed_last(GRANT_PAGE),
    reordered(GRANT_PAGE),
    {**GRANT_PAGE, "grants": documents.members(GRANT_PAGE, "grants")[:1]},
    {**GRANT_PAGE, "complete": False},
    documents.grant_page(GRANTS, 1, 1),
    {**documents.grant_page(GRANTS, 0, 2), "complete": True},
    {**GRANT_PAGE, "snapshot": documents.grant_snapshot(GRANTS[:2])},
    {
        **GRANT_PAGE,
        "start": {**documents.grant_position([]), "prefix_digest": documents.SHA},
    },
    documents.grant_page(
        [*GRANTS[:2], documents.grant(3, execution_domain_id="domain-2")], 1, 3
    ),
    documents.grant_page([documents.grant(2), documents.grant(1)], 0, 2),
    documents.grant_page(history(MAX_PAGE_ROWS + 1), 0, MAX_PAGE_ROWS + 1),
]


@pytest.mark.parametrize("page", REFUSED_GRANT_PAGES)
def test_grant_pages_refuse_broken_relationships(page: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GrantHistoryPage.model_validate(page)


def test_grant_pages_accept_contiguous_ranges() -> None:
    pages = [
        documents.grant_page(GRANTS, start, end) for start, end in ((0, 1), (1, 3))
    ]
    empty_final = documents.grant_page([], 0, 0)

    parsed = [GrantHistoryPage.model_validate(page) for page in pages]
    final = GrantHistoryPage.model_validate(empty_final)

    assert [page.complete for page in parsed] == [False, True]
    assert parsed[0].end == parsed[1].start
    assert final.complete is True


def test_grant_pages_hold_128_rows_or_their_byte_envelope() -> None:
    rows = history(MAX_PAGE_ROWS)
    widest = [widest_grant(index) for index in range(1, 69)]

    full = GrantHistoryPage.model_validate(documents.grant_page(rows, 0, MAX_PAGE_ROWS))
    packed = GrantHistoryPage.model_validate(
        documents.grant_page(widest, 0, 67, WIDEST_SCOPE)
    )

    assert len(full.grants) == MAX_PAGE_ROWS
    assert len(packed.grants) == 67
    with pytest.raises(ValidationError):
        GrantHistoryPage.model_validate(
            documents.grant_page(widest, 0, 68, WIDEST_SCOPE)
        )


@pytest.mark.parametrize(
    ("accepted", "complete"), [(1, True), (3, False), (3, None), (0, True)]
)
def test_grant_progress_stays_within_its_snapshot(
    accepted: int, complete: bool | None
) -> None:
    document = {
        "snapshot": documents.grant_snapshot(GRANTS[:2]),
        "accepted": documents.grant_position(history(accepted)),
        "complete": complete,
    }

    with pytest.raises(ValidationError):
        GrantHistoryProgress.model_validate(document)


CLOSURES: list[dict[str, object]] = documents.dispatcher_closures(3)
RECOVERY_PAGE: dict[str, object] = documents.recovery_page(CLOSURES, 1, 3)


def with_observation(
    page: dict[str, object], change: Callable[[dict[str, object]], dict[str, object]]
) -> dict[str, object]:
    return {**page, "observation": change(documents.member(page, "observation"))}


REFUSED_RECOVERY_PAGES: list[dict[str, object]] = [
    {**RECOVERY_PAGE, "generation": 2},
    {**RECOVERY_PAGE, "observation": None},
    {
        **documents.recovery_page(CLOSURES, 0, 1),
        "observation": RECOVERY_PAGE["observation"],
    },
    {**documents.recovery_page(CLOSURES, 0, 1), "complete": True},
    {
        **RECOVERY_PAGE,
        "closed_dispatchers": documents.members(RECOVERY_PAGE, "closed_dispatchers")[
            :1
        ],
    },
    documents.recovery_page(CLOSURES, 1, 1),
    with_observation(
        RECOVERY_PAGE,
        lambda observation: {
            **observation,
            "closed_dispatchers": documents.closure_snapshot(CLOSURES[:2]),
        },
    ),
    {
        **RECOVERY_PAGE,
        "start": {"sequence": 1, "count": 2, "prefix_digest": documents.SHA},
    },
    documents.recovery_page(
        [
            *CLOSURES[:2],
            documents.closure(quiescence=documents.quiescence(attempt_id="attempt-2")),
        ],
        1,
        3,
    ),
]


@pytest.mark.parametrize("page", REFUSED_RECOVERY_PAGES)
def test_recovery_pages_refuse_broken_relationships(page: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RecoveryPage.model_validate(page)


def test_closure_pages_hold_their_byte_envelope() -> None:
    widest = [widest_closure(index) for index in range(1, 60)]

    relayed = ClosureEvidence.model_validate(
        documents.closure_evidence(runtime=None, dispatchers=widest[:58])
    )
    page = RecoveryPage.model_validate(
        documents.recovery_page(widest, 0, 57, WIDEST_SCOPE)
    )
    final_page = RecoveryPage.model_validate(
        documents.recovery_page(widest[:43], 0, 43, WIDEST_SCOPE)
    )

    assert len(relayed.dispatchers) == 58
    assert len(page.closed_dispatchers) == 57
    assert final_page.observation is not None
    with pytest.raises(ValidationError):
        ClosureEvidence.model_validate(
            documents.closure_evidence(runtime=None, dispatchers=widest)
        )
    with pytest.raises(ValidationError):
        RecoveryPage.model_validate(
            documents.recovery_page(widest, 0, 58, WIDEST_SCOPE)
        )


def test_final_page_content_sections_are_outside_the_envelope() -> None:
    blob = "x" * (MAX_JSON_BYTES - 64)
    reference = {**documents.VCS_REF, "diff_stat": "d" * (MAX_JSON_BYTES - 4096)}
    page = with_observation(
        documents.recovery_page(CLOSURES, 0, 3),
        lambda observation: {
            **observation,
            "handle": documents.handle(data={"blob": blob}),
            "publication_outcome": documents.outcome(
                state="completed", operation_id="op-1", ref=reference
            ),
        },
    )

    parsed = RecoveryPage.model_validate(page)
    records = [record_bytes(closure) for closure in parsed.closed_dispatchers]

    assert parsed.observation is not None
    assert len(json.dumps(page)) > 2 * MAX_ENVELOPE_BYTES
    assert recovery_page_size(parsed, records) < MAX_ENVELOPE_BYTES


def test_page_size_counters_match_canonical_bytes() -> None:
    grant_page = GrantHistoryPage.model_validate(
        documents.grant_page(
            [widest_grant(epoch) for epoch in range(1, 4)], 0, 3, WIDEST_SCOPE
        )
    )
    recovery_page = RecoveryPage.model_validate(documents.recovery_page(CLOSURES, 0, 3))
    observation = recovery_page.model_dump(mode="json")
    observation["observation"]["handle"]["data"] = None
    observation["observation"]["publication_outcome"]["ref"] = None

    grant_records = [record_bytes(grant) for grant in grant_page.grants]
    closure_records = [record_bytes(item) for item in recovery_page.closed_dispatchers]

    assert grant_page_size(grant_page, grant_records) == len(
        canonical_bytes(grant_page.model_dump(mode="json"))
    )
    assert recovery_page_size(recovery_page, closure_records) == len(
        canonical_bytes(observation)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 2},
        {"end": documents.closure_position(documents.dispatcher_closures(4))},
    ],
)
def test_acknowledge_refuses_a_foreign_pass(change: dict[str, object]) -> None:
    document = {**documents.acknowledge(CLOSURES), **change}

    with pytest.raises(ValidationError):
        AcknowledgeRead.model_validate(document)


def disposition(sequence: int, **changes: object) -> dict[str, object]:
    return {
        "sequence": sequence,
        "item_digest": documents.SHA,
        "status": "accepted",
        "reason": None,
        **changes,
    }


@pytest.mark.parametrize(
    "dispositions",
    [
        [disposition(2), disposition(1)],
        [disposition(1), disposition(1)],
        [disposition(4)],
        [disposition(0)],
        [disposition(1, status="refused")],
        [disposition(1, reason="evidence_unbound")],
        [disposition(sequence) for sequence in range(1, MAX_PAGE_ROWS + 2)],
    ],
)
def test_acknowledge_dispositions_are_unique_ascending_and_bounded(
    dispositions: list[dict[str, object]],
) -> None:
    document = {**documents.acknowledge(CLOSURES), "dispositions": dispositions}

    with pytest.raises(ValidationError):
        AcknowledgeRead.model_validate(document)


@pytest.mark.parametrize(
    "document",
    [
        {**documents.grant_snapshot([]), "digest": documents.SHA},
        {**documents.grant_snapshot([]), "count": 1},
        {**documents.grant_snapshot(GRANTS), "upper_epoch": None},
        {**documents.closure_snapshot([]), "digest": documents.SHA},
        {
            **documents.closure_snapshot([]),
            "grant_snapshot": {
                **documents.grant_snapshot([documents.grant(1)]),
                "attempt_id": "attempt-2",
            },
        },
        {"after_epoch": None, "received_count": 1, "prefix_digest": documents.SHA},
        {"sequence": 1, "count": 2, "prefix_digest": documents.SHA},
        {"after_epoch": 1, "received_count": 1, "prefix_digest": "A" * 64},
    ],
)
def test_snapshots_and_positions_check_their_seed_and_counts(
    document: dict[str, object],
) -> None:
    models = [
        GrantHistorySnapshot,
        ClosureSnapshot,
        GrantHistoryPosition,
        ClosurePosition,
    ]

    refused = [model for model in models if not accepts(model, document)]

    assert refused == models


def accepts(
    model: type[
        GrantHistorySnapshot | ClosureSnapshot | GrantHistoryPosition | ClosurePosition
    ],
    document: dict[str, object],
) -> bool:
    try:
        model.model_validate(document)
    except ValidationError:
        return False
    return True


def test_recovery_progress_stays_within_its_snapshot() -> None:
    document = documents.recovery_progress(scanned=2)

    with pytest.raises(ValidationError):
        RecoveryProgress.model_validate(document)


def test_readers_refuse_oversized_messages_before_decoding() -> None:
    page = json.dumps(documents.grant_page(GRANTS, 0, 3)).encode()
    request = json.dumps(documents.acknowledge(CLOSURES)).encode()
    padding = b" " * MAX_ENVELOPE_BYTES

    parsed_page = read_grant_history_page(page)
    parsed_request = read_recovery_read(request)

    assert parsed_page.complete is True
    assert isinstance(parsed_request, AcknowledgeRead)
    with pytest.raises(HistoryEnvelopeExceeded):
        read_grant_history_page(page + padding)
    with pytest.raises(HistoryEnvelopeExceeded):
        read_recovery_read(b"{" + padding)
    with pytest.raises(ValidationError):
        read_recovery_read(b'{"kind": "unknown"}')


def tip_mismatches() -> list[dict[str, object]]:
    grants = GRANTS[:2]
    tip = documents.grant_position(grants)
    return [
        {**tip, "after_epoch": 1},
        {**documents.grant_position(grants[:1]), "after_epoch": 2},
        {**tip, "prefix_digest": documents.SHA},
    ]


@pytest.mark.parametrize("accepted", tip_mismatches())
def test_a_position_at_the_tip_must_be_the_tip(accepted: dict[str, object]) -> None:
    document = {
        "snapshot": documents.grant_snapshot(GRANTS[:2]),
        "accepted": accepted,
        "complete": False,
    }

    with pytest.raises(ValidationError):
        GrantHistoryProgress.model_validate(document)


def test_a_closure_end_at_the_count_must_be_the_tip() -> None:
    document = documents.acknowledge(
        CLOSURES,
        end={"sequence": 3, "count": 3, "prefix_digest": documents.SHA},
    )

    with pytest.raises(ValidationError):
        AcknowledgeRead.model_validate(document)


def test_a_changed_closure_breaks_the_recovery_chain() -> None:
    page = documents.recovery_page(CLOSURES, 1, 3)
    closures = documents.members(page, "closed_dispatchers")
    changed = documents.closure(
        quiescence=documents.quiescence(
            dispatcher_id="dispatcher-3", evidence_ref="other"
        )
    )

    altered = {**page, "closed_dispatchers": [closures[0], changed]}

    with pytest.raises(ValidationError):
        RecoveryPage.model_validate(altered)
