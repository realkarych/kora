import os
import re
import stat
from codecs import BOM_UTF8
from pathlib import Path

from kora.ir.diagnostics import Diagnostic, SourceLocation

MAX_SOURCE_BYTES = 64 * 1024
SOURCE_LABEL = "graph.py"
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
ENCODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+", re.ASCII)
BLANK_OR_COMMENT = re.compile(r"^[ \t\f]*(?:#|$)")


class SourceRefusal(ValueError):
    def __init__(
        self, message: str, line: int | None = None, code: str = "DSL016"
    ) -> None:
        super().__init__(message)
        self.diagnostic: Diagnostic = Diagnostic(
            code=code,
            message=message,
            location=SourceLocation(file=SOURCE_LABEL, line=line)
            if line and line > 0
            else None,
        )


def read_source(path: Path) -> bytes:
    if path.suffix != ".py" or "\x00" in str(path):
        raise SourceRefusal("Select one regular Python source file ending in .py.")
    try:
        descriptor = os.open(path, READ_FLAGS)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SourceRefusal(
                    "Select one regular Python source file ending in .py."
                )
            source = bytearray()
            while len(source) < MAX_SOURCE_BYTES:
                chunk = os.read(descriptor, MAX_SOURCE_BYTES - len(source))
                if not chunk:
                    return bytes(source)
                source.extend(chunk)
            if os.read(descriptor, 1):
                raise SourceRefusal("Python source exceeds the 64 KiB limit.")
            return bytes(source)
        finally:
            os.close(descriptor)
    except (OSError, UnicodeEncodeError):
        raise SourceRefusal(
            "The Python source file could not be opened and read."
        ) from None


def decode_source(source: bytes) -> str:
    if source.startswith(BOM_UTF8):
        raise SourceRefusal("A Python source file may not include a UTF-8 BOM.", 1)
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        prefix = source[: error.start]
        line = prefix.count(b"\n") + prefix.count(b"\r") - prefix.count(b"\r\n") + 1
        raise SourceRefusal("Python source must be valid UTF-8.", line) from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    nul = text.find("\x00")
    if nul != -1:
        raise SourceRefusal(
            "Python source may not include a NUL character.",
            text.count("\n", 0, nul) + 1,
        )
    first, _, remaining = text.partition("\n")
    second, _, _ = remaining.partition("\n")
    if ENCODING_COOKIE.match(first):
        raise SourceRefusal("Python source may not declare an encoding.", 1)
    if BLANK_OR_COMMENT.match(first) and ENCODING_COOKIE.match(second):
        raise SourceRefusal("Python source may not declare an encoding.", 2)
    return text
