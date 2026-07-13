# Extended Ingestion Lock Retry + Status Staleness — Design

**Date:** 2026-07-13
**Issue:** #106

## Problem

When the MCP server's auto-start ingestion task (`_run_ingestion`) fails to acquire the
graph's advisory lock at startup — e.g. because another process (often an orphan, #104's
failure mode) still holds it — the startup lock acquisition (`_load_ingestion_preload_state`
→ `get_db()`) only retries for ~1.55s total (the shared `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE`
budget used by every synchronous DB-open call site). Real orphan cleanup can take 30s+
(SIGTERM grace period before SIGKILL), so this budget is routinely exhausted well before
the lock actually frees. Once exhausted, `_run_ingestion` sets a permanent `"error"` state
that `minigraf_ingest_status` echoes byte-for-byte forever, still citing the original
(now-dead) holder PID, with no self-healing and no signal that a retry is now safe. The
only recovery path is a manual `minigraf_ingest_git` call that nobody is prompted to make.

#108 (already shipped) added a `"skipped"` status with the same problem: it's permanent
for the server process's lifetime by design, deferring exactly this staleness question to
#106.

## Goal

1. Give the startup/manual-trigger lock acquisition enough patience to self-heal past
   typical orphan-cleanup windows without ever reaching a terminal state.
2. If a terminal state (`"error"` or `"skipped"`) is reached anyway, let
   `minigraf_ingest_status` tell the caller whether it's stale (the blocking condition is
   now gone) instead of blindly re-echoing a dead PID forever. No automatic retry is
   triggered — staleness is informational, surfaced so a caller knows a manual
   `minigraf_ingest_git` retry is worth making.

## Design

### Part A — Extended startup lock retry

New constants, separate from the existing `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE` (which stay
untouched — they gate synchronous per-request paths like `call_tool()`, where long blocking
would be harmful):

```python
_INGEST_LOCK_RETRY_BASE = 0.05    # seconds; matches the existing base for consistency
_INGEST_LOCK_RETRY_CAP = 15.0     # seconds; per-attempt sleep never exceeds this
_INGEST_LOCK_RETRY_BUDGET = 120.0 # seconds; total time before giving up
```

New function `_open_db_at_with_extended_retry(path: str) -> MiniGrafDb`, placed near
`_open_db_at_with_retry` (mcp_server.py:818). Time-budget-based (not attempt-count-based)
capped-exponential-backoff loop, reusing the existing `_try_open_with_self_heal` for every
attempt — so a dead holder is still cleaned up mid-retry exactly as today; only the overall
patience changes:

```python
def _open_db_at_with_extended_retry(path: str) -> MiniGrafDb:
    deadline = time.monotonic() + _INGEST_LOCK_RETRY_BUDGET
    delay = _INGEST_LOCK_RETRY_BASE
    last_exc: Optional[Exception] = None
    while True:
        try:
            return _try_open_with_self_heal(path)
        except Exception as e:
            if not _is_lock_error(e):
                raise
            last_exc = e
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _INGEST_LOCK_RETRY_CAP)
    assert last_exc is not None
    raise last_exc
```

`_load_ingestion_preload_state` (mcp_server.py:2879-2899) changes its `db = get_db()` call
to `db = _open_db_at_with_extended_retry(_graph_path or _get_graph_path())`. This is the
only call site affected — `get_db()` itself, `_ensure_db_async()`, and every other caller
keep the existing ~1.55s budget.

### Part B — Extract `_pid_is_alive(pid: int) -> bool`

`_clear_stale_lock` (mcp_server.py:772-790) and `_live_lock_holder_pid` (mcp_server.py:793,
added by #108) each inline the identical conservative liveness check (`os.kill(pid, 0)`;
`ProcessLookupError` → dead; anything else, including success, → presumed alive). Part C
needs the same check a third time, so extract it once, placed immediately before
`_clear_stale_lock`:

```python
def _pid_is_alive(pid: int) -> bool:
    """Conservative liveness check: only a positive ProcessLookupError counts as
    dead. Uncertain cases (PermissionError, other OSError) are treated as alive
    rather than risking a false "safe to proceed" signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True
```

Refactor `_clear_stale_lock` and `_live_lock_holder_pid` to call `_pid_is_alive` instead of
inlining the check. Behavior-preserving — both are already covered by existing tests
(`TestGetDbLockRetry`, `TestLiveLockHolderPid`), which double as regression guards for the
refactor.

### Part C — Staleness annotation on `minigraf_ingest_status`

`_run_ingestion`'s failure path (mcp_server.py:3305-3311) adds an `error_at` timestamp
alongside the existing assignments, reusing the shared `_now_utc_ms()` helper
(mcp_server.py:1942):

```python
except Exception as e:
    _ingest_progress["status"] = "error"
    _ingest_progress["error"] = str(e)
    _ingest_progress["error_at"] = _now_utc_ms()
    _db = None
```

`_ingest_progress` gains a permanent `"error_at": None` default key across all reset sites
(module-level default, `main()`'s reset, `handle_minigraf_ingest_git`'s reset) — same
pattern #108 used for `owner_pid`.

`handle_minigraf_ingest_status` (mcp_server.py:3354) gets purely additive logic, evaluated
before the existing `status != "running"` graph-read block:

```python
if _ingest_progress["status"] == "error":
    holder_pid = _stale_lock_holder_pid(_ingest_progress.get("error") or "")
    if holder_pid is not None:
        result["stale"] = not _pid_is_alive(holder_pid)
elif _ingest_progress["status"] == "skipped":
    owner_pid = _ingest_progress.get("owner_pid")
    if owner_pid is not None:
        result["stale"] = not _pid_is_alive(owner_pid)
```

`_stale_lock_holder_pid` (mcp_server.py:766-769) already accepts anything coercible via
`str(exc)` — passing the already-`str` `_ingest_progress["error"]` directly works
unmodified (`str()` on a `str` is a no-op), no signature change needed.

`stale` is omitted entirely when it can't be computed (non-lock error with no extractable
PID, or a terminal state with no PID at all) rather than defaulting to `false` — an absent
field is an honest "can't tell," whereas a `false` default would falsely imply "confirmed
not stale."

### Docs

- `minigraf_ingest_status` tool description (mcp_server.py, near line 3600s): mention
  `error_at` and the new optional `stale` field, and that `stale: true` means the caller
  can retry via `minigraf_ingest_git` now.
- `SKILL.md`: extend the status-vocabulary section with the same explanation, following the
  existing `stopped`/`skipped` documentation pattern.

## Affected functions

| Function | Change |
|---|---|
| `_pid_is_alive` | New — shared conservative liveness check |
| `_clear_stale_lock` | Refactor to use `_pid_is_alive` (no behavior change) |
| `_live_lock_holder_pid` | Refactor to use `_pid_is_alive` (no behavior change) |
| `_open_db_at_with_extended_retry` | New — time-budget capped-backoff retry for the startup lock path only |
| `_load_ingestion_preload_state` | Switch from `get_db()` to `_open_db_at_with_extended_retry` |
| `_run_ingestion` | Add `error_at` timestamp on the failure path |
| `_ingest_progress` (module default + 2 reset sites) | Add `error_at: None` key |
| `handle_minigraf_ingest_status` | Add `stale` computation for `error`/`skipped` states |

## Testing

- `_pid_is_alive`: dead PID → `False`; live PID → `True`; permission-denied → `True`
  (conservative). These mostly already exist as inline cases in `TestGetDbLockRetry` and
  `TestLiveLockHolderPid` — after the refactor, verify those still pass unchanged (proving
  behavior preservation) and add direct unit tests for the extracted function itself.
- `_open_db_at_with_extended_retry`: succeeds after N lock-contention retries within budget
  (mock `time.monotonic`/`time.sleep` to avoid a real 2-minute test); gives up and raises
  after the budget is exhausted; non-lock errors propagate immediately without retrying;
  self-heals a stale (dead-holder) lock mid-loop exactly like the existing retry path.
- `_run_ingestion`: failure path sets `error_at` to a well-formed timestamp alongside
  `status`/`error`.
- `handle_minigraf_ingest_status`: `stale: true` when the cited/owner PID is dead;
  `stale: false` when alive; `stale` key absent when status is `error` with no extractable
  PID, and absent for any status other than `error`/`skipped`.

## Non-goals

- No automatic re-triggering of ingestion when staleness is detected — purely
  informational, consistent with #108's "manual retry only" decision for `"skipped"`.
- No change to the general-purpose `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE` budget used by
  `get_db()`/`_ensure_db_async()`/other synchronous call sites.
- No change to how `"complete"`/`"stopped"`/`"running"`/`"idle"` are reported — staleness
  only applies to the two terminal-and-potentially-wrong states.
