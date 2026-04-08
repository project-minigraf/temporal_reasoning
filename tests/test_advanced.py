"""
Tests for functions not covered by test_minigraf_tool.py:
  - get_graph_path()
  - export()
  - import_data() — valid data, failed transact, malformed/unsafe facts
  - HTTP mode (_run_http via MINIGRAF_MODE=http)
  - report_issue — gh available, gh unavailable, invalid issue type
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import minigraf_tool
from minigraf_tool import get_graph_path, export, import_data
from report_issue import report_issue


# ---------------------------------------------------------------------------
# get_graph_path()
# ---------------------------------------------------------------------------

def test_get_graph_path_returns_string():
    path = get_graph_path()
    assert isinstance(path, str)
    assert len(path) > 0


def test_get_graph_path_env_override(tmp_path):
    custom = str(tmp_path / "custom.graph")
    with patch.dict(os.environ, {"MINIGRAF_GRAPH_PATH": custom}):
        # Re-evaluate the default path using the internal helper directly
        result = minigraf_tool._get_default_graph_path()
    assert result == custom


# ---------------------------------------------------------------------------
# export()
# ---------------------------------------------------------------------------

def test_export_missing_graph(tmp_path):
    path = str(tmp_path / "nonexistent.graph")
    result = export(graph_path=path)
    assert not result["ok"]
    assert "No graph file" in result["error"]


def test_export_returns_expected_shape(mock_minigraf, temp_graph):
    # Make the graph file exist
    open(temp_graph, "w").close()
    # Mock query response: one fact row
    mock_minigraf.return_value = MagicMock(
        returncode=0,
        stdout="?e | ?a | ?v\n---\n:ent | :attr | \"val\"\n",
        stderr=""
    )
    result = export(graph_path=temp_graph)
    assert result["ok"]
    data = result["data"]
    assert "version" in data
    assert "exported_at" in data
    assert "facts" in data
    assert isinstance(data["facts"], list)


# ---------------------------------------------------------------------------
# import_data()
# ---------------------------------------------------------------------------

def test_import_data_empty_facts():
    result = import_data({"facts": []})
    assert not result["ok"]
    assert "No facts to import" in result["error"]


def test_import_data_missing_facts_key():
    result = import_data({})
    assert not result["ok"]


def test_import_data_valid(mock_minigraf, temp_graph):
    mock_minigraf.return_value = MagicMock(
        returncode=0,
        stdout="Transacted successfully (tx: 1)",
        stderr=""
    )
    data = {"facts": [[":e", ":attr", '"value"']]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 1
    assert result["failed"] == 0


def test_import_data_failed_transact(mock_minigraf, temp_graph):
    mock_minigraf.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="some error"
    )
    data = {"facts": [[":e", ":attr", '"value"']]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 0
    assert result["failed"] == 1


def test_import_data_malformed_fact(temp_graph):
    # Fact with fewer than 3 elements — should be skipped
    data = {"facts": [[":e", ":attr"]]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 0
    assert result["failed"] == 1


def test_import_data_unsafe_token(temp_graph):
    # Injection attempt — should be rejected by _safe_datalog_token
    data = {"facts": [[":e]] [[:injected :x :y", ":attr", '"value"']]}
    result = import_data(data, graph_path=temp_graph)
    assert result["ok"]
    assert result["imported"] == 0
    assert result["failed"] == 1


# ---------------------------------------------------------------------------
# HTTP mode
# ---------------------------------------------------------------------------

def test_run_http_success():
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"results": [["val"]]}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("minigraf_tool.urllib.request.urlopen", return_value=mock_response):
        result = minigraf_tool._run_http("query", {"datalog": "[:find ?x :where [?e :a ?x]]"})

    assert result["ok"]
    assert "data" in result


def test_http_mode_query_calls_run_http(temp_graph):
    open(temp_graph, "w").close()
    with patch.dict(os.environ, {"MINIGRAF_MODE": "http"}):
        # Reload the mode variable for this call
        with patch("minigraf_tool._run_http") as mock_http:
            mock_http.return_value = {"ok": True, "data": {"results": []}}
            # Access MINIGRAF_MODE at call time via the module global
            saved = minigraf_tool.MINIGRAF_MODE
            minigraf_tool.MINIGRAF_MODE = "http"
            try:
                result = minigraf_tool.query(
                    "[:find ?x :where [?e :a ?x]]", graph_path=temp_graph
                )
            finally:
                minigraf_tool.MINIGRAF_MODE = saved
    mock_http.assert_called_once()
    assert result["ok"]


# ---------------------------------------------------------------------------
# report_issue
# ---------------------------------------------------------------------------

def test_report_issue_invalid_type():
    result = report_issue("not_a_valid_type", "some description")
    assert not result["ok"]
    assert "Invalid issue_type" in result["error"]


def test_report_issue_gh_unavailable():
    with patch("report_issue._check_gh_available", return_value=False):
        result = report_issue("parse_error", "test issue")
    assert result["ok"]
    assert result["method"] == "log"


def test_report_issue_no_repo(tmp_path):
    with patch("report_issue._check_gh_available", return_value=True), \
         patch("report_issue._get_current_repo", return_value=None), \
         patch("report_issue._is_minigraf_related", return_value=False):
        result = report_issue("parse_error", "test issue")
    assert result["ok"]
    assert result["method"] == "log"


def test_report_issue_gh_success():
    mock_run = MagicMock()
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="https://github.com/owner/repo/issues/1",
        stderr=""
    )
    with patch("report_issue._check_gh_available", return_value=True), \
         patch("report_issue._get_current_repo", return_value={"owner": "owner", "name": "repo"}), \
         patch("report_issue.subprocess.run", mock_run):
        result = report_issue("parse_error", "test issue")
    assert result["ok"]
    assert result["method"] == "gh"
    assert "github.com" in result["result"]


def test_report_issue_minigraf_bug_routes_to_minigraf_repo():
    mock_run = MagicMock()
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="https://github.com/adityamukho/minigraf/issues/1",
        stderr=""
    )
    with patch("report_issue._check_gh_available", return_value=True), \
         patch("report_issue.subprocess.run", mock_run):
        result = report_issue("minigraf_bug", "core engine bug")
    assert result["ok"]
    assert result["repo"] == "adityamukho/minigraf"
