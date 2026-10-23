import enum
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

LEVEL_VARIABLE: str = "KORA_LOG_LEVEL"
COMPONENT_LEVELS_VARIABLE: str = "KORA_LOG_COMPONENT_LEVELS"
THIRD_PARTY_VARIABLE: str = "KORA_LOG_THIRD_PARTY"
FORMAT_VARIABLE: str = "KORA_LOG_FORMAT"
MAX_RECORD_BYTES_VARIABLE: str = "KORA_LOG_MAX_RECORD_BYTES"
QUEUE_RECORDS_VARIABLE: str = "KORA_LOG_QUEUE_RECORDS"
QUEUE_BYTES_VARIABLE: str = "KORA_LOG_QUEUE_BYTES"
DRAIN_SECONDS_VARIABLE: str = "KORA_LOG_DRAIN_SECONDS"

MIN_RECORD_BYTES: int = 4096
MAX_RECORD_BYTES: int = 1_048_576
MIN_QUEUE_RECORDS: int = 16
MAX_QUEUE_RECORDS: int = 65_536
MIN_QUEUE_BYTES: int = 65_536
MAX_QUEUE_BYTES: int = 268_435_456
MAX_DRAIN_SECONDS: float = 1.0
RECORD_TO_QUEUE_RATIO: int = 4

MAX_STRING_BYTES: int = 1024
MAX_FIELDS: int = 32
MAX_COLLECTION_ITEMS: int = 64
MAX_NESTING: int = 4
MAX_VALUES: int = 256
MAX_FRAMES: int = 32
MAX_CHAINED_EXCEPTIONS: int = 8
MAX_SECRETS: int = 256
MAX_SECRET_BYTES: int = 65_536
MAX_SECRET_VALUE_BYTES: int = 4096
MIN_SECRET_LENGTH: int = 8
MAX_NAME_BYTES: int = 128
DEFAULT_THIRD_PARTY_LEVEL_NAME: str = "WARNING"
WARNINGS_LOGGER: str = "py.warnings"

DOTTED_NAME: re.Pattern[str] = re.compile(
    r"\A[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)


class Level(enum.IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


LEVEL_NAMES: str = ", ".join(level.name for level in Level)


class RenderFormat(enum.StrEnum):
    JSON = "json"
    TEXT = "text"


class LoggingConfigurationError(ValueError):
    def __init__(self, variable: str, reason: str) -> None:
        super().__init__(f"{variable}: {reason}")
        self.variable = variable
        self.reason = reason


def new_levels() -> dict[str, Level]:
    return {}


@dataclass(frozen=True)
class LoggingSettings:
    level: Level = Level.INFO
    component_levels: Mapping[str, Level] = field(default_factory=new_levels)
    third_party: Mapping[str, Level] = field(default_factory=new_levels)
    render_format: RenderFormat = RenderFormat.JSON
    max_record_bytes: int = 16_384
    queue_records: int = 1024
    queue_bytes: int = 4_194_304
    drain_seconds: float = 1.0

    def level_for(self, component: str) -> Level:
        return self.component_levels.get(component, self.level)

    def third_party_levels(self) -> dict[str, Level]:
        levels = dict(self.third_party)
        levels[WARNINGS_LOGGER] = Level.WARNING
        return levels


def is_dotted_name(name: str) -> bool:
    return len(name) <= MAX_NAME_BYTES and bool(DOTTED_NAME.match(name))


def parse_level(variable: str, value: str) -> Level:
    try:
        return Level[value.strip().upper()]
    except KeyError:
        raise LoggingConfigurationError(
            variable, f"expected one of {LEVEL_NAMES}"
        ) from None


def parse_name_levels(
    variable: str, value: str, default_level: Level | None
) -> dict[str, Level]:
    levels: dict[str, Level] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, level_text = item.partition("=")
        name = name.strip()
        if not is_dotted_name(name):
            raise LoggingConfigurationError(
                variable, "expected name=LEVEL pairs separated by commas"
            )
        if separator:
            levels[name] = parse_level(variable, level_text)
        elif default_level is None:
            raise LoggingConfigurationError(
                variable, "expected name=LEVEL pairs separated by commas"
            )
        else:
            levels[name] = default_level
    return levels


def parse_integer(variable: str, value: str, lower: int, upper: int) -> int:
    reason = f"expected an integer between {lower} and {upper}"
    try:
        parsed = int(value.strip())
    except ValueError:
        raise LoggingConfigurationError(variable, reason) from None
    if parsed < lower or parsed > upper:
        raise LoggingConfigurationError(variable, reason)
    return parsed


def parse_drain(variable: str, value: str) -> float:
    reason = f"expected a number between 0 and {MAX_DRAIN_SECONDS:g}"
    try:
        parsed = float(value.strip())
    except ValueError:
        raise LoggingConfigurationError(variable, reason) from None
    if parsed != parsed or parsed < 0 or parsed > MAX_DRAIN_SECONDS:
        raise LoggingConfigurationError(variable, reason)
    return parsed


def parse_format(variable: str, value: str) -> RenderFormat:
    try:
        return RenderFormat(value.strip().lower())
    except ValueError:
        choices = ", ".join(item.value for item in RenderFormat)
        raise LoggingConfigurationError(
            variable, f"expected one of {choices}"
        ) from None


def read_logging_settings(
    environ: Mapping[str, str], registered_components: Collection[str]
) -> LoggingSettings:
    defaults = LoggingSettings()
    level = defaults.level
    if LEVEL_VARIABLE in environ:
        level = parse_level(LEVEL_VARIABLE, environ[LEVEL_VARIABLE])
    component_levels = parse_name_levels(
        COMPONENT_LEVELS_VARIABLE, environ.get(COMPONENT_LEVELS_VARIABLE, ""), None
    )
    for component in component_levels:
        if component not in registered_components:
            raise LoggingConfigurationError(
                COMPONENT_LEVELS_VARIABLE, "unknown component"
            )
    third_party = parse_name_levels(
        THIRD_PARTY_VARIABLE, environ.get(THIRD_PARTY_VARIABLE, ""), Level.WARNING
    )
    render_format = defaults.render_format
    if FORMAT_VARIABLE in environ:
        render_format = parse_format(FORMAT_VARIABLE, environ[FORMAT_VARIABLE])
    max_record_bytes = defaults.max_record_bytes
    if MAX_RECORD_BYTES_VARIABLE in environ:
        max_record_bytes = parse_integer(
            MAX_RECORD_BYTES_VARIABLE,
            environ[MAX_RECORD_BYTES_VARIABLE],
            MIN_RECORD_BYTES,
            MAX_RECORD_BYTES,
        )
    queue_records = defaults.queue_records
    if QUEUE_RECORDS_VARIABLE in environ:
        queue_records = parse_integer(
            QUEUE_RECORDS_VARIABLE,
            environ[QUEUE_RECORDS_VARIABLE],
            MIN_QUEUE_RECORDS,
            MAX_QUEUE_RECORDS,
        )
    queue_bytes = defaults.queue_bytes
    if QUEUE_BYTES_VARIABLE in environ:
        queue_bytes = parse_integer(
            QUEUE_BYTES_VARIABLE,
            environ[QUEUE_BYTES_VARIABLE],
            MIN_QUEUE_BYTES,
            MAX_QUEUE_BYTES,
        )
    if max_record_bytes * RECORD_TO_QUEUE_RATIO > queue_bytes:
        raise LoggingConfigurationError(
            MAX_RECORD_BYTES_VARIABLE,
            f"must be at most a quarter of {QUEUE_BYTES_VARIABLE}",
        )
    drain_seconds = defaults.drain_seconds
    if DRAIN_SECONDS_VARIABLE in environ:
        drain_seconds = parse_drain(
            DRAIN_SECONDS_VARIABLE, environ[DRAIN_SECONDS_VARIABLE]
        )
    return LoggingSettings(
        level=level,
        component_levels=component_levels,
        third_party=third_party,
        render_format=render_format,
        max_record_bytes=max_record_bytes,
        queue_records=queue_records,
        queue_bytes=queue_bytes,
        drain_seconds=drain_seconds,
    )
