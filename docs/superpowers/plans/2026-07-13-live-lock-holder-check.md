# Live Lock-Holder Check for Auto-Ingest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before starting ingestion (at server boot or via an explicit `minigraf_ingest_git` call), check whether another live process already owns `<graph>.lock`, and skip starting ingestion in this process if so, instead of racing for the lock and losing.

**Architecture:** A new helper, `_live_lock_holder_pid(path)`, reads the sidecar `<path>.lock` file directly (no DB open attempt) and returns the holder's PID only if that PID is confirmed alive and isn't our own process. Both ingestion entry points (`main()`'s boot auto-start, and `handle_minigraf_ingest_git`) call it before creating an ingestion task; on a live hit they set `_ingest_progress["status"] = "skipped"` + `owner_pid` instead of starting `_run_ingestion`. `minigraf_ingest_status` already spreads `_ingest_progress` into its response, so `owner_pid` and the new `skipped` status are exposed with no handler changes.

**Tech Stack:** Python 3, asyncio, pytest + pytest-asyncio, unittest.mock.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-13-live-lock-holder-check-design.md` (issue #108).
- The lock file at `<graph_path>.lock` contains the holder's raw PID as plain text (written by minigraf's Rust `FileLock::acquire` — see spec). Liveness is checked via `os.kill(pid, 0)`.
- Conservative bias on uncertainty: if we can't confirm the holder is dead (`PermissionError`, other `OSError`, or the `os.kill` call succeeds), treat it as alive. Only `ProcessLookupError` counts as dead. This mirrors the existing `_clear_stale_lock` (mcp_server.py:772) semantics — don't invert it.
- A PID equal to our own (`os.getpid()`) is never a blocker (mirrors the Rust `acquire()`'s `pid == our_pid` self-lock handling).
- This check is best-effort/racy (TOCTOU) by design — it must not attempt to replace or bypass the existing retry/self-heal machinery (`_try_open_with_self_heal`, `_open_db_at_with_retry`, `_ensure_db_async`), only run *before* it as a fast-path.
- A skip is permanent for that server process's lifetime — no automatic retry/backoff. Resuming requires a fresh `minigraf_ingest_git` call or a new server boot.
- All new tests must be hermetic: never rely on or touch the real repo's `memory.graph`/`memory.graph.lock` at cwd. Every test that reaches `_live_lock_holder_pid` (directly or via `main()`/`handle_minigraf_ingest_git`) must pin `mcp_server._graph_path` or `MINIGRAF_GRAPH_PATH` to a `tmp_path`-based location, or mock `_live_lock_holder_pid` itself.

---

### Task 1: `_live_lock_holder_pid` helper

**Files:**
- Modify: `mcp_server.py` (insert new function after `_clear_stale_lock`, mcp_server.py:772-790, before `_try_open_with_self_heal` at mcp_server.py:793)
- Test: `tests/test_mcp_server.py` (new `TestLiveLockHolderPid` class, insert after `TestGetDbLockRetry` which ends at tests/test_mcp_server.py:207-208, before `class TestMinigrafQuery:` at tests/test_mcp_server.py:210)

**Interfaces:**
- Produces: `_live_lock_holder_pid(path: str) -> Optional[int]` — module-level function in `mcp_server.py`. Returns the PID recorded in `<path>.lock` if and only if that PID is not our own and is confirmed (or presumed, on uncertainty) alive; otherwise `None`.

- [ ] **Step 1: Write the failing tests**

Add this new class to `tests/test_mcp_server.py`, immediately after the `TestGetDbLockRetry` class (after line 208, before `class TestMinigrafQuery:` on line 210):

```python
class TestLiveLockHolderPid:
    """Unit tests for _live_lock_holder_pid — the proactive pre-check used
    to avoid racing another live process for the ingestion lock (#108)."""

    def test_no_lock_file_returns_none(self, tmp_path):
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        assert mcp_server._live_lock_holder_pid(graph_path) is None

    def test_unparsable_content_returns_none(self, tmp_path):
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        with open(graph_path + ".lock", "w") as f:
            f.write("not-a-pid")
        assert mcp_server._live_lock_holder_pid(graph_path) is None

    def test_dead_holder_returns_none(self, tmp_path):
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        dead_pid = 999999  # not running on any reasonable test machine
        with open(graph_path + ".lock", "w") as f:
            f.write(str(dead_pid))
        assert mcp_server._live_lock_holder_pid(graph_path) is None

    def test_live_holder_returns_pid(self, tmp_path, monkeypatch):
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        other_pid = 424242
        with open(graph_path + ".lock", "w") as f:
            f.write(str(other_pid))
        monkeypatch.setattr(mcp_server.os, "kill", lambda pid, sig: None)
        assert mcp_server._live_lock_holder_pid(graph_path) == other_pid

    def test_own_pid_returns_none(self, tmp_path):
        """The lock file recording our own PID means a leaked handle from
        this same process, not another process — never a blocker."""
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        with open(graph_path + ".lock", "w") as f:
            f.write(str(os.getpid()))
        assert mcp_server._live_lock_holder_pid(graph_path) is None

    def test_permission_error_on_kill_treated_as_alive(self, tmp_path, monkeypatch):
        """Can't confirm death -> conservatively assume alive (matches
        _clear_stale_lock's existing bias)."""
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        other_pid = 424242
        with open(graph_path + ".lock", "w") as f:
            f.write(str(other_pid))

        def raise_permission_error(pid, sig):
            raise PermissionError()

        monkeypatch.setattr(mcp_server.os, "kill", raise_permission_error)
        assert mcp_server._live_lock_holder_pid(graph_path) == other_pid
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestLiveLockHolderPid -v`
Expected: every test FAILS with `AttributeError: module 'mcp_server' has no attribute '_live_lock_holder_pid'`

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, insert this function immediately after `_clear_stale_lock` (which ends at line 790 with `return False`) and before `def _try_open_with_self_heal` (line 793):

```python
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestLiveLockHolderPid -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _live_lock_holder_pid helper for proactive lock checks (#108)"
```

---

### Task 2: Wire the check into boot auto-start (`main()`)

**Files:**
- Modify: `mcp_server.py:78-81` (module-level `_ingest_progress` default — the dict literal spans these lines; the enclosing `_ingest_task` declaration on line 77 is untouched)
- Modify: `mcp_server.py:3656-3669` (`main()`)
- Test: `tests/test_mcp_server.py` (new `TestMainAutoIngestLockCheck` class, insert after `TestMainShutdown` which ends at tests/test_mcp_server.py:3115, before `class TestOrphanWatchdog:` at tests/test_mcp_server.py:3118)

**Interfaces:**
- Consumes: `_live_lock_holder_pid(path: str) -> Optional[int]` from Task 1.
- Produces: `main()` now sets `_ingest_progress["status"] = "skipped"` and `_ingest_progress["owner_pid"] = <pid>` instead of creating an ingest task, when a live holder is found. `_ingest_progress` gains a permanent `"owner_pid"` key (default `None`) wherever it's reset.

- [ ] **Step 1: Write the failing tests**

Add this new class to `tests/test_mcp_server.py`, immediately after `TestMainShutdown` (after line 3115), before `class TestOrphanWatchdog:` (line 3118):

```python
class TestMainAutoIngestLockCheck:
    @pytest.mark.asyncio
    async def test_skips_auto_ingest_when_live_holder_present(self, monkeypatch, tmp_path):
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: 424242)
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None

        @contextlib.asynccontextmanager
        async def fake_stdio_server():
            yield (object(), object())

        monkeypatch.setattr(mcp_server, "stdio_server", fake_stdio_server)

        run_started = asyncio.Event()

        async def fake_run(read_stream, write_stream, init_opts):
            run_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(mcp_server.server, "run", fake_run)

        main_task = asyncio.create_task(mcp_server.main())
        await run_started.wait()
        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)

        assert mcp_server._ingest_task is None
        assert mcp_server._ingest_progress["status"] == "skipped"
        assert mcp_server._ingest_progress["owner_pid"] == 424242

    @pytest.mark.asyncio
    async def test_starts_auto_ingest_when_no_live_holder(self, monkeypatch, tmp_path):
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        async def fake_run_ingestion(repo_path, branch):
            await asyncio.Event().wait()  # never completes on its own

        monkeypatch.setattr(mcp_server, "_run_ingestion", fake_run_ingestion)
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None

        @contextlib.asynccontextmanager
        async def fake_stdio_server():
            yield (object(), object())

        monkeypatch.setattr(mcp_server, "stdio_server", fake_stdio_server)

        run_started = asyncio.Event()

        async def fake_run(read_stream, write_stream, init_opts):
            run_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(mcp_server.server, "run", fake_run)

        main_task = asyncio.create_task(mcp_server.main())
        await run_started.wait()

        assert mcp_server._ingest_task is not None
        assert mcp_server._ingest_progress["status"] != "skipped"

        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestMainAutoIngestLockCheck -v`
Expected: `test_skips_auto_ingest_when_live_holder_present` FAILS (status is `"idle"`, not `"skipped"`; `owner_pid` KeyError). `test_starts_auto_ingest_when_no_live_holder` currently PASSES already (no regression to check yet) — that's fine, it becomes a real regression guard once Step 3 lands.

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, update the module-level `_ingest_progress` default (lines 77-81) to add the `owner_pid` key:

```python
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None,
}
```

Then replace `main()`'s auto-start block (lines 3656-3669):

```python
async def main() -> None:
    global _server_ref, _ingest_task, _ingest_progress, _launch_ppid
    _server_ref = server
    _launch_ppid = os.getppid()
    # Auto-start incremental ingest on server startup so ingestion begins
    # immediately without waiting for a user prompt.  Runs as a background
    # asyncio task — never blocks the message loop.
    # Set MINIGRAF_NO_AUTO_INGEST=1 to skip auto-start (used by eval sandboxes).
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None,
    }
    if not os.environ.get("MINIGRAF_NO_AUTO_INGEST"):
        _ingest_task = asyncio.create_task(_run_ingestion(str(Path.cwd()), "HEAD"))
```

with:

```python
async def main() -> None:
    global _server_ref, _ingest_task, _ingest_progress, _launch_ppid
    _server_ref = server
    _launch_ppid = os.getppid()
    # Auto-start incremental ingest on server startup so ingestion begins
    # immediately without waiting for a user prompt.  Runs as a background
    # asyncio task — never blocks the message loop.
    # Set MINIGRAF_NO_AUTO_INGEST=1 to skip auto-start (used by eval sandboxes).
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None,
    }
    if not os.environ.get("MINIGRAF_NO_AUTO_INGEST"):
        # Proactive check-before-attempt: if another live process already
        # owns the graph lock, don't start ingestion here at all rather
        # than racing for it and losing (#108).
        holder_pid = _live_lock_holder_pid(_get_graph_path())
        if holder_pid is not None:
            print(
                f"[ingestion] skipped: already owned by live pid {holder_pid}",
                file=sys.stderr,
            )
            _ingest_progress["status"] = "skipped"
            _ingest_progress["owner_pid"] = holder_pid
        else:
            _ingest_task = asyncio.create_task(_run_ingestion(str(Path.cwd()), "HEAD"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestMainAutoIngestLockCheck -v`
Expected: both tests PASS

Run the full existing main/orphan-watchdog suite to confirm no regressions: `python -m pytest tests/test_mcp_server.py::TestMainShutdown tests/test_mcp_server.py::TestOrphanWatchdog -v`
Expected: all PASS (these set `MINIGRAF_NO_AUTO_INGEST=1`, so they never reach the new branch)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: skip boot auto-ingest when another live process owns the lock (#108)"
```

---

### Task 3: Wire the check into `handle_minigraf_ingest_git`

**Files:**
- Modify: `mcp_server.py:3281-3308` (`handle_minigraf_ingest_git`)
- Modify: `tests/test_mcp_server.py:2766-2800` (existing `test_handle_minigraf_ingest_git_returns_immediately`, `test_second_call_while_running_returns_error`, `test_returns_error_for_invalid_repo` — pin `_graph_path` to a tmp path so they stay hermetic once the new check runs unconditionally)
- Test: `tests/test_mcp_server.py` (new test in the same class as the tests above, immediately after `test_returns_error_for_invalid_repo`, which ends at line 2800, before `test_processed_seeded_from_prior_ingested` at line 2803)

**Interfaces:**
- Consumes: `_live_lock_holder_pid(path: str) -> Optional[int]` from Task 1.
- Produces: `handle_minigraf_ingest_git` returns `{"ok": False, "error": "ingestion already owned by live process (pid <N>)", "owner_pid": <N>}` and does not create an ingest task when a live holder is found.

- [ ] **Step 1: Update the three existing tests for hermetic isolation, and write the new failing test**

In `tests/test_mcp_server.py`, update `test_handle_minigraf_ingest_git_returns_immediately` (lines 2765-2778) to pin `_graph_path`:

```python
    @pytest.mark.asyncio
    async def test_handle_minigraf_ingest_git_returns_immediately(self, mock_minigraf_db, git_repo, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is True
        assert "job_id" in result
```

Update `test_second_call_while_running_returns_error` (lines 2779-2792):

```python
    @pytest.mark.asyncio
    async def test_second_call_while_running_returns_error(self, mock_minigraf_db, git_repo, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is False
        assert "already in progress" in result["error"]
```

Update `test_returns_error_for_invalid_repo` (lines 2794-2800):

```python
    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_repo(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        result = await mcp_server.handle_minigraf_ingest_git(repo_path="/nonexistent/path")
        assert result["ok"] is False
        assert "Not a git repository" in result["error"]
```

Then add this new test immediately after it (before `test_processed_seeded_from_prior_ingested`):

```python
    @pytest.mark.asyncio
    async def test_skips_when_live_holder_present(self, mock_minigraf_db, git_repo, tmp_path, monkeypatch):
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: 424242)

        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))

        assert result["ok"] is False
        assert "424242" in result["error"]
        assert result["owner_pid"] == 424242
        assert mcp_server._ingest_task is None
        assert mcp_server._ingest_progress["status"] == "skipped"
        assert mcp_server._ingest_progress["owner_pid"] == 424242
```

- [ ] **Step 2: Run tests to verify the new test fails and the updated ones still pass**

Run: `python -m pytest tests/test_mcp_server.py -k "ingest_git" -v`
Expected: `test_skips_when_live_holder_present` FAILS (`ok` is `True`, ingestion started instead of skipped). The three updated tests PASS unchanged (they didn't depend on new behavior, just added a `_graph_path` pin).

- [ ] **Step 3: Write the implementation**

In `mcp_server.py`, replace `handle_minigraf_ingest_git` (lines 3281-3308):

```python
async def handle_minigraf_ingest_git(
    repo_path: Optional[str] = None,
    branch: str = "HEAD",
) -> Dict[str, Any]:
    """Start background git ingestion. Returns immediately."""
    global _ingest_task, _ingest_progress
    if _ingest_task and not _ingest_task.done():
        return {"ok": False, "error": "ingestion already in progress"}
    repo = repo_path or str(Path.cwd())
    try:
        check = _subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo, capture_output=True, text=True,
        )
        valid = check.returncode == 0
    except OSError:
        valid = False
    if not valid:
        return {
            "ok": False,
            "error": f"Not a git repository (or git not found): {repo}",
        }
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None,
    }
    _ingest_task = asyncio.create_task(_run_ingestion(repo, branch))
    return {"ok": True, "job_id": "git-ingest", "message": f"Ingestion started for {repo}"}
```

with:

```python
async def handle_minigraf_ingest_git(
    repo_path: Optional[str] = None,
    branch: str = "HEAD",
) -> Dict[str, Any]:
    """Start background git ingestion. Returns immediately."""
    global _ingest_task, _ingest_progress
    if _ingest_task and not _ingest_task.done():
        return {"ok": False, "error": "ingestion already in progress"}
    # Proactive check-before-attempt: don't start ingestion in this process
    # if another live process already owns the graph lock (#108).
    holder_pid = _live_lock_holder_pid(_graph_path or _get_graph_path())
    if holder_pid is not None:
        _ingest_progress["status"] = "skipped"
        _ingest_progress["owner_pid"] = holder_pid
        return {
            "ok": False,
            "error": f"ingestion already owned by live process (pid {holder_pid})",
            "owner_pid": holder_pid,
        }
    repo = repo_path or str(Path.cwd())
    try:
        check = _subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo, capture_output=True, text=True,
        )
        valid = check.returncode == 0
    except OSError:
        valid = False
    if not valid:
        return {
            "ok": False,
            "error": f"Not a git repository (or git not found): {repo}",
        }
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None,
    }
    _ingest_task = asyncio.create_task(_run_ingestion(repo, branch))
    return {"ok": True, "job_id": "git-ingest", "message": f"Ingestion started for {repo}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py -k "ingest_git" -v`
Expected: all PASS, including `test_skips_when_live_holder_present`

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: skip manual minigraf_ingest_git when another live process owns the lock (#108)"
```

---

### Task 4: Status reporting test and docs

**Files:**
- Modify: `mcp_server.py:3557-3563` (`minigraf_ingest_status` tool description)
- Modify: `SKILL.md:263, 294-297` (status vocabulary + ingest_git section)
- Test: `tests/test_mcp_server.py` (new test in `TestMinigrafIngestStatus`, insert after `test_running_status_skips_graph_query` which ends at line 1671, before `test_returns_total_ingested_from_graph` at line 1673)

**Interfaces:**
- Consumes: `_ingest_progress` dict shape from Tasks 2/3 (`status`, `owner_pid` keys).
- Produces: none new — this task verifies `owner_pid` propagates through `handle_minigraf_ingest_status`'s existing dict spread, and brings docs in line with the new `skipped` status.

- [ ] **Step 1: Write the failing test**

Add this test to the `TestMinigrafIngestStatus` class in `tests/test_mcp_server.py`, immediately after `test_running_status_skips_graph_query` (after line 1671), before `test_returns_total_ingested_from_graph` (line 1673):

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

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestMinigrafIngestStatus::test_reports_owner_pid_when_skipped -v`

This should actually already PASS, since Tasks 2/3 already made `owner_pid` a real key in `_ingest_progress` and `handle_minigraf_ingest_status` has always spread the whole dict (mcp_server.py:3313, unchanged). Confirm this — it's a regression guard, not new behavior, so no implementation step follows. If it fails, that means Task 2 or 3's `_ingest_progress` wiring is incomplete — go back and check those tasks before proceeding.

- [ ] **Step 3: Update the tool description in `mcp_server.py`**

In `mcp_server.py`, find the `minigraf_ingest_status` tool definition (lines 3557-3563):

```python
    Tool(
        name="minigraf_ingest_status",
        description=(
            "Return the current git ingestion progress. "
            "status is one of: idle, running, complete, error."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
```

Replace the description with:

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

- [ ] **Step 4: Update `SKILL.md`**

In `SKILL.md`, update the `minigraf_ingest_git` section's example (around line 274-276):

```
# If already running:
# → {"ok": false, "error": "ingestion already in progress"}
```

Replace with:

```
# If already running:
# → {"ok": false, "error": "ingestion already in progress"}

# If another live process already owns the graph lock:
# → {"ok": false, "error": "ingestion already owned by live process (pid 12345)", "owner_pid": 12345}
```

Then update the status vocabulary line (around line 294-297):

```
`status` is one of: `idle`, `running`, `complete`, `error`, `stopped`. `stopped`
means a graceful shutdown (session end) paused ingestion between commits —
not a failure; the next `minigraf_ingest_git` call (or server auto-start)
resumes from the watermark automatically. `processed` is the
```

Replace with:

```
`status` is one of: `idle`, `running`, `complete`, `error`, `stopped`, `skipped`.
`stopped` means a graceful shutdown (session end) paused ingestion between commits —
not a failure; the next `minigraf_ingest_git` call (or server auto-start)
resumes from the watermark automatically. `skipped` means another live process
already owns the graph lock (its PID is in `owner_pid`) — this server will not
attempt ingestion on its own; call `minigraf_ingest_git` again later to retry.
`processed` is the
```

- [ ] **Step 5: Run the full test suite to confirm no regressions**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py SKILL.md tests/test_mcp_server.py
git commit -m "docs: document skipped ingest status and owner_pid (#108)"
```

---

## Self-Review Notes

- **Spec coverage:** helper (Task 1), boot call site (Task 2), manual call site (Task 3), status reporting + docs (Task 4) — all spec sections covered. No auto-retry was explicitly a non-goal and no task implements it.
- **Hermetic tests:** every test touching `_live_lock_holder_pid` either uses `tmp_path` directly or mocks the function — none rely on or can accidentally touch this repo's real `memory.graph`/`memory.graph.lock`, which was a genuine risk identified while reading the existing `handle_minigraf_ingest_git` tests (they didn't pin `_graph_path` before this plan).
- **Type/signature consistency:** `_live_lock_holder_pid(path: str) -> Optional[int]` is defined once in Task 1 and consumed identically (same name, same signature) in Tasks 2 and 3.
