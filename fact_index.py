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

# Words that carry no retrieval signal. Lives here rather than in mcp_server
# (which imports it back for heuristic extraction) so the match expression can
# drop them without a circular import. Before #354 the prepare query ORed every
# token of the prompt, so a one-word reply of "A" matched every row containing
# the word "a".
_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would could should "
    "may might shall can need dare ought used to am i we you he she it they what which who "
    "this that these those my our your his her its their about above after all also and as at "
    "before but by for from if in into just me more most no not of on only or other our out "
    "same so than then there they through to too under up us very via was we what when where "
    "which while who why with".split()
)

# SQL predicate for "entity is a memory fact", shared by the boost and the
# memory-only filter so the two can never disagree about what counts.
_MEMORY_ENTITY_SQL = "(" + " OR ".join(
    f"entity LIKE '{prefix}%'" for prefix in _MEMORY_PREFIXES
) + ")"

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
#
# As of #236 this table also serves the DELETE path: its implicit rowid IS
# the corresponding facts_fts rowid (assigned explicitly by insert_facts), so
# delete_facts seeks here and then deletes from facts_fts by rowid rather
# than scanning it. That is why _SCHEMA_VERSION moved to 4 -- in a v3 file
# the facts_fts rowids were auto-assigned and unrelated to these, so the
# identity does not hold and ensure_schema wipes such a file.
_DEDUP_SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS facts_dedup ("
    "entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL, "
    "valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
    "UNIQUE(entity, attribute, value, valid_from, valid_to))"
)
_SCHEMA_VERSION = "4"


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


def _stored_schema_version(con: sqlite3.Connection) -> Optional[str]:
    """Return the schema_version stamped in this index file, or None if
    there isn't one readable -- a v1 file with no index_meta table at all,
    a file whose index_meta exists but lacks the row, or anything that
    raises. All three mean "not the current schema" to the only caller,
    ensure_schema, which treats None exactly like a mismatched version."""
    try:
        row = con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _migrate_schema(con: sqlite3.Connection) -> None:
    """Wipe and recreate all three tables for a schema_version mismatch,
    atomically. Called by ensure_schema, and only when its unlocked read
    said the file is stale.

    Everything happens inside one BEGIN IMMEDIATE ... COMMIT, and the stored
    version is re-read AFTER the write lock is held. That re-read is the
    whole point: the version check ensure_schema made before calling here
    was unlocked, so two callers can both have seen the old version. Under
    pysqlite, DROP/CREATE run in autocommit (con.in_transaction stays False
    -- only DML opens the implicit transaction), so without this the drops
    would commit one at a time with nothing serializing them. The reachable
    damage was not corruption but wasted work: mcp_server's _run_ingestion
    and _run_startup_backfill start concurrently on the first run after an
    upgrade, and an ingestion thread that read the old version before a
    rebuild_index() completed would then drop the freshly-rebuilt tables,
    discarding a multi-minute rescan (self-healing, since needs_backfill()
    stays True -- but the repeat can land in the hook's 5s-bounded
    subprocess, which is what the eager startup backfill of #147 exists to
    keep it out of). With the re-read, the second racer sees the current
    version, drops nothing, and its CREATE ... IF NOT EXISTS statements are
    no-ops.

    isolation_level is forced to None for the duration and restored
    afterwards. rebuild_index's docstring documents the hazard this avoids:
    mixing an explicit BEGIN IMMEDIATE with pysqlite's implicit transaction
    management. Unlike rebuild_index -- which owns its connection and can
    simply connect with isolation_level=None -- this function is handed
    open_writer's connection, which must keep its DEFAULT isolation_level
    once we return: insert_facts/delete_facts deliberately don't commit, and
    batched ingestion writes rely on pysqlite opening an implicit
    transaction for them that close_writer's commit() ends. Leaving the
    connection in autocommit would silently turn every batched write into
    its own transaction. Hence save/restore rather than a permanent change.
    Safe because ensure_schema runs before any caller has issued DML on this
    connection, so there is never a pending implicit transaction for the
    isolation_level assignment to commit out from under.

    No retry loop, unlike rebuild_index. That loop exists for a documented
    SQLite quirk in which PRAGMA journal_mode=WAL alone does not honor
    busy_timeout; BEGIN IMMEDIATE does honor it, so a racer here blocks for
    up to _BUSY_TIMEOUT_MS and then proceeds rather than failing fast. The
    journal_mode PRAGMA on this path is open_writer's, issued before this
    function is reached and unchanged by #236. A genuine lock timeout that
    does escape propagates out of open_writer, where the only production
    caller that can race a long rebuild (_open_index_writer_safe) already
    retries lock errors with exponential backoff and otherwise degrades to
    per-triple index writes.
    """
    prior_isolation = con.isolation_level
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            if _stored_schema_version(con) != _SCHEMA_VERSION:
                # A stale-version file must be emptied, not merely reshaped.
                # The index is a derived cache, so this costs one rebuild and
                # never data -- and dropping index_meta takes the 'backfilled'
                # sentinel with it, so needs_backfill() stays True and the
                # next caller that acts on it (handle_memory_prepare_turn, or
                # _run_startup_backfill) repopulates from the graph's full
                # history. A stale-version file thereby becomes exactly the
                # already-working "index file missing" case. On a brand-new
                # empty file these are no-ops.
                con.execute("DROP TABLE IF EXISTS facts_fts")
                con.execute("DROP TABLE IF EXISTS index_meta")
                con.execute("DROP TABLE IF EXISTS facts_dedup")
            con.execute(_SCHEMA_SQL)
            con.execute(_META_SCHEMA_SQL)
            con.execute(_DEDUP_SCHEMA_SQL)
            con.execute(
                "INSERT OR IGNORE INTO index_meta (key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        con.isolation_level = prior_isolation


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create facts_fts, index_meta and facts_dedup if they don't exist yet,
    and stamp schema_version. Idempotent and safe under concurrent callers:
    the purely additive path below is CREATE ... IF NOT EXISTS plus INSERT OR
    IGNORE, serialized by busy_timeout, while the destructive
    version-mismatch path is delegated to _migrate_schema, which takes a
    write lock and re-reads the stored version under it so a racer whose
    unlocked read was stale drops nothing. Deliberately does NOT set the
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

    On a schema_version mismatch -- including a v1 file with no index_meta
    at all -- all three tables are dropped and recreated empty, by
    _migrate_schema. This is what lets delete_facts trust that
    facts_dedup.rowid IS facts_fts.rowid (#236): a v3 file's facts_fts
    rowids were auto-assigned and unrelated to its dedup rowids, so
    rowid-based deletes against one would remove unrelated rows.
    needs_backfill() already returned True for such a file, but only the
    read path acts on that -- open_writer would otherwise write straight
    into the stale file. Dropping here also supersedes the old #152 caveat
    (a v2 file got facts_dedup created empty, never backfilled, with the
    version never bumped, leaving the dedup guard under-protecting until
    some read happened to trigger a rebuild).
    """
    if _stored_schema_version(con) != _SCHEMA_VERSION:
        _migrate_schema(con)
        return
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
    genuinely distinct fact, mirroring minigraf's own graph semantics.

    The dedup row's rowid is assigned explicitly as the facts_fts rowid
    (#236), making the two tables' rowids the same number by construction --
    that identity is what lets delete_facts seek facts_dedup's B-tree and
    then delete from facts_fts by rowid, instead of scanning the FTS5 table.
    Reading cur.lastrowid is only valid because of the rowcount guard above
    it: on an ignored INSERT OR IGNORE, lastrowid holds the PREVIOUS
    successful insert's rowid rather than being cleared.

    Dedup is written first, so the only inconsistency this path can leave
    behind is a dedup row whose fts insert failed -- costing one unindexed
    fact. The reverse, an fts row with no dedup row, would be far worse (its
    rowid could later be recycled by a new dedup row into a collision) and
    is unreachable from here.
    """
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
            "INSERT INTO facts_fts (rowid, entity, attribute, value, valid_from, valid_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cur.lastrowid, entity, attribute, value, valid_from, valid_to),
        )


def delete_facts(
    con: sqlite3.Connection,
    triples: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]],
) -> None:
    """Delete matching CURRENT rows from facts_fts (valid_to IS NULL only).
    Does not commit -- see insert_facts. Historical rows for the same
    (entity, attribute, value) from an earlier lifecycle are never touched
    by a retract -- only the live, open-ended assertion is removed.

    Seeks facts_dedup for the rowid and then deletes facts_fts BY that rowid
    (#236). The obvious equality DELETE against facts_fts costs O(index size)
    per triple: FTS5 maintains a full-text index, not a B-tree over column
    values, so `WHERE entity = ?` cannot seek and scans the whole table.
    At an 80,000-row index the spec measured 11.088 ms per deleted triple
    against 0.0127 ms for the rowid form -- 73.5% of a full at-scale
    ingestion's wall clock. Those absolutes are one machine's snapshot and
    drift between runs (the landing run read 13.7531 vs 0.0134); the durable
    claim is the shape, legacy linear in index size and the rowid form flat.
    Batching does not help -- the cost follows facts, not calls -- which is
    why this stayed a per-triple loop rather than one big executemany.

    The lookup is a seek on the (entity, attribute, value) prefix of
    facts_dedup's UNIQUE(entity, attribute, value, valid_from, valid_to)
    index. It deliberately does NOT constrain valid_from, matching the
    equality DELETE it replaced -- delete_facts doesn't know which valid_from
    the current row carries, and the same (entity, attribute, value) can hold
    several current rows at distinct valid_from values, all of which go. The
    valid_to = '' filter is the normalized-NULL sentinel insert_facts writes,
    corresponding one-to-one with facts_fts's valid_to IS NULL -- on the
    precondition that no caller ever passes valid_to='' to insert_facts,
    which would normalize to the same '' in dedup (looking current) while
    landing in facts_fts as '' rather than NULL (not current). Every
    production caller satisfies this: _transact (mcp_server.py) passes only
    None or a real ISO timestamp, and _retract passes None, None. This is
    documented as a precondition rather than enforced, since a runtime check
    on the hot per-triple path would cost more than the unreachable case.

    facts_fts is deleted BEFORE facts_dedup, and the order is load-bearing. A
    failure between the two leaves an orphan dedup row: harmless, invisible
    to queries, and cleared by the next delete of that triple. The reverse
    order would leave an orphan facts_fts row -- permanently stale in query
    results, holding a rowid that a later dedup insert could recycle into an
    IntegrityError.

    Clearing facts_dedup is also required for correctness independent of the
    rowid scheme -- code-review finding on #152: a stale dedup row left after
    a retract would make a later insert_facts call for the same (entity,
    attribute, value, valid_from) silently no-op, since the dedup guard can't
    distinguish "already indexed and still live" from "was indexed once,
    since retracted."
    """
    if not triples:
        return
    for entity, attribute, value, _valid_from, _valid_to in triples:
        rowids = [
            row[0] for row in con.execute(
                "SELECT rowid FROM facts_dedup WHERE entity = ? AND attribute = ? "
                "AND value = ? AND valid_to = ''",
                (entity, attribute, value),
            )
        ]
        if not rowids:
            continue
        params = [(rowid,) for rowid in rowids]
        con.executemany("DELETE FROM facts_fts WHERE rowid = ?", params)
        con.executemany("DELETE FROM facts_dedup WHERE rowid = ?", params)


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Split text on non-alphanumeric chars, lowercase, filter empties."""
    return _TOKEN_PATTERN.findall(text.lower())


def _fts5_match_query(text: str) -> Optional[str]:
    """Build an FTS5 MATCH expression that matches ANY query token (OR
    semantics), matching the "any token overlap" relevance model the old
    rank_bm25-based FactIndex used. Returns None if there are no usable
    tokens. Each token is double-quoted to neutralize FTS5 special syntax
    characters a raw user message could otherwise trigger. Stop words and
    single-character tokens are dropped (#354): they match nearly every row
    and carry no signal, so a prompt made only of them matches nothing."""
    tokens = [t for t in _tokenize(text) if len(t) > 1 and t not in _STOP_WORDS]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def query_facts(
    path: str, text: str, top_n: int, boost: float, historical_discount: float,
    memory_only: bool = False,
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

    memory_only restricts results to memory-fact entities (_MEMORY_PREFIXES)
    with a WHERE clause, so top_n bounds memory rows rather than being filled
    by better-matching code rows first (#354). It is a QUERY-time filter by
    design: the index still holds every row, because it is also #302's
    independent witness for evals/at_scale/fact_audit.py.

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
            f"    * (CASE WHEN {_MEMORY_ENTITY_SQL} THEN ? ELSE 1.0 END) "
            "    * (CASE WHEN valid_to IS NULL THEN 1.0 ELSE ? END) "
            "  ) AS score "
            "FROM facts_fts WHERE facts_fts MATCH ? "
            + (f"AND {_MEMORY_ENTITY_SQL} " if memory_only else "")
            + "ORDER BY score ASC LIMIT ?",
            (boost, historical_discount, match_expr, top_n),
        ).fetchall()
    finally:
        con.close()
    return [[entity, attribute, value, valid_from, valid_to] for entity, attribute, value, valid_from, valid_to, _score in rows]


def _file_identity(path: str) -> Optional[Tuple[int, int]]:
    """(st_dev, st_ino) for the file at path, or None if nothing is there.

    Identifies WHICH file a path currently names, so rebuild_index can tell
    "the file I opened" from "whatever happens to sit at this path now" after
    a racing recovery has swapped it. Any OSError (not just
    FileNotFoundError) reads as "no identifiable file": an unreadable path is
    equally not-the-file-we-opened for both callers below.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _is_still_the_same_file(path: str, identity: Optional[Tuple[int, int]]) -> bool:
    """True only when path POSITIVELY still names the file `identity` came
    from. Unknown identity (None) and a vanished path both answer False:
    callers use this to decide whether they may delete a file or must treat a
    failure as fatal, and both of those need proof of sameness, not the
    absence of proof of difference. An unlinked file and a file we could
    never name are equally "not demonstrably ours".
    """
    return identity is not None and _file_identity(path) == identity


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
        # Which file this attempt is about to work on (#274). Sampled BEFORE
        # connect so a racer that swaps the path afterwards is detectable;
        # when the path is empty, sqlite3.connect creates the file eagerly
        # (before any statement runs), so re-sampling after it yields the
        # identity of the file this connection actually holds. That second
        # sample is the load-bearing one: the failure this guards against
        # struck a process that found NO file at the path, had connect create
        # one, and then lost it to a racer's unlink -- an interleaving that a
        # before-connect sample alone cannot see, because there was no file
        # to name at the time.
        #
        # Not airtight, and cannot be: a racer that swaps the path in the
        # window between this stat and the connect leaves us holding an
        # identity for a file we did not open. Closing that would need the
        # inode of the connection's own descriptor, which sqlite3 does not
        # expose. The window is orders of magnitude smaller than the one this
        # replaces (a whole rebuild, vs. two adjacent syscalls), and both
        # callers of the comparison fail safe -- they skip a delete, or take a
        # bounded retry.
        identity = _file_identity(path)
        con = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        if identity is None:
            identity = _file_identity(path)
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
            # Two kinds of transient. Lock/busy contention names itself in the
            # message. The other kind (#274) does not: when a racing recovery
            # unlinks or replaces the file mid-attempt, the statements still
            # running against that now-orphaned inode fail with messages that
            # describe the SYMPTOM, not the race -- observed as "disk I/O
            # error", "attempt to write a readonly database" and "unable to
            # open database file", and varying by which statement happened to
            # touch the file first. Matching those strings would be wrong in
            # both directions: they equally name genuinely fatal conditions
            # (a full disk, a read-only mount), and the list is open-ended.
            # So gate on evidence instead, and demand the evidence point at
            # FATAL: re-raise only when the path positively still holds the
            # same file we opened. Anything else -- a different file, or no
            # file at all -- is the race. Note the asymmetry is deliberate:
            # "no file at the path" cannot mean "we were never able to make
            # one", because sqlite3.connect() runs OUTSIDE this try block, so
            # an unwritable directory already raised there and never reaches
            # this classifier. Reaching here with the path empty means the
            # file existed and something unlinked it.
            if (
                "locked" not in message
                and "busy" not in message
                and _is_still_the_same_file(path, identity)
            ):
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
            # Remove only the file we actually diagnosed (#274). This used to
            # unlink by path unconditionally, which is a different and wider
            # act: every racer detects this corruption at the same instant
            # (it is a static property of the file, unlike timing-dependent
            # lock contention), so by the time a straggler reaches here the
            # winner has typically already removed the corrupt file and a NEW
            # database sits in its place -- and the straggler would delete
            # that replacement, a file it never inspected, purely for sharing
            # the path. That is what broke the 8-racer test. Measured there,
            # the deleted replacement was an EMPTY database that another
            # racer's sqlite3.connect had just created (connect creates the
            # file before any statement runs), and that racer's next
            # statement then failed on the orphaned inode.
            #
            # A mismatch here means some other racer already did this branch's
            # work, so skipping the removal IS the correct outcome, not a
            # missed cleanup. The next attempt re-samples the identity and
            # will remove the new file if that one is corrupt too.
            #
            # TOCTOU: the file can still vanish between this check and the
            # unlink (another racer removing the very same file), so keep
            # swallowing FileNotFoundError -- the file being gone is exactly
            # what this branch wants anyway.
            if _is_still_the_same_file(path, identity):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        finally:
            con.close()
