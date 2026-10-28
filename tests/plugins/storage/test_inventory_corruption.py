import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.plugins.storage.local import LocalArtifactStorage
from kora.protocols.errors import KoraProtocolError
from tests.plugins.storage.configuration import local_configuration


def test_inventory_refuses_malformed_persisted_content_as_integrity_error(
    tmp_path: Path, kora_capture: Capture
) -> None:
    configuration = local_configuration(tmp_path / "store")
    initial = LocalArtifactStorage(configuration)
    asyncio.run(initial.aclose())
    with closing(sqlite3.connect(tmp_path / "store" / "control.sqlite")) as connection:
        with connection:
            connection.execute(
                "INSERT INTO contents(digest, reference, device, inode) "
                "VALUES(?, ?, ?, ?)",
                ("sha256:" + "0" * 64, "{", 1, 1),
            )
    provider = LocalArtifactStorage(configuration)
    context = context_for(kora_capture)

    async def scenario() -> None:
        try:
            with pytest.raises(KoraProtocolError) as refused:
                await provider.scan("content", None, 128, context)
        finally:
            await provider.aclose()

        assert refused.value.code == "storage_integrity"

    asyncio.run(scenario())
