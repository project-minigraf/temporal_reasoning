# Stage A Lease Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage A should hold one graph lease across up to 25 commits / 2 s instead of dropping the `MiniGrafDb` handle (and paying a full O(graph size) checkpoint) after every commit, while still releasing often enough for the out-of-process auto-memory hooks to win the lock (#280).

**Architecture:** A new `_LeaseWindow` object holds an outer `_db_lease_async_committing_index` lease. Stage A's existing per-commit lease joins it at refcount 1→2, so the per-commit exit no longer drops the handle. At the loop head the window decides whether to release for real (commit count or clock), then sleeps `_SWEEP_YIELD_PAUSE_SECONDS` outside any lease. A `try/finally` closes it on every exit.

**Tech Stack:** Python 3.10–3.14, asyncio, minigraf ≥2.0.2 (`MiniGrafDb`), SQLite FTS5 fact index, pytest + pytest-asyncio (real backend only).

**Spec:** `docs/superpowers/specs/2026-09-29-stage-a-lease-window-design.md`

## Global Constraints

- Always run Python as `.venv/bin/python`. System python carries minigraf 1.1.1 and produces 122 fake failures.
- Run the suite from a shell whose environment has no `MINIGRAF_*` variables. `tests/conftest.py` scrubs them per test, but module constants are read at import.
- Stage A reuses `_SWEEP_YIELD_COMMITS` (25), `_SWEEP_YIELD_SECONDS` (2.0) and `_SWEEP_YIELD_PAUSE_SECONDS` (0.1). No new constants and no new env vars.
- Tests patch the **constant** (`monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", ...)`), never the env var, because the constants are read at import.
- Every sleep on the event loop is `asyncio.sleep`, never `time.sleep` (#99).
- The single-handle invariant stands. The window takes an ordinary lease through `_db_lease_async_committing_index`; nothing may hold or reuse a raw handle.
- No `GRAPH_FORMAT_VERSION` bump and no migration.
- Every new regression test is **ablation-proven**: revert the specific production line, watch the named assertion fail, restore, and record the result in the commit message.
- Stage B's window loop is NOT modified.
- Don't put closing keywords (`closes`/`fixes`/`resolves` + `#N`) in commit messages. Use `Refs #280`.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
  ```
- Branch: `280-stage-a-lease-window` (already exists, spec committed). Work in place with no worktree.

## Review Focus

1. **An exception or shutdown mid-window must not leak the window's lease.** A leaked lease leaves `_lease_manager.lease_count` at 1 after the run, and the next process-wide open silently joins a stale handle. Task 2 covers this with a test for each path.
2. **A hook that arrives during a slow Stage A must still get in within its ~2.6 s acquire budget at shipped defaults.** Task 4 tests this with a real hook process.
3. **A real release with the fact-index transaction still open** would reintroduce #347's lock-order inversion. Task 2 tests this with a two-step ablation.
4. **An empty Stage A, or one where every extraction fails, must not open a handle.** Today no lease is taken on those paths. Task 1 tests laziness directly; Task 2's drop-count test would show an extra drop.
5. **The drop cost must not become invisible to the trace again.** `yield_s` must account for boundary time even across commits that emit no record. Task 3 has an accumulation test that uses a failed write.

---

### Task 1: `_LeaseWindow`

**Files:**
- Modify: `mcp_server.py`. Insert the class directly after `_db_lease_async_committing_index` (currently ends ~line 3822, just before `def _graph_path_current`).
- Test: `tests/test_mcp_server.py`. Add a new class `TestLeaseWindow` directly before `class TestStageBYieldsTheLock:` (~line 30376).

**Interfaces:**
- Consumes: `_db_lease_async_committing_index(loop, write_executor, index_con)` (existing async context manager), `_lease_manager.lease_count` (existing property), `_open_index_writer_safe(path)`, `fact_index.index_path_for(graph_path)`.
- Produces:
  ```python
  class _LeaseWindow:
      def __init__(self, loop, write_executor, index_con, *,
                   max_commits: int, max_seconds: float, pause_seconds: float) -> None
      is_open: bool                        # property
      async def ensure_open(self) -> None
      def note_commit(self) -> None
      async def maybe_yield(self) -> float  # seconds spent at the boundary (0.0 if none)
      async def close(self) -> None         # no pause; idempotent
  ```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` before `class TestStageBYieldsTheLock:`:

```python
class TestLeaseWindow:
    """#280. _LeaseWindow holds ONE outer lease across several Stage A commits
    so the per-commit lease joins it (refcount 1 -> 2) instead of dropping the
    handle -- and with it a full O(graph size) checkpoint in minigraf's
    `Drop for Inner` -- after every commit. It releases for real only at a
    boundary (commit count or clock), then pauses OUTSIDE any lease so an
    out-of-process hook can take the graph lock.

    Driven against a real graph and a real batched index connection, never a
    fake: the properties under test (who holds the file lock, whether SQLite
    is mid-transaction) exist only in the real backends.
    """

    def _setup(self, tmp_path, monkeypatch):
        import concurrent.futures
        import fact_index
        import mcp_server
        graph = tmp_path / "g.graph"
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph))
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        index_con = mcp_server._open_index_writer_safe(
            fact_index.index_path_for(str(graph))
        )
        return graph, executor, index_con

    def _window(self, executor, index_con, **kw):
        import mcp_server
        kw.setdefault("max_commits", 10**9)
        kw.setdefault("max_seconds", 10**9)
        kw.setdefault("pause_seconds", 0.0)
        return mcp_server._LeaseWindow(
            asyncio.get_running_loop(), executor, index_con, **kw
        )

    @pytest.mark.asyncio
    async def test_is_lazy_and_a_yield_before_opening_does_nothing(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        _graph, executor, index_con = self._setup(tmp_path, monkeypatch)
        try:
            w = self._window(executor, index_con, max_commits=0, max_seconds=0.0)
            assert not w.is_open
            assert mcp_server._lease_manager.lease_count == 0
            assert await w.maybe_yield() == 0.0
            assert not w.is_open, "maybe_yield opened a window nobody asked for"
            assert mcp_server._lease_manager.lease_count == 0
        finally:
            executor.shutdown(wait=True)
            mcp_server._reset_db_state()

    @pytest.mark.asyncio
    async def test_a_nested_lease_joins_and_does_not_release_the_graph(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        graph, executor, index_con = self._setup(tmp_path, monkeypatch)
        loop = asyncio.get_running_loop()
        try:
            w = self._window(executor, index_con)
            await w.ensure_open()
            assert mcp_server._lease_manager.lease_count == 1
            async with mcp_server._db_lease_async_committing_index(
                loop, executor, index_con
            ):
                assert mcp_server._lease_manager.lease_count == 2
            assert mcp_server._lease_manager.lease_count == 1, (
                "the per-commit lease's exit dropped the window's handle"
            )
            assert not _another_process_can_open(str(graph)), (
                "another process could open the graph while the window was "
                "open -- the window is not holding the lock"
            )
            await w.close()
            assert mcp_server._lease_manager.lease_count == 0
            assert _another_process_can_open(str(graph))
        finally:
            executor.shutdown(wait=True)
            mcp_server._reset_db_state()

    @pytest.mark.asyncio
    async def test_the_commit_count_closes_the_window_and_pauses(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        _graph, executor, index_con = self._setup(tmp_path, monkeypatch)
        try:
            w = self._window(executor, index_con, max_commits=2, pause_seconds=0.05)
            await w.ensure_open()
            w.note_commit()
            assert await w.maybe_yield() == 0.0
            assert w.is_open
            w.note_commit()
            spent = await w.maybe_yield()
            assert not w.is_open
            assert mcp_server._lease_manager.lease_count == 0
            assert spent >= 0.05, f"the boundary paid no pause ({spent:.4f}s)"
            # Reopening starts a fresh count.
            await w.ensure_open()
            w.note_commit()
            assert await w.maybe_yield() == 0.0
            await w.close()
        finally:
            executor.shutdown(wait=True)
            mcp_server._reset_db_state()

    @pytest.mark.asyncio
    async def test_the_clock_alone_closes_the_window(self, tmp_path, monkeypatch):
        """count is unreachable (10**9), so only the clock operand of the
        boundary's `or` can fire. A count-driven test never EVALUATES the
        clock operand (short-circuit) -- see TestStageBYieldsTheLock's
        test_the_clock_alone_closes_a_window."""
        import mcp_server
        _graph, executor, index_con = self._setup(tmp_path, monkeypatch)
        try:
            w = self._window(executor, index_con, max_seconds=0.0)
            await w.ensure_open()
            await w.maybe_yield()
            assert not w.is_open, "the clock operand never closed the window"
            assert mcp_server._lease_manager.lease_count == 0
        finally:
            executor.shutdown(wait=True)
            mcp_server._reset_db_state()

    @pytest.mark.asyncio
    async def test_close_pays_no_pause_and_is_idempotent(self, tmp_path, monkeypatch):
        import mcp_server
        _graph, executor, index_con = self._setup(tmp_path, monkeypatch)
        try:
            w = self._window(executor, index_con, pause_seconds=5.0)
            await w.ensure_open()
            t = time.perf_counter()
            await w.close()
            await w.close()
            assert time.perf_counter() - t < 1.0, "close() paid the boundary pause"
            assert mcp_server._lease_manager.lease_count == 0
        finally:
            executor.shutdown(wait=True)
            mcp_server._reset_db_state()

    @pytest.mark.asyncio
    async def test_a_real_release_commits_the_index_first(self, tmp_path, monkeypatch):
        """#347: a release with index_con mid-transaction is a lock-order
        inversion against the hooks. The window's own lease is the committing
        wrapper, so its release commits by construction."""
        import mcp_server
        _graph, executor, index_con = self._setup(tmp_path, monkeypatch)
        assert index_con is not None, "no batched index connection to test against"
        try:
            w = self._window(executor, index_con)
            await w.ensure_open()
            index_con.execute("create table if not exists _t280(x)")
            index_con.execute("insert into _t280 values (1)")
            assert index_con.in_transaction, "setup did not open a transaction"
            await w.close()
            assert not index_con.in_transaction, (
                "the window released the graph with the index transaction open"
            )
        finally:
            executor.shutdown(wait=True)
            mcp_server._reset_db_state()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestLeaseWindow -v`
Expected: all 6 FAIL with `AttributeError: module 'mcp_server' has no attribute '_LeaseWindow'`.

- [ ] **Step 3: Implement `_LeaseWindow`**

Insert after `_db_lease_async_committing_index` in `mcp_server.py`:

```python
class _LeaseWindow:
    """#280. One graph lease held across several Stage A commits.

    Stage A used to take a lease per commit. Its two streams essentially
    never overlap their leases, so the refcount hit 0 after every commit and
    _DbLeaseManager.release() dropped the handle -- and minigraf's `Drop for
    Inner` runs a full O(graph size) checkpoint, outside _CheckpointPolicy's
    duty gate and invisible to the trace's ckpt_d_seconds. Measured at 110 s
    of a 277 s Stage A over 600 commits (minigraf 2.0.2), i.e. ~1 drop per
    commit.

    The window holds its OWN lease; Stage A's per-commit lease then JOINS it
    at refcount 1 -> 2 and its exit is 2 -> 1, which drops nothing. The
    window releases for real only at a boundary (maybe_yield), then sleeps
    OUTSIDE any lease so an out-of-process auto-memory hook -- which retries
    for only ~2.6 s of wall clock and then silently discards its write --
    can take the graph file lock. Holding one lease for all of Stage A
    instead would discard every hook write for its duration (#280's own
    correction comment).

    The window's lease is _db_lease_async_committing_index, so every REAL
    release commits index_con first (#347) by construction, not by call-site
    care.

    Lazy: nothing is opened until ensure_open(), so a Stage A that never
    reaches write dispatch (empty, or every extraction failed) takes no
    lease, as before.

    Stage B keeps its own inline window loop (#222 phase 5 item C). Both use
    the same _SWEEP_YIELD_* constants, which bound hook lockout, not sweep
    behaviour.
    """

    def __init__(self, loop, write_executor, index_con, *,
                 max_commits: int, max_seconds: float, pause_seconds: float) -> None:
        self._loop = loop
        self._write_executor = write_executor
        self._index_con = index_con
        self._max_commits = max_commits
        self._max_seconds = max_seconds
        self._pause_seconds = pause_seconds
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._opened_at = 0.0
        self._count = 0

    @property
    def is_open(self) -> bool:
        return self._stack is not None

    async def ensure_open(self) -> None:
        """Take the window's lease if it is not already held."""
        if self._stack is not None:
            return
        stack = contextlib.AsyncExitStack()
        await stack.enter_async_context(
            _db_lease_async_committing_index(
                self._loop, self._write_executor, self._index_con,
            )
        )
        self._stack = stack
        self._opened_at = time.monotonic()
        self._count = 0

    def note_commit(self) -> None:
        """Count one commit that reached write dispatch inside this window."""
        if self._stack is not None:
            self._count += 1

    async def maybe_yield(self) -> float:
        """At a boundary, release for real and pause; return seconds spent.

        A boundary is `count >= max_commits or elapsed >= max_seconds`. The
        COUNT bounds how many drop-checkpoints the releases cost; the CLOCK
        bounds how long a hook is locked out when one window's commits are
        individually slow, which on a large graph they are. Neither is
        redundant. The pause is what makes the release usable: a bare
        release-then-reacquire leaves the lock free for microseconds, not an
        interval (see _SWEEP_YIELD_PAUSE_SECONDS).
        """
        if self._stack is None:
            return 0.0
        if not (
            self._count >= self._max_commits
            or time.monotonic() - self._opened_at >= self._max_seconds
        ):
            return 0.0
        started = time.perf_counter()
        await self.close()
        # asyncio.sleep, never time.sleep: this runs on the event loop (#99).
        await asyncio.sleep(self._pause_seconds)
        return time.perf_counter() - started

    async def close(self) -> None:
        """Release the window's lease, with no pause. Idempotent."""
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestLeaseWindow -v`
Expected: 6 passed.

- [ ] **Step 5: Ablate**

Run each ablation one at a time, confirm the named test fails, then restore with `git checkout -p mcp_server.py` or by hand. Don't use `git stash push <file>`, which is a no-op ablation (see memory).
- In `maybe_yield`, delete the `or time.monotonic() - self._opened_at >= self._max_seconds` operand → `test_the_clock_alone_closes_the_window` fails.
- In `ensure_open`, replace `_db_lease_async_committing_index(...)` with `db_lease_async()` → `test_a_real_release_commits_the_index_first` fails.
- In `maybe_yield`, delete `await asyncio.sleep(self._pause_seconds)` → `test_the_commit_count_closes_the_window_and_pauses` fails on `spent >= 0.05`.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _LeaseWindow: one graph lease across several Stage A commits (#280)

<body: what it is, the three ablations and which test each reddened>

Refs #280

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 2: Wire the window into Stage A

**Files:**
- Modify: `mcp_server.py`
  - `_run_ingestion` Stage A: around `run_progress.stage_a_started()` (~line 14882) through the end of the `while pending:` loop (~line 15087, just before `run_progress.stage_a_finished(completed_all)`).
  - The comment block above `_SWEEP_YIELD_COMMITS` (~lines 170–218).
- Test: `tests/test_mcp_server.py`
  - Add new class `TestStageAYieldsTheLock` directly after `TestLeaseWindow`.
  - Edit `TestIngestionCommitsTheIndexBeforeReleasingTheGraph.test_a_hook_writing_between_stage_a_leases_lands_in_both` (~line 31167).
  - Add one new test to `TestIngestionCommitsTheIndexBeforeReleasingTheGraph`.

**Interfaces:**
- Consumes: `_LeaseWindow` (Task 1). Existing: `TestStageBYieldsTheLock._prepare(tmp_path, monkeypatch, n=12)`, which returns `(repo, graph)` for a 12-commit repo on branch `master`; `ingest_progress.RunProgress.stage_a_finished(self, completed: bool)`; `ingest_progress.RunProgress.retired(self, stream, outcome, pos)`; `_forbid_blocking_sleep_on_event_loop(monkeypatch)`.
- Produces: a local variable `window` in `_run_ingestion`'s Stage A. Task 3 extends the loop-head line `await window.maybe_yield()` into an accumulation.

- [ ] **Step 1: Write the failing tests**

Add after `class TestLeaseWindow` (needs `import math` inside the test; `contextlib` is already imported at module top):

```python
class TestStageAYieldsTheLock:
    """#280. Stage A takes one _LeaseWindow lease across several commits, so
    its per-commit leases JOIN rather than drop the handle every commit.

    Drops are counted at the lease seam: inside the real lease, after the
    body, `_lease_manager.lease_count == 1` means THIS exit takes the count
    to 0 and drops the handle. Only exits after the first Stage A apply and
    before RunProgress.stage_a_finished are counted -- phase is already
    "converging" during the preload lease, which is not a Stage A drop.
    """

    def _prepare(self, tmp_path, monkeypatch, n=12):
        return TestStageBYieldsTheLock()._prepare(tmp_path, monkeypatch, n=n)

    async def _run_counting_drops(self, tmp_path, monkeypatch):
        import ingest_progress
        import mcp_server
        repo, _graph = self._prepare(tmp_path, monkeypatch)
        applied, drops, over = [], [], []
        real_fwd = mcp_server._forward_apply
        real_rev = mcp_server._reverse_apply
        real_lease = mcp_server.db_lease_async
        real_finished = ingest_progress.RunProgress.stage_a_finished

        def fwd_spy(*a, **kw):
            if not kw.get("lifecycle_only"):
                applied.append("fwd")
            return real_fwd(*a, **kw)

        def rev_spy(*a, **kw):
            applied.append("rev")
            return real_rev(*a, **kw)

        def finished_spy(self_, *a, **kw):
            over.append(True)
            return real_finished(self_, *a, **kw)

        @contextlib.asynccontextmanager
        async def lease_spy():
            async with real_lease() as db:
                try:
                    yield db
                finally:
                    if applied and not over and mcp_server._lease_manager.lease_count == 1:
                        drops.append(len(applied))

        monkeypatch.setattr(mcp_server, "_forward_apply", fwd_spy)
        monkeypatch.setattr(mcp_server, "_reverse_apply", rev_spy)
        monkeypatch.setattr(mcp_server, "db_lease_async", lease_spy)
        monkeypatch.setattr(ingest_progress.RunProgress, "stage_a_finished", finished_spy)
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress.get("status") == "complete", mcp_server._ingest_progress
        return applied, drops

    @pytest.mark.asyncio
    async def test_stage_a_drops_the_handle_once_per_window_not_per_commit(
        self, tmp_path, monkeypatch
    ):
        import math
        import mcp_server
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 4)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_SECONDS", 10**9)
        applied, drops = await self._run_counting_drops(tmp_path, monkeypatch)
        assert len(applied) >= 8, (
            f"only {len(applied)} Stage A applies -- too few for a 4-commit "
            f"window to be distinguishable from a per-commit lease"
        )
        assert len(drops) == math.ceil(len(applied) / 4), (
            f"{len(drops)} handle drops over {len(applied)} Stage A commits "
            f"with a 4-commit window; expected {math.ceil(len(applied) / 4)}. "
            f"{len(applied)} drops means Stage A is still dropping the handle "
            f"-- and paying minigraf's O(graph size) Drop checkpoint -- per commit"
        )

    @pytest.mark.asyncio
    async def test_the_clock_alone_closes_a_stage_a_window(self, tmp_path, monkeypatch):
        """Count unreachable, clock 0 s: only the clock operand can end a
        window. Also the Stage A run that reaches the boundary's pause
        repeatedly, so it carries the blocking-sleep guard."""
        import mcp_server
        _forbid_blocking_sleep_on_event_loop(monkeypatch)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 10**9)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_SECONDS", 0.0)
        applied, drops = await self._run_counting_drops(tmp_path, monkeypatch)
        assert len(applied) >= 2
        assert len(drops) >= 2, (
            f"{len(drops)} drop(s) with the count disjunct unreachable -- the "
            f"clock never closed a Stage A window"
        )

    @pytest.mark.asyncio
    async def test_an_exception_mid_window_leaks_no_lease(self, tmp_path, monkeypatch):
        """Something raising out of the Stage A loop (here: the third
        RunProgress.retired, which sits outside every per-commit try) must
        still release the window's lease. A leaked lease leaves the count at 1
        after the run; the outer finally's final-checkpoint lease would just
        JOIN it, so nothing else notices."""
        import ingest_progress
        import mcp_server
        repo, _graph = self._prepare(tmp_path, monkeypatch)
        real_retired = ingest_progress.RunProgress.retired
        calls = []

        def retired_spy(self_, stream, outcome, pos):
            calls.append(pos)
            if len(calls) == 3:
                raise RuntimeError("injected mid-window failure (#280 test)")
            return real_retired(self_, stream, outcome, pos)

        monkeypatch.setattr(ingest_progress.RunProgress, "retired", retired_spy)
        try:
            await mcp_server._run_ingestion(str(repo), "master")
            assert mcp_server._ingest_progress.get("status") == "error", (
                "the injected failure did not reach the run -- test proves nothing"
            )
            assert "injected mid-window" in (mcp_server._ingest_progress.get("error") or "")
            assert mcp_server._lease_manager.lease_count == 0, (
                f"lease_count is {mcp_server._lease_manager.lease_count} after "
                f"the run -- the Stage A window's lease leaked past an exception"
            )
        finally:
            mcp_server._reset_db_state()

    @pytest.mark.asyncio
    async def test_a_shutdown_mid_window_leaks_no_lease_and_pays_no_pause(
        self, tmp_path, monkeypatch
    ):
        import mcp_server
        repo, _graph = self._prepare(tmp_path, monkeypatch)
        # N=3 and the flag set on the 3rd apply: the FIRST boundary would fall
        # at exactly the loop head that sees the shutdown flag. That is what
        # lets the ablation (maybe_yield above the shutdown check) pay the 5 s
        # pause and redden -- at the default N=25 no boundary would ever be
        # reached in a 3-commit Stage A, and the elapsed check would pass
        # whether or not the order was right.
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 3)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_SECONDS", 10**9)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_PAUSE_SECONDS", 5.0)
        real_rev = mcp_server._reverse_apply
        real_fwd = mcp_server._forward_apply
        n = [0]

        def interrupt(*a, **kw):
            n[0] += 1
            if n[0] >= 3:
                mcp_server._shutdown_requested.set()

        def rev_spy(*a, **kw):
            out = real_rev(*a, **kw)
            interrupt()
            return out

        def fwd_spy(*a, **kw):
            out = real_fwd(*a, **kw)
            if not kw.get("lifecycle_only"):
                interrupt()
            return out

        monkeypatch.setattr(mcp_server, "_reverse_apply", rev_spy)
        monkeypatch.setattr(mcp_server, "_forward_apply", fwd_spy)
        t = time.perf_counter()
        try:
            await mcp_server._run_ingestion(str(repo), "master")
            elapsed = time.perf_counter() - t
            assert n[0] >= 3, "the shutdown was never requested -- test proves nothing"
            assert mcp_server._lease_manager.lease_count == 0, (
                "the Stage A window's lease leaked past the shutdown break"
            )
            assert elapsed < 5.0, (
                f"the run took {elapsed:.1f}s with a 5 s boundary pause -- the "
                f"shutdown path paid a window pause it must skip"
            )
        finally:
            mcp_server._shutdown_requested.clear()
            mcp_server._reset_db_state()
```

Add to `TestIngestionCommitsTheIndexBeforeReleasingTheGraph`, after `test_the_skipped_span_flush_commits_the_index_too`:

```python
    @pytest.mark.asyncio
    async def test_every_real_stage_a_release_commits_the_index(
        self, tmp_path, monkeypatch
    ):
        """#280 moved Stage A's REAL releases from the per-commit lease to the
        window's boundary. Check the index transaction at exactly those: exits
        where this lease takes the count to 0, after a reverse write. A 2-commit
        window guarantees several of them mid-Stage-A, and the test fails if it
        saw none -- a check that never saw a window close proves nothing
        about one."""
        import mcp_server
        repo, _graph = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 2)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_SECONDS", 10**9)
        cons = []
        real_open = mcp_server._open_index_writer_safe

        def open_spy(path):
            con = real_open(path)
            cons.append(con)
            return con

        real_releases = []  # in_transaction at each real Stage A release
        rev = []
        real_rev = mcp_server._reverse_apply
        real_lease = mcp_server.db_lease_async

        def rev_spy(*a, **kw):
            rev.append(1)
            return real_rev(*a, **kw)

        @contextlib.asynccontextmanager
        async def lease_spy():
            async with real_lease() as db:
                try:
                    yield db
                finally:
                    if (rev and mcp_server._ingest_progress.get("phase") == "converging"
                            and mcp_server._lease_manager.lease_count == 1
                            and cons and cons[0] is not None):
                        real_releases.append(cons[0].in_transaction)

        monkeypatch.setattr(mcp_server, "_open_index_writer_safe", open_spy)
        monkeypatch.setattr(mcp_server, "_reverse_apply", rev_spy)
        monkeypatch.setattr(mcp_server, "db_lease_async", lease_spy)
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress.get("status") == "complete"
        assert not any(real_releases), (
            f"a Stage A window released the graph with the fact-index "
            f"transaction open: {real_releases} (#347)"
        )
        assert len(real_releases) >= 2, (
            f"only {len(real_releases)} real Stage A release(s) after a reverse "
            f"write were observed -- the window boundary went unexercised"
        )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestStageAYieldsTheLock "tests/test_mcp_server.py::TestIngestionCommitsTheIndexBeforeReleasingTheGraph::test_every_real_stage_a_release_commits_the_index" -v`
Expected:
- `test_stage_a_drops_the_handle_once_per_window_not_per_commit` FAILS: drops == applied.
- `test_the_clock_alone_closes_a_stage_a_window` PASSES on master, because per-commit drops ≥ 2 trivially. That is expected; it is ablation-proven in Step 5 instead.
- `test_an_exception_mid_window_leaks_no_lease` and `test_a_shutdown_mid_window...` PASS on master, because there is no window yet. They are also ablation-proven in Step 5.
- `test_every_real_stage_a_release_commits_the_index` PASSES on master.

Record which tests failed and which passed in the commit body.

- [ ] **Step 3: Wire the window into `_run_ingestion`**

In `mcp_server.py`, `_run_ingestion`, find:

```python
                run_progress.stage_a_started()
                for _ in range(pipeline_depth):
                    if not submit_next():
                        break

                while pending:
                    if _shutdown_requested.is_set():
                        completed_all = False
                        break
```

Replace with the block below. The entire existing body of `while pending:` moves one indentation level right, under the `try:`. Nothing else in the body changes except the three insertions shown in this step.

```python
                # #280: one lease across up to _SWEEP_YIELD_COMMITS commits /
                # _SWEEP_YIELD_SECONDS, instead of one per commit. Every
                # per-commit lease below JOINS the window's (refcount 1 -> 2),
                # so its exit no longer drops the handle and with it a full
                # O(graph size) checkpoint in minigraf's `Drop for Inner`. The
                # window releases for real at the loop head, between commits,
                # and pauses outside any lease so the out-of-process hooks can
                # get in. See _LeaseWindow. The constants are read HERE, not at
                # import, so tests patching them are honoured.
                window = _LeaseWindow(
                    loop, write_executor, index_con,
                    max_commits=_SWEEP_YIELD_COMMITS,
                    max_seconds=_SWEEP_YIELD_SECONDS,
                    pause_seconds=_SWEEP_YIELD_PAUSE_SECONDS,
                )
                run_progress.stage_a_started()
                for _ in range(pipeline_depth):
                    if not submit_next():
                        break

                # try/finally, not a bare close after the loop: the shutdown
                # break, BrokenProcessPool and anything else propagating out of
                # this loop must all release the window's lease. A leaked one
                # is silent -- the outer finally's final-checkpoint lease would
                # just join it -- and the count would stay 1 after the run.
                try:
                    while pending:
                        if _shutdown_requested.is_set():
                            completed_all = False
                            break
                        # The ONLY boundary: between commits, whatever path the
                        # previous one took. Never after the last commit (the
                        # head runs only while `pending` is non-empty) and never
                        # on shutdown (checked just above). Before
                        # _trace_t_await so await_s stays pure extraction stall.
                        await window.maybe_yield()
                        ... (existing body, unchanged, re-indented) ...
                finally:
                    await window.close()
```

Inside the re-indented body, make two insertions.

(a) Immediately before the per-commit `async with _db_lease_async_committing_index(loop, write_executor, index_con) as db:` (the one directly after `_trace_write_ok = True`):

```python
                        # Inside apply_s, which already documents that it spans
                        # the lease ACQUIRE: on the first commit of a window this
                        # is the handle open; otherwise a no-op.
                        await window.ensure_open()
```

(b) Immediately after that `async with` block ends (before the `# #260: no record for a commit whose write failed` comment):

```python
                        # Written or failed, this commit touched the graph.
                        window.note_commit()
```

Also rewrite the existing `# #260 M2: it also spans the lease RELEASE ...` sentences in the comment above `_trace_t_apply` to:

```python
                    # ... #260 M2: it also spans the lease RELEASE. Since #280
                    # that release is a JOIN's 2 -> 1 and drops nothing; the
                    # handle open lands in the first apply_s of each window,
                    # and the drop lands at the window boundary (yield_s).
```

Verify the re-indent touched nothing else:
Run: `git diff -w mcp_server.py`
Expected: only the added lines, the `try:`/`finally:` pair and the comment rewrite show. No existing statement appears changed.

- [ ] **Step 4: Update the `_SWEEP_YIELD_COMMITS` comment block**

In the comment above `_SWEEP_YIELD_COMMITS`:
- Change the first sentence to: `# #222 phase 5 item C, extended to Stage A by #280. Stage B releases its lease every _SWEEP_YIELD_COMMITS swept commits or _SWEEP_YIELD_SECONDS, whichever comes first, and Stage A's _LeaseWindow applies the same bounds -- so the out-of-process auto-memory hooks can win the graph file lock. The constants bound hook lockout, not anything specific to the sweep, which is why both stages share them.`
- Replace the paragraph `# When #280 lands (blocked on upstream minigraf#322), the drop checkpoint is suppressed and N can safely go to 1 -- ...` with: `# When upstream minigraf#322 exposes OpenOptions (wal_checkpoint_threshold = usize::MAX suppresses the Drop checkpoint), N can safely go to 1 -- which is why this is a constant to lower rather than a structure to rewrite. #280 itself landed as this window, which amortises the drop checkpoint but does not remove it.`

- [ ] **Step 5: Adapt the existing Stage A hook test**

In `test_a_hook_writing_between_stage_a_leases_lands_in_both`, add right after `repo, graph = self._prepare(tmp_path, monkeypatch)`:

```python
        # #280: under a multi-commit window the lease after a reverse write is
        # a JOIN -- the graph is still held -- so the 1 s widening below would
        # lock the hook out instead of letting it in. A 1-commit window makes
        # every commit's boundary a real release again, which is the gap this
        # test widens (#347's per-release index commit).
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 1)
```

Also add one sentence to its docstring: `Since #280 the release after a reverse write is the Stage A window's boundary, so the window is pinned to 1 commit.`

- [ ] **Step 6: Run the new and adapted tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestLeaseWindow tests/test_mcp_server.py::TestStageAYieldsTheLock tests/test_mcp_server.py::TestIngestionCommitsTheIndexBeforeReleasingTheGraph tests/test_mcp_server.py::TestStageBYieldsTheLock -v`
Expected: all pass.

- [ ] **Step 7: Ablate**

One at a time; confirm the named failure, then restore:
- Delete `await window.ensure_open()` → `test_stage_a_drops_the_handle_once_per_window_not_per_commit` fails (drops == applied).
- In `_LeaseWindow.maybe_yield`, delete the clock operand → `test_the_clock_alone_closes_a_stage_a_window` fails (drops == 1).
- Replace the `try:`/`finally: await window.close()` with a plain `await window.close()` after the loop → `test_an_exception_mid_window_leaks_no_lease` fails (`lease_count` 1).
- Move `await window.maybe_yield()` above the shutdown check → `test_a_shutdown_mid_window_leaks_no_lease_and_pays_no_pause` fails on `elapsed < 5.0`.
- **Two-step ablation for the index test.**
  - Step 1: change the per-commit `async with _db_lease_async_committing_index(...)` to `async with db_lease_async() as db:`. `test_every_real_stage_a_release_commits_the_index` stays GREEN, because the window's own commit covers it. Record that.
  - Step 2: additionally change `_LeaseWindow.ensure_open` to use `db_lease_async()`. The test now FAILS.
  - This shows the window's committing lease is what guards Stage A's real releases.

- [ ] **Step 8: Full suite**

Run: `.venv/bin/python -m pytest -q -x -p no:randomly 2>&1 | tail -20`
Expected: `N passed, 1 xfailed` and no failures. A failure in a test that counts Stage A leases or assumes per-commit release is a real consequence of this change: read it, and adapt it the way Step 5 did, with a docstring sentence saying why. Don't weaken its assertion.

- [ ] **Step 9: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Hold one lease across each Stage A window instead of per commit (#280)

<body: mechanism, boundary placement, try/finally, adapted test and why, every ablation and its result>

Refs #280

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 3: `yield_s` in the per-commit trace

**Files:**
- Modify: `mcp_server.py`
  - `_IngestTrace.emit` (~line 3995).
  - The `_IngestTrace` docstring.
  - Stage A loop head and emit call (from Task 2).
- Modify: `evals/at_scale/probe_per_commit_cost.py`, in the `result["exploratory"]` block (~line 196).
- Test: `tests/test_mcp_server.py`, in `TestIngestTrace` (~line 29073) and `TestStageAYieldsTheLock`.

**Interfaces:**
- Consumes: `window.maybe_yield() -> float` (Task 1), wired at the loop head (Task 2).
- Produces: `_IngestTrace.emit(..., policy, yield_s: float = 0.0)`. Every record gains key `"yield_s"`. `probe_per_commit_cost` gains `exploratory["yield_s_total_seconds"]`.

- [ ] **Step 1: Write the failing tests**

In `TestIngestTrace`, add:

```python
    def test_record_carries_yield_s_defaulting_to_zero(self, tmp_path):
        import mcp_server
        path = tmp_path / "trace.jsonl"
        trace = mcp_server._IngestTrace(str(path))
        trace.emit(0, "fwd", "a", 0.0, 0.0, [], None)
        trace.emit(1, "rev", "b", 0.0, 0.0, [], None, yield_s=0.25)
        trace.close()
        records = self._read(path)
        assert [r["yield_s"] for r in records] == [0.0, 0.25]
```

In `TestStageAYieldsTheLock`, add:

```python
    @pytest.mark.asyncio
    async def test_the_trace_accounts_for_every_boundary(self, tmp_path, monkeypatch):
        """#280's lesson is that a drop the trace cannot see gets blamed on
        nothing. yield_s carries each boundary's drop + pause, accumulated
        onto the next EMITTED record -- so a boundary before a commit whose
        write fails (no record) is still counted, on the one after it."""
        import json
        import mcp_server
        repo, _graph = self._prepare(tmp_path, monkeypatch)
        trace_path = tmp_path / "trace.jsonl"
        monkeypatch.setenv("MINIGRAF_INGEST_TRACE_PATH", str(trace_path))
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 1)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_SECONDS", 10**9)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_PAUSE_SECONDS", 0.05)
        real_rev = mcp_server._reverse_apply
        failed = []

        def rev_spy(*a, **kw):
            if not failed:
                failed.append(1)
                raise RuntimeError("injected write failure (#280 trace test)")
            return real_rev(*a, **kw)

        spent = []
        real_yield = mcp_server._LeaseWindow.maybe_yield

        async def yield_spy(self_):
            s = await real_yield(self_)
            spent.append(s)
            return s

        monkeypatch.setattr(mcp_server, "_reverse_apply", rev_spy)
        monkeypatch.setattr(mcp_server._LeaseWindow, "maybe_yield", yield_spy)
        await mcp_server._run_ingestion(str(repo), "master")
        records = [json.loads(l) for l in trace_path.read_text().splitlines() if l.strip()]
        assert failed, "no write failure was injected -- test proves nothing"
        assert all("yield_s" in r for r in records)
        boundaries = [s for s in spent if s > 0]
        assert len(boundaries) >= 2
        assert sum(r["yield_s"] for r in records) == pytest.approx(sum(boundaries), abs=1e-9), (
            f"trace yield_s sums to {sum(r['yield_s'] for r in records):.4f}s but "
            f"the boundaries took {sum(boundaries):.4f}s -- boundary time was "
            f"dropped (e.g. across the failed commit that emitted no record)"
        )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest "tests/test_mcp_server.py::TestIngestTrace::test_record_carries_yield_s_defaulting_to_zero" "tests/test_mcp_server.py::TestStageAYieldsTheLock::test_the_trace_accounts_for_every_boundary" -v`
Expected: the first FAILS with `TypeError: emit() got an unexpected keyword argument 'yield_s'`; the second FAILS on `"yield_s" in r`.

- [ ] **Step 3: Implement**

In `_IngestTrace.emit`, add the parameter `yield_s: float = 0.0` after `policy`, and add `"yield_s": yield_s,` to `record` right after `"apply_s": apply_s,`.

In the `_IngestTrace` docstring, add a paragraph:

```
    `yield_s` (#280) is Stage A lease-window boundary time -- the handle
    drop (a full O(graph size) checkpoint in minigraf's `Drop for Inner`)
    plus the pause -- accumulated since the previous EMITTED record, so the
    sum over a trace equals total boundary time even across commits that
    emit no record. One drop is outside it: the window's final close after
    the last record.
```

In the Stage A loop:
- Before `try:` (next to the `window = _LeaseWindow(...)` construction) add `_trace_yield_s = 0.0`.
- Change the loop-head line `await window.maybe_yield()` to `_trace_yield_s += await window.maybe_yield()`.
- In the emit call, add `yield_s=_trace_yield_s` as the last argument. Immediately after the `_ingest_trace.emit(...)` call, inside the same `if` block, add `_trace_yield_s = 0.0`.

In `evals/at_scale/probe_per_commit_cost.py`, in `result["exploratory"]`, after `"apply_s_total_seconds": apply_total,` add:

```python
        # #280: Stage A lease-window boundary time (handle drop + pause).
        # Absent from traces written before #280, hence .get.
        "yield_s_total_seconds": sum(float(r.get("yield_s", 0.0)) for r in records),
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestTrace tests/test_mcp_server.py::TestIngestTraceWiring tests/test_mcp_server.py::TestStageAYieldsTheLock tests/test_at_scale_trace_fit.py -v`
Expected: all pass.

- [ ] **Step 5: Ablate**

Delete the `_trace_yield_s = 0.0` reset after emit → the trace test fails, because the sum over-counts. Then restore it, and instead make the emit pass `yield_s=0.0` → the trace test fails, because the sum is 0.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py evals/at_scale/probe_per_commit_cost.py
git commit -m "Record Stage A window boundary time as the trace's yield_s (#280)

<body + ablations>

Refs #280

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 4: A real hook lands mid-Stage A at shipped defaults

**Files:**
- Test: `tests/test_mcp_server.py`, in `TestStageAYieldsTheLock`.

**Interfaces:**
- Consumes: the Stage A window (Task 2); `fact_index.open_reader`, `fact_index.index_path_for`; the hook-script pattern from `test_a_hook_writing_between_stage_a_leases_lands_in_both`; `_subprocess` (already imported in the test module under that name).
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
    @pytest.mark.asyncio
    async def test_a_hook_lands_mid_stage_a_at_shipped_defaults(
        self, tmp_path, monkeypatch
    ):
        """A real hook process doing a real handle_minigraf_transact while
        Stage A is running, with the window at its SHIPPED constants (25
        commits / 2.0 s / 0.1 s). Every Stage A write is slowed to 0.4 s so
        Stage A (~4.8 s) outlasts the hook's ~2.6 s acquire budget -- the hook
        can only succeed if a window boundary releases the graph within that
        budget. The positive control requires the write to land BEFORE
        Stage A ended, so a hook that merely waited for Stage B proves
        nothing."""
        import fact_index
        import ingest_progress
        import mcp_server
        repo, graph = self._prepare(tmp_path, monkeypatch)

        go = tmp_path / "hook_go"
        ready = tmp_path / "hook_ready"
        script = (
            "import json, os, sys, time\n"
            f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})\n"
            "import mcp_server\n"
            f"open({str(ready)!r}, 'w').close()\n"
            f"while not os.path.exists({str(go)!r}):\n"
            "    time.sleep(0.002)\n"
            "started = time.time()\n"
            "try:\n"
            "    r = mcp_server.handle_minigraf_transact(\n"
            "        '[[:decision/hook-280 :description \"written mid stage A\"]]', 'hook')\n"
            "    print(json.dumps({'ok': bool(r.get('ok')), 'err': r.get('error'),\n"
            "                      'started': started, 'written_at': time.time()}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'err': repr(e), 'started': started,\n"
            "                      'written_at': time.time()}))\n"
            "sys.stdout.flush()\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGRAF_")}
        env["MINIGRAF_GRAPH_PATH"] = str(graph)
        proc = _subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, text=True, env=env,
        )
        state = {"stage_a_end": None}
        try:
            deadline = time.monotonic() + 60
            while not ready.exists():
                assert proc.poll() is None, proc.communicate()
                assert time.monotonic() < deadline, "the hook process never got ready"
                await asyncio.sleep(0.01)

            real_fwd = mcp_server._forward_apply
            real_rev = mcp_server._reverse_apply
            real_finished = ingest_progress.RunProgress.stage_a_finished

            def slow(real):
                def spy(*a, **kw):
                    out = real(*a, **kw)
                    if not kw.get("lifecycle_only"):
                        go.touch()
                        time.sleep(0.4)  # write-executor thread, not the event loop
                    return out
                return spy

            def finished_spy(self_, *a, **kw):
                state["stage_a_end"] = time.time()
                return real_finished(self_, *a, **kw)

            monkeypatch.setattr(mcp_server, "_forward_apply", slow(real_fwd))
            monkeypatch.setattr(mcp_server, "_reverse_apply", slow(real_rev))
            monkeypatch.setattr(ingest_progress.RunProgress, "stage_a_finished", finished_spy)
            await mcp_server._run_ingestion(str(repo), "master")
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        lines = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
        assert lines, f"the hook printed no result. stdout={out!r} stderr={err!r}"
        hook = json.loads(lines[-1])
        assert mcp_server._ingest_progress.get("status") == "complete", mcp_server._ingest_progress
        assert hook["ok"], (
            f"the hook could not write while Stage A ran at shipped window "
            f"settings: {hook!r}. The window held the graph longer than the "
            f"hook's acquire budget. stderr tail: {err[-2000:]!r}"
        )
        assert state["stage_a_end"] is not None
        assert hook["written_at"] < state["stage_a_end"], (
            f"the hook wrote at {hook['written_at']:.3f}, after Stage A ended at "
            f"{state['stage_a_end']:.3f} -- it did not get in through a Stage A "
            f"boundary, so this test proved nothing"
        )

        mcp_server._reset_db_state()
        with mcp_server.db_lease() as db:
            graph_rows = json.loads(mcp_server._db_execute(
                db, '(query [:find ?d :where [:decision/hook-280 :description ?d]])'
            )).get("results", [])
        con = fact_index.open_reader(fact_index.index_path_for(str(graph)))
        try:
            index_rows = con.execute(
                "select value from facts_fts where entity = ':decision/hook-280'"
            ).fetchall()
        finally:
            con.close()
        assert graph_rows, "the hook's fact is not in the graph"
        assert index_rows, "the hook's fact reached the graph but not the index (#302)"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest "tests/test_mcp_server.py::TestStageAYieldsTheLock::test_a_hook_lands_mid_stage_a_at_shipped_defaults" -v`
Expected: PASS.

- [ ] **Step 3: Ablate**

Add temporarily at the top of the test body: `monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 10**9)` and `monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_SECONDS", 10**9)`. The window then never closes during Stage A. Expected: FAIL on `hook["ok"]`, with the hook's lock error in `err`. Remove the ablation.

- [ ] **Step 4: Stability check (timing-sensitive test)**

Run: `for i in $(seq 20); do .venv/bin/python -m pytest -q "tests/test_mcp_server.py::TestStageAYieldsTheLock::test_a_hook_lands_mid_stage_a_at_shipped_defaults" 2>&1 | tail -1; done`
Expected: 20/20 passed. Then repeat under load: run `stress-ng --cpu $(nproc) --timeout 120s &`, or failing that a `yes > /dev/null` per core, while running 10 more iterations.
- If any iteration fails, STOP and report the failure output. That means the shipped 2.0 s window is too tight for the hook budget under load, which is a design finding. Do not loosen the test.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "Test that a real hook lands mid-Stage A at shipped window settings (#280)

<body: ablation result, 20/20 + 10/10-under-load stability>

Refs #280

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 5: Promote the spike instrument and measure

**Files:**
- Modify: `evals/at_scale/probe_lease_drop_cost.py`
- Create: `evals/at_scale/results/280-stage-a-window-ab.json`
- Modify: `evals/at_scale/benchmark.md`. Add a new section after the "Handle drop — the attributed cause" section (~line 1287).

**Interfaces:**
- Consumes: the branch's Stage A window (Tasks 1–3); a master worktree for the baseline arm.
- Produces: the probe's result JSON keys `per_phase` (dict phase → `{opens, opens_s, drops, drops_s, wal_writes, wal_writes_s, auto_ckpt, auto_ckpt_s, explicit_ckpt, explicit_ckpt_s, explicit_ckpt_wal_shrank}`) and `code_dir`, plus every pre-existing key unchanged.

- [ ] **Step 1: Extend the probe**

In `probe_lease_drop_cost.py`:

1. Replace `sys.path.insert(0, REPO)` with:
   ```python
   # PROBE_CODE_DIR selects WHICH checkout's mcp_server is measured, so a
   # master worktree can be the A arm while this probe file stays the same
   # (#280). Defaults to this repo.
   CODE_DIR = os.environ.get("PROBE_CODE_DIR", REPO)
   sys.path.insert(0, CODE_DIR)
   ```
2. After `import mcp_server as m`, add `WAL_PATH = os.environ["MINIGRAF_GRAPH_PATH"] + ".wal"` and `assert os.path.dirname(os.path.abspath(m.__file__)) == os.path.abspath(CODE_DIR), m.__file__`.
3. Add phase-keyed counters and wrappers, ported from the spike (`spike280.py` in the session scratchpad; the code is given in full here):
   ```python
   import threading
   _lock = threading.Lock()
   per_phase: dict = {}

   def _phase() -> str:
       return m._ingest_progress.get("phase") or "other"

   def _bump(phase, key, secs=None):
       with _lock:
           s = per_phase.setdefault(phase, {})
           s[key] = s.get(key, 0) + 1
           if secs is not None:
               s[key + "_s"] = s.get(key + "_s", 0.0) + secs

   def _wal_size() -> int:
       try:
           return os.path.getsize(WAL_PATH)
       except OSError:
           return 0

   def install_wal_counters() -> None:
       """Auto-checkpoint detection: minigraf deletes <graph>.wal on every
       checkpoint (do_checkpoint, src/db.rs), and auto-checkpoint runs inside
       the transact that crosses wal_checkpoint_threshold (1000 entries,
       one per transact/retract). So a write after which the WAL SHRANK was
       an auto-checkpoint. Positive control: explicit checkpoints are counted
       the same way (explicit_ckpt_wal_shrank), and the suppressed-duty run
       in benchmark.md shows the detector firing."""
       orig_exec, orig_ckpt = m._db_execute, m._db_checkpoint

       def db_execute(db, datalog):
           if not (datalog.startswith("(transact") or datalog.startswith("(retract")):
               return orig_exec(db, datalog)
           p, before, t = _phase(), _wal_size(), time.perf_counter()
           out = orig_exec(db, datalog)
           el = time.perf_counter() - t
           _bump(p, "wal_writes", el)
           if _wal_size() < before:
               _bump(p, "auto_ckpt", el)
           return out

       def db_checkpoint(db):
           p, before, t = _phase(), _wal_size(), time.perf_counter()
           orig_ckpt(db)
           _bump(p, "explicit_ckpt", time.perf_counter() - t)
           if before and _wal_size() < before:
               _bump(p, "explicit_ckpt_wal_shrank")

       m._db_execute = db_execute
       m._db_checkpoint = db_checkpoint
   ```
4. In the existing `install_counters` wrappers, also call `_bump(_phase(), "opens", <acquire seconds>)` on a 0→1 open and `_bump(<phase read before release>, "drops", elapsed)` on a 1→0 drop. Time the acquire around `orig_acquire(path)`.
5. In `main()`, call `install_wal_counters()` next to `install_counters()`. In `summarize`, add `"per_phase": per_phase, "code_dir": CODE_DIR, "phase_wall_s": <dict>`. Track `phase_wall_s` with a 50 ms asyncio watcher task around `_run_ingestion`, as the spike did, and fold in the final phase interval at the end.
6. In `report()`, print one line per phase: `opens drops drop_s writes auto_ckpt explicit_ckpt`.
7. After `_run_ingestion` returns and `summarize` has run, audit the graph in the same process. The graph is still bound to `m`, and `audit_graph_against_index` takes its own lease through `handle_minigraf_query`:
   ```python
   from evals.at_scale.fact_audit import audit_graph_against_index
   audit = audit_graph_against_index(
       os.environ["MINIGRAF_INDEX_PATH"],
       expected_graph_path=os.environ["MINIGRAF_GRAPH_PATH"],
   )
   with m.db_lease() as db:
       commit_entities = m._count_commit_entities(db)
   result["audit"] = {k: audit.get(k) for k in ("divergence", "audit_error", "graph_facts")}
   result["commit_entities"] = commit_entities
   ```
   Put this after the lease-counter reporting, so the audit's own leases never reach `opens`/`drops` or `per_phase`. `phase` is `None` ("other") by then, and the summary is already built.
8. Update the module docstring: add a "PHASE SPLIT AND AUTO-CHECKPOINTS (#280)" paragraph, the `PROBE_CODE_DIR` usage, and the positive-control command:
   ```
   MINIGRAF_INGEST_CHECKPOINT_DUTY=0.000001 MINIGRAF_SWEEP_YIELD_COMMITS=1000000000 \
   MINIGRAF_SWEEP_YIELD_SECONDS=1000000000 PYTHONHASHSEED=0 \
     .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref <250th commit>
   ```
   With it, `converging.auto_ckpt` must be ≥ 1. In the spike it was 3 out of 3940 writes.

- [ ] **Step 2: Smoke test and positive control**

```bash
REF250=$(git rev-list --reverse master | sed -n 250p)
PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref "$REF250"
MINIGRAF_INGEST_CHECKPOINT_DUTY=0.000001 MINIGRAF_SWEEP_YIELD_COMMITS=1000000000 \
  MINIGRAF_SWEEP_YIELD_SECONDS=1000000000 PYTHONHASHSEED=0 \
  .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref "$REF250"
```
Expected:
- First run: `converging` drops ≈ 250/25 + 1.
- Second run: `converging` `auto_ckpt` ≥ 1.
- If the positive control reads 0, STOP. The detector is blind and nothing measured with it is trustworthy.

- [ ] **Step 3: A/B runs.** These are long: 600 commits ≈ 12 min per run and full history ≈ 1 h per run, so run them in the background. Don't edit any file the runs import while they run.

```bash
git worktree add ../tr-master-280 master
REF600=$(git rev-list --reverse master | sed -n 600p)
OUT=evals/at_scale/results/_280_runs; mkdir -p $OUT
for r in 1 2; do
  PROBE_CODE_DIR=$(realpath ../tr-master-280) PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref "$REF600" --out $OUT/600-master-$r.json
  PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref "$REF600" --out $OUT/600-branch-$r.json
done
PROBE_CODE_DIR=$(realpath ../tr-master-280) PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref master --out $OUT/full-master.json
PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref master --out $OUT/full-branch.json
```

Before trusting any master-arm result, check that its printed `code_dir` is the worktree. The probe's own assert guarantees the import came from there.

- [ ] **Step 4: Graph equivalence**

Read `audit` and `commit_entities` out of each run's JSON (Step 1 item 7). Expected:
- `audit.audit_error` is None and `audit.divergence == 0` on every run.
- `commit_entities` is equal between the two arms at the same ref.
- If `audit_error` is set, the audit never scanned the graph, so the run's result is untrusted and must be re-run.

- [ ] **Step 5: Write the result file and the benchmark section**

Collect everything into `evals/at_scale/results/280-stage-a-window-ab.json`, then delete `$OUT` and the worktree (`git worktree remove ../tr-master-280`). The file holds:
- the per-run JSONs keyed by arm and rep;
- a `verdict` block: Stage A (`converging`) wall, drops per commit, drop+open seconds, and auto_ckpt per arm, with medians and spread;
- the audit results and commit counts.

Acceptance, all required:
- Branch Stage A drops per commit ≤ 0.06, against ~1.0 on master.
- Branch Stage A wall ≤ 0.80× master at 600 commits.
- Stage A `auto_ckpt` ≤ 1 per run at 600.
- Divergence 0 and equal commit counts at full history.

If the full-history Stage A auto-checkpoint count is large, record it as a finding in the verdict. Don't widen scope.

Add a `### Stage A lease window — #280` section to `evals/at_scale/benchmark.md`, following the section above it: script, raw file, question, a metrics table with both arms, and verdict. It must state the batch caveat: absolute numbers are comparable only within this interleaved batch.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_lease_drop_cost.py evals/at_scale/results/280-stage-a-window-ab.json evals/at_scale/benchmark.md
git commit -m "Measure the Stage A lease window against master (#280)

<body: headline numbers, positive control result, acceptance verdict>

Refs #280

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Check only: `SKILL.md`. Expected: no change; confirm with `grep -n "lease\|drop\|checkpoint" SKILL.md`.

**Interfaces:**
- Consumes: Task 5's measured numbers.
- Produces: nothing.

- [ ] **Step 1: Update CLAUDE.md**

1. In "**Dropping the handle is not free: it runs a full O(graph size) checkpoint**", replace "Ingestion currently drops it ~1.02 times per commit, measured at 48% of write time and growing 3.47x within a 220-commit run (#280)." with a statement of the pre-#280 figure as history plus Task 5's measured windowed figures (drops per commit, Stage A wall change, both at 600 commits and full history). Name `_LeaseWindow`.
2. In "**Stage B now yields its lease on a bounded window ...**", add a paragraph headed "**Stage A takes the same window since #280.**". It covers: `_LeaseWindow`; the per-commit lease joins at 1→2; the boundary is at the loop head, after the shutdown check; `try/finally` closes it on every exit; shared constants; `yield_s`; the adapted `test_a_hook_writing_between_stage_a_leases_lands_in_both`, pinned to 1 commit and why; and the Stage B auto-checkpoint observation (≈237 WAL writes per swept commit, so its window crosses minigraf's 1000-entry auto-checkpoint: 77 firings = 24 s at 600 commits in the spike), recorded as out of scope.
3. Change "When #280 lands (blocked on upstream minigraf#322) the drop checkpoint is suppressed and N can safely go to 1" to name minigraf#322 as the condition, matching Task 2 Step 4.
4. Verify every mechanism claim against the code, never against neighbouring prose (standing rule): grep each function and constant named in the new text.

- [ ] **Step 2: Scan for closing keywords across the branch**

Run: `git log master..HEAD --format=%B | grep -niE "\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^.]*#[0-9]+"`
Expected: no output.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -5`
Expected: all passed, 1 xfailed.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the Stage A lease window (#280)

Refs #280

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```
