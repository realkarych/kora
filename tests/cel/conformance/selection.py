from dataclasses import dataclass
from enum import Enum
from typing import Final

from kora.cel import Reason


class Feature(Enum):
    MESSAGE_TYPES = "protobuf message types and qualified type names"
    CONTAINERS = "containers and qualified-name resolution"
    TWO_VARIABLE_COMPREHENSIONS = "two-variable comprehension macros"
    EXTENSIONS = "CEL extension libraries"
    OPTIONAL_SYNTAX = "optional syntax"
    UNKNOWN_SETS = "unknown sets"
    TYPE_DEDUCTION = "type-checker deductions"
    QUOTED_FIELDS = "backtick-quoted field selection"
    RAW_CONTROL_CHARACTERS = "unescaped control characters in literals"
    NARROWED_FUNCTIONS = "standard functions the pinned binding does not implement"
    UNDECLARED_FUNCTIONS = "functions outside the standard library"


@dataclass(frozen=True)
class ExcludedFile:
    name: str
    features: tuple[Feature, ...]


@dataclass(frozen=True)
class Rejection:
    file: str
    section: str
    test: str | None
    feature: Feature
    reason: Reason


INCLUDED_FILES: Final[tuple[str, ...]] = (
    "basic",
    "comparisons",
    "conversions",
    "fields",
    "fp_math",
    "integer_math",
    "lists",
    "logic",
    "macros",
    "parse",
    "plumbing",
    "string",
    "timestamps",
)

EXCLUDED_FILES: Final[tuple[ExcludedFile, ...]] = (
    ExcludedFile("dynamic", (Feature.MESSAGE_TYPES, Feature.CONTAINERS)),
    ExcludedFile("namespace", (Feature.CONTAINERS,)),
    ExcludedFile("macros2", (Feature.TWO_VARIABLE_COMPREHENSIONS,)),
    ExcludedFile("proto2", (Feature.MESSAGE_TYPES,)),
    ExcludedFile("proto3", (Feature.MESSAGE_TYPES,)),
    ExcludedFile("proto2_ext", (Feature.MESSAGE_TYPES, Feature.EXTENSIONS)),
    ExcludedFile("wrappers", (Feature.MESSAGE_TYPES,)),
    ExcludedFile("enums", (Feature.MESSAGE_TYPES,)),
    ExcludedFile("optionals", (Feature.OPTIONAL_SYNTAX,)),
    ExcludedFile("unknowns", (Feature.UNKNOWN_SETS,)),
    ExcludedFile("type_deduction", (Feature.TYPE_DEDUCTION,)),
    ExcludedFile("bindings_ext", (Feature.EXTENSIONS,)),
    ExcludedFile("block_ext", (Feature.EXTENSIONS,)),
    ExcludedFile("encoders_ext", (Feature.EXTENSIONS,)),
    ExcludedFile("lists_ext", (Feature.EXTENSIONS,)),
    ExcludedFile("math_ext", (Feature.EXTENSIONS,)),
    ExcludedFile("network_ext", (Feature.EXTENSIONS,)),
    ExcludedFile("string_ext", (Feature.EXTENSIONS,)),
)

MESSAGES: Final[Feature] = Feature.MESSAGE_TYPES
CONSTRUCTION: Final[Reason] = Reason.MESSAGE_CONSTRUCTION
CONTROL: Final[Feature] = Feature.RAW_CONTROL_CHARACTERS
INVALID: Final[Reason] = Reason.INVALID_CHARACTER
CONTROL_CASES: Final[tuple[str, ...]] = tuple(
    f"{quoting}_unescaped_{kind}_control_characters"
    for quoting in (
        "single_quoted",
        "double_quoted",
        "triple_single_quoted",
        "triple_double_quoted",
    )
    for kind in ("special", "all")
)

KORA_REJECTIONS: Final[tuple[Rejection, ...]] = (
    Rejection(
        "basic",
        "functions",
        "unbound",
        Feature.UNDECLARED_FUNCTIONS,
        Reason.UNKNOWN_FUNCTION,
    ),
    Rejection(
        "basic",
        "functions",
        "unbound_is_runtime_error",
        Feature.UNDECLARED_FUNCTIONS,
        Reason.UNKNOWN_FUNCTION,
    ),
    Rejection("comparisons", "eq_wrapper", None, MESSAGES, CONSTRUCTION),
    *(
        Rejection("comparisons", "eq_literal", name, MESSAGES, CONSTRUCTION)
        for name in (
            "eq_dyn_json_null",
            "not_eq_dyn_proto2_msg_null",
            "not_eq_dyn_proto3_msg_null",
        )
    ),
    *(
        Rejection("comparisons", "ne_literal", name, MESSAGES, CONSTRUCTION)
        for name in (
            "ne_proto2",
            "ne_proto3",
            "ne_proto2_missing_fields_neq",
            "ne_proto3_missing_fields_neq",
            "ne_proto_nan_not_equal",
            "ne_proto_different_types",
            "ne_proto2_any_unpack",
            "ne_proto2_any_unpack_bytewise_fallback",
            "ne_proto3_any_unpack",
            "ne_proto3_any_unpack_bytewise_fallback",
        )
    ),
    Rejection("comparisons", "gt_literal", "gt_bytes_one", CONTROL, INVALID),
    Rejection("comparisons", "gt_literal", "gt_bytes_one_to_empty", CONTROL, INVALID),
    Rejection("comparisons", "gt_literal", "not_gt_bytes_sorting", CONTROL, INVALID),
    Rejection("comparisons", "lte_literal", "lte_bytes_empty", CONTROL, INVALID),
    Rejection("comparisons", "lte_literal", "not_lte_bytes_length", CONTROL, INVALID),
    Rejection("comparisons", "gte_literal", "gte_bytes_to_empty", CONTROL, INVALID),
    Rejection(
        "comparisons",
        "gte_literal",
        "not_gte_bytes_empty_to_nonempty",
        CONTROL,
        INVALID,
    ),
    Rejection("comparisons", "gte_literal", "gte_bytes_samelength", CONTROL, INVALID),
    Rejection("comparisons", "bound", "bytes_gt_left_false", CONTROL, INVALID),
    Rejection(
        "conversions",
        "bool",
        None,
        Feature.NARROWED_FUNCTIONS,
        Reason.UNKNOWN_FUNCTION,
    ),
    Rejection(
        "conversions",
        "identity",
        "bool",
        Feature.NARROWED_FUNCTIONS,
        Reason.UNKNOWN_FUNCTION,
    ),
    Rejection(
        "fields",
        "quoted_map_fields",
        None,
        Feature.QUOTED_FIELDS,
        Reason.QUOTED_FIELD,
    ),
    Rejection("parse", "nest", "message_literal", MESSAGES, CONSTRUCTION),
    Rejection("parse", "repeat", "select", MESSAGES, CONSTRUCTION),
    Rejection("parse", "repeat", "message_literal", MESSAGES, CONSTRUCTION),
    Rejection("parse", "struct_field_names", None, MESSAGES, CONSTRUCTION),
    Rejection("parse", "whitespace", None, MESSAGES, Reason.QUALIFIED_NAME),
    Rejection("parse", "comments", None, MESSAGES, Reason.QUALIFIED_NAME),
    Rejection(
        "parse",
        "receiver_function_names",
        None,
        Feature.UNDECLARED_FUNCTIONS,
        Reason.UNKNOWN_FUNCTION,
    ),
    *(
        Rejection("parse", section, name, CONTROL, INVALID)
        for section in ("string_literals", "bytes_literals")
        for name in CONTROL_CASES
    ),
    Rejection(
        "timestamps",
        "timestamp_selectors_tz",
        None,
        Feature.NARROWED_FUNCTIONS,
        Reason.UNKNOWN_FUNCTION,
    ),
)


CEL_STANDARD_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "size",
        "int",
        "uint",
        "double",
        "string",
        "bytes",
        "bool",
        "type",
        "dyn",
        "duration",
        "timestamp",
        "contains",
        "endsWith",
        "startsWith",
        "matches",
        "getDate",
        "getDayOfMonth",
        "getDayOfWeek",
        "getDayOfYear",
        "getFullYear",
        "getHours",
        "getMilliseconds",
        "getMinutes",
        "getMonth",
        "getSeconds",
    }
)


FEATURE_REASONS: Final[dict[Feature, frozenset[Reason]]] = {
    Feature.MESSAGE_TYPES: frozenset(
        {Reason.MESSAGE_CONSTRUCTION, Reason.QUALIFIED_NAME}
    ),
    Feature.QUOTED_FIELDS: frozenset({Reason.QUOTED_FIELD}),
    Feature.RAW_CONTROL_CHARACTERS: frozenset({Reason.INVALID_CHARACTER}),
    Feature.NARROWED_FUNCTIONS: frozenset({Reason.UNKNOWN_FUNCTION}),
    Feature.UNDECLARED_FUNCTIONS: frozenset({Reason.UNKNOWN_FUNCTION}),
}


def rejection_for(file: str, section: str, test: str) -> Rejection | None:
    for rejection in KORA_REJECTIONS:
        if (rejection.file, rejection.section) != (file, section):
            continue
        if rejection.test is None or rejection.test == test:
            return rejection
    return None
