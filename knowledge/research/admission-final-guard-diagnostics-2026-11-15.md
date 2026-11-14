# Admission preparation final-guard diagnostics, 2026-11-15

This record supports the owner-approved [bounded diagnostic plan](https://github.com/realkarych/kora/issues/1562#issuecomment-6885168760) for recurring admission-preparation refusals. It changes test observation only. Production preparation, the ten-second test deadline, required checks, native cleanup, CI configuration and persisted/public contracts remain unchanged.

## Evidence motivating the diagnostic

The previous [diagnostic delivery](https://github.com/realkarych/kora/pull/1565) passed its complete final-revision CI. Mandatory [postmerge CI 43643850783](https://github.com/realkarych/kora/actions/runs/43643850783), attempt 1 at `72f49f0ed41e9ffc97fe6f8eb45e5d7127f148ca`, subsequently failed the SQLite/distinct preparation case on Linux Python 3.11. Its note records seven entered checks, 11.96487 elapsed seconds and zero owned units. Entry does not establish completion, the causal exception or native closure. The historical failure remains a failure; the earlier passing run does not replace it.

A [finite observer comparison](https://github.com/realkarych/kora/issues/1562#issuecomment-6885146448) did not select a repair. On macOS arm64/Python 3.11.17, the current profile observer took 2.245739792 seconds and a call-only trace candidate took 2.249172583 seconds on the same SQLite fixture. Both preparations returned with the expected seven entries, one checkout, zero outstanding checkouts/units and restored hooks. The trace candidate missed the declared directional selection criterion. The small shared-host difference is not a meaningful regression or historical-cause result.

The comparison then failed at an invalid whole-snapshot equality assertion: nested protected representations use identity equality, so independently acquired objects cannot be compared that way for value equivalence. PostgreSQL samples, complete value comparison and final lifetime assertions were not reached. Database shutdown ran in `finally`, but that does not replace the skipped closure assertions. The script was not repaired and repeated for another timing result; the candidate was not applied. Script SHA-256: `78fd010914fc79f1dac236627b9548b391445607538aa86ade78cfb65398012c`; two-row output SHA-256: `bd7123747a2cf6ccdd1db74f380c8664f8015ad806d49ed67de14019ad6e6006`.

## Meaning of the new observations

The existing test profiler still checks the same five function identities, asserting zero owned database units at every selected entry. The distinct-version case still enters seven checks. Two additional Boolean fields are appended to the same refusal exception only after restoring the prior profiler:

| Field | Exact observation | Limit |
| --- | --- | --- |
| `final_guard_entered` | `require_snapshot_time` entered directly from `prepare_admission_snapshot` | Acquisition's guard calls do not match. Entry follows complete synchronous preparation and its cancellation checkpoint, but does not prove the guard returned. |
| `final_deadline_branch_entered` | `DatabaseSnapshotRejected.__init__` entered from that guard, with `prepare_admission_snapshot` as the next caller | The inspected guard reaches this constructor only in its expired-deadline branch. Entry does not prove completed exception construction or attribute an earlier run. |

Non-call events return before additional work. Caller inspection occurs only after matching one of the two exact code identities and follows at most two caller links. The callback retains only booleans; it copies no frame, argument, local variable, exception content, source payload, deadline value or dynamic identity. It performs no new clock, database or provider read. The original elapsed/count/name/accounting fields remain unchanged. Fixed labels, two single-digit Boolean values and the existing bounded fields keep the note below 1 KiB.

The profile callback receives call and return events through CPython's documented [profiling API](https://docs.python.org/3.11/library/sys.html#sys.setprofile). Added identity comparisons have unmeasured cost and may affect deadline incidence. A newly observed refusal describes execution under this augmented observer; it cannot establish that an older run took the same branch. A passing CI with no refusal leaves that diagnostic question unobserved. No performance guarantee or new numerical gate follows.

TM-06/TM-07 govern content exclusion, TM-10 governs bounded observation and actual cleanup, and TM-12 governs truthful verification provenance. Existing threat-model entries cover this test note; no threat-model amendment is needed. The production Boolean diagnostic remains unchanged and grants no execution authority.

## Regression ownership

`test_admission_observer_identifies_final_deadline_branch` exercises real preparation on both metadata databases for success, short-work-allowance refusal, acquisition deadline, final handoff deadline and cancellation. The acquisition refusal proves that another call to the shared deadline guard cannot set the final-owner flags. The existing `WriterCutoffLoop` advances only for deterministic deadline cases; the ten-second operation allowance is retained.

For final handoff and cancellation, a separately owned trace hook observes the real prepared-result constructor's return. It returns a local tracer only for that constructor, disables line tracing there and declines tracing for all other frames. It does not replace the profile observer or production behavior. The original lifecycle profiler/tests are unchanged. Success and early-refusal cases need no additional trace hook.

The regression preserves the exact exception object across observer annotation, validates note fields and size, checks hook restoration, unchanged metadata and the expected check sequence. Its database ledger attaches before the first connection opens and checks one checkout where applicable, zero outstanding checkouts/units and native opened-equals-closed counts after completed shutdown. Cancellation retains its original exception and receives no preparation-refusal note. These resource assertions remain separate from diagnostic flags.

Existing preparation, allowance, malformed-source, deadline, cancellation, SQLite encoding, observability and no-suppression tests remain required. Independent actual-result reviews, commands, tested revisions and the single ordinary final-head CI are recorded in the implementing PR. This diagnostic neither completes common admission #1446 nor supplies accepted-output producers or physical settlement #1472.

The latest inspected master mutation audit, [43584820056](https://github.com/realkarych/kora/actions/runs/43584820056), attempt 1 at `d623a105c17ac682e8017a469fc8c6535990ec28`, is a budget-limited partial pricing/CEL report with restored continuation. Its report SHA-256 is `a16a15546e915a1196b860912f60d1414dca7694179edf884af27d441d80b392`; it contains no applicable admission finding and adds no PR mutation campaign.
