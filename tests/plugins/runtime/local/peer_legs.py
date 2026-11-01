import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.runner.identity import IdentityObservation, NativeIdentityReader
from kora.runner.peers import (
    PID,
    SO_PEERPIDFD,
    DarwinSource,
    PeerRefusal,
    PeerRefused,
    PidfdSource,
    kernel_source,
    pidfd_pid,
    supports_pidfd,
)
from tests.support.local_peers import CONDITION_SECONDS, judging_source
from tests.support.local_runners import short_directory
from tests.support.local_runtime import SteeredIdentityReader, open_descriptors

CONNECTED_PEER = (
    "import socket, sys\n"
    "peer = socket.socket(socket.AF_UNIX)\n"
    "peer.connect(sys.argv[1])\n"
    "sys.stdin.buffer.read(1)\n"
)


async def kernel_peer_selection(tmp_path: Path, capture: Capture) -> None:
    descriptors = open_descriptors()
    reader = NativeIdentityReader()

    source = kernel_source(reader)
    if sys.platform == "darwin":
        expected = DarwinSource
    else:
        expected = PidfdSource if supports_pidfd() else type(judging_source())
    assert type(source) is expected
    assert open_descriptors() == descriptors


async def peer_clock_window(tmp_path: Path, capture: Capture) -> None:
    descriptors = open_descriptors()
    source = judging_source()
    left, right = socket.socketpair()
    try:
        credentials = source.credentials(left)
        accepted = source.stamp()

        observed = source.judge(credentials, accepted)
        connecting = source.judge(credentials, None)
        with pytest.raises(PeerRefused) as refused:
            source.judge(credentials, 0)

        assert observed == connecting
        assert observed.pid == os.getpid()
        assert refused.value.reason is PeerRefusal.STARTED_AFTER_ACCEPT
        if isinstance(source, DarwinSource):
            assert 0 < source.started(observed.pid) <= accepted
        else:
            tick_ns = 1_000_000_000 // os.sysconf("SC_CLK_TCK")
            assert (observed.start_time + 1) * tick_ns <= accepted
    finally:
        left.close()
        right.close()
    assert open_descriptors() == descriptors


async def peer_exit_during_attestation(tmp_path: Path, capture: Capture) -> None:
    await exited_peer(tmp_path, capture, during_read=True)


async def peer_option_failure_never_downgrades(
    tmp_path: Path, capture: Capture
) -> None:
    await exited_peer(tmp_path, capture, during_read=False)


async def exited_peer(tmp_path: Path, capture: Capture, *, during_read: bool) -> None:
    descriptors = open_descriptors()
    reader = SteeredIdentityReader()
    source = kernel_source(reader)
    with short_directory() as base, socket.socket(socket.AF_UNIX) as listener:
        path = str(base / "peer")
        listener.bind(path)
        listener.listen(1)
        listener.setblocking(False)
        peer = subprocess.Popen(
            [sys.executable, "-c", CONNECTED_PEER, path], stdin=subprocess.PIPE
        )
        try:
            endpoint, _ = await asyncio.wait_for(
                asyncio.get_running_loop().sock_accept(listener), CONDITION_SECONDS
            )
            with endpoint:

                def stop_after_read(
                    observation: IdentityObservation, count: int
                ) -> IdentityObservation:
                    if during_read and count == 1:
                        peer.kill()
                        peer.wait(timeout=CONDITION_SECONDS)
                    return observation

                reader.steered[peer.pid] = stop_after_read
                if not during_read:
                    peer.kill()
                    peer.wait(timeout=CONDITION_SECONDS)

                expected = PeerRefusal.UNREADABLE
                if not during_read and isinstance(source, PidfdSource):
                    try:
                        raw = endpoint.getsockopt(
                            socket.SOL_SOCKET, SO_PEERPIDFD, PID.size
                        )
                    except OSError:
                        expected = PeerRefusal.UNREADABLE
                    else:
                        descriptor = PID.unpack(raw)[0]
                        try:
                            assert pidfd_pid(descriptor) < 0
                        finally:
                            os.close(descriptor)
                        expected = PeerRefusal.EXITED

                with pytest.raises(PeerRefused) as refused:
                    source.attest(endpoint, None)

                if during_read:
                    assert refused.value.reason is PeerRefusal.EXITED
                    assert reader.reads[peer.pid] >= 1
                elif isinstance(source, (PidfdSource, DarwinSource)):
                    assert refused.value.reason is expected
                    assert reader.reads.get(peer.pid, 0) == 0
                else:
                    assert refused.value.reason is PeerRefusal.EXITED
                assert type(kernel_source(NativeIdentityReader())) is type(source)
        finally:
            peer.kill()
            peer.wait(timeout=CONDITION_SECONDS)
            if peer.stdin is not None:
                peer.stdin.close()
    assert open_descriptors() == descriptors
