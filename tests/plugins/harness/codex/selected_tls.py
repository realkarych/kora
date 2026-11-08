import asyncio
import math
import ssl
import subprocess
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

OPENSSL = "/usr/bin/openssl"


class SelectedTlsError(ValueError):
    pass


@dataclass(frozen=True)
class SelectedTls:
    context: ssl.SSLContext
    roots: Path
    bundle: Path


def run_openssl(directory: Path, *arguments: str) -> None:
    try:
        result = subprocess.run(
            (OPENSSL, *arguments),
            cwd=directory,
            env={"OPENSSL_CONF": "/etc/ssl/openssl.cnf"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SelectedTlsError("selected_tls_openssl_failed") from None
    if result.returncode != 0:
        raise SelectedTlsError("selected_tls_openssl_failed")


def create_selected_tls(directory: Path) -> SelectedTls:
    directory.mkdir(mode=0o700)
    roots = directory / "roots"
    roots.mkdir(mode=0o700)
    ca_key = directory / "root.key"
    leaf_key = directory / "server.key"
    bundle = roots / "root.pem"
    extensions = directory / "extensions.cnf"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=IP:127.0.0.1\n",
        encoding="ascii",
    )
    try:
        try:
            run_openssl(
                directory,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-noenc",
                "-days",
                "2",
                "-sha256",
                "-subj",
                "/CN=kora-fixture-root",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout",
                "root.key",
                "-out",
                "roots/root.pem",
            )
            run_openssl(
                directory,
                "req",
                "-newkey",
                "rsa:2048",
                "-noenc",
                "-sha256",
                "-subj",
                "/CN=kora-fixture-server",
                "-keyout",
                "server.key",
                "-out",
                "server.csr",
            )
            run_openssl(
                directory,
                "x509",
                "-req",
                "-sha256",
                "-days",
                "2",
                "-in",
                "server.csr",
                "-CA",
                "roots/root.pem",
                "-CAkey",
                "root.key",
                "-set_serial",
                "1",
                "-extfile",
                "extensions.cnf",
                "-out",
                "server.pem",
            )
        finally:
            ca_key.unlink(missing_ok=True)
        run_openssl(directory, "rehash", "roots")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / "server.pem", leaf_key)
    finally:
        leaf_key.unlink(missing_ok=True)
    return SelectedTls(context, roots, bundle)


class SelectedTlsProtocol(asyncio.StreamReaderProtocol):
    def __init__(
        self,
        context: ssl.SSLContext,
        accept: Callable[[asyncio.StreamReader, asyncio.StreamWriter], None],
    ) -> None:
        super().__init__(asyncio.StreamReader(limit=16_384), client_connected_cb=accept)
        self.context = context


async def start_selected_tls_server(
    context: ssl.SSLContext,
    accept: Callable[[asyncio.StreamReader, asyncio.StreamWriter], None],
    *,
    handshake_timeout: float,
) -> asyncio.Server:
    return await asyncio.get_running_loop().create_server(
        lambda: SelectedTlsProtocol(context, accept),
        "127.0.0.1",
        0,
        ssl=context,
        ssl_handshake_timeout=handshake_timeout,
    )


class UnknownCaAlert:
    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context
        self.observed = asyncio.Event()
        self.unexpected = False

    def observe(
        self, loop: asyncio.AbstractEventLoop, details: dict[str, object]
    ) -> None:
        protocol = details.get("protocol")
        error = details.get("exception")
        if not (
            details.get("message")
            == "Error on transport creation for incoming connection"
            and isinstance(protocol, SelectedTlsProtocol)
            and protocol.context is self.context
            and isinstance(error, ssl.SSLError)
            and error.reason == "TLSV1_ALERT_UNKNOWN_CA"
        ):
            self.unexpected = True
        self.observed.set()

    @asynccontextmanager
    async def capture(self) -> AsyncGenerator[None]:
        loop = asyncio.get_running_loop()
        if not isinstance(loop, asyncio.BaseEventLoop):
            raise SelectedTlsError("selected_tls_unexpected_event_loop")
        previous_handler = loop.get_exception_handler()
        previous_debug = loop.get_debug()
        previous_slow_callback_duration = loop.slow_callback_duration
        try:
            loop.slow_callback_duration = math.inf
            loop.set_debug(True)
            loop.set_exception_handler(self.observe)
            yield
        finally:
            loop.set_exception_handler(previous_handler)
            loop.set_debug(previous_debug)
            restored: asyncio.Future[None] = loop.create_future()

            def restore_threshold() -> None:
                loop.slow_callback_duration = previous_slow_callback_duration
                if not restored.done():
                    restored.set_result(None)

            loop.call_soon(restore_threshold)
            await restored

    async def wait_for_refusal(self, turn: Coroutine[object, object, None]) -> None:
        running_turn = asyncio.create_task(turn)
        alert = asyncio.create_task(self.observed.wait())
        try:
            done, _ = await asyncio.wait(
                (running_turn, alert), return_when=asyncio.FIRST_COMPLETED
            )
            if running_turn in done:
                running_turn.result()
                raise SelectedTlsError("selected_tls_unexpected_terminal")
            self.verify()
        finally:
            running_turn.cancel()
            alert.cancel()
            await asyncio.gather(running_turn, alert, return_exceptions=True)

    def verify(self) -> None:
        if not self.observed.is_set() or self.unexpected:
            raise SelectedTlsError("selected_tls_expected_unknown_ca")
