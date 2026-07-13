# Live Lock-Holder Check for Auto-Ingest — Design

**Date:** 2026-07-13
**Issue:** #108

## Problem

A project-scoped `.mcp.json` pointing multiple sessions at the same `MINIGRAF_GRAPH_PATH`
means each session's MCP server spawns its own subprocess, and each one auto-starts
ingestion on boot. If two sessions are open at once (both perfectly legitimate, neither
orphaned), both processes race for the same `<graph>.lock` file. One loses the race and,
per existing self-heal/retry logic (#91/#99), either eventually errors out or the two
processes periodically contend for writes on their respective ingest cycles — wasteful at
best, a contributing factor to transient write failures at worst.

This is distinct from #104/#106, which cover a *dead* holder (orphaned process, stale
lock). Here neither process is orphaned — both are live, so a reactive retry only delays
the contention, it doesn't avoid it.

## Goal

Before starting ingestion (at boot or on an explicit `minigraf_ingest_git` call), check
whether `<graph>.lock` is already held by a **live** PID and, if so, skip starting
ingestion in this process entirely rather than attempting to acquire the lock and losing
the race. Report the skip (and the owning PID) via `minigraf_ingest_status` so a caller
in the second session can tell at a glance that ingestion is handled elsewhere.

## Design

### `_live_lock_holder_pid(path: str) -> Optional[int]`

New helper, placed near `_clear_stale_lock` (mcp_server.py:772). Reads `<path>.lock`
directly — no `open()` attempt on the DB, so this check never itself contends for the
lock.

- No lock file, or content isn't a bare integer → `None`.
- Parsed PID equals our own `os.getpid()` (mirrors the Rust `FileLock::acquire`'s
  `pid == our_pid` self-lock case — can happen if a previous run in this same process
  leaked its handle) → `None`, not a blocker.
- `os.kill(pid, 0)`: `ProcessLookupError` → holder is dead → `None`. Success,
  `PermissionError`, or any other `OSError` → can't confirm death → conservatively
  treat as alive, return `pid`. This mirrors `_clear_stale_lock`'s existing bias
  ("leave it" when uncertain).

This is a best-effort, inherently racy (TOCTOU) fast-path — like the advisory lock
itself. It dodges the *common* case (two sessions both alive at boot) but does not
replace the existing retry/self-heal machinery, which still runs if the race is lost
anyway (e.g. a third process grabs the lock between this check and the real open).

### Call site 1 — boot auto-start (`main()`, mcp_server.py:3668)

Before `asyncio.create_task(_run_ingestion(...))`, call
`_live_lock_holder_pid(_get_graph_path())`. If it returns a PID: don't create the task;
set `_ingest_progress["status"] = "skipped"` and `_ingest_progress["owner_pid"] = pid`;
print a one-line notice to stderr (matching the file's existing ad hoc
`print(..., file=sys.stderr)` diagnostics, e.g. `_orphan_watchdog`-adjacent code has no
logger to hook into).

### Call site 2 — manual trigger (`handle_minigraf_ingest_git`, mcp_server.py:3281)

Same check, placed immediately after the existing "ingestion already in progress" guard
(cheapest check first). If blocked: return
`{"ok": False, "error": "ingestion already owned by live process (pid <N>)", "owner_pid": <N>}`
without creating a task, and set `_ingest_progress` the same way as call site 1.

### Status reporting

Add `"owner_pid": None` to all three `_ingest_progress` initializer sites (module-level
default, `main()`, `handle_minigraf_ingest_git`). `handle_minigraf_ingest_status` already
spreads `_ingest_progress` into its response dict, so `owner_pid` is exposed for free —
no change needed there beyond the new key existing.

New `status` value: `"skipped"`, added alongside the existing
`idle/running/complete/error/stopped` vocabulary.

### No auto-retry

A skip is permanent for that server process's lifetime — mirrors how `stopped` requires
an explicit next `minigraf_ingest_git` call (or a fresh server boot) to resume. Periodic
re-check/auto-retry once the other holder releases the lock is out of scope (that's
reactive retry/backoff territory, adjacent to #106, not "avoid racing in the first
place").

### Docs

- `minigraf_ingest_status` tool description (mcp_server.py:3561): add `skipped` to the
  status list.
- `SKILL.md`: extend the `status` vocabulary line (currently documents `stopped`) to
  cover `skipped` + `owner_pid`, and note in the `minigraf_ingest_git` section that a
  skip is possible when another live session owns the graph.

## Affected functions

| Function | Change |
|---|---|
| `_live_lock_holder_pid` | New — reads lock file directly, returns live holder PID or `None` |
| `main` | Check before creating the auto-ingest task; skip + record `owner_pid` if blocked |
| `handle_minigraf_ingest_git` | Same check before creating the task; return `ok: False` + `owner_pid` if blocked |
| `_ingest_progress` (module default, and the two reset sites) | Add `owner_pid: None` key |
| `handle_minigraf_ingest_status` | No code change — `owner_pid` propagates via existing dict spread |

## Testing

Following `TestGetDbLockRetry`'s patterns (tests/test_mcp_server.py:96):

- `_live_lock_holder_pid`: no lock file → `None`; unparsable content → `None`; dead PID
  → `None`; live PID (`os.getpid()` of the test process) → that PID; lock file
  containing our own PID → `None`.
- Boot path: auto-ingest task is not created, and `_ingest_progress` reflects
  `skipped`/`owner_pid` when a live holder is present at `main()` startup.
- `handle_minigraf_ingest_git`: returns `ok: False` with `owner_pid` and does not create
  an ingest task when a live holder is present; unaffected (existing behavior) when no
  lock file exists or the holder is dead.
- `handle_minigraf_ingest_status`: surfaces `owner_pid` after a skip.

## Non-goals

- Distributed coordination / leader election across sessions — this is a best-effort
  check, not a guarantee (TOCTOU races remain possible and fall back to existing
  retry/self-heal).
- Automatic retry once the other holder's lock is released (manual `minigraf_ingest_git`
  or a fresh server boot is the way back in).
- Changing `minigraf_ingest_status`'s handling of the `running`/`complete`/`error` graph
  read fallback — unrelated to this change.
