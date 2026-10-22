# CEL calibration protocol and conversion pilot

Preregistered on 2026-10-22 for [issue #167](https://github.com/realkarych/kora/issues/167), under the [owner-approved plan](https://github.com/realkarych/kora/issues/167#issuecomment-6346588560). This record starts with the conversion stop condition. Constants, golden corpus, crash margins and product consumers remain pending; no measurement result is inferred from the protocol.

## Conversion decision

The reference is GitHub-hosted Ubuntu 24.04 x86_64 with CPython 3.11.17, uv 0.13.1 and the locked common-expression-language 0.10.0 wheel. The report records CPU, image, kernel, source and checkout/tree revisions, installed native-image hashes and the matching locked wheel archive digest. The latter is the hash enforced by the locked installer, rather than a new hash of a retained wheel ZIP. Local runs are explicitly non-reference and cannot pass this decision.

Run the approved first phase with `PYTHONPATH=src uv run --no-sync python -m tests.measurements.cel_cost_calibration pilot --output measurements/cel-conversion-pilot.json`. CI dispatch selects `cel_calibration_mode=pilot`; its artifact is `cel-conversion-pilot`. Existing regular CI jobs and required statuses remain unchanged. The later collection/fitting and crash commands are not implemented at this stage.

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

## Process and evidence ownership

Every child has a ten-second external deadline, 256 KiB per output stream and explicit environment/import paths. Core dumps are disabled before binding load. The supervisor closes pipes and reaps its child on success, failure, timeout and overflow. New real-process tests cover these paths and reject incomplete, mismatched or malformed observations. Full-child close must return handles, outstanding jobs and charged bytes to zero; one process-owned daemon remains until child exit. Native probe workers complete and terminate before their report.

The complete pilot has a 240-second deadline. It retains every completed observation, raw timing vector and partial failure evidence. No tracer, forced garbage collection, sleeps for synchronization, new dependency or production hook is introduced. Python heap and native-memory calibration remain later phases. macOS may add its system-derived `__CF_USER_TEXT_ENCODING` environment entry after process launch; it is permitted only on that platform, alongside the explicit PATH/locale entries.

The existing evaluator lifetime, logging, database, provider, browser and scanner checks remain required. A clean pilot is not calibration readiness and does not activate a consumer. The subsequent protocol, model corrections, five-job fits, independent final reviews and owner-reviewed documentation remain governed by the approved issue plan.

Observability impact is confined to synthetic development-measurement artifacts. Runtime event ownership, context, redaction and sinks are unchanged; normal evaluator cases retain their existing component diagnostics. Measurement records supply no execution or administrative authority.
