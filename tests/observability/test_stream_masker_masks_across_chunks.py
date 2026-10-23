import hashlib
import json
import re

import pytest

from kora.observability.redaction import MASK, SecretRegistry, StreamMasker
from kora.protocols.secret_value import registered_forms

PLAIN: str = "plain-secret-7d41c0"
NON_ASCII: str = "секрет-значение-é"
FIRST: str = "abcdefgh12"
SECOND: str = "gh12ijkl99"
REPEATED: str = "aaaaaaaa"


def registry_with(*values: str) -> SecretRegistry:
    registry = SecretRegistry()
    for value in values:
        for form in registered_forms(value):
            registry.register(form)
    return registry


def masked_stream(registry: SecretRegistry, chunks: list[bytes]) -> str:
    parts: list[str] = []
    with StreamMasker(registry, parts.append) as masker:
        for chunk in chunks:
            masker.feed(chunk)
    return "".join(parts)


def split_everywhere(data: bytes) -> list[list[bytes]]:
    return [[data[:index], data[index:]] for index in range(len(data) + 1)]


@pytest.mark.parametrize("value", [PLAIN, NON_ASCII])
def test_a_value_split_at_every_byte_offset_is_masked(value: str) -> None:
    registry = registry_with(value)
    data = f"before {value} after\n".encode()

    outputs = [masked_stream(registry, chunks) for chunks in split_everywhere(data)]

    assert set(outputs) == {f"before {MASK} after\n"}


def test_overlapping_values_are_masked_whole() -> None:
    registry = registry_with(FIRST, SECOND)

    output = masked_stream(registry, [b"x abcdefgh12ijkl99 y"])

    assert output == f"x {MASK} y"


def test_a_self_overlapping_value_is_masked_in_a_long_run() -> None:
    registry = registry_with(REPEATED)
    data = b"a" * 100_003

    output = masked_stream(registry, [data[:50_000], data[50_000:]])

    assert "a" * len(REPEATED) not in output
    assert set(output.replace(MASK, "")) <= {"a"}


def test_a_value_registered_mid_stream_is_masked_from_then_on() -> None:
    registry = SecretRegistry()
    parts: list[str] = []

    with StreamMasker(registry, parts.append) as masker:
        masker.feed(b"early line\n")
        registry.register(PLAIN)
        masker.feed(f"late {PLAIN}\n".encode())

    assert "".join(parts) == f"early line\nlate {MASK}\n"


def test_escaped_forms_of_a_value_are_masked() -> None:
    registry = registry_with(NON_ASCII)
    escaped = (json.dumps(NON_ASCII), json.dumps(NON_ASCII, ensure_ascii=False))

    output = masked_stream(registry, [f"{escaped[0]} {escaped[1]}".encode()])

    assert output == f'"{MASK}" "{MASK}"'


def test_invalid_utf8_is_replaced_and_masking_continues() -> None:
    registry = registry_with(PLAIN)

    output = masked_stream(registry, [b"bad \xff\xfe ", PLAIN.encode()])

    assert output == f"bad �� {MASK}"


def test_ordinary_output_is_not_held_back() -> None:
    registry = registry_with(PLAIN)
    parts: list[str] = []
    masker = StreamMasker(registry, parts.append)

    masker.feed(b"ordinary output line\n")

    assert masker.held == 0
    assert "".join(parts) == "ordinary output line\n"
    masker.close()


def test_input_above_the_chunk_size_is_split_and_masked() -> None:
    registry = registry_with(PLAIN)
    padding = b"x" * (65_536 - 5)
    data = padding + PLAIN.encode() + padding

    output = masked_stream(registry, [data])

    assert output == padding.decode() + MASK + padding.decode()


def feed_then_fail(masker: StreamMasker, data: bytes) -> None:
    with masker:
        masker.feed(data)
        raise LookupError


def test_closing_after_an_exception_emits_the_held_tail_masked() -> None:
    registry = registry_with(PLAIN)
    parts: list[str] = []
    masker = StreamMasker(registry, parts.append)

    with pytest.raises(LookupError):
        feed_then_fail(masker, PLAIN[:-1].encode())
    masker.close()

    assert "".join(parts) == PLAIN[:-1]
    assert masker.held == 0


def test_overlapping_values_split_at_every_offset_are_masked_whole() -> None:
    registry = registry_with(FIRST, SECOND)
    data = b"x abcdefgh12ijkl99 y"

    outputs = {masked_stream(registry, chunks) for chunks in split_everywhere(data)}

    assert {collapsed(output) for output in outputs} == {f"x {MASK} y"}


def test_a_shorter_partial_value_does_not_shrink_the_held_tail() -> None:
    registry = registry_with("abcdefgh12", "gzzzzzzz99")

    output = masked_stream(registry, [b"x abcdefg", b"h12 y\n"])

    assert output == f"x {MASK} y\n"


def collapsed(text: str) -> str:
    return re.sub(f"({re.escape(MASK)})+", MASK, text)


class CaseSource:
    def __init__(self, index: int) -> None:
        self._bytes = b"".join(
            hashlib.sha256(f"masker-case-{index}-{block}".encode()).digest()
            for block in range(16)
        )
        self._position = 0

    def below(self, limit: int) -> int:
        byte = self._bytes[self._position % len(self._bytes)]
        self._position += 1
        return byte % limit

    def word(self, low: int, high: int) -> str:
        return "".join(
            "abcz"[self.below(4)] for _ in range(low + self.below(high - low + 1))
        )


def generated_case(index: int) -> tuple[set[str], str, list[int]]:
    source = CaseSource(index)
    values = {source.word(8, 12) for _ in range(1 + source.below(4))}
    text = source.word(0, 80)
    for value in sorted(values):
        at = source.below(len(text) + 1)
        text = text[:at] + value + text[at:]
    cuts = sorted({source.below(len(text) + 1) for _ in range(source.below(7))})
    return values, text, cuts


def test_streaming_masks_exactly_what_whole_text_masking_masks() -> None:
    cases = [generated_case(index) for index in range(2000)]

    mismatches: list[str] = []
    for values, text, cuts in cases:
        registry = registry_with(*values)
        data = text.encode()
        chunks = [
            data[start:end]
            for start, end in zip([0, *cuts], [*cuts, len(data)], strict=True)
        ]
        streamed = masked_stream(registry, chunks)
        if collapsed(streamed) != collapsed(registry.mask(text)):
            mismatches.append(text)

    assert mismatches == []


def test_windowed_record_masking_is_a_prefix_of_whole_text_masking() -> None:
    cases = [generated_case(index) for index in range(500)]

    mismatches: list[str] = []
    for values, text, _ in cases:
        registry = registry_with(*values)
        whole = collapsed(registry.mask(text))
        for retained in (1, 5, 13):
            windowed = collapsed(registry.mask(text, retained)).removesuffix(MASK)
            if not whole.startswith(windowed):
                mismatches.append(text)

    assert mismatches == []


def covered_reference(text: str, values: set[str]) -> str:
    covered = [False] * len(text)
    for value in values:
        for start in range(len(text) - len(value) + 1):
            if text.startswith(value, start):
                covered[start : start + len(value)] = [True] * len(value)
    parts = [MASK if flag else char for flag, char in zip(covered, text, strict=True)]
    return collapsed("".join(parts))


def test_masking_covers_every_occurrence_of_every_form() -> None:
    cases = [generated_case(index) for index in range(1000)]

    mismatches = [
        text
        for values, text, _ in cases
        if collapsed(registry_with(*values).mask(text))
        != covered_reference(text, values)
    ]

    assert mismatches == []


def periodic_case(index: int) -> tuple[set[str], str]:
    source = CaseSource(index)
    values: set[str] = set()
    runs: list[str] = []
    for _ in range(1 + source.below(3)):
        base = source.word(1, 4)
        value = (base * 12)[: 8 + source.below(5)]
        values.add(value)
        runs.append(base * (3 + source.below(20)))
    filler = [source.word(0, 6) for _ in runs]
    text = "".join(f"{gap}{run}" for gap, run in zip(filler, runs, strict=True))
    return values, text


def test_periodic_runs_are_covered_whole_in_every_path() -> None:
    cases = [periodic_case(index) for index in range(1000)]

    mismatches: list[str] = []
    for values, text in cases:
        registry = registry_with(*values)
        expected = covered_reference(text, values)
        data = text.encode()
        bytewise = [data[offset : offset + 1] for offset in range(len(data))]
        if collapsed(registry.mask(text)) != expected:
            mismatches.append(text)
        if collapsed(masked_stream(registry, bytewise)) != expected:
            mismatches.append(text)

    assert mismatches == []


def test_a_record_of_masked_runs_stops_with_one_trailing_mask() -> None:
    registry = registry_with("abababab")
    text = "x" + "ab" * 4000

    masked = registry.mask(text, 8)

    assert masked == f"x{MASK}"
    assert "ab" * 4 not in masked
