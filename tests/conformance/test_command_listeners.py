import asyncio
import os
import socket
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.io_helper import (
    CreateListenersMutation,
    HelperError,
    InstalledIOHelperLauncher,
)
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied", [False, True])
async def test_command_listeners_stay_in_the_registered_child_tree(
    tmp_path: Path, occupied: bool
) -> None:
    clock = ControlledLifetimeClock()
    original_cwd = Path.cwd()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    try:
        async with asyncio.timeout(5):
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.TEMPORARY_TREE,
                )
            )
            acquired = await owner.acquire(
                tree.token, new_operation_id(), TemporaryTreeRequest()
            )
            await owner.acknowledge(
                tree.token, acquired.operation_id, acquired.revision
            )
            directory = owner.records[tree.token.resource_id].directory
            invocation = owner.mutations.directory
            assert directory is not None
            assert invocation is not None
            if occupied:
                descriptor = os.open(
                    "e",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory.descriptor,
                )
                os.close(descriptor)
            mutation = CreateListenersMutation(directory=directory.identity)

            with ExitStack() as resources:
                if occupied:
                    with pytest.raises(HelperError):
                        await owner.mutations.perform(
                            mutation, directory, owner.deadline()
                        )
                else:
                    result = await owner.mutations.perform(
                        mutation, directory, owner.deadline()
                    )
                    helper = next(
                        helper
                        for helper in owner.mutations.helpers
                        if helper.result is result
                    )
                    descriptors = helper.take_descriptors()
                    assert len(descriptors) == result.descriptor_count == 3
                    listeners = tuple(
                        resources.enter_context(socket.socket(fileno=descriptor))
                        for descriptor in descriptors
                    )
                    connected = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-c",
                        "import socket\n"
                        "for name in ('r', 'e', 'l'):\n"
                        "    with socket.socket(socket.AF_UNIX) as endpoint:\n"
                        "        endpoint.connect(name)\n"
                        "        assert endpoint.recv(1) == b'a'\n"
                        "        endpoint.sendall(name.encode())\n"
                        "        assert endpoint.recv(1) == b'z'\n",
                        cwd=next(tmp_path.rglob(tree.token.resource_id)),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    loop = asyncio.get_running_loop()
                    try:
                        for listener, name in zip(
                            listeners, (b"r", b"e", b"l"), strict=True
                        ):
                            listener.setblocking(False)
                            endpoint, _ = await loop.sock_accept(listener)
                            with endpoint:
                                await loop.sock_sendall(endpoint, b"a")
                                assert await loop.sock_recv(endpoint, 1) == name
                                await loop.sock_sendall(endpoint, b"z")
                        output, errors = await connected.communicate()
                        assert output == errors == b""
                        assert connected.returncode == 0
                    finally:
                        if connected.returncode is None:
                            connected.kill()
                        await connected.communicate()
                owner.mutations.prune()
                retained = os.stat(
                    "r", dir_fd=directory.descriptor, follow_symlinks=False
                )
                if not occupied:
                    for name in ("r", "e", "l"):
                        os.unlink(name, dir_fd=directory.descriptor)
            report = await owner.close(None)

        assert Path.cwd() == original_cwd
        assert stat.S_ISSOCK(retained.st_mode)
        assert retained.st_uid == os.getuid()
        assert owner.inventory().helpers.retained_helpers == 0
        if occupied:
            assert report.state is CleanupOutcome.UNKNOWN
            assert not invocation.closed
            assert os.stat("journal", dir_fd=invocation.descriptor).st_size > 0
            assert os.stat("claim", dir_fd=invocation.descriptor).st_nlink == 1
            assert os.stat("e", dir_fd=directory.descriptor).st_size == 0
        else:
            assert report.state is CleanupOutcome.CLOSED
            assert invocation.closed
    finally:
        await dispose_test_owner(owner, clock)
