# Storage resolution work, 2026-10-29

## Failure and scope

[Issue #396](https://github.com/realkarych/kora/issues/396) tracks the macOS
storage lifetime timeout. Run
[40874097622](https://github.com/realkarych/kora/actions/runs/40874097622), at
`e8adaaeac1169c1d0e5c42d3378a18838f7e64e4`, failed its 480-second child deadline.
The failed cohort completed 831 of 832 rows. Its completed gross work was
416.453863 seconds, including 292.159386 seconds for 800 small cycles. The
interrupted frame and final cleanup are unavailable. Other macOS cohorts
completed all 832 rows in 369.183193 and 345.972582 seconds. These failures remain
failures; host variation alone does not identify a source defect.

The repair starts from `c5b21a320125cb7a08444580827ba06c2d99f77b`. It preserves the
manual release schedule, six complete lifetime cohorts, both platform
qualification producers, evidence union, workload, observers, assertions,
480-second child deadline and ten-minute job deadlines. It changes no public
contract, persisted format, dependency or diagnostic schema.

## Deterministic duplicate work

`StagedPayloads.resolve` decoded all stage receipts to locate a sealed result,
then `segments` decoded the same rows again. Resolving prepared content decoded
them three times when its sealed source belonged to the same stage. All these
reads held the same root gate and SQLite transaction, without intervening writes
or callbacks.

Each outer resolution now creates a local map from the complete `StageRef` to
the unchanged, fully decoded observation tuple. Recursive resolution and segment
binding reuse that tuple within the synchronous call. A prepared reference has
one sealed source, so the map holds at most two stage views and is discarded on
return. Separate source stages remain separate keys. Seal admission independently
loads fresh observations before registering its receipt.

Public reading still locates content and revalidates it after acquiring ownership
in separate transactions. Each invokes the fresh outer resolver. All receipts,
including unrelated rows, retain complete decoding; all stage, manifest, source,
alias and private-file identity checks remain. Admission, payload I/O, native
flushes, crash recovery and owned-resource closure are unchanged.

## Focused evidence and limitations

The integration regression profiles the real synchronous resolver with the
standard-library `cProfile`, counting its unchanged receipt-loader code object.
On the base source, six cases failed: sealed resolution decoded two views;
alias and distinct-output preparation decoded three. The initial corruption
regression passed. On the changed source, each normal case decodes one complete
view and public reads preserve the expected byte count and digest. One-part and
multipart sources are covered. Corruption between independent calls is tested
both in a used part receipt and an otherwise unused begin receipt; both must
refuse with `storage_integrity`.

An external all-thread profile ran one existing 64 KiB raw lifetime cycle before
and after the change, with real storage and existing tracing. Full cycle cleanup
left zero stage, deletion, receipt and content rows, reserved bytes, owned/native
jobs, readers and connections in both runs. Stage-observation JSON loads fell
from 169 to 101, and storage finite-JSON validator calls from 2,167 to 1,719.
The full decoders themselves were unchanged.

These profiles instrument Python calls, add overhead and contain overlapping
inclusive durations. Elapsed time was 0.869650 seconds before and 1.154039 seconds
after; the latter ran while focused tests were active on the shared local host.
They establish work-count reduction, not a timing improvement, deadline
prediction, memory bound or hosted qualification. No unchanged-source profile
was repeated for a favorable time. Earlier bounded profiles found duplicate
settlement flushes below one millisecond per cycle and did not support removing
them as a timeout repair; all durability calls remain.

## Acceptance

The issue plan is
[comment 6499733548](https://github.com/realkarych/kora/issues/396#issuecomment-6499733548).
The owning regressions are
`tests/plugins/storage/test_stage_resolution.py`; focused checks cover
`tests/plugins/storage`, `tests/protocols/test_storage_models.py` and
`tests/architecture`, alongside mandatory Ruff, format and strict Pyright.
Independent correctness, security and performance review cover the receipt
provider, resolver, seal admission and ownership-validation boundaries under
the storage protocol and existing threat-model entries.

One complete changed-source manual storage workflow must establish actual
deadline sufficiency, with all six cohorts, both qualification producers and
the required union passing. Ordinary final-head CI must also pass on Linux and
macOS. Exact commands, revision identity, results, artifacts and independent
review conclusions belong in the pull request. A failed qualification remains
failed; after two unsuccessful repairs, request an owner decision. This record
does not claim a future release is qualified.
