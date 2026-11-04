import asyncio
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType

import pytest

from kora.api.approval_compilation import (
    COMPLETED,
    CompiledApprovalRendering,
    compile_root_approval_rendering,
)
from kora.api.exchange import COMPONENT
from kora.cel import PathSet
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator, ValidationWork
from kora.conformance.capture import captured
from kora.ir.approval_compilation import ApprovalPrerequisite
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from tests.ir.approval_default_cases import gate
from tests.ir.approval_input_cases import BUILTINS, producer, unavailable_inputs
from tests.ir.inline_schema_cases import document_for
from tests.support.observability import FakeClock

AUTHORED: str = "response.decision == 'approve' && inputs.data.ok && params.ok"
PATTERN: str = "'private_literal'.matches('^private_pattern')"


def approval_document() -> GraphDocument:
    schema: JsonObject = {
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "description": "private_schema",
    }
    return document_for(
        contracts=BUILTINS,
        params=schema,
        nodes={
            "producer": producer(schema=schema),
            "default_gate": gate(),
            "typed_gate": gate(
                predicate=AUTHORED,
                inputs={
                    "data": {
                        "type": "StructuredOutput",
                        "from": "producer",
                        "output": "value",
                    }
                },
            ),
            "private_node": gate(
                predicate=PATTERN,
                source={"file": "private_source.py", "line": 1},
            ),
            "invalid": gate(predicate="params.ok == 0"),
            "invalid_default": gate({}),
            "syntax": gate(predicate="("),
            "context": gate(
                predicate="inputs.unavailable", inputs=unavailable_inputs()
            ),
            "schema": gate({"$ref": "https://example.invalid/private_schema"}),
        },
        bodies={"unused": {"nodes": {"body_only": gate()}}},
    )


@dataclass
class Submissions:
    checked_ids: list[int] = field(default_factory=list[int])
    charges: list[int] = field(default_factory=list[int])
    accepted: int = 0


@contextmanager
def observe_submissions(*, cancel_after_submit: bool = False) -> Generator[Submissions]:
    counts = Submissions()
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if frame.f_code is not EvaluationThread.submit.__code__:
            return
        if event == "call":
            work: object = frame.f_locals.get("work")
            charge: object = frame.f_locals.get("charge")
            assert isinstance(work, ValidationWork)
            assert type(charge) is int
            counts.checked_ids.append(id(work.checked))
            counts.charges.append(charge)
        elif event == "return" and isinstance(argument, asyncio.Future):
            counts.accepted += 1
            if cancel_after_submit:
                caller = asyncio.current_task()
                assert caller is not None
                assert caller.cancel()

    try:
        sys.setprofile(observe)
        yield counts
    finally:
        sys.setprofile(previous)


def assert_settled(evaluator: Evaluator, handles: int) -> None:
    snapshot = evaluator.snapshot()
    assert snapshot.handles == handles
    assert snapshot.outstanding == snapshot.bytes == snapshot.cache_entries == 0


async def compile_cases() -> None:
    document = approval_document()
    clock = FakeClock()
    evaluator = Evaluator(clock.now)
    names = ("default_gate", "typed_gate", "private_node")

    try:
        with captured(
            COMPONENT, LoggingSettings(level=Level.DEBUG), "test", clock.now
        ) as logs:
            with observe_submissions() as submissions:
                results = [
                    await compile_root_approval_rendering(document, name, evaluator)
                    for name in names
                ]
            records = [
                record for record in logs.finish() if record["event"] == COMPLETED.name
            ]
        assert_settled(evaluator, 1)
    finally:
        await evaluator.close()

    assert all(isinstance(result, CompiledApprovalRendering) for result in results)
    facts = [
        result.approval
        for result in results
        if isinstance(result, CompiledApprovalRendering)
    ]
    assert [fact.path for fact in facts] == [
        ("graph", "nodes", name, "config", "approve_when") for name in names
    ]
    default, typed, pattern = [fact.checked for fact in facts]
    assert default.rendering == '(response.decision == "approve")'
    assert (
        typed.rendering
        == '(((response.decision == "approve") && inputs.data.ok) && params.ok)'
    )
    assert typed.projections == {
        "response": PathSet((".decision",)),
        "inputs": PathSet((".data.ok",)),
        "params": PathSet((".ok",)),
    }
    assert pattern.rendering == '"private_literal".matches("^private_pattern")'
    assert submissions.accepted == 3
    assert submissions.checked_ids == [id(fact.checked) for fact in facts]
    assert submissions.charges == [
        len(fact.checked.rendering) * 4
        + fact.checked.bound_bytes
        + fact.checked.projection_bytes
        for fact in facts
    ]
    assert [record["status"] for record in records] == ["rendering_compiled"] * 3
    assert all(
        record["level"] == "DEBUG" and record["component"] == COMPONENT
        for record in records
    )
    assert b"private_" not in logs.reader.data
    assert_settled(evaluator, 0)


async def refuse_cases() -> None:
    document = approval_document()
    evaluator = Evaluator(FakeClock().now)
    sites = (
        "syntax",
        "context",
        "schema",
        "body_only",
        "producer",
        "invalid",
        "invalid_default",
    )

    try:
        with observe_submissions() as static:
            results = [
                await compile_root_approval_rendering(document, name, evaluator)
                for name in sites
            ]
    finally:
        await evaluator.close()
    with (
        observe_submissions() as closed,
        pytest.raises(EvaluatorUnavailable) as unavailable,
    ):
        await compile_root_approval_rendering(document, "default_gate", evaluator)

    assert results[:5] == [
        ApprovalPrerequisite.SYNTAX_UNRESOLVED,
        ApprovalPrerequisite.CONTEXT_UNRESOLVED,
        ApprovalPrerequisite.SCHEMA_UNRESOLVED,
        ApprovalPrerequisite.SITE_UNAVAILABLE,
        ApprovalPrerequisite.SITE_UNAVAILABLE,
    ]
    for name, result in zip(sites[5:], results[5:], strict=True):
        assert isinstance(result, DiagnosticCollection)
        assert len(result.errors) == 1
        assert result.errors[0].code == "IR011"
        assert result.errors[0].path == (
            "graph",
            "nodes",
            name,
            "config",
            "approve_when",
        )
    assert static.checked_ids == []
    assert static.accepted == closed.accepted == 0
    assert len(closed.checked_ids) == 1
    assert unavailable.value.reason is UnavailableReason.CLOSED
    assert_settled(evaluator, 0)


async def cancel_case() -> None:
    document = approval_document()
    clock = FakeClock()
    evaluator = Evaluator(clock.now)

    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "test", clock.now
    ) as logs:
        try:
            with observe_submissions(cancel_after_submit=True) as submissions:
                task = asyncio.create_task(
                    compile_root_approval_rendering(document, "typed_gate", evaluator)
                )
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            await evaluator.close()
        records = logs.finish()

    assert submissions.accepted == len(submissions.checked_ids) == 1
    assert task.cancelled()
    assert all(record["event"] != COMPLETED.name for record in records)
    assert_settled(evaluator, 0)


def main() -> None:
    probe = {"compile": compile_cases, "refuse": refuse_cases, "cancel": cancel_case}
    asyncio.run(probe[sys.argv[2]]())
