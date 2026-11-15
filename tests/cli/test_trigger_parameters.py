import math
from typing import cast

import pytest

from kora.cli.parameters import trigger_parameters


def test_trigger_parameter_grammar_preserves_original_values() -> None:
    arguments = (
        'literal.key={"text":"a=b\\u0000é","values":[1,1.0,-0.0,true,null]}',
        "integer=9007199254740993",
    )

    result = trigger_parameters(arguments)

    nested = cast(dict[str, object], result["literal.key"])
    values = cast(list[object], nested["values"])
    assert nested["text"] == "a=b\x00é"
    assert type(values[0]) is int
    assert type(values[1]) is float
    assert math.copysign(1, cast(float, values[2])) == -1
    assert values[3:] == [True, None]
    assert result["integer"] == 9_007_199_254_740_993


@pytest.mark.parametrize(
    "arguments",
    [
        ("=1",),
        ("x",),
        ("x=hello",),
        ("x=1", "x=2"),
        ('x={"a":1,"a":2}',),
        ("x=NaN",),
        ("x=1e999",),
        ("x=9223372036854775808",),
        ('x="\\ud800"',),
        ("x=" + "[" * 33 + "0" + "]" * 33,),
    ],
)
def test_trigger_parameter_grammar_refuses_before_transport(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises((ValueError, RecursionError)):
        trigger_parameters(arguments)
