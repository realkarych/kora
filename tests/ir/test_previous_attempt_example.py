import json
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import ErrorContext
from kora.cel_eval.evaluate import Machine, ResultType
from kora.ir import CommandResult, GraphDocument, PreviousAttempt, VcsRef
from kora.ir.branch_expressions import checked_branch_sites
from kora.ir.command_result import command_result_digest, verify_command_result_captures
from kora.ir.expression_contexts import InputParameterContexts
from kora.ir.json import JsonObject
from kora.ir.previous_attempt import parse_retained_previous_attempt
from kora.ir.topology import BranchNode, CommandNode
from kora.ir.vcs_ref import parse_vcs_ref, vcs_ref_digest
from kora.protocols.runtime import ArtifactRef
from tests.cel_eval.test_cel_evaluation import clock
from tests.ir.command_result_cases import fixture, stream_bytes
from tests.ir.test_previous_attempt import ARTIFACTS, EXAMPLE, evidence, reference

SQL_ORIGINAL = "SELECT amount FROM sample WHERE amount >= 2 ORDER BY amount"
SQL_OTHER_WRAPPER = "SELECT amount FROM sample WHERE amount = 3 ORDER BY amount"
MISMATCH = "SQL result mismatch"
CANDIDATE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
FAILURE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
OTHER_CANDIDATE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
OTHER_GATE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAY"


@dataclass(frozen=True, slots=True)
class Candidate:
    query: str
    vcs: VcsRef


def candidate(query: str) -> Candidate:
    raw: object = json.loads((ARTIFACTS / "examples/vcs-ref.json").read_bytes())
    value = cast(JsonObject, raw) | {"commit": sha256(query.encode()).hexdigest()}
    return Candidate(query, parse_vcs_ref(value))


def run_gate(directory: Path, command: CommandNode, change: Candidate) -> CommandResult:
    assert command.config.cmd[0] == "python3"
    (directory / "query.sql").write_text(change.query, encoding="utf-8")
    execution = subprocess.run(
        [sys.executable, *command.config.cmd[1:]],
        cwd=directory,
        capture_output=True,
        check=False,
        timeout=5,
    )
    result = CommandResult(
        fixture()
        | {
            "exit_code": execution.returncode,
            "commit": change.vcs.commit,
            "base_commit": change.vcs.base_commit,
            "repo": change.vcs.repo,
            "stdout": stream_bytes(execution.stdout),
            "stderr": stream_bytes(execution.stderr),
        }
    )
    verify_command_result_captures(
        result, stdout=execution.stdout, stderr=execution.stderr
    )
    return result


def accepted_versions(
    change_id: str, change: Candidate, gate_id: str, gate: CommandResult, reason: str
) -> PreviousAttempt:
    refs = {
        "implement.change": ArtifactRef(
            artifact_version_id=change_id,
            payload_digest="sha256:" + vcs_ref_digest(change.vcs),
            type_name="VcsRef",
        ),
        "check.result": ArtifactRef(
            artifact_version_id=gate_id,
            payload_digest="sha256:" + command_result_digest(gate),
            type_name="CommandResult",
        ),
    }
    return PreviousAttempt(
        evidence(
            {slot: reference(ref.artifact_version_id) for slot, ref in refs.items()},
            reason=reason,
        )
    )


def scripted_query(
    feedback: PreviousAttempt | None,
    candidates: dict[str, Candidate],
    gates: dict[str, CommandResult],
) -> str:
    if feedback is None:
        return SQL_ORIGINAL
    value = feedback.value
    refs = cast("dict[str, dict[str, str]]", value["artifacts"])
    change = candidates[refs["implement.change"]["artifact_version_id"]]
    result = gates[refs["check.result"]["artifact_version_id"]]
    assert result.value["commit"] == change.vcs.commit
    if value["reason"] == MISMATCH and result.value["exit_code"] == 3:
        return change.query.replace(">= 2", "> 2")
    return SQL_ORIGINAL


def test_previous_attempt_sql_feedback_example(tmp_path: Path) -> None:
    document = GraphDocument.model_validate_json(EXAMPLE.read_bytes())
    assert isinstance(document.graph.subgraphs, dict)
    body = document.graph.subgraphs["repair_iteration"]
    check, decide = body.nodes["check"], body.nodes["decide"]
    assert isinstance(check, CommandNode)
    assert isinstance(decide, BranchNode)
    assert check.config.success_exit_codes == (0, 3)
    authored = decide.config.cases[0]
    sites = checked_branch_sites(document, max_declarations=32)
    assert isinstance(sites, tuple)
    condition, reason = (site.checked for site in sites)
    contexts = InputParameterContexts(document.type_contracts, document.graph.params)
    environment = contexts.context(body, decide, authored.when)
    assert isinstance(environment, dict)
    types = {name: variable.type for name, variable in environment.items()}
    machine = Machine(load_binding(), clock)
    original = candidate(scripted_query(None, {}, {}))
    unrelated = candidate(SQL_OTHER_WRAPPER)
    consumers = [
        name
        for name, node in body.nodes.items()
        if isinstance(node.inputs, dict)
        and any(
            binding.from_ == "$previous_attempt" for binding in node.inputs.values()
        )
    ]

    first = run_gate(tmp_path, check, original)
    rejected = machine.evaluate(
        condition,
        {"inputs": {"gate": first.value}},
        types,
        ErrorContext("when"),
        ResultType.BOOL,
    )
    evaluated = machine.evaluate(
        reason, {}, {}, ErrorContext("reason"), ResultType.STRING
    )
    assert isinstance(evaluated, str)
    selected = accepted_versions(CANDIDATE_ID, original, FAILURE_ID, first, evaluated)
    saved = parse_retained_previous_attempt(
        selected.canonical_bytes(), expected_payload_digest=selected.payload_digest()
    )
    unrelated_result = run_gate(tmp_path, check, unrelated)
    newest = accepted_versions(
        OTHER_CANDIDATE_ID, unrelated, OTHER_GATE_ID, unrelated_result, evaluated
    )
    candidates = {CANDIDATE_ID: original, OTHER_CANDIDATE_ID: unrelated}
    gates = {FAILURE_ID: first, OTHER_GATE_ID: unrelated_result}
    repaired = candidate(scripted_query(saved, candidates, gates))
    second = run_gate(tmp_path, check, repaired)
    accepted = machine.evaluate(
        condition,
        {"inputs": {"gate": second.value}},
        types,
        ErrorContext("when"),
        ResultType.BOOL,
    )
    wrong = run_gate(
        tmp_path, check, candidate(scripted_query(newest, candidates, gates))
    )
    missing = run_gate(
        tmp_path, check, candidate(scripted_query(None, candidates, gates))
    )

    assert consumers == ["implement"]
    assert first.value["exit_code"] == 3
    assert rejected is True
    assert authored.to == ("$fail_attempt",)
    assert evaluated == MISMATCH
    assert saved.value["artifacts"] == {
        "implement.change": reference(CANDIDATE_ID),
        "check.result": reference(FAILURE_ID),
    }
    assert second.value["exit_code"] == 0
    assert accepted is False
    assert unrelated_result.value["exit_code"] == 0
    assert wrong.value["exit_code"] == missing.value["exit_code"] == 3
