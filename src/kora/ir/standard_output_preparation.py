import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from kora.ir.json import JsonObject, JsonValue
from kora.ir.standard_schema_policy import (
    SchemaDomain,
    SourceAccounting,
    bind_policy,
    refuse,
    require,
)
from kora.ir.standard_schema_scan import SourceScan, preparation_score
from kora.ir.standard_schema_source import Sources
from kora.ir.standard_schema_support import prepare_source
from kora.ir.structured_output import require_structured_output_revision

FIELDS: tuple[str, ...] = (
    "name",
    "builtin_revision",
    "parent",
    "behavior",
    "descriptor",
    "source",
    "schema",
)
ROLES: tuple[str, ...] = ("validation", "digest_fields", "summary")


@dataclass(frozen=True, slots=True)
class StandardOutputInput:
    declared_type: object
    type_contracts: object
    inline_schema: object
    execution_semantics: object
    checker_binding: object
    schema_validation: object


@dataclass(frozen=True, slots=True)
class OutputBehaviors:
    validation: str
    digest_fields: str
    summary: str


@dataclass(frozen=True, slots=True)
class PreparedStandardOutput:
    source_bytes: bytes = field(repr=False)
    behaviors: tuple[OutputBehaviors, ...] = field(repr=False)
    accounting: SourceAccounting


def members(value: object, names: tuple[str, ...]) -> dict[str, object]:
    require(type(value) is dict)
    result = cast("dict[str, object]", value)
    require(len(result) == len(names))
    require(all(type(key) is str and key in names for key in result))
    return result


def source_identity(value: object, kind: str) -> None:
    source = members(value, ("kind", "path", "location"))
    require(source["kind"] == kind)
    require(type(source["path"]) is list)
    require(source["location"] is None or type(source["location"]) is dict)
    require(
        all(
            type(item) is str or type(item) is int and item >= 0
            for item in cast("list[object]", source["path"])
        )
    )


def contract(original: dict[str, object]) -> None:
    name, parent, revision = (
        original["name"],
        original["parent"],
        original["builtin_revision"],
    )
    require(type(name) is str and bool(name))
    require(parent is None or type(parent) is str)
    behavior = members(original["behavior"], ROLES)
    require(all(type(value) is str and bool(value) for value in behavior.values()))
    descriptor = original["descriptor"]
    if name == "StructuredOutput" and revision == "1":
        require_structured_output_revision(descriptor)
    if revision is None:
        require(descriptor is None and "/" in cast("str", name))
    else:
        require(type(revision) is str and revision == "1" and parent is None)
        require(name in ("StructuredOutput", "VcsRef", "AgentSession", "CommandResult"))
        require(type(descriptor) is dict)
        saved = cast("dict[str, object]", descriptor)
        require(saved.get("name") == name and saved.get("builtin_revision") == revision)
        require("parent" in saved and saved["parent"] is None)
        require(members(saved.get("behavior"), ROLES) == behavior)
    source_identity(original["source"], "type_schema")


def wrappers(value: StandardOutputInput, limits: Mapping[str, int]) -> JsonObject:
    require(type(value.declared_type) is str)
    require(type(value.type_contracts) is list)
    originals = cast("list[object]", value.type_contracts)
    require(bool(originals))
    require(
        len(originals) + int(value.inline_schema is not None)
        <= limits["max_schema_documents"],
        capacity=True,
    )
    documents: list[object] = []
    contracts: list[object] = []
    for raw in originals:
        original = members(raw, FIELDS)
        documents.append({"name": original["name"], "schema": original["schema"]})
        contracts.append({key: original[key] for key in FIELDS if key != "schema"})
    inline_source: object = None
    if value.inline_schema is not None:
        inline = members(value.inline_schema, ("source", "schema"))
        documents.append({"name": None, "schema": inline["schema"]})
        inline_source = inline["source"]
    return cast(
        "JsonObject",
        {
            "documents": documents,
            "constraints": [],
            "metadata": {
                "declared_type": value.declared_type,
                "type_contracts": contracts,
                "inline_source": inline_source,
                "execution_semantics": value.execution_semantics,
                "checker_binding": value.checker_binding,
                "schema_validation": value.schema_validation,
                "checker_document": 0,
            },
        },
    )


def ancestry(
    contracts: list[dict[str, object]], names: dict[str, int], start: int
) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    current = start
    while True:
        require(current not in seen)
        seen.add(current)
        result.append(current)
        parent = contracts[current]["parent"]
        if parent is None:
            return result
        require(type(parent) is str and parent in names)
        current = names[cast("str", parent)]


def roles(bundle: JsonObject, limits: Mapping[str, int]) -> tuple[OutputBehaviors, ...]:
    metadata = cast("JsonObject", bundle["metadata"])
    contracts = cast("list[dict[str, object]]", metadata["type_contracts"])
    names: dict[str, int] = {}
    for ordinal, original in enumerate(contracts):
        contract(original)
        name = cast("str", original["name"])
        require(name not in names)
        names[name] = ordinal
    selected = cast("str", metadata["declared_type"])
    require(selected in names and not selected.startswith("List["))
    for ordinal in range(len(contracts)):
        ancestry(contracts, names, ordinal)
    chain = ancestry(contracts, names, names[selected])
    root = contracts[chain[-1]]
    require(root["name"] == "StructuredOutput" and root["builtin_revision"] == "1")
    require_structured_output_revision(root["descriptor"])
    chain.reverse()
    constraints = chain.copy()
    inline = len(cast("list[object]", bundle["documents"])) > len(contracts)
    if inline:
        source_identity(metadata["inline_source"], "inline_schema")
        constraints.append(len(contracts))
    bundle["constraints"] = cast("list[JsonValue]", constraints)
    metadata["checker_document"] = (
        len(contracts) if inline and selected == "StructuredOutput" else names[selected]
    )
    sources = Sources(bundle, limits)
    dependencies: list[set[int]] = [set() for _ in sources.documents]
    for (position, _), target in sources.references.items():
        dependencies[sources.sites[position].document].add(
            sources.sites[target].document
        )
    pending = constraints.copy()
    reached: set[int] = set()
    while pending:
        document = pending.pop()
        if document in reached:
            continue
        reached.add(document)
        pending.extend(dependencies[document] - reached)
        if document < len(contracts):
            parent = contracts[document]["parent"]
            if parent is not None:
                pending.append(names[cast("str", parent)])
    require(len(reached) == len(sources.documents))
    return tuple(
        OutputBehaviors(**cast("dict[str, str]", contracts[index]["behavior"]))
        for index in chain
    )


def prepare_standard_output(value: StandardOutputInput) -> PreparedStandardOutput:
    if type(value) is not StandardOutputInput:
        refuse("unsupported_policy")
    limits = bind_policy(SchemaDomain.OUTPUT, value.schema_validation)
    if (
        type(value.execution_semantics) is not str
        or value.execution_semantics != "kora.execution.v1"
        or type(value.checker_binding) is not str
        or value.checker_binding != "kora.structured-output-view.v1"
    ):
        refuse("unsupported_policy")
    bundle = wrappers(value, limits)
    measured = SourceScan(limits).measure(bundle)
    require(
        preparation_score(measured, limits["max_validation_score"])
        <= limits["max_validation_score"],
        capacity=True,
    )
    behaviors = roles(bundle, limits)
    measured, sources = prepare_source(bundle, limits)
    accounting = SourceAccounting(
        measured.tree.encoded,
        measured.tree.nodes,
        len(sources.sites),
        len(sources.references),
        len(sources.documents),
        preparation_score(measured, limits["max_validation_score"]),
    )
    encoded = json.dumps(
        bundle, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    return PreparedStandardOutput(encoded.encode("ascii"), behaviors, accounting)
