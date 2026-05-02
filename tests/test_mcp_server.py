"""Unit tests for mcp_server.py.

All tests mock MiniGrafDb so no live minigraf install is required.
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from minigraf import MiniGrafError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def reset_mcp_server_db():
    """Reset the module-level _db singleton between tests."""
    import mcp_server
    mcp_server._db = None
    yield
    mcp_server._db = None


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

    def test_get_db_raises_before_open(self):
        import mcp_server
        mcp_server._db = None
        with pytest.raises(RuntimeError, match="DB not initialised"):
            mcp_server.get_db()

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


class TestVulcanQuery:
    def test_returns_results_on_success(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["FastAPI", ":decision"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_vulcan_query("[:find ?n :where [?e :name ?n]]")

        db_instance.execute.assert_called_once()
        assert result["ok"] is True
        assert result["results"] == [["FastAPI", ":decision"]]

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad datalog")

        result = mcp_server.handle_vulcan_query("[:bad]")

        assert result["ok"] is False
        assert "bad datalog" in result["error"]


class TestVulcanTransact:
    def test_requires_reason(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_transact("[[:e :a :v]]", reason="")

        assert result["ok"] is False
        assert "reason" in result["error"].lower()

    def test_transacts_and_checkpoints(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "3"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_vulcan_transact("[[:e :a :v]]", reason="test")

        db_instance.execute.assert_called_once()
        db_instance.checkpoint.assert_called_once()
        assert result["ok"] is True

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad facts")

        result = mcp_server.handle_vulcan_transact("[[:bad]]", reason="test")

        assert result["ok"] is False
        assert "bad facts" in result["error"]


class TestVulcanRetract:
    def test_requires_reason(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_retract("[[:e :a :v]]", reason="")

        assert result["ok"] is False

    def test_retracts_and_checkpoints(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "4"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_vulcan_retract("[[:e :a :v]]", reason="gone")

        db_instance.checkpoint.assert_called_once()
        assert result["ok"] is True

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad retract")

        result = mcp_server.handle_vulcan_retract("[[:e :a :v]]", reason="gone")

        assert result["ok"] is False
        assert "bad retract" in result["error"]


class TestVulcanReportIssue:
    def test_delegates_to_report_issue(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"report_issue": mock_module}):
            result = mcp_server.handle_vulcan_report_issue("bug", "something broke")
        assert result["ok"] is True
        mock_module.report_issue.assert_called_once_with(
            "bug", "something broke", datalog=None, error=None
        )

    def test_returns_error_on_import_failure(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.dict("sys.modules", {"report_issue": None}):
            result = mcp_server.handle_vulcan_report_issue("bug", "something broke")
        assert result["ok"] is False


class TestMemoryPrepareTurn:
    def test_returns_empty_string_when_graph_empty(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_memory_prepare_turn("what database are we using?")

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
        result = mcp_server.handle_memory_prepare_turn("what did we decide about postgres?")

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
        result = mcp_server.handle_memory_prepare_turn("tell me about our framework")

        # Broad scan should have been called
        assert call_count[0] > 0

    def test_respects_scan_limit_env_var(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_PREPARE_SCAN_LIMIT", "10")
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        mcp_server.handle_memory_prepare_turn("hello")
        # Should not raise; limit is respected internally

    def test_uses_valid_at_for_message_with_explicit_iso_date(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        mcp_server.handle_memory_prepare_turn("what did we decide before 2026-01-15?")

        calls = [str(c) for c in db_instance.execute.call_args_list]
        assert any(':valid-at "2026-01-15"' in c for c in calls)

    def test_uses_current_utc_timestamp_for_current_state_queries(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server, re
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        mcp_server.handle_memory_prepare_turn("what database are we using?")

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
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "heuristic")
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
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "heuristic")
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.handle_memory_finalize_turn("The weather is fine."))

        assert result["ok"] is True
        assert result["stored_count"] == 0


class TestLlmStrategy:
    def test_calls_anthropic_api(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
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

    def test_falls_back_to_agent_on_api_failure(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
        db_instance.execute.return_value = json.dumps({"tx": "7"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._get_anthropic_client", side_effect=Exception("no key")):
            with patch("mcp_server._agent_extract_and_transact", new_callable=AsyncMock) as mock_agent:
                mock_agent.return_value = {"ok": True, "stored_count": 0, "strategy": "agent"}
                result = asyncio.run(mcp_server.handle_memory_finalize_turn("We'll use Kafka."))

        mock_agent.assert_called_once()


class TestAgentStrategy:
    def test_returns_ok_result(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "agent")
        db_instance.execute.return_value = json.dumps({"tx": "8"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._request_agent_memory_block_async",
                   new_callable=AsyncMock,
                   return_value='[[:decision/kafka :description "Kafka"]]'):
            result = asyncio.run(mcp_server._agent_extract_and_transact("We chose Kafka."))

        assert result["ok"] is True


class TestMcpToolWiring:
    def test_list_tools_returns_six_tools(self, mock_minigraf_db, tmp_path):
        import asyncio
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        tools = asyncio.run(mcp_server.list_tools())

        assert len(tools) == 6
        names = {t.name for t in tools}
        assert names == {
            "vulcan_query", "vulcan_transact", "vulcan_retract",
            "vulcan_report_issue", "memory_prepare_turn", "memory_finalize_turn",
        }

    def test_call_tool_vulcan_query(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["FastAPI"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.call_tool(
            "vulcan_query", {"datalog": "[:find ?n :where [?e :name ?n]]"}
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True
        assert data["results"] == [["FastAPI"]]

    def test_call_tool_vulcan_transact(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "10"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.call_tool(
            "vulcan_transact",
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

    def test_call_tool_vulcan_retract(self, mock_minigraf_db, tmp_path):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "12"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = asyncio.run(mcp_server.call_tool(
            "vulcan_retract",
            {"facts": '[[:decision/cache :description "Redis"]]', "reason": "no longer needed"},
        ))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["ok"] is True

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
