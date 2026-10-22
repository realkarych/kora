from kora.observability import ProcessDiagnostics
from kora.protocols.common import BoundaryModel
from kora.protocols.secret_value import SecretTransfer

PLAIN: str = "plain-secret-0b7e51"
ESCAPED: str = 'quote"slash\\tab\tnon-ascii-é-end'


class Holder(BoundaryModel):
    credential: SecretTransfer


class Wrapper(BoundaryModel):
    inner: Holder


def registry_state(diagnostics: ProcessDiagnostics) -> tuple[int, int]:
    return diagnostics.registry.count(), diagnostics.registry.retained_bytes()
