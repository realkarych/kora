import sys
import weakref
from collections.abc import Sized
from dataclasses import dataclass, field, fields
from types import CodeType, FrameType, FunctionType
from typing import cast
from urllib.parse import urlsplit

import pytest
from jsonschema import ValidationError

from kora.ir.standard_schema_adapter import (
    ENGINE,
    ORDINALS,
    boolean_decision,
    evaluate,
    numerical_view,
)
from kora.ir.standard_schema_admission import call_certificate
from kora.ir.standard_schema_policy import SchemaAccounting, SchemaDomain
from kora.ir.standard_schema_source import Sources
from kora.ir.standard_schema_validation import validate_standard_schema
from kora.observability import Level
from kora.observability.context import bound_scope
from tests.ir.standard_schema_cases import binding, chain, source
from tests.ir.structured_schema_cases import HostileHeader
from tests.ir.test_standard_schema_source_lifetime import CachedParser, diagnostics_pipe
from tests.observability.conftest import wait_until


def engine_code(name: str) -> CodeType:
    method = cast("FunctionType", object.__getattribute__(ENGINE, name))
    return method.__code__


@dataclass
class ReleaseObservation:
    view_calls: int = 0
    history_slots: int = 0
    errors: list[weakref.ReferenceType[ValidationError]] = field(
        default_factory=list[weakref.ReferenceType[ValidationError]]
    )
    engines: list[weakref.ReferenceType[object]] = field(
        default_factory=list[weakref.ReferenceType[object]]
    )
    generators: list[weakref.ReferenceType[object]] = field(
        default_factory=list[weakref.ReferenceType[object]]
    )
    parser_inputs: set[str] = field(default_factory=set[str])
    engine_codes: tuple[CodeType, ...] = (
        engine_code("iter_errors"),
        engine_code("descend"),
    )
    parser_code: CodeType = cast(
        "FunctionType", object.__getattribute__(urlsplit, "__wrapped__")
    ).__code__

    def observe(self, frame: FrameType, event: str, argument: object) -> None:
        code = frame.f_code
        if code in self.engine_codes:
            if event == "call":
                self.engines.append(weakref.ref(frame.f_locals["self"]))
                live = {
                    id(engine): engine
                    for reference in self.engines
                    if (engine := reference()) is not None
                }
                slots = sum(
                    len(
                        cast(
                            "Sized",
                            object.__getattribute__(
                                object.__getattribute__(engine, "_resolver"),
                                "_previous",
                            ),
                        )
                    )
                    for engine in live.values()
                )
                self.history_slots = max(self.history_slots, slots)
            if event == "return" and isinstance(argument, ValidationError):
                assert not argument.context
                assert argument.parent is None
                assert argument.cause is None
                assert argument.__cause__ is None
                assert argument.__context__ is None
                self.errors.append(weakref.ref(argument))
        elif code is boolean_decision.__code__ and event == "return":
            errors = frame.f_locals["errors"]
            assert object.__getattribute__(errors, "gi_frame") is None
            self.generators.append(weakref.ref(errors))
        elif code is self.parser_code and event == "call":
            self.parser_inputs.add(cast("str", frame.f_locals["url"]))
        elif code is numerical_view.__code__ and event == "call":
            self.view_calls += 1


@pytest.mark.parametrize(
    ("schema", "outcome"),
    [
        ({"anyOf": [{"const": "absent"}, {"const": {"x": [0.3]}}]}, "valid"),
        ({"anyOf": [{"const": "absent"}, {"oneOf": [False, False]}]}, "invalid"),
        ({"not": {"anyOf": [False, False]}}, "valid"),
        ({"if": {"oneOf": [False, False]}, "else": {"anyOf": [False, True]}}, "valid"),
        (
            {"properties": {"x": {"contains": {"oneOf": [False, {"const": 0.3}]}}}},
            "valid",
        ),
        ({"properties": {"x": {"contains": {"anyOf": [False, False]}}}}, "invalid"),
        ({"oneOf": [True, True]}, "invalid"),
        ({"properties": {"x": {"uniqueItems": True}}}, "valid"),
        ({"not": {"pattern": "private"}}, "refused"),
        ({"not": {"const": [0] * 2048}}, "refused"),
        (chain(33), "refused"),
    ],
)
def test_standard_schema_errors_release_owned_values(
    schema: object, outcome: str
) -> None:
    marker = "".join(["private", "_value", "_marker"])
    value = {"x": [0.3], marker: marker}
    candidate = source(schema, metadata=marker)
    if type(schema) is dict and "anyOf" in cast("dict[str, object]", schema):
        value = {"x": [0.3]}
    before = (
        sys.getrefcount(value),
        sys.getrefcount(candidate),
        sys.getrefcount(schema),
        sys.getrefcount(marker),
    )
    observation = ReleaseObservation()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        result = validate_standard_schema(
            value, candidate, SchemaDomain.OUTPUT, binding()
        )
    finally:
        sys.setprofile(previous)
    after = (
        sys.getrefcount(value),
        sys.getrefcount(candidate),
        sys.getrefcount(schema),
        sys.getrefcount(marker),
    )

    assert result.outcome == outcome
    assert before == after
    assert all(reference() is None for reference in observation.errors)
    assert all(reference() is None for reference in observation.engines)
    assert all(reference() is None for reference in observation.generators)
    if outcome != "refused":
        assert observation.engines
        assert observation.generators
        assert observation.view_calls > 0
    else:
        assert not observation.engines
        assert observation.view_calls == 0
    if outcome == "invalid":
        assert observation.errors
    assert marker not in repr(result)
    assert observation.parser_inputs <= set(ORDINALS) | {""}


def test_standard_schema_original_pointer_never_enters_shared_parser_cache() -> None:
    marker = "".join(["private", "_pointer", "_marker"])
    leaf = {"type": "integer"}
    schema = {"$defs": {marker: leaf}, "$ref": "#/$defs/" + marker}
    candidate = source(schema, metadata=marker)
    observation = ReleaseObservation()
    before = (sys.getrefcount(marker), sys.getrefcount(leaf), sys.getrefcount(schema))
    cache = cast("CachedParser", urlsplit).cache_info()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        first = validate_standard_schema(1.0, candidate, SchemaDomain.OUTPUT, binding())
        second = validate_standard_schema(
            False, candidate, SchemaDomain.OUTPUT, binding()
        )
    finally:
        sys.setprofile(previous)

    assert first.outcome == "valid"
    assert second.outcome == "invalid"
    assert (
        sys.getrefcount(marker),
        sys.getrefcount(leaf),
        sys.getrefcount(schema),
    ) == before
    assert observation.parser_inputs <= set(ORDINALS) | {""}
    assert cache[2] == 128
    assert cast("CachedParser", urlsplit).cache_info()[3] <= 128
    assert all(reference() is None for reference in observation.errors)
    assert all(reference() is None for reference in observation.engines)


@pytest.mark.parametrize("position", ["value", "schema"])
def test_standard_schema_refusal_drops_original_exception_graph(position: str) -> None:
    rejected = HostileHeader()
    retained = weakref.ref(rejected)
    value: object = rejected if position == "value" else None
    candidate = source(True, metadata=rejected if position == "schema" else None)

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())
    del rejected
    del value
    del candidate

    assert result.outcome == "refused"
    assert retained() is None


def test_standard_schema_resolver_histories_are_bounded_and_released() -> None:
    candidate = source(chain(32))
    observation = ReleaseObservation()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        result = validate_standard_schema(
            None, candidate, SchemaDomain.OUTPUT, binding()
        )
    finally:
        sys.setprofile(previous)

    assert result.outcome == "valid"
    assert 31 <= observation.history_slots <= 32 * 32
    assert all(reference() is None for reference in observation.engines)
    assert observation.parser_inputs <= set(ORDINALS) | {""}


@dataclass
class TableObservation:
    position_entries: int = 0
    reference_entries: int = 0
    document_entries: int = 0
    graph_seen: bool = False
    registry_seen: bool = False

    def observe(self, frame: FrameType, event: str, argument: object) -> None:
        if frame.f_code is call_certificate.__code__:
            if "ready" not in frame.f_locals:
                return
        elif frame.f_code is not evaluate.__code__ or event != "return":
            return
        sources = frame.f_locals["sources"]
        assert isinstance(sources, Sources)
        positions, references = len(sources.sites), len(sources.references)
        edges = sum(len(site.edges) for site in sources.sites)
        position_entries = (
            2 * positions + 2 * len(sources.index) + 2 * (edges - references)
        )
        reference_entries = 4 * references
        if frame.f_code is call_certificate.__code__:
            parents = cast("list[list[tuple[int, int]]]", frame.f_locals["parents"])
            ready = cast("list[int]", frame.f_locals["ready"])
            position_entries += len(parents) + 2 * (edges - references) + len(ready)
            reference_entries += 2 * references
            self.graph_seen = True
        else:
            schemas = cast("list[object]", frame.f_locals["schemas"])
            registry = frame.f_locals["registry"]
            position_entries += (
                len(schemas)
                + 2 * len(cast("Sized", registry))
                + len(cast("Sized", object.__getattribute__(registry, "_uncrawled")))
            )
            assert len(cast("Sized", registry)) == positions
            self.registry_seen = True
        self.position_entries = max(self.position_entries, position_entries)
        self.reference_entries = max(self.reference_entries, reference_entries)
        self.document_entries = len(sources.documents) + len(sources.named)


def test_standard_schema_position_registry_and_edge_allocations() -> None:
    schema = {
        "$defs": {f"n{index}": {"type": "integer"} for index in range(24)},
        "allOf": [
            {"$ref": f"#/$defs/n{index}", "$dynamicRef": f"#/$defs/n{index}"}
            for index in range(24)
        ],
    }
    observation = TableObservation()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        result = validate_standard_schema(
            1.0, source(schema), SchemaDomain.OUTPUT, binding()
        )
    finally:
        sys.setprofile(previous)

    assert result.outcome == "valid"
    assert observation.graph_seen
    assert observation.registry_seen
    assert 0 < observation.position_entries <= 12 * result.accounting.schema_positions
    assert 0 < observation.reference_entries <= 8 * result.accounting.reference_edges
    assert observation.document_entries <= 4


@pytest.mark.parametrize(
    "outcome", ["valid", "invalid", "policy", "schema", "capacity"]
)
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
def test_standard_schema_diagnostics_exclude_content(
    outcome: str, level: Level, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "private_schema_validation_marker"
    value = {marker: marker}
    policy = binding()
    candidate = source(outcome != "invalid", metadata=marker)
    if outcome == "policy":
        policy["policy"] = marker
    elif outcome == "schema":
        candidate = source({"$ref": "https://example.invalid/" + marker})
    elif outcome == "capacity":
        value = {marker: "x" * 1048577}

    with diagnostics_pipe(level) as (diagnostics, pipe):
        pipe.reader.start()
        with bound_scope(request_id="value-check-request"):
            try:
                raise RuntimeError(marker)
            except RuntimeError:
                result = validate_standard_schema(
                    value, candidate, SchemaDomain.OUTPUT, policy
                )
        assert diagnostics.shutdown()
        pipe.close_write_end()
        pipe.reader.wait_closed()
        records = [
            record
            for record in pipe.reader.records()
            if record["event"] == "ir.standard_schema_validation.completed"
        ]
        output = pipe.reader.data

    assert result.outcome == (outcome if outcome in {"valid", "invalid"} else "refused")
    assert marker.encode() not in output
    assert marker not in repr(result)
    assert capsys.readouterr().out == ""
    assert len(records) == (1 if level is Level.DEBUG else 0)
    if records:
        record = records[0]
        assert record["component"] == "ir"
        assert record["request_id"] == "value-check-request"
        assert record["outcome"] == result.outcome
        for member in fields(SchemaAccounting):
            assert record[member.name] == getattr(result.accounting, member.name)
    if result.outcome == "refused":
        assert result.accounting == SchemaAccounting()


@pytest.mark.parametrize("broken", [False, True], ids=["blocked", "broken"])
def test_standard_schema_survives_unavailable_sink(broken: bool) -> None:
    candidate = source({"multipleOf": 0.1})
    policy = binding()
    expected = validate_standard_schema(0.3, candidate, SchemaDomain.OUTPUT, policy)

    with diagnostics_pipe(blocked=not broken) as (diagnostics, pipe):
        if broken:
            pipe.close_read_end()
        else:
            wait_until(lambda: diagnostics.snapshot().in_flight_bytes > 0)
        matches = all(
            validate_standard_schema(0.3, candidate, SchemaDomain.OUTPUT, policy)
            == expected
            for _ in range(256)
        )
        if broken:
            wait_until(lambda: diagnostics.snapshot().sink_failed)
        snapshot = diagnostics.snapshot()
        if not broken:
            pipe.reader.start()
        drained = diagnostics.shutdown()

    assert matches
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= 64
    assert snapshot.queue_bytes <= 65536
    assert drained
    assert not diagnostics.writer_is_alive()
