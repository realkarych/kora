# CommandResult model evidence

Date: 2026-10-30. Implementing issue: [#451](https://github.com/realkarych/kora/issues/451), child of #138, work item 2. Approved scope: bounded standalone revision-1 model, semantic admission, owned canonical identity and supplied-capture byte integrity. The implementation plan and approval provenance are recorded in the [issue plan](https://github.com/realkarych/kora/issues/451#issuecomment-6509948620).

## Delivered boundary

The model owns immutable canonical bytes and exposes detached JSON values. Strict native/raw admission checks closed keys, exact scalar/container types, frozen field limits and relations before snapshot/canonicalization. Existing bounded JSON and protected-path helpers are reused unchanged. Root canonicalization follows Unicode code-point key order; the independently frozen resource manifest uses its fixed ASCII-key/integer JCS subset and UTF-8 name order. All public exports revalidate unchecked state.

Supplied-byte integrity checks compare size, complete SHA-256 and exact retained suffix after preflighting both capture byte limits. They load no content, preserve no external resource and establish no sanitation/provenance/owner authorization or execution success. For captures up to 4 KiB, the complete retained bytes are present in the tail and their digest is checked during model admission.

Actual producers, upload wire and capture-owner mappings, installed behavior, complete replay/retention, CEL/checker and aggregate lists remain pending with the named owners in the [CommandResult contract](../specs/artifacts/CommandResult.md). Raw model parsing can canonicalize whitespace; addressed canonical-wire refusal remains an upload/promotion obligation.

## Verification plan and applicability

Blocking evidence covers the four named model tests, every applicable immutable canonical/semantic vector, strict integer tokens and native types, named boundary pairs, manifest hash/order/NFC, path protection, stream counters/base64/tail, immutable ownership, forged state, exact descriptor and safe diagnostics. Existing publication files remain unchanged. No metadata database is touched; both-database lifecycle checks activate with the real consumers.

The model acquires no descriptor, thread, socket, process, storage owner or cache. Its value owns bounded bytes until normal Python release; measurement child processes and their pipes are owned by the existing harness. No logger is configured. Fixed CR001–CR006 ContractViolation messages/root paths exclude content and decoder exception context. This supplies applicable pure-library diagnostics without activating a process logging row.

Threat coverage uses existing TM-04/TM-10 untrusted admission, TM-06/TM-07 content/diagnostics, TM-08/TM-11 immutable identity/integrity and TM-12 frozen semantics/evidence. Existing threat entries already cover the actors, authority and controls. The model adds scoped evidence, not another trust boundary; no threat-model control or authority change is needed.

## Existing measurement method

Extend the existing IR action/lifetime route: three warm-ups, ten measured action samples, signed traced settled/peak observations, and three serial traced/untraced 500-action lifecycle pairs with 21 checkpoints. Each child exercises 248 successes and 252 refusals. The preexisting 366 test identities, 1,271 proof entries and 11,054 samples remain an unchanged subset. New CommandResult evidence adds seven tests, 35 proof entries and 296 samples; the complete union is 373 tests, 1,306 proof entries and 11,350 samples. New identities are assigned once to existing IR shard 3 with unchanged job/child/output deadlines.

Minimal and dense valid native/raw/canonical/digest/summary, raw-at-cap/over/depth/duplicate/late refusal, oversized native shape/fields, forged state and capture integrity at 0/1/4095/4096/4097/64 MiB plus over-limit refusal are measured. Large caller-owned fixtures exist before observation baselines. Dense canonical size is observed, not asserted as a universal valid-shape maximum. Existing JSON/path tests retain their independently owned canonical and work bounds.

Field/root/capture bounds, correct outcomes, identity and exact external-resource counters are blocking. Heap/RSS/time are diagnostics under the existing approved IR method, with retained signed observations; they prove no universal latency or leak-freedom guarantee. No campaign, lane, timeout, numerical threshold or acceptance-policy change is introduced.

## Independent reviews

Fresh read-only assignments reviewed trusted base `84c35e22339e5d4325a9cb1280f6fd3181ef904e`, approved scope, governing contracts and existing helpers. Separate correctness, security and performance reviewers returned passed design results without blocking findings; their actual identities and returned evidence are preserved in the issue and PR review records. Their source reviews found no production defect. Correctness requested an isolated full-capture hash regression and removal of session-local identifiers; both corrections are included. Final committed-input and CI/artifact matching remains required in the PR. No implementation-authored summary substitutes for the actual returned results recorded in the PR.

## Actual results

On macOS/Python 3.11.17, the final focused CommandResult, IR schema, evidence-reader and affected architecture run passed 488 tests. Ruff lint/format, strict Pyright and changed-document Markdown lint passed. The separate architecture no-suppression boundary also passed all 18 tests. An earlier broad IR/architecture run passed 2,881 tests and found a stale additive inventory-count literal; the corrected owning reader and union regressions pass in the focused run. Historical failing development runs are not reported as passes.

The seven new measurement tests passed in 11.90 seconds. The exact evidence reader accepted 35 proofs and 296 samples with the declared manifests and lifecycle outcomes. Child measured loops took 0.576–0.589 seconds traced and 0.331–0.332 seconds untraced, below the unchanged hard child deadline. The largest action settled traced delta was +4,400 bytes; this is diagnostic, not a leak-freedom guarantee. Local exploratory observations share a developer host and part of the run overlapped a short ordinary test process; no comparative latency/heap guarantee is inferred. Final CI provides the controlled revision-matched evidence.

Local artifacts: `/tmp/kora-command-result-local/measurements.json` and `junit.xml`, labelled `84c35e2-command-result-working-tree`. They are working-tree development evidence, never relabelled as committed-head results. The source/workload inputs are preserved in this change. Final committed revisions, static-check outcomes, complete supported-platform CI, bounded artifacts and returned reviewer matches are recorded in the PR before readiness.
