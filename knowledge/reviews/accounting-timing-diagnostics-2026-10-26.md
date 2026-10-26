# Accounting timing diagnostics

The project owner approved the additional plan in [issue #255](https://github.com/realkarych/kora/issues/255#issuecomment-6408655018) on 2026-10-26. The 48 accounting p95 reference values remain available, but their 144 per-child comparisons are diagnostics. These values no longer guarantee microbenchmark latency. No API or engine accounting consumer currently supplies an operational latency requirement for this method.

Each comparison uses its own validated child samples, independently recomputed p95 and case record count to express both observation and reference in seconds. The union output retains child and workload identity, observation, reference and whether the reference was exceeded. Pooling children would conceal individual outliers and is not used for these comparisons.

The hard gates retain finite nonnegative timing capture, exact raw-output/process binding, summaries and sample inventories, all 126 child outcomes, the 262144-byte heap and 8 MiB RSS growth limits, actual resource closure and every existing execution/output/cleanup deadline. The union still requires 352 tests, 1211 gates and 10536 samples. Pricing, CEL, other IR and storage acceptance methods are unchanged.

The historical run at revision `8387fe959033966c9638d8b560f405b64ee57702` remains failed. Its `cost_source_8191` child p95 was 562.500 microseconds against a 488.28125-microsecond reference; the other two observations were 202.346 and 80.212 microseconds. Only 20 of 27 timing-child outcomes were captured, so the incomplete run cannot become complete evidence through this change. These observations do not establish that the outlier is harmless or identify its cause.

Revisit the timing method when the first real accounting API or engine consumer defines an operational latency requirement, or a reproducible accounting latency defect demonstrates one. Define its workload, environment and failure decision before adopting a new blocking threshold.
