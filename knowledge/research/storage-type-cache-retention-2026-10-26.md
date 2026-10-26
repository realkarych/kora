# Storage lifetime allocation attribution

Checked 2026-10-26 against CPython 3.11.17 on macOS and provider revision `6790e4a10360edb6e40c3935833736b983f1a039`. This is a bounded attribution diagnostic, not a numerical qualification run or a change to production cleanup.

## Question and method

Earlier 100-cycle raw-storage diagnostics showed positive late retained allocations at JSON decoder construction and the asyncio callback boundary. Bounded Python-owner traversal did not identify the owner. An external Memray 1.20.0 capture of cycles 51–100 found native allocation frames `PyUnicode_FromString` → `PyObject_GetAttrString` → `_json.scanner_new`, including 36 retained allocations of 55 bytes. Native instrumentation substantially changes memory consumption; its totals are not comparable qualification baselines.

The [preregistered follow-up](https://github.com/realkarych/kora/issues/255#issuecomment-6404774423) used the unchanged real storage cycle, one warm-up, 100 raw 65,536-byte cycles and five-frame tracemalloc. GC preceded snapshots at baseline, cycles 50/100 and one diagnostic endpoint. Only after preserving ordinary cycle 100, it called the documented `sys._clear_type_cache()` reference-leak debugging operation and collected one final snapshot. Production and qualification never call that operation. The parent invoked the existing bounded `collect_process(child, 480)` helper; the execution receipt records exit zero and actual reap, but does not independently record elapsed time or demonstrate the watchdog deadline. Shared local host load was authorized; this is not isolated-runner evidence.

## Observation

The [complete changed traceback groups](storage-type-cache-comparisons-2026-10-26.json) retain positive and negative differences rather than only selected allocations. They were derived with standard `Snapshot.load` and `compare_to(..., "traceback")` from the four raw snapshots.

| Quantity | Observation |
| --- | --- |
| Current traced memory, cycle 50 → 100 | 54,506 → 57,639 bytes; +3,133 bytes |
| JSON decoder leaf, cycle 50 → 100 | +188 bytes; +6 allocations, with turnover |
| Asyncio callback leaf, cycle 50 → 100 | +3,245 bytes; +59 allocations |
| Endpoint intervention, total difference | −49,407 bytes |
| JSON decoder after intervention | 42,622 bytes / 682 allocations → zero |
| Asyncio callback after intervention | 6,905 bytes / 123 allocations → 360 bytes / 4 allocations |
| All remaining traced allocations | 8,232 bytes |

The experiment attributes the disappearing groups to a bounded interpreter cache. It does not establish an ordinary plateau, attribute every residual allocation or prove leak freedom for other workloads, platforms or historical producers. All ordinary retained bytes remain charged to the operational budgets; endpoint values never substitute for qualification samples.

## Owner, bound and release

CPython's [`scanner_new`](https://github.com/python/cpython/blob/v3.11.17/Modules/_json.c#L1095) retrieves the fixed attribute names `strict`, `object_hook`, `object_pairs_hook`, `parse_float`, `parse_int` and `parse_constant`. [`PyObject_GetAttrString`](https://github.com/python/cpython/blob/v3.11.17/Objects/object.c#L743) constructs a Unicode attribute name and decrements its temporary reference after lookup. The interpreter's [type cache](https://github.com/python/cpython/blob/v3.11.17/Include/internal/pycore_typeobject.h#L19) has 4,096 slots, owns name references and borrows value references. [Cache insertion](https://github.com/python/cpython/blob/v3.11.17/Objects/typeobject.c#L3556) can retain an exact Unicode name of at most 100 characters; its slot hash and equality use object identity, so repeated equal names can occupy different slots. Replacement releases the previous name. [Clearing and interpreter finalization](https://github.com/python/cpython/blob/v3.11.17/Objects/typeobject.c#L195) release cached names.

This source establishes an owner, a structural bound and release conditions for the attributed groups. The cache does not own storage payloads through those fixed scanner attribute names. The structural bound is not a measured byte ceiling or proof of cache saturation. CPython [documents the clearing operation](https://docs.python.org/3.11/library/sys.html#sys._clear_type_cache) specifically for dropping unnecessary references during reference-leak debugging. No production cache clearing, sample subtraction or acceptance change follows from this diagnostic.

## Provenance

The storage source digest is `9667215027c3e1435d057a9872d1466e4cfdad4eb1bcae273be0f7a8a2e75d08`, matching the completed storage campaign at the stated revision. The external diagnostic script SHA-256 is `e4d87ea597f28fbc0b6eb3b377d64e509d2ba5920d8092c800bb272e73e34a3a`. Its only endpoint intervention is the documented type-cache operation after cycle 100. The retained complete comparison file SHA-256 is `1afcae915e4dcc0146e6b4711e3a01b7890ff8a271beed88af5a9fdddf944fe3`.

| Raw snapshot | Bytes | SHA-256 |
| --- | ---: | --- |
| Baseline | 601 | `af2492894ab026c85fc196114f5caf738ef989c360c11a0f94ccc995aceb5d4a` |
| Cycle 50 | 15,192 | `85e4dc87ed1de2b06ef51686978ae6190fab17d5b05e6f872a2012b53edee173` |
| Cycle 100 | 16,028 | `a8cde9da80788b88cc091bfa5561ab10b2eaacbf56089455e24bfdc42f82007a` |
| Diagnostic endpoint | 3,249 | `595934c5c59772a5dfb62dd40575bb3f60d4951d03d2b14835ca24d6dd3a3caf` |

An independent performance reassessment inspected the external script, native frames, primary CPython sources, complete textual differences and raw snapshot identities. It checked each comparison's byte/count arithmetic and supports targeted attribution. Broader final lifetime qualification, revision-matched CI, numerical policy approval and human-facing documentation review remain separate obligations.
