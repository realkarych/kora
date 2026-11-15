from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

import pytest

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.engine.graph_output_inventory import read_graph_output_inventory
from kora.engine.graph_output_policy import OutputPublicationBinding
from kora.engine.graph_outputs import GraphOutputContract, build_graph_output_contract
from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
from kora.protocols.effective_artifact_type import (
    BehaviorBinding,
    EffectiveRefusal,
    Role,
    prepare_effective_artifact_type,
)
from tests.engine.graph_inventory_cases import (
    complete_inventory,
    entries,
    inventory_arguments,
)
from tests.engine.graph_output_cases import (
    LIMIT,
    child,
    output_arguments,
    output_document,
)
from tests.engine.graph_schema_cases import declaration
from tests.engine.saved_output_history import historical_outputs
from tests.ir.standard_output_cases import output_input
from tests.protocols.effective_artifact_cases import (
    ObservedArtifactType,
    install_custom,
    installed,
)
from tests.support.output_bindings import publication_binding


@dataclass
class ObservedInstallation(Mapping[tuple[Role, str], BehaviorBinding]):
    bindings: dict[tuple[Role, str], BehaviorBinding]
    lookups: list[tuple[Role, str]] = field(default_factory=list[tuple[Role, str]])
    enumerations: int = 0

    def __getitem__(self, key: tuple[Role, str]) -> BehaviorBinding:
        self.lookups.append(key)
        return self.bindings[key]

    def __iter__(self) -> Iterator[tuple[Role, str]]:
        self.enumerations += 1
        return iter(self.bindings)

    def __len__(self) -> int:
        return len(self.bindings)


def publication_policy() -> dict[str, object]:
    return {
        "policy": SchemaDomain.OUTPUT.value,
        "parameters": domain_limits(SchemaDomain.OUTPUT),
    }


@pytest.mark.parametrize("missing", [None, "validation", "digest_fields", "summary"])
def test_saved_output_v2_publication_requires_installed_binding(
    missing: Role | None,
) -> None:
    raw = output_document()
    child(raw, "type_contracts")["example/Value"] = declaration({"type": "object"})
    child(raw, "graph", "nodes", "producer", "outputs", "value")["type"] = (
        "example/Value"
    )
    arguments = output_arguments(raw)
    provider = ObservedArtifactType()
    bindings = installed()
    install_custom(bindings, provider)
    if missing is not None:
        suffix = {
            "validation": "validation",
            "digest_fields": "digest",
            "summary": "summary",
        }[missing]
        bindings.pop((missing, "example." + suffix + ".v1"))
    observed = ObservedInstallation(bindings)
    policy = publication_policy()
    binding = OutputPublicationBinding(policy, observed)
    policy.clear()

    result = build_graph_output_contract(
        *arguments,
        binding=binding,
        max_encoded_bytes=LIMIT,
    )

    if missing is None:
        assert isinstance(result, GraphOutputContract)
        assert (
            ObjectReader(result.text).object()["schema_validation"]
            == publication_policy()
        )
        assert len(observed.lookups) == 6
    else:
        assert result == EffectiveRefusal("unsupported_schema")
    assert provider.calls == []
    assert observed.enumerations == 0
    assert binding.behaviors is observed


@pytest.mark.parametrize(
    "mutation", ["old", "unknown", "missing", "extra", "order", "boolean", "value"]
)
def test_output_publication_never_defaults_an_unavailable_policy(mutation: str) -> None:
    policy = publication_policy()
    limits = domain_limits(SchemaDomain.OUTPUT)
    if mutation == "old":
        historical = ObjectReader(historical_outputs().leaves[0].contract).object()
        policy = dict(child(historical, "schema_validation"))
    elif mutation == "unknown":
        policy["policy"] = "kora.structured-output-schema-policy.future"
    elif mutation == "missing":
        del policy["parameters"]
    elif mutation == "extra":
        policy["extra"] = None
    elif mutation == "order":
        policy["parameters"] = dict(reversed(limits.items()))
    else:
        policy["parameters"] = limits | {
            "max_schema_nodes": True if mutation == "boolean" else 1
        }

    with pytest.raises(GraphValueRejected) as failure:
        OutputPublicationBinding(policy, installed())

    assert failure.value.reason is GraphValueFailure.VERSION


def test_old_output_consistency_never_grants_v2_value_authority() -> None:
    leaf = historical_outputs().leaves[0]
    saved = ObjectReader(leaf.contract).object()
    candidate = replace(output_input(), schema_validation=saved["schema_validation"])

    result = prepare_effective_artifact_type(candidate, installed())

    assert result == EffectiveRefusal("unsupported_policy")


def test_reference_only_sources_do_not_require_unselected_behavior() -> None:
    raw = output_document()
    child(raw, "type_contracts")["example/Reference"] = declaration({"type": "object"})
    child(raw, "graph", "nodes", "producer", "config")["output_schema"] = {
        "$ref": "urn:kora:artifact:example:Reference",
    }
    arguments = output_arguments(raw)
    binding = OutputPublicationBinding(publication_policy(), installed())

    result = build_graph_output_contract(
        *arguments, binding=binding, max_encoded_bytes=LIMIT
    )

    assert isinstance(result, GraphOutputContract)
    assert ObjectReader(result.text).object()["constraints"] == [
        {"kind": "type_schema", "name": "StructuredOutput"},
        {"kind": "inline_schema"},
    ]


@pytest.mark.parametrize("field", ["policy", "parameters", "missing", "order"])
def test_saved_output_inventory_rejects_partial_or_rebound_records(field: str) -> None:
    arguments = inventory_arguments(output_document())
    saved = entries(complete_inventory(arguments))
    structured = next(
        record
        for record in saved
        if child(record, "contract")["declared_type"] == "StructuredOutput"
    )
    contract = child(structured, "contract")
    policy = child(contract, "schema_validation")
    if field == "policy":
        policy["policy"] = "unavailable"
    elif field == "parameters":
        child(policy, "parameters")["max_schema_nodes"] = 1
    elif field == "missing":
        del contract["schema_validation"]
    else:
        policy["policy"] = policy.pop("policy")

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_output_inventory(*arguments, saved, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.VERSION
    assert publication_binding().policy() == publication_policy()
