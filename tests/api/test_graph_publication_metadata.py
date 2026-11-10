from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.api.graph_publication_metadata import (
    PreparedGraphPublicationMetadata,
    prepare_graph_publication_metadata,
)
from kora.api.graph_publication_values import (
    DEPLOYED_BY_PARAMETER,
    RESOLVED_DIGESTS_PARAMETER,
)
from kora.db.connection import Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.identifiers import encode
from kora.db.types import canonical_json
from kora.protocols.authentication import Principal

ACTOR = "private-publication-owner"
ENTRIES = (("日本", "private-digest 😀"), ("clé", 'quote " and \\'))


def personal_principal(owner: str = ACTOR) -> Principal:
    return Principal(
        token_id=encode(1),
        kind="user",
        owner=owner,
        namespaces=(),
        approve=False,
        platform=False,
        runtime=None,
    )


@pytest.mark.parametrize("dialect", list(Dialect))
@pytest.mark.parametrize("entries", [(), ENTRIES])
def test_publication_metadata_retains_exact_canonical_values(
    dialect: Dialect, entries: tuple[tuple[str, str], ...]
) -> None:
    principal = personal_principal()
    canonical = canonical_json({name: digest for name, digest in entries})
    limit = len(canonical.encode()) + len(principal.owner)

    prepared = prepare_graph_publication_metadata(
        dialect, principal, entries, max_metadata_bytes=limit
    )
    reversed_input = prepare_graph_publication_metadata(
        dialect, principal, entries[::-1], max_metadata_bytes=limit
    )
    binds = prepared.parameters()
    binds[DEPLOYED_BY_PARAMETER] = "different-owner"
    binds[RESOLVED_DIGESTS_PARAMETER] = "{}"
    with pytest.raises(AttributeError):
        prepared.dialect = Dialect.SQLITE
    with pytest.raises(TypeError):
        prepared.__getstate__()

    assert prepared.dialect is dialect
    assert (
        prepared.parameters()
        == reversed_input.parameters()
        == {
            DEPLOYED_BY_PARAMETER: principal.owner,
            RESOLVED_DIGESTS_PARAMETER: canonical,
        }
    )
    assert principal.owner not in repr(prepared)
    assert "private-digest" not in repr(prepared)


@pytest.mark.parametrize(
    ("entries", "limit", "expected"),
    [
        (
            ENTRIES,
            len(canonical_json(dict(ENTRIES)).encode()) + len(ACTOR) - 1,
            GraphValueFailure.BUDGET,
        ),
        (ENTRIES, 1, GraphValueFailure.BUDGET),
        ((), len(ACTOR) + 1, GraphValueFailure.BUDGET),
        (
            (("private-key", "one"), ("private-key", "two")),
            4096,
            GraphValueFailure.INVALID,
        ),
        ((("private-key", "\x00"),), 4096, GraphValueFailure.INVALID),
        ((("\ud800", "private-digest"),), 4096, GraphValueFailure.INVALID),
        ([("private-key", "private-digest")], 4096, GraphValueFailure.INVALID),
        ((("private-key",),), 4096, GraphValueFailure.INVALID),
        ((("private-key", 7),), 4096, GraphValueFailure.INVALID),
    ],
)
def test_publication_metadata_refuses_private_malformed_or_oversized_values(
    entries: object, limit: int, expected: GraphValueFailure
) -> None:
    principal = personal_principal()

    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_publication_metadata(
            Dialect.SQLITE,
            principal,
            cast(tuple[tuple[str, str], ...], entries),
            max_metadata_bytes=limit,
        )

    assert refused.value.reason is expected
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert all(
        marker not in "".join(format_exception(refused.value))
        for marker in (ACTOR, "private-key", "private-digest", principal.token_id)
    )
    for frame, _ in walk_tb(refused.value.__traceback__):
        if frame.f_code.co_name == "prepare_graph_publication_metadata":
            assert {"principal", "resolved_digests", "owner", "encoded"}.isdisjoint(
                frame.f_locals
            )
        assert frame.f_code.co_name != "_canonical_resolved_digests"


@pytest.mark.parametrize(
    "principal",
    [
        Principal(
            token_id=encode(1),
            kind="service",
            owner=ACTOR,
            namespaces=(),
            approve=False,
            platform=False,
            runtime="local",
        ),
        Principal.model_construct(kind="user", owner=ACTOR + " "),
        Principal.model_construct(kind="user", owner=ACTOR + "\x00"),
        Principal.model_construct(kind="user", owner=ACTOR + "é"),
    ],
)
def test_publication_metadata_refuses_unusable_actor(principal: Principal) -> None:
    dialect = Dialect.SQLITE

    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_publication_metadata(
            dialect, principal, (), max_metadata_bytes=4096
        )

    assert refused.value.reason is GraphValueFailure.INVALID
    assert ACTOR not in str(refused.value)


def test_publication_metadata_preserves_native_actor_capacity() -> None:
    long_principal = personal_principal("A" * 256)
    short_principal = personal_principal("B" * 255)

    sqlite = prepare_graph_publication_metadata(
        Dialect.SQLITE, long_principal, (), max_metadata_bytes=258
    )
    postgres = prepare_graph_publication_metadata(
        Dialect.POSTGRESQL, short_principal, (), max_metadata_bytes=257
    )
    with pytest.raises(GraphValueRejected) as refused:
        prepare_graph_publication_metadata(
            Dialect.POSTGRESQL, long_principal, (), max_metadata_bytes=258
        )

    assert sqlite.parameters()[DEPLOYED_BY_PARAMETER] == long_principal.owner
    assert postgres.parameters()[DEPLOYED_BY_PARAMETER] == short_principal.owner
    assert refused.value.reason is GraphValueFailure.INVALID


def test_publication_metadata_requires_the_checked_producer() -> None:
    external_issuer = object()

    with pytest.raises(TypeError) as refused:
        PreparedGraphPublicationMetadata(external_issuer, Dialect.SQLITE, "{}", ACTOR)

    assert ACTOR not in str(refused.value)
