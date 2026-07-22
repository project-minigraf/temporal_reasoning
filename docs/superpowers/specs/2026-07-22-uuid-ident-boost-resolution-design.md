# `#uuid`-Tagged Entities Missing the Memory-Fact BM25 Boost — Design

**Date:** 2026-07-22
**Issue:** #194

## Problem

`fact_index.py`'s `_MEMORY_PREFIXES` BM25 boost (`query_facts`) only fires for entity strings
that start with `:decision/`, `:preference/`, `:constraint/`, or `:dependency/`. An entity
created via a keyword (e.g. `[:decision/cache :decision/description "use Redis"]`) gets an
opaque internal UUID inside minigraf, and no `:ident` fact is written back linking that UUID to
`:decision/cache` unless the caller writes one explicitly — today only git-ingestion does this
(mcp_server.py:5380). So when a caller later queries the graph, gets back the entity's raw UUID,
and adds more facts against it via a `#uuid "<uuid>" ...` reference, those facts get indexed
under the raw UUID string — never boost-eligible, even though they're facts about a genuine
decision/preference/constraint/dependency entity.

This is a ranking-quality regression, not data loss: nothing is silently dropped, and the fact
remains fully searchable. But `memory_prepare_turn`'s retrieval ranks these facts lower than an
equally-relevant fact written against the same entity via its original keyword form.

The issue's own suggested fix (resolve `#uuid` → `:ident` at index time, mirroring
`handle_minigraf_audit`'s existing pattern) does not cover the common case on its own: ordinary
`minigraf_transact`-created decision/preference entities never get an `:ident` fact in the first
place, so there's nothing to resolve to.

## Root cause confirmed

- Entities created via a keyword literal in the entity position are internally addressed by an
  opaque UUID; the keyword form is only recoverable if an explicit `:ident` fact was written
  (confirmed via `_rebuild_index_from_graph`'s existing ident-map-with-fallback pattern,
  mcp_server.py:5106-5145).
- Re-transacting an identical `(entity, attribute, value)` triple at a **different** `valid_from`
  creates a new bi-temporal history row every time — confirmed empirically against a real
  `MiniGrafDb` (two `(transact ...)` calls with different `valid_from`, same triple, produced two
  concurrently-current rows). Per #156 (documented in `_checkpoint_after_write`'s docstring),
  minigraf only treats an identical `(entity, attribute, value, valid_from)` tuple as idempotent —
  same `valid_from` collapses, different `valid_from` duplicates. This means any fix that writes
  `:ident` automatically must be gated on "does it already exist," not unconditional, or it will
  bloat history on every subsequent update to the same entity.

## Design

Two coordinated changes, both scoped to `mcp_server.py`.

### Part 1 — auto-write `:ident` on memory-prefixed creates

In `handle_minigraf_transact`, after the primary `_transact` call succeeds, scan the parsed facts
(`_parse_facts_block(facts)`) for distinct keyword entities whose string starts with one of
`fact_index._MEMORY_PREFIXES`. For each one not already assigned an `:ident` fact within this
same call, query `[:find ?v :where [{entity} :ident ?v]]`; if empty, write
`[{entity} :ident "{_edn_escape(entity)}"]` via a second `_transact` call reusing the same
`valid_from` as the primary write (same syntax convention git-ingestion already uses,
mcp_server.py:5380). This is query-gated specifically to avoid the history-bloat behavior
confirmed above. A failure in either the existence check or the write is caught and logged
(`[fact_index] ...` to stderr) — never raised, since the caller's actual write has already
committed by this point and must not be affected.

Scope is deliberately narrow: only entities under the four memory prefixes get this treatment.
Ordinary entities (services, components, git-ingested code entities) are unaffected — the latter
already get an explicit `:ident` from ingestion's own code path.

### Part 2 — resolve `#uuid`-tagged entities to `:ident` when indexing

`_transact` and `_retract` currently derive fact-index rows from `_parse_facts_block(datalog_facts)`
by default when the caller doesn't pass `index_triples` explicitly. Add:

- `_query_ident(db, entity_ref) -> Optional[str]`: executes
  `(query [:find ?v :where [{entity_ref} :ident ?v]])` where `entity_ref` is either a bare keyword
  or a `#uuid "..."`-tagged literal; returns the first string result, or `None` on no match or
  query failure (logged, not raised).
- `_resolved_facts_triples(facts_str, db) -> List[Tuple[str, str, str]]`: calls
  `_parse_facts_block`, then for any triple whose entity token did not come from a keyword (i.e.
  doesn't start with `:` — meaning it was `#uuid`/`#inst`-tagged), resolves it via `_query_ident`,
  falling back to the raw UUID/timestamp string when no `:ident` exists. Resolutions are cached
  per-call (a dict keyed by the raw token) so a UUID referenced across multiple triples in one
  `transact`/`retract` call only queries once.

`_transact` and `_retract` switch their default deriver (used only when `index_triples is None`)
from `_parse_facts_block(datalog_facts)` to `_resolved_facts_triples(datalog_facts, db)`.
`handle_minigraf_audit`'s existing explicit `index_triples` path is untouched — it already
computes its own resolved `kw_ident` more cheaply (from attributes it already fetched) and
continues to bypass this new default path entirely.

This adds zero extra queries for the common case (ingestion, ordinary keyword-only
decision/preference writes) — `_resolved_facts_triples` only queries when a `#uuid`/`#inst`-tagged
entity actually appears in the triples.

Together, Part 1 + Part 2 close the loop: a decision created via keyword now gets a resolvable
`:ident`, and a later `#uuid`-tagged update against it now indexes under that keyword, restoring
the BM25 boost for that fact.

## Known limitation (not fixed by this change)

Entities created **before** this fix that already have facts indexed under a raw UUID will not
retroactively get boosted. This fix only closes the gap going forward, for entities touched again
after deployment. No backfill/migration is in scope — consistent with how other incremental fixes
in this codebase have been handled (e.g. #134's secondary-attribute retraction fix did not
retroactively clean up already-orphaned attributes).

## Testing

Per `docs/testing-conventions.md` (real `minigraf` backend via the `real_db` fixture, no mocks):

- **Part 1:** transact a `:decision/x` fact, assert `[:find ?v :where [:decision/x :ident ?v]]`
  returns `":decision/x"`. Transact a second fact against the same entity and assert the
  `:any-valid-time` row count for `:decision/x :ident` stays at 1 (no duplicate history row).
- **Part 1 scoping:** transact a non-memory-prefix entity (e.g. `:service/auth`) and assert no
  `:ident` fact is auto-written for it.
- **Part 2:** transact a keyword entity, query its raw UUID via a free `?e` clause, transact a
  follow-up fact against `#uuid "<uuid>" ...`, then assert `fact_index.query_facts` returns that
  fact indexed under the keyword entity string (not the raw UUID) and that it receives the boost.
- **Part 2 fallback:** a `#uuid`-tagged entity with no `:ident` anywhere still indexes under the
  raw UUID — no regression from current behavior for entities that were never idented at all.
