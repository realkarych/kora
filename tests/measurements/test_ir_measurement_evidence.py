import hashlib
import json
import sys
from html import escape
from pathlib import Path
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
    verify_configuration_payloads,
    verify_report,
    verify_union,
)

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
        }
    )
    report.gates.update(dict.fromkeys(inventory.gates, True))
    for run in range(3):
        if configuration_evidence(run).nodes <= inventory.nodes:
            report.environment[f"config_lifetime_{run}"] = json.dumps(
                configuration_payload()
            )
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

    workers = [configuration_evidence(run) for run in range(3)]

    assert len(expected.nodes) == 320
    assert len(expected.gates) == 328
    assert sum(expected.samples.values()) == 6689
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
    worker1 = configuration_evidence(1)
    worker2 = configuration_evidence(2)
    first = Evidence(
        full.nodes - worker1.nodes - worker2.nodes,
        full.gates - worker1.gates - worker2.gates,
        full.samples - worker1.samples - worker2.samples,
    )
    for run, inventory in enumerate((first, worker1, worker2)):
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
        (tmp_path / f"ir-models-cost-measurement-{REVISION}-3").mkdir()

    if mutation is None:
        result = verify_union(tmp_path, REVISION)

        assert (result["tests"], result["gates"], result["samples"]) == (320, 328, 6689)
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
