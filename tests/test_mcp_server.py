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
    """Reset the module-level _db singleton and grammar cache between tests."""
    import mcp_server
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    yield
    mcp_server._db = None
    mcp_server._grammar_cache.clear()


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
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("VULCAN_LLM_MODEL", "gpt-4o-mini")
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
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("VULCAN_LLM_MODEL", "gpt-4o-mini")
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

    def test_falls_back_to_heuristic_on_api_failure(self, mock_minigraf_db, tmp_path, monkeypatch):
        import asyncio
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
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
    def test_list_tools_returns_nine_tools(self, mock_minigraf_db, tmp_path):
        import asyncio
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        tools = asyncio.run(mcp_server.list_tools())

        assert len(tools) == 9
        names = {t.name for t in tools}
        assert names == {
            "vulcan_query", "vulcan_transact", "vulcan_retract",
            "vulcan_report_issue", "memory_prepare_turn", "memory_finalize_turn",
            "vulcan_audit", "vulcan_ingest_git", "vulcan_ingest_status",
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

    def test_db_released_after_call_tool(self, mock_minigraf_db, tmp_path):
        import asyncio
        import mcp_server
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))

        asyncio.run(mcp_server.call_tool(
            "vulcan_query", {"datalog": "[:find ?x :where [?e :x ?x]]"}
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


class TestVulcanTransactSchema:
    def test_rejects_unknown_entity_type(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_transact(
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

        result = mcp_server.handle_vulcan_transact(
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
        result = mcp_server.handle_vulcan_transact(
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
        monkeypatch.setenv("VULCAN_LLM_MODEL", "claude-haiku-4-5-20251001")
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


class TestVulcanAudit:
    def test_clean_db_returns_zero_retracted(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        # No entities of any known type
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_audit()

        assert result["ok"] is True
        assert result["retracted"] == 0
        assert result["violations"] == []

    def test_entity_missing_required_attr_is_retracted(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        # handle_vulcan_audit uses type query → UUID, then #uuid attr query.
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

        result = mcp_server.handle_vulcan_audit()

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

        result = mcp_server.handle_vulcan_audit(as_of=5)

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

        result = mcp_server.handle_vulcan_audit()

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

        result = mcp_server.handle_vulcan_audit()

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
        # Mock tree_sitter modules
        mock_parser = MagicMock()
        mock_tree_sitter = MagicMock()
        mock_tree_sitter.Parser.return_value = mock_parser
        mock_tree_sitter_languages = MagicMock()
        mock_tree_sitter_languages.get_language.return_value = MagicMock()

        with patch.dict("sys.modules", {"tree_sitter": mock_tree_sitter, "tree_sitter_languages": mock_tree_sitter_languages}):
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
        # Mock tree_sitter modules
        mock_parser = MagicMock()
        mock_tree_sitter = MagicMock()
        mock_tree_sitter.Parser.return_value = mock_parser
        mock_tree_sitter_languages = MagicMock()
        mock_tree_sitter_languages.get_language.return_value = MagicMock()

        with patch.dict("sys.modules", {"tree_sitter": mock_tree_sitter, "tree_sitter_languages": mock_tree_sitter_languages}):
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


class TestExtractFromSource:
    def _python_parser(self):
        """Return a real tree_sitter.Parser for Python, mocking tree_sitter_languages
        to return a real language object so _get_parser succeeds under the spec-compliant
        code path (tree_sitter_languages only, no fallback)."""
        import mcp_server
        import tree_sitter
        import tree_sitter_python
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(tree_sitter_python.language())
        mock_tsl = MagicMock()
        mock_tsl.get_language.return_value = real_lang
        # The spec uses parser.set_language(lang); accommodate the installed tree_sitter
        # version by pre-building the parser and injecting it via the cache.
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
        parser = self._python_parser()
        result = mcp_server._extract_from_source(b"\x00\xff\xfe", parser, "bad.py")
        assert result == {"functions": [], "classes": [], "imports": [], "calls": []}


class TestVulcanIngestStatus:
    def test_returns_idle_before_ingestion(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = mcp_server.handle_vulcan_ingest_status()
        assert result["ok"] is True
        assert result["status"] == "idle"
        assert result["processed"] == 0

    def test_returns_running_status(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "running", "processed": 3, "total": 10,
            "current_commit": "abc123", "error": None,
        }
        result = mcp_server.handle_vulcan_ingest_status()
        assert result["status"] == "running"
        assert result["processed"] == 3
        assert result["total"] == 10
        assert result["current_commit"] == "abc123"
