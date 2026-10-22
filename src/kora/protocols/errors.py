import re

from kora.ir.json import JsonObject, parse_json_object

ERROR_CODE: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_]{0,63}")


class KoraProtocolError(Exception):
    def __init__(
        self, code: str, message: str, retryable: bool, details: JsonObject
    ) -> None:
        if not ERROR_CODE.fullmatch(code):
            raise ValueError("protocol error codes are bounded identifiers")
        super().__init__(code, message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: JsonObject = parse_json_object(details)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
