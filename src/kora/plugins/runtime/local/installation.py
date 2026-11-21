import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

from kora.ir.json import JsonObject, JsonValue
from kora.observability import ProcessDiagnostics
from kora.plugins.runtime.local.workspace import CONTEXT_NAMESPACE
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from kora.runner.filesystem import DirectoryAnchor, FilesystemError, open_file_at

INSTALLATION_NAME: Final[str] = "runner.json"
INSTALLATION_LIMIT: Final[int] = 1_048_576
FORMAT: Final[str] = "kora.runner/1"
MAX_PROVIDERS: Final[int] = 16


class RunnerRefused(Exception):
    pass


@dataclass(frozen=True)
class InstallationConfiguration:
    harness: Mapping[str, JsonObject]
    policy: Mapping[str, JsonObject]
    secrets: Mapping[str, JsonObject] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness", MappingProxyType(dict(self.harness)))
        object.__setattr__(self, "policy", MappingProxyType(dict(self.policy)))
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))

    @classmethod
    def empty(cls) -> "InstallationConfiguration":
        return cls({}, {})


def installation_bytes(root: DirectoryAnchor) -> bytes | None:
    try:
        root.verify()
        try:
            descriptor = open_file_at(
                root,
                INSTALLATION_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            )
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
                or metadata.st_nlink != 1
            ):
                raise RunnerRefused("installation_unreadable")
            content = bytearray()
            while len(content) <= INSTALLATION_LIMIT:
                chunk = os.read(descriptor, INSTALLATION_LIMIT + 1 - len(content))
                if not chunk:
                    return bytes(content)
                content.extend(chunk)
            raise RunnerRefused("installation_too_large")
        finally:
            os.close(descriptor)
    except (OSError, FilesystemError):
        raise RunnerRefused("installation_unreadable") from None


def unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_key")
        result[name] = value
    return result


def finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non_finite")
    return value


def provider_sections(value: JsonValue) -> dict[str, JsonObject]:
    if not isinstance(value, dict) or len(value) > MAX_PROVIDERS:
        raise RunnerRefused("installation_malformed")
    result: dict[str, JsonObject] = {}
    for name, section in value.items():
        if not isinstance(section, dict):
            raise RunnerRefused("installation_malformed")
        result[name] = section
    return result


def validate_sections(
    protocol: ProtocolName,
    sections: Mapping[str, JsonObject],
    diagnostics: ProcessDiagnostics,
) -> None:
    for name, section in sections.items():
        try:
            metadata = load_provider(protocol, name, diagnostics)
        except KoraProtocolError:
            raise RunnerRefused("installation_provider") from None
        validate_section(metadata.config_schema, section)


def validate_section(schema: JsonObject, section: JsonObject) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise RunnerRefused("installation_provider") from None
    validator = cast("Validator", Draft202012Validator(schema, registry=Registry()))
    try:
        validator.validate(section)
    except (ValidationError, Unresolvable, RecursionError, OverflowError):
        raise RunnerRefused("installation_invalid") from None


def read_installation(
    root: DirectoryAnchor, diagnostics: ProcessDiagnostics, state_root: Path
) -> InstallationConfiguration:
    content = installation_bytes(root)
    if content is None:
        return InstallationConfiguration.empty()
    try:
        value = cast(
            "JsonValue",
            json.loads(
                content.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=finite_number,
                parse_float=finite_number,
            ),
        )
    except (ValueError, RecursionError):
        raise RunnerRefused("installation_malformed") from None
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {"format", "harness", "policy"},
            {"format", "harness", "policy", "secrets"},
        )
        or not isinstance(value["format"], str)
    ):
        raise RunnerRefused("installation_malformed")
    harness = provider_sections(value["harness"])
    policy = provider_sections(value["policy"])
    secrets = provider_sections(value.get("secrets", {}))
    if value["format"] != FORMAT:
        raise RunnerRefused("installation_format")
    validate_sections(ProtocolName.HARNESS, harness, diagnostics)
    validate_sections(ProtocolName.POLICY, policy, diagnostics)
    if any(name != "file" for name in secrets):
        raise RunnerRefused("installation_provider")
    validate_sections(ProtocolName.SECRETS, secrets, diagnostics)
    if "file" in secrets:
        section = secrets["file"]
        directory = section.get("directory")
        prefixes = section.get("namespace_prefixes")
        if not isinstance(directory, str) or not isinstance(prefixes, dict):
            raise RunnerRefused("installation_invalid")
        components = directory.split("/")
        root_components = str(state_root).split("/")
        if (
            set(prefixes) != {CONTEXT_NAMESPACE}
            or components[0] != ""
            or any(component in {"", ".", ".."} for component in components[1:])
            or len(components) <= len(root_components)
            or components[: len(root_components)] != root_components
        ):
            raise RunnerRefused("installation_invalid")
    return InstallationConfiguration(harness, policy, secrets)
