import gc
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import BuiltinFunctionType, CodeType, FrameType, MethodDescriptorType


@dataclass
class ObservedCalls:
    functions: list[CodeType] = field(default_factory=list[CodeType])
    builtins: list[str] = field(default_factory=list[str])


def within(caller: FrameType | None, entries: tuple[CodeType, ...]) -> bool:
    while caller is not None:
        if caller.f_code in entries:
            return True
        caller = caller.f_back
    return False


@contextmanager
def observing_calls(calls: ObservedCalls, *entries: CodeType) -> Generator[None]:
    previous = sys.getprofile()
    collecting = gc.isenabled()

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and within(frame.f_back, entries):
            calls.functions.append(frame.f_code)
        elif event == "c_call" and within(frame, entries):
            assert isinstance(argument, BuiltinFunctionType | MethodDescriptorType)
            calls.builtins.append(argument.__qualname__)

    gc.disable()
    sys.setprofile(observe)
    try:
        yield
    finally:
        sys.setprofile(previous)
        if collecting:
            gc.enable()
