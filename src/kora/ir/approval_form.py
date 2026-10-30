from kora.ir.json import JsonObject


def default_approval_form() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "comments": {"type": "string"},
        },
        "required": ["decision"],
        "additionalProperties": False,
    }
