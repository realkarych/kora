import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

MAX_GLOB_CHARACTERS: Final[int] = 4096
MAX_GLOB_SEGMENTS: Final[int] = 32
MAX_PROTECTED_PATHS: Final[int] = 64
MAX_GLOB_PIECES: Final[int] = 8192
MAX_GLOB_BYTES: Final[int] = 65_536
MAX_GLOB_NON_ASCII: Final[int] = 16_384
SURROGATE: Final[re.Pattern[str]] = re.compile("[\ud800-\udfff]")
CALL_CHARGE: Final[int] = 384
POSITION_CHARGE: Final[int] = 960
PIECE_CHARGE: Final[int] = 64
MIDDLE_PIECE_CHARGE: Final[int] = 192
CHARGED_EDGE_PIECES: Final[int] = 2
CONTROL_CHARACTER: Final[re.Pattern[str]] = re.compile("[\x00-\x1f\x7f-\x9f]")
MAX_SEARCHED_PIECE: Final[int] = 96
NEEDLE_WEIGHT_STEP: Final[int] = 2
BASE_WEIGHT: Final[int] = 2
REFUSED_GLOB_CHARACTERS: Final[frozenset[str]] = frozenset("?[]{}\\")
ANY_SEGMENTS: Final[str] = "**"


class FuelExhausted(Exception):
    def __init__(self) -> None:
        super().__init__("the decision ran out of fuel")


class GlobInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("the path glob is invalid")


class Fuel:
    def __init__(self, budget: int) -> None:
        self.budget: int = budget
        self.spent: int = 0

    def charge(self, units: int) -> None:
        self.spent += units
        if self.spent > self.budget:
            raise FuelExhausted


@dataclass(frozen=True)
class StarPattern:
    pieces: tuple[str, ...]
    weight: int

    @property
    def literal(self) -> bool:
        return len(self.pieces) == 1


def needle_weight(length: int) -> int:
    return BASE_WEIGHT + length // NEEDLE_WEIGHT_STEP


def star_pattern(text: str) -> StarPattern:
    parts = text.split("*")
    if len(parts) > 2:
        parts = [parts[0], *[part for part in parts[1:-1] if part], parts[-1]]
    middle = max(map(len, parts[1:-1]), default=0)
    return StarPattern(tuple(parts), needle_weight(middle))


def searchable(pattern: StarPattern) -> bool:
    return max(map(len, pattern.pieces[:-1]), default=0) <= MAX_SEARCHED_PIECE


def star_match(pattern: StarPattern, text: str, fuel: Fuel, start: int = 0) -> bool:
    pieces = pattern.pieces
    first, last = pieces[0], pieces[-1]
    fuel.charge(
        CALL_CHARGE
        + PIECE_CHARGE * min(len(pieces), CHARGED_EDGE_PIECES)
        + MIDDLE_PIECE_CHARGE * max(len(pieces) - CHARGED_EDGE_PIECES, 0)
        + len(first)
        + len(last)
    )
    if pattern.literal:
        fuel.charge(len(text) - start)
        return text[start:] == first
    end = len(text) - len(last)
    if end < start + len(first):
        return False
    if not text.startswith(first, start) or not text.endswith(last):
        return False
    position = start + len(first)
    middle = pieces[1:-1]
    if middle:
        fuel.charge((end - position) * pattern.weight)
    for piece in middle:
        found = text.find(piece, position, end)
        if found < 0:
            return False
        position = found + len(piece)
    return True


def folded(text: str) -> str:
    if text.isascii():
        return text.lower()
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


def has_control_character(text: str) -> bool:
    return CONTROL_CHARACTER.search(text) is not None


def relative_path_pattern_valid(text: str) -> bool:
    if not text or text.startswith(("/", "~")) or has_control_character(text):
        return False
    return all(segment not in ("", ".", "..") for segment in text.split("/"))


@dataclass(frozen=True)
class PathSubject:
    segments: tuple[str, ...]


def normalised_segments(path: str) -> list[str]:
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return segments


def folded_subject(folded_path: str) -> PathSubject:
    return PathSubject(tuple(normalised_segments(folded_path)))


@dataclass(frozen=True)
class PathGlob:
    runs: tuple[tuple[StarPattern, ...], ...]
    anchored: bool

    def matches(self, subject: PathSubject, fuel: Fuel) -> bool:
        fuel.charge(CALL_CHARGE)
        segments = subject.segments
        position = 0
        for index, run in enumerate(self.runs):
            if index == 0 and self.anchored:
                if not run_matches_at(run, segments, 0, fuel):
                    return False
                position = len(run)
                continue
            found = first_run_match(run, segments, position, fuel)
            if found is None:
                return False
            position = found + len(run)
        return True


def run_matches_at(
    run: tuple[StarPattern, ...], segments: tuple[str, ...], at: int, fuel: Fuel
) -> bool:
    fuel.charge(POSITION_CHARGE)
    if at + len(run) > len(segments):
        return False
    return all(
        star_match(pattern, segments[at + offset], fuel)
        for offset, pattern in enumerate(run)
    )


def first_run_match(
    run: tuple[StarPattern, ...], segments: tuple[str, ...], start: int, fuel: Fuel
) -> int | None:
    for at in range(start, len(segments) - len(run) + 1):
        if run_matches_at(run, segments, at, fuel):
            return at
    return None


def utf8_size(texts: Iterable[str]) -> int:
    return sum(len(text.encode("utf-8", "surrogatepass")) for text in texts)


def non_ascii_count(texts: Iterable[str]) -> int:
    return sum(len(text) - len(text.encode("ascii", "ignore")) for text in texts)


def glob_piece_count(texts: Iterable[str]) -> int:
    return sum(text.count("*") + text.count("/") + 1 for text in texts)


def glob_segments(text: str) -> list[str]:
    if (
        len(text) > MAX_GLOB_CHARACTERS
        or SURROGATE.search(text) is not None
        or not relative_path_pattern_valid(text)
        or not REFUSED_GLOB_CHARACTERS.isdisjoint(text)
    ):
        raise GlobInvalid
    segments = text.split("/")
    if len(segments) > MAX_GLOB_SEGMENTS or any(
        ANY_SEGMENTS in segment and segment != ANY_SEGMENTS for segment in segments
    ):
        raise GlobInvalid
    return segments


def touches_mark(pattern: StarPattern) -> bool:
    last = len(pattern.pieces) - 1
    return last > 0 and any(
        (index < last and unicodedata.combining(piece[-1]) != 0)
        or (index > 0 and unicodedata.combining(piece[0]) != 0)
        for index, piece in enumerate(pattern.pieces)
        if piece
    )


def compile_glob(text: str) -> PathGlob:
    segments = glob_segments(text)
    runs: list[tuple[StarPattern, ...]] = []
    current: list[StarPattern] = []
    for segment in segments:
        if segment == ANY_SEGMENTS:
            if current:
                runs.append(tuple(current))
            current = []
        else:
            pattern = star_pattern(folded(segment))
            if not searchable(pattern) or touches_mark(pattern):
                raise GlobInvalid
            current.append(pattern)
    if current:
        runs.append(tuple(current))
    return PathGlob(tuple(runs), segments[0] != ANY_SEGMENTS)


def compile_globs(texts: tuple[str, ...]) -> tuple[PathGlob, ...]:
    if (
        len(texts) > MAX_PROTECTED_PATHS
        or glob_piece_count(texts) > MAX_GLOB_PIECES
        or utf8_size(texts) > MAX_GLOB_BYTES
        or non_ascii_count(texts) > MAX_GLOB_NON_ASCII
    ):
        raise GlobInvalid
    return tuple(compile_glob(text) for text in texts)
