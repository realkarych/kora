from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.ir.config import NodeLimits
from kora.ir.cost import PriceRevision
from kora.protocols.harness import SessionInputs, prompt_digest
from kora.protocols.vcs import WorkspaceTarget
from tests.plugins.harness.fake.fake_support import harness, result, scripted

__all__ = ["kora_capture"]


def test_fake_session_spec_uses_supplied_inputs(kora_capture: Capture) -> None:
    adapter = harness(scripted([result()]), context_for(kora_capture))
    workspace = WorkspaceTarget(root="/kora-fake-workspace", host_path=None)
    inputs: list[SessionInputs] = []
    for index in range(2):
        prompt = f"prompt {index} — Cafe\u0301"
        revision = PriceRevision.model_validate(
            {
                "id": f"prices-{index}",
                "namespace_id": f"namespace-{index}",
                "effective_from": f"2026-01-0{index + 1}T00:00:00Z",
                "version": 1,
                "normalization_version": 1,
                "prices": {
                    f"model-{index}": {
                        "input": str(index + 1),
                        "cached_input": None,
                        "cache_creation_5m": "2",
                        "cache_creation_1h": "3",
                        "output": "4",
                        "reasoning": "5",
                    }
                },
            }
        )
        inputs.append(
            SessionInputs(
                prompt,
                prompt_digest(prompt),
                revision.id,
                revision,
                NodeLimits(max_events=index + 1),
            )
        )

    specs = [
        adapter.build_session_spec({}, workspace, None, inputs=value)
        for value in inputs
    ]

    for spec, value in zip(specs, inputs, strict=True):
        assert spec.prompt == value.prompt
        assert spec.price_table_id == value.price_table_id
        assert spec.options == {}
        assert spec.price_table == value.price_table.model_dump(mode="json")
