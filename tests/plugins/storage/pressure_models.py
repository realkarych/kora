from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr

from kora.protocols.common import BoundaryModel


class ProcessToken(BoundaryModel):
    pid: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    identity: Annotated[StrictStr, Field(min_length=1, max_length=256)]


class ImageStamp(BoundaryModel):
    device: Annotated[StrictInt, Field(ge=0)]
    inode: Annotated[StrictInt, Field(ge=1)]


class PressureManifest(BoundaryModel):
    version: Literal[1]
    platform: Literal["linux", "darwin"]
    uid: Annotated[StrictInt, Field(ge=0)]
    directory: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    directory_stamp: ImageStamp
    nonce: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{32}$")]
    controller: ProcessToken
    lease_stamp: ImageStamp
    child: ProcessToken | None = None
    command: ProcessToken | None = None
    image_requested: StrictBool = False
    image_stamp: ImageStamp | None = None
    attachment_requested: StrictBool = False
    mount_requested: StrictBool = False
    command_inflight: StrictBool = False
    command_exit_status: Annotated[StrictInt, Field(ge=0, le=255)] | None = None
    detached: StrictBool = False
    resources_closed: StrictBool = False


class PressureReport(BoundaryModel):
    version: Literal[1] = 1
    provider_started: StrictBool = False
    payload_enospc: StrictBool = False
    metadata_enospc: StrictBool = False
    payload_errno: StrictInt | None = None
    metadata_errno: StrictInt | None = None
    metadata_sqlite_code: StrictInt | None = None
    metadata_headroom_bytes: StrictInt | None = None
    unknown_state_charged: StrictBool = False
    cleanup_completed: StrictBool = False
    resources_closed: StrictBool = False
    failure: Annotated[StrictStr, Field(max_length=64)] | None = None
    filesystem: Literal["ext4", "hfs"] | None = None
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0


class PressureFailure(RuntimeError):
    code: str
    command_stderr: bytes | None

    def __init__(self, code: str, *, command_stderr: bytes | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.command_stderr = command_stderr
