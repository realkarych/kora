import json
import threading
from typing import Annotated, Final, NoReturn, Protocol, cast, final

from pydantic import PlainSerializer, PlainValidator, SerializationInfo, ValidationInfo
from pydantic_core import PydanticCustomError

from kora.ir.json import fits_utf8, utf8_length
from kora.observability import (
    ProcessDiagnostics,
    SecretAdmissionError,
    SecretRegistration,
)

RENDERED: Final[str] = "SecretValue(<redacted>)"
TRANSFER_SCOPE_KEY: Final[str] = "secret_scope"
TRANSFER_ENCODE_KEY: Final[str] = "secret_transfer"
TRANSFER_ERROR: Final[str] = "secret_transfer_invalid"
TRANSFER_MESSAGE: Final[str] = "the secret transfer field is invalid"
MAX_TRANSFER_BYTES: Final[int] = 16384


class SecretRevealRefused(Exception):
    def __init__(self) -> None:
        super().__init__("the secret's scope is released")


class SecretRenderRefused(TypeError):
    def __init__(self) -> None:
        super().__init__("a secret value cannot be serialised or copied")


def registered_forms(value: str) -> tuple[str, ...]:
    forms = [value]
    for ascii_only in (True, False):
        body = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
        if body not in forms:
            forms.append(body)
    return tuple(forms)


class SecretScope:
    def __init__(self, diagnostics: ProcessDiagnostics | None) -> None:
        self._diagnostics = diagnostics
        self._lock = threading.Lock()
        self._registrations: dict[str, SecretRegistration] = {}
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def admit(self, value: str) -> "SecretValue":
        return SecretValue(value, self)

    def admission(self) -> "SecretAdmission":
        return ScopeAdmission(self)

    def register(self, value: str) -> None:
        diagnostics = self._diagnostics
        if diagnostics is None:
            raise SecretAdmissionError("diagnostics are not configured")
        if utf8_length(value) is None:
            raise SecretAdmissionError("the secret is not valid text")
        with self._lock:
            if self._released:
                raise SecretAdmissionError("the secret scope is released")
            added: list[str] = []
            refused = False
            for form in registered_forms(value):
                if form in self._registrations:
                    continue
                registration = admitted_registration(diagnostics, form)
                if registration is None:
                    refused = True
                    break
                self._registrations[form] = registration
                added.append(form)
            if refused:
                for form in added:
                    self._registrations.pop(form).release()
                raise SecretAdmissionError("the secret was refused admission")

    def release(self) -> None:
        with self._lock:
            self._released = True
            registrations = list(self._registrations.values())
            self._registrations.clear()
        for registration in registrations:
            registration.release()

    def registered_count(self) -> int:
        with self._lock:
            return len(self._registrations)


def admitted_registration(
    diagnostics: ProcessDiagnostics, form: str
) -> SecretRegistration | None:
    try:
        return diagnostics.register_secret(form)
    except SecretAdmissionError:
        return None


class SecretAdmission(Protocol):
    def admit(self, value: str) -> "SecretValue": ...


@final
class ScopeAdmission:
    __slots__ = ("_scope",)

    def __init__(self, scope: SecretScope) -> None:
        self._scope = scope

    def admit(self, value: str) -> "SecretValue":
        return self._scope.admit(value)

    def owns(self, value: "SecretValue") -> bool:
        return not self._scope.released and value.belongs_to(self._scope)

    def __repr__(self) -> str:
        return "SecretAdmission()"


@final
class SecretValue:
    __slots__ = ("_kora_secret", "_scope")

    def __init__(self, value: str, scope: SecretScope) -> None:
        scope.register(value)
        self._kora_secret = value
        self._scope = scope

    def __init_subclass__(cls) -> NoReturn:
        raise TypeError("SecretValue cannot be subclassed")

    def belongs_to(self, scope: SecretScope) -> bool:
        return self._scope is scope

    def reveal(self) -> str:
        if self._scope.released:
            raise SecretRevealRefused
        return self._kora_secret

    def __repr__(self) -> str:
        return RENDERED

    def __str__(self) -> str:
        return RENDERED

    def __format__(self, format_spec: str) -> str:
        return RENDERED

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise SecretRenderRefused

    def __reduce__(self) -> NoReturn:
        raise SecretRenderRefused

    def __getstate__(self) -> NoReturn:
        raise SecretRenderRefused

    def __copy__(self) -> NoReturn:
        raise SecretRenderRefused

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise SecretRenderRefused


def transfer_invalid() -> PydanticCustomError:
    return PydanticCustomError(TRANSFER_ERROR, TRANSFER_MESSAGE)


def context_value(context: object, key: str) -> object:
    if not isinstance(context, dict):
        return None
    return cast("dict[str, object]", context).get(key)


def transfer_scope(info: ValidationInfo) -> SecretScope | None:
    scope = context_value(info.context, TRANSFER_SCOPE_KEY)
    return scope if isinstance(scope, SecretScope) else None


def transfer_text(value: object) -> str | None:
    if not isinstance(value, str) or not fits_utf8(value, MAX_TRANSFER_BYTES):
        return None
    return value


def try_admit(scope: SecretScope, text: str) -> SecretValue | None:
    try:
        return scope.admit(text)
    except SecretAdmissionError:
        return None


def admitted_in_scope(value: SecretValue, info: ValidationInfo) -> bool:
    owner = context_value(info.context, TRANSFER_SCOPE_KEY)
    if isinstance(owner, ScopeAdmission):
        return owner.owns(value)
    return (
        isinstance(owner, SecretScope)
        and not owner.released
        and value.belongs_to(owner)
    )


def decode_transfer(value: object, info: ValidationInfo) -> SecretValue:
    scope = transfer_scope(info)
    if isinstance(value, SecretValue):
        if info.mode != "python" or not admitted_in_scope(value, info):
            raise transfer_invalid() from None
        return value
    text = transfer_text(value)
    admitted = None if scope is None or text is None else try_admit(scope, text)
    if admitted is None:
        raise transfer_invalid() from None
    return admitted


def encode_transfer(value: SecretValue, info: SerializationInfo) -> str:
    if context_value(info.context, TRANSFER_ENCODE_KEY) is not True:
        raise SecretRenderRefused
    return value.reveal()


SecretTransfer = Annotated[
    SecretValue,
    PlainValidator(decode_transfer, json_schema_input_type=str),
    PlainSerializer(encode_transfer, return_type=str),
]
