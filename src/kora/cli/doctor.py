import enum
import json
from dataclasses import dataclass

from kora.cli.app import CLI_COMPONENT
from kora.observability import CATALOG, EventSpec, FieldKind, Level

DOCTOR_CHECK_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        name="cli.doctor_check_finished",
        component=CLI_COMPONENT,
        level=Level.DEBUG,
        message="a doctor check finished",
        fields={
            "check": FieldKind.STRING,
            "status": FieldKind.STRING,
            "reason": FieldKind.STRING,
        },
    )
)
DOCTOR_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        name="cli.doctor_finished",
        component=CLI_COMPONENT,
        level=Level.INFO,
        message="doctor finished",
        fields={
            "passed": FieldKind.INTEGER,
            "failed": FieldKind.INTEGER,
            "exit_code": FieldKind.INTEGER,
        },
    )
)

DOCTOR_PASSED: int = 0
DOCTOR_FAILED: int = 1


class CheckName(enum.StrEnum):
    VERSION = "version"
    CEL = "cel"
    DATABASE = "database"
    STATE_ROOT = "state_root"


class CheckStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    name: CheckName
    status: CheckStatus
    reason: str | None
    detail: str | None

    @classmethod
    def passed(cls, name: CheckName, detail: str) -> "CheckResult":
        return cls(name, CheckStatus.PASSED, None, detail)

    @classmethod
    def failed(cls, name: CheckName, reason: str) -> "CheckResult":
        return cls(name, CheckStatus.FAILED, reason, None)

    def view(self) -> dict[str, str | None]:
        return {
            "check": self.name.value,
            "status": self.status.value,
            "reason": self.reason,
            "detail": self.detail,
        }

    def line(self) -> str:
        if self.status is CheckStatus.PASSED:
            return f"{self.name.value} passed {self.detail}"
        return f"{self.name.value} failed {self.reason}"


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def failed(self) -> int:
        return sum(1 for check in self.checks if check.status is CheckStatus.FAILED)

    @property
    def passed(self) -> int:
        return len(self.checks) - self.failed

    @property
    def exit_code(self) -> int:
        return DOCTOR_FAILED if self.failed else DOCTOR_PASSED

    def render(self, *, as_json: bool) -> str:
        if as_json:
            return json.dumps(
                {
                    "status": (
                        CheckStatus.FAILED if self.failed else CheckStatus.PASSED
                    ).value,
                    "checks": [check.view() for check in self.checks],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return "\n".join(check.line() for check in self.checks)
