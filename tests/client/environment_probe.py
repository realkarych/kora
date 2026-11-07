import asyncio
import ssl
import sys
from pathlib import Path

import httpx
import pytest

from kora.client import ClientError, KoraClient
from kora.client.errors import Failure
from tests.client.support import CREDENTIAL


async def main(cleartext: str, tls: str, bundle: Path) -> None:
    async with KoraClient(cleartext, CREDENTIAL) as client:
        assert (await client.openapi())["openapi"] == "3.1.0"
    async with KoraClient(tls, CREDENTIAL, ca_bundle=bundle) as client:
        assert (await client.openapi())["openapi"] == "3.1.0"
    async with KoraClient(tls, CREDENTIAL) as client:
        with pytest.raises(ClientError) as refused:
            await client.openapi()
    assert refused.value.reason is Failure.TLS
    async with httpx.AsyncClient(
        verify=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ) as control:
        response = await control.get(cleartext + "/proxy-control")
    assert response.status_code == 200


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
