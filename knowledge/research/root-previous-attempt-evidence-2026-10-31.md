# Root previous-attempt reference evidence

Checked 2026-10-31. Implementation owner: [issue #546](https://github.com/realkarych/kora/issues/546). The [pre-code plan](https://github.com/realkarych/kora/issues/546#issuecomment-6537600101) implements the accepted root exclusion from [IR validation](../specs/ir.md#10-validation-rules-kora-validate) and [ADR-0007](../../docs/decisions/0007-attempt-model-and-retries.md).

## Delivered scope and ownership

`validate_root_previous_attempt_refs` scans every root node and authored input once. Each exact `$previous_attempt` source receives one fixed IR006 at the exact `from` field. Node kind, optionality, node retry and referenced-body retry cannot place a root declaration inside a subgraph iteration. No prerequisite type, link or path failure suppresses this lexical exclusion.

The frozen result retains only copied bounded diagnostics and root node/input counts. Its scoped validity covers no stored-body usage: unused, shared and recursive definitions are not traversed. Full body/default/inherited retry semantics, PreviousAttempt payload/type, installation/provider metadata and public admission remain with their implementing consumers in work items 3/11. The shared IR026/coverage metadata design is independent and unimplemented here.

No graph-link behavior, public API/client/CLI/MCP operation, database, dependency, package-layer permission, persistent format, supported platform, quota or verification gate changes. Existing TM-04/TM-06/TM-07/TM-10 obligations are exercised through source selection, safe output, detached ownership and bounded collection; no new actor or authority is introduced.

## Blocking evidence

| Obligation | Owning evidence |
|---|---|
| All eight root node kinds and exact operational source | `test_root_previous_attempt_refuses_every_node_kind`, exact-source and absent/empty-input cases |
| Optional/node/body retry cannot legalize root use | Node-retry/optional and referenced-body retry cases |
| Scoped result does not claim body legality or expand definitions | `test_root_previous_attempt_does_not_check_stored_bodies` includes unused/shared/recursive bodies and absent/empty/one/multiple-attempt retry forms |
| Independent faults and deterministic safe metadata | Prerequisite-independence and node-only safe-source/path/null-offset cases |
| Existing count/byte omissions never permit success | One `test_root_previous_attempt_preserves_omission_invalidity` integration case; collector infrastructure remains separately owned |
| No input mutation or retained document/node/input/source/result | `test_root_previous_attempt_preserves_and_releases_borrowed_inputs`, success and refusal, exact map/export identity and weak-reference release |
| Safe completion and sink independence | `test_root_previous_attempt_logging_is_content_free` and `test_root_previous_attempt_result_survives_unavailable_sink`: DEBUG configuration, authorized context/reset, stdout exclusion, real blocked/broken pipes, bounded queues and actual writer shutdown |

Local CPython 3.11.17 in the task-private locked environment: 47 new semantic/logging cases passed, and 100 related graph-link/collector/import-layer/no-suppression cases passed. JUnit files are `/tmp/kora-root-546-focused-final.xml` and `/tmp/kora-root-546-related.xml`; these are local observations without embedded commit identity. The implementation coordinator binds their unchanged source inputs to the committed revision in the PR. Whole Ruff format/lint and edited-specification Markdown checks passed. Actual final-revision strict typing, independent implementation review and complete supported-platform CI are recorded in the PR; this document does not substitute for them.

The initial focused execution had 45 passes and two fixture failures because logging markers used uppercase node/input identifiers. The corrected markers satisfy the unchanged structural identifier contract; no product assertion was removed. Initial Pyright execution lacked the already-declared CEL optional package in the fresh environment. Installing the existing locked optional packages changes no dependency file. The initial JUnit remains `/tmp/kora-root-546-focused.xml`.

## Review and resource limits

Independent correctness and security design conclusions cover the root-only exclusion and removal of a redundant result counter. Relevant instructions, authored models, graph-link/collector boundaries and ADR-0007 are unchanged between their accepted baseline `a27639436710006141b529442155030c481a65e4` and implementation base `cceee36c14380f2f780c0f11e459f0185de85255`. Later independent policy and StructuredOutput additions do not change root retry scope. Actual implementation review remains a separate required conclusion; service failure leaves it unverified.

The pass owns no process, task, provider, database, sink, cache or retained input. Local traversal state is released on return; diagnostics reconstruct their retained source locations. Existing diagnostics owns bounded collection and logging owns the queue/writer. No new performance method or time/RSS/heap campaign is selected. One successful complete final-head CI supplies full supported-platform checks; the ten-minute whole-workflow feedback target remains a visible diagnostic, without relaxing any hard deadline.
