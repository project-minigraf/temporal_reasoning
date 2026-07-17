# Persisted Fact Index (#118) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-memory `rank-bm25` `FactIndex`/`IndexCache` singleton (always cold in the short-lived `UserPromptSubmit` hook process) with a SQLite FTS5 sidecar file, written incrementally on every graph write and shared between the hook and the MCP server via the OS page cache — no RPC, no shared Python object.

**Architecture:** A new `fact_index.py` module owns the SQLite FTS5 file (schema, connection lifecycle, CRUD, ranked query, full rebuild). `mcp_server.py` gains two choke-point functions, `_transact`/`_retract`, that every one of the 12 existing raw `_db_execute` transact/retract call sites is migrated through; each write updates `facts_fts` in the same step it writes to minigraf. `handle_memory_prepare_turn` is rewritten to query the index file directly instead of an in-process singleton.

**Tech Stack:** Python stdlib `sqlite3` (FTS5), no new dependencies. `rank-bm25` is removed.

## Global Constraints

- Design source of truth: `docs/superpowers/specs/2026-07-17-persisted-fact-index-design.md` — re-read it if any task here seems to contradict it; the spec wins.
- No new dependency: only stdlib `sqlite3`. `rank-bm25` is deleted from `pyproject.toml`'s core `dependencies`.
- Testing convention (`docs/testing-conventions.md`, extended by this plan): every test uses a real `sqlite3` file (`:memory:` for fast unit tests, a real `tmp_path` file for persistence/cross-process tests) and a real `MiniGrafDb` (`real_db` fixture or a real file-backed DB) — never `MagicMock`. Never assert on mock call arguments; always re-query and assert on actual data.
- Python floor: `>=3.10` (unchanged).
- `_transact`/`_retract` signature (from the spec, decoupled): `datalog_facts: str`, `index_triples: Optional[List[Tuple[str,str,str]]] = None` (auto-derived from `datalog_facts` via `_parse_facts_block` when omitted), `index_con: Optional[sqlite3.Connection] = None` (caller-controlled commit boundary for batching; omitted means open/write/commit/close immediately).
- Bi-temporal rule: only `valid_to is None` (open-ended) transacts get inserted into `facts_fts`. Bounded (`valid_to` set) re-transacts are historical and are never inserted.
- Every commit in this plan follows the existing repo convention: small, TDD (RED before implementation), real backend.

---

## File Structure

- **Create:** `fact_index.py` — the SQLite FTS5 module. Public surface: `index_path_for`, `open_writer`, `open_reader`, `close_writer`, `ensure_schema`, `insert_facts`, `delete_facts`, `query_facts`, `rebuild_index`. Owns `_MEMORY_PREFIXES` and its own `_tokenize` (moved from `mcp_server.py`, which loses both once `FactIndex` is deleted).
- **Create:** `tests/test_fact_index.py` — unit tests for the new module, real `sqlite3` only.
- **Modify:** `mcp_server.py` — add `_parse_facts_block`, `_transact`, `_retract`; migrate 12 call sites; rewrite `handle_memory_prepare_turn`; delete `FactIndex`, `IndexCache`, `_index_cache`, `_handle_memory_prepare_turn_heuristic`, the `_BM25_AVAILABLE` branch, `_tokenize`, `_MEMORY_PREFIXES`, `_MEMORY_ENTITY_TYPES`, and the `rank_bm25` import.
- **Modify:** `pyproject.toml` — remove `rank-bm25` from `dependencies`; add `fact_index` to `[tool.setuptools] py-modules`.
- **Modify:** `install.py` — remove any leftover `bm25`-extra references in `.mcp.json` generation (check; #117 may have already removed them).
- **Modify:** `tests/test_mcp_server.py` — remove the `_index_cache`-related fixture logic, the `_HAS_RANK_BM25`/`requires_bm25` marker, all `monkeypatch.setattr(mcp_server._index_cache, "invalidate", lambda: None)` call sites, and replace `TestIndexCache`/`TestMemoryPrepareTurnBM25` with tests against the new architecture.
- **Modify:** `docs/testing-conventions.md` — document the new real-`sqlite3`-file pattern.
- **Modify:** `SKILL.md`, `CLAUDE.md` — mention `MINIGRAF_INDEX_PATH`.

---

### Task 1: `fact_index.py` — schema, connection lifecycle, CRUD primitives

**Files:**
- Create: `fact_index.py`
- Modify: `pyproject.toml` (add `fact_index` to `py-modules`, same commit — this is scaffolding the task's own deliverable needs)
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Produces: `index_path_for(graph_path: str) -> str`; `open_writer(path: str) -> sqlite3.Connection`; `open_reader(path: str) -> sqlite3.Connection`; `close_writer(con: sqlite3.Connection) -> None`; `ensure_schema(con: sqlite3.Connection) -> None`; `insert_facts(con, triples: Sequence[Tuple[str,str,str]]) -> None`; `delete_facts(con, triples: Sequence[Tuple[str,str,str]]) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fact_index.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fact_index.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fact_index'`

- [ ] **Step 3: Write `fact_index.py`**

```python
#!/usr/bin/env python3
"""Persisted, mmap-able SQLite FTS5 fact index.

Shared, via the OS page cache, between the MCP server process and the
UserPromptSubmit hook process -- both open this same file directly, with no
RPC and no shared Python object between them. See
docs/superpowers/specs/2026-07-17-persisted-fact-index-design.md for the
full design rationale.
"""
import os
import re
import sqlite3
from typing import List, Optional, Sequence, Tuple

# Same categories mcp_server.py's write paths use to decide which entities
# get the memory-fact boost at query time. Kept here (not imported from
# mcp_server) to avoid a circular import -- mcp_server.py imports this module.
_MEMORY_PREFIXES = (":decision/", ":preference/", ":constraint/", ":dependency/")

_MMAP_SIZE = 1_073_741_824  # 1 GiB
_BUSY_TIMEOUT_MS = 5000


def index_path_for(graph_path: str) -> str:
    """Return the sidecar index path for a given graph path.

    MINIGRAF_INDEX_PATH overrides the default `<graph_path>.fts.sqlite3`,
    mirroring the MINIGRAF_GRAPH_PATH convention in mcp_server.py.
    """
    override = os.environ.get("MINIGRAF_INDEX_PATH")
    if override:
        return override
    return f"{graph_path}.fts.sqlite3"


def _configure(con: sqlite3.Connection) -> None:
    con.execute(f"PRAGMA mmap_size={_MMAP_SIZE}")
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create facts_fts if it doesn't exist yet. Idempotent and safe under
    concurrent callers (IF NOT EXISTS + busy_timeout serializes racers)."""
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
        "entity, attribute, value, tokenize='unicode61')"
    )
    con.commit()


def open_writer(path: str) -> sqlite3.Connection:
    """Open a read-write connection, WAL-enabled, schema ensured."""
    con = sqlite3.connect(path, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    _configure(con)
    ensure_schema(con)
    return con


def open_reader(path: str) -> sqlite3.Connection:
    """Open a read-only connection against an existing index file.

    Raises sqlite3.OperationalError if the file doesn't exist -- callers
    (mcp_server.handle_memory_prepare_turn) catch this and trigger a
    backfill rebuild, then retry.
    """
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    _configure(con)
    return con


def close_writer(con: sqlite3.Connection) -> None:
    con.commit()
    con.close()


def insert_facts(con: sqlite3.Connection, triples: Sequence[Tuple[str, str, str]]) -> None:
    """Insert rows into facts_fts. Does not commit -- caller controls the
    transaction boundary (immediate for single-fact writes, batched per
    ingestion-commit for git ingestion)."""
    if not triples:
        return
    con.executemany(
        "INSERT INTO facts_fts (entity, attribute, value) VALUES (?, ?, ?)", triples
    )


def delete_facts(con: sqlite3.Connection, triples: Sequence[Tuple[str, str, str]]) -> None:
    """Delete matching rows from facts_fts. Does not commit -- see insert_facts."""
    if not triples:
        return
    con.executemany(
        "DELETE FROM facts_fts WHERE entity = ? AND attribute = ? AND value = ?", triples
    )
```

- [ ] **Step 4: Add `fact_index` to `pyproject.toml`'s `py-modules`**

In `pyproject.toml`, find:
```toml
[tool.setuptools]
py-modules = ["mcp_server", "report_issue"]
```
Replace with:
```toml
[tool.setuptools]
py-modules = ["mcp_server", "report_issue", "fact_index"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_fact_index.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add fact_index.py tests/test_fact_index.py pyproject.toml
git commit -m "feat: add fact_index.py — SQLite FTS5 schema and CRUD primitives (#118)"
```

---

### Task 2: `fact_index.py` — ranked query with memory-fact boost

**Files:**
- Modify: `fact_index.py`
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Consumes: `open_reader`, `insert_facts`, `open_writer`, `close_writer` (Task 1)
- Produces: `query_facts(path: str, text: str, top_n: int, boost: float) -> List[List[str]]` — returns `[entity, attribute, value]` rows, best match first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fact_index.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fact_index.py -v -k query_facts`
Expected: FAIL with `AttributeError: module 'fact_index' has no attribute 'query_facts'`

- [ ] **Step 3: Implement `query_facts`**

Add to `fact_index.py`, after `delete_facts`:

```python
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Split text on non-alphanumeric chars, lowercase, filter empties."""
    return _TOKEN_PATTERN.findall(text.lower())


def _fts5_match_query(text: str) -> Optional[str]:
    """Build an FTS5 MATCH expression that matches ANY query token (OR
    semantics), matching the "any token overlap" relevance model the old
    rank_bm25-based FactIndex used. Returns None if there are no usable
    tokens. Each token is double-quoted to neutralize FTS5 special syntax
    characters a raw user message could otherwise trigger."""
    tokens = _tokenize(text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def query_facts(path: str, text: str, top_n: int, boost: float) -> List[List[str]]:
    """Ranked, read-only query against the index.

    Returns up to top_n [entity, attribute, value] rows, best match first.
    Facts whose entity starts with a memory-fact prefix (_MEMORY_PREFIXES)
    get their score multiplied by boost. FTS5's bm25() is negative-is-better
    (SQLite convention, opposite of rank_bm25) -- multiplying a negative
    score by boost > 1 makes it more negative, i.e. better, so this has the
    same boosting effect as the old FactIndex's positive-score multiply.

    Raises sqlite3.OperationalError if the index file doesn't exist -- the
    caller (mcp_server.handle_memory_prepare_turn) is responsible for
    triggering a backfill and retrying.
    """
    match_expr = _fts5_match_query(text)
    if match_expr is None:
        return []
    con = open_reader(path)
    try:
        rows = con.execute(
            "SELECT entity, attribute, value, bm25(facts_fts) AS score "
            "FROM facts_fts WHERE facts_fts MATCH ? "
            "ORDER BY score ASC LIMIT ?",
            (match_expr, top_n * 4),  # over-fetch before boost re-sort, trimmed below
        ).fetchall()
    finally:
        con.close()
    scored = []
    for entity, attribute, value, score in rows:
        if entity.startswith(_MEMORY_PREFIXES):
            score *= boost
        scored.append((score, [entity, attribute, value]))
    scored.sort(key=lambda pair: pair[0])
    return [row for _, row in scored[:top_n]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fact_index.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "feat: add ranked FTS5 query with memory-fact boost to fact_index.py (#118, #141)"
```

---

### Task 3: `fact_index.py` — full rebuild (backfill)

**Files:**
- Modify: `fact_index.py`
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Produces: `rebuild_index(path: str, facts: Sequence[Tuple[str,str,str]]) -> None` — drops and recreates `facts_fts`, bulk-inserts `facts`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fact_index.py`:

```python
def test_rebuild_index_creates_fresh_table(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/x", ":description", "hello world")])
    results = fact_index.query_facts(path, "hello", top_n=10, boost=2.0)
    assert len(results) == 1
    assert results[0][0] == ":decision/x"


def test_rebuild_index_replaces_existing_data(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/old", ":description", "old fact")])
    fact_index.rebuild_index(path, [(":decision/new", ":description", "new fact")])
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == [(":decision/new",)]


def test_rebuild_index_empty_facts(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [])
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT * FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == []


def test_concurrent_rebuild_race_is_safe(tmp_path):
    """Two processes racing to backfill the same missing index file (e.g.
    two hook invocations firing close together) must not corrupt the file
    or raise -- CREATE VIRTUAL TABLE IF NOT EXISTS + busy_timeout means the
    second racer just waits and finds the table already there."""
    import subprocess
    import sys as _sys
    path = str(tmp_path / "t.fts.sqlite3")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import fact_index\n"
        "fact_index.rebuild_index(%r, [(':decision/x', ':description', 'concurrent')])\n"
    ) % (str(tmp_path.parent.parent), path)
    # Run two rebuilds concurrently against the same path.
    p1 = subprocess.Popen([_sys.executable, "-c", script])
    p2 = subprocess.Popen([_sys.executable, "-c", script])
    assert p1.wait(timeout=10) == 0
    assert p2.wait(timeout=10) == 0
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == [(":decision/x",)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fact_index.py -v -k rebuild`
Expected: FAIL with `AttributeError: module 'fact_index' has no attribute 'rebuild_index'`

- [ ] **Step 3: Implement `rebuild_index`**

Add to `fact_index.py`, after `query_facts`:

```python
def rebuild_index(path: str, facts: Sequence[Tuple[str, str, str]]) -> None:
    """Full rebuild: drop and recreate facts_fts, then bulk-insert facts.

    Used for backfill (index file missing -- fresh install, pre-existing
    graph, or corruption recovery). CREATE VIRTUAL TABLE IF NOT EXISTS makes
    this safe under a concurrent racing rebuild from another process: the
    second caller's DROP+CREATE+INSERT still runs to completion under
    SQLite's own locking (busy_timeout), it just ends up re-doing work
    rather than corrupting anything.
    """
    con = sqlite3.connect(path, timeout=5.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        _configure(con)
        con.execute("DROP TABLE IF EXISTS facts_fts")
        ensure_schema(con)
        insert_facts(con, facts)
        con.commit()
    finally:
        con.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fact_index.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "feat: add rebuild_index backfill to fact_index.py (#118)"
```

---

### Task 4: `mcp_server.py` — `_parse_facts_block` and the `_transact`/`_retract` choke point

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `fact_index.open_writer`, `fact_index.close_writer`, `fact_index.insert_facts`, `fact_index.delete_facts`, `fact_index.index_path_for` (Task 1); `_db_execute`, `_get_graph_path`, `_graph_path` (existing).
- Produces: `_parse_facts_block(facts_str: str) -> List[Tuple[str,str,str]]`; `_transact(db, datalog_facts, valid_from, valid_to=None, index_triples=None, index_con=None) -> str`; `_retract(db, datalog_facts, index_triples=None, index_con=None) -> str`. These are what every later migration task calls.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` (new class, place near `TestMinigrafTransact`):

```python
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
            real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0)
        assert results == [[":decision/x", ":description", "hello"]]

    def test_transact_writes_to_minigraf(self, real_db):
        import mcp_server
        mcp_server._transact(
            real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z",
        )
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:decision/x :description ?v]])')
        import json
        assert json.loads(raw)["results"] == [["hello"]]

    def test_transact_with_valid_to_does_not_index(self, real_db):
        """Bounded (historical) transacts must not appear in the live index."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[:decision/x :description "hello"]',
            "2025-01-01T00:00:00.000Z", valid_to="2025-06-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0)
        assert results == []

    def test_retract_removes_from_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._transact(real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z")
        mcp_server._retract(real_db, '[[:decision/x :description "hello"]]')
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0)
        assert results == []

    def test_retract_removes_from_minigraf(self, real_db):
        import mcp_server
        import json
        mcp_server._transact(real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z")
        mcp_server._retract(real_db, '[[:decision/x :description "hello"]]')
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:decision/x :description ?v]])')
        assert json.loads(raw)["results"] == []

    def test_transact_explicit_index_triples_overrides_auto_derive(self, real_db):
        """handle_minigraf_audit's use case: the Datalog string references a
        #uuid literal, but the index should record the resolved keyword ident."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z",
            index_triples=[(":decision/explicit-override", ":description", "hello")],
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0)
        assert results == [[":decision/explicit-override", ":description", "hello"]]

    def test_transact_index_write_failure_does_not_raise(self, real_db, monkeypatch):
        """Index maintenance must never block a graph write -- mirrors
        IndexCache._rebuild's existing try/except at the call site."""
        import mcp_server
        import fact_index
        monkeypatch.setattr(fact_index, "open_writer", lambda path: (_ for _ in ()).throw(OSError("disk full")))
        # Must not raise despite the index write failing.
        mcp_server._transact(real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z")
        raw = mcp_server._db_execute(real_db, '(query [:find ?v :where [:decision/x :description ?v]])')
        import json
        assert json.loads(raw)["results"] == [["hello"]]  # the graph write still succeeded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k "ParseFactsBlock or TransactRetractChokePoint"`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_parse_facts_block'`

- [ ] **Step 3: Implement `_parse_facts_block`, `_transact`, `_retract`**

In `mcp_server.py`, add `import fact_index` near the top with the other local imports (after `from minigraf import MiniGrafDb, MiniGrafError` at line 29). Then insert the following immediately after `handle_minigraf_query` and before `handle_minigraf_transact` (currently lines 3043–3046):

```python
_FACTS_TRIPLE_PATTERN = re.compile(
    r'\[(\:[^\s\]]+)\s+(\:[^\s\]]+)\s+("(?:[^"\\]|\\.)*"|\:[^\s\]]+)\]'
)


def _parse_facts_block(facts_str: str) -> List[Tuple[str, str, str]]:
    """Parse every [entity attribute value] triple out of a Datalog facts
    block or a single triple string -- scans for all matches rather than
    requiring a strict split, so it works on both shapes uniformly (mirrors
    _parse_transact_facts' existing regex-scan approach, extended to also
    capture keyword-valued triples, which schema validation intentionally
    skips but the index must not). Value is unquoted for string-valued
    triples, kept as-is (a keyword or entity reference) otherwise.
    """
    triples = []
    for m in _FACTS_TRIPLE_PATTERN.finditer(facts_str):
        entity, attribute, raw_value = m.groups()
        value = raw_value[1:-1] if raw_value.startswith('"') else raw_value
        triples.append((entity, attribute, value))
    return triples


def _index_write(
    action: str,
    triples: List[Tuple[str, str, str]],
    index_con: Optional[Any] = None,
) -> None:
    """Apply an insert or delete to the fact index, never raising -- index
    maintenance must never block a graph write (mirrors IndexCache._rebuild's
    existing exception handling). action is 'insert' or 'delete'. When
    index_con is provided, writes onto it without committing (caller controls
    the transaction boundary — used by ingestion's batching). Otherwise opens
    a connection, writes, commits, and closes immediately.
    """
    if not triples:
        return
    try:
        if index_con is not None:
            (fact_index.insert_facts if action == "insert" else fact_index.delete_facts)(
                index_con, triples
            )
            return
        path = fact_index.index_path_for(_graph_path or _get_graph_path())
        con = fact_index.open_writer(path)
        try:
            (fact_index.insert_facts if action == "insert" else fact_index.delete_facts)(
                con, triples
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"[fact_index] {action} failed: {e}", file=sys.stderr)


def _transact(
    db: Any,
    datalog_facts: str,
    valid_from: str,
    valid_to: Optional[str] = None,
    index_triples: Optional[List[Tuple[str, str, str]]] = None,
    index_con: Optional[Any] = None,
) -> str:
    """Execute (transact {opts} datalog_facts) against minigraf, then --
    only when valid_to is None -- write index_triples into the fact index.

    index_triples defaults to auto-parsing datalog_facts via
    _parse_facts_block(); pass it explicitly when the Datalog string's own
    entity reference isn't the searchable identity (e.g.
    handle_minigraf_audit's #uuid-tagged retracts, whose index_triples must
    use the resolved keyword ident instead).
    """
    opts = f':valid-from "{valid_from}"'
    if valid_to is not None:
        opts += f' :valid-to "{valid_to}"'
    raw = _db_execute(db, f"(transact {{{opts}}} {datalog_facts})")
    if valid_to is None:
        triples = index_triples if index_triples is not None else _parse_facts_block(datalog_facts)
        _index_write("insert", triples, index_con=index_con)
    return raw


def _retract(
    db: Any,
    datalog_facts: str,
    index_triples: Optional[List[Tuple[str, str, str]]] = None,
    index_con: Optional[Any] = None,
) -> str:
    """Execute (retract datalog_facts) against minigraf, then delete
    index_triples from the fact index (same decoupling as _transact)."""
    raw = _db_execute(db, f"(retract {datalog_facts})")
    triples = index_triples if index_triples is not None else _parse_facts_block(datalog_facts)
    _index_write("delete", triples, index_con=index_con)
    return raw


```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k "ParseFactsBlock or TransactRetractChokePoint"`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _transact/_retract choke-point functions to mcp_server.py (#118)"
```

---

### Task 5: Migrate `handle_minigraf_transact` and `handle_minigraf_retract`

**Files:**
- Modify: `mcp_server.py:3046-3094` (current line numbers; re-locate by function name — Task 4 shifted everything below it down)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_transact`, `_retract` (Task 4)

- [ ] **Step 1: Write the failing tests**

Add to the existing `TestMinigrafTransact`/`TestMinigrafRetract` classes in `tests/test_mcp_server.py`:

```python
# inside class TestMinigrafTransact:
    def test_transact_populates_fact_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis caching", top_n=10, boost=2.0)
        assert any(r[0] == ":decision/use-redis" for r in results)


# inside class TestMinigrafRetract:
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
        results = fact_index.query_facts(index_path, "redis caching", top_n=10, boost=2.0)
        assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k "test_transact_populates_fact_index or test_retract_removes_from_fact_index"`
Expected: FAIL — index still empty, since `handle_minigraf_transact`/`handle_minigraf_retract` haven't been migrated yet.

- [ ] **Step 3: Migrate both functions**

In `mcp_server.py`, replace `handle_minigraf_transact`'s body (find by function name — the `try` block currently reads `raw = _db_execute(db, f'(transact {{:valid-from "{_now_utc_ms()}"}} {facts})')` down through `_index_cache.invalidate()`):

```python
def handle_minigraf_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required.

    :valid-at is set to the current UTC ms timestamp so every agent-initiated
    write has a recorded valid time, enabling correct bi-temporal queries.
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    parsed = _parse_transact_facts(facts)
    if parsed:
        violations = _validate_facts(parsed)
        if violations:
            return {"ok": False, "error": f"schema violations: {'; '.join(violations)}"}
    _refresh_if_stale()
    db = get_db()
    try:
        raw = _transact(db, facts, _now_utc_ms())
        _db_checkpoint(db)
        _update_mtime()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_minigraf_retract(facts: str, reason: str) -> Dict[str, Any]:
    """Retract facts from the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for retract"}
    _refresh_if_stale()
    db = get_db()
    try:
        raw = _retract(db, facts)
        _db_checkpoint(db)
        _update_mtime()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}
```

Note what changed: `_db_execute(db, f'(transact {{:valid-from "..."}} {facts})')` → `_transact(db, facts, _now_utc_ms())`; `_db_execute(db, f"(retract {facts})")` → `_retract(db, facts)`; both `_index_cache.invalidate()` calls are deleted (the choke point handles indexing inline, no separate invalidation step needed).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k "TestMinigrafTransact or TestMinigrafRetract"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: migrate handle_minigraf_transact/retract to the index choke point (#118)"
```

---

### Task 6: Migrate `handle_minigraf_audit`

**Files:**
- Modify: `mcp_server.py` (function `handle_minigraf_audit`, currently lines 3133–3234; re-locate by name)
- Test: `tests/test_mcp_server.py` (existing `TestMinigrafAudit` class)

**Interfaces:**
- Consumes: `_retract` (Task 4), passing `index_triples` explicitly (the decoupled-signature case this whole design revision exists for).

- [ ] **Step 1: Write the failing test**

Add to `TestMinigrafAudit` in `tests/test_mcp_server.py`:

```python
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
        results = fact_index.query_facts(index_path, "decision bad", top_n=10, boost=2.0)
        assert not any(r[0] == ":decision/bad" for r in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py -v -k test_audit_retract_removes_from_fact_index_by_keyword_ident`
Expected: FAIL — audit doesn't touch the fact index at all yet, so the stale row (from before manual retract, if any survived) or the test's own setup assumptions won't hold. If the test errors out on setup (e.g. no violation actually detected), inspect `_validate_facts`'s exact rules in `mcp_server.py` and adjust the setup to produce a genuine violation — the assertion under test is the index-deletion behavior, not the exact violation mechanics.

- [ ] **Step 3: Migrate the retract call**

In `handle_minigraf_audit`, replace:
```python
                        retract_expr = f"(retract [{' '.join(retract_triples)}])"
                        _db_execute(db, retract_expr)
                        _db_checkpoint(db)
                        _update_mtime()
                        retracted += 1
```
with:
```python
                        retract_facts = "[" + " ".join(retract_triples) + "]"
                        index_triples = [
                            (kw_ident, ":entity-type", f":type/{entity_type}"),
                        ] + [
                            (kw_ident, a, v) for a, v in attr_rows if isinstance(v, str)
                        ]
                        _retract(db, retract_facts, index_triples=index_triples)
                        _db_checkpoint(db)
                        _update_mtime()
                        retracted += 1
```

This mirrors exactly what `retract_triples` already builds (the `#uuid`-tagged Datalog triples, unchanged, still sent to minigraf as-is), but supplies `index_triples` built from `kw_ident` — the same resolved keyword ident the function already computes at line 3183 for its own violation-reporting output — so the index deletion actually matches the row that was inserted under that ident at transact time.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py -v -k TestMinigrafAudit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: migrate handle_minigraf_audit's retract to the index choke point (#118)"
```

---

### Task 7: Migrate `_ingest_close` (both steps)

**Files:**
- Modify: `mcp_server.py` (function `_ingest_close`, currently lines 3979–4010)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_transact`, `_retract` (Task 4)
- Produces: `_ingest_close(db, triples, original_ts_iso, commit_ts_iso, reason, index_con=None)` — new optional `index_con` param, threaded through in Task 8.

This is the task the design-review process (see the spec's "Write path" section) found was the actual gap: `_ingest_close` makes two separate writes — a retract-loop (the mechanism that removes a closed entity's facts from the live index) and a bounded re-transact (historical, must NOT be re-indexed). Both must migrate correctly for a closed entity to actually disappear from the index.

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_mcp_server.py`, near the existing ingestion tests:

```python
class TestIngestCloseFactIndex:
    def test_close_removes_open_assertion_from_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._ingest_transact(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z", "test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "foo module", top_n=10, boost=2.0)
        assert results == []

    def test_close_bounded_retransact_not_indexed(self, real_db):
        """The historical (valid_to-bounded) half of a close must never
        appear in the live index -- this is the exact case an earlier draft
        of the design doc got wrong by only naming the visible half of
        _ingest_close."""
        import mcp_server
        import fact_index
        mcp_server._ingest_transact(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2026-01-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z", "test",
        )
        # Directly assert nothing at all references :module/foo post-close,
        # not just that this exact text is unmatched.
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT * FROM facts_fts WHERE entity = ?", (":module/foo",)
            ).fetchall()
        finally:
            con.close()
        assert rows == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k TestIngestCloseFactIndex`
Expected: FAIL — `:module/foo`'s facts are still in the index (`_ingest_close` hasn't been migrated).

- [ ] **Step 3: Migrate `_ingest_close`**

Replace the function body:

```python
def _ingest_close(
    db: Any,
    triples: List[str],
    original_ts_iso: str,
    commit_ts_iso: str,
    reason: str,
    index_con: Optional[Any] = None,
) -> None:
    """Close a fact's valid window at the deletion commit timestamp.

    Two-step process:
    1. Retract each original open-ended fact so it vanishes from current-time
       queries (retract has no temporal options, so this removes the unbounded
       assertion from the live view while keeping it in transaction history).
       This is also the step that removes the fact from the live index.
    2. Re-transact the same facts with explicit :valid-from + :valid-to so the
       historical valid window is preserved for point-in-time queries. Bounded
       (valid_to is not None), so _transact does not index this half.

    Triples are retracted one-by-one to avoid EAVT collision on :contains edges
    (Minigraf's pending index omits value bytes, so batching multiple
    [module :contains fn] retracts could collide).
    """
    if not triples:
        return
    for triple in triples:
        try:
            _retract(db, f"[{triple}]", index_con=index_con)
        except Exception:
            pass  # best-effort: original may not exist if preload was incomplete
    facts_str = "[" + " ".join(triples) + "]"
    _transact(
        db, facts_str, original_ts_iso, valid_to=commit_ts_iso, index_con=index_con,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k TestIngestCloseFactIndex`
Expected: PASS

- [ ] **Step 5: Run the full ingestion test suite to check for regressions**

Run: `pytest tests/test_mcp_server.py -v -k "Ingest or ingest"`
Expected: PASS, no regressions (existing ingestion tests don't touch the index at all, so they should be unaffected by this migration — this step exists to confirm that assumption).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: migrate _ingest_close to the index choke point (#118)

This is the critical migration: _ingest_close makes two separate writes
(a retract-loop, then a bounded re-transact), and both must route through
the choke point for a closed entity to actually disappear from the index."
```

---

### Task 8: Migrate `_ingest_transact` and wire batched `index_con` through `_run_ingestion`

**Files:**
- Modify: `mcp_server.py` (function `_ingest_transact`, currently lines 3966–3976; function `_run_ingestion`, currently lines 5814 onward)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_transact` (Task 4), `_ingest_close` with `index_con` (Task 7), `fact_index.open_writer`/`close_writer`/`index_path_for` (Task 1)
- Produces: `_ingest_transact(db, triples, commit_ts_iso, reason, index_con=None)`

This is the batching task: large repositories can cross 1M facts well before ingestion completes (per the scale concern raised during design review), so index writes during ingestion must be batched at commit granularity, not one SQLite transaction per triple.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
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
        results = fact_index.query_facts(index_path, "foo module", top_n=10, boost=2.0)
        assert results


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
        """
        import mcp_server
        import fact_index

        commit_calls = []
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
            return CountingConnection(original_open_writer(path))

        monkeypatch.setattr(fact_index, "open_writer", counting_open_writer)
        monkeypatch.setattr(mcp_server, "_live_lock_holder_pid", lambda path: None)

        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # 2 commits ingested -> at most 2 index commits (one per commit, plus
        # possibly one final flush), never one per triple (which would be
        # several per commit given each commit's module/function/class
        # triples, :entity-type, :ident, and :introduced-by facts).
        assert len(commit_calls) <= 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k "TestIngestTransactFactIndex or TestRunIngestionBatchedIndexWrites"`
Expected: FAIL — `_ingest_transact` doesn't accept `index_con` yet, and `_run_ingestion` doesn't open/thread one through.

- [ ] **Step 3: Migrate `_ingest_transact`**

```python
def _ingest_transact(
    db: Any,
    triples: List[str],
    commit_ts_iso: str,
    reason: str,
    index_con: Optional[Any] = None,
) -> None:
    """Transact code-structure facts with :valid-from set to the commit timestamp."""
    if not triples:
        return
    facts_str = "[" + " ".join(triples) + "]"
    _transact(db, facts_str, commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Wire a batched `index_con` through `_run_ingestion`**

In `_run_ingestion`, find the `write_executor` construction (`write_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)`, currently line 5901). Immediately after it, before the `try:` block that follows, add:

```python
        index_path = fact_index.index_path_for(_graph_path or _get_graph_path())
        index_con = await loop.run_in_executor(write_executor, fact_index.open_writer, index_path)
```

Every call to `_ingest_transact(...)` and `_ingest_close(...)` inside the per-commit loop (currently around lines 6300–6316) gets `index_con=index_con` appended to its argument list. There are 4 such calls: the `other_triples` transact, the `contains_triples` per-triple transact loop, the `dep_add_triples` per-triple transact loop, and the `close_items` close loop. For example:

```python
                        await loop.run_in_executor(
                            write_executor, _ingest_transact, db, other_triples, commit_ts_iso, reason, index_con
                        )
                        for ct in contains_triples:
                            await loop.run_in_executor(
                                write_executor, _ingest_transact, db, [ct], commit_ts_iso, reason, index_con
                            )
                        for dt in dep_add_triples:
                            await loop.run_in_executor(
                                write_executor, _ingest_transact, db, [dt], commit_ts_iso, reason, index_con
                            )
                        for close_triples, orig_ts in close_items:
                            await loop.run_in_executor(
                                write_executor, _ingest_close, db, close_triples, orig_ts, commit_ts_iso, reason, index_con
                            )
```

Immediately after the existing per-commit `await loop.run_in_executor(write_executor, _db_checkpoint, db)` call (currently line 6334), add the batched index commit:

```python
                        await loop.run_in_executor(write_executor, index_con.commit)
```

In the outer `finally` block, right before `await loop.run_in_executor(write_executor, executor.shutdown)` (currently line 6351), add:

```python
                await loop.run_in_executor(write_executor, fact_index.close_writer, index_con)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k "TestIngestTransactFactIndex or TestRunIngestionBatchedIndexWrites"`
Expected: PASS

- [ ] **Step 6: Run the full ingestion test suite to check for regressions**

Run: `pytest tests/test_mcp_server.py -v -k "Ingest or ingest or Ingestion"`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: batch fact-index writes per ingestion commit, not per triple (#118)

Guards the 1M+-fact scale case: one SQLite commit per ingested commit
(matching the existing per-commit _db_checkpoint cadence), not one per
triple, which would be dominated by commit/fsync overhead at scale."
```

---

### Task 9: Migrate `_watermark_update`, `_last_run_write`, `_ingest_tags`

**Files:**
- Modify: `mcp_server.py` (functions `_watermark_update` lines 4044–4056, `_last_run_write` lines 4059–4069, `_ingest_tags` lines 5510–5538 as of before this plan's earlier edits — re-locate by name)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_transact`, `_retract` (Task 4)

These three are lower-stakes bookkeeping writes (no bi-temporal bounding, no batching complexity) — combined into one task since each migration is a small, mechanical, independent change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestBookkeepingWritesFactIndex:
    def test_watermark_update_indexes_new_hash(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._watermark_update(real_db, "abc123", "2026-01-01T00:00:00.000Z", "test")
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "abc123", top_n=10, boost=2.0)
        assert any(r[2] == "abc123" for r in results)

    def test_watermark_update_removes_old_hash_from_index(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._watermark_update(real_db, "abc123", "2026-01-01T00:00:00.000Z", "test")
        mcp_server._watermark_update(real_db, "def456", "2026-01-02T00:00:00.000Z", "test")
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "abc123", top_n=10, boost=2.0)
        assert not any(r[2] == "abc123" for r in results)

    def test_last_run_write_indexes(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._last_run_write(real_db, "abc123", "2026-01-01T00:00:00.000Z", 42)
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "abc123", top_n=10, boost=2.0)
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
        results = fact_index.query_facts(index_path, "v1.0.0", top_n=10, boost=2.0)
        assert results
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k TestBookkeepingWritesFactIndex`
Expected: FAIL — none of these functions write to the index yet.

- [ ] **Step 3: Migrate `_watermark_update`**

```python
def _watermark_update(db: Any, commit_hash: str, commit_ts_iso: str, reason: str) -> None:
    """Record the last successfully ingested commit hash in the graph."""
    existing = _watermark_query(db)
    if existing:
        _retract(db, f'[[:ingestion/watermark :hash "{existing}"]]')
    _transact(
        db,
        f'[[:ingestion/watermark :entity-type :type/ingestion] '
        f'[:ingestion/watermark :ident ":ingestion/watermark"] '
        f'[:ingestion/watermark :description "git ingestion watermark"] '
        f'[:ingestion/watermark :hash "{commit_hash}"]]',
        commit_ts_iso,
    )
```

- [ ] **Step 4: Migrate `_last_run_write`**

```python
def _last_run_write(db: Any, commit_hash: str, run_at: str, total_ingested: int) -> None:
    """Record the wall-clock time, final commit hash, and cumulative ingested count."""
    _transact(
        db,
        f'[[:ingestion/last-run-at :entity-type :type/ingestion] '
        f'[:ingestion/last-run-at :ident ":ingestion/last-run-at"] '
        f'[:ingestion/last-run-at :description "last ingestion run timestamp"] '
        f'[:ingestion/last-run-at :last-run-at "{run_at}"] '
        f'[:ingestion/last-run-at :last-commit "{commit_hash}"] '
        f'[:ingestion/last-run-at :total-ingested {total_ingested}]]',
        run_at,
    )
```

Note: the original had no explicit `:valid-from` clause at all (`(transact [[...]])`), relying on minigraf's default. `_transact` always sets `:valid-from` explicitly — pass `run_at` (the value already available and semantically the correct timestamp for this write) rather than inventing a new one.

- [ ] **Step 5: Migrate `_ingest_tags`**

Inside `_ingest_tags`'s per-tag loop, replace:
```python
            _db_execute(db, f'(transact {{:valid-from "{run_ts_iso}"}} [{" ".join(triples)}])')
```
with:
```python
            _transact(db, "[" + " ".join(triples) + "]", run_ts_iso)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k TestBookkeepingWritesFactIndex`
Expected: PASS

- [ ] **Step 7: Run the full ingestion suite to check for regressions**

Run: `pytest tests/test_mcp_server.py -v -k "Ingest or ingest or Watermark or watermark"`
Expected: PASS, no regressions.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: migrate watermark/last-run-at/tags bookkeeping writes to the index choke point (#118)"
```

---

### Task 10: Migrate `_transact_extracted_facts` and `_agent_extract_and_transact`

**Files:**
- Modify: `mcp_server.py` (functions `_transact_extracted_facts` lines 4634–4679, `_agent_extract_and_transact` lines 4896–4922 as of before this plan's earlier edits — re-locate by name)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_transact` (Task 4)

- [ ] **Step 1: Write the failing tests**

```python
class TestConversationalMemoryFactIndex:
    def test_transact_extracted_facts_indexes(self, real_db):
        import mcp_server
        import fact_index
        mcp_server._transact_extracted_facts([
            {"entity": ":decision/x", "entity_type": "decision", "attribute": ":description", "value": "use redis"},
        ])
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis", top_n=10, boost=2.0)
        assert any(r[0] == ":decision/x" for r in results)

    def test_agent_extract_and_transact_indexes(self, real_db, monkeypatch):
        import mcp_server
        import fact_index
        import asyncio as _asyncio

        async def fake_request(conversation_delta, canonical_section):
            return '[[:decision/x :description "use redis"] [:decision/x :entity-type :type/decision] [:decision/x :ident ":decision/x"]]'

        monkeypatch.setattr(mcp_server, "_request_agent_memory_block_async", fake_request)
        monkeypatch.setattr(mcp_server, "_query_canonical_entities", lambda: "")
        result = _asyncio.run(mcp_server._agent_extract_and_transact("we should use redis"))
        assert result["ok"] is True
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "redis", top_n=10, boost=2.0)
        assert any(r[0] == ":decision/x" for r in results)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k TestConversationalMemoryFactIndex`
Expected: FAIL — neither function writes to the index yet.

- [ ] **Step 3: Migrate `_transact_extracted_facts`**

Replace the per-fact write inside the loop:
```python
            _db_execute(db, f'(transact {{:valid-from "{now_z}"}} [{triples}])')
```
with:
```python
            _transact(db, "[" + triples + "]", now_z)
```

- [ ] **Step 4: Migrate `_agent_extract_and_transact`**

Replace:
```python
        _db_execute(db, f'(transact {{:valid-from "{valid_at}"}} {datalog})')
```
with:
```python
        _transact(db, datalog, valid_at)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k TestConversationalMemoryFactIndex`
Expected: PASS

- [ ] **Step 6: Run the broader memory-finalize-turn suite to check for regressions**

Run: `pytest tests/test_mcp_server.py -v -k "MemoryFinalizeTurn or AgentStrategy or LlmStrategy"`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: migrate conversational-memory extraction writes to the index choke point (#118)"
```

---

### Task 11: Rewrite `handle_memory_prepare_turn` and wire lazy backfill

**Files:**
- Modify: `mcp_server.py` (function `handle_memory_prepare_turn`, currently lines 4553–4577; re-locate by name — do not delete `FactIndex`/`IndexCache`/the heuristic fallback yet, that's Task 12)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `fact_index.query_facts`, `fact_index.rebuild_index`, `fact_index.index_path_for` (Tasks 1–3)
- Produces: the new `handle_memory_prepare_turn(user_message: str) -> str`, and a new `_rebuild_index_from_graph() -> None` helper for lazy backfill.

- [ ] **Step 1: Write the failing tests**

Add a new class in `tests/test_mcp_server.py` (this replaces `TestMemoryPrepareTurnBM25`, but keep both temporarily until Task 13 deletes the old one, to avoid a broken intermediate state — check for naming collisions and pick a distinct class name here):

```python
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

    def test_memory_facts_rank_above_non_memory_facts(self, real_db):
        """#141 regression, end to end through handle_memory_prepare_turn:
        the boost now actually fires, since the index's entity column is
        always the real keyword ident (callers supply it directly), never a
        re-derived Datalog ?e binding."""
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching layer"] '
            '[:decision/use-redis :entity-type :type/decision] '
            '[:decision/use-redis :ident ":decision/use-redis"]]',
            reason="test",
        )
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:function/unrelated :name "use redis for caching layer somewhere else"]'],
            "2026-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("use redis for caching layer")
        redis_pos = result.find(":decision/use-redis")
        other_pos = result.find(":function/unrelated")
        assert redis_pos != -1
        assert other_pos == -1 or redis_pos < other_pos

    def test_triggers_backfill_when_index_file_missing(self, real_db, tmp_path, monkeypatch):
        """The lazy-backfill path: if the index file doesn't exist (e.g. a
        pre-existing graph from before this feature shipped), the first
        query rebuilds it from a real graph rescan and still returns
        results, rather than returning empty."""
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)  # simulate a missing index despite an existing graph
        result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert "use redis for caching" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -v -k TestHandleMemoryPrepareTurnFts5`
Expected: FAIL — `handle_memory_prepare_turn` is still the old `IndexCache`-based implementation.

- [ ] **Step 3: Implement the backfill-rescan helper and rewrite `handle_memory_prepare_turn`**

Replace `handle_memory_prepare_turn` (leave `_handle_memory_prepare_turn_heuristic` in place for now — Task 12 deletes it) with:

```python
def _rebuild_index_from_graph() -> None:
    """One-time full rebuild: rescan the graph's current-valid snapshot and
    write it into a fresh fact_index table. This is the only place a full
    Datalog rescan happens post-launch (everywhere else is incremental via
    _transact/_retract) -- used for backfill (index file missing: fresh
    install, a pre-existing graph from before this feature shipped, or
    corruption recovery).

    Uses the same :ident-projection clause-ordering fix _preload_known_
    entities established (project through :ident explicitly, rather than
    binding ?e directly, which resolves to minigraf's internal UUID, not
    the keyword ident) -- this is the #141 root cause, and the one
    remaining place in this design a Datalog rescan still needs it.
    """
    db = get_db()
    raw = _db_execute(
        db,
        f'(query [:find ?ident ?a ?v :valid-at "{_now_utc_ms()}" '
        f':where [?e :ident ?ident] [?e ?a ?v]])',
    )
    facts = json.loads(raw).get("results", [])
    triples = [(str(e), str(a), str(v)) for e, a, v in facts]
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    fact_index.rebuild_index(path, triples)


def handle_memory_prepare_turn(user_message: str) -> str:
    """Query the persisted fact index for facts relevant to the user message.

    Returns a formatted context block string for injection as
    additionalContext, or an empty string if no relevant facts are found.
    If the index file doesn't exist yet (fresh install, pre-existing graph,
    or corruption recovery), triggers a one-time backfill rebuild and
    retries once.
    """
    scan_limit = int(os.environ.get("MINIGRAF_PREPARE_SCAN_LIMIT", "50"))
    boost = float(os.environ.get("MINIGRAF_MEMORY_BOOST", "2.0"))
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    try:
        results = fact_index.query_facts(path, user_message, top_n=scan_limit, boost=boost)
    except Exception:
        try:
            _rebuild_index_from_graph()
            results = fact_index.query_facts(path, user_message, top_n=scan_limit, boost=boost)
        except Exception as e:
            print(f"[fact_index] backfill failed: {e}", file=sys.stderr)
            return ""
    if not results:
        return ""
    return f"Relevant memory context:\n{_format_facts(results)}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -v -k TestHandleMemoryPrepareTurnFts5`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: rewrite handle_memory_prepare_turn to query the persisted fact index (#118, #141)"
```

---

### Task 12: Delete dead code — `FactIndex`, `IndexCache`, the heuristic fallback, `rank-bm25`

**Files:**
- Modify: `mcp_server.py` (delete `FactIndex`, `IndexCache`, `_index_cache`, `_handle_memory_prepare_turn_heuristic`, `_tokenize`, `_MEMORY_PREFIXES`, `_MEMORY_ENTITY_TYPES`, the `_BM25Okapi`/`_BM25_AVAILABLE` import block)
- Modify: `pyproject.toml` (remove `rank-bm25` from `dependencies`)
- Modify: `install.py` (check for and remove any leftover `bm25`-extra references)

**Interfaces:**
- No new interfaces — pure deletion. Every caller of the deleted symbols was already migrated in Tasks 5–11; this task is safe only because of that.

- [ ] **Step 1: Confirm nothing still references the symbols being deleted**

Run: `grep -n "_index_cache\|IndexCache\|FactIndex\|_handle_memory_prepare_turn_heuristic\|_BM25_AVAILABLE\|_BM25Okapi\|rank_bm25" mcp_server.py`
Expected: every remaining hit is inside the definitions about to be deleted in Step 2 (no external call sites left). If any hit is outside those definitions, stop and check which earlier task's migration was missed before proceeding.

- [ ] **Step 2: Delete the dead code from `mcp_server.py`**

Delete:
- The `try: from rank_bm25 import BM25Okapi as _BM25Okapi ... except ImportError: ...` block (currently lines 31–36).
- `_MEMORY_PREFIXES` and `_MEMORY_ENTITY_TYPES` (currently lines 4357–4363).
- `_tokenize` (currently lines 4366–4373).
- `FactIndex` (the whole class, currently lines 4376–4437).
- `IndexCache` and `_index_cache = IndexCache()` (currently lines 4440–4483).
- `_handle_memory_prepare_turn_heuristic` (currently lines 4486–4550).

- [ ] **Step 3: Remove `rank-bm25` from `pyproject.toml`**

Find:
```toml
dependencies = [
    "minigraf>=1.2.1",
    "mcp>=1.27.0",
    "rank-bm25",
]
```
Replace with:
```toml
dependencies = [
    "minigraf>=1.2.1",
    "mcp>=1.27.0",
]
```

- [ ] **Step 4: Check `install.py` for leftover `bm25`-extra references**

Run: `grep -n "bm25" install.py`
Expected: no hits (per the design spec, #117 already dropped the `[bm25]` extras string from `.mcp.json` generation — this step exists to confirm, and remove anything found).

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/test_mcp_server.py tests/test_fact_index.py -v`
Expected: many failures — `tests/test_mcp_server.py` still references the deleted symbols (`TestIndexCache`, `TestMemoryPrepareTurnBM25`, the `reset_mcp_server_db` fixture, `requires_bm25`/`_HAS_RANK_BM25`, and ~20 `monkeypatch.setattr(mcp_server._index_cache, ...)` sites). This is expected — Task 13 fixes the test file. Confirm the failures are all `AttributeError: module 'mcp_server' has no attribute ...` for the deleted symbols specifically, not something else.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py pyproject.toml install.py
git commit -m "refactor: delete FactIndex/IndexCache and the rank-bm25 heuristic fallback (#118, #117)

FTS5 has no missing-dependency failure mode the way rank-bm25 did -- it's
compiled into stdlib sqlite3 on every mainstream Python build this project
targets. tests/test_mcp_server.py still references these deleted symbols;
fixed in the next commit."
```

---

### Task 13: Clean up `tests/test_mcp_server.py`

**Files:**
- Modify: `tests/test_mcp_server.py`

**Interfaces:**
- None — pure test-file cleanup, restoring the suite to green after Task 12's deletions.

- [ ] **Step 1: Remove the `_HAS_RANK_BM25`/`requires_bm25` marker**

Delete (currently lines 30–39):
```python
try:
    import rank_bm25  # noqa: F401
    _HAS_RANK_BM25 = True
except ImportError:
    _HAS_RANK_BM25 = False

requires_bm25 = pytest.mark.skipif(
    not _HAS_RANK_BM25,
    reason="rank_bm25 not installed — it is a core dependency (pip install -e .)",
)
```

Run: `grep -n "@requires_bm25" tests/test_mcp_server.py` and remove the decorator from every test/class it's applied to (each of those tests keeps running unconditionally now — FTS5 has no availability gate).

- [ ] **Step 2: Update the `reset_mcp_server_db` fixture**

Replace:
```python
@pytest.fixture(autouse=True)
def reset_mcp_server_db(monkeypatch):
    """Reset the module-level _db singleton, grammar cache, and index cache between tests."""
    import mcp_server
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    mcp_server._index_cache = mcp_server.IndexCache()
    yield
    # Suppress index cache rebuilds during teardown to avoid race with background
    # rebuild threads that may still be running from the previous test (see #133).
    monkeypatch.setattr(mcp_server._index_cache, "invalidate", lambda: None)
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    mcp_server._index_cache = mcp_server.IndexCache()
```
with:
```python
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
```

- [ ] **Step 3: Remove every `monkeypatch.setattr(mcp_server._index_cache, "invalidate", lambda: None)` line**

Run: `grep -n 'monkeypatch.setattr(mcp_server._index_cache' tests/test_mcp_server.py`
For each hit, delete that line entirely (these existed only to suppress the old background-thread rebuild during test teardown — there's no equivalent background thread in the new design, so there's nothing to suppress).

- [ ] **Step 4: Replace `TestIndexCache` and `TestMemoryPrepareTurnBM25`**

Delete both classes entirely (`TestIndexCache`, currently lines 7054–7123, and `TestMemoryPrepareTurnBM25`, currently starting at line 7125) — their behavior is now covered by `tests/test_fact_index.py` (Tasks 1–3) and `TestHandleMemoryPrepareTurnFts5` (Task 11). Before deleting, read through both classes fully and confirm every behavior they exercised has an equivalent assertion somewhere in the new tests; if any gap is found (e.g. a specific edge case only `TestIndexCache` covered), add an equivalent test to `tests/test_fact_index.py` or `TestHandleMemoryPrepareTurnFts5` rather than silently dropping coverage.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/test_mcp_server.py tests/test_fact_index.py -v`
Expected: PASS, full suite green. Compare the total pass count against the pre-#118 baseline (601 passed, per the #117 session's final state, plus every new test this plan added) to confirm no silent count drop from an accidentally-deleted test.

- [ ] **Step 6: Run `ruff`/`black` to confirm no new lint regressions**

Run: `ruff check mcp_server.py fact_index.py tests/test_mcp_server.py tests/test_fact_index.py && black --check mcp_server.py fact_index.py tests/test_mcp_server.py tests/test_fact_index.py`
Expected: the same pre-existing findings this project's baseline already has (per the #117 session's notes: 11 findings/4 files needing reformat) — confirm via `git stash` + re-run if any new finding shows up, to distinguish pre-existing from newly introduced.

- [ ] **Step 7: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test: clean up test_mcp_server.py after the FactIndex/IndexCache deletion (#118)"
```

---

### Task 14: Cross-process sharing regression test, docs sync, final verification

**Files:**
- Test: `tests/test_fact_index.py` (the definitive #118 regression test)
- Modify: `docs/testing-conventions.md`
- Modify: `SKILL.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- None new — this is the closing verification task.

- [ ] **Step 1: Write the cross-process sharing regression test**

This is the test that would have caught #118's actual bug: a second process, with no shared Python state, must see facts committed by a different process — because both open the same file, not because of any RPC. Add to `tests/test_fact_index.py`:

```python
def test_cross_process_reader_sees_writer_commits(tmp_path):
    """The definitive #118 regression test: a fresh subprocess (no shared
    Python state, no RPC) opening the index file read-only after the main
    process writes to it must see the committed rows via the OS page
    cache -- this is the exact scenario the UserPromptSubmit hook is in on
    every turn."""
    import subprocess
    import sys as _sys

    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [(":decision/use-redis", ":description", "use redis for caching")])
    fact_index.close_writer(con)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        f"import sys; sys.path.insert(0, {repo_root!r})\n"
        "import fact_index\n"
        f"results = fact_index.query_facts({path!r}, 'redis caching', top_n=10, boost=2.0)\n"
        "assert results, 'subprocess found no results — cross-process sharing broken'\n"
        "assert results[0][0] == ':decision/use-redis'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [_sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `pytest tests/test_fact_index.py -v -k test_cross_process_reader_sees_writer_commits`
Expected: PASS. (If this fails, something about the WAL/mmap configuration is wrong — this is the test that matters most for #118's actual bug, so do not proceed past a failure here without root-causing it.)

- [ ] **Step 3: Update `docs/testing-conventions.md`**

Add a new section after "Observing real calls without faking them: `execute_spy()`":

```markdown
## Real sqlite3 for the fact index

`tests/test_fact_index.py` and any `mcp_server.py` test touching the
persisted fact index follow the same "real backend, always" rule extended
to `fact_index.py`'s SQLite FTS5 file: tests open a real `sqlite3.Connection`
(a `tmp_path`-backed file, or `:memory:` where cross-process behavior isn't
under test) — never a mocked `sqlite3.Connection`. The one test that
specifically needs a second real OS process (not just a second connection)
is `test_cross_process_reader_sees_writer_commits`, which spawns a real
subprocess via `subprocess.run` to prove the index is actually shared via
the filesystem/OS page cache, not via any in-process Python state — mirrors
the existing DB lock-retry cluster's "spawn a real subprocess to
manufacture a real condition" pattern.
```

- [ ] **Step 4: Update `SKILL.md`**

Find the section documenting environment variables (search for `MINIGRAF_GRAPH_PATH`) and add `MINIGRAF_INDEX_PATH` alongside it, documenting: overrides the default `<graph_path>.fts.sqlite3` sidecar path for the persisted fact index used by memory retrieval.

- [ ] **Step 5: Update `CLAUDE.md`**

In the "Graph Storage" section (which currently documents `MINIGRAF_GRAPH_PATH`), add a line noting the fact index's default location and its own override variable:

```markdown
Memory retrieval index: `<graph_path>.fts.sqlite3` alongside the graph file.

Override: `MINIGRAF_INDEX_PATH=/custom/path`
```

- [ ] **Step 6: Run the complete test suite one final time**

Run: `pytest tests/ -v`
Expected: full suite passes. Note the final pass count for the commit message.

- [ ] **Step 7: Commit**

```bash
git add tests/test_fact_index.py docs/testing-conventions.md SKILL.md CLAUDE.md
git commit -m "test: add cross-process sharing regression test; sync docs for #118

This is the test that directly targets #118's original bug: a fresh
subprocess with no shared Python state must see facts committed by another
process, via the OS page cache backing the shared sqlite3 file."
```

---

## Post-plan verification checklist (for the final whole-branch review)

- [ ] Every one of the 12 write call sites identified in the design doc actually routes through `_transact`/`_retract` — re-run the Task 12 Step 1 grep one more time against the final state of `mcp_server.py`.
- [ ] `hooks/prepare_hook.py` needs no code change (it just calls `mcp_server.get_db()` and `mcp_server.handle_memory_prepare_turn()`, both of which keep their existing signatures) — confirm by reading it once more, not by assumption.
- [ ] `handle_minigraf_ingest_git`'s `inputSchema` description and any other tool-schema text mentioning the old BM25/heuristic behavior is updated if it exists (grep the MCP tool registration block for stale references).
- [ ] Full test suite pass count is compared against the pre-#118 baseline (601, per the #117 session) plus every new test this plan added, to catch a silently-dropped test.
