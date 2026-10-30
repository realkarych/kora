import pytest

from kora.conformance.authentication import (
    check_authentication_preserves_principals,
    check_authentication_revalidates_current_authority,
    kora_capture,
)
from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.db.connection import Database
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import Principal, RequestInfo
from kora.protocols.common import ProtocolContext
from tests.plugins.authentication.support import fixture_inputs

__all__ = ["kora_capture"]


class BrokenAuthenticator(StaticTokenAuthenticator):
    def __init__(self, database: Database, stale: bool) -> None:
        super().__init__({}, database)
        self.stale = stale
        self.prior: Principal | None = None

    async def authenticate(
        self, request: RequestInfo, ctx: ProtocolContext
    ) -> Principal:
        if self.stale:
            if self.prior is None:
                self.prior = await super().authenticate(request, ctx)
            return self.prior
        principal = await super().authenticate(request, ctx)
        return Principal.model_validate({**principal.model_dump(), "namespaces": ()})


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_shared_authentication_checks_reject_wrong_or_stale_authority(
    database: Database, kora_capture: Capture, stale: bool
) -> None:
    inputs = await fixture_inputs(database)
    provider = BrokenAuthenticator(database, stale)
    check = (
        check_authentication_revalidates_current_authority
        if stale
        else check_authentication_preserves_principals
    )

    with pytest.raises(pytest.fail.Exception, match="authority is exact"):
        await check(provider, inputs, context_for(kora_capture))
