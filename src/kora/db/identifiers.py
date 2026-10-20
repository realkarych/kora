import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime

CROCKFORD_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
IDENTIFIER_LENGTH: int = 26
TIME_BITS: int = 48
RANDOM_BITS: int = 80
RANDOM_BYTES: int = RANDOM_BITS // 8
MAX_TIME_MILLISECONDS: int = (1 << TIME_BITS) - 1
MAX_RANDOM: int = (1 << RANDOM_BITS) - 1
DECODE_TABLE: dict[str, int] = {
    character: index for index, character in enumerate(CROCKFORD_ALPHABET)
}

Clock = Callable[[], datetime]
Randomness = Callable[[int], bytes]


class IdentifierError(ValueError):
    pass


def encode(value: int) -> str:
    if value < 0 or value >> (TIME_BITS + RANDOM_BITS):
        raise IdentifierError("an identifier is a 128-bit value")
    characters: list[str] = []
    for _ in range(IDENTIFIER_LENGTH):
        characters.append(CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(characters))


def decode(text: str) -> int:
    if len(text) != IDENTIFIER_LENGTH:
        raise IdentifierError("an identifier has twenty-six characters")
    value = 0
    for character in text:
        digit = DECODE_TABLE.get(character)
        if digit is None:
            raise IdentifierError("an identifier uses the Crockford alphabet")
        value = (value << 5) | digit
    if value >> (TIME_BITS + RANDOM_BITS):
        raise IdentifierError("an identifier is a 128-bit value")
    return value


def is_identifier(text: str) -> bool:
    try:
        decode(text)
    except IdentifierError:
        return False
    return True


def milliseconds_of(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise IdentifierError("the clock returns aware datetimes")
    milliseconds = int(moment.timestamp() * 1000)
    if milliseconds < 0 or milliseconds > MAX_TIME_MILLISECONDS:
        raise IdentifierError("the clock is outside the identifier range")
    return milliseconds


def timestamp_of(text: str) -> datetime:
    milliseconds = decode(text) >> RANDOM_BITS
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


class IdentifierGenerator:
    def __init__(
        self, clock: Clock, randomness: Randomness = secrets.token_bytes
    ) -> None:
        self._clock = clock
        self._randomness = randomness
        self._lock = threading.Lock()
        self._last_milliseconds = -1
        self._last_random = 0

    def new(self) -> str:
        now = milliseconds_of(self._clock())
        with self._lock:
            if now > self._last_milliseconds:
                self._last_milliseconds = now
                self._last_random = self._fresh_random()
            else:
                self._last_random += 1
                if self._last_random > MAX_RANDOM:
                    self._last_milliseconds += 1
                    self._last_random = self._fresh_random()
            value = (self._last_milliseconds << RANDOM_BITS) | self._last_random
        return encode(value)

    def _fresh_random(self) -> int:
        raw = self._randomness(RANDOM_BYTES)
        if len(raw) != RANDOM_BYTES:
            raise IdentifierError("the randomness source returns ten bytes")
        return int.from_bytes(raw, "big")
