"""Unit tests for mcp_server.py.

All tests use a real minigraf backend — the `real_db` fixture opens a genuine
MiniGrafDb.open_in_memory() instance, so every test exercises real Datalog
parsing, schema validation, and bi-temporal semantics. A handful of
multi-commit git-ingestion tests use a real file-backed MiniGrafDb.open()
instead of `real_db`, since they need the graph to persist across separate
ingestion runs against the same path. A narrow exception: the DB lock-retry
cluster (TestGetDbLockRetry, TestTryOpenWithSelfHealReuse,
TestOpenDbAtWithExtendedRetry) uses real file-backed MiniGrafDb.open() with
genuine subprocess-manufactured lock contention, since locking is inherently
file-based. External, non-minigraf network APIs (LLM provider clients,
GitHub via the report_issue module) still get mocked to avoid real API
cost/network/non-determinism in CI — see docs/testing-conventions.md for the
full rationale and pattern reference.
"""
import asyncio
import contextlib
import json
import sqlite3
import sys
import os
import subprocess as _subprocess
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from minigraf import MiniGrafError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def reset_mcp_server_db():
    """Reset the module-level _db singleton and grammar cache between tests.

    The fact index needs no equivalent reset: real_db's tmp_path already
    gives each test an isolated graph path, and fact_index.index_path_for()
    derives the sidecar index path from it, so each test's index file lives
    in its own fresh temp directory with no cross-test state to leak.
    """
    import mcp_server
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    yield
    mcp_server._db = None
    mcp_server._grammar_cache.clear()


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
        assert mcp_server.SESSION_RULES  # sanity: rules exist to register
        # Invoke the 'linked' rule (first rule in SESSION_RULES) to confirm
        # it was registered and is callable.
        result = json.loads(real_db.execute("(query [:find ?a ?b :where (linked ?a ?b)])"))
        assert "results" in result
        # No exception during open_db (already happened via the real_db fixture)
        # is itself the regression signal for registration failures.

    def test_get_db_auto_opens_when_db_none(self, monkeypatch, tmp_path):
        from minigraf import MiniGrafDb
        real_open_in_memory = MiniGrafDb.open_in_memory
        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
        import mcp_server
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "auto.graph"))
        mcp_server._db = None
        mcp_server._graph_path = ""

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
        custom_path = str(tmp_path / "custom.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", custom_path)
        mcp_server._db = None
        mcp_server._graph_path = ""

        mcp_server.get_db()

        assert mcp_server._graph_path == custom_path


@contextlib.contextmanager
def _hold_lock_subprocess(path, exit_immediately=False, hold_seconds=None):
    """Spawn a real subprocess that opens a real MiniGrafDb at `path`, producing
    genuine cross-process lock contention.

    Exactly one of three modes applies, chosen by the arguments:

    - exit_immediately=True: the subprocess opens, prints its PID, and exits
      right away; this call blocks until it's reaped, so the yielded PID is
      guaranteed to be real and already-dead by the time control returns to
      the caller.
      NOTE: the installed minigraf build resolves a dead-holder lock file
      internally the moment MiniGrafDb.open() is next called on that path —
      verified empirically (see task-2-report.md) — so a subprocess that exits
      cleanly actually removes its own lock file rather than leaving a stale
      one behind, and even a hand-written stale lock file naming a dead PID
      lets a fresh open() succeed silently with no MiniGrafError raised at
      all. Practical effect: real subprocess timing can reproduce "holder is
      alive and contending" and "holder has cleanly finished", but not
      "open() raises citing a holder that's already dead" — that combination
      is not reachable through genuine process death on this platform, only
      through an unschedulable microsecond TOCTOU race. Tests that need to
      exercise mcp_server._clear_stale_lock's own dead-PID-detection logic use
      the PID yielded here (real, verifiably dead) to reconstruct that
      on-disk artifact by hand and call the real function directly — see
      test_self_heals_stale_lock_from_dead_pid and
      test_self_heals_dead_holder_mid_loop for the full rationale.

    - hold_seconds=<float>: the subprocess holds the lock for that many real
      wall-clock seconds (a genuine `time.sleep` inside the subprocess, not a
      mock) and then exits cleanly on its own — for "succeeds once real
      contention clears within N seconds" tests. Control returns to the
      caller as soon as the subprocess has opened the db and printed its PID
      (i.e. while it's still holding the lock), so the caller can immediately
      contend against it.

    - neither given (the default): the subprocess holds the lock alive until
      the `with` block exits — for "holder still alive" tests.

    In every mode, yields the holder subprocess's PID, and the `finally`
    clause guarantees the subprocess is terminated/reaped and its stdout pipe
    closed no matter what happens inside the `with` block (including an
    unexpected exception), so nothing leaks.
    """
    if exit_immediately and hold_seconds is not None:
        raise ValueError("exit_immediately and hold_seconds are mutually exclusive")
    if hold_seconds is not None:
        sleep_stmt = f"time.sleep({hold_seconds})\n"
    elif exit_immediately:
        sleep_stmt = ""
    else:
        sleep_stmt = "time.sleep(30)\n"
    hold_script = (
        "import minigraf, sys, time\n"
        f"db = minigraf.MiniGrafDb.open({path!r})\n"
        "print(str(__import__('os').getpid()), flush=True)\n"
        + sleep_stmt
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
        proc.stdout.close()


class TestGetDbLockRetry:
    """Regression tests for #84: get_db() must retry lock contention with
    backoff instead of letting a single "database is locked" error abort
    the caller (e.g. the git-ingestion loop), and must self-heal a stale
    lock left behind by a dead holder process."""

    def test_retries_on_lock_error_then_succeeds(self, tmp_path, monkeypatch):
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        # Real backoff (not mocked) — the subprocess needs genuine wall-clock
        # time to hold the lock and then exit before a later retry attempt
        # observes it free again.
        with _hold_lock_subprocess(graph_path, hold_seconds=0.1):
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
        # A real, deterministic non-lock MiniGrafError: pointing the graph
        # path at a directory (not a file) fails with "Is a directory" on
        # the very first open() attempt — no fabricated exception, no mock.
        graph_path = str(tmp_path / "adir")
        os.makedirs(graph_path)
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        with pytest.raises(MiniGrafError) as exc_info:
            mcp_server.get_db()
        assert "locked" not in str(exc_info.value).lower()

    def test_self_heals_stale_lock_from_dead_pid(self, tmp_path, monkeypatch):
        """If the lock's recorded holder PID is no longer running, the stale
        .lock file should be removed so the retry can succeed without
        requiring the operator to delete it manually.

        NOTE: as documented on _hold_lock_subprocess, a real open() call
        never actually raises "locked" for a lock file naming an
        already-dead PID on this platform/minigraf build — it self-heals
        silently inside MiniGrafDb.open() itself before mcp_server.py's own
        _clear_stale_lock ever gets a chance to run. This test therefore (a)
        exercises the real _clear_stale_lock function directly against a
        real stale lock file naming a real, verifiably-dead PID (obtained
        from a genuinely spawned-and-reaped subprocess, not a hardcoded
        guess), and (b) separately confirms the practically-important
        end-to-end guarantee: get_db() must not get stuck just because a
        stale lock file is lying around.
        """
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        lock_path = graph_path + ".lock"
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        with _hold_lock_subprocess(graph_path, exit_immediately=True) as dead_pid:
            pass  # holder opened, printed its PID, and is confirmed reaped/dead

        with open(lock_path, "w") as f:
            f.write(str(dead_pid))
        assert mcp_server._clear_stale_lock(graph_path, dead_pid) is True
        assert not os.path.exists(lock_path)

        with open(lock_path, "w") as f:
            f.write(str(dead_pid))
        result = mcp_server.get_db()
        assert result is not None

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
        open attempt, no matter which iteration triggered it.

        NOTE: mcp_server._clear_stale_lock itself is not reachable end-to-end
        here for the same reason documented on _hold_lock_subprocess — real
        holder death and a real "locked" MiniGrafError never coincide on
        this platform. What's still real and worth guarding: the retry loop
        must keep contending against a real, live-held lock all the way
        through its *last* attempt and succeed the moment that real
        contention clears, instead of giving up early. A real subprocess
        holds the lock through the first several backoff attempts and exits
        only shortly before the final one; a real (uninstrumented-behavior)
        open() call counter — not a canned exception — proves the loop
        actually reached its last attempt rather than succeeding trivially
        on the first.
        """
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        open_calls = {"n": 0}
        real_open = mcp_server.MiniGrafDb.open

        def counting_open(path):
            open_calls["n"] += 1
            return real_open(path)

        monkeypatch.setattr(mcp_server.MiniGrafDb, "open", staticmethod(counting_open))

        # get_db()'s cumulative real backoff before each of its 5 attempts is
        # 0, .05, .15, .35, .75s — hold the lock past the 4th attempt (~.35s)
        # but let it die before the 5th (~.75s), forcing success on the last
        # possible attempt.
        with _hold_lock_subprocess(graph_path, hold_seconds=0.5):
            result = mcp_server.get_db()

        assert result is not None
        assert open_calls["n"] == mcp_server._LOCK_RETRY_MAX, (
            f"expected the retry loop to genuinely exhaust all "
            f"{mcp_server._LOCK_RETRY_MAX} attempts against real contention "
            f"before succeeding on the last one, got {open_calls['n']} real "
            "open() calls"
        )


class TestTryOpenWithSelfHealReuse:
    """Regression test for #107: minigraf_ingest_status incorrectly reported
    "Database is locked by another process" while minigraf_ingest_git was
    actively running. Root cause: _try_open_with_self_heal always called
    _open_db_at(path) unconditionally, even when another thread had already
    opened the db and populated _db in the window between this thread's
    None-check and its own open attempt (e.g. the ingestion preload thread,
    which opens its own handle on a worker thread, racing against an
    _ensure_db_async() caller like minigraf_ingest_status during the
    "starting" phase, before _run_ingestion flips status to "running").
    That produced a second, redundant MiniGrafDb.open() from this same
    process, which collides with the first handle's still-live lock file and
    surfaces as "locked by another process" with the lock file's own PID
    equal to our own."""

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


class TestGetDbConcurrentResetRace:
    """Regression test for #122: IndexCache._rebuild calls get_db() from a
    background thread. get_db() used to read the module-level _db global
    twice -- once in `if _db is None`, once again in `return _db` -- so if
    call_tool()'s finally block reset _db to None in the window between
    those two reads, get_db() returned None even though a live db existed
    when it was called, producing "'NoneType' object has no attribute
    'execute'" in _rebuild."""

    def test_returns_live_db_despite_concurrent_reset_before_return(self):
        import inspect
        import sys as _sys
        import threading
        import mcp_server
        from minigraf import MiniGrafDb

        real_db = MiniGrafDb.open_in_memory()
        mcp_server._db = real_db

        target_code = mcp_server.get_db.__code__
        src_lines, start_line = inspect.getsourcelines(mcp_server.get_db)
        return_line = start_line + len(src_lines) - 1

        paused = threading.Event()
        resume = threading.Event()
        outcome = {}

        def local_trace(frame, event, arg):
            if event == "line" and frame.f_lineno == return_line:
                paused.set()
                resume.wait(timeout=2)
            return local_trace

        def global_trace(frame, event, arg):
            if event == "call" and frame.f_code is target_code:
                return local_trace
            return None

        def rebuild_worker():
            _sys.settrace(global_trace)
            try:
                outcome["db"] = mcp_server.get_db()
            finally:
                _sys.settrace(None)

        t = threading.Thread(target=rebuild_worker)
        t.start()
        try:
            assert paused.wait(timeout=2), "tracer never reached get_db's return line"
            # Simulate call_tool's finally block racing in between get_db()'s
            # None-check and its return.
            mcp_server._db = None
            resume.set()
        finally:
            t.join(timeout=2)

        assert outcome.get("db") is real_db, (
            "get_db() returned a stale/None value after a concurrent reset "
            "of the global between its None-check and its return (#122)"
        )


class TestDbNativeCallSerialization:
    """Regression test for #110: concurrent minigraf_query calls during active
    ingestion could silently return wrong results (ok: true) or a transient
    'Header checksum mismatch'. Root cause: the event-loop thread (call_tool
    handlers), the ingestion write_executor thread, and IndexCache._rebuild's
    background thread could all call execute()/checkpoint() on the shared
    MiniGrafDb handle at the same instant with no synchronization -- minigraf's
    own .lock file only guarantees single-process exclusivity, not thread
    safety of concurrent calls into one open handle. _db_execute/_db_checkpoint
    must serialize every native call via _db_native_lock so two threads never
    invoke the handle at the same time."""

    def test_db_execute_and_checkpoint_never_overlap_across_threads(self):
        import threading
        import time as _time
        import mcp_server

        overlap_detected = threading.Event()
        active = {"count": 0}
        active_lock = threading.Lock()

        class SlowFakeDb:
            def _mark_enter_exit(self):
                with active_lock:
                    active["count"] += 1
                    if active["count"] > 1:
                        overlap_detected.set()
                _time.sleep(0.02)
                with active_lock:
                    active["count"] -= 1

            def execute(self, datalog):
                self._mark_enter_exit()
                return json.dumps({"results": []})

            def checkpoint(self):
                self._mark_enter_exit()

        db = SlowFakeDb()

        def worker():
            for _ in range(5):
                mcp_server._db_execute(db, "(query [:find ?e :where [?e :entity-type ?t]])")
                mcp_server._db_checkpoint(db)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not overlap_detected.is_set(), (
            "concurrent threads executed native db calls at the same time -- "
            "_db_execute/_db_checkpoint must serialize via _db_native_lock (#110)"
        )


class TestOpenDbAtWithExtendedRetry:
    """Unit tests for _open_db_at_with_extended_retry — the longer,
    time-budgeted backoff used only for ingestion startup lock acquisition,
    separate from get_db()'s ~1.55s budget (#106)."""

    def test_succeeds_after_retries_within_budget(self, tmp_path, monkeypatch):
        import mcp_server
        graph_path = str(tmp_path / "t.graph")

        # Real backoff (not mocked) — the subprocess needs genuine wall-clock
        # time to hold the lock and then exit before a later retry attempt
        # observes it free again. The extended retry's 120s budget is real
        # wall-clock time too, so this stays far inside it.
        with _hold_lock_subprocess(graph_path, hold_seconds=0.1):
            result = mcp_server._open_db_at_with_extended_retry(graph_path)

        assert result is not None

    def test_gives_up_after_budget_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        # Shrink the 120s real-time budget so the test doesn't actually wait
        # two minutes — the budget is real wall-clock time (time.monotonic
        # is not mocked), so this must be a real constant, not a faked clock.
        import mcp_server
        monkeypatch.setattr(mcp_server, "_INGEST_LOCK_RETRY_BUDGET", 0.3)
        graph_path = str(tmp_path / "t.graph")

        with _hold_lock_subprocess(graph_path):
            with pytest.raises(MiniGrafError):
                mcp_server._open_db_at_with_extended_retry(graph_path)

    def test_non_lock_error_propagates_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        # A real, deterministic non-lock MiniGrafError: pointing the graph
        # path at a directory (not a file) fails with "Is a directory" on
        # the very first open() attempt — no fabricated exception, no mock.
        graph_path = str(tmp_path / "adir")
        os.makedirs(graph_path)

        with pytest.raises(MiniGrafError) as exc_info:
            mcp_server._open_db_at_with_extended_retry(graph_path)
        assert "locked" not in str(exc_info.value).lower()

    def test_self_heals_dead_holder_mid_loop(self, tmp_path, monkeypatch):
        """NOTE: as documented on _hold_lock_subprocess (see TestGetDbLockRetry
        for the full rationale), a real open() call never actually raises
        "locked" for a lock file naming an already-dead PID on this
        platform/minigraf build — it self-heals silently inside
        MiniGrafDb.open() itself. This test therefore (a) exercises the real
        _clear_stale_lock function directly against a real stale lock file
        naming a real, verifiably-dead PID, and (b) separately confirms the
        practically-important end-to-end guarantee: the extended retry must
        not get stuck just because a stale lock file is lying around.
        """
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        lock_path = graph_path + ".lock"

        with _hold_lock_subprocess(graph_path, exit_immediately=True) as dead_pid:
            pass  # holder opened, printed its PID, and is confirmed reaped/dead

        with open(lock_path, "w") as f:
            f.write(str(dead_pid))
        assert mcp_server._clear_stale_lock(graph_path, dead_pid) is True
        assert not os.path.exists(lock_path)

        with open(lock_path, "w") as f:
            f.write(str(dead_pid))
        result = mcp_server._open_db_at_with_extended_retry(graph_path)
        assert result is not None


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

    def test_transact_populates_fact_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis caching", top_n=10, boost=2.0, historical_discount=1.0)
        assert any(r[0] == ":decision/use-redis" for r in results)


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

    def test_retract_removes_from_fact_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        mcp_server.handle_minigraf_retract(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="cleanup"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis caching", top_n=10, boost=2.0, historical_discount=1.0)
        assert results == []


class TestParseFactsBlock:
    def test_single_string_valued_triple(self):
        import mcp_server
        result = mcp_server._parse_facts_block('[:decision/x :description "hello"]')
        assert result == [(":decision/x", ":description", "hello")]

    def test_keyword_valued_triple(self):
        import mcp_server
        result = mcp_server._parse_facts_block("[:decision/x :entity-type :type/decision]")
        assert result == [(":decision/x", ":entity-type", ":type/decision")]

    def test_whole_block_multiple_triples(self):
        import mcp_server
        block = (
            '[[:decision/x :description "hello"] '
            '[:decision/x :entity-type :type/decision] '
            '[:decision/x :ident ":decision/x"]]'
        )
        result = mcp_server._parse_facts_block(block)
        assert result == [
            (":decision/x", ":description", "hello"),
            (":decision/x", ":entity-type", ":type/decision"),
            (":decision/x", ":ident", ":decision/x"),
        ]

    def test_empty_block(self):
        import mcp_server
        assert mcp_server._parse_facts_block("[]") == []


class TestTransactRetractChokePoint:
    def test_transact_writes_to_index(self, real_db, tmp_path):
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:decision/x :description "hello"]]', "2026-01-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
        assert results == [[":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None]]

    def test_transact_writes_to_minigraf(self, real_db):
        import mcp_server
        mcp_server._transact(
            real_db, '[[:decision/x :description "hello"]]', "2026-01-01T00:00:00.000Z",
        )
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:decision/x :description ?v]])')
        import json
        assert json.loads(raw)["results"] == [["hello"]]

    def test_transact_with_valid_to_indexes_as_historical(self, real_db):
        """Bounded (historical) transacts are now indexed too, with their window."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:decision/x :description "hello"]]',
            "2025-01-01T00:00:00.000Z", valid_to="2025-06-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
        assert len(results) == 1
        assert results[0][0] == ":decision/x"
        assert results[0][3] == "2025-01-01T00:00:00.000Z"  # valid_from
        assert results[0][4] == "2025-06-01T00:00:00.000Z"  # valid_to

    def test_retract_removes_from_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._transact(real_db, '[[:decision/x :description "hello"]]', "2026-01-01T00:00:00.000Z")
        mcp_server._retract(real_db, '[[:decision/x :description "hello"]]')
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
        assert results == []

    def test_retract_removes_from_minigraf(self, real_db):
        import mcp_server
        import json
        mcp_server._transact(real_db, '[[:decision/x :description "hello"]]', "2026-01-01T00:00:00.000Z")
        mcp_server._retract(real_db, '[[:decision/x :description "hello"]]')
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:decision/x :description ?v]])')
        assert json.loads(raw)["results"] == []

    def test_transact_explicit_index_triples_overrides_auto_derive(self, real_db):
        """handle_minigraf_audit's use case: the Datalog string references a
        #uuid literal, but the index should record the resolved keyword ident."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:decision/x :description "hello"]]', "2026-01-01T00:00:00.000Z",
            index_triples=[(":decision/explicit-override", ":description", "hello")],
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
        assert results == [[":decision/explicit-override", ":description", "hello", "2026-01-01T00:00:00.000Z", None]]

    def test_transact_index_write_failure_does_not_raise(self, real_db, monkeypatch):
        """Index maintenance must never block a graph write -- mirrors
        IndexCache._rebuild's existing try/except at the call site."""
        import mcp_server
        import fact_index
        monkeypatch.setattr(fact_index, "open_writer", lambda path: (_ for _ in ()).throw(OSError("disk full")))
        # Must not raise despite the index write failing.
        mcp_server._transact(real_db, '[[:decision/x :description "hello"]]', "2026-01-01T00:00:00.000Z")
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:decision/x :description ?v]])')
        import json
        assert json.loads(raw)["results"] == [["hello"]]  # the graph write still succeeded


class TestBookkeepingWritesFactIndex:
    def test_watermark_update_indexes_new_hash(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._watermark_update(real_db, "abc123", "2026-01-01T00:00:00.000Z", "test")
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "abc123", top_n=10, boost=2.0, historical_discount=1.0)
        assert any(r[2] == "abc123" for r in results)

    def test_watermark_update_removes_old_hash_from_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._watermark_update(real_db, "abc123", "2026-01-01T00:00:00.000Z", "test")
        mcp_server._watermark_update(real_db, "def456", "2026-01-02T00:00:00.000Z", "test")
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "abc123", top_n=10, boost=2.0, historical_discount=1.0)
        assert not any(r[2] == "abc123" for r in results)

    def test_last_run_write_indexes(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._last_run_write(real_db, "abc123", "2026-01-01T00:00:00.000Z", 42)
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "abc123", top_n=10, boost=2.0, historical_discount=1.0)
        assert results

    def test_ingest_tags_indexes(self, real_db, tmp_path, monkeypatch):
        import mcp_server
        import fact_index
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-01T00:00:00.000Z")
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "v1.0.0", top_n=10, boost=2.0, historical_discount=1.0)
        assert results


class TestIngestTagsGraphLevelIdempotency:
    """#156: minigraf itself is not idempotent when re-transacting the same
    (entity, attribute, value) under a different valid-from -- it creates a
    second live duplicate fact, not a no-op. _ingest_tags re-runs on every
    ingestion, so it must diff against the tag's current live facts and only
    retract+re-transact attributes whose value actually changed."""

    def test_unchanged_tag_not_duplicated_on_second_run(self, real_db, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-01T00:00:00.000Z")
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-02T00:00:00.000Z")
        raw = mcp_server._db_execute(real_db, '(query [:find (count ?v) :where [:tag/v1-0-0 :name ?v]])')
        assert json.loads(raw)["results"] == [[1]]

    def test_unchanged_tag_second_run_writes_nothing(self, real_db, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-01T00:00:00.000Z")
        with execute_spy() as calls:
            mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-02T00:00:00.000Z")
        assert not any(c.startswith("(transact") or c.startswith("(retract") for c in calls)

    def test_changed_tag_value_retracts_stale_and_keeps_single_live_fact(self, real_db, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-01T00:00:00.000Z")
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-03T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-02T00:00:00.000Z")
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:tag/v1-0-0 :date ?v]])')
        assert json.loads(raw)["results"] == [["2026-01-03T00:00:00Z"]]

    def test_moved_tag_retracts_stale_commit_ref_and_keeps_single_live_fact(self, real_db, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-01T00:00:00.000Z")
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "b" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-02T00:00:00.000Z")
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:tag/v1-0-0 :tagged-commit ?v]])')
        assert json.loads(raw)["results"] == [[f":commit/{'b' * 12}"]]

    def test_new_tag_pointing_to_previously_ingested_commit_still_ingests(self, real_db, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z")],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-01T00:00:00.000Z")
        monkeypatch.setattr(
            mcp_server, "_git_tags",
            lambda repo_path: [
                ("v1.0.0", "a" * 40, "2026-01-01T00:00:00Z"),
                ("v1.1.0", "b" * 40, "2026-01-02T00:00:00Z"),
            ],
        )
        mcp_server._ingest_tags(real_db, str(tmp_path), "2026-01-02T00:00:00.000Z")
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:tag/v1-1-0 :name ?v]])')
        assert json.loads(raw)["results"] == [["v1.1.0"]]


class TestMinigrafReportIssue:
    def test_delegates_to_report_issue(self, real_db):
        import mcp_server
        mock_module = MagicMock()
        mock_module.report_issue.return_value = {
            "ok": True, "method": "gh", "repo": "org/repo", "result": "https://github.com/org/repo/issues/1"
        }
        with patch.dict("sys.modules", {"report_issue": mock_module}):
            result = mcp_server.handle_minigraf_report_issue("bug", "something broke")
        assert result == {
            "ok": True, "method": "gh", "repo": "org/repo", "result": "https://github.com/org/repo/issues/1"
        }
        mock_module.report_issue.assert_called_once_with(
            "bug", "something broke", datalog=None, error=None
        )

    def test_propagates_failure_from_report_issue(self, real_db):
        import mcp_server
        mock_module = MagicMock()
        mock_module.report_issue.return_value = {"ok": False, "error": "gh command failed"}
        with patch.dict("sys.modules", {"report_issue": mock_module}):
            result = mcp_server.handle_minigraf_report_issue("bug", "something broke")
        assert result == {"ok": False, "error": "gh command failed"}

    def test_returns_error_on_import_failure(self, real_db):
        import mcp_server
        with patch.dict("sys.modules", {"report_issue": None}):
            result = mcp_server.handle_minigraf_report_issue("bug", "something broke")
        assert result["ok"] is False


class TestHeuristicExtraction:
    def test_extracts_decision_language(self):
        import mcp_server
        facts = mcp_server.heuristic_extract(
            "User: We'll use FastAPI for the API layer.\nAgent: Got it."
        )
        assert len(facts) > 0
        assert any("FastAPI" in f["value"] for f in facts)

    def test_extracts_preference_language(self):
        import mcp_server
        facts = mcp_server.heuristic_extract(
            "I prefer PostgreSQL over MySQL for this project."
        )
        assert any("PostgreSQL" in f["value"] for f in facts)

    def test_returns_empty_list_for_no_signals(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("The sky is blue today.")
        assert facts == []

    def test_each_fact_has_required_fields(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("We decided to use Redis for caching.")
        for fact in facts:
            assert "entity" in fact
            assert "attribute" in fact
            assert "value" in fact
            assert "reason" in fact


class TestMemoryFinalizeTurnHeuristic:
    def test_transacts_extracted_facts(self, real_db, monkeypatch):
        import asyncio
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
        import mcp_server

        result = asyncio.run(mcp_server.handle_memory_finalize_turn(
            "User: We'll use Redis.\nAgent: Stored."
        ))

        assert result["ok"] is True
        assert result["stored_count"] == 1
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert queried["results"] == [["Redis"]]

    def test_returns_zero_stored_when_no_signals(self, real_db, monkeypatch):
        import asyncio
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
        import mcp_server

        result = asyncio.run(mcp_server.handle_memory_finalize_turn("The weather is fine."))

        assert result["ok"] is True
        assert result["stored_count"] == 0


class TestStripCodeFences:
    def test_no_fences_unchanged(self):
        import mcp_server
        assert mcp_server._strip_code_fences("[[]]") == "[[]]"

    def test_strips_plain_fence(self):
        import mcp_server
        assert mcp_server._strip_code_fences("```\n[[:decision/redis :description \"use Redis\"]]\n```") == '[[:decision/redis :description "use Redis"]]'

    def test_strips_language_tagged_fence(self):
        import mcp_server
        assert mcp_server._strip_code_fences("```datalog\n[[:decision/redis :description \"use Redis\"]]\n```") == '[[:decision/redis :description "use Redis"]]'

    def test_strips_surrounding_whitespace(self):
        import mcp_server
        assert mcp_server._strip_code_fences("  ```\n[[]]\n```  ") == "[[]]"

    def test_empty_list_unchanged(self):
        import mcp_server
        assert mcp_server._strip_code_fences("[]") == "[]"


class TestCallLlm:
    def test_is_openai_model_gpt(self):
        import mcp_server
        assert mcp_server._is_openai_model("gpt-4o-mini") is True

    def test_is_openai_model_o_series(self):
        import mcp_server
        assert mcp_server._is_openai_model("o1") is True
        assert mcp_server._is_openai_model("o3-mini") is True
        assert mcp_server._is_openai_model("o4") is True

    def test_is_openai_model_claude(self):
        import mcp_server
        assert mcp_server._is_openai_model("claude-haiku-4-5-20251001") is False

    def test_call_llm_anthropic_path(self, monkeypatch):
        import mcp_server
        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="[]")]
        )
        with patch("mcp_server._get_anthropic_client", return_value=mock_client):
            result = mcp_server._call_llm("claude-haiku-4-5-20251001", "test prompt")
        mock_client.messages.create.assert_called_once()
        assert result == "[]"

    def test_call_llm_openai_path(self, monkeypatch):
        import mcp_server
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="[]"))]
        )
        with patch("mcp_server._get_openai_client", return_value=mock_client):
            result = mcp_server._call_llm("gpt-4o-mini", "test prompt")
        mock_client.chat.completions.create.assert_called_once()
        assert result == "[]"


class TestLlmStrategyOpenAI:
    def test_calls_openai_api(self, real_db, monkeypatch):
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("MINIGRAF_LLM_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        import mcp_server

        fake_response_text = '[[:decision/redis :description "Redis"]]\n'
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=fake_response_text))]
        )

        with patch("mcp_server._get_openai_client", return_value=mock_openai_client):
            result = mcp_server._llm_extract_and_transact(
                "User: We'll use Redis.\nAgent: Stored."
            )

        assert result["ok"] is True
        assert result["stored_count"] > 0
        mock_openai_client.chat.completions.create.assert_called_once()
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert queried["results"] == [["Redis"]]

    def test_falls_back_to_heuristic_on_openai_failure(self, real_db, monkeypatch):
        import asyncio
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("MINIGRAF_LLM_MODEL", "gpt-4o-mini")
        import mcp_server

        with patch("mcp_server._get_openai_client", side_effect=Exception("no key")):
            result = asyncio.run(mcp_server.handle_memory_finalize_turn("We'll use Kafka."))

        assert result["ok"] is True
        assert "heuristic" in result["strategy"]
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/kafka :description ?d]])'
        ))
        assert queried["results"] == [["Kafka"]]


class TestLlmStrategy:
    def test_calls_anthropic_api(self, real_db, monkeypatch):
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import mcp_server

        fake_response_text = '[[:decision/redis :description "Redis"]]\n'
        mock_anthropic_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=fake_response_text)]
        mock_anthropic_client.messages.create.return_value = mock_message

        with patch("mcp_server._get_anthropic_client", return_value=mock_anthropic_client):
            result = mcp_server._llm_extract_and_transact(
                "User: We'll use Redis.\nAgent: Stored."
            )

        assert result["ok"] is True
        mock_anthropic_client.messages.create.assert_called_once()
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert queried["results"] == [["Redis"]]

    def test_falls_back_to_heuristic_on_api_failure(self, real_db, monkeypatch):
        import asyncio
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        import mcp_server

        with patch("mcp_server._get_anthropic_client", side_effect=Exception("no key")):
            result = asyncio.run(mcp_server.handle_memory_finalize_turn("We'll use Kafka."))

        assert result["ok"] is True
        assert "heuristic" in result["strategy"]
        assert "warning" in result
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/kafka :description ?d]])'
        ))
        assert queried["results"] == [["Kafka"]]


class TestAgentStrategy:
    def test_returns_ok_result(self, real_db, monkeypatch):
        import asyncio
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "agent")
        import mcp_server

        with patch("mcp_server._request_agent_memory_block_async",
                   new_callable=AsyncMock,
                   return_value='[[:decision/kafka :description "Kafka"]]'):
            result = asyncio.run(mcp_server._agent_extract_and_transact("We chose Kafka."))

        assert result["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/kafka :description ?d]])'
        ))
        assert queried["results"] == [["Kafka"]]

    def test_does_not_bypass_schema_validation_on_raw_sampled_datalog(self, real_db, monkeypatch):
        """#146: the agent-sampling strategy must not hand the sampled
        model's raw text straight to _transact -- that path runs no schema
        validation at all (unlike every other write path), so a
        prompt-injected model response (e.g. from a poisoned commit message
        reflected back into conversation_delta) could plant arbitrary
        attributes/entity shapes straight into the graph. It must go through
        the same parse-and-re-serialize path as the LLM strategy, which
        rejects any entity carrying an attribute outside MINIGRAF_SCHEMA."""
        import mcp_server
        import asyncio as _asyncio

        async def fake_request(conversation_delta, canonical_section):
            return (
                '[[:decision/x :description "use redis"] '
                '[:decision/x :totally-not-a-real-attr "malicious payload"]]'
            )

        monkeypatch.setattr(mcp_server, "_request_agent_memory_block_async", fake_request)
        monkeypatch.setattr(mcp_server, "_query_canonical_entities", lambda: "")
        result = _asyncio.run(mcp_server._agent_extract_and_transact("let's use redis"))

        assert result["ok"] is True
        evil = json.loads(real_db.execute(
            '(query [:find ?v :where [:decision/x :totally-not-a-real-attr ?v]])'
        ))
        assert evil["results"] == []

    def test_auto_tags_entity_type_and_ident_without_manual_triples(self, real_db, monkeypatch):
        """#153: once routed through _transact_extracted_facts, the agent
        strategy no longer needs the model to hand-emit :entity-type/:ident
        companion triples itself (_AGENT_SAMPLING_PROMPT never asked it to) --
        auto-tagging kicks in the same way it already does for the heuristic
        and LLM strategies."""
        import mcp_server
        import asyncio as _asyncio

        async def fake_request(conversation_delta, canonical_section):
            return '[[:decision/use-redis :description "use Redis for caching"]]'

        monkeypatch.setattr(mcp_server, "_request_agent_memory_block_async", fake_request)
        monkeypatch.setattr(mcp_server, "_query_canonical_entities", lambda: "")
        result = _asyncio.run(mcp_server._agent_extract_and_transact("let's use redis"))

        assert result["ok"] is True
        ident = json.loads(real_db.execute(
            '(query [:find ?i :where [:decision/use-redis :ident ?i]])'
        ))
        assert ident["results"] == [[":decision/use-redis"]]


class TestAliasEnrichment:
    def test_llm_strategy_alias_bridges_a_lexically_disjoint_query(self, real_db, monkeypatch):
        """The actual point of this feature: a query using words that never
        appear in the fact's own description text finds it anyway, via an
        LLM-generated :alias fact."""
        import mcp_server

        def fake_call_llm(model, prompt):
            return (
                '[[:decision/use-redis :description "use Redis for the caching layer"] '
                '[:decision/use-redis :alias "in-memory data store, key-value cache, caching backend"]]'
            )

        monkeypatch.setattr(mcp_server, "_call_llm", fake_call_llm)
        result = mcp_server._llm_extract_and_transact("let's use Redis for caching")
        assert result["ok"] is True
        assert result["stored_count"] >= 1
        # "key-value cache" appears ONLY in the alias, never in the description.
        context = mcp_server.handle_memory_prepare_turn("key-value cache")
        assert ":decision/use-redis" in context

    def test_agent_strategy_alias_bridges_a_lexically_disjoint_query(self, real_db, monkeypatch):
        """Like the LLM strategy, _agent_extract_and_transact routes the
        agent's Datalog block through _transact_extracted_facts (#146/#153),
        which auto-tags :entity-type/:ident -- so the entity stays
        ident-resolvable (vs. falling back to its raw UUID) after the
        mandatory first-call index backfill even though the fake response
        below doesn't supply those companion triples itself."""
        import mcp_server
        import asyncio as _asyncio

        async def fake_request(conversation_delta, canonical_section):
            return (
                '[[:decision/use-redis :description "use Redis for the caching layer"] '
                '[:decision/use-redis :alias "in-memory data store, key-value cache, caching backend"]]'
            )

        monkeypatch.setattr(mcp_server, "_request_agent_memory_block_async", fake_request)
        monkeypatch.setattr(mcp_server, "_query_canonical_entities", lambda: "")
        result = _asyncio.run(mcp_server._agent_extract_and_transact("let's use Redis for caching"))
        assert result["ok"] is True
        context = mcp_server.handle_memory_prepare_turn("key-value cache")
        assert ":decision/use-redis" in context

    def test_llm_extraction_prompt_instructs_alias_generation(self):
        """A prompt-content smoke test: the instruction must actually exist,
        not just be theoretically supported by the schema/parser."""
        import mcp_server
        assert "alias" in mcp_server._LLM_EXTRACTION_PROMPT.lower()
        assert "synonym" in mcp_server._LLM_EXTRACTION_PROMPT.lower() or "alternative term" in mcp_server._LLM_EXTRACTION_PROMPT.lower()

    def test_agent_sampling_prompt_instructs_alias_generation(self):
        import mcp_server
        assert "alias" in mcp_server._AGENT_SAMPLING_PROMPT.lower()
        assert "synonym" in mcp_server._AGENT_SAMPLING_PROMPT.lower() or "alternative term" in mcp_server._AGENT_SAMPLING_PROMPT.lower()


class TestConversationalMemoryFactIndex:
    def test_transact_extracted_facts_indexes(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._transact_extracted_facts([
            {"entity": ":decision/x", "entity_type": "decision", "attribute": ":description", "value": "use redis"},
        ])
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis", top_n=10, boost=2.0, historical_discount=1.0)
        assert any(r[0] == ":decision/x" for r in results)

    def test_transact_extracted_facts_escapes_quotes_in_value(self, real_db):
        """#146: a value containing an embedded double-quote must not be able
        to close the Datalog string literal early and splice in extra facts
        -- values here can originate from LLM-produced text (the LLM
        extraction strategy) which is not constrained to a safe character
        set the way heuristic-extracted values are."""
        import mcp_server
        malicious = 'use redis"] [:decision/evil :pwned "yes'
        stored = mcp_server._transact_extracted_facts([
            {"entity": ":decision/x", "entity_type": "decision", "attribute": ":description", "value": malicious},
        ])
        assert stored == 1
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/x :description ?d]])'
        ))
        assert queried["results"] == [[malicious]]
        evil = json.loads(real_db.execute(
            '(query [:find ?v :where [:decision/evil :pwned ?v]])'
        ))
        assert evil["results"] == []

    def test_agent_extract_and_transact_indexes(self, real_db, monkeypatch):
        import mcp_server
        import fact_index
        import asyncio as _asyncio

        async def fake_request(conversation_delta, canonical_section):
            return '[[:decision/x :description "use redis"]]'

        monkeypatch.setattr(mcp_server, "_request_agent_memory_block_async", fake_request)
        monkeypatch.setattr(mcp_server, "_query_canonical_entities", lambda: "")
        result = _asyncio.run(mcp_server._agent_extract_and_transact("we should use redis"))
        assert result["ok"] is True
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis", top_n=10, boost=2.0, historical_discount=1.0)
        assert any(r[0] == ":decision/x" for r in results)


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
            "minigraf_query", {"datalog": "[:find ?n :where [:e1 :name ?n]]"}
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

    def test_call_tool_memory_prepare_turn(self, real_db):
        import asyncio
        import mcp_server

        result = asyncio.run(mcp_server.call_tool(
            "memory_prepare_turn", {"user_message": "what database are we using?"}
        ))

        assert len(result) == 1
        assert isinstance(result[0].text, str)

    def test_call_tool_memory_finalize_turn(self, real_db):
        import asyncio
        import mcp_server

        result = asyncio.run(mcp_server.call_tool(
            "memory_finalize_turn", {"conversation_delta": "We'll use Redis for caching."}
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True
        assert data["stored_count"] >= 1
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/redis :description ?d]])'
        ))
        assert queried["results"] != []

    def test_call_tool_minigraf_retract(self, real_db):
        import asyncio
        import mcp_server
        real_db.execute('(transact {} [[:decision/cache :description "Redis"]])')

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_retract",
            {"facts": '[[:decision/cache :description "Redis"]]', "reason": "no longer needed"},
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?d :where [:decision/cache :description ?d]])'
        ))
        assert queried["results"] == []

    def test_db_released_after_call_tool(self, real_db):
        import asyncio
        import mcp_server

        asyncio.run(mcp_server.call_tool(
            "minigraf_query", {"datalog": "[:find ?x :where [?e :x ?x]]"}
        ))

        assert mcp_server._db is None, "lock must be released after call_tool so prepare_hook can open the DB"

    def test_call_tool_unknown_raises(self, real_db):
        import asyncio
        import mcp_server

        with pytest.raises(Exception, match="Unknown tool"):
            asyncio.run(mcp_server.call_tool("nonexistent_tool", {}))

    def test_call_tool_lock_retry_does_not_block_event_loop(self, tmp_path, monkeypatch):
        """Regression test for #99: lock-retry backoff hit while opening the DB
        for a tool call must not use a blocking time.sleep(), since call_tool
        runs on the single-threaded asyncio event loop — a blocking sleep there
        would freeze the very coroutine (e.g. ingestion) that's about to
        release the lock we're waiting on.

        Uses a real subprocess (_hold_lock_subprocess, see TestGetDbLockRetry)
        to manufacture genuine cross-process lock contention rather than
        mocking MiniGrafDb.open, since this test specifically exercises the
        real lock-retry backoff path (_ensure_db_async), not general dispatch."""
        import asyncio
        import mcp_server
        graph_path = str(tmp_path / "t.graph")
        mcp_server._db = None
        mcp_server._graph_path = graph_path

        def fail_if_called(_delay):
            raise AssertionError("time.sleep() must not be called on the event-loop retry path (see #99)")
        monkeypatch.setattr(mcp_server.time, "sleep", fail_if_called)

        # Real backoff (not mocked) — the subprocess needs genuine wall-clock
        # time to hold the lock and then exit before a later retry attempt
        # observes it free again.
        with _hold_lock_subprocess(graph_path, hold_seconds=0.1):
            result = asyncio.run(mcp_server.call_tool(
                "minigraf_query", {"datalog": "[:find ?x :where [?e :x ?x]]"}
            ))

        data = json.loads(result[0].text)
        assert data["ok"] is True


class TestParseValidAtHint:
    def test_returns_utc_ms_timestamp_when_no_hint(self):
        import re
        import mcp_server
        valid_at, datalog = mcp_server._parse_valid_at_hint('[[:e :a "v"]]')
        assert datalog == '[[:e :a "v"]]'
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", valid_at)

    def test_extracts_valid_at_date_from_comment(self):
        import mcp_server
        raw = '; valid-at: 2024-03-15\n[[:decision/x :desc "y"]]'
        valid_at, datalog = mcp_server._parse_valid_at_hint(raw)
        assert valid_at == "2024-03-15"
        assert "; valid-at:" not in datalog
        assert '[[:decision/x :desc "y"]]' in datalog

    def test_ignores_invalid_date_format_in_comment(self):
        import re
        import mcp_server
        raw = '; valid-at: not-a-date\n[[:e :a "v"]]'
        valid_at, datalog = mcp_server._parse_valid_at_hint(raw)
        # Falls back to current UTC ms timestamp
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", valid_at)

    def test_last_valid_at_comment_wins_when_multiple(self):
        import mcp_server
        raw = '; valid-at: 2024-01-01\n; valid-at: 2025-06-30\n[[:e :a "v"]]'
        valid_at, datalog = mcp_server._parse_valid_at_hint(raw)
        assert valid_at == "2025-06-30"


class TestCanonicalIdent:
    def test_lowercases_value(self):
        import mcp_server
        assert mcp_server._canonical_ident("decision", "Redis") == ":decision/redis"

    def test_replaces_spaces_with_hyphens(self):
        import mcp_server
        assert mcp_server._canonical_ident("preference", "use postgres") == ":preference/use-postgres"

    def test_replaces_underscores(self):
        import mcp_server
        assert mcp_server._canonical_ident("constraint", "must_be_stateless") == ":constraint/must-be-stateless"

    def test_replaces_dots(self):
        import mcp_server
        assert mcp_server._canonical_ident("dependency", "pydantic.v2") == ":dependency/pydantic-v2"

    def test_collapses_consecutive_hyphens(self):
        import mcp_server
        assert mcp_server._canonical_ident("decision", "use  Redis") == ":decision/use-redis"

    def test_strips_leading_trailing_hyphens(self):
        import mcp_server
        assert mcp_server._canonical_ident("decision", " redis ") == ":decision/redis"


class TestValidateFacts:
    def test_valid_fact_no_violations(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"}]
        assert mcp_server._validate_facts(facts) == []

    def test_missing_required_attribute(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":rationale", "value": "fast"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert ":description" in violations[0]

    def test_unknown_entity_type_rejected(self):
        import mcp_server
        facts = [{"entity": ":service/auth", "entity_type": "service",
                  "attribute": ":description", "value": "auth service"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert "service" in violations[0]

    def test_unknown_attribute_rejected(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"},
                 {"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":unknown-attr", "value": "foo"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert ":unknown-attr" in violations[0]

    def test_wrong_value_type_rejected(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": 42}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1

    def test_valid_alias_passes(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"},
                 {"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":alias", "value": "Redis-based cache"}]
        assert mcp_server._validate_facts(facts) == []

    def test_alias_wrong_type_rejected(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"},
                 {"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":alias", "value": 99}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1

    def test_all_four_entity_types_accepted(self):
        import mcp_server
        for etype in ("decision", "preference", "constraint", "dependency"):
            facts = [{"entity": f":{etype}/x", "entity_type": etype,
                      "attribute": ":description", "value": "test"}]
            assert mcp_server._validate_facts(facts) == [], f"Failed for {etype}"


class TestHeuristicNormalization:
    def test_ident_uses_canonical_slug(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("We'll use Redis for caching.")
        assert any(f["entity"] == ":decision/redis" for f in facts)

    def test_ident_not_underscore_form(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("We'll use postgres-db for storage.")
        matching = [f for f in facts if "postgres" in f["entity"]]
        assert matching, "No fact with postgres found"
        assert "_" not in matching[0]["entity"], f"Underscore found in {matching[0]['entity']}"


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

    def test_transact_extracted_facts_does_not_drop_sibling_optional_attributes(self, real_db):
        """Regression test for a real bug: _transact_extracted_facts used to
        validate each fact dict in isolation, so a standalone :alias (or
        :rationale/:date) triple for an entity was always judged "missing
        required :description" and silently dropped, even when a sibling
        :description fact for the same entity was present elsewhere in the
        same batch -- exactly the shape the extraction prompts have always
        asked models to produce. Validation must be done per-entity-group,
        not per-fact."""
        import mcp_server
        facts = [
            {"entity": ":decision/redis", "entity_type": "decision",
             "attribute": ":description", "value": "use Redis"},
            {"entity": ":decision/redis", "entity_type": "decision",
             "attribute": ":alias", "value": "in-memory store, key-value cache"},
        ]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 2
        result = json.loads(real_db.execute(
            '(query [:find ?a ?v :where [:decision/redis ?a ?v]])'
        ))
        attrs = {a: v for a, v in result["results"]}
        assert attrs[":description"] == "use Redis"
        assert attrs[":alias"] == "in-memory store, key-value cache"

    def test_entity_missing_description_entirely_is_still_rejected(self, real_db):
        """Confirms the per-entity-group fix doesn't loosen validation: an
        entity with no :description fact ANYWHERE in the batch (not just in
        the one triple under consideration) must still be rejected. This
        exact input is unaffected by the grouping change (the entity's group
        has only one fact, so per-fact and per-group validation coincide),
        which is what proves the fix only changes behavior for entities that
        genuinely do have a sibling :description elsewhere in the batch."""
        import mcp_server
        facts = [
            {"entity": ":decision/redis", "entity_type": "decision",
             "attribute": ":alias", "value": "in-memory store, key-value cache"},
        ]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 0
        result = json.loads(real_db.execute(
            '(query [:find ?a ?v :where [:decision/redis ?a ?v]])'
        ))
        assert result["results"] == []


class TestParseTransactFacts:
    def test_parses_single_triple(self):
        import mcp_server
        facts = mcp_server._parse_transact_facts(
            '[[:decision/redis :description "use Redis"]]'
        )
        assert len(facts) == 1
        assert facts[0]["entity"] == ":decision/redis"
        assert facts[0]["attribute"] == ":description"
        assert facts[0]["value"] == "use Redis"
        assert facts[0]["entity_type"] == "decision"

    def test_parses_multiple_triples(self):
        import mcp_server
        facts = mcp_server._parse_transact_facts(
            '[[:decision/redis :description "use Redis"] '
            '[:decision/redis :rationale "fast"]]'
        )
        assert len(facts) == 2

    def test_returns_empty_for_non_string_values(self):
        import mcp_server
        facts = mcp_server._parse_transact_facts(
            "[[:decision/redis :entity-type :type/decision]]"
        )
        assert facts == []

    def test_extracts_entity_type_from_namespace(self):
        import mcp_server
        facts = mcp_server._parse_transact_facts(
            '[[:service/auth :description "auth service"]]'
        )
        assert len(facts) == 1
        assert facts[0]["entity_type"] == "service"
        assert facts[0]["entity"] == ":service/auth"


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
        # Keyword-only triple (no quoted string values) — not schema-validated by design
        result = mcp_server.handle_minigraf_transact(
            '[[:service/auth :calls :component/jwt]]',
            reason="test relationship edge"
        )

        assert result["ok"] is True
        queried = json.loads(real_db.execute(
            '(query [:find ?c :where [:service/auth :calls ?c]])'
        ))
        assert queried["results"] == [[":component/jwt"]]


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

    def test_audit_retract_removes_from_fact_index_by_keyword_ident(self, real_db):
        """The Datalog retract uses #uuid literals (audit's own design,
        so it can retract without a keyword-to-UUID lookup), but the entity
        was originally indexed under its keyword ident -- the index
        deletion must use kw_ident, not the #uuid string, or the row is
        stranded."""
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/bad :description "placeholder"] '
            '[:decision/bad :entity-type :type/decision] '
            '[:decision/bad :ident ":decision/bad"]]',
            reason="test",
        )
        # Manufacture a real schema violation for audit to find: retract the
        # entity's only non-system attribute (:description), leaving just
        # :entity-type/:ident (both in _SYSTEM_ATTRS, filtered out of
        # attr_facts). handle_minigraf_audit's own "if not attr_facts"
        # fallback then substitutes a single :__no_attributes__ fact, which
        # _validate_facts flags two ways: "decision" requires :description
        # (missing) and :__no_attributes__ itself is an unknown attribute.
        # Verified directly against _validate_facts (mcp_server.py) before
        # writing this test, rather than assumed.
        mcp_server._retract(real_db, '[[:decision/bad :description "placeholder"]]')
        result = mcp_server.handle_minigraf_audit()
        assert result["ok"] is True
        assert result["retracted"] >= 1
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "decision bad", top_n=10, boost=2.0, historical_discount=1.0)
        assert not any(r[0] == ":decision/bad" for r in results)


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
        """SESSION_RULES registers `linked`/`reachable` over the :contains
        edge (module/class -> function/field structural containment):
            (rule [(linked ?a ?b) [?a :contains ?b]])
            (rule [(reachable ?a ?b) [?a :contains ?b]])
        Prove those clauses were actually registered at open_db time by
        transacting a real :contains edge and invoking `linked`/`reachable`
        with the subject pinned to the literal entity (so the returned
        value is the human-readable keyword, not the entity's internal
        UUID) to confirm the rule actually derives the edge — a raw
        [?a :contains ?b] triple pattern would pass even if the rule
        registration silently failed, so this specifically calls through
        the rule name.
        """
        import mcp_server
        real_db.execute(
            '(transact {} [[:module/foo :contains :function/bar]])'
        )

        linked = json.loads(real_db.execute(
            '(query [:find ?b :where (linked :module/foo ?b)])'
        ))
        assert linked["results"] == [[":function/bar"]]

        reachable = json.loads(real_db.execute(
            '(query [:find ?b :where (reachable :module/foo ?b)])'
        ))
        assert reachable["results"] == [[":function/bar"]]

        # A non-matching pair must not spuriously match — proves this is a
        # real join against the transacted data, not a rule that always
        # succeeds regardless of the graph contents.
        no_match = json.loads(real_db.execute(
            '(query [:find ?b :where (linked :module/nonexistent ?b)])'
        ))
        assert no_match["results"] == []


class TestGetParser:
    def test_python_file_returns_parser(self):
        import mcp_server
        from unittest.mock import MagicMock
        mcp_server._grammar_cache.clear()
        # Mock tree_sitter and the individual tree_sitter_python package
        mock_parser = MagicMock()
        mock_tree_sitter = MagicMock()
        mock_tree_sitter.Parser.return_value = mock_parser
        mock_tree_sitter_python = MagicMock()
        mock_tree_sitter_python.language.return_value = MagicMock()

        with patch.dict("sys.modules", {"tree_sitter": mock_tree_sitter, "tree_sitter_python": mock_tree_sitter_python}):
            parser = mcp_server._get_parser("src/auth.py")
            assert parser is not None

    def test_unknown_extension_returns_none(self):
        import mcp_server
        parser = mcp_server._get_parser("data.csv")
        assert parser is None

    def test_parser_is_cached_on_second_call(self):
        import mcp_server
        from unittest.mock import MagicMock
        mcp_server._grammar_cache.clear()
        mock_parser = MagicMock()
        mock_tree_sitter = MagicMock()
        mock_tree_sitter.Parser.return_value = mock_parser
        mock_tree_sitter_python = MagicMock()
        mock_tree_sitter_python.language.return_value = MagicMock()

        with patch.dict("sys.modules", {"tree_sitter": mock_tree_sitter, "tree_sitter_python": mock_tree_sitter_python}):
            p1 = mcp_server._get_parser("foo.py")
            p2 = mcp_server._get_parser("bar.py")
            assert p1 is p2  # same cached parser instance

    def test_unsupported_grammar_returns_none(self):
        import mcp_server
        # Simulate a language in the ext map but whose grammar fails to load
        mcp_server._grammar_cache.clear()
        mcp_server._grammar_cache["python"] = None
        parser = mcp_server._get_parser("foo.py")
        assert parser is None

    def test_warns_once_when_grammar_unavailable(self, capsys):
        """Regression test for issue #86: a failed parser construction must
        surface a warning, not fail completely silently, and must only warn
        once per language (not once per file/commit)."""
        import mcp_server
        mcp_server._grammar_cache.clear()

        with patch.dict("sys.modules", {"tree_sitter_python": None}):
            p1 = mcp_server._get_parser("a.py")
            p2 = mcp_server._get_parser("b.py")

        assert p1 is None
        assert p2 is None
        err = capsys.readouterr().err
        assert "no tree-sitter grammar available for 'python'" in err
        assert err.count("no tree-sitter grammar available for 'python'") == 1


class TestThreadParser:
    def test_returns_none_for_unsupported_extension(self):
        import mcp_server
        assert mcp_server._thread_parser("data.csv") is None

    def test_returns_a_parser_for_supported_extension(self):
        import mcp_server
        parser = mcp_server._thread_parser("foo.py")
        assert parser is not None

    def test_different_threads_get_different_parser_instances(self):
        import mcp_server
        import threading

        results = {}

        def grab(name):
            results[name] = mcp_server._thread_parser("foo.py")

        t1 = threading.Thread(target=grab, args=("t1",))
        t2 = threading.Thread(target=grab, args=("t2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["t1"] is not None
        assert results["t2"] is not None
        assert results["t1"] is not results["t2"]

    def test_same_thread_reuses_its_own_parser_instance(self):
        import mcp_server
        p1 = mcp_server._thread_parser("foo.py")
        p2 = mcp_server._thread_parser("bar.py")  # same language, same thread
        assert p1 is p2

    def test_concurrent_first_use_of_new_language_warns_once(self, capsys):
        """Two threads racing to build the same never-seen-before language's
        grammar for the first time must not double-import or double-warn —
        regression guard for the lock added around _get_parser's
        first-time-construction branch."""
        import mcp_server
        import threading

        barrier = threading.Barrier(2)

        def touch():
            barrier.wait()
            mcp_server._thread_parser("foo.c")

        threads = [threading.Thread(target=touch) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        err = capsys.readouterr().err
        # Either the grammar loads fine (no warning at all) or, if it's
        # missing in this environment, the warning fires at most once.
        assert err.count("no tree-sitter grammar available for 'c'") <= 1


class TestExtToLangHeaders:
    """Regression tests for issue #92 bug 2: header extensions (.h/.hpp/...)
    were missing from _EXT_TO_LANG entirely, so _get_parser returned None for
    them and header files were skipped by the ingest loop."""

    def test_h_maps_to_c(self):
        import mcp_server
        assert mcp_server._EXT_TO_LANG[".h"] == "c"

    def test_hpp_hh_hxx_map_to_cpp(self):
        import mcp_server
        assert mcp_server._EXT_TO_LANG[".hpp"] == "cpp"
        assert mcp_server._EXT_TO_LANG[".hh"] == "cpp"
        assert mcp_server._EXT_TO_LANG[".hxx"] == "cpp"

    def test_cc_cxx_map_to_cpp(self):
        import mcp_server
        assert mcp_server._EXT_TO_LANG[".cc"] == "cpp"
        assert mcp_server._EXT_TO_LANG[".cxx"] == "cpp"


class TestExtractFromSource:
    def _python_parser(self):
        """Return a real tree_sitter.Parser for Python, built directly from the
        installed tree-sitter-python package and injected into the grammar cache
        so _get_parser's import machinery isn't exercised here."""
        import mcp_server
        import tree_sitter
        import tree_sitter_python
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(tree_sitter_python.language())
        real_parser = tree_sitter.Parser(real_lang)
        mcp_server._grammar_cache["python"] = real_parser
        return real_parser

    def test_extracts_function_names(self):
        import mcp_server
        source = b"def login(user):\n    pass\ndef logout():\n    pass\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "auth.py")
        assert "login" in result["functions"]
        assert "logout" in result["functions"]

    def test_extracts_class_names(self):
        import mcp_server
        source = b"class User:\n    pass\nclass Admin(User):\n    pass\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "models.py")
        assert "User" in result["classes"]
        assert "Admin" in result["classes"]

    def test_extracts_from_imports(self):
        import mcp_server
        source = b"import os\nfrom pathlib import Path\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "foo.py")
        assert "os" in result["imports"]
        assert "pathlib" in result["imports"]

    def test_extracts_call_names(self):
        import mcp_server
        source = b"def foo():\n    bar()\n    baz(1, 2)\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "foo.py")
        assert "bar" in result["calls"]
        assert "baz" in result["calls"]

    def test_parse_error_returns_empty(self):
        import mcp_server
        # Passing None as parser triggers AttributeError → except block returns empty dict
        result = mcp_server._extract_from_source(b"def foo(): pass", None, "x.py")
        assert result == {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "globals": [], "fields": [],
        }

    def test_body_text_not_shipped_across_process_boundary(self):
        """_extract_from_source's dict crosses the ProcessPoolExecutor boundary
        (via _extract_commit), so it must NOT carry full entity body text — the
        matcher works on live re-parsed nodes, never on this text. These four
        keys used to be pickled per-file for no consumer (P3)."""
        import mcp_server
        source = b"GLOBAL_X = 5\n\ndef login(user):\n    return user.ok\n\nclass User:\n    field = 1\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "auth.py")
        assert "login" in result["functions"]
        assert "User" in result["classes"]
        assert "GLOBAL_X" in result["globals"]
        for dead_key in ("function_bodies", "class_bodies", "global_bodies", "field_info"):
            assert dead_key not in result, f"{dead_key} must not cross the process boundary"


class TestExtractGlobalsAndFields:
    def _python_parser(self):
        import tree_sitter_python
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_python.language()))

    def test_unsupported_language_returns_empty(self):
        import mcp_server
        result = mcp_server._extract_globals_and_fields(None, "nonexistent_lang")
        assert result == {
            "globals": [], "global_bodies": {}, "fields": [], "field_info": {},
            "global_nodes": {}, "field_nodes": {},
        }

    def test_dispatches_to_registered_language_extractor(self):
        import mcp_server
        sentinel = {
            "globals": ["X"], "global_bodies": {"X": "X = 1"}, "fields": [], "field_info": {},
            "global_nodes": {}, "field_nodes": {},
        }
        mcp_server._GLOBAL_FIELD_EXTRACTORS["_test_lang"] = lambda root: sentinel
        try:
            result = mcp_server._extract_globals_and_fields("fake_root", "_test_lang")
            assert result == sentinel
        finally:
            del mcp_server._GLOBAL_FIELD_EXTRACTORS["_test_lang"]

    def test_extract_from_source_merges_globals_and_fields(self):
        import mcp_server
        source = b"def foo(): pass"
        result = mcp_server._extract_from_source(source, self._python_parser(), "x.py")
        assert result["globals"] == []
        assert result["fields"] == []


class TestPythonGlobalsAndFields:
    def _parser(self):
        import tree_sitter_python
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_python.language()))

    def test_module_level_global(self):
        import mcp_server
        tree = self._parser().parse(b"GLOBAL_X = 5\n")
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        assert "GLOBAL_X" in result["globals"]
        assert "GLOBAL_X = 5" in result["global_bodies"]["GLOBAL_X"]

    def test_class_variable_is_static_field(self):
        import mcp_server
        source = b"class Foo:\n    class_var = 10\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        names = [n for n, _c, _s in result["fields"]]
        assert "class_var" in names
        info = result["field_info"]["class_var"]
        assert info["class"] == "Foo"
        assert info["static"] is True

    def test_self_attribute_in_init_is_instance_field(self):
        import mcp_server
        source = b"class Foo:\n    def __init__(self):\n        self.instance_var = 1\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        names = [n for n, _c, _s in result["fields"]]
        assert "instance_var" in names
        info = result["field_info"]["instance_var"]
        assert info["class"] == "Foo"
        assert info["static"] is False

    def test_local_variable_inside_function_not_captured(self):
        import mcp_server
        source = b"def foo():\n    local_x = 1\n    return local_x\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_self_attribute_outside_init_not_captured(self):
        """Scoped deliberately to __init__ only — see design plan's stated limitation."""
        import mcp_server
        source = b"class Foo:\n    def other(self):\n        self.dynamic = 1\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        assert result["fields"] == []


class TestJsFamilyGlobalsAndFields:
    def _js_parser(self):
        import tree_sitter_javascript
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_javascript.language()))

    def _ts_parser(self):
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_typescript.language_typescript()))

    def test_js_module_level_global(self):
        import mcp_server
        tree = self._js_parser().parse(b"const GLOBAL_X = 5;\n")
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert "GLOBAL_X" in result["globals"]

    def test_js_static_and_instance_fields(self):
        import mcp_server
        source = b"class Foo {\n  static staticField = 1;\n  instanceField = 2;\n}\n"
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_ts_public_field_definition_static(self):
        import mcp_server
        source = b"class Foo {\n  static staticField: number = 1;\n  instanceField: number = 2;\n}\n"
        tree = self._ts_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_local_variable_not_captured(self):
        import mcp_server
        source = b"function foo() {\n  const localX = 1;\n  return localX;\n}\n"
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_export_const_captured_as_global(self):
        import mcp_server
        tree = self._js_parser().parse(b"export const GLOBAL_X = 5;\n")
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert "GLOBAL_X" in result["globals"]
        assert result["global_bodies"]["GLOBAL_X"].startswith("export const")

    def test_export_let_captured_as_global(self):
        import mcp_server
        tree = self._js_parser().parse(b"export let GLOBAL_Y = 5;\n")
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert "GLOBAL_Y" in result["globals"]
        assert result["global_bodies"]["GLOBAL_Y"].startswith("export let")

    def test_export_class_captures_fields(self):
        import mcp_server
        source = b"export class Foo { static a = 1; b = 2; }\n"
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", True)
        assert info["b"] == ("Foo", False)

    def test_export_default_class_captures_fields(self):
        import mcp_server
        source = b"export default class Bar { c = 3; }\n"
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["c"] == ("Bar", False)

    def test_export_const_and_class_together(self):
        import mcp_server
        source = (
            b"export const X = 5;\n"
            b"export class Foo { static a = 1; b = 2; }\n"
            b"export default class Bar { c = 3; }\n"
        )
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert "X" in result["globals"]
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", True)
        assert info["b"] == ("Foo", False)
        assert info["c"] == ("Bar", False)


class TestRustGoCGlobalsAndFields:
    def _rust_parser(self):
        import tree_sitter_rust
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_rust.language()))

    def _go_parser(self):
        import tree_sitter_go
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_go.language()))

    def _c_parser(self):
        import tree_sitter_c
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_c.language()))

    def test_rust_static_and_const_are_globals(self):
        import mcp_server
        source = b"static GLOBAL_X: i32 = 5;\nconst GLOBAL_Y: i32 = 10;\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"GLOBAL_X", "GLOBAL_Y"}

    def test_rust_struct_field_is_instance_only(self):
        import mcp_server
        source = b"struct Foo {\n    instance_field: i32,\n}\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instance_field"] == ("Foo", False)

    def test_rust_impl_const_is_static_field(self):
        import mcp_server
        source = b"impl Foo {\n    const ASSOC_CONST: i32 = 1;\n}\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["ASSOC_CONST"] == ("Foo", True)

    def test_rust_generic_impl_const_owning_class_strips_type_params(self):
        # For `impl<T> Foo<T> { ... }`, the impl_item's `type` field is a
        # generic_type node whose text is "Foo<T>", not a bare
        # type_identifier -- verified against the real installed
        # tree-sitter-rust grammar. The owning_class recorded for an
        # associated const must be the clean base name "Foo" (matching
        # what struct_item's `name` field produces for the same struct
        # elsewhere), not "Foo<T>", or the field's :class edge orphans
        # itself from the struct's actual registered class ident.
        import mcp_server
        source = b"struct Foo<T> {\n    val: T,\n}\nimpl<T> Foo<T> {\n    const CAP: usize = 16;\n}\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["CAP"] == ("Foo", True)
        assert result["field_info"]["CAP"]["class"] == "Foo"

    def test_rust_pub_visibility_does_not_break_extraction(self):
        # `pub` is a visibility_modifier CHILD of static_item/struct_item/
        # field_declaration/const_item (verified against the real installed
        # tree-sitter-rust grammar) -- it does NOT wrap the declaration in a
        # separate node the way JS's `export` does. This test locks in that
        # finding: pub items must still be captured, with the same
        # field:name resolution as their non-pub counterparts.
        import mcp_server
        source = (
            b"pub static GLOBAL_X: i32 = 5;\n"
            b"pub struct Foo {\n    pub instance_field: i32,\n}\n"
            b"impl Foo {\n    pub const ASSOC_CONST: i32 = 1;\n}\n"
        )
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        assert "GLOBAL_X" in result["globals"]
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instance_field"] == ("Foo", False)
        assert info["ASSOC_CONST"] == ("Foo", True)

    def test_go_package_level_var_and_const_are_globals(self):
        import mcp_server
        source = b"package main\n\nvar GlobalX = 5\nconst GlobalY = 10\n"
        tree = self._go_parser().parse(source)
        result = mcp_server._extract_go_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"GlobalX", "GlobalY"}

    def test_go_grouped_var_declaration_captures_all_specs(self):
        # A grouped `var (...)` block wraps its var_spec children in an
        # intermediate var_spec_list node in the real installed
        # tree-sitter-go grammar -- unlike grouped const/type blocks,
        # which don't wrap. Verified empirically; this locks in that a
        # grouped var block (an idiomatic, common real-world Go pattern)
        # isn't silently dropped the way an unwrapped implementation
        # would drop it.
        import mcp_server
        source = b"package main\n\nvar (\n\tA = 1\n\tB = 2\n)\nconst (\n\tC = 3\n\tD = 4\n)\n"
        tree = self._go_parser().parse(source)
        result = mcp_server._extract_go_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"A", "B", "C", "D"}

    def test_go_struct_field_is_instance_only(self):
        import mcp_server
        source = b"package main\n\ntype Foo struct {\n    InstanceField int\n}\n"
        tree = self._go_parser().parse(source)
        result = mcp_server._extract_go_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["InstanceField"] == ("Foo", False)

    def test_go_multi_name_struct_field_captures_all_names(self):
        # `X, Y int` inside a struct puts more than one node under the
        # `name` field of a single field_declaration -- singular
        # child_by_field_name only returns the first, silently dropping
        # `Y`. Verified empirically against the real installed
        # tree-sitter-go grammar; children_by_field_name (plural) is
        # used instead.
        import mcp_server
        source = b"package main\n\ntype Foo struct {\n\tX, Y int\n}\n"
        tree = self._go_parser().parse(source)
        result = mcp_server._extract_go_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["X"] == ("Foo", False)
        assert info["Y"] == ("Foo", False)

    def test_c_file_scope_declaration_is_global(self):
        import mcp_server
        source = b"int global_x = 5;\nstatic int file_static_x = 10;\n"
        tree = self._c_parser().parse(source)
        result = mcp_server._extract_c_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"global_x", "file_static_x"}

    def test_c_struct_field_is_instance_only(self):
        import mcp_server
        source = b"struct Foo {\n    int instance_field;\n};\n"
        tree = self._c_parser().parse(source)
        result = mcp_server._extract_c_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instance_field"] == ("Foo", False)

    def test_c_multi_declarator_statement_captures_all_names(self):
        # `int a, b = 2;` puts more than one node under the `declarator`
        # field of a single `declaration` node -- child_by_field_name
        # (singular) only returns the first one, silently dropping `b`.
        # Verified empirically against the real installed tree-sitter-c
        # grammar; children_by_field_name (plural) must be used instead.
        import mcp_server
        source = b"int a, b = 2;\n"
        tree = self._c_parser().parse(source)
        result = mcp_server._extract_c_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"a", "b"}

    def test_c_multi_declarator_struct_field_captures_all_names(self):
        import mcp_server
        source = b"struct Foo {\n    int a, b;\n};\n"
        tree = self._c_parser().parse(source)
        result = mcp_server._extract_c_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", False)
        assert info["b"] == ("Foo", False)


class TestJavaCSharpGlobalsAndFields:
    def _java_parser(self):
        import tree_sitter_java
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_java.language()))

    def _csharp_parser(self):
        import tree_sitter_c_sharp
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_c_sharp.language()))

    def test_java_static_and_instance_fields(self):
        import mcp_server
        source = b"public class Foo {\n    static int staticField = 1;\n    int instanceField = 2;\n}\n"
        tree = self._java_parser().parse(source)
        result = mcp_server._extract_java_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
        assert result["globals"] == []

    def test_csharp_static_and_instance_fields(self):
        import mcp_server
        source = b"public class Foo {\n    static int staticField = 1;\n    int instanceField = 2;\n}\n"
        tree = self._csharp_parser().parse(source)
        result = mcp_server._extract_csharp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
        assert result["globals"] == []

    def test_java_multi_declarator_field_captures_all_names(self):
        # `int a, b;` puts more than one node under the `declarator` field
        # of a single field_declaration -- child_by_field_name (singular)
        # only returns the first, silently dropping `b`. Verified
        # empirically against the real installed tree-sitter-java grammar
        # (same lesson as Go's multi-name struct field and C's
        # multi-declarator statement); children_by_field_name (plural)
        # must be used instead.
        import mcp_server
        source = b"public class Foo {\n    int a, b = 3;\n}\n"
        tree = self._java_parser().parse(source)
        result = mcp_server._extract_java_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", False)
        assert info["b"] == ("Foo", False)

    def test_csharp_multi_declarator_field_captures_all_names(self):
        # Unlike Java, C#'s field_declaration wraps a single
        # variable_declaration child whose variable_declarator children
        # (for `int a, b;`) are all plain positional children -- iterating
        # var_decl.children already captures all of them. This test locks
        # in that a multi-name field isn't silently dropped.
        import mcp_server
        source = b"public class Foo {\n    int a, b = 3;\n}\n"
        tree = self._csharp_parser().parse(source)
        result = mcp_server._extract_csharp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", False)
        assert info["b"] == ("Foo", False)

    def test_java_nested_class_fields_captured(self):
        # walk() recurses into every node (not just direct root children)
        # to find nested class_declarations, since a nested/inner class is
        # a real, valid Java construct. This locks in that its fields are
        # attributed to the inner class's own name, not the outer one, and
        # that the method body sibling isn't mistakenly walked for fields.
        import mcp_server
        source = (
            b"public class Outer {\n"
            b"    class Inner {\n"
            b"        static int innerStatic = 5;\n"
            b"    }\n"
            b"    void m() {\n"
            b"        int local = 1;\n"
            b"    }\n"
            b"}\n"
        )
        tree = self._java_parser().parse(source)
        result = mcp_server._extract_java_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["innerStatic"] == ("Inner", True)
        assert "local" not in info
        assert result["globals"] == []

    def test_csharp_nested_class_fields_captured(self):
        import mcp_server
        source = (
            b"public class Outer {\n"
            b"    class Inner {\n"
            b"        static int innerStatic = 5;\n"
            b"    }\n"
            b"    void M() {\n"
            b"        int local = 1;\n"
            b"    }\n"
            b"}\n"
        )
        tree = self._csharp_parser().parse(source)
        result = mcp_server._extract_csharp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["innerStatic"] == ("Inner", True)
        assert "local" not in info
        assert result["globals"] == []


class TestCppGlobalsAndFields:
    def _cpp_parser(self):
        import tree_sitter_cpp
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_cpp.language()))

    def test_top_level_declaration_is_global(self):
        import mcp_server
        tree = self._cpp_parser().parse(b"int global_x = 5;\n")
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        assert "global_x" in result["globals"]

    def test_static_and_instance_class_fields(self):
        import mcp_server
        source = b"class Foo {\npublic:\n    static int staticField;\n    int instanceField;\n};\n"
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_struct_fields_same_shape_as_class(self):
        # struct_specifier and class_specifier expose field:body /
        # field_declaration_list identically -- verified empirically
        # against the real installed tree-sitter-cpp grammar. Structs
        # default to public visibility but the AST shape for extracting
        # fields is the same either way.
        import mcp_server
        source = b"struct Bar {\n    int a;\n    static float f;\n};\n"
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Bar", False)
        assert info["f"] == ("Bar", True)

    def test_multiple_access_specifier_sections_do_not_affect_extraction(self):
        # A class with several public:/private:/public: sections in
        # sequence -- access_specifier nodes are just siblings in the
        # field_declaration_list body and must not disrupt iteration over
        # the field_declaration members that follow them.
        import mcp_server
        source = (
            b"class Foo {\n"
            b"public:\n"
            b"    int pub1;\n"
            b"private:\n"
            b"    static int priv1;\n"
            b"public:\n"
            b"    int pub2;\n"
            b"};\n"
        )
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["pub1"] == ("Foo", False)
        assert info["priv1"] == ("Foo", True)
        assert info["pub2"] == ("Foo", False)

    def test_multi_declarator_global_captures_all_names(self):
        # `int a, b;` at file scope puts more than one node under the
        # `declarator` field of a single declaration -- child_by_field_name
        # (singular) only returns the first, silently dropping `b`.
        # Verified empirically against the real installed tree-sitter-cpp
        # grammar (same lesson as C/Java's multi-declarator statement);
        # children_by_field_name (plural) must be used instead.
        import mcp_server
        source = b"int a, b;\n"
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        assert "a" in result["globals"]
        assert "b" in result["globals"]

    def test_multi_declarator_field_captures_all_names(self):
        # Same multi-declarator gap as globals, but for class/struct
        # fields: `int x, y;` inside a field_declaration_list.
        import mcp_server
        source = b"class Foo {\npublic:\n    int x, y;\n};\n"
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["x"] == ("Foo", False)
        assert info["y"] == ("Foo", False)

    def test_method_declaration_not_captured_as_field(self):
        # A method's declarator is a function_declarator wrapping a
        # field_identifier, not a bare field_identifier -- must not be
        # misclassified as a field.
        import mcp_server
        source = b"class Foo {\npublic:\n    void method();\n};\n"
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        names = [n for n, _, _ in result["fields"]]
        assert "method" not in names


class TestRubyGlobalsAndFields:
    def _parser(self):
        import tree_sitter_ruby
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_ruby.language()))

    def test_global_variable_and_constant_are_globals(self):
        import mcp_server
        source = b"$global_var = 5\nCONST_VAR = 10\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"$global_var", "CONST_VAR"}

    def test_class_variable_is_static_instance_variable_in_initialize_is_not(self):
        import mcp_server
        source = b"class Foo\n  @@class_var = 1\n  def initialize\n    @instance_var = 2\n  end\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["@@class_var"] == ("Foo", True)
        assert info["@instance_var"] == ("Foo", False)

    def test_multi_assignment_globals_captures_all_names(self):
        # `$a, $b = 1, 2` wraps the left side in a left_assignment_list
        # node instead of exposing a bare global_variable/constant
        # directly under field:left -- verified empirically against the
        # real installed tree-sitter-ruby grammar. Same lesson as the
        # multi-declarator gaps found in Go/C/Java/C++: must unwrap
        # left_assignment_list to avoid silently dropping every name but
        # the first.
        import mcp_server
        source = b"$a, $b = 1, 2\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"$a", "$b"}

    def test_multi_assignment_class_and_instance_variables_captures_all_names(self):
        import mcp_server
        source = b"class Foo\n  @@a, @@b = 1, 2\n  def initialize\n    @x, @y = 1, 2\n  end\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["@@a"] == ("Foo", True)
        assert info["@@b"] == ("Foo", True)
        assert info["@x"] == ("Foo", False)
        assert info["@y"] == ("Foo", False)

    def test_attr_accessor_call_not_captured_as_field(self):
        # attr_accessor/attr_reader/attr_writer are ordinary method calls
        # (node type `call`) in the grammar, not assignments -- verified
        # empirically. Out of scope for this task (which only looks at
        # `@x = ...` inside initialize); they must not be misclassified
        # as fields.
        import mcp_server
        source = b"class Foo\n  attr_accessor :bar\n  def initialize\n    @baz = 1\n  end\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        names = [n for n, _, _ in result["fields"]]
        assert "bar" not in names
        assert names == ["@baz"]

    def test_module_is_not_treated_as_class(self):
        # `module Foo ... end` parses as a `module` node, distinct from
        # `class` -- verified empirically. The brief scopes this
        # extractor to `class` specifically, so module bodies (which can
        # also hold constants/class variables) must not be walked into
        # for field extraction.
        import mcp_server
        source = b"module Baz\n  @@mod_var = 1\n  CONST_IN_MOD = 5\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        assert result["fields"] == []
        assert result["globals"] == []


class TestPhpGlobalsAndFields:
    def _parser(self):
        import tree_sitter_php
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_php.language_php()))

    def test_top_level_variable_is_global(self):
        import mcp_server
        source = b"<?php\n$globalVar = 5;\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        assert "$globalVar" in result["globals"]

    def test_static_and_instance_properties(self):
        import mcp_server
        source = b"<?php\nclass Foo {\n    public static $staticField = 1;\n    public $instanceField = 2;\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["$staticField"] == ("Foo", True)
        assert info["$instanceField"] == ("Foo", False)

    def test_multi_property_declaration_captures_all_names(self):
        # `public static $a = 1, $b = 2;` -- a single property_declaration
        # containing multiple property_element children -- verified
        # empirically against the real installed tree-sitter-php grammar.
        # This is the PHP analog of the recurring multi-declarator gap
        # (Go/Java/C++), but here the brief's plain iteration over every
        # property_element child already handles it correctly.
        import mcp_server
        source = b"<?php\nclass Foo {\n    public static $a = 1, $b = 2;\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["$a"] == ("Foo", True)
        assert info["$b"] == ("Foo", True)

    def test_typed_property_still_captured(self):
        # PHP 7.4+ typed properties (`public int $x = 5;`) add a
        # primitive_type/named_type child to property_declaration but
        # keep the same property_element shape -- verified empirically.
        import mcp_server
        source = b"<?php\nclass Foo {\n    public int $x = 5;\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["$x"] == ("Foo", False)

    def test_semicolon_style_namespace_globals_still_captured(self):
        # `namespace App;` (semicolon style) does not wrap subsequent
        # statements -- they remain direct children of `program` --
        # verified empirically. No special-casing needed for this form.
        import mcp_server
        source = b"<?php\nnamespace App;\n$x = 5;\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        assert "$x" in result["globals"]

    def test_block_style_namespace_globals_and_classes_captured(self):
        # `namespace App { ... }` (block style) wraps its statements in
        # a compound_statement exposed via field:body on
        # namespace_definition -- verified empirically against the real
        # installed tree-sitter-php grammar. Same shape-changing-wrapper
        # lesson as JS's export_statement: without unwrapping, every
        # global/class inside a block-style namespace is silently
        # dropped, and PHP namespaces are extremely common in real code.
        import mcp_server
        source = (
            b"<?php\nnamespace App {\n"
            b"    $x = 5;\n"
            b"    class Foo {\n        public $y = 1;\n    }\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        assert "$x" in result["globals"]
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["$y"] == ("Foo", False)

    def test_promoted_constructor_property_captured_as_instance_field(self):
        # PHP 8+ constructor property promotion
        # (`public function __construct(public int $x) {}`) produces a
        # property_promotion_parameter node inside the constructor's
        # formal_parameters -- NOT a property_declaration under the
        # class body -- verified empirically. The brief's code as
        # written only scans property_declaration nodes in the class
        # body, so promoted properties would be silently missed without
        # this extra handling. Promoted properties cannot carry a
        # `static` modifier in real PHP (verified: adding one produces
        # a parse ERROR node), so they are always instance fields.
        import mcp_server
        source = (
            b"<?php\nclass Foo {\n"
            b"    public function __construct(public int $x, private string $y = \"z\") {}\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["$x"] == ("Foo", False)
        assert info["$y"] == ("Foo", False)


class TestKotlinGlobalsAndFields:
    def _parser(self):
        import tree_sitter_kotlin
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_kotlin.language()))

    def test_top_level_property_is_global(self):
        import mcp_server
        tree = self._parser().parse(b"val globalX = 5\n")
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_companion_object_property_is_static_plain_is_instance(self):
        import mcp_server
        source = b"class Foo {\n    companion object {\n        val staticField = 1\n    }\n    val instanceField = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_top_level_destructuring_declaration_captures_all_names(self):
        # `val (a, b) = Pair(1, 2)` wraps its names in a
        # multi_variable_declaration instead of a bare
        # variable_declaration -- verified empirically against the real
        # installed tree-sitter-kotlin grammar. Same multi-declarator
        # lesson as every prior language in this plan (Go/C/Java/C++/
        # Ruby): every identifier inside it must be extracted, not just
        # treated as absent.
        import mcp_server
        source = b"val (a, b) = Pair(1, 2)\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        assert "a" in result["globals"]
        assert "b" in result["globals"]

    def test_primary_constructor_val_param_is_instance_field_plain_param_excluded(self):
        # Kotlin's primary-constructor property shorthand (`class
        # Foo(val x: Int)`) is an idiomatic and extremely common way to
        # declare instance fields (near-universal in `data class`), but
        # it is structurally a class_parameter inside primary_
        # constructor's class_parameters list -- NOT a property_
        # declaration under class_body -- verified empirically. A plain
        # constructor parameter with neither `val` nor `var` (`y: Int`
        # below) is not a property and must be excluded.
        import mcp_server
        source = b"class Foo(val x: Int, y: Int) {\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["x"] == ("Foo", False)
        assert "y" not in info

    def test_data_class_constructor_properties_and_body_property_combined(self):
        import mcp_server
        source = (
            b"data class Point(val x: Int, val y: Int) {\n"
            b"    val label = \"point\"\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["x"] == ("Point", False)
        assert info["y"] == ("Point", False)
        assert info["label"] == ("Point", False)

    def test_nested_class_not_recursed_into(self):
        # Fields two classes deep are out of scope, consistent with
        # every other language in this plan: only class_body's direct
        # children are inspected, so a nested class_declaration is
        # skipped without crashing and without contributing its own
        # field to the outer class.
        import mcp_server
        source = (
            b"class Outer {\n"
            b"    class Inner {\n"
            b"        val innerField = 3\n"
            b"    }\n"
            b"    val outerField = 4\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["outerField"] == ("Outer", False)
        assert "innerField" not in info

    def test_no_globals_or_fields_when_absent(self):
        import mcp_server
        source = b"fun main() {\n    println(\"hi\")\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["fields"] == []


class TestSwiftGlobalsAndFields:
    def _parser(self):
        import tree_sitter_swift
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_swift.language()))

    def test_top_level_let_is_global(self):
        import mcp_server
        tree = self._parser().parse(b"let globalX = 5\n")
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_static_and_instance_properties(self):
        import mcp_server
        source = b"class Foo {\n    static var staticField = 1\n    var instanceField = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_top_level_multi_binding_let_captures_all_names(self):
        # `let a = 1, b = 2` binds multiple names in one
        # property_declaration, each its own `field:name` -- verified
        # empirically against the real installed tree-sitter-swift
        # grammar. Same multi-declarator lesson as every prior language
        # in this plan: every name must be extracted, not just the
        # first.
        import mcp_server
        tree = self._parser().parse(b"let a = 1, b = 2\n")
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        assert "a" in result["globals"]
        assert "b" in result["globals"]

    def test_class_multi_binding_property_captures_all_names(self):
        import mcp_server
        source = b"class Foo {\n    let a = 1, b = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", False)
        assert info["b"] == ("Foo", False)

    def test_struct_and_enum_properties_extracted(self):
        # struct and enum declarations share the class_declaration node
        # type with class (distinguished only by declaration_kind), and
        # enum's body node is a differently-typed enum_class_body --
        # both verified empirically to work via child_by_field_name
        # ("body"), which is keyed by field name, not node type.
        import mcp_server
        source = (
            b"struct Bar {\n    var structField = 5\n}\n"
            b"enum E {\n    static let enumField = 1\n}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["structField"] == ("Bar", False)
        assert info["enumField"] == ("E", True)

    def test_tuple_destructuring_produces_no_globals(self):
        # `let (x, y) = (1, 2)` wraps names in a pattern whose nested
        # per-name patterns expose no bound_identifier field -- verified
        # empirically -- so it is safely skipped rather than crashing
        # or misattributing a name.
        import mcp_server
        tree = self._parser().parse(b"let (x, y) = (1, 2)\n")
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_extension_properties_attributed_to_extended_type(self):
        # extension Foo { ... } shares the class_declaration node type;
        # its `name` field is a user_type wrapping Foo's
        # type_identifier, and .text on it still resolves to plain
        # "Foo" -- verified empirically -- so extension properties are
        # picked up rather than silently dropped or misattributed.
        import mcp_server
        source = b"extension Foo {\n    var extField = 3\n    static var extStatic = 4\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["extField"] == ("Foo", False)
        assert info["extStatic"] == ("Foo", True)

    def test_init_only_class_has_no_fields(self):
        # Swift has no primary-constructor property shorthand: a class
        # with only an `init` method and no property_declaration
        # produces zero fields -- verified empirically.
        import mcp_server
        source = b"class Foo {\n    init() { self.x = 1 }\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        assert result["fields"] == []

    def test_nested_class_not_recursed_into(self):
        import mcp_server
        source = (
            b"class Outer {\n"
            b"    class Inner {\n"
            b"        var innerField = 3\n"
            b"    }\n"
            b"    var outerField = 4\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["outerField"] == ("Outer", False)
        assert "innerField" not in info

    def test_no_globals_or_fields_when_absent(self):
        import mcp_server
        source = b"func main() {\n    print(\"hi\")\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["fields"] == []


class TestScalaGlobalsAndFields:
    def _parser(self):
        import tree_sitter_scala
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_scala.language()))

    def test_top_level_object_members_are_globals(self):
        import mcp_server
        source = b"object Globals {\n  val globalX = 5\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_class_members_are_instance_fields(self):
        import mcp_server
        source = b"class Foo {\n  val instanceField = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instanceField"] == ("Foo", False)

    def test_tuple_destructuring_captures_all_names(self):
        # `val (a, b) = (1, 2)` puts a tuple_pattern in field:pattern
        # instead of a bare identifier -- verified empirically. Same
        # multi-declarator lesson as nearly every other language in
        # this plan: every identifier inside it must be extracted.
        import mcp_server
        source = b"object O {\n  val (a, b) = (1, 2)\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        assert "a" in result["globals"]
        assert "b" in result["globals"]

    def test_nested_tuple_destructuring_captures_all_names(self):
        import mcp_server
        source = b"object O {\n  val (a, (b, c)) = (1, (2, 3))\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        for name in ("a", "b", "c"):
            assert name in result["globals"]

    def test_comma_separated_multi_name_binding_captures_all_names(self):
        # `val a, b = 5` binds both names to the same value via an
        # "identifiers" node in field:pattern -- a structurally
        # DIFFERENT shape from tuple destructuring, verified
        # empirically. Both must be handled.
        import mcp_server
        source = b"object O {\n  val a, b = 5\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        assert "a" in result["globals"]
        assert "b" in result["globals"]

    def test_primary_constructor_val_param_is_instance_field_plain_param_excluded(self):
        # `class Foo(val x: Int, y: Int)` -- x is a val/var-marked
        # class_parameter (idiomatic, extremely common for case
        # classes); y has neither val nor var and is a plain
        # constructor parameter, excluded -- same shape as Kotlin's
        # analogous primary-constructor shorthand.
        import mcp_server
        source = b"class Foo(val x: Int, y: Int) {\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["x"] == ("Foo", False)
        assert "y" not in info

    def test_case_class_implicit_val_param_excluded(self):
        # Case class parameters without an explicit val/var keyword are
        # semantically public vals in real Scala, but recognizing that
        # requires keying off the `case` child -- separate semantic
        # inference outside the structural, by-keyword scope of this
        # extension -- so it is deliberately excluded, not extracted.
        import mcp_server
        source = b"case class Foo(x: Int, y: String)\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert "x" not in info
        assert "y" not in info

    def test_constructor_param_and_body_val_combined(self):
        import mcp_server
        source = (
            b"class Point(val x: Int, val y: Int) {\n"
            b"    val label = \"point\"\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["x"] == ("Point", False)
        assert info["y"] == ("Point", False)
        assert info["label"] == ("Point", False)

    def test_braced_package_block_is_unwrapped(self):
        # `package foo { ... }` wraps its contents in package_clause's
        # field:body -- the same shape-changing-wrapper hazard as JS's
        # export_statement and PHP's block-style namespace_definition
        # -- verified empirically. Without unwrapping, everything
        # inside would be silently dropped.
        import mcp_server
        source = (
            b"package foo {\n"
            b"  object Globals {\n"
            b"    val globalX = 5\n"
            b"  }\n"
            b"  class Foo {\n"
            b"    val instanceField = 2\n"
            b"  }\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert "globalX" in result["globals"]
        assert info["instanceField"] == ("Foo", False)

    def test_bare_package_clause_does_not_hide_siblings(self):
        # The bare `package foo` form (no braces) does NOT wrap
        # subsequent statements -- they remain direct siblings under
        # compilation_unit -- verified empirically, so no unwrapping
        # is needed (and none should be attempted) for this form.
        import mcp_server
        source = b"package foo\n\nobject Globals {\n  val globalX = 5\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_nested_object_inside_class_is_not_a_globals_namespace(self):
        # Deliberate simplification: only a TOP-LEVEL (or
        # package-wrapped top-level) object_definition is a globals
        # namespace. One nested inside a class's template_body is out
        # of scope -- it doesn't match the val/var_definition type
        # filter used there, so it is silently skipped, and no
        # companion-object pairing is ever attempted.
        import mcp_server
        source = (
            b"class Foo {\n"
            b"  object Inner {\n"
            b"    val innerX = 1\n"
            b"  }\n"
            b"  val instanceField = 2\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert "innerX" not in result["globals"]
        assert "innerX" not in info
        assert info["instanceField"] == ("Foo", False)

    def test_nested_class_not_recursed_into(self):
        import mcp_server
        source = (
            b"class Outer {\n"
            b"    class Inner {\n"
            b"        val innerField = 3\n"
            b"    }\n"
            b"    val outerField = 4\n"
            b"}\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["outerField"] == ("Outer", False)
        assert "innerField" not in info

    def test_no_globals_or_fields_when_absent(self):
        import mcp_server
        source = b"def main(): Unit = {\n  println(\"hi\")\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["fields"] == []


class TestHaskellGlobalsAndFields:
    def _parser(self):
        import tree_sitter_haskell
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_haskell.language()))

    def test_zero_arg_bind_is_global(self):
        import mcp_server
        source = b"globalX :: Int\nglobalX = 5\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_record_fields_extracted(self):
        import mcp_server
        source = b"data Foo = Foo { fieldA :: Int, fieldB :: String }\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["fieldA"] == ("Foo", False)
        assert info["fieldB"] == ("Foo", False)

    def test_parameterized_function_is_not_a_global(self):
        # A parameterized top-level declaration is a "function" node
        # (targeted separately by _LANG_NODE_TYPES["haskell"]["functions"]),
        # structurally distinct from a zero-argument "bind" node -- it
        # must NOT also be picked up here as a global value.
        import mcp_server
        source = b"double :: Int -> Int\ndouble x = x * 2\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert "double" not in result["globals"]

    def test_module_header_does_not_hide_declarations(self):
        # `module Foo where` produces a sibling "header" field on the
        # root node -- verified empirically -- it does NOT wrap
        # declarations the way JS's export_statement or PHP's
        # block-style namespace_definition do elsewhere in this plan.
        # field:declarations still holds them directly.
        import mcp_server
        source = b"module Foo where\n\nglobalX :: Int\nglobalX = 5\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_tuple_pattern_bind_excluded(self):
        # `(a, b) = (1, 2)` puts a "tuple" node in field:pattern instead
        # of field:name holding a "variable" -- verified empirically.
        # Deliberate simplification (not a bug): only simple
        # single-name binds are recognized as globals here; destructured
        # binds are silently skipped.
        import mcp_server
        source = b"(a, b) = (1, 2)\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_where_clause_local_binds_not_treated_as_globals(self):
        # local_binds under a function's `where` clause are nested
        # inside the function node's body, not direct children of
        # field:declarations -- verified empirically -- so they are
        # correctly excluded without any special-casing.
        import mcp_server
        source = b"f x = y + z\n  where\n    y = 1\n    z = 2\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_multiple_record_constructors_each_extracted(self):
        # `data Shape = Circle {..} | Rectangle {..} | Point` -- each
        # data_constructor within data_constructors needs its own
        # record-shape check; a non-record constructor (Point, a
        # "prefix" node) mixed in must not break extraction of the
        # record ones -- verified empirically.
        import mcp_server
        source = (
            b"data Shape = Circle { radius :: Double } "
            b"| Rectangle { width :: Double, height :: Double } "
            b"| Point\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["radius"] == ("Shape", False)
        assert info["width"] == ("Shape", False)
        assert info["height"] == ("Shape", False)
        assert "Point" not in info

    def test_newtype_record_field_extracted(self):
        # `newtype Foo = Foo { unFoo :: Int }` is a completely different
        # top-level node type ("newtype", not "data_type") -- verified
        # empirically. Its constructor is a "newtype_constructor" node
        # whose record child is reached via field:field (a confusingly
        # named but real field name in the grammar) directly holding the
        # "record" node -- there is no intermediate data_constructor
        # wrapper the way data_type has. A newtype's single field is a
        # very common real Haskell pattern and must be extracted.
        import mcp_server
        source = b"newtype Foo = Foo { unFoo :: Int }\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["unFoo"] == ("Foo", False)

    def test_newtype_without_record_yields_no_fields(self):
        # `newtype Foo = Foo Int` (no braces) puts a plain "field" node
        # (not "record") at field:field -- verified empirically -- must
        # not be misidentified as a record field.
        import mcp_server
        source = b"newtype Foo = Foo Int\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert result["fields"] == []

    def test_no_globals_or_fields_when_absent(self):
        import mcp_server
        source = b"double :: Int -> Int\ndouble x = x * 2\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["fields"] == []


class TestLuaGlobalsAndFields:
    def _parser(self):
        import tree_sitter_lua
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_lua.language()))

    def test_true_global_assignment(self):
        import mcp_server
        tree = self._parser().parse(b"globalX = 5\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_local_declaration_not_captured(self):
        import mcp_server
        tree = self._parser().parse(b"local localY = 10\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_table_field_assignment_not_captured(self):
        import mcp_server
        tree = self._parser().parse(b"Foo = {}\nFoo.staticField = 1\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == ["Foo"]
        assert result["fields"] == []

    def test_multiple_assignment_captures_all_names(self):
        # `a, b = 1, 2` puts multiple identifier children under
        # variable_list, each exposed via field:name -- verified
        # empirically. child_by_field_name (singular) would silently
        # return only the first ("a"), matching the multi-assignment gap
        # that showed up in every other language in this plan (Ruby,
        # Kotlin, Swift, Scala) -- children_by_field_name (plural) is
        # required to capture all of them.
        import mcp_server
        tree = self._parser().parse(b"a, b = 1, 2\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert "a" in result["globals"]
        assert "b" in result["globals"]

    def test_mixed_identifier_and_table_field_in_multi_assignment(self):
        # `a, Foo.x = 1, 2` mixes a plain identifier with a
        # dot_index_expression in the same variable_list -- verified
        # empirically. Only the plain identifier is a true global; the
        # table-field write must still be excluded.
        import mcp_server
        tree = self._parser().parse(b"a, Foo.x = 1, 2\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == ["a"]

    def test_local_function_not_captured_as_global(self):
        # `local function foo() ... end` parses as a function_declaration
        # node (with a "local" child), never an assignment_statement --
        # verified empirically -- so it cannot be misidentified as a
        # global variable assignment. Functions are handled separately
        # by _LANG_NODE_TYPES["lua"]["functions"].
        import mcp_server
        tree = self._parser().parse(b"local function foo() end\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_top_level_function_not_captured_as_global(self):
        # `function foo() ... end` (no `local`) is also a
        # function_declaration node, not an assignment_statement --
        # verified empirically -- so it must not be captured here either.
        import mcp_server
        tree = self._parser().parse(b"function foo() end\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_no_globals_or_fields_when_absent(self):
        import mcp_server
        tree = self._parser().parse(b"function foo() end\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["fields"] == []


class TestElixirGlobalsAndFields:
    def _parser(self):
        import tree_sitter_elixir
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_elixir.language()))

    def test_module_attribute_is_static_field(self):
        import mcp_server
        source = b"defmodule Foo do\n  @module_attr 5\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["module_attr"] == ("Foo", True)
        assert result["globals"] == []

    def test_globals_always_empty(self):
        # Elixir has no top-level mutable globals outside module attributes --
        # verified empirically there is no syntactic form that would produce one.
        import mcp_server
        source = b"defmodule Foo do\n  @a 1\n  @b 2\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["global_bodies"] == {}

    def test_multiple_attributes_in_one_module(self):
        import mcp_server
        source = b"defmodule Foo do\n  @a 1\n  @b 2\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["a"] == ("Foo", True)
        assert info["b"] == ("Foo", True)

    def test_nested_module_attribute_attributed_to_inner_module(self):
        # A nested `defmodule` is itself a `call` node reachable by the
        # recursive walk -- verified empirically its own do_block members
        # are scanned independently of the outer module's, so an inner
        # module's attribute must be attributed to the inner module's name,
        # not the outer one.
        import mcp_server
        source = (
            b"defmodule Outer do\n"
            b"  @outer_attr 1\n\n"
            b"  defmodule Inner do\n"
            b"    @inner_attr 2\n"
            b"  end\n"
            b"end\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["outer_attr"] == ("Outer", True)
        assert info["inner_attr"] == ("Inner", True)
        assert result["globals"] == []

    def test_dotted_nested_module_name(self):
        # `defmodule Foo.Bar do ... end` is a single call whose alias node's
        # text is the full dotted name "Foo.Bar" -- verified empirically --
        # rather than two levels of AST nesting.
        import mcp_server
        source = b"defmodule Foo.Bar do\n  @attr 1\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["attr"] == ("Foo.Bar", True)

    def test_attribute_reference_without_value_not_captured_as_field(self):
        # `@attr` (no value, a read-reference to a previously defined
        # attribute) parses with operand type "identifier", not "call" --
        # verified empirically -- so it is correctly excluded here; only
        # `@attr value` (operand type "call") defines a field.
        import mcp_server
        source = (
            b"defmodule Foo do\n"
            b"  @attr 1\n"
            b"  def bar do\n"
            b"    @attr\n"
            b"  end\n"
            b"end\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["attr"] == ("Foo", True)
        assert len(result["fields"]) == 1

    def test_moduledoc_and_doc_attributes_are_captured_as_fields(self):
        # @moduledoc/@doc/@spec parse identically to any other module
        # attribute (unary_operator -> call with an identifier target) --
        # verified empirically -- so this extractor does not special-case
        # them as noise, consistent with no other language task in this
        # plan inventing bespoke exclusion lists for built-in
        # annotations/decorators. Documented here as a known characteristic,
        # not a bug.
        import mcp_server
        source = (
            b"defmodule Foo do\n"
            b'  @moduledoc "hi"\n'
            b"  @attr 1\n"
            b"end\n"
        )
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["moduledoc"] == ("Foo", True)
        assert info["attr"] == ("Foo", True)

    def test_no_globals_or_fields_when_absent(self):
        import mcp_server
        source = b"defmodule Foo do\n  def bar do\n    :ok\n  end\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        assert result["globals"] == []
        assert result["fields"] == []


class TestMatchCandidatePair:
    def _parse(self, source: str):
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        tree = parser.parse(source.encode())
        # first top-level statement's node (a function_definition, in every fixture below)
        return tree.root_node.children[0]

    def test_identical_bodies_match_with_empty_bijection(self):
        import mcp_server
        old = self._parse("def foo(x):\n    return x + 1\n")
        new = self._parse("def foo(x):\n    return x + 1\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result == {}

    def test_renamed_local_variable_matches_via_bijection(self):
        import mcp_server
        old = self._parse("def foo(x):\n    y = x + 1\n    return y\n")
        new = self._parse("def foo(x):\n    z = x + 1\n    return z\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result == {"y": "z"}

    def test_inconsistent_local_rename_does_not_match(self):
        """y is renamed to z in one spot but stays y in another -> not a valid bijection."""
        import mcp_server
        old = self._parse("def foo(x):\n    y = x + 1\n    return y + y\n")
        new = self._parse("def foo(x):\n    z = x + 1\n    return z + y\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result is None

    def test_two_distinct_locals_collapsing_onto_one_new_name_does_not_match(self):
        """y and w are two distinct old locals; if both map to the same new
        name 'z' that is not a valid bijection (not injective) even though
        each individual old->new mapping is internally consistent."""
        import mcp_server
        old = self._parse("def foo(x):\n    y = x + 1\n    w = x + 2\n    return y + w\n")
        new = self._parse("def foo(x):\n    z = x + 1\n    z = x + 2\n    return z + z\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result is None

    def test_tracked_entity_with_confirmed_rename_must_match_new_name(self):
        """Body calls a helper that was itself confirmed renamed this round."""
        import mcp_server
        old = self._parse("def foo(x):\n    return helper_old(x)\n")
        new = self._parse("def foo(x):\n    return helper_new(x)\n")
        result = mcp_server._match_candidate_pair(old, new, {"helper_old": "helper_new"})
        assert result == {}

    def test_tracked_entity_without_rename_must_match_exactly(self):
        old = self._parse("def foo(x):\n    return helper(x)\n")
        new = self._parse("def foo(x):\n    return other(x)\n")
        import mcp_server
        result = mcp_server._match_candidate_pair(old, new, {"helper": None})
        assert result is None

    def test_structurally_different_bodies_do_not_match(self):
        import mcp_server
        old = self._parse("def foo(x):\n    return x + 1\n")
        new = self._parse("def foo(x):\n    if x:\n        return x\n    return 1\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result is None

    def test_local_identifier_colliding_with_confirmed_tracked_rename_does_not_match(self):
        """A local (untracked) old identifier 'y' must not be allowed to map
        to 'helper_new' when 'helper_new' is already claimed as the confirmed
        new name of a different, tracked old entity 'helper_old'. Both old
        tokens are genuinely distinct, so collapsing them onto the same new
        token violates injectivity even though 'y' isn't itself tracked."""
        import mcp_server
        old = self._parse("def foo(x):\n    y = helper_old(x)\n    return y\n")
        new = self._parse(
            "def foo(x):\n    helper_new = helper_new(x)\n    return helper_new\n"
        )
        result = mcp_server._match_candidate_pair(old, new, {"helper_old": "helper_new"})
        assert result is None

    def test_local_identifier_colliding_with_unchanged_tracked_name_does_not_match(self):
        """Same collision, but the tracked entity is unchanged (tracked_names
        value is None) rather than renamed: a local old identifier must not
        be allowed to map to the tracked entity's own (unchanged) text."""
        import mcp_server
        old = self._parse("def foo(x):\n    y = helper(x)\n    return y\n")
        new = self._parse("def foo(x):\n    helper = helper(x)\n    return helper\n")
        result = mcp_server._match_candidate_pair(old, new, {"helper": None})
        assert result is None


class TestMatchRenamedEntities:
    def _parse_fn(self, source: str):
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        tree = parser.parse(source.encode())
        return tree.root_node.children[0]

    def test_simple_rename_matched(self):
        import mcp_server
        old = self._parse_fn("def foo(x):\n    return x + 1\n")
        new = self._parse_fn("def bar(x):\n    return x + 1\n")
        removed = {"function": [("foo", old)]}
        added = {"function": [("bar", new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        # 5-tuples now: (category, old_name, old_node, new_name, new_node) —
        # project down to the name-only shape and check node identity separately.
        assert len(matches) == 1
        category, old_name, old_node, new_name, new_node = matches[0]
        assert (category, old_name, new_name) == ("function", "foo", "bar")
        assert old_node is old
        assert new_node is new
        assert removed["function"] == []
        assert added["function"] == []

    def test_cascading_mutual_rename_resolves_across_rounds(self):
        """A calls B; both A and B are renamed in the same commit."""
        import mcp_server
        old_a = self._parse_fn("def a(x):\n    return b(x) + 1\n")
        old_b = self._parse_fn("def b(x):\n    return x * 2\n")
        new_a1 = self._parse_fn("def a1(x):\n    return b1(x) + 1\n")
        new_b1 = self._parse_fn("def b1(x):\n    return x * 2\n")
        removed = {"function": [("a", old_a), ("b", old_b)]}
        added = {"function": [("a1", new_a1), ("b1", new_b1)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        projected = [(category, old_name, new_name) for category, old_name, _, new_name, _ in matches]
        assert ("function", "a", "a1") in projected
        assert ("function", "b", "b1") in projected
        assert len(matches) == 2

    def test_ambiguous_duplicate_bodies_not_matched(self):
        import mcp_server
        old1 = self._parse_fn("def stub1():\n    pass\n")
        old2 = self._parse_fn("def stub2():\n    pass\n")
        new1 = self._parse_fn("def stub3():\n    pass\n")
        new2 = self._parse_fn("def stub4():\n    pass\n")
        removed = {"function": [("stub1", old1), ("stub2", old2)]}
        added = {"function": [("stub3", new1), ("stub4", new2)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []
        assert len(removed["function"]) == 2
        assert len(added["function"]) == 2

    def test_below_minimum_size_not_matched(self):
        import mcp_server
        old = self._parse_fn("def x():\n    pass\n")
        new = self._parse_fn("def y():\n    pass\n")
        removed = {"function": [("x", old)]}
        added = {"function": [("y", new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []

    def test_cross_category_no_match(self):
        """A function and a class with coincidentally-matchable text never match across categories."""
        import mcp_server
        old = self._parse_fn("def foo(x):\n    return x + 1\n")
        new = self._parse_fn("def bar(x):\n    return x + 1\n")
        removed = {"function": [("foo", old)], "class": []}
        added = {"function": [], "class": [("bar", new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []

    def test_field_leaf_not_captured_by_unrelated_confirmed_rename(self):
        """Regression for the task-26 false-positive: a field is pooled under a
        QUALIFIED key ("A.Config") but its body text contains only the BARE
        leaf token ("Config"). If an UNRELATED entity with the same bare name
        (a function `Config`) is confirmed-renamed to `ConfigFn` in an earlier
        round, the field's own `Config` token must NOT inherit that constraint
        — otherwise the field is wrongly forced to a specific candidate.

        Here the field rename is genuinely ambiguous (`A.Config`'s body
        `Config = <n>` matches BOTH `A.ConfigFn` and `A.ConfigField` as a
        single-token rename), so the matcher must stay conservative and leave
        the field UNMATCHED. Pre-fix, the stale `Config -> ConfigFn` constraint
        from the function disambiguated it into the WRONG match
        `A.Config -> A.ConfigFn`.
        """
        import mcp_server
        parser = mcp_server._get_parser("test.py")

        def parse(src):
            return parser.parse(src.encode()).root_node

        old_root = parse(
            "def Config(x):\n    return x + 1\n"
            "class A:\n    Config = 1234567890123\n"
        )
        new_root = parse(
            "def ConfigFn(x):\n    return x + 1\n"
            "class A:\n    ConfigFn = 1234567890123\n    ConfigField = 1234567890123\n"
        )

        old_fn = mcp_server._collect_entity_nodes(old_root, "python")["function"]
        new_fn = mcp_server._collect_entity_nodes(new_root, "python")["function"]
        old_fields = mcp_server._extract_globals_and_fields(old_root, "python")["field_nodes"]
        new_fields = mcp_server._extract_globals_and_fields(new_root, "python")["field_nodes"]

        removed = {
            "function": [("Config", old_fn["Config"])],
            "field": [("A.Config", old_fields["A.Config"])],
        }
        added = {
            "function": [("ConfigFn", new_fn["ConfigFn"])],
            "field": [
                ("A.ConfigFn", new_fields["A.ConfigFn"]),
                ("A.ConfigField", new_fields["A.ConfigField"]),
            ],
        }

        matches = mcp_server._match_renamed_entities(removed, added)
        projected = [(c, o, n) for c, o, _, n, _ in matches]

        # The unrelated function rename is legitimate and expected.
        assert ("function", "Config", "ConfigFn") in projected
        # The field rename is genuinely ambiguous -> must stay UNMATCHED.
        field_matches = [m for m in projected if m[0] == "field"]
        assert field_matches == [], f"field must stay ambiguous, got {field_matches}"
        assert ("field", "A.Config", "A.ConfigFn") not in projected

    def test_unchanged_tracked_helper_blocks_false_rename(self):
        """P1 (second-pass): an identifier referencing a still-present, unchanged
        tracked entity must match EXACTLY, not be treated as a free local that
        can be bijectively substituted.

        `load_users` (removed) calls unchanged helper `fetch_users`;
        `load_orders` (added) calls unchanged helper `fetch_orders`. Bodies are
        otherwise structurally identical. Without the unchanged-name constraint
        the matcher maps `fetch_users -> fetch_orders` as a free-local bijection
        and produces a FALSE rename `load_users -> load_orders`. Passing both
        helper names as unchanged (must-match-exactly) blocks it.
        """
        import mcp_server
        old = self._parse_fn(
            "def load_users(db):\n    rows = fetch_users(db)\n    return [r for r in rows]\n"
        )
        new = self._parse_fn(
            "def load_orders(db):\n    rows = fetch_orders(db)\n    return [r for r in rows]\n"
        )
        removed = {"function": [("load_users", old)]}
        added = {"function": [("load_orders", new)]}
        matches = mcp_server._match_renamed_entities(
            removed, added, unchanged_names={"fetch_users", "fetch_orders"}
        )
        assert matches == [], f"expected no match, got {matches}"

    def test_unchanged_helper_shared_by_genuine_rename_still_matches(self):
        """The unchanged-name constraint must not block a genuine rename: both
        the old and new body reference the SAME unchanged helper, so the exact
        match is satisfied and the rename is still confirmed."""
        import mcp_server
        old = self._parse_fn(
            "def process_old(db):\n    rows = fetch_data(db)\n    return [r for r in rows]\n"
        )
        new = self._parse_fn(
            "def process_new(db):\n    rows = fetch_data(db)\n    return [r for r in rows]\n"
        )
        removed = {"function": [("process_old", old)]}
        added = {"function": [("process_new", new)]}
        matches = mcp_server._match_renamed_entities(
            removed, added, unchanged_names={"fetch_data"}
        )
        projected = [(c, o, n) for c, o, _, n, _ in matches]
        assert projected == [("function", "process_old", "process_new")]

    def test_pathological_nesting_degrades_to_no_match_not_recursionerror(self):
        """P2 (second-pass): a pair whose AST is deep enough to survive
        _collect_entity_nodes but blow the recursion limit inside
        _match_candidate_pair's walk() must degrade to no-match for that pair,
        never raise RecursionError out of _match_renamed_entities (which would
        propagate out of _extract_commit and abort the whole ingestion run).

        Depth 600 was found empirically to pass collection while breaking the
        matcher walk pre-fix (300-400 pass both; 500+ break the matcher).
        """
        import mcp_server
        depth = 600
        nested = "(" * depth + "1" + ")" * depth
        old = self._parse_fn(f"def f(a):\n    x = {nested}\n    return x\n")
        new = self._parse_fn(f"def g(a):\n    x = {nested}\n    return x\n")
        # Sanity: collection survives this depth (so the nodes ARE pooled).
        old_root = mcp_server._get_parser("test.py").parse(
            f"def f(a):\n    x = {nested}\n    return x\n".encode()
        ).root_node
        assert "f" in mcp_server._collect_entity_nodes(old_root, "python")["function"]
        removed = {"function": [("f", old)]}
        added = {"function": [("g", new)]}
        # Must not raise; the pathological pair simply isn't matched.
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []

    def test_pathological_pair_does_not_discard_other_commit_matches(self):
        """Per-pair RecursionError isolation: a pathological pair in the pool
        must not prevent an unrelated, well-formed rename in the same pool from
        being confirmed."""
        import mcp_server
        depth = 600
        nested = "(" * depth + "1" + ")" * depth
        bad_old = self._parse_fn(f"def bad_old(a):\n    x = {nested}\n    return x\n")
        bad_new = self._parse_fn(f"def bad_new(a):\n    x = {nested}\n    return x\n")
        good_old = self._parse_fn("def foo(x):\n    return x + 1\n")
        good_new = self._parse_fn("def bar(x):\n    return x + 1\n")
        removed = {"function": [("bad_old", bad_old), ("foo", good_old)]}
        added = {"function": [("bad_new", bad_new), ("bar", good_new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        projected = [(c, o, n) for c, o, _, n, _ in matches]
        assert ("function", "foo", "bar") in projected

    def test_pool_size_cap_skips_matching(self, monkeypatch):
        """P2 (second-pass): above _MAX_MATCH_POOL_SIZE total entries the
        matcher skips entirely (git -M rename-limit style degradation) rather
        than paying the ~cubic pairwise cost on a giant vendored-dependency
        commit. A rename that WOULD match under a normal pool is left
        unmatched once the cap is exceeded."""
        import mcp_server
        old = self._parse_fn("def foo(x):\n    return x + 1\n")
        new = self._parse_fn("def bar(x):\n    return x + 1\n")
        removed = {"function": [("foo", old)]}
        added = {"function": [("bar", new)]}
        # Sanity: matches under the default cap.
        assert len(mcp_server._match_renamed_entities(
            {"function": [("foo", old)]}, {"function": [("bar", new)]}
        )) == 1
        monkeypatch.setattr(mcp_server, "_MAX_MATCH_POOL_SIZE", 1)
        assert mcp_server._match_renamed_entities(removed, added) == []
        # Pools are left untouched when matching is skipped.
        assert removed["function"] == [("foo", old)]
        assert added["function"] == [("bar", new)]

    def test_matcher_growth_is_bounded_not_cubic(self):
        """P2 (second-pass) performance regression guard. The per-pair
        O(all_names) dict rebuild (the cubic term) is gone; a 600x600 pool of
        realistic small functions must complete well within a generous bound.
        Pre-fix a 600x600 pool took ~32s (and grew ~cubically); post-fix it is
        ~5s. The 12s bound is chosen to sit cleanly between the two — loose
        enough to stay non-flaky on slower CI, tight enough that a regression
        back to the cubic rebuild (which would blow past ~30s) fails here."""
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        n = 600
        removed = {"function": [], "class": [], "variable": [], "field": []}
        added = {"function": [], "class": [], "variable": [], "field": []}
        for i in range(n):
            src_o = f"def old_{i}(a, b):\n    total = a + b + {i}\n    return total * a\n"
            src_n = f"def new_{i}(a, b):\n    total = a + b + {i}\n    return total * a\n"
            removed["function"].append((f"old_{i}", parser.parse(src_o.encode()).root_node.children[0]))
            added["function"].append((f"new_{i}", parser.parse(src_n.encode()).root_node.children[0]))
        start = time.perf_counter()
        matches = mcp_server._match_renamed_entities(removed, added)
        elapsed = time.perf_counter() - start
        assert len(matches) == n
        assert elapsed < 12.0, f"600x600 matcher took {elapsed:.2f}s (expected ~5s; cubic regression?)"


class TestCollectEntityNodes:
    def test_collects_function_and_class_nodes_by_name(self):
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        source = b"def foo():\n    pass\n\nclass Bar:\n    pass\n"
        tree = parser.parse(source)
        result = mcp_server._collect_entity_nodes(tree.root_node, "python")
        assert "foo" in result["function"]
        assert result["function"]["foo"].type == "function_definition"
        assert "Bar" in result["class"]
        assert result["class"]["Bar"].type == "class_definition"


class TestExtractFromSourceCFamily:
    """Regression tests for issue #92 bug 1: the generic `name` field lookup
    in _walk_ast doesn't match the C/C++ grammar for functions — a C/C++
    function_definition has no direct `name` field, so every function was
    silently skipped. Also covers bug 2 (header extensions) end-to-end."""

    def _c_parser(self):
        import mcp_server
        import tree_sitter
        import tree_sitter_c
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(tree_sitter_c.language())
        real_parser = tree_sitter.Parser(real_lang)
        mcp_server._grammar_cache["c"] = real_parser
        return real_parser

    def _cpp_parser(self):
        import mcp_server
        import tree_sitter
        import tree_sitter_cpp
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(tree_sitter_cpp.language())
        real_parser = tree_sitter.Parser(real_lang)
        mcp_server._grammar_cache["cpp"] = real_parser
        return real_parser

    def test_c_extracts_function_names(self):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b"int AddNewElement(int a, int b) {\n    return a + b;\n}\n"
        result = mcp_server._extract_from_source(source, self._c_parser(), "associative.c")
        assert result["functions"] == ["AddNewElement"]

    def test_c_pointer_return_function(self):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b"char* makeStr(void) {\n    return 0;\n}\n"
        result = mcp_server._extract_from_source(source, self._c_parser(), "foo.c")
        assert result["functions"] == ["makeStr"]

    def test_c_header_extension_extracts_function_names(self):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b"int helper(void) {\n    return 0;\n}\n"
        result = mcp_server._extract_from_source(source, self._c_parser(), "associative.h")
        assert result["functions"] == ["helper"]

    def test_cpp_extracts_method_names(self):
        pytest.importorskip("tree_sitter_cpp")
        import mcp_server
        source = b"class Foo {\npublic:\n    int bar(int x) { return x; }\n};\n"
        result = mcp_server._extract_from_source(source, self._cpp_parser(), "foo.cpp")
        assert result["functions"] == ["bar"]

    def test_cpp_qualified_out_of_line_definition(self):
        pytest.importorskip("tree_sitter_cpp")
        import mcp_server
        source = b"int Foo::baz() { return 0; }\n"
        result = mcp_server._extract_from_source(source, self._cpp_parser(), "foo.cpp")
        assert result["functions"] == ["baz"]

    def test_cpp_destructor_and_operator_overload(self):
        pytest.importorskip("tree_sitter_cpp")
        import mcp_server
        source = (
            b"class Foo {\npublic:\n"
            b"    ~Foo() {}\n"
            b"    Foo operator+(const Foo& o) { return *this; }\n"
            b"};\n"
        )
        result = mcp_server._extract_from_source(source, self._cpp_parser(), "foo.cpp")
        assert "~Foo" in result["functions"]
        assert "operator+" in result["functions"]

    def test_cpp_hpp_extension_extracts_function_names(self):
        pytest.importorskip("tree_sitter_cpp")
        import mcp_server
        source = b"inline int square(int x) { return x * x; }\n"
        result = mcp_server._extract_from_source(source, self._cpp_parser(), "foo.hpp")
        assert result["functions"] == ["square"]


class TestTsxParserLoading:
    """Regression test for the .tsx module-name bug: _build_parser assumed
    the importable module is always tree_sitter_{lang_name}, but tsx's grammar
    ships inside the tree_sitter_typescript package under language_tsx()."""

    def test_tsx_parser_builds_successfully(self):
        pytest.importorskip("tree_sitter_typescript")
        import mcp_server
        mcp_server._grammar_cache.clear()
        parser = mcp_server._get_parser("component.tsx")
        assert parser is not None

    def test_tsx_extracts_functions_classes_imports(self):
        pytest.importorskip("tree_sitter_typescript")
        import mcp_server
        mcp_server._grammar_cache.clear()
        source = (
            b"import React from 'react';\n"
            b"class Widget extends React.Component {\n"
            b"  render() { return null; }\n"
            b"}\n"
            b"function useThing() { return 1; }\n"
        )
        parser = mcp_server._get_parser("component.tsx")
        result = mcp_server._extract_from_source(source, parser, "component.tsx")
        assert "Widget" in result["classes"]
        assert "useThing" in result["functions"] or "render" in result["functions"]
        assert "react" in result["imports"]


class TestMinigrafIngestStatus:
    def test_returns_idle_before_ingestion(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["ok"] is True
        assert result["status"] == "idle"
        assert result["processed"] == 0
        assert result["last_run_at"] is None
        assert result["last_commit"] is None
        assert result["total_ingested"] is None

    def test_returns_last_run_at_from_graph(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        real_db.execute(
            '(transact {} [[:ingestion/last-run-at :entity-type :type/ingestion] '
            '[:ingestion/last-run-at :last-run-at "2026-05-27T10:00:00Z"] '
            '[:ingestion/last-run-at :last-commit "deadbeef"]])'
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
        # Must not query the graph while running
        assert calls == []

    def test_reports_owner_pid_when_skipped(self, real_db, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "skipped", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": 424242,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: True)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["status"] == "skipped"
        assert result["owner_pid"] == 424242
        assert result["stale"] is False

    def test_skipped_status_is_stale_when_owner_pid_dead(self, real_db, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "skipped", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": 424242,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: False)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["stale"] is True

    def test_error_status_reports_stale_when_holder_pid_dead(self, real_db, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "error", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "",
            "error": "Database is locked by another process (lock file: x.graph.lock, holder PID: 424242).",
            "owner_pid": None,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: False)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["stale"] is True

    def test_error_status_not_stale_when_holder_pid_alive(self, real_db, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "error", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "",
            "error": "Database is locked by another process (lock file: x.graph.lock, holder PID: 424242).",
            "owner_pid": None,
        }
        monkeypatch.setattr(mcp_server, "_pid_is_alive", lambda pid: True)
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["stale"] is False

    def test_error_status_omits_stale_when_no_pid_in_message(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "error", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": "corrupt graph file", "owner_pid": None,
        }
        result = mcp_server.handle_minigraf_ingest_status()
        assert "stale" not in result

    def test_returns_total_ingested_from_graph(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        real_db.execute(
            '(transact {} [[:ingestion/last-run-at :entity-type :type/ingestion] '
            '[:ingestion/last-run-at :last-run-at "2026-05-27T10:00:00Z"] '
            '[:ingestion/last-run-at :last-commit "deadbeef"]])'
        )
        # Batch all 1017 commit entities into a single transact call (one
        # round-trip) rather than looping individual execute() calls — the
        # count query only cares that 1017 :type/commit entities exist, not
        # how many transacts it took to write them.
        n = 1017
        triples = " ".join(f"[:commit/c{i} :entity-type :type/commit]" for i in range(n))
        real_db.execute(f"(transact {{}} [{triples}])")

        result = mcp_server.handle_minigraf_ingest_status()

        assert result["total_ingested"] == 1017

    def test_total_ingested_reflects_true_persisted_count_not_stale_watermark(
        self, real_db
    ):
        """Regression test for #85: total_ingested must come from a direct
        :type/commit entity count, not the :total-ingested watermark — the
        watermark is only written on clean run completion, so after a run is
        interrupted mid-way it drifts far below the true persisted count.

        A count of 50 (vs. a stale watermark of 4) is representative enough:
        the test only needs stale-watermark-count != real-count, not any
        specific magnitude, so it doesn't need to replay the original mock's
        arbitrary 21715."""
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        real_db.execute(
            '(transact {} [[:ingestion/last-run-at :entity-type :type/ingestion] '
            '[:ingestion/last-run-at :last-run-at "2026-05-27T10:00:00Z"] '
            '[:ingestion/last-run-at :last-commit "deadbeef"] '
            # Stale watermark from the last *completed* run — far below reality.
            '[:ingestion/last-run-at :total-ingested 4]])'
        )
        n = 50
        triples = " ".join(f"[:commit/c{i} :entity-type :type/commit]" for i in range(n))
        real_db.execute(f"(transact {{}} [{triples}])")

        result = mcp_server.handle_minigraf_ingest_status()

        # True count of durably persisted commit entities, not the stale watermark.
        assert result["total_ingested"] == 50

    def test_total_ingested_absent_returns_none(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["total_ingested"] is None


class TestCodeIdent:
    def test_module_ident_from_path(self):
        import mcp_server
        assert mcp_server._code_ident("module", "src/auth.py") == ":module/src-auth-py"

    def test_function_ident_distinct_from_module(self):
        import mcp_server
        # Same entity_type — separator must place tokens in a different order
        fn_ident = mcp_server._code_ident("function", "src/auth.py", "login")
        # "src/auth.py::login" → src-auth-py-login
        # "src/auth_login.py"  → src-auth-login-py  (py comes last, not before login)
        file_ident = mcp_server._code_ident("function", "src/auth_login.py")
        assert fn_ident == ":function/src-auth-py-login"
        assert file_ident == ":function/src-auth-login-py"
        assert fn_ident != file_ident

    def test_class_ident(self):
        import mcp_server
        assert mcp_server._code_ident("class", "src/auth.py", "User") == ":class/src-auth-py-user"

    def test_name_is_lowercased(self):
        import mcp_server
        assert mcp_server._code_ident("function", "Foo.py", "MyFunc") == ":function/foo-py-myfunc"


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with two commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    # Commit 1
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)

    # Commit 2
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)

    return repo


class TestGitHelpers:
    def test_git_commits_full_history(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        assert len(commits) == 2
        hash_, ts_iso, author, subject = commits[0]
        assert len(hash_) == 40
        assert ts_iso.endswith("Z")
        assert subject == "add auth"

    def test_git_commits_incremental(self, git_repo):
        import mcp_server
        all_commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = all_commits[0][0]
        incremental = mcp_server._git_commits(str(git_repo), watermark_hash=first_hash)
        assert len(incremental) == 1
        assert incremental[0][3] == "add models"

    def test_git_changed_files(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        second_hash = commits[1][0]
        changes = mcp_server._git_changed_files(str(git_repo), second_hash)
        assert ("A", "models.py") in changes

    def test_git_file_content(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]
        content = mcp_server._git_file_content(str(git_repo), first_hash, "auth.py")
        assert b"def login" in content

    def test_git_blob_content_returns_raw_bytes(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(git_repo), commits[0][0])
        _, _, _, _, new_sha, _ = entries[0][:6]
        content = mcp_server._git_blob_content(str(git_repo), new_sha)
        assert b"def login" in content


class TestDefaultGitBranch:
    def _init_repo_with_branch(self, tmp_path, branch_name):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", branch_name], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("x = 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        return repo

    def test_env_var_takes_precedence_over_auto_detect(self, tmp_path, monkeypatch):
        import mcp_server
        repo = self._init_repo_with_branch(tmp_path, "main")
        monkeypatch.setenv("MINIGRAF_GIT_BRANCH", "release")
        # "release" doesn't even exist as a branch here -- the env var is
        # trusted as-is, not validated against the repo.
        assert mcp_server._default_git_branch(str(repo)) == "release"

    def test_auto_detects_main_when_present(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_GIT_BRANCH", raising=False)
        repo = self._init_repo_with_branch(tmp_path, "main")
        assert mcp_server._default_git_branch(str(repo)) == "main"

    def test_auto_detects_master_when_no_main(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_GIT_BRANCH", raising=False)
        repo = self._init_repo_with_branch(tmp_path, "master")
        assert mcp_server._default_git_branch(str(repo)) == "master"

    def test_falls_back_to_head_when_neither_main_nor_master_exist(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_GIT_BRANCH", raising=False)
        repo = self._init_repo_with_branch(tmp_path, "trunk")
        assert mcp_server._default_git_branch(str(repo)) == "HEAD"


class TestGitDiffTreeRaw:
    def test_regular_file_add(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(git_repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "A"
        assert old_mode == "000000"
        assert new_mode == "100644"
        assert path == "auth.py"
        assert old_path == ""
        assert similarity is None

    def test_gitlink_add_reports_mode_160000(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

        sub = tmp_path / "sub"
        sub.mkdir()
        _subprocess.run(["git", "init"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "--allow-empty", "-m", "e"], cwd=sub, check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=sub, check=True, capture_output=True, text=True,
        ).stdout.strip()

        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},vendor/lib"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "A"
        assert new_mode == "160000"
        assert new_sha == sub_hash
        assert path == "vendor/lib"
        assert old_path == ""

    def test_pure_rename_reports_both_paths_and_similarity(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text("def login():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[1][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "R"
        assert path == "new_name.py"
        assert old_path == "old_name.py"
        assert similarity == 100

    def test_rename_with_content_change_reports_partial_similarity(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text(
            "def login():\n    pass\n\ndef a():\n    pass\n\ndef b():\n    pass\n\ndef c():\n    pass\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, capture_output=True)
        (repo / "new_name.py").write_text(
            "def login():\n    pass\n\ndef a():\n    pass\n\ndef extra():\n    pass\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename and edit"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[1][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "R"
        assert old_path == "old_name.py"
        assert path == "new_name.py"
        assert similarity is not None and 0 < similarity < 100

    def test_unrelated_add_and_delete_not_reported_as_rename(self, tmp_path):
        """Below git's default 50% similarity threshold, -M must NOT report a rename."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text("def login():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "rm", "old_name.py"], cwd=repo, check=True, capture_output=True)
        (repo / "unrelated.py").write_text("class Widget:\n    def render(self):\n        return 42\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "unrelated churn"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[1][0])
        statuses = {e[0] for e in entries}
        assert statuses == {"A", "D"}


class TestIsIgnoredPath:
    def test_directory_pattern_matches_nested_path(self):
        import mcp_server
        assert mcp_server._is_ignored_path("src/vendor/foo.js", ["vendor/"]) is True

    def test_directory_pattern_matches_top_level_path(self):
        import mcp_server
        assert mcp_server._is_ignored_path("vendor/bar.js", ["vendor/"]) is True

    def test_directory_pattern_does_not_match_substring(self):
        import mcp_server
        assert mcp_server._is_ignored_path("vendored_thing.js", ["vendor/"]) is False

    def test_glob_pattern_matches_basename(self):
        import mcp_server
        assert mcp_server._is_ignored_path("dist/app.min.js", ["*.min.js"]) is True

    def test_glob_pattern_no_match_on_unrelated_file(self):
        import mcp_server
        assert mcp_server._is_ignored_path("dist/app.js", ["*.min.js"]) is False

    def test_map_glob_pattern_matches(self):
        import mcp_server
        assert mcp_server._is_ignored_path("dist/app.js.map", ["*.map"]) is True

    def test_exact_segment_match(self):
        import mcp_server
        assert mcp_server._is_ignored_path("a/node_modules/pkg/index.js", ["node_modules"]) is True

    def test_exact_basename_match(self):
        import mcp_server
        assert mcp_server._is_ignored_path("some/path/README.md", ["README.md"]) is True

    def test_no_patterns_never_matches(self):
        import mcp_server
        assert mcp_server._is_ignored_path("src/main.py", []) is False

    def test_no_matching_pattern_returns_false(self):
        import mcp_server
        assert mcp_server._is_ignored_path("src/main.py", ["vendor/", "*.min.js"]) is False


class TestLoadIgnorePatterns:
    def test_defaults_present_with_no_env_or_file(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "vendor/" in patterns
        assert "third_party/" in patterns
        assert "3rdParty/" in patterns
        assert "node_modules/" in patterns
        assert "dist/" in patterns
        assert "build/" in patterns
        assert "*.min.js" in patterns
        assert "*.map" in patterns

    def test_env_var_patterns_are_appended(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_INGEST_IGNORE", "generated/,*.pb.go")
        repo = tmp_path / "repo"
        repo.mkdir()
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "generated/" in patterns
        assert "*.pb.go" in patterns
        assert "vendor/" in patterns  # defaults still present

    def test_temporalignore_file_patterns_are_merged(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".temporalignore").write_text(
            "# comment line\n\nlegacy/\n*.generated.ts\n"
        )
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "legacy/" in patterns
        assert "*.generated.ts" in patterns
        assert "vendor/" in patterns  # defaults still present
        assert "# comment line" not in patterns

    def test_missing_temporalignore_file_is_not_an_error(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert isinstance(patterns, list)
        assert len(patterns) > 0

    def test_all_three_sources_merge_together(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_INGEST_IGNORE", "from_env/")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".temporalignore").write_text("from_file/\n")
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "vendor/" in patterns       # default
        assert "from_env/" in patterns     # env var
        assert "from_file/" in patterns    # .temporalignore

    def test_unreadable_temporalignore_fails_closed(self, tmp_path, monkeypatch):
        """Unreadable .temporalignore should not abort ingestion; defaults + env patterns still apply."""
        import mcp_server
        from pathlib import Path

        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".temporalignore").write_text("from_file/\n")

        # Monkeypatch Path.read_text to raise OSError for our .temporalignore file
        original_read_text = Path.read_text

        def mock_read_text(self, encoding=None, errors=None):
            if ".temporalignore" in str(self):
                raise OSError("Permission denied")
            return original_read_text(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", mock_read_text)

        # Should not raise; should return defaults at minimum
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert isinstance(patterns, list)
        assert "vendor/" in patterns  # default should still be present
        assert len(patterns) > 0


class TestKnownFilesAtCommit:
    def test_returns_files_present_at_that_commit(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_commit_hash = commits[0][0]
        known = mcp_server._known_files_at_commit(str(git_repo), first_commit_hash)
        assert "auth.py" in known
        # models.py isn't added until the second commit
        assert "models.py" not in known

    def test_second_commit_sees_both_files(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        second_commit_hash = commits[1][0]
        known = mcp_server._known_files_at_commit(str(git_repo), second_commit_hash)
        assert "auth.py" in known
        assert "models.py" in known

    def test_filters_out_unsupported_extensions(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.py").write_text("def f(): pass\n")
        (repo / "README.md").write_text("hello\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(repo), commits[0][0])
        assert "main.py" in known
        assert "README.md" not in known

    def test_returned_dict_shape_matches_file_entities(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(git_repo), commits[0][0])
        assert known["auth.py"] == []

    def test_ignored_path_excluded_even_with_supported_extension(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.py").write_text("def f(): pass\n")
        (repo / "vendor").mkdir()
        (repo / "vendor" / "lib.py").write_text("def g(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(repo), commits[0][0], ["vendor/"])
        assert "main.py" in known
        assert "vendor/lib.py" not in known

    def test_no_ignore_patterns_keeps_default_behavior(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(git_repo), commits[0][0])
        assert "auth.py" in known


class TestGitlinkChanges:
    def test_non_gitlink_rows_are_ignored(self):
        import mcp_server
        raw = [("A", "000000", "100644", "0" * 40, "a" * 40, "auth.py", "", None)]
        assert mcp_server._gitlink_changes(raw) == []

    def test_add_when_new_mode_is_gitlink(self):
        import mcp_server
        raw = [("A", "000000", "160000", "0" * 40, "b" * 40, "vendor/lib", "", None)]
        assert mcp_server._gitlink_changes(raw) == [("add", "b" * 40, "vendor/lib")]

    def test_bump_when_both_modes_are_gitlink(self):
        import mcp_server
        raw = [("M", "160000", "160000", "b" * 40, "c" * 40, "vendor/lib", "", None)]
        assert mcp_server._gitlink_changes(raw) == [("bump", "c" * 40, "vendor/lib")]

    def test_remove_when_old_mode_is_gitlink(self):
        import mcp_server
        raw = [("D", "160000", "000000", "c" * 40, "0" * 40, "vendor/lib", "", None)]
        assert mcp_server._gitlink_changes(raw) == [("remove", "c" * 40, "vendor/lib")]

    def test_type_change_into_internal_reported_as_remove(self):
        import mcp_server
        raw = [("T", "160000", "100644", "c" * 40, "d" * 40, "vendor/lib", "", None)]
        assert mcp_server._gitlink_changes(raw) == [("remove", "c" * 40, "vendor/lib")]

    def test_type_change_into_external_reported_as_add(self):
        import mcp_server
        raw = [("T", "100644", "160000", "d" * 40, "e" * 40, "vendor/lib", "", None)]
        assert mcp_server._gitlink_changes(raw) == [("add", "e" * 40, "vendor/lib")]


class TestParseGitmodules:
    def test_parses_single_submodule(self):
        import mcp_server
        content = (
            b'[submodule "abseil-cpp"]\n'
            b'\tpath = 3rdParty/abseil-cpp\n'
            b'\turl = https://github.com/abseil/abseil-cpp.git\n'
        )
        result = mcp_server._parse_gitmodules(content)
        assert result == {
            "3rdParty/abseil-cpp": {
                "name": "abseil-cpp",
                "url": "https://github.com/abseil/abseil-cpp.git",
            }
        }

    def test_parses_multiple_submodules(self):
        import mcp_server
        content = (
            b'[submodule "a"]\n\tpath = vendor/a\n\turl = https://x/a.git\n'
            b'[submodule "b"]\n\tpath = vendor/b\n\turl = https://x/b.git\n'
        )
        result = mcp_server._parse_gitmodules(content)
        assert set(result.keys()) == {"vendor/a", "vendor/b"}

    def test_malformed_content_returns_empty_dict(self):
        import mcp_server
        result = mcp_server._parse_gitmodules(b"not a valid [ini file")
        assert result == {}

    def test_empty_content_returns_empty_dict(self):
        import mcp_server
        assert mcp_server._parse_gitmodules(b"") == {}

    def test_parses_url_containing_percent_sign(self):
        import mcp_server
        content = (
            b'[submodule "weird"]\n'
            b'\tpath = vendor/weird\n'
            b'\turl = https://example.com/repo%2Fname.git\n'
        )
        result = mcp_server._parse_gitmodules(content)
        assert result == {
            "vendor/weird": {
                "name": "weird",
                "url": "https://example.com/repo%2Fname.git",
            }
        }

    def test_git_gitmodules_at_missing_file_returns_empty(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        result = mcp_server._git_gitmodules_at(str(git_repo), commits[0][0])
        assert result == {}


class TestExtractCommit:
    def test_added_file_returns_extracted_dict(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]

        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(str(git_repo), first_hash)

        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "A"
        assert file_path == "auth.py"
        assert "login" in extracted["functions"]
        assert "resolved_imports" in precomputed
        assert "function_entries" in precomputed
        assert "class_entries" in precomputed
        assert old_path == ""
        assert gitlink_changes == []
        assert gitmodules_map == {}

    def test_deleted_file_has_none_extracted(self, git_repo_with_deletion):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_with_deletion), watermark_hash=None)
        delete_hash = commits[-1][0]

        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(str(git_repo_with_deletion), delete_hash)

        d_entries = [r for r in results if r[0] == "D"]
        assert len(d_entries) == 1
        assert d_entries[0][2] is None

    def test_unsupported_extension_is_omitted(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_diff_tree_raw",
            lambda repo, commit: [("A", "000000", "100644", "0" * 40, "a" * 40, "notes.txt", "", None)],
        )
        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []

    def test_content_fetch_failure_is_omitted_not_raised(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_diff_tree_raw",
            lambda repo, commit: [("A", "000000", "100644", "0" * 40, "a" * 40, "auth.py", "", None)],
        )

        def boom(repo, commit, path):
            raise mcp_server.MiniGrafError("simulated git-show failure")

        monkeypatch.setattr(mcp_server, "_git_file_content", boom)
        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []

    def test_gitlink_add_is_reported_separately_from_file_results(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

        sub = tmp_path / "sub"
        sub.mkdir()
        _subprocess.run(["git", "init"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "--allow-empty", "-m", "e"], cwd=sub, check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=sub, check=True, capture_output=True, text=True,
        ).stdout.strip()

        (repo / ".gitmodules").write_text(
            '[submodule "lib"]\n\tpath = vendor/lib\n\turl = https://example.com/lib.git\n'
        )
        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},vendor/lib"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "add", ".gitmodules"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(str(repo), commits[0][0])

        # Neither the gitlink path (vendor/lib) nor .gitmodules itself has a
        # resolvable extension (_EXT_TO_LANG has no entry for either), so
        # file_results is empty for this commit — the submodule info is
        # carried entirely by gitlink_changes/gitmodules_map instead.
        assert results == []
        assert gitlink_changes == [("add", sub_hash, "vendor/lib")]
        assert gitmodules_map == {"vendor/lib": {"name": "lib", "url": "https://example.com/lib.git"}}

    def test_segment_index_built_once_per_commit_not_per_file(self, git_repo_with_deps, monkeypatch):
        """#102: the tier 3a/3b index must be built once per commit and reused
        across every file's import resolution in that commit, not rebuilt per file."""
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_with_deps), watermark_hash=None)

        build_count = 0
        original_init = mcp_server._SegmentSuffixIndex.__init__

        def counting_init(self, *args, **kwargs):
            nonlocal build_count
            build_count += 1
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(mcp_server._SegmentSuffixIndex, "__init__", counting_init)
        results, _, _, _ = mcp_server._extract_commit(str(git_repo_with_deps), commits[0][0])

        assert len(results) == 2  # mod_a.py (imports mod_b) and mod_b.py both added here
        assert build_count == 1

    def test_ignored_file_produces_no_results_entry(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "lib.py").write_text("def vendored_fn(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add vendored lib"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(
            str(repo), commits[0][0], ["vendor/"]
        )
        assert results == []

    def test_no_ignore_patterns_keeps_default_behavior(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]
        results, gitlink_changes, gitmodules_map, _renamed_pairs = mcp_server._extract_commit(str(git_repo), first_hash)
        assert len(results) == 1
        assert results[0][1] == "auth.py"

    def test_new_side_pathological_nesting_does_not_abort_commit(self, tmp_path):
        """Reviewer finding 1 on Task 9 (47b962e): the new-side
        `_collect_entity_nodes` call in _extract_commit's per-file loop was
        NOT wrapped in a best-effort try/except, unlike the structurally
        identical old-side call a few lines above. A file whose body is
        pathologically deeply nested parses fine under tree-sitter (a C
        parser) but blows the Python recursion limit in
        _collect_entity_nodes's own recursive `walk()`, raising
        RecursionError. Uncaught, that propagates out of _extract_commit and
        (in the real pipeline) aborts the entire ingestion run rather than
        just this one commit — contradicting _extract_commit's own docstring
        promise that "ordinary exceptions... still fail only the one commit
        as before". This must degrade gracefully: the pathological file's
        new-side node pool ends up empty, matching just doesn't happen for
        it, but the commit still processes and _extract_commit still returns.
        """
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        nested = "(" * 6000 + "1" + ")" * 6000
        (repo / "deep.py").write_text(f"def f():\n    x = {nested}\n    return x\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add pathologically nested file"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)

        # Sanity check the reproduction still triggers RecursionError from
        # _collect_entity_nodes specifically (not _extract_from_source, which
        # is already wrapped) before asserting _extract_commit survives it.
        parser = mcp_server._get_parser("deep.py")
        content = mcp_server._git_file_content(str(repo), commits[0][0], "deep.py")
        tree = parser.parse(content)
        with pytest.raises(RecursionError):
            mcp_server._collect_entity_nodes(tree.root_node, "python")

        results, gitlink_changes, gitmodules_map, renamed_pairs = mcp_server._extract_commit(
            str(repo), commits[0][0]
        )
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "A"
        assert file_path == "deep.py"
        assert renamed_pairs == []

    def test_matcher_side_pathological_nesting_does_not_abort_commit(self, tmp_path):
        """P2 (second-pass): the `_match_renamed_entities` call in
        _extract_commit was outside any try/except, and
        _match_candidate_pair's walk uses more stack per AST level than
        _collect_entity_nodes. A file whose AST depth SURVIVES collection (so
        its nodes ARE pooled) can still blow the recursion limit inside the
        pair walk, raising RecursionError that escapes _extract_commit and
        aborts the whole ingestion run.

        Reproduced with an in-place function rename of a body whose parenthesis
        nesting (depth 600) passes collection on both sides but breaks the
        matcher walk pre-fix. The commit must still process and _extract_commit
        must still return, degrading to no confirmed rename for that pair.
        """
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        nested = "(" * 600 + "1" + ")" * 600
        (repo / "svc.py").write_text(f"def handler_old(a):\n    x = {nested}\n    return x\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)
        # Same file, function renamed in place (structurally identical body) ->
        # the deep node lands in BOTH removed and added pools for this "M"
        # commit, so the matcher walks it.
        (repo / "svc.py").write_text(f"def handler_new(a):\n    x = {nested}\n    return x\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "v2"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)

        # Sanity: collection survives on both sides (nodes ARE pooled) so the
        # failure, if any, is inside the matcher walk — not collection.
        parser = mcp_server._get_parser("svc.py")
        for h in (commits[0][0], commits[1][0]):
            content = mcp_server._git_file_content(str(repo), h, "svc.py")
            tree = parser.parse(content)
            mcp_server._collect_entity_nodes(tree.root_node, "python")  # must not raise

        # Must not raise RecursionError; commit processes, no confirmed rename.
        results, gitlink_changes, gitmodules_map, renamed_pairs = mcp_server._extract_commit(
            str(repo), commits[1][0]
        )
        assert len(results) == 1
        assert results[0][0] == "M"
        assert results[0][1] == "svc.py"
        assert renamed_pairs == []


class TestExtractCommitRename:
    def test_rename_status_extracts_new_path_and_tags_old_path(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text("def login():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(repo), commits[1][0])[:3]
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = mcp_server._extract_commit(str(repo), commits[1][0])[0][0]
        assert status == "R"
        assert file_path == "new_name.py"
        assert old_path == "old_name.py"
        assert "login" in extracted["functions"]

    def test_non_rename_status_has_empty_old_path(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        results = mcp_server._extract_commit(str(git_repo), commits[0][0])[0]
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "A"
        assert old_path == ""

    def test_cross_file_move_produces_renamed_pair(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "fileA.py").write_text(
            "def stayHere(x):\n    return x + 1\n\ndef moveMe(x):\n    return x * 2 + 7\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "fileA.py").write_text("def stayHere(x):\n    return x + 1\n")
        (repo / "fileB.py").write_text("def moveMe(x):\n    return x * 2 + 7\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "move function"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        _, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        assert ("function", "fileA.py", "moveMe", "fileB.py", "moveMe") in renamed_pairs

    def test_cross_extension_rename_parses_old_blob_with_old_grammar(self, tmp_path, monkeypatch):
        """Reviewer finding 2 on Task 9 (47b962e): for status "R", `parser =
        _thread_parser(file_path)` is selected using the NEW path's
        extension, then reused to parse `old_content` (the OLD blob) even
        when the old path's extension maps to a different language.
        `old_lang` is correctly computed from `old_lang_path` for the
        node-type lookup inside _collect_entity_nodes, but the Tree actually
        walked was built with the wrong grammar — silently losing/misparsing
        old-side structure on a genuine cross-language-extension rename.

        Renames old_name.cpp (real C++ requiring the C++ grammar: a
        destructor and an out-of-line qualified definition) to new_name.c.
        Under the bug, `parser` is built for the NEW path's extension (.c ->
        C grammar) and reused on the OLD (C++) blob; the C grammar cannot
        parse `Foo::baz(...)` / `~Foo()` correctly and the class vanishes
        entirely from the resulting tree (proven directly below), even
        though old_lang is still correctly computed as "cpp" for the
        node-type lookup.
        """
        import mcp_server
        pytest.importorskip("tree_sitter_c")
        pytest.importorskip("tree_sitter_cpp")
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        old_source = (
            "class Foo {\n"
            "public:\n"
            "    ~Foo() {}\n"
            "};\n"
            "int Foo::baz() { return 0; }\n"
        )
        (repo / "old_name.cpp").write_text(old_source)
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add cpp file"], cwd=repo, check=True, capture_output=True)
        # A pure rename (no content change) so git's rename detection reports
        # status "R" with 100% similarity rather than a delete+add pair —
        # the new file's content doesn't matter for what this test checks
        # (the OLD blob's grammar), only its extension does.
        _subprocess.run(["git", "mv", "old_name.cpp", "new_name.c"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename cpp to c"], cwd=repo, check=True, capture_output=True)

        # Sanity check: prove the wrong-grammar parse actually loses the
        # class, so this test would fail before the fix and isn't
        # vacuously true.
        mcp_server._grammar_cache.clear()
        c_parser = mcp_server._get_parser("new_name.c")
        cpp_parser = mcp_server._get_parser("old_name.cpp")
        wrong_tree = c_parser.parse(old_source.encode())
        correct_tree = cpp_parser.parse(old_source.encode())
        wrong_result = mcp_server._collect_entity_nodes(wrong_tree.root_node, "cpp")
        correct_result = mcp_server._collect_entity_nodes(correct_tree.root_node, "cpp")
        assert wrong_result["class"] == {}
        assert "Foo" in correct_result["class"]

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        rename_hash = commits[1][0]

        captured_old_side = []
        original_collect = mcp_server._collect_entity_nodes

        def spy(root_node, lang_name):
            result = original_collect(root_node, lang_name)
            if lang_name == "cpp":
                captured_old_side.append(result)
            return result

        monkeypatch.setattr(mcp_server, "_collect_entity_nodes", spy)

        results, gitlink_changes, gitmodules_map, renamed_pairs = mcp_server._extract_commit(
            str(repo), rename_hash
        )

        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "R"
        assert file_path == "new_name.c"
        assert old_path == "old_name.cpp"

        assert len(captured_old_side) == 1
        assert "Foo" in captured_old_side[0]["class"]
        assert "baz" in captured_old_side[0]["function"]
        assert "~Foo" in captured_old_side[0]["function"]

    def test_global_rename_produces_renamed_pair(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        # Value padded to a 13-digit literal (not the brief's bare "12345")
        # so the assignment's normalized body clears _match_renamed_entities'
        # _MIN_MATCH_BODY_LEN=20 floor (Task 8) -- confirmed empirically that
        # the brief's literal "GLOBAL_X = 12345" (16 normalized chars) is
        # silently dropped as a "trivial stub" before this padding was added,
        # producing an empty renamed_pairs regardless of the pooling/matcher
        # logic under test here.
        (repo / "config.py").write_text("GLOBAL_X = 1234567890123\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "config.py").write_text("GLOBAL_Y = 1234567890123\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename global"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        _, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        assert ("variable", "config.py", "GLOBAL_X", "config.py", "GLOBAL_Y") in renamed_pairs

    def test_unchanged_helpers_block_false_rename_in_modified_file(self, tmp_path):
        """P1 (second-pass) end-to-end repro: a modified file keeps two unchanged
        helpers `fetch_users`/`fetch_orders`; the commit deletes `load_users`
        (which calls `fetch_users`) and adds `load_orders` (which calls
        `fetch_orders`), bodies otherwise structurally identical. The unchanged
        helpers never appear in the removed/added pools, so pre-fix the matcher
        treated `fetch_users -> fetch_orders` as a free-local bijection and
        produced a FALSE `load_users -> load_orders` rename. Post-fix the
        unchanged names are seeded as must-match-exactly, so NO rename is
        produced.
        """
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "svc.py").write_text(
            "def fetch_users(db):\n    return db.query('users')\n\n"
            "def fetch_orders(db):\n    return db.query('orders')\n\n"
            "def load_users(db):\n    rows = fetch_users(db)\n    return [r for r in rows]\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "svc.py").write_text(
            "def fetch_users(db):\n    return db.query('users')\n\n"
            "def fetch_orders(db):\n    return db.query('orders')\n\n"
            "def load_orders(db):\n    rows = fetch_orders(db)\n    return [r for r in rows]\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "swap load fn"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        _, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        false_pairs = [
            p for p in renamed_pairs
            if p[0] == "function" and p[2] == "load_users" and p[4] == "load_orders"
        ]
        assert false_pairs == [], f"false rename produced: {false_pairs}"

    def test_genuine_rename_with_unchanged_helper_still_tracked(self, tmp_path):
        """Positive counterpart: a genuine same-file function rename whose body
        references an UNCHANGED helper must still be detected post-fix — the
        must-match-exactly constraint is satisfied because both bodies call the
        same surviving helper."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "svc.py").write_text(
            "def fetch_data(db):\n    return db.query('rows')\n\n"
            "def process_old(db):\n    rows = fetch_data(db)\n    return [r for r in rows]\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "svc.py").write_text(
            "def fetch_data(db):\n    return db.query('rows')\n\n"
            "def process_new(db):\n    rows = fetch_data(db)\n    return [r for r in rows]\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename process fn"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        _, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        assert ("function", "svc.py", "process_old", "svc.py", "process_new") in renamed_pairs

    def _init_repo(self, repo):
        import subprocess as _sp
        repo.mkdir()
        _sp.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _sp.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def _commit(self, repo, msg):
        _subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)

    def test_rename_tracked_to_unsupported_ext_emits_synthetic_delete(self, tmp_path):
        """Forward -M regression: `git mv auth.py auth.txt`. The new path has
        no parser, so keying the skip on it alone would drop the whole "R" row
        and leak the old module open forever. The fix must rewrite the row as a
        synthetic delete of the OLD path so downstream close logic runs."""
        import mcp_server
        repo = tmp_path / "repo"
        self._init_repo(repo)
        (repo / "auth.py").write_text("AUTH_KEY = 1234567890123\n\ndef login(x):\n    return x + 1\n")
        self._commit(repo, "add")
        _subprocess.run(["git", "mv", "auth.py", "auth.txt"], cwd=repo, check=True, capture_output=True)
        self._commit(repo, "rename to txt")

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        # Exactly one result: a synthetic delete keyed to the OLD path.
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "D"
        assert file_path == "auth.py"
        assert extracted is None and precomputed is None
        assert old_path == ""
        # No rename linkage should be attempted for an untrackable new side.
        assert renamed_pairs == []

    def test_rename_tracked_into_ignored_dir_emits_synthetic_delete(self, tmp_path):
        """Forward -M regression via ignore pattern: `git mv auth.py
        vendor/auth.py` under ignore_patterns=["vendor/"]. New path is ignored,
        old path is tracked -> synthetic delete of the old path."""
        import mcp_server
        repo = tmp_path / "repo"
        self._init_repo(repo)
        (repo / "auth.py").write_text("AUTH_KEY = 1234567890123\n\ndef login(x):\n    return x + 1\n")
        self._commit(repo, "add")
        (repo / "vendor").mkdir()
        _subprocess.run(["git", "mv", "auth.py", "vendor/auth.py"], cwd=repo, check=True, capture_output=True)
        self._commit(repo, "move into vendor")

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, _, _, renamed_pairs = mcp_server._extract_commit(
            str(repo), commits[1][0], ignore_patterns=["vendor/"]
        )
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "D"
        assert file_path == "auth.py"
        assert old_path == ""
        assert renamed_pairs == []

    def test_rename_unsupported_ext_to_tracked_is_plain_add(self, tmp_path):
        """Reverse -M regression: `git mv notes.txt notes.py`. The old path was
        never tracked (.txt has no parser), so the fix must treat the row as a
        plain add — extracting the new path but attaching NO rename linkage
        (no old_path), so no phantom old module gets closed downstream."""
        import mcp_server
        repo = tmp_path / "repo"
        self._init_repo(repo)
        (repo / "notes.txt").write_text("def login(x):\n    return x + 1\n")
        self._commit(repo, "add txt")
        _subprocess.run(["git", "mv", "notes.txt", "notes.py"], cwd=repo, check=True, capture_output=True)
        self._commit(repo, "rename to py")

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "A"
        assert file_path == "notes.py"
        # Crucially: old_path is empty, so _run_ingestion's R-close never fires.
        assert old_path == ""
        assert "login" in extracted["functions"]
        assert renamed_pairs == []

    def test_rename_ignored_to_tracked_is_plain_add(self, tmp_path):
        """Reverse -M regression via ignore pattern: moving a file OUT of an
        ignored dir into a tracked location. Old side ignored -> plain add."""
        import mcp_server
        repo = tmp_path / "repo"
        self._init_repo(repo)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "auth.py").write_text("def login(x):\n    return x + 1\n")
        self._commit(repo, "add vendored")
        _subprocess.run(["git", "mv", "vendor/auth.py", "auth.py"], cwd=repo, check=True, capture_output=True)
        self._commit(repo, "un-vendor")

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, _, _, renamed_pairs = mcp_server._extract_commit(
            str(repo), commits[1][0], ignore_patterns=["vendor/"]
        )
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "A"
        assert file_path == "auth.py"
        assert old_path == ""
        assert renamed_pairs == []


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
        """_watermark_update (mcp_server.py) writes [:ingestion/watermark :hash <hash>]
        directly (see its source) — the query below uses that exact ident/attribute,
        not a guess."""
        import mcp_server
        mcp_server._watermark_update(real_db, "deadbeef", "2025-03-01T10:00:00Z", "git:deadbeef x: y")
        result = json.loads(real_db.execute(
            '(query [:find ?h :where [:ingestion/watermark :hash ?h]])'
        ))
        assert result["results"] == [["deadbeef"]]

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

    def test_build_close_triples_includes_ident_and_description(self):
        import mcp_server
        fn_ident = ":function/auth-py-login"
        module_ident = ":module/auth-py"
        triples = mcp_server._build_close_triples(fn_ident, "login", module_ident)
        assert any(f'[{fn_ident} :ident "{fn_ident}"]' in t for t in triples)
        assert any(f'[{fn_ident} :description "login"]' in t for t in triples)

    def test_build_close_triples_includes_contains_edge_for_child_entity(self):
        import mcp_server
        fn_ident = ":function/auth-py-login"
        module_ident = ":module/auth-py"
        triples = mcp_server._build_close_triples(fn_ident, "login", module_ident)
        assert any(f'[{module_ident} :contains {fn_ident}]' in t for t in triples)

    def test_build_close_triples_for_module_excludes_contains(self):
        import mcp_server
        module_ident = ":module/auth-py"
        triples = mcp_server._build_close_triples(module_ident, "auth.py", module_ident)
        assert not any(":contains" in t for t in triples)

    def test_build_code_triples_writes_modified_in_for_preexisting_functions(self):
        import mcp_server
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        cls_ident = mcp_server._code_ident("class", "auth.py", "User")
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {
            module_ident: "2025-01-01T00:00:00Z",
            fn_ident: "2025-01-01T00:00:00Z",
            cls_ident: "2025-01-01T00:00:00Z",
        }
        commit_ident = ":commit/deadbeef12345678"
        extracted = {"functions": ["login"], "classes": ["User"], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        triples = mcp_server._build_code_triples(
            "auth.py",
            extracted,
            "2025-02-01T00:00:00Z",
            entity_valid_from,
            {},
            {},
            commit_ident,
            precomputed,
        )
        assert any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)
        assert any(f"[{cls_ident} :modified-in {commit_ident}]" in t for t in triples)

    def test_build_code_triples_does_not_write_modified_in_for_new_functions(self):
        import mcp_server
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {module_ident: "2025-01-01T00:00:00Z"}
        commit_ident = ":commit/deadbeef12345678"
        extracted = {"functions": ["new_func"], "classes": [], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        triples = mcp_server._build_code_triples(
            "auth.py",
            extracted,
            "2025-02-01T00:00:00Z",
            entity_valid_from,
            {},
            {},
            commit_ident,
            precomputed,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "new_func")
        assert not any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)
        assert any(f"[{fn_ident} :introduced-by {commit_ident}]" in t for t in triples)

    def test_build_code_triples_populates_entity_descriptions(self):
        import mcp_server
        entity_valid_from: dict = {}
        entity_descriptions: dict = {}
        file_entities: dict = {}
        commit_ident = ":commit/abc123456789"
        extracted = {"functions": ["login"], "classes": ["User"], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        mcp_server._build_code_triples(
            "auth.py",
            extracted,
            "2025-01-01T00:00:00Z",
            entity_valid_from,
            entity_descriptions,
            file_entities,
            commit_ident,
            precomputed,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        cls_ident = mcp_server._code_ident("class", "auth.py", "User")
        module_ident = mcp_server._code_ident("module", "auth.py")
        assert entity_descriptions.get(fn_ident) == "login"
        assert entity_descriptions.get(cls_ident) == "User"
        assert entity_descriptions.get(module_ident) == "auth.py"

    def test_preload_known_entities_loads_descriptions_and_valid_from(
        self, real_db, git_repo
    ):
        """_preload_known_entities' query (see its source) requires the full
        [:entity-type :ident :file/:path :description :introduced-by] shape
        plus the introducing commit's :date — a bare :description/:file pair
        (as an earlier draft of this test assumed) never matches the query
        and silently yields empty results, so the seed below mirrors the
        real fact shape _run_ingestion actually writes."""
        import mcp_server
        real_db.execute(
            '(transact [[:function/auth-py-login :entity-type :type/function] '
            '[:function/auth-py-login :ident ":function/auth-py-login"] '
            '[:function/auth-py-login :file "auth.py"] '
            '[:function/auth-py-login :description "login"] '
            '[:function/auth-py-login :introduced-by :commit/c1] '
            '[:commit/c1 :date "2025-01-15T10:00:00Z"]])'
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

    def test_run_ingestion_writes_last_run_on_completion(self, real_db, git_repo, monkeypatch):
        """Uses the real git_repo fixture (2 real commits) rather than
        faking a commit list + empty _git_diff_tree_raw: _extract_commit
        runs in a spawned worker process (#116), which re-imports
        mcp_server fresh and never sees _git_diff_tree_raw patched on this
        (parent) process's module object — a fabricated commit hash against
        a non-git tmp_path would just make the real git call fail in the
        worker. _last_run_write itself still runs on write_executor, an
        in-process thread, so patching it here still works as before.
        """
        import mcp_server

        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        last_hash = commits[-1][0]

        last_run_calls = []
        monkeypatch.setattr(
            mcp_server, "_last_run_write",
            lambda db, h, t, n, index_con=None: last_run_calls.append((h, t, n))
        )

        asyncio.run(mcp_server._run_ingestion(str(git_repo), "HEAD"))

        assert len(last_run_calls) == 1
        assert last_run_calls[0][0] == last_hash
        assert last_run_calls[0][1].endswith("Z")
        assert last_run_calls[0][2] == 2  # 2 commits processed

    def test_run_ingestion_writes_last_run_when_no_commits(self, real_db, tmp_path, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: "abc123")
        monkeypatch.setattr(mcp_server, "_git_commits", lambda repo, watermark, branch: [])

        last_run_calls = []
        monkeypatch.setattr(
            mcp_server, "_last_run_write",
            lambda db, h, t, n, index_con=None: last_run_calls.append((h, t, n))
        )

        asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))

        assert len(last_run_calls) == 1
        assert last_run_calls[0][0] == "abc123"
        assert last_run_calls[0][1].endswith("Z")
        assert last_run_calls[0][2] == 0  # no commits processed this run, prior was 0


class TestIngestCloseFactIndex:
    """_ingest_close makes two separate writes -- a retract-loop (the actual
    live-index removal mechanism) and a bounded re-transact (historical,
    now indexed as a historical row). Both must route through the
    _transact/_retract choke point for a closed entity to actually disappear
    from the live index. The bounded re-transact carries its valid_from and
    valid_to window in the index for point-in-time retrieval (the entry point
    into history that the whole plan is building).
    This is the exact gap the design-review process for #118 caught: an
    earlier draft of the design doc mislabeled which half does the real
    removal.

    Seeding below uses mcp_server._transact directly, NOT _ingest_transact --
    _ingest_transact was migrated in a later task to route through _transact
    too (given an index_con), so either would touch the fact index equally
    now; _transact is used here purely because it needs no index_con
    plumbing for a single-fact seed. Each index-state test below also
    asserts the seed itself landed in the index, so a future regression to
    a non-indexing seed path would fail loudly here instead of silently
    validating nothing.
    """

    def test_close_removes_open_assertion_from_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]',
            "2026-01-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        # Sanity: the seed genuinely indexed the fact. If this fails, the
        # assertion below would be vacuous (nothing there to remove).
        seeded = fact_index.query_facts(index_path, "foo module", top_n=10, boost=2.0, historical_discount=1.0)
        assert any(r[0] == ":module/foo" for r in seeded)

        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z", "test",
        )

        results = fact_index.query_facts(index_path, "foo module", top_n=10, boost=2.0, historical_discount=1.0)
        # After close, the open assertion is removed from live-time queries
        # (retract removes it), but _ingest_close re-transacts it bounded as
        # a historical record so it's still in the index with valid_to set
        assert len(results) == 1
        assert results[0][0] == ":module/foo"
        assert results[0][3] == "2026-01-01T00:00:00.000Z"  # valid_from
        assert results[0][4] == "2026-02-01T00:00:00.000Z"  # valid_to

    def test_close_bounded_retransact_indexed_as_historical(self, real_db):
        """After close, the historical (valid_to-bounded) half of a close is
        indexed as a historical row, not skipped. The open half is retracted
        (removed from live-time queries) and the bounded half is re-transacted
        to preserve the valid window in the index for point-in-time retrieval."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]',
            "2026-01-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            seeded_rows = con.execute(
                "SELECT * FROM facts_fts WHERE entity = ?", (":module/foo",)
            ).fetchall()
        finally:
            con.close()
        assert seeded_rows != [], "seed must genuinely index the fact, or this test is vacuous"

        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z", "test",
        )

        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT * FROM facts_fts WHERE entity = ?", (":module/foo",)
            ).fetchall()
        finally:
            con.close()
        # After close, the bounded version should be in the index as a historical row
        assert len(rows) == 1
        assert rows[0][0] == ":module/foo"
        assert rows[0][3] == "2026-01-01T00:00:00.000Z"  # valid_from
        assert rows[0][4] == "2026-02-01T00:00:00.000Z"  # valid_to

    def test_close_routes_both_writes_through_choke_point(self, real_db, monkeypatch):
        """Structural proof that both halves of _ingest_close call the
        _retract/_transact choke-point functions -- not just that the
        resulting index state looks right.

        This matters specifically for the bounded re-transact half: a
        bounded write (valid_to != None) is now indexed as a historical row.
        An index-state-only test (like the two above) cannot structurally
        distinguish "correctly migrated to call _transact" from "still calling
        raw _db_execute, never migrated at all" for that half if the final
        state could theoretically arise either way. This test can, because it
        inspects the calls themselves rather than their downstream effect on
        the index.
        """
        import mcp_server
        # Seed the graph directly (not via _transact/_ingest_transact) so the
        # retract-loop has a real fact to retract, without itself invoking
        # either spied choke-point function.
        real_db.execute(
            '(transact {:valid-from "2026-01-01T00:00:00.000Z"} '
            '[[:module/foo :description "the foo module"]])'
        )

        retract_calls = []
        transact_calls = []
        real_retract = mcp_server._retract
        real_transact = mcp_server._transact

        def spy_retract(db, datalog_facts, **kwargs):
            retract_calls.append(datalog_facts)
            return real_retract(db, datalog_facts, **kwargs)

        def spy_transact(db, datalog_facts, valid_from, valid_to=None, **kwargs):
            transact_calls.append((datalog_facts, valid_from, valid_to))
            return real_transact(db, datalog_facts, valid_from, valid_to=valid_to, **kwargs)

        monkeypatch.setattr(mcp_server, "_retract", spy_retract)
        monkeypatch.setattr(mcp_server, "_transact", spy_transact)

        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z", "test",
        )

        assert len(retract_calls) == 1, (
            "retract-loop must call the _retract choke point once per triple"
        )
        assert ':module/foo :description "the foo module"' in retract_calls[0]
        assert len(transact_calls) == 1, (
            "bounded re-transact must call the _transact choke point"
        )
        _, _, valid_to = transact_calls[0]
        assert valid_to == "2026-02-01T00:00:00.000Z", (
            "bounded re-transact must pass valid_to so _transact's own "
            "indexing guard skips it"
        )

    def test_close_produces_a_historical_row_not_a_dropped_one(self, real_db):
        """Complements the existing test_close_removes_open_assertion_from_index
        (which only proves the CURRENT row is gone) -- this proves the fact
        didn't just vanish, it became a labeled historical row."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]', "2024-01-01T00:00:00.000Z",
        )
        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT entity, valid_from, valid_to FROM facts_fts WHERE entity = ':module/foo'"
            ).fetchall()
        finally:
            con.close()
        assert rows == [(":module/foo", "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z")]

    def test_reopen_after_close_produces_current_plus_historical_rows(self, real_db):
        """assert -> close -> re-assert the same (e, a, v): the historical
        row from the close survives, a new current row exists too -- proves
        delete_facts' valid_to IS NULL scoping (Task 2) holds through the
        real _ingest_close/_transact call sites, not just direct fact_index
        calls."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]', "2024-01-01T00:00:00.000Z",
        )
        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]', "2025-06-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT valid_from, valid_to FROM facts_fts WHERE entity = ':module/foo' ORDER BY valid_from"
            ).fetchall()
        finally:
            con.close()
        assert rows == [
            ("2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z"),
            ("2025-06-01T00:00:00.000Z", None),
        ]

    def test_backfill_after_close_reconstructs_the_same_historical_row(self, real_db):
        """assert+close directly (no fact_index involvement at all, simulating
        a pre-existing graph), delete the index, backfill -- the rebuilt
        index has exactly the historical row a live _ingest_close would
        have produced, with the same window. Uses raw minigraf calls (not
        the choke point) for the seed, to prove backfill reconstructs
        windows correctly from the graph alone, not from any index state.

        The :ident triple is added to the seed so the entity can be recovered
        by keyword during backfill (a known gotcha from Task 6: _rebuild_index_from_graph
        cannot recover a keyword ident from a raw graph rescan without an explicit
        [:entity :ident ":keyword"] companion triple)."""
        import mcp_server
        import fact_index
        import os
        # Seed via raw minigraf calls (not _transact/_ingest_close choke points) to
        # prove _rebuild_index_from_graph works from the graph alone.
        # Step 1: transact an open fact
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z"} '
            '[[:module/bar :description "the bar module"]])'
        )
        # Step 2: retract it (the first half of the close operation)
        real_db.execute('(retract [[:module/bar :description "the bar module"]])')
        # Step 3: re-transact it bounded (the second half of the close operation)
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z" :valid-to "2025-01-01T00:00:00.000Z"} '
            '[[:module/bar :description "the bar module"]])'
        )
        # Step 4: add the :ident triple so backfill can recover the entity by keyword
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z"} '
            '[[:module/bar :ident ":module/bar"]])'
        )
        # Now delete the index to simulate a recovered pre-index graph
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        if os.path.exists(index_path):
            os.remove(index_path)
        # Rebuild from graph
        mcp_server._rebuild_index_from_graph()
        # Verify the backfilled index matches -- query for the specific fact we closed,
        # not all facts for the entity (which includes the :ident fact)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT entity, valid_from, valid_to FROM facts_fts WHERE entity = ':module/bar' AND attribute = ':description'"
            ).fetchall()
        finally:
            con.close()
        assert rows == [(":module/bar", "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z")]


class TestPrecomputeFileTriples:
    def test_module_candidate_triples_include_introduced_by(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "auth.py",
            {"functions": [], "classes": [], "imports": []},
            ":commit/abc123456789",
            {},
        )
        module_ident = mcp_server._code_ident("module", "auth.py")
        assert result["module_ident"] == module_ident
        assert any(
            f"[{module_ident} :introduced-by :commit/abc123456789]" in t
            for t in result["module_candidate_triples"]
        )

    def test_function_entries_carry_ident_name_and_candidate_triples(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "auth.py",
            {"functions": ["login"], "classes": [], "imports": []},
            ":commit/abc123456789",
            {},
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert len(result["function_entries"]) == 1
        ident, name, triples = result["function_entries"][0]
        assert ident == fn_ident
        assert name == "login"
        assert any(f'[{fn_ident} :description "login"]' in t for t in triples)
        assert any(f"[{fn_ident} :introduced-by :commit/abc123456789]" in t for t in triples)

    def test_class_entries_carry_ident_name_and_candidate_triples(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "auth.py",
            {"functions": [], "classes": ["User"], "imports": []},
            ":commit/abc123456789",
            {},
        )
        cls_ident = mcp_server._code_ident("class", "auth.py", "User")
        assert len(result["class_entries"]) == 1
        ident, name, triples = result["class_entries"][0]
        assert ident == cls_ident
        assert name == "User"
        assert any(f'[{cls_ident} :description "User"]' in t for t in triples)

    def test_resolved_imports_use_known_files_not_file_entities(self):
        import mcp_server
        known_files = {"mod_b.py": []}
        result = mcp_server._precompute_file_triples(
            "mod_a.py",
            {"functions": [], "classes": [], "imports": ["mod_b"]},
            ":commit/abc123456789",
            known_files,
        )
        assert len(result["resolved_imports"]) == 1
        import_name, dep_ident, is_resolved = result["resolved_imports"][0]
        assert import_name == "mod_b"
        assert is_resolved is True
        assert dep_ident == mcp_server._code_ident("module", "mod_b.py")

    def test_unresolved_import_flagged_false(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "main.rs",
            {"functions": [], "classes": [], "imports": ["totally_unknown_crate"]},
            ":commit/abc123456789",
            {},
        )
        import_name, dep_ident, is_resolved = result["resolved_imports"][0]
        assert is_resolved is False
        assert dep_ident == mcp_server._canonical_ident("module", "totally_unknown_crate")


class TestPrecomputeGlobalsAndFields:
    def test_global_entries_shape(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "globals": ["GLOBAL_X"], "fields": [],
        }
        result = mcp_server._precompute_file_triples(
            "config.py", extracted, ":commit/abc123", {}, segment_index=None,
        )
        assert len(result["global_entries"]) == 1
        ident, name, triples = result["global_entries"][0]
        assert ident == mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        assert name == "GLOBAL_X"
        assert f"[{ident} :entity-type :type/variable]" in triples
        assert f"[{ident} :introduced-by :commit/abc123]" in triples

    def test_field_entries_shape_disambiguates_by_class(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": ["Foo"], "imports": [], "calls": [],
            "globals": [],
            "fields": [("staticField", "Foo", True)],
        }
        result = mcp_server._precompute_file_triples(
            "models.py", extracted, ":commit/abc123", {}, segment_index=None,
        )
        assert len(result["field_entries"]) == 1
        ident, name, triples = result["field_entries"][0]
        expected_ident = mcp_server._code_ident("field", "models.py", "Foo.staticField")
        assert ident == expected_ident
        assert f"[{ident} :entity-type :type/field]" in triples
        assert f"[{ident} :static true]" in triples
        class_ident = mcp_server._code_ident("class", "models.py", "Foo")
        assert f"[{ident} :class {class_ident}]" in triples


class TestFieldClassContainment:
    """Fields owned by a real (extracted) class get BOTH module- and
    class-containment plus a :class edge; fields whose reported owner is not a
    real extracted class fall back to module-only containment with no dangling
    :class or class-contains edge (issues.md P2 findings)."""

    def _parser(self, module_name, lang_key):
        import mcp_server
        import tree_sitter
        import importlib
        grammar_mod = importlib.import_module(module_name)
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(grammar_mod.language())
        real_parser = tree_sitter.Parser(real_lang)
        mcp_server._grammar_cache[lang_key] = real_parser
        return real_parser

    def test_real_class_field_gets_both_contains_and_class_edge(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": ["Foo"], "imports": [], "calls": [],
            "globals": [], "fields": [("bar", "Foo", True)],
        }
        result = mcp_server._precompute_file_triples(
            "models.py", extracted, ":commit/c1", {}, segment_index=None,
        )
        field_ident = mcp_server._code_ident("field", "models.py", "Foo.bar")
        module_ident = mcp_server._code_ident("module", "models.py")
        class_ident = mcp_server._code_ident("class", "models.py", "Foo")
        _, _, triples = result["field_entries"][0]
        assert f"[{module_ident} :contains {field_ident}]" in triples
        assert f"[{class_ident} :contains {field_ident}]" in triples
        assert f"[{field_ident} :class {class_ident}]" in triples
        assert result["field_class_map"] == {field_ident: class_ident}

    def test_field_with_no_extracted_owner_class_is_module_only(self):
        import mcp_server
        # owner "Ghost" is NOT in classes -> no :class, no class-contains edge.
        extracted = {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "globals": [], "fields": [("attr", "Ghost", True)],
        }
        result = mcp_server._precompute_file_triples(
            "x.py", extracted, ":commit/c1", {}, segment_index=None,
        )
        field_ident = mcp_server._code_ident("field", "x.py", "Ghost.attr")
        module_ident = mcp_server._code_ident("module", "x.py")
        class_ident = mcp_server._code_ident("class", "x.py", "Ghost")
        _, _, triples = result["field_entries"][0]
        assert f"[{module_ident} :contains {field_ident}]" in triples
        assert all(":class " not in t for t in triples)
        assert f"[{class_ident} :contains {field_ident}]" not in triples
        assert result["field_class_map"] == {}

    def test_elixir_module_attribute_falls_back_to_module_only(self):
        import mcp_server
        parser = self._parser("tree_sitter_elixir", "elixir")
        extracted = mcp_server._extract_from_source(
            b"defmodule Foo do\n  @attr 1\nend\n", parser, "foo.ex",
        )
        # Confirm the reproduction from issues.md: field extracted, no class.
        assert extracted["fields"] == [("attr", "Foo", True)]
        assert extracted["classes"] == []
        result = mcp_server._precompute_file_triples(
            "foo.ex", extracted, ":commit/c1", {}, segment_index=None,
        )
        field_ident = mcp_server._code_ident("field", "foo.ex", "Foo.attr")
        module_ident = mcp_server._code_ident("module", "foo.ex")
        class_ident = mcp_server._code_ident("class", "foo.ex", "Foo")
        _, _, triples = result["field_entries"][0]
        assert f"[{module_ident} :contains {field_ident}]" in triples
        assert all(":class " not in t for t in triples)
        assert f"[{class_ident} :contains {field_ident}]" not in triples
        assert result["field_class_map"] == {}

    def test_haskell_newtype_field_falls_back_to_module_only(self):
        import mcp_server
        parser = self._parser("tree_sitter_haskell", "haskell")
        extracted = mcp_server._extract_from_source(
            b"newtype Foo = Foo { unFoo :: Int }\n", parser, "foo.hs",
        )
        assert extracted["fields"] == [("unFoo", "Foo", False)]
        assert extracted["classes"] == []
        result = mcp_server._precompute_file_triples(
            "foo.hs", extracted, ":commit/c1", {}, segment_index=None,
        )
        field_ident = mcp_server._code_ident("field", "foo.hs", "Foo.unFoo")
        module_ident = mcp_server._code_ident("module", "foo.hs")
        class_ident = mcp_server._code_ident("class", "foo.hs", "Foo")
        _, _, triples = result["field_entries"][0]
        assert f"[{module_ident} :contains {field_ident}]" in triples
        assert all(":class " not in t for t in triples)
        assert f"[{class_ident} :contains {field_ident}]" not in triples
        assert result["field_class_map"] == {}

    def test_build_code_triples_populates_field_class_ident(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": ["Foo"], "imports": [], "calls": [],
            "globals": [], "fields": [("bar", "Foo", True)],
        }
        precomputed = mcp_server._precompute_file_triples("models.py", extracted, ":commit/c1", {})
        field_class_ident = {}
        mcp_server._build_code_triples(
            "models.py", extracted, "2024-01-01T00:00:00Z", {}, {}, {}, ":commit/c1",
            precomputed, field_class_ident,
        )
        field_ident = mcp_server._code_ident("field", "models.py", "Foo.bar")
        class_ident = mcp_server._code_ident("class", "models.py", "Foo")
        assert field_class_ident == {field_ident: class_ident}

    def test_build_code_triples_omits_field_class_ident_for_dangling_owner(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "globals": [], "fields": [("attr", "Ghost", True)],
        }
        precomputed = mcp_server._precompute_file_triples("x.py", extracted, ":commit/c1", {})
        field_class_ident = {}
        mcp_server._build_code_triples(
            "x.py", extracted, "2024-01-01T00:00:00Z", {}, {}, {}, ":commit/c1",
            precomputed, field_class_ident,
        )
        assert field_class_ident == {}

    def test_close_triples_retracts_extra_class_contains_edge(self):
        import mcp_server
        field_ident = mcp_server._code_ident("field", "models.py", "Foo.bar")
        module_ident = mcp_server._code_ident("module", "models.py")
        class_ident = mcp_server._code_ident("class", "models.py", "Foo")
        triples = mcp_server._build_close_triples(
            field_ident, "Foo.bar", module_ident, class_ident,
        )
        assert f"[{module_ident} :contains {field_ident}]" in triples
        assert f"[{class_ident} :contains {field_ident}]" in triples

    def test_close_triples_ignores_none_extra_parent(self):
        import mcp_server
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        module_ident = mcp_server._code_ident("module", "auth.py")
        triples = mcp_server._build_close_triples(fn_ident, "login", module_ident, None)
        # Only the module-contains edge, no phantom extra :contains.
        contains = [t for t in triples if ":contains" in t]
        assert contains == [f"[{module_ident} :contains {fn_ident}]"]


class TestBuildCodeTriplesGlobalsAndFields:
    def test_new_global_writes_full_triples(self):
        import mcp_server
        extracted = {"functions": [], "classes": [], "imports": [], "calls": [],
                     "globals": ["GLOBAL_X"], "fields": []}
        precomputed = mcp_server._precompute_file_triples("config.py", extracted, ":commit/c1", {})
        entity_valid_from, entity_descriptions, file_entities = {}, {}, {}
        triples = mcp_server._build_code_triples(
            "config.py", extracted, "2024-01-01T00:00:00Z", entity_valid_from,
            entity_descriptions, file_entities, ":commit/c1", precomputed,
        )
        ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        assert any(f"{ident} :entity-type :type/variable" in t for t in triples)
        assert ident in entity_valid_from

    def test_preexisting_global_only_gets_modified_in(self):
        import mcp_server
        extracted = {"functions": [], "classes": [], "imports": [], "calls": [],
                     "globals": ["GLOBAL_X"], "fields": []}
        precomputed = mcp_server._precompute_file_triples("config.py", extracted, ":commit/c2", {})
        ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        module_ident = mcp_server._code_ident("module", "config.py")
        # Module must already be known too, otherwise _build_code_triples treats
        # it as newly introduced and emits module_candidate_triples alongside
        # the global's :modified-in line, breaking the strict equality below.
        entity_valid_from = {
            module_ident: "2024-01-01T00:00:00Z",
            ident: "2024-01-01T00:00:00Z",
        }
        entity_descriptions = {module_ident: "config.py", ident: "GLOBAL_X"}
        file_entities = {"config.py": [module_ident, ident]}
        triples = mcp_server._build_code_triples(
            "config.py", extracted, "2024-01-02T00:00:00Z", entity_valid_from,
            entity_descriptions, file_entities, ":commit/c2", precomputed,
        )
        # Both the already-known module and the already-known global only get
        # a :modified-in edge — none of the candidate (:entity-type, :ident, …)
        # triples are re-asserted.
        assert triples == [
            f"[{module_ident} :modified-in :commit/c2]",
            f"[{ident} :modified-in :commit/c2]",
        ]


class TestPreloadKnownDeps:
    def test_reloads_open_depends_on_edge(self, real_db):
        """Real-backend regression test for the #133 UUID-vs-ident bug:
        _preload_known_deps' query used to bind the bare ?src subject
        variable directly (`[?src :depends-on ?dep]`), which a real minigraf
        backend resolves to its *internal* UUID rather than the
        ":module/…" keyword ident used to create the entity — confirmed
        empirically during the #133 investigation:
            db.execute('(transact [[:module/mod-a-py :entity-type :type/module]
              [:module/mod-a-py :ident ":module/mod-a-py"]]))')
            db.execute('(transact {:valid-from "2024-01-01T00:00:00Z"}
              [[:module/mod-a-py :depends-on :module/mod-b]])')
            db.execute('(query [:find ?src ?dep :where [?src :depends-on ?dep]])')
            => {"results": [["6b877d67-...uuid...", ":module/mod-b"]]}
        `ident_to_file.get(src_ident)` was then keyed by the ":module/…"
        ident string, which never matched a UUID — every row was silently
        dropped, so against a real backend the function returned ({}, {})
        unconditionally, discarding all known deps on every restart. Fixed
        by projecting `[?src :ident ?srci]` and finding ?srci instead of
        ?src (mirroring _preload_known_entities).

        This test also proves the fix's clause ORDERING is correct, not
        just that it returns non-empty: minigraf's per-fact :db/valid-from
        pseudo-attribute binds to whichever EAV clause on ?src most
        recently precedes it in clause order, so the entity's :ident fact
        and its :depends-on fact are seeded here with deliberately
        DIFFERENT :valid-from timestamps (2020 vs. 2024). If the :ident
        clause were placed after :depends-on (or anywhere but immediately
        before it), ?vf would silently rebind to the :ident fact's
        valid-from (2020) instead of the :depends-on fact's (2024) —
        a different, provably wrong value that this assertion would catch.
        """
        import mcp_server

        real_db.execute(
            '(transact {:valid-from "2020-01-01T00:00:00Z"} '
            '[[:module/mod-a-py :entity-type :type/module] '
            '[:module/mod-a-py :ident ":module/mod-a-py"]])'
        )
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00Z"} '
            '[[:module/mod-a-py :depends-on :module/mod-b]])'
        )

        file_entities = {"mod_a.py": [":module/mod-a-py"]}
        file_deps, dep_valid_from = mcp_server._preload_known_deps(real_db, file_entities)

        assert file_deps["mod_a.py"] == {":module/mod-b"}
        assert dep_valid_from[(":module/mod-a-py", ":module/mod-b")] == "2024-01-01T00:00:00.000Z"

    def test_query_includes_any_valid_time_and_forever_filter(self, real_db):
        """The query must ask for :any-valid-time (required for any per-fact
        pseudo-attribute to bind) and filter :db/valid-to down to the
        VALID_TIME_FOREVER sentinel so closed edges aren't reloaded as open."""
        import mcp_server
        with execute_spy() as calls:
            mcp_server._preload_known_deps(real_db, {})

        assert len(calls) == 1
        query = calls[0]
        assert ":any-valid-time" in query
        assert ":depends-on" in query
        assert ":db/valid-from" in query
        assert ":db/valid-to" in query
        assert "9223372036854775807" in query

    def test_no_deps_returns_empty_structures(self, real_db):
        import mcp_server
        file_deps, dep_valid_from = mcp_server._preload_known_deps(real_db, {"mod_a.py": []})

        assert file_deps == {}
        assert dep_valid_from == {}

    def test_query_failure_is_non_fatal(self, real_db, monkeypatch):
        """Break _preload_known_deps' hard-coded query into genuinely
        malformed Datalog by corrupting the _VALID_TIME_FOREVER_MS constant
        it interpolates into the `(= ?vt ...)` predicate — the real minigraf
        parser then raises a real MiniGrafError ("Unclosed vector" for
        unbalanced brackets), confirming the function's try/except actually
        catches a real parse failure rather than a mocked exception."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_VALID_TIME_FOREVER_MS", "))) malformed [[[")

        file_deps, dep_valid_from = mcp_server._preload_known_deps(real_db, {"mod_a.py": []})

        assert file_deps == {}
        assert dep_valid_from == {}


class TestPreloadExternalDependencies:
    def test_preload_known_entities_includes_external_dependency(self, real_db, tmp_path):
        import mcp_server
        real_db.execute(
            '(transact [[:module/vendor-lib :entity-type :type/external-dependency] '
            '[:module/vendor-lib :ident ":module/vendor-lib"] '
            '[:module/vendor-lib :path "vendor/lib"] '
            '[:module/vendor-lib :description "lib"] '
            '[:module/vendor-lib :introduced-by :commit/c1] '
            '[:commit/c1 :date "2026-01-01T00:00:00Z"]])'
        )

        entity_valid_from, entity_descriptions, file_entities, submodule_paths = (
            mcp_server._preload_known_entities(real_db, str(tmp_path))
        )

        assert entity_valid_from[":module/vendor-lib"] == "2026-01-01T00:00:00Z"
        assert entity_descriptions[":module/vendor-lib"] == "lib"
        assert "vendor/lib" in file_entities
        assert submodule_paths[":module/vendor-lib"] == "vendor/lib"

    def test_preload_pinned_commits_reloads_current_sha(self, real_db):
        """Real-backend regression test for the #133 UUID-vs-ident bug in
        _preload_pinned_commits — same root cause as
        TestPreloadKnownDeps.test_reloads_open_depends_on_edge:
        the query used to bind the bare ?e subject variable directly
        (`[?e :pinned-commit ?sha]`), which a real minigraf backend
        resolves to its internal UUID rather than the entity's ":module/…"
        ident. Every call site that reads this function's return value
        (_run_ingestion's gitlink bump/remove handling) looks it up by
        ident string (`pinned_commit_state.get(ext_ident, ...)`), so
        against a real backend the lookup always missed after a restart,
        silently resetting each pin's original valid-from. Fixed by
        projecting `[?e :ident ?ei]` and finding ?ei instead of ?e.

        As with the depends-on test, the :ident fact and the :pinned-commit
        fact are seeded with deliberately DIFFERENT :valid-from timestamps
        (2020 vs. 2026) to prove the fix's clause ORDERING (:ident before
        :pinned-commit) is correct — not just that the result is non-empty.
        If :ident were ordered wrong, ?vf would bind to the :ident fact's
        valid-from (2020) instead of the :pinned-commit fact's (2026).
        """
        import mcp_server

        real_db.execute(
            '(transact {:valid-from "2020-01-01T00:00:00Z"} '
            '[[:module/vendor-lib :entity-type :type/external-dependency] '
            '[:module/vendor-lib :ident ":module/vendor-lib"]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-01-01T00:00:00Z"} '
            '[[:module/vendor-lib :pinned-commit "abc123"]])'
        )

        pinned = mcp_server._preload_pinned_commits(real_db)

        assert pinned[":module/vendor-lib"] == ("abc123", "2026-01-01T00:00:00.000Z")

    def test_preload_pinned_commits_returns_empty_on_query_failure(self, real_db, monkeypatch):
        """Same malformed-query technique as
        TestPreloadKnownDeps.test_query_failure_is_non_fatal: corrupt
        _VALID_TIME_FOREVER_MS so the hard-coded `(= ?vt ...)` predicate
        becomes genuinely unparseable Datalog, triggering a real
        MiniGrafError from the real parser."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_VALID_TIME_FOREVER_MS", "))) malformed [[[")

        assert mcp_server._preload_pinned_commits(real_db) == {}


class TestIngestTransactFactIndex:
    def test_ingest_transact_writes_to_index_with_explicit_con(self, real_db):
        import mcp_server
        import fact_index
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_writer(index_path)
        try:
            mcp_server._ingest_transact(
                real_db, ['[:module/foo :description "the foo module"]'],
                "2026-01-01T00:00:00.000Z", "test", index_con=con,
            )
            con.commit()
        finally:
            fact_index.close_writer(con)
        results = fact_index.query_facts(index_path, "foo module", top_n=10, boost=2.0, historical_discount=1.0)
        assert results


class TestTotalIngestedQuery:
    def test_returns_zero_when_absent(self, real_db):
        import mcp_server
        assert mcp_server._total_ingested_query(real_db) == 0

    def test_returns_stored_count(self, real_db):
        import mcp_server
        real_db.execute(
            '(transact [[:ingestion/last-run-at :entity-type :type/ingestion] '
            '[:ingestion/last-run-at :total-ingested 462]])'
        )
        assert mcp_server._total_ingested_query(real_db) == 462


class TestRunIngestion:
    @pytest.mark.asyncio
    async def test_ingestion_processes_all_commits(self, real_db, git_repo, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2

    @pytest.mark.asyncio
    async def test_sets_error_at_timestamp_on_failure(self, real_db, git_repo, monkeypatch):
        import mcp_server
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

    @pytest.mark.asyncio
    async def test_watermark_updated_after_each_commit(self, real_db, git_repo, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        with execute_spy() as calls:
            await mcp_server._run_ingestion(str(git_repo), "HEAD")
        watermark_calls = [
            c for c in calls if ":ingestion/watermark" in c and "transact" in c
        ]
        assert len(watermark_calls) >= 2  # one per commit

    @pytest.mark.asyncio
    async def test_per_commit_get_db_lock_retry_does_not_block_event_loop(
        self, real_db, git_repo, monkeypatch
    ):
        """Regression test for #99: a lock-retry hit while reacquiring the DB
        between commits must not block via time.sleep() — _run_ingestion runs
        on the single-threaded event loop, and a blocking sleep there would
        freeze the very coroutine responsible for eventually releasing that
        lock.

        Real-backend version: rather than a canned MagicMock.open.side_effect
        list, this wraps the real (in-memory-backed) MiniGrafDb.open that
        real_db already installed — same technique as
        TestGetDbLockRetry.test_retries_open_after_clearing_stale_lock_on_final_attempt
        — so the very first post-preload reacquire raises a genuine
        MiniGrafError('...locked...') once, then every subsequent call opens
        a real handle normally."""
        import mcp_server
        from minigraf import MiniGrafDb

        base_open = MiniGrafDb.open
        call_count = {"n": 0}

        def flaky_open(path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise MiniGrafError(
                    "Database is locked by another process (lock file: x.graph.lock, holder PID: 1)."
                )
            return base_open(path)

        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(flaky_open))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        def fail_if_called(_delay):
            raise AssertionError("time.sleep() must not be called on the event-loop retry path (see #99)")
        monkeypatch.setattr(mcp_server.time, "sleep", fail_if_called)

        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2
        assert call_count["n"] >= 2, "expected the flaky open() to actually be exercised"

    @pytest.mark.asyncio
    async def test_preload_phase_does_not_block_event_loop(
        self, real_db, git_repo, monkeypatch
    ):
        """Regression test for #103: opening the DB and running the startup
        preload queries (_watermark_query, _count_commit_entities,
        _preload_known_entities/_deps/_pinned_commits) must run off the event
        loop. Before the fix these ran synchronously inline with no `await`
        between them, so on a large graph the phase could run long enough to
        starve the stdio handshake past a client's connection timeout,
        leaving the server permanently unable to connect."""
        import mcp_server

        def slow_preload(db, repo_path):
            time.sleep(0.3)
            return {}, {}, {}

        monkeypatch.setattr(mcp_server, "_preload_known_entities", slow_preload)
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        heartbeat_ticks = 0

        async def heartbeat():
            nonlocal heartbeat_ticks
            while True:
                heartbeat_ticks += 1
                await asyncio.sleep(0.02)

        heartbeat_task = asyncio.create_task(heartbeat())
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

        assert heartbeat_ticks >= 5, (
            "event loop was starved during the preload phase: only "
            f"{heartbeat_ticks} heartbeat ticks during a 0.3s slow preload"
        )

    @pytest.mark.asyncio
    async def test_db_released_between_commits(self, real_db, git_repo, monkeypatch):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        db_none_snapshots = []

        original_sleep = asyncio.sleep
        async def patched_sleep(t):
            db_none_snapshots.append(mcp_server._db is None)
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", patched_sleep):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert all(db_none_snapshots), f"_db was not None at yield: {db_none_snapshots}"

    @pytest.mark.asyncio
    async def test_local_db_reference_dropped_before_commit_enumeration(
        self, git_repo, monkeypatch
    ):
        """Regression test for #84's deeper root cause: minigraf exposes no
        explicit close(), so the OS-level file lock is only released once
        *every* reference to the handle is gone via CPython refcounting.
        Clearing the module-global `_db` alone is not enough if
        `_run_ingestion`'s local `db` variable still points at the same
        object — verified against the real (unmocked) minigraf FFI, where a
        stale local reference left the `.lock` file in place.

        This specifically targets the pre-loop release point (before
        `_git_commits` walks the repo's history), because that call has no
        `await` before it: unlike the per-commit loop — where CPython's
        dead-local clearing at the `await asyncio.sleep(0)` yield point can
        mask a missing explicit clear — a potentially slow, synchronous
        `git log` walk here would hold the lock for its entire duration
        unless `db` is explicitly dropped.

        No real_db fixture here — this test's whole point
        is CPython refcounting on the DB handle object itself, so it needs a
        deliberately lightweight, non-mock handle. real_db's actual FFI
        object carries its own internal cross-references that would pollute
        sys.getrefcount() just as badly as MagicMock's self-referential
        state does (see _FakeDb's docstring below), so MiniGrafDb.open is
        monkeypatched directly to a plain-object factory instead.
        """
        import mcp_server
        from minigraf import MiniGrafDb

        class _FakeDb:
            """Plain object, not MagicMock — MagicMock carries internal
            self-referential state that inflates sys.getrefcount() far past
            what a real released reference would show."""
            def execute(self, *a, **k):
                return json.dumps({"results": []})
            def checkpoint(self):
                pass

        opened_instances = []

        def _open(path):
            inst = _FakeDb()
            opened_instances.append(inst)
            return inst

        monkeypatch.setattr(MiniGrafDb, "open", staticmethod(_open))
        mcp_server._db = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        refcount_at_enumeration = {}
        real_git_commits = mcp_server._git_commits

        def spy_git_commits(repo, watermark, branch):
            assert len(opened_instances) == 1
            refcount_at_enumeration["count"] = sys.getrefcount(opened_instances[0])
            return real_git_commits(repo, watermark, branch)

        with patch.object(mcp_server, "_git_commits", side_effect=spy_git_commits):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # 1 ref for opened_instances' slot + 1 for the local `inst` in
        # sys.getrefcount's own argument frame == 2 if nothing else (e.g. a
        # stale `db` local in _run_ingestion's frame) is still holding it.
        assert refcount_at_enumeration["count"] <= 2, (
            "a stale local reference kept the DB instance (and its file "
            f"lock) alive during commit enumeration: "
            f"refcount={refcount_at_enumeration['count']}"
        )

    @pytest.mark.asyncio
    async def test_handle_minigraf_ingest_git_returns_immediately(self, real_db, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is True
        assert "job_id" in result
        # Cleanup: let the real background ingestion this started finish
        # before the test (and real_db's MiniGrafDb.open monkeypatch) tears
        # down, so nothing is left running against a reverted fixture.
        await mcp_server._ingest_task

    @pytest.mark.asyncio
    async def test_second_call_while_running_returns_error(self, real_db, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is False
        assert "already in progress" in result["error"]
        # Cleanup: drain the still-running first ingestion task.
        await mcp_server._ingest_task

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_repo(self, monkeypatch):
        """Fails at the git-repo validity check, before any DB is ever
        touched — no DB fixture needed at all."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path="/nonexistent/path")
        assert result["ok"] is False
        assert "Not a git repository" in result["error"]

    @pytest.mark.asyncio
    async def test_skips_when_live_holder_present(self, git_repo, tmp_path, monkeypatch):
        """_live_lock_holder_pid is monkeypatched directly and the skip path
        returns before any DB is ever opened, so no DB fixture is needed —
        the original test only ever received the mocked DB fixture
        incidentally, never using its mock instance."""
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

    @pytest.mark.asyncio
    async def test_proceeds_when_no_live_holder(self, real_db, git_repo, monkeypatch):
        """When no live process owns the graph lock, handle_minigraf_ingest_git
        proceeds normally and starts the ingestion task."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is True
        assert "job_id" in result
        assert mcp_server._ingest_task is not None
        await mcp_server._ingest_task

    @pytest.mark.asyncio
    async def test_status_not_idle_immediately_after_ingest_git_starts(
        self, real_db, git_repo, monkeypatch
    ):
        """Regression test for #109: handle_minigraf_ingest_git creates
        _ingest_task and returns before _run_ingestion's preload phase has
        had a chance to run, so _ingest_progress must already reflect
        "in progress" the instant it returns. Leaving it at "idle" reproduces
        the reported contradiction, where a caller sees status "idle" but
        a subsequent minigraf_ingest_git call is rejected with "already in
        progress" because the task-existence check is accurate immediately."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is True
        assert mcp_server._ingest_progress["status"] == "starting"
        await mcp_server._ingest_task

    @pytest.mark.asyncio
    async def test_ingest_git_resolves_branch_via_default_when_not_specified(
        self, real_db, git_repo, monkeypatch
    ):
        """#130: omitting `branch` must resolve through _default_git_branch,
        not hardcode "HEAD"."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        monkeypatch.setattr(mcp_server, "_default_git_branch", lambda repo_path: "resolved-branch")
        captured = {}

        async def fake_run_ingestion(repo_path, branch):
            captured["branch"] = branch

        monkeypatch.setattr(mcp_server, "_run_ingestion", fake_run_ingestion)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        await mcp_server._ingest_task
        assert result["ok"] is True
        assert captured["branch"] == "resolved-branch"

    @pytest.mark.asyncio
    async def test_ingest_git_explicit_branch_overrides_default(
        self, real_db, git_repo, monkeypatch
    ):
        """An explicit `branch` argument must win over _default_git_branch,
        which shouldn't even be consulted in that case."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        def unexpected_call(repo_path):
            raise AssertionError("_default_git_branch should not be called when branch is explicit")

        monkeypatch.setattr(mcp_server, "_default_git_branch", unexpected_call)
        captured = {}

        async def fake_run_ingestion(repo_path, branch):
            captured["branch"] = branch

        monkeypatch.setattr(mcp_server, "_run_ingestion", fake_run_ingestion)
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo), branch="feature-x")
        await mcp_server._ingest_task
        assert result["ok"] is True
        assert captured["branch"] == "feature-x"

    @pytest.mark.asyncio
    async def test_processed_seeded_from_prior_ingested(self, real_db, git_repo, monkeypatch):
        """processed starts at the true persisted commit count and increments
        cumulatively — regression test for #85 (seeding must not rely on the
        :total-ingested watermark, which goes stale after an interrupted run).

        _count_commit_entities is monkeypatched directly to the desired prior
        count rather than seeded via 462 real :type/commit entities — this
        test is about _run_ingestion's seeding arithmetic (prior_ingested +
        newly-processed commits), not about _count_commit_entities' own query
        correctness, which has its own coverage (TestTotalIngestedQuery and
        friends)."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_count_commit_entities", lambda db: 462)
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        # git_repo fixture has 2 commits; prior was 462 → final should be 464
        assert mcp_server._ingest_progress["processed"] == 464
        assert mcp_server._ingest_progress["total"] == 2  # git_repo has 2 commits
        assert mcp_server._ingest_progress["prior_ingested"] == 462

    @pytest.mark.asyncio
    async def test_processed_seed_ignores_stale_total_ingested_watermark(
        self, real_db, git_repo, monkeypatch
    ):
        """A stale :total-ingested watermark (left behind by a prior run that
        was interrupted before writing its completion record) must not affect
        seeding — only the true :type/commit count matters.

        Same rationale as test_processed_seeded_from_prior_ingested for
        monkeypatching _count_commit_entities directly rather than seeding
        21715 real entities; _total_ingested_query is likewise monkeypatched
        to a stale value to prove _run_ingestion's seeding never even
        consults it."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_count_commit_entities", lambda db: 21715)
        monkeypatch.setattr(mcp_server, "_total_ingested_query", lambda db: 104)  # stale, must be ignored
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["prior_ingested"] == 21715
        assert mcp_server._ingest_progress["processed"] == 21717  # 21715 + 2 commits


class TestRunIngestionBatchedIndexWrites:
    @pytest.mark.asyncio
    async def test_ingestion_commits_index_once_per_commit_not_per_triple(self, real_db, git_repo, monkeypatch):
        """Guards the 1M+-fact scale concern: SQLite commit-call count must
        scale with the number of ingested commits, not the number of facts.

        Uses the existing `git_repo` fixture (tests/test_mcp_server.py:4124)
        -- two commits, each adding one file (auth.py, then models.py) -- and
        calls _run_ingestion directly, the same pattern the existing
        refcount regression test uses (see
        test_db_instance_not_retained_during_commit_enumeration, which calls
        `await mcp_server._run_ingestion(str(git_repo), "HEAD")` directly
        rather than going through handle_minigraf_ingest_git's
        fire-and-forget wrapper).

        Asserts an exact count, not just an upper bound: an upper-bound-only
        assertion (e.g. `<= 3`) would pass vacuously if the whole feature were
        broken and zero index writes ever happened, which is exactly the kind
        of structurally-unfalsifiable test earlier tasks in this plan caught.
        A prior manual count (see the per-triple `_index_write` call count
        below) confirmed this fixture would need 4 separate open+commit+close
        cycles if ingestion still wrote to the index one triple-batch at a
        time (the pre-Task-8 behavior) -- each with its own fsync -- versus
        exactly 1 connection open and 3 commits (one per source commit, plus
        one final flush-on-close) once batched. The gap between those two
        numbers is what would grow unboundedly on a 1M-fact repo if this
        regressed back to per-triple commits.
        """
        import mcp_server
        import fact_index

        commit_calls = []
        open_calls = []
        original_open_writer = fact_index.open_writer

        class CountingConnection:
            def __init__(self, con):
                self._con = con
            def __getattr__(self, name):
                return getattr(self._con, name)
            def commit(self):
                commit_calls.append(1)
                self._con.commit()

        def counting_open_writer(path):
            open_calls.append(1)
            return CountingConnection(original_open_writer(path))

        monkeypatch.setattr(fact_index, "open_writer", counting_open_writer)
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # Exactly one connection opened for the whole run -- proves writes
        # were batched onto a single session, not reopened per triple-batch
        # (which is what pre-Task-8 _ingest_transact/_ingest_close did via
        # _index_write's index_con=None fallback, one open+commit+close per
        # call). A regression back to per-call connections would make this
        # >= 4 for this fixture (see docstring).
        assert len(open_calls) == 1
        # git_repo has 2 commits -> 2 per-commit commits (one right after
        # each commit's _db_checkpoint) + 1 final flush inside
        # fact_index.close_writer at run end = 3. This is a tight equality,
        # not just an upper bound: 0 (nothing wired up) and any count that
        # scaled with the ~36 individual facts these 2 commits produce would
        # both fail it, so a regression to either "no batching wired up" or
        # "still committing per triple" is caught, not just silently allowed
        # through by a loose bound.
        assert len(commit_calls) == 3


class TestRunIngestionIndexFaultIsolation:
    """#150: the batched fact-index connection's three call sites in
    _run_ingestion (open_writer, the per-commit commit(), close_writer) must
    each degrade gracefully like every per-triple _index_write call already
    does -- a failure there must never abort the whole ingestion run, since
    "index maintenance never blocks a graph write" is a stated invariant
    everywhere else in this module (see _index_write's own docstring and
    TestBookkeepingWritesFactIndex.test_transact_index_write_failure_does_not_raise
    for the single-write equivalent of this same guarantee)."""

    @pytest.mark.asyncio
    async def test_open_writer_failure_does_not_abort_ingestion(self, real_db, git_repo, monkeypatch, capsys):
        import mcp_server
        import fact_index

        monkeypatch.setattr(fact_index, "open_writer", lambda path: (_ for _ in ()).throw(OSError("disk full")))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2
        assert "open_writer failed" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_commit_failure_does_not_abort_ingestion(self, real_db, git_repo, monkeypatch, capsys):
        import mcp_server
        import fact_index

        original_open_writer = fact_index.open_writer

        class FailingCommitConnection:
            def __init__(self, con):
                self._con = con
            def __getattr__(self, name):
                return getattr(self._con, name)
            def commit(self):
                raise OSError("disk full")

        def failing_commit_open_writer(path):
            return FailingCommitConnection(original_open_writer(path))

        monkeypatch.setattr(fact_index, "open_writer", failing_commit_open_writer)
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2
        assert "commit failed" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_close_writer_failure_does_not_abort_ingestion(self, real_db, git_repo, monkeypatch, capsys):
        import mcp_server
        import fact_index

        monkeypatch.setattr(fact_index, "close_writer", lambda con: (_ for _ in ()).throw(OSError("disk full")))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2
        assert "close_writer failed" in capsys.readouterr().err


class TestRunIngestionParentEdgeFactIndex:
    @pytest.mark.asyncio
    async def test_parent_edge_is_searchable_via_fact_index(self, real_db, git_repo, monkeypatch):
        """Review-finding regression test (#118): the git-ingestion
        :parent-edge write (one transact per parent hash, guarding against
        an EAVT collision on merge commits) called _db_execute directly
        with a raw, single-quoted f-string Datalog literal instead of
        routing through _transact -- so :parent triples were written to the
        graph but never to the persisted fact index, silently unsearchable
        via fact_index.query_facts unlike every other structural fact type
        (:contains, :depends-on, ...) ingested through _ingest_transact.
        git_repo (tests/test_mcp_server.py:4165) has two linear commits, so
        the second commit produces exactly one :parent edge back to the
        first -- enough to prove the edge lands in the index."""
        import mcp_server
        import fact_index
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "parent", top_n=50, boost=2.0, historical_discount=1.0)
        parent_rows = [r for r in results if r[1] == ":parent"]
        assert parent_rows, "no :parent-attribute rows found in the fact index after ingestion"


class TestRunIngestionConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_run_matches_sequential_facts(
        self, tmp_path, git_repo_with_deps, monkeypatch
    ):
        """A run using the thread-pool pipeline must produce the exact same
        set of transacted triples, in the same commit order, AND the exact
        same set of durably persisted facts, as today's sequential loop —
        this is the core correctness guarantee for the producer/consumer
        split.

        Uses a real, file-backed MiniGrafDb per run (not the `real_db`
        in-memory fixture): `_run_ingestion` releases and reacquires its DB
        handle between every commit and again after the run completes, and
        `MiniGrafDb.open_in_memory()` hands back a brand-new, isolated store
        on every call. Under the in-memory fixture, those reopens would
        silently wipe state at each boundary, so querying "the graph" after
        the run would only ever reflect the last write in isolation — the
        comparison below would be vacuous. A real on-disk graph (same
        pattern as TestRunIngestionBitemporalClose's
        test_renamed_to_is_open_ended_against_real_graph) genuinely persists
        across those reopens, so the facts queried back after each run
        reflect that run's true cumulative state.
        """
        import mcp_server

        # Capture the true, unpatched transact function once — each run below
        # re-patches mcp_server._ingest_transact, so grabbing this later would
        # pick up the previous run's capture wrapper instead of the original.
        real_ingest_transact = mcp_server._ingest_transact

        async def run_and_capture(worker_count, graph_path):
            if worker_count is None:
                monkeypatch.delenv("MINIGRAF_INGEST_WORKERS", raising=False)
            else:
                monkeypatch.setenv("MINIGRAF_INGEST_WORKERS", str(worker_count))

            # Fresh, dedicated on-disk graph per run so each run starts from
            # a genuinely empty store — no leftover watermark, progress, or
            # shutdown signal from the previous run.
            mcp_server._db = None
            mcp_server._graph_path = None
            mcp_server.open_db(str(graph_path))
            mcp_server._ingest_progress = {
                "status": "idle", "processed": 0, "total": 0,
                "current_commit": "", "error": None,
            }
            mcp_server._shutdown_requested.clear()

            transacted: list = []

            def capture(db, triples, ts_iso, reason="", index_con=None):
                transacted.append(list(triples))
                return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

            monkeypatch.setattr(mcp_server, "_ingest_transact", capture)
            await mcp_server._run_ingestion(str(git_repo_with_deps), "HEAD")
            assert mcp_server._ingest_progress["status"] == "complete"
            assert mcp_server._ingest_progress["processed"] == 1

            # Query the real, persisted graph for every currently-valid fact.
            # The :last-run-at value is excluded: it legitimately carries a
            # wall-clock timestamp (see _last_run_write), so it differs
            # between the two runs below without indicating any divergence
            # in the actual ingested content. Everything else — including
            # that same entity's :ident/:last-commit/:total-ingested — is
            # deterministic given the same repo content and is compared.
            db = mcp_server.get_db()
            raw = mcp_server._db_execute(db, "(query [:find ?e ?a ?v :where [?e ?a ?v]])")
            rows = json.loads(raw)["results"]
            facts = {
                (e, a, v) for e, a, v in rows
                if a != ":last-run-at"
            }
            mcp_server._db = None  # release the file lock for the next run
            return transacted, facts

        sequential_triples, sequential_facts = await run_and_capture(1, tmp_path / "sequential.graph")
        concurrent_triples, concurrent_facts = await run_and_capture(4, tmp_path / "concurrent.graph")

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        mod_b_ident = mcp_server._code_ident("module", "mod_b.py")
        all_triples = [t for batch in sequential_triples for t in batch]
        assert any(mod_a_ident in t for t in all_triples)
        assert any(mod_b_ident in t for t in all_triples)
        assert any(a == ":ident" and v == mod_a_ident for e, a, v in sequential_facts)
        assert any(a == ":ident" and v == mod_b_ident for e, a, v in sequential_facts)

        # The core equivalence guarantee: identical triples emitted in
        # identical per-commit batches and identical order, AND identical
        # facts genuinely persisted to (and read back from) the graph —
        # regardless of how many worker threads did the extraction.
        assert concurrent_triples == sequential_triples
        assert concurrent_facts == sequential_facts

    @pytest.mark.asyncio
    async def test_worker_count_env_var_is_respected(self, real_db, git_repo, monkeypatch):
        monkeypatch.setenv("MINIGRAF_INGEST_WORKERS", "1")
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2

    @pytest.mark.asyncio
    async def test_one_commits_file_failure_does_not_affect_other_commits(
        self, real_db, git_repo, monkeypatch
    ):
        """A file-content fetch failure must be induced for real, not via
        monkeypatch: _extract_commit runs in a spawned worker process
        (#116), which re-imports mcp_server fresh and never sees a patch
        applied to this (parent) process's module object. Corrupting the
        actual loose git blob object for auth.py makes `git show
        <hash>:auth.py` genuinely fail inside the worker, exercising the
        same try/except continue path the old monkeypatch used to reach."""
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        failing_hash = commits[0][0]

        blob_sha = _subprocess.run(
            ["git", "rev-parse", f"{failing_hash}:auth.py"],
            cwd=git_repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        object_path = git_repo / ".git" / "objects" / blob_sha[:2] / blob_sha[2:]
        assert object_path.is_file(), "expected a loose object for a freshly committed blob"
        object_path.chmod(0o644)  # git writes loose objects read-only
        object_path.write_bytes(b"not a valid git object")

        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # Both commits still get counted as processed even though the first
        # commit's only changed file failed to fetch.
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2


class TestRunIngestionEventLoopResponsiveness:
    @pytest.mark.asyncio
    async def test_event_loop_stays_responsive_during_heavy_extraction(
        self, real_db, tmp_path, monkeypatch
    ):
        """#116: tree-sitter's C parse holds the GIL for its whole duration
        (confirmed empirically — a single hammering thread stalls a
        concurrent event loop's asyncio.sleep(0) ticks by tens of ms per
        tick vs sub-millisecond baseline). Extraction must therefore run in
        real OS processes, not GIL-sharing threads, so a heavy commit's
        parse work cannot starve the MCP server's event loop.

        Uses one giant function body (30k trivial statements) rather than
        many small functions: parse time scales with statement count
        (~500ms here) while the *extracted* output stays a single
        function entity (~1KB pickled) — this isolates "the parse itself
        holds the GIL for the whole commit" (the bug) from "deserializing
        a large extracted result briefly holds the GIL" (an unavoidable,
        output-sized, sub-commit-duration cost of any cross-process
        handoff). A thread pool would freeze the loop for close to the
        full ~500ms parse; a process pool's residual cost is bounded by
        the tiny output, not the parse time.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

        big_body = "".join(f"    x{i} = {i} + a * {i}\n" for i in range(30000))
        big_source = "def one_big_function(a):\n" + big_body + "    return x0\n"
        (repo / "big.py").write_text(big_source)
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add big file"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        gaps = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        hb_task = asyncio.ensure_future(heartbeat())
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task

        assert mcp_server._ingest_progress["status"] == "complete"
        max_gap = max(gaps) if gaps else 0.0
        assert max_gap < 0.2, (
            f"event loop tick blocked for {max_gap * 1000:.1f}ms during extraction "
            "(a GIL-bound thread pool would block for close to the full ~1s parse here)"
        )


class TestRunIngestionShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_mid_run_stops_at_commit_boundary(self, real_db, git_repo, monkeypatch):
        """git_repo has 2 commits. Request shutdown right after the first
        commit's extraction is consumed but before the second is processed;
        the loop must stop cleanly with status 'stopped' and only 1 commit
        durably processed."""
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        original_sleep = asyncio.sleep

        async def patched_sleep(t):
            # Fires after the first commit's processed += 1, i.e. exactly at
            # the next loop-top boundary check.
            mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", patched_sleep):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "stopped"
        assert mcp_server._ingest_progress["processed"] == 1

    @pytest.mark.asyncio
    async def test_resumes_from_watermark_after_shutdown(self, git_repo, monkeypatch):
        """After a simulated shutdown mid-run, a second _run_ingestion call
        against the same DB must pick up the watermark that was written for
        the last fully-completed commit and finish the remaining commit(s),
        without re-processing or skipping any.

        Uses a real, file-backed MiniGrafDb (not the `real_db` in-memory
        fixture, and not a MagicMock): the watermark written by run 1 must
        genuinely be visible to run 2's preload read, across the DB
        open/close cycles _run_ingestion performs both between commits and
        between the two separate _run_ingestion calls below. Neither a
        canned-response mock nor `MiniGrafDb.open_in_memory()` (which hands
        back a brand-new, isolated store on every open) can model that —
        only a real on-disk graph, reopened at the same path both times,
        actually persists the watermark for run 2 to read.
        """
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        original_sleep = asyncio.sleep
        stop_once = {"done": False}

        async def stop_after_first(t):
            if not stop_once["done"]:
                stop_once["done"] = True
                mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", stop_after_first):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "stopped"
        first_run_processed = mcp_server._ingest_progress["processed"]
        assert first_run_processed == 1

        # Prove the watermark and the first commit's entity were genuinely
        # persisted to disk before run 2 starts (not just left in the
        # in-process _ingest_progress counter).
        db = mcp_server.get_db()
        watermark = mcp_server._watermark_query(db)
        assert watermark, "run 1's watermark must be durably persisted for run 2 to resume from"
        assert mcp_server._count_commit_entities(db) == 1

        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        # Second run's preload now sees the 1 :type/commit entity genuinely
        # persisted by run 1 (a real DB, not a canned mock response), so
        # prior_ingested correctly seeds at 1; processed = prior_ingested (1)
        # + the 1 remaining commit this run itself completes = 2. This is
        # also proof the watermark actually gated re-processing: git_repo
        # has 2 commits total, and only 1 was processed in each run.
        assert mcp_server._ingest_progress["processed"] == 2
        assert mcp_server._count_commit_entities(mcp_server.get_db()) == 2

        mcp_server._db = None  # release the real file lock for subsequent tests


class TestMainShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_event_cancels_server_run_and_returns(self, monkeypatch):
        """A SIGTERM/SIGINT handler that only sets _shutdown_requested does
        nothing unless something is racing that event against server.run().
        This proves main() actually returns promptly once the event fires,
        instead of hanging forever waiting on a live (never-completing)
        connection."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_NO_AUTO_INGEST", "1")
        # A fresh Event (not just .clear()) — asyncio.Event binds itself to
        # whichever event loop first calls .wait() on it, and pytest-asyncio
        # gives each test function its own loop, so reusing the module-level
        # singleton's Event object across tests raises "bound to a different
        # event loop" on the second test that calls .wait() on it.
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None

        @contextlib.asynccontextmanager
        async def fake_stdio_server():
            yield (object(), object())

        monkeypatch.setattr(mcp_server, "stdio_server", fake_stdio_server)

        run_started = asyncio.Event()

        async def fake_run(read_stream, write_stream, init_opts):
            run_started.set()
            await asyncio.Event().wait()  # simulates a live connection that never completes on its own

        monkeypatch.setattr(mcp_server.server, "run", fake_run)

        main_task = asyncio.create_task(mcp_server.main())
        await run_started.wait()
        mcp_server._shutdown_requested.set()

        await asyncio.wait_for(main_task, timeout=2)

    @pytest.mark.asyncio
    async def test_server_run_completing_normally_returns_without_shutdown_event(self, monkeypatch):
        """Regression guard for the non-signal path: if server.run() finishes
        on its own (e.g. the client closes the connection cleanly), main()
        must still return promptly without needing _shutdown_requested set."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_NO_AUTO_INGEST", "1")
        # A fresh Event (not just .clear()) — asyncio.Event binds itself to
        # whichever event loop first calls .wait() on it, and pytest-asyncio
        # gives each test function its own loop, so reusing the module-level
        # singleton's Event object across tests raises "bound to a different
        # event loop" on the second test that calls .wait() on it.
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None

        @contextlib.asynccontextmanager
        async def fake_stdio_server():
            yield (object(), object())

        monkeypatch.setattr(mcp_server, "stdio_server", fake_stdio_server)

        async def fake_run(read_stream, write_stream, init_opts):
            return  # completes immediately, as if the client disconnected cleanly

        monkeypatch.setattr(mcp_server.server, "run", fake_run)

        main_task = asyncio.create_task(mcp_server.main())
        await asyncio.wait_for(main_task, timeout=2)


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
            # Wait for either shutdown or an event that never gets set.
            # When _shutdown_requested is set, this exits cleanly.
            done, _ = await asyncio.wait(
                {
                    asyncio.create_task(mcp_server._shutdown_requested.wait()),
                    asyncio.create_task(asyncio.Event().wait()),
                },
                return_when=asyncio.FIRST_COMPLETED
            )

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

    @pytest.mark.asyncio
    async def test_status_not_idle_immediately_after_auto_start(self, monkeypatch, tmp_path):
        """Regression test for #109: main() creates _ingest_task for the
        auto-started ingestion, then _run_ingestion's preload phase (a full
        re-scan that can take minutes on a large repo) runs before status
        ever leaves "idle" — so a caller polling minigraf_ingest_status
        right after startup sees "idle" while minigraf_ingest_git already
        rejects a start attempt with "already in progress", an actionable
        -but-confusing contradiction. _ingest_progress must reflect
        "in progress" the instant the task exists, not just once preload
        completes."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        async def fake_run_ingestion(repo_path, branch):
            # Never touches _ingest_progress — isolates what main() itself
            # sets before/at task creation from what _run_ingestion would
            # later set once its preload phase completes.
            await asyncio.wait(
                {
                    asyncio.create_task(mcp_server._shutdown_requested.wait()),
                    asyncio.create_task(asyncio.Event().wait()),
                },
                return_when=asyncio.FIRST_COMPLETED
            )

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
        assert mcp_server._ingest_progress["status"] == "starting"

        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)

    @pytest.mark.asyncio
    async def test_auto_ingest_resolves_branch_via_default_git_branch(self, monkeypatch, tmp_path):
        """#130: auto-start at server boot must resolve the branch through
        _default_git_branch instead of hardcoding "HEAD"."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)
        monkeypatch.setattr(mcp_server, "_default_git_branch", lambda repo_path: "resolved-default")

        captured = {}

        async def fake_run_ingestion(repo_path, branch):
            captured["branch"] = branch
            done, _ = await asyncio.wait(
                {
                    asyncio.create_task(mcp_server._shutdown_requested.wait()),
                    asyncio.create_task(asyncio.Event().wait()),
                },
                return_when=asyncio.FIRST_COMPLETED
            )

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

        assert captured["branch"] == "resolved-default"

        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)


class TestRunStartupBackfillDbLockRelease:
    @pytest.mark.asyncio
    async def test_releases_db_lock_after_triggered_rebuild(self, real_db):
        """Code-review finding on #147: every other _db-touching call site in
        this file releases the graph's file lock afterward (call_tool's
        finally, _run_ingestion's per-commit resets) so the prepare_hook
        subprocess can acquire it between turns. _run_startup_backfill must
        do the same -- otherwise the persistent server process holds the
        lock open indefinitely once eager backfill runs once, reproducing
        this issue's own failure mode (a hook can't get the lock in time) by
        a different mechanism than the one it fixes."""
        import mcp_server
        import fact_index
        import os

        index_path = fact_index.index_path_for(mcp_server._graph_path)
        if os.path.exists(index_path):
            os.remove(index_path)

        assert mcp_server._db is not None  # real_db fixture already opened it

        await mcp_server._run_startup_backfill()

        assert mcp_server._db is None

    @pytest.mark.asyncio
    async def test_releases_db_lock_even_when_no_rebuild_needed(self, real_db):
        """Same release discipline must hold on the no-op path (index already
        backfilled) -- matches call_tool's finally, which resets _db
        unconditionally after every tool call regardless of whether that
        specific call touched it."""
        import mcp_server
        import fact_index

        # Seed an already-complete index so needs_backfill() is False and
        # _rebuild_index_from_graph (hence get_db()) is never invoked.
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        fact_index.rebuild_index(index_path, [])
        assert fact_index.needs_backfill(index_path) is False

        assert mcp_server._db is not None  # real_db fixture already opened it

        await mcp_server._run_startup_backfill()

        assert mcp_server._db is None


class TestMainStartupBackfill:
    @pytest.mark.asyncio
    async def test_kicks_off_backfill_when_needed(self, monkeypatch, tmp_path):
        """#147: main() must eagerly check-and-run the fact-index backfill in
        a background task at startup, mirroring the auto-start-ingestion
        pattern, instead of leaving it to the first lazy
        handle_memory_prepare_turn call -- which can run inside a short-lived,
        5-second-timeout-bound UserPromptSubmit hook process and retry-storm
        on a large graph."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        async def fake_run_ingestion(repo_path, branch):
            await asyncio.wait(
                {
                    asyncio.create_task(mcp_server._shutdown_requested.wait()),
                    asyncio.create_task(asyncio.Event().wait()),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )

        monkeypatch.setattr(mcp_server, "_run_ingestion", fake_run_ingestion)
        monkeypatch.setattr(mcp_server.fact_index, "needs_backfill", lambda path: True)
        rebuild_called = asyncio.Event()

        def fake_rebuild():
            rebuild_called.set()

        monkeypatch.setattr(mcp_server, "_rebuild_index_from_graph", fake_rebuild)
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None
        mcp_server._backfill_task = None

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

        assert mcp_server._backfill_task is not None
        await asyncio.wait_for(rebuild_called.wait(), timeout=2)

        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)

    @pytest.mark.asyncio
    async def test_skips_rebuild_when_already_backfilled(self, monkeypatch, tmp_path):
        """No unnecessary full rescan when the index is already complete."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        async def fake_run_ingestion(repo_path, branch):
            await asyncio.wait(
                {
                    asyncio.create_task(mcp_server._shutdown_requested.wait()),
                    asyncio.create_task(asyncio.Event().wait()),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )

        monkeypatch.setattr(mcp_server, "_run_ingestion", fake_run_ingestion)
        needs_backfill_checked = asyncio.Event()

        def fake_needs_backfill(path):
            needs_backfill_checked.set()
            return False

        monkeypatch.setattr(mcp_server.fact_index, "needs_backfill", fake_needs_backfill)
        rebuild_called = False

        def fake_rebuild():
            nonlocal rebuild_called
            rebuild_called = True

        monkeypatch.setattr(mcp_server, "_rebuild_index_from_graph", fake_rebuild)
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None
        mcp_server._backfill_task = None

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
        await asyncio.wait_for(needs_backfill_checked.wait(), timeout=2)
        await asyncio.wait_for(mcp_server._backfill_task, timeout=2)

        assert rebuild_called is False

        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)

    @pytest.mark.asyncio
    async def test_skips_backfill_when_auto_ingest_disabled(self, monkeypatch, tmp_path):
        """MINIGRAF_NO_AUTO_INGEST=1 (used by eval sandboxes) must also
        suppress the eager backfill task, not just ingestion -- both are
        background writes to on-disk state that a deterministic sandbox
        wants to opt out of together."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        monkeypatch.setenv("MINIGRAF_NO_AUTO_INGEST", "1")
        checked = False

        def fake_needs_backfill(path):
            nonlocal checked
            checked = True
            return True

        monkeypatch.setattr(mcp_server.fact_index, "needs_backfill", fake_needs_backfill)
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None
        mcp_server._backfill_task = None

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

        assert mcp_server._backfill_task is None
        assert checked is False

        mcp_server._shutdown_requested.set()
        await asyncio.wait_for(main_task, timeout=2)


class TestOrphanWatchdog:
    @pytest.mark.asyncio
    async def test_sets_shutdown_when_ppid_changes(self, monkeypatch):
        """If our immediate supervisor dies without forwarding a signal or
        closing stdin, we get reparented (ppid changes, classically to 1 or
        the reaping init/systemd pid). The watchdog must notice on its next
        poll and set _shutdown_requested so main() exits via the same
        graceful path as a real SIGTERM."""
        import mcp_server

        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._launch_ppid = 12345
        monkeypatch.setattr(mcp_server, "_ORPHAN_CHECK_INTERVAL", 0.01)
        monkeypatch.setattr(mcp_server.os, "getppid", lambda: 99999)

        await asyncio.wait_for(mcp_server._orphan_watchdog(), timeout=2)

        assert mcp_server._shutdown_requested.is_set()

    @pytest.mark.asyncio
    async def test_does_not_set_shutdown_while_ppid_unchanged(self, monkeypatch):
        """No false positives: as long as our supervisor is still our
        parent, the watchdog must keep polling without ever requesting
        shutdown."""
        import mcp_server

        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._launch_ppid = 12345
        monkeypatch.setattr(mcp_server, "_ORPHAN_CHECK_INTERVAL", 0.01)
        monkeypatch.setattr(mcp_server.os, "getppid", lambda: 12345)

        watchdog_task = asyncio.ensure_future(mcp_server._orphan_watchdog())
        await asyncio.sleep(0.05)  # several poll intervals

        assert not mcp_server._shutdown_requested.is_set()
        assert not watchdog_task.done()

        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task

    @pytest.mark.asyncio
    async def test_main_exits_when_orphaned(self, monkeypatch):
        """End-to-end: main() must self-terminate via the orphan watchdog
        even when no signal ever arrives and stdin never sees EOF — the
        exact failure mode from #104 (uvx dies, server reparents to
        systemd --user, and just keeps running)."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_NO_AUTO_INGEST", "1")
        mcp_server._shutdown_requested = asyncio.Event()
        mcp_server._ingest_task = None
        monkeypatch.setattr(mcp_server, "_ORPHAN_CHECK_INTERVAL", 0.01)

        real_getppid = os.getppid()
        # First call (inside main(), recording _launch_ppid) returns the
        # real ppid; every call after that simulates reparenting to init.
        ppid_calls = {"n": 0}

        def fake_getppid():
            ppid_calls["n"] += 1
            return real_getppid if ppid_calls["n"] == 1 else 1

        monkeypatch.setattr(mcp_server.os, "getppid", fake_getppid)

        @contextlib.asynccontextmanager
        async def fake_stdio_server():
            yield (object(), object())

        monkeypatch.setattr(mcp_server, "stdio_server", fake_stdio_server)

        async def fake_run(read_stream, write_stream, init_opts):
            await asyncio.Event().wait()  # simulates a live connection that never completes on its own

        monkeypatch.setattr(mcp_server.server, "run", fake_run)

        main_task = asyncio.create_task(mcp_server.main())

        await asyncio.wait_for(main_task, timeout=2)


class TestHandleMemoryPrepareTurnFts5:
    def test_returns_ranked_context_for_matching_query(self, real_db):
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert "use redis for caching" in result

    def test_returns_empty_for_unmatched_query(self, real_db):
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        result = mcp_server.handle_memory_prepare_turn("elephants trombone")
        assert result == ""

    def test_returns_empty_string_on_a_totally_fresh_graph(self, real_db):
        import mcp_server
        result = mcp_server.handle_memory_prepare_turn("anything at all")
        assert result == ""

    def test_memory_facts_rank_above_non_memory_facts(self, real_db):
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching layer"] '
            '[:decision/use-redis :entity-type :type/decision] '
            '[:decision/use-redis :ident ":decision/use-redis"]]',
            reason="test",
        )
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:function/unrelated :name "use redis for caching layer somewhere else use redis for caching layer somewhere else"]'],
            "2026-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("use redis for caching layer")
        redis_pos = result.find(":decision/use-redis")
        other_pos = result.find(":function/unrelated")
        assert redis_pos != -1
        assert other_pos == -1 or redis_pos < other_pos

    def test_respects_scan_limit_env_var(self, real_db, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_PREPARE_SCAN_LIMIT", "2")
        for i in range(5):
            mcp_server.handle_minigraf_transact(
                f'[[:decision/x{i} :description "redis caching option {i}"]]', reason="test"
            )
        result = mcp_server.handle_memory_prepare_turn("redis caching")
        lines = [l for l in result.splitlines() if "|" in l]
        assert len(lines) <= 2

    def test_triggers_backfill_when_index_file_missing(self, real_db, tmp_path):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)
        result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert "use redis for caching" in result

    def test_backfill_recovers_facts_written_without_explicit_ident(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/prefer-sqlite :description "prefer sqlite over postgres for embedded use"]]',
            reason="test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)
        result = mcp_server.handle_memory_prepare_turn("prefer sqlite over postgres for embedded use")
        assert "prefer sqlite over postgres for embedded use" in result

    def test_backfill_preserves_all_facts_for_an_idented_entity(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching layer"] '
            '[:decision/use-redis :entity-type :type/decision] '
            '[:decision/use-redis :ident ":decision/use-redis"]]',
            reason="test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)
        result = mcp_server.handle_memory_prepare_turn("use redis for caching layer")
        assert "use redis for caching layer" in result
        assert result.count(":decision/use-redis") >= 1

    def test_write_race_backfill_regression(self, real_db):
        """THE regression test for the original bug: a fact pre-exists in
        the graph (seeded via a raw db.execute, bypassing the index choke
        point entirely -- simulating a pre-existing graph from before this
        feature, or before this fix, ever indexed it), then ONE choke-point
        write happens (creating the index file with only its own content),
        THEN handle_memory_prepare_turn must still recover the pre-existing
        fact. Must fail against the reactive file-existence check (proves
        this test catches the real bug), pass against needs_backfill()."""
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z"} '
            '[[:decision/pre-existing :description "a decision from before this feature shipped"]])'
        )
        # One choke-point write -- creates the index file via open_writer,
        # with only ITS OWN content, before any read has ever happened.
        mcp_server.handle_minigraf_transact(
            '[[:decision/unrelated :description "something else entirely"]]', reason="test"
        )
        result = mcp_server.handle_memory_prepare_turn("a decision from before this feature shipped")
        assert "a decision from before this feature shipped" in result

    def test_historical_fact_surfaces_as_labeled_entry_point(self, real_db):
        """The headline new behavior: a closed/removed entity's facts are
        still findable, labeled as historical with their validity window.

        Includes an explicit `:ident` companion triple, matching how every
        real git-ingested code entity is actually written (see
        mcp_server.py:5033's `[{module_ident} :ident "{module_ident}"]` for
        the production convention `_code_ident`-derived entities always
        follow) -- without it, this scenario forces a needs_backfill()
        rebuild (a fresh graph's index is never marked 'backfilled' by
        incremental writes alone, by design -- see
        test_write_race_backfill_regression) that rescans via a Datalog
        query returning minigraf's internal per-entity UUID for `?e`, not
        the keyword literal; recovering the keyword ident from that UUID
        requires this explicit `:ident` fact (confirmed empirically: a bare
        keyword entity reference resolves to a stable but opaque UUID with
        no reverse-lookup pseudo-attribute exposed by minigraf 1.2.1).
        """
        import mcp_server
        triples = [
            '[:module/old-cache :description "legacy caching layer using memcached"]',
            '[:module/old-cache :ident ":module/old-cache"]',
        ]
        mcp_server._ingest_transact(
            mcp_server.get_db(), triples, "2024-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            mcp_server.get_db(), triples,
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("legacy caching layer using memcached")
        assert ":module/old-cache" in result
        assert "2024-01-01" in result
        assert "2025-01-01" in result

    def test_current_fact_ranks_above_equally_matching_historical_fact(self, real_db):
        """See test_historical_fact_surfaces_as_labeled_entry_point's
        docstring for why an explicit `:ident` triple is required here."""
        import mcp_server
        old_triples = [
            '[:module/old-cache :description "shared caching layer text for ranking test"]',
            '[:module/old-cache :ident ":module/old-cache"]',
        ]
        mcp_server._ingest_transact(
            mcp_server.get_db(), old_triples, "2024-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            mcp_server.get_db(), old_triples,
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            [
                '[:module/new-cache :description "shared caching layer text for ranking test"]',
                '[:module/new-cache :ident ":module/new-cache"]',
            ],
            "2025-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("shared caching layer text for ranking test")
        old_pos = result.find(":module/old-cache")
        new_pos = result.find(":module/new-cache")
        assert new_pos != -1 and old_pos != -1
        assert new_pos < old_pos

    def test_respects_historical_discount_env_var(self, real_db, monkeypatch):
        """See test_historical_fact_surfaces_as_labeled_entry_point's
        docstring for why an explicit `:ident` triple is required here."""
        import mcp_server
        monkeypatch.setenv("MINIGRAF_HISTORICAL_DISCOUNT", "1.0")
        triples = [
            '[:module/old-cache :description "discount env var test text repeated repeated"]',
            '[:module/old-cache :ident ":module/old-cache"]',
        ]
        mcp_server._ingest_transact(
            mcp_server.get_db(), triples, "2024-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            mcp_server.get_db(), triples,
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        # With discount=1.0 (neutral), historical and current-equivalent
        # scoring collapses to pure relevance -- just confirm it still finds
        # the historical fact at all when the discount is disabled.
        result = mcp_server.handle_memory_prepare_turn("discount env var test text repeated repeated")
        assert ":module/old-cache" in result


class TestIndexCacheInvalidation:
    def test_successful_transact_triggers_invalidation(self, real_db):
        import mcp_server
        import fact_index
        # New behavior: transact populates the fact index directly (not via cache invalidation)
        mcp_server.handle_minigraf_transact(
            '[[:decision/test :description "test"]]', reason="test"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "test", top_n=10, boost=2.0, historical_discount=1.0)
        assert any(r[0] == ":decision/test" for r in results)

    def test_failed_transact_does_not_modify_fact_index(self, real_db):
        import mcp_server
        import fact_index
        bad_facts = '[[:decision/leaky :description "should not be indexed"]] ('
        result = mcp_server.handle_minigraf_transact(bad_facts, reason="test")
        assert result["ok"] is False
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        try:
            results = fact_index.query_facts(index_path, "leaky", top_n=10, boost=2.0, historical_discount=1.0)
        except sqlite3.OperationalError:
            # The failed transact never reached _index_write, so the index
            # file may not exist yet at all -- distinct from "exists but
            # empty". Either way, nothing was leaked into it.
            results = []
        assert results == []

    def test_successful_retract_triggers_invalidation(self, real_db):
        import mcp_server
        import fact_index
        # New behavior: retract removes from the fact index directly (not via cache invalidation)
        real_db.execute('(transact {} [[:decision/test :description "test"]])')
        mcp_server.handle_minigraf_retract(
            '[[:decision/test :description "test"]]', reason="cleanup"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "test", top_n=10, boost=2.0, historical_discount=1.0)
        assert results == []

    def test_failed_retract_does_not_modify_fact_index(self, real_db):
        import mcp_server
        import fact_index
        # Seed via the real handler (not a raw real_db.execute) so the fact is
        # genuinely present in the fact index before the failed retract -- this
        # is required for the assertion below to be capable of failing: if the
        # fact were never indexed to begin with, a no-op delete would pass this
        # test regardless of whether _index_write incorrectly ran before the
        # _db_execute call that fails.
        mcp_server.handle_minigraf_transact(
            '[[:decision/leaky :description "should not be indexed"]]', reason="setup"
        )
        bad_facts = '[[:decision/leaky :description "should not be indexed"]] ('
        result = mcp_server.handle_minigraf_retract(bad_facts, reason="cleanup")
        assert result["ok"] is False
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "leaky", top_n=10, boost=2.0, historical_discount=1.0)
        # The entry must still be present -- a failed retract must not remove
        # anything from the index.
        assert any(r[0] == ":decision/leaky" for r in results)

    def test_run_ingestion_leaves_ingested_facts_queryable_via_fact_index(self, real_db, git_repo):
        """Replaces the old IndexCache.invalidate() assertion (#118): the new
        design has no cache-invalidation step on completion at all --
        _run_ingestion writes into the persisted fact index incrementally
        throughout the run via index_con (see TestIngestTransactFactIndex),
        so there is no discrete "on completion" hook left to spy on. The
        meaningful post-completion guarantee is behavioral: once the run
        finishes, facts it ingested are actually queryable through the fact
        index, not that some particular internal method fired.
        """
        import mcp_server
        import fact_index
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        asyncio.run(mcp_server._run_ingestion(str(git_repo), "HEAD"))
        assert mcp_server._ingest_progress["status"] == "complete"
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "auth", top_n=10, boost=2.0, historical_discount=1.0)
        assert results, (
            "ingested commit/module facts must be queryable via the fact "
            "index once _run_ingestion completes"
        )


# ---------------------------------------------------------------------------
# Fixtures for bi-temporal close integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo_with_deletion(tmp_path):
    """Repo: commit 1 adds auth.py (def login), commit 2 deletes it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)

    # Sleep so the two commits land in different git-timestamp seconds (git's
    # committer time has 1s resolution): real-backend tests point-in-time
    # query :valid-at the add commit's timestamp to confirm the fact was open
    # right after creation, which requires add_ts != delete_ts.
    time.sleep(1.1)
    (repo / "auth.py").unlink()
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "delete auth"], cwd=repo, check=True, capture_output=True)

    return repo


@pytest.fixture
def git_repo_with_intra_file_deletion(tmp_path):
    """Repo: commit 1 adds auth.py (def login + def logout), commit 2 removes logout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "auth.py").write_text("def login(): pass\ndef logout(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)

    time.sleep(1.1)  # ensure distinct commit-timestamp seconds; see git_repo_with_deletion
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "remove logout"], cwd=repo, check=True, capture_output=True)

    return repo


@pytest.fixture
def git_repo_with_rename(tmp_path):
    """Repo: commit 1 adds old_auth.py (def login), commit 2 renames to new_auth.py."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "old_auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)

    time.sleep(1.1)  # ensure distinct commit-timestamp seconds; see git_repo_with_deletion
    _subprocess.run(["git", "mv", "old_auth.py", "new_auth.py"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "rename auth"], cwd=repo, check=True, capture_output=True)

    return repo


def _reused_path_repo(repo, variant):
    """Build a 4-commit repo that reuses path a.py after it is closed.

    commit1: add a.py with function f
    commit2: rename a.py -> b.py  (variant="rename")  OR  delete a.py (variant="delete")
    commit3: re-create a.py with a DIFFERENT function g (a "leave a shim" pattern)
    commit4: edit the shim

    Commit timestamps are pinned to distinct days so point-in-time queries can
    target the window a.py's original function f was closed for.
    """
    env_base = dict(os.environ)

    def _commit(msg, day):
        env = dict(env_base)
        ts = f"2020-01-0{day}T00:00:00Z"
        env["GIT_AUTHOR_DATE"] = ts
        env["GIT_COMMITTER_DATE"] = ts
        _subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True, env=env)

    repo.mkdir(parents=True, exist_ok=True)
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "a.py").write_text("def f():\n    return 1\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _commit("c1 add a.py f", 1)

    if variant == "rename":
        _subprocess.run(["git", "mv", "a.py", "b.py"], cwd=repo, check=True, capture_output=True)
    else:
        _subprocess.run(["git", "rm", "a.py"], cwd=repo, check=True, capture_output=True)
    _commit("c2 close a.py", 2)

    (repo / "a.py").write_text("def g():\n    return 2\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _commit("c3 re-add a.py g", 3)

    (repo / "a.py").write_text("def g():\n    return 3\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _commit("c4 edit a.py g", 4)
    return repo


class TestClosedEntityLifecyclePurge:
    """Real-backend regression tests for the closed-entity lifecycle purge.

    Before the purge fix, close sites in _run_ingestion left the entity's ident
    in the in-memory bookkeeping dicts (entity_valid_from / entity_descriptions /
    field_class_ident / file_entities). Once closes became genuinely real (commit
    1b2e262), reusing a closed path produced two corruptions:

      * ghost entity: a NEW entity re-created at a previously-closed ident took
        _build_code_triples' "already known" branch (its ident was still in
        entity_valid_from) and never re-asserted :ident/:description/:path, so it
        had no current :ident fact — invisible to nearly every query.
      * phantom resurrection: a stale ident lingering in file_entities got closed
        a SECOND time by a later removal diff, but with its ORIGINAL introduction
        timestamp, widening its valid window across the span it was closed for.
    """

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "",
                "error": None, "prior_ingested": 0}

    async def _ingest_and_open(self, repo, monkeypatch):
        import mcp_server
        graph = str(repo / "memory.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._db = None
        mcp_server._graph_path = graph
        mcp_server._ingest_progress = self._make_progress()
        await mcp_server._run_ingestion(str(repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress
        mcp_server._db = None  # release the lock so we can reopen for querying
        from minigraf import MiniGrafDb
        return MiniGrafDb.open(graph)

    @staticmethod
    def _results(db, datalog):
        return json.loads(db.execute(datalog))["results"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["rename", "delete"])
    async def test_reused_path_new_entity_is_not_a_ghost(self, tmp_path, monkeypatch, variant):
        """The module re-created at the reused path a.py must have a CURRENT
        :ident fact (join target for nearly every query)."""
        repo = _reused_path_repo(tmp_path / "repo", variant)
        db = await self._ingest_and_open(repo, monkeypatch)

        import mcp_server
        module_ident = mcp_server._code_ident("module", "a.py")
        g_ident = mcp_server._code_ident("function", "a.py", "g")

        module_now = self._results(db, f'(query [:find ?i :where [{module_ident} :ident ?i]])')
        assert module_now == [[module_ident]], \
            f"[{variant}] re-created module at reused path has no current :ident (ghost): {module_now}"

        # The re-created function g must also exist as a genuine current entity.
        g_now = self._results(db, f'(query [:find ?i :where [{g_ident} :ident ?i]])')
        assert g_now == [[g_ident]], f"[{variant}] re-created function g missing current :ident: {g_now}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["rename", "delete"])
    async def test_reused_path_no_phantom_resurrection_window(self, tmp_path, monkeypatch, variant):
        """The original function f (closed at commit2) must NOT be visible at any
        point-in-time between its close (commit2) and the final commit."""
        repo = _reused_path_repo(tmp_path / "repo", variant)
        db = await self._ingest_and_open(repo, monkeypatch)

        import mcp_server
        f_ident = mcp_server._code_ident("function", "a.py", "f")

        # f was introduced at commit1 (2020-01-01) and closed at commit2 (2020-01-02).
        # It must be visible strictly inside [c1, c2) and invisible at/after c2.
        visible_before = self._results(
            db, f'(query [:find ?i :valid-at "2020-01-01T12:00:00Z" :where [{f_ident} :ident ?i]])')
        assert visible_before == [[f_ident]], \
            f"[{variant}] f must remain visible within its true window [c1,c2): {visible_before}"

        for label, when in [("c2", "2020-01-02T00:00:00Z"),
                            ("between c2 and c4", "2020-01-03T00:00:00Z"),
                            ("c4", "2020-01-04T00:00:00Z")]:
            phantom = self._results(
                db, f'(query [:find ?i :valid-at "{when}" :where [{f_ident} :ident ?i]])')
            assert phantom == [], \
                f"[{variant}] f (closed at c2) phantom-resurrected at {label} ({when}): {phantom}"

        # And f must have no CURRENT :ident either.
        assert self._results(db, f'(query [:find ?i :where [{f_ident} :ident ?i]])') == [], \
            f"[{variant}] f (closed at c2) must not be visible at current time"


class TestRunIngestionBitemporalClose:
    """Integration tests verifying bi-temporal correctness of entity lifecycle handling."""

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_file_deletion_closes_with_real_description_not_empty_string(
        self, git_repo_with_deletion
    ):
        """Verified against the REAL minigraf backend: the deleted function's
        :description fact must be closed using its REAL value ("login"), not
        an empty-string placeholder. If the close path used the wrong (empty)
        value, the retract in `_ingest_close` would silently no-op (it is
        best-effort) and the real fact would leak open forever -- observable
        here as the fact still being visible in a current-time query after
        the deletion commit."""
        import mcp_server
        repo = git_repo_with_deletion
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            fn_ident = mcp_server._code_ident("function", "auth.py", "login")

            # While the file existed, the REAL description was open.
            open_desc = results(
                f'(query [:find ?d :valid-at "{add_ts}" :where [{fn_ident} :description ?d]])'
            )
            assert open_desc == [["login"]], \
                f"Expected the real description 'login' to be open after the add commit, got {open_desc}"

            # After deletion, the fact must be CLOSED -- an empty-string
            # placeholder bug would leave the real value visible forever
            # because the retract could never match it.
            current_desc = results(f"(query [:find ?d :where [{fn_ident} :description ?d]])")
            assert current_desc == [], \
                f"Deleted function's :description must be closed at current time, got {current_desc}"
        finally:
            mcp_server._db = None  # release the real file lock for subsequent tests

    @pytest.mark.asyncio
    async def test_file_deletion_close_includes_ident_and_contains_triples(
        self, git_repo_with_deletion
    ):
        """Verified against the REAL backend: deleting a file must close BOTH
        the function's own :ident fact AND the parent module's :contains
        edge pointing at it -- not just one or the other."""
        import mcp_server
        repo = git_repo_with_deletion
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            fn_ident = mcp_server._code_ident("function", "auth.py", "login")
            module_ident = mcp_server._code_ident("module", "auth.py")

            # Both facts were open right after creation.
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{fn_ident} :ident ?x]])'
            ) == [[fn_ident]]
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{module_ident} :contains ?x]])'
            ) == [[fn_ident]]

            # Both must be closed (invisible at current time) after deletion.
            assert results(f"(query [:find ?x :where [{fn_ident} :ident ?x]])") == [], \
                "Deleted function's :ident fact must be closed"
            assert results(f"(query [:find ?x :where [{module_ident} :contains ?x]])") == [], \
                "Deleted function's :contains edge from the module must be closed"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_intra_file_deletion_closes_removed_function(
        self, git_repo_with_intra_file_deletion
    ):
        """Verified against the REAL backend: removing one function from a
        file that still contains other functions must close only the
        removed function's entity, leaving the still-present sibling open."""
        import mcp_server
        repo = git_repo_with_intra_file_deletion
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            logout_ident = mcp_server._code_ident("function", "auth.py", "logout")
            login_ident = mcp_server._code_ident("function", "auth.py", "login")

            # Both were open right after the add commit.
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{logout_ident} :ident ?x]])'
            ) == [[logout_ident]]
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{login_ident} :ident ?x]])'
            ) == [[login_ident]]

            # logout() removed from the modified file must be closed now.
            assert results(f"(query [:find ?x :where [{logout_ident} :ident ?x]])") == [], \
                "logout() removed from modified file must trigger a close"
            # login() still present in the file must stay open.
            assert results(f"(query [:find ?x :where [{login_ident} :ident ?x]])") == [[login_ident]], \
                "login() still present in file must not be closed"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_renamed_file_links_old_and_new_via_rename_edges(
        self, git_repo_with_rename
    ):
        """Verified against the REAL backend: renaming a file must close the
        old module while still creating the new module's entities, and the
        :renamed-from/:renamed-to link must stay visible at current time --
        it must never get folded into the old module's closed window."""
        import mcp_server
        repo = git_repo_with_rename
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            old_module_ident = mcp_server._code_ident("module", "old_auth.py")
            new_module_ident = mcp_server._code_ident("module", "new_auth.py")
            new_fn_ident = mcp_server._code_ident("function", "new_auth.py", "login")

            # Old module was open right after creation.
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{old_module_ident} :ident ?x]])'
            ) == [[old_module_ident]]

            # Old module must be closed after the rename.
            assert results(f"(query [:find ?x :where [{old_module_ident} :ident ?x]])") == [], \
                "Old module entities must still be closed when file is renamed"

            # New module's function must be created and stay open (not closed).
            assert results(f"(query [:find ?x :where [{new_fn_ident} :ident ?x]])") == [[new_fn_ident]], \
                "New module's entities must still be created after file is renamed"

            # :renamed-to/:renamed-from must remain visible at current time
            # (Fix 1: open-ended, not folded into the closed window).
            assert results(
                f"(query [:find ?x :where [{old_module_ident} :renamed-to ?x]])"
            ) == [[new_module_ident]], \
                "Old module's :renamed-to must be transacted open-ended, not closed"
            assert results(
                f"(query [:find ?x :where [{new_module_ident} :renamed-from ?x]])"
            ) == [[old_module_ident]], \
                "New module's open triples must include :renamed-from pointing at the old ident"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_in_file_function_rename_links_via_rename_edges(
        self, tmp_path
    ):
        """Verified against the REAL backend, with a pass-through spy on
        `_db_execute` (still forwards every call to the real backend and
        lets ingestion persist) so we can count the actual retract commands
        issued, exactly like `test_renamed_to_is_open_ended_against_real_graph`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def oldName(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def newName(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename fn"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        real_execute = mcp_server._db_execute
        executed_cmds = []

        def spy(db, datalog):
            executed_cmds.append(datalog)
            return real_execute(db, datalog)

        mcp_server._db_execute = spy
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")
        finally:
            mcp_server._db_execute = real_execute

        try:
            old_fn_ident = mcp_server._code_ident("function", "auth.py", "oldName")
            new_fn_ident = mcp_server._code_ident("function", "auth.py", "newName")

            # Fix 1: :renamed-to is transacted open-ended, never carrying :valid-to.
            renamed_to_triple = f"{old_fn_ident} :renamed-to {new_fn_ident}"
            renamed_to_cmds = [c for c in executed_cmds if renamed_to_triple in c]
            assert renamed_to_cmds, ":renamed-to must be emitted against the real DB"
            for c in renamed_to_cmds:
                assert ":valid-to" not in c, ":renamed-to must never be closed with a :valid-to"

            # Fix 4: the old ident must be closed exactly ONCE (rename loop
            # only, not also a second time as a plain removal) -- observed as
            # exactly one retract command against its :ident fact.
            old_ident_retracts = [
                c for c in executed_cmds
                if c.strip().startswith("(retract") and f"{old_fn_ident} :ident" in c
            ]
            assert len(old_ident_retracts) == 1, \
                f"same-file rename should close old ident once, got {len(old_ident_retracts)}: {old_ident_retracts}"

            # Real graph reflects the rename: old closed, new open, links resolve.
            db = mcp_server.get_db()

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            assert results(f"(query [:find ?x :where [{old_fn_ident} :ident ?x]])") == [], \
                "Old function must be closed after in-file rename"
            assert results(f"(query [:find ?x :where [{new_fn_ident} :ident ?x]])") == [[new_fn_ident]], \
                "New function must be open after in-file rename"
            assert results(f"(query [:find ?x :where [{new_fn_ident} :renamed-from ?x]])") == [[old_fn_ident]]
            assert results(f"(query [:find ?x :where [{old_fn_ident} :renamed-to ?x]])") == [[new_fn_ident]]
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_global_rename_links_via_rename_edges_end_to_end(
        self, tmp_path
    ):
        """Verified against the REAL backend via a pass-through `_db_execute`
        spy, same pattern as `test_in_file_function_rename_links_via_rename_edges`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        # Value padded to a 13-digit literal (not a bare "12345") so the
        # assignment's normalized body clears _match_renamed_entities'
        # _MIN_MATCH_BODY_LEN=20 floor (Task 8) -- see
        # test_global_rename_produces_renamed_pair's identical note. A
        # shorter literal is silently dropped as a "trivial stub" before
        # matching, producing an empty renamed_pairs and no rename triples.
        (repo / "config.py").write_text("GLOBAL_X = 1234567890123\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "config.py").write_text("GLOBAL_Y = 1234567890123\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename global"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        real_execute = mcp_server._db_execute
        executed_cmds = []

        def spy(db, datalog):
            executed_cmds.append(datalog)
            return real_execute(db, datalog)

        mcp_server._db_execute = spy
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")
        finally:
            mcp_server._db_execute = real_execute

        try:
            old_ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
            new_ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_Y")

            # Fix 1: :renamed-to open-ended via transact, never with :valid-to.
            renamed_to_triple = f"{old_ident} :renamed-to {new_ident}"
            renamed_to_cmds = [c for c in executed_cmds if renamed_to_triple in c]
            assert renamed_to_cmds, ":renamed-to must be emitted against the real DB"
            for c in renamed_to_cmds:
                assert ":valid-to" not in c, ":renamed-to must never be closed with a :valid-to"

            # Fix 4: the renamed old global is closed once (rename loop), not twice.
            old_ident_retracts = [
                c for c in executed_cmds
                if c.strip().startswith("(retract") and f"{old_ident} :ident" in c
            ]
            assert len(old_ident_retracts) == 1, \
                f"same-file global rename should close old ident once, got {len(old_ident_retracts)}: {old_ident_retracts}"

            db = mcp_server.get_db()

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            assert results(f"(query [:find ?x :where [{old_ident} :ident ?x]])") == [], \
                "Old global must be closed after rename"
            assert results(f"(query [:find ?x :where [{new_ident} :ident ?x]])") == [[new_ident]], \
                "New global must be open after rename"
            assert results(f"(query [:find ?x :where [{new_ident} :renamed-from ?x]])") == [[old_ident]]
            assert results(f"(query [:find ?x :where [{old_ident} :renamed-to ?x]])") == [[new_ident]]
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_rename_to_unsupported_ext_closes_old_entities(
        self, tmp_path
    ):
        """Forward -M regression, end-to-end through _run_ingestion: renaming a
        tracked .py to an unsupported .txt must close the old module AND its
        child function/global via the synthetic-delete path. Pre-fix the whole
        "R" row was dropped in _extract_commit, so nothing was ever closed and
        the old entities leaked open forever.

        Verified against the REAL backend via a pass-through `_db_execute` spy
        (real persistence) plus real bi-temporal queries: open at the add
        commit, closed at current time."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("AUTH_KEY = 1234567890123\n\ndef login(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        time.sleep(1.1)  # ensure distinct commit-timestamp seconds; see git_repo_with_deletion
        _subprocess.run(["git", "mv", "auth.py", "auth.txt"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename to txt"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        real_execute = mcp_server._db_execute
        executed_cmds = []

        def spy(db, datalog):
            executed_cmds.append(datalog)
            return real_execute(db, datalog)

        mcp_server._db_execute = spy
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")
        finally:
            mcp_server._db_execute = real_execute

        try:
            module_ident = mcp_server._code_ident("module", "auth.py")
            fn_ident = mcp_server._code_ident("function", "auth.py", "login")
            var_ident = mcp_server._code_ident("variable", "auth.py", "AUTH_KEY")

            db = mcp_server.get_db()

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            for ident, label in (
                (module_ident, "module"), (fn_ident, "function"), (var_ident, "global"),
            ):
                assert results(
                    f'(query [:find ?x :valid-at "{add_ts}" :where [{ident} :ident ?x]])'
                ) == [[ident]], f"Old {label} must have been open right after the add commit"
                assert results(f"(query [:find ?x :where [{ident} :ident ?x]])") == [], \
                    f"Old {label} must be closed when its file is renamed to an unsupported extension"

            # No .txt module should ever be opened.
            txt_module_ident = mcp_server._code_ident("module", "auth.txt")
            assert not any(txt_module_ident in c for c in executed_cmds if c.strip().startswith("(transact")), \
                "No module entity should be created for the unsupported .txt path"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_rename_from_unsupported_ext_creates_no_phantom_module(
        self, tmp_path
    ):
        """Reverse -M regression, end-to-end: renaming an unsupported .txt into
        a tracked .py must NOT close a phantom old module (the .txt ident was
        never opened) and must NOT write a dangling :renamed-from edge. Pre-fix
        _run_ingestion's R-branch unconditionally closed :module/notes-txt and
        wrote a :renamed-from pointing at it.

        Verified against the REAL backend: since the .txt module ident was
        never a real entity, it must have NO :ident fact at any point in time
        -- not even right after the add commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "notes.txt").write_text("def login(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add txt"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "notes.txt", "notes.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename to py"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            old_module_ident = mcp_server._code_ident("module", "notes.txt")
            new_module_ident = mcp_server._code_ident("module", "notes.py")
            new_fn_ident = mcp_server._code_ident("function", "notes.py", "login")

            # No phantom old module: it must never have had an :ident fact,
            # not even right after the (never-ingested) add commit.
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{old_module_ident} :ident ?x]])'
            ) == [], "The never-opened .txt module must never have an :ident fact (no phantom)"
            assert results(f"(query [:find ?x :where [{old_module_ident} :ident ?x]])") == [], \
                "The never-opened .txt module must not be closed (no phantom)"

            # No dangling :renamed-from edge either way.
            assert results(
                f"(query [:find ?x :where [{new_module_ident} :renamed-from ?x]])"
            ) == [], "No :renamed-from edge should dangle at the never-opened .txt module"

            # The new .py path is still ingested normally.
            assert results(f"(query [:find ?x :where [{new_fn_ident} :ident ?x]])") == [[new_fn_ident]], \
                "The new .py file's entities must still be created as a plain add"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_renamed_to_is_open_ended_against_real_graph(self, tmp_path):
        """Fix 1, verified end-to-end against the REAL minigraf backend (no
        MiniGrafDb mock, no _ingest_close monkeypatch) — this is the un-mocked
        query test the review required to close the blind spot that let the
        :renamed-to valid-window bug ship.

        The essence of Fix 1 is the *valid-time window* of the emitted Datalog:
        :renamed-to is a brand-new fact that becomes true at the rename commit
        and must stay true forever after, so it must be transacted OPEN-ENDED
        (`{:valid-from <rename-commit>}`, no :valid-to), NOT folded into the old
        entity's closed window via _ingest_close (`retract` + re-transact with
        `:valid-to`). We assert exactly that by capturing the real Datalog
        commands executed against the live DB (a pass-through spy that still
        forwards every call to the real backend and lets ingestion persist),
        then also query the real graph to confirm both rename directions
        resolve.

        Point-in-time verification: we query the real graph with `:valid-at`
        at a timestamp BEFORE the rename commit and confirm :renamed-to /
        :renamed-from are NOT yet visible, then query at the rename commit
        timestamp and confirm they ARE visible. This is now expressible because
        the `transact` argument-order bug (options map was passed AFTER the
        fact vector, so minigraf ignored `:valid-from`/`:valid-to` and stamped
        every fact with wall-clock now) has been fixed — the options map now
        correctly precedes the fact vector, matching the documented grammar
        `(transact {:valid-from ...} [facts...])`. The earlier claim that the
        historical window "was not honoured by this build" was a symptom of
        that bug, not a real minigraf limitation.
        """
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_auth.py").write_text("def login(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_auth.py", "new_auth.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename auth"], cwd=repo, check=True, capture_output=True)

        rename_commit_ts = mcp_server._git_commits(str(repo), None)[1][1]

        # Real backend: real MiniGrafDb, real execute, real persistence.
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        real_execute = mcp_server._db_execute
        executed_cmds = []

        def spy(db, datalog):
            executed_cmds.append(datalog)
            return real_execute(db, datalog)

        mcp_server._db_execute = spy
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")
        finally:
            mcp_server._db_execute = real_execute

        old_module = mcp_server._code_ident("module", "old_auth.py")
        new_module = mcp_server._code_ident("module", "new_auth.py")
        renamed_to_triple = f"{old_module} :renamed-to {new_module}"

        cmds_with_renamed_to = [c for c in executed_cmds if renamed_to_triple in c]
        assert cmds_with_renamed_to, ":renamed-to must be emitted against the real DB"
        # It must NEVER be closed: no retract of it, and no transact carrying it
        # may set a :valid-to (that would make it a bounded historical fact).
        for c in cmds_with_renamed_to:
            assert not c.strip().startswith("(retract"), \
                ":renamed-to must not be retracted (it is open-ended, not closed)"
            assert ":valid-to" not in c, \
                ":renamed-to must be transacted open-ended, never with a :valid-to"
        # At least one open-ended transact introduces it at the RENAME commit.
        open_transacts = [
            c for c in cmds_with_renamed_to
            if c.strip().startswith("(transact") and f':valid-from "{rename_commit_ts}"' in c
        ]
        assert open_transacts, \
            ":renamed-to must be transacted open-ended with :valid-from = rename commit ts"

        # By contrast, the old module's own identity IS still closed (retract).
        assert any(
            c.strip().startswith("(retract") and f"{old_module} :ident" in c
            for c in executed_cmds
        ), "Old module's :ident must still be closed (retracted) on rename"

        # Query the real graph: both rename directions resolve after ingestion.
        db = mcp_server.get_db()
        rt = json.loads(real_execute(db, f"(query [:find ?x :where [{old_module} :renamed-to ?x]])")).get("results", [])
        rf = json.loads(real_execute(db, f"(query [:find ?x :where [{new_module} :renamed-from ?x]])")).get("results", [])
        assert rt == [[new_module]], f"forward :renamed-to must resolve to new module, got {rt}"
        assert rf == [[old_module]], f"reverse :renamed-from must resolve to old module, got {rf}"

        # REAL point-in-time verification (the check the review originally asked
        # for, now expressible after the transact argument-order fix): the rename
        # edges become true AT the rename commit, so a :valid-at BEFORE that
        # commit must NOT observe them, and a :valid-at at the rename commit must.
        rt_before = json.loads(real_execute(
            db, f'(query [:find ?x :valid-at "2000-01-01T00:00:00Z" :where [{old_module} :renamed-to ?x]])'
        )).get("results", [])
        rf_before = json.loads(real_execute(
            db, f'(query [:find ?x :valid-at "2000-01-01T00:00:00Z" :where [{new_module} :renamed-from ?x]])'
        )).get("results", [])
        assert rt_before == [], f":renamed-to must NOT be visible before the rename commit, got {rt_before}"
        assert rf_before == [], f":renamed-from must NOT be visible before the rename commit, got {rf_before}"

        rt_at = json.loads(real_execute(
            db, f'(query [:find ?x :valid-at "{rename_commit_ts}" :where [{old_module} :renamed-to ?x]])'
        )).get("results", [])
        rf_at = json.loads(real_execute(
            db, f'(query [:find ?x :valid-at "{rename_commit_ts}" :where [{new_module} :renamed-from ?x]])'
        )).get("results", [])
        assert rt_at == [[new_module]], f":renamed-to must be visible at the rename commit, got {rt_at}"
        assert rf_at == [[old_module]], f":renamed-from must be visible at the rename commit, got {rf_at}"

        mcp_server._db = None  # release the real file lock for subsequent tests

    @pytest.mark.asyncio
    async def test_removed_field_secondary_attrs_are_closed_against_real_graph(self, tmp_path):
        """Issue #134: closing a field must retract :static/:class/:file/:entity-type
        too, not just :ident/:description/:contains — otherwise a query that filters
        on one of those secondary attributes WITHOUT joining current :ident (a
        realistic "find all static fields" style query) still returns the field
        after it has been removed from the source.

        Verified against the REAL minigraf backend (no mock), counting rows for
        each secondary attribute before/after the removing commit. The owning
        class (Foo) and a sibling field (baz) are kept alive across the edit so
        this exercises the "M status removed_idents" close path specifically,
        with a real (non-dangling) :class edge in play.
        """
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "models.py").write_text("class Foo:\n    bar = 1\n    baz = 2\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "models.py").write_text("class Foo:\n    baz = 2\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "remove bar"], cwd=repo, check=True, capture_output=True)

        # Real backend: real MiniGrafDb, real execute, real persistence.
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        await mcp_server._run_ingestion(str(repo), "HEAD")

        db = mcp_server.get_db()
        real_execute = mcp_server._db_execute

        def count(attr, value):
            raw = real_execute(db, f"(query [:find ?e :where [?e {attr} {value}]])")
            return len(json.loads(raw).get("results", []))

        # Only baz should remain static/classed/filed/typed-as-field; bar's facts
        # must be closed, not left leaking open forever.
        assert count(":static", "true") == 1, \
            "removed field bar must not still satisfy [?e :static true] (issue #134)"
        assert count(":class", mcp_server._code_ident("class", "models.py", "Foo")) == 1, \
            "removed field bar's own :class edge must be closed, not just the class's :contains edge"
        assert count(":file", '"models.py"') == 2, \
            "removed field bar's :file must be closed (only class Foo + field baz should remain)"
        assert count(":entity-type", ":type/field") == 1, \
            "removed field bar's :entity-type must be closed so type-only queries don't resurrect it"

        mcp_server._db = None  # release the real file lock for subsequent tests

    @pytest.mark.asyncio
    async def test_unchanged_global_and_field_survive_unrelated_edit(
        self, tmp_path
    ):
        """Fix 2: an unchanged module global and class field must NOT be closed
        when a later commit only edits an unrelated function body. Pre-fix,
        current_extracted_idents omitted globals/fields, so every still-present
        global/field looked 'removed' on any M commit and was wrongly closed.

        Verified against the REAL backend: both facts must still be visible
        in a current-time query after the unrelated edit commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "x.py").write_text(
            "GLOBAL_CONF = 1234567890123\n\nclass C:\n    field_a = 1\n\ndef f():\n    return 1\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        # Second commit changes only f()'s body — global and field are untouched.
        (repo / "x.py").write_text(
            "GLOBAL_CONF = 1234567890123\n\nclass C:\n    field_a = 1\n\ndef f():\n    return 2\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "edit f body"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            gvar_ident = mcp_server._code_ident("variable", "x.py", "GLOBAL_CONF")
            field_ident = mcp_server._code_ident("field", "x.py", "C.field_a")

            # Both were open right after the add commit.
            assert results(f'(query [:find ?x :valid-at "{add_ts}" :where [{gvar_ident} :ident ?x]])') == [[gvar_ident]]
            assert results(f'(query [:find ?x :valid-at "{add_ts}" :where [{field_ident} :ident ?x]])') == [[field_ident]]

            # Both must STILL be open (not closed) after the unrelated edit.
            assert results(f"(query [:find ?x :where [{gvar_ident} :ident ?x]])") == [[gvar_ident]], \
                "Unchanged global must NOT be closed on an unrelated function edit"
            assert results(f"(query [:find ?x :where [{field_ident} :ident ?x]])") == [[field_ident]], \
                "Unchanged field must NOT be closed on an unrelated function edit"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_file_rename_closes_unmatched_child_and_dependency(
        self, tmp_path
    ):
        """Fix 3: when a file is renamed, child entities the matcher could not
        confirm a continuity edge for (e.g. short/ambiguous bodies) and the old
        path's :depends-on edges must still be closed as plain removals under
        the OLD path. Pre-fix only the old module was closed, leaking unmatched
        children and dependency edges open forever alongside the new file's.

        Verified against the REAL backend via real bi-temporal queries."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "dep.py").write_text("def helper():\n    return 1\n")
        # go() has a body below _MIN_MATCH_BODY_LEN, so the matcher leaves it
        # unmatched — the case Fix 3 must still close under the old path.
        (repo / "main.py").write_text("import dep\n\ndef go():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        time.sleep(1.1)  # ensure distinct commit-timestamp seconds; see git_repo_with_deletion
        _subprocess.run(["git", "mv", "main.py", "app.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename main to app"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            old_go = mcp_server._code_ident("function", "main.py", "go")
            old_module = mcp_server._code_ident("module", "main.py")
            dep_module = mcp_server._code_ident("module", "dep.py")
            new_module = mcp_server._code_ident("module", "app.py")

            # Unmatched old child was open right after add, then closed as a
            # PLAIN removal (no :renamed-to linkage) after the rename.
            assert results(f'(query [:find ?x :valid-at "{add_ts}" :where [{old_go} :ident ?x]])') == [[old_go]]
            assert results(f"(query [:find ?x :where [{old_go} :ident ?x]])") == [], \
                "Unmatched old child function must be closed under the old path"
            assert results(f"(query [:find ?x :where [{old_go} :renamed-to ?x]])") == [], \
                "Unmatched child has no continuity edge — must not get a :renamed-to"

            # Old path's surviving dependency edge is closed too.
            assert results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{old_module} :depends-on ?x]])'
            ) == [[dep_module]]
            assert results(f"(query [:find ?x :where [{old_module} :depends-on ?x]])") == [], \
                "Old path's :depends-on edge must be closed on file rename"

            # The new file is still ingested.
            assert results(f"(query [:find ?x :where [{new_module} :ident ?x]])") == [[new_module]], \
                "New renamed file's module must still be created"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_same_file_rename_closes_old_ident_exactly_once(
        self, tmp_path
    ):
        """Fix 4: an in-place rename must close the old ident exactly once (via
        the renamed_pairs loop, with :renamed-to linkage) — not also a second
        time as a plain removal from the M-status removal detector.

        Verified against the REAL backend, with a pass-through `_db_execute`
        spy so we can count the actual retract commands issued, same pattern
        as `test_in_file_function_rename_links_via_rename_edges`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def oldName(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def newName(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename fn"], cwd=repo, check=True, capture_output=True)

        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        real_execute = mcp_server._db_execute
        executed_cmds = []

        def spy(db, datalog):
            executed_cmds.append(datalog)
            return real_execute(db, datalog)

        mcp_server._db_execute = spy
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")
        finally:
            mcp_server._db_execute = real_execute

        try:
            old_fn = mcp_server._code_ident("function", "auth.py", "oldName")
            new_fn = mcp_server._code_ident("function", "auth.py", "newName")

            old_ident_retracts = [
                c for c in executed_cmds
                if c.strip().startswith("(retract") and f"{old_fn} :ident" in c
            ]
            assert len(old_ident_retracts) == 1, \
                f"same-file rename must close old ident exactly once, got {len(old_ident_retracts)}: {old_ident_retracts}"

            renamed_to_triple = f"{old_fn} :renamed-to {new_fn}"
            assert any(renamed_to_triple in c for c in executed_cmds), \
                "the single close path must be the rename path (with :renamed-to linkage)"

            db = mcp_server.get_db()

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            assert results(f"(query [:find ?x :where [{old_fn} :ident ?x]])") == [], \
                "Old function must be closed after same-file rename"
            assert results(f"(query [:find ?x :where [{new_fn} :ident ?x]])") == [[new_fn]], \
                "New function must be open after same-file rename"
        finally:
            mcp_server._db = None


# ---------------------------------------------------------------------------
# Fixtures for bi-temporal :depends-on integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo_with_deps(tmp_path):
    """Repo: commit 1 adds mod_a.py (imports mod_b) and mod_b.py."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "mod_b.py").write_text("def helper(): pass\n")
    (repo / "mod_a.py").write_text("import mod_b\n\ndef main(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add modules"], cwd=repo, check=True, capture_output=True)

    return repo


@pytest.fixture
def git_repo_with_dep_removal(tmp_path):
    """Repo: commit 1 adds mod_a.py (imports mod_b) + mod_b.py, commit 2 removes the import."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "mod_b.py").write_text("def helper(): pass\n")
    (repo / "mod_a.py").write_text("import mod_b\n\ndef main(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add modules with dep"], cwd=repo, check=True, capture_output=True)

    time.sleep(1.1)  # ensure distinct commit-timestamp seconds; see git_repo_with_deletion
    (repo / "mod_a.py").write_text("def main(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "remove import"], cwd=repo, check=True, capture_output=True)

    return repo


class TestTransactValidTimeArgumentOrder:
    """Regression tests for the transact argument-order bug.

    minigraf's documented grammar is `(transact {options} [facts...])` — the
    valid-time options map MUST precede the fact vector. Every valid-time-bounded
    transact in mcp_server.py historically emitted the reversed order
    `(transact [facts...] {options})`, which minigraf silently ignored: the fact
    was stamped with wall-clock now instead of the intended window. This meant a
    "closed" (bounded) fact stayed visible in current-time queries forever, and
    `:valid-at` point-in-time reads could never observe the intended window.

    These tests run against a REAL temp minigraf .graph file (no MiniGrafDb mock)
    and prove the fixed order behaves exactly as documented.
    """

    @pytest.mark.asyncio
    async def test_bounded_window_honoured_against_real_graph(self, tmp_path):
        """Transact a fact with a bounded historical valid window and confirm:
        (a) a default/current-time query does NOT see it,
        (b) a :valid-at WITHIN the window DOES see it,
        (c) a :valid-at BEFORE the window does NOT see it.
        """
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(tmp_path / "vt.graph"))
        db = mcp_server.get_db()

        try:
            # Bounded historical window: valid 2020-01-01 .. 2021-01-01 only.
            mcp_server._db_execute(
                db,
                '(transact {:valid-from "2020-01-01T00:00:00Z" '
                ':valid-to "2021-01-01T00:00:00Z"} [[:alice :employment/status :active]])',
            )

            def q(extra=""):
                raw = mcp_server._db_execute(
                    db, f"(query [:find ?s {extra} :where [:alice :employment/status ?s]])"
                )
                return json.loads(raw).get("results", [])

            # (a) current time is AFTER the closed window -> not visible.
            assert q() == [], "closed bounded fact must NOT appear in a current-time query"
            # (b) point-in-time inside the window -> visible.
            assert q(':valid-at "2020-06-01T00:00:00Z"') == [[":active"]], \
                "fact must be visible at a :valid-at inside its window"
            # (c) point-in-time before the window -> not visible.
            assert q(':valid-at "2019-06-01T00:00:00Z"') == [], \
                "fact must NOT be visible at a :valid-at before its window"
        finally:
            mcp_server._db = None  # release the real file lock for subsequent tests


class TestRunIngestionBitemporalDeps:
    """Tests verifying that :depends-on edges are written/closed bi-temporally in the commit loop."""

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_new_import_writes_depends_on_via_ingest_transact(
        self, git_repo_with_deps
    ):
        """Adding a file with an import must write a :depends-on triple whose
        :valid-from is the git commit timestamp.

        Verified against the REAL backend: the edge must be open both right
        after the add commit (:valid-at) and at current time."""
        import mcp_server
        repo = git_repo_with_deps
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
            # mod_b.py genuinely exists in file_entities, so the generalized
            # tiered matcher (Task 12) resolves "mod_b" to the real internal
            # module via the basename tier, instead of the old Rust-only fallback.
            mod_b_resolved = mcp_server._code_ident("module", "mod_b.py")

            at_add = results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{mod_a_ident} :depends-on ?x]])'
            )
            assert at_add == [[mod_b_resolved]], (
                f"Expected {mod_a_ident} :depends-on {mod_b_resolved} to be open "
                f"right after the add commit, got: {at_add}"
            )
            current = results(f"(query [:find ?x :where [{mod_a_ident} :depends-on ?x]])")
            assert current == [[mod_b_resolved]], (
                f"Expected the :depends-on edge to still be open at current time, got: {current}"
            )
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_removed_import_closes_depends_on_edge(
        self, git_repo_with_dep_removal
    ):
        """Removing an import in a modified file must close the :depends-on
        edge with a :valid-to bound.

        Verified against the REAL backend: the edge must be open at the add
        commit and closed (invisible) at current time, after the removal commit."""
        import mcp_server
        repo = git_repo_with_dep_removal
        commits = mcp_server._git_commits(str(repo), None)
        add_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
            mod_b_resolved = mcp_server._code_ident("module", "mod_b.py")

            at_add = results(
                f'(query [:find ?x :valid-at "{add_ts}" :where [{mod_a_ident} :depends-on ?x]])'
            )
            assert at_add == [[mod_b_resolved]], \
                f"Expected the :depends-on edge to be open right after the add commit, got: {at_add}"

            current = results(f"(query [:find ?x :where [{mod_a_ident} :depends-on ?x]])")
            assert current == [], (
                f"Expected the :depends-on edge to be closed (:valid-to bound) after the "
                f"import was removed, got: {current}"
            )
        finally:
            mcp_server._db = None


class TestUnresolvedImportTagging:
    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    def test_resolve_module_import_returns_bool_flag(self):
        import mcp_server
        file_entities = {"src/storage.rs": []}
        ident, is_resolved = mcp_server._resolve_module_import("storage", file_entities)
        assert is_resolved is True
        ident, is_resolved = mcp_server._resolve_module_import("totally_unknown_crate", file_entities)
        assert is_resolved is False

    @pytest.mark.asyncio
    async def test_unresolved_import_gets_tagged_external_dependency(self, tmp_path, monkeypatch):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.rs").write_text('use tokio;\nfn main() {}\n')
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add main"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            tokio_ident = mcp_server._canonical_ident("module", "tokio")
            assert any(f"[{tokio_ident} :entity-type :type/external-dependency]" in t for t in transact_calls)
            assert any(f'[{tokio_ident} :description "tokio"]' in t for t in transact_calls)

            # Verified against the REAL backend: the tag actually persisted,
            # not just that a matching triple string was captured.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            results = json.loads(
                real_execute(db, f"(query [:find ?x :where [{tokio_ident} :entity-type ?x]])")
            ).get("results", [])
            assert results == [[":type/external-dependency"]], \
                "tokio's external-dependency tag must be queryable against the real graph"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_unresolved_relative_import_not_tagged_external_end_to_end(self, tmp_path, monkeypatch):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.ts").write_text("import { thing } from './missing';\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add main"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            missing_ident = mcp_server._canonical_ident("module", "./missing")
            assert not any(
                f"[{missing_ident} :entity-type :type/external-dependency]" in t for t in transact_calls
            )

            # Verified against the REAL backend: nothing under the relative
            # import's ident is queryable as an external-dependency.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            results = json.loads(
                real_execute(db, f"(query [:find ?x :where [{missing_ident} :entity-type ?x]])")
            ).get("results", [])
            assert results == [], \
                "unresolved relative import must not be queryable as an entity at all"
        finally:
            mcp_server._db = None


class TestGitIngestionPathIgnore:
    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_default_ignored_directory_produces_no_code_entities(
        self, tmp_path, monkeypatch
    ):
        """A file under a default-ignored directory (vendor/) must not produce
        any :type/module, :type/function, or :type/class triples."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "lib.py").write_text("def vendored_fn(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add vendored lib"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            vendored_module_ident = mcp_server._code_ident("module", "vendor/lib.py")
            assert not any(vendored_module_ident in t for t in transact_calls)
            assert not any(":entity-type :type/function" in t for t in transact_calls)
            assert not any(":entity-type :type/class" in t for t in transact_calls)

            # Verified against the REAL backend: nothing under vendor/ is
            # queryable at all, not just that no matching triple was captured.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            module_results = json.loads(
                real_execute(db, f"(query [:find ?x :where [{vendored_module_ident} :ident ?x]])")
            ).get("results", [])
            assert module_results == [], "vendored module must not be queryable against the real graph"
            func_results = json.loads(
                real_execute(db, "(query [:find ?e :where [?e :entity-type :type/function]])")
            ).get("results", [])
            assert func_results == [], "no function entities should exist under the ignored vendor/ directory"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_import_into_ignored_path_becomes_external_dependency(
        self, tmp_path, monkeypatch
    ):
        """Before this feature, vendor/foo.py would resolve as a normal in-tree
        module (see _resolve_module_import's segment-suffix matcher) and
        main.py's import of it would create an internal :depends-on edge, not
        an external-dependency entity. Excluding vendor/ from known_files must
        make it fall through to the same fallback used for real external
        packages (see TestUnresolvedImportTagging)."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "foo.py").write_text("def helper(): pass\n")
        (repo / "main.py").write_text("import vendor.foo\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add vendor and main"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            external_ident = mcp_server._canonical_ident("module", "vendor.foo")
            assert any(
                f"[{external_ident} :entity-type :type/external-dependency]" in t for t in transact_calls
            )

            # Verified against the REAL backend: the external-dependency tag
            # actually persisted, not just that a matching triple was captured.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            results = json.loads(
                real_execute(db, f"(query [:find ?x :where [{external_ident} :entity-type ?x]])")
            ).get("results", [])
            assert results == [[":type/external-dependency"]], \
                "vendor.foo's external-dependency tag must be queryable against the real graph"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_env_var_ignore_pattern_excludes_custom_directory(
        self, tmp_path, monkeypatch
    ):
        """MINIGRAF_INGEST_IGNORE must add to the default ignore list, not
        replace it — a custom pattern not in the built-in defaults must still
        be honored."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "generated").mkdir()
        (repo / "generated" / "codegen.py").write_text("def generated_fn(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add generated file"], cwd=repo, check=True, capture_output=True)

        monkeypatch.setenv("MINIGRAF_INGEST_IGNORE", "generated/")
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            generated_module_ident = mcp_server._code_ident("module", "generated/codegen.py")
            assert not any(generated_module_ident in t for t in transact_calls)

            # Verified against the REAL backend: nothing under generated/ is
            # queryable at all, not just that no matching triple was captured.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            results = json.loads(
                real_execute(db, f"(query [:find ?x :where [{generated_module_ident} :ident ?x]])")
            ).get("results", [])
            assert results == [], "generated/ module must not be queryable against the real graph"
        finally:
            mcp_server._db = None


class TestResolveModuleImportTieredMatcher:
    def test_exact_file_match_java_package(self):
        import mcp_server
        file_entities = {"com/google/gson/Gson.java": []}
        ident, is_resolved = mcp_server._resolve_module_import("com.google.gson.Gson", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "com/google/gson/Gson.java")

    def test_parent_directory_match_java_wildcard_style(self):
        import mcp_server
        # "com.google.gson" (no trailing class name) is a package-level
        # reference — it matches via the file's *parent directory*, not the
        # file's own path, since there's no specific file named exactly that.
        file_entities = {"com/google/gson/JsonElement.java": []}
        ident, is_resolved = mcp_server._resolve_module_import("com.google.gson", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "com/google/gson/JsonElement.java")

    def test_genuinely_external_java_package_not_resolved(self):
        import mcp_server
        file_entities = {"com/mycompany/App.java": []}
        # com.fasterxml.jackson.Foo shares no path with the project's own "com" tree
        ident, is_resolved = mcp_server._resolve_module_import("com.fasterxml.jackson.Foo", file_entities)
        assert is_resolved is False

    def test_exact_file_match_go_full_path(self):
        import mcp_server
        # Vendored Go deps live under a vendor/ prefix that never appears in
        # the import string itself — this must match as a segment suffix,
        # not exact path equality, and "github.com" must survive as one path
        # segment rather than being split on its literal dot.
        file_entities = {"vendor/github.com/user/pkg/pkg.go": []}
        ident, is_resolved = mcp_server._resolve_module_import("github.com/user/pkg/pkg", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "vendor/github.com/user/pkg/pkg.go")

    def test_basename_match_vendored_c_header(self):
        import mcp_server
        file_entities = {"3rdParty/icu/include/unicode/uloc.h": []}
        ident, is_resolved = mcp_server._resolve_module_import("unicode/uloc", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "3rdParty/icu/include/unicode/uloc.h")

    def test_genuinely_external_c_stdlib_header_not_resolved(self):
        import mcp_server
        file_entities = {"src/main.cpp": []}
        ident, is_resolved = mcp_server._resolve_module_import("vector", file_entities)
        assert is_resolved is False


class TestSegmentSuffixIndex:
    """Reverse index used to speed up tiers 3a/3b of _resolve_module_import (#102)."""

    def test_match_file_finds_exact_match(self):
        import mcp_server
        index = mcp_server._SegmentSuffixIndex({"com/google/gson/Gson.java": []})
        assert index.match_file(["com", "google", "gson", "Gson"]) == "com/google/gson/Gson.java"

    def test_match_file_returns_none_when_no_file_shares_last_segment(self):
        import mcp_server
        index = mcp_server._SegmentSuffixIndex({"com/google/gson/Gson.java": []})
        assert index.match_file(["org", "other", "Widget"]) is None

    def test_match_file_ignores_decoy_sharing_last_segment_but_wrong_suffix(self):
        import mcp_server
        # Both files end in "Gson", so both land in the same bucket — only the
        # one whose full segment suffix matches the candidate should be returned.
        index = mcp_server._SegmentSuffixIndex({
            "com/google/gson/Gson.java": [],
            "org/other/nested/Gson.java": [],
        })
        assert index.match_file(["google", "gson", "Gson"]) == "com/google/gson/Gson.java"

    def test_match_parent_finds_wildcard_style_match(self):
        import mcp_server
        index = mcp_server._SegmentSuffixIndex({"com/google/gson/JsonElement.java": []})
        assert index.match_parent(["com", "google", "gson"]) == "com/google/gson/JsonElement.java"

    def test_match_parent_returns_none_when_no_match(self):
        import mcp_server
        index = mcp_server._SegmentSuffixIndex({"com/google/gson/JsonElement.java": []})
        assert index.match_parent(["org", "other"]) is None


class TestResolveModuleImportWithPrecomputedIndex:
    """A precomputed segment_index must resolve identically to the no-index default."""

    def test_precomputed_index_matches_default_for_tier_3a(self):
        import mcp_server
        file_entities = {"com/google/gson/Gson.java": []}
        index = mcp_server._SegmentSuffixIndex(file_entities)
        with_index = mcp_server._resolve_module_import(
            "com.google.gson.Gson", file_entities, segment_index=index,
        )
        without_index = mcp_server._resolve_module_import("com.google.gson.Gson", file_entities)
        assert with_index == without_index == (mcp_server._code_ident("module", "com/google/gson/Gson.java"), True)

    def test_precomputed_index_matches_default_for_tier_3b(self):
        import mcp_server
        file_entities = {"com/google/gson/JsonElement.java": []}
        index = mcp_server._SegmentSuffixIndex(file_entities)
        with_index = mcp_server._resolve_module_import(
            "com.google.gson", file_entities, segment_index=index,
        )
        without_index = mcp_server._resolve_module_import("com.google.gson", file_entities)
        assert with_index == without_index == (mcp_server._code_ident("module", "com/google/gson/JsonElement.java"), True)

    def test_precomputed_index_matches_default_for_unresolved_case(self):
        import mcp_server
        file_entities = {"com/mycompany/App.java": []}
        index = mcp_server._SegmentSuffixIndex(file_entities)
        with_index = mcp_server._resolve_module_import(
            "com.fasterxml.jackson.Foo", file_entities, segment_index=index,
        )
        without_index = mcp_server._resolve_module_import("com.fasterxml.jackson.Foo", file_entities)
        assert with_index == without_index == (mcp_server._canonical_ident("module", "com.fasterxml.jackson.Foo"), False)


@pytest.fixture
def git_repo_with_future_dep(tmp_path):
    """commit 1: mod_a.py imports mod_b, which does not exist yet.
    commit 2: mod_b.py is added.
    Resolving mod_a's import while processing commit 1 must reflect commit 1's
    OWN tree (mod_b.py doesn't exist there yet) — not HEAD's tree, where it does."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "mod_a.py").write_text("import mod_b\n\ndef main(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add mod_a importing not-yet-existing mod_b"], cwd=repo, check=True, capture_output=True)

    (repo / "mod_b.py").write_text("def helper(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add mod_b"], cwd=repo, check=True, capture_output=True)

    return repo


class TestPerCommitAccurateImportResolution:
    @pytest.mark.asyncio
    async def test_import_of_not_yet_existing_file_tagged_external_at_introduction(
        self, git_repo_with_future_dep, monkeypatch
    ):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_with_future_dep), None)
        commit1_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(git_repo_with_future_dep / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None,
        }

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(git_repo_with_future_dep), "HEAD")

            mod_b_external_ident = mcp_server._canonical_ident("module", "mod_b")
            assert any(
                f"[{mod_b_external_ident} :entity-type :type/external-dependency]" in t
                for t in transact_calls
            ), (
                "mod_b.py did not exist yet at commit 1, so resolving mod_a's import "
                "must use commit 1's own tree and tag it external — not silently "
                "resolve against HEAD's tree, where mod_b.py exists by the end."
            )

            # Verified against the REAL backend: at commit 1's own timestamp,
            # mod_b was actually queryable as an external-dependency, not just
            # that a matching triple string was captured.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            results = json.loads(
                real_execute(
                    db,
                    f'(query [:find ?x :valid-at "{commit1_ts}" '
                    f':where [{mod_b_external_ident} :entity-type ?x]])',
                )
            ).get("results", [])
            assert results == [[":type/external-dependency"]], \
                "mod_b must be queryable as external-dependency at commit 1's timestamp"
        finally:
            mcp_server._db = None


class TestResolveModuleImportRelative:
    def test_js_relative_import_resolves_against_importing_file(self):
        import mcp_server
        file_entities = {"src/utils/foo.ts": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "./utils/foo", file_entities, importing_file="src/main.ts",
        )
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "src/utils/foo.ts")

    def test_js_parent_relative_import_resolves(self):
        import mcp_server
        file_entities = {"lib.ts": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "../lib", file_entities, importing_file="src/main.ts",
        )
        assert is_resolved is True

    def test_python_single_dot_relative_import_resolves(self):
        import mcp_server
        file_entities = {"pkg/sibling.py": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            ".sibling", file_entities, importing_file="pkg/main.py",
        )
        assert is_resolved is True

    def test_python_double_dot_relative_import_resolves(self):
        import mcp_server
        # For a file at a/b/c/main.py, the containing package is a.b.c: one
        # leading dot means "this package" (a/b/c), two dots means "the
        # parent package" (a/b) — so "..sibling" resolves to a/b/sibling.py,
        # not a top-level sibling.py.
        file_entities = {"a/b/sibling.py": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "..sibling", file_entities, importing_file="a/b/c/main.py",
        )
        assert is_resolved is True

    def test_ruby_require_relative_marker_resolves(self):
        import mcp_server
        file_entities = {"lib/helper.rb": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "./helper", file_entities, importing_file="lib/main.rb",
        )
        assert is_resolved is True

    def test_unresolved_relative_import_is_not_tagged_external(self):
        import mcp_server
        file_entities: dict = {}
        ident, is_resolved = mcp_server._resolve_module_import(
            "./missing", file_entities, importing_file="src/main.ts",
        )
        assert is_resolved is False
        # Caller-side contract (see _run_ingestion): a relative import is only
        # ever tagged external if the generic (non-relative) tiers would also
        # tag it — this test documents that resolution itself still reports
        # is_resolved=False for a genuinely missing relative target, same as
        # any other unresolved import; the "don't mislabel" guarantee lives in
        # the caller, verified by Task 13's Step 5 integration test below.


class TestRunIngestionGitlinks:
    """End-to-end tests for submodule add/bump/remove/flip via _run_ingestion."""

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    def _add_submodule_commit(self, repo, path="vendor/lib", name="lib", url="https://example.com/lib.git"):
        sub = repo.parent / f"{repo.name}-sub"
        _subprocess.run(["git", "init", "-q", str(sub)], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "config", "user.email", "t@t.com"], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "config", "user.name", "T"], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "commit", "--allow-empty", "-m", "e"], check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "-C", str(sub), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        (repo / ".gitmodules").write_text(f'[submodule "{name}"]\n\tpath = {path}\n\turl = {url}\n')
        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},{path}"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "add", ".gitmodules"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)
        return sub_hash

    @pytest.mark.asyncio
    async def test_submodule_add_creates_external_dependency_entity(self, tmp_path, monkeypatch):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        sub_hash = self._add_submodule_commit(repo)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason="", index_con=None):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            ident = mcp_server._code_ident("module", "vendor/lib")
            assert any(f"[{ident} :entity-type :type/external-dependency]" in t for t in transact_calls)
            assert any(f'[{ident} :pinned-commit "{sub_hash}"]' in t for t in transact_calls)
            assert any(f'[{ident} :submodule-name "lib"]' in t for t in transact_calls)
            assert any(f'[{ident} :submodule-url "https://example.com/lib.git"]' in t for t in transact_calls)

            # Verified against the REAL backend, same count() pattern as
            # test_submodule_removal_closes_entity_type_and_path_against_real_graph.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def count(attr, value):
                raw = real_execute(db, f"(query [:find ?e :where [?e {attr} {value}]])")
                return len(json.loads(raw).get("results", []))

            assert count(":pinned-commit", f'"{sub_hash}"') == 1, \
                "submodule's pinned-commit must be queryable against the real graph"
            assert count(":submodule-name", '"lib"') == 1, \
                "submodule's name must be queryable against the real graph"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_submodule_bump_closes_old_pinned_commit(self, tmp_path, monkeypatch):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        first_sha = self._add_submodule_commit(repo)
        time.sleep(1.1)  # ensure distinct commit-timestamp seconds; see git_repo_with_deletion

        sub_dir = tmp_path / f"{repo.name}-sub"
        _subprocess.run(["git", "-C", str(sub_dir), "commit", "--allow-empty", "-m", "bump"], check=True, capture_output=True)
        second_sha = _subprocess.run(
            ["git", "-C", str(sub_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        _subprocess.run(
            ["git", "update-index", "--cacheinfo", f"160000,{second_sha},vendor/lib"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "commit", "-m", "bump submodule"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), None)
        first_commit_ts = commits[0][1]

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        real_ingest_close = mcp_server._ingest_close

        def capture_close(db, triples, orig_ts, commit_ts, reason="", index_con=None):
            close_triples_seen.extend(triples)
            return real_ingest_close(db, triples, orig_ts, commit_ts, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_close", capture_close)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            ident = mcp_server._code_ident("module", "vendor/lib")
            assert any(f'[{ident} :pinned-commit "{first_sha}"]' in t for t in close_triples_seen)

            # Verified against the REAL backend via real bi-temporal queries:
            # the old pinned-commit was open right after the add commit, then
            # closed (no longer current) after the bump — replaced by the new sha.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute

            def results(query):
                return json.loads(real_execute(db, query)).get("results", [])

            assert results(
                f'(query [:find ?x :valid-at "{first_commit_ts}" :where [{ident} :pinned-commit ?x]])'
            ) == [[first_sha]], "old pinned-commit must have been open right after the submodule add"
            assert results(
                f'(query [:find ?x :where [{ident} :pinned-commit "{first_sha}"]])'
            ) == [], "old pinned-commit must be closed (not current) after the bump"
            assert results(
                f"(query [:find ?x :where [{ident} :pinned-commit ?x]])"
            ) == [[second_sha]], "current pinned-commit must reflect the bumped submodule sha"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_submodule_removal_closes_entity(self, tmp_path, monkeypatch):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        self._add_submodule_commit(repo)

        _subprocess.run(["git", "rm", "-f", "vendor/lib"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "remove submodule"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        real_ingest_close = mcp_server._ingest_close

        def capture_close(db, triples, orig_ts, commit_ts, reason="", index_con=None):
            close_triples_seen.extend(triples)
            return real_ingest_close(db, triples, orig_ts, commit_ts, reason, index_con=index_con)

        monkeypatch.setattr(mcp_server, "_ingest_close", capture_close)
        try:
            await mcp_server._run_ingestion(str(repo), "HEAD")

            ident = mcp_server._code_ident("module", "vendor/lib")
            assert any(f'[{ident} :ident "{ident}"]' in t for t in close_triples_seen)

            # Verified against the REAL backend: the removed submodule's
            # :ident is actually closed, not just captured in a close triple.
            db = mcp_server.get_db()
            real_execute = mcp_server._db_execute
            results = json.loads(
                real_execute(db, f"(query [:find ?x :where [{ident} :ident ?x]])")
            ).get("results", [])
            assert results == [], \
                "removed submodule's :ident must be closed and no longer queryable against the real graph"
        finally:
            mcp_server._db = None

    @pytest.mark.asyncio
    async def test_submodule_removal_closes_entity_type_and_path_against_real_graph(self, tmp_path):
        """Issue #137: a removed submodule's :entity-type/:path must be closed
        too, not just :ident/:description (the pre-existing behavior verified
        by test_submodule_removal_closes_entity above via a mock). Pre-fix,
        [?e :entity-type :type/external-dependency] and [?e :path ?p] queries
        without an :ident join still returned the removed submodule forever,
        the same class of bug as #134 (see PR #136) but for the gitlink
        "remove" close site, which #136 deliberately left unfixed since
        deriving :entity-type from the ident's "module" prefix there would
        have transacted a false :type/module fact.

        Verified against the REAL minigraf backend, counting rows before/after
        removal (no mock — this is exactly the un-mocked query check #134's
        fix required).
        """
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        self._add_submodule_commit(repo)
        _subprocess.run(["git", "rm", "-f", "vendor/lib"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "remove submodule"], cwd=repo, check=True, capture_output=True)

        # Real backend: real MiniGrafDb, real execute, real persistence.
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))

        await mcp_server._run_ingestion(str(repo), "HEAD")

        db = mcp_server.get_db()
        real_execute = mcp_server._db_execute

        def count(attr, value):
            raw = real_execute(db, f"(query [:find ?e :where [?e {attr} {value}]])")
            return len(json.loads(raw).get("results", []))

        assert count(":entity-type", ":type/external-dependency") == 0, \
            "removed submodule's :entity-type must be closed (issue #137)"
        assert count(":path", '"vendor/lib"') == 0, \
            "removed submodule's :path must be closed (issue #137)"

        mcp_server._db = None  # release the real file lock for subsequent tests


class TestSubmoduleDependencyLinking:
    """Issue #112: a dependency-edge stub created from an unresolvable
    include path (e.g. #include "vendor/libX/api.h") and the submodule's own
    entity (from .gitmodules / gitlink mode 160000) are computed via two
    different ident schemes — _canonical_ident("module", import_name) for the
    stub vs. _code_ident("module", path) for the submodule — so they land as
    permanently disconnected idents even once both exist. A :resolves-to edge
    from the stub to the submodule must connect them.

    Verified against the REAL minigraf backend (no mock), since the fix reads
    back existing external-dependency rows via a DB query at gitlink-add time.
    """

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    def _add_submodule_commit(self, repo, path="vendor/libX", name="libX", url="https://example.com/libX.git"):
        sub = repo.parent / f"{repo.name}-sub"
        _subprocess.run(["git", "init", "-q", str(sub)], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "config", "user.email", "t@t.com"], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "config", "user.name", "T"], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "commit", "--allow-empty", "-m", "e"], check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "-C", str(sub), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        (repo / ".gitmodules").write_text(f'[submodule "{name}"]\n\tpath = {path}\n\turl = {url}\n')
        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},{path}"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "add", ".gitmodules"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)
        return sub_hash

    @pytest.mark.asyncio
    async def test_preexisting_stub_links_to_submodule_added_later(self, tmp_path):
        """Issue #112's exact repro order: the unresolvable include is ingested
        first (no submodule exists yet), then the real submodule is added and
        ingested incrementally. The stub from step one must end up with a
        :resolves-to edge to the submodule entity from step two."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "consumer.py").write_text("import vendor.libX.api\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add consumer"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        await mcp_server._run_ingestion(str(repo), "HEAD")

        self._add_submodule_commit(repo)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        db = mcp_server.get_db()
        real_execute = mcp_server._db_execute

        def rows(query):
            raw = real_execute(db, query)
            return json.loads(raw).get("results", [])

        stub_ident = mcp_server._canonical_ident("module", "vendor.libX.api")
        submodule_ident = mcp_server._code_ident("module", "vendor/libX")
        assert rows(f"(query [:find ?v :where [{stub_ident} :resolves-to ?v]])") == [[submodule_ident]], \
            "unresolved-include stub must gain a :resolves-to edge to the submodule entity (issue #112)"

        mcp_server._db = None  # release the real file lock for subsequent tests

    @pytest.mark.asyncio
    async def test_stub_created_after_submodule_links_immediately(self, tmp_path):
        """Reverse ordering: the submodule already exists, then a later commit
        adds an include reaching under it. Since submodule directories are
        never walked as tracked files, this import can never resolve — the fix
        must link the new stub the moment it's created, not just at the next
        gitlink event."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        self._add_submodule_commit(repo)

        (repo / "consumer.py").write_text("import vendor.libX.api\n")
        _subprocess.run(["git", "add", "consumer.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add consumer"], cwd=repo, check=True, capture_output=True)

        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server._ingest_progress = self._make_progress()
        mcp_server.open_db(str(repo / "memory.graph"))
        await mcp_server._run_ingestion(str(repo), "HEAD")

        db = mcp_server.get_db()
        real_execute = mcp_server._db_execute

        def rows(query):
            raw = real_execute(db, query)
            return json.loads(raw).get("results", [])

        stub_ident = mcp_server._canonical_ident("module", "vendor.libX.api")
        submodule_ident = mcp_server._code_ident("module", "vendor/libX")
        assert rows(f"(query [:find ?v :where [{stub_ident} :resolves-to ?v]])") == [[submodule_ident]], \
            "stub created after the submodule already exists must link immediately (issue #112)"

        mcp_server._db = None  # release the real file lock for subsequent tests


# ---------------------------------------------------------------------------
# Helpers for TestExtractImportName
# ---------------------------------------------------------------------------

def _find_node(root, node_type: str):
    """DFS search for the first node matching node_type."""
    if root.type == node_type:
        return root
    for child in root.children:
        found = _find_node(child, node_type)
        if found:
            return found
    return None


def _parse_import_node(lang_name: str, source: bytes, node_type: str, tmp_path):
    """Parse source for lang_name, return first node of node_type or skip."""
    import mcp_server
    ext = {
        "go": ".go", "java": ".java", "c": ".c", "cpp": ".cpp",
        "c_sharp": ".cs", "ruby": ".rb", "php": ".php", "kotlin": ".kt",
        "swift": ".swift", "scala": ".scala", "haskell": ".hs",
        "lua": ".lua", "elixir": ".ex",
    }[lang_name]
    tmp_file = tmp_path / f"test{ext}"
    tmp_file.write_bytes(source)
    parser = mcp_server._get_parser(str(tmp_file))
    if parser is None:
        pytest.skip(f"No tree-sitter parser available for {lang_name}")
    tree = parser.parse(source)
    node = _find_node(tree.root_node, node_type)
    if node is None:
        pytest.fail(
            f"No {node_type!r} node found in AST.\n"
            f"Full AST sexp:\n{tree.root_node.sexp()}"
        )
    return node


class TestExtractImportName:
    """Unit tests for _extract_import_name — one per language, using real parsers."""

    def test_go_single_import(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport "fmt"'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert result == ["fmt"]

    def test_go_grouped_import(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport (\n\t"os"\n\t"github.com/user/pkg"\n)'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert "os" in result
        assert "github.com/user/pkg" in result

    def test_python_import_from_preserves_full_dotted_name(self):
        import mcp_server
        source = b"from os.path import join\n"
        result = mcp_server._extract_from_source(
            source, TestExtractFromSource()._python_parser(), "foo.py"
        )
        assert "os.path" in result["imports"]

    def test_python_dotted_import_preserves_full_name(self):
        import mcp_server
        source = b"import os.path\n"
        result = mcp_server._extract_from_source(
            source, TestExtractFromSource()._python_parser(), "foo.py"
        )
        assert "os.path" in result["imports"]

    def test_java_import(self, tmp_path):
        pytest.importorskip("tree_sitter_java")
        import mcp_server
        source = b'import java.util.List;'
        node = _parse_import_node("java", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "java")
        assert result == ["java.util.List"]

    def test_c_system_include(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include <stdio.h>'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["stdio"]

    def test_c_local_include(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include "myheader.h"'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["myheader"]

    def test_cpp_include(self, tmp_path):
        pytest.importorskip("tree_sitter_cpp")
        import mcp_server
        source = b'#include <iostream>'
        node = _parse_import_node("cpp", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "cpp")
        assert result == ["iostream"]

    def test_csharp_using_simple(self, tmp_path):
        pytest.importorskip("tree_sitter_c_sharp")
        import mcp_server
        source = b'using System;'
        node = _parse_import_node("c_sharp", source, "using_directive", tmp_path)
        result = mcp_server._extract_import_name(node, "c_sharp")
        assert result == ["System"]

    def test_csharp_using_dotted(self, tmp_path):
        pytest.importorskip("tree_sitter_c_sharp")
        import mcp_server
        source = b'using System.Collections.Generic;'
        node = _parse_import_node("c_sharp", source, "using_directive", tmp_path)
        result = mcp_server._extract_import_name(node, "c_sharp")
        assert result == ["System.Collections.Generic"]

    def test_ruby_require(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require 'rails'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["rails"]

    def test_ruby_require_relative(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require_relative 'my_module'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["./my_module"]

    def test_ruby_non_require_call_ignored(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"puts 'hello'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == []

    def test_php_require(self, tmp_path):
        pytest.importorskip("tree_sitter_php")
        import mcp_server
        source = b"<?php\nrequire 'config.php';"
        node = _parse_import_node("php", source, "require_expression", tmp_path)
        result = mcp_server._extract_import_name(node, "php")
        assert result == ["config"]

    def test_php_include(self, tmp_path):
        pytest.importorskip("tree_sitter_php")
        import mcp_server
        source = b"<?php\ninclude 'header.php';"
        node = _parse_import_node("php", source, "include_expression", tmp_path)
        result = mcp_server._extract_import_name(node, "php")
        assert result == ["header"]

    def test_kotlin_import(self, tmp_path):
        pytest.importorskip("tree_sitter_kotlin")
        import mcp_server
        source = b'import kotlin.collections.List'
        node = _parse_import_node("kotlin", source, "import", tmp_path)
        result = mcp_server._extract_import_name(node, "kotlin")
        assert result == ["kotlin.collections.List"]

    def test_swift_import(self, tmp_path):
        pytest.importorskip("tree_sitter_swift")
        import mcp_server
        source = b'import Foundation'
        node = _parse_import_node("swift", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "swift")
        assert result == ["Foundation"]

    def test_swift_submodule_import_preserves_full_name(self, tmp_path):
        pytest.importorskip("tree_sitter_swift")
        import mcp_server
        source = b'import Foundation.NSString'
        node = _parse_import_node("swift", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "swift")
        assert result == ["Foundation.NSString"]

    def test_scala_import(self, tmp_path):
        pytest.importorskip("tree_sitter_scala")
        import mcp_server
        source = b'import scala.collection.mutable'
        node = _parse_import_node("scala", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "scala")
        assert result == ["scala.collection.mutable"]

    def test_haskell_import(self, tmp_path):
        pytest.importorskip("tree_sitter_haskell")
        import mcp_server
        source = b'import Data.List'
        node = _parse_import_node("haskell", source, "import", tmp_path)
        result = mcp_server._extract_import_name(node, "haskell")
        assert result == ["Data.List"]

    def test_lua_require(self, tmp_path):
        pytest.importorskip("tree_sitter_lua")
        import mcp_server
        source = b'require("socket")'
        node = _parse_import_node("lua", source, "function_call", tmp_path)
        result = mcp_server._extract_import_name(node, "lua")
        assert result == ["socket"]

    def test_lua_non_require_ignored(self, tmp_path):
        pytest.importorskip("tree_sitter_lua")
        import mcp_server
        source = b'print("hello")'
        node = _parse_import_node("lua", source, "function_call", tmp_path)
        result = mcp_server._extract_import_name(node, "lua")
        assert result == []

    def test_elixir_alias(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'alias MyApp.Router'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["MyApp.Router"]

    def test_elixir_import(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'import Ecto.Query'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["Ecto.Query"]

    def test_elixir_non_module_call_ignored(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        # A plain expression that's not alias/import/use
        source = b'IO.puts("hello")'
        # If this produces no "call" node, the test should just pass with []
        try:
            node = _parse_import_node("elixir", source, "call", tmp_path)
        except Exception:
            return  # no call node found — that's fine
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == []

    def test_c_local_include_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include "unicode/uloc.h"'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["unicode/uloc"]

    def test_c_angle_include_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include <sys/socket.h>'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["sys/socket"]

    def test_ruby_require_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require 'active_support/core_ext/string'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["active_support/core_ext/string"]

    def test_ruby_require_relative_gets_dot_slash_marker(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require_relative 'my_module'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["./my_module"]

    def test_php_require_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_php")
        import mcp_server
        source = b"<?php\nrequire 'app/config/database.php';"
        node = _parse_import_node("php", source, "require_expression", tmp_path)
        result = mcp_server._extract_import_name(node, "php")
        assert result == ["app/config/database"]

    def test_go_grouped_import_preserves_full_path(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport (\n\t"os"\n\t"github.com/user/pkg"\n)'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert "os" in result
        assert "github.com/user/pkg" in result


def test_schema_has_renamed_from_and_to_on_code_entities():
    import mcp_server
    for entity_type in ("module", "function", "class", "variable", "field"):
        optional = mcp_server.MINIGRAF_SCHEMA[entity_type]["optional"]
        assert optional[":renamed-from"] is str
        assert optional[":renamed-to"] is str


def test_schema_has_variable_and_field_types():
    import mcp_server
    assert mcp_server.MINIGRAF_SCHEMA["variable"]["required"][":description"] is str
    field_optional = mcp_server.MINIGRAF_SCHEMA["field"]["optional"]
    assert field_optional[":static"] is bool
    assert field_optional[":class"] is str


class TestFieldClassContainmentE2E:
    """Real-backend (non-mocked) end-to-end test: a class field is queryable via
    the class's :contains edge in the live graph, and that edge is closed when
    the field is removed (issues.md P2: "add a graph-level test asserting a
    class's :contains set includes its fields" + close-logic verification)."""

    def _init_repo(self, repo):
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def _commit(self, repo, msg):
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)

    def _query(self, path, q):
        from minigraf import MiniGrafDb
        db = MiniGrafDb.open(path)
        try:
            return json.loads(db.execute(q)).get("results", [])
        finally:
            del db

    def test_class_contains_field_is_queryable_and_closed_on_removal(self, tmp_path, monkeypatch):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        # Commit 1: a class with a field.
        (repo / "models.py").write_text("class Account:\n    balance = 0\n")
        self._commit(repo, "add Account")

        graph_path = str(tmp_path / "e2e.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph_path)
        mcp_server._db = None
        mcp_server._graph_path = ""

        asyncio.run(mcp_server._run_ingestion(str(repo), "HEAD"))
        mcp_server._db = None

        class_ident = mcp_server._code_ident("class", "models.py", "Account")
        field_ident = mcp_server._code_ident("field", "models.py", "Account.balance")

        # The class :contains set includes its field (structural graph traversal).
        contains = self._query(
            graph_path, f"(query [:find ?f :where [{class_ident} :contains ?f]])"
        )
        contained = {row[0] for row in contains}
        assert field_ident in contained, f"expected {field_ident} in class :contains {contained}"

        # Commit 2: remove the field (replace with a method) so it is a removal.
        (repo / "models.py").write_text("class Account:\n    def deposit(self):\n        pass\n")
        self._commit(repo, "drop balance field")

        mcp_server._db = None
        mcp_server._graph_path = ""
        asyncio.run(mcp_server._run_ingestion(str(repo), "HEAD"))
        mcp_server._db = None

        # The class-contains edge to the removed field is CLOSED at current time.
        contains_after = self._query(
            graph_path, f"(query [:find ?f :where [{class_ident} :contains ?f]])"
        )
        contained_after = {row[0] for row in contains_after}
        assert field_ident not in contained_after, (
            f"class-contains edge to removed field leaked open: {contained_after}"
        )

        # And the module-contains edge to the field is closed too (no half-close).
        module_ident = mcp_server._code_ident("module", "models.py")
        mod_contains_after = {
            row[0] for row in self._query(
                graph_path, f"(query [:find ?f :where [{module_ident} :contains ?f]])"
            )
        }
        assert field_ident not in mod_contains_after
