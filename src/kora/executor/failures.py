from enum import StrEnum
from typing import NoReturn

from kora.ir.node import ErrorClass


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


class ExecutorError(Exception):
    def __init__(
        self,
        failure_class: ErrorClass,
        code: str,
        phase: Phase,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.failure_class = failure_class
        self.code = code
        self.phase = phase
        self.exit_code = exit_code

    def __str__(self) -> str:
        return f"{self.failure_class.value}: {self.code}"


def fail(failure_class: ErrorClass, code: str, phase: Phase) -> NoReturn:
    raise ExecutorError(failure_class, code, phase)


def invalid(code: str, phase: Phase) -> NoReturn:
    fail(ErrorClass.ARTIFACT_INVALID, code, phase)


def misconfigured(code: str, phase: Phase) -> NoReturn:
    fail(ErrorClass.CONFIG_ERROR, code, phase)


def unavailable(code: str, phase: Phase) -> NoReturn:
    fail(ErrorClass.INFRASTRUCTURE, code, phase)


def deadline_due(phase: Phase) -> NoReturn:
    fail(ErrorClass.TIMEOUT, "deadline_due", phase)
