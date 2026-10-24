# Pricing foundation implementation evidence

Evidence collected on 2026-10-23 and 2026-10-24 for [issue #213](https://github.com/realkarych/kora/issues/213) and [PR #232](https://github.com/realkarych/kora/pull/232). This record distinguishes implemented component results from final revision-specific CI qualification, which remains in the PR verification record.

## Implemented boundaries

The foundation provides immutable price, usage, coverage and approval values, exact cost arithmetic, forward migration `0002_pricing_foundation` and scoped typed metadata reads. Canonical money is a nonnegative decimal string with at most fourteen integer digits and ten fractional digits; signs, exponents, redundant leading zeros and trailing fractional zeros are refused. Arithmetic uses an explicit Decimal context and one final `ROUND_HALF_EVEN` rounding. Historical missing values remain distinct from observed zero.

`tests/ir/test_cost_models.py` and `tests/ir/test_usage_pricing.py` cover strict values, nested ownership, bounded raw JSON, content-free errors, source times, coverage, arithmetic and exact serialization. `tests/db/test_cost_reads.py` and `tests/db/test_pricing_migration.py` exercise the real SQLite and PostgreSQL boundaries, predecessor history, scoped queries, decimal round trips, interruption and migration recovery. Shared-model validation does not establish authorization or transactional accounting authority.

Engine installation, attempt pinning and aggregation; runner observation and replay; adapter normalization; approval transactions; public-surface authorization and parity; and budget enforcement remain with their named implementing issues and work items. See [the accounting contract](../specs/data-model.md#7-usage-records-and-completeness-fr-91), [protocol ownership](../specs/protocols.md) and [ADR-0010](../../docs/decisions/0010-cost-accounting-and-limits.md).

## Hosted pricing baseline and fixed monitor

The complete pricing component in [run 40010387162](https://github.com/realkarych/kora/actions/runs/40010387162), job `120608946539`, ran all 63 cases. Its authoritative `pricing_workload_total` was **226.883101796 seconds**. The selected artifact contained 75 files totaling 20,995,822 bytes, 12 normal children, 36 fragments, 24 streams, 13,380 parent samples and 18 true parent resource gates. The full pytest selection took 316.96 seconds and its waited command took 318.151 seconds. These are different clocks.

The workload clock excludes only the common PostgreSQL fixture/readiness/current-template interval of 3.266011280 seconds and parent JSON encoding/write interval of 8.253329974 seconds. Pricing-specific setup and migrations, child work and collection, factory release, parent payload release and memory-observer close remain included. Final manifest publication is outside that clock and is recorded separately. Earlier parent snapshots are not authoritative workload totals.

Independent assessment matched all 13 baseline manifest inputs against `f54bce2d7e4f6776714d23089110c8df74576484`. At `946cce7826a26e0dc89ddeece18116c45a6c9cb3`, the only changed manifest input was the workflow, whose intervening diff changed IR shard selectors. Numerical pricing source, imports, dependencies and resource owners remained matched. This component baseline does not qualify the failed whole pipeline or prove the later schedule's capacity.

The [accepted monitor rationale](https://github.com/realkarych/kora/issues/213#issuecomment-6377144662) fixes a strict workload limit of **256.0 seconds**. The 29.116898204-second margin is approximately 12.83% of the retained observation: a prospective tolerance within the approved allocation, not measured variance or a guarantee. The assertion follows `PricingEvidence.finish()` and non-null confirmation, so cleanup and evidence finalization precede a timing failure. The outer command propagates pytest failure and marks final command completion false. All 63 cases, 18 parent resource gates, 60-second child and cleanup bounds, and byte limits remain unchanged.

The independent producer changes placement and preconditioning. Complete final-route CI must revalidate timing, publication, release and whole-pipeline capacity. Moving the intact command does not require another ungated calibration, and an over-budget result remains a failure.

## Complete local IR comparison

Accepted source `ca6505e37eb0d7aa1536f5f5419f4c4365a60006` and pricing source `946cce7826a26e0dc89ddeece18116c45a6c9cb3` each completed every declared shard exactly once. Their own source-derived evidence readers accepted the complete unions:

| Source | Collected nodes | True gates | Samples |
|---|---:|---:|---:|
| Accepted | 330 | 446 | 7,791 |
| Pricing | 352 | 1,211 | 10,536 |

All 7,791 inherited sample identities and multiplicities remain, with 2,745 additional samples. The comparison recomputed medians, p95, maxima and ordered tails for 478 shared workload groups and 58 additional groups. All 48 accounting cases and 126 normal accounting child outcomes were retained: 27 timing, 93 traced and six native children. Resource gates and actual process release were reconciled independently. The literals collect 328/1/1 accepted nodes and 328/11/13 pricing nodes; the complete partitions are disjoint and preserve order and the full union.

The shared `vcs_decode_65536` workload has an adverse observation:

| Source | Median, microseconds | p95, microseconds | Maximum, microseconds |
|---|---:|---:|---:|
| Accepted | 894.187 | 952.500 | 974.833 |
| Pricing | 3,284.250 | 3,337.792 | 3,339.125 |

The median ratio is approximately 3.673 and the p95 ratio 3.504. Decoder and fixture source are unchanged, so causal attribution is not established. `vcs_dense_invalid_types_object` also has a p95 ratio of approximately 2.079 while its median ratio is approximately 0.986. Ordered samples remain retained; neither a blanket absence-of-regression claim nor a favorable rerun follows from these results.

The resource comparison includes all three configuration lifetime payload pairs and all six cold-import payload pairs. The inner cold-import clock wraps `import kora.ir` after path preparation and garbage collection. Parent archive and copy work is outside that clock. Traced live and peak allocations, tracer overhead, native-inclusive current RSS, process high-water memory, descriptors and threads retain their distinct meanings.

Accounting retained-heap growth stayed within the existing 262,144-byte monitor; observed maxima included 28,818 bytes for ordinary cases and 1,352 bytes for large cases. Native growth stayed within the existing 8 MiB monitor, with a retained maximum of 4,734,976 bytes. Late native increases do not establish a permanent allocator plateau. Configuration RSS observed with tracemalloc active is not an independent untraced lifetime result. Configuration and import child PIDs not retained by their producers remain unavailable.

Whole evidence-reader invocations under `/usr/bin/time -l` reported:

| Source | Real seconds | Maximum native RSS, bytes | Complete input files/bytes |
|---|---:|---:|---:|
| Accepted | 1.41 | 340,066,304 | 9 / 1,907,813 |
| Pricing | 1.65 | 374,341,632 | 9 / 4,815,539 |

These include imports, source recipes, parsing, validation and artifact handling. They are not parser-only allocation measurements or equal-work latency ratios. The head source copy also contained four generated frontend files totaling 223,912 bytes, pinned alongside 91 tracked source files; accepted and historical copies contained 87 and 82 tracked files. Unequal outer copying is disclosed rather than normalized away.

The temporary accepted checkout disappeared before the evidence readers launched. The failed preflight was retained. The [recorded environment repair](https://github.com/realkarych/kora/issues/213#issuecomment-6377011395) reconstructed a **new physical** full-history checkout at the declared path, with the exact accepted commit/tree, historical `41ea853883201665adaeb7d730da9d15be148456` source and all 59 selected source hashes. It did not reconstruct reports, relabel the historical fixture or repeat successful measurements. Runtime, native dependency, real stripped-child origin, archive/copy and owned release evidence remains attached to its actual invocation.

## Original metadata comparison

| Source | Authoritative workload seconds | Workload/count entries | True gates |
|---|---:|---:|---:|
| Accepted `ca6505` | 47.502122083 | 29 | 94 |
| Pricing `946cce` | 52.826965459 | 29 | 94 |

Both complete original workloads selected SQLite and PostgreSQL and passed the unchanged 120-second gate once. Owners were waited and reaped with no recorded survivors. Selected same-interval container create/start/destroy events and final absence corroborate fixture release; they do not independently prove PID-to-container-session attribution. Actual per-container resource overrides and unavailable native RSS are not invented.

The first admission refused an absent pinned image before numerical work. Exact-pin image setup later succeeded. A between-observer self-completion error remained a failed attempt; the accepted endpoint's successful final observation preserved its result, and the unused pricing endpoint ran in a separate bounded continuation. No full-pair retry, favorable endpoint replacement or hard-end extension occurred.

The [owner-approved concurrent-local method](https://github.com/realkarych/kora/issues/213#issuecomment-6376761399) superseded exclusive-host reservations for the remaining local work. Possible peer load, differing migration schemas and different ending paths prevent an uncontended or equal-work whole-command timing interpretation. Higher upgrade times and PostgreSQL traced peaks remain in the retained comparison. Earlier completed IR endpoints were retained once under their actual execution conditions.

## Capacity failures, local checks and final acceptance

[Run 40010387162](https://github.com/realkarych/kora/actions/runs/40010387162) failed its full campaign despite the complete pricing component result: its main IR lane timed out and the observed campaign span was 645 seconds. [Run 40018290964](https://github.com/realkarych/kora/actions/runs/40018290964) spanned 624 seconds and both Linux scaffold lanes reached ten minutes. Successful component results did not qualify either campaign; interrupted workloads and unexecuted later steps remain incomplete.

The owner approved [three independent Linux producers and a mandatory final join](https://github.com/realkarych/kora/issues/213#issuecomment-6374424055). Original resource measurements retain their own post-measurement locked synchronization. Pricing retains its original Python 3.15 environment and isolated Python 3.11 measurement lifecycle. CEL artifacts have separate exact-membership and source-receipt admission. Producer success requires every owned required outcome, release and final synchronization; the join requires dependency results and explicit proofs. Platform checks, complete workloads and the 600-second job and whole-pipeline budget remain mandatory.

A local ordinary run retained 2,961 passing tests, 64 errors and one failed PostgreSQL test caused by Docker connection refusal, plus a shutdown socket warning. Subsequent CLI and installed-SDK checks observed the same healthy daemon/socket without restart. That failed run remains a failure. After the concrete resource-job synchronization correction, tree `4a5898e4f593344b6ebfea2b0d1fff1b6e7260e0` passed repository formatting, Ruff with `--ignore-noqa`, strict Pyright, 144 focused routing tests and **3,032 ordinary SQLite/PostgreSQL tests in 195.99 seconds**; the existing default marker deselected 464 measurement tests. All supervised checks exited zero and reaped without recorded survivors. Commit `8c4960403f9afb1310fab292871cff647b9c23b5` preserves that tested tree.

These local checks and component measurements do not substitute for the final PR revision's complete routed CI, browser, packaging, security and platform results. [PR #232](https://github.com/realkarych/kora/pull/232) carries the revision-specific checks, artifacts and independent conclusions, including failures and pending obligations.

## Identifier admission correction after failed routed CI

[Run 40045522340](https://github.com/realkarych/kora/actions/runs/40045522340) tested `f273b8e9a2cea51d664f2793c54698cc4515ddfe` through synthetic merge `b526c283798258aeaa1cf1f98e8a455ce5453e47`, whose tree exactly matches the candidate. The campaign lasted 455 seconds from creation through its final update, including 47 seconds before the first job. Fourteen of sixteen jobs passed. Pricing passed all 63 cases with authoritative workload **235.282425405 seconds**, below 256, and complete publication, isolated-environment and private-owner release evidence. Original metadata retained 29 samples and 94 true gates with authoritative workload **86.515295402 seconds**, below 120.

IR shard 1 failed `cost_lexical_identifier_refused` in the third large timing child: p95 **2.084789 ms** exceeded the unchanged **1.953125 ms** limit, with three of ten samples over the limit. Twenty normal accounting children completed; seven later children were not launched after the failure. The mandatory final join correctly rejected the incomplete IR union. All raw reports, logs, cancelled superseded runs and artifact digests remain retained; neither elapsed capacity nor successful components qualify this failed campaign.

The workload supplies a one-million-character model string. Source inspection establishes that generic JSON preflight scanned that scalar before the existing 256-character typed identifier check. The particular timing excursion's CPU, garbage-collection or scheduling cause is unverified. The [bounded correction](https://github.com/realkarych/kora/issues/213#issuecomment-6377699849) adds four top-level, non-coercing lookups before preflight and refuses exact built-in strings over the shared identifier limit. Both general preflights, unknown-key checks and handler order remain intact. Opaque source JSON keeps its separate 8-KiB budget and may contain longer strings under the same key names.

The exact-length, content-free overlength and opaque-source tests exercise direct mappings, read-only mapping proxies and raw JSON without mutating input. All 48 accounting cases, 126 child outcomes, timing/resource gates, workloads and deadlines remain unchanged. The correction passes repository formatting, Ruff with `--ignore-noqa`, strict Pyright, 159 model/arithmetic tests and the complete ordinary SQLite/PostgreSQL suite: **3,059 passed, 464 measurement tests deselected, in 176.11 seconds**. All check owners exited zero and were reaped without recorded survivors. No local numerical workload was repeated. Complete CI at the corrected revision remains required; the failed candidate is not relabeled as passing.
