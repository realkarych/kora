from dataclasses import FrozenInstanceError, fields
from typing import cast

import pytest

from kora.protocols.artifact_type import ArtifactTypeCapabilities
from tests.plugins.artifact_type.support import binding


def test_artifact_type_capabilities_are_frozen() -> None:
    capabilities = ArtifactTypeCapabilities()
    attribute = type(capabilities).__name__

    with pytest.raises(FrozenInstanceError):
        setattr(cast("object", capabilities), attribute, True)

    assert fields(capabilities) == ()


def test_artifact_type_binding_is_shallow_frozen_input() -> None:
    supplied = binding()
    attribute = fields(supplied)[0].name

    supplied.schema["title"] = "caller-owned"
    with pytest.raises(FrozenInstanceError):
        setattr(cast("object", supplied), attribute, "other")

    assert supplied.schema["title"] == "caller-owned"
