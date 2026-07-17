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
