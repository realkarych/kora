# ADR-0012: No transform node kind; deterministic steps are command nodes, the narrative is an agent node, kora attaches cost

- Status: accepted, 2026-10-15; amended 2026-10-16 (packing and formula selection in the canonical graph are command nodes; [architecture critique of 2026-10-16, finding A05](../../knowledge/reviews/architecture-critique-2026-10-16.md#a05-deterministic-selection), issue #17)
- Reviewed: by the project owner, 2026-10-16
- Deciders: project owner, during the design of the intermediate representation; the cost rule revised after the design review of 2026-10-15
- Absorbs: DD-8
- Related: ADR-0006, ADR-0008, ADR-0010

## Context

The canonical SQL optimisation graph has three steps between the agent sessions that do the real work: pack an accepted candidate (its `VcsRef`, metrics and hypothesis) into one `Candidate` artifact; pick the best candidate across the fan-out (largest `cpu_seconds` gain over baseline, at least 15 %, one per query); and assemble the `ReviewBundle` a human approves. None of these is open-ended work, and the temptation while designing the intermediate representation (IR) was to add a deterministic `transform` node kind, a small expression or template over typed inputs, plus a helper CLI for the review bundle, so that no model call would be spent on data shuffling. Such a node needs an expression language rich enough for list filtering, grouping and aggregation, a template language for prose and a cost-aggregation primitive: each a new surface to validate, document and keep in parity across the DSL, the IR and future SDKs and UI editors.

Separately, the approver must see what the run has cost so far. An early draft had the bundle step compute cost by walking lineage from the selected candidate; the design review of 2026-10-15 ([record](../../knowledge/reviews/review-2026-10-15.md)) flagged that lineage walk as a special case.

## Decision

There is no `transform` node kind and no helper CLI (DD-8). The IR keeps eight node kinds: `agent`, `command`, `human_input`, `wait_event`, `timer`, `branch`, `fan_out`, `subgraph`.

- **Deterministic steps are command nodes; the narrative is an agent node** (amended 2026-10-16). A copy and a formula need no model and must be reproducible, so in the canonical example `accept` (the accepted candidate's `VcsRef`, metrics and hypothesis copied into a `Candidate`) and `select_best` (the gain over the baseline, the 15 % threshold, ties broken by the lowest commit, failure when no candidate qualifies) are command nodes that run `pack_candidate.py` and `select_best.py`, command resources that travel with the graph version and are pinned by digest (issue #45; earlier scripts of the target repository under its protected paths), over their materialised inputs; no new node kind is needed for them. `bundle` stays an agent node on the `packer` preset (harness `claude`, model `claude-haiku-5-5`, `sandbox: read_only`, `Read` and read-only git commands, an `output_schema` for the narrative), because it writes prose. An agent node remains the choice where a selection needs judgement: it returns a reference and an explanation, and a command node verifies that the reference belongs to the input set and that every threshold holds.
- **A command node is the deterministic step.** `command` already runs argv in an isolated workspace and parses a declared `output_file` into a `StructuredOutput` or a custom artifact type (FR-2.8); the worker writes the payload of each input to `.kora/inputs/<input name>.json` in the workspace before the command starts, so a script reads lists and nested artifacts from files ([ir.md §4](../../knowledge/specs/ir.md)).
- **kora attaches cost itself.** When the scheduler creates an approval request it sums the run's usage records at request time, per model, with the weakest completeness of its parts, into `approval_request.run_cost`; no node computes cost and no lineage walk runs. The approval view in the API and `kora web` show it next to the form.

This keeps the split that ADR-0008 fixes for the `ReviewBundle` ([0008-content-bound-approvals.md](0008-content-bound-approvals.md)): the agent `bundle` node writes only `narrative` (`summary`, `rationale`); kora writes `facts` from its own records (inputs with digests and provenance, gate results from `CommandResult` rows, metrics with their provenance, the `VcsRef` with its commit, attempt cost) and they are read-only to agents. Approvals bind to the digests of `facts`. The bundle agent explains the choice; it never writes facts. Where an upstream agent session reported a measurement through domain tools, the fact carries that provenance, so the approver sees which numbers a command node measured and which an agent reported.

## Alternatives considered

- **A `transform` node with its own expression language.** Rejected: a second computation model in the IR with its own validator, no clear boundary with the branch node's CEL, and a surface every SDK and UI editor would have to reproduce.
- **Packing and selection as agent nodes on a cheap model**, the decision until 2026-10-16: `accept` copied commit, branch, task id and `cpu_seconds` through a prompt, `select_best` computed the gain and applied the threshold through a prompt, and an `output_schema` typed the result. Replaced: the schema checks the shape only, not that the returned commit belongs to the input set or that the arithmetic is right, so a well-formed output could carry the wrong reference or number into the facts ([finding A05](../../knowledge/reviews/architecture-critique-2026-10-16.md#a05-deterministic-selection)).
- **A helper CLI for the review bundle inside a command node.** Rejected: kora code running inside user workspaces with knowledge of kora's records; the facts now come from the control plane directly.
- **Cost by lineage walk from the selected candidate.** Rejected after the design review of 2026-10-15: the plain sum over the run is what the approver needs (every attempt, fan-out losers included) and has one definition. Cost per accepted change stays derivable offline from `artifact_lineage` and `usage_record` (NFR-8.3).

## Consequences

- Packing and selection cost no model call and produce a `CommandResult` each; only `bundle` produces an `AgentSession` artifact, bounded by the preset's `max_events` and `budget_usd`, and its usage records enter the run's cost.
- Selection rules live in scripts under protected paths, so the candidate an agent wrote cannot alter them; an author whose selection needs judgement writes an agent node that returns a reference and a command node that verifies it.
- The IR, the DSL and the validator stay small: one node vocabulary, CEL for routing, Jinja2 for prompts, nothing else.
- `run_cost` is summed at request time, a snapshot of the run's spend when the request was created; approval requests keep it regardless of retention.

## Where it is applied

- [ir.md §3](../../knowledge/specs/ir.md) (no node kind for building artifacts, `ReviewBundle` facts and narrative), [ir.md §4](../../knowledge/specs/ir.md) (node kinds table, command node outputs, materialised command inputs), [ir.md §4.2](../../knowledge/specs/ir.md) (cost attached to approval requests), [ir.md §11](../../knowledge/specs/ir.md) (`test_candidate_selection_exact_and_reproducible`)
- [examples/sql-optimization.ir.yaml](../../knowledge/specs/examples/sql-optimization.ir.yaml) (`accept` and `select_best` command nodes; `packer` preset and `bundle` agent node)
- [data-model.md §5](../../knowledge/specs/data-model.md) (cost on an approval request) and `approval_request.run_cost` in [data-model.md §2](../../knowledge/specs/data-model.md)
- [scheduler.md §2](../../knowledge/specs/scheduler.md) (the admission pass sums usage records into `run_cost`)
- [api-and-agent-surface.md §2](../../knowledge/specs/api-and-agent-surface.md) (approvals route) and [api-and-agent-surface.md §6](../../knowledge/specs/api-and-agent-surface.md) (web frontend)
- [dsl.md §1](../../knowledge/specs/dsl.md) (`Candidate` type, the two `node.command` constructors, `packer` registry preset)
- [FR-2.8, FR-4.2, FR-9.5](../requirements/functional.md); [functional.md §13, decision 17](../requirements/functional.md); [NFR-8.3](../requirements/non-functional.md)
