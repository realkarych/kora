# CEL conformance data and parser calibration inputs

Checked: 2026-10-20, for [issue #161](https://github.com/realkarych/kora/issues/161), on top of `master` at `409a763`.

## Vendored cel-spec files

Source: [google/cel-spec](https://github.com/google/cel-spec), directory `tests/simple/testdata`, at commit `40a3c9007a9305d1f2638d5f538a4e011732cace` (committed 2026-09-16 as the squash merge of upstream pull request 530, carrying GitHub's verified web-flow signature; the latest commit touching that directory on 2026-10-20). The files are copied byte for byte into `tests/cel/conformance/testdata/`. Their git blob identifiers equal the upstream ones, and `tests/cel/conformance/manifest.py` holds their SHA-256 values.

| File | Bytes | Upstream git blob | SHA-256 |
|---|---|---|---|
| `basic.textproto` | 6,335 | `e52604ce33cda7dcc209beaf6fee0450226f7f03` | `22cf5b68d9ca7c37881bcb8c3d958d2e1cc2c4a8a0c5bda8387ced5089ee7d9d` |
| `comparisons.textproto` | 62,348 | `e712196282f34046227f5d3044e3c1a9c99d63f5` | `db6b7d55f9991054a2caca70d9efc4e54829cc604c5d275c961aabc749548491` |
| `conversions.textproto` | 13,193 | `0e4d4b24cc0f887410e2d5092ccd8238d7d1be7a` | `f25b686d21fbf1b0689d8e580035a118b30e9eb0dd38d7896a2fc712fe7700d6` |
| `fields.textproto` | 14,960 | `66eb5d41c472a5c2656c54f24cd08aa5c6f7afb4` | `24897c82f78106a9962e97e90e673d139bd7c315f2ef0a91a63c5f92b57dcba2` |
| `fp_math.textproto` | 3,867 | `72474bc7717e00309013b70b8e2c6827c50fab7d` | `4baa5351db23b39794cf0ba49aa40800489b201654e5b1f263617c3c4942e493` |
| `integer_math.textproto` | 8,345 | `c0d4f5140e30a3ac211a55efafd8ba96e16329b6` | `71063679ee0841f8fbe260dbd9266cebdb8587997e6351121b3123988d0ebe6a` |
| `lists.textproto` | 5,205 | `2148118a4f6ccbaf3ecd93244e5fff71a424cea1` | `36f84a783d8e89e273217d2d0c4566aa01048903cbe5631a6b067778e68cefc4` |
| `logic.textproto` | 4,151 | `384e5ce06cb8a368aba9d1e3439951510297ae9f` | `544e550fac6f730b792025acc173b594f08e23442e52287e8e2ff6f92e1bda4d` |
| `macros.textproto` | 7,156 | `26a201fa8013825d8bddb28b30d5204b87a8b6d4` | `780bcac0b2d8e7375aa1d245111a08ccd28abaa134d1df863b0bb5f2357283e8` |
| `parse.textproto` | 51,107 | `6b140009005b43500e7023bd45154ff40045cd78` | `4e7065345a8caa07bc8258b1197e2358c2f7c476efa301fd2b958527ce77c85f` |
| `plumbing.textproto` | 1,795 | `7b347aa1c127053866d2d9ed533b24a53e51fbd6` | `8079b65dabb254d65f657ca8673fa7cd2f6fb30b199d7030a2087bdb8bf3b9f2` |
| `string.textproto` | 6,174 | `3571b11fc8c338954438c2ac14bc255dd1553dc4` | `1602b94ab8c315b6097a024c845de163ea5c6a5956271127f23e21720c72d4b1` |
| `timestamps.textproto` | 13,392 | `89e873d7e3b6204488fb807761f64a9977c46574` | `343c530fd664afca903a553204bae5d10e173bb70b74998dbe0ffe3b79ceaed2` |
| `LICENSE` (repository root) | 11,358 | `d645695673349e3947e8e5ae42332d0ac3164cd7` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |

Licence: Apache License 2.0. The repository has no NOTICE file and the vendored files carry no copyright headers. Keeping the files unmodified avoids the modification notices of section 4(b); the licence file is vendored beside them. The upstream `testdata/README.md` is not vendored, because its relative links point into the upstream repository.

The schema of the files is `cel.expr.conformance.test.SimpleTestFile` (`proto/cel/expr/conformance/test/simple.proto` at the same commit). Across the 13 included files the reader meets 1,178 cases and these fields: file and section `name` and `description`; test `name`, `description`, `expr`, `disable_check`, `container`, `type_env` (identifiers typed by `primitive`, `list_type`, `map_type`, `message_type` or `null`), `bindings` (values only), `value` and `eval_error` (messages only). No included case uses `disable_macros`, `check_only`, `typed_result`, `any_eval_errors`, `unknown` or `any_unknowns`.

## Cases kora rejects

`kora.cel` accepts 1,005 of the 1,178 cases and rejects 173 by design; `tests/cel/conformance/selection.py` lists each one with its feature and reason:

| Feature | Reason | Cases |
|---|---|---|
| Message types and qualified type names | `message_construction` (92), `qualified_name` (6) | 98 |
| Unescaped control characters in literals | `invalid_character` | 25 |
| Standard functions binding 0.10.0 lacks (`bool()`, time-zone accessors) | `unknown_function` | 25 |
| Functions outside the standard library (`f_unknown`, methods named after reserved words) | `unknown_function` | 19 |
| Backtick-quoted fields | `quoted_field` | 6 |

Each case has one entry, so the counts add up to the 173 rejections.

## Standard functions in binding 0.10.0

Evaluated on 2026-10-20 with `common-expression-language` 0.10.0 (CPython 3.11, macOS arm64), each call with literal arguments:

- Executed: `size` (global and method, on strings, bytes, lists and maps), `int`, `uint`, `double`, `string`, `bytes`, `type`, `dyn`, `duration`, `timestamp`, `contains`, `endsWith`, `startsWith`, the method `matches`, and the ten timestamp accessors without an argument. On durations only `getHours`, `getMilliseconds`, `getMinutes` and `getSeconds` execute.
- Compiled but failed at execution with "Undefined variable or function": `bool`, the global `matches(s, p)`, every accessor with a time-zone argument, `int(timestamp)` and `timestamp(int)`.

## Provisional pattern class weights

Bisected on 2026-10-20 against binding 0.10.0's compiled-size limit of 10 MiB, with `'a'.matches('(?:C){n}')` for each class `C`: the largest `n` that compiles gives the bytes per instance (10 MiB divided by `n`). These are the provisional class-size table of `kora.cel.regex`; the calibration of the binding work item replaces them with measurements on the reference runner.

| Class | Largest n | Bytes per instance |
|---|---|---|
| literal | 327,673 | 32 |
| case-insensitive literal | 131,069 | 80 |
| `.` | 10,485 | 1,000 |
| `\d` | 2,046 | 5,125 |
| `\D` | 822 | 12,756 |
| `\w` | 209 | 50,171 |
| `\W` | 225 | 46,603 |
| `\s` | 15,419 | 680 |
| `\S` | 5,448 | 1,925 |
| `\p{L}`, `\P{L}` | 244 | 42,974 |
| `\p{Lu}` | 395 | 26,546 |
| `\p{Ll}` | 379 | 27,667 |
| `\p{N}` | 1,074 | 9,763 |
| `\p{Nd}` | 2,046 | 5,125 |
| `\p{P}` | 880 | 11,916 |
| `\p{S}` | 662 | 15,840 |
| `\p{Z}` | 17,246 | 608 |
| `\p{Greek}` | 4,667 | 2,247 |
| `\p{Latin}` | 3,680 | 2,849 |
| `\p{Han}` | 2,890 | 3,628 |
| `\p{Cyrillic}` | 8,426 | 1,245 |
| `[a-z]` | 145,632 | 72 |
| case-insensitive `[a-z]` | 34,492 | 304 |
| `[^a]` | 10,485 | 1,000 |
| `[\p{L}\d]` | 233 | 45,003 |

`[\p{L}\d]` costs less than the sum of its members, so charging a bracketed class the sum of its members is an upper bound for this case. Anchors and `\b` stayed under the limit beyond two million instances. Compiling about 1 MiB of estimated pattern took 2 to 3 ms on this machine, so the provisional cap is 1 MiB of estimate.

## Provisional estimate weights

The weights of `kora.cel.cost.PROVISIONAL_WEIGHTS` are in femtoseconds on the probe machine and come from the [CEL binding probe](cel-binding-probe-2026-10-19.md) and the planning measurements of [issue #160](https://github.com/realkarych/kora/issues/160): a base of 1.6 µs per evaluation, 14 ns times the squared range length for `map` and `filter`, and conversion at 50 ns per value and 0.08 ns per byte. The conversion of 64 KiB of bound data measured from 0.005 ms (four strings) to 9.3 ms (21,000 nested empty lists) on the same machine; the per-value weight uses the scalar case, and whether the worst shape needs a value-count data bound is the stop condition recorded on issue #160. With these weights an expression that references a variable at the default data bounds is charged about 1.6 ms for its conversion, and any `map` or `filter` over a range of 1,000 elements exceeds the 10 ms budget.
