# Standalone StructuredOutput artifact-type evidence

Recorded on 2026-10-30 for [issue #489](https://github.com/realkarych/kora/issues/489), under the [approved first-slice plan and wording](https://github.com/realkarych/kora/issues/138#issuecomment-6525165457) and [owner approval](https://github.com/realkarych/kora/issues/138#issuecomment-6525217425). This record describes the standalone exact built-in boundary and local observations; the implementing pull request records independent implementation-review results and complete final-head CI evidence.

## Implemented boundary

`artifact_type.structured_output` selects the trusted `structured_output` implementation, whose logical name is `StructuredOutput`. The closed empty deployment configuration and operation-local binding use the existing ArtifactType ABI. The constructor compares the exact published revision-1 schema, null parent and complete descriptor before copying supplied data. Retained schema/descriptor values derive from trusted fixed definitions; returned schema views are defensive.

Native validation delegates to the unchanged bounded model. Digest selection is a fresh `[""]`, and summary is the detached complete identity value, including root null. JSON arrays remain valid StructuredOutput values; unavailable derived `List[T]` activation is a separate contract. Exact integer/float kind, signed zero and NUL remain part of canonical payload identity. Hooks acquire no external resource and retain no context, payload, result or per-call cache. Expected violations and fixed unchained protocol errors carry no submitted content. Ordinary DEBUG outcomes use only the supplied caller context and fixed operation/outcome/reason/violation fields.

This binding carries no inline schema or custom parent/reference closure. Its pass does not establish complete effective-output validity. Full saved-schema evaluation, exact schema arithmetic, bounded regex/reference/keyword work, immutable policy interpretation and shared runner/API acceptance remain pending their separately approved core design. Producer, API, CEL, upload/replay/storage/retention and finite derived-list activation retain their named owners; no consumer may substitute this standalone pass for that complete decision.

## Local checks and input identity

Local source revision: `4d9df83e6db1c38a235532553439622e2cfb2819`, based on `783a1c3e86774b0e3fc0fa789247a67a8da7f422`. Environment: macOS arm64, Python 3.11.17, uv 0.11.7; installed-wheel preparation used pinned Node 24.22.0 and pnpm 12.14.0. The checkout was clean before checks and after measurement. Adding this evidence record changes no executable, producer, contract or dependency input; original reports retain their actual source revision.

| Check | Actual result |
|---|---|
| Ruff format, Ruff lint with `--ignore-noqa`, strict Pyright | Passed; 789 files formatted, no lint findings or type errors/warnings |
| Focused artifact-type, installed StructuredOutput/listing, model/contract, architecture and light-helper checks | Initial invocation failed: 973 passed and four existing model cases had setup errors because `-p no:logging` removed their required `caplog` fixture. That failed result is retained. |
| Only the four affected `test_structured_output_safe_diagnostics` cases, with logging enabled | Passed: four cases in 0.05 s, exit 0, without source changes or a repeated full campaign |
| Revision-bound provider diagnostic | Passed: one test in 3.64 s, nineteen rows, exit 0 |

The passing focused cases cover all six roots and canonical identity, complete binding/kind/annotation/cross-type substitutions, wide/deep/cyclic/subclass refusal, input/result detachment, fixed extraction errors, caller-context isolation, content exclusion, both real blocked/broken sinks and actual diagnostic closure. Shared direct conformance and the real installed adapter execute the selected provider with `{}`. The installed command asserts ten completed checks, the original complete outcome and closed parent-owned cleanup.

The corrected command was `uv run --no-sync pytest -q tests/ir/test_structured_output.py::test_structured_output_safe_diagnostics`. Measurement used `KORA_MEASUREMENT_REVISION=4d9df83e6db1c38a235532553439622e2cfb2819`, the existing `KORA_MEASUREMENT_OUTPUT` destination and `uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_protocol_models_cost.py::test_structured_output_artifact_type_provider_cost`. Exact output destinations, complete commands, JUnit, logs and native artifacts are recorded in the implementing PR. No complete final-CI result is inferred from local checks.

## Bounded diagnostic method and observations

The existing protocol-model producer adds exactly nineteen rows: one constructor bind, four operations for each of four accepted canonical sizes, and two hostile binding refusals. Each row has five warmups and thirty measured calls. Sizes are 1,024, 65,536, 1,048,575 and 1,048,576 bytes. Paired native refusals cover early domain, late domain and canonical over-limit inputs. One hundred lifecycle cycles use a caller-owned 1-KiB value, discarding projections. Raw-token and long-mantissa model campaigns are not repeated.

Fixed trusted schema/descriptor/configuration JSON sizes are 172/665/49 bytes. Caller-held inputs and the reference canonical value are constructed before observation and reported separately. After lifecycle capture completion, queue and in-flight bytes are zero, the writer is stopped and capture release assertions pass. The provider retains only its unchanged fixed trusted state.

| Observation | Local value and interpretation |
|---|---|
| 1-KiB validation / summary process CPU per call | 64.77 / 96.40 microseconds, derived from an instrumented batch average |
| 1-MiB validation / selection / summary process CPU per call | 7.18 / 7.32 / 26.85 milliseconds, derived from instrumented batch averages |
| Settled lifecycle traced delta / peak | +2,150,919 / 2,563,909 bytes; observer-inclusive, not component retention attribution |
| Intentionally retained observer capture | 345,324 bytes, 968 decoded records and nineteen report samples |
| Current macOS RSS | Unavailable, reported as null with availability false |
| Queue / in-flight / writer | Zero bytes / zero bytes / stopped; deterministic release checks passed |

`time.process_time` runs under tracemalloc and DEBUG capture, including writer-thread CPU and observation overhead. The shared report's median/p95 fields contain a single batch average, not individual-call percentiles or wall latency. `capture.finish` materializes retained observer records; memory deltas include that state and establish neither a provider leak nor general leak freedom. Time, process heap and RSS remain diagnostics without new thresholds. Frozen product limits, fixed retained state and actual resource release remain blocking.

## Review and remaining activation

The existing threat entries cover this boundary: TM-04/TM-10 admission/work, TM-06/TM-07 disclosure, TM-08/TM-11 immutable identity and TM-12 cooperative installation/evidence. No actor, authority or control changes. The [independent design results](https://github.com/realkarych/kora/issues/138#issuecomment-6525184318) cover only the approved first slice; later evaluator semantics remain undecided. The implementing PR binds actual source/evidence reviews and mandatory CI to its final head, including any documentation-only evidence delta.

The inspected master mutation run `41025696626`, source/tooling `4cf962d6db69fdec13de102be31e7350330ff917`, is partial and covers pricing/CEL only. It supplies no applicable StructuredOutput/provider finding and is not a component audit pass. No additional mutation or full-CI campaign is introduced by this record.
