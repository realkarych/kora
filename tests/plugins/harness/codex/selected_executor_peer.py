import os
import select
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Protocol, runtime_checkable

from aiohttp import web

TABLE = Path("/proc/net/tcp")
MAX_TABLE_BYTES = 256 * 1024
MAX_ROWS = 1024
MAX_DESCRIPTORS = 256


class PeerObservationError(ValueError):
    pass


@runtime_checkable
class SocketDescriptor(Protocol):
    def fileno(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Connection:
    local: bytes
    remote: bytes
    server_inode: int


@dataclass(frozen=True, slots=True)
class Observation:
    client_inode: int
    app_owns: bool
    coordinator_owns: bool


def loopback_key(endpoint: object) -> bytes:
    match endpoint:
        case ("127.0.0.1", int(port)) if type(port) is int and 0 < port <= 65_535:
            return f"0100007F:{port:04X}".encode()
        case _:
            raise PeerObservationError("relay peer is not selected IPv4 loopback")


def connection(request: web.Request) -> Connection:
    transport = request.transport
    if transport is None:
        raise PeerObservationError("relay peer transport unavailable")
    endpoint: object = transport.get_extra_info("socket")
    if not isinstance(endpoint, SocketDescriptor):
        raise PeerObservationError("relay peer descriptor unavailable")
    local: object = transport.get_extra_info("sockname")
    remote: object = transport.get_extra_info("peername")
    return Connection(
        loopback_key(local),
        loopback_key(remote),
        os.fstat(endpoint.fileno()).st_ino,
    )


def table_rows() -> list[list[bytes]]:
    with TABLE.open("rb") as table:
        raw = table.read(MAX_TABLE_BYTES + 1)
    lines = raw.splitlines()
    if (
        len(raw) > MAX_TABLE_BYTES
        or not lines
        or len(lines) > MAX_ROWS + 1
        or not lines[0].lstrip().startswith(b"sl")
    ):
        raise PeerObservationError("relay TCP observation exceeds fixture bounds")
    rows = [line.split() for line in lines[1:]]
    if any(len(row) < 10 for row in rows):
        raise PeerObservationError("relay TCP observation is incomplete")
    return rows


def connected_inode(rows: list[list[bytes]], local: bytes, remote: bytes) -> int:
    matches = [row for row in rows if row[1:4] == [local, remote, b"01"]]
    if len(matches) != 1 or int(matches[0][7]) != os.geteuid():
        raise PeerObservationError("relay connected row unavailable")
    inode = int(matches[0][9])
    if inode <= 0:
        raise PeerObservationError("relay connected inode unavailable")
    return inode


def owns_socket(pid: int, inode: int) -> bool:
    socket_name = f"socket:[{inode}]"
    owned = False
    with os.scandir(f"/proc/{pid}/fd") as entries:
        for index, entry in enumerate(islice(entries, MAX_DESCRIPTORS + 1)):
            if index == MAX_DESCRIPTORS:
                raise PeerObservationError(
                    "relay peer descriptor view exceeds fixture bounds"
                )
            target = os.readlink(entry.path)
            owned = owned or target == socket_name
    return owned


class ExecutorPeerWitness:
    def __init__(self, app_pid: int, app_pidfd: int) -> None:
        self._app_pid = app_pid
        self._app_pidfd = app_pidfd
        self._inodes: set[int] = set()
        self.admitted = 0
        self.decoy_refused = 0
        self.unavailable = 0

    def _app_live(self) -> bool:
        return not select.select((self._app_pidfd,), (), (), 0)[0]

    def _same_network_namespace(self) -> bool:
        app = os.stat(f"/proc/{self._app_pid}/ns/net")
        coordinator = os.stat("/proc/self/ns/net")
        return (app.st_dev, app.st_ino) == (coordinator.st_dev, coordinator.st_ino)

    def _sample(self, channel: Connection) -> Observation:
        rows = table_rows()
        if connected_inode(rows, channel.local, channel.remote) != channel.server_inode:
            raise PeerObservationError("relay socket identity changed")
        client_inode = connected_inode(rows, channel.remote, channel.local)
        return Observation(
            client_inode,
            owns_socket(self._app_pid, client_inode),
            owns_socket(os.getpid(), client_inode),
        )

    def admits(self, request: web.Request) -> bool:
        try:
            if not self._app_live() or not self._same_network_namespace():
                raise PeerObservationError("selected relay peer unavailable")
            channel = connection(request)
            first = self._sample(channel)
            if (
                first != self._sample(channel)
                or not self._app_live()
                or not self._same_network_namespace()
                or first.app_owns == first.coordinator_owns
            ):
                raise PeerObservationError("selected relay peer observation changed")
        except (OSError, ValueError):
            self.unavailable += 1
            return False
        self._inodes.update((channel.server_inode, first.client_inode))
        if first.app_owns:
            self.admitted += 1
            return True
        self.decoy_refused += 1
        return False

    def sockets_closed(self) -> bool:
        return self._inodes.isdisjoint(int(row[9]) for row in table_rows())
