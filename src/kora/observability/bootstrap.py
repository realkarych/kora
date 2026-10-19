import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import structlog
import structlog.contextvars
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

from kora.observability.catalog import (
    CATALOG,
    CONTEXT_KEYS,
    LOGGING_COMPONENT,
    LOGGING_CONFIGURED,
    LOSS_SUMMARY,
    RECORD_REJECTED,
    RECORDS_DROPPED,
    SCHEMA_VERSION,
    SINK_RECOVERED,
    THIRD_PARTY_COMPONENT,
    THIRD_PARTY_RECORD,
    CatalogError,
    EventSpec,
    LossReason,
)
from kora.observability.context import IDENTIFIER, validate_context
from kora.observability.policy import ContentPolicy, RecordRejected, SafeValue
from kora.observability.redaction import (
    SecretAdmissionError,
    SecretRegistration,
    SecretRegistry,
)
from kora.observability.render import render
from kora.observability.settings import Level, LoggingSettings, is_dotted_name
from kora.observability.sink import (
    BoundedQueue,
    DiagnosticsSnapshot,
    DiagnosticsState,
    StreamWriter,
)

Clock = Callable[[], datetime]

SUPPRESSION_WINDOW_SECONDS: float = 60.0
MAX_BRIDGED_LOGGERS: int = 256
PAYLOAD_KEY: str = "kora"
STDERR_DESCRIPTOR: int = 2


class AlreadyConfiguredError(RuntimeError):
    pass


class Emission:
    def __init__(
        self,
        spec: EventSpec,
        level: Level,
        component: str,
        logger_name: str,
        fields: Mapping[str, object],
    ) -> None:
        self.spec = spec
        self.level = level
        self.component = component
        self.logger_name = logger_name
        self.fields = fields


class QueueLogger:
    def __init__(self, deliver: Callable[[bytes], None]) -> None:
        self._deliver = deliver

    def debug(self, rendered: bytes) -> None:
        self._deliver(rendered)

    def info(self, rendered: bytes) -> None:
        self._deliver(rendered)

    def warning(self, rendered: bytes) -> None:
        self._deliver(rendered)

    def error(self, rendered: bytes) -> None:
        self._deliver(rendered)

    def critical(self, rendered: bytes) -> None:
        self._deliver(rendered)


class IncidentTracker:
    def __init__(self, clock: Clock, window_seconds: float) -> None:
        self._clock = clock
        self._window = window_seconds
        self._lock = threading.Lock()
        self._last_emitted: dict[tuple[str, str, str], datetime] = {}
        self._suppressed: dict[tuple[str, str, str], int] = {}

    def admit(self, key: tuple[str, str, str]) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            last = self._last_emitted.get(key)
            if last is not None and (now - last).total_seconds() < self._window:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False, 0
            self._last_emitted[key] = now
            return True, self._suppressed.pop(key, 0)


class ComponentLogger:
    def __init__(
        self,
        diagnostics: "ProcessDiagnostics",
        component: str,
        logger_name: str,
        threshold: Level,
        bound: FilteringBoundLogger,
    ) -> None:
        self._diagnostics = diagnostics
        self.component = component
        self.logger_name = logger_name
        self._threshold = threshold
        self._bound = bound

    def is_enabled(self, level: Level) -> bool:
        return level >= self._threshold

    def emit(self, spec: EventSpec, **fields: object) -> None:
        self.emit_at(spec.level, spec, **fields)

    def emit_at(self, level: Level, spec: EventSpec, **fields: object) -> None:
        if not self.is_enabled(level):
            return
        emission = Emission(spec, level, self.component, self.logger_name, fields)
        if CATALOG.get(spec.name) != spec:
            self._diagnostics.reject(emission, LossReason.UNKNOWN_EVENT)
            return
        self._bound.log(int(level), spec.name, **{PAYLOAD_KEY: emission})

    def bind(self, **context: str | int) -> "ComponentLogger":
        bound = self._bound.bind(**validate_context(context))
        return ComponentLogger(
            self._diagnostics, self.component, self.logger_name, self._threshold, bound
        )


class BridgeHandler(logging.Handler):
    def __init__(self, diagnostics: "ProcessDiagnostics", admitted: list[str]) -> None:
        super().__init__(level=logging.DEBUG)
        self._diagnostics = diagnostics
        self._admitted = admitted
        self._loggers: dict[str, ComponentLogger] = {}
        self._lock = threading.Lock()

    def is_admitted(self, name: str) -> bool:
        return any(
            name == prefix or name.startswith(f"{prefix}.") for prefix in self._admitted
        )

    def filter(self, record: logging.LogRecord) -> bool:
        return self.is_admitted(record.name)

    def bridged_logger(self, name: str) -> ComponentLogger:
        with self._lock:
            logger = self._loggers.get(name)
            if logger is None:
                if len(self._loggers) >= MAX_BRIDGED_LOGGERS:
                    self._loggers.clear()
                logger = self._diagnostics.logger(
                    THIRD_PARTY_COMPONENT, name=name, threshold=Level.DEBUG
                )
                self._loggers[name] = logger
            return logger

    def emit(self, record: logging.LogRecord) -> None:
        name = getattr(record, "name", None)
        if not isinstance(name, str) or not is_dotted_name(name):
            self._diagnostics.state.count_rejected(
                THIRD_PARTY_COMPONENT, Level.DEBUG, LossReason.INVALID_FIELD
            )
            return
        level = level_from_number(record.levelno)
        fields: dict[str, object] = {}
        if record.exc_info and isinstance(record.exc_info[1], BaseException):
            fields["exception"] = record.exc_info[1]
        self.bridged_logger(record.name).emit_at(level, THIRD_PARTY_RECORD, **fields)

    def handleError(self, record: logging.LogRecord) -> None:
        self._diagnostics.state.count_rejected(
            THIRD_PARTY_COMPONENT,
            level_from_number(record.levelno),
            LossReason.INVALID_FIELD,
        )

    def flush(self) -> None:
        return None


def level_from_number(number: int) -> Level:
    for level in reversed(Level):
        if number >= level:
            return level
    return Level.DEBUG


def format_timestamp(moment: datetime) -> str:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return (
        aware.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


class ProcessDiagnostics:
    def __init__(
        self,
        component: str,
        settings: LoggingSettings,
        instance_id: str,
        clock: Clock,
        descriptor: int,
    ) -> None:
        self.component = component
        self.settings = settings
        self.instance_id = instance_id
        self.clock = clock
        self.state = DiagnosticsState()
        self.registry = SecretRegistry()
        self._policy = ContentPolicy(self.registry, settings.max_record_bytes)
        self._queue = BoundedQueue(
            settings.queue_records, settings.queue_bytes, self.state
        )
        self._writer = StreamWriter(descriptor, self._queue, self.state)
        self._incidents = IncidentTracker(clock, SUPPRESSION_WINDOW_SECONDS)
        self._reentrancy = threading.local()
        self._pending = threading.local()
        self._loss_pending = False
        self._dropped_since_recovery = 0
        self._loss_lock = threading.Lock()
        self._internal = self.logger(LOGGING_COMPONENT)
        self._handler: BridgeHandler | None = None
        self._previous_root_level: int | None = None
        self._previous_logger_levels: dict[str, int] = {}
        self._active = True

    def logger(
        self, component: str, name: str | None = None, threshold: Level | None = None
    ) -> ComponentLogger:
        if component not in CATALOG.components():
            raise CatalogError("loggers belong to registered components")
        logger_name = name if name is not None else f"kora.{component}"
        if not is_dotted_name(logger_name):
            raise CatalogError("logger names are bounded dotted identifiers")
        level = (
            threshold if threshold is not None else self.settings.level_for(component)
        )
        bound: FilteringBoundLogger = structlog.wrap_logger(
            QueueLogger(self._deliver),
            processors=[
                structlog.contextvars.merge_contextvars,
                self._assemble,
                self._render,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(int(level)),
        )
        return ComponentLogger(self, component, logger_name, level, bound)

    def register_secret(self, value: str) -> SecretRegistration:
        try:
            return self.registry.register(value)
        except SecretAdmissionError:
            self.state.count_secret_admission_failure()
            raise

    def snapshot(self) -> DiagnosticsSnapshot:
        return self.state.snapshot()

    def _assemble(
        self, logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> EventDict:
        payload = event_dict.get(PAYLOAD_KEY)
        if not isinstance(payload, Emission):
            raise structlog.DropEvent
        emission: Emission = payload
        bound = {key: event_dict[key] for key in CONTEXT_KEYS if key in event_dict}
        try:
            fields = self._policy.safe_fields(emission.spec, emission.fields)
            context = self._policy.safe_context(bound)
        except RecordRejected as rejected:
            self.reject(emission, rejected.reason)
            raise structlog.DropEvent from None
        except Exception:
            self.reject(emission, LossReason.POLICY_FAILURE)
            raise structlog.DropEvent from None
        record: dict[str, SafeValue] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": format_timestamp(self.clock()),
            "level": emission.level.name,
            "event": emission.spec.name,
            "message": emission.spec.message,
            "component": emission.component,
            "logger": emission.logger_name,
            "instance_id": self.instance_id,
            **context,
            **fields,
        }
        return {"record": record, PAYLOAD_KEY: emission}

    def _render(
        self, logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> bytes:
        record = event_dict["record"]
        emission = event_dict[PAYLOAD_KEY]
        try:
            rendered = render(record, self.settings.render_format)
        except Exception:
            self.reject(emission, LossReason.POLICY_FAILURE)
            raise structlog.DropEvent from None
        if len(rendered) > self.settings.max_record_bytes:
            self.reject(emission, LossReason.OVER_BUDGET)
            raise structlog.DropEvent
        self._pending.emission = emission
        return rendered

    def _deliver(self, rendered: bytes) -> None:
        emission: Emission = self._pending.emission
        reason = self._queue.put(rendered)
        if reason is not None:
            self.state.count_dropped(emission.component, emission.level, reason)
            if reason is LossReason.SHUTDOWN or reason is LossReason.SINK_FAILED:
                return
            with self._loss_lock:
                self._loss_pending = True
                self._dropped_since_recovery += 1
            self._report_loss(RECORDS_DROPPED, emission, reason)
            return
        with self._loss_lock:
            recovered = self._loss_pending
            self._loss_pending = False
        if recovered:
            self._report_recovery(emission)

    def _report_recovery(self, emission: Emission) -> None:
        self.state.mark_sink_recovered()
        if self._entering():
            return
        try:
            key = (LOGGING_COMPONENT, SINK_RECOVERED.name, "")
            admitted, _ = self._incidents.admit(key)
            if not admitted:
                self.state.count_suppressed(
                    LOGGING_COMPONENT, SINK_RECOVERED.level, SINK_RECOVERED.name
                )
                return
            with self._loss_lock:
                dropped = self._dropped_since_recovery
                self._dropped_since_recovery = 0
            self._internal.emit(SINK_RECOVERED, dropped=dropped)
        finally:
            self._leave()

    def reject(self, emission: Emission, reason: LossReason) -> None:
        self.state.count_rejected(emission.component, emission.level, reason)
        self._report_loss(RECORD_REJECTED, emission, reason)

    def _report_loss(
        self, spec: EventSpec, emission: Emission, reason: LossReason
    ) -> None:
        if self._entering():
            return
        try:
            key = (emission.component, spec.name, reason.value)
            admitted, suppressed = self._incidents.admit(key)
            if not admitted:
                self.state.count_suppressed(
                    emission.component, emission.level, spec.name
                )
                return
            self._internal.emit(
                spec,
                reason=reason.value,
                source_component=emission.component,
                source_level=emission.level.name,
                suppressed=suppressed,
            )
        finally:
            self._leave()

    def _entering(self) -> bool:
        if getattr(self._reentrancy, "active", False):
            return True
        self._reentrancy.active = True
        return False

    def _leave(self) -> None:
        self._reentrancy.active = False

    def start(self) -> None:
        self._writer.start()
        self._install_bridge()
        self._internal.emit(
            LOGGING_CONFIGURED,
            configured_level=self.settings.level.name,
            render_format=self.settings.render_format.value,
            max_record_bytes=self.settings.max_record_bytes,
            queue_records=self.settings.queue_records,
            queue_bytes=self.settings.queue_bytes,
            drain_seconds=self.settings.drain_seconds,
        )

    def _install_bridge(self) -> None:
        levels = self.settings.third_party_levels()
        root = logging.getLogger()
        self._previous_root_level = root.level
        root.setLevel(logging.WARNING)
        for name, level in levels.items():
            logger = logging.getLogger(name)
            self._previous_logger_levels[name] = logger.level
            logger.setLevel(int(level))
        self._handler = BridgeHandler(self, sorted(levels))
        root.addHandler(self._handler)
        logging.raiseExceptions = False
        logging.captureWarnings(True)

    def _remove_bridge(self) -> None:
        root = logging.getLogger()
        if self._handler is not None:
            root.removeHandler(self._handler)
            self._handler.close()
            self._handler = None
        logging.captureWarnings(False)
        for name, level in self._previous_logger_levels.items():
            logging.getLogger(name).setLevel(level)
        if self._previous_root_level is not None:
            root.setLevel(self._previous_root_level)

    def is_active(self) -> bool:
        return self._active

    def shutdown(self, deadline_seconds: float | None = None) -> bool:
        if not self._active:
            return True
        self._active = False
        deadline = (
            deadline_seconds
            if deadline_seconds is not None
            else self.settings.drain_seconds
        )
        self._remove_bridge()
        summary = self.state.snapshot()
        lost = (
            summary.rejected_total()
            + summary.dropped_total()
            + summary.suppressed_total()
            + summary.secret_admission_failures
        )
        if lost and not summary.sink_failed:
            self._internal.emit(
                LOSS_SUMMARY,
                rejected=summary.rejected_total(),
                dropped=summary.dropped_total(),
                suppressed=summary.suppressed_total(),
                secret_admission_failures=summary.secret_admission_failures,
                sink_failures=summary.sink_failures,
            )
        self._queue.seal()
        drained = self._writer.stop(deadline)
        self._queue.close()
        release_current(self)
        return drained

    def writer_is_alive(self) -> bool:
        return self._writer.is_alive()


_current: ProcessDiagnostics | None = None
_current_LOCK: threading.Lock = threading.Lock()


def release_current(diagnostics: ProcessDiagnostics) -> None:
    global _current
    with _current_LOCK:
        if _current is diagnostics:
            _current = None


def configure_process(
    component: str,
    settings: LoggingSettings,
    instance_id: str,
    clock: Clock,
    descriptor: int = STDERR_DESCRIPTOR,
) -> ProcessDiagnostics:
    global _current
    if component not in CATALOG.components():
        raise CatalogError("the process component is registered before configuration")
    if not IDENTIFIER.match(instance_id):
        raise CatalogError("instance identifiers are bounded printable identifiers")
    with _current_LOCK:
        current = _current
        if current is not None:
            if current.settings == settings and current.component == component:
                return current
            raise AlreadyConfiguredError(
                "process diagnostics are configured once per process"
            )
        diagnostics = ProcessDiagnostics(
            component, settings, instance_id, clock, descriptor
        )
        _current = diagnostics
    diagnostics.start()
    return diagnostics


def current_diagnostics() -> ProcessDiagnostics | None:
    with _current_LOCK:
        return _current
