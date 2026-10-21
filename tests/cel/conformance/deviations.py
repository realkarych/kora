import enum

from tests.cel.binding_support import Typed


class Category(enum.StrEnum):
    STRING_SIZE_IN_BYTES = "string_size_in_bytes"
    MIXED_NUMERIC_KEYS = "mixed_numeric_keys"
    ERROR_ABSORPTION = "error_absorption"
    NEGATED_BOOLEAN = "negated_boolean"
    WRAPPING_NEGATION = "wrapping_negation"
    INVALID_UTF8_STRING = "invalid_utf8_string"
    TIME_RANGES = "time_ranges"
    TIME_FORMATTING = "time_formatting"
    DURATION_ACCESSORS = "duration_accessors"
    MISSING_OVERLOADS = "missing_overloads"
    QUALIFIED_NAMES = "qualified_names"
    REPEATED_MAP_KEYS = "repeated_map_keys"


CaseKey = tuple[str, str, str]

DEVIATIONS: dict[CaseKey, tuple[Category, Typed]] = {
    ("comparisons", "eq_literal", "eq_map_mixed_type_numbers"): (
        Category.MIXED_NUMERIC_KEYS,
        ("bool", "False"),
    ),
    ("comparisons", "eq_literal", "not_eq_dyn_timestamp_null"): (
        Category.MISSING_OVERLOADS,
        ("error", "RuntimeError"),
    ),
    ("conversions", "int", "timestamp"): (
        Category.MISSING_OVERLOADS,
        ("error", "RuntimeError"),
    ),
    ("conversions", "string", "bytes_invalid"): (
        Category.INVALID_UTF8_STRING,
        ("str", "'\\x00\ufffd'"),
    ),
    ("conversions", "identity", "timestamp"): (
        Category.MISSING_OVERLOADS,
        ("error", "RuntimeError"),
    ),
    ("fields", "map_fields", "map_key_mixed_numbers_double_key"): (
        Category.MIXED_NUMERIC_KEYS,
        ("error", "TypeError"),
    ),
    ("fields", "map_fields", "map_key_mixed_numbers_uint_key"): (
        Category.MIXED_NUMERIC_KEYS,
        ("error", "KeyError"),
    ),
    ("fields", "map_fields", "map_key_mixed_numbers_int_key"): (
        Category.MIXED_NUMERIC_KEYS,
        ("error", "KeyError"),
    ),
    ("fields", "qualified_identifier_resolution", "qualified_ident"): (
        Category.QUALIFIED_NAMES,
        ("error", "RuntimeError"),
    ),
    ("fields", "qualified_identifier_resolution", "map_field_select"): (
        Category.QUALIFIED_NAMES,
        ("error", "RuntimeError"),
    ),
    (
        "fields",
        "qualified_identifier_resolution",
        "qualified_identifier_resolution_unchecked",
    ): (Category.QUALIFIED_NAMES, ("error", "RuntimeError")),
    ("fields", "qualified_identifier_resolution", "ident_with_longest_prefix_check"): (
        Category.QUALIFIED_NAMES,
        ("error", "RuntimeError"),
    ),
    ("fields", "qualified_identifier_resolution", "map_value_repeat_key"): (
        Category.REPEATED_MAP_KEYS,
        ("int", "3"),
    ),
    ("fields", "in", "mixed_numbers_and_keys_present"): (
        Category.MIXED_NUMERIC_KEYS,
        ("bool", "False"),
    ),
    ("fields", "in", "mixed_numbers_and_keys_absent"): (
        Category.MIXED_NUMERIC_KEYS,
        ("error", "TypeError"),
    ),
    ("integer_math", "int64_math", "unary_minus_not_bool"): (
        Category.NEGATED_BOOLEAN,
        ("bool", "True"),
    ),
    ("integer_math", "int64_math", "int64_min_negate"): (
        Category.WRAPPING_NEGATION,
        ("int", "-9223372036854775808"),
    ),
    ("lists", "index", "zero_based_double"): (
        Category.MIXED_NUMERIC_KEYS,
        ("error", "TypeError"),
    ),
    ("macros", "all", "list_elem_error_shortcircuit"): (
        Category.ERROR_ABSORPTION,
        ("error", "ZeroDivisionError"),
    ),
    ("string", "size", "one_unicode"): (Category.STRING_SIZE_IN_BYTES, ("int", "2")),
    ("string", "size", "unicode"): (Category.STRING_SIZE_IN_BYTES, ("int", "10")),
    ("timestamps", "timestamp_conversions", "toInt_timestamp"): (
        Category.MISSING_OVERLOADS,
        ("error", "RuntimeError"),
    ),
    ("timestamps", "timestamp_conversions", "toString_timestamp"): (
        Category.TIME_FORMATTING,
        ("str", "'2009-02-13T23:31:30+00:00'"),
    ),
    ("timestamps", "timestamp_conversions", "toString_timestamp_nanos"): (
        Category.TIME_FORMATTING,
        ("str", "'9999-12-31T23:59:59.999999999+00:00'"),
    ),
    ("timestamps", "timestamp_conversions", "type_comparison"): (
        Category.QUALIFIED_NAMES,
        ("error", "RuntimeError"),
    ),
    ("timestamps", "duration_conversions", "toString_duration"): (
        Category.TIME_FORMATTING,
        ("str", "'277h46m40s'"),
    ),
    ("timestamps", "duration_conversions", "type_comparison"): (
        Category.QUALIFIED_NAMES,
        ("error", "RuntimeError"),
    ),
    ("timestamps", "timestamp_arithmetic", "add_time_to_duration"): (
        Category.MISSING_OVERLOADS,
        ("error", "TypeError"),
    ),
    ("timestamps", "duration_converters", "get_milliseconds"): (
        Category.DURATION_ACCESSORS,
        ("int", "123321"),
    ),
    ("timestamps", "timestamp_range", "sub_time_duration_over"): (
        Category.TIME_RANGES,
        ("timedelta", "datetime.timedelta(days=3652058, seconds=86399)"),
    ),
    ("timestamps", "timestamp_range", "sub_time_duration_under"): (
        Category.TIME_RANGES,
        ("timedelta", "datetime.timedelta(days=-3652059, seconds=1)"),
    ),
    ("timestamps", "duration_range", "from_string_under"): (
        Category.TIME_RANGES,
        (
            "timedelta",
            "datetime.timedelta(days=-106752, seconds=763, microseconds=145225)",
        ),
    ),
    ("timestamps", "duration_range", "from_string_over"): (
        Category.TIME_RANGES,
        (
            "timedelta",
            "datetime.timedelta(days=106751, seconds=85636, microseconds=854775)",
        ),
    ),
    ("timestamps", "duration_range", "add_under"): (
        Category.TIME_RANGES,
        (
            "timedelta",
            "datetime.timedelta(days=-213504, seconds=1526, microseconds=290449)",
        ),
    ),
    ("timestamps", "duration_range", "add_over"): (
        Category.TIME_RANGES,
        (
            "timedelta",
            "datetime.timedelta(days=213503, seconds=84873, microseconds=709551)",
        ),
    ),
    ("timestamps", "duration_range", "sub_under"): (
        Category.TIME_RANGES,
        (
            "timedelta",
            "datetime.timedelta(days=-213504, seconds=1526, microseconds=290449)",
        ),
    ),
    ("timestamps", "duration_range", "sub_over"): (
        Category.TIME_RANGES,
        (
            "timedelta",
            "datetime.timedelta(days=213503, seconds=84873, microseconds=709551)",
        ),
    ),
}
