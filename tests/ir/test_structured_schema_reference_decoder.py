import pytest

from kora.ir.structured_schema_locations import decode_reference
from kora.ir.structured_schema_work import PreparationMeter, PreparationRefused
from tests.ir.structured_schema_cases import parameters


@pytest.mark.parametrize(
    ("reference", "name", "tokens"),
    [
        pytest.param("#", None, [], id="local-root"),
        pytest.param(
            "urn:kora:artifact:example:Target", "example/Target", [], id="saved-root"
        ),
        pytest.param("#/$defs/a~1b~0c", None, ["$defs", "a/b~c"], id="tilde"),
        pytest.param(
            "#/$defs/a%7E1b", None, ["$defs", "a/b"], id="percent-before-tilde"
        ),
        pytest.param("#/$defs/a~01b", None, ["$defs", "a~1b"], id="single-tilde-pass"),
        pytest.param("#/$defs/%F0%9F%8C%8D", None, ["$defs", "🌍"], id="strict-utf8"),
        pytest.param("#/$defs/..", None, ["$defs", ".."], id="literal-dot-segment"),
        pytest.param("#/$defs/", None, ["$defs", ""], id="empty-token"),
        pytest.param("#/$defs/%00", None, ["$defs", "\x00"], id="nul-token"),
    ],
)
def test_schema_reference_decode_uses_one_fixed_token_pool_and_scratch_reservation(
    reference: str, name: str | None, tokens: list[str]
) -> None:
    meter = PreparationMeter()

    target, pool, used = decode_reference(reference, meter)

    assert target == name
    assert used == len(tokens)
    assert pool[:used] == tokens
    assert len(pool) == len(reference) + 1
    assert meter.work == 1 + 8 * len(reference)
    assert meter.auxiliary_bytes == 4 * len(reference)
    assert meter.auxiliary_entries == len(reference) + 1


@pytest.mark.parametrize("dimension", ["work", "bytes", "entries"])
@pytest.mark.parametrize("delta", [0, 1], ids=["exact", "one-short"])
def test_schema_reference_decode_reserves_before_parsing(
    dimension: str, delta: int
) -> None:
    reference = "#/$defs/a~1b"
    length = len(reference)
    meter = PreparationMeter(
        work=parameters()["max_preparation_work"] - (1 + 8 * length) + delta
        if dimension == "work"
        else 0,
        auxiliary_bytes=parameters()["max_auxiliary_bytes"] - 4 * length + delta
        if dimension == "bytes"
        else 0,
        auxiliary_entries=parameters()["max_auxiliary_entries"] - (length + 1) + delta
        if dimension == "entries"
        else 0,
    )

    if delta:
        with pytest.raises(PreparationRefused) as rejected:
            decode_reference(reference, meter)

        assert rejected.value.reason == "preparation_exhausted"
        return
    target, pool, used = decode_reference(reference, meter)

    assert target is None
    assert pool[:used] == ["$defs", "a/b"]
    assert {
        "work": meter.work,
        "bytes": meter.auxiliary_bytes,
        "entries": meter.auxiliary_entries,
    }[dimension] == parameters()[
        {
            "work": "max_preparation_work",
            "bytes": "max_auxiliary_bytes",
            "entries": "max_auxiliary_entries",
        }[dimension]
    ]


@pytest.mark.parametrize(
    "reference",
    [
        "#named",
        "#/%",
        "#/%GG",
        "#/%FF",
        "#/%ED%A0%80",
        "#/a~",
        "#/a~2",
        "relative#",
        "https://example.invalid/#",
    ],
    ids=[
        "anchor",
        "short-percent",
        "hex",
        "utf8",
        "surrogate",
        "short-tilde",
        "tilde",
        "relative",
        "external",
    ],
)
def test_schema_reference_decoder_refuses_fixed_unsupported_grammar(
    reference: str,
) -> None:
    meter = PreparationMeter()

    with pytest.raises(PreparationRefused) as rejected:
        decode_reference(reference, meter)

    assert rejected.value.reason == "unsupported_schema"
