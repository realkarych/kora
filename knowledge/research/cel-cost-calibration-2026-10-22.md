# CEL cost regression and historical calibration evidence

The owner-approved [verification audit amendment](approved-verification-audit-2026-10-29.md#numerical-observations) makes the compact 100 ms sample comparisons diagnostic. The ten-second child deadline, 240-second suite watchdog, exact results, work/admission bounds and resource checks remain blocking. Historical results below keep their original classifications.

Preregistered on 2026-10-22 for [issue #167](https://github.com/realkarych/kora/issues/167), under the [original plan](https://github.com/realkarych/kora/issues/167#issuecomment-6346588560). On 2026-10-23 the project owner approved [lightweight validation with a 100 ms target](https://github.com/realkarych/kora/issues/167#issuecomment-6351849696). The current protocol replaces full coefficient fitting, RSS-envelope and GIL calibration, native crash-threshold searches and the five-job collection. None of those removed checks passed. The failed default-bound pilot and qualified smaller-bound selection below remain historical evidence, with their original revisions and artifacts.

## Current compact regression protocol

The steps budget is 100,000,000,000,000 logical charge units, ten times the previous provisional budget. All fourteen time weights remain unchanged. The 16,777,216-byte logical memory budget, `memory_base=0`, `memory_byte=1`, regex class tables, base and syntax coefficients of 1 and pattern cap of 1,048,576 remain provisional and unchanged. The [golden estimate and specification consistency checks](../../tests/cel/test_cel_cost_regression.py) pin those provisional outputs and budget values; they do not calibrate them. Admission retains 4,096 canonical bytes and 256 values across both original and converted referenced roots, 4,096 source code points, depth 32 and four comprehensions nested at most two deep.

`tests/measurements/cel_cost_smoke.py` declares exactly ten cases. Each runs in a fresh child through the real public `Evaluator.evaluate`, once with an empty program cache and then three times with a cache hit:

| Case | Fixed input and expression |
| --- | --- |
| `ascii` | 4,094 ASCII `a` characters, compared with themselves |
| `unicode` | 1,023 four-byte supplementary characters followed by `aa`, compared with themselves |
| `escapes` | 682 NUL characters followed by `aa`, compared with themselves |
| `nested` | 32 singleton-list levels around integer zero, compared with themselves |
| `linear` | 255 integer zeros; `data.all(x, x == 0)` |
| `quadratic` | 255 integer zeros; `data.map(x, x).filter(x, x == 0) == data` |
| `regex_success` | `^(?:a\|aa)*$` over 4,094 `a` characters |
| `regex_late_failure` | The same anchored pattern over 4,093 `a` characters followed by `b` |
| `regex_empty` | The empty pattern over 4,094 `a` characters |
| `large_rendering` | An exact 4,096-code-point source: a single-quoted string of 4,090 supplementary characters compared with the empty string |

Expression checking and evaluator startup precede timing. Each interval includes binding, queueing, cache-miss compilation when applicable, native execution and result delivery; result assertions follow it. Every one of the forty raw intervals records **100,000,000 ns (100 ms)** as a diagnostic reference with an explicit exceedance flag. This is an observed coarse regression target for these cases, not an interruption deadline or a calibrated bound for all admitted expressions. The logical memory budget is not a 16 MiB RSS envelope. No fitting, RSS/GIL calibration, forced GC, snapshots, artificial leaks or crash-threshold search is part of this protocol.

The reference environment is GitHub-hosted Ubuntu 24.04 x86_64, CPython 3.11.17, uv 0.13.1 and the locked common-expression-language 0.10.0 wheel. The serial suite has a 240-second limit; each child retains the ten-second deadline and 256 KiB per-stream cap. The existing supervisor owns pipes and reaping on completion, failure, timeout and overflow. Whole-CI feedback follows [AGENTS.md](../../AGENTS.md#development-verification); protective job/test/child/cleanup deadlines remain mandatory.

Run the current suite on an otherwise idle machine:

```sh
KORA_CEL_COST_OUTPUT=measurements/cel-cost-smoke.json \
  uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_cel_cost_regression.py
```

The existing dedicated `CEL evaluator lifetime` CI job runs the compact suite alongside the unchanged evaluator lifetime suite and retains its existing required status. Artifact `cel-cost-regression-measurement` contains `cel-cost-smoke.json` and `cel-checkout-revision.txt`, recording the checked-out revision. A qualifying CI report requires a clean checkout matching the measurement revision on the reference environment. Local macOS results are marked `non_reference`: they are functional observations and cannot qualify as reference evidence.

The schema-1 aggregate records the target, four calls per case, complete manifest, provenance, raw child streams and measurement samples/gates. Provenance includes checkout/tree, dirty state, interpreter, uv, binding version and installed native-image hashes, lock digest, machine/kernel and runner/run identity. Child reports bind the source, canonical rendering, input, variable types and expected result to the manifest; each sample records its phase, raw nanoseconds, before/after cache counts and exact Boolean result. Validation requires the completion marker, matching PID, restored stack setting and descriptors back at baseline. After handle close, handles, outstanding jobs/bytes, queue, running jobs, deliveries and diagnostic counters are zero; the one cached program and process-owned daemon remain until child exit. These counters do not replace the separate native-memory and resource-lifetime suite.

The [verification exclusion](../reviews/ai-development-setup.md#cel-numerical-calibration-exclusion) is scoped to removed numerical calibration. Existing static/refusal, binding conformance, lifetime, browser, logging, database, provider and scanner checks remain mandatory. Schema-driven checking/projections, native minimum-int negation correction, consumer diagnostic ownership, real API/scheduler integration, fairness and the 1,000-node/five-second scheduling requirements retain their implementation owners. The `cel` diagnostics activation row in [observability.md](../specs/observability.md#7-component-coverage-and-activation) is unchanged. Measurement artifacts supply no execution or administrative authority.

## Historical protocol status

The sections below describe the earlier protocols at their measured revisions. Their 10 ms target, 2.5 ms conversion allocation and later five-job plan do not define current commands or acceptance gates. Unfinished exhaustive calibration catalogues have been removed from the active tree; no full calibration passed.

## Historical conversion protocol

The reference is GitHub-hosted Ubuntu 24.04 x86_64 with CPython 3.11.17, uv 0.13.1 and the locked common-expression-language 0.10.0 wheel. The report records CPU, image, kernel, source and checkout/tree revisions, installed native-image hashes and the matching locked wheel archive digest. The latter is the hash enforced by the locked installer, rather than a new hash of a retained wheel ZIP. Local runs are explicitly non-reference and cannot pass this decision.

The historical pilot CLI and its `cel_calibration_mode=pilot` dispatch are available at source revision `69c890b8e9bdcaef084053d3736630b4175b308b`, with artifact `cel-conversion-pilot`. They are not active commands or CI modes in the current tree. No later full collection or crash calibration passed.

Each shape has scales 1, 2, 4 and 8. Every phase retains three warm-ups and fifteen measured calls. Original and converted values must pass the production bounds walker; each observation records their exact maximum depth, with the root at depth zero. Largest cases are:

| Family | Construction at scale 8 | Original / converted canonical bytes | Converted value count |
| --- | --- | --- | --- |
| ASCII | Four strings, 16,376 ASCII characters each | 65,517 / 65,517 | 5 |
| Unicode | Four strings, 4,088 four-byte characters each | 65,421 / 65,421 | 5 |
| Escapes | Four strings, 2,728 NUL characters each | 65,485 / 65,485 | 5 |
| Empty lists | 1,000 lists, each containing 21 empty lists | 65,001 / 65,001 | 22,001 |
| Small maps | 1,000 maps, each with six short keys and null values | 62,001 / 62,001 | 7,001 |
| Deep chains | 1,000 independent singleton-list chains ending at depth 32 | 65,001 / 65,001 | 32,001 |
| Numbers | 1,000 lists of integers 0 through 13, typed as nested lists of double | 34,001 / 62,001 | 15,001 |

Outer-size expressions assert the full native result. The numeric case also checks the converted first numeric value. Native conversion visits every bound value even when the expression reads only its outer size. A matched empty-list control returns false; neither success nor a short-circuited control substitutes for conversion of the full input.

The preparation child records the original bounds walk, typed copy, converted bounds walk, complete `bind`, full warm-cache production evaluation and an empty-expression thread-hop control. The native child uses a test-only subclass inheriting production evaluation-thread stack/context/start/restore construction, runs real checked binding compilation and measures full versus empty bound-value execution on that worker. It does not claim ordinary queue/cache dispatch coverage; the full production child supplies that evidence.

For each retained sample index, the conversion estimate is `bind_ns + native_ns - control_ns`. The report keeps those components, their signed native differences and the full production interval. A nonpositive native contribution fails identification; it is never floored or omitted. The maximum across shapes of the minimum retained conversion estimate is compared with **2,500,000 ns**, without normalization. A reference-runner exceedance writes `conversion_stop`, exits nonzero and stops later calibration for the owner's choice of a value-count limit, lower byte bound or larger evaluation budget. No bound or budget is changed automatically.

## Historical process and evidence ownership

Every child has a ten-second external deadline, 256 KiB per output stream and explicit environment/import paths. Core dumps are disabled before binding load. The supervisor closes pipes and reaps its child on success, failure, timeout and overflow. Real-process tests cover these paths and reject incomplete, mismatched or malformed observations. Full-child close must return handles, outstanding jobs and charged bytes to zero; one process-owned daemon remains until child exit. Native probe workers complete and terminate before their report.

The complete pilot has a 240-second deadline. It retains every completed observation, raw timing vector and partial failure evidence. No tracer, forced garbage collection, sleeps for synchronization, new dependency or production hook is introduced. Python heap and native-memory calibration were planned later phases and did not complete. macOS may add its system-derived `__CF_USER_TEXT_ENCODING` environment entry after process launch; it is permitted only on that platform, alongside the explicit PATH/locale entries.

The existing evaluator lifetime, logging, database, provider, browser and scanner checks remain required. A clean pilot is not calibration readiness and does not activate a consumer. The five-job fits and full calibration were later removed from the activation gate by the 2026-10-23 amendment; no such result is inferred here.

Observability impact is confined to synthetic development-measurement artifacts. Runtime event ownership, context, redaction and sinks are unchanged; normal evaluator cases retain their existing component diagnostics. Measurement records supply no execution or administrative authority.

## Historical default-bound result

The single [reference run](https://github.com/realkarych/kora/actions/runs/39827907788) at source/checkout `69c890b8e9bdcaef084053d3736630b4175b308b`, tree `add748ba3fed069b643bb2e5ea3f038e7ed64f98`, completed all 56 observations and 28 decisions in 43.574 seconds. Its [artifact](https://github.com/realkarych/kora/actions/runs/39827907788/artifacts/12446509232) records `conversion_stop`, 21 cases above 2.5 ms and no child failures. The reference used Ubuntu 24.04 image `20261012.334.1`, AMD EPYC 7763, CPython 3.11.17, uv 0.13.1 and the locked binding wheel. This is valid failed-budget evidence, not a calibrated bound.

| Largest family | Minimum complete conversion |
| --- | --- |
| ASCII strings | 16.622720 ms |
| Four-byte Unicode strings | 5.058381 ms |
| Escaped controls | 2.830931 ms |
| Dense empty lists | 65.770838 ms |
| Small maps | 20.512998 ms |
| Depth-32 chains | 99.957938 ms |
| Integer-to-double conversion | 34.038217 ms |

The deepest case contains 32,001 values and 65,001 canonical bytes. Its minimum full production interval is 102.126815 ms. The ASCII case contains only five values and 65,517 canonical bytes, so a value-count limit alone cannot address the measured conversion cost. Independent correctness and performance audits reconstructed all workloads and recomputed every decision without mismatch; the [review and CI ledger](https://github.com/realkarych/kora/pull/203#issuecomment-6347818444) retains their scopes and evidence.

The retained ZIP SHA-256 is `4cb25bf01f8a298abd5b70185c72212a6f127a9142c5653c86607ccc22c9efcf`, matching GitHub metadata. Its schema-1 JSON SHA-256 is `082654e37d7889908fe7a26b5b4720fd22df5e2ddb4fbe8343d021ceb67609d8`. Historical acceptance is reproduced only at that pinned revision. Later smaller limits do not add a production bypass for old workloads.

## Historical smaller-bound selection preregistration

The [historical amended plan](https://github.com/realkarych/kora/issues/167#issuecomment-6348001217) retained the then-current 10 ms / 16 MiB target and selected a smaller byte/value pair before continuing calibration. This preregistration alone established no new limits.

The fixed candidate grid is the Cartesian product of canonical byte limits 1,024, 2,048, 4,096 and 8,192 with value-count limits 128, 256 and 512. Each pair has ten workload families: ASCII, four-byte Unicode, escaped controls, dense empty lists, small maps, depth-32 chains, numeric expansion, key-heavy maps, mixed chains with ASCII padding, and many separately referenced variables. The last family uses a list of all variable-size operations before comparing with its expected result, so the control cannot short-circuit those operations. The successful comparison may visit more elements than its false control; this positive operation residual is retained conservatively.

Construct each case deterministically within both original and converted candidate limits. Count each referenced root and scalar/container occurrence; keys contribute canonical bytes but no values. The recipe identifies the structural unit, byte or value constraint preventing an unreachable corner. Reports retain both representations' digests, exact bytes/counts/depths, types, expression/rendering and control identity. Independent literal small-corner expectations and canonical serialization check construction separately from production admission.

The historical bounds CLI and `cel_calibration_mode=bounds` dispatch are available at source revision `31c1e90be895e9d08d9fbfa7740ecd468398f3de`, with artifact `cel-conversion-pilot`. They are not active commands or CI modes in the current tree. Schema 2 distinguishes this catalogue from the historical pilot. Selection requires a clean, matching reference checkout; non-reference or provenance-initialization failures write an explicitly nonqualifying report and exit nonzero.

Each phase retains three warm-ups and fifteen samples. Preparation/production samples contain one call. Native and control samples contain 32 real calls, each with its result asserted, and retain exact batch totals and counts. For every retained pair, require a strictly positive raw `native_total - control_total` before any rounding. A zero or negative raw difference fails the whole collection, even if rounding could manufacture a positive nanosecond. Warm-ups remain recorded and excluded from identification and qualification.

The conservative per-call conversion sample is `bind_ns + ceil(native_total / 32) - floor(control_total / 32)`. Each case contributes its minimum retained paired sample. A candidate qualifies only if `3 * worst_case_minimum_ns <= 5_000_000`, applying 1.5 headroom to the unchanged 2.5 ms allocation. Select the qualifying pair maximizing `(byte_limit * value_limit, byte_limit, value_limit)`. All 120 identities must be complete and valid before selection. A valid budget exceedance disqualifies its candidate; malformed, missing, duplicate or mismatched evidence invalidates the entire collection.

One qualifying reference job collects the grid under the unchanged 240-second suite, ten-second child and bounded-stream limits. No grid change or rerun follows an observed failure without an amended plan. The plan required five later independent calibration jobs to re-establish conversion under the raw 2.5 ms ceiling for the selected catalogue; those jobs did not complete and are no longer current gates. Selection is not final cost, memory, lifetime or crash evidence. Production caps and the initial execution-semantics contract change only after qualifying selection and their separate implementation/review steps.

## Qualified smaller-bound result

The single [bounds collection](https://github.com/realkarych/kora/actions/runs/39836408320) at clean source/checkout `31c1e90be895e9d08d9fbfa7740ecd468398f3de`, tree `1582d5e93ab600ed403022f8a495482739c73b2d`, completed all 120 cases and 240 child observations in 67.309544263 seconds. The [artifact](https://github.com/realkarych/kora/actions/runs/39836408320/artifacts/12450767954) retains 960 timing vectors, 17,280 raw observations and every candidate disposition. All 1,800 retained raw native/control differences are positive; the smallest is 80,238 ns. All completion, reference, deadline and selection gates pass, with no failures.

| Canonical byte limit | Value limit | Worst minimum conversion | Qualifies with 1.5 headroom |
| --- | --- | --- | --- |
| 1,024 | 128 | 0.457523 ms | Yes |
| 1,024 | 256 | 0.724039 ms | Yes |
| 1,024 | 512 | 1.200144 ms | Yes |
| 2,048 | 128 | 0.651869 ms | Yes |
| 2,048 | 256 | 0.908992 ms | Yes |
| 2,048 | 512 | 1.517275 ms | Yes |
| 4,096 | 128 | 1.071875 ms | Yes |
| 4,096 | 256 | 1.323915 ms | Yes |
| 4,096 | 512 | 1.828399 ms | No |
| 8,192 | 128 | 1.894702 ms | No |
| 8,192 | 256 | 2.111792 ms | No |
| 8,192 | 512 | 2.663414 ms | No |

The deterministic selection is **4,096 bytes and 256 values**. Its product ties 2,048 / 512; the registered byte tie-break chooses 4,096 / 256. Mixed containers at depth 32 are the worst selected family: 1.323915 ms, or 1.9858725 ms with headroom, below 2.5 ms. The largest retained selected production interval is 1.602646 ms. These results selected limits under the then-current 10 ms evaluation target; they do not establish the current 100 ms target for every admitted expression, an interrupt or performance on arbitrary hardware.

The reference used Ubuntu 24.04 image `20261012.334.1`, AMD EPYC 9V74, CPython 3.11.17, uv 0.13.1 and the locked binding 0.10.0 wheel. ZIP SHA-256 is `6e0613597fb9c1a68f2d305827c69a50c45e7448ffe6eb35e938cf54eb16bead`; sole JSON SHA-256 is `18734e9dda19ea541b85b968969900810047694920ed5ccb50e9a325c3c2c30a`. Separate independent correctness, performance and CI provenance audits reconstructed the evidence without mismatch. The complete dispatch passed in 390 seconds.

All 120 full children release handles, outstanding jobs and charged bytes to zero, leaving the expected process-owned daemon. All 120 native probes finish with zero workers. Successful-child wall times, PIDs and descriptor snapshots are not retained individually; the ten-second termination/reaping guarantee depends on the separately tested supervisor completion path. This collection supplies no full memory, accumulation, GIL, crash or five-job calibration evidence. Its missing evidence is not a passing result; the current scope is the compact protocol above and the separately retained evaluator lifetime suite.

Production admission uses the selected shared byte/value constants. Both original and converted aggregates are checked; typed numeric shrinkage cannot admit an oversized original. Boundary tests retain converted expansion, original shrinkage refusal, exact limits, multiple roots and real evaluator propagation. The historical recipes, exact original/converted statistics and selected-catalogue child assertions are available at the collection revision above and the bound-adoption revision `0088dd23198f60982980eaf877358bdffa1114d1`; those pilot/bounds factories and tests are not current coverage. The current lifetime workload retains its original native errors and resource thresholds, exercises bounded list/string payloads and separately refuses old oversized payloads. Its list fixture is not claimed to be the largest admitted workload under the increased steps budget. The completed-result admission regression retains its 32,768-byte threshold while fitting its original string into the canonical input limit.
