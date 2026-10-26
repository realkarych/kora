import json
import socket
import struct
import sys
import uuid
from datetime import UTC, datetime

from kora.cli import CLI_COMPONENT
from kora.cli.plugins import DiscoveryComplete, FailureView, ProviderHeader
from kora.observability import Level, configure_process
from kora.observability.settings import LoggingSettings
from kora.protocols.loading import LOADING_COMPONENT, discover_providers


def send_record(endpoint: socket.socket, value: bytes) -> None:
    if len(value) > 1048576:
        raise ValueError("record capacity")
    endpoint.sendall(struct.pack("!II", 4, len(value)))
    for offset in range(0, len(value), 65536):
        chunk = value[offset : offset + 65536]
        endpoint.sendall(struct.pack("!I", len(chunk)))
        endpoint.sendall(chunk)


def main(result_path: str, event_path: str) -> None:
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as result,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as events,
    ):
        result.connect(result_path)
        events.connect(event_path)
        diagnostics = configure_process(
            CLI_COMPONENT,
            LoggingSettings(component_levels={LOADING_COMPONENT: Level.DEBUG}),
            uuid.uuid4().hex,
            lambda: datetime.now(UTC),
            descriptor=events.fileno(),
        )
        try:
            discovery = discover_providers(diagnostics)
        finally:
            drained = diagnostics.shutdown()
        snapshot = diagnostics.snapshot()
        diagnostics_ok = (
            drained
            and snapshot.rejected_total() == 0
            and snapshot.dropped_total() == 0
            and snapshot.sink_failures == 0
        )
        events.shutdown(socket.SHUT_WR)
        for provider in discovery.providers:
            header = ProviderHeader(
                protocol=provider.protocol, name=provider.name, version=provider.version
            )
            send_record(result, header.model_dump_json().encode())
            send_record(
                result,
                json.dumps(
                    provider.config_schema, ensure_ascii=False, separators=(",", ":")
                ).encode(),
            )
        for failure in discovery.failures:
            view = FailureView(
                entry_name_length=failure.entry_name_length,
                protocol=failure.protocol,
                name=failure.name,
                code=failure.error.code,
            )
            send_record(result, view.model_dump_json().encode())
        send_record(
            result,
            DiscoveryComplete(
                providers=len(discovery.providers),
                failures=len(discovery.failures),
                diagnostics_ok=diagnostics_ok,
            )
            .model_dump_json()
            .encode(),
        )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
