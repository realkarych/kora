from typing import Annotated, Literal
from xml.parsers import expat

from pydantic import Field, StrictBool, StrictInt

from kora.conformance.capture import MAX_CAPTURE_BYTES
from kora.protocols.common import BoundaryModel
from kora.protocols.metadata import ProtocolName


class InputFiles(BoundaryModel):
    configuration: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    fixture_data: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class ChildInput(BoundaryModel):
    protocol: ProtocolName
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
    fixtures: Annotated[
        str, Field(max_length=255, pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
    ]
    files: InputFiles | None = None


class ChildResult(BoundaryModel):
    phase: Literal["bootstrap", "ready", "pytest", "storage", "complete"]
    expected: Annotated[StrictInt, Field(ge=0, le=18)] = 0
    completed: Annotated[StrictInt, Field(ge=0, le=18)] = 0
    pytest_status: Annotated[StrictInt, Field(ge=0, le=5)] | None = None
    wrapper_closed: StrictBool = False
    report_accepted: StrictBool = False

    @property
    def accepted(self) -> bool:
        return (
            self.phase == "complete"
            and self.expected > 0
            and self.completed == self.expected
            and self.pytest_status == 0
            and self.wrapper_closed
            and self.report_accepted
        )


class ReportRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("conformance_report_refused")


def require_report(condition: bool) -> None:
    if not condition:
        raise ReportRefused


def accepted_report(data: bytes, discarded: int) -> bool:
    stack: list[str] = []
    seen: list[str] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        expected = ("testsuites", "testsuite", "testcase")
        require_report(len(seen) < 3 and name == expected[len(seen)])
        require_report(tuple(stack) == expected[: len(seen)])
        if name == "testsuite":
            require_report(
                all(
                    attributes.get(key) == value
                    for key, value in (
                        ("tests", "1"),
                        ("errors", "0"),
                        ("failures", "0"),
                        ("skipped", "0"),
                    )
                )
            )
        if name == "testcase":
            require_report(attributes.get("classname") == "command_case")
            require_report(attributes.get("name") == "test_configured_provider")
        seen.append(name)
        stack.append(name)

    def end(name: str) -> None:
        require_report(bool(stack) and stack.pop() == name)

    def text(value: str) -> None:
        require_report(not value.strip())

    def doctype(name: str, system: str | None, public: str | None, subset: int) -> None:
        raise ReportRefused

    try:
        require_report(bool(data) and len(data) <= MAX_CAPTURE_BYTES and discarded == 0)
        document = data.decode("utf-8", errors="strict")
        parser = expat.ParserCreate(encoding="utf-8")
        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = text
        parser.StartDoctypeDeclHandler = doctype
        parser.Parse(document, True)
        require_report(seen == ["testsuites", "testsuite", "testcase"] and not stack)
    except (ReportRefused, UnicodeDecodeError, expat.ExpatError):
        return False
    return True
