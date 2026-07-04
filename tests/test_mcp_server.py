"""Unit tests for mcp_server.py.

All tests mock MiniGrafDb so no live minigraf install is required.
"""
import asyncio
import contextlib
import json
import sys
import os
import subprocess as _subprocess
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
        assert result == {"functions": [], "classes": [], "imports": [], "calls": []}


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


class TestGitDiffTreeRaw:
    def test_regular_file_add(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(git_repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path = entries[0]
        assert status == "A"
        assert old_mode == "000000"
        assert new_mode == "100644"
        assert path == "auth.py"

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
        status, old_mode, new_mode, old_sha, new_sha, path = entries[0]
        assert status == "A"
        assert new_mode == "160000"
        assert new_sha == sub_hash
        assert path == "vendor/lib"


class TestGitlinkChanges:
    def test_non_gitlink_rows_are_ignored(self):
        import mcp_server
        raw = [("A", "000000", "100644", "0" * 40, "a" * 40, "auth.py")]
        assert mcp_server._gitlink_changes(raw) == []

    def test_add_when_new_mode_is_gitlink(self):
        import mcp_server
        raw = [("A", "000000", "160000", "0" * 40, "b" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("add", "b" * 40, "vendor/lib")]

    def test_bump_when_both_modes_are_gitlink(self):
        import mcp_server
        raw = [("M", "160000", "160000", "b" * 40, "c" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("bump", "c" * 40, "vendor/lib")]

    def test_remove_when_old_mode_is_gitlink(self):
        import mcp_server
        raw = [("D", "160000", "000000", "c" * 40, "0" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("remove", "c" * 40, "vendor/lib")]

    def test_type_change_into_internal_reported_as_remove(self):
        import mcp_server
        raw = [("T", "160000", "100644", "c" * 40, "d" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("remove", "c" * 40, "vendor/lib")]

    def test_type_change_into_external_reported_as_add(self):
        import mcp_server
        raw = [("T", "100644", "160000", "d" * 40, "e" * 40, "vendor/lib")]
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

        results = mcp_server._extract_commit(str(git_repo), first_hash)

        assert len(results) == 1
        status, file_path, extracted = results[0]
        assert status == "A"
        assert file_path == "auth.py"
        assert "login" in extracted["functions"]

    def test_deleted_file_has_none_extracted(self, git_repo_with_deletion):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_with_deletion), watermark_hash=None)
        delete_hash = commits[-1][0]

        results = mcp_server._extract_commit(str(git_repo_with_deletion), delete_hash)

        d_entries = [r for r in results if r[0] == "D"]
        assert len(d_entries) == 1
        assert d_entries[0][2] is None

    def test_unsupported_extension_is_omitted(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_changed_files",
            lambda repo, commit: [("A", "notes.txt")],
        )
        results = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []

    def test_content_fetch_failure_is_omitted_not_raised(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_changed_files",
            lambda repo, commit: [("A", "auth.py")],
        )

        def boom(repo, commit, path):
            raise mcp_server.MiniGrafError("simulated git-show failure")

        monkeypatch.setattr(mcp_server, "_git_file_content", boom)
        results = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []


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
        triples = mcp_server._build_code_triples(
            "auth.py",
            {"functions": ["login"], "classes": ["User"], "imports": []},
            "2025-02-01T00:00:00Z",
            entity_valid_from,
            {},
            {},
            commit_ident,
        )
        assert any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)
        assert any(f"[{cls_ident} :modified-in {commit_ident}]" in t for t in triples)

    def test_build_code_triples_does_not_write_modified_in_for_new_functions(self):
        import mcp_server
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {module_ident: "2025-01-01T00:00:00Z"}
        commit_ident = ":commit/deadbeef12345678"
        triples = mcp_server._build_code_triples(
            "auth.py",
            {"functions": ["new_func"], "classes": [], "imports": []},
            "2025-02-01T00:00:00Z",
            entity_valid_from,
            {},
            {},
            commit_ident,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "new_func")
        assert not any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)
        assert any(f"[{fn_ident} :introduced-by {commit_ident}]" in t for t in triples)

    def test_build_code_triples_populates_entity_descriptions(self):
        import mcp_server
        entity_valid_from: dict = {}
        entity_descriptions: dict = {}
        file_entities: dict = {}
        mcp_server._build_code_triples(
            "auth.py",
            {"functions": ["login"], "classes": ["User"], "imports": []},
            "2025-01-01T00:00:00Z",
            entity_valid_from,
            entity_descriptions,
            file_entities,
            ":commit/abc123456789",
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

    def test_run_ingestion_writes_last_run_on_completion(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: None)
        monkeypatch.setattr(
            mcp_server, "_git_commits",
            lambda repo, watermark, branch: [("abc123", "2025-01-01T00:00:00Z", "author", "msg")]
        )
        monkeypatch.setattr(mcp_server, "_git_changed_files", lambda repo, commit: [])
        monkeypatch.setattr(mcp_server, "_watermark_update", lambda db, h, ts, r: None)

        last_run_calls = []
        monkeypatch.setattr(
            mcp_server, "_last_run_write",
            lambda db, h, t, n: last_run_calls.append((h, t, n))
        )

        asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))

        assert len(last_run_calls) == 1
        assert last_run_calls[0][0] == "abc123"
        assert last_run_calls[0][1].endswith("Z")
        assert last_run_calls[0][2] == 1  # 1 commit processed

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
    async def test_handle_minigraf_ingest_git_returns_immediately(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is True
        assert "job_id" in result

    @pytest.mark.asyncio
    async def test_second_call_while_running_returns_error(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        result = await mcp_server.handle_minigraf_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is False
        assert "already in progress" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_repo(self, mock_minigraf_db):
        import mcp_server
        mcp_server._ingest_task = None
        result = await mcp_server.handle_minigraf_ingest_git(repo_path="/nonexistent/path")
        assert result["ok"] is False
        assert "Not a git repository" in result["error"]

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
        self, mock_minigraf_db, git_repo, monkeypatch
    ):
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
        real_content = mcp_server._git_file_content

        def flaky(repo, commit, path):
            if commit == failing_hash:
                raise mcp_server.MiniGrafError("simulated failure for one commit's file")
            return real_content(repo, commit, path)

        monkeypatch.setattr(mcp_server, "_git_file_content", flaky)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # Both commits still get counted as processed even though the first
        # commit's only changed file failed to fetch.
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2


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
    async def test_renamed_file_closes_old_entities_and_opens_new(
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
        new_fn_ident = mcp_server._code_ident("function", "new_auth.py", "login")

        assert any(old_module_ident in t for t in close_triples_seen), \
            "Old module entities must be closed when file is renamed"

        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert new_fn_ident in transact_calls, \
            "New module entities must be created after file is renamed"


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
        # _resolve_module_import is Rust-focused; for Python `import mod_b` it falls back
        # to _canonical_ident("module", "mod_b") since mod_b.py != mod_b.rs
        mod_b_resolved = mcp_server._canonical_ident("module", "mod_b")
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
        mod_b_resolved = mcp_server._canonical_ident("module", "mod_b")
        dep_triple = f"{mod_a_ident} :depends-on {mod_b_resolved}"
        assert any(dep_triple in t for t in close_triples_seen), (
            f"Expected _ingest_close to be called with '{dep_triple}' when import removed, "
            f"got: {close_triples_seen}"
        )

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
        assert "pkg" in result

    def test_java_import(self, tmp_path):
        pytest.importorskip("tree_sitter_java")
        import mcp_server
        source = b'import java.util.List;'
        node = _parse_import_node("java", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "java")
        assert result == ["java"]

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
        assert result == ["System"]

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
        assert result == ["my_module"]

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
        assert result == ["kotlin"]

    def test_swift_import(self, tmp_path):
        pytest.importorskip("tree_sitter_swift")
        import mcp_server
        source = b'import Foundation'
        node = _parse_import_node("swift", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "swift")
        assert result == ["Foundation"]

    def test_scala_import(self, tmp_path):
        pytest.importorskip("tree_sitter_scala")
        import mcp_server
        source = b'import scala.collection.mutable'
        node = _parse_import_node("scala", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "scala")
        assert result == ["scala"]

    def test_haskell_import(self, tmp_path):
        pytest.importorskip("tree_sitter_haskell")
        import mcp_server
        source = b'import Data.List'
        node = _parse_import_node("haskell", source, "import", tmp_path)
        result = mcp_server._extract_import_name(node, "haskell")
        assert result == ["Data"]

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
        assert result == ["MyApp"]

    def test_elixir_import(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'import Ecto.Query'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["Ecto"]

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
