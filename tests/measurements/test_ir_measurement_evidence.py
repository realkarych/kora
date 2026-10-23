import hashlib
import json
import sys
from collections import Counter
from html import escape
from pathlib import Path
from traceback import format_exception
from typing import cast

import pytest

from tests.measurements.harness import Report, Sample
from tests.measurements.ir_config_lifetime import CONFIGURATIONS
from tests.measurements.ir_measurement_evidence import (
    ROOT,
    Evidence,
    MeasurementReport,
    configuration_evidence,
    expected_evidence,
    junit_nodes,
    schema_evidence,
    verify_configuration_payloads,
    verify_report,
    verify_schema_payloads,
    verify_union,
)
from tests.measurements.ir_schema_workload import schema_fixture_manifest, schema_models
from tests.measurements.vcs_ref_workload import cases as vcs_ref_cases

REVISION = "a" * 40


def configuration_payload() -> dict[str, object]:
    return {
        "baseline_rss_bytes": 100,
        "baseline_peak_rss_bytes": 200,
        "baseline_threads": 1,
        "baseline_os_threads": 1,
        "baseline_descriptors": 4,
        "checkpoints": [
            {
                "cycles": cycle,
                "seconds": 0.1,
                "retained_bytes": 0,
                "peak_traced_bytes": 32,
                "current_rss_bytes": 100,
                "peak_rss_bytes": 200,
                "live_models": 0,
                "threads": 1,
                "os_threads": 1,
                "tracer_bytes": 16,
                "descriptors": 4,
                "allocation_differences": [],
            }
            for cycle in (250, 500, 750, 1000)
        ],
        "cycles": 1000,
        "models_per_cycle": 53,
        "class_schema_inventory": sorted(model.__name__ for model in CONFIGURATIONS),
        "class_schemas_unchanged": True,
        "supplemental_fixtures": 14,
        "trigger_union_array_size": 5,
        "trigger_union_schema_unchanged": True,
        "document_miniature_nodes": 22,
        "document_full_size_slots": 10,
        "document_full_size_rotation": {
            "flat_valid": 3,
            "flat_final_invalid": 3,
            "flat_all_invalid": 2,
            "nested_valid": 2,
        },
        "warm_up": 3,
        "rss_source": "synthetic resource observations",
    }


def schema_payload() -> dict[str, object]:
    resources = configuration_payload()
    checkpoints = cast(list[dict[str, object]], resources["checkpoints"])
    for checkpoint in checkpoints:
        checkpoint.pop("live_models")
    return {
        **{
            name: value
            for name, value in resources.items()
            if name.startswith("baseline_")
        },
        "checkpoints": checkpoints,
        "revision": REVISION,
        "pid": 123,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cycles": 1000,
        "action_counts": {"validation": 334, "serialization": 333, "render_utf8": 333},
        "class_schema_inventory": sorted(schema_models()),
        "class_schemas_unchanged": True,
        "schema_manifest": json.loads(schema_fixture_manifest()),
        "warm_up": 3,
        "rss_source": "synthetic resource observations",
    }


def rendered_report(inventory: Evidence) -> str:
    report = Report(
        "2026-10-23T00:00:00Z", sys.platform, sys.version.split()[0], REVISION
    )
    report.environment.update(
        {
            "selected_test_nodes": json.dumps(sorted(inventory.nodes)),
            "uv_lock_sha256": hashlib.sha256(
                (ROOT / "uv.lock").read_bytes()
            ).hexdigest(),
            "remaining_fixture_manifest": "remaining",
            "declaration_fixture_manifest": "declaration",
            "document_fixture_manifest": "document",
            "schema_fixture_manifest": schema_fixture_manifest(),
        }
    )
    report.gates.update(dict.fromkeys(inventory.gates, True))
    if any(node.endswith("::test_vcs_ref_action_cost") for node in inventory.nodes):
        report.environment["vcs_ref_fixture_manifest"] = json.dumps(vcs_ref_cases()[1])
    for run in range(3):
        if configuration_evidence(run).nodes <= inventory.nodes:
            report.environment[f"config_lifetime_{run}"] = json.dumps(
                configuration_payload()
            )
        if schema_evidence(run).nodes <= inventory.nodes:
            prefix = f"schema_lifetime_{run}"
            report.environment.update(
                {
                    prefix: json.dumps(schema_payload()),
                    f"{prefix}_stderr": "",
                    f"{prefix}_exit": 0,
                    f"{prefix}_timeout": False,
                    f"{prefix}_overflow": False,
                    f"{prefix}_parent_descriptors_before": 5,
                    f"{prefix}_parent_descriptors_after": 5,
                    f"{prefix}_parent_threads_before": 1,
                    f"{prefix}_parent_threads_after": 1,
                }
            )
        for traced in (True, False):
            if any(
                node.endswith(f"::test_vcs_ref_validation_lifetime[{run}-{traced}]")
                for node in inventory.nodes
            ):
                mode = "traced" if traced else "untraced"
                prefix = f"vcs_ref_{mode}_{run}"
                report.environment.update(
                    {
                        f"{prefix}_stdout": "{}",
                        f"{prefix}_stderr": "",
                        f"{prefix}_exit": 0,
                        f"{prefix}_native_growth_review": "required",
                    }
                )
        if any(
            node.endswith(f"::test_vcs_ref_cold_public_import[{run}]")
            for node in inventory.nodes
        ):
            for source in ("base", "head"):
                report.environment[f"vcs_import_{source}_{run}"] = "{}"
    for (name, repeat, count), multiplicity in inventory.samples.items():
        report.samples.extend(
            Sample(name, repeat, count, 0.1) for _ in range(multiplicity)
        )
    return report.to_json()


def rendered_junit(inventory: Evidence) -> str:
    return (
        "<testsuites><testsuite>"
        + "".join(
            '<testcase classname="tests.measurements.test_ir_models_cost" '
            f'name="{escape(node.split("::", 1)[1])}"/>'
            for node in sorted(inventory.nodes)
        )
        + "</testsuite></testsuites>"
    )


def test_ir_evidence_inventory_preserves_every_named_action_and_sample() -> None:
    expected = expected_evidence()

    workers = [configuration_evidence(run) for run in range(3)] + [
        schema_evidence(run) for run in range(3)
    ]

    assert len(expected.nodes) == 337
    assert len(expected.gates) == 462
    assert sum(expected.samples.values()) == 7923
    assert all(len(worker.nodes) == 1 for worker in workers)
    assert all(len(worker.gates) == 4 for worker in workers)
    assert all(sum(worker.samples.values()) == 4 for worker in workers)
    assert all(worker.nodes <= expected.nodes for worker in workers)
    assert all(worker.gates <= expected.gates for worker in workers)
    assert all(worker.samples <= expected.samples for worker in workers)


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_junit_refuses_failed_or_skipped_measurement(outcome: str) -> None:
    xml = (
        '<testsuites><testsuite><testcase classname="tests.measurements.'
        'test_ir_models_cost" name="test_repeated_failures_return_to_baseline">'
        f"<{outcome}/></testcase></testsuite></testsuites>"
    )

    with pytest.raises(ValueError, match="incomplete IR test evidence"):
        junit_nodes(xml)


def test_junit_refuses_duplicate_nodes_and_doctype() -> None:
    node = (
        '<testcase classname="tests.measurements.test_ir_models_cost" '
        'name="test_repeated_failures_return_to_baseline"/>'
    )
    documents = (
        f"<testsuites><testsuite>{node}{node}</testsuite></testsuites>",
        '<!DOCTYPE testsuites [<!ENTITY value "x">]><testsuites/>',
    )

    for document in documents:
        with pytest.raises(ValueError, match="incomplete IR test evidence"):
            junit_nodes(document)


def test_ir_union_refuses_missing_required_shards(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()

    with pytest.raises(ValueError, match="incomplete IR shard evidence"):
        verify_union(directory, "a" * 40)


@pytest.mark.parametrize(
    "mutation",
    ["revision", "selected", "gate", "duplicate", "count", "missing", "lock"],
)
def test_ir_report_refuses_incomplete_or_duplicate_evidence(mutation: str) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    junit = rendered_junit(expected)
    if mutation == "revision":
        report.revision = "b" * 40
    elif mutation == "selected":
        report.environment["selected_test_nodes"] = "[]"
    elif mutation == "gate":
        report.gates[next(iter(report.gates))] = False
    elif mutation == "duplicate":
        report.samples[1] = report.samples[0]
    elif mutation == "count":
        report.samples[0].count = 251
    elif mutation == "missing":
        report.samples.pop()
    else:
        report.environment["uv_lock_sha256"] = "wrong"

    with pytest.raises(ValueError, match="incomplete IR shard evidence"):
        verify_report(report.model_dump_json(), junit, REVISION, expected)


def test_ir_report_accepts_exact_named_sample_inventory() -> None:
    expected = configuration_evidence(1)

    report = verify_report(
        rendered_report(expected), rendered_junit(expected), REVISION, expected
    )

    assert len(report.gates) == 4
    assert [
        (sample.workload, sample.repeat, sample.count) for sample in report.samples
    ] == [("config_lifetime", 1, cycle) for cycle in (250, 500, 750, 1000)]


@pytest.mark.parametrize(
    "mutation", [None, "missing", "duplicate", "revision", "manifest"]
)
def test_ir_union_checks_all_artifact_identities(
    tmp_path: Path, mutation: str | None
) -> None:
    full = expected_evidence()
    workers = (
        configuration_evidence(1),
        configuration_evidence(2),
        *(schema_evidence(run) for run in range(3)),
    )
    first = Evidence(
        full.nodes - frozenset().union(*(worker.nodes for worker in workers)),
        full.gates - frozenset().union(*(worker.gates for worker in workers)),
        full.samples
        - sum((worker.samples for worker in workers), Counter[tuple[str, int, int]]()),
    )
    for run, inventory in enumerate((first, *workers)):
        shard = tmp_path / f"ir-models-cost-measurement-{REVISION}-{run}"
        shard.mkdir()
        document = rendered_report(inventory)
        if mutation == "manifest" and run == 2:
            changed = MeasurementReport.model_validate_json(document)
            changed.environment["document_fixture_manifest"] = "different"
            document = changed.model_dump_json()
        (shard / f"ir-models-cost-{run}.json").write_text(document)
        (shard / f"ir-models-cost-{run}.xml").write_text(rendered_junit(inventory))
        checkout = "b" * 40 if mutation == "revision" and run == 1 else REVISION
        (shard / f"ir-checkout-revision-{run}.txt").write_text(checkout + "\n")
    if mutation == "missing":
        (
            tmp_path
            / f"ir-models-cost-measurement-{REVISION}-2"
            / "ir-models-cost-2.xml"
        ).unlink()
    elif mutation == "duplicate":
        (tmp_path / f"ir-models-cost-measurement-{REVISION}-6").mkdir()

    if mutation is None:
        result = verify_union(tmp_path, REVISION)

        assert (result["tests"], result["gates"], result["samples"]) == (337, 462, 7923)
    else:
        with pytest.raises(ValueError, match="IR"):
            verify_union(tmp_path, REVISION)


@pytest.mark.parametrize("mutation", ["missing", "malformed", "object", "extra_run"])
def test_ir_report_requires_only_its_selected_configuration_payload(
    mutation: str,
) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    if mutation == "missing":
        report.environment.pop("config_lifetime_1")
    elif mutation == "malformed":
        report.environment["config_lifetime_1"] = "{"
    elif mutation == "object":
        report.environment["config_lifetime_1"] = "[]"
    else:
        report.environment["config_lifetime_0"] = json.dumps(configuration_payload())

    with pytest.raises(ValueError, match="IR configuration"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("models_per_cycle", 40),
        ("cycles", 999),
        ("class_schema_inventory", ["wrong"]),
        ("class_schemas_unchanged", False),
        ("class_schemas_unchanged", 1),
        ("trigger_union_schema_unchanged", False),
        ("trigger_union_schema_unchanged", 1),
        ("warm_up", 2),
        ("supplemental_fixtures", 13),
        ("trigger_union_array_size", 4),
        ("document_miniature_nodes", 21),
        ("document_full_size_slots", 9),
        ("document_full_size_rotation", {"flat_valid": 10}),
        ("baseline_threads", True),
        ("checkpoints", []),
        ("class_schema_inventory", ["AgentConfig"] * 53),
        ("baseline_rss_bytes", None),
        ("unknown", "metadata"),
    ],
)
def test_ir_report_refuses_wrong_configuration_inventory(
    field: str, value: object
) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = configuration_payload()
    payload[field] = value
    report.environment["config_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR configuration"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cycles", 251),
        ("live_models", 1),
        ("live_models", False),
        ("retained_bytes", 65536),
        ("threads", 2),
        ("descriptors", 5),
        ("os_threads", True),
        ("seconds", 0.2),
        ("peak_traced_bytes", True),
    ],
)
def test_ir_report_refuses_inconsistent_configuration_checkpoints(
    field: str, value: object
) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = configuration_payload()
    checkpoints = cast(list[dict[str, object]], payload["checkpoints"])
    checkpoints[0][field] = value
    report.environment["config_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR configuration"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize("field", tuple(configuration_payload()))
def test_ir_report_refuses_missing_configuration_metadata(field: str) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = configuration_payload()
    payload.pop(field)
    report.environment["config_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR configuration"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize("field", ["current_rss_bytes", "peak_rss_bytes", "os_threads"])
def test_linux_configuration_evidence_requires_native_counters(field: str) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.platform = "linux"
    payload = configuration_payload()
    checkpoints = cast(list[dict[str, object]], payload["checkpoints"])
    checkpoints[0][field] = None
    report.environment["config_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR configuration"):
        verify_configuration_payloads(report, expected)


def test_ir_evidence_preserves_every_upstream_vcs_ref_identity() -> None:
    actions, _ = vcs_ref_cases()
    expected_nodes = {
        "tests/measurements/test_ir_models_cost.py::test_vcs_ref_action_cost",
        *(
            "tests/measurements/test_ir_models_cost.py::"
            f"test_vcs_ref_validation_lifetime[{run}-{traced}]"
            for run in range(3)
            for traced in (True, False)
        ),
        *(
            "tests/measurements/test_ir_models_cost.py::"
            f"test_vcs_ref_cold_public_import[{run}]"
            for run in range(3)
        ),
    }
    expected_gates = (
        {f"{action.name}_retained" for action in actions}
        | {
            f"vcs_ref_{mode}_{run}_lifecycle_assertions_passed"
            for run in range(3)
            for mode in ("traced", "untraced")
        }
        | {f"vcs_import_{run}_resources_released" for run in range(3)}
    )
    expected_samples = Counter(
        (action.name, repeat, 1) for action in actions for repeat in range(10)
    )
    expected_samples.update(
        (f"vcs_ref_{mode}_{run}_process", run, 500)
        for run in range(3)
        for mode in ("traced", "untraced")
    )
    expected_samples.update(
        (f"vcs_import_{source}", run, 1)
        for run in range(3)
        for source in ("base", "head")
    )

    evidence = expected_evidence()

    assert len(actions) == 109
    assert {
        node for node in evidence.nodes if "::test_vcs_ref_" in node
    } == expected_nodes
    assert {
        gate for gate in evidence.gates if gate.startswith("vcs_")
    } == expected_gates
    assert len(expected_nodes) == 10
    assert len(expected_gates) == 118
    assert (
        Counter(
            {
                identity: multiplicity
                for identity, multiplicity in evidence.samples.items()
                if identity[0].startswith("vcs_")
            }
        )
        == expected_samples
    )
    assert (
        sum(
            multiplicity
            for (workload, _, _), multiplicity in evidence.samples.items()
            if workload.startswith("vcs_")
        )
        == 1102
    )
    assert len(evidence.nodes - expected_nodes) == 327
    assert len(evidence.gates - expected_gates) == 344
    assert (
        sum(
            multiplicity
            for (workload, _, _), multiplicity in evidence.samples.items()
            if not workload.startswith("vcs_")
        )
        == 6821
    )


@pytest.mark.parametrize("mutation", ["missing", "wrong", "bool_count"])
def test_ir_vcs_ref_manifest_must_match_its_source(mutation: str) -> None:
    expected = Evidence(
        frozenset(
            {"tests/measurements/test_ir_models_cost.py::test_vcs_ref_action_cost"}
        ),
        frozenset(),
        Counter(),
    )
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    if mutation == "missing":
        report.environment.pop("vcs_ref_fixture_manifest")
    elif mutation == "wrong":
        report.environment["vcs_ref_fixture_manifest"] = "{}"
    else:
        report.environment["vcs_ref_fixture_manifest"] = (
            '{"vcs_decode_1024":{"json_bytes":true}}'
        )

    with pytest.raises(ValueError, match="IR VcsRef"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


def test_ir_worker_shard_refuses_an_unselected_vcs_ref_manifest() -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.environment["vcs_ref_fixture_manifest"] = json.dumps(vcs_ref_cases()[1])

    with pytest.raises(ValueError, match="IR VcsRef"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vcs_ref_traced_0_stdout", ""),
        ("vcs_ref_traced_0_stderr", "failed"),
        ("vcs_ref_traced_0_exit", 1),
        ("vcs_ref_traced_0_exit", False),
    ],
)
def test_ir_vcs_ref_lifetime_requires_its_transport_evidence(
    field: str, value: str | int | bool
) -> None:
    expected = Evidence(
        frozenset(
            {
                "tests/measurements/test_ir_models_cost.py::test_vcs_ref_validation_lifetime[0-True]"
            }
        ),
        frozenset(),
        Counter(),
    )
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.environment[field] = value

    with pytest.raises(ValueError, match="IR VcsRef"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


def test_ir_vcs_ref_cold_import_requires_both_sources() -> None:
    expected = Evidence(
        frozenset(
            {
                "tests/measurements/test_ir_models_cost.py::test_vcs_ref_cold_public_import[0]"
            }
        ),
        frozenset(),
        Counter(),
    )
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.environment.pop("vcs_import_base_0")

    with pytest.raises(ValueError, match="IR VcsRef"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize("boundary", ["configuration", "vcs_manifest"])
def test_ir_new_payload_refusals_do_not_echo_input_in_tracebacks(boundary: str) -> None:
    sentinel = "artifact-input-" + "must-remain-private"
    expected = (
        configuration_evidence(1)
        if boundary == "configuration"
        else Evidence(
            frozenset(
                {"tests/measurements/test_ir_models_cost.py::test_vcs_ref_action_cost"}
            ),
            frozenset(),
            Counter(),
        )
    )
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    if boundary == "configuration":
        payload = configuration_payload()
        payload["baseline_threads"] = sentinel
        report.environment["config_lifetime_1"] = json.dumps(payload)
    else:
        report.environment["vcs_ref_fixture_manifest"] = json.dumps(
            {"vcs_decode_1024": {"json_bytes": sentinel}}
        )

    with pytest.raises(ValueError, match="IR") as raised:
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )
    rendered = "".join(format_exception(raised.value))

    assert sentinel not in rendered
    assert "IR" in rendered


@pytest.mark.parametrize("mutation", ["missing", "malformed", "object", "extra_run"])
def test_ir_report_requires_only_selected_schema_payload(mutation: str) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    if mutation == "missing":
        report.environment.pop("schema_lifetime_1")
    elif mutation == "malformed":
        report.environment["schema_lifetime_1"] = "{"
    elif mutation == "object":
        report.environment["schema_lifetime_1"] = "[]"
    else:
        report.environment["schema_lifetime_0"] = json.dumps(schema_payload())

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", "stale"),
        ("platform", "other"),
        ("python", "other"),
        ("pid", 0),
        ("pid", True),
        ("cycles", 999),
        (
            "action_counts",
            {"validation": 333, "serialization": 334, "render_utf8": 333},
        ),
        ("class_schema_inventory", ["AgentConfig"] * 53),
        ("class_schemas_unchanged", False),
        ("class_schemas_unchanged", 1),
        ("schema_manifest", {}),
        ("warm_up", 2),
        ("checkpoints", []),
        ("baseline_threads", True),
        ("baseline_rss_bytes", -1),
        ("unknown", "artifact-input-must-remain-private"),
    ],
)
def test_ir_report_refuses_wrong_schema_inventory(field: str, value: object) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = schema_payload()
    payload[field] = value
    report.environment["schema_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR schema") as raised:
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )
    traceback = "".join(format_exception(raised.value))

    assert "artifact-input-must-remain-private" not in traceback


@pytest.mark.parametrize("field", tuple(schema_payload()))
def test_ir_report_refuses_missing_schema_metadata(field: str) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = schema_payload()
    payload.pop(field)
    report.environment["schema_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cycles", 251),
        ("seconds", 0.2),
        ("seconds", float("inf")),
        ("retained_bytes", 65536),
        ("threads", 2),
        ("os_threads", True),
        ("descriptors", 5),
        ("peak_traced_bytes", -1),
        ("tracer_bytes", True),
        ("current_rss_bytes", -1),
    ],
)
def test_ir_report_refuses_inconsistent_schema_checkpoints(
    field: str, value: object
) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = schema_payload()
    checkpoints = cast(list[dict[str, object]], payload["checkpoints"])
    checkpoints[0][field] = value
    report.environment["schema_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("suffix", "value"),
    [
        ("stderr", "failed"),
        ("exit", 1),
        ("exit", False),
        ("timeout", True),
        ("timeout", 0),
        ("overflow", True),
        ("parent_descriptors_after", 6),
        ("parent_threads_after", 2),
        ("parent_threads_before", True),
    ],
)
def test_ir_schema_payload_requires_success_and_parent_release(
    suffix: str, value: str | int | bool
) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.environment[f"schema_lifetime_1_{suffix}"] = value

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


def test_ir_report_accepts_selected_schema_with_signed_allocation_differences() -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = schema_payload()
    checkpoints = cast(list[dict[str, object]], payload["checkpoints"])
    checkpoints[0]["retained_bytes"] = -10
    checkpoints[0]["allocation_differences"] = [
        {"location": "source.py:1", "bytes": -10, "count": -1}
    ]
    report.environment["schema_lifetime_1"] = json.dumps(payload)

    result = verify_report(
        report.model_dump_json(), rendered_junit(expected), REVISION, expected
    )

    assert len(result.samples) == 4


@pytest.mark.parametrize(
    "suffix",
    [
        "stderr",
        "exit",
        "timeout",
        "overflow",
        "parent_descriptors_before",
        "parent_descriptors_after",
        "parent_threads_before",
        "parent_threads_after",
    ],
)
def test_ir_schema_process_metadata_is_required(suffix: str) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.environment.pop(f"schema_lifetime_1_{suffix}")

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    "field",
    ["current_rss_bytes", "peak_rss_bytes", "os_threads", "baseline_os_threads"],
)
def test_linux_schema_evidence_requires_native_counters(field: str) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.platform = "linux"
    payload = schema_payload()
    payload["platform"] = "linux"
    if field.startswith("baseline_"):
        payload[field] = None
    else:
        checkpoints = cast(list[dict[str, object]], payload["checkpoints"])
        checkpoints[0][field] = None
    report.environment["schema_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR schema"):
        verify_schema_payloads(report, expected)


@pytest.mark.parametrize(
    "field", ["sha256", "bytes", "definitions", "references", "definition_names"]
)
def test_ir_schema_manifest_matches_every_source_owned_artifact_field(
    field: str,
) -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = schema_payload()
    manifest = cast(dict[str, object], payload["schema_manifest"])
    artifact = cast(dict[str, object], manifest["validation"])
    artifact.pop(field)
    report.environment["schema_lifetime_1"] = json.dumps(payload)

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


def test_ir_schema_payload_rejects_a_stale_outer_manifest() -> None:
    expected = schema_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    report.environment["schema_fixture_manifest"] = "{}"

    with pytest.raises(ValueError, match="IR schema"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )
