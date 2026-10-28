# Protocol helper refactor qualification scope

Checked and owner-approved on 2026-10-28.

## Delivered change

[PR #340](https://github.com/realkarych/kora/pull/340), closing
[issue #393](https://github.com/realkarych/kora/issues/393), consolidates protocol,
JSON, UTF-8, refusal and CEL-bound helpers. Validation results, error behavior,
wire bytes and storage digests remain unchanged. It does not change the storage
provider implementation or its measured workloads.

## Approved acceptance scope

The owner approved the exact
[acceptance amendment](https://github.com/realkarych/kora/issues/393#issuecomment-6472140342),
with [approval recorded here](https://github.com/realkarych/kora/issues/393#issuecomment-6472193800).
Only the additional requirement for a successful fresh storage qualification
before merging this refactor is removed. This is a narrow PR acceptance exception,
not a general rule for subsequent storage changes.

All ordinary final-head checks, supported operating systems and databases, strict
static checks, independent source review and behavioral-equivalence obligations
remain mandatory. The complete actual diff must still match those conclusions.

The accepted [storage qualification schedule](storage-qualification-schedule-2026-10-26.md)
is unchanged. A release containing local storage still requires successful
qualification for its exact release inputs. Failed or missing evidence refuses
release. No workload, population, assertion, sampling rule, 480-second child
deadline, job timeout or protected status is removed, relaxed or reclassified.

## Retained failed qualification

[Run 40688568316](https://github.com/realkarych/kora/actions/runs/40688568316)
tested source `2280336441810667d623af6c88aab92d73438a65`. Its macOS cohort 0
exceeded the 480-second child deadline and was killed. The complete qualification
union remains failed. Cohorts 1 and 2 completed successfully.

All three macOS cohorts report CPython 3.11.17, APFS, GitHub-hosted load context,
clean checkouts and source digest
`b7ddb9526e1f2ae0d31966e8fa90d0cc817982c8e35279d339ff92a43e063783`.
They have distinct boot and native process identities. Independent inspection
recomputed this digest from all 77 declared source inputs.

| Cohort | Complete checkpoint rows | Completed gross cycles | Complete duration | Exit |
| --- | ---: | ---: | ---: | --- |
| 0 | 829 of 832 | 403.980 s | unavailable | killed; deadline exceeded |
| 1 | 832 of 832 | 382.097 s | 434.621 s | 0 |
| 2 | 832 of 832 | 220.185 s | 249.361 s | 0 |

Cohort 0 completed all eight small workloads and fifteen large measured cycles.
It reached the final 256 MiB alias warmup but completed none of that workload's
three measured cycles. The checkpoint header declares 832 expected rows; the raw
file contains 829 complete rows without a partial trailing row.

The final completed resource checkpoints show zero disposable provider resources.
They do not establish closure of the interrupted cycle or final shutdown. The
retained data does not locate the interrupted frame or exclude a stall inside
the first final alias cycle, including its cleanup.

## Source review and limits

The production patch at `2280336` has the same stable patch identity as reviewed
`e039441622b2f0465278472a94f4d13bc267f5c1`:
`49297fd2ccac6c8a6169b2d724741d628b61b102`. Relevant source, contract and dependency
inputs were compared before carrying the independent correctness, security and
condensation conclusions. This does not relabel an older executable result as a
new-head measurement.

Independent performance inspection found no demonstrated refactor-caused
regression or dominant avoidable cost. Shared canonical encoding adds sorting in
a bounded 16 KiB metadata-size precheck; retained evidence does not establish a
material timing contribution. The timeout's cause and a sufficient fix remain
unverified. No favorable rerun was used to replace the failed study.

There is no successful fresh storage-lifetime qualification for this source
revision. Merging the refactor establishes no new lifetime, latency or leak-freedom
guarantee. [Issue #396](https://github.com/realkarych/kora/issues/396) retains the
timeout and required completion outcome. It must be resolved, and qualification
of the exact release inputs must pass, before a release containing local storage.
The separate macOS `hdiutil detach` exit-16 attribution work remains with
[issue #302](https://github.com/realkarych/kora/issues/302).
