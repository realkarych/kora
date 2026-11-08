import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from kora.plugins.runtime.local.bound_checkout import (
    BoundCheckoutError,
    CheckoutFailure,
)
from kora.plugins.runtime.local.bound_native_launch import BoundNativeLaunch
from kora.plugins.runtime.local.bound_role_configuration import (
    BoundRoleConfigurationError,
    RoleConfigurationFailure,
)
from kora.plugins.runtime.local.bound_slots import SLOT_UNAVAILABLE, BoundSlotDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import Launch, LaunchInputs, NativeLaunch, Placement
from kora.protocols.secret_value import SecretRevealRefused
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.processes import ProcessError


@dataclass(frozen=True, repr=False, slots=True)
class _BorrowedReader:
    _stream: asyncio.StreamReader
    _limit: int

    async def read(self, size: int, /) -> bytes:
        if type(size) is not int or not 0 < size <= self._limit:
            raise ValueError("native stream read requires a bounded positive size")
        return await self._stream.read(size)

    async def readline(self) -> bytes:
        return await self._stream.readline()


@dataclass(frozen=True, repr=False, slots=True)
class _BorrowedWriter:
    _stream: asyncio.StreamWriter

    def write(self, data: bytes, /) -> None:
        self._stream.write(data)

    async def drain(self) -> None:
        await self._stream.drain()


class _Refusal(StrEnum):
    INVALID = "the bound native launch inputs are invalid"
    UNAVAILABLE = SLOT_UNAVAILABLE
    DEADLINE = "the bound native start reached its deadline"
    FAILED = "the bound native start failed"


class BoundNativeLaunchContext:
    def __init__(
        self, owner: BoundNativeLaunch, placements: Mapping[Placement, Path]
    ) -> None:
        self._owner = owner
        self.placements: Mapping[Placement, Path] = MappingProxyType(dict(placements))

    async def allocate(self, inputs: LaunchInputs) -> Launch:
        raise KoraProtocolError(
            "launch_rejected", "this invocation requires bound native start", False, {}
        )

    async def start_bound(self, role: str) -> NativeLaunch:
        if type(role) is not str:
            raise _public_error(_Refusal.INVALID)
        try:
            gate = self._owner.start(role)
            await gate.prepare()
            await gate.acknowledge()
            reader, writer = gate.borrow_stdio()
        except BoundCheckoutError as error:
            reason = (
                _Refusal.DEADLINE
                if error.reason is CheckoutFailure.DEADLINE
                else _Refusal.INVALID
            )
        except BoundRoleConfigurationError as error:
            reason = (
                _Refusal.DEADLINE
                if error.reason is RoleConfigurationFailure.DEADLINE
                else _Refusal.INVALID
            )
        except SecretRevealRefused:
            reason = _Refusal.INVALID
        except BoundSlotDeadline:
            reason = _Refusal.UNAVAILABLE
        except ChannelError as error:
            reason = (
                _Refusal.DEADLINE
                if error.reason is ChannelFailure.DEADLINE
                else _Refusal.FAILED
            )
        except (ProcessError, OSError):
            reason = _Refusal.FAILED
        else:
            return NativeLaunch(
                _BorrowedReader(reader, self._owner.stream_limit),
                _BorrowedWriter(writer),
            )
        raise _public_error(reason)


def _public_error(reason: _Refusal) -> KoraProtocolError:
    code = (
        "launch_rejected"
        if reason in (_Refusal.INVALID, _Refusal.UNAVAILABLE)
        else "launch_failed"
    )
    return KoraProtocolError(code, reason.value, False, {})
