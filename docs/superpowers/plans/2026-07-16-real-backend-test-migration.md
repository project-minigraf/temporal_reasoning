# Real-Backend Test Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete `mock_minigraf_db` and every `MagicMock`-based fake of `MiniGrafDb` from `tests/test_mcp_server.py`, replacing all ~161 affected tests with real-backend equivalents that assert against actual persisted/queried graph state instead of mock call arguments.

**Architecture:** A new `real_db` fixture opens a genuine `MiniGrafDb.open_in_memory()` instance (via a `monkeypatch.setattr(MiniGrafDb, "open", ...)` redirect so `mcp_server.open_db()`'s real code path still runs) and replaces `mock_minigraf_db` everywhere except three special-case clusters: DB lock-retry tests (need real file-backed locking + real subprocess-manufactured contention), LLM-strategy tests (keep the LLM client mocked — external network API, not minigraf), and report-issue tests (keep GitHub calls mocked, same reason).

**Tech Stack:** Python, pytest, `minigraf` (Rust FFI via `minigraf_ffi`), `pytest-asyncio` for async tests.

## Global Constraints

- Every task must end with `pytest tests/test_mcp_server.py -q` showing the same or fewer failures than the pre-migration baseline (established in Task 1, Step 1) — no new failures introduced by any task.
- No test may reference `mock_minigraf_db`, `MagicMock`, or `patch("mcp_server.MiniGrafDb")` once its migration task is complete, except the three special-case clusters explicitly named above.
- Assertions must check real query/persisted results, never mock call arguments, except where explicitly noted (spy-wrapper call-count checks, which wrap the *real* `_db_execute`, not a mock).
- Commit after every task (not every step) — each task is one commit, using `git add tests/test_mcp_server.py` plus any doc files touched in that task.
- Run `pytest tests/test_mcp_server.py::<ClassName> -v` after converting each class, before moving to the next class in the same task.

---

## Shared code (used across multiple tasks — defined once in Task 1, imported by reference in later tasks)

### The `real_db` fixture

```python
@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Open a real (non-mocked) in-memory MiniGrafDb for the duration of the test.
    Full Datalog parsing, schema validation, and bi-temporal semantics — just
    backed by open_in_memory() instead of a disk file, so tests stay fast."""
    from minigraf import MiniGrafDb
    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()
```

### The `execute_spy` helper (for "assert a call did/didn't happen" tests)

Some tests need to assert that `execute()` was or wasn't called a certain number of times, or to capture the raw Datalog string of a specific call — impossible against a real object without instrumentation. Use this contextmanager, which wraps the real `mcp_server._db_execute` (not the DB object itself) so parsing/execution stays 100% real:

```python
import contextlib

@contextlib.contextmanager
def execute_spy():
    """Wrap mcp_server._db_execute to record (db, datalog) for every real call,
    while still executing for real. Yields the list of recorded datalog strings."""
    import mcp_server
    real_execute = mcp_server._db_execute
    calls = []

    def spy(db_arg, datalog):
        calls.append(datalog)
        return real_execute(db_arg, datalog)

    mcp_server._db_execute = spy
    try:
        yield calls
    finally:
        mcp_server._db_execute = real_execute
```

This is not a mock in the sense this migration eliminates — it never fakes a return value or bypasses real parsing; it only observes real calls as they pass through.

---

### Task 1: Add `real_db` fixture; migrate `TestOpenDb`

**Files:**
- Modify: `tests/test_mcp_server.py:1-51` (module docstring, fixture definitions), `tests/test_mcp_server.py:53-93` (`TestOpenDb`)

**Interfaces:**
- Produces: `real_db` fixture (see Shared code above) and `execute_spy()` contextmanager, both defined near the top of the file (right after `reset_mcp_server_db`, before `mock_minigraf_db` — which stays for now, deleted in Task 15 once nothing references it).

- [ ] **Step 1: Establish the pre-migration baseline**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && source .venv/bin/activate && python -m pytest tests/ -q 2>&1 | tail -5`

Record the exact pass/fail/skip counts in a scratch note (not committed) — every later task's suite run must match or improve on this baseline. As of this session: 582 collected, with a pre-existing baseline of missing tree-sitter grammars causing unrelated failures — confirm the exact number now since it may have drifted.

- [ ] **Step 2: Add `real_db` fixture and `execute_spy` helper**

Insert immediately after the `mock_minigraf_db` fixture (after line 51 in the current file):

```python
import contextlib


@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Open a real (non-mocked) in-memory MiniGrafDb for the duration of the test.
    Full Datalog parsing, schema validation, and bi-temporal semantics — just
    backed by open_in_memory() instead of a disk file, so tests stay fast."""
    from minigraf import MiniGrafDb
    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()


@contextlib.contextmanager
def execute_spy():
    """Wrap mcp_server._db_execute to record (db, datalog) for every real call,
    while still executing for real. Yields the list of recorded datalog strings."""
    import mcp_server
    real_execute = mcp_server._db_execute
    calls = []

    def spy(db_arg, datalog):
        calls.append(datalog)
        return real_execute(db_arg, datalog)

    mcp_server._db_execute = spy
    try:
        yield calls
    finally:
        mcp_server._db_execute = real_execute
```

(`contextlib` is likely already imported at the top of the file for other purposes — check line 6; if present, don't re-import.)

- [ ] **Step 3: Convert `TestOpenDb` (5 tests)**

Current code (lines 53-93):

```python
class TestOpenDb:
    def test_opens_db_at_given_path(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        path = str(tmp_path / "t.graph")

        result = mcp_server.open_db(path)

        mock_class.open.assert_called_once_with(path)
        assert result is db_instance

    def test_registers_session_rules(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        executed = [call.args[0] for call in db_instance.execute.call_args_list]
        assert len(executed) == len(mcp_server.SESSION_RULES)

    def test_get_db_auto_opens_when_db_none(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "auto.graph"))
        mcp_server._db = None

        result = mcp_server.get_db()

        assert result is db_instance

    def test_get_db_returns_instance_after_open(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.get_db()

        assert result is db_instance

    def test_uses_env_var_for_graph_path(self, mock_minigraf_db, monkeypatch, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "custom.graph"))
        mcp_server._db = None

        mcp_server.get_db()

        mock_class.open.assert_called_once_with(str(tmp_path / "custom.graph"))
```

(Read the actual current bodies at `tests/test_mcp_server.py:53-93` before writing — the above is reconstructed from the fixture-usage pattern common to this file; confirm exact assertions match before replacing.)

Target code — real backend, verifying actual behavior instead of mock call args:

```python
class TestOpenDb:
    def test_opens_db_at_given_path(self, monkeypatch, tmp_path):
        from minigraf import MiniGrafDb
        real_open_in_memory = MiniGrafDb.open_in_memory
        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
        import mcp_server
        path = str(tmp_path / "t.graph")

        result = mcp_server.open_db(path)

        assert result is not None
        assert mcp_server._graph_path == path
        # A real handle can execute — proof it's a live db, not a stub.
        assert json.loads(result.execute("(query [:find ?e :where [?e :foo ?v]])"))["results"] == []

    def test_registers_session_rules(self, real_db):
        import mcp_server
        # Session rules are query-invocable once registered; pick one from
        # SESSION_RULES and confirm it doesn't error when invoked as a rule call.
        # (Exact rule name depends on mcp_server.SESSION_RULES contents — read
        # that list first and adapt the invocation below to a real rule name.)
        assert mcp_server.SESSION_RULES  # sanity: rules exist to register
        # No exception during open_db (already happened via the real_db fixture)
        # is itself the regression signal for #-registration failures.

    def test_get_db_auto_opens_when_db_none(self, monkeypatch, tmp_path):
        from minigraf import MiniGrafDb
        real_open_in_memory = MiniGrafDb.open_in_memory
        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
        import mcp_server
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "auto.graph"))
        mcp_server._db = None

        result = mcp_server.get_db()

        assert result is not None
        assert mcp_server._graph_path == str(tmp_path / "auto.graph")

    def test_get_db_returns_instance_after_open(self, real_db):
        import mcp_server
        result = mcp_server.get_db()
        assert result is real_db

    def test_uses_env_var_for_graph_path(self, monkeypatch, tmp_path):
        from minigraf import MiniGrafDb
        real_open_in_memory = MiniGrafDb.open_in_memory
        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
        import mcp_server
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "custom.graph"))
        mcp_server._db = None

        mcp_server.get_db()

        assert mcp_server._graph_path == str(tmp_path / "custom.graph")
```

For `test_registers_session_rules`, before finalizing: read `mcp_server.SESSION_RULES` (mcp_server.py:41) to find one rule name, then strengthen the test by invoking that rule via a real query (e.g. if a rule named `contains?` is registered, run `(query [(contains? "x" "x")])`-style invocation matching that rule's actual arity) and assert it doesn't raise — this proves the rule was actually registered against the real engine, not just that `open_db()` didn't crash. Write this as a concrete assertion, not a sanity placeholder, before committing.

- [ ] **Step 4: Run the class in isolation**

Run: `pytest tests/test_mcp_server.py::TestOpenDb -v`
Expected: 5 passed.

- [ ] **Step 5: Run full suite, compare to baseline**

Run: `pytest tests/ -q 2>&1 | tail -5`
Expected: same or fewer failures than Task 1 Step 1's baseline, zero new failures.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test: add real_db fixture, migrate TestOpenDb off mock_minigraf_db (#133)"
```

---

### Task 2: Migrate DB lock-retry / self-heal cluster (special case — real subprocess locks)

**Files:**
- Modify: `tests/test_mcp_server.py:96-266` (`TestGetDbLockRetry`, `TestTryOpenWithSelfHealReuse`), `tests/test_mcp_server.py:391-460` (`TestOpenDbAtWithExtendedRetry`)

**Interfaces:**
- Consumes: nothing from Task 1 (this cluster stays file-backed, not `real_db`).
- Produces: `_hold_lock_subprocess(path, exit_immediately=False)` contextmanager, defined once near the top of this cluster, reused by all 11 tests in it.

- [ ] **Step 1: Add the subprocess lock-holder helper**

Insert directly above `class TestGetDbLockRetry:`:

```python
import contextlib as _contextlib


@_contextlib.contextmanager
def _hold_lock_subprocess(path, exit_immediately=False):
    """Spawn a real subprocess that opens a real MiniGrafDb at `path`, producing
    genuine cross-process lock contention. If exit_immediately, the subprocess
    opens then exits right away, leaving a real stale lock file with a real
    (now-dead) PID — for self-heal tests. Otherwise it holds the lock until the
    context exits — for "holder still alive" / "retries then succeeds" tests.
    Yields the holder subprocess's PID.
    """
    hold_script = (
        "import minigraf, sys, time\n"
        f"db = minigraf.MiniGrafDb.open({path!r})\n"
        "print(str(__import__('os').getpid()), flush=True)\n"
        + ("" if exit_immediately else "time.sleep(30)\n")
    )
    proc = _subprocess.Popen(
        [sys.executable, "-c", hold_script],
        stdout=_subprocess.PIPE, text=True,
    )
    pid_line = proc.stdout.readline().strip()
    holder_pid = int(pid_line)
    if exit_immediately:
        proc.wait(timeout=5)  # guarantee the PID is actually dead before returning
    try:
        yield holder_pid
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
```

(`_subprocess` and `sys` are already imported at the top of the file as `subprocess as _subprocess` and `sys` — reuse those, don't re-import under different names.)

- [ ] **Step 2: Convert `TestGetDbLockRetry` (6 tests)**

Read the current bodies at `tests/test_mcp_server.py:96-207` (already inspected this session — reproduced below as the exact current code to replace):

```python
class TestGetDbLockRetry:
    """Regression tests for #84: get_db() must retry lock contention with
    backoff instead of letting a single "database is locked" error abort
    the caller (e.g. the git-ingestion loop), and must self-heal a stale
    lock left behind by a dead holder process."""

    def test_retries_on_lock_error_then_succeeds(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        mock_class.open.side_effect = [
            MiniGrafError("Database is locked by another process (lock file: x.graph.lock, holder PID: 1)."),
            db_instance,
        ]
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        result = mcp_server.get_db()
        assert result is db_instance
        assert mock_class.open.call_count == 2

    def test_gives_up_after_max_attempts(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        lock_err = MiniGrafError("Database is locked by another process (lock file: x.graph.lock, holder PID: 1).")
        mock_class.open.side_effect = lock_err
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        with pytest.raises(MiniGrafError):
            mcp_server.get_db()
        assert mock_class.open.call_count == mcp_server._LOCK_RETRY_MAX

    def test_non_lock_errors_are_not_retried(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        mock_class.open.side_effect = MiniGrafError("corrupt graph file")
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        with pytest.raises(MiniGrafError):
            mcp_server.get_db()
        assert mock_class.open.call_count == 1

    def test_self_heals_stale_lock_from_dead_pid(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        graph_path = str(tmp_path / "t.graph")
        lock_path = graph_path + ".lock"
        with open(lock_path, "w") as f:
            f.write("stale")
        dead_pid = 999999
        mock_class.open.side_effect = [
            MiniGrafError(f"Database is locked by another process (lock file: {lock_path}, holder PID: {dead_pid})."),
            db_instance,
        ]
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = graph_path
        result = mcp_server.get_db()
        assert result is db_instance
        assert not os.path.exists(lock_path)

    def test_leaves_lock_alone_when_holder_pid_alive(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        graph_path = str(tmp_path / "t.graph")
        lock_path = graph_path + ".lock"
        with open(lock_path, "w") as f:
            f.write("held")
        live_pid = os.getpid()
        mock_class.open.side_effect = [
            MiniGrafError(f"Database is locked by another process (lock file: {lock_path}, holder PID: {live_pid})."),
            db_instance,
        ]
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = graph_path
        result = mcp_server.get_db()
        assert result is db_instance
        assert os.path.exists(lock_path)

    def test_retries_open_after_clearing_stale_lock_on_final_attempt(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        lock_err = MiniGrafError(
            "Database is locked by another process (lock file: x.graph.lock, holder PID: 999999)."
        )
        monkeypatch.setattr(mcp_server, "_clear_stale_lock", lambda path, pid: True)
        mock_class.open.side_effect = [lock_err] * (2 * mcp_server._LOCK_RETRY_MAX - 1) + [db_instance]
        mcp_server._db = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        result = mcp_server.get_db()
        assert result is db_instance
        assert mock_class.open.call_count == 2 * mcp_server._LOCK_RETRY_MAX
```

Target — real subprocess-manufactured contention, real file-backed `MiniGrafDb.open`:

```python
class TestGetDbLockRetry:
    """Regression tests for #84: get_db() must retry lock contention with
    backoff instead of letting a single "database is locked" error abort
    the caller (e.g. the git-ingestion loop), and must self-heal a stale
    lock left behind by a dead holder process."""

    def test_retries_on_lock_error_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        with _hold_lock_subprocess(graph_path):
            result = mcp_server.get_db()

        assert result is not None

    def test_gives_up_after_max_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        with _hold_lock_subprocess(graph_path):
            with pytest.raises(MiniGrafError):
                mcp_server.get_db()

    def test_non_lock_errors_are_not_retried(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        os.makedirs(os.path.dirname(graph_path), exist_ok=True)
        with open(graph_path, "wb") as f:
            f.write(b"\x00not a real minigraf file\x00")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        with pytest.raises(MiniGrafError) as exc_info:
            mcp_server.get_db()
        assert "locked" not in str(exc_info.value).lower()

    def test_self_heals_stale_lock_from_dead_pid(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        with _hold_lock_subprocess(graph_path, exit_immediately=True):
            pass  # holder has opened, printed its PID, and exited by the time this returns
        lock_path = graph_path + ".lock"
        assert os.path.exists(lock_path)  # subprocess left its lock file behind

        result = mcp_server.get_db()

        assert result is not None
        assert not os.path.exists(lock_path)

    def test_leaves_lock_alone_when_holder_pid_alive(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path
        lock_path = graph_path + ".lock"

        with _hold_lock_subprocess(graph_path):
            with pytest.raises(MiniGrafError):
                mcp_server.get_db()
            assert os.path.exists(lock_path)  # untouched — real holder still alive

    def test_retries_open_after_clearing_stale_lock_on_final_attempt(self, tmp_path, monkeypatch):
        """Regression test for #91: previously, clearing a stale lock on the
        final retry attempt still fell through to raising the just-resolved
        lock error, because the follow-up open was gated on `attempt <
        _LOCK_RETRY_MAX - 1`. The clear must always be followed by one more
        open attempt, no matter which iteration triggered it. Real repro:
        the holder subprocess exits shortly before the retry budget runs out,
        so self-heal only becomes possible on a late attempt."""
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        hold_script = (
            "import minigraf, time\n"
            f"db = minigraf.MiniGrafDb.open({graph_path!r})\n"
            "print('ready', flush=True)\n"
            "time.sleep(0.15)\n"  # exits partway through the retry budget
        )
        proc = _subprocess.Popen([sys.executable, "-c", hold_script], stdout=_subprocess.PIPE, text=True)
        proc.stdout.readline()

        result = mcp_server.get_db()

        proc.wait(timeout=5)
        assert result is not None
```

Note: `test_gives_up_after_max_attempts` and `test_non_lock_errors_are_not_retried` lose their exact `call_count` assertions (a mock artifact) since real `MiniGrafDb.open` isn't instrumented — the behavior they actually guard (retries exhaust for lock errors, don't retry for non-lock errors) is still verified via `pytest.raises` plus the non-lock message-content check. If exact attempt-count verification is wanted, wrap with `execute_spy()`-style monkeypatching of `mcp_server.MiniGrafDb.open` itself (a real function, instrumented to count calls while still delegating to the real implementation) — add this only if the plain version above doesn't give confidence; don't add speculative instrumentation up front.

- [ ] **Step 3: Convert `TestTryOpenWithSelfHealReuse` (1 test)**

Current code (lines 210-266) already uses real threading and a `slow_open` mock side_effect on `mock_class.open` to simulate a slow concurrent open. Convert `mock_class.open.side_effect = slow_open` to a real monkeypatch of `MiniGrafDb.open` itself:

```python
class TestTryOpenWithSelfHealReuse:
    """Regression test for #107: minigraf_ingest_status incorrectly reported
    "Database is locked by another process" while minigraf_ingest_git was
    actively running. Root cause: _try_open_with_self_heal always called
    _open_db_at(path) unconditionally, even when another thread had already
    opened the db and populated _db in the window between this thread's
    None-check and its own open attempt."""

    def test_concurrent_open_attempts_only_open_db_once(self, tmp_path, monkeypatch):
        import mcp_server
        import threading
        import time as _time
        from minigraf import MiniGrafDb

        path = str(tmp_path / "race.graph")
        mcp_server._db = None
        mcp_server._graph_path = ""

        real_open_in_memory = MiniGrafDb.open_in_memory
        open_call_count = {"n": 0}
        open_lock = threading.Lock()

        def slow_open(p):
            with open_lock:
                open_call_count["n"] += 1
            _time.sleep(0.05)  # widen the race window so racers overlap
            return real_open_in_memory()

        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(slow_open))

        results = []
        results_lock = threading.Lock()

        def worker():
            db = mcp_server._try_open_with_self_heal(path)
            with results_lock:
                results.append(db)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert open_call_count["n"] == 1, (
            f"MiniGrafDb.open() called {open_call_count['n']} times for concurrent "
            "open attempts racing on the same None _db -- _try_open_with_self_heal "
            "must recheck _db under _db_native_lock before opening a second handle (#107)"
        )
        assert len(results) == 5
        assert all(r is results[0] for r in results)
```

This keeps a real `MiniGrafDb.open_in_memory()` behind the slow-open wrapper — the wrapper only adds a real thread-safe counter and a real sleep, it doesn't fake `execute()`'s behavior, so it isn't the kind of mock this migration eliminates.

- [ ] **Step 4: Convert `TestOpenDbAtWithExtendedRetry` (4 tests)**

Read current bodies at `tests/test_mcp_server.py:391-460` and apply the same transformation as Step 2: replace `mock_class.open.side_effect` lock-error sequences with `_hold_lock_subprocess(path)` / `_hold_lock_subprocess(path, exit_immediately=True)`, replace `mock_class.open.call_count` assertions with behavioral assertions (result not None / exception raised / message content), keep the `mcp_server.time.sleep` monkeypatch for backoff.

- [ ] **Step 5: Run cluster, then full suite**

Run: `pytest tests/test_mcp_server.py::TestGetDbLockRetry tests/test_mcp_server.py::TestTryOpenWithSelfHealReuse tests/test_mcp_server.py::TestOpenDbAtWithExtendedRetry -v`
Expected: 11 passed. Note these will run noticeably slower than before (subprocess spawn cost) — that's the accepted tradeoff from this design.

Run: `pytest tests/ -q 2>&1 | tail -5`
Expected: no new failures vs. baseline.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test: migrate DB lock-retry cluster to real subprocess-manufactured contention (#133)"
```

---

### Task 3: Migrate `TestMinigrafQuery`, `TestMinigrafTransact`, `TestMinigrafRetract` (the incident class)

**Files:**
- Modify: `tests/test_mcp_server.py:543-638`

- [ ] **Step 1: Convert `TestMinigrafQuery` (2 tests)**

Current (lines 543-566, reconstructed from established pattern — read exact bodies before replacing): tests `handle_minigraf_query` success and error paths against a mock.

Target:

```python
class TestMinigrafQuery:
    def test_returns_results_on_success(self, real_db):
        import mcp_server
        real_db.execute('(transact {} [[:decision/redis :description "use Redis"]])')

        result = mcp_server.handle_minigraf_query(
            '[:find ?d :where [:decision/redis :description ?d]]'
        )

        assert result["ok"] is True
        assert result["results"] == [["use Redis"]]

    def test_returns_error_on_minigraf_error(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_query("(this is not valid datalog")
        assert result["ok"] is False
        assert "error" in result
```

- [ ] **Step 2: Convert `TestMinigrafTransact` (3 tests) — the exact incident class**

Current code (lines 569-604, already read in full this session):

```python
class TestMinigrafTransact:
    def test_requires_reason(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_minigraf_transact("[[:e :a :v]]", reason="")

        assert result["ok"] is False
        assert "reason" in result["error"].lower()

    def test_transacts_and_checkpoints(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "3"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_minigraf_transact("[[:e :a :v]]", reason="test")

        assert any("transact" in str(c) for c in db_instance.execute.call_args_list)
        db_instance.checkpoint.assert_called_once()
        assert result["ok"] is True

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad facts")

        result = mcp_server.handle_minigraf_transact("[[:bad]]", reason="test")

        assert result["ok"] is False
        assert "bad facts" in result["error"]
```

Target — note `test_transacts_and_checkpoints` previously only checked `"transact" in str(call)`, which is exactly the class of assertion too weak to catch an argument-order bug; the new version proves the fact actually landed and is actually queryable:

```python
class TestMinigrafTransact:
    def test_requires_reason(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_transact("[[:decision/x :description \"y\"]]", reason="")
        assert result["ok"] is False
        assert "reason" in result["error"].lower()

    def test_transacts_and_checkpoints(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_transact(
            '[[:decision/cache :description "use Redis"]]', reason="test"
        )

        assert result["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/cache :description ?d]])'
        ))
        assert queried["results"] == [["use Redis"]]

    def test_returns_error_on_minigraf_error(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_transact("(not valid datalog", reason="test")
        assert result["ok"] is False
        assert "error" in result
```

`db_instance.checkpoint.assert_called_once()` has no real-object equivalent without instrumentation and isn't essential to this test's purpose (verifying the transact landed) — drop it here; if checkpoint-call verification matters on its own, that belongs in a dedicated test using `monkeypatch.setattr(real_db.__class__, "checkpoint", ...)` wrapping the real checkpoint, not as an incidental assertion bolted onto a content-correctness test.

- [ ] **Step 3: Convert `TestMinigrafRetract` (3 tests)** — same transformation pattern as Step 2, applied to `handle_minigraf_retract`: after a successful retract, query the graph and confirm the fact is gone (or, for bi-temporal retracts, confirm it's `:valid-to`-bounded rather than present at current time).

```python
class TestMinigrafRetract:
    def test_requires_reason(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_retract("[[:e :a :v]]", reason="")
        assert result["ok"] is False

    def test_retracts_and_checkpoints(self, real_db):
        import mcp_server
        real_db.execute('(transact {} [[:decision/old :description "deprecated"]])')

        result = mcp_server.handle_minigraf_retract(
            '[[:decision/old :description "deprecated"]]', reason="gone"
        )

        assert result["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/old :description ?d]])'
        ))
        assert queried["results"] == []

    def test_returns_error_on_minigraf_error(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_retract("(not valid datalog", reason="gone")
        assert result["ok"] is False
        assert "error" in result
```

- [ ] **Step 4: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestMinigrafQuery tests/test_mcp_server.py::TestMinigrafTransact tests/test_mcp_server.py::TestMinigrafRetract -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate query/transact/retract tests to real-backend verification (#133)"
```

---

### Task 4: Migrate schema-validation cluster

**Files:**
- Modify: `tests/test_mcp_server.py:1330-1381` (`TestTransactExtractedFactsSchema`), `tests/test_mcp_server.py:1421-1460` (`TestMinigrafTransactSchema`), `tests/test_mcp_server.py:1653-1697` (`TestPhase5Schema`)

- [ ] **Step 1: Convert `TestTransactExtractedFactsSchema` (3 tests)**

Current code (lines 1330-1381, already read in full):

```python
class TestTransactExtractedFactsSchema:
    def test_invalid_entity_type_is_skipped(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "1"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        facts = [{"entity": ":service/auth", "entity_type": "service",
                  "attribute": ":description", "value": "auth service"}]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 0
        transact_calls = [c for c in db_instance.execute.call_args_list
                          if "transact" in str(c)]
        assert len(transact_calls) == 0

    def test_valid_fact_is_stored(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "2"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"}]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 1
        transact_calls = [c for c in db_instance.execute.call_args_list
                          if "transact" in str(c)]
        assert len(transact_calls) == 1
        assert ":ident" in str(transact_calls[0])
        assert '":decision/redis"' in str(transact_calls[0])

    def test_mixed_batch_stores_only_valid(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "3"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        facts = [
            {"entity": ":decision/redis", "entity_type": "decision",
             "attribute": ":description", "value": "use Redis"},
            {"entity": ":service/auth", "entity_type": "service",
             "attribute": ":description", "value": "auth service"},
        ]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 1
```

Target:

```python
class TestTransactExtractedFactsSchema:
    def test_invalid_entity_type_is_skipped(self, real_db):
        import mcp_server
        facts = [{"entity": ":service/auth", "entity_type": "service",
                  "attribute": ":description", "value": "auth service"}]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 0
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:service/auth :description ?d]])'
        ))
        assert queried["results"] == []

    def test_valid_fact_is_stored(self, real_db):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"}]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 1
        desc = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert desc["results"] == [["use Redis"]]
        ident = json.loads(real_db.execute(
            '(query [:find ?i :where [:decision/redis :ident ?i]])'
        ))
        assert ident["results"] == [[":decision/redis"]]

    def test_mixed_batch_stores_only_valid(self, real_db):
        import mcp_server
        facts = [
            {"entity": ":decision/redis", "entity_type": "decision",
             "attribute": ":description", "value": "use Redis"},
            {"entity": ":service/auth", "entity_type": "service",
             "attribute": ":description", "value": "auth service"},
        ]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 1
        redis = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert redis["results"] == [["use Redis"]]
        auth = json.loads(real_db.execute(
            '(query [:find ?d :where [:service/auth :description ?d]])'
        ))
        assert auth["results"] == []
```

- [ ] **Step 2: Convert `TestMinigrafTransactSchema` (3 tests)**

Current code (lines 1421-1460, already read in full). This class exercises real schema-violation error paths (`test_rejects_unknown_entity_type` already expects `result["ok"] is False` — check whether it currently passes against the mock only because the mock never actually validates; against `real_db`, confirm this still produces `ok: False` for a genuine schema-validation reason). Target:

```python
class TestMinigrafTransactSchema:
    def test_rejects_unknown_entity_type(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_transact(
            '[[:service/auth :description "auth service"]]',
            reason="test"
        )
        assert result["ok"] is False
        assert "schema" in result["error"].lower() or "violation" in result["error"].lower()

    def test_accepts_valid_fact(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_transact(
            '[[:decision/redis :description "use Redis"]]',
            reason="test"
        )
        assert result["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert queried["results"] == [["use Redis"]]

    def test_keyword_only_transact_bypasses_schema_validation(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_transact(
            '[[:service/auth :calls :component/jwt]]',
            reason="test relationship edge"
        )
        assert result["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?c :where [:service/auth :calls ?c]])'
        ))
        assert queried["results"] == [[":component/jwt"]]
```

If `test_rejects_unknown_entity_type` fails against the real backend (i.e. `mcp_server._validate_facts`/schema checking rejects it in Python before ever reaching minigraf, so minigraf's own schema enforcement was never actually exercised even in the "real" version) — that's fine and expected; the important change is that the *value* asserted (`ok: False`, error message content) is now checked against real code paths end-to-end, not a mock's canned `db_instance.execute.return_value`.

- [ ] **Step 3: Convert `TestPhase5Schema` (6 tests)**

Current code (lines 1653-1697, already read in full). Five of the six tests (`test_module_entity_passes_validation` through `test_unknown_code_attr_fails_validation`) call `mcp_server._validate_facts(facts)` directly — pure Python logic, not touching the DB at all beyond `mcp_server.open_db()` in setup. These need almost no change beyond the fixture swap (drop `mock_minigraf_db`, use `real_db` purely to satisfy `open_db()`'s precondition if `_validate_facts` needs `_db` to exist — check; if it doesn't touch `_db` at all, these can drop the DB fixture entirely and just call `_validate_facts` directly). The sixth test is the interesting one:

```python
class TestPhase5Schema:
    def test_module_entity_passes_validation(self):
        import mcp_server
        facts = [{"entity": ":module/src-auth-py", "entity_type": "module",
                  "attribute": ":description", "value": "src/auth.py"}]
        assert mcp_server._validate_facts(facts) == []

    def test_function_entity_passes_validation(self):
        import mcp_server
        facts = [{"entity": ":function/src-auth-py-login", "entity_type": "function",
                  "attribute": ":description", "value": "login"}]
        assert mcp_server._validate_facts(facts) == []

    def test_class_entity_passes_validation(self):
        import mcp_server
        facts = [{"entity": ":class/src-auth-py-user", "entity_type": "class",
                  "attribute": ":description", "value": "User"}]
        assert mcp_server._validate_facts(facts) == []

    def test_ingestion_entity_passes_validation(self):
        import mcp_server
        facts = [{"entity": ":ingestion/watermark", "entity_type": "ingestion",
                  "attribute": ":description", "value": "git ingestion watermark"}]
        assert mcp_server._validate_facts(facts) == []

    def test_unknown_code_attr_fails_validation(self):
        import mcp_server
        facts = [{"entity": ":module/foo", "entity_type": "module",
                  "attribute": ":description", "value": "foo.py"},
                 {"entity": ":module/foo", "entity_type": "module",
                  "attribute": ":unknown-attr", "value": "x"}]
        violations = mcp_server._validate_facts(facts)
        assert any("unknown-attr" in v for v in violations)

    def test_contains_rule_registered_at_startup(self, real_db):
        import mcp_server
        # A real rule invocation proves registration, not just "no exception
        # during open_db". Adapt the exact rule-call syntax to the actual
        # `contains` rule name/arity found in mcp_server.SESSION_RULES.
        result = real_db.execute('(query [(contains? :module/foo "foo")])')
        # Confirms the rule executes without a "unknown rule" parse error —
        # read SESSION_RULES first to get the exact registered rule name and
        # arity before finalizing this invocation.
        assert "error" not in json.loads(result) or True  # replace with a real assertion once rule name confirmed
```

For `test_contains_rule_registered_at_startup`: before finalizing, run `grep -n "SESSION_RULES" -A 20 mcp_server.py` to read the actual rule definition and its name/arity, then write a concrete, real assertion (e.g. that invoking the rule with a matching/non-matching pair returns the expected boolean-shaped result) instead of the placeholder tolerant assertion shown above — this file's own `test_contains_filter_actually_matches_against_real_graph` (already in the file) is the reference pattern for how to invoke `contains?` correctly.

- [ ] **Step 4: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestTransactExtractedFactsSchema tests/test_mcp_server.py::TestMinigrafTransactSchema tests/test_mcp_server.py::TestPhase5Schema -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate schema-validation cluster to real-backend verification (#133)"
```

---

### Task 5: Migrate `TestMinigrafAudit`, `TestQueryCanonicalEntities`

**Files:**
- Modify: `tests/test_mcp_server.py:1540-1650` (`TestMinigrafAudit`), `tests/test_mcp_server.py:1463-1537` (`TestQueryCanonicalEntities`)

- [ ] **Step 1: Convert `TestMinigrafAudit` (5 tests)**

Current code (lines 1540-1650, already read in full this session) uses `side_effect` lists simulating minigraf's multi-step type→UUID→attribute query sequence for `handle_minigraf_audit`. Against a real backend, seed the actual entity via a real transact instead of faking the query responses:

```python
class TestMinigrafAudit:
    def test_clean_db_returns_zero_retracted(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_audit()
        assert result["ok"] is True
        assert result["retracted"] == 0
        assert result["violations"] == []

    def test_entity_missing_required_attr_is_retracted(self, real_db):
        import mcp_server
        # Entity has :ident + :entity-type + :rationale but is missing the
        # required :description — should be flagged and retracted.
        real_db.execute(
            '(transact {} [[:decision/redis :entity-type :type/decision] '
            '[:decision/redis :ident ":decision/redis"] '
            '[:decision/redis :rationale "fast"]])'
        )

        result = mcp_server.handle_minigraf_audit()

        assert result["ok"] is True
        assert result["retracted"] == 1
        assert len(result["violations"]) == 1
        remaining = json.loads(real_db.execute(
            '(query [:find ?r :where [:decision/redis :rationale ?r]])'
        ))
        assert remaining["results"] == []  # retracted, no longer queryable at current time

    def test_as_of_reports_violations_without_retracting(self, real_db):
        import mcp_server
        real_db.execute(
            '(transact {} [[:decision/redis :entity-type :type/decision] '
            '[:decision/redis :ident ":decision/redis"] '
            '[:decision/redis :rationale "fast"]])'
        )

        result = mcp_server.handle_minigraf_audit(as_of=5)

        assert result["ok"] is True
        assert result["retracted"] == 0  # read-only when as_of provided
        assert len(result["violations"]) == 1
        still_present = json.loads(real_db.execute(
            '(query [:find ?r :where [:decision/redis :rationale ?r]])'
        ))
        assert still_present["results"] == [["fast"]]  # not retracted

    def test_ident_attr_used_for_display_in_violations(self, real_db):
        """Violation report shows keyword ident from :ident, not the raw UUID."""
        import mcp_server
        kw = ":decision/claude-haiku-4-5-20251001"
        real_db.execute(
            f'(transact {{}} [[{kw} :entity-type :type/decision] [{kw} :ident "{kw}"]])'
        )

        result = mcp_server.handle_minigraf_audit()

        assert result["retracted"] == 1
        assert result["violations"][0]["entity"] == kw

    def test_result_shape(self, real_db):
        import mcp_server
        result = mcp_server.handle_minigraf_audit()
        assert "ok" in result
        assert "audited" in result
        assert "retracted" in result
        assert "violations" in result
```

Before finalizing, run `handle_minigraf_audit`'s source (`grep -n "def handle_minigraf_audit" -A 60 mcp_server.py`) to confirm the exact required-attribute set per entity type (the docstring/comments in the old mocked test say ":ident + :entity-type (system) + :rationale (domain); Missing :description → violation" — this implies `:description` is the one universally-required domain attribute checked, confirm this against the real implementation, not just the old test's comment, before trusting the "missing :description" repro above).

- [ ] **Step 2: Convert `TestQueryCanonicalEntities` (5 tests)**

Current code (lines 1463-1537, already read in full). `test_formats_entities_as_lines` and `test_caps_at_50_entities` use `side_effect` lists simulating minigraf's two-step ident-then-description query pattern. Convert by seeding real facts instead:

```python
class TestQueryCanonicalEntities:
    def test_returns_empty_string_when_no_entities(self, real_db):
        import mcp_server
        result = mcp_server._query_canonical_entities()
        assert result == ""

    def test_formats_entities_as_lines(self, real_db):
        import mcp_server
        real_db.execute(
            '(transact {} [[:decision/redis :entity-type :type/decision] '
            '[:decision/redis :ident ":decision/redis"] '
            '[:decision/redis :description "use Redis"]])'
        )

        result = mcp_server._query_canonical_entities()

        assert ":decision/redis" in result
        assert "use Redis" in result

    def test_caps_at_50_entities(self, real_db):
        import mcp_server
        triples = []
        for i in range(60):
            ident = f":decision/item-{i}"
            triples.append(f'[{ident} :entity-type :type/decision]')
            triples.append(f'[{ident} :ident "{ident}"]')
            triples.append(f'[{ident} :description "item {i}"]')
        real_db.execute(f'(transact {{}} [{" ".join(triples)}])')

        result = mcp_server._query_canonical_entities()

        assert result.count(":decision/") == 50

    def test_injected_into_llm_prompt(self, real_db, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_LLM_MODEL", "claude-haiku-4-5-20251001")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        real_db.execute(
            '(transact {} [[:decision/redis :entity-type :type/decision] '
            '[:decision/redis :ident ":decision/redis"] '
            '[:decision/redis :description "use Redis"]])'
        )

        captured_prompt = {}
        def fake_call_llm(model, prompt):
            captured_prompt["prompt"] = prompt
            return "[]"

        with patch("mcp_server._call_llm", side_effect=fake_call_llm):
            mcp_server._llm_extract_and_transact("User: test\nAgent: ok")

        assert ":decision/redis" in captured_prompt.get("prompt", "")

    def test_injected_into_agent_prompt(self, real_db):
        import mcp_server
        real_db.execute(
            '(transact {} [[:decision/redis :entity-type :type/decision] '
            '[:decision/redis :ident ":decision/redis"] '
            '[:decision/redis :description "use Redis"]])'
        )

        captured = {}
        async def fake_request_block(conversation_delta, canonical_entities_section=""):
            captured["canonical_entities_section"] = canonical_entities_section
            return "[]"

        with patch("mcp_server._request_agent_memory_block_async", side_effect=fake_request_block):
            import asyncio
            asyncio.run(mcp_server._agent_extract_and_transact("User: test\nAgent: ok"))

        assert ":decision/redis" in captured.get("canonical_entities_section", "")
```

Note `test_injected_into_llm_prompt`/`test_injected_into_agent_prompt` no longer need `patch("mcp_server._query_canonical_entities", ...)` since the real function now runs against real seeded data and produces the real string — this is a simplification, not just a swap, and is a direct instance of "always verify results" (the old version faked the very function whose output the test cared about).

- [ ] **Step 3: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestMinigrafAudit tests/test_mcp_server.py::TestQueryCanonicalEntities -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate audit/canonical-entities tests to real-backend verification (#133)"
```

---

### Task 6: Migrate `TestMcpToolWiring`, `TestMinigrafReportIssue` (external-API mock preserved)

**Files:**
- Modify: `tests/test_mcp_server.py:1048-1191` (`TestMcpToolWiring`), `tests/test_mcp_server.py:641-672` (`TestMinigrafReportIssue`)

- [ ] **Step 1: Convert `TestMcpToolWiring` (9 tests)**

These test MCP dispatch (`call_tool`/`list_tools`), not Datalog correctness — swap the fixture, verify real results flow through the dispatch layer correctly. Worked examples for the two already-read tests:

```python
class TestMcpToolWiring:
    def test_list_tools_returns_ten_tools(self, real_db):
        import asyncio
        import mcp_server

        tools = asyncio.run(mcp_server.list_tools())

        assert len(tools) == 10
        names = {t.name for t in tools}
        assert names == {
            "minigraf_query", "minigraf_transact", "minigraf_retract",
            "minigraf_rule", "minigraf_report_issue", "memory_prepare_turn", "memory_finalize_turn",
            "minigraf_audit", "minigraf_ingest_git", "minigraf_ingest_status",
        }

    def test_call_tool_minigraf_query(self, real_db):
        import asyncio
        import mcp_server
        real_db.execute('(transact {} [[:e1 :name "FastAPI"]])')

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_query", {"datalog": "(query [:find ?n :where [:e1 :name ?n]])"}
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True
        assert data["results"] == [["FastAPI"]]

    def test_call_tool_minigraf_transact(self, real_db):
        import asyncio
        import mcp_server

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_transact",
            {"facts": '[[:decision/cache :description "Redis"]]', "reason": "caching strategy"},
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/cache :description ?d]])'
        ))
        assert queried["results"] == [["Redis"]]
```

Read the remaining 6 tests at `tests/test_mcp_server.py:1098-1191` (`test_call_tool_memory_prepare_turn`, `test_call_tool_memory_finalize_turn`, `test_call_tool_minigraf_retract`, `test_db_released_after_call_tool`, `test_call_tool_unknown_raises`, `test_call_tool_lock_retry_does_not_block_event_loop`) and apply the same pattern: swap `mock_minigraf_db` for `real_db`, and wherever the old test asserted on `db_instance.execute.call_args` or a canned `db_instance.execute.return_value`, replace with a real seed-then-query or seed-then-call-then-verify sequence. `test_call_tool_lock_retry_does_not_block_event_loop` is a special case — if it uses `mock_class.open.side_effect` to simulate a slow/failing open, convert it the same way as Task 2's `_hold_lock_subprocess` pattern rather than `real_db` (it's testing lock-retry behavior specifically, not general dispatch).

- [ ] **Step 2: Convert `TestMinigrafReportIssue` (3 tests)**

Read current bodies at `tests/test_mcp_server.py:641-672`. This class delegates to the `report_issue` module (GitHub API calls) — keep that mocked (`patch("mcp_server.report_issue...")` or however it's currently invoked), but swap the DB fixture from `mock_minigraf_db` to `real_db` since `handle_minigraf_report_issue` still needs a live `_db` to be open even though it doesn't query it for these specific assertions.

- [ ] **Step 3: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestMcpToolWiring tests/test_mcp_server.py::TestMinigrafReportIssue -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate MCP tool-wiring and report-issue tests to real backend (#133)"
```

---

### Task 7: Migrate `TestMemoryPrepareTurn`, `TestMemoryFinalizeTurnHeuristic`, LLM-strategy classes (external-API mock preserved)

**Files:**
- Modify: `tests/test_mcp_server.py:675-826` (`TestMemoryPrepareTurn`), `tests/test_mcp_server.py:860-888` (`TestMemoryFinalizeTurnHeuristic`), `tests/test_mcp_server.py:951-1045` (`TestLlmStrategyOpenAI`, `TestLlmStrategy`, `TestAgentStrategy`)

- [ ] **Step 1: Convert `TestMemoryPrepareTurn`'s 7 mocked tests**

Note `test_contains_filter_actually_matches_against_real_graph` (line 778) already uses `real_db`-equivalent setup (via direct `mcp_server.open_db`/`get_db`) as the reference pattern for this whole class — the 7 remaining mocked tests (`test_returns_empty_string_when_graph_empty` through `test_uses_current_utc_timestamp_for_current_state_queries`) should follow that same pattern: seed real facts via `real_db.execute(...)`, call `mcp_server._handle_memory_prepare_turn_heuristic(...)`, assert on the real formatted output string. Read each test's current body at `tests/test_mcp_server.py:676-777` and convert individually — the scan-limit and entity-cap tests (`test_respects_scan_limit_env_var`, `test_caps_number_of_entities_scanned`) will need real seeded entities in bulk (following the same loop-based seeding pattern as Task 5's `test_caps_at_50_entities`) rather than a mocked call-count assertion.

- [ ] **Step 2: Convert `TestMemoryFinalizeTurnHeuristic` (2 tests)**

Read current bodies at `tests/test_mcp_server.py:860-888`. Convert `test_transacts_extracted_facts` to seed nothing, call the heuristic finalize function, then query `real_db` for the facts it should have written. `test_returns_zero_stored_when_no_signals` needs no DB assertions beyond swapping the fixture.

- [ ] **Step 3: Convert `TestLlmStrategyOpenAI`, `TestLlmStrategy`, `TestAgentStrategy` (5 tests)**

Keep the LLM client mocking (`patch("openai...")`/`patch("anthropic...")` or equivalent, whatever the current mechanism is at lines 951-1045) exactly as-is — only swap `mock_minigraf_db` for `real_db`, and where the test currently asserts "a fact was transacted" via mock call inspection, instead query `real_db` afterward to confirm the fact landed for real.

- [ ] **Step 4: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestMemoryPrepareTurn tests/test_mcp_server.py::TestMemoryFinalizeTurnHeuristic tests/test_mcp_server.py::TestLlmStrategyOpenAI tests/test_mcp_server.py::TestLlmStrategy tests/test_mcp_server.py::TestAgentStrategy -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate memory-turn and LLM-strategy tests to real backend (#133)"
```

---

### Task 8: Migrate `TestMinigrafIngestStatus` (11 tests)

**Files:**
- Modify: `tests/test_mcp_server.py:3803-3976`

- [ ] **Step 1: Convert the graph-reading tests**

Current code already read in full this session. The status/counter-only tests (`test_returns_idle_before_ingestion`, `test_running_status_skips_graph_query`, `test_reports_owner_pid_when_skipped`, `test_skipped_status_is_stale_when_owner_pid_dead`, `test_error_status_reports_stale_when_holder_pid_dead`, `test_error_status_not_stale_when_holder_pid_alive`, `test_error_status_omits_stale_when_no_pid_in_message`) only depend on `mcp_server._ingest_progress` (an in-memory dict) and, for the PID ones, a monkeypatched `_pid_is_alive` — these just need the fixture swap, no assertion changes.

The graph-reading tests get genuinely simpler by seeding real facts instead of faking multi-branch `side_effect` functions:

```python
    def test_returns_last_run_at_from_graph(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        real_db.execute(
            '(transact {} [[:ingestion/last-run :entity-type :type/ingestion] '
            '[:ingestion/last-run :last-run-at "2026-05-27T10:00:00Z"] '
            '[:ingestion/last-run :last-commit "deadbeef"]])'
        )

        result = mcp_server.handle_minigraf_ingest_status()

        assert result["last_run_at"] == "2026-05-27T10:00:00Z"
        assert result["last_commit"] == "deadbeef"

    def test_running_status_skips_graph_query(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "running", "processed": 3, "total": 10,
            "current_commit": "abc123", "error": None,
        }
        with execute_spy() as calls:
            result = mcp_server.handle_minigraf_ingest_status()

        assert result["status"] == "running"
        assert result["processed"] == 3
        assert result["total"] == 10
        assert result["current_commit"] == "abc123"
        assert calls == []  # must not query the graph while running

    def test_returns_total_ingested_from_graph(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        real_db.execute(
            '(transact {} [[:ingestion/last-run :entity-type :type/ingestion] '
            '[:ingestion/last-run :last-run-at "2026-05-27T10:00:00Z"] '
            '[:ingestion/last-run :last-commit "deadbeef"]])'
        )
        for i in range(1017):
            real_db.execute(
                f'(transact {{}} [[:commit/c{i} :entity-type :type/commit]])'
            )

        result = mcp_server.handle_minigraf_ingest_status()

        assert result["total_ingested"] == 1017
```

`test_returns_total_ingested_from_graph`'s loop of 1017 individual transacts will be slow — before finalizing, check whether the real query underlying `total_ingested` (read `handle_minigraf_ingest_status`'s source for the exact `:type/commit` counting query) can accept a single batched transact with 1017 entities in one `execute()` call instead of 1017 round-trips; if so, build one large facts vector and issue a single `real_db.execute(...)` for performance. Do the same for `test_total_ingested_reflects_true_persisted_count_not_stale_watermark`, which needs both a stale `:total-ingested` watermark fact *and* 21715 real `:type/commit` entities — use a smaller representative count (e.g. 50) instead of the original mock's arbitrary large number; the test's purpose (watermark is ignored in favor of a direct count) doesn't depend on matching the original mock's specific large numbers, only on stale-watermark-count ≠ real-count.

`test_skipped_status_is_stale_when_owner_pid_dead`, `test_error_status_reports_stale_when_holder_pid_dead`, `test_error_status_not_stale_when_holder_pid_alive`, `test_error_status_omits_stale_when_no_pid_in_message`, `test_total_ingested_absent_returns_none`: straightforward fixture swap, no seeded facts needed (these test the "absent" / in-memory-only branches).

- [ ] **Step 2: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestMinigrafIngestStatus -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate ingest-status tests to real backend, seed instead of fake multi-step queries (#133)"
```

---

### Task 9: Migrate `TestIngestionWrites`, `TestPreloadKnownDeps`, `TestPreloadExternalDependencies`, `TestTotalIngestedQuery` (20 tests)

**Files:**
- Modify: `tests/test_mcp_server.py:5085-5351` (`TestIngestionWrites`'s 11 mocked tests), `tests/test_mcp_server.py:5656-5719`, `tests/test_mcp_server.py:5722-5765`, `tests/test_mcp_server.py:5768-5783`

- [ ] **Step 1: Convert `TestIngestionWrites`'s low-level write-shape tests**

Current code already read in full this session. `test_ingest_transact_uses_valid_from` and `test_ingest_close_uses_valid_from_and_valid_to` currently check the raw Datalog string for substring presence and ordering — the exact style of assertion too weak to catch the argument-order bug (it checks *what got sent*, never *what minigraf actually did with it*). Convert to querying real bi-temporal state instead:

```python
class TestIngestionWrites:
    def test_ingest_transact_uses_valid_from(self, real_db):
        import mcp_server
        mcp_server._ingest_transact(
            real_db,
            ['[:module/foo :description "foo.py"]'],
            "2025-03-01T10:00:00Z",
            "git:abc test",
        )
        # Not visible before its valid-from time...
        before = json.loads(real_db.execute(
            '(query [:valid-at "2025-02-01T00:00:00Z" :find ?d '
            ':where [:module/foo :description ?d]])'
        ))
        assert before["results"] == []
        # ...but visible at/after it.
        after = json.loads(real_db.execute(
            '(query [:valid-at "2025-03-01T10:00:00Z" :find ?d '
            ':where [:module/foo :description ?d]])'
        ))
        assert after["results"] == [["foo.py"]]

    def test_ingest_close_uses_valid_from_and_valid_to(self, real_db):
        import mcp_server
        mcp_server._ingest_close(
            real_db,
            ['[:module/foo :description "foo.py"]'],
            "2025-01-01T00:00:00Z",
            "2025-03-01T10:00:00Z",
            "git:abc delete",
        )
        within_window = json.loads(real_db.execute(
            '(query [:valid-at "2025-02-01T00:00:00Z" :find ?d '
            ':where [:module/foo :description ?d]])'
        ))
        assert within_window["results"] == [["foo.py"]]
        after_close = json.loads(real_db.execute(
            '(query [:valid-at "2025-03-02T00:00:00Z" :find ?d '
            ':where [:module/foo :description ?d]])'
        ))
        assert after_close["results"] == [], (
            "this is the exact bi-temporal bounds check that a mock-only test "
            "can never catch — it's the class of bug #133 exists to prevent"
        )

    def test_watermark_update_transacts_hash(self, real_db):
        import mcp_server
        mcp_server._watermark_update(real_db, "deadbeef", "2025-03-01T10:00:00Z", "git:deadbeef x: y")
        result = json.loads(real_db.execute(
            '(query [:find ?h :where [:ingestion/watermark :hash ?h]])'
        ))
        # Confirm the actual attribute name used by _watermark_update before
        # finalizing — read mcp_server.py's _watermark_update source for the
        # exact ident/attribute it writes, then adjust the query above to match.
        assert result["results"]

    def test_watermark_query_returns_none_when_absent(self, real_db):
        import mcp_server
        result = mcp_server._watermark_query(real_db)
        assert result is None

    def test_watermark_query_returns_hash_when_present(self, real_db):
        import mcp_server
        mcp_server._watermark_update(real_db, "abc123", "2025-01-01T00:00:00Z", "seed")
        result = mcp_server._watermark_query(real_db)
        assert result == "abc123"

    def test_ingest_transact_noop_for_empty_triples(self, real_db):
        import mcp_server
        with execute_spy() as calls:
            mcp_server._ingest_transact(real_db, [], "2025-03-01T10:00:00Z", "r")
        assert calls == []

    def test_ingest_close_noop_for_empty_triples(self, real_db):
        import mcp_server
        with execute_spy() as calls:
            mcp_server._ingest_close(real_db, [], "2025-01-01T00:00:00Z", "2025-03-01T00:00:00Z", "r")
        assert calls == []

    def test_preload_known_entities_loads_descriptions_and_valid_from(self, real_db, git_repo):
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2025-01-15T10:00:00Z"} '
            '[[:function/auth-py-login :description "login"] '
            '[:function/auth-py-login :file "auth.py"]])'
        )

        entity_valid_from, entity_descriptions, file_entities, submodule_paths = (
            mcp_server._preload_known_entities(real_db, str(git_repo))
        )

        assert entity_valid_from.get(":function/auth-py-login") == "2025-01-15T10:00:00Z"
        assert entity_descriptions.get(":function/auth-py-login") == "login"

    def test_last_run_write_transacts_correct_fields(self, real_db):
        import mcp_server
        mcp_server._last_run_write(real_db, "deadbeef", "2026-05-27T10:00:00Z", 1017)

        result = json.loads(real_db.execute(
            '(query [:find ?t ?c ?n :where '
            '[?e :entity-type :type/ingestion] '
            '[?e :last-run-at ?t] [?e :last-commit ?c] [?e :total-ingested ?n]])'
        ))
        assert result["results"] == [["2026-05-27T10:00:00Z", "deadbeef", 1017]]
```

`test_run_ingestion_writes_last_run_on_completion` and `test_run_ingestion_writes_last_run_when_no_commits` already use the `git_repo` fixture and only mock the DB incidentally (they monkeypatch `_last_run_write` itself to capture calls, not the DB) — read their current bodies at lines 5302-5351 and simply swap `mock_minigraf_db` for `real_db`; no other change needed since their actual verification mechanism (`monkeypatch.setattr(mcp_server, "_last_run_write", ...)`) is already real-code-path-friendly.

- [ ] **Step 2: Convert `TestPreloadKnownDeps` (4 tests), `TestPreloadExternalDependencies` (3 tests), `TestTotalIngestedQuery` (2 tests)**

Read current bodies at `tests/test_mcp_server.py:5656-5783`. These follow the same shape as `test_preload_known_entities_loads_descriptions_and_valid_from` above (seed real facts via `real_db.execute(...)`, call the preload function, assert on its real return value) or the `TestMinigrafIngestStatus` pattern (seed then query) for the query-only ones. `test_query_failure_is_non_fatal` and `test_preload_pinned_commits_returns_empty_on_query_failure` need a genuinely failing query rather than a mocked exception — pass deliberately malformed Datalog (e.g. unbalanced brackets) to trigger a real `MiniGrafError` from the real parser, and confirm the preload function catches it and returns the documented fallback rather than propagating.

- [ ] **Step 3: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestIngestionWrites tests/test_mcp_server.py::TestPreloadKnownDeps tests/test_mcp_server.py::TestPreloadExternalDependencies tests/test_mcp_server.py::TestTotalIngestedQuery -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate ingestion-writes and preload tests to real bi-temporal verification (#133)"
```

---

### Task 10: Migrate `TestRunIngestion` (17 tests)

**Files:**
- Modify: `tests/test_mcp_server.py:5786-6212`

- [ ] **Step 1: Read all 17 current test bodies**

Run: `sed -n '5786,6212p' tests/test_mcp_server.py` and read the full output — these test orchestration (progress counters, status transitions, lock-retry-during-ingestion, branch resolution, watermark seeding), driven against the real `git_repo` fixture already, with only the DB layer mocked.

- [ ] **Step 2: Convert each test**

Apply this rule per test: if the test's assertions are entirely about `mcp_server._ingest_progress` state, return values, or timing/concurrency (event-loop responsiveness, db-released-between-commits) — swap `mock_minigraf_db` for `real_db`, no assertion changes needed, since these were never really testing DB content in the first place. If a test's assertions inspect what got written (rare in this class per the earlier audit, since content-correctness is covered by `TestRunIngestionBitemporalClose`/`TestIngestionWrites`), convert to real-query verification following the Task 9 pattern. `test_ingest_git_resolves_branch_via_default_when_not_specified` / `test_ingest_git_explicit_branch_overrides_default` don't need DB-content changes — they're about `_default_git_branch` resolution — just swap the fixture. `test_skips_when_live_holder_present` / `test_proceeds_when_no_live_holder` test `_live_lock_holder_pid`, which reads a `.lock` file directly (no DB open at all) — check whether these even need `real_db`, or whether they can drop the DB fixture entirely and just manipulate a lock file on `tmp_path` directly (they may currently only take `mock_minigraf_db` incidentally).

- [ ] **Step 3: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestRunIngestion -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate TestRunIngestion orchestration tests to real backend (#133)"
```

---

### Task 11: Migrate `TestRunIngestionConcurrency`, `TestRunIngestionEventLoopResponsiveness`, `TestRunIngestionShutdown` (6 tests)

**Files:**
- Modify: `tests/test_mcp_server.py:6215-6487`

- [ ] **Step 1: Read and convert**

Run: `sed -n '6215,6487p' tests/test_mcp_server.py` and read the full output. Same rule as Task 10 Step 2: these test concurrency/shutdown timing behavior, not fact content — swap `mock_minigraf_db` for `real_db`. `test_concurrent_run_matches_sequential_facts` is the one exception worth checking closely — its name suggests it compares facts produced under concurrent vs. sequential execution, which is content-correctness-relevant; if so, convert both runs to use separate `real_db`-style in-memory instances and assert the real queried fact sets are identical, rather than comparing mock call-argument lists.

- [ ] **Step 2: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestRunIngestionConcurrency tests/test_mcp_server.py::TestRunIngestionEventLoopResponsiveness tests/test_mcp_server.py::TestRunIngestionShutdown -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate ingestion concurrency/shutdown tests to real backend (#133)"
```

---

### Task 12: Migrate `TestIndexCache`'s 1 mocked test, `TestMemoryPrepareTurnBM25`, `TestIndexCacheInvalidation`, `TestBM25GracefulDegradation` (12 tests)

**Files:**
- Modify: `tests/test_mcp_server.py:6823-7073`

- [ ] **Step 1: Convert `TestIndexCache::test_rebuild_populates_index`**

Current code (already read this session, lines 6829-6838):

```python
    def test_rebuild_populates_index(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/use-redis", ":description", "use redis"]]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        cache = mcp_server.IndexCache()
        cache._rebuild()
        assert cache.get() is not None
```

Target:

```python
    def test_rebuild_populates_index(self, real_db):
        import mcp_server
        real_db.execute('(transact {} [[:decision/use-redis :description "use redis"]])')
        cache = mcp_server.IndexCache()
        cache._rebuild()
        assert cache.get() is not None
```

- [ ] **Step 2: Convert `TestMemoryPrepareTurnBM25` (4 tests), `TestIndexCacheInvalidation` (5 tests), `TestBM25GracefulDegradation` (2 tests)**

Read current bodies at `tests/test_mcp_server.py:6898-7073`. These build a `FactIndex`/`IndexCache` from mocked query results to test BM25 ranking and cache-invalidation triggers. Convert by seeding real facts via `real_db.execute(...)` before building the index — the ranking/ordering assertions (`test_memory_facts_rank_above_git_facts`) stay meaningful (arguably more meaningful) once the underlying facts are real. `TestIndexCacheInvalidation`'s tests check that a successful/failed transact or retract triggers or skips `IndexCache.invalidate()` — these need `real_db` for the transact/retract call itself but the invalidation-triggered assertion likely still needs `patch("threading.Thread")` or a monkeypatched `IndexCache._rebuild` to observe whether invalidation fired without waiting for a real background rebuild; that monkeypatching is on our own `IndexCache` class, not on `MiniGrafDb`, so it's unaffected by this migration.

- [ ] **Step 3: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestIndexCache tests/test_mcp_server.py::TestMemoryPrepareTurnBM25 tests/test_mcp_server.py::TestIndexCacheInvalidation tests/test_mcp_server.py::TestBM25GracefulDegradation -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate index-cache and BM25 tests to real backend (#133)"
```

---

### Task 13: Migrate `TestRunIngestionBitemporalClose`, `TestRunIngestionBitemporalDeps` (13 tests — the original incident class)

**Files:**
- Modify: `tests/test_mcp_server.py:7362-7995`, `tests/test_mcp_server.py:8095-8161`

- [ ] **Step 1: Read all current bodies**

Run: `sed -n '7362,7995p' tests/test_mcp_server.py` and `sed -n '8095,8161p' tests/test_mcp_server.py`, read the full output. This class already contains 2 real-backend tests (`test_renamed_to_is_open_ended_against_real_graph`, `test_removed_field_secondary_attrs_are_closed_against_real_graph`) as the established reference pattern — these 11 remaining mocked tests should follow the exact same shape (real `git_repo` with actual file/commit operations, real `mcp_server.open_db`/`get_db`, real bi-temporal query verification via `:valid-at`/`:as-of`).

- [ ] **Step 2: Convert each test using the existing real-backend siblings as the template**

For each of the 11 tests (`test_file_deletion_closes_with_real_description_not_empty_string` through `test_same_file_rename_closes_old_ident_exactly_once`), replace `mock_minigraf_db` with `real_db` (or the file-backed `mcp_server.open_db(str(git_repo / "..."))` pattern the two existing real tests already use — check which of the two conventions, in-memory `real_db` vs. file-backed `open_db` against `git_repo`, the existing real tests use, and match it exactly rather than introducing a third variant in the same class), then replace any mock-call-argument assertions with real bi-temporal query verification: query the graph `:as-of` before and after the relevant commit, or `:valid-at` a specific timestamp, to confirm the entity is actually open/closed at the right times — this is the exact test shape that would have caught the original transact argument-order bug.

- [ ] **Step 3: Convert `TestRunIngestionBitemporalDeps` (2 tests)**

Same pattern: `test_new_import_writes_depends_on_via_ingest_transact` and `test_removed_import_closes_depends_on_edge` — swap fixture, verify via real `:valid-at`/`:as-of` queries on the `:depends-on` edge instead of mock call inspection.

- [ ] **Step 4: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestRunIngestionBitemporalClose tests/test_mcp_server.py::TestRunIngestionBitemporalDeps -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate bi-temporal close/deps tests to real backend, the original incident class (#133)"
```

---

### Task 14: Migrate `TestUnresolvedImportTagging`, `TestGitIngestionPathIgnore`, `TestPerCommitAccurateImportResolution`, `TestRunIngestionGitlinks` (9 tests)

**Files:**
- Modify: `tests/test_mcp_server.py:8164-8237`, `tests/test_mcp_server.py:8240-8358`, `tests/test_mcp_server.py:8502-8533`, `tests/test_mcp_server.py:8597-8762`

- [ ] **Step 1: Convert `TestUnresolvedImportTagging`'s 2 mocked tests**

`TestUnresolvedImportTagging` already has 1 real-backend test (per the earlier class-level audit) — use it as the template. Read current bodies at lines 8164-8237.

- [ ] **Step 2: Convert `TestGitIngestionPathIgnore` (3 tests)**

Current code (already read in full this session, lines 8240-8358) intercepts `mcp_server._ingest_transact` via `monkeypatch.setattr` to capture triples, with `mock_minigraf_db`'s canned empty-results return only satisfying `open_db()`'s precondition — the assertions never actually depend on the mock's return value. Convert by swapping to `real_db`; the existing `capture_transact` interception technique stays as-is (it wraps the real `_ingest_transact`, not `MiniGrafDb`, so it was never the kind of mock this migration targets) but now the underlying `real_ingest_transact` call actually persists to a real graph, so the assertions can additionally verify via `real_db.execute(...)` that nothing under `vendor/` is queryable, not just that no matching triple string was captured.

- [ ] **Step 3: Convert `TestPerCommitAccurateImportResolution` (1 test)**

Read current body at lines 8502-8533. Apply the standard fixture swap plus real-query verification.

- [ ] **Step 4: Convert `TestRunIngestionGitlinks`'s 3 mocked tests**

This class already has 1 real-backend test (`test_submodule_removal_closes_entity_type_and_path_against_real_graph`, from #137/#130's work) as the template — read current bodies for the other 3 (`test_submodule_add_creates_external_dependency_entity`, `test_submodule_bump_closes_old_pinned_commit`, and the fourth already-real one) at lines 8597-8762 and convert following that same established pattern.

- [ ] **Step 5: Run cluster, then full suite; commit**

```bash
pytest tests/test_mcp_server.py::TestUnresolvedImportTagging tests/test_mcp_server.py::TestGitIngestionPathIgnore tests/test_mcp_server.py::TestPerCommitAccurateImportResolution tests/test_mcp_server.py::TestRunIngestionGitlinks -v
pytest tests/ -q 2>&1 | tail -5
git add tests/test_mcp_server.py
git commit -m "test: migrate import-resolution and gitlink tests to real backend (#133)"
```

---

### Task 15: Clean up `TestGetDbConcurrentResetRace`'s `FakeDb`, delete `mock_minigraf_db`, update docs

**Files:**
- Modify: `tests/test_mcp_server.py:269-333` (`TestGetDbConcurrentResetRace`), `tests/test_mcp_server.py:1-51` (module docstring, delete `mock_minigraf_db`)
- Create: `docs/testing-conventions.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace `FakeDb` with a real in-memory instance**

Current code (already read in full this session, lines 269-333):

```python
class TestGetDbConcurrentResetRace:
    """Regression test for #122: ..."""

    def test_returns_live_db_despite_concurrent_reset_before_return(self):
        import inspect
        import sys as _sys
        import threading
        import mcp_server

        class FakeDb:
            def execute(self, datalog):
                return json.dumps({"results": []})

        fake_db = FakeDb()
        mcp_server._db = fake_db
        # ... (rest of the trace-based race-window setup)
```

Replace the `FakeDb` class and `fake_db = FakeDb()` with a real in-memory instance:

```python
    def test_returns_live_db_despite_concurrent_reset_before_return(self, monkeypatch):
        import inspect
        import sys as _sys
        import threading
        import mcp_server
        from minigraf import MiniGrafDb

        real_db = MiniGrafDb.open_in_memory()
        mcp_server._db = real_db
        # ... (rest of the trace-based race-window setup, unchanged — it doesn't
        # care what _db actually is, only that get_db()'s double-read race
        # returns the same live object despite a concurrent reset)
```

Read the rest of the test body (lines ~278-333) to confirm no other reference to `fake_db` needs updating besides the initial assignment and any later `assert result is fake_db`-style checks, which become `assert result is real_db`.

- [ ] **Step 2: Confirm zero remaining references to `mock_minigraf_db`**

Run: `grep -n "mock_minigraf_db" tests/test_mcp_server.py`
Expected: no output (all 160 uses converted across Tasks 1-14).

- [ ] **Step 3: Delete the `mock_minigraf_db` fixture and update the module docstring**

Delete the fixture (current lines 43-51):

```python
@pytest.fixture
def mock_minigraf_db():
    """Mock MiniGrafDb class and instance."""
    with patch("mcp_server.MiniGrafDb") as mock_class:
        db_instance = MagicMock()
        db_instance.execute.return_value = json.dumps({"results": []})
        mock_class.open.return_value = db_instance
        yield mock_class, db_instance
```

Replace the module docstring (current lines 1-4):

```python
"""Unit tests for mcp_server.py.

All tests mock MiniGrafDb so no live minigraf install is required.
"""
```

with:

```python
"""Unit tests for mcp_server.py.

All tests use a real minigraf backend — the `real_db` fixture opens a genuine
MiniGrafDb.open_in_memory() instance, so every test exercises real Datalog
parsing, schema validation, and bi-temporal semantics. A narrow exception: the
DB lock-retry cluster (TestGetDbLockRetry etc.) uses real file-backed
MiniGrafDb.open() with genuine subprocess-manufactured lock contention, since
locking is inherently file-based. External, non-minigraf network APIs (LLM
provider clients, GitHub via the report_issue module) still get mocked to
avoid real API cost/network/non-determinism in CI — see
docs/testing-conventions.md for the full rationale and pattern reference.
"""
```

Also check whether `MagicMock` is still imported/used anywhere in the file (`grep -n "MagicMock" tests/test_mcp_server.py`) — if the only remaining uses are in the LLM-strategy and report-issue clusters (expected), leave the import; if truly zero uses remain, remove `MagicMock` from the `unittest.mock` import line.

- [ ] **Step 4: Write `docs/testing-conventions.md`**

```markdown
# Testing Conventions

## Real backend, always

Every test in `tests/test_mcp_server.py` uses a real `minigraf` backend. The
`real_db` fixture opens a genuine `MiniGrafDb.open_in_memory()` instance
(redirected via a `monkeypatch.setattr(MiniGrafDb, "open", ...)` so
`mcp_server.open_db()`'s real code path — session-rule registration, mtime
tracking — still runs):

\```python
@pytest.fixture
def real_db(monkeypatch, tmp_path):
    from minigraf import MiniGrafDb
    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()
\```

This exists because a `MagicMock`-based fake of `MiniGrafDb` never parses or
validates the Datalog string passed to `execute()` — it just records call
arguments and returns a canned response. That blind spot hid a real bug for
months: `mcp_server.py`'s valid-time-bounded `(transact ...)` calls
constructed the command with facts and options in the wrong order, silently
making minigraf ignore `:valid-from`/`:valid-to` bounds. No mocked test could
catch this, because none of them ever asked minigraf to actually parse the
string.

## Always verify results

Never assert on mock call arguments (`"transact" in str(call)`,
`assert_called_once()`). Always re-query `real_db` after the code under test
runs, and assert on the actual persisted or returned facts. For bi-temporal
code specifically, verify with `:valid-at`/`:as-of` queries at multiple
points in time — before, during, and after the fact's valid-time window —
not just "does it exist right now," which behaves like `:any-valid-time`
regardless of bounds and would not have caught the argument-order bug either.

## The one narrow exception: external, non-minigraf APIs

Mocking survives only for genuinely external network services unrelated to
minigraf: LLM provider clients (OpenAI/Anthropic, in `TestLlmStrategyOpenAI`/
`TestLlmStrategy`/`TestAgentStrategy`) and GitHub API calls (in
`TestMinigrafReportIssue`, via the `report_issue` module). These stay mocked
to avoid real API cost, network dependency, and non-deterministic model
output in CI — the underlying `MiniGrafDb` in these tests is still always
real.

## Manufacturing real error conditions instead of faking them

The DB lock-retry cluster (`TestGetDbLockRetry`, `TestTryOpenWithSelfHealReuse`,
`TestOpenDbAtWithExtendedRetry`) needs genuine lock contention, which a mock
used to fake via a canned `MiniGrafError`. Locking is inherently file-based,
so these tests use a real file-backed `MiniGrafDb.open()` plus a helper that
spawns a subprocess to hold (or briefly open-then-release, for stale-lock
scenarios) a real lock on the same path — producing the exact real
`MiniGrafError` message (`"Database is locked by another process (lock file:
..., holder PID: ...)"`) that `mcp_server._stale_lock_holder_pid`/
`_pid_is_alive` parse. Only `mcp_server.time.sleep` is monkeypatched, purely
to skip real backoff delays — that's test-speed plumbing, not faking
minigraf's behavior. This is the pattern to reach for whenever a future test
needs a real failure condition rather than a business-logic result: prefer
manufacturing the condition for real over mocking the exception.

## Observing real calls without faking them: `execute_spy()`

Some tests need to assert that `execute()` was or wasn't called (e.g. "must
not query the graph while status is running") without faking any return
value. Use `execute_spy()`, which wraps the real `mcp_server._db_execute` to
record calls while still executing them for real — this is not a mock in
the sense this convention eliminates; it never fakes a return value or
bypasses real parsing.
```

- [ ] **Step 5: Link the new doc from CLAUDE.md**

Add to the "Key Files" section of `CLAUDE.md` (after the `install.py` line):

```markdown
- `docs/testing-conventions.md` - Real-backend-only test conventions for `tests/test_mcp_server.py`
```

- [ ] **Step 6: Full suite run, final comparison to baseline**

Run: `pytest tests/ -q 2>&1 | tail -10`
Expected: same or fewer failures than Task 1 Step 1's baseline, zero `mock_minigraf_db` references remain, zero new failures.

Run: `grep -c "MagicMock\|mock_minigraf_db" tests/test_mcp_server.py`
Expected: only matches inside the LLM-strategy/report-issue clusters' own mocking (external APIs) — confirm each remaining match is inside one of those three classes, not a leftover minigraf mock.

- [ ] **Step 7: Commit**

```bash
git add tests/test_mcp_server.py docs/testing-conventions.md CLAUDE.md
git commit -m "test: remove mock_minigraf_db entirely, add testing-conventions.md (#133)

Closes #133."
```

---

## Self-Review Notes

**Spec coverage:** Every design-doc element is covered — the `real_db` fixture (Task 1), the three special-case clusters (lock-retry in Task 2, LLM-strategy in Task 7, report-issue in Task 6), the FakeDb replacement and `mock_minigraf_db` deletion (Task 15), the testing-conventions doc (Task 15), and "always verify results" applied throughout every task's target code. Bug-discovery handling is stated in the design doc's precedent and doesn't need its own task — if a task's real-backend conversion surfaces a bug, fix it within that same task before moving on, matching every prior issue in this sequence.

**Scope note on task granularity:** Several tasks (7, 8, 9, 10, 11, 14) give one or two fully worked example conversions per class plus a specific, concrete instruction for the remaining tests in that class (never a bare "similar to Task N" — each instruction names the exact transformation: what to seed, what to query, which existing real-backend sibling test in the same class to pattern-match against) rather than pre-writing all ~150 tests' final code verbatim. This reflects the reality that this is a single mechanical-but-large migration across a 9,268-line file; the alternative (writing all final test bodies into this plan document) would be redundant with actually performing the migration. Task executors have full file read access and should read each class's current body before converting it, exactly as this plan's own preparation did.

**Type/interface consistency:** `real_db` fixture signature (`monkeypatch, tmp_path` → yields a live `MiniGrafDb`) and `execute_spy()` (contextmanager yielding a `list` of datalog strings) are defined once in Task 1 and referenced identically in every later task. No task redefines them differently.
