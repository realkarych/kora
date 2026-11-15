from kora.ir.standard_schema_policy import SchemaDomain, SourceDecision, domain_limits
from kora.ir.standard_schema_support import check_standard_schema_source


def binding(domain: SchemaDomain = SchemaDomain.OUTPUT) -> dict[str, object]:
    names = (
        ("policy", "parameters")
        if domain is SchemaDomain.OUTPUT
        else ("profile", "limits")
    )
    return {names[0]: domain.value, names[1]: domain_limits(domain)}


def source(schema: object, *, metadata: object = None) -> dict[str, object]:
    return {
        "documents": [{"name": None, "schema": schema}],
        "constraints": [0],
        "metadata": metadata,
    }


def decide(schema: object, *, metadata: object = None) -> SourceDecision:
    return check_standard_schema_source(
        source(schema, metadata=metadata), SchemaDomain.OUTPUT, binding()
    )


def chain(count: int, *, cycle: bool = False) -> dict[str, object]:
    definitions: dict[str, object] = {
        f"s{index}": {"$ref": f"#/$defs/s{index + 1}"} for index in range(count - 2)
    }
    definitions[f"s{count - 2}"] = {"$ref": "#"} if cycle else True
    return {"$defs": definitions, "$ref": "#/$defs/s0"}
