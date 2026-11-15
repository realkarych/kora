from types import MappingProxyType

from kora.plugins.artifact_type.structured_output import (
    StructuredOutputArtifactType,
    binding_error,
)
from kora.protocols.effective_artifact_type import (
    InstalledBehaviors,
    SelectionBinding,
    StandardSchemaBinding,
    SummaryBinding,
    ValidationBinding,
)


def effective_behavior_bindings(
    provider: StructuredOutputArtifactType,
) -> InstalledBehaviors:
    if type(provider) is not StructuredOutputArtifactType:
        raise binding_error("provider")
    return MappingProxyType(
        {
            ("validation", "kora.structured-output-validation.v1"): ValidationBinding(
                "kora.structured-output-validation.v1", provider.validate
            ),
            ("validation", "kora.json-schema-2020-12.v1"): StandardSchemaBinding(),
            ("digest_fields", "kora.json-payload.v1"): SelectionBinding(
                "kora.json-payload.v1", provider.digest_fields
            ),
            ("summary", "kora.json-payload.v1"): SummaryBinding(
                "kora.json-payload.v1", provider.summary
            ),
        }
    )
