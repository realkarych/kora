from kora.observability import CATALOG, EventSpec, FieldKind, Level

CEL_COMPONENT: str = CATALOG.register_component("cel")
CEL_LOGGER_NAME: str = "kora.cel_eval"

BINDING_LOADED: EventSpec = CATALOG.register(
    EventSpec(
        name="cel.binding.loaded",
        component=CEL_COMPONENT,
        level=Level.INFO,
        message="CEL binding loaded",
        fields={"version": FieldKind.STRING},
    )
)
