# Provisional/Authoritative Lineage Fact Model + Candidate-Diff Persistence — Design Spec

**Issue:** #222 (Phase 2, sub-phase 2a of 4)
**Date:** 2026-07-24 (revised same day after spec review — see "Revision" note)

## Background

#222's overall design is a converging multi-stream ingestion: a forward-truth
stream (the existing engine) running concurrently with a reverse-bulk-fill
stream that provisionally back-fills recent history from `HEAD` downward, so
recent history is visible almost immediately while the forward stream still
owns lineage correctness. Phase 1 (merged, PR #226) built the foundational
piece both streams need to claim work from — a frontier/interval registry
and shared-gap allocator (`frontier_registry.py` + `mcp_server.py`'s
`_frontier_load`/`_frontier_persist_claim`) — but nothing from phase 1 is
wired into the actual ingestion walk yet.

Phase 2 ("Streams 1+2 converging") is itself too large for one cycle and
splits into four sub-phases:

- **2a (this spec)** — the provisional/authoritative fact model and
  candidate-diff persistence primitives. Foundational: 2b/2c/2d all depend
  on this existing first.
- **2b** — Stream 2's actual reverse-bulk-fill walk, using 2a's primitives
  to write provisional lineage.
- **2c** — Stream 1's correction sweep, continuing past the point where
  Stream 2 already covered ground, converting provisional facts to
  authoritative using 2a's persisted candidate diffs (cheap replay, no
  re-parse).
- **2d** — the actual concurrency wiring inside `_run_ingestion`: two
  asyncio tasks sharing the frontier allocator and the existing single
  write_executor, with fairness so neither starves.

Like phase 1, 2a builds only the data-model plumbing — no caller yet. 2b
writes with it, 2c reads/clears with it, 2d ties everything together into
the actual commit loop.

## Revision note

A review of the first draft of this spec (grounded in the actual repo code
and `MINIGRAF_SCHEMA`) found two High and two Medium issues, all confirmed
real against the code before this revision:

1. **High** — writing `:lineage-status` directly onto tracked code entities
   (`module`/`function`/`class`/`variable`/`field`) is unsafe: those types
   are registered in `MINIGRAF_SCHEMA` (mcp_server.py:5083) and audited by
   `minigraf_audit` (mcp_server.py:3711), which retracts any attribute not
   in a type's allowed set — `:lineage-status` isn't one, so a routine
   audit run would silently destroy the provisional marker. **Fixed** by
   moving the marker to its own unregistered companion entity (see below).
2. **High** — "the deterministic ident is naturally idempotent" was false:
   re-transacting the same (entity, attribute, value) at a new valid_from
   creates a *duplicate live datom* under minigraf's actual write semantics
   (documented at `_checkpoint_after_write`, mcp_server.py:3295, issue
   #156), independent of whether the ident itself is deterministic. **Fixed**
   by giving every new write function the same query-before-write guard
   `_watermark_update` already established (mcp_server.py:4867-4909).
3. **Medium** — `lineage-confirmed-through` was underspecified as a trust
   predicate: it stays unset for the region phase 1's migration already
   seeded `[C0, W]` authoritative, so a consumer checking only the
   watermark would wrongly treat that region as unconfirmed. **Fixed** by
   seeding the watermark to `W` at the same moment migration seeds the
   `[C0, W]` frontier interval, making "position ≤ lineage-confirmed-through"
   a complete standalone predicate with no special-casing.
4. **Medium** — candidate-diff records' schema/audit status was implicit.
   **Fixed** by stating explicitly (see below) that both new entity types
   this spec introduces are deliberately unregistered, internal/scratch
   bookkeeping — the same status phase 1's own `:type/ingest-interval`
   already has.

## Scope (2a only)

In scope:

- A per-entity provisional marker for lineage (`:introduced-by`), stored on
  its own companion entity (not on the tracked code entity itself — see
  Revision note #1), following phase 1's migration precedent: absent =
  authoritative, so every entity ingested by today's existing forward-only
  walk (which never creates this companion entity) reads as authoritative
  with zero migration required.
- A `lineage-confirmed-through` watermark entity, structurally identical to
  `:ingestion/watermark`, giving a cheap single-query "is region X's lineage
  fully confirmed" answer without scanning individual entity markers — the
  same mechanism phase 4's status reporting (and a future
  `memory_prepare_turn` trustworthiness check) will read.
- A persisted candidate-diff record schema: one entity per (commit,
  candidate entity) pair, holding the entity's #221 normalized-body-hash —
  enough for Stream 1's later correction sweep (2c) to confirm or reject a
  candidate via hash comparison, without re-invoking git-show + tree-sitter
  parsing.
- A small addition to phase 1's already-merged `_frontier_seed_from_watermark`
  to also seed the new lineage-confirmed-through watermark (see Revision
  note #3) — the only change to existing code in this sub-phase.

Explicitly deferred: the actual reverse-walk diffing logic that decides
what counts as a "candidate" (2b), the rename-spanning-the-gap resolution
and confirm/reject replay logic (2c), and the concurrency wiring (2d). 2a
provides only the read/write primitives those will call.

## Design

### Schema/audit status of new entity types (applies to both sections below)

`:type/lineage-marker` and `:type/candidate-diff` (defined below) are
**deliberately not added to `MINIGRAF_SCHEMA`** — internal bookkeeping,
same status as phase 1's own `:type/ingest-interval`. `minigraf_audit`
(mcp_server.py:3711) only iterates `MINIGRAF_SCHEMA`'s known entity types
when looking for violations to retract (`for entity_type in
MINIGRAF_SCHEMA`, mcp_server.py:3728) — it never scans for or touches
entities of a type it doesn't know about. This is a load-bearing property
this design relies on, not an incidental detail: it is what keeps these
scratch/tracking entities safe from ever being silently swept by a routine
audit run. Neither type needs `:description`/`:ident` or any other
schema-required attribute for the same reason — they are outside the
closed-world surface `MINIGRAF_SCHEMA` governs entirely.

### Provisional marker

A separate companion entity per tracked entity — **not** an attribute on
the tracked entity itself (see Revision note #1: `module`/`function`/
`class`/`variable`/`field` are schema-validated/audited types, and
`:lineage-status` is not an allowed attribute for any of them):

```
[:lineage/<entity-ident-slug> :entity-type :type/lineage-marker]
[:lineage/<entity-ident-slug> :entity <entity-ident>]
[:lineage/<entity-ident-slug> :status :provisional]
```

The ident is deterministic from the tracked entity's own ident:
`f":lineage/{entity_ident.lstrip(':').replace('/', '-')}"`.

Presence of this companion entity means the tracked entity's
`:introduced-by` is not yet confirmed; absence means authoritative —
matching phase 1's "unflagged = authoritative" migration default exactly,
since today's existing forward-only walk never creates a
`:type/lineage-marker` entity at all.

When Stream 1's correction sweep (2c) later confirms an entity's
`:introduced-by` is correct as-is, it retracts the companion entity's facts
entirely (no re-assertion — absence is the authoritative state). When the
sweep finds the provisional guess was *wrong* (e.g. a rename-spanning-the-gap
case, resolved in 2c), it retracts the companion entity's facts *and* the
tracked entity's incorrect `:introduced-by`, then asserts the tracked
entity's correct `:introduced-by` — still no new companion entity, since
the tracked entity is now authoritative.

2a's primitives (no caller yet). Each write is idempotent via the same
query-before-write guard `_watermark_update` established (mcp_server.py:
4867-4909) — never a blind unconditional transact, since re-transacting an
identical (entity, attribute, value) at a new valid_from creates a
duplicate live datom (see Revision note #2):

```python
def _lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=None) -> None:
    """Create the :type/lineage-marker companion entity for entity_ident,
    if one doesn't already exist. Queries for an existing marker first
    (mirrors _watermark_update) -- a marker already present is a no-op,
    never a duplicate write."""

def _lineage_confirm(db, entity_ident, index_con=None) -> None:
    """Retract the :type/lineage-marker companion entity's facts for
    entity_ident if present; no-op if absent, so callers (2c) can call
    this unconditionally without checking first."""

def _lineage_is_provisional(db, entity_ident) -> bool:
    """True iff a :type/lineage-marker companion entity currently exists
    for entity_ident."""
```

### `lineage-confirmed-through` watermark

A new entity `:ingestion/lineage-confirmed-through` with a `:hash`
attribute, structurally identical to `:ingestion/watermark` — same
retract-then-reassert-only-if-changed pattern as `_watermark_update`, just
a different ident:

```python
def _lineage_confirmed_through_query(db) -> Optional[str]:
    """Return the hash of the last commit through which lineage is fully
    confirmed, or None if nothing has been confirmed yet."""

def _lineage_confirmed_through_update(db, commit_hash, commit_ts_iso, index_con=None) -> None:
    """Record the last lineage-confirmed commit hash, mirroring
    _watermark_update's retract-only-if-changed pattern."""
```

**Migration seeding (fixes Revision note #3):** phase 1's already-merged
`_frontier_seed_from_watermark` (mcp_server.py) gains one addition: after
seeding the `[C0, W]` frontier interval as authoritative, it also calls
`_lineage_confirmed_through_update(db, watermark_hash, run_ts_iso,
index_con=index_con)`. That region's lineage genuinely *is* fully
confirmed — it was ingested by the original single-stream forward-only
authoritative walk, so it needs no separate confirmation sweep. With this
seeding, the watermark becomes a complete, standalone trust predicate with
no special-casing: an entity's lineage is trustworthy exactly when
`_lineage_is_provisional(entity)` is `False` **and** its `:introduced-by`
commit's position is `<=` the position of `_lineage_confirmed_through_query`'s
hash in the current linearization. (2a does not implement this composed
query itself — that's a consumer concern for phase 4's status reporting —
but 2a's migration seeding is what makes the predicate well-defined instead
of needing a special case for migrated graphs.)

### Candidate-diff persistence schema

One small entity per (commit, candidate entity) pair — mirroring the
codebase's existing style of minting entities with clean single-valued
attributes (e.g. `:type/commit` entities) rather than packing multiple
values into one delimited string:

```
[:candidate/<commit-hash[:12]>-<entity-ident-slug> :entity-type :type/candidate-diff]
[:candidate/<commit-hash[:12]>-<entity-ident-slug> :commit :commit/<commit-hash[:12]>]
[:candidate/<commit-hash[:12]>-<entity-ident-slug> :entity <entity-ident>]
[:candidate/<commit-hash[:12]>-<entity-ident-slug> :body-hash "<normalized-body-hash>"]
```

The ident is deterministic:
`f":candidate/{commit_hash[:12]}-{entity_ident.lstrip(':').replace('/', '-')}"`
— strip the entity ident's leading `:` and replace its `/` with `-`, then
prefix with the commit hash's existing 12-char short form (matching
`commit_ident = f":commit/{commit_hash[:12]}"`'s existing truncation
elsewhere in `_run_ingestion`).

A deterministic ident makes repeated writes target the *same* entity, but
(per Revision note #2) does **not** by itself make the write idempotent —
`_candidate_diff_persist` must query for an existing record at that ident
first, no-op if the persisted hash already matches, and retract-and-reassert
only if it's genuinely different (same guard as the provisional marker
above).

2a's primitives (2b writes, 2c reads/clears — no caller in this sub-phase):

```python
def _candidate_diff_persist(db, commit_hash, entity_ident, body_hash, commit_ts_iso, index_con=None) -> None:
    """Mint/update one candidate-diff record for (commit_hash, entity_ident).
    Queries for an existing record first (mirrors _watermark_update) --
    no-ops if the persisted body_hash already matches, retracts+reasserts
    only if it genuinely changed."""

def _candidate_diff_read(db, commit_hash, entity_ident) -> Optional[str]:
    """Return the persisted body_hash for (commit_hash, entity_ident), or
    None if no candidate record exists for that pair."""

def _candidate_diff_clear(db, commit_hash, entity_ident, index_con=None) -> None:
    """Retract the candidate record once consumed (2c calls this after
    confirming/rejecting), so these scratch facts don't accumulate
    unbounded across a full ingest."""
```

## Testing

Following `docs/testing-conventions.md` (real backend, no mocked
`MiniGrafDb`):

- **Provisional marker round-trip**: an entity with no companion marker
  entity reads as authoritative (`_lineage_is_provisional` returns
  `False`); mark it provisional, assert `True`; confirm it, assert `False`
  again; confirm an already-authoritative entity a second time (never
  marked, or already confirmed) and assert it's a genuine no-op — verified
  via a raw fact-count query on the `:type/lineage-marker` entity's facts,
  not just the derived boolean, per the row-collapsing lesson from phase
  1's Tasks 3/4.
- **Provisional marker idempotency**: call `_lineage_mark_provisional`
  twice for the same entity and assert exactly one live `:type/
  lineage-marker` entity/fact set exists afterward (raw count, not
  `_lineage_is_provisional`'s boolean) — this is the test that would have
  caught Revision note #2 if it had been run against the first draft's
  (incorrect) unconditional-transact implementation.
- **Audit safety test**: mark an entity provisional, run
  `handle_minigraf_audit()`, and assert the `:type/lineage-marker` entity
  (and, separately, a real `:type/function`/`:type/module` entity carrying
  ordinary schema-valid attributes alongside it) both survive unretracted —
  this directly verifies Revision note #1's fix, not just that the schema
  doesn't reject the write at transact time.
- **Watermark round-trip**: mirrors phase 1's `_watermark_update`/
  `_watermark_query` tests — persist, re-query, update again, confirm only
  one live `:hash` fact exists via a `(count ...)` query.
- **Candidate-diff round-trip + idempotency**: persist a candidate record,
  read it back, confirm the correct `(commit, entity)` pair resolves to the
  correct hash and a *different* `(commit, entity)` pair returns `None`;
  persist the same `(commit, entity)` pair again with the same hash and
  assert via raw count that no duplicate was created; persist again with a
  *different* hash and assert the read now returns the new hash with still
  only one live record; clear it; confirm a cleared record reads back as
  `None`, verified via both the accessor and a raw fact-count check that the
  underlying `:type/candidate-diff` entity's facts are actually gone.
- **Migration seeding test**: run phase 1's migration path (`_frontier_load`
  against a graph with only the old `:ingestion/watermark`) and assert
  `_lineage_confirmed_through_query` now returns `W` (the watermark hash),
  not `None` — this directly verifies Revision note #3's fix.
