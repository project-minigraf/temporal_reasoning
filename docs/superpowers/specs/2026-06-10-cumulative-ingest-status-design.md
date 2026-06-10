# Cumulative Ingest Status — Design

**Date:** 2026-06-10

## Problem

`vulcan_ingest_status` reports `processed` and `total` for the current run only.
A repo ingested over two interrupted runs (e.g., 462 + 555 = 1017 commits) shows
`processed: 555, total: 555` even though all commits are present — misleading.

## Goal

Real-time cumulative progress: `processed` and `total` reflect the full git history
across all runs, not just the current one.

## Design

### Persistent state

Extend `:ingestion/last-run-at` entity (written by `_last_run_write`) with one new
attribute: `:total-ingested` — the cumulative count of commits processed across all
runs up to and including the just-completed run.

### Run start (`_run_ingestion`)

1. Read `prior_ingested` via new `_total_ingested_query(db)` (0 on first run).
2. Run `git rev-list --count HEAD` → `repo_total`.
3. Set `_ingest_progress["total"] = repo_total`.
4. Set `_ingest_progress["processed"] = prior_ingested` (seeds the counter).

The existing `_ingest_progress["processed"] += 1` loop then naturally accumulates
to a cumulative total in real time.

### Run end

Pass final `_ingest_progress["processed"]` to `_last_run_write`; store as
`:total-ingested` on the last-run entity.

### Status handler

No changes needed for the in-progress case — `processed`/`total` are already right.
For idle/complete, the existing graph read already fetches `last_commit`/`last_run_at`;
extend it to also return `total_ingested` from the graph for display after a run.

## Affected functions

| Function | Change |
|---|---|
| `_total_ingested_query` | New — reads `:total-ingested` from graph |
| `_last_run_write` | Add `total_ingested: int` param; store `:total-ingested` |
| `_run_ingestion` | Seed `_ingest_progress` from prior count + repo total at start; pass final count to `_last_run_write` |
| `handle_vulcan_ingest_status` | Extend graph read to expose `total_ingested` |

## Non-goals

- Backfilling `total-ingested` for existing deployments (first post-fix run will
  set it to the current watermark position; prior runs are not retroactively counted).
- Changing what constitutes a "processed" commit.
