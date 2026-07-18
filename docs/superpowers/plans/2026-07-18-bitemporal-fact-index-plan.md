# Bi-temporal Fact Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the persisted fact index bi-temporal (index historical facts with their validity windows instead of silently dropping them), fix backfill with an atomically-set completion marker instead of file-existence inference, close the unbounded-fetch scale risk by moving ranking into SQL, and add write-time `:alias` enrichment so lexically-disjoint vocabularies can meet in the index.

**Architecture:** `fact_index.py` gains a schema v2 (window columns + a meta table for the completion sentinel) and SQL-side ranking. `mcp_server.py`'s `_transact`/`_retract` choke point always indexes now (the `valid_to is None` guard is removed — bounded facts become historical rows instead of being skipped), `_rebuild_index_from_graph` gains window projection via minigraf's `:db/valid-from`/`:db/valid-to` pseudo-attributes, and `handle_memory_prepare_turn` switches from reactive (catch a missing-file exception) to proactive (`needs_backfill()` check) backfill triggering. Two extraction prompts gain explicit alias-generation instructions.

**Tech Stack:** Same as the base branch — stdlib `sqlite3` (FTS5), minigraf's Datalog pseudo-attributes. No new dependencies.

## Global Constraints

- Design source of truth: `docs/superpowers/specs/2026-07-18-bitemporal-fact-index-design.md` — re-read it if any task here seems to contradict it; the spec wins.
- **Always use `.venv/bin/pytest` / `.venv/bin/python3` explicitly, never bare `python3`/`pytest`** — this machine has a stale `minigraf==1.1.1` in `~/.local` that shadows this repo's own pinned `minigraf==1.2.1`. `which python3` should show `/usr/bin/python3` (the wrong one); use `.venv/bin/python3` instead.
- Current clean baseline: `.venv/bin/pytest tests/ -q` → 628 passed, 0 failed.
- Testing convention (`docs/testing-conventions.md`, unchanged): every test uses a real `MiniGrafDb`/real `sqlite3` file — never mocked, except the narrow LLM-response-text exception already established for `TestLlmStrategy`/`TestAgentStrategy`.
- "Verify by construction" convention (established repeatedly across the base branch's own review history): for any "X does not happen" assertion, prove the test would actually fail if X happened — simulate the regression, confirm RED, revert, confirm GREEN. Do not trust that a test "looks like" it checks the right thing.
- `_VALID_TIME_FOREVER_MS = (1 << 63) - 1` (mcp_server.py:5316) — minigraf's i64::MAX "still open" sentinel for `:db/valid-to`. Already defined; reuse it, don't redefine.
- Bi-temporal rule (**changed by this plan** — supersedes the base branch's rule): every transact is now indexed, current (`valid_to=None`) as `valid_to IS NULL` in the row, bounded (`valid_to` set) as a historical row carrying that window. A retract deletes only the current row for that `(entity, attribute, value)` — historical rows from earlier lifecycles of the same triple are untouched.
- Every commit follows this repo's established convention: small, TDD (RED before implementation), real backend, frequent commits.

---

## File Structure

- **Modify:** `fact_index.py` — schema v2 (`valid_from`/`valid_to` columns, `index_meta` table), `needs_backfill`, `insert_facts`/`delete_facts` for 5-tuples, `query_facts` SQL-side ranking, `rebuild_index` sentinel-stamping.
- **Modify:** `mcp_server.py` — `_index_write`/`_transact`/`_retract` (always index, thread windows), `_rebuild_index_from_graph` (window projection), `handle_memory_prepare_turn` (proactive backfill check), `_format_facts` (historical labeling), `_LLM_EXTRACTION_PROMPT`/`_AGENT_SAMPLING_PROMPT` (alias-generation instruction).
- **Modify:** `tests/test_fact_index.py` — mechanical 5-tuple/5-element updates to the 24 existing tests, plus new tests for `needs_backfill`, schema migration, and SQL-side ranking (boost + historical discount + bounded LIMIT).
- **Modify:** `tests/test_mcp_server.py` — mechanical updates to `TestHandleMemoryPrepareTurnFts5`/`TestIngestCloseFactIndex`/`TestRunIngestionBatchedIndexWrites`/etc. for the new row shape, plus new tests for the historical entry point, the write-race backfill regression, and the alias bridge.
- **Modify:** `docs/superpowers/specs/2026-07-17-persisted-fact-index-design.md` — rewrite "Backfill / bootstrap" and "Non-goals" sections to reflect the reversal (this plan's own design doc, `2026-07-18-...`, is already written and committed — this task updates the *original* doc so it doesn't contradict the current state).
- **Modify:** `SKILL.md`, `CLAUDE.md` — document `MINIGRAF_HISTORICAL_DISCOUNT`, the existing-but-undocumented `MINIGRAF_PREPARE_SCAN_LIMIT`/`MINIGRAF_MEMORY_BOOST`, the lexical-retrieval + `:alias`-bridging convention, and (briefly) that memory context can include labeled historical facts.

---

### Task 1: `fact_index.py` — schema v2, completion marker, `needs_backfill`

**Files:**
- Modify: `fact_index.py`
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Produces: `needs_backfill(path: str) -> bool`. Modifies `_SCHEMA_SQL` (adds `valid_from UNINDEXED, valid_to UNINDEXED`), adds `_META_SCHEMA_SQL` and `_SCHEMA_VERSION = "2"`, modifies `ensure_schema` to also create `index_meta` and stamp `schema_version`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fact_index.py` (near the existing `test_open_writer_creates_schema`):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_fact_index.py -v -k "index_meta or needs_backfill"`
Expected: FAIL — `needs_backfill` doesn't exist yet (`AttributeError`), and `open_writer` doesn't create `index_meta` yet.

- [ ] **Step 3: Implement schema v2 and `needs_backfill`**

In `fact_index.py`, replace the `_SCHEMA_SQL` constant and add new constants right after it:

```python
_SCHEMA_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5("
    "entity, attribute, value, valid_from UNINDEXED, valid_to UNINDEXED, "
    "tokenize='unicode61')"
)
_META_SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)"
_SCHEMA_VERSION = "2"
```

Replace `ensure_schema`:

```python
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
```

Add `needs_backfill` after `open_reader`:

```python
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
    except sqlite3.OperationalError:
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
    except sqlite3.OperationalError:
        # index_meta doesn't exist at all (v1 file) or facts_fts is corrupt.
        return True
    finally:
        con.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_fact_index.py -v -k "index_meta or needs_backfill"`
Expected: `test_needs_backfill_false_after_rebuild_index` still FAILS (rebuild_index doesn't stamp the sentinel yet — that's Task 2). All other tests in this selection PASS. Confirm this specific expected failure, don't chase it in this task.

- [ ] **Step 5: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "feat: add schema v2 (window columns, index_meta) and needs_backfill to fact_index.py"
```

---

### Task 2: `fact_index.py` — `insert_facts`/`delete_facts`/`rebuild_index` for 5-tuples + sentinel

**Files:**
- Modify: `fact_index.py`
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Consumes: schema v2 from Task 1.
- Produces: `insert_facts(con, triples: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]])`, `delete_facts` (unchanged signature, changed WHERE clause), `rebuild_index(path, facts: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]])` now stamps the sentinel.

- [ ] **Step 1: Write the failing tests**

Update the existing tests in `tests/test_fact_index.py` that construct 3-tuples for `insert_facts`/`delete_facts`/`rebuild_index` to use 5-tuples instead. Apply this exact mechanical transformation everywhere a 3-tuple triple is passed to one of these three functions: `(entity, attribute, value)` becomes `(entity, attribute, value, valid_from, valid_to)`, where `valid_from`/`valid_to` are `None` unless the test is specifically about historical facts (none of the *existing* tests are — that's new in this task and Task 6). Concretely, in `test_insert_and_read_back`:

```python
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
```

Apply the identical `(e, a, v)` → `(e, a, v, None, None)` transformation to: `test_delete_removes_matching_row`, `test_delete_only_removes_exact_match`, `test_open_reader_sees_writer_commits`, `test_rebuild_index_creates_fresh_table`, `test_rebuild_index_replaces_existing_data`, `test_concurrent_rebuild_race_is_safe`, `test_cross_process_reader_sees_writer_commits`. For `delete_facts` calls specifically, the tuple passed to `delete_facts` must match the tuple used at `insert_facts` time (5 elements, `None, None` for current facts) since the new WHERE clause matches on all 5 columns for identifying which row(s) to delete among current rows -- see Step 3.

Add new tests for historical-row survival on delete and for the sentinel:

```python
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


def test_needs_backfill_false_after_rebuild_index(tmp_path):
    # Re-stated here as the GREEN half of Task 1's Step 4 expected-fail note.
    path = str(tmp_path / "t.fts.sqlite3")
    fact_index.rebuild_index(path, [(":decision/x", ":description", "hello", None, None)])
    assert fact_index.needs_backfill(path) is False
```

Note: `test_needs_backfill_false_after_rebuild_index` already exists from Task 1 (it was left failing there) — this step just confirms it now passes; don't duplicate the test function, just verify it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_fact_index.py -v`
Expected: many FAILs — `insert_facts`/`delete_facts` still expect 3-tuples (will raise `sqlite3.ProgrammingError` on a 5-tuple, or silently accept fewer bound params incorrectly), `rebuild_index` doesn't stamp the sentinel.

- [ ] **Step 3: Implement**

Replace `insert_facts`:

```python
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
```

Replace `delete_facts`:

```python
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
```

In `rebuild_index`, change the docstring's type reference and add the sentinel stamp inside the existing atomic transaction. Replace the whole function body from the `BEGIN IMMEDIATE` line through `con.execute("COMMIT")`:

```python
def rebuild_index(
    path: str,
    facts: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]],
) -> None:
    """Full rebuild: drop and recreate facts_fts + index_meta, bulk-insert
    facts, and stamp the 'backfilled' sentinel -- all inside one atomic
    transaction. Used for backfill (index file missing, schema-only from a
    racing write, wrong schema_version, or corruption recovery).

    [... existing concurrency docstring paragraphs about BEGIN IMMEDIATE /
    isolation_level=None / the WAL-mode busy_timeout quirk / the retry loop
    stay unchanged from the base branch -- do not remove them, they document
    a real, previously-reproduced concurrency bug ...]

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
```

Note: `DROP TABLE IF EXISTS index_meta` is new here — this is deliberate: a rebuild must reset `schema_version`/`backfilled` from scratch too (e.g. migrating a v1 file, which has no `index_meta` at all, into a fully-stamped v2 file in one atomic step), not just `facts_fts`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_fact_index.py -v`
Expected: all pass.

- [ ] **Step 5: Re-run the concurrency race test multiple times to confirm the DROP-two-tables change didn't reintroduce the Task-3-class race**

Run: `for i in $(seq 1 15); do .venv/bin/pytest tests/test_fact_index.py -v -k test_concurrent_rebuild_race_is_safe || break; done`
Expected: 15/15 pass. This is exactly the class of bug (a real, reproducible SQLite concurrency race) that took real stress-testing to catch the first time on this branch — don't trust a single green run.

- [ ] **Step 6: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "feat: thread valid_from/valid_to through insert_facts/delete_facts/rebuild_index, stamp backfill sentinel"
```

---

### Task 3: `fact_index.py` — `query_facts` SQL-side ranking rewrite

**Files:**
- Modify: `fact_index.py`
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Produces: `query_facts(path, text, top_n, boost, historical_discount) -> List[List[str]]` — new `historical_discount` parameter; return rows become `[entity, attribute, value, valid_from, valid_to]` (5 elements, was 3).

- [ ] **Step 1: Write the failing tests**

Update every existing `query_facts` test in `tests/test_fact_index.py` for the new 5-element return shape and the new required `historical_discount` parameter. Mechanical transformation: every call site `fact_index.query_facts(path, text, top_n=N, boost=B)` becomes `fact_index.query_facts(path, text, top_n=N, boost=B, historical_discount=1.0)` (pass `1.0` = no discount, i.e. current-vs-historical-neutral, for every *existing* test that isn't specifically testing the discount — this preserves each test's original intent unchanged). Every assertion comparing a result row like `[":decision/x", ":description", "hello"]` becomes `[":decision/x", ":description", "hello", None, None]` (assuming the test's seeded facts used `insert_facts` with `None, None` per Task 2's updates).

Concretely, `test_query_facts_ranks_by_relevance`:
```python
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
```

Apply the same two-part transformation (add `historical_discount=1.0` to every call; add `, None, None` to every expected result row) to: `test_query_facts_excludes_non_matching_rows`, `test_query_facts_on_empty_index_returns_empty`, `test_query_facts_respects_top_n`, `test_query_facts_boosts_memory_prefixed_entities`, `test_query_facts_missing_index_raises`, `test_query_facts_boost_surfaces_fact_outside_old_limit_window`.

Add new tests for the historical discount and the now-bounded LIMIT:

```python
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
    """Regression guard for the Task-2 bug class (an early LIMIT dropping a
    boost-eligible fact) -- proves the new SQL-side LIMIT, unlike the old
    unbounded-Python-fetch-then-truncate approach, still lets a buried
    memory fact win via boost because scoring happens BEFORE the LIMIT in
    the SQL ORDER BY, not after a Python-side truncation."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_fact_index.py -v -k query_facts`
Expected: FAIL — `query_facts` doesn't accept `historical_discount` yet, doesn't return window columns yet.

- [ ] **Step 3: Implement**

Replace `query_facts` in `fact_index.py` entirely:

```python
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
```

Note: `_MEMORY_PREFIXES` is still used elsewhere in this module (the alias/tokenize tests reference it directly) — this rewrite moves the boost decision into SQL via `LIKE` patterns matching the same 4 prefixes, but does not delete the `_MEMORY_PREFIXES` tuple itself.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_fact_index.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "feat: move query_facts ranking (boost, historical discount, LIMIT) fully into SQL"
```

---

### Task 4: `mcp_server.py` — `_index_write`/`_transact`/`_retract` always index, thread windows

**Files:**
- Modify: `mcp_server.py` (`_index_write`, `_transact`, `_retract`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `fact_index.insert_facts`/`delete_facts` (5-tuple, Task 2).
- Produces: `_index_write` now takes 5-tuples. `_transact` no longer skips indexing when `valid_to` is set — it always calls `_index_write`, passing the window through. `_retract`'s `index_triples` are now built as 5-tuples (with `None, None` since retracts only ever target current rows).

This task does NOT yet touch any of the 12 call sites (`_ingest_close`, `_watermark_update`, etc.) — those keep working unchanged, since `_transact`/`_retract`'s own *external* signature (parameters, positional order) doesn't change, only their internal indexing behavior. This task also does not yet touch `_parse_facts_block` (still returns 3-tuples `(entity, attribute, value)` — auto-derived index_triples get `None, None` appended at the `_transact`/`_retract` level, not inside `_parse_facts_block` itself, so that function's own tests are unaffected).

- [ ] **Step 1: Write the failing tests**

Update `tests/test_mcp_server.py`'s `TestTransactRetractChokePoint` class for the new indexing behavior. The critical behavior change: `test_transact_with_valid_to_does_not_index` (which currently asserts a bounded transact is NOT indexed) must be **replaced**, since that's exactly the old behavior this task removes. Replace it with:

```python
    def test_transact_with_valid_to_indexes_as_historical(self, real_db):
        """Bounded (historical) transacts are now indexed too, with their window."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[:decision/x :description "hello"]',
            "2025-01-01T00:00:00.000Z", valid_to="2025-06-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
        assert len(results) == 1
        assert results[0][0] == ":decision/x"
        assert results[0][3] == "2025-01-01T00:00:00.000Z"  # valid_from
        assert results[0][4] == "2025-06-01T00:00:00.000Z"  # valid_to
```

Update every other test in `TestTransactRetractChokePoint` that calls `fact_index.query_facts` to pass `historical_discount=1.0` explicitly (mechanical, matching Task 3's transformation), and every assertion on a result row to expect 5 elements (append `, None, None` for current facts seeded via `_transact` with no `valid_to`). For example `test_transact_writes_to_index`:

```python
    def test_transact_writes_to_index(self, real_db, tmp_path):
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[:decision/x :description "hello"]', "2026-01-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(index_path, "hello", top_n=10, boost=2.0, historical_discount=1.0)
        assert results == [[":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None]]
```

Apply the same pattern to `test_transact_writes_to_minigraf` (no query_facts call, unaffected), `test_retract_removes_from_index`, `test_retract_removes_from_minigraf` (unaffected), `test_transact_explicit_index_triples_overrides_auto_derive` (its explicit `index_triples` tuple needs `, None, None` appended — see Step 3's signature change), `test_transact_index_write_failure_does_not_raise` (unaffected, doesn't call query_facts).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k TestTransactRetractChokePoint`
Expected: FAIL — `query_facts` signature mismatch (missing `historical_discount`), `test_transact_with_valid_to_indexes_as_historical` fails because bounded transacts still aren't indexed.

- [ ] **Step 3: Implement**

In `mcp_server.py`, replace `_index_write`, `_transact`, `_retract`:

```python
def _index_write(
    action: str,
    triples: List[Tuple[str, str, str, Optional[str], Optional[str]]],
    index_con: Optional[Any] = None,
) -> None:
    """Apply an insert or delete to the fact index, never raising -- index
    maintenance must never block a graph write. action is 'insert' or 'delete'.
    Each triple is (entity, attribute, value, valid_from, valid_to). When
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
    """Execute (transact {opts} datalog_facts) against minigraf, then write
    index_triples into the fact index -- ALWAYS, not just when valid_to is
    None. A current (valid_to=None) transact is indexed as a live row; a
    bounded transact is indexed as a historical row carrying its window,
    which is the actual entry point into retracted/superseded graph regions
    (see the design doc). This is the one behavior change from the base
    branch's _transact: previously bounded transacts were skipped entirely.

    index_triples defaults to auto-parsing datalog_facts via
    _parse_facts_block() (which returns 3-tuples (entity, attribute, value)
    -- the window is appended here, not inside that function, since
    _parse_facts_block has no way to know valid_from/valid_to); pass
    index_triples explicitly when the Datalog string's own entity reference
    isn't the searchable identity (e.g. handle_minigraf_audit's #uuid-tagged
    retracts, whose index_triples must use the resolved keyword ident
    instead) -- in that case pass 3-tuples too, the window is still appended
    here uniformly.
    """
    opts = f':valid-from "{valid_from}"'
    if valid_to is not None:
        opts += f' :valid-to "{valid_to}"'
    raw = _db_execute(db, f"(transact {{{opts}}} {datalog_facts})")
    triples_3 = index_triples if index_triples is not None else _parse_facts_block(datalog_facts)
    triples_5 = [(e, a, v, valid_from, valid_to) for e, a, v in triples_3]
    _index_write("insert", triples_5, index_con=index_con)
    return raw


def _retract(
    db: Any,
    datalog_facts: str,
    index_triples: Optional[List[Tuple[str, str, str]]] = None,
    index_con: Optional[Any] = None,
) -> str:
    """Execute (retract datalog_facts) against minigraf, then delete the
    matching CURRENT row from the fact index (same decoupling as _transact
    -- index_triples overrides auto-derivation when the Datalog entity
    reference isn't the searchable identity). delete_facts only ever
    targets valid_to IS NULL rows, so historical rows from an earlier
    lifecycle of the same (entity, attribute, value) are untouched -- pass
    None, None for the window here unconditionally, since a retract only
    ever means "remove the live assertion.\""""
    raw = _db_execute(db, f"(retract {datalog_facts})")
    triples_3 = index_triples if index_triples is not None else _parse_facts_block(datalog_facts)
    triples_5 = [(e, a, v, None, None) for e, a, v in triples_3]
    _index_write("delete", triples_5, index_con=index_con)
    return raw
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k TestTransactRetractChokePoint`
Expected: all pass.

- [ ] **Step 5: Run the full suite to check for regressions from this signature-adjacent change**

Run: `.venv/bin/pytest tests/ -q`
Expected: many failures — every other call site (`_ingest_close`, `handle_minigraf_audit`, etc.) and every other test that calls `fact_index.query_facts` without `historical_discount`, or asserts on a 3-element result row, is now broken. This is expected at this point in the plan — Tasks 5-8 fix these systematically. Do not attempt to fix them in this task; just confirm the failures are all of this expected shape (missing `historical_discount` kwarg, or a result-row-length assertion), not something new/unexpected.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: _transact always indexes now (bounded facts become historical rows, not skipped)"
```

---

### Task 5: `mcp_server.py` — fix every remaining caller of `query_facts`/index-triple construction across the 12 choke-point call sites

**Files:**
- Modify: `mcp_server.py` (no behavior change needed in `_ingest_close`, `handle_minigraf_audit`, `_watermark_update`, etc. themselves — they all call `_transact`/`_retract`, whose *external* signatures didn't change in Task 4)
- Modify: `tests/test_mcp_server.py` — every test across the file that calls `fact_index.query_facts` directly, or asserts on a fact-index result row's shape.

**Interfaces:**
- No new interfaces. This task is purely mechanical test repair, isolated from Task 4's implementation change so that task's diff stays reviewable.

- [ ] **Step 1: Find every remaining broken call site**

Run: `.venv/bin/pytest tests/ -q 2>&1 | grep "^FAILED"`
Read through the full failure list. Every failure should be one of exactly two shapes:
1. `TypeError: query_facts() missing 1 required positional argument: 'historical_discount'` — fix: add `historical_discount=1.0` to that call (neutral, preserves the test's original intent, matching Task 3's convention for pre-existing tests).
2. An assertion comparing a fact-index result row to a 3-element list/tuple — fix: append `, None, None` (or the appropriate window values if the test seeded a bounded fact, but none of the *pre-existing* tests do — that's new territory covered in Tasks 6-8).

Do NOT guess at this list in advance — the exact set of affected tests depends on the current state of the file, which has been touched by 14+ prior tasks. Enumerate it from the actual failing-test output.

- [ ] **Step 2: Fix each one mechanically**

Known candidates likely to need this (confirm against the actual Step 1 output, this list may be incomplete or have drifted):
- `TestMinigrafAudit::test_audit_retract_removes_from_fact_index_by_keyword_ident`
- `TestIngestCloseFactIndex` (all 3 tests)
- `TestIngestTransactFactIndex::test_ingest_transact_writes_to_index_with_explicit_con`
- `TestBookkeepingWritesFactIndex` (all 4 tests)
- `TestConversationalMemoryFactIndex` (both tests)
- `TestHandleMemoryPrepareTurnFts5` (handled separately in Task 6, since that class also needs the `handle_memory_prepare_turn` behavior change — skip it here, don't fix it in this task)
- `TestIndexCacheInvalidation` (the two still-real tests: `test_successful_transact_triggers_invalidation`, `test_successful_retract_triggers_invalidation`)

For each, apply the same two-part mechanical fix as Task 3/4's pattern. Do not change any test's underlying assertion *intent* — only the call signature and result-row shape.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest tests/ -q 2>&1 | grep "^FAILED"`
Expected: only `TestHandleMemoryPrepareTurnFts5` and anything touching `_rebuild_index_from_graph` should remain — those are Task 6's job. Confirm the remaining failure list is now scoped to exactly that, not something broader (if something outside that scope still fails, investigate before proceeding — it may be a genuine regression, not expected mechanical fallout).

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test: fix historical_discount/result-row-shape breakage across the 12 choke-point call sites' tests"
```

---

### Task 6: `mcp_server.py` — `_rebuild_index_from_graph` window projection + `handle_memory_prepare_turn` proactive backfill

**Files:**
- Modify: `mcp_server.py` (`_rebuild_index_from_graph`, `handle_memory_prepare_turn`, `_format_facts`)
- Test: `tests/test_mcp_server.py` (`TestHandleMemoryPrepareTurnFts5`)

**Interfaces:**
- Produces: `_rebuild_index_from_graph()` now builds 5-tuples with real windows. `handle_memory_prepare_turn` calls `fact_index.needs_backfill()` proactively instead of catching a missing-file exception reactively. `_format_facts` labels historical rows.
- New env var: `MINIGRAF_HISTORICAL_DISCOUNT` (default `0.5`).

This is the task that closes the original backfill-completeness bug (the whole reason this plan exists) and adds the historical-entry-point behavior.

- [ ] **Step 1: Write the failing tests**

Replace `TestHandleMemoryPrepareTurnFts5` in `tests/test_mcp_server.py` with the following (some tests carry over with mechanical fixes, several are new):

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

    def test_returns_empty_string_on_a_totally_fresh_graph(self, real_db):
        import mcp_server
        result = mcp_server.handle_memory_prepare_turn("anything at all")
        assert result == ""

    def test_memory_facts_rank_above_non_memory_facts(self, real_db):
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching layer"] '
            '[:decision/use-redis :entity-type :type/decision] '
            '[:decision/use-redis :ident ":decision/use-redis"]]',
            reason="test",
        )
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:function/unrelated :name "use redis for caching layer somewhere else use redis for caching layer somewhere else"]'],
            "2026-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("use redis for caching layer")
        redis_pos = result.find(":decision/use-redis")
        other_pos = result.find(":function/unrelated")
        assert redis_pos != -1
        assert other_pos == -1 or redis_pos < other_pos

    def test_respects_scan_limit_env_var(self, real_db, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_PREPARE_SCAN_LIMIT", "2")
        for i in range(5):
            mcp_server.handle_minigraf_transact(
                f'[[:decision/x{i} :description "redis caching option {i}"]]', reason="test"
            )
        result = mcp_server.handle_memory_prepare_turn("redis caching")
        lines = [l for l in result.splitlines() if "|" in l]
        assert len(lines) <= 2

    def test_triggers_backfill_when_index_file_missing(self, real_db, tmp_path):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching"]]', reason="test"
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)
        result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert "use redis for caching" in result

    def test_backfill_recovers_facts_written_without_explicit_ident(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/prefer-sqlite :description "prefer sqlite over postgres for embedded use"]]',
            reason="test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)
        result = mcp_server.handle_memory_prepare_turn("prefer sqlite over postgres for embedded use")
        assert "prefer sqlite over postgres for embedded use" in result

    def test_backfill_preserves_all_facts_for_an_idented_entity(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/use-redis :description "use redis for caching layer"] '
            '[:decision/use-redis :entity-type :type/decision] '
            '[:decision/use-redis :ident ":decision/use-redis"]]',
            reason="test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        os.remove(index_path)
        result = mcp_server.handle_memory_prepare_turn("use redis for caching layer")
        assert "use redis for caching layer" in result
        assert result.count(":decision/use-redis") >= 1

    def test_write_race_backfill_regression(self, real_db):
        """THE regression test for the original bug: a fact pre-exists in
        the graph (seeded via a raw db.execute, bypassing the index choke
        point entirely -- simulating a pre-existing graph from before this
        feature, or before this fix, ever indexed it), then ONE choke-point
        write happens (creating the index file with only its own content),
        THEN handle_memory_prepare_turn must still recover the pre-existing
        fact. Must fail against the reactive file-existence check (proves
        this test catches the real bug), pass against needs_backfill()."""
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z"} '
            '[[:decision/pre-existing :description "a decision from before this feature shipped"]])'
        )
        # One choke-point write -- creates the index file via open_writer,
        # with only ITS OWN content, before any read has ever happened.
        mcp_server.handle_minigraf_transact(
            '[[:decision/unrelated :description "something else entirely"]]', reason="test"
        )
        result = mcp_server.handle_memory_prepare_turn("a decision from before this feature shipped")
        assert "a decision from before this feature shipped" in result

    def test_historical_fact_surfaces_as_labeled_entry_point(self, real_db):
        """The headline new behavior: a closed/removed entity's facts are
        still findable, labeled as historical with their validity window."""
        import mcp_server
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:module/old-cache :description "legacy caching layer using memcached"]'],
            "2024-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            mcp_server.get_db(),
            ['[:module/old-cache :description "legacy caching layer using memcached"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("legacy caching layer using memcached")
        assert ":module/old-cache" in result
        assert "2024-01-01" in result
        assert "2025-01-01" in result

    def test_current_fact_ranks_above_equally_matching_historical_fact(self, real_db):
        import mcp_server
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:module/old-cache :description "shared caching layer text for ranking test"]'],
            "2024-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            mcp_server.get_db(),
            ['[:module/old-cache :description "shared caching layer text for ranking test"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:module/new-cache :description "shared caching layer text for ranking test"]'],
            "2025-01-01T00:00:00.000Z", "test",
        )
        result = mcp_server.handle_memory_prepare_turn("shared caching layer text for ranking test")
        old_pos = result.find(":module/old-cache")
        new_pos = result.find(":module/new-cache")
        assert new_pos != -1 and old_pos != -1
        assert new_pos < old_pos

    def test_respects_historical_discount_env_var(self, real_db, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_HISTORICAL_DISCOUNT", "1.0")
        mcp_server._ingest_transact(
            mcp_server.get_db(),
            ['[:module/old-cache :description "discount env var test text repeated repeated"]'],
            "2024-01-01T00:00:00.000Z", "test",
        )
        mcp_server._ingest_close(
            mcp_server.get_db(),
            ['[:module/old-cache :description "discount env var test text repeated repeated"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        # With discount=1.0 (neutral), historical and current-equivalent
        # scoring collapses to pure relevance -- just confirm it still finds
        # the historical fact at all when the discount is disabled.
        result = mcp_server.handle_memory_prepare_turn("discount env var test text repeated repeated")
        assert ":module/old-cache" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k TestHandleMemoryPrepareTurnFts5`
Expected: FAIL — `_rebuild_index_from_graph` doesn't project windows yet, `handle_memory_prepare_turn` doesn't check `needs_backfill` yet, `_format_facts` doesn't label historical rows yet. Confirm `test_write_race_backfill_regression` specifically fails against the current (pre-this-task) code — this is the proof it catches the real bug.

- [ ] **Step 3: Implement `_rebuild_index_from_graph`**

Replace the function body (keep the docstring's explanation of the `:ident`-projection pattern and the free-clause-vs-named-clause distinction from the base branch, since that reasoning is still exactly why query 1 stays a bound-only lookup — add a new paragraph documenting the spike-test verification of query 2's new shape):

```python
def _rebuild_index_from_graph() -> None:
    """One-time full rebuild: rescan the graph's full history (not just the
    current-valid snapshot) and write it into a fresh fact_index table, with
    each fact's validity window preserved -- this is what makes a closed/
    retracted entity's facts recoverable as labeled historical entries after
    an index file is lost or was never built. This is the only place a full
    Datalog rescan happens post-launch (everywhere else is incremental via
    _transact/_retract) -- triggered by fact_index.needs_backfill().

    [... existing paragraphs about the :ident-projection query-1 shape and
    why _preload_known_entities' clause-ordering pattern alone isn't
    sufficient safety for a free [?e ?a ?v] clause stay unchanged here ...]

    Query 2 now also projects each fact's validity window via minigraf's
    :db/valid-from/:db/valid-to pseudo-attributes, combined with a free
    [?e ?a ?v] clause and :any-valid-time (to see retracted/historical facts
    at all, not just current ones). This exact combination -- pseudo-attrs
    joined to a FREE clause, not a named one like _preload_known_deps uses
    -- was not previously exercised anywhere in this codebase and was
    spike-tested directly against the real, pinned minigraf>=1.2.1 before
    being relied on here (see the 2026-07-18 design doc): confirmed correct
    per-fact window binding (no collapse/cross-contamination) and confirmed
    :any-valid-time does not duplicate a retracted-then-bounded-re-transacted
    fact as a ghost row alongside its historical replacement.

    A row's ?vt equal to _VALID_TIME_FOREVER_MS means still-open (current,
    valid_to=None in the index); any other value means historical
    (valid_to=ISO(?vt)). ms->ISO conversion reuses the exact pattern
    _preload_known_deps already uses, rather than duplicating it.
    """
    db = get_db()
    ident_raw = _db_execute(
        db, '(query [:find ?e ?ident :any-valid-time :where [?e :ident ?ident]])'
    )
    ident_map = {e: ident for e, ident in json.loads(ident_raw).get("results", [])}

    facts_raw = _db_execute(
        db,
        "(query [:find ?e ?a ?v ?vf ?vt :any-valid-time "
        ":where [?e ?a ?v] [?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])",
    )
    triples = []
    for e, a, v, vf_ms, vt_ms in json.loads(facts_raw).get("results", []):
        entity = ident_map.get(str(e), str(e))
        vf_iso = (
            datetime.datetime.fromtimestamp(int(vf_ms) / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        if int(vt_ms) == _VALID_TIME_FOREVER_MS:
            vt_iso = None
        else:
            vt_iso = (
                datetime.datetime.fromtimestamp(int(vt_ms) / 1000, datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
        triples.append((entity, str(a), str(v), vf_iso, vt_iso))
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    fact_index.rebuild_index(path, triples)
```

**Important — verify before trusting this code**: run a focused manual check (or write it as part of Step 4's test run) confirming query 1 also needs `:any-valid-time` added (the base branch's version didn't have it, since it only ever needed to look up *currently* idented entities — but backfill must now recover idents for *historical* entities too, or a closed entity's historical rows would fall back to their raw UUID instead of the correct ident). This is a deliberate addition in the code above (`:any-valid-time` added to query 1) — confirm it doesn't break the existing `:ident`-projection safety property (it shouldn't, since `:any-valid-time` only affects *which* facts are visible, not the free-vs-named-clause join-shape question that was the actual risk).

- [ ] **Step 4: Implement `handle_memory_prepare_turn` and `_format_facts`**

Replace `handle_memory_prepare_turn`:

```python
def handle_memory_prepare_turn(user_message: str) -> str:
    """Query the persisted fact index for facts relevant to the user message,
    including labeled historical (retracted/superseded) facts -- the index
    is the entry point into history, the bi-temporal graph is the archive.

    Returns a formatted context block string for injection as
    additionalContext, or an empty string if no relevant facts are found.
    Proactively checks fact_index.needs_backfill() before querying (fresh
    install, pre-existing graph, corruption recovery, or a write that raced
    ahead of the first read all leave the index in a needs-backfill state --
    see the 2026-07-18 design doc for why file-existence alone is not a
    reliable signal).
    """
    scan_limit = int(os.environ.get("MINIGRAF_PREPARE_SCAN_LIMIT", "50"))
    boost = float(os.environ.get("MINIGRAF_MEMORY_BOOST", "2.0"))
    historical_discount = float(os.environ.get("MINIGRAF_HISTORICAL_DISCOUNT", "0.5"))
    path = fact_index.index_path_for(_graph_path or _get_graph_path())
    try:
        if fact_index.needs_backfill(path):
            _rebuild_index_from_graph()
        results = fact_index.query_facts(
            path, user_message, top_n=scan_limit, boost=boost,
            historical_discount=historical_discount,
        )
    except Exception as e:
        print(f"[fact_index] prepare_turn failed: {e}", file=sys.stderr)
        return ""
    if not results:
        return ""
    return f"Relevant memory context:\n{_format_facts(results)}"
```

Replace `_format_facts`:

```python
def _format_facts(results: List[List[str]]) -> str:
    """Format fact-index rows as a readable block. Each row is
    [entity, attribute, value] (2-tuple attr/val rows from other callers) or
    [entity, attribute, value, valid_from, valid_to] (5-element fact-index
    rows). A historical row (valid_to present and non-None) is labeled with
    its validity window so the agent has the entity ident + window it needs
    to follow up with a precise :as-of/:valid-at Datalog query."""
    if not results:
        return ""
    lines = []
    for row in results:
        if len(row) == 5:
            entity, attribute, value, valid_from, valid_to = row
            base = f"  {entity} | {attribute} | {value}"
            if valid_to is not None:
                base += f"  [was valid {valid_from} → {valid_to}]"
            lines.append(base)
        else:
            lines.append("  " + " | ".join(str(v) for v in row))
    return "\n".join(lines)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k TestHandleMemoryPrepareTurnFts5`
Expected: all pass, including `test_write_race_backfill_regression` (the headline fix) and `test_historical_fact_surfaces_as_labeled_entry_point` (the headline new capability).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: close to fully green — Task 7 covers the remaining historical-scenario and schema-migration-specific tests this task didn't add. Confirm no *unexpected* failures (anything outside `_format_facts`-consumers-with-old-2-3-element-row-assumptions or the not-yet-written Task 7/8 tests).

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: fix backfill-completeness bug with needs_backfill(); index historical facts as labeled entry points into history"
```

---

### Task 7: Bi-temporal write-path regression tests — `_ingest_close`'s two-step lifecycle end to end

**Files:**
- Modify: `tests/test_mcp_server.py` (`TestIngestCloseFactIndex`)

**Interfaces:**
- No new interfaces. This task adds the specific regression coverage the design doc's testing section calls for: delete-only-current, and backfill window fidelity, exercised through the real `_ingest_close` two-step lifecycle (not just direct `fact_index` calls, which Task 2 already covers at the module level).

- [ ] **Step 1: Write the failing tests**

Add to `TestIngestCloseFactIndex` in `tests/test_mcp_server.py`:

```python
    def test_close_produces_a_historical_row_not_a_dropped_one(self, real_db):
        """Complements the existing test_close_removes_open_assertion_from_index
        (which only proves the CURRENT row is gone) -- this proves the fact
        didn't just vanish, it became a labeled historical row."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]', "2024-01-01T00:00:00.000Z",
        )
        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT entity, valid_from, valid_to FROM facts_fts WHERE entity = ':module/foo'"
            ).fetchall()
        finally:
            con.close()
        assert rows == [(":module/foo", "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z")]

    def test_reopen_after_close_produces_current_plus_historical_rows(self, real_db):
        """assert -> close -> re-assert the same (e, a, v): the historical
        row from the close survives, a new current row exists too -- proves
        delete_facts' valid_to IS NULL scoping (Task 2) holds through the
        real _ingest_close/_transact call sites, not just direct fact_index
        calls."""
        import mcp_server
        import fact_index
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]', "2024-01-01T00:00:00.000Z",
        )
        mcp_server._ingest_close(
            real_db, ['[:module/foo :description "the foo module"]'],
            "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z", "test",
        )
        mcp_server._transact(
            real_db, '[[:module/foo :description "the foo module"]]', "2025-06-01T00:00:00.000Z",
        )
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT valid_from, valid_to FROM facts_fts WHERE entity = ':module/foo' ORDER BY valid_from"
            ).fetchall()
        finally:
            con.close()
        assert rows == [
            ("2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z"),
            ("2025-06-01T00:00:00.000Z", None),
        ]

    def test_backfill_after_close_reconstructs_the_same_historical_row(self, real_db):
        """assert+close directly (no fact_index involvement at all, simulating
        a pre-existing graph), delete the index, backfill -- the rebuilt
        index has exactly the historical row a live _ingest_close would
        have produced, with the same window. Uses raw minigraf calls (not
        the choke point) for the seed, to prove backfill reconstructs
        windows correctly from the graph alone, not from any index state."""
        import mcp_server
        import fact_index
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z"} '
            '[[:module/bar :description "the bar module"]])'
        )
        real_db.execute('(retract [[:module/bar :description "the bar module"]])')
        real_db.execute(
            '(transact {:valid-from "2024-01-01T00:00:00.000Z" :valid-to "2025-01-01T00:00:00.000Z"} '
            '[[:module/bar :description "the bar module"]])'
        )
        mcp_server._rebuild_index_from_graph()
        index_path = fact_index.index_path_for(mcp_server._graph_path)
        con = fact_index.open_reader(index_path)
        try:
            rows = con.execute(
                "SELECT entity, valid_from, valid_to FROM facts_fts WHERE entity = ':module/bar'"
            ).fetchall()
        finally:
            con.close()
        assert rows == [(":module/bar", "2024-01-01T00:00:00.000Z", "2025-01-01T00:00:00.000Z")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k "test_close_produces_a_historical_row_not_a_dropped_one or test_reopen_after_close_produces_current_plus_historical_rows or test_backfill_after_close_reconstructs_the_same_historical_row"`
Expected: FAIL before Tasks 4/6 land correctly (should already pass if Tasks 1-6 were done correctly and in order — if any of these three fail at this point, it means Task 4 or Task 6 has a real gap, not just a missing test; investigate before proceeding rather than assuming this task's own tests are wrong).

- [ ] **Step 3: If all three already pass (expected, since Tasks 4-6 already implemented the underlying behavior), this task is pure regression-coverage addition — proceed to commit**

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test: add end-to-end bi-temporal regression coverage through the real _ingest_close/_rebuild_index_from_graph call sites"
```

---

### Task 8: Write-time `:alias` enrichment

**Files:**
- Modify: `mcp_server.py` (`_LLM_EXTRACTION_PROMPT`, `_AGENT_SAMPLING_PROMPT`)
- Test: `tests/test_mcp_server.py` (`TestLlmStrategy`/`TestAgentStrategy` or a new class alongside them)

**Interfaces:**
- No new function signatures — this task only changes prompt text. `:alias` is already a valid optional attribute on `decision`/`preference`/`constraint`/`dependency` in `MINIGRAF_SCHEMA` (no schema change needed) and is already parsed/validated/transacted through the existing `_transact_extracted_facts`/`_parse_transact_facts` path unchanged.

**Important nuance verified before writing this task**: `:alias` already appears in both prompts today, but only as a *reuse-for-dedup* mechanism ("if a reference matches an existing canonical ident or alias above, reuse that exact ident") — there is currently no instruction telling the model to *generate* new alias terms for search-bridging purposes. This task adds that instruction as a new, clearly-separated paragraph; it does not touch or weaken the existing dedup guidance.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`, near `TestLlmStrategy`/`TestAgentStrategy` (mock the LLM *response text* only, per this project's narrow external-API exception — the transact/index path underneath stays real):

```python
class TestAliasEnrichment:
    def test_llm_strategy_alias_bridges_a_lexically_disjoint_query(self, real_db, monkeypatch):
        """The actual point of this feature: a query using words that never
        appear in the fact's own description text finds it anyway, via an
        LLM-generated :alias fact."""
        import mcp_server

        def fake_call_llm(model, prompt):
            return (
                '[[:decision/use-redis :description "use Redis for the caching layer"] '
                '[:decision/use-redis :alias "in-memory data store, key-value cache, caching backend"]]'
            )

        monkeypatch.setattr(mcp_server, "_call_llm", fake_call_llm)
        result = mcp_server._llm_extract_and_transact("let's use Redis for caching")
        assert result["ok"] is True
        assert result["stored_count"] >= 1
        # "key-value cache" appears ONLY in the alias, never in the description.
        context = mcp_server.handle_memory_prepare_turn("key-value cache")
        assert ":decision/use-redis" in context

    def test_agent_strategy_alias_bridges_a_lexically_disjoint_query(self, real_db, monkeypatch):
        import mcp_server
        import asyncio as _asyncio

        async def fake_request(conversation_delta, canonical_section):
            return (
                '[[:decision/use-redis :description "use Redis for the caching layer"] '
                '[:decision/use-redis :alias "in-memory data store, key-value cache, caching backend"]]'
            )

        monkeypatch.setattr(mcp_server, "_request_agent_memory_block_async", fake_request)
        monkeypatch.setattr(mcp_server, "_query_canonical_entities", lambda: "")
        result = _asyncio.run(mcp_server._agent_extract_and_transact("let's use Redis for caching"))
        assert result["ok"] is True
        context = mcp_server.handle_memory_prepare_turn("key-value cache")
        assert ":decision/use-redis" in context

    def test_llm_extraction_prompt_instructs_alias_generation(self):
        """A prompt-content smoke test: the instruction must actually exist,
        not just be theoretically supported by the schema/parser."""
        import mcp_server
        assert "alias" in mcp_server._LLM_EXTRACTION_PROMPT.lower()
        assert "synonym" in mcp_server._LLM_EXTRACTION_PROMPT.lower() or "alternative term" in mcp_server._LLM_EXTRACTION_PROMPT.lower()

    def test_agent_sampling_prompt_instructs_alias_generation(self):
        import mcp_server
        assert "alias" in mcp_server._AGENT_SAMPLING_PROMPT.lower()
        assert "synonym" in mcp_server._AGENT_SAMPLING_PROMPT.lower() or "alternative term" in mcp_server._AGENT_SAMPLING_PROMPT.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k TestAliasEnrichment`
Expected: the two prompt-content smoke tests FAIL (no alias-generation instruction present yet). The two end-to-end tests may pass or fail depending on whether the fake LLM response's `:alias` fact gets stored and indexed correctly by the *existing* (unmodified) transact/parse path — if they fail, that's informative (a real gap in the existing pipeline, not just missing prompt text) and must be investigated, not assumed away.

- [ ] **Step 3: Implement — add alias-generation instructions to both prompts**

In `_LLM_EXTRACTION_PROMPT`, insert a new paragraph immediately after the existing `IMPORTANT — entity resolution:` paragraph (do not modify that paragraph):

```
IMPORTANT — alias generation: for each NEWLY-minted entity (not one you're reusing an
existing ident for), also emit an :alias fact with 2-5 comma-separated alternative
terms, synonyms, or broader concepts a developer might later use to refer to it —
e.g. for a decision to use Redis, `:alias "in-memory data store, key-value cache,
caching backend"`. Retrieval is purely lexical (exact word match), so these aliases
are what let a later, differently-worded query still find this fact.
```

Apply the equivalent addition to `_AGENT_SAMPLING_PROMPT`, immediately after its existing `Use these attributes:` line (which already lists `:alias (optional)` — leave that line as-is, add the new paragraph after it):

```
For each newly-minted entity, also emit an :alias fact with 2-5 comma-separated
alternative terms or broader concepts someone might use to refer to it later —
retrieval is purely lexical, so this is what lets a differently-worded query still
find the fact.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k TestAliasEnrichment`
Expected: all pass. If the two end-to-end tests still fail after the prompt change (they shouldn't need the prompt change to pass, since the fake LLM response already hardcodes an `:alias` fact — the prompt-content tests are testing something different), investigate the transact/parse/index path directly rather than assuming the prompt fix will resolve it.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: instruct LLM/agent extraction strategies to generate :alias bridge terms for new entities"
```

---

### Task 9: Docs sync

**Files:**
- Modify: `docs/superpowers/specs/2026-07-17-persisted-fact-index-design.md`
- Modify: `SKILL.md`
- Modify: `CLAUDE.md`

**Interfaces:** None — documentation only.

- [ ] **Step 1: Rewrite the original design doc's "Backfill / bootstrap" section**

In `docs/superpowers/specs/2026-07-17-persisted-fact-index-design.md`, find the "Backfill / bootstrap" section (documents the file-existence check this plan replaces). Replace its content with a short pointer:

```markdown
### Backfill / bootstrap

**Superseded** by the 2026-07-18 bi-temporal fact index design
(`docs/superpowers/specs/2026-07-18-bitemporal-fact-index-design.md`), which replaced the
file-existence check described in the original version of this section with an explicit,
atomically-set completion marker (`fact_index.needs_backfill()`) — the original approach
had a real bug: any incremental write reaching the index before the first read would
create a schema-only file that was indistinguishable from a fully-backfilled one. See the
newer doc for the current design.
```

- [ ] **Step 2: Rewrite the original design doc's "Non-goals" section**

Find the "Non-goals" bullet about historical retrieval (`"Historical (:as-of/:valid-at-in-the-past) retrieval through the index..."`). Replace it with:

```markdown
- ~~Historical (`:as-of`/`:valid-at`-in-the-past) retrieval through the index.~~
  **Reversed** by the 2026-07-18 bi-temporal fact index design — a first-principles
  design discussion found that excluding history from the one retrieval path that
  reaches the model unprompted (the hook) contradicted the project's own bi-temporal-
  memory premise. See that doc for the current design; historical facts are now
  indexed as labeled entry points into the graph's archive.
```

- [ ] **Step 3: `SKILL.md` updates**

In the `## Graph Storage` section (or wherever `MINIGRAF_INDEX_PATH` was documented by the base branch's Task 14), add:

```markdown
Memory context returned by `memory_prepare_turn` can include historical facts (things
that were true in the past but have since changed or been removed) alongside current
ones — historical entries are labeled with their validity window, e.g. `[was valid
2024-06-01 → 2025-01-15]`. Follow up with a precise `:as-of`/`:valid-at` query against
the graph directly for the full picture at that point in time.

Retrieval is purely lexical (exact word/token match, not semantic similarity) — write
fact descriptions and `:alias` values that name both the concept and the specific
technology/term someone might search for later (e.g. a decision described only as "use
Redis" won't be found by a query for "caching layer" unless an alias bridges them).

Tuning env vars: `MINIGRAF_PREPARE_SCAN_LIMIT` (default 50, max facts returned),
`MINIGRAF_MEMORY_BOOST` (default 2.0, ranking boost for decision/preference/constraint/
dependency facts over git-ingested code structure), `MINIGRAF_HISTORICAL_DISCOUNT`
(default 0.5, ranking discount for historical facts relative to current ones — values
below 1.0 demote history, 1.0 is neutral).
```

- [ ] **Step 4: `CLAUDE.md` update**

In the `## Graph Storage` section, add one line after the existing `MINIGRAF_INDEX_PATH` mention:

```markdown
The fact index is bi-temporal: it includes historical (retracted/superseded) facts
alongside current ones, labeled with their validity window.
```

- [ ] **Step 5: Run the full suite one final time**

Run: `.venv/bin/pytest tests/ -q`
Expected: fully green. Note the exact final pass count (baseline 628 + every test added across Tasks 1-8) for the commit message and for the eventual whole-branch review.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-07-17-persisted-fact-index-design.md SKILL.md CLAUDE.md
git commit -m "docs: sync design doc, SKILL.md, CLAUDE.md for the bi-temporal fact index"
```

---

## Post-plan verification checklist (for the final whole-branch review)

- [ ] Choke-point exhaustiveness re-check: `grep -n "(transact\|(retract" mcp_server.py` (unquoted — the pattern that already caught a real 13th bypass site once on the base branch) — confirm no new raw `_db_execute` write bypassing `_transact`/`_retract` was introduced by this plan.
- [ ] Re-run `test_cross_process_reader_sees_writer_commits` and the batching-cadence tests (`TestRunIngestionBatchedIndexWrites`) — confirm the schema v2 changes (extra columns, extra `index_meta` table) didn't affect either invariant.
- [ ] Confirm `_MEMORY_PREFIXES` is still single-sourced in `fact_index.py` (Task 3's SQL-side `LIKE` patterns duplicate the 4 prefix strings as literals in SQL — this is a deliberate, unavoidable duplication since SQL can't reference a Python tuple; flag it in the review as intentional, not a regression of the single-source-of-truth property Task 14 verified for the *Python-level* constant).
- [ ] Confirm the full suite's final count reconciles against the 628 baseline plus every test this plan added (don't just check "more tests pass," verify the exact delta).
