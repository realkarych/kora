import hashlib
import json
from dataclasses import dataclass, field
from typing import cast

from kora.cel import BoundError, PathSet, SchemaError, SchemaTypes, cut
from kora.cel.cost import Variable
from kora.cel.schema import SchemaView
from kora.cel_eval.artifact_projection_codec import (
    ProjectionAdmission,
    ProjectionFailure,
    ProjectionIdentity,
    ProjectionRecord,
    ProjectionRejected,
    checked_conversion,
    encode_record,
)
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission, inventory_meter
from kora.ir.json import JsonObject, JsonValue

STRUCTURED_OUTPUT_VIEW = SchemaView.STRUCTURED_OUTPUT.value


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionView:
    paths: PathSet = field(repr=False)
    variable: Variable = field(repr=False)
    path_set_digest: str


def prepare_projection_view(
    contract: JsonObject, selected: JsonObject, admission: ProjectionAdmission
) -> ProjectionView:
    records = cast(list[JsonObject], contract["type_contracts"])
    roots = {
        "urn:kora:artifact:" + cast(str, record["name"]).replace("/", ":"): record[
            "schema"
        ]
        for record in records
    }
    checker = cast(JsonObject, contract["checker"])
    schema = (
        cast(JsonObject, contract["inline_schema"])["schema"]
        if checker["kind"] == "inline_schema"
        else roots["urn:kora:artifact:" + cast(str, checker["name"]).replace("/", ":")]
    )
    meter = inventory_meter(CheckedInventoryAdmission(admission.work, 0))
    try:
        variable = SchemaTypes(roots.get, meter).variable(
            schema, SchemaView.STRUCTURED_OUTPUT
        )
        paths = PathSet(
            tuple(cast(list[str], selected["whole"])),
            tuple(cast(list[str], selected["presence"])),
        )
    except (BoundError, SchemaError):
        raise ProjectionRejected(ProjectionFailure.BINDING) from None
    admission.reserve(0, 0, meter.steps)
    path_bytes = json.dumps(
        {"whole": paths.whole, "presence": paths.presence},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return ProjectionView(paths, variable, hashlib.sha256(path_bytes).hexdigest())


def prepare_record(
    identity: ProjectionIdentity,
    value: JsonValue,
    view: ProjectionView,
    admission: ProjectionAdmission,
) -> ProjectionRecord:
    meter = inventory_meter(CheckedInventoryAdmission(admission.work, 0))
    try:
        original = cut(value, view.paths, meter)
        converted = checked_conversion(original, view.variable)
        admission.reserve(0, 0, meter.steps)
        return encode_record(identity, original, converted)
    except BoundError:
        raise ProjectionRejected(ProjectionFailure.VALUE) from None
