# Remaining IR configuration and JSON-data evidence

Date: 2026-10-23. Scope: [issue #237](https://github.com/realkarych/kora/issues/237), the remaining configuration foundation of [#218](https://github.com/realkarych/kora/issues/218). The owner accepted the contract and exact human-facing wording in [the approval](https://github.com/realkarych/kora/issues/218#issuecomment-6363920858); [the child plan](https://github.com/realkarych/kora/issues/237#issuecomment-6363940279) declares the implementation and measurement scope. This record supplies evidence and reproducible checks, not another behavioral decision.

## Implemented structural boundary

The eight remaining records are `SubagentConfig`, `AgentConfig`, `HumanInputBinds`, `HumanInputConfig`, `EventSelector`, `SensorPoll`, `WaitEventConfig` and `TimerConfig`. They retain the closed-record vocabulary, strict wire names, bounded `x-` extensions and authored omission policy. The agent reuses existing workspace, MCP, network and tool-policy records. Its skills preserve order while refusing duplicates. Subagent names and tool arrays remain structurally unrestricted strings, including empty and duplicate values. Optional polling intervals stay absent when omitted. Form defaults remain schema annotations; no form, response, predicate, polling or provider default is inserted.

The timestamp scalar checks the approved ASCII lexical profile and the Gregorian calendar without normalization. Its six-digit fraction limit follows the approved microsecond representation decision. Year-boundary UTC conversion and scheduling remain later consumer work. The test YAML constructor overrides only the timestamp tag on its own constructor subclass, returning the raw scalar text. There is no new production YAML loader.

`data.py` validates and detaches materialized JSON trees with an explicit stack and a per-field identity set. It does not call the bounded protocol parser. A repeated mutable container is refused even after its first branch has completed; distinct empty containers, shared immutable scalars and identity shared between separate fields remain valid. Non-finite numbers, non-string keys and runtime objects get fixed content-free errors. The whole open field owns the identity scope. The parsed tree and each canonical export are independently detached.

`export_ir` supports only IR-record roots. It uses model metadata, the shared wire-name helper and authored field sets, flattens record extensions, and emits string-enum wire values. The explicit open-data marker preserves the any-value field's provenance through a union with `MISSING`; mutated open trees are validated again before export. Active record/container tracking refuses cycles while allowing a validated record instance to be reused in separate positions. Ordinary shallow Pydantic dumps remain regression coverage, not a promise of deep export support.

The iterative canonical writer emits compact ASCII JSON, with escaped non-ASCII, astral, control and materialized lone-surrogate code points, exact unquoted integers, finite floats and no trailing newline. It uses the existing Pydantic scalar integer encoder rather than changing process-wide integer or recursion settings. The correctness oracle for the 5,000-digit integer is exactly 5,000 nines; other large integer expectations use the independent standard-library Decimal conversion. No new decimal algorithm is implemented.

## Checker, YAML and text-decoder evidence

The model corpus covers timestamp year, calendar, case, offset, fractional widths zero/one/six/seven, comma, leap-second and final-newline boundaries; huge durations; XOR, null, wrong types and extensions. Separate exact-fragment assertions retain the timestamp pattern and `format: date-time` in both schema modes and the full default-form annotation. Normalized structural fragment equality alone omits annotations and does not establish those properties.

The snapshot and corpus are verification evidence only, not an alternate schema registry or execution authority. Their provenance records the base revision, approved amendment state and normalized schema SHA-256. The [frozen checker corpus](ir-remaining-configuration-checker-corpus-2026-10-23.json) contains 52 independently expected cases: minimal, populated and refused values for all eight records, timestamp boundaries and timer presence/type/extension/duration cases. It is evidence separate from the model tests. A pinned `check-jsonschema` 0.38.2 probe used the actual `SchemaLoader`, default ECMAScript regex implementation and enabled `FormatOptions`; all 52 outcomes matched. The observed initial probe interpreter was Python 3.15.0. A corroborating run of the same frozen corpus with `uvx --python 3.11.17 --from check-jsonschema@0.38.2 python` also matched all 52 outcomes. The initial offline Python 3.11 attempt lacked a cached compatible `regress` wheel; normal pinned tool setup completed outside the project, with no project dependency changes. These two interpreter observations remain separate from pending final Linux CI. The offline reproduction below assumes that pinned tool environment is already cached.

To reproduce the containing-fragment check with the pinned tool environment, run this code through `uvx --offline --python 3.11.17 --from check-jsonschema@0.38.2 python` from the repository root:

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from check_jsonschema.formats import FormatOptions
from check_jsonschema.regex_variants import RegexImplementation, RegexVariantName
from check_jsonschema.schema_loader import SchemaLoader

corpus = json.loads(Path("knowledge/research/ir-remaining-configuration-checker-corpus-2026-10-23.json").read_text())
schema = corpus["schema_snapshot"]
definitions = schema["$defs"]
definitions["subagentConfig"] = definitions["agentConfig"]["properties"]["subagents"]["items"]
definitions["humanInputBinds"] = definitions["humanInputConfig"]["properties"]["binds"]
definitions["eventSelector"] = definitions["waitEventConfig"]["properties"]["event"]
regex = RegexImplementation(RegexVariantName.default)
with TemporaryDirectory() as directory:
    path = Path(directory) / "fragment.schema.json"
    for case in corpus["cases"]:
        fragment = {
            "$schema": schema["$schema"],
            "$defs": definitions,
            "$ref": "#/$defs/" + case["definition"],
        }
        path.write_text(json.dumps(fragment))
        validator = SchemaLoader(str(path)).get_validator(
            Path(directory) / "input.json",
            case["document"],
            FormatOptions(regex_impl=regex),
            regex,
            False,
        )
        assert validator.is_valid(case["document"]) is case["valid"]
print("52 checker outcomes matched")
```

Correctness tests separately preserve an unquoted YAML timestamp token as its original string. Deep tests pass already materialized arrays/maps at depths 17, 64, 128, 255, 256, 512 and 1,024; they do not establish arbitrary-depth JSON/YAML text decoding. The prior exploratory characterization found framework deep-validation/serialization failures and the stdlib integer digit threshold. Those are library observations, not schema admission bounds. The canonical boundary handles the declared deep/large fixtures without changing those libraries or claiming that `model_validate_json` accepts them.

## Required measurement and ownership

The existing required IR command and artifact carry 64 additional timed actions and 960 samples: sixteen minimal/populated round trips with 30 samples each; eight record refusals; six scaled agent validation/export actions; four 10,000-entry agent refusal actions; 24 open-data validation/export/render actions; and six open-data refusals. The latter 48 actions use ten samples each. Every action retains three warm-ups and one post-drop traced measurement with the existing strictly-less-than-65,536-byte retained gate. No latency, RSS, integer, depth, byte or aggregate-field limit is added.

The eight open objects are alternating dictionary/list trees with 17/64/255/256/1,024 containers, a 1 MiB ASCII payload, 10,000 distinct small objects, and the 5,000-digit integer. Their data sizes are 71/257/1,023/1,025/4,097, 1,048,590, 108,902 and 5,006 bytes respectively. The agent wrapper adds 73 bytes to each. The artifact records fixture SHA-256 values, bytes, container counts/depths and separate wrapper/data sizes before timing. Refused non-JSON or aliased representations have no canonical byte count; the manifest records their unique containers and refusal reason rather than expanding them.

Three independent configuration workers retain 1,000 cycles and checkpoints at 250/500/750/1,000. The root inventory grows from eleven to nineteen, with every populated record and its fixed refusal exercised each cycle. The original eleven records retain their existing Pydantic serialization action; the new eight use canonical export. Iterative weak-reference traversal reaches nested models through fields, dictionaries, lists and tuples. One supplemental case rotates through eight accepted and six refused open-data fixtures each cycle; every supplemental action is warmed three times before baseline. The timer refusal exercises an impossible calendar date.

The harness owns and reaps subprocesses, closes pipes and retains the existing 60-second worker deadline and 256 KiB per-stream cap. Parent thread/descriptor equality is checked around each child. Evidence includes traced current/peak bytes, tracer-owned memory, allocation differences, current/high-water RSS, descriptors, Python/OS threads, weak-reference release and a stable per-class compiled-schema inventory. Linux counters are mandatory on Linux. RSS includes native allocations and tracing overhead; it is not exact native-live-byte accounting. The pure model layer acquires no sessions, connections, timers or asynchronous tasks. Runtime cancellation, recovery and shutdown remain with actual consumers.

An initial dirty macOS run passed 184 checks in 150.29 seconds before final fixture and lifetime-evidence refinements. That run is non-reference evidence and cannot establish final-revision readiness. The PR must retain the final source revision, complete samples and resource results, independent component/profile conclusions, Linux CI results and a whole-workflow duration below 600 seconds. The original 120 checks/128 gates, diagnostic workers and every other required check remain active. Final executable evidence is pending those recorded results; this record adds no passing claim for unavailable consumers.

The corrected dirty macOS run passed all 184 checks in 145.02 seconds, with all 192 artifact gates true and exactly 64 new timed actions/960 new samples. Its three 1,000-cycle workers completed in 31.594, 32.280 and 31.286 seconds, each with nineteen root record types and no live root/subordinate model weak references at any checkpoint. Python/OS thread and descriptor counts returned to baseline, and the nineteen class schemas retained their identities. Post-drop retained bytes were -48, 3,130, 7,448 and 11,882 at the four checkpoints in each worker; the allocation evidence attributes the bounded increases primarily to retained checkpoint/tracemalloc reporting structures, rather than IR model paths. Current RSS varied by checkpoint and worker, with observed process high-water RSS between 60,112,896 and 60,669,952 bytes. This is native-inclusive process evidence under tracing, not a calibrated RSS gate or proof of exact native-live memory. The artifact identifies the approved base plus a dirty implementation state, so these corrected observations remain non-reference evidence and do not replace final committed-revision Linux CI or independent review.

The source with extension-prefix export preflight passed a further dirty macOS run: 184 checks in 140.86 seconds, all 192 gates true, and exactly 64 added actions/960 samples. All 64 added actions retained zero traced bytes after drop. The nineteen-record workers completed in 31.069, 30.886 and 30.899 seconds, with the same bounded checkpoint-report retention, zero remaining model weak references and unchanged class schemas. This run verifies the mutation refusal fix under the registered workloads; it remains non-reference evidence until committed-revision CI and independent review cover the completed change.

## Configuration worker deadline investigation

The ready-for-review run [39954204803](https://github.com/realkarych/kora/actions/runs/39954204803), on revision `3fd2bebc9eecddeaaea97a43c2f23650d290e7c1`, passed 181 IR checks and failed all three configuration workers at their unchanged 60-second deadline. Each process was killed with empty stdout/stderr before its final JSON report. The failed artifact contains 3,957 samples and 180 passing gates, missing the twelve lifetime checkpoints and their twelve retained gates. It establishes deadline failure, not a passing or failing memory result for the interrupted workers. The earlier [same-revision run 39952371491](https://github.com/realkarych/kora/actions/runs/39952371491) completed its workers in approximately 45–46 seconds. The slower run's shared non-lifetime medians were broadly about twice the successful run's, including unchanged diagnostics; runner region alone does not establish causality.

The [implementation adjustment within the approved scope](https://github.com/realkarych/kora/issues/237#issuecomment-6365128338) preserves the exact workloads, actions, sample/warm-up counts, 1,000-cycle method, worker deadline, report cap, retained gates, data contracts and whole-workflow budget. Investigation and implementation address production traversal allocation cost; the measurement harness and fixtures remain unchanged.

A local cProfile run over 140 real cycles under tracemalloc attributed 3.015 of 3.687 seconds to supplemental workloads. The JSON copier accounted for 2.129 cumulative seconds, and rendering accounted for 1.367 seconds including its export. Independent phase probes separated validation, export, rendering and refusal, recording wall/CPU time both with and without tracing. The 10,000-distinct-object case dominated, followed by its final-invalid counterpart. On the original implementation its ten-call validation/export phases took about 0.063 seconds without tracing and 0.71 seconds with tracing; wall and process CPU time closely agreed. This identifies traced CPU/allocation cost rather than an observed blocking wait.

The copier now retains iterators for open containers and writes validated immutable scalars directly, instead of allocating pending destination tasks for every scalar. Its identity set still spans the whole field, including completed sibling branches. Native dictionary/list access avoids introducing overridden-method callbacks, and source-order traversal preserves the final-invalid workload. The writer similarly keeps container iterators and emits separators and scalar fragments directly rather than queueing literal/value tasks for every token. It uses the same stdlib ASCII string encoder previously called through `json.dumps`; finite-float encoding and the established exact integer encoder remain unchanged. Fixed source-owned type aliases and separate container checks avoid constructing GenericAlias/UnionType objects inside these loops. They retain no input-dependent cache or adapter registry.

The copier-only bounded worker completed 1,000 cycles in 20.953 seconds, with 21.178 seconds total wall time including interpreter/import/warm-ups and reporting. Its post-drop retained bytes were unchanged at -48/3,130/7,448/11,882, with traced peaks through 5,696,534 bytes. Further isolated writer/type-expression probes measured the completed production traversal below. Values are observational local seconds and traced peaks, not new gates; each phase has ten calls except the 140-cycle row.

| Traced workload | Calls | Before wall / CPU | Completed traversal wall / CPU | Before / completed peak bytes |
|---|---:|---:|---:|---:|
| 10,000 objects, validate | 10 | 0.717009 / 0.715681 | 0.255955 / 0.255384 | 2,898,544 / 2,775,988 |
| 10,000 objects, export | 10 | 0.706608 / 0.705070 | 0.245109 / 0.244656 | 2,895,696 / 2,773,140 |
| 10,000 objects, render including export | 10 | 1.248158 / 1.245603 | 0.516379 / 0.515423 | 3,823,580 / 3,585,508 |
| Final-invalid 10,000 objects | 10 | 0.717012 / 0.715340 | 0.257817 / 0.257184 | 2,901,032 / 2,778,940 |
| Complete configuration cycles | 140 | 4.397225 / 4.386201 | 1.944007 / 1.940118 | 5,801,436 / 5,708,844 |

Without tracing, the same completed ten-call phases took 0.042150/0.041504/0.093017/0.041678 seconds, respectively, and 140 cycles took 0.378209 seconds versus the original 0.483878. The probes also retain deep-1,024-container, 1 MiB string, 5,000-digit integer and folded-alias refusal paths. Retaining ancestor iterators trades some deep-only peak memory for lower wide/scalar bookkeeping: depth-1,024 validation's traced peak increased from 198,040 to 382,728 bytes while its time fell from 0.073921 to 0.021963 seconds. No universal peak-memory reduction is claimed; output ownership, release and the existing retained-resource gate remain mandatory.

The complete 45-row fixture manifest and its SHA-256, `ecd4b5d29817271f0910331c85ee7e8dc57aebd8b2aec521a390da1de8a7a714`, match before and after, with the same 64 actions and 960 samples. Twelve additional byte regressions independently pin null/booleans, signed zero, finite exponent/subnormal values, escaped controls/backslash/quote/surrogate, empty containers and mixed-container separators. They passed against the original writer and the optimized writer. The existing deep, alias, detached-data, exact-integer and wire-field tests remain active. Actual new-head Linux CI, including total worker wall time and the whole-workflow budget, remains required; these local profiles do not establish hosted readiness.

The completed dirty local source passed the unchanged required serial command, `uv run --no-sync pytest -q -p no:logging -m measurement tests/measurements/test_ir_models_cost.py`, with 184 checks in 93.78 seconds and all 192 gates true. The artifact `/tmp/kora-ir237-cost-fix-measurement.json` identifies `3fd2bebc9eecddeaaea97a43c2f23650d290e7c1-dirty-profiled-iterators`; it remains non-reference local evidence. The three 1,000-cycle loops completed in 14.455/14.399/14.522 seconds. Every worker retained the nineteen-root inventory, unchanged class schemas, zero live model weak references and baseline Python/OS thread and descriptor counts. Their checkpoint retained bytes remained -48/3,130/7,448/11,882, and traced peaks reached 5,721,454 bytes. All 64 added per-action retained measurements were zero; the 960 added samples and all prior workloads/gates remain present.

A separate observation using the unchanged bounded process harness and 60-second deadline measured 13.774 seconds total worker wall time and 13.555 seconds for the 1,000-cycle loop, leaving approximately 0.219 seconds for prelude and reporting. This establishes local completion including interpreter/import/warm-up/report overhead, not the final hosted deadline result. Full local functional verification passed 2,003 tests with 233 measurement tests deselected in 47.65 seconds; the IR/architecture selection passed 1,057 tests, and Ruff lint/format and strict Pyright passed. Final committed-revision hosted CI, independent reviews and the under-600-second whole-workflow budget remain pending.
