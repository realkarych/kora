from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from secrets import token_hex

from kora.models.console import LoginSelection
from kora.protocols.time import Clock

CAPACITY = 128
LINK_LIFETIME = timedelta(seconds=60)
SESSION_LIFETIME = timedelta(hours=8)


@dataclass(frozen=True, slots=True)
class Credential:
    digest: str = field(repr=False)
    token_id: str
    owner: str


@dataclass(frozen=True, slots=True)
class LoginLink:
    credential: Credential
    selection: LoginSelection
    expires: datetime


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    credential: Credential
    csrf: str = field(repr=False)
    expires: datetime


class ConsoleSessions:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.links: dict[str, LoginLink] = {}
        self.reserved: set[str] = set()
        self.sessions: dict[str, ConsoleSession] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.links.clear()
        self.reserved.clear()
        self.sessions.clear()

    def issue(self, selection: LoginSelection, credential: Credential) -> str | None:
        now = self.clock.now()
        self.links = {
            key: value for key, value in self.links.items() if value.expires > now
        }
        if self.closed or len(self.links) + len(self.reserved) >= CAPACITY:
            return None
        code = token_hex(32)
        if code in self.links or code in self.reserved:
            raise RuntimeError("A login code collision occurred.")
        self.links[code] = LoginLink(credential, selection, now + LINK_LIFETIME)
        return code

    @contextmanager
    def reserve(self, code: str) -> Generator[LoginLink | None]:
        link = self.links.pop(code, None)
        if link is None or link.expires <= self.clock.now():
            yield None
            return
        self.reserved.add(code)
        try:
            yield link
        finally:
            self.reserved.discard(code)

    def start(
        self, link: LoginLink, now: datetime
    ) -> tuple[str, ConsoleSession] | None:
        self.sessions = {
            key: value for key, value in self.sessions.items() if value.expires > now
        }
        if self.closed or now >= link.expires or len(self.sessions) >= CAPACITY:
            return None
        key = token_hex(32)
        if key in self.sessions:
            raise RuntimeError("A browser session collision occurred.")
        session = ConsoleSession(link.credential, token_hex(32), now + SESSION_LIFETIME)
        self.sessions[key] = session
        return key, session

    def current(self, key: str | None) -> ConsoleSession | None:
        if key is None or (session := self.sessions.get(key)) is None:
            return None
        if session.expires <= self.clock.now():
            del self.sessions[key]
            return None
        return session
