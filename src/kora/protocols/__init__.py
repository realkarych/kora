from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kora.protocols.artifact_type import (
        ArtifactType,
        ArtifactTypeCapabilities,
        BuiltinArtifactTypeBinding,
    )

__all__ = ["ArtifactType", "ArtifactTypeCapabilities", "BuiltinArtifactTypeBinding"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from kora.protocols import artifact_type

        return getattr(artifact_type, name)
    raise AttributeError(name)
