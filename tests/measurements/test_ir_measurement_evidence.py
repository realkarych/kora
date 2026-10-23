import hashlib
import json
import sys
from html import escape
from pathlib import Path

import pytest

from tests.measurements.harness import Report, Sample
from tests.measurements.ir_measurement_evidence import (
    ROOT,
    Evidence,
    MeasurementReport,
    configuration_evidence,
    expected_evidence,
    junit_nodes,
    verify_report,
    verify_union,
)

REVISION = "a" * 40


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
