import datetime
import ipaddress
import ssl
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

STALL_LIMIT_SECONDS = 30.0


@dataclass(frozen=True)
class Body:
    status: int
    content: bytes


@dataclass(frozen=True)
class Redirect:
    location: str


@dataclass(frozen=True)
class Stall:
    size: int


@dataclass(frozen=True)
class Drop:
    pass


Reply = Body | Redirect | Stall | Drop


@dataclass(frozen=True)
class Recorded:
    path: str
    text: str
    authorization: bool


@dataclass
class HttpsServer:
    netloc: str
    requests: list[Recorded] = field(default_factory=list[Recorded])

    def url(self, path: str) -> str:
        return f"https://{self.netloc}{path}"


@dataclass(frozen=True)
class Authority:
    bundle: Path
    context: ssl.SSLContext


def pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def create_authority(directory: Path) -> Authority:
    now = datetime.datetime.now(datetime.UTC)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "kora acquisition fixture root")]
    )
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(authority_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(authority_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    bundle = directory / "authorities.pem"
    bundle.write_bytes(pem(authority))
    chain = directory / "server.pem"
    chain.write_bytes(
        pem(server)
        + server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain)
    return Authority(bundle, context)


def handler_for(
    replies: Sequence[Reply], record: HttpsServer, released: threading.Event
) -> type[BaseHTTPRequestHandler]:
    pending = list(replies)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            record.requests.append(
                Recorded(
                    self.path,
                    f"{self.requestline}\n{self.headers}",
                    "Authorization" in self.headers,
                )
            )
            reply = pending.pop(0) if len(pending) > 1 else pending[0]
            if isinstance(reply, Drop):
                self.close_connection = True
                return
            if isinstance(reply, Redirect):
                self.send_response(302)
                self.send_header("Location", reply.location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if isinstance(reply, Stall):
                self.send_response(200)
                self.send_header("Content-Length", str(reply.size))
                self.end_headers()
                self.wfile.flush()
                released.wait(STALL_LIMIT_SECONDS)
                self.close_connection = True
                return
            self.send_response(reply.status)
            self.send_header("Content-Length", str(len(reply.content)))
            self.end_headers()
            self.wfile.write(reply.content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


@contextmanager
def https_server(
    authority: Authority, replies: Sequence[Reply]
) -> Generator[HttpsServer]:
    released = threading.Event()
    placeholder = HttpsServer("")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), handler_for(replies, placeholder, released)
    )
    server.daemon_threads = True
    server.socket = authority.context.wrap_socket(server.socket, server_side=True)
    placeholder.netloc = f"127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield placeholder
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=STALL_LIMIT_SECONDS)
