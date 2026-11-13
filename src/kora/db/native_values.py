import math
from datetime import datetime
from typing import cast

NAMESPACE_POLICY_NATIVE_LIMIT: int = 65_536


def native_value_bound(value: object, maximum: int | None = None) -> int:
    pending: list[object] = [value]
    total = 0
    while pending:
        current = pending.pop()
        if current is None or type(current) is datetime:
            total += 64
        elif type(current) is bool:
            total += 32
        elif type(current) is int:
            total += 64 + 2 * len(str(current))
        elif type(current) is float and math.isfinite(current):
            total += 1024
        elif type(current) is str:
            total += 64 + 12 * len(current.encode("utf-8"))
        elif type(current) is dict:
            fields = cast(dict[object, object], current)
            if not all(type(key) is str for key in fields):
                raise ValueError("native values require string member names")
            total += 64 + 32 * len(fields)
            for key, child in fields.items():
                pending.extend((key, child))
        elif type(current) is list:
            children = cast(list[object], current)
            total += 64 + 32 * len(children)
            pending.extend(children)
        else:
            raise ValueError("the native value is unsupported")
        if maximum is not None and total > maximum:
            raise ValueError("the native value exceeds its allowance")
    return total
