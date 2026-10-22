from importlib.util import find_spec


class ConformanceExtraMissing(ImportError):
    def __init__(self) -> None:
        super().__init__("kora.conformance needs the kora[conformance] extra")


if find_spec("pytest") is None:
    raise ConformanceExtraMissing
