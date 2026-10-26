import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol

import typer
from pydantic import Field, StrictBool, StrictInt, StrictStr

from kora.cli.app import CLI_COMPONENT
from kora.ir.json import JsonObjectField
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.boundary import BoundaryModel
from kora.protocols.metadata import ProtocolName

LISTING_FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        name="cli.provider_listing_finished",
        component=CLI_COMPONENT,
        level=Level.DEBUG,
        message="installed provider discovery finished",
        fields={"state": FieldKind.STRING, "discarded_bytes": FieldKind.INTEGER},
    )
)
LISTING_RETAINED: EventSpec = CATALOG.register(
    EventSpec(
        name="cli.provider_listing_retained",
        component=CLI_COMPONENT,
        level=Level.WARNING,
        message="installed provider discovery retained cleanup state",
        fields={"reason": FieldKind.STRING, "reference": FieldKind.STRING},
    )
)


class ProviderView(BoundaryModel):
    protocol: ProtocolName
    name: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
    version: Annotated[StrictStr, Field(pattern=r"^[\x20-\x7e]{1,64}$")]
    config_schema: JsonObjectField


class ProviderHeader(BoundaryModel):
    kind: Literal["provider"] = "provider"
    protocol: ProtocolName
    name: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
    version: Annotated[StrictStr, Field(pattern=r"^[\x20-\x7e]{1,64}$")]


class FailureView(BoundaryModel):
    kind: Literal["failure"] = "failure"
    entry_name_length: Annotated[StrictInt, Field(ge=0)]
    protocol: ProtocolName | None
    name: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")] | None
    code: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]


class DiscoveryComplete(BoundaryModel):
    kind: Literal["complete"] = "complete"
    providers: Annotated[StrictInt, Field(ge=0)]
    failures: Annotated[StrictInt, Field(ge=0)]
    diagnostics_ok: StrictBool


class RegistryRecord(BoundaryModel):
    schema_version: Literal[1]
    timestamp: datetime
    component: Literal["protocols.loading"]
    logger: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    instance_id: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{32}$")]


class LoadedRecord(RegistryRecord):
    level: Literal["DEBUG"]
    event: Literal["protocols.loading.provider_loaded"]
    message: Literal["a provider was loaded"]
    protocol: ProtocolName
    provider: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
    version: Annotated[StrictStr, Field(pattern=r"^[\x20-\x7e]{1,64}$")]
    distribution: Annotated[StrictStr, Field(max_length=1024)] | None = None


class UnavailableRecord(RegistryRecord):
    level: Literal["WARNING"]
    event: Literal["protocols.loading.provider_unavailable"]
    message: Literal["an installed provider entry point could not be loaded"]
    entry_name_length: Annotated[StrictInt, Field(ge=0)]
    code: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
    protocol: ProtocolName | None = None
    provider: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")] | None = (
        None
    )
    distribution: Annotated[StrictStr, Field(max_length=1024)] | None = None
    exception_type: (
        Annotated[
            StrictStr,
            Field(
                max_length=128,
                pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
            ),
        ]
        | None
    ) = None


@dataclass(frozen=True)
class PluginListing:
    providers: tuple[ProviderView, ...]
    failures: tuple[FailureView, ...]

    def render(self, *, as_json: bool, schema: bool) -> str:
        providers = sorted(self.providers, key=lambda item: (item.protocol, item.name))
        failures = sorted(
            self.failures,
            key=lambda item: (
                item.protocol or "",
                item.name or "",
                item.code,
                item.entry_name_length,
            ),
        )
        provider_views = [
            item.model_dump(mode="json", exclude=set() if schema else {"config_schema"})
            for item in providers
        ]
        failure_views = [
            item.model_dump(mode="json", exclude={"kind"}) for item in failures
        ]
        if as_json:
            return json.dumps(
                {"providers": provider_views, "failures": failure_views},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        lines = [
            f"{item.protocol.value}.{item.name} {item.version}" for item in providers
        ]
        if schema:
            lines = [
                f"{line}\n"
                + json.dumps(item.config_schema, ensure_ascii=False, sort_keys=True)
                for line, item in zip(lines, providers, strict=True)
            ]
        lines.extend(
            f"unavailable {item.protocol or '-'}.{item.name or '-'}: {item.code}"
            for item in failures
        )
        return "\n".join(lines)


class ListingError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PluginDiscovery(Protocol):
    async def discover(self) -> PluginListing: ...


def build_plugins_app(invocation: PluginDiscovery) -> typer.Typer:
    import asyncio

    app = typer.Typer(
        help="Inspect installed providers.",
        add_completion=False,
        pretty_exceptions_enable=False,
        rich_markup_mode=None,
    )

    @app.command("list", help="List installed provider metadata without construction.")
    def list_plugins(
        as_json: Annotated[bool, typer.Option("--json")] = False,
        schema: Annotated[bool, typer.Option("--schema")] = False,
    ) -> None:
        try:
            result = asyncio.run(invocation.discover())
        except ListingError as error:
            typer.echo(f"kora: provider discovery failed: {error.reason}", err=True)
            raise typer.Exit(1) from None
        typer.echo(result.render(as_json=as_json, schema=schema))
        if result.failures:
            raise typer.Exit(1)

    return app
