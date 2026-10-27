# StructuredOutput numerical contract and fixture provenance

Checked: 2026-10-23. This record supports the owner-approved [revision-1 contract](../specs/artifacts/StructuredOutput.md) under [issue #246](https://github.com/realkarych/kora/issues/246). It is dated evidence; the committed per-type schema, descriptor and specification govern acceptance. Contract publication does not implement a numerical codec, schema engine or storage consumer.

## Primary sources

| Source | Relevance and limit |
|---|---|
| [JSON Schema 2020-12 core, instance equality](https://json-schema.org/draft/2020-12/json-schema-core#section-4.2.2) | Mathematical numerical equality is distinct from lexical identity. Recursive array/object equality underlies `const`, `enum` and `uniqueItems`; booleans are a separate instance type. It does not choose a kora canonical byte representation. |
| [JSON Schema 2020-12 validation, numerical assertions](https://json-schema.org/draft/2020-12/json-schema-validation#section-6) | Integral mathematical values satisfy `integer`; bounds and `multipleOf` concern numerical values. The specification does not justify binary64 division or tolerance-based acceptance. |
| [CPython 3.13.0 float rendering](https://github.com/python/cpython/blob/v3.13.0/Python/pystrtod.c) | A fixed implementation reference for shortest round-trip decimal rendering, notation thresholds and preserving a floating suffix. The frozen contract specifies its own choices; installing a different Python does not amend them. |
| [Python 3.13 JSON encoder/decoder documentation](https://docs.python.org/3.13/library/json.html) | Explains integer/float token mapping, sorting and Unicode escaping options, and why default permissive nonfinite/duplicate handling is insufficient. The ordinary decoder cannot supply the contract's bounded lexical preflight or exact schema arithmetic. |
| [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html#section-3.2.2) | JCS uses ECMAScript number serialization and UTF-16 key ordering. StructuredOutput deliberately preserves exact admitted integers, integer/float identity and signed floating zero, and sorts Unicode scalar keys by code point; its bytes must not be labeled JCS. |

## Fixture construction

[StructuredOutput.vectors.json](../specs/artifacts/StructuredOutput.vectors.json) records raw and canonical numeric spellings as strings so parsing the fixture cannot round an integer or discard signed floating zero. Expected spellings follow the approved rules, including both nearest-even tie directions around 1, subnormal/normal and overflow boundaries, the notation thresholds, and integers beyond binary64's consecutive exact range. The 14,000-bit boundary is the exact decimal expansion of `2**14000 - 1`; the first refused magnitude is `2**14000`. No global integer conversion setting is changed.

Each canonical case has an exact UTF-8 byte count and SHA-256. Large repeated byte-boundary inputs use an ordered list of literal strings and `{unit, count}` segments: concatenate the literal text, then UTF-8 encode. This is a compact description of exact fixture bytes, not a parser, normalization rule or runtime canonicalization implementation. The fixture test assembles only these declared bytes and checks their pinned identity. The vector file's own two-space-indented UTF-8 bytes, with final newline, are pinned separately. File integrity is deliberate for immutable wire contracts; it is not runtime numerical conformance.

The two publication tests use the existing `jsonschema` development dependency's real `Draft202012Validator`, with an explicit `referencing.Registry()` for instance checks. Metaschema checking uses the validator's bundled draft resources. The six root examples and the `structural_cases` table are ordinary structural validation. Raw refusals, schema-arithmetic, inheritance, checker, producer and availability cases declare required outcomes for future conformance; they are not reported as executed. In particular, the library's ordinary floating `multipleOf` implementation cannot establish the frozen decimal-rational semantics.

The direct-root examples are intentionally small, readable JSON documents, with ordinary formatting. They describe values admitted before canonicalization; their formatted file bytes are not canonical-wire upload fixtures. Exact wire bytes are recorded in `canonical_cases`.

## Evidence still required

The [activation table](../specs/artifacts/StructuredOutput.md#5-verification-and-activation) assigns executable raw/native admission, immutable snapshots, exact schema arithmetic, effective checker views, producer parity, canonical wire/replay and retention to their implementing work items. It includes independent schema-work limits, the model's 500-cycle lifetime pairs, IR/projection bounds and the storage contract's applicable 1,000-cycle endings. Both databases must preserve the admitted snapshot and separately converted CEL projections, including numerical kind, signed zero, NUL and available root null. Existing event/observation/approval normalization before typed admission remains outside that preservation boundary.

No resource acquisition, new logger or executable component is introduced by this publication. TM-04/TM-10 bounds, TM-06/TM-07 content safety, TM-08/TM-11 identity/authority and TM-12 evidence separation still require actual implementation evidence at activation. Passing fixture tests cannot establish upload authorization, lossless JSONB storage, rendering safety, bounded schema work or leak freedom.
