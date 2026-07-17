# Persisted, mmap-able fact index (#118)

## Problem

Memory retrieval (`handle_memory_prepare_turn`) is backed by `FactIndex` — an
in-memory `rank-bm25` `BM25Okapi` object held behind `IndexCache`, a
module-level singleton rebuilt asynchronously on a background thread whenever
`invalidate()` fires.

This cannot work in the Claude Code hook invocation model. The
`UserPromptSubmit` hook (`hooks/prepare_hook.py`) and the MCP server are
**siblings**, not parent/child — Claude Code spawns each with its own private
stdio pipe back to itself, so there is no channel between them. The hook is a
fresh, short-lived Python process every turn: it imports `mcp_server` cold,
calls `get_db()` and `handle_memory_prepare_turn()`, and exits. `IndexCache`'s
singleton is therefore *always* `None` in the hook — `handle_memory_prepare_turn`
returns `""` at the `index is None` guard before any background rebuild can
possibly finish, and the process exits before that rebuild would even matter.
Nothing carries across turns because `BM25Okapi` has no persistence — it's a
Python object with no disk representation.

Measured on a real graph (~3.8 GB, 21,134 commits):

| hook state | latency | context returned |
|---|---|---|
| `rank-bm25` absent (heuristic fallback) | 81.8 s, ~7.0 GB RSS | non-empty (mostly noise) |
| `rank-bm25` installed (BM25 path) | 1.45 s | **empty** — index cold |

Installing `rank-bm25` (#117's mitigation) makes the hook fast but silent.
BM25 retrieval never actually runs in the hook path.

*Note: #118's own "Environment" section describes the graph as "ArangoDB."
That's a mislabeling — the actual backend is `minigraf`, an embedded
single-file Rust-FFI database (`memory.graph`), not ArangoDB. This doesn't
change the diagnosis or the fix.*

## Goals

- The hook must serve real, ranked results without needing a warm rebuild in
  its own (cold, short-lived) process.
- The index must be shared across the hook and server processes without an
  IPC/RPC dependency on a running server (a fallback UDS design was
  considered and rejected in #118's own text, for the same reason: it
  reintroduces a hard dependency on a healthy long-lived server, which has
  been the source of prior incidents — #103, #116).
- No new native-extension dependency (see "Engine choice" below, which
  revisits #117's lesson about optional/native-extension dependency risk).
- Fold in #141 (`FactIndex._is_memory`'s boost never fires against real data)
  since this design rewrites the exact code the bug lives in.

## Non-goals

- Historical (`:as-of`/`:valid-at`-in-the-past) retrieval through the index.
  Today's `FactIndex` only ever indexes the current-valid snapshot
  (`:valid-at now`); this design preserves that scope exactly and does not
  add a new historical-query capability.
- Changing what "relevant memory context" means (still whole-graph, boosted
  for memory-fact idents) — this is an infrastructure fix, not a retrieval-
  scope change.

## Why not build inside minigraf (Tantivy)

#118's own text reasons "given minigraf is already Rust, the natural engine
is Tantivy," on the assumption that the index would be built as part of
minigraf's own storage engine. That assumption doesn't hold: `minigraf` is a
separate PyPI package (Rust-FFI bindings) whose source lives in a different
repository. This repo (`temporal_reasoning`) doesn't own or build minigraf's
Rust code. The persisted index doesn't need to live there anyway — it's a
derived, rebuildable artifact (a full-text index over facts minigraf already
stores), so it can be a pure sidecar file that `mcp_server.py` builds and
reads itself, entirely independent of minigraf's engine.

## Engine choice: SQLite FTS5

Verified in this environment: `sqlite3.connect(':memory:').execute("CREATE
VIRTUAL TABLE t USING fts5(x)")` succeeds — FTS5 is compiled into this
Python's stdlib `sqlite3`, as it is on all mainstream CPython builds
(python.org installers, most Linux distro packages, Homebrew).

Two engines were considered:

- **SQLite FTS5 (chosen).** Stdlib `sqlite3` — zero new dependency, zero
  wheel/platform risk. Built-in `bm25()` ranking. Native incremental
  `INSERT`/`DELETE` per row (fits the write-path design below). `PRAGMA
  journal_mode=WAL` gives concurrent multi-process readers that never block
  on a writer. `PRAGMA mmap_size` backs reads with mmap, making the OS page
  cache the cross-process shared state — the issue's central architectural
  ask, achieved without a running server. Directly avoids repeating #117's
  lesson: that issue's whole point was that an optional/hard-to-install
  native dependency (`rank-bm25` as an extra) breaks in practice; adding a
  *new* native-extension dependency (`tantivy-py`) to fix the index-sharing
  problem would reintroduce the same risk category one layer up.
- **Tantivy (`tantivy-py`), rejected.** A real Lucene-style segmented
  inverted index with a mmap-based reader model — arguably the "purest" fit
  for the issue's own framing, and technically stronger for very large
  corpora. But it's a new third-party native-extension PyPI dependency with
  its own wheel-availability/platform risk, more implementation surface
  (schema, tokenizer config, writer lifecycle), and no compensating
  requirement this project actually has today that SQLite FTS5 can't meet.
- **Custom on-disk format, rejected.** No dependency, full control — but
  reinvents BM25 ranking, tokenization, and concurrent-access safety that
  FTS5 already solves robustly. Not worth the engineering and testing
  surface.

## Architecture

A single SQLite FTS5 file lives alongside the graph:

- Default path: `<graph_path>.fts.sqlite3` (e.g. `memory.graph.fts.sqlite3`
  next to `memory.graph`).
- Override: `MINIGRAF_INDEX_PATH`, mirroring the existing
  `MINIGRAF_GRAPH_PATH` convention (`_get_graph_path()`).

Both the MCP server process and the hook process open this file directly.
There is no RPC between them and no shared Python object — each process
opens its own `sqlite3.Connection` against the same file, and the OS page
cache (via `PRAGMA mmap_size`) is what makes repeated hook invocations fast
without a rebuild.

### Schema

```sql
CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, tokenize='unicode61');
```

One row per live `[entity, attribute, value]` fact — the same granularity as
today's one-document-per-fact-row `FactIndex`.

### Write path — the choke point

Every write to the graph must also update the index, incrementally, so the
index never drifts and never needs a rescan under normal operation. A fully
exhaustive pass — every `_db_execute(db, ...)` call site in the file, not
filtered by any same-line pattern (an earlier, quote-agnostic-but-still-
same-line-anchored grep pass missed calls where the Datalog string is built
into a local variable on an earlier line) — turned up exactly **12** call
sites: 8 transact, 4 retract.

Transact:
1. `handle_minigraf_transact` (interactive tool)
2. `_ingest_transact` (git ingestion, per-commit code-structure facts)
3. `_ingest_close`'s bounded re-transact (`valid_from` **and** `valid_to`
   both set — the historical-record half of closing an entity)
4. `_watermark_update`'s write of the new watermark hash
5. `_last_run_write` (ingestion completion bookkeeping)
6. `_transact_extracted_facts` (heuristic-extracted memory facts)
7. `_agent_extract_and_transact` (LLM/agent-extracted memory facts — a
   distinct raw call, not routed through `_transact_extracted_facts`)
8. `_ingest_tags` (git tag ingestion)

Retract:
1. `handle_minigraf_retract` (interactive tool)
2. `_ingest_close`'s retract-loop — retracts each open-ended assertion
   one-by-one before the bounded re-transact above. **This is the actual
   mechanism that removes a closed entity/field/class/module's facts from
   the live index** — easy to miss because it's a plain `for` triple in a
   `try/except`, four lines above the more visible bounded-transact call in
   the same function. (An earlier draft of this doc named `_build_close_
   triples` here, which is wrong — that function is pure/no-DB-write
   precomputation, per its own docstring; `_ingest_close` is what actually
   executes against the database.)
3. `_watermark_update`'s retract of the *previous* watermark hash, before
   writing the new one
4. `handle_minigraf_audit`'s schema-violation auto-retract — found only by
   manually reading every `_db_execute` call site, since the Datalog string
   (`retract_expr`) is assembled into a local variable one line above the
   call, invisible to any same-line grep. This site has a real subtlety:
   it retracts using `#uuid "{entity_uuid}"`-tagged literals (deliberately
   — so it can retract without a keyword-to-UUID lookup), but the entity
   was originally *inserted* into the index under its keyword ident (e.g.
   `:decision/foo`), which the function separately resolves as `kw_ident`
   for its own violation-reporting output. If the index-deletion step just
   reused whatever entity string appears in the Datalog retract call, it
   would search the index for the UUID string, find nothing, and leave the
   original keyword-ident rows stranded. See the decoupled signature below.

All 12 are migrated to two new choke-point functions. Their signature
deliberately **decouples** the Datalog string sent to minigraf from the
triples used to update the index, rather than deriving one from the other
via a second regex parser:

```python
def _transact(db, datalog_facts: str, valid_from: str,
               index_triples: List[Tuple[str, str, str]],
               valid_to: Optional[str] = None,
               index_con: Optional["sqlite3.Connection"] = None) -> str:
    """Execute (transact {...opts...} datalog_facts) against minigraf, then
    (only when valid_to is None) insert index_triples into the fact index.

    index_triples is caller-supplied (entity, attribute, value) tuples using
    whatever entity form is actually searchable/meaningful (e.g. the keyword
    ident) — independent of what entity-reference form datalog_facts itself
    uses. For every call site except handle_minigraf_audit these are the
    same triples serialized into datalog_facts; audit's are not (see above).
    """

def _retract(db, datalog_facts: str,
             index_triples: List[Tuple[str, str, str]],
             index_con: Optional["sqlite3.Connection"] = None) -> str:
    """Execute (retract datalog_facts) against minigraf, then delete
    index_triples from the fact index (same decoupling as _transact)."""
```

`index_con`, when supplied, is an already-open write connection the caller
controls the commit boundary for (used by ingestion's batching, below); when
omitted, the function opens a connection, writes, commits, and closes it
immediately (the interactive single-fact path).

This is mechanical for 11 of the 12 call sites — each already holds the
triples as Python data before serializing them into a Datalog string, so
`index_triples` is just "the same values, structured" — but it touches every
write path, by design, so the index can never silently fall out of sync with
one write site that was missed.

**Bi-temporal rule for what's "live":** only `valid_to=None` (open-ended)
transacts are inserted into `facts_fts`. This is the current-valid snapshot
the index represents — exactly what `IndexCache._rebuild`'s `:valid-at now`
query returns today. `_ingest_close`'s bounded re-transact step
(`valid_from` **and** `valid_to` both set, writing history after a close) is
deliberately **not** inserted — it's historical, never part of the
currently-valid snapshot. Concretely, closing an entity nets out to: the
retract-loop deletes the row for the open-ended assertion (via `_retract`),
and the paired bounded re-transact never re-adds it (via the `valid_to`
check in `_transact`) — so a closed entity's facts are absent from
`facts_fts` after both steps run, with no window where a stale row survives.
This preserves today's semantics exactly; no historical-query capability is
introduced.

**Batching for scale.** Large repositories can cross 1M facts well before
ingestion completes. Writes are batched at the same granularity minigraf's
own ingestion already commits at — one SQLite `BEGIN...COMMIT` per
ingestion-commit-batch (matching the existing per-commit `_db_checkpoint`
call in `_run_ingestion`), not one SQLite transaction per triple, which would
be dominated by commit/fsync overhead at this scale. A periodic `PRAGMA
wal_checkpoint` rides alongside the existing per-commit checkpoint so the
index's WAL file doesn't grow unbounded across a long ingestion run.

### Query path

```sql
SELECT entity, attribute, value, bm25(facts_fts) AS score
FROM facts_fts WHERE facts_fts MATCH ?
ORDER BY score ASC LIMIT ?
```

FTS5's `bm25()` is negative-is-better by SQLite convention (opposite of
`rank_bm25`'s positive-is-better), so ranking sorts `ASC`, not `DESC`. The
memory-fact boost multiplier moves to Python, applied post-query:

```python
if entity.startswith(_MEMORY_PREFIXES):
    score *= boost
```

Multiplying a negative score by `boost > 1` makes it *more* negative, i.e.
ranks better — the same effect as today's boost, sign-adjusted for FTS5's
convention. This replaces `FactIndex.query()`'s manual token-overlap
detection and score-shifting entirely; FTS5 already excludes non-matching
rows and handles negative-IDF correctly internally.

### The #141 fix

Today's bug: `IndexCache._rebuild`'s query
(`[:find ?e ?a ?v :where [?e ?a ?v]]`) binds `?e` to minigraf's internal
UUID, not the `:ident` keyword — the same failure class as the
`_preload_known_deps`/`_preload_pinned_commits` bug fixed during #133 (bare
subject variable resolves to the internal UUID unless clause-ordered behind
an explicit `:ident` projection, mirroring `_preload_known_entities`'s
correct pattern). Because `_is_memory` checks `_MEMORY_PREFIXES` against
`row[0]`, and `row[0]` is actually a raw UUID string, the check never
matches — the boost never fires against real data.

The new incremental writer takes triples directly from Python call sites
that already hold the real keyword idents (they're building the Datalog
string from these same values) — it never re-derives the entity from a
query, so this bug class cannot recur on the write path. The one place a
Datalog rescan still happens — backfill (below) — gets the `:ident`-projected
clause-ordering fix explicitly.

### Backfill / bootstrap

If the index file doesn't exist — fresh install, a pre-existing graph from
before this feature shipped, or manual deletion/corruption recovery — a
one-time full rebuild runs: the same `:valid-at now` rescan query
`IndexCache._rebuild` uses today, corrected with the `:ident`-projection fix
above, writing straight into a fresh `facts_fts` table. This is the only
place a full graph rescan happens post-launch. It's triggered lazily:
whichever process (hook or server) first finds the index file missing runs
the rebuild before serving that query — the same self-healing precedent
already used for stale graph-lock recovery (`_open_db_at_with_retry`).

Backfill is the one deliberate exception to "only the server writes"
(below): the hook may also perform it, since it can be the first process to
observe a missing index file after install or corruption-recovery. This is
safe under ordinary SQLite locking — `CREATE VIRTUAL TABLE IF NOT EXISTS`
plus a `busy_timeout` means a second, concurrently-racing backfill (e.g. two
hook invocations firing close together) simply blocks briefly and then finds
the table already populated, rather than corrupting or duplicating rows.

### Concurrency

- `PRAGMA journal_mode=WAL` on the index file — readers never block on a
  writer.
- `PRAGMA mmap_size=<large>` on every connection open — mmap'd reads, OS
  page cache shared across processes.
- Only the interactive MCP server process performs incremental writes
  (mirrors today's single `_db` global — writes are already serialized
  through one process), so no new multi-writer contention is introduced on
  the steady-state path. The lazy backfill path is the sole exception — see
  "Backfill / bootstrap" above.
- Readers (the hook, and the server's own read path) open read-only
  (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`); each hook process
  opens fresh — cheap, since mmap warms from the shared page cache rather
  than requiring a rebuild.

### Error handling

The index-write step inside `_transact`/`_retract` is wrapped in
`try/except`, logging to stderr on failure without raising — preserving
today's "never block a graph write on index maintenance" behavior (mirrors
`IndexCache._rebuild`'s existing exception handling at the call site).

## Deletions

Once the FTS5 path is live, the following become dead and are removed in the
same change: `FactIndex`, `IndexCache`, `_index_cache`,
`_handle_memory_prepare_turn_heuristic`, the `_BM25_AVAILABLE` branch in
`handle_memory_prepare_turn`, and `rank-bm25` — from `pyproject.toml`'s core
`dependencies` and from `install.py`'s `.mcp.json` generation (`[bm25]`
extras string, if any remnants exist post-#117).

FTS5 has no missing-dependency failure mode the way `rank-bm25` did — it's
compiled into stdlib `sqlite3` on every mainstream Python build this project
targets. Keeping a second retrieval implementation around for a failure mode
that can't occur here would be exactly the complexity #117 was trying to
eliminate.

## Testing

Follows `docs/testing-conventions.md`'s "real backend, always" convention,
extended to the new engine: tests use a real `sqlite3` file (`:memory:` for
fast unit tests, a real `tmp_path` file for persistence/cross-process tests)
— never a mocked `sqlite3.Connection`.

- **Cross-process sharing regression test** (the test that would have caught
  #118's actual bug): spawn a subprocess that opens the index file read-only
  *after* the main test process writes to it via `_transact`, and assert the
  subprocess's query sees the committed rows. Mirrors the existing "spawn a
  real subprocess to manufacture a real condition" pattern already used for
  the DB lock-retry cluster (`docs/testing-conventions.md`).
- **Batching test**: assert SQLite commit-call count scales with
  ingestion-commit count, not fact count, guarding the 1M+-fact-scale
  concern.
- **#141 regression test**: assert a fact with a `:decision/`-prefixed ident
  ranks above a non-memory fact with otherwise identical text, using a real
  `facts_fts` table populated via `_transact` (not the old broken rescan
  path).
- **Bi-temporal scope test**: assert a bounded (`valid_to` set) re-transact
  from `_ingest_close` does *not* appear in `facts_fts`, while the original
  open-ended assertion it replaces is removed by the paired retract-loop
  call. This is the test that directly targets the gap this design doc
  itself had until review: `_ingest_close` makes two separate `_db_execute`
  calls (retract-loop, then bounded re-transact), and both must be migrated
  to the choke point for a closed entity to actually disappear from the
  index.
- **Backfill test**: delete the index file, confirm the next query triggers
  a full rebuild that reproduces the same rows an incrementally-built index
  would have.
- Migrate/replace the existing `TestIndexCache`/`FactIndex` test classes in
  `tests/test_mcp_server.py` accordingly; delete tests that only existed to
  cover the now-deleted heuristic fallback.

## Related

- #103 — server rebuilds the whole graph in RAM at startup, blocking the MCP
  handshake. Not directly fixed here, but a persisted index removes the
  index-rebuild portion of that cost entirely.
- #117 — made `rank-bm25` a core dependency and bounded the heuristic
  fallback's scan. This design supersedes both: `rank-bm25` is removed
  entirely, and the heuristic fallback (bounded or not) becomes dead code.
- #141 — `FactIndex._is_memory`'s boost never fires against real data. Fixed
  as part of this design (see above), not deferred further.
- #133 — established the `_preload_known_entities`-style `:ident`-projection
  clause-ordering pattern this design reuses for the backfill rescan.
