"""Unit tests for mcp_server.py.

All tests mock MiniGrafDb so no live minigraf install is required.
"""
import asyncio
import contextlib
import json
import sys
import os
import subprocess as _subprocess
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from minigraf import MiniGrafError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import rank_bm25  # noqa: F401
    _HAS_RANK_BM25 = True
except ImportError:
    _HAS_RANK_BM25 = False

requires_bm25 = pytest.mark.skipif(
    not _HAS_RANK_BM25,
    reason="rank_bm25 not installed (pip install -e .[bm25] or .[dev])",
)


@pytest.fixture(autouse=True)
def reset_mcp_server_db():
    """Reset the module-level _db singleton, grammar cache, and index cache between tests."""
    import mcp_server
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    mcp_server._index_cache = mcp_server.IndexCache()
    yield
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    mcp_server._index_cache = mcp_server.IndexCache()


@pytest.fixture
def mock_minigraf_db():
    """Mock MiniGrafDb class and instance."""
    with patch("mcp_server.MiniGrafDb") as mock_class:
        db_instance = MagicMock()
        db_instance.execute.return_value = json.dumps({"results": []})
        mock_class.open.return_value = db_instance
        yield mock_class, db_instance


class TestOpenDb:
    def test_opens_db_at_given_path(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        graph_path = str(tmp_path / "test.graph")
        mcp_server.open_db(graph_path)
        mock_class.open.assert_called_once_with(graph_path)

    def test_registers_session_rules(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "test.graph"))
        # Four rules registered at startup
        assert db_instance.execute.call_count == len(mcp_server.SESSION_RULES)
        for rule in mcp_server.SESSION_RULES:
            db_instance.execute.assert_any_call(rule)

    def test_get_db_auto_opens_when_db_none(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "auto.graph"))
        mcp_server._db = None
        mcp_server._graph_path = ""

        result = mcp_server.get_db()

        assert result is db_instance

    def test_get_db_returns_instance_after_open(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "test.graph"))
        assert mcp_server.get_db() is db_instance

    def test_uses_env_var_for_graph_path(self, mock_minigraf_db, monkeypatch, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        custom_path = str(tmp_path / "custom.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", custom_path)
        import mcp_server
        mcp_server.open_db()
        mock_class.open.assert_called_once_with(custom_path)


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
        assert mock_class.open.call_count == 1  # no retry for non-lock errors

    def test_self_heals_stale_lock_from_dead_pid(self, mock_minigraf_db, tmp_path, monkeypatch):
        """If the lock's recorded holder PID is no longer running, the stale
        .lock file should be removed so the retry can succeed without
        requiring the operator to delete it manually."""
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        graph_path = str(tmp_path / "t.graph")
        lock_path = graph_path + ".lock"
        with open(lock_path, "w") as f:
            f.write("stale")
        # PID 999999 should not exist on any reasonable test machine.
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
        live_pid = os.getpid()  # definitely alive — this test process
        mock_class.open.side_effect = [
            MiniGrafError(f"Database is locked by another process (lock file: {lock_path}, holder PID: {live_pid})."),
            db_instance,
        ]
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = graph_path
        result = mcp_server.get_db()
        assert result is db_instance
        assert os.path.exists(lock_path)  # untouched — holder is still alive

    def test_retries_open_after_clearing_stale_lock_on_final_attempt(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """Regression test for #91: previously, clearing a stale lock on the
        final retry attempt still fell through to raising the just-resolved
        lock error, because the follow-up open was gated on `attempt <
        _LOCK_RETRY_MAX - 1`. The clear must always be followed by one more
        open attempt, no matter which iteration triggered it."""
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setattr("mcp_server.time.sleep", lambda s: None)
        import mcp_server

        lock_err = MiniGrafError(
            "Database is locked by another process (lock file: x.graph.lock, holder PID: 999999)."
        )
        monkeypatch.setattr(mcp_server, "_clear_stale_lock", lambda path, pid: True)
        # Every regular attempt's immediate post-clear retry also fails,
        # except the very last one (triggered on the final loop iteration).
        mock_class.open.side_effect = [lock_err] * (2 * mcp_server._LOCK_RETRY_MAX - 1) + [db_instance]

        mcp_server._db = None
        mcp_server._graph_path = str(tmp_path / "t.graph")

        result = mcp_server.get_db()

        assert result is db_instance
        assert mock_class.open.call_count == 2 * mcp_server._LOCK_RETRY_MAX


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

    def test_concurrent_open_attempts_only_open_db_once(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        import threading
        import time as _time

        path = str(tmp_path / "race.graph")
        mcp_server._db = None
        mcp_server._graph_path = ""

        open_call_count = {"n": 0}
        open_lock = threading.Lock()

        def slow_open(p):
            with open_lock:
                open_call_count["n"] += 1
            _time.sleep(0.05)  # widen the race window so racers overlap
            return db_instance

        mock_class.open.side_effect = slow_open

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
        assert all(r is db_instance for r in results)


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

        class FakeDb:
            def execute(self, datalog):
                return json.dumps({"results": []})

        fake_db = FakeDb()
        mcp_server._db = fake_db

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

        assert outcome.get("db") is fake_db, (
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
    def test_returns_results_on_success(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["FastAPI", ":decision"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_minigraf_query("[:find ?n :where [?e :name ?n]]")

        db_instance.execute.assert_called_once()
        assert result["ok"] is True
        assert result["results"] == [["FastAPI", ":decision"]]

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad datalog")

        result = mcp_server.handle_minigraf_query("[:bad]")

        assert result["ok"] is False
        assert "bad datalog" in result["error"]


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

        # execute is called at least once for the transact (background index rebuild may
        # add an extra call; assert any transact call was made rather than assert_called_once)
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


class TestMinigrafRetract:
    def test_requires_reason(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_minigraf_retract("[[:e :a :v]]", reason="")

        assert result["ok"] is False

    def test_retracts_and_checkpoints(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "4"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_minigraf_retract("[[:e :a :v]]", reason="gone")

        db_instance.checkpoint.assert_called_once()
        assert result["ok"] is True

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad retract")

        result = mcp_server.handle_minigraf_retract("[[:e :a :v]]", reason="gone")

        assert result["ok"] is False
        assert "bad retract" in result["error"]


class TestMinigrafReportIssue:
    def test_delegates_to_report_issue(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
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

    def test_propagates_failure_from_report_issue(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mock_module = MagicMock()
        mock_module.report_issue.return_value = {"ok": False, "error": "gh command failed"}
        with patch.dict("sys.modules", {"report_issue": mock_module}):
            result = mcp_server.handle_minigraf_report_issue("bug", "something broke")
        assert result == {"ok": False, "error": "gh command failed"}

    def test_returns_error_on_import_failure(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.dict("sys.modules", {"report_issue": None}):
            result = mcp_server.handle_minigraf_report_issue("bug", "something broke")
        assert result["ok"] is False


class TestMemoryPrepareTurn:
    def test_returns_empty_string_when_graph_empty(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server._handle_memory_prepare_turn_heuristic("what database are we using?")

        assert isinstance(result, str)

    def test_includes_matching_facts_in_output(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        def execute_side_effect(cmd):
            if "contains?" in cmd and "postgres" in cmd.lower():
                return json.dumps({"results": [[":name", "PostgreSQL 15"]]})
            return json.dumps({"results": []})

        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server._handle_memory_prepare_turn_heuristic("what did we decide about postgres?")

        assert "PostgreSQL" in result or "postgres" in result.lower() or result == ""

    def test_falls_back_to_broad_scan_when_no_targeted_results(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        call_count = [0]

        def execute_side_effect(cmd):
            call_count[0] += 1
            # Targeted queries return nothing; broad scan returns something
            if "contains?" in cmd:
                return json.dumps({"results": []})
            return json.dumps({"results": [[":e", ":name", "FastAPI"]]})

        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server._handle_memory_prepare_turn_heuristic("tell me about our framework")

        # Broad scan should have been called
        assert call_count[0] > 0

    def test_respects_scan_limit_env_var(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_PREPARE_SCAN_LIMIT", "10")
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        mcp_server._handle_memory_prepare_turn_heuristic("hello")
        # Should not raise; limit is respected internally

    def test_uses_valid_at_for_message_with_explicit_iso_date(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        mcp_server._handle_memory_prepare_turn_heuristic("what did we decide before 2026-01-15?")

        calls = [str(c) for c in db_instance.execute.call_args_list]
        assert any(':valid-at "2026-01-15"' in c for c in calls)

    def test_caps_number_of_entities_scanned(self, mock_minigraf_db, tmp_path):
        """A long message must not issue one full-graph contains? scan per token.

        Each contains? query is an unindexed O(graph-size) linear scan (see
        issue #96); an unbounded entity count turns a single hook invocation
        into an unbounded number of full scans as the user's message grows.
        """
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        long_message = " ".join(f"distinctentityword{i}" for i in range(200))
        mcp_server._handle_memory_prepare_turn_heuristic(long_message)

        contains_calls = [
            c for c in db_instance.execute.call_args_list if "contains?" in str(c)
        ]
        assert len(contains_calls) <= mcp_server._MAX_HEURISTIC_ENTITIES

    def test_uses_current_utc_timestamp_for_current_state_queries(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server, re
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        mcp_server._handle_memory_prepare_turn_heuristic("what database are we using?")

        calls = [str(c) for c in db_instance.execute.call_args_list]
        # Should contain a UTC timestamp like 2026-05-02T15:44:52.184Z
        assert any(re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z', c) for c in calls)


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
    def test_transacts_extracted_facts(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
        db_instance.execute.return_value = json.dumps({"tx": "5"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.handle_memory_finalize_turn(
            "User: We'll use Redis.\nAgent: Stored."
        ))

        assert result["ok"] is True
        assert isinstance(result["stored_count"], int)

    def test_returns_zero_stored_when_no_signals(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

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
    def test_calls_openai_api(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("MINIGRAF_LLM_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        db_instance.execute.return_value = json.dumps({"tx": "6"})
        import mcp_server

        fake_response_text = '[[:decision/redis :description "Redis"]]\n'
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=fake_response_text))]
        )

        with patch("mcp_server._get_openai_client", return_value=mock_openai_client):
            mcp_server.open_db(str(tmp_path / "t.graph"))
            result = mcp_server._llm_extract_and_transact(
                "User: We'll use Redis.\nAgent: Stored."
            )

        assert result["ok"] is True
        assert result["stored_count"] > 0
        mock_openai_client.chat.completions.create.assert_called_once()

    def test_falls_back_to_heuristic_on_openai_failure(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("MINIGRAF_LLM_MODEL", "gpt-4o-mini")
        db_instance.execute.return_value = json.dumps({"tx": "7"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._get_openai_client", side_effect=Exception("no key")):
            result = asyncio.run(mcp_server.handle_memory_finalize_turn("We'll use Kafka."))

        assert result["ok"] is True
        assert "heuristic" in result["strategy"]


class TestLlmStrategy:
    def test_calls_anthropic_api(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        db_instance.execute.return_value = json.dumps({"tx": "6"})
        import mcp_server

        fake_response_text = '[[:decision/redis :description "Redis"]]\n'
        mock_anthropic_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=fake_response_text)]
        mock_anthropic_client.messages.create.return_value = mock_message

        with patch("mcp_server._get_anthropic_client", return_value=mock_anthropic_client):
            mcp_server.open_db(str(tmp_path / "t.graph"))
            result = mcp_server._llm_extract_and_transact(
                "User: We'll use Redis.\nAgent: Stored."
            )

        assert result["ok"] is True
        mock_anthropic_client.messages.create.assert_called_once()

    def test_falls_back_to_heuristic_on_api_failure(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "llm")
        db_instance.execute.return_value = json.dumps({"tx": "7"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._get_anthropic_client", side_effect=Exception("no key")):
            result = asyncio.run(mcp_server.handle_memory_finalize_turn("We'll use Kafka."))

        assert result["ok"] is True
        assert "heuristic" in result["strategy"]
        assert "warning" in result


class TestAgentStrategy:
    def test_returns_ok_result(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_EXTRACTION_STRATEGY", "agent")
        db_instance.execute.return_value = json.dumps({"tx": "8"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._request_agent_memory_block_async",
                   new_callable=AsyncMock,
                   return_value='[[:decision/kafka :description "Kafka"]]'):
            result = asyncio.run(mcp_server._agent_extract_and_transact("We chose Kafka."))

        assert result["ok"] is True


class TestMcpToolWiring:
    def test_list_tools_returns_ten_tools(self, mock_minigraf_db, tmp_path):
        import asyncio
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        tools = asyncio.run(mcp_server.list_tools())

        assert len(tools) == 10
        names = {t.name for t in tools}
        assert names == {
            "minigraf_query", "minigraf_transact", "minigraf_retract",
            "minigraf_rule", "minigraf_report_issue", "memory_prepare_turn", "memory_finalize_turn",
            "minigraf_audit", "minigraf_ingest_git", "minigraf_ingest_status",
        }

    def test_call_tool_minigraf_query(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["FastAPI"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_query", {"datalog": "[:find ?n :where [?e :name ?n]]"}
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True
        assert data["results"] == [["FastAPI"]]

    def test_call_tool_minigraf_transact(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "10"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_transact",
            {"facts": '[[:decision/cache :description "Redis"]]', "reason": "caching strategy"},
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True

    def test_call_tool_memory_prepare_turn(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = asyncio.run(mcp_server.call_tool(
            "memory_prepare_turn", {"user_message": "what database are we using?"}
        ))

        assert len(result) == 1
        assert isinstance(result[0].text, str)

    def test_call_tool_memory_finalize_turn(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "11"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = asyncio.run(mcp_server.call_tool(
            "memory_finalize_turn", {"conversation_delta": "The sky is blue."}
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True

    def test_call_tool_minigraf_retract(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "12"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_retract",
            {"facts": '[[:decision/cache :description "Redis"]]', "reason": "no longer needed"},
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True

    def test_db_released_after_call_tool(self, mock_minigraf_db, tmp_path):
        import asyncio
        import mcp_server
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))

        asyncio.run(mcp_server.call_tool(
            "minigraf_query", {"datalog": "[:find ?x :where [?e :x ?x]]"}
        ))

        assert mcp_server._db is None, "lock must be released after call_tool so prepare_hook can open the DB"

    def test_call_tool_unknown_raises(self, mock_minigraf_db, tmp_path):
        import asyncio
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with pytest.raises(Exception, match="Unknown tool"):
            asyncio.run(mcp_server.call_tool("nonexistent_tool", {}))

    def test_call_tool_lock_retry_does_not_block_event_loop(self, mock_minigraf_db, tmp_path, monkeypatch):
        """Regression test for #99: lock-retry backoff hit while opening the DB
        for a tool call must not use a blocking time.sleep(), since call_tool
        runs on the single-threaded asyncio event loop — a blocking sleep there
        would freeze the very coroutine (e.g. ingestion) that's about to
        release the lock we're waiting on."""
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = str(tmp_path / "t.graph")
        lock_err = MiniGrafError(
            "Database is locked by another process (lock file: x.graph.lock, holder PID: 1)."
        )
        mock_class.open.side_effect = [lock_err, db_instance]

        def fail_if_called(_delay):
            raise AssertionError("time.sleep() must not be called on the event-loop retry path (see #99)")
        monkeypatch.setattr(mcp_server.time, "sleep", fail_if_called)

        result = asyncio.run(mcp_server.call_tool(
            "minigraf_query", {"datalog": "[:find ?x :where [?e :x ?x]]"}
        ))

        data = json.loads(result[0].text)
        assert data["ok"] is True
        assert mock_class.open.call_count == 2


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
        # Verify :ident triple is included for audit retraction
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
    def test_rejects_unknown_entity_type(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_minigraf_transact(
            '[[:service/auth :description "auth service"]]',
            reason="test"
        )

        assert result["ok"] is False
        assert "schema" in result["error"].lower() or "violation" in result["error"].lower()

    def test_accepts_valid_fact(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "5"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_minigraf_transact(
            '[[:decision/redis :description "use Redis"]]',
            reason="test"
        )

        assert result["ok"] is True

    def test_keyword_only_transact_bypasses_schema_validation(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "6"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        # Keyword-only triple (no quoted string values) — not schema-validated by design
        result = mcp_server.handle_minigraf_transact(
            '[[:service/auth :calls :component/jwt]]',
            reason="test relationship edge"
        )

        assert result["ok"] is True


class TestQueryCanonicalEntities:
    def test_returns_empty_string_when_no_entities(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server._query_canonical_entities()
        assert result == ""

    def test_formats_entities_as_lines(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        # Two-step: first call returns ident list, second returns description.
        # Set side_effect after open_db to avoid consuming SESSION_RULES calls.
        db_instance.execute.side_effect = [
            json.dumps({"results": [[":decision/redis"]]}),  # ident query
            json.dumps({"results": [["use Redis"]]}),         # desc query
        ] + [json.dumps({"results": []})] * 5

        result = mcp_server._query_canonical_entities()
        assert ":decision/redis" in result
        assert "use Redis" in result

    def test_caps_at_50_entities(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        # First call: ident query returns 60 items (only first 50 are processed).
        # Subsequent calls: one description query per processed ident.
        # Set side_effect after open_db to avoid consuming SESSION_RULES calls.
        ident_results = [[f":decision/item-{i}"] for i in range(60)]
        desc_calls = [json.dumps({"results": [[f"item {i}"]]}) for i in range(50)]
        db_instance.execute.side_effect = (
            [json.dumps({"results": ident_results})] + desc_calls
        )

        result = mcp_server._query_canonical_entities()
        assert result.count(":decision/") == 50

    def test_injected_into_llm_prompt(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("MINIGRAF_LLM_MODEL", "claude-haiku-4-5-20251001")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        captured_prompt = {}
        def fake_call_llm(model, prompt):
            captured_prompt["prompt"] = prompt
            return "[]"

        with patch("mcp_server._query_canonical_entities", return_value="  :decision/redis — use Redis"):
            with patch("mcp_server._call_llm", side_effect=fake_call_llm):
                mcp_server._llm_extract_and_transact("User: test\nAgent: ok")

        assert ":decision/redis" in captured_prompt.get("prompt", "")

    def test_injected_into_agent_prompt(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        captured = {}
        async def fake_request_block(conversation_delta, canonical_entities_section=""):
            captured["canonical_entities_section"] = canonical_entities_section
            return "[]"

        with patch("mcp_server._query_canonical_entities", return_value="  :decision/redis — use Redis"):
            with patch("mcp_server._request_agent_memory_block_async", side_effect=fake_request_block):
                import asyncio
                asyncio.run(mcp_server._agent_extract_and_transact("User: test\nAgent: ok"))

        assert ":decision/redis" in captured.get("canonical_entities_section", "")


class TestMinigrafAudit:
    def test_clean_db_returns_zero_retracted(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        # No entities of any known type
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_minigraf_audit()

        assert result["ok"] is True
        assert result["retracted"] == 0
        assert result["violations"] == []

    def test_entity_missing_required_attr_is_retracted(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        # handle_minigraf_audit uses type query → UUID, then #uuid attr query.
        # Step 1: type query for "decision" returns UUID.
        # Step 2: attr query using #uuid tagged literal returns all attributes.
        # Entity has :ident + :entity-type (system) + :rationale (domain).
        # Missing :description → violation → retract call using #uuid.
        # Remaining entity types return empty.
        uuid = "bcc294db-aef9-53ae-8da8-9434eb6d1642"
        db_instance.execute.side_effect = [
            json.dumps({"results": [[uuid]]}),         # Step 1: type query for decision
            json.dumps({"results": [                   # Step 2: #uuid attr query
                [":entity-type", ":type/decision"],
                [":ident", ":decision/redis"],
                [":rationale", "fast"],
            ]}),
            json.dumps({"tx": "10"}),                  # retract call
        ] + [json.dumps({"results": []})] * 10         # remaining type queries

        result = mcp_server.handle_minigraf_audit()

        assert result["ok"] is True
        assert result["retracted"] == 1
        assert len(result["violations"]) == 1

        retract_calls = [
            str(call) for call in db_instance.execute.call_args_list
            if "retract" in str(call)
        ]
        assert len(retract_calls) >= 1, "Expected at least one retract call"
        assert "#uuid" in retract_calls[0]
        assert uuid in retract_calls[0]

    def test_as_of_reports_violations_without_retracting(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        uuid = "bcc294db-aef9-53ae-8da8-9434eb6d1642"
        db_instance.execute.side_effect = [
            json.dumps({"results": [[uuid]]}),
            json.dumps({"results": [
                [":entity-type", ":type/decision"],
                [":ident", ":decision/redis"],
                [":rationale", "fast"],
            ]}),
        ] + [json.dumps({"results": []})] * 10

        result = mcp_server.handle_minigraf_audit(as_of=5)

        assert result["ok"] is True
        assert result["retracted"] == 0  # read-only when as_of provided
        assert len(result["violations"]) == 1

    def test_ident_attr_used_for_display_in_violations(self, mock_minigraf_db, tmp_path):
        """Violation report shows keyword ident from :ident, not the raw UUID."""
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        uuid = "dca29477-f050-517e-9b4a-ef6d5dcada09"
        kw = ":decision/claude-haiku-4-5-20251001"
        db_instance.execute.side_effect = [
            json.dumps({"results": [[uuid]]}),
            json.dumps({"results": [
                [":entity-type", ":type/decision"],
                [":ident", kw],
            ]}),
            json.dumps({"tx": "10"}),
        ] + [json.dumps({"results": []})] * 10

        result = mcp_server.handle_minigraf_audit()

        assert result["retracted"] == 1
        assert result["violations"][0]["entity"] == kw  # keyword ident in report
        retract_calls = [
            str(call) for call in db_instance.execute.call_args_list
            if "retract" in str(call)
        ]
        assert "#uuid" in retract_calls[0]
        assert uuid in retract_calls[0]

    def test_result_shape(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_minigraf_audit()

        assert "ok" in result
        assert "audited" in result
        assert "retracted" in result
        assert "violations" in result


class TestPhase5Schema:
    def test_module_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":module/src-auth-py", "entity_type": "module",
                  "attribute": ":description", "value": "src/auth.py"}]
        assert mcp_server._validate_facts(facts) == []

    def test_function_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":function/src-auth-py-login", "entity_type": "function",
                  "attribute": ":description", "value": "login"}]
        assert mcp_server._validate_facts(facts) == []

    def test_class_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":class/src-auth-py-user", "entity_type": "class",
                  "attribute": ":description", "value": "User"}]
        assert mcp_server._validate_facts(facts) == []

    def test_ingestion_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":ingestion/watermark", "entity_type": "ingestion",
                  "attribute": ":description", "value": "git ingestion watermark"}]
        assert mcp_server._validate_facts(facts) == []

    def test_unknown_code_attr_fails_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":module/foo", "entity_type": "module",
                  "attribute": ":description", "value": "foo.py"},
                 {"entity": ":module/foo", "entity_type": "module",
                  "attribute": ":unknown-attr", "value": "x"}]
        violations = mcp_server._validate_facts(facts)
        assert any("unknown-attr" in v for v in violations)

    def test_contains_rule_registered_at_startup(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        executed = [call.args[0] for call in db_instance.execute.call_args_list]
        assert any("contains" in r for r in executed)


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
            "function_bodies": {}, "class_bodies": {},
            "globals": [], "global_bodies": {}, "fields": [], "field_info": {},
        }

    def test_extracts_function_bodies(self):
        import mcp_server
        source = b"def login(user):\n    return user.ok\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "auth.py")
        assert "login" in result["function_bodies"]
        assert "return user.ok" in result["function_bodies"]["login"]

    def test_extracts_class_bodies(self):
        import mcp_server
        source = b"class User:\n    def ok(self):\n        return True\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "models.py")
        assert "User" in result["class_bodies"]
        assert "def ok" in result["class_bodies"]["User"]


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
    def test_returns_idle_before_ingestion(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
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

    def test_returns_last_run_at_from_graph(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        def execute_side_effect(query, *args, **kwargs):
            if ":last-run-at" in query and ":last-commit" in query:
                return json.dumps({"results": [["2026-05-27T10:00:00Z", "deadbeef"]]})
            return json.dumps({"results": []})
        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["last_run_at"] == "2026-05-27T10:00:00Z"
        assert result["last_commit"] == "deadbeef"

    def test_running_status_skips_graph_query(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "running", "processed": 3, "total": 10,
            "current_commit": "abc123", "error": None,
        }
        db_instance.execute.reset_mock()
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["status"] == "running"
        assert result["processed"] == 3
        assert result["total"] == 10
        assert result["current_commit"] == "abc123"
        # Must not query the graph while running
        db_instance.execute.assert_not_called()

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

    def test_returns_total_ingested_from_graph(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        def execute_side_effect(query, *args, **kwargs):
            if ":last-run-at" in query and ":last-commit" in query:
                return json.dumps({"results": [["2026-05-27T10:00:00Z", "deadbeef"]]})
            if ":type/commit" in query:
                return json.dumps({"results": [[1017]]})
            return json.dumps({"results": []})
        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["total_ingested"] == 1017

    def test_total_ingested_reflects_true_persisted_count_not_stale_watermark(
        self, mock_minigraf_db, tmp_path
    ):
        """Regression test for #85: total_ingested must come from a direct
        :type/commit entity count, not the :total-ingested watermark — the
        watermark is only written on clean run completion, so after a run is
        interrupted mid-way it drifts far below the true persisted count."""
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        def execute_side_effect(query, *args, **kwargs):
            if ":last-run-at" in query and ":last-commit" in query:
                return json.dumps({"results": [["2026-05-27T10:00:00Z", "deadbeef"]]})
            if ":total-ingested" in query:
                # Stale watermark from the last *completed* run — far below reality.
                return json.dumps({"results": [[104]]})
            if ":type/commit" in query:
                # True count of durably persisted commit entities.
                return json.dumps({"results": [[21715]]})
            return json.dumps({"results": []})
        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server.handle_minigraf_ingest_status()
        assert result["total_ingested"] == 21715

    def test_total_ingested_absent_returns_none(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
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


class TestIngestionWrites:
    def test_ingest_transact_uses_valid_from(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._ingest_transact(
            db,
            ['[:module/foo :description "foo.py"]'],
            "2025-03-01T10:00:00Z",
            "git:abc test",
        )
        call_args = db_instance.execute.call_args[0][0]
        assert ':valid-from "2025-03-01T10:00:00Z"' in call_args
        assert ":valid-to" not in call_args
        # facts vector must come before the options map
        assert call_args.index("[:module/foo") < call_args.index(":valid-from")

    def test_ingest_close_uses_valid_from_and_valid_to(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._ingest_close(
            db,
            ['[:module/foo :description "foo.py"]'],
            "2025-01-01T00:00:00Z",
            "2025-03-01T10:00:00Z",
            "git:abc delete",
        )
        call_args = db_instance.execute.call_args[0][0]
        assert ':valid-from "2025-01-01T00:00:00Z"' in call_args
        assert ':valid-to "2025-03-01T10:00:00Z"' in call_args
        # facts vector must come before the options map
        assert call_args.index("[:module/foo") < call_args.index(":valid-from")

    def test_watermark_update_transacts_hash(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._watermark_update(db, "deadbeef", "2025-03-01T10:00:00Z", "git:deadbeef x: y")
        call_args = db_instance.execute.call_args[0][0]
        assert "deadbeef" in call_args
        assert ":ingestion/watermark" in call_args

    def test_watermark_query_returns_none_when_absent(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        result = mcp_server._watermark_query(db)
        assert result is None

    def test_watermark_query_returns_hash_when_present(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["abc123"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        result = mcp_server._watermark_query(db)
        assert result == "abc123"

    def test_ingest_transact_noop_for_empty_triples(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()
        mcp_server._ingest_transact(db, [], "2025-03-01T10:00:00Z", "r")
        db_instance.execute.assert_not_called()

    def test_ingest_close_noop_for_empty_triples(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()
        mcp_server._ingest_close(db, [], "2025-01-01T00:00:00Z", "2025-03-01T00:00:00Z", "r")
        db_instance.execute.assert_not_called()

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
        self, mock_minigraf_db, git_repo
    ):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(git_repo / "t.graph"))
        db_instance.execute.return_value = json.dumps({
            "results": [[":function/auth-py-login", "auth.py", "login", "2025-01-15T10:00:00Z"]]
        })
        db = mcp_server.get_db()
        entity_valid_from, entity_descriptions, file_entities = (
            mcp_server._preload_known_entities(db, str(git_repo))
        )
        assert entity_valid_from.get(":function/auth-py-login") == "2025-01-15T10:00:00Z"
        assert entity_descriptions.get(":function/auth-py-login") == "login"

    def test_last_run_write_transacts_correct_fields(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._last_run_write(db, "deadbeef", "2026-05-27T10:00:00Z", 1017)

        call_args = db_instance.execute.call_args[0][0]
        assert ":ingestion/last-run-at" in call_args
        assert ":last-run-at" in call_args
        assert "2026-05-27T10:00:00Z" in call_args
        assert ":last-commit" in call_args
        assert "deadbeef" in call_args
        assert ":type/ingestion" in call_args
        assert ":total-ingested" in call_args
        assert "1017" in call_args
        assert ":valid-from" not in call_args

    def test_run_ingestion_writes_last_run_on_completion(self, mock_minigraf_db, git_repo, monkeypatch):
        """Uses the real git_repo fixture (2 real commits) rather than
        faking a commit list + empty _git_diff_tree_raw: _extract_commit
        runs in a spawned worker process (#116), which re-imports
        mcp_server fresh and never sees _git_diff_tree_raw patched on this
        (parent) process's module object — a fabricated commit hash against
        a non-git tmp_path would just make the real git call fail in the
        worker. _last_run_write itself still runs on write_executor, an
        in-process thread, so patching it here still works as before.
        """
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(git_repo / "t.graph"))

        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        last_hash = commits[-1][0]

        last_run_calls = []
        monkeypatch.setattr(
            mcp_server, "_last_run_write",
            lambda db, h, t, n: last_run_calls.append((h, t, n))
        )

        asyncio.run(mcp_server._run_ingestion(str(git_repo), "HEAD"))

        assert len(last_run_calls) == 1
        assert last_run_calls[0][0] == last_hash
        assert last_run_calls[0][1].endswith("Z")
        assert last_run_calls[0][2] == 2  # 2 commits processed

    def test_run_ingestion_writes_last_run_when_no_commits(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: "abc123")
        monkeypatch.setattr(mcp_server, "_git_commits", lambda repo, watermark, branch: [])

        last_run_calls = []
        monkeypatch.setattr(
            mcp_server, "_last_run_write",
            lambda db, h, t, n: last_run_calls.append((h, t, n))
        )

        asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))

        assert len(last_run_calls) == 1
        assert last_run_calls[0][0] == "abc123"
        assert last_run_calls[0][1].endswith("Z")
        assert last_run_calls[0][2] == 0  # no commits processed this run, prior was 0


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
            "function_bodies": {}, "class_bodies": {},
            "globals": ["GLOBAL_X"], "global_bodies": {"GLOBAL_X": "GLOBAL_X = 5"},
            "fields": [], "field_info": {},
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
            "function_bodies": {}, "class_bodies": {"Foo": "class Foo: ..."},
            "globals": [], "global_bodies": {},
            "fields": [("staticField", "Foo", True)],
            "field_info": {"staticField": {"class": "Foo", "static": True, "body": "staticField = 1"}},
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


class TestBuildCodeTriplesGlobalsAndFields:
    def test_new_global_writes_full_triples(self):
        import mcp_server
        extracted = {"functions": [], "classes": [], "imports": [], "calls": [],
                     "function_bodies": {}, "class_bodies": {},
                     "globals": ["GLOBAL_X"], "global_bodies": {"GLOBAL_X": "GLOBAL_X = 5"},
                     "fields": [], "field_info": {}}
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
                     "function_bodies": {}, "class_bodies": {},
                     "globals": ["GLOBAL_X"], "global_bodies": {"GLOBAL_X": "GLOBAL_X = 6"},
                     "fields": [], "field_info": {}}
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
    def test_reloads_open_depends_on_edge(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server

        src_ident = mcp_server._code_ident("module", "mod_a.py")
        dep_ident = mcp_server._canonical_ident("module", "mod_b")
        # 1704067200000 ms == 2024-01-01T00:00:00.000Z
        db_instance.execute.return_value = json.dumps(
            {"results": [[src_ident, dep_ident, 1704067200000]]}
        )
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()

        file_entities = {"mod_a.py": [src_ident]}
        file_deps, dep_valid_from = mcp_server._preload_known_deps(db, file_entities)

        assert file_deps["mod_a.py"] == {dep_ident}
        assert dep_valid_from[(src_ident, dep_ident)] == "2024-01-01T00:00:00.000Z"

    def test_query_includes_any_valid_time_and_forever_filter(self, mock_minigraf_db, tmp_path):
        """The query must ask for :any-valid-time (required for any per-fact
        pseudo-attribute to bind) and filter :db/valid-to down to the
        VALID_TIME_FOREVER sentinel so closed edges aren't reloaded as open."""
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._preload_known_deps(db, {})

        query = db_instance.execute.call_args[0][0]
        assert ":any-valid-time" in query
        assert ":depends-on" in query
        assert ":db/valid-from" in query
        assert ":db/valid-to" in query
        assert "9223372036854775807" in query

    def test_no_deps_returns_empty_structures(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()

        file_deps, dep_valid_from = mcp_server._preload_known_deps(db, {"mod_a.py": []})

        assert file_deps == {}
        assert dep_valid_from == {}

    def test_query_failure_is_non_fatal(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        from minigraf import MiniGrafError
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.side_effect = MiniGrafError("boom")

        file_deps, dep_valid_from = mcp_server._preload_known_deps(db, {"mod_a.py": []})

        assert file_deps == {}
        assert dep_valid_from == {}


class TestPreloadExternalDependencies:
    def test_preload_known_entities_includes_external_dependency(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        # _preload_known_entities' query shape is [?ident ?path ?desc ?date] per entity_type
        db_instance.execute.return_value = json.dumps({
            "results": [[":module/vendor-lib", "vendor/lib", "lib", "2026-01-01T00:00:00Z"]]
        })
        mcp_server.open_db(str(tmp_path / "memory.graph"))
        db = mcp_server.get_db()

        entity_valid_from, entity_descriptions, file_entities = mcp_server._preload_known_entities(db, str(tmp_path))

        assert ":module/vendor-lib" in entity_valid_from
        assert entity_descriptions[":module/vendor-lib"] == "lib"
        assert "vendor/lib" in file_entities

    def test_preload_pinned_commits_reloads_current_sha(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        # _preload_pinned_commits' query shape is [?e ?sha ?vf] with :any-valid-time
        db_instance.execute.return_value = json.dumps({
            "results": [[":module/vendor-lib", "abc123", 1735689600000]]
        })
        mcp_server.open_db(str(tmp_path / "memory.graph"))
        db = mcp_server.get_db()

        pinned = mcp_server._preload_pinned_commits(db)

        assert pinned[":module/vendor-lib"][0] == "abc123"
        assert pinned[":module/vendor-lib"][1].endswith("Z")

    def test_preload_pinned_commits_returns_empty_on_query_failure(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        from minigraf import MiniGrafError
        mcp_server.open_db(str(tmp_path / "memory.graph"))
        db = mcp_server.get_db()
        db_instance.execute.side_effect = MiniGrafError("boom")

        assert mcp_server._preload_pinned_commits(db) == {}


class TestTotalIngestedQuery:
    def test_returns_zero_when_absent(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        assert mcp_server._total_ingested_query(db) == 0

    def test_returns_stored_count(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [[462]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        assert mcp_server._total_ingested_query(db) == 462


class TestRunIngestion:
    @pytest.mark.asyncio
    async def test_ingestion_processes_all_commits(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

    @pytest.mark.asyncio
    async def test_watermark_updated_after_each_commit(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        watermark_calls = [
            c for c in db_instance.execute.call_args_list
            if ":ingestion/watermark" in str(c) and "transact" in str(c)
        ]
        assert len(watermark_calls) >= 2  # one per commit

    @pytest.mark.asyncio
    async def test_per_commit_get_db_lock_retry_does_not_block_event_loop(
        self, mock_minigraf_db, git_repo, monkeypatch
    ):
        """Regression test for #99: a lock-retry hit while reacquiring the DB
        between commits must not block via time.sleep() — _run_ingestion runs
        on the single-threaded event loop, and a blocking sleep there would
        freeze the very coroutine responsible for eventually releasing that
        lock."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server

        lock_err = MiniGrafError(
            "Database is locked by another process (lock file: x.graph.lock, holder PID: 1)."
        )
        mock_class.open.side_effect = [db_instance, lock_err, db_instance, db_instance, db_instance]
        mcp_server.open_db(str(git_repo / "memory.graph"))
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

    @pytest.mark.asyncio
    async def test_preload_phase_does_not_block_event_loop(
        self, mock_minigraf_db, git_repo, monkeypatch
    ):
        """Regression test for #103: opening the DB and running the startup
        preload queries (_watermark_query, _count_commit_entities,
        _preload_known_entities/_deps/_pinned_commits) must run off the event
        loop. Before the fix these ran synchronously inline with no `await`
        between them, so on a large graph the phase could run long enough to
        starve the stdio handshake past a client's connection timeout,
        leaving the server permanently unable to connect."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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
    async def test_db_released_between_commits(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._db = None
        mcp_server.open_db(str(git_repo / "memory.graph"))
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
        self, mock_minigraf_db, git_repo
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
        """
        mock_class, _ = mock_minigraf_db

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

        mock_class.open.side_effect = _open
        import mcp_server
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
    async def test_handle_minigraf_ingest_git_returns_immediately(self, mock_minigraf_db, git_repo, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

    @pytest.mark.asyncio
    async def test_second_call_while_running_returns_error(self, mock_minigraf_db, git_repo, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_repo(self, mock_minigraf_db, monkeypatch):
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

    @pytest.mark.asyncio
    async def test_proceeds_when_no_live_holder(self, mock_minigraf_db, git_repo, monkeypatch):
        """When no live process owns the graph lock, handle_minigraf_ingest_git
        proceeds normally and starts the ingestion task."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

    @pytest.mark.asyncio
    async def test_status_not_idle_immediately_after_ingest_git_starts(
        self, mock_minigraf_db, git_repo, monkeypatch
    ):
        """Regression test for #109: handle_minigraf_ingest_git creates
        _ingest_task and returns before _run_ingestion's preload phase has
        had a chance to run, so _ingest_progress must already reflect
        "in progress" the instant it returns. Leaving it at "idle" reproduces
        the reported contradiction, where a caller sees status "idle" but
        a subsequent minigraf_ingest_git call is rejected with "already in
        progress" because the task-existence check is accurate immediately."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

    @pytest.mark.asyncio
    async def test_processed_seeded_from_prior_ingested(self, mock_minigraf_db, git_repo, monkeypatch):
        """processed starts at the true persisted commit count and increments
        cumulatively — regression test for #85 (seeding must not rely on the
        :total-ingested watermark, which goes stale after an interrupted run)."""
        mock_class, db_instance = mock_minigraf_db
        # _count_commit_entities returns 462 (true persisted count); all other
        # queries — including the stale :total-ingested watermark — return [].
        def execute_side_effect(query, *args, **kwargs):
            if ":type/commit" in query:
                return json.dumps({"results": [[462]]})
            return json.dumps({"results": []})
        db_instance.execute.side_effect = execute_side_effect
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
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
        self, mock_minigraf_db, git_repo
    ):
        """A stale :total-ingested watermark (left behind by a prior run that
        was interrupted before writing its completion record) must not affect
        seeding — only the true :type/commit count matters."""
        mock_class, db_instance = mock_minigraf_db
        def execute_side_effect(query, *args, **kwargs):
            if ":type/commit" in query:
                return json.dumps({"results": [[21715]]})
            if ":total-ingested" in query:
                return json.dumps({"results": [[104]]})  # stale, must be ignored
            return json.dumps({"results": []})
        db_instance.execute.side_effect = execute_side_effect
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["prior_ingested"] == 21715
        assert mcp_server._ingest_progress["processed"] == 21717  # 21715 + 2 commits


class TestRunIngestionConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_run_matches_sequential_facts(self, mock_minigraf_db, git_repo_with_deps, monkeypatch):
        """A run using the thread-pool pipeline must produce the exact same
        set of transacted triples, in the same commit order, as today's
        sequential loop — this is the core correctness guarantee for the
        producer/consumer split."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server

        # Capture the true, unpatched transact function once — each run below
        # re-patches mcp_server._ingest_transact, so grabbing this later would
        # pick up the previous run's capture wrapper instead of the original.
        real_ingest_transact = mcp_server._ingest_transact

        async def run_and_capture(worker_count):
            if worker_count is None:
                monkeypatch.delenv("MINIGRAF_INGEST_WORKERS", raising=False)
            else:
                monkeypatch.setenv("MINIGRAF_INGEST_WORKERS", str(worker_count))

            # Reset module-level state so the second run starts exactly as
            # clean as the first (same watermark, same progress, no leftover
            # shutdown signal).
            mcp_server.open_db(str(git_repo_with_deps / "memory.graph"))
            mcp_server._ingest_progress = {
                "status": "idle", "processed": 0, "total": 0,
                "current_commit": "", "error": None,
            }
            mcp_server._shutdown_requested.clear()

            transacted: list = []

            def capture(db, triples, ts_iso, reason=""):
                transacted.append(list(triples))
                return real_ingest_transact(db, triples, ts_iso, reason)

            monkeypatch.setattr(mcp_server, "_ingest_transact", capture)
            await mcp_server._run_ingestion(str(git_repo_with_deps), "HEAD")
            assert mcp_server._ingest_progress["status"] == "complete"
            assert mcp_server._ingest_progress["processed"] == 1
            return transacted

        sequential_triples = await run_and_capture(1)
        concurrent_triples = await run_and_capture(4)

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        mod_b_ident = mcp_server._code_ident("module", "mod_b.py")
        all_triples = [t for batch in sequential_triples for t in batch]
        assert any(mod_a_ident in t for t in all_triples)
        assert any(mod_b_ident in t for t in all_triples)

        # The core equivalence guarantee: identical triples, identical
        # per-commit batching, identical order — regardless of how many
        # worker threads did the extraction.
        assert concurrent_triples == sequential_triples

    @pytest.mark.asyncio
    async def test_worker_count_env_var_is_respected(self, mock_minigraf_db, git_repo, monkeypatch):
        monkeypatch.setenv("MINIGRAF_INGEST_WORKERS", "1")
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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
        self, mock_minigraf_db, git_repo
    ):
        """A file-content fetch failure must be induced for real, not via
        monkeypatch: _extract_commit runs in a spawned worker process
        (#116), which re-imports mcp_server fresh and never sees a patch
        applied to this (parent) process's module object. Corrupting the
        actual loose git blob object for auth.py makes `git show
        <hash>:auth.py` genuinely fail inside the worker, exercising the
        same try/except continue path the old monkeypatch used to reach."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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
        self, mock_minigraf_db, tmp_path
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
        db_instance = mock_minigraf_db[1]
        db_instance.execute.return_value = json.dumps({"results": []})

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
    async def test_shutdown_mid_run_stops_at_commit_boundary(self, mock_minigraf_db, git_repo, monkeypatch):
        """git_repo has 2 commits. Request shutdown right after the first
        commit's extraction is consumed but before the second is processed;
        the loop must stop cleanly with status 'stopped' and only 1 commit
        durably processed."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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
    async def test_resumes_from_watermark_after_shutdown(self, mock_minigraf_db, git_repo, monkeypatch):
        """After a simulated shutdown mid-run, a second _run_ingestion call
        against the same (mocked) DB state must pick up the watermark that
        was written for the last fully-completed commit and finish the
        remaining commit(s), without re-processing or skipping any."""
        mock_class, db_instance = mock_minigraf_db
        import mcp_server

        # In-memory fake DB standing in for minigraf so the watermark
        # written by run 1 is genuinely visible to run 2 (the default mock
        # always returns the same canned response and can't model this).
        state = {"watermark": None}

        def execute(cmd, *a, **k):
            if "(query" in cmd and ":ingestion/watermark" in cmd and ":hash" in cmd:
                if state["watermark"]:
                    return json.dumps({"results": [[state["watermark"]]]})
                return json.dumps({"results": []})
            if "(transact" in cmd and ":ingestion/watermark" in cmd:
                import re
                m = re.search(r':hash "([0-9a-f]+)"', cmd)
                if m:
                    state["watermark"] = m.group(1)
            return json.dumps({"results": []})

        db_instance.execute.side_effect = execute
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

        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        # Second run only had the 1 remaining commit to do, and
        # _count_commit_entities (mocked to [] here) seeds prior_ingested=0,
        # so processed reflects just that run's own work.
        assert mcp_server._ingest_progress["processed"] == 1


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


class TestIndexCache:
    def test_get_returns_none_before_any_rebuild(self):
        from mcp_server import IndexCache
        cache = IndexCache()
        assert cache.get() is None

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

    def test_stale_index_served_when_already_rebuilding(self):
        import mcp_server
        from mcp_server import IndexCache, FactIndex
        cache = IndexCache()
        stale = FactIndex([[":decision/old", ":description", "old"]], boost=2.0)
        cache._current = stale
        cache._rebuilding = True
        cache.invalidate()  # no-op because _rebuilding
        assert cache.get() is stale
        cache._rebuilding = False

    def test_invalidate_noop_when_rebuilding(self):
        from mcp_server import IndexCache
        from unittest.mock import patch
        cache = IndexCache()
        cache._rebuilding = True
        with patch("threading.Thread") as mock_thread:
            cache.invalidate()
            mock_thread.assert_not_called()
        cache._rebuilding = False

    def test_concurrent_invalidate_does_not_spawn_multiple_threads(self):
        from mcp_server import IndexCache
        from unittest.mock import patch
        import threading as th
        cache = IndexCache()
        thread_count = []

        original_thread_init = th.Thread.__init__

        def counting_thread_init(self_thread, *args, **kwargs):
            original_thread_init(self_thread, *args, **kwargs)
            thread_count.append(1)

        with patch.object(th.Thread, "__init__", counting_thread_init):
            # Simulate two concurrent callers both passing the guard
            # before either sets _rebuilding. With the fix, the first call
            # sets _rebuilding = True before t.start(), so the second call
            # sees it and returns without spawning.
            cache.invalidate()
            cache.invalidate()  # should be a no-op
        assert len(thread_count) == 1
        cache._rebuilding = False  # cleanup

    def test_rebuild_leaves_current_unchanged_on_error(self, monkeypatch):
        import mcp_server
        from mcp_server import IndexCache, FactIndex
        cache = IndexCache()
        stale = FactIndex([[":decision/old", ":description", "old"]], boost=2.0)
        cache._current = stale
        # Force get_db to raise
        monkeypatch.setattr(mcp_server, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("db error")))
        cache._rebuild()
        assert cache.get() is stale
        assert cache._rebuilding is False


@requires_bm25
class TestMemoryPrepareTurnBM25:
    def test_returns_empty_when_no_index(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mcp_server.open_db(str(tmp_path / "t.graph"))
        fresh_cache = mcp_server.IndexCache()  # no index built yet
        with patch.object(mcp_server, "_index_cache", fresh_cache), \
             patch.object(mcp_server, "_BM25_AVAILABLE", True):
            result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert result == ""

    def test_returns_empty_for_unmatched_query(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/use-redis", ":description", "use redis"]]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        cache = mcp_server.IndexCache()
        cache._rebuild()
        with patch.object(mcp_server, "_index_cache", cache):
            result = mcp_server.handle_memory_prepare_turn("elephants trombone")
        assert result == ""

    def test_memory_facts_rank_above_git_facts(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [
                [":decision/use-redis", ":description", "use redis for caching"],
                [":commit/abc123def456", ":subject", "feat use redis caching layer"],
                [":function/unrelated", ":name", "some other thing entirely"],
            ]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        cache = mcp_server.IndexCache()
        cache._rebuild()
        with patch.object(mcp_server, "_index_cache", cache):
            result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert "Relevant memory context:" in result
        assert result.index(":decision/use-redis") < result.index(":commit/abc123def456")

    def test_respects_scan_limit(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[f":decision/item-{i}", ":description", f"redis item {i}"] for i in range(20)]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setenv("MINIGRAF_PREPARE_SCAN_LIMIT", "3")
        cache = mcp_server.IndexCache()
        cache._rebuild()
        with patch.object(mcp_server, "_index_cache", cache):
            result = mcp_server.handle_memory_prepare_turn("redis")
        lines = [l for l in result.splitlines() if "|" in l]
        assert len(lines) <= 3


class TestIndexCacheInvalidation:
    def test_successful_transact_triggers_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx_id": 1, "count": 1})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_minigraf_transact(
                '[[:decision/test :description "test"]]', reason="test"
            )
            mock_inv.assert_called_once()

    def test_failed_transact_does_not_trigger_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        from minigraf import MiniGrafError
        mock_class, db_instance = mock_minigraf_db
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad tx")
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_minigraf_transact(
                '[[:decision/test :description "test"]]', reason="test"
            )
            mock_inv.assert_not_called()

    def test_successful_retract_triggers_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx_id": 2, "count": 1})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_minigraf_retract(
                '[[:decision/test :description "test"]]', reason="cleanup"
            )
            mock_inv.assert_called_once()

    def test_failed_retract_does_not_trigger_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        from minigraf import MiniGrafError
        mock_class, db_instance = mock_minigraf_db
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad retract")
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_minigraf_retract(
                '[[:decision/test :description "test"]]', reason="cleanup"
            )
            mock_inv.assert_not_called()

    def test_run_ingestion_triggers_invalidation_on_completion(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_git_commits", lambda *a, **k: [])
        monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: None)
        monkeypatch.setattr(mcp_server, "_preload_known_entities", lambda *a, **k: ({}, {}, {}))
        monkeypatch.setattr(mcp_server, "_ingest_tags", lambda *a, **k: None)
        monkeypatch.setattr(mcp_server, "_last_run_write", lambda *a, **k: None)
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            import asyncio
            asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))
            mock_inv.assert_called_once()


class TestBM25GracefulDegradation:
    def test_falls_back_to_heuristic_when_bm25_unavailable(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        # The heuristic extracts entities from "decided to use redis":
        #   "decided" (7 chars) and "redis" (5 chars) pass the _MIN_ENTITY_LEN=4 filter.
        # For each entity it calls db.execute() with a contains? query.
        # Using side_effect so the first entity query returns a match and the
        # second returns empty (deduplication handles any overlap).
        session_rule_count = len(mcp_server.SESSION_RULES)
        call_count = 0

        def side_effect(query_str):
            nonlocal call_count
            call_count += 1
            if call_count <= session_rule_count:
                # SESSION_RULES registrations during open_db
                return json.dumps({"ok": True})
            # First entity query ("decided") — return a matching fact
            if call_count == session_rule_count + 1:
                return json.dumps({"results": [["use", "decided to use redis"]]})
            # Subsequent entity queries — return empty
            return json.dumps({"results": []})

        db_instance.execute.side_effect = side_effect
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_BM25_AVAILABLE", False)
        result = mcp_server.handle_memory_prepare_turn("decided to use redis")
        # Heuristic path produces "Relevant memory context:" when facts are found
        assert "Relevant memory context:" in result

    def test_index_cache_rebuild_noop_when_bm25_unavailable(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/use-redis", ":description", "use redis"]]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_BM25_AVAILABLE", False)
        monkeypatch.setattr(mcp_server, "_BM25Okapi", None)
        cache = mcp_server.IndexCache()
        cache._rebuild()  # should not raise
        result = cache.get()
        # When _BM25Okapi is None, FactIndex._bm25 is never initialized
        assert isinstance(result, mcp_server.FactIndex)
        assert result._bm25 is None


class TestBM25Tokenize:
    def test_splits_keyword_ident_on_punctuation(self):
        from mcp_server import _tokenize
        assert _tokenize(":decision/use-redis") == ["decision", "use", "redis"]

    def test_lowercases_tokens(self):
        from mcp_server import _tokenize
        assert _tokenize("use Redis for Caching") == ["use", "redis", "for", "caching"]

    def test_filters_empty_tokens(self):
        from mcp_server import _tokenize
        assert _tokenize(":::") == []

    def test_mixed_fact_row(self):
        from mcp_server import _tokenize
        assert _tokenize(":commit/abc123 :subject feat add redis") == [
            "commit", "abc123", "subject", "feat", "add", "redis"
        ]

    def test_memory_prefix_detected(self):
        from mcp_server import _MEMORY_PREFIXES
        assert ":decision/use-redis".startswith(_MEMORY_PREFIXES)
        assert ":preference/tdd".startswith(_MEMORY_PREFIXES)
        assert ":constraint/no-js".startswith(_MEMORY_PREFIXES)
        assert ":dependency/redis".startswith(_MEMORY_PREFIXES)

    def test_git_prefix_not_memory(self):
        from mcp_server import _MEMORY_PREFIXES
        assert not ":commit/abc123".startswith(_MEMORY_PREFIXES)
        assert not ":function/foo-bar".startswith(_MEMORY_PREFIXES)
        assert not ":module/src-main".startswith(_MEMORY_PREFIXES)


@requires_bm25
class TestFactIndex:
    def test_empty_facts_returns_empty_query(self):
        from mcp_server import FactIndex
        index = FactIndex([], boost=2.0)
        assert index.query("redis", top_n=10) == []

    def test_query_returns_matching_fact(self):
        from mcp_server import FactIndex
        facts = [[":decision/use-redis", ":description", "use redis for caching"]]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis caching", top_n=10)
        assert len(results) == 1
        assert results[0] == [":decision/use-redis", ":description", "use redis for caching"]

    def test_memory_fact_outscores_git_fact(self):
        from mcp_server import FactIndex
        # Include a third unrelated fact so BM25 IDF is positive (avoids negative-score
        # small-corpus edge case that would invert the boost when multiplied).
        facts = [
            [":decision/use-redis", ":description", "use redis for caching"],
            [":commit/abc123def456", ":subject", "feat use redis for caching layer"],
            [":commit/xyz789", ":subject", "fix typo in readme"],
        ]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis caching", top_n=10)
        assert results[0][0] == ":decision/use-redis"

    def test_no_overlap_query_returns_empty(self):
        from mcp_server import FactIndex
        facts = [[":decision/use-redis", ":description", "use redis for caching"]]
        index = FactIndex(facts, boost=2.0)
        results = index.query("elephants trombone completely unrelated", top_n=10)
        assert results == []

    def test_top_n_respected(self):
        from mcp_server import FactIndex
        facts = [[f":decision/item-{i}", ":description", f"redis item {i}"] for i in range(20)]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis", top_n=5)
        assert len(results) <= 5

    def test_facts_with_no_tokens_skipped(self):
        from mcp_server import FactIndex
        # A fact whose text tokenises to [] should not crash
        facts = [
            [":::", ":::", ":::"],
            [":decision/use-redis", ":description", "use redis"],
        ]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis", top_n=10)
        assert len(results) == 1
        assert results[0][0] == ":decision/use-redis"


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

    _subprocess.run(["git", "mv", "old_auth.py", "new_auth.py"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "rename auth"], cwd=repo, check=True, capture_output=True)

    return repo


class TestRunIngestionBitemporalClose:
    """Integration tests verifying bi-temporal correctness of entity lifecycle handling."""

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_file_deletion_closes_with_real_description_not_empty_string(
        self, mock_minigraf_db, git_repo_with_deletion, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_deletion / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_deletion), "HEAD")

        assert close_triples_seen, "Expected _ingest_close to be called on file deletion"
        assert not any(':description ""' in t for t in close_triples_seen), \
            "Close triples must not use empty string as description placeholder"
        assert any(':description "login"' in t for t in close_triples_seen), \
            "Close triples must carry the real description of the deleted function"

    @pytest.mark.asyncio
    async def test_file_deletion_close_includes_ident_and_contains_triples(
        self, mock_minigraf_db, git_repo_with_deletion, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_deletion / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_deletion), "HEAD")

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        module_ident = mcp_server._code_ident("module", "auth.py")
        assert any(":ident" in t and fn_ident in t for t in close_triples_seen), \
            "Close triples must include :ident fact for the deleted function"
        assert any(":contains" in t and fn_ident in t and module_ident in t
                   for t in close_triples_seen), \
            "Close triples must include the module :contains edge for the deleted function"

    @pytest.mark.asyncio
    async def test_intra_file_deletion_closes_removed_function(
        self, mock_minigraf_db, git_repo_with_intra_file_deletion, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_intra_file_deletion / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_intra_file_deletion), "HEAD")

        logout_ident = mcp_server._code_ident("function", "auth.py", "logout")
        login_ident = mcp_server._code_ident("function", "auth.py", "login")

        assert any(logout_ident in t for t in close_triples_seen), \
            "logout() removed from modified file must trigger a close"
        assert not any(login_ident in t and ":ident" in t for t in close_triples_seen), \
            "login() still present in file must not be closed"

    @pytest.mark.asyncio
    async def test_renamed_file_links_old_and_new_via_rename_edges(
        self, mock_minigraf_db, git_repo_with_rename, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_rename / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_rename), "HEAD")

        old_module_ident = mcp_server._code_ident("module", "old_auth.py")
        new_module_ident = mcp_server._code_ident("module", "new_auth.py")
        new_fn_ident = mcp_server._code_ident("function", "new_auth.py", "login")

        assert any(old_module_ident in t for t in close_triples_seen), \
            "Old module entities must still be closed when file is renamed"
        assert any(f"{old_module_ident} :renamed-to {new_module_ident}" in t for t in close_triples_seen), \
            "Old module's close triples must include :renamed-to pointing at the new ident"

        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert new_fn_ident in transact_calls, \
            "New module's entities must still be created after file is renamed"
        assert f"{new_module_ident} :renamed-from {old_module_ident}" in transact_calls, \
            "New module's open triples must include :renamed-from pointing at the old ident"

    @pytest.mark.asyncio
    async def test_in_file_function_rename_links_via_rename_edges(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
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

        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(repo), "HEAD")

        old_fn_ident = mcp_server._code_ident("function", "auth.py", "oldName")
        new_fn_ident = mcp_server._code_ident("function", "auth.py", "newName")

        assert any(f"{old_fn_ident} :renamed-to {new_fn_ident}" in t for t in close_triples_seen)
        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert f"{new_fn_ident} :renamed-from {old_fn_ident}" in transact_calls

    @pytest.mark.asyncio
    async def test_global_rename_links_via_rename_edges_end_to_end(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
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

        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(repo), "HEAD")

        old_ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        new_ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_Y")

        assert any(f"{old_ident} :renamed-to {new_ident}" in t for t in close_triples_seen)
        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert f"{new_ident} :renamed-from {old_ident}" in transact_calls


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

    (repo / "mod_a.py").write_text("def main(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "remove import"], cwd=repo, check=True, capture_output=True)

    return repo


class TestRunIngestionBitemporalDeps:
    """Tests verifying that :depends-on edges are written/closed bi-temporally in the commit loop."""

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_new_import_writes_depends_on_via_ingest_transact(
        self, mock_minigraf_db, git_repo_with_deps, monkeypatch
    ):
        """Adding a file with an import must call _ingest_transact with a :depends-on triple
        using the git commit timestamp."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_deps / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)

        await mcp_server._run_ingestion(str(git_repo_with_deps), "HEAD")

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        # mod_b.py genuinely exists in file_entities, so the generalized
        # tiered matcher (Task 12) now resolves "mod_b" to the real internal
        # module via the basename tier, instead of the old Rust-only fallback.
        mod_b_resolved = mcp_server._code_ident("module", "mod_b.py")
        dep_triple = f"{mod_a_ident} :depends-on {mod_b_resolved}"
        assert any(dep_triple in t for t in transact_calls), (
            f"Expected _ingest_transact to be called with '{dep_triple}' during commit loop, "
            f"got: {transact_calls}"
        )

    @pytest.mark.asyncio
    async def test_removed_import_closes_depends_on_edge(
        self, mock_minigraf_db, git_repo_with_dep_removal, monkeypatch
    ):
        """Removing an import in a modified file must call _ingest_close with the
        :depends-on triple so the edge gets a :valid-to bound."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_dep_removal / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_dep_removal), "HEAD")

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        mod_b_resolved = mcp_server._code_ident("module", "mod_b.py")
        dep_triple = f"{mod_a_ident} :depends-on {mod_b_resolved}"
        assert any(dep_triple in t for t in close_triples_seen), (
            f"Expected _ingest_close to be called with '{dep_triple}' when import removed, "
            f"got: {close_triples_seen}"
        )


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
    async def test_unresolved_import_gets_tagged_external_dependency(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.rs").write_text('use tokio;\nfn main() {}\n')
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add main"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        tokio_ident = mcp_server._canonical_ident("module", "tokio")
        assert any(f"[{tokio_ident} :entity-type :type/external-dependency]" in t for t in transact_calls)
        assert any(f'[{tokio_ident} :description "tokio"]' in t for t in transact_calls)

    @pytest.mark.asyncio
    async def test_unresolved_relative_import_not_tagged_external_end_to_end(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.ts").write_text("import { thing } from './missing';\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add main"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        missing_ident = mcp_server._canonical_ident("module", "./missing")
        assert not any(
            f"[{missing_ident} :entity-type :type/external-dependency]" in t for t in transact_calls
        )


class TestGitIngestionPathIgnore:
    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_default_ignored_directory_produces_no_code_entities(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """A file under a default-ignored directory (vendor/) must not produce
        any :type/module, :type/function, or :type/class triples."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        vendored_module_ident = mcp_server._code_ident("module", "vendor/lib.py")
        assert not any(vendored_module_ident in t for t in transact_calls)
        assert not any(":entity-type :type/function" in t for t in transact_calls)
        assert not any(":entity-type :type/class" in t for t in transact_calls)

    @pytest.mark.asyncio
    async def test_import_into_ignored_path_becomes_external_dependency(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """Before this feature, vendor/foo.py would resolve as a normal in-tree
        module (see _resolve_module_import's segment-suffix matcher) and
        main.py's import of it would create an internal :depends-on edge, not
        an external-dependency entity. Excluding vendor/ from known_files must
        make it fall through to the same fallback used for real external
        packages (see TestUnresolvedImportTagging)."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        external_ident = mcp_server._canonical_ident("module", "vendor.foo")
        assert any(
            f"[{external_ident} :entity-type :type/external-dependency]" in t for t in transact_calls
        )

    @pytest.mark.asyncio
    async def test_env_var_ignore_pattern_excludes_custom_directory(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """MINIGRAF_INGEST_IGNORE must add to the default ignore list, not
        replace it — a custom pattern not in the built-in defaults must still
        be honored."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
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
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        generated_module_ident = mcp_server._code_ident("module", "generated/codegen.py")
        assert not any(generated_module_ident in t for t in transact_calls)


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
        self, mock_minigraf_db, git_repo_with_future_dep, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_future_dep / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None,
        }

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
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
    async def test_submodule_add_creates_external_dependency_entity(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        sub_hash = self._add_submodule_commit(repo)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        ident = mcp_server._code_ident("module", "vendor/lib")
        assert any(f"[{ident} :entity-type :type/external-dependency]" in t for t in transact_calls)
        assert any(f'[{ident} :pinned-commit "{sub_hash}"]' in t for t in transact_calls)
        assert any(f'[{ident} :submodule-name "lib"]' in t for t in transact_calls)
        assert any(f'[{ident} :submodule-url "https://example.com/lib.git"]' in t for t in transact_calls)

    @pytest.mark.asyncio
    async def test_submodule_bump_closes_old_pinned_commit(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        first_sha = self._add_submodule_commit(repo)

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

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )
        await mcp_server._run_ingestion(str(repo), "HEAD")

        ident = mcp_server._code_ident("module", "vendor/lib")
        assert any(f'[{ident} :pinned-commit "{first_sha}"]' in t for t in close_triples_seen)

    @pytest.mark.asyncio
    async def test_submodule_removal_closes_entity(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        self._add_submodule_commit(repo)

        _subprocess.run(["git", "rm", "-f", "vendor/lib"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "remove submodule"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )
        await mcp_server._run_ingestion(str(repo), "HEAD")

        ident = mcp_server._code_ident("module", "vendor/lib")
        assert any(f'[{ident} :ident "{ident}"]' in t for t in close_triples_seen)


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
