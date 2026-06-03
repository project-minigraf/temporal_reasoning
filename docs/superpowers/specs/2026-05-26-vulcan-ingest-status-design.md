# Design: Fix `vulcan_ingest_status` to reflect hook-driven ingestion

**Date:** 2026-05-26

## Problem

`handle_vulcan_ingest_status` only reads in-memory `_ingest_progress`, which is:
- reset on every MCP server restart
- never updated when ingestion runs via the `UserPromptSubmit` hook (a separate subprocess)

This makes the tool useless for verifying whether the hook-driven ingest fired.

## Goal

After this change, calling `vulcan_ingest_status` returns the wall-clock time of the last completed ingestion run and the final commit hash, regardless of whether ingestion ran in-process or via the hook subprocess. If `last_run_at` is later than session start time, ingestion ran successfully this session.

## Schema

A new named entity written to the graph after each successful ingestion run:

```
[:ingestion/last-run-at :entity-type  :type/ingestion]
[:ingestion/last-run-at :ident        ":ingestion/last-run-at"]
[:ingestion/last-run-at :description  "last ingestion run timestamp"]
[:ingestion/last-run-at :last-run-at  "<ISO 8601 UTC wall-clock time>"]
[:ingestion/last-run-at :last-commit  "<final commit hash>"]
```

No `:valid-from` — this entity has no real-world valid-time axis. Written once per run on success, after the commit loop completes.

## Changes

### `_run_ingestion` (`mcp_server.py`)

After `_ingest_progress["status"] = "complete"`, write the `:ingestion/last-run-at` entity using `datetime.utcnow().isoformat() + "Z"` and the hash of the last processed commit.

Only written on success. If the run processes zero commits (already up-to-date), still write the entity so callers know ingestion was attempted.

### `handle_vulcan_ingest_status` (`mcp_server.py`)

When in-memory status is `"running"`, return in-memory progress as before.

When not running, open the DB and query:

```datalog
[:find ?t ?h
 :any-valid-time
 :where [:ingestion/last-run-at :last-run-at ?t]
        [:ingestion/last-run-at :last-commit ?h]]
```

Return shape:

```json
{
  "ok": true,
  "status": "idle",
  "processed": 0,
  "total": 0,
  "current_commit": "",
  "error": null,
  "last_run_at": "2026-05-26T10:00:00Z",
  "last_commit": "3e5501826eb3a75f47fcf06a8246f6b14914a2bb"
}
```

`last_run_at` and `last_commit` are `null` if the graph has no entry yet.

## Tests

- Update existing idle-state test: assert `last_run_at` and `last_commit` are `null` when DB returns no results.
- Add test: DB returns a row; assert `last_run_at` and `last_commit` pass through correctly.
- Add test: `_run_ingestion` writes the `:ingestion/last-run-at` entity with correct fields on success.
- Existing running-state test: assert `last_run_at`/`last_commit` are absent (or null) while status is `"running"`.

## Out of scope

- Fixing `_watermark_query` returning non-latest hash (separate issue).
- Surfacing per-commit ingestion timestamps.
