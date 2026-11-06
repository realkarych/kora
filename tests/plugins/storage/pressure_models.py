import re
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from kora.protocols.common import BoundaryModel


def child_diagnostic(stderr: bytes, limit: int = 24576) -> str:
    if limit < 16:
        raise ValueError("diagnostic limit is too small")
    escaped = "".join(
        chr(value) if value == 10 or 32 <= value <= 126 else f"\\x{value:02x}"
        for value in stderr[-limit:]
    ).replace("::", ":\\x3a")
    if len(stderr) > limit or len(escaped) > limit:
        return "[truncated] " + escaped[-(limit - len("[truncated] ")) :]
    return escaped


class ProcessToken(BoundaryModel):
    pid: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    identity: Annotated[StrictStr, Field(min_length=1, max_length=256)]


class ImageStamp(BoundaryModel):
    device: Annotated[StrictInt, Field(ge=0)]
    inode: Annotated[StrictInt, Field(ge=1)]


NativePhase: TypeAlias = Literal["unmount", "eject"]
NativeIdentifier: TypeAlias = Annotated[
    StrictStr, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
]


class NativeOutcome(BoundaryModel):
    version: Literal[1]
    request_id: NativeIdentifier
    phase: NativePhase
    helper: ProcessToken
    result: Literal["success", "refused", "not_requested"]
    code: Annotated[StrictInt, Field(ge=0, le=4294967295)] | None
    cf_closed: Literal[True]
    text_present: StrictBool
    text_truncated: StrictBool
    dissenter_pid: Annotated[StrictInt, Field(ge=1, le=2147483647)] | None = None
    dissenter_comm: (
        Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._+-]{1,16}$")] | None
    ) = None

    @field_validator("version", mode="before")
    @classmethod
    def explicit_version(cls, value: object) -> Literal[1]:
        if type(value) is not int or value != 1:
            raise ValueError("native version is invalid")
        return 1

    @field_validator("cf_closed", mode="before")
    @classmethod
    def explicit_closure(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("native closure is invalid")
        return True

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if (
            self.result == "not_requested"
            and (self.code is not None or self.text_present or self.text_truncated)
            or self.result != "not_requested"
            and self.code is None
            or self.result == "success"
            and self.code != 0
            or self.text_truncated
            and not self.text_present
            or self.dissenter_pid is not None
            and self.result != "refused"
            or self.dissenter_comm is not None
            and self.dissenter_pid is None
        ):
            raise ValueError("native outcome is inconsistent")
        return self


class NativeRequest(BoundaryModel):
    version: Literal[1]
    request_id: NativeIdentifier
    nonce: NativeIdentifier
    controller: ProcessToken
    image_stamp: ImageStamp
    phase: NativePhase
    device: Annotated[StrictStr, Field(max_length=64)]
    volume: Annotated[StrictStr, Field(max_length=64)] | None
    entities: Annotated[
        tuple[Annotated[StrictStr, Field(max_length=64)], ...],
        Field(min_length=1, max_length=16),
    ]
    deadline: Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]
    helper: ProcessToken | None
    outcome: NativeOutcome | None

    @field_validator("version", mode="before")
    @classmethod
    def explicit_version(cls, value: object) -> Literal[1]:
        if type(value) is not int or value != 1:
            raise ValueError("native version is invalid")
        return 1

    @model_validator(mode="after")
    def consistent_binding(self) -> Self:
        pattern = re.escape(self.device) + r"(?:s[0-9]{1,4}){0,2}"
        if (
            re.fullmatch(r"/dev/disk[0-9]{1,8}", self.device) is None
            or len(set(self.entities)) != len(self.entities)
            or self.device not in self.entities
            or any(re.fullmatch(pattern, entity) is None for entity in self.entities)
            or self.volume is not None
            and self.volume not in self.entities
            or self.phase == "unmount"
            and self.volume is None
        ):
            raise ValueError("native binding is invalid")
        if self.helper == self.controller:
            raise ValueError("native helper is not a child")
        if self.outcome is not None and (
            self.helper is None
            or self.outcome.helper != self.helper
            or self.outcome.request_id != self.request_id
            or self.outcome.phase != self.phase
        ):
            raise ValueError("native outcome does not match")
        return self


class PressureManifest(BoundaryModel):
    version: Literal[1, 2]
    platform: Literal["linux", "darwin"]
    uid: Annotated[StrictInt, Field(ge=0)]
    directory: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    directory_stamp: ImageStamp
    nonce: Annotated[
        StrictStr, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    ]
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
    native: NativeRequest | None = None

    @field_validator("version", mode="before")
    @classmethod
    def explicit_version(cls, value: object) -> Literal[1, 2]:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("manifest version is invalid")
        return 1 if value == 1 else 2

    @model_validator(mode="after")
    def explicit_native_state(self) -> Self:
        if self.version == 2 and "native" not in self.model_fields_set:
            raise ValueError("native state is missing")
        if self.native is not None and (
            self.version != 2
            or self.native.nonce != self.nonce
            or self.native.image_stamp != self.image_stamp
            or not self.attachment_requested
            or self.native.outcome is None
            and (self.detached or self.resources_closed)
        ):
            raise ValueError("native state is inconsistent")
        return self

    def encoded(self) -> bytes:
        return self.model_dump_json(
            exclude={"native"} if self.version == 1 else None
        ).encode()


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


NativeCommandLabel: TypeAlias = Literal[
    "hdiutil create",
    "hdiutil attach",
    "hdiutil detach",
    "diskutil eject",
    "hdiutil info",
    "hdiutil imageinfo",
    "python",
    "mkfs.ext4",
    "losetup",
    "mount",
    "umount",
    "native",
]


class PressureFailure(RuntimeError):
    code: str
    command_stderr: bytes | None
    command_stdout: bytes | None

    def __init__(
        self,
        code: str,
        *,
        command_stderr: bytes | None = None,
        command_stdout: bytes | None = None,
        command_label: NativeCommandLabel | None = None,
        command_status: int | None = None,
        private_output: bool = False,
    ) -> None:
        message = code
        if command_label is not None:
            message += f": command={command_label}"
        if command_status is not None:
            message += f" exit={command_status}"
        if command_stderr is not None and not private_output:
            message += f" stderr={command_stderr[:16384]!r}"
            if len(command_stderr) > 16384 or len(message) > 16384:
                message = message[: 16384 - len(" [truncated]")] + " [truncated]"
        super().__init__(message)
        self.code = code
        self.command_stderr = command_stderr
        self.command_stdout = command_stdout
