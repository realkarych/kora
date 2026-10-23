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
