# Lineage point-query cache (#239) — design

Status: DRAFT, awaiting review. Branch `239-lineage-cache` (from master `15bcb6d`).

## Problem

minigraf 2.0.2 (#380) made a bound-entity point query `[<e> <attr> ?v]` cost
O(that attribute's own history) instead of O(the entity's). The attributes
lineage reconciliation rewrites — `:introduced-by`, `:modified-in` — keep
most of their cost; the structural fix (minigraf#379, v8 keys) ships only in
v3.0.0, which our `<3.0.0` cap excludes. The user's decision (2026-09-27): an
in-repo cache.

## Measured before designing (`evals/at_scale/probe_lineage_cache_hit_rate.py`)

One real full-history ingestion of this repo at `15bcb6d` (1,039 commits,
`status: complete`), every `_db_execute` recorded in order, then candidate
policies replayed offline. Every simulated hit is compared with the result
the real query returned at that moment.
Data: `evals/at_scale/results/239-lineage-cache-hit-rate.json`.

Point queries on the four watched attributes: 2,640,158 calls, **652.6 s**
of exec time (instrumented wall 2,406 s; the recording roughly doubles wall,
so the share is indicative only — the real delta comes from the A/B below).

| policy | scope | hits | saved | mismatches |
|---|---|---:|---:|---:|
| invalidate on write | whole run | 81.0% | 444.4 s | 0 |
| invalidate on write | per handle | 39.5% | 124.8 s | 0 |
| **write-through** | **whole run** | **99.4%** | **650.4 s** | **0** |
| write-through | per handle | 42.5% | 140.2 s | 0 |
| never invalidate (negative control) | whole run | 99.4% | — | **482,079** |

The negative control proves the oracle can fail. Write-through's hit count
equals the never-invalidate ceiling (minus 1): every remaining miss is a
cold first read. Per attribute (write-through, run scope): `:introduced-by`
330.9 s of 331.2, `:modified-in` 178.8 of 180.1, lineage marker `:entity`
115.1 of 115.5, `:ident` 25.5 of 25.9.

**Scope is the whole decision.** Per-handle scope throws away 510 s, because
the run opened 319 handles (Stage A/B lease windows, #280). A per-handle
cache is the naive safe choice — between our drop and our next open, another
PROCESS (an auto-memory hook, #366) may have written. So the cache needs an
exact cross-process "did anyone write?" signal.

## Cross-process validity: the graph header's tx counter

Measured with minigraf 2.0.2 (scratch experiments, reproduced in the tests):

* Graph header bytes 24..32 are `last_checkpointed_tx_count` (u64 LE,
  `FileHeader`, minigraf `src/storage/mod.rs`). It advances on every
  checkpointed transact AND retract, including a foreign process's.
* A foreign process that writes and is killed before its drop leaves a
  non-empty `<graph>.wal`; the next open replays it.
* A foreign open/close, or a foreign read, changes neither.
* Our own drop checkpoints, so right after it the WAL is absent.

So **stamp = (format version @4..8, tx count @24..32, WAL size or absent)**,
recorded right after our 1 -> 0 drop and compared right after the next
0 -> 1 open (while we hold the kernel lock, so nobody can write in between).
Equal: keep the cache. Different, unreadable, or a format version other than
7: clear it. This fails safe — every doubt costs one cold refill, never a
wrong answer — and it is exact, unlike mtime (coarse-grained timestamps
could miss a same-tick, same-size write).

This couples production code to minigraf's v7 header layout, which the
tests already do (`_keep_rightmost_leaf`, #336). The version check makes a
layout change degrade to per-handle scope rather than to wrong answers, and
a test pins the counter's behaviour against the installed minigraf.

## Design

`_LineageQueryCache` in `mcp_server.py`, one module-level instance.

* **Keys:** `(entity, attribute)` for keyword entities and
  `attribute ∈ {:introduced-by, :modified-in, :entity, :ident}`. Values: the
  set of values a current-time point query returns.
* **Reads:** one helper, `_point_query_values(db, entity, attr)`, used by
  `_entity_introduced_by_values_query`, `_entity_ident_is_live`,
  `_lineage_is_provisional`, and `_correction_sweep_apply`'s two inline
  `:modified-in` queries. Miss -> run the real query, store. Returns a copy.
* **Writes:** `_transact`/`_retract` — the only graph writers in the module —
  update the cache for every watched triple they carry:
  * current transact (`valid_to is None`, `valid_from <= now`): add the value;
  * retract: remove the value (measured: minigraf's retract removes a value
    even when it was asserted twice);
  * anything else (bounded window, future `valid_from`): drop the key;
  * a non-keyword entity (`#uuid`): drop every key on that attribute;
  * the write raised: drop every touched key.
* **Atomicity:** `_db_native_lock` becomes an `RLock`, and both the read
  (check, query, store) and the write (execute, update) run inside one hold
  of it. Otherwise a reader's fill can land after a concurrent writer's
  update and resurrect a stale value. Nothing relies on the lock being
  non-reentrant today (to verify during implementation).
* **Active only during ingestion.** `_run_ingestion` enables it and clears
  and disables it in `finally`. Outside a run, the helpers query directly and
  writes skip maintenance. This bounds memory in the long-lived MCP server
  (~17k keys on this repo) and keeps `call_tool` behaviour unchanged.
* **Cleared** on a stamp mismatch, `bind_path`, `reset`/`_reset_db_state`,
  and at run start and end.
* **Verify mode:** a module constant (`_LINEAGE_CACHE_VERIFY`, patched on in
  `tests/conftest.py`) makes every hit also run the real query and raise on a
  difference. The whole suite then acts as an oracle, which is how the
  probe's zero-mismatch result keeps being re-proven after this ships.

### Residuals (stated, not fixed)

* **Valid-time drift.** A fill reflects "now" at fill time. A fact already
  in the graph with a FUTURE `:valid-from` (a commit dated ahead of the
  clock) becomes visible to a real query once the clock passes it, but not
  to the cache. It lasts no longer than the cache's scope (one run).
  Write-through never adds a future-dated value — it drops the key instead.
* **Header layout.** A minigraf format change is caught by the version
  check, not by the tests alone.

## Verification

1. **Suite in verify mode:** every hit is checked against the real query.
2. **Ablations:** remove write-through on retract / on transact / the stamp
   check / the RLock critical section. Each must redden a named test (a
   real cross-process writer for the stamp test, as `TestHooksCatchLeaseWindowReleases` does).
3. **Write-sequence parity:** `probe_forward_apply_write_parity.py`, cache on
   vs off. A cache that changes an answer changes what ingestion writes.
4. **Same-batch A/B, interleaved:** cache off vs on, full history, with
   `fact_audit` divergence 0 and equal commit counts, reusing
   `probe_minigraf_upgrade_cost.py`'s arm/batch harness.

## Out of scope

The sweep's `_retract` cost (643 s on 2.0.2, the largest remaining DB cost).
Batching it runs into minigraf#287's pending-index collapse for retracts that
share `(entity, attribute)`, so it needs its own measurement — a follow-up
issue.
