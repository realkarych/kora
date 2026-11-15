from types import MappingProxyType

from kora.engine.graph_output_policy import OutputPublicationBinding
from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
from tests.conformance.artifact_types import SelectedFieldArtifactType
from tests.protocols.effective_artifact_cases import install_custom, installed


def publication_binding() -> OutputPublicationBinding:
    behaviors = installed()
    install_custom(behaviors, SelectedFieldArtifactType())
    return OutputPublicationBinding(
        {
            "policy": SchemaDomain.OUTPUT.value,
            "parameters": domain_limits(SchemaDomain.OUTPUT),
        },
        MappingProxyType(behaviors),
    )
