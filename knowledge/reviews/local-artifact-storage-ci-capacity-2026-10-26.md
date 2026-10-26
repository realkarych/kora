# Local artifact storage CI capacity

Checked 2026-10-26 for issue #255 and PR #258. The approved storage plan remains the implementation authority. This record concerns preservation and scheduling of existing ordinary checks; it does not relax resource budgets or establish final pipeline capacity.

## Observed limit

At revision `6790e4a10360edb6e40c3935833736b983f1a039`, the [storage workflow](https://github.com/realkarych/kora/actions/runs/40242106476) passed all six numerical producers, both qualification jobs and the combined evidence gate. Each numerical producer completed all 832 rows and 112 resource points. Linux durations were 213.605–278.296 seconds and macOS durations were 362.332–392.126 seconds. Each qualification job passed all 38 tests.

The [general workflow](https://github.com/realkarych/kora/actions/runs/40242106485) did not qualify. Linux Python 3.11 passed 5,408 tests in 524.84 seconds, then passed distribution and environment checks, but its job took 601 seconds and was cancelled. The macOS job was cancelled during tests at 82% progress. Successful individual steps do not convert either job or the workflow into a pass. No unchanged repeat supplies capacity evidence.

## Partition and preserved authority

The [capacity refinement](https://github.com/realkarych/kora/issues/255#issuecomment-6404657601) divides each of the three existing platform/interpreter/database environments into two native jobs. The general selection excludes exactly `tests/plugins/storage` and `tests/conformance/test_artifact_storage_conformance.py`; the storage selection includes exactly those two paths. Both retain the default `not measurement` marker, conformance plugin, strict pytest settings, assertions and database parameterization. No subprocess scheduler or concurrent pytest execution is introduced inside a CI job.

Execution jobs have distinct lane-qualified names. Only the three always-running joins use the existing protected Scaffold names. They depend on the entire execution matrix and require successful setup, artifact download, evidence verification and uncancelled execution. CEL retains its `scaffold` dependency. Read-only permissions, action/tool pins, lockfiles, app bindings and ten-minute job limits remain unchanged.

Each execution records actual collection, test output, JUnit and explicit collection/test exit files. The general execution also records the complete default collection. Typed receipts bind the exact arguments, lane, environment, checkout revision, requested revision, run ID, attempt and SHA-256 hashes. The joins require exactly six expected lane directories, exact provenance and file coverage, successful exits, no skipped/failed JUnit cases, and equality between executed and collected identities. Per environment, the two nonempty collections must be disjoint and their union must equal the complete collection. A matrix aggregate without this evidence is insufficient.

The existing ordinary JUnit artifact was 9,292,705 bytes because regression parameter identities can be very large. Scaffold text/XML inputs have a fixed 16 MiB per-file bound; receipts have a 16 KiB bound. Existing storage callers retain the shared XML parser's default 4 MiB bound. Parameter names are preserved in full. No test identity is shortened to fit evidence limits.

## Verification and remaining evidence

`test_scaffold_workflow_arguments_partition_actual_default_collection` collects the actual workflow arguments in the current database environment and proves the partition. Receipt checker regressions cover missing/extra evidence, stale runs, changed hashes, failed exits, skipped JUnit cases, mismatched execution and overlapping selections. `test_scaffold_join_refuses_failed_cancelled_skipped_or_missing_evidence` executes the actual join shell against unsuccessful or absent prerequisites. Existing measurement-routing and CEL dependency guards remain active.

The first scoped local check passed 324 tests covering these guards, measurement routing, IR/CEL routing and the no-suppression floor. Strict Python typing, format, lint and actionlint passed for that dirty checkpoint. These checks do not demonstrate final committed CI capacity. Final evidence must cover both platforms and every mandatory job, include collection/upload/download/setup and downstream joins/CEL, and show less than 600 seconds from the earliest job start to the latest completion. Queue/start timestamps must remain visible so scheduling delay is not concealed by individual job durations.

Correctness, security, performance and condensation planning assessments cover this bounded scheduling refinement. Implementation and final-revision evidence require affected independent reassessment. Exact workflow and human-facing wording approval remains pending.
