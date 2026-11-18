from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.protocols.runtime import SessionResultObservation
from kora.runner.registry import Running, takeover_cell
from kora.testing.local import script_chunks, script_item, unless_busy
from kora.testing.script import FakeScript
from tests.plugins.runtime.local.test_transfer import (
    transfer_body,
    transfer_channel,
    transfer_header,
    transferred,
)
from tests.plugins.runtime.local.testing_support import (
    attempt,
    completed_script,
    composition,
    records,
    transfer_outcomes,
)
from tests.support.local_peers import identity_of, until
from tests.support.local_runtime import protocol_error, recorded, stopped_holder

__all__ = ["kora_capture"]


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_workspace_file(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()

        handle = await local.run_agent("attempt-1", source)
        observation = await unless_busy(
            lambda: local.runtime.session_observation(handle, local.ctx)
        )
        planted = (attempt(local) / "candidate" / "script.json").read_text()

        assert isinstance(observation, SessionResultObservation)
        assert observation.result.stop_reason == "completed"
        assert planted != source.serialise().decode()


@pytest.mark.parametrize(
    "damage", ["changed_script", "write_past_declared", "symlink_swap"]
)
@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_integrity(
    damage: str, tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        work = await local.workload("attempt-1", source)
        handle = await local.runtime.submit(work, local.ctx)
        buffer = source.serialise()
        await local.runtime.transfer_in(
            handle, script_item(buffer), script_chunks(buffer), local.ctx
        )
        path = attempt(local) / "control/script/script.json"
        path.chmod(0o600)
        if damage == "changed_script":
            path.write_bytes(buffer.replace(b"success", b"failure"))
        elif damage == "write_past_declared":
            path.write_bytes(buffer + b" ")
        else:
            target = path.with_name("other.json")
            path.rename(target)
            target.chmod(0o400)
            path.symlink_to(target)
        if damage != "symlink_swap":
            path.chmod(0o400)

        refused = await protocol_error(
            unless_busy(lambda: local.runtime.start(handle, local.ctx))
        )
        observation = await protocol_error(
            local.runtime.session_observation(handle, local.ctx)
        )

        assert refused.code == "integrity_error"
        assert observation.code == "session_unobserved"
        assert all(event.kind != "runner.session_spec" for event in records(local))


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_start_without_script(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        buffer = source.serialise()
        handle = await local.runtime.submit(
            await local.workload("attempt-1", source), local.ctx
        )

        refused = await protocol_error(
            unless_busy(lambda: local.runtime.start(handle, local.ctx))
        )
        repeated = await protocol_error(local.runtime.start(handle, local.ctx))
        late = await protocol_error(
            local.runtime.transfer_in(
                handle, script_item(buffer), script_chunks(buffer), local.ctx
            )
        )

        assert (refused.code, refused.retryable) == (
            "workload_kind_unavailable",
            False,
        )
        assert repeated.code == "workload_kind_unavailable"
        assert late.code == "transfer_refused"
        assert all(event.kind != "runner.session_spec" for event in records(local))


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_second_script(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        work = await local.workload("attempt-1", source)
        handle = await local.runtime.submit(work, local.ctx)
        buffer = source.serialise()
        item = script_item(buffer)
        await local.runtime.transfer_in(handle, item, script_chunks(buffer), local.ctx)

        await local.runtime.transfer_in(handle, item, script_chunks(buffer), local.ctx)
        other = buffer + b" "
        refused = await protocol_error(
            local.runtime.transfer_in(
                handle, script_item(other), script_chunks(other), local.ctx
            )
        )
        renamed = item.model_copy(update={"name": "other.json"})
        renamed_error = await protocol_error(
            local.runtime.transfer_in(handle, renamed, script_chunks(buffer), local.ctx)
        )

        assert refused.code == renamed_error.code == "transfer_refused"
        assert (
            sum(event.kind == "transfer.materialised" for event in records(local)) == 1
        )


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_command_workload(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        work = await local.workload("attempt-1")
        handle = await local.runtime.submit(work, local.ctx)
        buffer = completed_script().serialise()

        refused = await protocol_error(
            local.runtime.transfer_in(
                handle, script_item(buffer), script_chunks(buffer), local.ctx
            )
        )

        assert refused.code == "transfer_refused"
        assert not (attempt(local) / "control/script").exists()


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_after_start(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        handle = await local.run_agent("attempt-1", source)
        buffer = source.serialise()

        refused = await protocol_error(
            local.runtime.transfer_in(
                handle, script_item(buffer), script_chunks(buffer), local.ctx
            )
        )

        assert refused.code == "transfer_refused"


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_other_submitter(
    tmp_path: Path, kora_capture: Capture
) -> None:

    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        work = await local.workload("attempt-1", source)
        handle = await local.runtime.submit(work, local.ctx)
        buffer = source.serialise()
        entry = await recorded(local.runtime, handle.attempt_id)
        assert isinstance(entry, Running)
        incoming = attempt(local) / "control/script/.incoming"

        async with transfer_channel(local.runtime, local.ctx, entry) as channel:
            await transfer_header(channel, local.ctx, script_item(buffer))
            await until(incoming.exists)
            successor = entry.submitter.model_copy(
                update={"dispatcher_id": "successor"}
            )
            await local.runtime.transition(
                local.runtime.registry.key(handle.attempt_id),
                lambda current: takeover_cell(current, entry, successor, False),
                local.ctx,
                "submit",
            )
            await transfer_body(channel, local.ctx, buffer)
            refused = await protocol_error(transferred(channel, local.ctx))
        await until(lambda: not incoming.exists())

        assert refused.code == "submitter_superseded"
        assert not (incoming.parent / "script.json").exists()


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_oversize_composition(
    tmp_path: Path, kora_capture: Capture
) -> None:

    async with composition(tmp_path, kora_capture) as local:
        content = "\0" * 1_048_576
        source = FakeScript.model_validate(
            {
                "end": "completed",
                "on_interrupt": "acknowledge",
                "steps": [
                    {
                        "kind": "change",
                        "files": {f"file-{index}": content for index in range(14)},
                    },
                    {"kind": "result", "status": "success"},
                ],
            }
        )

        refusal = await protocol_error(local.run_agent("attempt-1", source))

        assert refusal.code == "transfer_refused"
        assert await local.runtime.lookup("attempt-1", local.ctx) is None


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_rejected_replay_is_final(
    tmp_path: Path, kora_capture: Capture
) -> None:

    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        work = await local.workload("attempt-1", source)
        handle = await local.runtime.submit(work, local.ctx)
        buffer = source.serialise()
        item = script_item(buffer)
        await local.runtime.transfer_in(handle, item, script_chunks(buffer), local.ctx)
        entry = await recorded(local.runtime, handle.attempt_id)
        assert isinstance(entry, Running)

        async with transfer_channel(local.runtime, local.ctx, entry) as channel:
            await transfer_header(channel, local.ctx, item)
            await transfer_body(channel, local.ctx, b" " * len(buffer))
            refusal = await protocol_error(transferred(channel, local.ctx))
        start = await protocol_error(
            unless_busy(lambda: local.runtime.start(handle, local.ctx))
        )
        observation = await protocol_error(
            local.runtime.session_observation(handle, local.ctx)
        )

        assert refusal.code == start.code == "integrity_error"
        assert observation.code == "session_unobserved"
        assert ("rejected", "WARNING") in transfer_outcomes(local)
        assert all(event.kind != "runner.session_spec" for event in records(local))


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_observational_peer(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with composition(tmp_path, kora_capture) as local:
        source = completed_script()
        work = await local.workload("attempt-1", source)
        handle = await local.runtime.submit(work, local.ctx)
        buffer = source.serialise()
        entry = await recorded(local.runtime, handle.attempt_id)
        assert isinstance(entry, Running)
        holder_lock = tmp_path / "observer-holder"
        holder_lock.touch()
        with stopped_holder(holder_lock) as holder:
            departed = identity_of(holder.pid)
        successor = entry.submitter.model_copy(
            update={"dispatcher_id": "departed", "process": departed}
        )
        await local.runtime.transition(
            local.runtime.registry.key(handle.attempt_id),
            lambda current: takeover_cell(current, entry, successor, False),
            local.ctx,
            "submit",
        )

        async with transfer_channel(local.runtime, local.ctx, entry) as channel:
            await transfer_header(channel, local.ctx, script_item(buffer))
            await transfer_body(channel, local.ctx, buffer)
            refused = await protocol_error(transferred(channel, local.ctx))

        assert refused.code == "execution_unauthorized"
        assert not (attempt(local) / "control/script").exists()
        assert all(event.kind != "transfer.materialised" for event in records(local))
