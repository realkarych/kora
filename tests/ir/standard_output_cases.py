from kora.ir.standard_output_preparation import StandardOutputInput
from tests.ir.standard_schema_cases import binding
from tests.ir.structured_schema_cases import builtin, contract


def output_input(
    schema: object = True,
    *,
    records: list[dict[str, object]] | None = None,
    declared: str = "example/Declared",
    inline: object = None,
) -> StandardOutputInput:
    original = contract(schema)
    original["behavior"] = {
        "validation": "kora.json-schema-2020-12.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    }
    return StandardOutputInput(
        declared,
        [builtin(), original] if records is None else records,
        inline,
        "kora.execution.v1",
        "kora.structured-output-view.v1",
        binding(),
    )
