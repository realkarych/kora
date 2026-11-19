# Runner sink drain and memory baseline, 2026-11-19

Checked on 2026-11-19 for [issue #1684](https://github.com/realkarych/kora/issues/1684), part of [issue #199](https://github.com/realkarych/kora/issues/199). The numbers are diagnostics from one hosted Linux runner. They are not a portable benchmark, set no numerical gate and are not guarantees. The blocking checks of the measurement are its contract gates, listed below.

## Source

- Measurement: `tests/measurements/test_runner_sink_cost.py`, run by the `provider-protocol-measurements` job of the `Daily master lanes` workflow.
- Run: [44527560008](https://github.com/realkarych/kora/actions/runs/44527560008), on the pull request that added the measurement, at revision `2406834b73753528573b9f1bcde12502f6110c77`. Artifact `runner-sink-measurement`. Ubuntu 24.04 hosted runner, Python 3.11.17, report started 2026-11-19T12:53:51Z.
- Step time: 67 s (12:53:50 to 12:54:57), against the 2-minute target. The whole job took 4 min 56 s, inside its 10-minute deadline.
- All nine gates were true: complete samples, batch bound, queue record bound, queue byte bound, open-call bound, no omitted record in the paced workloads, saturation accounting, threads released and descriptors released.

## Method

The workloads follow the [plan](https://github.com/realkarych/kora/issues/1684#issuecomment-7008506613) and its [pacing](https://github.com/realkarych/kora/issues/1684#issuecomment-7008828924) and [sizing](https://github.com/realkarych/kora/issues/1684#issuecomment-7009296954) updates. Each sample drives the real `RunnerSink` and a fresh real `RunningJournal` in a private temporary directory, on the system clock, serially in one process, with no warm-up.

- **Paced workloads.** Events go out in bursts of 1,000. The next burst starts once the sink's queue is empty.
  - `drain`: 100,000 mixed `turn` and `tool_result` events with text of 256 to 4,096 bytes, 3 repeats.
  - `slow_sync_5ms`: 20,000 events, 3 repeats.
  - `slow_sync_50ms`: 10,000 events, 3 repeats.
  - `memory`: 101,000 `tool_call` events, of which the sink tracks the 100,000-call ceiling, then 101,000 closing `tool_result` events. One repeat, under `tracemalloc`.
  - In the two slow-sync workloads every journal sync waits the named delay, adapter batches included. A second thread appends a supervisor record roughly every 10 ms and serializes with the sink's batches.
- **Unpaced workload.** `saturation`: 100,000 `turn` events of 256 bytes, 3 repeats. With events this small the queue's 4,096-record bound binds before its 4 MiB byte bound.
- **Latency.** `durable_latency` runs from the record's queue stamp to the return of its durable batch append, on `time.monotonic_ns`. It covers admitted durable records only and excludes validation before the queue. `emit` is the time of one `emit` call, on `time.perf_counter_ns`.

## Results

Medians of the three repeats, with the range in brackets; `memory` has one sample.

| Workload | Sample time | Events/s | Emit p50 / p99 | Durable latency p50 / p99 / max | Batches | Omitted |
|---|---|---|---|---|---|---|
| `drain` | 3.38 s [3.32–3.38] | 29,627 [29,589–30,106] | 20.6 µs / 43.3 µs | 17.9 ms / 40.1 ms / 73.8 ms | 391 | 0 |
| `slow_sync_5ms` | 1.28 s [1.25–1.30] | 15,622 [15,402–15,957] | 20.5 µs / 43.5 µs | 38.0 ms / 72.7 ms / 74.5 ms | 79 | 0 |
| `slow_sync_50ms` | 4.16 s [4.12–4.19] | 2,403 [2,387–2,430] | 20.4 µs / 39.5 µs | 302.9 ms / 498.5 ms / 500.0 ms | 40 | 0 |
| `memory` | 27.85 s | 7,254 | 86.0 µs / 187.5 µs | 50.7 ms / 83.8 ms / 107.1 ms | 790 | 0 |
| `saturation` | 2.95 s [2.91–2.98] | 33,871 [33,521–34,412] | 17.4 µs / 130.2 µs | 550.3 ms / 719.2 ms / 724.1 ms | 95–103 | 73,709–75,754 |

Notes on the results:

- **Supervisor writer.** It achieved 78–80 appends per `slow_sync_5ms` sample, about 62 per second, and 39–40 per `slow_sync_50ms` sample, about 10 per second. It does not reach 100 per second because each append waits for its own delayed sync behind the sink's batch.
- **Queue peaks.** Paced bursts peaked at 984–1,000 records and about 1.7 MB. `memory` peaked at 534 records. `saturation` peaked at exactly 4,096 records and 1.6 MB.
- **Saturation accounting.** Omitted plus journalled adapter records equalled 100,000 in every `saturation` sample, for example 73,713 + 26,287.
- **Memory workload.**
  - Peak open calls: 100,000.
  - Traced Python memory with all calls open: 11.52 MB settled and 12.14 MB peak. After close: 7.64 MB settled.
  - The observer's timing arrays account for 3.23 MB of the after-close figure. Most of the rest is the open-call set's hash table, which keeps its capacity while the sink object lives.
  - Net of the arrays (about 1.6 MB while all calls were open), the open-call tracking costs about 100 bytes per call.
  - RSS rose from 114.5 MB before the sample to 131.6 MB with all calls open and 133.5 MB after close. RSS is process-wide and includes the interpreter, `tracemalloc` and the earlier samples.
- **Journal size.** A `drain` sample wrote 172.9 MB, about 1.73 kB per event.

## Reading rules

- **Latency depends on the burst.** The paced latencies describe bursts of 1,000 events on an empty queue: a record waits for the rest of its burst and for the batches ahead of it. An isolated event instead waits up to the sink's 50 ms batch window plus one sync. These figures are therefore not the latency of sparse adapter traffic.
- **Slow sync delays every journal sync.** The slow-sync figures show a slow journal device with a serialized supervisor writer. They do not isolate the supervisor's share.
- **Memory timings are not comparable.** `memory` timings include `tracemalloc` overhead and must not be compared with the other workloads or read as sink cost.
- **Saturation is bound by the GIL.** The emit loop and the writer thread share one process. Its omitted count, throughput and latency describe that workload, not the sink's storage capacity.
- **Low repeat counts.** `slow_sync_5ms` and `slow_sync_50ms` percentiles come from 20 and 10 bursts per sample, so their p99 is the latency of a burst's last records, not a tail estimate. `memory` has no repeat-to-repeat variance; the daily runs supply repetition over time. The harness summary's p95 is not meaningful at three or one repeat.
- **Byte bound not pressed.** No workload presses the queue's 4 MiB byte bound: paced bursts peak at about 1.7 MB and `saturation` at about 1.6 MB. The gate checks the bound but does not exercise its limit.
- **macOS timings are noisy.** Local macOS runs of the same code varied about twofold on a loaded host. Compare daily Linux runs with each other, not with local runs.

## Scope

The measurement covers the runner sink and journal. It does not cover the harness adapter process (the Claude session lifetime measurement does), transfer or `finalize` peaks from the implementation-plan row on runner memory, the `max_events` ceiling path, the local runtime resource-lifetime row or any held-time leg pending fake runtime 7b.
