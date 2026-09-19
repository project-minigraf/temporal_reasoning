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
        assert version == ("4",)
        backfilled = con.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone()
        assert backfilled is None
    finally:
        fact_index.close_writer(con)


def _build_v3_index_file(path):
    """Hand-build a schema-v3 file: the exact pre-#236 shape, including a
    facts_fts row whose rowid was auto-assigned and therefore bears no
    relation to its facts_dedup row's rowid. This is the file that would be
    silently mis-deleted if v4 delete code ever ran against it."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "valid_from UNINDEXED, valid_to UNINDEXED, tokenize='unicode61')"
    )
    con.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE facts_dedup ("
        "entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL, "
        "valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
        "UNIQUE(entity, attribute, value, valid_from, valid_to))"
    )
    con.execute(
        "INSERT INTO facts_fts (entity, attribute, value, valid_from, valid_to) "
        "VALUES (':decision/old', ':description', 'stale', '2026-01-01T00:00:00.000Z', NULL)"
    )
    con.execute(
        "INSERT INTO facts_dedup VALUES (':decision/old', ':description', 'stale', "
        "'2026-01-01T00:00:00.000Z', '')"
    )
    con.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '3')")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('backfilled', '1')")
    con.commit()
    con.close()


def test_open_writer_wipes_a_stale_schema_version_file(tmp_path):
    """#236: a v3 file's facts_fts rowids are auto-assigned and unrelated to
    its facts_dedup rowids, so v4's rowid-based delete would remove wrong
    rows. The version bump alone doesn't prevent that -- only the read path
    acts on needs_backfill(), while open_writer would happily write to the
    stale file. So ensure_schema must wipe it at writer-open time."""
    path = str(tmp_path / "t.fts.sqlite3")
    _build_v3_index_file(path)
    assert fact_index.needs_backfill(path) is True

    con = fact_index.open_writer(path)
    try:
        assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM facts_dedup").fetchone() == (0,)
        assert con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        # The 'backfilled' sentinel went with index_meta, so a rebuild still
        # follows -- the wipe must not look like a completed backfill.
        assert con.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone() is None
    finally:
        fact_index.close_writer(con)
    assert fact_index.needs_backfill(path) is True


def test_open_writer_does_not_wipe_a_current_version_file(tmp_path):
    """The wipe fires ONLY on a version mismatch. Every ingestion run after
    the first reopens a current-version file and must keep its rows -- this
    is the regression that would quietly destroy the index on every open."""
    path = str(tmp_path / "t.fts.sqlite3")
    con1 = fact_index.open_writer(path)
    fact_index.insert_facts(con1, [(":decision/x", ":description", "hello", None, None)])
    fact_index.close_writer(con1)

    con2 = fact_index.open_writer(path)
    try:
        assert con2.execute("SELECT entity FROM facts_fts").fetchall() == [(":decision/x",)]
    finally:
        fact_index.close_writer(con2)


def test_open_writer_wipes_a_v1_file_with_no_meta_table(tmp_path):
    """A v1 file has a 3-column facts_fts and no index_meta at all. Reading
    schema_version must not raise, and the drop must actually replace the
    old table -- CREATE ... IF NOT EXISTS alone would leave the 3-column
    shape in place, and every later insert would fail on column count."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "tokenize='unicode61')"
    )
    con.execute("INSERT INTO facts_fts VALUES (':decision/old', ':description', 'stale')")
    con.commit()
    con.close()

    writer = fact_index.open_writer(path)
    try:
        cols = [r[1] for r in writer.execute("PRAGMA table_info(facts_fts)").fetchall()]
        assert cols == ["entity", "attribute", "value", "valid_from", "valid_to"]
        assert writer.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
        assert writer.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        # And the wiped file is immediately usable, not just well-shaped.
        fact_index.insert_facts(
            writer, [(":decision/new", ":description", "fresh", None, None)])
        writer.commit()
        assert writer.execute("SELECT entity FROM facts_fts").fetchall() == [(":decision/new",)]
    finally:
        fact_index.close_writer(writer)


def test_open_writer_wipes_a_v2_file_and_stamps_current_version(tmp_path):
    """A v2 file (backfilled, no facts_dedup). Before #236, ensure_schema
    created facts_dedup empty here but left schema_version at '2' and left
    'backfilled'='1' standing -- the gap ensure_schema's own docstring
    documented. The wipe closes it."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "valid_from UNINDEXED, valid_to UNINDEXED, tokenize='unicode61')"
    )
    con.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '2')")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('backfilled', '1')")
    con.commit()
    con.close()

    writer = fact_index.open_writer(path)
    try:
        assert writer.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        assert writer.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone() is None
    finally:
        fact_index.close_writer(writer)
    assert fact_index.needs_backfill(path) is True


def _open_writer_style_connection(path):
    """A connection configured exactly the way open_writer configures its
    own -- same PRAGMAs, same DEFAULT isolation_level -- but WITHOUT calling
    ensure_schema, so a test can drive the schema functions by hand."""
    con = sqlite3.connect(path, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA mmap_size=%d" % fact_index._MMAP_SIZE)
    con.execute("PRAGMA busy_timeout=%d" % fact_index._BUSY_TIMEOUT_MS)
    return con


def test_migrate_schema_does_not_wipe_a_migration_a_racer_already_completed(tmp_path):
    """#236 review: ensure_schema's version check is unlocked, and pysqlite
    runs DROP/CREATE in autocommit, so two callers can both read the stale
    version before either drops. The loser must not then wipe the winner's
    completed work -- the concrete case is an ingestion thread that read '3'
    being descheduled while _run_startup_backfill's rebuild_index finishes a
    full rescan, then waking up and dropping it.

    The interleave is simulated deterministically: con_b's stale read is
    taken for real (it returns '3'), con_a then completes the migration and
    a rebuild-like population, and only then does con_b proceed into the
    branch its stale read selected -- which is exactly _migrate_schema, the
    body of ensure_schema's mismatch branch. This exercises the re-read-
    under-lock path, not the happy path: at the top of ensure_schema con_b
    would now see '4' and never get here at all."""
    path = str(tmp_path / "t.fts.sqlite3")
    _build_v3_index_file(path)

    con_a = _open_writer_style_connection(path)
    con_b = _open_writer_style_connection(path)
    try:
        # con_b's version check happens FIRST and genuinely reads the stale
        # version -- this is the decision that sends it into the drop branch.
        assert fact_index._stored_schema_version(con_b) == "3"

        # con_a wins the race: it migrates, and then a rebuild completes and
        # stamps 'backfilled' plus real rows -- the multi-minute work that
        # must survive.
        fact_index.ensure_schema(con_a)
        fact_index.insert_facts(
            con_a, [(":decision/rebuilt", ":description", "expensive", None, None)])
        con_a.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('backfilled', '1')")
        con_a.commit()

        # con_b now acts on its stale read. The re-read under the write lock
        # is the only thing standing between it and three DROP TABLEs.
        fact_index._migrate_schema(con_b)

        assert con_b.execute("SELECT entity FROM facts_fts").fetchall() == [
            (":decision/rebuilt",)]
        assert con_b.execute("SELECT count(*) FROM facts_dedup").fetchone() == (1,)
        assert con_b.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone() == ("1",)
        assert con_b.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        # The completed backfill is still recognized as complete.
        assert fact_index.needs_backfill(path) is False

        # And the branch is still live: against a file that really IS stale,
        # the same call still wipes. (Without this the test above would pass
        # for a _migrate_schema that had simply stopped dropping anything.)
        con_b.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('schema_version', '3')")
        con_b.commit()
        fact_index._migrate_schema(con_b)
        assert con_b.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
        assert con_b.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone() is None
    finally:
        con_a.close()
        con_b.close()


def test_migrate_schema_restores_the_connection_isolation_level(tmp_path):
    """_migrate_schema forces isolation_level=None to keep its explicit
    BEGIN IMMEDIATE clear of pysqlite's implicit transaction management. It
    must hand the connection back unchanged: batched ingestion writes
    (insert_facts/delete_facts never commit) depend on pysqlite opening an
    implicit transaction that close_writer's commit() ends, and an autocommit
    connection would silently commit each write on its own."""
    path = str(tmp_path / "t.fts.sqlite3")
    _build_v3_index_file(path)
    con = _open_writer_style_connection(path)
    try:
        default_isolation = con.isolation_level
        fact_index.ensure_schema(con)  # takes the migration branch
        assert con.isolation_level == default_isolation
        # Behavioural proof, not just the attribute: an uncommitted write is
        # still rollback-able, i.e. a real transaction is open.
        fact_index.insert_facts(
            con, [(":decision/x", ":description", "uncommitted", None, None)])
        assert con.in_transaction is True
        con.rollback()
        assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
    finally:
        con.close()


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


# ---------------------------------------------------------------------------
# #152 -- insert_facts must be idempotent on an exact (entity, attribute,
# value, valid_from, valid_to) match, not a plain unconditional INSERT.
# ---------------------------------------------------------------------------


def test_insert_facts_is_idempotent_for_current_fact(tmp_path):
    """Re-transacting the same already-current fact (e.g. _watermark_update's
    :entity-type/:ident/:description triples, re-asserted on every ingested
    commit) must not append a second facts_fts row for it."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None)
        fact_index.insert_facts(con, [triple])
        con.commit()
        fact_index.insert_facts(con, [triple])
        con.commit()
        rows = con.execute("SELECT * FROM facts_fts WHERE entity = ':decision/x'").fetchall()
        assert len(rows) == 1
    finally:
        fact_index.close_writer(con)


def test_insert_facts_is_idempotent_for_historical_fact(tmp_path):
    """The same dedup guard applies to a historical (valid_to set) row."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (
            ":module/foo", ":description", "the foo module",
            "2024-01-01T00:00:00.000Z", "2024-06-01T00:00:00.000Z",
        )
        fact_index.insert_facts(con, [triple])
        con.commit()
        fact_index.insert_facts(con, [triple])
        con.commit()
        rows = con.execute("SELECT * FROM facts_fts WHERE entity = ':module/foo'").fetchall()
        assert len(rows) == 1
    finally:
        fact_index.close_writer(con)


def test_insert_facts_is_idempotent_with_null_valid_from(tmp_path):
    """valid_from=None is a real input shape used elsewhere in this suite
    (and by rebuild_index callers) -- the dedup key must handle it, not just
    the more common non-null valid_from case."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", None, None)
        fact_index.insert_facts(con, [triple])
        con.commit()
        fact_index.insert_facts(con, [triple])
        con.commit()
        rows = con.execute("SELECT * FROM facts_fts WHERE entity = ':decision/x'").fetchall()
        assert len(rows) == 1
    finally:
        fact_index.close_writer(con)


def test_insert_facts_keeps_distinct_valid_from_as_separate_rows(tmp_path):
    """Dedup is scoped to the exact 5-tuple only -- the same (entity,
    attribute, value) re-asserted with a genuinely different valid_from is a
    distinct fact (mirrors minigraf's own graph semantics, confirmed
    directly: re-transacting with a different valid_from is NOT idempotent
    at the graph level either) and must not be collapsed."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(con, [(":tag/v1", ":name", "v1", "2026-01-01T00:00:00.000Z", None)])
        con.commit()
        fact_index.insert_facts(con, [(":tag/v1", ":name", "v1", "2026-02-01T00:00:00.000Z", None)])
        con.commit()
        rows = con.execute("SELECT valid_from FROM facts_fts WHERE entity = ':tag/v1'").fetchall()
        assert len(rows) == 2
    finally:
        fact_index.close_writer(con)


def test_insert_facts_dedup_persists_across_writer_reopen(tmp_path):
    """The dedup guard must survive a close/reopen of the writer connection
    -- the real-world case is separate ingestion runs, each opening a fresh
    connection to the same on-disk index file."""
    path = str(tmp_path / "t.fts.sqlite3")
    triple = (":ingestion/watermark", ":ident", ":ingestion/watermark", "2026-01-01T00:00:00.000Z", None)
    con1 = fact_index.open_writer(path)
    fact_index.insert_facts(con1, [triple])
    fact_index.close_writer(con1)

    con2 = fact_index.open_writer(path)
    fact_index.insert_facts(con2, [triple])
    fact_index.close_writer(con2)

    con3 = fact_index.open_reader(path)
    try:
        rows = con3.execute(
            "SELECT * FROM facts_fts WHERE entity = ':ingestion/watermark'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        con3.close()


def test_insert_facts_after_delete_is_not_silently_swallowed(tmp_path):
    """Code-review finding on #152: delete_facts must also clear the
    matching facts_dedup row, not just the facts_fts row -- otherwise a
    retract followed by a re-assert that happens to reuse the exact same
    valid_from (a general-purpose possibility via handle_minigraf_transact/
    handle_minigraf_retract, even if today's specific retract-then-reassert
    callers in mcp_server.py don't hit it) would see insert_facts treat the
    row as "already indexed" from the now-deleted assertion and silently
    skip re-inserting it -- the fact stays live in the graph but vanishes
    from the index forever, until the next full rebuild_index()."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None)
        fact_index.insert_facts(con, [triple])
        con.commit()
        fact_index.delete_facts(con, [triple])
        con.commit()
        fact_index.insert_facts(con, [triple])
        con.commit()
        rows = con.execute("SELECT * FROM facts_fts WHERE entity = ':decision/x'").fetchall()
        assert len(rows) == 1
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


def test_needs_backfill_true_for_pre_dedup_schema_v2_file(tmp_path):
    """A schema-v2 index file (backfilled, but predating facts_dedup / #152)
    must be flagged for rebuild -- schema_version mismatch is what forces
    the rebuild that creates facts_dedup and repopulates it, rather than an
    old writer connection silently reusing a v2 file that lacks the table
    insert_facts' dedup guard now depends on."""
    import sqlite3 as _sqlite3
    path = str(tmp_path / "t.fts.sqlite3")
    con = _sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "valid_from UNINDEXED, valid_to UNINDEXED, tokenize='unicode61')"
    )
    con.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '2')")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('backfilled', '1')")
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


def test_insert_facts_assigns_dedup_rowid_as_fts_rowid(tmp_path):
    """#236: the whole fix rests on this identity. delete_facts seeks
    facts_dedup's B-tree for a rowid and then deletes facts_fts by that
    rowid -- if the two ever diverge, a retract silently removes an
    unrelated fact from the index."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(con, [
            (":decision/a", ":description", "first", None, None),
            (":decision/b", ":description", "second", None, None),
            (":decision/c", ":description", "third", None, None),
        ])
        con.commit()
        fts = dict(con.execute("SELECT entity, rowid FROM facts_fts").fetchall())
        dedup = dict(con.execute("SELECT entity, rowid FROM facts_dedup").fetchall())
        assert len(fts) == 3
        assert fts == dedup
    finally:
        fact_index.close_writer(con)


def test_rowid_identity_survives_delete_and_reinsert(tmp_path):
    """SQLite recycles a b-tree rowid once it's freed. A delete/reinsert
    cycle therefore reuses the rowid its own fts row just vacated -- which
    is only safe because the two are deleted in lockstep. If a stale fts row
    survived at that rowid, the reinsert would raise IntegrityError."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None)
        for _ in range(3):
            fact_index.insert_facts(con, [triple])
            con.commit()
            fts = con.execute("SELECT rowid FROM facts_fts").fetchall()
            dedup = con.execute("SELECT rowid FROM facts_dedup").fetchall()
            assert len(fts) == 1
            assert fts == dedup
            fact_index.delete_facts(con, [triple])
            con.commit()
            assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
            assert con.execute("SELECT count(*) FROM facts_dedup").fetchone() == (0,)
    finally:
        fact_index.close_writer(con)


def test_delete_facts_removes_every_current_row_regardless_of_valid_from(tmp_path):
    """insert_facts deliberately keeps the same (entity, attribute, value) at
    two distinct valid_from values as two rows (see
    test_insert_facts_keeps_distinct_valid_from_as_separate_rows), and
    delete_facts is deliberately not scoped to a valid_from -- it doesn't
    know which one the current row carries. So the rowid lookup must return
    a list and clear all of them, not seek a single row."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(
            con, [(":tag/v1", ":name", "v1", "2026-01-01T00:00:00.000Z", None)])
        fact_index.insert_facts(
            con, [(":tag/v1", ":name", "v1", "2026-02-01T00:00:00.000Z", None)])
        con.commit()
        assert len(con.execute(
            "SELECT * FROM facts_fts WHERE entity = ':tag/v1'").fetchall()) == 2

        fact_index.delete_facts(con, [(":tag/v1", ":name", "v1", None, None)])
        con.commit()
        assert con.execute("SELECT * FROM facts_fts WHERE entity = ':tag/v1'").fetchall() == []
        assert con.execute("SELECT * FROM facts_dedup WHERE entity = ':tag/v1'").fetchall() == []
    finally:
        fact_index.close_writer(con)


def test_delete_facts_tolerates_an_orphan_dedup_row(tmp_path):
    """Dedup-first insert ordering plus fts-first delete ordering means the
    only inconsistency physically reachable is a dedup row whose fts row is
    missing (an fts insert that failed after the dedup insert committed).
    Deleting it must be a clean no-op on facts_fts and must still clear the
    dedup row, so the rowid is freed rather than blocking a later reinsert."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None)
        fact_index.insert_facts(con, [triple])
        con.commit()
        con.execute("DELETE FROM facts_fts")
        con.commit()

        fact_index.delete_facts(con, [triple])
        con.commit()
        assert con.execute("SELECT count(*) FROM facts_dedup").fetchone() == (0,)

        fact_index.insert_facts(con, [triple])
        con.commit()
        assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (1,)
    finally:
        fact_index.close_writer(con)


def test_rowid_identity_holds_after_dedup_runs_ahead_of_fts(tmp_path):
    """The discriminating test for #236's explicit rowid assignment.

    The four tests above pin the identity but NOT the construction: in every
    scenario they build, facts_fts and facts_dedup share the same
    max(rowid), so the pre-#236 auto-assigned insert produces exactly the
    same rowids as the explicit one and they pass either way (verified by
    reverting insert_facts and re-running them -- all four still passed).
    This test is the one that actually fails on the auto-assigned form.

    It works by first making the two tables' rowid counters diverge: an
    orphan dedup row whose fts insert failed after the dedup row committed,
    which delete_facts' docstring identifies as the one inconsistency that
    is physically reachable. From then on auto-assignment is off by one, so
    a retract seeks the correct dedup rowid and deletes the WRONG fts row --
    the silent index corruption this whole task exists to prevent. Do not
    "simplify" this back into the coincidence case the others cover.
    """
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(con, [(":d/A", ":description", "a", None, None)])
        con.execute(
            "INSERT INTO facts_dedup (entity, attribute, value, valid_from, valid_to) "
            "VALUES (':d/orphan', ':description', 'orphan', '', '')"
        )
        fact_index.insert_facts(con, [
            (":d/C", ":description", "c", None, None),
            (":d/D", ":description", "d", None, None),
        ])
        con.commit()

        fts = dict(con.execute("SELECT entity, rowid FROM facts_fts").fetchall())
        dedup = dict(con.execute("SELECT entity, rowid FROM facts_dedup").fetchall())
        assert fts == {e: r for e, r in dedup.items() if e != ":d/orphan"}

        fact_index.delete_facts(con, [(":d/C", ":description", "c", None, None)])
        con.commit()
        assert sorted(
            row[0] for row in con.execute("SELECT entity FROM facts_fts")
        ) == [":d/A", ":d/D"]
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


def test_rebuild_index_resets_dedup_state_across_rebuilds(tmp_path):
    """A rebuild must clear whatever dedup bookkeeping insert_facts uses --
    otherwise a second rebuild reinserting the exact same fact (identical
    5-tuple, e.g. re-running a full backfill against an unchanged graph)
    would see it as "already indexed" from the first rebuild's dedup state
    and skip inserting it into the freshly emptied facts_fts, leaving the
    index with zero rows for a fact that's actually present."""
    path = str(tmp_path / "t.fts.sqlite3")
    triple = (":decision/x", ":description", "hello world", None, None)
    fact_index.rebuild_index(path, [triple])
    fact_index.rebuild_index(path, [triple])
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == [(":decision/x",)]


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
# #274: the three tests below pin the two halves of rebuild_index's recovery
# loop that the 8-racer test above only exercises probabilistically (it failed
# roughly 2 runs in 30 on Python 3.10 before this fix, which is why CI caught
# it on PR #273 and local runs did not). Each drives the SAME interleaving
# deterministically by orchestrating WHEN a real racing unlink lands, using
# real sqlite3 connections against real files throughout -- no faked
# exceptions, in keeping with this module's real-sqlite3-only convention.
# ---------------------------------------------------------------------------


def _connect_hook(monkeypatch, target_path, side_effect):
    """Run side_effect(real_connect) immediately after rebuild_index's FIRST
    connect to target_path -- inside the window where the connection is open
    but has not yet executed a statement, which is where a racing process's
    unlink lands in the real failure.

    side_effect is handed the UNPATCHED connect so a racer it simulates can
    open databases of its own without counting as one of rebuild_index's
    attempts. Returns the list of connected paths, so a test can prove how
    many attempts the loop made.
    """
    real_connect = fact_index.sqlite3.connect
    calls = []

    def hooked(path_arg, *args, **kwargs):
        con = real_connect(path_arg, *args, **kwargs)
        calls.append(path_arg)
        if path_arg == target_path and len(calls) == 1:
            side_effect(real_connect)
        return con

    monkeypatch.setattr(fact_index.sqlite3, "connect", hooked)
    return calls


def test_rebuild_index_does_not_remove_a_file_it_never_diagnosed(tmp_path, monkeypatch):
    """The corruption branch's os.remove must delete only the file this
    process actually diagnosed as corrupt, not whatever happens to sit at the
    path by the time it gets there. A racer that recovers first replaces the
    corrupt file with a NEW database; a straggler still in its own corruption
    branch would otherwise unlink that replacement -- a file it never
    inspected -- purely because it shares the path.

    Ablation: with the identity guard removed, this fails because the
    replacement's inode is gone from the path afterwards."""
    path = str(tmp_path / "t.fts.sqlite3")
    corrupt_keepalive = str(tmp_path / "corrupt.keepalive")
    with open(path, "wb") as f:
        f.write(b"not a real sqlite file at all, just garbage bytes")
    # A hard link keeps the corrupt inode ALIVE after the path stops pointing
    # at it, so this process's open connection still reads real garbage and
    # takes the corruption branch. Without it the inode would be unlinked and
    # sqlite would raise a transient I/O error instead -- the other test's
    # path, not this one's.
    os.link(path, corrupt_keepalive)

    replacement = str(tmp_path / "replacement.sqlite3")
    replaced_inode = []

    def racer_replaces_the_corrupt_file(real_connect):
        # A different, valid database atomically takes the corrupt file's
        # place -- exactly what the winning racer's rebuild leaves behind.
        con = real_connect(replacement, isolation_level=None)
        con.execute("CREATE TABLE marker (a)")
        con.close()
        os.replace(replacement, path)
        # Recorded HERE, not after rebuild_index returns -- comparing the
        # final inode against itself would assert nothing.
        replaced_inode.append(os.stat(path).st_ino)

    _connect_hook(monkeypatch, path, racer_replaces_the_corrupt_file)
    fact_index.rebuild_index(
        path, [(":decision/x", ":description", "survivor", None, None)]
    )

    # The replacement must still be the file at the path: same inode, never
    # unlinked and re-created underneath us.
    assert replaced_inode, "the racing replacement never ran"
    assert os.stat(path).st_ino == replaced_inode[0]
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == [(":decision/x",)]


def test_rebuild_index_retries_when_its_file_is_unlinked_mid_attempt(tmp_path, monkeypatch):
    """The exact #274 CI failure, driven deterministically. A racer's
    corruption branch unlinks the brand-new empty database that THIS
    process's connect just created; the next statement on that now-unlinked
    inode raises OperationalError('disk I/O error') -- a message matching
    neither 'locked'/'busy' nor the corruption substrings, so it used to
    propagate and fail the run. It is transient: retrying recreates the file.

    Note the victim here is a freshly-created EMPTY database, and the process
    that loses it is one that found no file at the path at all -- so a guard
    keyed on 'the file that was at this path when I started' cannot see the
    substitution (there was no file to name). The guard must key on the file
    the connection is actually using.

    Ablation: without the fix this raises sqlite3.OperationalError."""
    path = str(tmp_path / "t.fts.sqlite3")
    assert not os.path.exists(path)

    def racer_unlinks_our_new_file(_real_connect):
        # connect() created the file eagerly; a racing corruption branch
        # removes it by path before we execute anything against it.
        os.remove(path)

    calls = _connect_hook(monkeypatch, path, racer_unlinks_our_new_file)
    fact_index.rebuild_index(
        path, [(":decision/x", ":description", "recovered", None, None)]
    )

    assert len(calls) > 1, "expected the loop to retry after the unlink"
    con = fact_index.open_reader(path)
    try:
        rows = con.execute("SELECT entity FROM facts_fts").fetchall()
    finally:
        con.close()
    assert rows == [(":decision/x",)]


def test_rebuild_index_still_raises_when_the_file_is_intact(tmp_path, monkeypatch):
    """Negative control for the widened retry: an OperationalError whose file
    is still exactly the file we opened is NOT transient, so it must
    propagate on the first attempt rather than being absorbed by six
    backoffs. A read-only database yields 'attempt to write a readonly
    database' -- one of the very messages the retry gate now tolerates when
    the file has been swapped -- so this proves the gate keys on file
    identity and not on the message text."""
    import pytest
    if os.geteuid() == 0:
        pytest.skip("root ignores the read-only permission bits this relies on")
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/x", ":description", "intact", None, None)])
    # Drop the WAL sidecars so the reopen below has to write the main file.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    os.chmod(path, 0o444)

    real_connect = fact_index.sqlite3.connect
    calls = []

    def counting(path_arg, *args, **kwargs):
        calls.append(path_arg)
        return real_connect(path_arg, *args, **kwargs)

    try:
        monkeypatch.setattr(fact_index.sqlite3, "connect", counting)
        with pytest.raises(sqlite3.OperationalError):
            fact_index.rebuild_index(
                path, [(":decision/y", ":description", "denied", None, None)]
            )
    finally:
        # Restore write permission so pytest's tmp_path cleanup can remove it.
        os.chmod(path, 0o644)

    assert len(calls) == 1, f"a fatal error must not be retried, got {len(calls)} attempts"


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


# ---------------------------------------------------------------------------
# #354: memory_prepare_turn relevance -- query-time memory-only filter and a
# stop-word-free match expression. The index itself is unchanged (it is
# #302's independent witness), so every exclusion here is at QUERY time.
# ---------------------------------------------------------------------------


def test_query_facts_memory_only_excludes_code_rows_but_the_index_keeps_them(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":decision/use-redis", ":description", "use redis for caching", None, None),
        (":function/redis_helper", ":description", "redis caching helper redis caching", None, None),
        (":commit/abc123", ":subject", "switch caching to redis", None, None),
        (":lineage/function-redis_helper", ":status", "redis caching provisional", None, None),
    ])
    fact_index.close_writer(con)

    filtered = fact_index.query_facts(
        path, "redis caching", top_n=10, boost=2.0, historical_discount=1.0, memory_only=True,
    )
    assert [r[0] for r in filtered] == [":decision/use-redis"]

    # Query-time only: without the flag every row is still there to match.
    unfiltered = fact_index.query_facts(
        path, "redis caching", top_n=10, boost=2.0, historical_discount=1.0,
    )
    assert {r[0] for r in unfiltered} == {
        ":decision/use-redis", ":function/redis_helper", ":commit/abc123",
        ":lineage/function-redis_helper",
    }


def test_query_facts_memory_only_limit_applies_after_the_filter(tmp_path):
    """The LIMIT must bound MEMORY rows, not rows in general -- otherwise a
    corpus of better-matching code rows fills the window and the filter
    returns nothing although a memory fact matches."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (f":function/f{i}", ":description", "redis redis redis caching caching", None, None)
        for i in range(20)
    ] + [(":constraint/no-redis", ":description", "redis is banned in production for caching", None, None)])
    fact_index.close_writer(con)
    results = fact_index.query_facts(
        path, "redis caching", top_n=3, boost=1.0, historical_discount=1.0, memory_only=True,
    )
    assert [r[0] for r in results] == [":constraint/no-redis"]


def test_match_query_drops_stop_words_and_single_characters():
    assert fact_index._fts5_match_query("A") is None
    assert fact_index._fts5_match_query("and a 2 and 3") is None
    expr = fact_index._fts5_match_query("create a branch and open a pr")
    assert expr is not None
    for dropped in ('"a"', '"and"'):
        assert dropped not in expr
    for kept in ('"create"', '"branch"', '"open"', '"pr"'):
        assert kept in expr


def test_query_facts_stop_word_only_prompt_matches_nothing(tmp_path):
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, [
        (":decision/a-thing", ":description", "a decision about the thing and a plan", None, None),
    ])
    fact_index.close_writer(con)
    for prompt in ("A", "and the", "is it a"):
        assert fact_index.query_facts(
            path, prompt, top_n=10, boost=2.0, historical_discount=1.0, memory_only=True,
        ) == [], prompt


# --- has_commit_entities (#353) -------------------------------------------
# The navigation nudge asks "has this graph been ingested?" of the index
# instead of taking a graph lease. The predicate must mean what the old
# graph query meant: a LIVE `:entity-type :type/commit` row.

def _index_with(tmp_path, rows):
    path = str(tmp_path / "idx.sqlite3")
    con = fact_index.open_writer(path)
    fact_index.insert_facts(con, rows)
    con.commit()
    con.close()
    return path


def test_has_commit_entities_true_for_a_live_commit_type_row(tmp_path):
    path = _index_with(tmp_path, [
        (":commit/abc123", ":entity-type", ":type/commit", "2026-01-01T00:00:00Z", None),
    ])
    assert fact_index.has_commit_entities(path) is True


def test_has_commit_entities_false_for_an_empty_index(tmp_path):
    path = _index_with(tmp_path, [])
    assert fact_index.has_commit_entities(path) is False


def test_has_commit_entities_false_for_a_missing_file(tmp_path):
    assert fact_index.has_commit_entities(str(tmp_path / "absent.sqlite3")) is False


def test_has_commit_entities_ignores_historical_rows(tmp_path):
    path = _index_with(tmp_path, [
        (":commit/abc123", ":entity-type", ":type/commit",
         "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
    ])
    assert fact_index.has_commit_entities(path) is False


def test_has_commit_entities_ignores_other_commit_attributes(tmp_path):
    path = _index_with(tmp_path, [
        (":commit/abc123", ":description", "a commit", "2026-01-01T00:00:00Z", None),
    ])
    assert fact_index.has_commit_entities(path) is False


def test_has_commit_entities_range_excludes_neighbouring_prefixes(tmp_path):
    # ':commitment/' sorts after ':commit0', ':commit' (no slash) before
    # ':commit/' -- both must fall outside the range seek.
    path = _index_with(tmp_path, [
        (":commitment/x", ":entity-type", ":type/commit", "2026-01-01T00:00:00Z", None),
        (":commit", ":entity-type", ":type/commit", "2026-01-01T00:00:00Z", None),
        (":decision/x", ":entity-type", ":type/commit", "2026-01-01T00:00:00Z", None),
    ])
    assert fact_index.has_commit_entities(path) is False
