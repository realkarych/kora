import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

from kora.cel import CheckedExpression, JsonValue, ValueBound, Variable, check
from kora.cel.cost import DATA_BOUND, DATA_BYTES
from kora.cel.types import DOUBLE, DYN, INT, STRING, CelType, ListOf, MapOf
from tests.measurements.cel_cost_workload import ConversionCase, digest

SMALL_SIZES: tuple[int, ...] = (8, 16, 32, 64)
TEXT_SIZES: tuple[int, ...] = (64, 128, 256, 512)


class ResultRecipe(Enum):
    CONCAT_STRING = "concat_string"
    REPEAT_TEXT = "repeat_text"


@dataclass(frozen=True)
class CostCase:
    family: str
    size: int
    coefficient: str
    units: int
    source: str
    control: str
    bindings: dict[str, JsonValue]
    types: dict[str, CelType]
    expected_data: JsonValue | ResultRecipe
    control_expected: JsonValue
    phase: Literal["native", "compile", "production"] = "native"
    batch: int = 32
    declared_bounds: dict[str, ValueBound] | None = None

    @property
    def name(self) -> str:
        return f"{self.family}-{self.size}"

    @property
    def expected(self) -> JsonValue:
        if self.expected_data is ResultRecipe.REPEAT_TEXT:
            text = self.bindings["text"]
            assert isinstance(text, str)
            return text * self.size
        if self.expected_data is ResultRecipe.CONCAT_STRING:
            left = self.bindings["left"]
            right = self.bindings["right"]
            assert isinstance(left, str)
            assert isinstance(right, str)
            return (left + right) * 4
        assert not isinstance(self.expected_data, ResultRecipe)
        return self.expected_data

    def environment(self) -> dict[str, Variable]:
        environment: dict[str, Variable] = {}
        for name, value in self.bindings.items():
            kind = self.types[name]
            if self.declared_bounds is not None:
                bound = self.declared_bounds[name]
            elif kind == INT:
                bound = ValueBound(20, 1, 0, 0)
            elif kind == STRING:
                assert isinstance(value, str)
                bound = ValueBound(
                    min(DATA_BYTES, 6 * len(value) + 2), 1, 0, 4 * len(value)
                )
            elif kind == ListOf(INT):
                assert isinstance(value, list)
                bound = ValueBound(
                    min(DATA_BYTES, 21 * len(value) + 2), len(value) + 1, len(value), 0
                )
            else:
                raise ValueError("undeclared cost binding type")
            environment[name] = Variable(kind, bound)
        return environment

    def checked(self) -> CheckedExpression:
        result = check(self.source, self.environment())
        assert isinstance(result, CheckedExpression), (self.name, result)
        return result

    def control_checked(self) -> CheckedExpression:
        result = check(self.control, self.environment())
        assert isinstance(result, CheckedExpression), (self.name, result)
        return result

    def metadata(self) -> dict[str, object]:
        return {
            "case": self.name,
            "family": self.family,
            "size": self.size,
            "coefficient": self.coefficient,
            "units": self.units,
            "source": self.source,
            "control": self.control,
            "input_sha256": digest(self.bindings),
            "variable_types": {name: repr(kind) for name, kind in self.types.items()},
            "expected_sha256": digest(self.expected),
            "control_expected_sha256": digest(self.control_expected),
            "rendering_sha256": self.checked().digest,
            "control_rendering_sha256": self.control_checked().digest,
            "phase": self.phase,
            "calls": self.batch,
        }


def repeated_booleans(source: str) -> str:
    return "[" + ",".join([source] * 8) + "].all(p, p)"


def reference_case() -> CostCase:
    values: list[JsonValue] = list(range(64))
    source = "values.all(x, x >= 0)"
    return CostCase(
        "reference",
        64,
        "reference",
        64,
        source,
        source,
        {"values": values},
        {"values": ListOf(INT)},
        True,
        True,
    )


TEMPORAL_GETTERS: tuple[tuple[str, int], ...] = (
    ("getDate", 2),
    ("getDayOfMonth", 1),
    ("getDayOfWeek", 5),
    ("getDayOfYear", 1),
    ("getFullYear", 2026),
    ("getHours", 3),
    ("getMilliseconds", 6),
    ("getMinutes", 4),
    ("getMonth", 0),
    ("getSeconds", 5),
)
OPERATION_IDENTITIES: tuple[tuple[str, int], ...] = (
    (("evaluation", 1),)
    + tuple(
        (family, size)
        for size in (2, 4, 8, 16)
        for family in ("node_arithmetic", "node_boolean")
    )
    + tuple(
        (family, size)
        for size in SMALL_SIZES
        for family in (
            "iteration_all",
            "iteration_exists",
            "iteration_exists_one",
            "quadratic_map",
            "quadratic_filter",
            "concat_list",
            "comparison_values",
            "membership_values",
        )
    )
    + tuple(
        (family, size)
        for size in TEXT_SIZES
        for family in (
            "concat_string",
            "comparison_bytes",
            "collection_string",
            "function_contains",
            "function_startsWith",
            "function_endsWith",
            "function_bytes",
        )
    )
    + tuple(("collection_values", size) for size in (4, 8, 16, 32))
    + tuple(
        (family, size)
        for size in (2, 4, 8, 16)
        for family in (
            "function_int",
            "function_uint",
            "function_double",
            *("node_" + name for name, _ in TEMPORAL_GETTERS),
        )
    )
    + tuple(
        (family, size)
        for size in (16, 32, 64, 128)
        for family in ("function_string_control", "function_string_invalid")
    )
    + tuple(
        (family, size)
        for size in (1, 2, 4, 8)
        for family in ("function_duration", "function_timestamp")
    )
    + tuple(("compile_rendering", size) for size in SMALL_SIZES)
)


def operation_case(family: str, size: int) -> CostCase:
    if type(size) is not int or (family, size) not in OPERATION_IDENTITIES:
        raise ValueError("undeclared cost case")
    if family == "evaluation":
        return CostCase(
            family,
            size,
            "evaluation",
            1,
            "true",
            "true",
            {},
            {},
            True,
            True,
            "production",
        )
    if family == "node_arithmetic":
        additions = ",".join(["1+1"] * (8 * size))
        numbers = ",".join(["2"] * (8 * size))
        return CostCase(
            family,
            size,
            "node",
            16 * size,
            f"[{additions}].size()=={8 * size}",
            f"[{numbers}].size()=={8 * size}",
            {},
            {},
            True,
            True,
        )
    if family == "node_boolean":
        return CostCase(
            family,
            size,
            "node",
            16 * size - 16,
            repeated_booleans("&&".join(["true"] * size)),
            repeated_booleans("true"),
            {},
            {},
            True,
            True,
        )
    if family.startswith(("iteration_", "quadratic_")) or family in (
        "concat_list",
        "comparison_values",
        "membership_values",
    ):
        values: list[JsonValue] = list(range(size))
        bindings: dict[str, JsonValue] = {"values": values}
        types: dict[str, CelType] = {"values": ListOf(INT)}
        if family.startswith("iteration_"):
            macro = family.removeprefix("iteration_")
            predicate = "x>=0" if macro == "all" else f"x=={size - 1}"
            return CostCase(
                family,
                size,
                "iteration",
                size,
                f"values.{macro}(x,{predicate})",
                f"values.size()=={size}",
                bindings,
                types,
                True,
                True,
            )
        if family.startswith("quadratic_"):
            macro = family.removeprefix("quadratic_")
            body = "x" if macro == "map" else "true"
            return CostCase(
                family,
                size,
                "map_quadratic",
                size * size,
                f"values.{macro}(x,{body}).size()=={size}",
                "values.all(x,x>=0)",
                bindings,
                types,
                True,
                True,
            )
        if family == "membership_values":
            bindings["needle"] = size - 1
            types["needle"] = INT
            return CostCase(
                family,
                size,
                "compare_value",
                8 * (size + 2),
                repeated_booleans("needle in values"),
                repeated_booleans("true"),
                bindings,
                types,
                True,
                True,
            )
        bindings = {"left": values, "right": list(values)}
        types = {"left": ListOf(INT), "right": ListOf(INT)}
        if family == "concat_list":
            return CostCase(
                family,
                size,
                "concat_value",
                35 * (size + 1),
                "(" + "+".join(["left", "right"] * 4) + f").size()=={8 * size}",
                f"left.size()+right.size()=={2 * size}",
                bindings,
                types,
                True,
                True,
            )
        return CostCase(
            family,
            size,
            "compare_value",
            16 * (size + 1),
            repeated_booleans("left==right"),
            repeated_booleans("true"),
            bindings,
            types,
            True,
            True,
        )
    if family in ("concat_string", "comparison_bytes"):
        left = "a" * size
        right = ("b" if family == "concat_string" else "a") * size
        if family == "concat_string":
            return CostCase(
                family,
                size,
                "concat_byte",
                35 * (size + 2),
                "+".join(["left", "right"] * 4),
                "left",
                {"left": left, "right": right},
                {"left": STRING, "right": STRING},
                ResultRecipe.CONCAT_STRING,
                left,
            )
        return CostCase(
            family,
            size,
            "compare_byte",
            16 * (size + 2),
            repeated_booleans("left==right"),
            repeated_booleans("true"),
            {"left": left, "right": right},
            {"left": STRING, "right": STRING},
            True,
            True,
        )
    if family == "collection_string":
        return CostCase(
            family,
            size,
            "concat_byte",
            8 * (2 * (size + 2) + 3),
            repeated_booleans("[text,text].size()==2"),
            repeated_booleans("text==text"),
            {"text": "a" * size},
            {"text": STRING},
            True,
            True,
        )
    if family in ("function_contains", "function_startsWith", "function_endsWith"):
        function = family.removeprefix("function_")
        text = (
            "z" + "a" * (size - 1)
            if function == "startsWith"
            else "a" * (size - 1) + "z"
        )
        return CostCase(
            family,
            size,
            "function_byte",
            8 * (size + 5),
            repeated_booleans(f"text.{function}('z')"),
            repeated_booleans("true"),
            {"text": text},
            {"text": STRING},
            True,
            True,
        )
    if family == "function_bytes":
        return CostCase(
            family,
            size,
            "function_byte",
            size + 2,
            f"bytes(text).size()=={size}",
            "true",
            {"text": "a" * size},
            {"text": STRING},
            True,
            True,
        )
    if family == "collection_values":
        elements = ",".join(["value"] * size)
        return CostCase(
            family,
            size,
            "concat_value",
            size + 1,
            f"[{elements}].size()=={size}",
            "value==value",
            {"value": 1},
            {"value": INT},
            True,
            True,
        )
    if family in ("function_int", "function_uint", "function_double"):
        text = "1" * size
        function = family.removeprefix("function_")
        suffix = {"int": "", "uint": "u", "double": ".0"}[function]
        return CostCase(
            family,
            size,
            "function_byte",
            8 * (size + 2),
            repeated_booleans(f"{function}(text)=={text}{suffix}"),
            repeated_booleans("true"),
            {"text": text},
            {"text": STRING},
            True,
            True,
        )
    if family.startswith("node_"):
        getter = family.removeprefix("node_")
        expected = dict(TEMPORAL_GETTERS)[getter]
        fields = ",".join([f"stamp.{getter}()"] * (8 * size))
        constants = ",".join([str(expected)] * (8 * size))
        prefix = "[timestamp('2026-01-02T03:04:05.006Z')].all(stamp,["
        suffix = f"].all(p,p=={expected}))"
        return CostCase(
            family,
            size,
            "node",
            8 * size,
            prefix + fields + suffix,
            prefix + constants + suffix,
            {},
            {},
            True,
            True,
        )
    if family in ("function_string_control", "function_string_invalid"):
        escape, character = (
            ("00", "\x00") if family == "function_string_control" else ("ff", "\ufffd")
        )
        literal = "b'" + f"\\x{escape}" * size + "'"
        strings = ",".join([f"string({literal})"] * 4)
        expected_string = json.dumps(character * size, ensure_ascii=False)
        return CostCase(
            family,
            size,
            "function_byte",
            4 * (size + 2),
            f"[{strings}].all(x,x=={expected_string})",
            "['','','',''].all(x,x=='')",
            {},
            {},
            True,
            True,
        )
    if family == "function_duration":
        duration = "1" * size + "ns"
        milliseconds = int("1" * size) // 1_000_000
        return CostCase(
            family,
            size,
            "function_byte",
            len(duration) + 2,
            f"duration(text).getMilliseconds()=={milliseconds}",
            "true",
            {"text": duration},
            {"text": STRING},
            True,
            True,
        )
    if family == "function_timestamp":
        timestamp = "2026-01-01T00:00:00." + "1" * size + "Z"
        milliseconds = 100 if size == 1 else 110 if size == 2 else 111
        return CostCase(
            family,
            size,
            "function_byte",
            len(timestamp) + 2,
            f"timestamp(text).getMilliseconds()=={milliseconds}",
            "true",
            {"text": timestamp},
            {"text": STRING},
            True,
            True,
        )
    if family == "compile_rendering":
        source = "[" + ",".join(["true"] * size) + f"].size()=={size}"
        case = CostCase(
            family,
            size,
            "rendering_character",
            1,
            source,
            "true",
            {},
            {},
            True,
            True,
            "compile",
        )
        units = len(case.checked().rendering) - len(case.control_checked().rendering)
        return CostCase(
            family,
            size,
            case.coefficient,
            units,
            source,
            "true",
            {},
            {},
            True,
            True,
            "compile",
        )
    raise ValueError("undeclared cost case")


def operation_cases() -> tuple[CostCase, ...]:
    return tuple(operation_case(family, size) for family, size in OPERATION_IDENTITIES)


def cost_family_cases(family: str) -> tuple[CostCase, ...]:
    if family.startswith("regex_"):
        from tests.measurements.cel_regex_workload import REGEX_IDENTITIES, regex_case

        return tuple(
            regex_case(name, size) for name, size in REGEX_IDENTITIES if name == family
        )
    return tuple(
        operation_case(name, size)
        for name, size in OPERATION_IDENTITIES
        if name == family
    )


MEMORY_IDENTITIES: tuple[tuple[str, int], ...] = (
    tuple(
        ("memory_control_" + name, 1)
        for name in ("true", "arithmetic", "negation", "comparison")
    )
    + tuple(("memory_control_arithmetic_tree", size) for size in (128, 256, 512, 1024))
    + tuple(("memory_control_boolean_tree", size) for size in (64, 128, 256, 512))
    + (("memory_control_depth", 32),)
    + tuple(
        ("memory_data_" + name, 1)
        for name in (
            "ascii",
            "unicode",
            "escapes",
            "empty_lists",
            "small_maps",
            "deep_chains",
            "numbers",
            "key_heavy",
            "mixed",
            "many_variables",
        )
    )
    + tuple(("memory_intermediate", size) for size in (8, 16, 32, 64))
    + (("memory_intermediate", 128), ("memory_intermediate", 250))
    + tuple(("memory_string_delivery", size) for size in (1, 2, 4, 8))
    + tuple(
        (family, size)
        for size in (16, 32, 64, 128)
        for family in ("memory_map_literal", "memory_map_typed", "memory_map_dyn")
    )
    + tuple(("memory_empty_result", size) for size in (1, 2, 3, 4))
    + tuple(
        (family, size)
        for size in (64, 128, 256, 512)
        for family in ("memory_literal_unicode", "memory_literal_escapes")
    )
    + (("memory_four_macros", 32),)
    + tuple(
        (family, size)
        for family, size in OPERATION_IDENTITIES
        if (family, size)
        in (
            ("concat_string", 512),
            ("concat_list", 64),
            ("function_string_control", 128),
            ("function_string_invalid", 128),
        )
    )
)


def memory_case(family: str, size: int) -> CostCase:
    if family.startswith("regex_"):
        from tests.measurements.cel_regex_workload import (
            REGEX_MEMORY_IDENTITIES,
            regex_case,
        )

        if type(size) is not int or (family, size) not in REGEX_MEMORY_IDENTITIES:
            raise ValueError("undeclared memory case")
        return replace(regex_case(family, size), batch=1)
    if type(size) is not int or (family, size) not in MEMORY_IDENTITIES:
        raise ValueError("undeclared memory case")
    if family in {name for name, _ in OPERATION_IDENTITIES}:
        return replace(operation_case(family, size), batch=1)
    if family.startswith("memory_control_"):
        if family in ("memory_control_arithmetic_tree", "memory_control_boolean_tree"):
            leaf, operator = (
                ("1", "+") if family.endswith("arithmetic_tree") else ("true", "&&")
            )
            level = [leaf] * size
            if leaf == "1" and size == 1024:
                level[0] = "11"
            while len(level) > 1:
                level = [
                    f"({left}{operator}{right})"
                    for left, right in zip(level[::2], level[1::2], strict=True)
                ]
            source = level[0] + (">0" if leaf == "1" else "")
            return CostCase(
                family, size, "memory", 0, source, "true", {}, {}, True, True, batch=1
            )
        if family == "memory_control_depth":
            return CostCase(
                family,
                size,
                "memory",
                0,
                "false?false:" * size + "true",
                "true",
                {},
                {},
                True,
                True,
                batch=1,
            )
        source = {
            "true": "true",
            "arithmetic": "1+1==2",
            "negation": "!false",
            "comparison": "1<2&&2<3",
        }[family.removeprefix("memory_control_")]
        return CostCase(
            family, size, "memory", 0, source, "true", {}, {}, True, True, batch=1
        )
    if family == "memory_intermediate":
        row = ",".join(["x"] * 64)
        repeated = ",".join(["row"] * 8)
        source = f"values.map(x,[{row}]).map(row,[{repeated}]).size()=={size}"
        values: list[JsonValue] = list(range(size))
        return CostCase(
            family,
            size,
            "memory",
            size,
            source,
            "true",
            {"values": values},
            {"values": ListOf(INT)},
            True,
            True,
            batch=1,
        )
    if family == "memory_string_delivery":
        return CostCase(
            family,
            size,
            "memory",
            size,
            "+".join(["text"] * size),
            "true",
            {"text": "a" * 2046},
            {"text": STRING},
            ResultRecipe.REPEAT_TEXT,
            True,
            batch=1,
        )
    if family == "memory_map_literal":
        fields = ",".join(f"'k{index:03}':value" for index in range(size))
        return CostCase(
            family,
            size,
            "memory",
            size,
            "{" + fields + f"}}.size()=={size}",
            "true",
            {"value": 1},
            {"value": INT},
            True,
            True,
            batch=1,
        )
    if family in ("memory_map_typed", "memory_map_dyn"):
        kind = MapOf(STRING, INT) if family == "memory_map_typed" else DYN
        mapping: dict[str, JsonValue] = {f"k{index:03}": index for index in range(size)}
        source = f"values.filter(k,values[k]>=0).map(k,values[k]).size()=={size}"
        return CostCase(
            family,
            size,
            "memory",
            size,
            source,
            "true",
            {"values": mapping},
            {"values": kind},
            True,
            True,
            batch=1,
            declared_bounds={"values": ValueBound(13 * size + 2, size + 1, size, 0)},
        )
    if family == "memory_empty_result":
        source = (
            "[].size()==0",
            "{}.size()==0",
            "[].map(x,x).size()==0",
            "[].filter(x,true).size()==0",
        )[size - 1]
        return CostCase(
            family, size, "memory", size, source, "true", {}, {}, True, True, batch=1
        )
    if family in ("memory_literal_unicode", "memory_literal_escapes"):
        literal = json.dumps(
            ("\U0001f642" if family.endswith("unicode") else "\x00") * size,
            ensure_ascii=False,
        )
        return CostCase(
            family,
            size,
            "memory",
            size,
            f"{literal}.size()=={size * (4 if family.endswith('unicode') else 1)}",
            "true",
            {},
            {},
            True,
            True,
            batch=1,
        )
    if family == "memory_four_macros":
        source = (
            "values.all(x,[x].exists(y,y==x))&&"
            f"values.filter(x,true).map(x,x).size()=={size}"
        )
        values = list(range(size))
        return CostCase(
            family,
            size,
            "memory",
            size,
            source,
            "true",
            {"values": values},
            {"values": ListOf(INT)},
            True,
            True,
            batch=1,
        )
    name = family.removeprefix("memory_data_")
    if name == "many_variables":
        bindings: dict[str, JsonValue] = {
            f"v{index}": ["a" * 28] for index in range(128)
        }
        operations = ",".join(f"{key}.size()" for key in bindings)
        expected = ",".join(["1"] * 128)
        return CostCase(
            family,
            size,
            "memory",
            1,
            f"[{operations}]==[{expected}]",
            "true",
            bindings,
            {key: DYN for key in bindings},
            True,
            True,
            batch=1,
            declared_bounds={key: ValueBound(32, 2, 1, 28) for key in bindings},
        )
    kind: CelType = DYN
    value: list[JsonValue]
    if name in ("ascii", "unicode", "escapes"):
        character, width = {
            "ascii": ("a", 1),
            "unicode": ("\U0001f642", 4),
            "escapes": ("\x00", 6),
        }[name]
        value = [character * ((4096 - 13) // (4 * width)) for _ in range(4)]
    elif name == "empty_lists":
        value = [[] for _ in range(255)]
    elif name == "small_maps":
        value = [{f"k{index}": None for index in range(6)} for _ in range(36)]
    elif name in ("deep_chains", "mixed"):
        from tests.measurements.cel_bound_workload import narrow_chain

        value = [narrow_chain(32) for _ in range(7)]
        if name == "deep_chains":
            value.append(narrow_chain(31))
        else:
            value.append("a" * 3637)
    elif name == "numbers":
        value = [list(range(14)) for _ in range(16)]
        kind = ListOf(ListOf(DOUBLE))
    elif name == "key_heavy":
        entries: dict[str, JsonValue] = {f"k{index:03}": None for index in range(249)}
        entries["k249" + "a" * 1093] = None
        value = [entries]
    else:
        raise ValueError("undeclared memory data recipe")
    source = f"values.size()=={len(value)}"
    if name == "numbers":
        source += "&&values[0][0]==0.0"
    return CostCase(
        family,
        size,
        "memory",
        1,
        source,
        "true",
        {"values": value},
        {"values": kind},
        True,
        True,
        batch=1,
        declared_bounds={"values": DATA_BOUND},
    )


AXIS_FAMILIES: tuple[str, ...] = ("convert_bytes", "convert_values")


def axis_cases(family: str) -> tuple[ConversionCase, ...]:
    if family not in AXIS_FAMILIES:
        raise ValueError("undeclared conversion axis")
    result: list[ConversionCase] = []
    for size in (
        (512, 1024, 2048, 4096) if family == "convert_bytes" else (16, 32, 64, 128)
    ):
        if family == "convert_bytes":
            target: JsonValue = "a" * (size - 2)
            control: JsonValue = ""
            kind: CelType = STRING
            expected = size - 2
            constraints = ("constant_value_count_1",)
        else:
            target = ["a" * (4098 - 3 * size), *[[] for _ in range(size - 2)]]
            control = ["a" * 4092]
            kind = DYN
            expected = size - 1
            constraints = ("constant_canonical_bytes_4096",)
        result.append(
            ConversionCase(
                f"{family}-{size}",
                f"values.size()=={expected}",
                {"values": target},
                {"values": kind},
                {"values": control},
                32,
                4096,
                256,
                constraints,
            )
        )
    return tuple(result)
