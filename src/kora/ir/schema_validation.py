from collections.abc import Callable
from dataclasses import dataclass

from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.json import JsonObject
from kora.ir.schema_keywords import KeywordValidation
from kora.ir.schema_locations import Location, SavedReferences, schema_sites

REFERENCE_KEYWORDS: frozenset[str] = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


@dataclass(frozen=True)
class SchemaCounts:
    schemas: int
    references: int


def validate_schema(
    root: JsonObject,
    location: Location,
    references: SavedReferences,
    keywords: KeywordValidation,
    fault: Callable[[Location], None],
) -> SchemaCounts:
    schema_count = 0
    reference_count = 0
    for schema, field in schema_sites(root, location):
        schema_count += 1
        if isinstance(schema, bool):
            continue
        keywords.check(schema, field)
        for keyword, value in schema.items():
            if keyword == "$id" and schema is not root:
                fault(field.child(keyword))
            elif (
                keyword == "$schema"
                and isinstance(value, str)
                and value != SCHEMA_DIALECT
            ):
                fault(field.child(keyword))
            elif keyword in REFERENCE_KEYWORDS and isinstance(value, str):
                reference_count += 1
                if not references.resolves(root, value):
                    fault(field.child(keyword))
    return SchemaCounts(schema_count, reference_count)
