from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from kora.executor import CONTENT_REFUSED
from kora.ir.node import ErrorClass
from kora.observability import ComponentLogger


class Phase(StrEnum):
    CHECKOUT = "checkout"
    EXPORT = "export"
    SEAL = "seal"
    PREPARE = "prepare"
    INSPECT = "inspect"
    SCAN = "scan"
    PUBLISH = "publish"
    RECONCILE = "reconcile"
    ACQUIRE = "acquire"


@dataclass(frozen=True)
class StepPosition:
    phase: Phase
    index: int
    exit_code: int | None


class ExecutorError(Exception):
    def __init__(
        self,
        failure_class: ErrorClass,
        code: str,
        phase: Phase,
        position: StepPosition | None = None,
    ) -> None:
        super().__init__(code)
        self.failure_class = failure_class
        self.code = code
        self.phase = phase
        self.position = position

    def __str__(self) -> str:
        return f"{self.failure_class.value}: {self.code}"


def fail(
    failure_class: ErrorClass,
    code: str,
    phase: Phase,
    position: StepPosition | None = None,
) -> NoReturn:
    raise ExecutorError(failure_class, code, phase, position)


def invalid(code: str, phase: Phase) -> NoReturn:
    fail(ErrorClass.ARTIFACT_INVALID, code, phase)


def misconfigured(code: str, phase: Phase) -> NoReturn:
    fail(ErrorClass.CONFIG_ERROR, code, phase)


def unavailable(code: str, phase: Phase) -> NoReturn:
    fail(ErrorClass.INFRASTRUCTURE, code, phase)


def deadline_due(phase: Phase) -> NoReturn:
    fail(ErrorClass.TIMEOUT, "deadline_due", phase)


def content_refused(
    logger: ComponentLogger,
    failure_class: ErrorClass,
    code: str,
    cause: str,
    phase: Phase,
) -> ExecutorError:
    logger.emit(
        CONTENT_REFUSED,
        phase=phase.value,
        failure_class=failure_class.value,
        code=code,
        cause=cause,
        count=1,
    )
    return ExecutorError(failure_class, code, phase)
