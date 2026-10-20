import logging
import types
import warnings
from typing import cast

import pytest
import structlog.contextvars
from sqlalchemy.exc import IntegrityError

from kora.db import DB_COMPONENT
from kora.db.connection import Database
from kora.db.reads import token_by_digest
from kora.observability import (
    EventSpec,
    Level,
    LoggingSettings,
    LossReason,
    RenderFormat,
    SecretAdmissionError,
)
from tests.db.seed import Seeder
from tests.observability.conftest import (
    LEVEL_EVENTS,
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
)

REGISTERED_VALUE: str = "canary-registered-value-0001"
UNREGISTERED_MARKER: str = "UNREGISTERED-MARKER-7731"
MASK: str = "[redacted]"
TRUNCATION_MARKER: str = "[truncated]"
BRIDGED_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "timestamp",
        "level",
        "event",
        "message",
        "component",
        "logger",
        "instance_id",
    }
)


def raise_chained(marker: str) -> None:
    local_copy = marker
    try:
        raise KeyError(f"cause {marker}")
    except KeyError as cause:
        try:
            raise ValueError(f"context {local_copy}")
        except ValueError:
            raise RuntimeError(f"outer {marker}") from cause


def raise_plain() -> None:
    raise ValueError("plain")


def finished_output(diagnostics_shutdown: object, pipe: Pipe) -> str:
    pipe.close_write_end()
    pipe.reader.wait_closed()
    return "\n".join(pipe.reader.lines())


@pytest.mark.parametrize("render_format", [RenderFormat.JSON, RenderFormat.TEXT])
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
def test_registered_values_are_masked_everywhere(
    configure: Configure, pipe: Pipe, render_format: RenderFormat, level: Level
) -> None:
    settings = LoggingSettings(level=level, render_format=render_format)
    diagnostics = configure(settings, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    registration = diagnostics.register_secret(REGISTERED_VALUE)
    pipe.reader.start()

    log.emit(NOTE, text=f"before {REGISTERED_VALUE} after")
    log.emit(NOTE, items=["plain", f"item {REGISTERED_VALUE}"])
    log.emit(NOTE, detail={"nested": {"deep": f"map {REGISTERED_VALUE}"}})
    log.emit(NOTE, text="a" * 1005 + REGISTERED_VALUE)
    log.emit(NOTE, text="a" * 1000 + REGISTERED_VALUE * 3 + "b" * 40)
    log.emit(LEVEL_EVENTS[Level.DEBUG], text=REGISTERED_VALUE)
    diagnostics.shutdown()
    output = finished_output(None, pipe)
    registration.release()

    expected_lines = 5 if level is Level.INFO else 7
    assert len(pipe.reader.lines()) == expected_lines
    assert REGISTERED_VALUE not in output
    assert REGISTERED_VALUE[:4] not in output
    assert "a" * 1005 + MASK[:8] in output
    assert "a" * 1000 + MASK + MASK[:3] in output
    assert output.count(MASK) >= 5
    assert TRUNCATION_MARKER in output


def test_a_value_extending_another_is_masked_whole_across_the_scan_window(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    extended = REGISTERED_VALUE + "-extended-0002"
    registrations = [
        diagnostics.register_secret(v) for v in (REGISTERED_VALUE, extended)
    ]
    head = ("a" * 5 + REGISTERED_VALUE) * 10
    text = head + "b" * (1030 - len(head)) + extended + "d" * 300
    pipe.reader.start()

    log.emit(NOTE, text=text)
    diagnostics.shutdown()
    output = finished_output(None, pipe)
    for registration in registrations:
        registration.release()

    assert "extended" not in output
    assert REGISTERED_VALUE[:4] not in output
    assert output.count(MASK) == 11
    assert TRUNCATION_MARKER in output


def test_unregistered_markers_in_exceptions_and_third_party_output_never_appear(
    configure: Configure, pipe: Pipe
) -> None:
    settings = LoggingSettings(level=Level.DEBUG, third_party={"vendor": Level.DEBUG})
    diagnostics = configure(settings, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    try:
        raise_chained(UNREGISTERED_MARKER)
    except RuntimeError as error:
        log.emit(NOTE, text="recorded", exception=error)
    vendor = logging.getLogger("vendor.lib")
    vendor.error("value %s", UNREGISTERED_MARKER)
    try:
        raise OSError(UNREGISTERED_MARKER)
    except OSError:
        vendor.warning("failed %s", UNREGISTERED_MARKER, exc_info=True)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(UNREGISTERED_MARKER, stacklevel=1)
    diagnostics.shutdown()
    output = finished_output(None, pipe)

    records = pipe.reader.records()
    assert UNREGISTERED_MARKER not in output
    assert "local_copy" not in output
    assert "raise RuntimeError" not in output
    recorded = next(record for record in records if record["event"] == "testing.note")
    chain = cast(list[dict[str, object]], recorded["exception"])
    frames = cast(list[dict[str, object]], chain[0]["frames"])
    assert [entry["type"] for entry in chain] == ["RuntimeError", "KeyError"]
    assert chain[0]["module"] == "builtins"
    assert frames[-1]["function"] == "raise_chained"
    assert all(set(frame) == {"module", "function", "line"} for frame in frames)
    bridged = [record for record in records if record["event"] == "thirdparty.record"]
    assert [record["logger"] for record in bridged] == [
        "vendor.lib",
        "vendor.lib",
        "py.warnings",
    ]
    assert [record["level"] for record in bridged] == ["ERROR", "WARNING", "WARNING"]
    assert "exception" not in bridged[0]
    bridged_chain = cast(list[dict[str, object]], bridged[1]["exception"])
    assert bridged_chain[0]["type"] == "OSError"
    assert str(bridged[0]["message"]).startswith("a third-party")


def test_control_characters_are_escaped_and_long_strings_truncated(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    log.emit(NOTE, text="line\nbreak\x1b[31mred\x1b[0m\r\x7f")
    log.emit(NOTE, text="b" * 3000)
    pipe.reader.wait_for_lines(2)

    first, second = pipe.reader.records()
    assert first["text"] == "line\\x0abreak\\x1b[31mred\\x1b[0m\\x0d\\x7f"
    assert "\x1b" not in pipe.reader.lines()[0]
    assert str(second["text"]).endswith(TRUNCATION_MARKER)
    assert len(str(second["text"]).encode("utf-8")) <= 1024


def test_records_over_the_budget_are_rejected_and_counted(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    log.emit(NOTE, detail={f"k{i}": "v" * 1024 for i in range(64)})
    log.emit(NOTE, detail={"a": {"b": {"c": {"d": {"e": "too deep"}}}}})
    log.emit(NOTE, items=["x"] * 65)
    log.emit(NOTE, text="kept")
    diagnostics.shutdown()
    finished_output(None, pipe)

    events = [record["event"] for record in pipe.reader.records()]
    rejected = diagnostics.snapshot().rejected
    assert events.count("testing.note") == 1
    assert events.count("logging.record_rejected") == 1
    assert rejected[(TEST_COMPONENT, "INFO", LossReason.OVER_BUDGET.value)] == 3


def test_unknown_fields_and_wrong_types_are_rejected(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    log.emit(NOTE, surprise="value")
    log.emit(NOTE, count="not an integer")
    log.emit(NOTE, ratio=float("inf"))
    log.emit(NOTE, text="kept")
    diagnostics.shutdown()
    finished_output(None, pipe)

    events = [record["event"] for record in pipe.reader.records()]
    rejected = diagnostics.snapshot().rejected
    assert events.count("testing.note") == 1
    assert rejected[(TEST_COMPONENT, "INFO", LossReason.UNKNOWN_FIELD.value)] == 1
    assert rejected[(TEST_COMPONENT, "INFO", LossReason.INVALID_FIELD.value)] == 2


def test_registration_outside_the_bounds_fails_before_any_record(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()
    registrations = [
        diagnostics.register_secret(f"registered-value-{index:04d}")
        for index in range(256)
    ]

    with pytest.raises(SecretAdmissionError):
        diagnostics.register_secret("short")
    with pytest.raises(SecretAdmissionError):
        diagnostics.register_secret("one-too-many-registered-value")
    log.emit(NOTE, text="registered-value-0100 and short")
    diagnostics.shutdown()
    output = finished_output(None, pipe)
    for registration in registrations:
        registration.release()

    records = pipe.reader.records()
    snapshot = diagnostics.snapshot()
    assert snapshot.secret_admission_failures == 2
    assert "registered-value-0100" not in output
    assert "short" in output
    assert records[0]["event"] == "testing.note"
    assert records[-1]["event"] == "logging.loss_summary"
    assert records[-1]["secret_admission_failures"] == 2
    assert diagnostics.registry.count() == 0


def test_context_bound_outside_the_validated_scope_is_rejected(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    registration = diagnostics.register_secret(REGISTERED_VALUE)
    pipe.reader.start()

    tokens = structlog.contextvars.bind_contextvars(
        request_id=f"req {REGISTERED_VALUE}\x1b[31m", run_id=12345
    )
    try:
        log.emit(NOTE, text="forged-context")
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
    log.emit(NOTE, text="clean-context")
    diagnostics.shutdown()
    output = finished_output(None, pipe)
    registration.release()

    events = [record["event"] for record in pipe.reader.records()]
    rejected = diagnostics.snapshot().rejected
    assert REGISTERED_VALUE not in output
    assert "forged-context" not in output
    assert events.count("testing.note") == 1
    assert rejected[(TEST_COMPONENT, "INFO", LossReason.INVALID_CONTEXT.value)] == 1


def test_exception_frame_names_and_odd_types_go_through_the_policy(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    registration = diagnostics.register_secret(REGISTERED_VALUE)
    pipe.reader.start()
    renamed_globals: dict[str, object] = {"__name__": f"mod {REGISTERED_VALUE}\x1b[31m"}
    boom = types.FunctionType(raise_plain.__code__, renamed_globals)

    try:
        boom()
    except ValueError as error:
        log.emit(NOTE, exception=error)
    odd_type = type("Odd", (Exception,), {"__module__": 7})
    log.emit(NOTE, exception=odd_type("odd"))
    log.emit(NOTE, count=2**70)
    log.emit(NOTE, text="\udcff surrogate")
    diagnostics.shutdown()
    output = finished_output(None, pipe)
    registration.release()

    records = [r for r in pipe.reader.records() if r["event"] == "testing.note"]
    rejected = diagnostics.snapshot().rejected
    assert REGISTERED_VALUE not in output
    assert "\x1b" not in output
    assert len(records) == 3
    odd = cast(list[dict[str, object]], records[1]["exception"])
    assert odd[0]["type"] == "Odd"
    assert odd[0]["module"] == ""
    assert str(records[2]["text"]).endswith("surrogate")
    assert rejected[(TEST_COMPONENT, "INFO", LossReason.INVALID_FIELD.value)] == 1


def test_unregistered_event_specifications_are_rejected(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()
    rogue = EventSpec("testing.rogue", TEST_COMPONENT, Level.INFO, "rogue", {})

    log.emit(rogue)
    log.emit(NOTE, text="kept")
    diagnostics.shutdown()
    finished_output(None, pipe)

    events = [record["event"] for record in pipe.reader.records()]
    rejected = diagnostics.snapshot().rejected
    assert "testing.rogue" not in events
    assert events.count("testing.note") == 1
    assert rejected[(TEST_COMPONENT, "INFO", LossReason.UNKNOWN_EVENT.value)] == 1


@pytest.mark.asyncio
async def test_sql_parameters_and_statements_never_appear_in_diagnostics(
    configure: Configure, pipe: Pipe, database: Database
) -> None:
    settings = LoggingSettings(
        level=Level.DEBUG,
        component_levels={DB_COMPONENT: Level.DEBUG},
        third_party={"sqlalchemy": Level.DEBUG},
    )
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()

    async with database.transaction() as connection:
        await Seeder(connection).namespace(UNREGISTERED_MARKER)
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await Seeder(connection).namespace(UNREGISTERED_MARKER)
    async with database.read_transaction() as connection:
        found = await token_by_digest(connection, UNREGISTERED_MARKER)
    diagnostics.shutdown()
    output = finished_output(None, pipe)

    records = pipe.reader.records()
    assert found is None
    assert UNREGISTERED_MARKER not in output
    assert "INSERT INTO" not in output
    assert "SELECT" not in output
    failed = [
        record for record in records if record["event"] == "db.transaction.failed"
    ]
    assert len(failed) == 1
    assert failed[0]["dialect"] == database.dialect.value
    chain = cast(list[dict[str, object]], failed[0]["exception"])
    assert chain[0]["type"] == "IntegrityError"
    assert all(set(entry) <= {"type", "module", "frames"} for entry in chain)
    bridged = [record for record in records if record["event"] == "thirdparty.record"]
    assert bridged
    assert all(str(record["logger"]).startswith("sqlalchemy") for record in bridged)
    assert all(set(record) <= BRIDGED_RECORD_KEYS | {"exception"} for record in bridged)
