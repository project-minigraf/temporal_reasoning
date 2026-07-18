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
_SCHEMA_VERSION = "2"


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

    Raises sqlite3.OperationalError if the file doesn't exist -- callers
    (mcp_server.handle_memory_prepare_turn) catch this and trigger a
    backfill rebuild, then retry.
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
    """Insert rows into facts_fts. Does not commit -- caller controls the
    transaction boundary (immediate for single-fact writes, batched per
    ingestion-commit for git ingestion). Each row is
    (entity, attribute, value, valid_from, valid_to); valid_to=None means a
    current (open-ended) fact, a real ISO timestamp means historical."""
    if not triples:
        return
    con.executemany(
        "INSERT INTO facts_fts (entity, attribute, value, valid_from, valid_to) "
        "VALUES (?, ?, ?, ?, ?)",
        triples,
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
        # No LIMIT here: FTS5 MATCH already bounds the result set to rows
        # containing at least one OR'd query token (not a full-corpus scan),
        # and the memory-fact boost below needs to see every matching row --
        # a fact whose *unboosted* bm25 rank falls outside a pre-boost LIMIT
        # window would never get a chance to be promoted by the boost.
        rows = con.execute(
            "SELECT entity, attribute, value, bm25(facts_fts) AS score "
            "FROM facts_fts WHERE facts_fts MATCH ? "
            "ORDER BY score ASC",
            (match_expr,),
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
            con.execute("DROP TABLE IF EXISTS facts_fts")
            con.execute("DROP TABLE IF EXISTS index_meta")
            con.execute(_SCHEMA_SQL)  # NOT ensure_schema() -- see its docstring
            con.execute(_META_SCHEMA_SQL)
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
        finally:
            con.close()
