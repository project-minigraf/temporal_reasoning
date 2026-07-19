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


def test_open_writer_creates_index_meta_table(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='index_meta'"
        ).fetchall()
        assert rows
    finally:
        fact_index.close_writer(con)


def test_open_writer_stamps_schema_version_but_not_backfilled(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        version = con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version == ("2",)
        backfilled = con.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone()
        assert backfilled is None
    finally:
        fact_index.close_writer(con)


def test_needs_backfill_true_for_missing_file(tmp_path):
    path = str(tmp_path / "nonexistent.fts.sqlite3")
    assert fact_index.needs_backfill(path) is True


def test_needs_backfill_true_for_schema_only_file(tmp_path):
    """A file created by open_writer (schema exists) but never backfilled --
    exactly the write-races-ahead-of-read scenario this whole plan fixes."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.close_writer(con)
    assert fact_index.needs_backfill(path) is True


def test_needs_backfill_false_after_rebuild_index(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/x", ":description", "hello", None, None)])
    assert fact_index.needs_backfill(path) is False


def test_delete_facts_only_deletes_current_rows(tmp_path):
    """A retract must not touch a historical row for the same (e, a, v) from
    an earlier lifecycle -- this is the mechanism _ingest_close relies on."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        # A historical row (valid_to set) for this exact triple...
        fact_index.insert_facts(con, [
            (":module/foo", ":description", "the foo module", "2024-01-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z"),
        ])
        # ...and a CURRENT row for the same triple (as if re-opened later).
        fact_index.insert_facts(con, [
            (":module/foo", ":description", "the foo module", "2024-06-01T00:00:00.000Z", None),
        ])
        con.commit()
        fact_index.delete_facts(con, [(":module/foo", ":description", "the foo module", "2024-06-01T00:00:00.000Z", None)])
        con.commit()
        rows = con.execute(
            "SELECT valid_from, valid_to FROM facts_fts WHERE entity = ':module/foo'"
        ).fetchall()
        assert rows == [("2024-01-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z")]
    finally:
        fact_index.close_writer(con)


def test_rebuild_index_stamps_backfilled_sentinel(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/x", ":description", "hello", None, None)])
    con = fact_index.open_reader(path)
    try:
        row = con.execute("SELECT value FROM index_meta WHERE key = 'backfilled'").fetchone()
        assert row == ("1",)
    finally:
        con.close()


def test_rebuild_index_recovers_a_corrupted_file(tmp_path):
    """A corrupted (non-SQLite) index file must self-heal: rebuild_index
    should not raise, and the resulting file must be a valid, fully
    backfilled index. sqlite3.DatabaseError ("file is not a database") is
    a distinct failure mode from the lock/busy OperationalError the retry
    loop already handles -- both are subclasses of sqlite3.DatabaseError,
    but only OperationalError is a transient contention condition; a
    corrupted file needs the file removed and the sequence restarted from
    scratch, not just retried in place."""
    path = str(tmp_path / "t.fts.sqlite3")
    with open(path, "wb") as f:
        f.write(b"not a real sqlite file at all, just garbage bytes")

    fact_index.rebuild_index(path, [(":decision/x", ":description", "hello", None, None)])

    assert fact_index.needs_backfill(path) is False
    con = fact_index.open_reader(path)
    try:
        rows = con.execute(
            "SELECT entity, attribute, value FROM facts_fts WHERE entity = ':decision/x'"
        ).fetchall()
        assert rows == [(":decision/x", ":description", "hello")]
    finally:
        con.close()


def test_needs_backfill_true_for_v1_index_file_no_meta_table(tmp_path):
    """Hand-build a v1-shaped file (facts_fts only, no index_meta at all) --
    simulates an index file created before this schema-v2 migration shipped."""
    import sqlite3 as _sqlite3
    path = str(tmp_path / "t.fts.sqlite3")
    con = _sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, tokenize='unicode61')"
    )
    con.commit()
    con.close()
    assert fact_index.needs_backfill(path) is True


def test_needs_backfill_true_for_corrupted_file(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    path_obj = tmp_path / "t.fts.sqlite3"
    path_obj.write_bytes(b"not a real sqlite file at all")
    assert fact_index.needs_backfill(path) is True


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
        fact_index.insert_facts(con, [(":decision/use-redis", ":description", "use redis for caching", None, None)])
        con.commit()
        rows = con.execute("SELECT entity, attribute, value, valid_from, valid_to FROM facts_fts").fetchall()
        assert rows == [(":decision/use-redis", ":description", "use redis for caching", None, None)]
    finally:
        fact_index.close_writer(con)


def test_delete_removes_matching_row(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/use-redis", ":description", "use redis for caching", None, None)
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
            (":decision/a", ":description", "keep me", None, None),
            (":decision/b", ":description", "delete me", None, None),
        ])
        con.commit()
        fact_index.delete_facts(con, [(":decision/b", ":description", "delete me", None, None)])
        con.commit()
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
        assert rows == [(":decision/a",)]
    finally:
        fact_index.close_writer(con)


def test_open_reader_sees_writer_commits(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    writer = fact_index.open_writer(path)
    fact_index.insert_facts(writer, [(":decision/x", ":description", "hello", None, None)])
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
        (":decision/use-redis", ":description", "use redis for caching layer", None, None),
        (":function/unrelated", ":name", "some other thing entirely", None, None),
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=10, boost=2.0, historical_discount=1.0)
    assert results
    assert results[0][0] == ":decision/use-redis"


def test_query_facts_excludes_non_matching_rows(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [(":function/unrelated", ":name", "some other thing entirely", None, None)])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=10, boost=2.0, historical_discount=1.0)
    assert results == []


def test_query_facts_on_empty_index_returns_empty(tmp_path):
    """Coverage-gap fill (Task 13, ported from the deleted mcp_server.py
    TestFactIndex.test_empty_facts_returns_empty_query): querying an index
    file that exists but has zero rows must return [] gracefully, not raise
    -- distinct from test_query_facts_missing_index_raises, where the file
    doesn't exist at all."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.close_writer(con)
    assert fact_index.query_facts(path, "redis", top_n=10, boost=2.0, historical_discount=1.0) == []


def test_query_facts_respects_top_n(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (f":decision/x{i}", ":description", "redis caching option", None, None) for i in range(5)
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=2, boost=2.0, historical_discount=1.0)
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
        (":function/caching_helper", ":name", "redis caching helper function", None, None),
        (":decision/redis", ":description", "redis caching helper function", None, None),
    ])
    fact_index.close_writer(con)
    try:
        results = fact_index.query_facts(path, "redis caching helper function", top_n=10, boost=2.0, historical_discount=1.0)
        assert results[0][0] == ":decision/redis"
    finally:
        _os.remove(path)


def test_query_facts_missing_index_raises():
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        fact_index.query_facts("/nonexistent/does-not-exist.sqlite3", "anything", top_n=10, boost=2.0, historical_discount=1.0)


def test_query_facts_boost_surfaces_fact_outside_old_limit_window(tmp_path):
    """Review-finding regression test: a previous implementation applied
    `LIMIT top_n * 4` to the raw (pre-boost) SQL query, then boosted and
    re-sorted only that pre-fetched window in Python. A :decision/-prefixed
    fact whose *unboosted* bm25 rank fell outside that window was silently
    dropped before the boost ever got a chance to promote it -- not merely
    ranked lower, but entirely absent from the results.

    Here 15 non-memory facts all score strongly (short text, high term
    frequency for both query tokens) and rank ahead of one :decision/-prefixed
    fact whose match is diluted by 40 filler tokens (weak raw bm25 score).
    With top_n=3, the old `LIMIT top_n * 4` (12) would fetch only the 12
    strongest noise facts and never even see the decision fact. A large
    boost (5.0) applied to the buried fact's true (weak) raw score is more
    than enough to beat every noise fact's raw score once it IS considered
    -- proving the fix fetches the full matching set before boosting."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    triples = [
        (f":function/noise{i}", ":name", "redis caching redis caching", None, None) for i in range(15)
    ]
    decision_text = "redis caching " + " ".join(f"filler{i}" for i in range(40))
    triples.append((":decision/buried", ":description", decision_text, None, None))
    fact_index.insert_facts(con, triples)
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=3, boost=5.0, historical_discount=1.0)
    entities = [row[0] for row in results]
    assert ":decision/buried" in entities


def test_query_facts_historical_discount_demotes(tmp_path):
    """A historical fact ranks below an equally-matching current fact."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":module/old-cache", ":description", "redis caching layer", "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z"),
        (":module/new-cache", ":description", "redis caching layer", "2025-01-01T00:00:00.000Z", None),
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching layer", top_n=10, boost=2.0, historical_discount=0.5)
    assert results[0][0] == ":module/new-cache"
    assert results[1][0] == ":module/old-cache"


def test_query_facts_historical_discount_of_one_means_no_discount(tmp_path):
    """historical_discount=1.0 (the default/neutral value existing tests
    use) leaves historical and current facts scored purely on relevance --
    proves the discount parameter, not some other factor, is what causes
    the demotion in the sibling test above."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":module/old-cache", ":description", "redis caching layer identical text here", "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z"),
        (":module/new-cache", ":description", "redis caching layer identical text here", "2025-01-01T00:00:00.000Z", None),
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching layer identical text here", top_n=10, boost=2.0, historical_discount=1.0)
    # Identical text -> identical raw bm25 score -> order between the two is
    # not asserted (implementation-defined tie order), only that BOTH appear.
    assert {r[0] for r in results} == {":module/old-cache", ":module/new-cache"}


def test_query_facts_returns_window_columns(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":module/foo", ":description", "the foo module", "2024-01-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z"),
    ])
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "foo module", top_n=10, boost=2.0, historical_discount=0.5)
    assert results == [[":module/foo", ":description", "the foo module", "2024-01-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z"]]


def test_query_facts_limit_is_bounded_but_boost_still_applies_inside_it(tmp_path):
    """Verifies boost correctly promotes a buried memory-prefixed fact into a
    bounded top_n result under the new SQL-side ranking (boost is applied
    inside the ORDER BY, before the LIMIT, so it can rescue a fact that would
    otherwise fall outside a small top_n). Note: this scenario also passed
    under the prior (Task 2-era) implementation, which already fetched all
    matching rows before boosting/truncating in Python — the earlier bug this
    whole design guards against (an early SQL LIMIT applied BEFORE boosting)
    was already fixed before this task; this test documents the still-correct
    current behavior, not a new discriminating regression guard for this
    specific rewrite."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    # 20 non-memory facts that outrank the one memory fact on raw bm25 alone
    # (repeated query terms boost raw term frequency), plus one buried
    # memory fact with the query terms only once.
    filler = [
        (f":function/noise{i}", ":name", "redis caching redis caching redis caching", None, None)
        for i in range(20)
    ]
    con2_facts = filler + [
        (":decision/buried", ":description", "redis caching", None, None),
    ]
    fact_index.insert_facts(con, con2_facts)
    fact_index.close_writer(con)
    results = fact_index.query_facts(path, "redis caching", top_n=3, boost=2.0, historical_discount=1.0)
    assert any(r[0] == ":decision/buried" for r in results)


def test_rebuild_index_creates_fresh_table(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/x", ":description", "hello world", None, None)])
    results = fact_index.query_facts(path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
    assert len(results) == 1
    assert results[0][0] == ":decision/x"


def test_rebuild_index_replaces_existing_data(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/old", ":description", "old fact", None, None)])
    fact_index.rebuild_index(path, [(":decision/new", ":description", "new fact", None, None)])
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
        "fact_index.rebuild_index(%r, [(':decision/x', ':description', 'concurrent', None, None)])\n"
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


def test_concurrent_rebuild_race_against_a_corrupted_file_is_safe(tmp_path):
    """Several processes racing to recover the SAME corrupted index file must
    not raise. Unlike lock/busy contention (a timing-dependent condition, so
    racers rarely observe it at the exact same instant), corruption is a
    static property of the file every racer detects at once -- so a naive
    check-then-remove (os.path.exists then os.remove) is a real TOCTOU race:
    one racer's os.remove can lose to another racer's os.remove already
    having deleted the file, raising FileNotFoundError. This must be
    swallowed, not propagated. Uses several concurrent processes (not just
    two, like the missing-file race test above) because this race is
    probabilistic and needs enough concurrent contenders to reliably surface
    it if the swallow-FileNotFoundError guard regresses."""
    import subprocess
    import sys as _sys
    path = str(tmp_path / "t.fts.sqlite3")
    with open(path, "wb") as f:
        f.write(b"not a real sqlite file at all, just garbage bytes")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import fact_index\n"
        "fact_index.rebuild_index(%r, [(':decision/x', ':description', 'concurrent', None, None)])\n"
    ) % (str(tmp_path.parent.parent), path)
    procs = [
        subprocess.Popen([_sys.executable, "-c", script], stderr=subprocess.PIPE)
        for _ in range(8)
    ]
    for p in procs:
        _, stderr = p.communicate(timeout=10)
        assert p.returncode == 0, stderr.decode()
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == [(":decision/x",)]


# ---------------------------------------------------------------------------
# _tokenize / _MEMORY_PREFIXES -- ported (Task 13 coverage-gap fill) from the
# deleted mcp_server.py TestBM25Tokenize, whose subject (mcp_server._tokenize
# / mcp_server._MEMORY_PREFIXES) no longer exists: fact text is now indexed
# by SQLite FTS5's own tokenizer, not a custom Python one. fact_index.py
# still has a same-named/same-shaped private _tokenize (query-side only, for
# building the MATCH expression) and the same _MEMORY_PREFIXES tuple, so
# these pin down equivalent behavior at its new home.
# ---------------------------------------------------------------------------


def test_tokenize_splits_keyword_ident_on_punctuation():
    assert fact_index._tokenize(":decision/use-redis") == ["decision", "use", "redis"]


def test_tokenize_lowercases_tokens():
    assert fact_index._tokenize("use Redis for Caching") == ["use", "redis", "for", "caching"]


def test_tokenize_filters_empty_tokens():
    assert fact_index._tokenize(":::") == []


def test_tokenize_mixed_fact_row():
    assert fact_index._tokenize(":commit/abc123 :subject feat add redis") == [
        "commit", "abc123", "subject", "feat", "add", "redis"
    ]


def test_memory_prefixes_include_all_memory_entity_types():
    assert ":decision/use-redis".startswith(fact_index._MEMORY_PREFIXES)
    assert ":preference/tdd".startswith(fact_index._MEMORY_PREFIXES)
    assert ":constraint/no-js".startswith(fact_index._MEMORY_PREFIXES)
    assert ":dependency/redis".startswith(fact_index._MEMORY_PREFIXES)


def test_memory_prefixes_exclude_git_entity_types():
    assert not ":commit/abc123".startswith(fact_index._MEMORY_PREFIXES)
    assert not ":function/foo-bar".startswith(fact_index._MEMORY_PREFIXES)
    assert not ":module/src-main".startswith(fact_index._MEMORY_PREFIXES)


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
    fact_index.insert_facts(con, [(":decision/use-redis", ":description", "use redis for caching", None, None)])
    fact_index.close_writer(con)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        f"import sys; sys.path.insert(0, {repo_root!r})\n"
        "import fact_index\n"
        f"results = fact_index.query_facts({path!r}, 'redis caching', top_n=10, boost=2.0, historical_discount=1.0)\n"
        "assert results, 'subprocess found no results — cross-process sharing broken'\n"
        "assert results[0][0] == ':decision/use-redis'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [_sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout
