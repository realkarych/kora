from typing import Annotated

from pydantic import Field, StrictStr

from kora.models.errors import WireRecord
from kora.models.run_events import RunIdentity

LOGIN_LINK_PATH = "/api/v1/auth/login-links"
REDEEM_PATH = "/api/v1/auth/session/{code}"
CODE_PATTERN = r"[0-9a-f]{64}"


class LoginSelection(WireRecord):
    namespace: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    run_id: RunIdentity


class LoginLinkReceipt(WireRecord):
    url: StrictStr
