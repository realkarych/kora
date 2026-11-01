from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Self, cast

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    model_validator,
)
from pydantic_core import PydanticCustomError, ValidationError

from kora.protocols.boundary import BoundaryModel
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import LaunchInputs, Placement
from kora.protocols.vcs import PathText

SDK_VERSION: Final[str] = "0.2.168"
CLI_VERSION: Final[str] = "2.1.301"


def absolute_path(value: str) -> str:
    if not Path(value).is_absolute():
        raise PydanticCustomError("path_invalid", "the path is invalid")
    return value


AbsolutePathText = Annotated[PathText, AfterValidator(absolute_path)]
PATH: Final[TypeAdapter[str]] = TypeAdapter(AbsolutePathText)


class ClaudeEnvironmentConfiguration(BoundaryModel):
    bash_path: AbsolutePathText
    max_retries: Annotated[StrictInt, Field(ge=0)]
    cli_telemetry: StrictBool

    @model_validator(mode="before")
    @classmethod
    def known_fields(cls: type[Self], value: object) -> object:
        if isinstance(value, Mapping):
            supplied = cast(Mapping[object, object], value)
            if any(name not in cls.model_fields for name in supplied):
                raise PydanticCustomError(
                    "configuration_invalid",
                    "the Claude environment configuration is invalid",
                )
            return supplied
        return value

    def launch_inputs(
        self, *, cwd: Path, placements: Mapping[Placement, Path]
    ) -> LaunchInputs:
        try:
            working_directory = PATH.validate_python(str(cwd))
            home = PATH.validate_python(str(placements["harness_home"]))
            temporary = PATH.validate_python(str(placements["harness_tmp"]))
            configuration = PATH.validate_python(str(placements["harness_config"]))
        except (KeyError, ValidationError):
            raise KoraProtocolError(
                "launch_rejected", "the Claude launch paths are invalid", False, {}
            ) from None
        environment = {
            "HOME": home,
            "TMPDIR": temporary,
            "CLAUDE_CONFIG_DIR": configuration,
            "SHELL": self.bash_path,
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_MAX_RETRIES": str(self.max_retries),
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": SDK_VERSION,
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        }
        if self.cli_telemetry:
            environment["DISABLE_AUTOUPDATER"] = "1"
        else:
            environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return LaunchInputs(environment=environment, cwd=Path(working_directory))
