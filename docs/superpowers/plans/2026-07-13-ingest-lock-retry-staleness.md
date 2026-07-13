# Extended Ingestion Lock Retry + Status Staleness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the ingestion startup/manual-trigger lock acquisition enough patience to self-heal past typical orphan-cleanup windows instead of failing after ~1.55s, and — if a terminal `error`/`skipped` state is reached anyway — let `minigraf_ingest_status` report whether it's stale (the blocking PID is now dead) instead of echoing it forever.

**Architecture:** A new time-budgeted retry function (`_open_db_at_with_extended_retry`), separate from the existing ~1.55s `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE` budget used everywhere else, is wired into only the one-time startup preload (`_load_ingestion_preload_state`). A shared `_pid_is_alive` helper (extracted from two existing near-duplicate liveness checks) backs a new, purely-informational `stale` field that `minigraf_ingest_status` computes fresh on every poll for `error`/`skipped` states, plus an `error_at` timestamp on the failure path.

**Tech Stack:** Python 3, asyncio, pytest + pytest-asyncio, unittest.mock.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-13-ingest-lock-retry-staleness-design.md` (issue #106).
- The extended retry budget (`_INGEST_LOCK_RETRY_BASE = 0.05`, `_INGEST_LOCK_RETRY_CAP = 15.0`, `_INGEST_LOCK_RETRY_BUDGET = 120.0`, all seconds) is a **separate** set of constants from the existing `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE` (mcp_server.py:73-74). Those existing constants and every call site that uses them (`get_db()`, `_ensure_db_async()`, `_open_db_at_with_retry()`) must be left untouched — they gate synchronous per-request paths (`call_tool()`, `IndexCache` rebuild) where long blocking would be harmful. Only `_load_ingestion_preload_state` switches to the new extended-retry function.
- `_open_db_at_with_extended_retry` must reuse `_try_open_with_self_heal` for each attempt (not duplicate its self-heal logic) — a dead holder's lock is still cleaned up mid-retry exactly as today.
- `_pid_is_alive`'s conservative bias must be preserved exactly: only `ProcessLookupError` counts as dead; success, `PermissionError`, or any other `OSError` count as alive. This is a pure refactor of existing logic in `_clear_stale_lock` (mcp_server.py:772) and `_live_lock_holder_pid` (mcp_server.py:793) — behavior must not change, and their existing tests (`TestGetDbLockRetry`, `TestLiveLockHolderPid`) must continue passing unmodified as regression proof.
- `stale` is **purely informational** — computing it (or `error_at`) must never trigger an automatic retry of ingestion. This mirrors #108's "manual retry only" decision for `skipped`.
- `stale` must be **omitted** from the response (not defaulted to `false`) when it can't be computed (a non-lock `error` with no extractable PID, or any status other than `error`/`skipped`) — an absent field means "can't tell," a `false` default would falsely claim "confirmed not stale."
- `_stale_lock_holder_pid` (mcp_server.py:766) already accepts anything coercible via `str(exc)` — passing the already-`str` `_ingest_progress["error"]` directly works unmodified (`str()` on a `str` is a no-op); do not change its signature.
- All new tests must be hermetic and fast: mock `time.sleep`/`time.monotonic` for any test exercising the extended retry's backoff/budget — a real 120s wait in a test is not acceptable.

---

### Task 1: Extract `_pid_is_alive`; refactor `_clear_stale_lock` and `_live_lock_holder_pid`

**Files:**
- Modify: `mcp_server.py:772-823` (insert `_pid_is_alive` before `_clear_stale_lock`; refactor both `_clear_stale_lock` and `_live_lock_holder_pid` to use it)
- Test: `tests/test_mcp_server.py` (new `TestPidIsAlive` class, inserted after `TestLiveLockHolderPid` which ends at line 265, before `class TestMinigrafQuery:` at line 268)

**Interfaces:**
- Produces: `_pid_is_alive(pid: int) -> bool` — module-level function in `mcp_server.py`. Returns `True` unless the PID is confirmed dead via `ProcessLookupError`.
- Consumed internally by this task's own refactor of `_clear_stale_lock` and `_live_lock_holder_pid`. Task 4 will also consume it directly (not your concern).

- [ ] **Step 1: Write the failing tests**

Add this new class to `tests/test_mcp_server.py`, immediately after `TestLiveLockHolderPid` (after line 265), before `class TestMinigrafQuery:` (line 268):

```python
class TestPidIsAlive:
    """Unit tests for _pid_is_alive — shared conservative liveness check
    extracted from _clear_stale_lock and _live_lock_holder_pid (#106)."""

    def test_dead_pid_returns_false(self):
        import mcp_server
        assert mcp_server._pid_is_alive(999999) is False  # not running on any reasonable test machine

    def test_live_pid_returns_true(self):
        import mcp_server
        assert mcp_server._pid_is_alive(os.getpid()) is True

    def test_permission_error_treated_as_alive(self, monkeypatch):
        import mcp_server

        def raise_permission_error(pid, sig):
            raise PermissionError()

        monkeypatch.setattr(mcp_server.os, "kill", raise_permission_error)
        assert mcp_server._pid_is_alive(424242) is True
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_server.py::TestPidIsAlive -v`
Expected: every test FAILS with `AttributeError: module 'mcp_server' has no attribute '_pid_is_alive'`

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, replace lines 772-823 (from `def _clear_stale_lock` through the end of `_live_lock_holder_pid`, i.e. its final `return pid` line):

```python
def _clear_stale_lock(path: str, holder_pid: int) -> bool:
    """Remove path's lock file if its recorded holder process is no longer alive.

    Returns True if a stale lock was removed.
    """
    try:
        os.kill(holder_pid, 0)
        return False  # holder still alive (or we lack permission to tell — leave it)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    except OSError:
        return False
    try:
        os.remove(path + ".lock")
        return True
    except OSError:
        return False


def _live_lock_holder_pid(path: str) -> Optional[int]:
    """Return path's lock-file holder PID if that process is live and isn't
    us, else None.

    Reads the sidecar `.lock` file directly — never attempts to open the DB,
    so this check can never itself contend for the lock. Used as a
    proactive pre-check before starting ingestion, to avoid racing another
    live session for the same lock instead of losing that race (#108).

    Best-effort / racy by nature (the holder can appear or disappear
    between this check and the real open attempt) — existing retry/self-heal
    logic (_try_open_with_self_heal, _ensure_db_async) still runs as the
    fallback if the race is lost anyway.
    """
    try:
        with open(path + ".lock") as f:
            holder = f.read().strip()
    except OSError:
        return None  # no lock file
    if not holder.isdigit():
        return None
    pid = int(holder)
    if pid == os.getpid():
        return None  # our own leaked handle, not another process
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # holder no longer running
    except OSError:
        pass  # PermissionError or other — can't confirm death, assume alive
    return pid
```

with:

```python
def _pid_is_alive(pid: int) -> bool:
    """Conservative liveness check: only a positive ProcessLookupError counts
    as dead. Uncertain cases (PermissionError, other OSError) are treated as
    alive rather than risking a false "safe to proceed" signal.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # PermissionError or other — can't confirm death, assume alive
    return True


def _clear_stale_lock(path: str, holder_pid: int) -> bool:
    """Remove path's lock file if its recorded holder process is no longer alive.

    Returns True if a stale lock was removed.
    """
    if _pid_is_alive(holder_pid):
        return False  # holder still alive (or we lack permission to tell — leave it)
    try:
        os.remove(path + ".lock")
        return True
    except OSError:
        return False


def _live_lock_holder_pid(path: str) -> Optional[int]:
    """Return path's lock-file holder PID if that process is live and isn't
    us, else None.

    Reads the sidecar `.lock` file directly — never attempts to open the DB,
    so this check can never itself contend for the lock. Used as a
    proactive pre-check before starting ingestion, to avoid racing another
    live session for the same lock instead of losing that race (#108).

    Best-effort / racy by nature (the holder can appear or disappear
    between this check and the real open attempt) — existing retry/self-heal
    logic (_try_open_with_self_heal, _ensure_db_async) still runs as the
    fallback if the race is lost anyway.
    """
    try:
        with open(path + ".lock") as f:
            holder = f.read().strip()
    except OSError:
        return None  # no lock file
    if not holder.isdigit():
        return None
    pid = int(holder)
    if pid == os.getpid():
        return None  # our own leaked handle, not another process
    return pid if _pid_is_alive(pid) else None
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_server.py::TestPidIsAlive -v`
Expected: all 3 tests PASS

- [ ] **Step 5: Run the existing regression suites to prove the refactor is behavior-preserving**

Run: `python3 -m pytest tests/test_mcp_server.py::TestGetDbLockRetry tests/test_mcp_server.py::TestLiveLockHolderPid -v`
Expected: all tests PASS, unmodified — this is the proof that extracting `_pid_is_alive` didn't change `_clear_stale_lock` or `_live_lock_holder_pid`'s observable behavior.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: extract _pid_is_alive from _clear_stale_lock and _live_lock_holder_pid (#106)"
```

---

### Task 2: `_open_db_at_with_extended_retry`; wire into `_load_ingestion_preload_state`

**Files:**
- Modify: `mcp_server.py:70-74` (add new constants after the existing `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE`)
- Modify: `mcp_server.py:851-872` (insert `_open_db_at_with_extended_retry` immediately after `_open_db_at_with_retry`, before `async def _ensure_db_async` at line 875)
- Modify: `mcp_server.py:2879-2899` (`_load_ingestion_preload_state`)
- Test: `tests/test_mcp_server.py` (new `TestOpenDbAtWithExtendedRetry` class, inserted after `TestGetDbLockRetry` which ends at line 207, before `class TestLiveLockHolderPid:` at line 210 — i.e. this task's new class goes *before* Task 1's `TestPidIsAlive`, which stays after `TestLiveLockHolderPid`)

**Interfaces:**
- Consumes: `_try_open_with_self_heal(path: str) -> MiniGrafDb` (existing, unchanged), `_is_lock_error(exc: Exception) -> bool` (existing, unchanged).
- Produces: `_open_db_at_with_extended_retry(path: str) -> MiniGrafDb` — module-level function in `mcp_server.py`. Same contract as `_open_db_at_with_retry` (raises the lock-contention exception if the budget is exhausted, raises any non-lock exception immediately) but with a ~120s time-budgeted backoff instead of ~1.55s.

- [ ] **Step 1: Write the failing tests**

Add this new class to `tests/test_mcp_server.py`, immediately after `TestGetDbLockRetry` (after line 207), before `class TestLiveLockHolderPid:` (line 210):

```python
class TestOpenDbAtWithExtendedRetry:
    """Unit tests for _open_db_at_with_extended_retry — the longer,
    time-budgeted backoff used only for ingestion startup lock acquisition,
    separate from get_db()'s ~1.55s budget (#106)."""

    def test_succeeds_after_retries_within_budget(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        fake_time = {"t": 0.0}
        monkeypatch.setattr("mcp_server.time.monotonic", lambda: fake_time["t"])
        monkeypatch.setattr(
            "mcp_server.time.sleep",
            lambda s: fake_time.__setitem__("t", fake_time["t"] + s),
        )
        live_pid = os.getpid()  # alive -> self-heal never fires, pure backoff-then-succeed
        lock_err = MiniGrafError(
            f"Database is locked by another process (lock file: x.graph.lock, holder PID: {live_pid})."
        )
        mock_class.open.side_effect = [lock_err, lock_err, db_instance]
        import mcp_server
        result = mcp_server._open_db_at_with_extended_retry(str(tmp_path / "t.graph"))
        assert result is db_instance
        assert mock_class.open.call_count == 3

    def test_gives_up_after_budget_exhausted(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        fake_time = {"t": 0.0}
        monkeypatch.setattr("mcp_server.time.monotonic", lambda: fake_time["t"])
        monkeypatch.setattr(
            "mcp_server.time.sleep",
            lambda s: fake_time.__setitem__("t", fake_time["t"] + s),
        )
        live_pid = os.getpid()  # alive -> self-heal never fires; pure budget exhaustion
        lock_err = MiniGrafError(
            f"Database is locked by another process (lock file: x.graph.lock, holder PID: {live_pid})."
        )
        mock_class.open.side_effect = lock_err  # always raises the same error
        import mcp_server
        with pytest.raises(MiniGrafError):
            mcp_server._open_db_at_with_extended_retry(str(tmp_path / "t.graph"))
        # Far more retries than the old ~1.55s/5-attempt budget would allow —
        # proves the extended budget, not the general-purpose one, was used.
        assert mock_class.open.call_count >= 10

    def test_non_lock_error_propagates_immediately(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        mock_class.open.side_effect = MiniGrafError("corrupt graph file")
        import mcp_server
        with pytest.raises(MiniGrafError):
            mcp_server._open_db_at_with_extended_retry(str(tmp_path / "t.graph"))
        assert mock_class.open.call_count == 1  # no retry for non-lock errors

    def test_self_heals_dead_holder_mid_loop(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        graph_path = str(tmp_path / "t.graph")
        lock_path = graph_path + ".lock"
        with open(lock_path, "w") as f:
            f.write("stale")
        dead_pid = 999999  # not running on any reasonable test machine
        mock_class.open.side_effect = [
            MiniGrafError(
                f"Database is locked by another process (lock file: {lock_path}, holder PID: {dead_pid})."
            ),
            db_instance,
        ]
        import mcp_server
        result = mcp_server._open_db_at_with_extended_retry(graph_path)
        assert result is db_instance
        assert not os.path.exists(lock_path)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m pytest tests/test_mcp_server.py::TestOpenDbAtWithExtendedRetry -v`
Expected: every test FAILS with `AttributeError: module 'mcp_server' has no attribute '_open_db_at_with_extended_retry'`

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, find the existing retry-parameter constants (lines 70-74):

```python
# Retry parameters for acquiring the DB file lock when another process
# (hook subprocess or background ingestion) is briefly holding it.
# Total max wait: 0.05 + 0.10 + 0.20 + 0.40 + 0.80 = 1.55s.
_LOCK_RETRY_MAX = 5
_LOCK_RETRY_BASE = 0.05  # seconds; doubles each attempt
```

Add immediately after it:

```python
# Extended retry budget for the one-time startup/manual-trigger lock
# acquisition only (_load_ingestion_preload_state) — separate from
# _LOCK_RETRY_MAX/_LOCK_RETRY_BASE above, which gate synchronous
# per-request paths (call_tool, IndexCache rebuild) where long blocking
# would be harmful. This path runs on a dedicated worker thread and can
# afford to be patient enough to survive a typical orphan-process cleanup
# window (SIGTERM grace period before SIGKILL) instead of giving up in
# ~1.55s and entering a permanent "error" state (#106).
_INGEST_LOCK_RETRY_BASE = 0.05     # seconds; matches _LOCK_RETRY_BASE for consistency
_INGEST_LOCK_RETRY_CAP = 15.0      # seconds; per-attempt sleep never exceeds this
_INGEST_LOCK_RETRY_BUDGET = 120.0  # seconds; total time before giving up
```

Then, in `mcp_server.py`, find `_open_db_at_with_retry` (lines 851-872), which ends with:

```python
def _open_db_at_with_retry(path: str) -> MiniGrafDb:
    """Open MiniGrafDb at path, retrying with blocking backoff on lock contention.

    Only safe off the asyncio event-loop thread (e.g. IndexCache's background
    rebuild thread): the backoff uses time.sleep(), which would otherwise
    freeze the single-threaded event loop for the whole retry budget — see
    _ensure_db_async for the event-loop-safe equivalent (issue #99).
    """
    delay = _LOCK_RETRY_BASE
    last_exc: Optional[Exception] = None
    for attempt in range(_LOCK_RETRY_MAX):
        try:
            return _try_open_with_self_heal(path)
        except Exception as e:
            if not _is_lock_error(e):
                raise
            last_exc = e
            if attempt < _LOCK_RETRY_MAX - 1:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc
```

Insert this new function immediately after it (still before `async def _ensure_db_async` on line 875):

```python
def _open_db_at_with_extended_retry(path: str) -> MiniGrafDb:
    """Open MiniGrafDb at path, retrying lock contention with a much longer
    time-budgeted backoff than _open_db_at_with_retry.

    Used only by _load_ingestion_preload_state, which runs on a dedicated
    worker thread (see issue #103) and can afford to wait out a typical
    orphan-process cleanup window instead of giving up after ~1.55s and
    leaving _run_ingestion permanently stuck in an "error" state (#106).
    Self-heals a dead holder's lock on every attempt via
    _try_open_with_self_heal, exactly like _open_db_at_with_retry.
    """
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

Finally, in `mcp_server.py`, replace `_load_ingestion_preload_state` (lines 2879-2899):

```python
def _load_ingestion_preload_state(repo_path: str) -> tuple:
    """Open the DB and run every startup preload query for _run_ingestion.

    Executed via run_in_executor on a worker thread (see _run_ingestion), not
    inline on the event loop: opening/mmapping a graph file plus these preload
    queries contain no internal awaits, so running them directly on the event
    loop thread starves the stdio handshake for as long as they take — on a
    large enough graph, longer than a client's connection timeout (issue #103).
    Uses get_db()'s blocking lock-retry rather than _ensure_db_async()'s
    event-loop-safe variant precisely because this now runs off that thread.
    """
    db = get_db()
    watermark = _watermark_query(db)
    prior_ingested = _count_commit_entities(db)
    entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
    file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
    pinned_commit_state = _preload_pinned_commits(db)
    return (
        watermark, prior_ingested, entity_valid_from, entity_descriptions,
        file_entities, file_deps, dep_valid_from, pinned_commit_state,
    )
```

with:

```python
def _load_ingestion_preload_state(repo_path: str) -> tuple:
    """Open the DB and run every startup preload query for _run_ingestion.

    Executed via run_in_executor on a worker thread (see _run_ingestion), not
    inline on the event loop: opening/mmapping a graph file plus these preload
    queries contain no internal awaits, so running them directly on the event
    loop thread starves the stdio handshake for as long as they take — on a
    large enough graph, longer than a client's connection timeout (issue #103).
    Uses _open_db_at_with_extended_retry's much longer blocking lock-retry
    (rather than get_db()'s ~1.55s budget or _ensure_db_async()'s
    event-loop-safe variant) precisely because this runs off that thread and
    can afford to wait out a typical orphan cleanup window instead of
    entering a permanent "error" state (#106). Mirrors get_db()'s
    "reuse the already-open handle" short-circuit rather than reopening
    unconditionally.
    """
    db = _db if _db is not None else _open_db_at_with_extended_retry(_graph_path or _get_graph_path())
    watermark = _watermark_query(db)
    prior_ingested = _count_commit_entities(db)
    entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
    file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
    pinned_commit_state = _preload_pinned_commits(db)
    return (
        watermark, prior_ingested, entity_valid_from, entity_descriptions,
        file_entities, file_deps, dep_valid_from, pinned_commit_state,
    )
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_server.py::TestOpenDbAtWithExtendedRetry -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python3 -m pytest tests/test_mcp_server.py -q`
Expected: all tests PASS (existing `_run_ingestion`/ingestion tests transitively exercise `_load_ingestion_preload_state`'s new wiring)

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extend startup ingestion lock retry past typical orphan-cleanup windows (#106)"
```

---

### Task 3: `error_at` timestamp on `_run_ingestion`'s failure path

**Files:**
- Modify: `mcp_server.py:78-81` (module-level `_ingest_progress` default — add `error_at` key)
- Modify: `mcp_server.py:3305-3311` (`_run_ingestion`'s `except` block)
- Modify: `mcp_server.py:3712-3715` (`main()`'s `_ingest_progress` reset — add `error_at` key)
- Modify: `mcp_server.py:3348-3351` (`handle_minigraf_ingest_git`'s `_ingest_progress` reset — add `error_at` key)
- Test: `tests/test_mcp_server.py` (new test in `TestRunIngestion`, the class starting at line 2638 — insert after `test_ingestion_processes_all_commits`, which ends at line 2651, before `test_watermark_updated_after_each_commit` at line 2653)

**Interfaces:**
- Consumes: `_now_utc_ms() -> str` (existing, unchanged, at mcp_server.py:1942).
- Produces: `_ingest_progress["error_at"]` — an ISO-8601 UTC-with-millisecond-precision string (e.g. `"2026-07-13T18:12:51.184Z"`) set whenever `_run_ingestion` fails, `None` otherwise. Task 4 reads this indirectly only via the error message itself (not `error_at` directly) — `error_at` is exposed to callers purely via the existing `{"ok": True, **_ingest_progress}` spread in `handle_minigraf_ingest_status`, no handler code change needed for it to appear.

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_mcp_server.py`, inside `class TestRunIngestion:` (starts at line 2638), immediately after `test_ingestion_processes_all_commits` (ends at line 2651), before `test_watermark_updated_after_each_commit` (line 2653):

```python
    @pytest.mark.asyncio
    async def test_sets_error_at_timestamp_on_failure(self, mock_minigraf_db, git_repo, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        }

        def raise_error(repo_path, watermark, branch):
            raise RuntimeError("boom")

        monkeypatch.setattr(mcp_server, "_git_commits", raise_error)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "error"
        assert "boom" in mcp_server._ingest_progress["error"]
        assert mcp_server._ingest_progress["error_at"] is not None
        assert mcp_server._ingest_progress["error_at"].endswith("Z")
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_server.py::TestRunIngestion::test_sets_error_at_timestamp_on_failure -v`
Expected: FAILS on `assert mcp_server._ingest_progress["error_at"] is not None` (the key doesn't exist yet, so `.get`/indexing behavior depends on the dict passed in — with the dict as constructed above the key exists as `None` and the assertion fails as `None is not None` → `False`)

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, update the module-level `_ingest_progress` default (lines 78-81):

```python
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None,
}
```

Replace with:

```python
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
}
```

Update `main()`'s reset (lines 3712-3715):

```python
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None,
    }
```

Replace with:

```python
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }
```

Update `handle_minigraf_ingest_git`'s reset (lines 3348-3351):

```python
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None,
    }
```

Replace with:

```python
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }
```

Update `_run_ingestion`'s `except` block (lines 3305-3311):

```python
    except Exception as e:
        # write_executor is already shut down by the inner finally above by the
        # time we get here (it runs on any exit from that try, including this
        # exception propagating through it) — nothing left to clean up.
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
        _db = None
```

Replace with:

```python
    except Exception as e:
        # write_executor is already shut down by the inner finally above by the
        # time we get here (it runs on any exit from that try, including this
        # exception propagating through it) — nothing left to clean up.
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
        _ingest_progress["error_at"] = _now_utc_ms()
        _db = None
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_server.py::TestRunIngestion::test_sets_error_at_timestamp_on_failure -v`
Expected: PASSES

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python3 -m pytest tests/test_mcp_server.py -q`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: record error_at timestamp when ingestion fails (#106)"
```

---

### Task 4: `stale` field on `minigraf_ingest_status`; docs

**Files:**
- Modify: `mcp_server.py:3356-3365` (`handle_minigraf_ingest_status`, insert staleness computation)
- Modify: `mcp_server.py:3602-3611` (`minigraf_ingest_status` tool description)
- Modify: `SKILL.md:297-303` (status vocabulary paragraph)
- Modify: `tests/test_mcp_server.py:1731-1741` (`test_reports_owner_pid_when_skipped` — add `monkeypatch` param, mock `_pid_is_alive`, assert `stale`)
- Test: `tests/test_mcp_server.py` (3 new tests in `TestMinigrafIngestStatus`, inserted immediately after the updated `test_reports_owner_pid_when_skipped`, before `test_returns_total_ingested_from_graph` at line 1743)

**Interfaces:**
- Consumes: `_pid_is_alive(pid: int) -> bool` (Task 1), `_stale_lock_holder_pid(exc: Exception) -> Optional[int]` (existing, unchanged, at mcp_server.py:766 — accepts a plain `str` unmodified since `str()` on a `str` is a no-op).
- Produces: `handle_minigraf_ingest_status()`'s response gains an optional `stale: bool` key, present only when `_ingest_progress["status"]` is `"error"` (with an extractable holder PID in the error text) or `"skipped"` (with a non-`None` `owner_pid`).

- [ ] **Step 1: Update the existing test and write the new failing tests**

In `tests/test_mcp_server.py`, replace `test_reports_owner_pid_when_skipped` (lines 1731-1741):

```python
    def test_reports_owner_pid_when_skipped(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "skipped", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": 424242,
        }
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["status"] == "skipped"
        assert result["owner_pid"] == 424242
```

with:

```python
    def test_reports_owner_pid_when_skipped(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "skipped", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": 424242,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: True)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["status"] == "skipped"
        assert result["owner_pid"] == 424242
        assert result["stale"] is False

    def test_skipped_status_is_stale_when_owner_pid_dead(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "skipped", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": 424242,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: False)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["stale"] is True

    def test_error_status_reports_stale_when_holder_pid_dead(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "error", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "",
            "error": "Database is locked by another process (lock file: x.graph.lock, holder PID: 424242).",
            "owner_pid": None,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: False)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["stale"] is True

    def test_error_status_not_stale_when_holder_pid_alive(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "error", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "",
            "error": "Database is locked by another process (lock file: x.graph.lock, holder PID: 424242).",
            "owner_pid": None,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: True)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["stale"] is False

    def test_error_status_omits_stale_when_no_pid_in_message(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "error", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": "corrupt graph file", "owner_pid": None,
        }
        result = mcp_server.handle_minigraf_ingest_status()
        assert "stale" not in result
```

- [ ] **Step 2: Run the tests to verify the new/updated ones fail**

Run: `python3 -m pytest tests/test_mcp_server.py::TestMinigrafIngestStatus -v`
Expected: `test_reports_owner_pid_when_skipped` FAILS on `assert result["stale"] is False` (`KeyError`); the 4 new tests FAIL the same way (`stale` key absent / `KeyError` or the "omits" test currently trivially passes since the key is already absent — confirm it passes for the *right* reason after Step 3, not by accident beforehand)

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, find `handle_minigraf_ingest_status` (lines 3356-3365):

```python
def handle_minigraf_ingest_status() -> Dict[str, Any]:
    """Return current ingestion progress, augmented with graph-backed last-run info."""
    result: Dict[str, Any] = {"ok": True, **_ingest_progress}
    # processed_this_run is derived in-memory (no extra DB query) so it stays
    # accurate even mid-run, distinguishing "this attempt's progress" from the
    # cumulative total in `processed` — see issue #85.
    result["processed_this_run"] = (
        _ingest_progress["processed"] - _ingest_progress.get("prior_ingested", 0)
    )
    if _ingest_progress["status"] != "running":
```

Replace with:

```python
def handle_minigraf_ingest_status() -> Dict[str, Any]:
    """Return current ingestion progress, augmented with graph-backed last-run info."""
    result: Dict[str, Any] = {"ok": True, **_ingest_progress}
    # processed_this_run is derived in-memory (no extra DB query) so it stays
    # accurate even mid-run, distinguishing "this attempt's progress" from the
    # cumulative total in `processed` — see issue #85.
    result["processed_this_run"] = (
        _ingest_progress["processed"] - _ingest_progress.get("prior_ingested", 0)
    )
    # Staleness: a terminal error/skipped state can outlive the condition
    # that caused it (e.g. the orphaned holder it names has since died) —
    # re-check liveness on every poll instead of echoing a dead PID forever.
    # Purely informational: never auto-retries ingestion (#106).
    if _ingest_progress["status"] == "error":
        holder_pid = _stale_lock_holder_pid(_ingest_progress.get("error") or "")
        if holder_pid is not None:
            result["stale"] = not _pid_is_alive(holder_pid)
    elif _ingest_progress["status"] == "skipped":
        owner_pid = _ingest_progress.get("owner_pid")
        if owner_pid is not None:
            result["stale"] = not _pid_is_alive(owner_pid)
    if _ingest_progress["status"] != "running":
```

(The remainder of the function, from the `try:` block onward, is unchanged — only the new `if`/`elif` block is inserted.)

Next, in `mcp_server.py`, find the `minigraf_ingest_status` tool description (lines 3602-3611):

```python
    Tool(
        name="minigraf_ingest_status",
        description=(
            "Return the current git ingestion progress. "
            "status is one of: idle, running, complete, error, skipped. "
            "skipped means another live process already owns the graph lock "
            "(see owner_pid) — this server will not start ingestion on its own; "
            "call minigraf_ingest_git again later if you want to retry."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
```

Replace with:

```python
    Tool(
        name="minigraf_ingest_status",
        description=(
            "Return the current git ingestion progress. "
            "status is one of: idle, running, complete, error, skipped. "
            "skipped means another live process already owns the graph lock "
            "(see owner_pid) — this server will not start ingestion on its own; "
            "call minigraf_ingest_git again later if you want to retry. "
            "For error and skipped, a stale field may be present: stale=true means "
            "the condition that caused this state (the cited or owning PID) is no "
            "longer alive, so a minigraf_ingest_git retry is likely to succeed now; "
            "error also includes error_at, the timestamp the failure occurred."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
```

- [ ] **Step 4: Update `SKILL.md`**

In `SKILL.md`, find the status vocabulary paragraph (lines 297-303):

```
`status` is one of: `idle`, `running`, `complete`, `error`, `stopped`, `skipped`.
`stopped` means a graceful shutdown (session end) paused ingestion between commits —
not a failure; the next `minigraf_ingest_git` call (or server auto-start)
resumes from the watermark automatically. `skipped` means another live process
already owns the graph lock (its PID is in `owner_pid`) — this server will not
attempt ingestion on its own; call `minigraf_ingest_git` again later to retry.
`processed` is the
```

Replace with:

```
`status` is one of: `idle`, `running`, `complete`, `error`, `stopped`, `skipped`.
`stopped` means a graceful shutdown (session end) paused ingestion between commits —
not a failure; the next `minigraf_ingest_git` call (or server auto-start)
resumes from the watermark automatically. `skipped` means another live process
already owns the graph lock (its PID is in `owner_pid`) — this server will not
attempt ingestion on its own; call `minigraf_ingest_git` again later to retry.
For `error` and `skipped`, a `stale` field may be present: `stale: true` means the
process that caused this state is no longer alive, so a `minigraf_ingest_git` retry
is likely to succeed now — check it before assuming a cached error is still accurate.
`error` also includes `error_at`, the timestamp the failure occurred. `processed` is the
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_mcp_server.py::TestMinigrafIngestStatus -v`
Expected: all tests PASS, including the updated `test_reports_owner_pid_when_skipped` and the 4 new tests

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `python3 -m pytest tests/test_mcp_server.py -q`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py SKILL.md tests/test_mcp_server.py
git commit -m "feat: report stale on minigraf_ingest_status for error/skipped states (#106)"
```

---

## Self-Review Notes

- **Spec coverage:** Part A (extended retry) → Task 2. Part B (`_pid_is_alive` extraction) → Task 1. Part C (`error_at` + `stale`) → Tasks 3 and 4. All spec sections covered; docs (mcp_server.py tool description + `SKILL.md`) are their own concrete task step (Task 4, Steps 3-4), not just prose, per explicit instruction to always give doc updates literal before/after text.
- **Task ordering:** Task 1 (extract `_pid_is_alive`) must land before Task 4 (which consumes it directly) and is independent of Task 2/3. Task 2 and Task 3 are independent of each other and of Task 1's internals (Task 2 doesn't call `_pid_is_alive`), but are sequenced 2-then-3 for narrative clarity; either order is safe. Task 4 depends on Task 1 (`_pid_is_alive`) and Task 3 (the `error_at`/`owner_pid` keys existing on `_ingest_progress` — though Task 4's own tests construct their own `_ingest_progress` dicts directly, so it doesn't strictly need Task 3's dict-default changes to pass, only Task 1's function).
- **Hermetic tests:** every test touching the extended retry's backoff mocks both `time.sleep` and `time.monotonic` (or just `time.sleep` when timing isn't exercised) — no real multi-second/minute waits. Every test touching `_pid_is_alive`/staleness either uses a real dead PID convention (`999999`) already established in this file, the test's own live `os.getpid()`, or mocks `_pid_is_alive` directly for determinism.
- **Type/signature consistency:** `_pid_is_alive(pid: int) -> bool` (Task 1) is consumed identically in Task 4. `_open_db_at_with_extended_retry(path: str) -> MiniGrafDb` (Task 2) matches `_open_db_at_with_retry`'s existing contract. `_stale_lock_holder_pid` (existing) is consumed unmodified in Task 4, passing a `str` where its signature says `Exception` — verified safe since the function only ever calls `str()` on its argument.
