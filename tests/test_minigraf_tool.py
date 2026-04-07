import pytest
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from minigraf_tool import query, transact, reset


@pytest.fixture
def temp_graph():
    """Create a temporary graph file for testing."""
    fd, graph_path = tempfile.mkstemp(suffix=".graph")
    os.close(fd)
    os.remove(graph_path)
    yield graph_path
    if os.path.exists(graph_path):
        os.remove(graph_path)


def test_recall_accuracy(temp_graph):
    """Test: Can we retrieve stored decisions?"""
    transact(
        "[[:project/cache :project/name \"distributed-cache\"] "
        "[:project/cache :project/priority \"low-latency\"] "
        "[:project/cache :decision/description \"use Redis\"]]",
        reason="Initial architecture decision",
        graph_path=temp_graph
    )
    
    result = query(
        "[:find ?priority :where [?e :project/priority ?priority]]",
        graph_path=temp_graph
    )
    
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert len(result["results"]) > 0, "No results returned"
    assert any("low-latency" in str(r) for r in result["results"])


def test_dependency_query(temp_graph):
    """Test: Can we find what components exist?"""
    transact(
        "[[:component/auth :component/name \"AuthService\"] "
        "[:component/auth :calls :component/jwt]]",
        reason="Component dependency",
        graph_path=temp_graph
    )
    
    result = query(
        "[:find ?name :where [?e :component/name ?name]]",
        graph_path=temp_graph
    )
    
    assert result["ok"], f"Query failed: {result.get('error')}"
    assert any("AuthService" in str(r) for r in result["results"])


def test_temporal_query(temp_graph):
    """Test: Can we query at a specific transaction time?"""
    transact(
        "[[:test :person/name \"Alice\"]]",
        reason="Initial setup",
        graph_path=temp_graph
    )
    
    result = query(
        "[:find ?name :as-of 1 :where [?e :person/name ?name]]",
        graph_path=temp_graph
    )
    
    assert result["ok"], f"Temporal query failed: {result.get('error')}"


def test_reason_required(temp_graph):
    """Test: transact requires reason parameter."""
    result = transact(
        "[[:test :person/name \"Alice\"]]",
        reason=None,
        graph_path=temp_graph
    )
    
    assert not result["ok"], "transact should fail without reason"
    assert "reason is required for all writes" in result.get("error", "")
    
    result_empty = transact(
        "[[:test :person/name \"Bob\"]]",
        reason="",
        graph_path=temp_graph
    )
    
    assert not result_empty["ok"], "transact should fail with empty reason"
    assert "reason is required for all writes" in result_empty.get("error", "")


def test_reset(temp_graph):
    """Test: reset clears the graph."""
    transact(
        "[[:test :person/name \"Test\"]]",
        reason="Setup for reset test",
        graph_path=temp_graph
    )
    
    assert os.path.exists(temp_graph), "Graph should exist after transact"
    
    result = reset(graph_path=temp_graph)
    assert result["ok"], "Reset should succeed"
    assert result.get("deleted") is not None, "Reset should return deleted path"
