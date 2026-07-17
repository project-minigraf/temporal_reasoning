"""Unit tests for fact_index.py. Real sqlite3 only -- never mocked."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fact_index


def test_index_path_for_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIGRAF_INDEX_PATH", raising=False)
    graph_path = str(tmp_path / "memory.graph")
    assert fact_index.index_path_for(graph_path) == graph_path + ".fts.sqlite3"


def test_index_path_for_env_override(tmp_path, monkeypatch):
    override = str(tmp_path / "custom.sqlite3")
    monkeypatch.setenv("MINIGRAF_INDEX_PATH", override)
    assert fact_index.index_path_for(str(tmp_path / "memory.graph")) == override


def test_open_writer_creates_schema(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='facts_fts'"
        ).fetchall()
        assert rows
    finally:
        fact_index.close_writer(con)


def test_open_writer_is_idempotent(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con1 = fact_index.open_writer(path)
    fact_index.close_writer(con1)
    con2 = fact_index.open_writer(path)  # must not raise "table already exists"
    fact_index.close_writer(con2)


def test_insert_and_read_back(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(con, [(":decision/use-redis", ":description", "use redis for caching")])
        con.commit()
        rows = con.execute("SELECT entity, attribute, value FROM facts_fts").fetchall()
        assert rows == [(":decision/use-redis", ":description", "use redis for caching")]
    finally:
        fact_index.close_writer(con)


def test_delete_removes_matching_row(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/use-redis", ":description", "use redis for caching")
        fact_index.insert_facts(con, [triple])
        con.commit()
        fact_index.delete_facts(con, [triple])
        con.commit()
        rows = con.execute("SELECT * FROM facts_fts").fetchall()
        assert rows == []
    finally:
        fact_index.close_writer(con)


def test_delete_only_removes_exact_match(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(con, [
            (":decision/a", ":description", "keep me"),
            (":decision/b", ":description", "delete me"),
        ])
        con.commit()
        fact_index.delete_facts(con, [(":decision/b", ":description", "delete me")])
        con.commit()
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
        assert rows == [(":decision/a",)]
    finally:
        fact_index.close_writer(con)


def test_open_reader_sees_writer_commits(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    writer = fact_index.open_writer(path)
    fact_index.insert_facts(writer, [(":decision/x", ":description", "hello")])
    writer.commit()
    reader = fact_index.open_reader(path)
    try:
        rows = reader.execute("SELECT entity FROM facts_fts").fetchall()
        assert rows == [(":decision/x",)]
    finally:
        reader.close()
        fact_index.close_writer(writer)


def test_open_reader_missing_file_raises():
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        fact_index.open_reader("/nonexistent/path/does-not-exist.sqlite3")


def test_query_facts_ranks_by_relevance(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":decision/use-redis", ":description", "use redis for caching layer"),
        (":function/unrelated", ":name", "some other thing entirely"),
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=10, boost=2.0)
    assert results
    assert results[0][0] == ":decision/use-redis"


def test_query_facts_excludes_non_matching_rows(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [(":function/unrelated", ":name", "some other thing entirely")])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=10, boost=2.0)
    assert results == []


def test_query_facts_respects_top_n(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (f":decision/x{i}", ":description", "redis caching option") for i in range(5)
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=2, boost=2.0)
    assert len(results) == 2


def test_query_facts_boosts_memory_prefixed_entities():
    """#141 regression test: a :decision/-prefixed fact must rank above a
    non-memory fact with otherwise identical text. This is the boost that
    never fired against real data in the old FactIndex._is_memory (it
    checked minigraf's internal UUID, never the keyword ident) -- here the
    entity column is always the real ident, since callers supply it
    directly rather than re-deriving it from a Datalog rescan."""
    import tempfile
    import os as _os
    fd, path = tempfile.mkstemp(suffix=".fts.sqlite3")
    _os.close(fd)
    _os.remove(path)  # let open_writer create it fresh
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":function/caching_helper", ":name", "redis caching helper function"),
        (":decision/redis", ":description", "redis caching helper function"),
    ])
    fact_index.close_writer(con)
    try:
        results = fact_index.query_facts(path, "redis caching helper function", top_n=10, boost=2.0)
        assert results[0][0] == ":decision/redis"
    finally:
        _os.remove(path)


def test_query_facts_missing_index_raises():
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        fact_index.query_facts("/nonexistent/does-not-exist.sqlite3", "anything", top_n=10, boost=2.0)
