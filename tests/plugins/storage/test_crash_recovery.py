import asyncio
import multiprocessing
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation
from kora.plugins.storage.local import LocalArtifactStorage
from kora.protocols.storage_models import (
    NoEffectFailure,
    PromotionRequest,
    PromotionResult,
)
from tests.plugins.storage.configuration import local_configuration


@pytest.mark.parametrize(
    "boundary",
    [
        "storage.promote.registered",
        "storage.promote.linked",
        "storage.promote.effect_persisted",
        "storage.promote.admitted",
    ],
)
def test_promotion_process_crash_preserves_original_attribution(
    tmp_path: Path, kora_capture: Capture, boundary: str
) -> None:
    root = tmp_path / "store"
    spawn = multiprocessing.get_context("spawn")
    parent, child = spawn.Pipe(duplex=True)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.plugins.storage.process_probe",
            str(root),
            boundary,
            str(child.fileno()),
        ],
        cwd=Path(__file__).resolve().parents[3],
        pass_fds=(child.fileno(),),
    )
    child.close()

    try:
        assert parent.poll(15)
        request = PromotionRequest.model_validate_json(parent.recv_bytes(16384))
        process.kill()
        process.wait(timeout=10)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        parent.close()

    shards = root / "artifacts" / "sha256"
    if boundary == "storage.promote.registered":
        encoded = request.arguments.address.payload_digest.removeprefix("sha256:")
        first = shards / encoded[:2]
        first.mkdir(mode=0o700)
        (first / encoded[2:4]).mkdir(mode=0o700)

    async def reconcile() -> None:
        provider = LocalArtifactStorage(local_configuration(root))
        context = context_for(kora_capture)
        stage = request.arguments.content.stage

        try:
            retirement = await provider.retire_stage(
                operation(provider.identity(), "crash-retire", "retire_stage", stage),
                stage,
                context,
            )
            observed = await provider.observe(request.operation, context)
            if boundary != "storage.promote.registered":
                replayed = await provider.promote(request, context)
                reader = await provider.get(replayed.content, context)
                payload = b"".join([chunk async for chunk in reader])
                await reader.aclose()
            else:
                replayed = None
                payload = None
        finally:
            await provider.aclose()

        assert retirement.completed
        assert observed.state == "settled"
        if boundary == "storage.promote.registered":
            assert isinstance(observed.result, NoEffectFailure)
            assert not tuple(shards.iterdir())
        else:
            assert isinstance(observed.result, PromotionResult)
            assert observed.result.created_object
            assert replayed == observed.result
            assert payload == b"crash-content"

    asyncio.run(reconcile())
