from collections import OrderedDict
from dataclasses import dataclass

from kora.cel import CheckedExpression
from kora.cel_eval.binding import Program
from kora.cel_eval.errors import Fault, raise_safe

DEFAULT_CACHE_ENTRIES: int = 128
DEFAULT_CACHE_CHARACTERS: int = 131_072


@dataclass(frozen=True)
class CacheSnapshot:
    entries: int
    characters: int
    hits: int
    evictions: int


class ProgramCache:
    def __init__(self, entries: int, characters: int) -> None:
        if entries < 1 or characters < 1:
            raise_safe(ValueError("cache limits must be positive"))
        self.max_entries = entries
        self.max_characters = characters
        self._programs: OrderedDict[tuple[str, str], Program] = OrderedDict()
        self._characters = 0
        self._hits = 0
        self._evictions = 0

    def find(self, checked: CheckedExpression, field: str) -> Program | Fault | None:
        key = (checked.digest, field)
        program = self._programs.get(key)
        if program is None:
            return None
        if program.rendering != checked.rendering:
            return Fault.UNMAPPED
        self._programs.move_to_end(key)
        self._hits += 1
        return program

    def put(self, checked: CheckedExpression, field: str, program: Program) -> None:
        length = len(program.rendering)
        if length > self.max_characters:
            return
        while self._programs and (
            len(self._programs) >= self.max_entries
            or self._characters + length > self.max_characters
        ):
            _, previous = self._programs.popitem(last=False)
            self._characters -= len(previous.rendering)
            self._evictions += 1
        self._programs[(checked.digest, field)] = program
        self._characters += length

    def clear(self) -> None:
        self._programs.clear()
        self._characters = 0

    def snapshot(self) -> CacheSnapshot:
        return CacheSnapshot(
            len(self._programs), self._characters, self._hits, self._evictions
        )
