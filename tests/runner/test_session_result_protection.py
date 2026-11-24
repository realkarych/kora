from datetime import UTC, datetime
from itertools import chain, islice, product
from threading import Event
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir.cost import UsageCoverage, UsageRow
from kora.ir.json import MAX_CANONICAL_BYTES, MAX_INTEGER_BITS, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability.settings import (
    MAX_SECRET_BYTES,
    MAX_SECRET_VALUE_BYTES,
    MAX_SECRETS,
)
from kora.protocols.harness import (
    DenialSample,
    OutputCandidate,
    SessionResult,
    UnconfirmedSample,
)
from kora.protocols.runtime import (
    SessionFailedObservation,
    SessionObservation,
    SessionResultObservation,
)
from kora.protocols.secret_value import registered_forms
from kora.runner.session_result import (
    digest_of,
    prepare_finalization,
    prepare_observation,
)
from kora.runner.session_result_encoding import (
    KEY_BATCH,
    ResultPreparationStopped,
    merge_key_runs,
)
from tests.runner.test_session_result import finalization_facts

KEY: Final[str] = 'fixture-credential-é"\\'
COVERAGE: Final[UsageCoverage] = UsageCoverage(
    version=1,
    observation_id=None,
    observation_sequence=None,
    completeness="unavailable",
    observed_at=None,
    attribution_gap=False,
    observation_gap=False,
)
REFUSAL: Final[bytes] = b'{"code":"secret_leak","kind":"session_failed"}'


def usage_row(index: int, content: JsonValue) -> UsageRow:
    return UsageRow(
        model=f"model-{index}",
        input_tokens=0,
        cached_input_tokens=0,
        cache_creation_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        known_token_fields=frozenset(),
        cache_creation_by_ttl=None,
        reported_usd=None,
        observation_id=f"observation-{index}",
        observation_sequence=index,
        observed_at=datetime(2026, 11, 21, tzinfo=UTC),
        completeness="partial",
        basis="price_table",
        billing_class="api_key",
        delta_of_session_totals=False,
        source={"content": content},
    )


def observation(
    value: JsonValue,
    *,
    usage: tuple[UsageRow, ...] = (),
    session_id: str = "test-session",
    harness: str = "test-harness",
    installed_version: str = "test-version",
) -> SessionResultObservation:
    return SessionResultObservation(
        kind="result",
        cause=None,
        journal_complete=True,
        result=SessionResult(
            harness=harness,
            session_id=session_id,
            installed_version=installed_version,
            billing_class=None,
            stop_reason="completed",
            terminal_reason=None,
            api_error_status=None,
            error_class=None,
            error_summary=None,
            output=OutputCandidate(source="structured_output", value=value),
            output_diagnostic=None,
            usage=usage,
            usage_coverage=COVERAGE,
            turns=0,
            denials=DenialSample(items=(), total=0),
            denials_complete=True,
            unconfirmed_calls=UnconfirmedSample(items=(), total=0),
            policy_bypass=False,
        ),
    )


@pytest.mark.parametrize("position", ["key", "value", "identifier", "usage", "code"])
@pytest.mark.parametrize("spelling", registered_forms(KEY))
def test_session_observation_refuses_registered_secret_forms(
    position: str, spelling: str, kora_capture: Capture
) -> None:
    kora_capture.scope.admit(KEY)
    candidate: SessionObservation
    if position == "key":
        candidate = observation({spelling: "nested"})
    elif position == "value":
        candidate = observation({"outer": [spelling]})
    elif position == "identifier":
        candidate = observation(None, session_id="prefix-secret-suffix")
        kora_capture.scope.admit("prefix-secret-suffix")
    elif position == "usage":
        candidate = observation(None, usage=(usage_row(1, {spelling: True}),))
    else:
        candidate = SessionFailedObservation(
            kind="session_failed", code="fixture_secret"
        )
        kora_capture.scope.admit("fixture_secret")
    forms = kora_capture.scope.result_spellings()
    original = canonical_bytes(candidate.model_dump(mode="json"))

    result = prepare_observation(candidate, forms, Event())

    assert result.failure == "secret_leak"
    assert result.payload == REFUSAL
    assert result.reference.digest != digest_of(original)
    assert result.reference.digest == digest_of(REFUSAL)


@pytest.mark.parametrize(
    ("secret", "value"),
    [
        ("left\\nright", "left\nright"),
        ("1234567890123", 1_234_567_890_123),
        ("raw-compound", "x" * 2043 + "raw-compound"),
        ("journal_complete", {"journal_complete": 1}),
        ("secret_leak", {"value": "secret_leak"}),
    ],
)
def test_session_observation_checks_dynamic_canonical_spellings_and_spans(
    secret: str, value: JsonValue, kora_capture: Capture
) -> None:
    kora_capture.scope.admit(secret)
    candidate = observation(value)
    forms = kora_capture.scope.result_spellings()

    result = prepare_observation(candidate, forms, Event())

    assert result.payload == REFUSAL
    assert result.failure == "secret_leak"


def test_fixed_result_envelope_fields_do_not_trigger_refusal(
    kora_capture: Capture,
) -> None:
    for value in ("journal_complete", "secret_leak", "structured_output"):
        kora_capture.scope.admit(value)
    candidate = observation(None)
    original = canonical_bytes(candidate.model_dump(mode="json"))
    forms = kora_capture.scope.result_spellings()

    result = prepare_observation(candidate, forms, Event())

    assert result.failure is None
    assert result.payload == original
    assert result.reference.digest == digest_of(original)
    assert b'"output":{"source":"structured_output","value":null}' in result.payload


def fill_registry(capture: Capture) -> None:
    remaining = MAX_SECRET_BYTES - MAX_SECRET_VALUE_BYTES
    size, extras = divmod(remaining, MAX_SECRETS - 1)
    lengths = (MAX_SECRET_VALUE_BYTES,) + tuple(
        size + int(index < extras) for index in range(MAX_SECRETS - 1)
    )
    for index, width in enumerate(lengths):
        prefix = f"synthetic-form-{index:03}-"
        capture.scope.admit(prefix + "A" * (width - len(prefix)))


def full_output() -> dict[str, JsonValue]:
    large_integer = 1 << (MAX_INTEGER_BITS - 1)
    deep: JsonValue = large_integer
    for _ in range(15):
        deep = [deep]
    value: dict[str, JsonValue] = {"maximum": deep, "padding": ""}
    value["padding"] = "x" * (MAX_CANONICAL_BYTES - len(canonical_bytes(value)))
    return value


def test_session_observation_preserves_one_upper_bound_clean_output(
    kora_capture: Capture,
) -> None:
    registry = kora_capture.diagnostics.registry
    base = (registry.count(), registry.retained_bytes())
    fill_registry(kora_capture)
    forms = kora_capture.scope.result_spellings()
    output = full_output()
    candidate = observation(
        output,
        usage=tuple(
            usage_row(index + 1, [index, "clean", -0.0]) for index in range(64)
        ),
    )
    original = canonical_bytes(candidate.model_dump(mode="json"))

    result = prepare_observation(candidate, forms, Event())
    kora_capture.scope.release()

    assert len(forms) == MAX_SECRETS
    assert sum(len(form.encode()) for form in forms) == MAX_SECRET_BYTES
    assert max(len(form.encode()) for form in forms) == MAX_SECRET_VALUE_BYTES
    assert len(canonical_bytes(output)) == MAX_CANONICAL_BYTES
    assert result.failure is None
    assert result.payload == original
    assert result.reference.size == len(original)
    assert result.reference.digest == digest_of(original)
    assert (registry.count(), registry.retained_bytes()) == base


def widest_output() -> dict[str, JsonValue]:
    alphabet = tuple(chr(code) for code in range(127, 31, -1) if code not in (34, 92))
    two_byte = chain(
        ("".join(parts) for parts in product(alphabet, repeat=2)),
        '"\\\b\f\n\r\t',
        map(chr, range(128, 2048)),
    )
    three_byte = ("".join(parts) for parts in product(alphabet, repeat=3))
    output: dict[str, JsonValue] = {}
    remaining = MAX_CANONICAL_BYTES - 1
    for width, keys in enumerate((("",), alphabet, two_byte, three_byte)):
        entry_bytes = width + 5
        for key in islice(keys, remaining // entry_bytes):
            output[key] = 0
            remaining -= entry_bytes
    output[""] = 10**remaining
    return output


def test_session_observation_preserves_widest_clean_key_order() -> None:
    output = widest_output()
    candidate = observation(output)
    original = canonical_bytes(candidate.model_dump(mode="json"))

    result = prepare_observation(candidate, ("impossible-secret-match",), Event())

    assert len(output) > KEY_BATCH**2
    assert len(canonical_bytes(output)) == MAX_CANONICAL_BYTES
    assert result.payload == original
    assert result.reference.digest == digest_of(original)
    assert result.failure is None


class StopAtSecondPoll(Event):
    polls: int = 0

    def is_set(self) -> bool:
        self.polls += 1
        if self.polls == 2:
            self.set()
        return super().is_set()


def test_result_key_merge_polls_stop_during_heap_initialization() -> None:
    stop = StopAtSecondPoll()
    groups = [[f"key-{index}"] for index in range(KEY_BATCH + 1)]

    with pytest.raises(ResultPreparationStopped):
        next(merge_key_runs(groups, stop))

    assert stop.polls == 2


def test_session_finalization_refuses_secret_and_preserves_observation() -> None:
    sensitive = "registered-sidecar-price"
    facts = finalization_facts().model_copy(update={"price_table_id": sensitive})
    stop = Event()
    original = prepare_observation(observation({"safe": "clean"}), (), stop)

    declined = prepare_finalization(original, facts, (sensitive,), stop)
    accepted = prepare_finalization(original, finalization_facts(), (sensitive,), stop)

    assert declined is original
    assert declined.finalization is None
    assert accepted.finalization is not None
    assert accepted.payload is original.payload
    assert accepted.reference == original.reference
    assert b"registered-sidecar-price" not in accepted.finalization.payload
