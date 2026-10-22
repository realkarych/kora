import functools
import importlib.metadata
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol, cast

from kora.ir.json import JsonObject
from kora.observability import (
    CATALOG,
    ComponentLogger,
    EventSpec,
    FieldKind,
    Level,
    ProcessDiagnostics,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import (
    METADATA_RULES,
    ProtocolName,
    ProviderMetadata,
    ProviderMetadataError,
    is_provider_name,
    parse_protocol_name,
)

ENTRY_POINT_GROUP: str = "kora.provider"
ENTRY_POINTS_FILE: str = "entry_points.txt"
COMMENT_PREFIX: str = "#"
RESERVED_PROVIDER_NAME: str = "fake"
TYPE_NAME: re.Pattern[str] = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
)
MAX_TYPE_NAME_LENGTH: int = 128
FALLBACK_TYPE_NAME: str = "builtins.Exception"
REFUSED_MODULES: re.Pattern[str] = re.compile(
    r"(kora\.plugins\.[^.]+\.fake|kora\.testing|kora\.conformance)(\..*)?"
)

LOADING_COMPONENT: str = CATALOG.register_component("protocols.loading")

PROVIDER_LOADED: EventSpec = CATALOG.register(
    EventSpec(
        name="protocols.loading.provider_loaded",
        component=LOADING_COMPONENT,
        level=Level.DEBUG,
        message="a provider was loaded",
        fields={
            "protocol": FieldKind.STRING,
            "provider": FieldKind.STRING,
            "version": FieldKind.STRING,
            "distribution": FieldKind.STRING,
        },
    )
)
PROVIDER_UNAVAILABLE: EventSpec = CATALOG.register(
    EventSpec(
        name="protocols.loading.provider_unavailable",
        component=LOADING_COMPONENT,
        level=Level.WARNING,
        message="an installed provider entry point could not be loaded",
        fields={
            "entry_name_length": FieldKind.INTEGER,
            "protocol": FieldKind.STRING,
            "provider": FieldKind.STRING,
            "code": FieldKind.STRING,
            "distribution": FieldKind.STRING,
            "exception_type": FieldKind.STRING,
        },
    )
)


@dataclass(frozen=True)
class ProviderEntry:
    name: str
    value: str
    module: str | None
    distribution: str | None
    distribution_version: str | None
    load: Callable[[], object]

    def identity(self) -> tuple[str, str | None, str | None, str | None]:
        return (self.value, self.module, self.distribution, self.distribution_version)


class EntryPointSource(Protocol):
    def entries(self) -> Iterable[ProviderEntry]: ...


class InstalledEntryPoints(EntryPointSource):
    def entries(self) -> Iterator[ProviderEntry]:
        for distribution in importlib.metadata.distributions():
            text = distribution.read_text(ENTRY_POINTS_FILE)
            if text is None:
                continue
            selected = list(provider_lines(text))
            if not selected:
                continue
            metadata = distribution.metadata
            name = metadata["Name"]
            version = metadata["Version"]
            for entry_name, value in selected:
                module = entry_point_module(value)
                yield ProviderEntry(
                    name=entry_name,
                    value=value,
                    module=module,
                    distribution=name,
                    distribution_version=version,
                    load=entry_point_loader(entry_name, value, module),
                )


def provider_lines(text: str) -> Iterator[tuple[str, str]]:
    group: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
            continue
        if group == ENTRY_POINT_GROUP:
            name, _, value = line.partition("=")
            yield name.strip(), value.strip()


def entry_point_module(value: str) -> str | None:
    match = importlib.metadata.EntryPoint.pattern.match(value)
    return match.group("module") if match is not None else None


def entry_point_loader(
    name: str, value: str, module: str | None
) -> Callable[[], object]:
    if module is None:
        return unparsable_entry_point
    return importlib.metadata.EntryPoint(name, value, ENTRY_POINT_GROUP).load


def unparsable_entry_point() -> object:
    raise ValueError("the entry point value does not parse")


INSTALLED: InstalledEntryPoints = InstalledEntryPoints()


@dataclass(frozen=True)
class ProviderFailure:
    entry_name_length: int
    protocol: ProtocolName | None
    name: str | None
    distribution: str | None
    error: KoraProtocolError


@dataclass(frozen=True)
class Discovery:
    providers: tuple[ProviderMetadata, ...]
    failures: tuple[ProviderFailure, ...]


def protocol_error(code: str, message: str, details: JsonObject) -> KoraProtocolError:
    return KoraProtocolError(code, message, False, details)


def entry_name(protocol: ProtocolName, name: str) -> str:
    return f"{protocol.value}.{name}"


def is_refused_module(module: object) -> bool:
    return isinstance(module, str) and REFUSED_MODULES.fullmatch(module) is not None


def qualified_type_name(error: BaseException) -> str:
    for kind in type(error).__mro__:
        text = type_name_text(kind.__module__, kind.__qualname__)
        if text is not None:
            return text
    return FALLBACK_TYPE_NAME


def type_name_text(module: object, qualname: object) -> str | None:
    if not isinstance(module, str) or not isinstance(qualname, str):
        return None
    text = f"{module}.{qualname}"
    if len(text) > MAX_TYPE_NAME_LENGTH or not TYPE_NAME.fullmatch(text):
        return None
    return text


def check_requested_name(protocol: ProtocolName, name: str) -> None:
    if not is_provider_name(name):
        raise protocol_error(
            "provider_name_invalid",
            "a provider name is a lowercase identifier of at most 63 characters",
            {"protocol": protocol.value, "name_length": len(name)},
        )
    if name == RESERVED_PROVIDER_NAME:
        raise protocol_error(
            "provider_reserved",
            "the provider name is reserved for kora's injected fakes",
            {"protocol": protocol.value, "name": name},
        )


def select_entry(
    protocol: ProtocolName, name: str, entries: Iterable[ProviderEntry]
) -> ProviderEntry:
    wanted = entry_name(protocol, name)
    matching: dict[tuple[str, str | None, str | None, str | None], ProviderEntry] = {}
    unprefixed = False
    for entry in entries:
        if entry.name == wanted:
            matching.setdefault(entry.identity(), entry)
        elif entry.name == name:
            unprefixed = True
    details: JsonObject = {"protocol": protocol.value, "name": name}
    if not matching:
        if unprefixed:
            details["hint"] = "entry points are named <protocol>.<name>"
        raise protocol_error(
            "provider_not_installed", "no installed provider has this name", details
        )
    if len(matching) > 1:
        raise protocol_error(
            "provider_ambiguous",
            "more than one installed entry point has this name",
            details,
        )
    return next(iter(matching.values()))


def load_entry(
    protocol: ProtocolName, name: str, entry: ProviderEntry
) -> ProviderMetadata:
    details: JsonObject = {"protocol": protocol.value, "name": name}
    if entry.module is None:
        raise metadata_invalid(details, "entry_point_value")
    if is_refused_module(entry.module):
        raise protocol_error(
            "provider_reserved",
            "the entry point names a module reserved for kora's tests",
            details,
        )
    exception_type: str | None = None
    factory: object = None
    try:
        factory = guarded(entry.load)
    except ProviderCallFailed as failed:
        exception_type = failed.exception_type
    if exception_type is not None:
        raise load_failed(details, exception_type)
    metadata = narrow_metadata(factory, details)
    if metadata.protocol is not protocol or metadata.name != name:
        raise protocol_error(
            "provider_metadata_mismatch",
            "the provider's metadata names another protocol or provider",
            details,
        )
    modules: object = None
    exception_type = None
    try:
        modules = guarded(lambda: implementation_modules(metadata.implementation))
    except ProviderCallFailed as failed:
        exception_type = failed.exception_type
    if exception_type is not None:
        raise load_failed(details, exception_type)
    if isinstance(modules, tuple) and any(
        is_refused_module(module) for module in cast("tuple[object, ...]", modules)
    ):
        raise protocol_error(
            "provider_reserved",
            "the provider's implementation is defined in a reserved module",
            details,
        )
    return metadata


def implementation_modules(implementation: object) -> tuple[object, ...]:
    modules: list[object] = [getattr(implementation, "__module__", None)]
    if isinstance(implementation, functools.partial):
        wrapped = cast("functools.partial[object]", implementation).func
        modules.append(getattr(wrapped, "__module__", None))
    return tuple(modules)


def narrow_metadata(factory: object, details: JsonObject) -> ProviderMetadata:
    if not callable(factory):
        raise metadata_invalid(details, "factory")
    rule: str | None = None
    exception_type: str | None = None
    result: object = None
    try:
        result = guarded(factory)
    except ProviderCallFailed as failed:
        cause = failed.__cause__
        if isinstance(cause, ProviderMetadataError) and is_metadata_rule(cause.rule):
            rule = cause.rule
        else:
            exception_type = failed.exception_type
    if rule is not None:
        raise metadata_invalid(details, rule)
    if exception_type is not None:
        raise load_failed(details, exception_type)
    if type(result) is not ProviderMetadata:
        raise metadata_invalid(details, "factory_result")
    return result


def is_metadata_rule(rule: object) -> bool:
    return type(rule) is str and rule in METADATA_RULES


def metadata_invalid(details: JsonObject, rule: str) -> KoraProtocolError:
    return protocol_error(
        "provider_metadata_invalid",
        "the provider's metadata breaks a validation rule",
        {**details, "rule": rule},
    )


def load_failed(details: JsonObject, exception_type: str) -> KoraProtocolError:
    return protocol_error(
        "provider_load_failed",
        "the provider's entry point raised while loading",
        {**details, "exception_type": exception_type},
    )


class ProviderCallFailed(Exception):
    def __init__(self, exception_type: str) -> None:
        super().__init__(exception_type)
        self.exception_type = exception_type


def guarded(function: Callable[[], object]) -> object:
    try:
        return function()
    except Exception as error:
        raise ProviderCallFailed(qualified_type_name(error)) from error


def report_loaded(
    logger: ComponentLogger, metadata: ProviderMetadata, entry: ProviderEntry
) -> None:
    fields: dict[str, object] = {
        "protocol": metadata.protocol.value,
        "provider": metadata.name,
        "version": metadata.version,
    }
    if entry.distribution is not None:
        fields["distribution"] = entry.distribution
    logger.emit(PROVIDER_LOADED, **fields)


def load_provider(
    protocol: ProtocolName,
    name: str,
    diagnostics: ProcessDiagnostics,
    source: EntryPointSource = INSTALLED,
) -> ProviderMetadata:
    check_requested_name(protocol, name)
    entry = select_entry(protocol, name, source.entries())
    metadata = load_entry(protocol, name, entry)
    report_loaded(diagnostics.logger(LOADING_COMPONENT), metadata, entry)
    return metadata


@dataclass(frozen=True)
class ParsedEntryName:
    protocol: ProtocolName | None
    name: str | None

    def is_valid(self) -> bool:
        return self.protocol is not None and self.name is not None


def parse_entry_name(text: str) -> ParsedEntryName:
    head, separator, tail = text.partition(".")
    protocol = parse_protocol_name(head) if separator else None
    if protocol is None or not is_provider_name(tail):
        return ParsedEntryName(None, None)
    return ParsedEntryName(protocol, tail)


def discover_providers(
    diagnostics: ProcessDiagnostics, source: EntryPointSource = INSTALLED
) -> Discovery:
    logger = diagnostics.logger(LOADING_COMPONENT)
    groups: dict[str, list[ProviderEntry]] = {}
    for entry in source.entries():
        groups.setdefault(entry.name, []).append(entry)
    providers: list[ProviderMetadata] = []
    failures: list[ProviderFailure] = []
    for text, entries in groups.items():
        parsed = parse_entry_name(text)
        first = entries[0]
        try:
            metadata = discover_one(text, parsed, entries)
        except KoraProtocolError as error:
            failure = ProviderFailure(
                len(text), parsed.protocol, parsed.name, first.distribution, error
            )
            failures.append(failure)
            report_unavailable(logger, failure)
            continue
        providers.append(metadata)
        report_loaded(logger, metadata, first)
    return Discovery(tuple(providers), tuple(failures))


def discover_one(
    text: str, parsed: ParsedEntryName, entries: list[ProviderEntry]
) -> ProviderMetadata:
    if parsed.protocol is None or parsed.name is None:
        details: JsonObject = {"name_length": len(text)}
        if "." not in text and is_provider_name(text):
            details["hint"] = "entry points are named <protocol>.<name>"
        raise protocol_error(
            "provider_name_invalid",
            "an entry point name is <protocol>.<name>",
            details,
        )
    check_requested_name(parsed.protocol, parsed.name)
    entry = select_entry(parsed.protocol, parsed.name, entries)
    return load_entry(parsed.protocol, parsed.name, entry)


def report_unavailable(logger: ComponentLogger, failure: ProviderFailure) -> None:
    fields: dict[str, object] = {
        "entry_name_length": failure.entry_name_length,
        "code": failure.error.code,
    }
    if failure.protocol is not None:
        fields["protocol"] = failure.protocol.value
    if failure.name is not None:
        fields["provider"] = failure.name
    if failure.distribution is not None:
        fields["distribution"] = failure.distribution
    exception_type = failure.error.details.get("exception_type")
    if isinstance(exception_type, str):
        fields["exception_type"] = exception_type
    logger.emit(PROVIDER_UNAVAILABLE, **fields)
