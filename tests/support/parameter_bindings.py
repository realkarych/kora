from kora.engine.graph_parameter_evaluation import ParameterPublicationBinding
from kora.ir.standard_schema_policy import SchemaDomain, domain_limits


def parameter_binding() -> ParameterPublicationBinding:
    return ParameterPublicationBinding(
        {
            "profile": SchemaDomain.PARAMETERS.value,
            "limits": domain_limits(SchemaDomain.PARAMETERS),
        }
    )
