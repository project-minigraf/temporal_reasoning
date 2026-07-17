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
    "entity, attribute, value, tokenize='unicode61')"
)


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
    concurrent callers (IF NOT EXISTS + busy_timeout serializes racers).

    Commits internally -- do NOT call this from rebuild_index(), which needs
    the CREATE statement inside its own explicit BEGIN IMMEDIATE transaction;
    this function's internal commit() would end that transaction early and
    reintroduce the non-atomicity race rebuild_index's retry loop exists to
    prevent. rebuild_index() inlines the schema SQL (_SCHEMA_SQL) instead.
    """
    con.execute(_SCHEMA_SQL)
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


def rebuild_index(path: str, facts: Sequence[Tuple[str, str, str]]) -> None:
    """Full rebuild: drop and recreate facts_fts, then bulk-insert facts.

    Used for backfill (index file missing -- fresh install, pre-existing
    graph, or corruption recovery). The whole drop+create+insert sequence
    runs inside one explicit transaction (BEGIN IMMEDIATE ... COMMIT) so a
    concurrently-racing rebuild from another process can't interleave and
    produce duplicate rows -- CREATE VIRTUAL TABLE IF NOT EXISTS alone only
    makes that one statement atomic, not the 3-statement sequence as a
    whole. isolation_level=None puts the connection in true autocommit
    mode so Python's own implicit transaction management doesn't conflict
    with the explicit BEGIN IMMEDIATE.

    PRAGMA journal_mode=WAL does not reliably honor busy_timeout's
    retry-and-wait behavior in SQLite (a documented quirk, not something
    BEGIN IMMEDIATE fixes) -- a second racer can still hit "database is
    locked" on that specific PRAGMA even with busy_timeout configured. The
    outer retry loop below handles that, mirroring this codebase's existing
    exponential-backoff pattern for minigraf's own lock contention
    (mcp_server.py's _LOCK_RETRY_MAX/_LOCK_RETRY_BASE).
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
            con.execute(_SCHEMA_SQL)  # NOT ensure_schema() -- see its docstring
            insert_facts(con, facts)
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
