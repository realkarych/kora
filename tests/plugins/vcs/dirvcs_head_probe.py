import asyncio
import sys
from pathlib import Path

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.ir import VcsRef
from kora.observability import LoggingSettings
from kora.plugins.vcs.fake import DirVcs
from kora.plugins.vcs.local import LocalConfiguration
from kora.protocols.errors import KoraProtocolError


def main() -> int:
    configuration = LocalConfiguration.model_validate_json(
        Path(sys.argv[1]).read_bytes()
    )
    reference = VcsRef.model_validate_json(Path(sys.argv[2]).read_bytes())
    provider = DirVcs(configuration)
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "head-probe", utc_now
    ) as capture:
        try:
            asyncio.run(provider.head_sha(reference, context_for(capture)))
        except KoraProtocolError as error:
            return 0 if error.code == "vcs_remote_unreachable" else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
