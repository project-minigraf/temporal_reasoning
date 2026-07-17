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
