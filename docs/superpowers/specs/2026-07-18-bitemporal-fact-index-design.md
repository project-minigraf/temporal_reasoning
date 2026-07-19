# Bi-temporal FTS index: historical entry points + reliable backfill

## Context

The `feat-persisted-fact-index-118` branch (all 14 plan tasks complete, unmerged) replaced
the in-memory BM25 index with a persisted SQLite FTS5 sidecar (`fact_index.py`). The final
whole-branch review, plus a design discussion working backward from first principles ("why
does this index exist, what does it add over the bi-temporal graph, where does retrieval
actually get used"), surfaced four connected problems with the shipped design — all inherited
from the original pre-#118 `FactIndex`, not introduced by #118 itself, but only now fully
understood in combination:

1. **Backfill is unreliable.** It triggers only when the index *file is missing*
   (`fact_index.query_facts` raising `sqlite3.OperationalError`), but any choke-point write
   creates the file — with only that write's own content — via `_index_write` →
   `fact_index.open_writer` → `ensure_schema()`. Git ingestion auto-starts at MCP server boot
   (`main()`'s `asyncio.create_task(_run_ingestion(...))`), independent of any hook or user
   prompt. On an upgraded graph with git ingestion enabled, a write from auto-start ingestion
   almost always reaches the index before the first read from the `UserPromptSubmit` hook —
   meaning backfill silently never fires and every pre-existing fact becomes permanently
   unretrievable. This is close to the *default* upgrade path, not a rare edge case.
2. **The index has no entry point into history.** It indexes only the current-valid snapshot;
   `_ingest_close`'s bounded re-transacts (deprecated/removed components, superseded facts)
   are deliberately excluded from indexing, and `_retract` deletes rows outright. The
   bi-temporal graph underneath preserves all of this — queryable via `:as-of`/`:valid-at` —
   but `handle_memory_prepare_turn` (the one retrieval path that reaches the model
   *unprompted*, via the hook) is architecturally blind to it. This contradicts the project's
   own stated premise: "Perfect memory. Exact reasoning. Complete history."
3. **Whole-graph scope is correct and stays as-is.** Narrowing the index to user-recorded
   memory facts only (dropping git-ingested code structure) was considered — it would trivially
   fix (1) and shrink (2)'s blast radius — and explicitly rejected: the git-ingested corpus is
   the majority of the graph's actual value to a coding agent, and a retrieval layer that
   ignores most of the corpus would be as broken as one that ignores history. All of Tasks 7-9's
   batching/thread-safety machinery remains necessary at this scope.
4. **The index is purely lexical.** BM25/FTS5 ranks exact token overlap only — "a caching
   layer" cannot match a fact whose only text is "use Redis." Checked against the *original*
   2026-06-04 semantic-retrieval design doc: despite its name, it always delivered lexical
   ranking with no semantic mechanism. The de facto bridges today are rich fact descriptions,
   whole-message OR-token matching, and the agent's own semantic reading of injected context.
   Embeddings were considered and rejected (conflicts with the no-network-calls-in-a-5s-timeout
   hook constraint, and the project's established aversion to heavyweight native dependencies
   per issue #117's own lesson). **Decision: write-time alias enrichment** — generate a handful
   of synonym/concept terms at extraction time (end of a conversation turn, not latency-critical)
   and store them as an indexed `:alias` fact, so lexically-disjoint vocabularies can meet in
   the index without touching the read path's latency budget.

## Decision

Make the FTS index genuinely bi-temporal (index historical facts alongside current ones, with
their validity windows, never silently drop history on close/retract), fix backfill with an
explicit, atomically-set completion marker instead of file-existence inference, and add
write-time alias enrichment at the two LLM/agent-backed extraction strategies.

## Key enabler — verified empirically, not assumed

minigraf can *project* a fact's validity window in a Datalog query via `:db/valid-from`/
`:db/valid-to` pseudo-attributes (integer milliseconds since epoch; the open-ended sentinel is
`9223372036854775807`). Production precedent already exists: `_preload_known_deps`
(mcp_server.py:5367-5390) projects `?vf` this way and converts it to ISO; a passing test
(`tests/test_mcp_server.py:5992`) confirms the round-trip. There is a documented binding
caveat (mcp_server.py:5358-5366): a pseudo-attribute binds to the entity of the *most recent
preceding EAV clause on the same subject variable* — clause ordering matters.

This design needs a *new* combination the codebase hadn't tried before: a **free** `[?e ?a ?v]`
clause (not a named-attribute clause like `_preload_known_deps` uses) joined with the
pseudo-attribute clauses, plus `:any-valid-time` to see retracted/historical facts at all. Given
this exact codebase already found a real, reproducible minigraf bug in this general family — a
query combining a *bound* clause with a *free* clause sharing the same entity variable silently
collapsed to one row under a stale `minigraf==1.1.1` (not reproduced under the pinned
`>=1.2.1`, discovered during #118's Task 11) — this shape was not assumed safe. **A spike test
was written and run against the real, pinned `minigraf==1.2.1` (via `.venv/bin/python3`,
verified via `which` that this bypasses the stale `1.1.1` shadowing `~/.local`) before any
implementation code was written**, checking two things directly:

- **(a) Window binding correctness**: two different facts transacted on the same entity at two
  different times, queried via `[?e ?a ?v] [?e :db/valid-from ?vf] [?e :db/valid-to ?vt]` —
  confirmed each fact's row carries its own correct `valid_from`, no collapse, no
  cross-contamination between the two facts.
- **(b) `:any-valid-time` duplication**: the exact assert → retract → bounded-re-transact
  lifecycle `_ingest_close` performs, queried the same way — confirmed exactly one row comes
  back (the bounded historical fact), not a ghost of the retracted open assertion alongside it.
  A sanity check (a never-closed fact) confirmed the query isn't just failing to see anything —
  it correctly returns one row with the FOREVER sentinel when nothing was ever closed.

**Both checks passed.** No fallback path (a per-attribute-named query loop, or Python-side
dedup) is needed — the query shapes below can be built directly on the verified combination.

**Environment caution**: always use `.venv/bin/pytest` / `.venv/bin/python3` explicitly — bare
`python3`/`pytest` on the primary dev machine resolves to a stale `minigraf==1.1.1` shadowing
this repo's own pinned `>=1.2.1` via `~/.local/lib/python3.14/site-packages`. Current clean
baseline: `.venv/bin/pytest tests/ -q` → 628 passed, 0 failed.

## Changes

### 1. `fact_index.py` — schema v2 + completion marker

- **Schema** (`_SCHEMA_SQL`): add `valid_from UNINDEXED, valid_to UNINDEXED` columns (FTS5
  `UNINDEXED` = stored, not tokenized/searchable — these are metadata, not search text).
  `valid_to IS NULL` ⇒ current fact; non-NULL ⇒ historical, with that window.
- **New meta table**: `CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value
  TEXT)`. Keys: `schema_version` (`"2"`), `backfilled` (`"1"`, set **only** by `rebuild_index`
  inside its own existing `BEGIN IMMEDIATE` transaction — atomically visible only once a
  genuinely complete rebuild has committed). `ensure_schema()` creates both tables and stamps
  `schema_version`, but never sets `backfilled` — that distinction is the whole fix.
- **New `needs_backfill(path: str) -> bool`**: True if the file is missing, unopenable, lacks
  `index_meta`, has a mismatched `schema_version`, or lacks `backfilled=1`. Any `sqlite3`
  exception encountered while checking is itself treated as "needs backfill" — `rebuild_index`
  is already self-healing (`DROP TABLE IF EXISTS` + recreate), so a corrupted-but-openable file
  recovers the same way a missing one does.
- **`insert_facts`**: row shape becomes 5-tuples `(entity, attribute, value, valid_from,
  valid_to)` — one shape for both incremental writes and backfill. `valid_from`/`valid_to` as
  ISO strings or `None`.
- **`delete_facts`**: delete **only current rows** — add `AND valid_to IS NULL` to the WHERE
  clause. A retract removes the open assertion; historical rows for the same `(e, a, v)` from
  an earlier lifecycle must survive untouched.
- **`query_facts`**: select the window columns; move boost/historical-discount ranking **into
  SQL** with a bounded `LIMIT` (this also resolves the whole-branch review's Important #2 —
  the prior unbounded-fetch scale risk):
  ```sql
  SELECT entity, attribute, value, valid_from, valid_to,
         (bm25(facts_fts)
           * (CASE WHEN entity LIKE ':decision/%' OR entity LIKE ':preference/%'
                    OR entity LIKE ':constraint/%' OR entity LIKE ':dependency/%'
              THEN :boost ELSE 1.0 END)
           * (CASE WHEN valid_to IS NULL THEN 1.0 ELSE :hist_discount END)
         ) AS score
  FROM facts_fts WHERE facts_fts MATCH :expr
  ORDER BY score ASC LIMIT :top_n
  ```
  `bm25()` is negative-is-better: `boost > 1` promotes (more negative), `hist_discount` in
  `(0, 1)` demotes (closer to zero). Because scoring is now fully in SQL, the `LIMIT` can never
  drop a boost-eligible row before boosting is applied — the exact bug class Task 2 found and
  fixed is structurally impossible here, since there's no post-hoc Python re-ranking of an
  already-truncated set anymore. Returned rows: `[entity, attribute, value, valid_from,
  valid_to]`. New env var `MINIGRAF_HISTORICAL_DISCOUNT` (default `0.5`), read in
  `handle_memory_prepare_turn` alongside the existing boost/scan-limit vars.
- **`rebuild_index`**: takes 5-tuples; inside its existing `BEGIN IMMEDIATE ... COMMIT` +
  retry-with-backoff loop (Task 3's concurrency fix — untouched, still needed), drop/recreate
  **both** tables, bulk-insert, stamp `schema_version` + `backfilled=1`.
- **v1 → v2 migration**: an old 3-column index file (no `index_meta` table) makes
  `needs_backfill` return True unconditionally (no meta table to check) — the first read
  rebuilds it to v2 from scratch. An incremental write racing ahead and hitting a v1 file before
  that first read fails silently inside `_index_write`'s existing exception swallow; acceptable,
  since the graph still has the fact and the eventual rebuild recovers it. Note this explicitly
  in `_index_write`'s docstring so it isn't mistaken for a new bug later.

### 2. `mcp_server.py` — write path

- **`_transact`**: remove the `if valid_to is None` indexing guard — **always** index, passing
  `(valid_from, valid_to)` through to the 5-tuple row. A bounded transact (the historical half
  of `_ingest_close`) becomes a historical row instead of being silently skipped.
- **`_retract`**: unchanged in shape, but now deletes only current rows (via `delete_facts`'s
  new `valid_to IS NULL` clause). Semantics fall out correctly with **zero changes to
  `_ingest_close` itself**: it pairs every retract with a bounded re-transact, so a closed
  entity's facts become historical rows automatically; a bare user retract
  (`handle_minigraf_retract`, `handle_minigraf_audit`) has no paired re-transact, so it means
  "this fact was wrong" and correctly vanishes with no history left behind.
- **`_index_write`**: thread the window through; keep the never-raise contract (index
  maintenance must never block a graph write).
- **`_rebuild_index_from_graph`**: replace the current two-query shape with window-projecting
  versions:
  - Query 1 (shape unchanged): UUID → keyword-ident lookup, `[:find ?e ?ident :where [?e
    :ident ?ident]]`, now with `:any-valid-time` added.
  - Query 2: `[:find ?e ?a ?v ?vf ?vt :any-valid-time :where [?e ?a ?v] [?e :db/valid-from ?vf]
    [?e :db/valid-to ?vt]]` — verified-safe shape (see "Key enabler" above). Classify each row:
    `?vt == 9223372036854775807` → current (`valid_to=None`); anything else → historical
    (`valid_to=ISO(vt)`). Reuse `_preload_known_deps`'s existing ms→ISO conversion
    (mcp_server.py:5385-5388) rather than duplicating it.
- **`handle_memory_prepare_turn`**: replace the reactive try/except-on-missing-file trigger with
  an explicit check — `if fact_index.needs_backfill(path): _rebuild_index_from_graph()` — then
  query. Keep an outer try/except returning `""` (a turn must never block on memory-retrieval
  failure). Read `MINIGRAF_HISTORICAL_DISCOUNT` alongside the existing env vars.
- **`_format_facts`**: label historical rows with their window, e.g. `:module/cache-py |
  :description | old caching layer  [was valid 2024-06-01 → 2025-01-15]`. Current rows
  unchanged. This is the actual fix for problem (2): the agent gets the entity ident and window
  it needs to follow up with a precise `:as-of`/`:valid-at` Datalog query — the index becomes
  the *entry point* into history, the bi-temporal graph remains the *archive*.

### 3. Write-time semantic enrichment (`mcp_server.py`, extraction prompts)

- **Mechanism**: one `:alias` fact per extracted entity, carrying a short free-text
  synonym/concept phrase list — e.g. `[:decision/use-redis :alias "caching layer, cache
  backend, key-value store"]`. FTS tokenization (splits on non-alphanumeric) turns this single
  row into every bridge token needed. No schema change (`:alias` is already an optional `str`
  attribute on every memory and code-entity type in `MINIGRAF_SCHEMA`), no multi-valued-attribute
  question, and it rides the existing choke point into the index exactly like any other fact.
- **LLM strategy** (`_llm_extract_and_transact`) and **agent strategy**
  (`_agent_extract_and_transact`): extend the extraction prompt (the block instructing the
  Datalog transact format, mcp_server.py ~4649) to also emit one `:alias` triple per entity —
  "2-5 alternative terms or broader concepts a developer might use to refer to this later." The
  triples ride the existing parse/validate/transact path unchanged.
- **Heuristic strategy**: no LLM available → no alias generation; regex extraction is unchanged.
  Degrades gracefully, matching the project's existing no-LLM-configured behavior elsewhere.

## Explicitly not pursued: LLM enrichment during git ingestion

Considered and rejected. Commit messages already serve as a human-written conceptual bridge for
code entities — they're indexed too, and graph-linked to entities via `:introduced-by`/
`:modified-in`, giving *some* signal that the finalize-turn case doesn't have without alias
generation. Two reasons weigh against adding LLM calls to ingestion regardless: ingestion runs
unattended over potentially thousands of commits in one pass (a 21k-commit repo, per this
project's own measured numbers) — per-commit LLM calls would wreck both throughput and cost at
that scale — and ingestion must keep working fully offline/without any LLM configured, matching
the existing heuristic-only fallback's behavior. If poor commit-message hygiene ever makes this
a real, observed gap, the right shape is a separate, explicitly-triggered batch annotation tool
run off the ingestion hot path — not a per-commit call inside `_run_ingestion`. Filed as a
possible future issue if this ever becomes a real, observed problem; not pursued now.

## Also explicitly out of scope for this design

- **Eager server-side backfill trigger at MCP startup** (hook-timeout mitigation) — the
  sentinel fix in this design makes backfill *correct* regardless of where it runs; making it
  run proactively in the long-lived server rather than reactively in the hook would make it
  *cheap* too, but that's a separate, additive change. Filed as
  [project-minigraf/temporal_reasoning#147](https://github.com/project-minigraf/temporal_reasoning/issues/147).
- **The dead-code cleanup flagged during Task 13** (`_MAX_HEURISTIC_ENTITIES`,
  `_build_query_clauses`/`_is_historical_query`/`_HISTORICAL_SIGNALS`/`_DATE_PATTERN`) —
  unrelated to this design, filed separately as
  [project-minigraf/temporal_reasoning#148](https://github.com/project-minigraf/temporal_reasoning/issues/148).
- Three more Minor findings from the final whole-branch review, filed as their own issues
  rather than folded into this design: numeric-valued triples silently dropped from
  auto-derived indexing
  ([#149](https://github.com/project-minigraf/temporal_reasoning/issues/149)), the batched
  `index_con.commit()` failure path during ingestion isn't isolated the way per-triple writes
  are ([#150](https://github.com/project-minigraf/temporal_reasoning/issues/150)), and an
  upgrade/editable-install gap for the `fact_index` module
  ([#151](https://github.com/project-minigraf/temporal_reasoning/issues/151)).
- A minigraf feature request for native server-side `:limit`/`:offset` — filed as
  [project-minigraf/minigraf#310](https://github.com/project-minigraf/minigraf/issues/310)
  since the gap affects the currently-pinned version, not just the older one.
- The original Task-11 bound-clause+free-clause collapse bug was **not** filed against
  minigraf — confirmed to affect only the older, unpinned `1.1.1`, not the pinned `>=1.2.1`
  this project actually depends on.
- PR creation/merge — the user will initiate their own branch review (standing instruction for
  this whole effort).

## Testing (real-backend per `docs/testing-conventions.md`; verify-by-construction for every
"does not happen" assertion — this branch's own history shows that convention repeatedly
catching vacuous tests that looked correct on inspection)

In rough dependency order:

1. **Historical entry point** (the headline behavior): assert a module fact via the choke
   point, close it via `_ingest_close`, then confirm `query_facts`/`handle_memory_prepare_turn`
   surfaces it as a labeled historical row; an equally-matching current fact ranks above it.
2. **Write-race backfill regression** (the original bug from the final review): seed a
   pre-existing fact into the graph *bypassing the index* (a raw `db.execute`, simulating an
   upgrade scenario), perform one choke-point write (creates the index file with only that
   write's content), then confirm `handle_memory_prepare_turn` still surfaces the pre-existing
   fact. Must fail against the pre-this-design code first (proves it actually catches the real
   bug), then pass against the fix.
3. **Schema migration**: hand-build a v1 3-column index file, confirm `needs_backfill` is True,
   confirm rebuild produces a v2 file with all facts present.
4. **Delete-only-current**: assert → close → re-assert the same `(e, a, v)` → retract: the
   historical row from the close survives, the current row from the retract is gone.
5. **Backfill window fidelity**: assert+close directly in the graph, delete the index file,
   rebuild — confirm exactly one historical row with the correct window; current facts have
   `NULL valid_to`; no duplicate rows.
6. **SQL-side ranking**: boost still promotes memory facts past a better-raw-scoring competitor
   (port Task 2's original regression scenario to the new SQL-side scoring); historical discount
   demotes a historical fact below an equally-matching current one; the bounded `LIMIT` doesn't
   reintroduce Task 2's drop bug (boost applies *inside* the `ORDER BY`, not after truncation).
7. Update every existing test for the new 5-tuple row shape / 5-element return shape
   (mechanical; `test_cross_process_reader_sees_writer_commits` and the batching-cadence tests
   must still pass unmodified in behavior).
8. **Alias bridge test**: LLM/agent extraction (the LLM *response text* is mocked, per this
   project's narrow external-API mocking exception — the transact/index path underneath stays
   real) produces an `:alias` fact; a query using a term present *only* in the alias surfaces
   the entity.

## Verification (end to end, beyond unit tests)

1. `.venv/bin/pytest tests/ -q` — fully green (628 baseline + new tests, minus any superseded
   assertions — reconcile the exact count, not just "more tests passed").
2. Real-graph end-to-end: build a small git repo fixture, ingest it, delete a file, re-ingest;
   then from a **fresh subprocess** (hook-style, no shared Python state) call
   `handle_memory_prepare_turn` with text matching the deleted entity — confirm the historical
   row surfaces with its window label.
3. Write-race scenario end-to-end: pre-existing graph + one write racing ahead of the first
   read → `prepare_turn` still recovers everything (the original bug, proven fixed in the
   actual deployment shape, not just a unit test).
4. Re-check the whole-branch invariants this design touches: choke-point exhaustiveness
   (`grep -n "(transact\|(retract" mcp_server.py`, unquoted — the pattern that already caught a
   real 13th bypass site once), the cross-process sharing test, and the batching-cadence test.
