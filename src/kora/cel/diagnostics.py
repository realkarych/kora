from dataclasses import dataclass
from enum import Enum


class Code(Enum):
    SYNTAX = "IR011"
    LIMIT = "IR027"


class Reason(Enum):
    UNEXPECTED_INPUT = "unexpected_input"
    UNEXPECTED_END = "unexpected_end"
    INVALID_CHARACTER = "invalid_character"
    INVALID_ESCAPE = "invalid_escape"
    INVALID_LITERAL = "invalid_literal"
    UNTERMINATED_LITERAL = "unterminated_literal"
    RESERVED_IDENTIFIER = "reserved_identifier"
    MESSAGE_CONSTRUCTION = "message_construction"
    OPTIONAL_SYNTAX = "optional_syntax"
    QUALIFIED_NAME = "qualified_name"
    QUOTED_FIELD = "quoted_field"
    TWO_VARIABLE_COMPREHENSION = "two_variable_comprehension"
    MALFORMED_MACRO = "malformed_macro"
    UNKNOWN_FUNCTION = "unknown_function"
    NON_LITERAL_PATTERN = "non_literal_pattern"
    UNSUPPORTED_PATTERN = "unsupported_pattern"
    INVALID_PATTERN = "invalid_pattern"
    INVALID_PATH = "invalid_path"
    RENDERING_MISMATCH = "rendering_mismatch"
    UNKNOWN_IDENTIFIER = "unknown_identifier"
    ROOT_NOT_SELECTED = "root_not_selected"
    UNKNOWN_FIELD = "unknown_field"
    SHADOWED_VARIABLE = "shadowed_variable"
    UNKNOWN_ENUM_VALUE = "unknown_enum_value"
    NO_MATCHING_OVERLOAD = "no_matching_overload"
    UNGUARDED_OPTIONAL = "unguarded_optional"
    STRING_SIZE = "string_size"
    REPEATED_MAP_KEY = "repeated_map_key"
    WRONG_RESULT_TYPE = "wrong_result_type"
    LENGTH = "length"
    DEPTH = "depth"
    COMPREHENSION_COUNT = "comprehension_count"
    COMPREHENSION_NESTING = "comprehension_nesting"
    PATTERN_NESTING = "pattern_nesting"
    PATTERN_SIZE = "pattern_size"
    STEPS = "steps"
    MEMORY = "memory"

    @property
    def code(self) -> Code:
        return Code.LIMIT if self in LIMIT_REASONS else Code.SYNTAX


LIMIT_REASONS: frozenset[Reason] = frozenset(
    {
        Reason.LENGTH,
        Reason.DEPTH,
        Reason.COMPREHENSION_COUNT,
        Reason.COMPREHENSION_NESTING,
        Reason.PATTERN_NESTING,
        Reason.PATTERN_SIZE,
        Reason.STEPS,
        Reason.MEMORY,
    }
)

MESSAGES: dict[Reason, str] = {
    Reason.UNEXPECTED_INPUT: "unexpected token",
    Reason.UNEXPECTED_END: "unexpected end of expression",
    Reason.INVALID_CHARACTER: "character not allowed in an expression",
    Reason.INVALID_ESCAPE: "invalid escape sequence",
    Reason.INVALID_LITERAL: "invalid or out-of-range literal",
    Reason.UNTERMINATED_LITERAL: "unterminated string or bytes literal",
    Reason.RESERVED_IDENTIFIER: "reserved word used as an identifier",
    Reason.MESSAGE_CONSTRUCTION: "message construction is not supported",
    Reason.OPTIONAL_SYNTAX: "optional syntax is not supported",
    Reason.QUALIFIED_NAME: "leading-dot qualified names are not supported",
    Reason.QUOTED_FIELD: "backtick-quoted fields are not supported",
    Reason.TWO_VARIABLE_COMPREHENSION: (
        "two-variable comprehensions are not supported"
    ),
    Reason.MALFORMED_MACRO: "malformed macro call",
    Reason.UNKNOWN_FUNCTION: "function is not in the standard library kora allows",
    Reason.NON_LITERAL_PATTERN: "matches() takes a string literal pattern",
    Reason.UNSUPPORTED_PATTERN: (
        "regular-expression construct outside the supported subset"
    ),
    Reason.INVALID_PATTERN: "invalid regular expression",
    Reason.INVALID_PATH: "a path allows only field selection and literal indexes",
    Reason.RENDERING_MISMATCH: "canonical rendering does not parse to the same tree",
    Reason.UNKNOWN_IDENTIFIER: "identifier is not declared in this environment",
    Reason.ROOT_NOT_SELECTED: "this variable is read only through field selection",
    Reason.UNKNOWN_FIELD: "field is not declared by the schema",
    Reason.SHADOWED_VARIABLE: "comprehension variable shadows an environment variable",
    Reason.UNKNOWN_ENUM_VALUE: "string literal is not an allowed value of the enum",
    Reason.NO_MATCHING_OVERLOAD: "no operator or function accepts these operand types",
    Reason.UNGUARDED_OPTIONAL: "optional field is read without a has() or in guard",
    Reason.STRING_SIZE: "size() of a string or dyn value counts bytes, not characters",
    Reason.REPEATED_MAP_KEY: "map literal repeats a key",
    Reason.WRONG_RESULT_TYPE: "expression result does not have the required type",
    Reason.LENGTH: "expression is longer than 4096 code points",
    Reason.DEPTH: "expression nests deeper than 32 levels",
    Reason.COMPREHENSION_COUNT: "more than 4 comprehension macros in one expression",
    Reason.COMPREHENSION_NESTING: "comprehension macros nest deeper than 2",
    Reason.PATTERN_NESTING: "regular expression nests deeper than 32 levels",
    Reason.PATTERN_SIZE: "regular expression exceeds the size estimate cap",
    Reason.STEPS: "worst-case evaluation cost exceeds the step budget",
    Reason.MEMORY: "worst-case evaluation memory exceeds the memory budget",
}


@dataclass(frozen=True)
class Diagnostic:
    reason: Reason
    offset: int

    @property
    def code(self) -> Code:
        return self.reason.code

    @property
    def message(self) -> str:
        return MESSAGES[self.reason]


class DiagnosticError(Exception):
    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.reason.value)
        self.diagnostic: Diagnostic = diagnostic


def fail(reason: Reason, offset: int) -> DiagnosticError:
    return DiagnosticError(Diagnostic(reason, offset))
