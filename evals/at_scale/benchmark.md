# At-Scale Code-Graph Benchmark

See issue #120 and `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.
Observational only -- no pass/fail thresholds.

The "cross-layer" ground-truth category (entries 5-6) is a genuine single-query
graph-level join: it binds the seeded decision's own `:db/valid-from` as an output
variable via minigraf's `:db/valid-from`/`:db/valid-to` pseudo-attributes, then
filters structural facts by comparing each one's own `:db/valid-from` against it
in the same query (`[(< ?fvf ?dvf)]` / `[(> ?fvf ?dvf)]`). If the seed decision
fact were silently missing, `?dvf` would never bind and `count-distinct` over the
resulting empty join returns `0`, not an error -- entry 5's own expected answer is
already `0`, so it can't distinguish a working join from a silently broken one on
its own; entry 6 (expects `12`, degrades to `0` if the join breaks) is the one
that actually proves the join fired. Run both together. This capability exists
in minigraf and is already used internally by `mcp_server.py` (`_preload_known_deps`,
`_rebuild_index_from_graph`), but is not yet documented in `SKILL.md` — see #165.
An earlier version of this note incorrectly claimed no such mechanism existed and
shipped entries 5-6 as a weaker two-query valid-time-bracket workaround instead;
that was wrong and has been corrected here.

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

## Query Correctness Run — 20260719T081810Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 7.4ms | 2.8ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 2.1ms | 7.5ms |
| 4 | dependency-impact | PASS | 14.4ms | 5.4ms |
| 5 | cross-layer | PASS | 9.6ms | 6.5ms |
| 6 | cross-layer | PASS | 9.6ms | 3.0ms |

## Query Correctness Run — 20260719T082707Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 6.6ms | 3.1ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 2.0ms | 7.0ms |
| 4 | dependency-impact | PASS | 14.1ms | 5.6ms |
| 5 | cross-layer | PASS | 9.4ms | 6.8ms |
| 6 | cross-layer | PASS | 9.4ms | 3.1ms |

## Query Correctness Run — 20260719T183705Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 6.8ms | 3.1ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 1.8ms | 6.9ms |
| 4 | dependency-impact | PASS | 14.1ms | 6.1ms |
| 5 | cross-layer | PASS | 571.3ms | 6.0ms |
| 6 | cross-layer | PASS | 551.7ms | 2.9ms |

## Query Correctness Run — 20260719T184747Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 6.8ms | 3.1ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 1.8ms | 8.0ms |
| 4 | dependency-impact | PASS | 14.5ms | 5.3ms |
| 5 | cross-layer | PASS | 568.4ms | 6.2ms |
| 6 | cross-layer | PASS | 549.6ms | 2.9ms |
