from dataclasses import fields
from typing import cast

from kora.ir.structured_schema_input import (
    PreparedStructuredSchemaInput,
    SchemaArena,
    StructuredSchemaPreparationAccounting,
    StructuredSchemaPreparationInput,
    StructuredSchemaPreparationRefusal,
)
from kora.ir.structured_schema_locations import (
    SchemaLocations,
    decode_reference,
    matches,
)
from kora.ir.structured_schema_work import (
    PARAMETERS,
    PreparationMeter,
    PreparationRefused,
    Reason,
    merge_sort,
    refuse,
)
from kora.ir.type_declarations import COMPONENT
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COUNTS: tuple[str, ...] = tuple(
    field.name for field in fields(StructuredSchemaPreparationAccounting)
)
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.structured_schema_preparation.completed",
        COMPONENT,
        Level.DEBUG,
        "structured schema input preparation completed",
        dict.fromkeys(
            (
                "prepared",
                "unsupported_binding",
                "unsupported_schema",
                "preparation_exhausted",
            ),
            FieldKind.BOOLEAN,
        )
        | dict.fromkeys(COUNTS, FieldKind.INTEGER),
    )
)
RECORD_FIELDS = (
    "name",
    "builtin_revision",
    "parent",
    "behavior",
    "descriptor",
    "source",
    "schema",
)


def record(
    value: object,
    names: tuple[str, ...],
    meter: PreparationMeter,
    reason: Reason = "unsupported_schema",
) -> dict[str, object]:
    meter.reserve(2)
    if type(value) is not dict:
        refuse(reason)
    mapping = cast("dict[object, object]", value)
    meter.reserve()
    if len(mapping) != len(names):
        refuse(reason)
    for key in mapping:
        meter.reserve(2)
        if type(key) is not str or not matches(key, names, meter):
            refuse(reason)
    return cast("dict[str, object]", mapping)


class Preparation:
    def __init__(self) -> None:
        self.meter = PreparationMeter()
        self.arena = SchemaArena(self.meter)
        self.locations = SchemaLocations(self.arena)
        self.types: list[tuple[int, int]] = []
        self.contracts: list[int] = []
        self.documents: list[int] = []
        self.maximum_name = 0
        self.declared = self.execution = self.checker_binding = self.parameters = -1

    def required(self, node: int, name: str) -> int:
        result = self.arena.lookup(node, name)
        if result < 0:
            refuse()
        return result

    def lookup_type(self, query: str) -> int:
        self.meter.reserve(
            1 + len(self.types).bit_length() * (1 + len(query) + self.maximum_name)
        )
        low, high = 0, len(self.types)
        while low < high:
            middle = (low + high) // 2
            if cast("str", self.arena.nodes[self.types[middle][0]].value) < query:
                low = middle + 1
            else:
                high = middle
        if (
            low == len(self.types)
            or self.arena.nodes[self.types[low][0]].value != query
        ):
            refuse()
        return self.types[low][1]

    def source(self, node: int, kind: str) -> None:
        original = self.arena.at(node)
        if original.kind != "object" or len(original.members) != 3:
            refuse()
        if self.meter.compare(self.arena.text(self.required(node, "kind")), kind) != 0:
            refuse()
        path = self.arena.at(self.required(node, "path"))
        location = self.arena.at(self.required(node, "location"))
        if path.kind != "array" or (
            location.kind != "object"
            and (location.kind != "scalar" or location.value is not None)
        ):
            refuse()
        for segment in path.children:
            value = self.arena.at(segment).value
            self.meter.reserve(3)
            if type(value) is not str and (type(value) is not int or value < 0):
                refuse()

    def contract(self, original: dict[str, object], document: int) -> None:
        values: list[tuple[str, int]] = []
        for field, depth in (
            ("behavior", 4),
            ("builtin_revision", 0),
            ("descriptor", 4),
            ("name", 0),
            ("parent", 0),
        ):
            self.meter.reserve(2)
            values.append((field, self.arena.snapshot(original[field], max(1, depth))))
        values.append(("schema_document", self.arena.scalar(document)))
        node = self.arena.wrap_object(tuple(values), 3)
        behavior = self.arena.at(self.required(node, "behavior"))
        if behavior.kind != "object" or len(behavior.members) != 3:
            refuse()
        for field in ("validation", "digest_fields", "summary"):
            if not self.arena.text(
                self.required(self.required(node, "behavior"), field)
            ):
                refuse()
        parent_node = self.arena.at(self.required(node, "parent"))
        if parent_node.kind != "scalar":
            refuse()
        parent = parent_node.value
        if parent is not None and type(parent) is not str:
            refuse()
        revision_node = self.arena.at(self.required(node, "builtin_revision"))
        if revision_node.kind != "scalar":
            refuse()
        revision = revision_node.value
        descriptor = self.arena.at(self.required(node, "descriptor"))
        name_node = self.required(node, "name")
        name = self.arena.text(name_node)
        self.meter.reserve(1 + len(name))
        if revision is None:
            if (
                descriptor.value is not None
                or descriptor.kind != "scalar"
                or "/" not in name
            ):
                refuse()
        elif (
            type(revision) is not str
            or self.meter.compare(revision, "1") != 0
            or parent is not None
            or descriptor.kind != "object"
            or not matches(
                name,
                ("StructuredOutput", "VcsRef", "AgentSession", "CommandResult"),
                self.meter,
            )
        ):
            refuse()
        if revision is not None:
            descriptor_id = self.required(node, "descriptor")
            for field in ("name", "builtin_revision"):
                if (
                    self.meter.compare(
                        self.arena.text(self.required(node, field)),
                        self.arena.text(self.required(descriptor_id, field)),
                    )
                    != 0
                ):
                    refuse()
            descriptor_parent = self.arena.at(self.required(descriptor_id, "parent"))
            if (
                descriptor_parent.kind != "scalar"
                or descriptor_parent.value is not None
            ):
                refuse()
            for field in ("validation", "digest_fields", "summary"):
                left = self.required(self.required(node, "behavior"), field)
                right = self.required(self.required(descriptor_id, "behavior"), field)
                if (
                    self.meter.compare(self.arena.text(left), self.arena.text(right))
                    != 0
                ):
                    refuse()
        self.meter.reserve(3, entries=1)
        self.maximum_name = max(self.maximum_name, len(name))
        self.types.append((name_node, document))
        self.contracts.append(node)
        self.document(original, "type_schema")

    def document(self, original: dict[str, object], kind: str) -> None:
        schema = self.arena.snapshot(original["schema"], 4)
        source = self.arena.snapshot(original["source"], 4)
        self.source(source, kind)
        self.documents.append(
            self.arena.wrap_object((("schema", schema), ("source", source)), 3)
        )

    def header(self, value: object) -> None:
        self.meter.reserve()
        if type(value) is not StructuredSchemaPreparationInput:
            refuse("unsupported_binding")
        for current, expected in (
            (value.execution_semantics, "kora.execution.v1"),
            (value.checker_binding, "kora.structured-output-view.v1"),
        ):
            if type(current) is not str or self.meter.compare(current, expected) != 0:
                refuse("unsupported_binding")
        parameters = record(
            value.preparation_parameters,
            tuple(PARAMETERS),
            self.meter,
            "unsupported_binding",
        )
        for name, expected in PARAMETERS.items():
            self.meter.reserve(3)
            current = parameters[name]
            if type(current) is not int or current != expected:
                refuse("unsupported_binding")
        records_kind = type(value.type_contracts)
        inline_kind = type(value.inline_schema)
        if type(value.declared_type) is not str or records_kind is not list:
            refuse()
        if value.inline_schema is not None and inline_kind is not dict:
            refuse()
        self.meter.reserve()
        originals = cast("list[object]", value.type_contracts)
        if (
            len(originals) + int(value.inline_schema is not None)
            > PARAMETERS["max_schema_documents"]
        ):
            refuse("preparation_exhausted")
        self.meter.reserve(entries=len(originals))
        order = list(range(len(originals)))
        for original in originals:
            native = record(original, RECORD_FIELDS, self.meter)
            if type(native["name"]) is not str:
                refuse()
        merge_sort(
            order,
            lambda index: cast(
                "str", cast("dict[str, object]", originals[index])["name"]
            ),
            self.meter,
        )
        previous: str | None = None
        for index in order:
            native = cast("dict[str, object]", originals[index])
            name = cast("str", native["name"])
            if previous is not None and self.meter.compare(previous, name) == 0:
                refuse()
            previous = name
        for document, index in enumerate(order):
            self.contract(cast("dict[str, object]", originals[index]), document)
        if value.inline_schema is not None:
            self.document(
                record(value.inline_schema, ("source", "schema"), self.meter),
                "inline_schema",
            )
        self.declared = self.arena.scalar(value.declared_type)
        self.execution = self.arena.scalar(value.execution_semantics)
        self.checker_binding = self.arena.scalar(value.checker_binding)
        self.parameters = self.arena.snapshot(value.preparation_parameters, 2)

    def references(self) -> int:
        for document, original in enumerate(self.documents):
            self.locations.visit(document, self.required(original, "schema"))
        self.locations.index()
        normalized: list[int] = []
        for index, (source, keyword, node, _) in enumerate(self.locations.references):
            self.meter.reserve(5)
            name, pool, used = decode_reference(self.arena.text(node), self.meter)
            document = (
                self.locations.positions[source][0]
                if name is None
                else self.lookup_type(name)
            )
            target = self.locations.target(
                self.required(self.documents[document], "schema"), pool, used
            )
            self.locations.references[index] = source, keyword, node, target
            self.locations.edge(source, target)
            normalized.append(
                self.arena.wrap_object(
                    (
                        (
                            "document",
                            self.arena.scalar(self.locations.positions[source][0]),
                        ),
                        ("keyword", self.arena.scalar(self.arena.text(keyword))),
                        ("path", self.locations.path(source)),
                        ("target_document", self.arena.scalar(document)),
                        ("target_path", self.locations.path(target)),
                    ),
                    3,
                )
            )
        return self.arena.wrap_array(tuple(normalized), 2)

    def roles(self, selected: str, inline: bool) -> tuple[int, int]:
        self.meter.reserve(1 + min(len(selected), 5))
        if selected.startswith("List["):
            refuse()
        declared = self.lookup_type(selected)
        count = len(self.types)
        self.meter.reserve(entries=count)
        states = [0] * count
        constraints: list[int] = []
        for ordinal in range(count + 1):
            start = declared if ordinal == 0 else ordinal - 1
            if states[start] == 2:
                continue
            stack: list[int] = []
            current = start
            while states[current] == 0:
                self.meter.reserve(5, entries=1)
                states[current] = 1
                stack.append(current)
                if ordinal == 0:
                    constraints.append(self.arena.scalar(current))
                parent = self.arena.at(
                    self.required(self.contracts[current], "parent")
                ).value
                if parent is None:
                    if (
                        ordinal == 0
                        and self.meter.compare(
                            self.arena.text(self.types[current][0]), "StructuredOutput"
                        )
                        != 0
                    ):
                        refuse()
                    break
                current = self.lookup_type(cast("str", parent))
            else:
                if states[current] == 1:
                    refuse()
            for current in reversed(stack):
                self.meter.reserve(3)
                states[current] = 2
        self.meter.reserve(1 + len(constraints))
        constraints.reverse()
        self.meter.reserve(entries=len(self.documents))
        membership = [False] * len(self.documents)
        queue: list[int] = []

        def enqueue(document: int) -> None:
            self.meter.reserve(3)
            if not membership[document]:
                self.meter.reserve(entries=1)
                membership[document] = True
                queue.append(document)

        for node in constraints:
            enqueue(cast("int", self.arena.at(node).value))
        if inline:
            constraints.append(self.arena.scalar(count))
            enqueue(count)
        cursor = 0
        while cursor < len(queue):
            self.meter.reserve(4)
            document = queue[cursor]
            cursor += 1
            if document < count:
                parent = self.arena.at(
                    self.required(self.contracts[document], "parent")
                ).value
                if parent is not None:
                    enqueue(self.lookup_type(cast("str", parent)))
            references = self.locations.references
            self.meter.reserve(1 + len(references).bit_length())
            low, high = 0, len(references)
            while low < high:
                middle = (low + high) // 2
                if self.locations.positions[references[middle][0]][0] < document:
                    low = middle + 1
                else:
                    high = middle
            while (
                low < len(references)
                and self.locations.positions[references[low][0]][0] == document
            ):
                self.meter.reserve(4)
                enqueue(self.locations.positions[references[low][3]][0])
                low += 1
        self.meter.reserve(1 + len(membership))
        if not all(membership):
            refuse()
        checker = (
            count
            if inline and self.meter.compare(selected, "StructuredOutput") == 0
            else declared
        )
        return self.arena.wrap_array(tuple(constraints), 2), self.arena.scalar(checker)

    def prepare(self, value: object) -> PreparedStructuredSchemaInput:
        self.header(value)
        references = self.references()
        constraints, checker = self.roles(
            self.arena.text(self.declared), len(self.documents) > len(self.types)
        )
        offsets, targets = self.locations.graph()
        root = self.arena.wrap_object(
            (
                ("checker_binding", self.checker_binding),
                ("checker_document", checker),
                ("constraint_documents", constraints),
                ("declared_type", self.declared),
                ("documents", self.arena.wrap_array(tuple(self.documents), 2)),
                (
                    "execution_semantics",
                    self.execution,
                ),
                (
                    "preparation_parameters",
                    self.parameters,
                ),
                ("reference_targets", references),
                ("type_contracts", self.arena.wrap_array(tuple(self.contracts), 2)),
            ),
            1,
        )
        self.arena.encode(root)
        frozen = self.arena.freeze(root)
        rows = (
            self.types,
            self.locations.positions,
            self.locations.references,
            self.locations.edges,
        )
        self.meter.reserve(
            1 + sum(len(row) for row in rows), entries=sum(len(row) for row in rows)
        )
        accounting = StructuredSchemaPreparationAccounting(
            frozen.input_bytes,
            len(frozen.nodes),
            frozen.input_depth,
            len(self.documents),
            len(self.locations.positions),
            len(self.locations.references),
            self.meter.work,
            self.meter.auxiliary_bytes,
            self.meter.auxiliary_entries,
        )
        return PreparedStructuredSchemaInput(
            accounting,
            frozen.nodes,
            root,
            tuple(self.types),
            tuple(self.locations.positions),
            self.locations.node_positions,
            tuple(self.locations.references),
            tuple(self.locations.edges),
            offsets,
            targets,
        )


def prepare_structured_schema_input(
    value: object,
) -> PreparedStructuredSchemaInput | StructuredSchemaPreparationRefusal:
    try:
        result: PreparedStructuredSchemaInput | StructuredSchemaPreparationRefusal = (
            Preparation().prepare(value)
        )
    except PreparationRefused as error:
        result = StructuredSchemaPreparationRefusal(error.reason)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        prepared = isinstance(result, PreparedStructuredSchemaInput)
        counts: dict[str, int] = dict.fromkeys(COUNTS, 0)
        if isinstance(result, PreparedStructuredSchemaInput):
            for name in COUNTS:
                count: object = getattr(result.accounting, name)
                if type(count) is not int:
                    raise RuntimeError("schema accounting type invariant")
                counts[name] = count
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            prepared=prepared,
            unsupported_binding=isinstance(result, StructuredSchemaPreparationRefusal)
            and result.reason == "unsupported_binding",
            unsupported_schema=isinstance(result, StructuredSchemaPreparationRefusal)
            and result.reason == "unsupported_schema",
            preparation_exhausted=isinstance(result, StructuredSchemaPreparationRefusal)
            and result.reason == "preparation_exhausted",
            **counts,
        )
    return result
