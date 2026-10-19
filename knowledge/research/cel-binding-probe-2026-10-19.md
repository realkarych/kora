# CEL binding probe

Checked on 2026-10-19. Scope: a hands-on probe for [issue #116](https://github.com/realkarych/kora/issues/116) of the Rust-backed CEL binding chosen by [ADR-0006](../../docs/decisions/0006-graph-definition-dsl-and-yaml-representation.md) and the "Expression language" row of the [stack table](../architecture/detailed.md), against the CEL usage in [ir.md section 7](../specs/ir.md) at `94cd45451f56b1b786f7b1c8c418276890a06ddb`. This is dated evidence, not a decision. The prototype is throwaway code outside the repository; the snippets below are the parts needed to reproduce each result.

Environment: macOS 27.0.1 on Apple silicon (arm64), uv 0.11.7, CPython 3.11.17 and 3.15.0 in separate throwaway virtual environments. Packages: `common-expression-language` 0.10.0, `cel-python` 0.5.0 and, as a side check, `cel-expr-python` 0.1.3. Timings are single-machine wall-clock figures and only their order of magnitude is meaningful.

## Summary

| Topic | Finding | Relation to ADR-0006 and the stack table |
|---|---|---|
| Installation | PyPI `common-expression-language`, import name `cel`, Apache-2.0, latest 0.10.0 (2026-09-15). Wheels for CPython 3.11–3.14 on macOS arm64, Linux x86_64 and Linux aarch64; 3.15 has a wheel only for Linux x86_64. Elsewhere on 3.15 the sdist builds with maturin, which downloads a Rust toolchain when none is installed. Single maintainer, beta status. | Confirms the choice for 3.11–3.14. On macOS arm64 and Linux aarch64, Python 3.15 has no wheel yet, a gap the stack table does not mention. |
| Evaluation | Every expression in the specifications and examples evaluates correctly. The FR-2.5 example fails at run time with `TypeError` because a JSON integer multiplied by a double has no overload. Missing fields raise `KeyError`. `size()` on strings counts UTF-8 bytes, not code points. | Confirms, with three integration requirements: per-schema number normalisation, an explicit exception mapping and result-type checks. |
| Static checking | The binding has no type checker and does not expose its syntax tree. `Program.references()` lists only top-level identifiers. kora has to parse expressions itself. A hand-written parser and checker sketch of about 690 non-blank lines checks every example expression and catches field typos, enum-literal typos, non-bool results and numeric mismatches. | Confirms "the binding has no static checker of its own" and that kora's own checker (`IR011`) is necessary. |
| Untrusted expressions | The binding has no cost limit, no timeout and holds the GIL during evaluation, and a signal cannot interrupt it. About 12,000 chained selections or additions crash the interpreter with SIGSEGV; on a 256 KiB thread stack, 1,000 are enough. `map` and `filter` take quadratic time in the list size. Chained `map` calls grow memory exponentially. Regular expressions run in linear time. | Not covered by ADR-0006 or ir.md. The checker has to enforce length and depth limits, and evaluation needs process isolation or bounded inputs. |
| cel-python | No static checker either. It exposes a lark syntax tree with positions. It deviates from current CEL semantics in 6 of the 7 cases where the two libraries disagree. Compile is about 6× slower and comprehensions about 80× slower. It cannot be installed on Python 3.15 on any platform because of its `google-re2` dependency. Deep nesting raises a catchable `RecursionError`, and a signal can interrupt evaluation. | Confirms "`cel-python` has no static checking"; as the fallback it is weaker than the decided binding. |

**Recommendation for the first slice:** keep the decided binding and do not switch to the fallback. The reasons and conditions are in the [last section](#recommendation-for-the-first-slice).

## Expressions under test

Collected from [ir.md](../specs/ir.md), [dsl.md](../specs/dsl.md), [FR-2.5](../../docs/requirements/functional.md) and the two example graphs in [examples/](../specs/examples/):

| Source | Field | Expression |
|---|---|---|
| ir.md section 4.2; both examples | `approve_when` | `response.decision == 'approve'` |
| ir.md section 4.3; dependency-upgrade | `poll.until` | `observation.status == 'merged'` |
| ir.md section 8 | sensor trigger `poll.until` (http sensor) | `observation.body.ready == true` |
| ir.md section 4.3 | `event.match` | `payload.target == inputs.target.id` |
| sql-optimization | `event.match` | `payload.commit == inputs.bundle.facts.change.commit` |
| sql-optimization | `cases[].when` | `inputs.verdict.approved == true && inputs.same.exit_code == 0` |
| sql-optimization | `else_reason` | `inputs.verdict.comments` (a string result) |
| dsl.md section 1 | branch condition | `inputs.verdict.approved && inputs.same.exit_code == 0` |
| ir.md section 6 | `retry.predicate` | `outputs.verdict.approved == false` |
| FR-2.5 | branch condition (illustrative) | `review.verdict == "approved" && bench.cpu < baseline.cpu * 0.85` |

No specification uses timestamps, durations, optional syntax or extension functions. ir.md section 7 allows only CEL's standard functions.

## Installation and wheels

The PyPI distribution is `common-expression-language`, import name `cel`, source [hardbyte/python-common-expression-language](https://github.com/hardbyte/python-common-expression-language). It is built with maturin and PyO3 over the `cel` crate 0.14.5 ([cel-rust](https://github.com/cel-rust/cel-rust), MIT), which parses with `antlr4rust`.

| Fact | Value (PyPI JSON API and GitHub API, 2026-10-19) |
|---|---|
| License | `Apache-2.0` (license expression; classifier "Apache Software License") |
| `requires_python` | `>=3.11`; classifiers list 3.11–3.14 |
| Status | "Development Status :: 4 - Beta". The changelog defers 1.0 until the upstream crate supports the CEL protobuf AST |
| Releases | 16 with files since 0.1.1 (2024-04-11). Recent: 0.6.0 2026-05-12, 0.7.0 2026-07-04, 0.8.0 2026-08-19, 0.9.0 2026-09-08, 0.10.0 2026-09-15 |
| Runtime dependencies | `typer`, `rich`, `prompt-toolkit` and `pygments` (for the bundled CLI and REPL). `import cel` loads all four. kora already uses Typer and rich for its CLI |
| Repository activity | 43 stars, 4 forks, last push 2026-10-17. 64 of the 66 human commits are by one author. Open dependency bumps include `cel` 0.14.5 → 0.15.0 (opened 2026-10-10, unmerged) |
| Relevant open issues | #46 abi3 wheels (day-one support for new Python releases), #47 musllinux wheels, #48 expose the parsed expression tree, #49 common exception base class, #57 comprehension macros are O(n²) in cel 0.14.5 |
| Upstream engine | crate `cel` 0.15.0 (2026-10-06), 696 GitHub stars, last push 2026-10-18, ~4.4 million crate downloads |

Wheel availability, computed from the PyPI JSON API (`wheel_matrix.py` scans file names for `cpXY-cpXY` tags and platform tags) and confirmed by wheel-only resolution with `uv pip compile --python-version X --python-platform P --only-binary :all:`:

```text
common-expression-language 0.10.0 (53 wheels, no abi3 wheel)
                  cp311   cp312   cp313   cp314   cp315
macOS arm64         yes     yes     yes     yes       -
Linux x86_64        yes     yes     yes     yes     yes
Linux aarch64       yes     yes     yes     yes       -
```

Releases 0.5.6 and 0.6.0 have the same matrix without any cp315 wheel; 0.9.0 is identical to 0.10.0. Free-threaded `cp314t` and `cp315t` wheels exist only for Linux.

Installing on Python 3.15 on this machine:

```text
$ uv pip install --python .venv315/bin/python --only-binary :all: common-expression-language==0.10.0
  × No solution found when resolving dependencies:
  ╰─▶ Because common-expression-language==0.10.0 has no usable wheels ...
$ uv pip install --python .venv315/bin/python common-expression-language==0.10.0
   Building common-expression-language==0.10.0
      Built common-expression-language==0.10.0
Prepared 1 package in 26.10s
```

No `cargo` or `rustc` was on `PATH`. The verbose build log shows how maturin coped:

```text
DEBUG Adding direct dependency: puccinialin*
DEBUG Rust not found, installing into a temporary directory
DEBUG Installing rust to /Users/<user>/Library/Caches/puccinialin/rustup
DEBUG    Compiling antlr4rust v0.5.2
DEBUG    Compiling cel v0.14.5
```

Maturin pulled in `puccinialin`, which downloaded a Rust toolchain of about 525 MB into the user cache directory and built the extension. The 3.15 build gave the same results as the 3.11 wheel on the 69-case evaluation corpus. A Rust toolchain, or network access to download one, is therefore needed wherever no wheel exists. Today that means CPython 3.15 on macOS arm64 and Linux aarch64, musl-based images (Alpine) and any other platform. Python 3.11–3.14 on the three target platforms needs none.

## Evaluation of the specification expressions

```python
import cel

program = cel.compile("response.decision == 'approve'")
program.execute({"response": {"decision": "approve", "comments": None}})   # True
```

The variables come from `json.loads` of a JSON document shaped like kora payloads: `response`, `observation`, `payload`, `inputs.{target, verdict, same, metrics, baseline, bundle.facts.{change, gates[], labels[]}}`, `outputs.verdict`, plus `review`, `bench` and `baseline` for the FR-2.5 example. `run_binding.py` and `run_celpy.py` evaluate the same 69 cases with both libraries.

Specification expressions (binding):

```text
ir.md/examples approve_when                 | ok    bool: True     (False with decision='reject')
ir.md/example until (change_request_status) | ok    bool: True
ir.md until (http sensor)                   | ok    bool: True
ir.md event match                           | ok    bool: True
sql-optimization event match                | ok    bool: True
sql-optimization branch when                | ok    bool: True
sql-optimization else_reason                | ok    str: 'equivalent, 22% faster'
dsl.md branch when                          | ok    bool: True
ir.md retry predicate                       | ok    bool: True
FR-2.5 example                              | error TypeError: No such overload: the operation is not defined for the given operand types. CEL does not coerce between types ...
```

The FR-2.5 example fails because `baseline.cpu` is `100` in the JSON. Python's `json` turns that into an `int`, and CEL has no `int * double` overload. cel-python and the reference cel-cpp runtime reject it too.

Targeted cases (binding, trimmed):

```text
list index                     inputs.bundle.facts.gates[2].exit_code == 3        ok True
dotted key via index           artifacts['implement.change'].commit == '4f1c2e9'  ok True
has() present / absent         has(inputs.verdict.approved) / has(response.reason) ok True / ok False
has() null-valued field        has(response.comments)                              ok True
has() on absent parent         has(response.missing.deeper)                        error KeyError: 'missing'
missing field access           response.reason == 'x'                              error KeyError: 'reason'
null equality                  response.comments == null                           ok True
select on null                 response.comments.text == 'x'                       error TypeError: No such overload ...
index out of range             inputs.bundle.facts.gates[7].exit_code == 0         error IndexError: index out of bounds: 7
all() / exists() / exists_one() over gates                                         ok False / True / True
map() / filter().map()                                                             ok ['tests','lint','equivalence'] / ['equivalence']
size(), .size(), 'x' in list, 'k' in map                                           ok
contains / startsWith / endsWith / matches('^[0-9a-f]{7}$')                        ok True
string size unicode            size('héllo') == 5                                  ok False   (size('héllo') returns 6)
lowerAscii (extension)         'ABC'.lowerAscii() == 'abc'                         error RuntimeError: Undefined variable or function: 'lowerAscii' ...
JSON int == double literal     inputs.same.exit_code == 0.0                        ok True
JSON double < int literal      inputs.metrics.cpu_seconds < 100                    ok True
JSON int * double literal      inputs.baseline.cpu_seconds * 0.85                  error TypeError: No such overload ...
JSON double * int literal      inputs.metrics.cpu_seconds * 2                      error TypeError: Unsupported operation: float * int ...
int + uint / int == uint       1 + 1u / 1 == 1u                                    error TypeError / ok True
int division / by zero         7 / 2 / 1 / 0                                       ok 3 / error ZeroDivisionError: division by zero in CEL expression
int overflow                   9223372036854775807 + 1                             error OverflowError: Arithmetic overflow in 'add' ...
type() of JSON int / double    type(x) == int / type(y) == double                  ok True / True
bool vs string                 inputs.verdict.approved == 'true'                   ok False
non-bool result                response.decision                                   ok str: 'approve'
error absorption               false && response.reason == 'x' / response.reason == 'x' || true   ok False / ok True
undeclared variable            approved == true                                    error RuntimeError: Undefined variable or function: 'approved' ...
syntax error                   response.decision ==                                error ValueError: Failed to parse expression ...: ERROR: <input>:1:21: Syntax error: mismatched input '<EOF>' ...
optional syntax                response.?reason.orValue('none')                    error ValueError: ... unsupported syntax '.?'
timestamps and durations       timestamp(...) + duration('24h') == timestamp(...)  ok True
```

Exceptions the binding raises. The package documents these classes as public API and the message text as unstable:

| Failure | Exception |
|---|---|
| Syntax error, unsupported syntax | `ValueError` at `compile`, with line and column inside the message |
| Python input that cannot be converted (for example `object()`) | `ValueError` ("Conversion Error") at `execute` |
| Undeclared variable or function; invalid regular expression | `RuntimeError` |
| Missing map key, including inside `has()` on an absent parent | `KeyError` |
| No matching overload, selection on null, indexing a string | `TypeError` |
| List index out of range | `IndexError` |
| Integer division by zero; integer overflow | `ZeroDivisionError`; `OverflowError` |
| Parser panic (`(` nested 100,000 deep) | `pyo3_runtime.PanicException`, a subclass of `BaseException` and not of `Exception` |
| Stack overflow (see [Untrusted expressions](#untrusted-expressions)) | the process dies with SIGSEGV or SIGBUS |

What this means for kora:

- **Numbers need schema-driven normalisation.** A JSON integer bound to a schema field of `type: number` reaches CEL as `int`, and arithmetic with a double literal then fails at run time. Before binding a payload, kora has to convert values to the CEL type the checker assumed: `number` to `float`, `integer` to `int`. Python integers of 2^63 and above silently become `uint`, and 2^64 and above become `double`.
- **Missing optional fields fail; they are not false.** `response.comments == 'x'` raises `KeyError` when the optional field is absent. ir.md does not say which error class an evaluation error produces in `approve_when`, a branch, a retry predicate, `match` or `until`; that question is open for the specification.
- **Results are plain Python values** (`bool`, `str`, `list`, …), and nothing enforces the result type: `response.decision` as an `approve_when` returns `'approve'`. The checker or the caller has to require `bool` (and `string` for `reason` and `else_reason`).
- **One conformance deviation:** `size()` on a string counts UTF-8 bytes (`size('日本')` is 6), while the CEL specification counts code points (cel-cpp and cel-python return 5 for `size('héllo')`). ADR-0006 runs the CEL conformance suite in CI. The test suite shipped in the binding's sdist does not include the cel-spec conformance tests, so kora's CI should expect failures at least on string size and the unsupported `.?` optional syntax, and keep an explicit list of known deviations.
- **Heterogeneous numeric equality and ordering work** (`0 == 0.0`, `1 == 1u`, `81.0 < 100`), matching current CEL semantics; arithmetic never converts implicitly.

Relation to the decision: this confirms that the binding evaluates every expression the specifications use. ADR-0006 and ir.md do not mention the normalisation, exception-mapping and result-type requirements.

## Static checking

The binding exposes no checker and no syntax tree. Its whole compiled-program surface is:

```text
>>> [a for a in dir(cel.compile("x")) if not a.startswith("_")]
['execute', 'functions', 'references', 'source', 'variables']
>>> p = cel.compile("inputs.verdict.approved && size(inputs.bundle.facts.gates.filter(g, g.exit_code != 0)) == 0 && response.decision == 'approve'")
>>> p.variables()
['g', 'inputs', 'response']
>>> p.functions()
['_!=_', '_&&_', '_+_', '_==_', '_?_:_', 'size']
```

`variables()` returns top-level identifiers only, and comprehension variables appear as if they were free. `functions()` includes the operators that the `filter` macro expands into. The package's introspection guide states that the upstream crate "has no protobuf support and no separate type-checking phase". Exposing the tree is open as issue #48. Field paths, types and positions of sub-expressions are therefore unavailable, which confirms ADR-0006: kora needs its own parser as well as its own checker.

Ways to obtain a syntax tree:

| Option | Assessment |
|---|---|
| Hand-written lexer and recursive-descent parser for the CEL grammar | No new dependency. The grammar is small: about two dozen parser rules in the cel-spec ANTLR grammar. The probe sketch below takes 310 non-blank lines. Position information and limits on length and depth come naturally. |
| `lark` with the grammar from cel-python (`celpy/cel.lark`, 179 lines, Apache-2.0) | `lark` is pure Python (`py3-none-any`). Vendoring a grammar adds a dependency to record in the stack table, and kora would still write the whole checker. |
| The cel-python package's own parser (`celpy.Environment().compile()` returns a `lark.Tree` with line and column) | It drags in `google-re2`, `pendulum`, `jmespath`, `lark` and `pyyaml`, and cannot be installed on Python 3.15 (see the comparison below). |
| `cel-expr-python` 0.1.3 (PyPI, Apache-2.0, the CEL project's own Python runtime over cel-cpp) | It has a real checker: `cel.NewEnv(variables={"count": Type.INT})` with `env.compile("count * 0.85")` raises `found no matching overload for '_*_' applied to '(int, double)'`, and `respons.decision` reports an undeclared reference. But a JSON object can only be declared as `map(string, dyn)`. Field-level typing needs protobuf message types, so kora would have to generate descriptors from JSON Schema. Version 0.1.x, an 11.5 MB wheel, and no cp315 wheel on any platform. A candidate to watch, not to adopt for the first slice. |

### Checker sketch

A minimal schema-driven checker in three modules (`celcheck/syntax.py`, `types.py`, `checker.py`):

- **Syntax:** a lexer for CEL literals (int, uint, double, hex, single, double and triple-quoted strings, raw strings and bytes, escapes), identifiers and operators, and a recursive-descent parser following CEL precedence: `?:`, `||`, `&&`, relations and `in`, `+ -`, `* / %`, unary, member and index chains, list and map literals. The parser enforces at most 4096 characters and an AST depth of at most 64, counting chained selections and binary operators. Both limits fall below the binding's crash thresholds. Message construction and `.?` are rejected with explicit errors.
- **Types:** CEL types from JSON Schema. An `object` with `properties` becomes a record with known fields and a required set; an object without properties becomes `map(string, dyn)`. String `enum` becomes `string` with its allowed values; `integer` becomes `int`, `number` becomes `double`, `boolean` becomes `bool`, `array` becomes `list(T)`, and anything else becomes `dyn`.
- **Checker:** resolves identifiers and comprehension variables, checks field selection against declared properties, and applies the standard overloads (equality, ordering with cross-numeric comparison, arithmetic without implicit conversion, `&&`, `||`, `!`, `?:`, `in`, indexing). It covers `size`, the string predicates, the conversions, `timestamp`, `duration`, `dyn` and `type`. Macros `has`, `all`, `exists`, `exists_one`, `map` and `filter` bind the element type. It rejects functions outside the standard library and checks the expected result type: `bool`, or `string` for reasons. Unknown fields, enum literals outside the allowed values and type mismatches are errors. An optional field read without a `has()` guard is a warning. Comprehensions are limited to 4 per expression and a nesting depth of 2.

```python
def check_expression(source: str, environment: Mapping[str, CelType], expected: CelType) -> CheckResult: ...

form = {"type": "object", "properties": {"decision": {"enum": ["approve", "reject"]}, "comments": {"type": "string"}}, "required": ["decision"]}
check_expression("response.decision == 'aprove'", {"response": from_json_schema(form, "response")}, BOOL)
```

`check_examples.py` loads both example IR files from the repository (read-only). It builds `inputs` from each node's declared input types: the custom schemas come from `type_contracts`, and the built-in `VcsRef`, `CommandResult` and `ReviewBundle` use field lists assumed for the probe. `response` comes from the node's `form`, and `payload` and `observation` become `map(string, dyn)`. The script then checks every expression and compiles it with the binding:

```text
== dependency-upgrade.ir.yaml
PASS approve.approve_when                         "response.decision == 'approve'" -> bool
PASS merged.poll.until                            "observation.status == 'merged'" -> bool
== sql-optimization.ir.yaml
PASS final_pr.approve_when                        "response.decision == 'approve'" -> bool
PASS prestable_rollout.event.match                'payload.commit == inputs.bundle.facts.change.commit' -> bool
PASS decide.cases.when                            'inputs.verdict.approved == true && inputs.same.exit_code == 0' -> bool
PASS decide.else_reason                           'inputs.verdict.comments' -> string
```

Targeted cases, each followed by what the binding does at run time with schema-conforming data:

```text
FAIL approve_when typo in field       "response.decison == 'approve'"
       IR011 error @9: 'response' has no field 'decison' (declared: comments, decision)
       binding at run time: KeyError: 'decison'
FAIL approve_when typo in enum value  "response.decision == 'aprove'"
       IR011 error @21: 'aprove' is not an allowed value (approve, reject), so the comparison is constant
       binding at run time: False
FAIL approve_when non-bool result     'response.decision'
       IR011 error @0: expression has type string, but this field needs bool
       binding at run time: 'approve'
PASS approve_when optional field      "response.decision == 'approve' && response.comments != ''"
       IR011 warning @43: 'response.comments' is optional in the schema; evaluation fails with 'No such key' when it is absent, guard it with has(response.comments)
       binding at run time: KeyError: 'comments'
PASS approve_when guarded             "response.decision == 'approve' && (!has(response.comments) || !response.comments.contains('block'))"
       binding at run time: True
FAIL approve_when wrong operand type  'response.decision == true'
       IR011 error @18: no matching overload for '_==_' applied to (string, bool)
       binding at run time: False
FAIL branch: int vs double literal    'inputs.same.exit_code == 0.0'
       IR011 error @22: no matching overload for '_==_' applied to (int, double)
       binding at run time: True
FAIL branch: double < int*double      'inputs.metrics.cpu_seconds < inputs.baseline.rows * 0.85'
       IR011 error @50: no matching overload for '_*_' applied to (int, double); CEL does not convert between int, uint and double, wrap one side in double() or int()
       binding at run time: TypeError: No such overload ...
FAIL branch: macro typo in element    'inputs.gates.all(g, g.exitcode == 0)'
       IR011 error @22: 'gates[]' has no field 'exitcode' (declared: exit_code, name)
FAIL branch: non-standard function    "inputs.verdict.comments.lowerAscii() == 'ok'"
       IR011 error @24: function 'lowerAscii' with 1 argument(s) is not in the CEL standard library available to kora
FAIL limit: nested comprehensions     'inputs.gates.all(a, inputs.gates.all(b, inputs.gates.all(c, true)))'
       IR011 error @53: comprehension macros nest deeper than 2
FAIL limit: deep parentheses          70 nested parentheses
       IR011 error @64: expression nests deeper than 64 levels
FAIL limit: string doubling           26 chained .map(a, a + a)
       IR011 error @306: more than 4 comprehension macros in one expression
```

The silent cases carry the main value of the checker. A misspelt enum value or a wrong operand type makes `approve_when` false forever, and the binding never reports an error. Every approval would then fail as `approval_rejected`.

Two of the checker's choices go beyond what the binding enforces and are policy decisions for the specification. Strict `int` versus `double` equality follows the cel-go and cel-cpp checkers, which keeps graphs portable to other SDKs (NFR-6.1) even though the binding accepts `0 == 0.0` at run time. Rejecting unknown fields on schemas that allow additional properties is the other choice.

Size: `syntax.py` has 310 non-blank lines, `types.py` 84 and `checker.py` 291, about 690 in total. `syntax.py` and `checker.py` pass `pyright --strict` (1.1.408). The 10 remaining errors all sit in the untyped JSON Schema walk in `types.py`, which production code would replace with a typed schema model at the boundary. Estimates:

| Scope | Estimate |
|---|---|
| Full-grammar parser plus the checker subset that `approve_when` needs: identifiers, selection and index, `==`/`!=`, `&&`/`||`/`!`, `has()`, string and bool literals, enum literals, the result type, limits | about 450–550 lines plus unit tests |
| A smaller subset parser that accepts only comparisons of paths with literals combined by `&&`, `||`, `!` and `has()`, rejecting everything else until the full checker exists | about 200–250 lines, at the cost of rejecting valid CEL for a while |
| The full checker for every expression kind in ir.md section 7, with `$ref` and type-closure resolution, built-in artifact schemas and nullable or `anyOf` schemas | about 900–1,100 lines plus unit tests |

Gaps the sketch exposed in the specifications:

- The built-in artifact types (`CommandResult`, `VcsRef`, `ReviewBundle`, `AgentSession`) have no published field schemas in the IR or the specifications, so `inputs.same.exit_code` cannot be typed until those schemas exist.
- Webhook `payload` and sensor `observation` have no declared schema (the `http` sensor's `body` is arbitrary JSON). `IR011` can type only the `inputs.*` side of `match` and `until`, and the rest stays `dyn`.

Relation to the decision: this confirms ADR-0006 and the stack table. The binding has no checker, kora's checker is required, and it has to include a parser.

## Untrusted expressions

Each case runs in a fresh child process with a 15-second kill timeout (`untrusted.py` and `untrusted_case.py`). Times exclude imports.

| Case | Binding 0.10.0 | cel-python 0.5.0 |
|---|---|---|
| `(` nested n deep | n ≤ 95 ok; 96 and above give `ValueError` ("Recursion limit of 96 exceeded"); n = 100,000 gives `PanicException` (a `BaseException`) | n = 50 ok; n = 100 gives `RecursionError` (catchable) |
| `[` nested n deep | n ≤ 96 ok; 97 and above give `ValueError` | n = 100 gives `RecursionError` |
| `a.b.b…` member chain | 10,000 ok; ~12,000 crashes the process with **SIGSEGV** while parsing | 1,000 gives `RecursionError` |
| `1 + 1 + …` chain | 10,000 ok; ~12,000 crashes with **SIGSEGV** while evaluating, and 30,000 while parsing | 1,000 gives `RecursionError` |
| `true && true && …` chain | 100,000 ok (0.26 s) | 1,000 gives `RecursionError` |
| 10 MB string literal; 10 MB string input with `contains` | ok, 0.03 s; ok, 0.002 s | ok, 1.1 s and 1.7 GB peak RSS; ok |
| `l.all(a, l.all(b, l.all(c, true)))`, \|l\| = 100 / 200 / 1,000 | 0.12 s / 0.96 s / **timeout** | 9.8 s / **timeout** / **timeout** |
| `size(l.map(x, x)) > 0`, \|l\| = 10,000 / 30,000 / 100,000 | 1.38 s / 12.3 s / **timeout** (quadratic; `filter` the same) | 0.27 s / 0.72 s / 2.28 s (linear) |
| `size(['ab'].map(a, a + a).map(…)…[0])` with 24 / 26 chained maps | 172 MB / 553 MB peak RSS in 0.05 s (doubles each step; 30 maps would need ~8.5 GB) | 104 MB / 296 MB |
| `s.matches('^(a+)+$')` and `'^(a\|aa)*$'`, s = 100,000 × `a` + `!` | false within 3 ms (linear-time `regex` crate) | false within 2 ms (RE2) |
| Backreference `(a)\1` | `RuntimeError`: backreferences are not supported | `re2` error |
| `a{1000}{1000}` | `RuntimeError`: compiled regex exceeds the 10 MiB size limit | `re2` error |

Crash thresholds of the binding, bisected in child processes (`crash_threshold.py`, `small_stack.py`):

```text
main   member survives ~11812 terms, crashes at ~12202 (returncode -11)
main   plus   survives ~11812 terms, crashes at ~12202 (returncode -11)
main   or     survives 200000
thread member survives ~23525 terms, crashes at ~24306 (returncode -10)
stack   256 KiB, member chain   300: KeyError survived
stack   256 KiB, member chain  1000: crashed rc=138
stack  1024 KiB, member chain  1000: KeyError survived
stack  1024 KiB, member chain  3000: crashed rc=138
```

A stack overflow in the Rust parser or evaluator kills the whole Python process; no exception reaches Python. A 4096-character limit on its own is not enough, because on a 256 KiB thread stack a member chain of 1,000 selections (about 2,000 characters) already crashes. An AST depth limit in the dozens is safe on every stack size measured.

The binding holds the GIL during evaluation, and a signal cannot interrupt it (`gil_and_alarm.py`; a background thread increments a counter every millisecond):

```text
binding: evaluation took 3.33s; background thread ticks during it: 1 (about 2665 expected if the GIL were released)
binding: SIGALRM at 0.5s interrupted evaluation after 3.29s
celpy: evaluation took 0.67s; background thread ticks during it: 92 (about 538 expected if the GIL were released)
celpy: SIGALRM at 0.5s interrupted evaluation after 0.50s
```

The binding releases the GIL only while parsing expressions of 32 bytes or more (0.10.0 changelog). Releasing it during evaluation is open issue #45. In-process, the binding offers no way to bound time or memory. An expensive expression stalls every thread of the process, including an asyncio event loop, until it finishes. A Python-level timeout fires only afterwards.

Bound data matters too. With an 888 KiB payload bound as `inputs`, `response.decision == 'approve'` takes 25 ms per evaluation, because the whole dictionary is converted on every call. Binding only `response` takes 1.7 µs. `Program.variables()` and `Context.set_variable_resolver()` let kora bind only the top-level names an expression references.

Consequences for kora, which ADR-0006 and ir.md do not address today:

1. The checker enforces a maximum source length, an AST depth limit well below the binding's parser limit of 96 (chained selections and binary operators count) and limits on comprehension count and nesting. It runs before the binding ever sees the expression, at `kora validate` and at deployment.
2. Comprehension cost depends on run-time data: quadratic `map` and `filter`, multiplicative nesting, exponential growth through chained `map` with `+`. Static limits alone cannot bound time and memory. One of these is still needed: evaluation in a separate worker process with a wall-clock timeout and a memory limit; a size bound on the lists bound into CEL (artifact payloads, sensor bodies); or a cost estimator. The scheduler is shared by many teams (FR-7.5), so in-process evaluation on its event loop is the riskiest placement.
3. The exception handling maps the binding's documented exception classes and `PanicException` to structured errors explicitly. A bare `except Exception` would miss `PanicException`, and the project rules forbid it anyway.

Relation to the decision: neither ADR-0006 nor the stack table makes a claim about untrusted-expression behaviour. These findings add requirements without contradicting the choice.

## Comparison with cel-python

Same 69-case corpus. The two libraries agree on 62 cases. For the 7 differences, the CEL project's own cel-cpp runtime (through `cel-expr-python` with `disable_check=True`) serves as the reference:

| Case | Binding | cel-python | cel-cpp reference | Correct |
|---|---|---|---|---|
| `has(response.missing.deeper)` | `KeyError` | `False` | error "Key not found in map" | binding |
| `size('héllo') == 5` | `False` (counts bytes) | `True` | `True` | cel-python |
| `inputs.same.exit_code == 0.0` | `True` | error (no overload) | `True` | binding |
| `inputs.metrics.cpu_seconds == 81` | `True` | error | `True` | binding |
| `inputs.metrics.cpu_seconds * 2` | error | `162.0` | error "No matching overloads" | binding |
| `1 + 1u` | error | `2` | error | binding |
| `1 == 1u` | `True` | error | `True` | binding |

cel-python lacks heterogeneous numeric equality and silently converts in `double * int` and `int + uint`. Expressions could then behave differently under another SDK's CEL implementation (NFR-6.1).

| Aspect | Binding | cel-python |
|---|---|---|
| Static checking | none; no syntax tree | none; `Environment(annotations=…)` only steers conversion of input values. `Environment.compile()` returns a `lark.Tree` with line and column, a usable base for a checker |
| Compile, `response.decision == 'approve'` / the sql-optimization branch condition | 6.5 µs / 14.0 µs | 38.5 µs / 82.5 µs |
| Evaluate the same two, small context | 0.14 µs / 0.60 µs with a reused `cel.Context`; 42 µs with a dict holding the whole corpus payload (conversion dominates) | 43 µs / 75 µs, plus 60 µs for `json_to_cel` of the same payload |
| One million comprehension iterations (`nested_all_3`, \|l\| = 100) | 0.12 s | 9.8 s |
| `import` time | 63 ms (loads typer, rich, prompt_toolkit, pygments) | 83 ms |
| Distribution | native wheels per platform and version; Rust needed where no wheel exists | `py3-none-any` wheel, but its dependencies `google-re2` (C++, no cp315 wheel anywhere) and `pendulum` (native, has cp315 wheels) are native |
| Python 3.15 | wheel on Linux x86_64; source build with an automatically downloaded Rust toolchain elsewhere | **not installable** on macOS arm64, Linux x86_64 or Linux aarch64: building `google-re2` fails with `'absl/strings/string_view.h' file not found` |
| Behaviour on hostile input | stack-overflow crash, GIL held, not interruptible | `RecursionError`, interruptible, but much slower |
| Maintenance | single maintainer, 5 releases in 2026, active upstream engine | cloud-custodian organisation, several maintainers, 173 stars, last release 0.5.0 on 2026-01-31, last commit on `main` 2026-07-17 |

Wheel-only resolution on all three platforms:

```text
== cel-python==0.5.0 (only-binary resolution)
platform                     3.11   3.12   3.13   3.14   3.15
aarch64-apple-darwin           ok     ok     ok     ok     NO
x86_64-manylinux_2_28          ok     ok     ok     ok     NO
aarch64-manylinux_2_28         ok     ok     ok     ok     NO
```

Relation to the decision: this confirms the stack table's note that `cel-python` has no static checking. As the fallback, cel-python offers no checker either, so kora's checker is needed in both cases. It also departs from current CEL semantics, is slower, and installs on fewer Python versions than the decided binding.

## Recommendation for the first slice

Keep the decided binding, `common-expression-language`, pinned to 0.10.0, and do not propose the `cel-python` fallback.

Reasons:

- It evaluates every expression the specifications use, and its semantics match the cel-cpp reference in 6 of the 7 cases where the two libraries differ. Its one deviation, string `size`, is irrelevant to `approve_when`.
- The fallback brings no static checking, so switching would not remove the main cost, kora's own parser and checker. It would add semantic deviations and make Python 3.15 impossible to install on every target platform.
- The binding's weaknesses are hostile-input crashes, the held GIL, quadratic macros and the 3.15 wheel gap. Each has a bounded mitigation that the first slice needs anyway.

Conditions for the slice:

1. Build kora's own parser and checker (`IR011`). For `approve_when` alone this is about 450–550 lines on a full CEL grammar, or about 200–250 lines for a temporary subset. The checker enforces the length, depth and comprehension limits before any expression reaches the binding.
2. Normalise payload values to the schema's CEL types before binding (`number` to `float`), and bind only the variables the expression references.
3. Map the binding's exception classes and `PanicException` to structured errors explicitly, and settle in the specification what an evaluation error means for each expression kind.
4. Decide where evaluation runs before untrusted graphs share a scheduler: a separate process with a timeout and a memory limit, or bounded data sizes. `approve_when` over a small form response is safe in-process once the checker limits apply.
5. In CI, expect the macOS and Linux aarch64 lanes on Python 3.15 to build from source with maturin's downloaded Rust toolchain until abi3 or cp315 wheels ship (issue #46). Run the CEL conformance suite with an explicit list of the binding's known deviations (string `size`, optional syntax).
6. Re-check when the binding moves to `cel` 0.15 (pending dependency bump; issue #57 on quadratic macros) and when `cel-expr-python` matures. The latter is the only Python package found with a real CEL type checker, but it needs protobuf message types for field-level typing of JSON objects.
