from collections.abc import Callable
from typing import cast

from pydantic import ValidationError

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_type_references import array_text, reference_text
from kora.ir.agent_session import agent_session_revision_descriptor
from kora.ir.command_result import command_result_revision_descriptor
from kora.ir.contracts import BuiltinTypeContract, TypeContract
from kora.ir.data import IrJsonObjectField
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema_definition
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import Location, SavedReferences, contract_location
from kora.ir.schema_validation import validate_schema
from kora.ir.structured_output import structured_output_revision_descriptor
from kora.ir.type_declarations import validate_type_declarations
from kora.ir.vcs_ref import vcs_ref_revision_descriptor

DESCRIPTORS: dict[str, Callable[[], JsonObject]] = {
    "VcsRef": vcs_ref_revision_descriptor,
    "StructuredOutput": structured_output_revision_descriptor,
    "AgentSession": agent_session_revision_descriptor,
    "CommandResult": command_result_revision_descriptor,
}


def read_document(text: str, budget: TextBudget) -> GraphDocument:
    if type(text) is not str:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    budget.reserve(len(text))
    if not text.isascii():
        raise GraphValueRejected(GraphValueFailure.INVALID)
    raw = ObjectReader(text).object()
    if render_object(raw, TextBudget(len(text))) != text:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    try:
        return GraphDocument.model_validate(raw)
    except ValidationError:
        raise GraphValueRejected(GraphValueFailure.INVALID) from None


def invalid_schema(location: Location) -> None:
    raise GraphValueRejected(GraphValueFailure.INVALID)


def type_sources(
    document: GraphDocument,
) -> tuple[dict[str, JsonObject], dict[str, JsonObject | None]]:
    if not validate_type_declarations(document).declarations_valid:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    roots: dict[str, JsonObject] = {}
    descriptors: dict[str, JsonObject | None] = {}
    for name, contract in document.type_contracts.items():
        if isinstance(contract, BuiltinTypeContract):
            definition = builtin_schema_definition(name, contract.builtin_revision)
            factory = DESCRIPTORS.get(name)
            if definition is None or factory is None:
                raise GraphValueRejected(GraphValueFailure.VERSION)
            descriptor = factory()
            if definition.file_digest != descriptor.get("schema_sha256"):
                raise GraphValueRejected(GraphValueFailure.INVALID)
            roots[name] = definition.schema
            descriptors[name] = descriptor
        else:
            roots[name] = contract.schema_
            descriptors[name] = None
    references = SavedReferences(roots)
    keywords = KeywordValidation(invalid_schema)
    for name, root in roots.items():
        validate_schema(
            root, contract_location(name), references, keywords, invalid_schema
        )
    return roots, descriptors


def schema_text(schema: JsonObject, budget: TextBudget) -> str:
    if type(schema) is not dict and type(schema) is not IrJsonObjectField:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    count = dict[str, JsonValue].__len__(schema)
    if (5 * count + 1 if count else 2) > budget.remaining:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    native = dict[str, JsonValue].copy(schema)
    return render_object(native, budget, reject_aliases=True)


def type_header(
    name: str, contract: TypeContract, descriptor: JsonObject | None
) -> JsonObject:
    behavior: JsonValue
    if isinstance(contract, BuiltinTypeContract):
        if descriptor is None:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        revision: str | None = contract.builtin_revision
        parent: str | None = None
        behavior = descriptor["behavior"]
    else:
        revision = None
        parent = contract.parent
        behavior = {
            "validation": contract.behavior.validation,
            "digest_fields": contract.behavior.digest_fields,
            "summary": contract.behavior.summary,
        }
    return {
        "name": name,
        "builtin_revision": revision,
        "parent": parent,
        "behavior": behavior,
        "descriptor": descriptor,
        "source": {
            "kind": "type_schema",
            "path": ["type_contracts", name, "schema"],
            "location": None,
        },
    }


def build_type_section(document: GraphDocument, budget: TextBudget) -> str:
    budget.reserve(2)
    roots, descriptors = type_sources(document)
    uris = {"urn:kora:artifact:" + name.replace("/", ":"): name for name in roots}
    records: list[str] = []
    for name in sorted(roots):
        budget.reserve(int(bool(records)) + len(',"schema":,"references":'))
        header = render_object(
            type_header(name, document.type_contracts[name], descriptors[name]), budget
        )
        schema = schema_text(roots[name], budget)
        references = reference_text(name, roots, uris, budget)
        records.append(
            header[:-1] + ',"schema":' + schema + ',"references":' + references + "}"
        )
    return "[" + ",".join(records) + "]"


def build_graph_type_contracts(ir_text: str, *, max_encoded_bytes: int) -> str:
    budget = TextBudget(max_encoded_bytes)
    document = read_document(ir_text, budget)
    return build_type_section(document, budget)


def read_graph_type_contracts(
    ir_text: str, value: object, *, max_encoded_bytes: int
) -> str:
    budget = TextBudget(max_encoded_bytes)
    document = read_document(ir_text, budget)
    if type(value) is not list:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    text = array_text(cast(list[object], value), budget)
    expected_budget = TextBudget(max_encoded_bytes)
    expected_budget.reserve(len(ir_text))
    expected = build_type_section(document, expected_budget)
    if text != expected:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return text
