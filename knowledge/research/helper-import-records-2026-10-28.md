# Canonical lifetime records in one-shot helper imports

Checked 2026-10-28 under [issue #395](https://github.com/realkarych/kora/issues/395). The [bounded implementation plan](https://github.com/realkarych/kora/issues/395#issuecomment-6475872103) removes unnecessary import-time construction without changing helper execution authority. Hosted capacity improvement and complete qualification remain unverified.

## Mechanism and boundaries

The fixed helper requires journal registration/identity records and mutation validation. Its previous imports also constructed parent-only reservation, launch, acquisition, observation and closure models. Importing parent-side process ownership additionally loaded the launch gate and its three adapters.

`kora.protocols.lifetime_records` now owns the existing seven identity/revision aliases, four journal enums, runtime binding, four resource-registration models and the canonical registration union. Their definitions are unchanged. `kora.protocols.lifetime` re-exports the same objects, preserving public and conformance identity. Journal, filesystem and helper code import only the records they need. The pure `complete_identity` predicate moves to runner identity and is re-exported through its former process-module path. Fixed parent-only imports occur inside helper-owner construction and settlement methods.

This removes 14 unrelated lifetime model constructions and the process/gate import path from a fresh helper. Mandatory Pydantic, journal/registration validation, eight mutation/result models and the remaining supporting records still load. There is one definition of each record and no new parser, proxy, package layer, dependency or dynamic loader.

The one-shot isolated launcher, sanitized environment, inherited invocation claim through helper kernel exit, descriptor receipt and `listeners_received` acknowledgement remain unchanged. So do no-follow filesystem anchoring, exact identity checks, serialized journal publication, generation fencing, stalled-writer ownership, recovery staging reconciliation, absolute deadlines and instrumentation. The [lifetime ABI](../specs/conformance-lifetime.md) and ADR-0005/0026 continue to govern these boundaries.

## Attribution and its limits

The [original finite attribution](https://github.com/realkarych/kora/issues/395#issuecomment-6474841382) already answered capacity versus exceptional hang. Source `62bc1791e6bdc56c52f82b1dfe4f21af86315de3`, run 40723666026 completed 261/270 cycles; each parent was interrupted during recovery cycle 87. Parent 0 recorded 1,192 helpers with 246.47 seconds importing the helper out of 251.48 measured helper seconds. This identifies repeated startup cost, not removable work or its critical-path share.

Three additional fresh-interpreter imports at that exact source provided untraced importtime, startup-traced importtime and cProfile attribution. No helper main, mutation or qualification workload ran. On shared macOS, untraced cumulative helper import was 316.491 ms and startup-traced import 589.230 ms. cProfile reported 37 model completions, cumulatively 33 ms within a 203.785 ms import. These categories overlap. The probes used `PYTHONPATH` and disabled bytecode writes, without the production `-I` setup; they cannot be compared as hosted helper latency or used to forecast sufficient savings. Static use tracing establishes the removable definitions independently of those timings. No additional timing campaign is claimed.

The implementation starts from approved master `8d8b766e776e52371921bfdfde2e1955932dfe71`. Extraction definitions and the identity predicate were checked across the baseline difference; historical observations keep their original source and limitations. The complete production diff is 282 added plus deleted lines before subsequent base integration, excluding tests and knowledge records.

## Local verification and independent review

The fresh isolated-import regression failed before the refactor because all three parent modules were loaded. It now excludes those exact modules while allowing the new record leaf. A separate fresh process checks canonical identity of every moved public value, applicable conformance exports and the old/new identity-predicate paths.

Twenty-five complete public DTO, journal, mutation-union and registration-union validation schemas were captured before and after using `model_json_schema()` and `TypeAdapter.json_schema()`. Canonical sorted JSON is byte-identical, SHA-256 `cf563caba3564ef43625d4a224a97a9af800a032d15b12c0e88e3e0d9634be7e`. This is validation-schema evidence, not a separate serialization-schema study; serializers and moved definitions are unchanged.

Local macOS with locked CPython 3.11.17 passed 122 import/value tests, 69 helper/claim/recovery/filesystem tests and 25 architecture checks. Ruff check/format and strict Pyright passed. These are dirty-snapshot observations, not final-head or Linux CI. Final CI retains command-listener and local-process identity coverage absent from the targeted local selection. No existing test, workload, assertion or deadline was removed.

Independent correctness, security, performance and condensation reviewed the bounded design and actual source delta through their distinct profiles and found no implementation defect. No independent source review supplies executable qualification or a latency guarantee. A revision-only or evidence-only update reuses matching conclusions after checking their relevant inputs.

## Remaining qualification

The failed 261/270 campaign remains failed. Completed-cycle settlement does not establish interrupted recovery release or successful parent completion. [Issue #349](https://github.com/realkarych/kora/issues/349) and #395 retain the complete three-parent 270-cycle hosted obligation, source-bound sidecars/raw/JUnit/retention receipts and final validation. The 600-second command cutoff, ten-minute job limit and every per-cycle/recovery/cleanup deadline remain. Neither this refactor nor the earlier narrow merge exception qualifies a release. A changed-input qualification may establish the result; an unchanged failed campaign is never retried for a favorable outcome.
