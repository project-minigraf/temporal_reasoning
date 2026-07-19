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
import time
from typing import List, Optional, Sequence, Tuple

# Same categories mcp_server.py's write paths use to decide which entities
# get the memory-fact boost at query time. Kept here (not imported from
# mcp_server) to avoid a circular import -- mcp_server.py imports this module.
_MEMORY_PREFIXES = (":decision/", ":preference/", ":constraint/", ":dependency/")

_MMAP_SIZE = 1_073_741_824  # 1 GiB
_BUSY_TIMEOUT_MS = 5000
_SCHEMA_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
    "entity, attribute, value, valid_from UNINDEXED, valid_to UNINDEXED, "
    "tokenize='unicode61')"
)
_META_SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)"
# A real (non-virtual) companion table, not part of facts_fts itself -- FTS5
# virtual tables support neither UNIQUE constraints nor upserts, so exact-row
# dedup for insert_facts (see #152) needs a B-tree-indexed table to check
# against instead of an O(n) scan over facts_fts. entity/attribute/value/
# valid_from/valid_to together are the dedup key; valid_from and valid_to are
# COALESCEd to '' on write because SQL's default UNIQUE semantics treat NULL
# as never equal to itself, which would otherwise let every current fact
# (valid_to=None) dodge the constraint entirely.
_DEDUP_SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS facts_dedup ("
    "entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL, "
    "valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
    "UNIQUE(entity, attribute, value, valid_from, valid_to))"
)
_SCHEMA_VERSION = "3"


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
    """Create facts_fts and index_meta if they don't exist yet, and stamp
    schema_version. Idempotent and safe under concurrent callers (IF NOT
    EXISTS + busy_timeout serializes racers). Deliberately does NOT set the
    'backfilled' meta key -- only rebuild_index() does that, atomically,
    after a genuinely complete rescan. This is the whole fix for the
    write-races-ahead-of-read backfill bug: a file created by an incremental
    write (via open_writer) has a schema but is never mistaken for complete.

    Commits internally -- do NOT call this from rebuild_index(), which needs
    both CREATE statements inside its own explicit BEGIN IMMEDIATE
    transaction; this function's internal commit() would end that
    transaction early and reintroduce the non-atomicity race rebuild_index's
    retry loop exists to prevent. rebuild_index() inlines both schema
    statements instead (_SCHEMA_SQL, _META_SCHEMA_SQL).
    """
    con.execute(_SCHEMA_SQL)
    con.execute(_META_SCHEMA_SQL)
    con.execute(_DEDUP_SCHEMA_SQL)
    con.execute(
        "INSERT OR IGNORE INTO index_meta (key, value) VALUES ('schema_version', ?)",
        (_SCHEMA_VERSION,),
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

    Raises sqlite3.OperationalError if the file doesn't exist. Callers
    (mcp_server.handle_memory_prepare_turn) are expected to check
    needs_backfill() proactively before calling this, not to catch this
    exception reactively -- but the exception is still raised for callers
    that skip that check, or for a file that vanishes between the check
    and the open.
    """
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    _configure(con)
    return con


def needs_backfill(path: str) -> bool:
    """Return True if the index at `path` has not completed a full backfill.

    True when: the file is missing, unopenable/corrupted, lacks the
    index_meta table entirely (a v1 index file predating this schema, or a
    schema-only file from open_writer that never got a real backfill), has
    a mismatched schema_version, or lacks a 'backfilled'='1' row.

    False only when a real rebuild_index() call has completed and committed
    -- the sentinel is set inside that same atomic transaction, so it can
    never be visible without the rebuild genuinely having finished.

    Any sqlite3 exception encountered while checking is itself treated as
    "needs backfill" -- rebuild_index() is self-healing (DROP TABLE IF
    EXISTS + recreate), so a corrupted-but-openable file recovers the same
    way a missing one does.
    """
    if not os.path.exists(path):
        return True
    try:
        con = open_reader(path)
    except sqlite3.Error:
        return True
    try:
        version_row = con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
        if version_row is None or version_row[0] != _SCHEMA_VERSION:
            return True
        backfilled_row = con.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone()
        return backfilled_row is None or backfilled_row[0] != "1"
    except sqlite3.Error:
        # index_meta doesn't exist at all (v1 file) or facts_fts is corrupt.
        return True
    finally:
        con.close()


def close_writer(con: sqlite3.Connection) -> None:
    con.commit()
    con.close()


def insert_facts(
    con: sqlite3.Connection,
    triples: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]],
) -> None:
    """Insert rows into facts_fts, skipping any exact (entity, attribute,
    value, valid_from, valid_to) 5-tuple that's already indexed (#152).
    Does not commit -- caller controls the transaction boundary (immediate
    for single-fact writes, batched per ingestion-commit for git ingestion).
    Each row is (entity, attribute, value, valid_from, valid_to);
    valid_to=None means a current (open-ended) fact, a real ISO timestamp
    means historical.

    Minigraf's own graph is idempotent under re-transacting an identical
    fact with the same validity window -- no new graph fact is created --
    but this used to be a plain INSERT with no corresponding guard, so
    re-transacting an already-current fact (e.g. _watermark_update's
    :entity-type/:ident/:description triples, re-asserted on every ingested
    commit) appended a fresh duplicate row on every call. facts_dedup (a
    real B-tree-indexed table facts_fts itself can't provide, being an FTS5
    virtual table with no UNIQUE/upsert support) makes each row's write
    conditional on genuinely not having been written before, one row at a
    time so INSERT OR IGNORE's per-statement rowcount reliably says whether
    that exact row was new (executemany's rowcount is not per-row reliable
    across sqlite3 driver versions). A distinct valid_from for the same
    (entity, attribute, value) is deliberately NOT deduped -- it's a
    genuinely distinct fact, mirroring minigraf's own graph semantics."""
    if not triples:
        return
    for entity, attribute, value, valid_from, valid_to in triples:
        cur = con.execute(
            "INSERT OR IGNORE INTO facts_dedup "
            "(entity, attribute, value, valid_from, valid_to) VALUES (?, ?, ?, ?, ?)",
            (
                entity, attribute, value,
                valid_from if valid_from is not None else "",
                valid_to if valid_to is not None else "",
            ),
        )
        if cur.rowcount == 0:
            continue
        con.execute(
            "INSERT INTO facts_fts (entity, attribute, value, valid_from, valid_to) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity, attribute, value, valid_from, valid_to),
        )


def delete_facts(
    con: sqlite3.Connection,
    triples: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]],
) -> None:
    """Delete matching CURRENT rows from facts_fts (valid_to IS NULL only).
    Does not commit -- see insert_facts. Historical rows for the same
    (entity, attribute, value) from an earlier lifecycle are never touched
    by a retract -- only the live, open-ended assertion is removed."""
    if not triples:
        return
    con.executemany(
        "DELETE FROM facts_fts WHERE entity = ? AND attribute = ? AND value = ? "
        "AND valid_to IS NULL",
        [(e, a, v) for e, a, v, _vf, _vt in triples],
    )


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


def query_facts(
    path: str, text: str, top_n: int, boost: float, historical_discount: float
) -> List[List[str]]:
    """Ranked, read-only query against the index.

    Returns up to top_n [entity, attribute, value, valid_from, valid_to]
    rows, best match first. Facts whose entity starts with a memory-fact
    prefix (_MEMORY_PREFIXES) get their score multiplied by boost.
    Historical facts (valid_to IS NOT NULL) get their score multiplied by
    historical_discount (expected in (0, 1] -- values below 1 demote
    history below an equally-relevant current fact; 1.0 is neutral).

    All ranking (boost, historical discount) and the top_n bound are applied
    entirely in SQL, inside the same ORDER BY that ranks by bm25() --
    unlike the prior Python-side-rerank-after-fetch approach, a LIMIT here
    can never drop a boost-eligible fact, because boosting happens before
    truncation, not after. FTS5's bm25() is negative-is-better (SQLite
    convention) -- multiplying a negative score by a factor > 1 makes it
    MORE negative, i.e. better/promoted; a factor in (0, 1) makes it closer
    to zero, i.e. worse/demoted. Both boost and historical_discount rely on
    this sign convention: boost should be > 1 to promote, historical_discount
    should be in (0, 1] to demote or leave unchanged.

    Raises sqlite3.OperationalError if the index file doesn't exist -- the
    caller (mcp_server.handle_memory_prepare_turn) is responsible for
    checking fact_index.needs_backfill() before calling this, not for
    catching this exception reactively.
    """
    match_expr = _fts5_match_query(text)
    if match_expr is None:
        return []
    con = open_reader(path)
    try:
        rows = con.execute(
            "SELECT entity, attribute, value, valid_from, valid_to, "
            "  (bm25(facts_fts) "
            "    * (CASE WHEN entity LIKE ':decision/%' OR entity LIKE ':preference/%' "
            "            OR entity LIKE ':constraint/%' OR entity LIKE ':dependency/%' "
            "       THEN ? ELSE 1.0 END) "
            "    * (CASE WHEN valid_to IS NULL THEN 1.0 ELSE ? END) "
            "  ) AS score "
            "FROM facts_fts WHERE facts_fts MATCH ? "
            "ORDER BY score ASC LIMIT ?",
            (boost, historical_discount, match_expr, top_n),
        ).fetchall()
    finally:
        con.close()
    return [[entity, attribute, value, valid_from, valid_to] for entity, attribute, value, valid_from, valid_to, _score in rows]


def rebuild_index(
    path: str,
    facts: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]],
) -> None:
    """Full rebuild: drop and recreate facts_fts + index_meta, bulk-insert
    facts, and stamp the 'backfilled' sentinel -- all inside one atomic
    transaction. Used for backfill (index file missing, schema-only from a
    racing write, wrong schema_version, or corruption recovery).

    The whole drop+create+insert sequence runs inside one explicit transaction
    (BEGIN IMMEDIATE ... COMMIT) so a concurrently-racing rebuild from another
    process can't interleave and produce duplicate rows -- CREATE VIRTUAL TABLE
    IF NOT EXISTS alone only makes that one statement atomic, not the
    multi-statement sequence as a whole. isolation_level=None puts the
    connection in true autocommit mode so Python's own implicit transaction
    management doesn't conflict with the explicit BEGIN IMMEDIATE.

    PRAGMA journal_mode=WAL does not reliably honor busy_timeout's
    retry-and-wait behavior in SQLite (a documented quirk, not something
    BEGIN IMMEDIATE fixes) -- a second racer can still hit "database is
    locked" on that specific PRAGMA even with busy_timeout configured. The
    outer retry loop below handles that, mirroring this codebase's existing
    exponential-backoff pattern for minigraf's own lock contention
    (mcp_server.py's _LOCK_RETRY_MAX/_LOCK_RETRY_BASE).

    Each fact is (entity, attribute, value, valid_from, valid_to);
    valid_to=None for current facts, an ISO timestamp for historical ones.
    """
    attempts = 6
    base_delay = 0.02
    for attempt in range(attempts):
        con = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(f"PRAGMA mmap_size={_MMAP_SIZE}")
            con.execute("BEGIN IMMEDIATE")
            # Note: FTS5-shadow-table-only corruption (e.g. facts_fts_data's
            # structure record) self-heals here unconditionally, before ever
            # reaching the except sqlite3.DatabaseError branch below -- this
            # DROP succeeds even against a corrupted shadow table on the
            # SQLite version this was verified against. If a future SQLite
            # version makes DROP TABLE validate shadow-table contents before
            # dropping, that corruption pattern would start raising here
            # instead, and would need its own message pattern recognized by
            # the except branch below (its real-world message is "fts5:
            # corrupt structure record", matching neither of the two
            # substrings currently checked).
            con.execute("DROP TABLE IF EXISTS facts_fts")
            con.execute("DROP TABLE IF EXISTS index_meta")
            # facts_dedup must be dropped and recreated in lockstep with
            # facts_fts, not just left alone -- insert_facts's dedup guard
            # keys off facts_dedup, so a stale row surviving from a PRIOR
            # rebuild would make it wrongly skip inserting that same fact
            # into the just-emptied facts_fts below (#152 regression test:
            # test_rebuild_index_resets_dedup_state_across_rebuilds).
            con.execute("DROP TABLE IF EXISTS facts_dedup")
            con.execute(_SCHEMA_SQL)  # NOT ensure_schema() -- see its docstring
            con.execute(_META_SCHEMA_SQL)
            con.execute(_DEDUP_SCHEMA_SQL)
            insert_facts(con, facts)
            con.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            con.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('backfilled', '1')"
            )
            con.execute("COMMIT")
            return
        except sqlite3.OperationalError as e:
            message = str(e).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))
        except sqlite3.DatabaseError as e:
            # sqlite3.DatabaseError is the parent class of OperationalError
            # (handled above, never reaches here) but also of
            # ProgrammingError/IntegrityError/DataError/InternalError, none
            # of which indicate file corruption -- only re-raise as "corrupt,
            # remove and retry" for the specific messages SQLite actually
            # uses for a corrupted/non-database file. Anything else (e.g. a
            # caller bug reaching insert_facts with malformed data) must
            # propagate immediately, not be masked behind a corruption
            # detour that deletes a perfectly good file.
            message = str(e).lower()
            if "file is not a database" not in message and "malformed" not in message:
                raise
            if attempt == attempts - 1:
                raise
            # TOCTOU: a concurrently-racing rebuild against the same
            # corrupted file can already have removed it by the time this
            # process gets here (unlike lock/busy contention, corruption is
            # a static file property every racer detects at once, not a
            # timing-dependent one) -- swallow the resulting FileNotFoundError
            # rather than letting it propagate uncaught, since the file being
            # gone is exactly the outcome this branch wants anyway.
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        finally:
            con.close()
