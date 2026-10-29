# AgentSession model evidence

Date: 2026-10-30. Implementing issue: [#453](https://github.com/realkarych/kora/issues/453), child of #138, work item 2. The owner approved the bounded model plan before edits; the [detailed implementation plan](https://github.com/realkarych/kora/issues/453#issuecomment-6510610771) and [scope clarification](https://github.com/realkarych/kora/issues/453#issuecomment-6510657561) record the boundaries and acceptance.

## Delivered boundary

The standalone model admits strict native/raw logical values and owns immutable canonical bytes. Complete logical identity includes metadata, the entire denial sample and original/retained transcript facts. Returned values and the frozen allowlisted metadata summary are detached; public exports revalidate unchecked state. Published schema, descriptor, policy, fixtures and vectors remain unchanged.

The existing JSON codec gains an explicit byte-budget argument. AgentSession passes 4,194,304 bytes; StructuredOutput and CommandResult retain their 1,048,576-byte defaults. Depth remains 16. Closed shape/scalar/collection checks precede copying, and the canonical budget precedes retained allocation. Accounting predicates are reused without widening package layers or changing accepted identifier domains. Local USD consistency is checked, while price authority and observation/provenance validation remain with real producers and API/engine consumers.

Supplied transcript bytes are preflighted for exact type and the existing 256 MiB cap before model revalidation or hashing. The verifier hashes the complete retained bytes directly and checks only the bounded truncation suffix; it reads no path, decodes no content, makes no complete-payload copy and does not verify an unreceived source tail. This transient logical model is not a database storage layout or a permanent copy of the denial sample.

## Verification and applicability

The named schema/model, invalid-payload and canonical-logical tests exercise frozen publication fixtures and hashes. Additional regressions cover strict integers and native types, Unicode/duplicate/depth/size refusal, accounting and denial coherence, predicate parity, canonical timestamps, large valid input, alias detachment, forged state, exact descriptor identity, summary exclusion and complete retained-byte integrity.

The pure library owns no descriptor, thread, socket, process, stream, storage lease or global value cache. Fixed AS001–AS006 ContractViolation root messages contain no supplied content or parser cause/context and configure no logger. `test_agent_session_safe_diagnostics` supplies the applicable diagnostics evidence; actual process logging activation stays with its owning consumer.

Existing TM-04/TM-10 cover hostile input/resource bounds, TM-06/TM-07 content/diagnostics, TM-08/TM-11 immutable identity/integrity and TM-12 frozen interpretation/evidence. The threat-model update records scoped implementation evidence without changing actors, controls or authority. Runtime/worker finalization, accepted-view mapping and authoritative pricing, API authorization/replay, storage and separately retained content, CEL and aggregate lists retain the implementing owners and activation tests in the contract. No metadata database is touched by this model; both-database integration remains a real-consumer obligation.

## Existing measurement method

The existing IR route retains three warm-ups, ten measured action samples, signed traced settled/peak observations and three serial traced/untraced 500-action lifecycle pairs. Each child exercises 248 successes and 252 refusals with 21 checkpoints. The prior 373 test identities, 1,306 proof entries and 11,350 samples remain an unchanged subset. AgentSession adds seven test identities, 34 proofs and 286 samples, yielding 380 tests, 1,340 proofs and 11,636 samples. New identities are assigned once to existing IR shard 3. No lane, campaign, deadline, numerical threshold or acceptance-policy change is introduced.

Workloads include minimum and dense native/raw/canonical/digest/summary values; exact/over 4 MiB raw envelopes and late malformed content; duplicate/depth and oversized native collection/scalar refusal; forged instances; and retained transcript integrity at 0/1/24/4097/256 MiB plus over-limit refusal. The dense canonical fixture is 1,287,145 bytes with 64 model rows and 1,000 denial items. Its size proves the explicit larger budget, not a universal maximum of all valid shapes. Caller-owned large fixtures precede observation baselines.

Correct outcomes, product limits and exact external-resource closure are blocking. Heap/RSS/time are diagnostics under the existing method, preserving signed observations without asserting general latency or leak freedom. The existing harness owns measurement child processes and pipes; revision/manifest/sample completeness and the full shard union remain mandatory.

## Independent review

Separate independent read-only correctness, security and performance assignments inspected the approved base `9b51552b5896230f4dad1dad20cbce3f4506518d`, plan, published contract and existing boundaries. All three returned passed plan results. Correctness clarified locally decidable USD consistency and nullable coverage timestamps; security added the scoped threat-model evidence update; performance pinned the dense value above the old default budget. Their actual identities, inspected inputs and results are recorded in the issue and PR. Implementation conclusions and final-head CI matching remain required before readiness.

## Development evidence

On macOS/Python 3.11.17, 164 model tests and 274 evidence-reader tests pass. Strict Pyright, Ruff lint and format pass repository-wide. The first combined focused run passed 776 tests and failed five stale additive evidence-count assertions; those owning inventory/union assertions were updated to include the new workload, and the 274-test evidence suite passes. The 17-test schema suite passes after its additive inventory update; all 189 accounting/pricing tests pass. Historical failures are not reported as passes.

Working-tree measurement artifacts use `/tmp/kora-agent-session-local/measurements.json` and `junit.xml`, labelled `9b51552-agent-session-working-tree`; they are never relabelled as final committed evidence. Actual measurement results, final revision, CI run/artifacts, duration and returned implementation review results are recorded in the PR before readiness.

The seven new measurement tests pass in 37.59 seconds; the exact existing report reader accepts all 34 proofs and 286 samples. Parent-observed child processes take 2.296–4.342 seconds under the unchanged ten-second deadline. Traced measured loops take 3.079–3.197 seconds and untraced loops 1.144–1.193 seconds. Every child retains four descriptors and one Python thread across all 21 checkpoints; native OS-thread counts are unavailable on this macOS method. The largest action settled traced delta is +34,767 bytes. These are diagnostic observations on a developer host, not a general latency or leak-freedom guarantee.

Final settled lifecycle traced deltas are +82,280, +68,153 and +79,154 bytes, exceeding the existing 65,536-byte diagnostic reference. The observation remains visible, not a failed product-bound assertion or evidence of an unbounded leak. Retaining-path summaries are preserved in the raw child artifacts; revisit if the same owned path shows sustained growth with continued operations or a changed consumer introduces retained state. No repeated campaign is used to seek a favorable result. The separate schema-inventory regression initially found one further stale additive total; its preserved-prior-inventory calculation now excludes the newly added AgentSession evidence explicitly.
