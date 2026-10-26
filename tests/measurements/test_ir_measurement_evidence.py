import base64
import hashlib
import json
import sys
from collections import Counter
from html import escape
from pathlib import Path
from traceback import format_exception
from typing import cast

import pytest

from tests.measurements.cost_workload import MEASURED, cost_cases
from tests.measurements.harness import ExtraValue, Report, Sample
from tests.measurements.ir_config_lifetime import CONFIGURATIONS
from tests.measurements.ir_measurement_evidence import (
    ACCOUNTING_ENCODED_LIMIT,
    ACCOUNTING_OUTPUT_LIMIT,
    ROOT,
    AccountingChild,
    Evidence,
    MeasurementReport,
    accepted_evidence,
    accounting_children,
    accounting_evidence,
    configuration_evidence,
    expected_evidence,
    junit_nodes,
    schema_evidence,
    shard_evidence,
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


def accounting_payload(child: AccountingChild) -> Report:
    report = Report(
        "2026-10-23T00:00:00Z", sys.platform, sys.version.split()[0], REVISION
    )
    report.environment.update(
        run=child.identity,
        pid=100 + child.run,
        pydantic="synthetic-version",
        pydantic_core="synthetic-version",
        executable="/synthetic/python",
        cwd="/synthetic/repository",
        warm_up=3,
        ordinary_measured=30,
        large_measured=10,
        operations_per_batch=1,
        ordinary_cycles=1000,
        large_cycles=3,
        rss_source="/bin/ps rss KiB converted to bytes"
        if sys.platform == "darwin"
        else "/proc/self/statm resident pages converted to bytes",
        peak_rss_source="getrusage RUSAGE_SELF ru_maxrss converted to bytes",
        unowned_resources="no tasks, connections, timers or subscriptions acquired",
    )
    if child.mode != "timing":
        cycles = child.cycles()
        traced = child.mode == "traced"
        report.environment.update(
            traced=traced,
            cycles=cycles,
            successes=cycles * sum(case.accepted for case in child.cases),
            refusals=cycles * sum(not case.accepted for case in child.cases),
            retaining_paths=json.dumps(["", "", ""]),
            checkpoints=json.dumps(
                [
                    {
                        "cycle": cycle,
                        "heap": 100 if traced else -1,
                        "peak": 200 if traced else -1,
                        "tracer": 300 if traced else -1,
                        "rss": -1 if traced else 1000,
                        "high_water": -1 if traced else 2000,
                        "descriptors": 4,
                        "threads": 1,
                    }
                    for cycle in (0, cycles // 3, cycles * 2 // 3, cycles)
                ]
            ),
        )
    for (name, repeat, count), multiplicity in child.samples().items():
        report.samples.extend(
            Sample(name, repeat, count, 0.00000001) for _ in range(multiplicity)
        )
    return report


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
    cases: dict[str, list[str]] = {"ordinary": [], "large": []}
    for case in cost_cases():
        cases[case.group].append(case.name)
    report.environment["accounting_case_inventory"] = json.dumps(cases)
    for child in accounting_children():
        if child.node not in inventory.nodes:
            continue
        payload = accounting_payload(child)
        raw = payload.to_json()
        report.environment[child.identity] = raw
        report.environment[f"{child.identity}_process"] = json.dumps(
            {
                "revision": REVISION,
                "run": child.run,
                "mode": child.mode,
                "group": child.group,
                "batch": child.batch,
                "cases": [case.name for case in child.cases],
                "pid": payload.environment["pid"],
                "code": 0,
                "timed_out": False,
                "overflow": False,
                "elapsed_seconds": 0.1,
                "stdout_base64": base64.b64encode(raw.encode()).decode(),
                "stderr_base64": "",
                "output_scope": "all_runner_retained_bytes_capped_per_stream",
            }
        )
        report.samples.extend(payload.samples)
        name, repeat, count = child.parent_sample()
        report.samples.append(Sample(name, repeat, count, 0.1))
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
        if name.startswith("cost_"):
            continue
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


@pytest.fixture(scope="module")
def accounting_document() -> str:
    return rendered_report(accounting_evidence())


@pytest.fixture(scope="module")
def shard_documents() -> tuple[str, ...]:
    return tuple(rendered_report(shard) for shard in shard_evidence())


def replace_accounting_output(
    report: MeasurementReport, identity: str, raw: str
) -> None:
    process = cast(
        dict[str, object],
        json.loads(cast(str, report.environment[f"{identity}_process"])),
    )
    process["stdout_base64"] = base64.b64encode(raw.encode()).decode()
    report.environment[f"{identity}_process"] = json.dumps(process)
    report.environment[identity] = raw


def test_accounting_timing_outlier_preserves_per_child_diagnostics() -> None:
    child = next(child for child in accounting_children() if child.mode == "timing")
    expected = accounting_evidence([child])
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = accounting_payload(child)
    workload = f"cost_{child.cases[0].name}"
    for sample in payload.samples:
        if sample.workload == workload:
            sample.seconds = 1.0
    raw = payload.to_json()
    replace_accounting_output(report, child.identity, raw)
    replacement = MeasurementReport.model_validate_json(raw)
    report.samples = [
        sample for sample in report.samples if sample.workload != workload
    ] + [sample for sample in replacement.samples if sample.workload == workload]
    diagnostics: list[dict[str, ExtraValue]] = []

    verify_report(
        report.model_dump_json(),
        rendered_junit(expected),
        REVISION,
        expected,
        diagnostics,
    )

    assert len(diagnostics) == len(child.cases)
    observed = next(item for item in diagnostics if item["workload"] == workload)
    assert observed["child"] == child.identity
    assert observed["observed_seconds"] == 1.0
    assert observed["exceeds_reference"] is True
    assert observed["reference_seconds"] == 2.0**-16


@pytest.mark.parametrize("seconds", [-1.0, float("inf"), float("nan")])
def test_accounting_timing_diagnostics_refuse_invalid_samples(seconds: float) -> None:
    child = next(child for child in accounting_children() if child.mode == "timing")
    expected = accounting_evidence([child])
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    payload = accounting_payload(child)
    payload.samples[0].seconds = seconds
    replace_accounting_output(report, child.identity, payload.to_json())

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


def test_ir_report_accepts_every_accounting_child(accounting_document: str) -> None:
    expected = accounting_evidence()

    report = verify_report(
        accounting_document, rendered_junit(expected), REVISION, expected
    )

    assert len(report.gates) == 765
    assert len(report.samples) == 2745
    assert (
        len([name for name in report.environment if name.endswith("_process")]) == 126
    )
    assert len([name for name in report.environment if name.startswith("cost_")]) == 252


@pytest.mark.parametrize(
    ("run", "gates", "samples", "children"),
    [
        (0, 442, 7903, 0),
        (1, 175, 2551, 27),
        (2, 598, 202, 99),
        (3, 4, 4, 0),
        (4, 4, 4, 0),
        (5, 4, 4, 0),
    ],
)
def test_ir_report_accepts_complete_owned_shard(
    shard_documents: tuple[str, ...],
    run: int,
    gates: int,
    samples: int,
    children: int,
) -> None:
    expected = shard_evidence()[run]

    report = verify_report(
        shard_documents[run], rendered_junit(expected), REVISION, expected
    )

    assert len(report.gates) == gates
    assert len(report.samples) == samples
    assert (
        len(
            [
                key
                for key in report.environment
                if key.startswith("cost_") and key.endswith("_process")
            ]
        )
        == children
    )
    assert (
        len([key for key in report.environment if key.startswith("cost_")])
        == children * 2
    )
    assert (
        f"config_lifetime_{run}" if run < 3 else f"schema_lifetime_{run - 3}"
    ) in report.environment


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_node",
        "duplicate_node",
        "corrupt_node",
        "missing_gate",
        "corrupt_gate",
        "extra_gate",
        "missing_sample",
        "duplicate_sample",
        "corrupt_sample",
        "same_set_multiplicity",
    ],
)
def test_ir_report_refuses_tampered_accounting_inventory(
    shard_documents: tuple[str, ...], mutation: str
) -> None:
    expected = shard_evidence()[2]
    report = MeasurementReport.model_validate_json(shard_documents[2])
    nodes = cast(
        list[str], json.loads(cast(str, report.environment["selected_test_nodes"]))
    )
    if mutation == "missing_node":
        nodes.pop()
    elif mutation == "duplicate_node":
        nodes.append(nodes[0])
    elif mutation == "corrupt_node":
        nodes[0] += "wrong"
    elif mutation == "missing_gate":
        report.gates.pop(next(iter(report.gates)))
    elif mutation == "corrupt_gate":
        report.gates[next(iter(report.gates))] = False
    elif mutation == "extra_gate":
        report.gates["cost_extra_completed"] = True
    elif mutation == "missing_sample":
        report.samples.pop()
    elif mutation == "duplicate_sample":
        report.samples.append(report.samples[0])
    elif mutation == "corrupt_sample":
        report.samples[0].count += 1
    else:
        before = Counter(
            (sample.workload, sample.repeat, sample.count) for sample in report.samples
        )
        index = next(
            index
            for index, sample in enumerate(report.samples)
            if sample.workload == "cost_large_traced_lifetime"
        )
        report.samples[index] = next(
            sample
            for sample in report.samples
            if sample.workload == "cost_ordinary_native_lifetime"
        )
        after = Counter(
            (sample.workload, sample.repeat, sample.count) for sample in report.samples
        )
        assert set(before) == set(after)
        assert sum(before.values()) == sum(after.values())
        assert before != after
    report.environment["selected_test_nodes"] = json.dumps(nodes)

    with pytest.raises(ValueError, match="incomplete IR shard evidence"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_process",
        "missing_report",
        "extra",
        "substituted",
        "child_value",
        "parent_value",
    ],
)
def test_ir_report_refuses_missing_or_substituted_accounting_outcomes(
    shard_documents: tuple[str, ...], mutation: str
) -> None:
    expected = shard_evidence()[1]
    report = MeasurementReport.model_validate_json(shard_documents[1])
    identity = "cost_timing_ordinary_0_0"
    if mutation == "missing_process":
        report.environment.pop(f"{identity}_process")
    elif mutation == "missing_report":
        report.environment.pop(identity)
    elif mutation == "extra":
        report.environment["cost_unselected_process"] = report.environment[
            f"{identity}_process"
        ]
    elif mutation == "substituted":
        report.environment[f"{identity}_process"] = report.environment[
            "cost_timing_ordinary_0_1_process"
        ]
    elif mutation == "child_value":
        sample = report.samples[0]
        sample.seconds *= 2
        sample.us_per_record *= 2
    else:
        sample = next(
            sample
            for sample in report.samples
            if sample.workload == "cost_ordinary_timing_process"
        )
        sample.seconds *= 2
        sample.us_per_record *= 2

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", "wrong"),
        ("run", 1),
        ("mode", "native"),
        ("group", "large"),
        ("batch", 1),
        ("cases", ["union_1", "union_1"]),
        ("pid", 0),
        ("pid", True),
        ("code", 1),
        ("code", False),
        ("timed_out", True),
        ("timed_out", 0),
        ("overflow", True),
        ("overflow", 0),
        ("elapsed_seconds", -1.0),
        ("elapsed_seconds", float("nan")),
        ("stderr_base64", "ZmFpbA=="),
        ("stdout_base64", "!invalid"),
        ("stdout_base64", "é"),
        ("output_scope", "unbounded"),
        ("extra", "unexpected"),
    ],
)
def test_ir_report_refuses_invalid_accounting_process_fields(
    shard_documents: tuple[str, ...], field: str, value: object
) -> None:
    expected = shard_evidence()[1]
    report = MeasurementReport.model_validate_json(shard_documents[1])
    key = "cost_timing_ordinary_0_0_process"
    process = cast(dict[str, object], json.loads(cast(str, report.environment[key])))
    process[field] = value
    report.environment[key] = json.dumps(process)

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize("field", ["pid", "code", "stdout_base64", "output_scope"])
def test_ir_report_requires_accounting_process_fields(
    shard_documents: tuple[str, ...], field: str
) -> None:
    expected = shard_evidence()[1]
    report = MeasurementReport.model_validate_json(shard_documents[1])
    key = "cost_timing_ordinary_0_0_process"
    process = cast(dict[str, object], json.loads(cast(str, report.environment[key])))
    process.pop(field)
    report.environment[key] = json.dumps(process)

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "revision",
        "sample",
        "duplicate_sample",
        "summary",
        "raw_mismatch",
        "duplicate_json",
    ],
)
def test_ir_report_refuses_corrupt_accounting_child_report(
    shard_documents: tuple[str, ...], mutation: str
) -> None:
    expected = shard_evidence()[1]
    report = MeasurementReport.model_validate_json(shard_documents[1])
    identity = "cost_timing_ordinary_0_0"
    child = MeasurementReport.model_validate_json(
        cast(str, report.environment[identity])
    )
    if mutation == "revision":
        child.revision = "wrong"
    elif mutation == "sample":
        child.samples.pop()
    elif mutation == "duplicate_sample":
        child.samples[1] = child.samples[0]
    elif mutation == "summary":
        child.summary["cost_union_1"]["repeats"] = 29
    raw = child.model_dump_json()
    if mutation == "raw_mismatch":
        report.environment[identity] = raw + " "
    else:
        if mutation == "duplicate_json":
            raw = '{"revision":"wrong",' + raw[1:]
        replace_accounting_output(report, identity, raw)

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run", "wrong"),
        ("pid", 999),
        ("warm_up", 2),
        ("ordinary_measured", True),
        ("operations_per_batch", 2),
        ("traced", False),
        ("cycles", 999),
        ("successes", 0),
        ("refusals", 0),
        ("retaining_paths", "[]"),
        ("checkpoints", "[]"),
    ],
)
def test_ir_report_refuses_invalid_accounting_child_metadata(
    shard_documents: tuple[str, ...], field: str, value: object
) -> None:
    expected = shard_evidence()[2]
    report = MeasurementReport.model_validate_json(shard_documents[2])
    identity = "cost_traced_ordinary_all_0"
    child = cast(dict[str, object], json.loads(cast(str, report.environment[identity])))
    environment = cast(dict[str, object], child["environment"])
    environment[field] = value
    replace_accounting_output(report, identity, json.dumps(child))

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cycle", 334),
        ("heap", 262245),
        ("rss", 0),
        ("threads", 2),
        ("descriptors", True),
        ("extra", 1),
    ],
)
def test_ir_report_refuses_invalid_accounting_checkpoints(
    shard_documents: tuple[str, ...], field: str, value: object
) -> None:
    expected = shard_evidence()[2]
    report = MeasurementReport.model_validate_json(shard_documents[2])
    identity = "cost_traced_ordinary_all_0"
    child = cast(dict[str, object], json.loads(cast(str, report.environment[identity])))
    environment = cast(dict[str, object], child["environment"])
    points = cast(
        list[dict[str, object]], json.loads(cast(str, environment["checkpoints"]))
    )
    points[1][field] = value
    environment["checkpoints"] = json.dumps(points)
    replace_accounting_output(report, identity, json.dumps(child))

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("rss", 8389609), ("heap", 100), ("high_water", -1), ("threads", 2)],
)
def test_ir_report_refuses_invalid_accounting_native_checkpoints(
    shard_documents: tuple[str, ...], field: str, value: int
) -> None:
    expected = shard_evidence()[2]
    report = MeasurementReport.model_validate_json(shard_documents[2])
    identity = "cost_native_ordinary_all_0"
    child = cast(dict[str, object], json.loads(cast(str, report.environment[identity])))
    environment = cast(dict[str, object], child["environment"])
    points = cast(
        list[dict[str, object]], json.loads(cast(str, environment["checkpoints"]))
    )
    points[1][field] = value
    environment["checkpoints"] = json.dumps(points)
    replace_accounting_output(report, identity, json.dumps(child))

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


def test_ir_configuration_shard_refuses_accounting_outcomes(
    accounting_document: str,
) -> None:
    expected = configuration_evidence(1)
    report = MeasurementReport.model_validate_json(rendered_report(expected))
    accounting = MeasurementReport.model_validate_json(accounting_document)
    key = "cost_timing_ordinary_0_0_process"
    report.environment[key] = accounting.environment[key]

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize(
    ("run", "identity"),
    [
        (0, "cost_timing_ordinary_0_0"),
        (0, "cost_traced_ordinary_all_0"),
        (1, "cost_traced_ordinary_all_0"),
        (2, "cost_timing_ordinary_0_0"),
    ],
)
def test_ir_shard_refuses_another_shards_valid_accounting_pair(
    shard_documents: tuple[str, ...],
    accounting_document: str,
    run: int,
    identity: str,
) -> None:
    expected = shard_evidence()[run]
    report = MeasurementReport.model_validate_json(shard_documents[run])
    accounting = MeasurementReport.model_validate_json(accounting_document)
    for key in (identity, f"{identity}_process"):
        report.environment[key] = accounting.environment[key]

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )


@pytest.mark.parametrize("run", [1, 2])
@pytest.mark.parametrize("key_suffix", ["", "_process"])
@pytest.mark.parametrize("shape", ["mapping", "sequence"])
def test_ir_accounting_outer_wrong_types_do_not_echo_input(
    shard_documents: tuple[str, ...], run: int, key_suffix: str, shape: str
) -> None:
    sentinel = "sensitive-artifact-" + "outer-type-must-not-escape"
    expected = shard_evidence()[run]
    document = cast(dict[str, object], json.loads(shard_documents[run]))
    environment = cast(dict[str, object], document["environment"])
    identity = "cost_timing_ordinary_0_0" if run == 1 else "cost_traced_ordinary_all_0"
    environment[f"{identity}{key_suffix}"] = (
        {"sensitive": sentinel} if shape == "mapping" else [sentinel]
    )

    with pytest.raises(ValueError, match="^incomplete IR shard evidence$") as raised:
        verify_report(
            json.dumps(document), rendered_junit(expected), REVISION, expected
        )
    rendered = "".join(format_exception(raised.value))

    assert str(raised.value) == "incomplete IR shard evidence"
    assert sentinel not in rendered


@pytest.mark.parametrize(
    ("run", "boundary"),
    [
        (2, "envelope"),
        (2, "child"),
        (2, "checkpoints"),
        (2, "retaining_paths"),
        (2, "oversized_encoded"),
        (2, "decoded_cap"),
        (2, "oversized_child"),
        (2, "unicode"),
        (2, "invalid_utf8"),
        (2, "typed_field"),
        (1, "envelope"),
        (1, "child"),
        (1, "oversized_encoded"),
        (1, "typed_field"),
    ],
)
def test_ir_accounting_raw_refusals_are_bounded_and_content_free(
    shard_documents: tuple[str, ...], run: int, boundary: str
) -> None:
    sentinel = "sensitive-artifact-" + "must-not-escape"
    expected = shard_evidence()[run]
    report = MeasurementReport.model_validate_json(shard_documents[run])
    identity = "cost_timing_ordinary_0_0" if run == 1 else "cost_traced_ordinary_all_0"
    key = f"{identity}_process"
    process = cast(dict[str, object], json.loads(cast(str, report.environment[key])))
    deep = "[" * 2000 + json.dumps(sentinel) + "]" * 2000
    if boundary == "envelope":
        report.environment[key] = deep
    elif boundary == "child":
        replace_accounting_output(report, identity, deep)
    elif boundary in {"checkpoints", "retaining_paths", "typed_field"}:
        child = cast(
            dict[str, object], json.loads(cast(str, report.environment[identity]))
        )
        environment = cast(dict[str, object], child["environment"])
        environment["pid" if boundary == "typed_field" else boundary] = (
            sentinel if boundary == "typed_field" else deep
        )
        replace_accounting_output(report, identity, json.dumps(child))
    elif boundary == "oversized_encoded":
        process["stdout_base64"] = sentinel + "A" * ACCOUNTING_ENCODED_LIMIT
        report.environment[key] = json.dumps(process)
    elif boundary == "decoded_cap":
        oversized = sentinel + "A" * (ACCOUNTING_OUTPUT_LIMIT + 1 - len(sentinel))
        process["stdout_base64"] = base64.b64encode(oversized.encode()).decode()
        report.environment[key] = json.dumps(process)
    elif boundary == "oversized_child":
        report.environment[identity] = sentinel + "x" * ACCOUNTING_OUTPUT_LIMIT
    elif boundary == "unicode":
        child = cast(
            dict[str, object], json.loads(cast(str, report.environment[identity]))
        )
        environment = cast(dict[str, object], child["environment"])
        environment["retaining_paths"] = sentinel + "\ud800"
        replace_accounting_output(report, identity, json.dumps(child))
    else:
        process["stdout_base64"] = base64.b64encode(
            b"\xff" + sentinel.encode()
        ).decode()
        report.environment[key] = json.dumps(process)

    with pytest.raises(ValueError, match="IR accounting") as raised:
        verify_report(
            report.model_dump_json(), rendered_junit(expected), REVISION, expected
        )
    rendered = "".join(format_exception(raised.value))

    assert sentinel not in rendered
    assert "IR accounting" in rendered


def test_ir_evidence_inventory_preserves_every_named_action_and_sample() -> None:
    expected = expected_evidence()
    accepted = accepted_evidence()
    accounting = accounting_evidence()

    workers = [configuration_evidence(run) for run in range(3)] + [
        schema_evidence(run) for run in range(3)
    ]

    assert (
        len(accepted.nodes),
        len(accepted.gates),
        sum(accepted.samples.values()),
    ) == (
        337,
        462,
        7923,
    )
    assert accepted.nodes <= expected.nodes
    assert accepted.gates <= expected.gates
    assert accepted.samples <= expected.samples
    assert (
        len(accounting.nodes),
        len(accounting.gates),
        sum(accounting.samples.values()),
    ) == (
        22,
        765,
        2745,
    )
    assert len(accounting_children()) == 126
    assert not accepted.nodes & accounting.nodes
    assert not accepted.gates & accounting.gates
    assert len(expected.nodes) == 359
    assert len(expected.gates) == 1227
    assert sum(expected.samples.values()) == 10668
    assert all(len(worker.nodes) == 1 for worker in workers)
    assert all(len(worker.gates) == 4 for worker in workers)
    assert all(sum(worker.samples.values()) == 4 for worker in workers)
    assert all(worker.nodes <= expected.nodes for worker in workers)
    assert all(worker.gates <= expected.gates for worker in workers)
    assert all(worker.samples <= expected.samples for worker in workers)


def test_ir_shard_inventory_preserves_exact_ownership_and_multiplicity() -> None:
    expected = expected_evidence()
    accepted = accepted_evidence()
    shards = shard_evidence()
    children = accounting_children()

    combined_samples = sum(
        (shard.samples for shard in shards), Counter[tuple[str, int, int]]()
    )

    assert [
        (len(shard.nodes), len(shard.gates), sum(shard.samples.values()))
        for shard in shards
    ] == [
        (332, 442, 7903),
        (11, 175, 2551),
        (13, 598, 202),
        (1, 4, 4),
        (1, 4, 4),
        (1, 4, 4),
    ]
    assert set().union(*(shard.nodes for shard in shards)) == expected.nodes
    assert sum(len(shard.nodes) for shard in shards) == len(expected.nodes)
    assert set().union(*(shard.gates for shard in shards)) == expected.gates
    assert sum(len(shard.gates) for shard in shards) == len(expected.gates)
    assert combined_samples == expected.samples
    assert [
        sum(child.node in shard.nodes for child in children) for shard in shards
    ] == [0, 27, 99, 0, 0, 0]
    assert all(
        child.node in shards[1 if child.mode == "timing" else 2].nodes
        for child in children
    )
    assert (
        shards[0].nodes
        | configuration_evidence(1).nodes
        | configuration_evidence(2).nodes
        | frozenset().union(*(shard.nodes for shard in shards[3:]))
        == accepted.nodes
    )
    assert (
        shards[0].gates
        | configuration_evidence(1).gates
        | configuration_evidence(2).gates
        | frozenset().union(*(shard.gates for shard in shards[3:]))
        == accepted.gates
    )
    assert (
        shards[0].samples
        + configuration_evidence(1).samples
        + configuration_evidence(2).samples
        + sum((shard.samples for shard in shards[3:]), Counter[tuple[str, int, int]]())
        == accepted.samples
    )
    assert len(cost_cases()) == 48


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


def test_ir_report_requires_selected_accounting_child_outcome() -> None:
    actions = [case for case in cost_cases() if case.group == "ordinary"]
    samples = Counter(
        (f"cost_{case.name}", repeat, case.records)
        for case in actions
        for repeat in range(MEASURED)
    )
    samples[("cost_ordinary_timing_process", 0, 1)] += 1
    expected = Evidence(
        frozenset(
            {
                "tests/measurements/test_ir_models_cost.py::"
                "test_accounting_value_cost[ordinary-0]"
            }
        ),
        frozenset(
            {"cost_timing_ordinary_0_0_completed"}
            | {
                f"cost_timing_ordinary_0_0_cost_{case.name}_p95_captured"
                for case in actions
            }
        ),
        samples,
    )
    document = MeasurementReport.model_validate_json(rendered_report(expected))
    document.environment.pop("cost_timing_ordinary_0_0_process")
    document.environment.pop("cost_timing_ordinary_0_0")
    junit = rendered_junit(expected)

    with pytest.raises(ValueError, match="IR accounting"):
        verify_report(document.model_dump_json(), junit, REVISION, expected)


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing",
        "duplicate",
        "revision",
        "manifest",
        "extra_file",
        "missing_receipt",
        "swapped_content",
        "accounting_move",
        "accounting_copy",
        "accounting_swap",
    ],
)
def test_ir_union_checks_all_artifact_identities(
    tmp_path: Path, mutation: str | None
) -> None:
    full = expected_evidence()
    inventories = list(shard_evidence())
    if mutation in {"accounting_move", "accounting_copy", "accounting_swap"}:
        children = accounting_children()
        timing = accounting_evidence(
            [child for child in children if child.mode == "timing"]
        )
        lifetime = accounting_evidence(
            [child for child in children if child.mode != "timing"]
        )
        worker1 = configuration_evidence(1)
        worker2 = configuration_evidence(2)
        if mutation == "accounting_swap":
            inventories[1] = Evidence(
                worker1.nodes | lifetime.nodes,
                worker1.gates | lifetime.gates,
                worker1.samples + lifetime.samples,
            )
            inventories[2] = Evidence(
                worker2.nodes | timing.nodes,
                worker2.gates | timing.gates,
                worker2.samples + timing.samples,
            )
        else:
            inventories[2] = Evidence(
                inventories[2].nodes | timing.nodes,
                inventories[2].gates | timing.gates,
                inventories[2].samples + timing.samples,
            )
            if mutation == "accounting_move":
                inventories[1] = worker1
        if mutation != "accounting_copy":
            assert (
                set().union(*(inventory.nodes for inventory in inventories))
                == full.nodes
            )
            assert sum(len(inventory.nodes) for inventory in inventories) == len(
                full.nodes
            )
            assert (
                set().union(*(inventory.gates for inventory in inventories))
                == full.gates
            )
            assert sum(len(inventory.gates) for inventory in inventories) == len(
                full.gates
            )
            assert (
                sum(
                    (inventory.samples for inventory in inventories),
                    Counter[tuple[str, int, int]](),
                )
                == full.samples
            )
    if mutation == "swapped_content":
        inventories[1], inventories[2] = inventories[2], inventories[1]
    for run, inventory in enumerate(inventories):
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
    elif mutation == "extra_file":
        (
            tmp_path / f"ir-models-cost-measurement-{REVISION}-1" / "unexpected.json"
        ).write_text("{}")
    elif mutation == "missing_receipt":
        (
            tmp_path
            / f"ir-models-cost-measurement-{REVISION}-2"
            / "ir-checkout-revision-2.txt"
        ).unlink()

    if mutation is None:
        result = verify_union(tmp_path, REVISION)

        assert (result["tests"], result["gates"], result["samples"]) == (
            359,
            1227,
            10668,
        )
    else:
        with pytest.raises(ValueError, match="IR"):
            verify_union(tmp_path, REVISION)


@pytest.mark.parametrize("mutation", ["missing", "malformed", "object", "extra_run"])
def test_ir_report_requires_only_its_selected_configuration_payload(
    mutation: str,
) -> None:
    expected = shard_evidence()[1]
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
    assert len(evidence.nodes - expected_nodes) == 349
    assert len(evidence.gates - expected_gates) == 1109
    assert (
        sum(
            multiplicity
            for (workload, _, _), multiplicity in evidence.samples.items()
            if not workload.startswith("vcs_")
        )
        == 9566
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
