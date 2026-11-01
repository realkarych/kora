from datetime import timedelta
from typing import Annotated, Final

from pydantic import AfterValidator, Field, StrictStr

from kora.ir.json import JsonObject, parse_json_object
from kora.protocols.boundary import BoundaryModel
from kora.protocols.runtime import RuntimeCapabilities
from kora.protocols.vcs import printable_path

STATE_ROOT_LENGTH: Final[int] = 4052

StateRoot = Annotated[
    StrictStr,
    Field(min_length=1, max_length=STATE_ROOT_LENGTH),
    AfterValidator(printable_path),
]


class LocalRuntimeConfiguration(BoundaryModel):
    state_root: StateRoot | None

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            supports_resume=False,
            portable_handles=False,
            enforces_network_policy=False,
            isolates_tool_secrets=False,
            credential_proxy=False,
            protected_execution=False,
            plan_executors=frozenset({"runner", "worker_host"}),
            max_concurrent=None,
            default_stall_after=timedelta(minutes=15),
        )


CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    LocalRuntimeConfiguration.model_json_schema()
)
