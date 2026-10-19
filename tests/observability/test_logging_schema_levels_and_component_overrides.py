import logging
import os

import pytest

from kora.observability import (
    AlreadyConfiguredError,
    Level,
    LoggingSettings,
    RenderFormat,
)
from tests.observability.conftest import (
    LEVEL_EVENTS,
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    run_kora,
)

SCHEMA_FIELDS: frozenset[str] = frozenset(
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
CONFIGURATION_PREFIX: str = "kora: invalid logging configuration: "


def test_default_level_keeps_stderr_silent() -> None:
    result = run_kora(["--version"], {})

    assert result.code == 0
    assert result.stdout == b"kora 0.1.0.dev0\n"
    assert result.stderr == b""


def test_debug_level_emits_schema_records_on_stderr_only() -> None:
    result = run_kora(["--version"], {"KORA_LOG_LEVEL": "DEBUG"})

    records = result.records()
    events = [record["event"] for record in records]
    assert result.code == 0
    assert result.stdout == b"kora 0.1.0.dev0\n"
    assert events == ["logging.configured", "process.started", "process.stopped"]
    assert all(SCHEMA_FIELDS <= record.keys() for record in records)
    assert all(record["schema_version"] == 1 for record in records)
    assert all(str(record["timestamp"]).endswith("Z") for record in records)
    assert len({record["instance_id"] for record in records}) == 1
    assert records[-1]["exit_code"] == 0
    assert records[1]["component"] == "cli"
    assert records[1]["logger"] == "kora.cli"


def test_component_override_enables_one_component() -> None:
    result = run_kora(["--version"], {"KORA_LOG_COMPONENT_LEVELS": "cli=DEBUG"})

    events = [record["event"] for record in result.records()]
    assert result.code == 0
    assert events == ["process.started", "process.stopped"]


def test_text_format_renders_the_same_sanitized_record() -> None:
    result = run_kora(
        ["--version"], {"KORA_LOG_LEVEL": "DEBUG", "KORA_LOG_FORMAT": "text"}
    )

    lines = result.stderr_lines()
    assert result.code == 0
    assert lines[1].split(" ")[1:4] == ["DEBUG", "cli", "process.started"]
    assert lines[2].endswith("process stopped exit_code=0")


def test_every_level_renders_with_its_name(configure: Configure, pipe: Pipe) -> None:
    diagnostics = configure(LoggingSettings(level=Level.DEBUG), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    for spec in LEVEL_EVENTS.values():
        log.emit(spec, text="note")
    pipe.reader.wait_for_lines(len(LEVEL_EVENTS) + 1)

    levels = [record["level"] for record in pipe.reader.records()[1:]]
    assert levels == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def test_component_threshold_filters_before_processing(
    configure: Configure, pipe: Pipe
) -> None:
    settings = LoggingSettings(level=Level.WARNING)
    diagnostics = configure(settings, pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    log.emit(LEVEL_EVENTS[Level.INFO], text="hidden")
    log.emit(LEVEL_EVENTS[Level.ERROR], text="shown")
    diagnostics.shutdown()
    pipe.close_write_end()
    pipe.reader.wait_closed()

    events = [record["event"] for record in pipe.reader.records()]
    assert events == ["testing.error"]
    assert not log.is_enabled(Level.INFO)


@pytest.mark.parametrize(
    ("environment", "variable", "hidden"),
    [
        ({"KORA_LOG_LEVEL": "verbose"}, "KORA_LOG_LEVEL", "verbose"),
        (
            {"KORA_LOG_COMPONENT_LEVELS": "unlisted=DEBUG"},
            "KORA_LOG_COMPONENT_LEVELS",
            "unlisted",
        ),
        ({"KORA_LOG_QUEUE_RECORDS": "7"}, "KORA_LOG_QUEUE_RECORDS", "7"),
        (
            {"KORA_LOG_MAX_RECORD_BYTES": "1048576", "KORA_LOG_QUEUE_BYTES": "65536"},
            "KORA_LOG_MAX_RECORD_BYTES",
            "1048576",
        ),
        ({"KORA_LOG_DRAIN_SECONDS": "2"}, "KORA_LOG_DRAIN_SECONDS", "2"),
        ({"KORA_LOG_FORMAT": "yaml"}, "KORA_LOG_FORMAT", "yaml"),
    ],
)
def test_invalid_settings_exit_with_one_content_free_line(
    environment: dict[str, str], variable: str, hidden: str
) -> None:
    result = run_kora(["--version"], environment)

    lines = result.stderr_lines()
    assert result.code == 78
    assert result.stdout == b""
    assert len(lines) == 1
    assert lines[0].startswith(f"{CONFIGURATION_PREFIX}{variable}: ")
    assert hidden not in lines[0].removeprefix(f"{CONFIGURATION_PREFIX}{variable}: ")


def test_invalid_level_line_is_exact() -> None:
    result = run_kora(["--version"], {"KORA_LOG_LEVEL": "verbose"})

    assert result.stderr == (
        b"kora: invalid logging configuration: KORA_LOG_LEVEL: "
        b"expected one of DEBUG, INFO, WARNING, ERROR, CRITICAL\n"
    )


def test_third_party_logger_is_bridged_only_when_allowlisted(
    configure: Configure, pipe: Pipe
) -> None:
    settings = LoggingSettings(third_party={"vendor": Level.INFO})
    diagnostics = configure(settings, pipe.write_end)
    pipe.reader.start()

    logging.getLogger("vendor.lib").info("marker-one")
    logging.getLogger("other.lib").warning("marker-two")
    logging.getLogger("vendor.lib").debug("marker-three")
    diagnostics.shutdown()
    pipe.close_write_end()
    pipe.reader.wait_closed()

    records = pipe.reader.records()
    assert [record["event"] for record in records] == ["thirdparty.record"]
    assert records[0]["logger"] == "vendor.lib"
    assert records[0]["level"] == "INFO"
    assert records[0]["component"] == "thirdparty"
    assert "marker" not in pipe.reader.lines()[0]


def test_configuring_twice_keeps_one_handler_and_refuses_other_settings(
    configure: Configure, pipe: Pipe
) -> None:
    settings = LoggingSettings()
    diagnostics = configure(settings, pipe.write_end)
    handlers_before = list(logging.getLogger().handlers)

    again = configure(LoggingSettings(), pipe.write_end)

    assert again is diagnostics
    assert logging.getLogger().handlers == handlers_before
    assert len([h for h in handlers_before if type(h).__name__ == "BridgeHandler"]) == 1
    with pytest.raises(AlreadyConfiguredError):
        configure(LoggingSettings(render_format=RenderFormat.TEXT), pipe.write_end)


def test_shutdown_leaves_the_process_unconfigured(
    configure: Configure, pipe: Pipe
) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    root_level = logging.getLogger().level

    diagnostics.shutdown()
    fresh = configure(LoggingSettings(render_format=RenderFormat.TEXT), pipe.write_end)

    assert fresh is not diagnostics
    assert fresh.writer_is_alive()
    assert not diagnostics.writer_is_alive()
    assert logging.getLogger().level == root_level


def test_bare_invocation_and_usage_errors_exit_two() -> None:
    bare = run_kora([], {})
    unknown = run_kora(["--nope"], {})

    assert bare.code == 2
    assert bare.stdout == b""
    assert bare.stderr_lines()[0].startswith("Usage: kora")
    assert bare.stderr_lines()[-1] == "Error: a command or --version is required"
    assert unknown.code == 2
    assert unknown.stdout == b""
    assert unknown.stderr_lines()[-1] == "Error: No such option: --nope"


def test_note_fields_of_every_kind_render(configure: Configure, pipe: Pipe) -> None:
    diagnostics = configure(LoggingSettings(), pipe.write_end)
    log = diagnostics.logger(TEST_COMPONENT)
    pipe.reader.start()

    log.emit(
        NOTE,
        text="plain",
        items=["one", "two"],
        detail={"nested": {"count": 2, "names": ["a"]}},
        count=3,
        ratio=0.5,
        flag=True,
    )
    pipe.reader.wait_for_lines(1)

    record = pipe.reader.records()[0]
    assert record["text"] == "plain"
    assert record["items"] == ["one", "two"]
    assert record["detail"] == {"nested": {"count": 2, "names": ["a"]}}
    assert record["count"] == 3
    assert record["ratio"] == 0.5
    assert record["flag"] is True
    assert os.linesep == "\n"
