# Control-plane load assumptions

Estimated on 2026-10-16; arithmetic and evidence limits corrected on 2026-10-18 under issue #95. Nothing was measured. These are planning inputs, not capacity claims. [ADR-0013](../../docs/decisions/0013-implementation-stack.md) owns the stack choice; [NFRs](../../docs/requirements/non-functional.md) and the [roadmap](../../docs/roadmap.md) own validation targets.

## Assumed workload

- 500 concurrent sessions at peak, 150 on average.
- A session lasts 20 minutes and emits 300 normalized events: 0.25 events/s, assumed 2,000 bytes/event.
- Example upload interval 5 seconds and lease-renewal interval 15 seconds. These are estimate inputs, not new protocol defaults.
- An independent volume scenario has 50,000 runs/month and 20 node instances/run. It implies roughly 0.386 node instances/s over 30 days; not every node is an agent session. It must be reconciled with the concurrency/duration assumptions in a real pilot.

## Conditional arithmetic

| Quantity | Derived value |
|---|---|
| Peak renewals | 500 / 15 ≈ 33 requests/s |
| Peak segment uploads | 500 / 5 = 100 requests/s |
| Event rate | 125 events/s at peak; 37.5 at average concurrency |
| Uncompressed event bytes/day | 21.6 GB at sustained peak; 6.48 GB at assumed average |
| Thirty days of those bytes | 648 GB at sustained peak; 194.4 GB at assumed average |
| JSON CPU if parsing takes 10–50 microseconds/event | 0.125–0.625% of one core at 125 events/s; 0.6–3% at a hypothetical 600-event/s burst |
| Runner memory if each process takes 50–80 MB | 25–40 GB for 500 processes |

Decimal units are used. Parsing time and memory per process are unmeasured assumptions. Storage excludes artifact payloads, indexes, replication and overhead, and does not account for compression. Transcripts may overlap event content; do not count the same bytes twice. Claims, transitions, reads, SSE connections, policy checks and external-observation work require separate measurements.

## Conclusions and measurements

The former FastAPI/PostgreSQL capacities, headroom ratios and “CPU cannot be the constraint” conclusion had no benchmark evidence and are withdrawn. These calculations neither prove the scale target nor justify a language rewrite. Local-disk suitability depends on capacity, retention and operations; the calculated volume does not prove that it cannot fit. S3-compatible storage remains an accepted MVP deployment choice under FR-4.4.

Measure real request mixes, DB contention, scheduling delay, event-loop latency, segment serialization, storage growth and runner/harness memory. Include both metadata databases, the large-run projection and notification-disabled scheduling case required by the NFRs. Reconsider a component only after identifying a measured bottleneck. The [original estimate](https://github.com/realkarych/kora/blob/a29bfa6a78adac97584a048a4ef4402969e8a100/knowledge/analysis/control-plane-load-estimate.md) preserves the assumptions and withdrawn conclusions for audit.
