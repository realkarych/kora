import asyncio
import errno
import multiprocessing
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation, seal_bytes
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.plugins.storage.state import LocalState
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits
from tests.plugins.storage.inspection import InspectedStorage, current_epoch
from tests.plugins.storage.pressure_native import collect_process


def close_probe(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def refuse_recovered_read_with_closed_descriptor(state: LocalState) -> None:
    with state.transaction(synchronize=False) as database:
        assert state.epoch(database) == 0
        os.close(state.root.fd)
        raise KoraProtocolError("storage_conflict", "the test read refuses", False, {})


@pytest.mark.parametrize(
    "entry", ["epoch", "reader", "refused_reader", "failed_barrier"]
)
def test_internal_read_recovers_actual_hot_journal_before_returning(
    tmp_path: Path, kora_capture: Capture, entry: str
) -> None:
    root = tmp_path / "store"
    context = context_for(kora_capture)

    async def scenario() -> None:
        provider = InspectedStorage(
            LocalStorageConfiguration(root=str(root), limits=StorageLimits(version=1))
        )
        try:
            sealed, _, seal = await seal_bytes(provider, "journal", b"saved", context)
            with ExitStack() as ownership:
                parent, child = multiprocessing.get_context("spawn").Pipe(duplex=True)
                ownership.callback(parent.close)
                ownership.callback(child.close)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.plugins.storage.hot_journal_probe",
                        str(root),
                        str(child.fileno()),
                    ],
                    cwd=Path(__file__).resolve().parents[3],
                    pass_fds=(child.fileno(),),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                ownership.callback(close_probe, process)
                child.close()
                assert parent.poll(15)
                assert parent.recv_bytes(32) == b"hot_journal"
                process.kill()
                output, errors, status = collect_process(process, 10)
                assert status < 0
                assert not output
                assert not errors
            journal = root / "control.sqlite-journal"
            assert journal.stat().st_size > 512

            if entry == "epoch":
                recovered = current_epoch(provider.control_state())
            elif entry == "reader":
                reader = await provider.read_stage(sealed, context)
                async with reader:
                    assert await reader.read(16) == b"saved"
                recovered = current_epoch(provider.control_state())
            elif entry == "refused_reader":
                changed = sealed.model_copy(update={"seal_identity": "foreign"})
                with pytest.raises(KoraProtocolError, match="storage_conflict"):
                    await provider.read_stage(changed, context)
                recovered = current_epoch(provider.control_state())
            else:
                state = provider.control_state()
                descriptor = state.root.fd
                backup = os.dup(descriptor)
                try:
                    with pytest.raises(
                        OSError, match=os.strerror(errno.EBADF)
                    ) as failure:
                        refuse_recovered_read_with_closed_descriptor(state)
                finally:
                    os.dup2(backup, descriptor, inheritable=False)
                    os.close(backup)
                assert failure.value.errno == errno.EBADF
                recovered = current_epoch(state)
                backup = os.dup(descriptor)
                try:
                    with pytest.raises(KoraProtocolError, match="storage_conflict"):
                        refuse_recovered_read_with_closed_descriptor(state)
                finally:
                    os.dup2(backup, descriptor, inheritable=False)
                    os.close(backup)
            observed = await provider.observe(seal, context)

            assert recovered == 0
            assert not journal.exists()
            assert observed.result == sealed
            retired = await provider.retire_stage(
                operation(provider.identity(), "retire", "retire_stage", sealed.stage),
                sealed.stage,
                context,
            )
            assert retired.completed
        finally:
            await provider.aclose()

    asyncio.run(scenario())
