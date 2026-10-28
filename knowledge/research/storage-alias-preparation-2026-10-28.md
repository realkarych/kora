# Verified alias preparation without duplicate payload writes

Checked 2026-10-28 under [issue #396](https://github.com/realkarych/kora/issues/396). This removes unnecessary preparation work; it does not establish the cause of the historical timeout or a successful release qualification. The [storage schedule](storage-qualification-schedule-2026-10-26.md) and every existing release gate remain unchanged.

## Mechanism and preserved ownership

The [storage contract](../specs/protocols.md#72-operations-and-lifecycle) permits a prepared candidate to alias its sealed raw bytes when their complete identities agree. The previous preparation path wrote every supplied output chunk into a private file, synchronized it, persisted an alias to the raw parts, then discarded the preparation file.

When the declared expected output digest equals the sealed raw digest and the output cap equals the raw size, preparation now consumes and independently hashes/counts the complete supplied stream without writing those chunks. Type, chunk-bound, cap, expected-digest, exact-size and EOF checks precede settlement. Invalid input is refused; an empty file never becomes a successful ordinary prepared candidate. Every nonempty eligible chunk executes an explicit cooperative scheduling checkpoint, preserving cancellation/control progress even for immediate-return one-byte reads. Other preparation requests retain their writes, and settled replay still verifies supplied bytes.

Admission, the conservative capacity reservation, exclusive stage ownership, durable intent and recorded file identity remain. The empty preparation file is still finished and synchronized; the same alias receipt and activity spans are persisted before identity-checked discard. Reads resolve the saved raw segments, and publication builds its separate verified witness. The existing 2N peak payload envelope and configured concurrency do not increase. No public DTO, persisted format, ownership boundary, dependency, diagnostic content, sampler, assertion or deadline changes.

## Local evidence

The inspected base is `8d8b766e776e52371921bfdfde2e1955932dfe71`. Candidate preparation source SHA-256 is `50131da1299fc3f1cd961b7e6a60dc760c410ed5df93fa1606ce8d0dca72aebc`. These observations used a dirty implementation snapshot on shared local macOS with locked CPython 3.11.17; they are not final committed-revision CI evidence.

The existing interrupted-settlement regression first failed with `preparation_bytes=65536` against the new zero-byte assertion. After the change, publication and replay-cancellation selections passed 13 cases. A wider selection passed 74 cases covering preparation, replay/cancellation, cleanup/recovery, readers, crash recovery, capacity, integrity/identity, deadlines, shutdown, diagnostics and workload closure. Architecture/checkpoint/budget checks passed 87 cases; Ruff check/format and strict Pyright passed. Independent correctness review found a test cleanup gap around barrier-file inspection; the provider is now protected by `finally`, and that affected test passed again. Existing results retain their original snapshot; final CI supplies final-revision coverage.

New regressions inspect the still-owned preparation file at the existing persisted-effect barrier, require complete source consumption, and preserve interrupted recovery/promotion. Immediate-return input permits cancellation before EOF, leaves a pending receipt, then supports reconstruction, exact retry, reread, release and zero disposable counters. Changed, short and overlong input refuses before alias settlement, while valid retry/replay preserves the raw bytes. No mocks or timing sleeps synchronize these tests.

The [diagnostic observations](storage-alias-preparation-diagnostics-2026-10-28.json) retain every sample, source/lock hashes and limits. One existing alias cycle at each size ran serially before and after, with fresh private roots, event-loop cProfile and one-frame tracemalloc. The probe used the unchanged `storage_lifetime_workload.cycle`, real provider and existing source/verification/cleanup, outside the complete qualification producer.

| Size | Executor submissions before → after | Complete seconds before → after | Traced peak bytes before → after |
| --- | --- | --- | --- |
| 64 KiB | 103 → 102 | 0.6240 → 0.5519 | 516,157 → 526,360 |
| 16 MiB | 1,633 → 1,377 | 1.0486 → 0.8818 | 525,269 → 533,380 |

The submission reductions match the omitted preparation writes. All four cycles reported zero disposable provider counters. Increased traced peaks remain visible. These single shared-host observations establish no speedup, native/RSS, retention or deadline-sufficiency guarantee. cProfile covers the event-loop thread and cannot attribute payload-thread write CPU. No further local attribution run is required for this bounded mechanism.

## Release limits

[Run 40688568316](https://github.com/realkarych/kora/actions/runs/40688568316), source `2280336441810667d623af6c88aab92d73438a65`, remains failed: macOS cohort 0 completed 829 of 832 rows before its 480-second child deadline. Last completed zero counters do not establish interrupted-cycle or final cleanup. This is separate from the native detach failure in #302/#363.

Independent correctness, security and performance inspected the actual change and supporting evidence. The corrected test received bounded correctness reassessment; unchanged production conclusions carry with their inspected inputs. Those reviews establish the implemented optimization, not capacity sufficiency. Required final-head Linux/macOS CI and the complete exact-source storage qualification, all cohorts and evidence union, remain mandatory. Failed or partial qualification never closes #396, and no historical failure is relabeled passed.
