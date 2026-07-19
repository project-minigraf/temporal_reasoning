# At-Scale Code-Graph Benchmark

See issue #120 and `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.
Observational only -- no pass/fail thresholds.

## Ingestion Run — 20260719T074053Z

- Repo: `.` @ `HEAD`

| Metric | Value |
|---|---|
| Commits ingested | 498 |
| Final status | complete |
| Wall-clock | 78.87s |
| Throughput | 378.9 commits/min |
| Peak RSS | 248528 KB |
| Graph size | 45801472 bytes |
| Fact-index size | 60080128 bytes |
| Status-query latency (min/p50/p99/max) | 0.0ms / 0.0ms / 0.0ms / 0.1ms |
| Graph-query latency (min/p50/p99/max) | 0.0ms / 35.0ms / 277.5ms / 305.0ms |
