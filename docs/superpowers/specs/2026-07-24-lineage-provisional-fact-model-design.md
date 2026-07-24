# Provisional/Authoritative Lineage Fact Model + Candidate-Diff Persistence — Design Spec

**Issue:** #222 (Phase 2, sub-phase 2a of 4)
**Date:** 2026-07-24 (revised three times same day after spec review — see "Revision" note)

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

A second review pass on that revision found one more High and one more
Medium issue, both confirmed real and fixed in this version:

5. **High** — the point-3 fix only fired *inside*
   `_frontier_seed_from_watermark`, which `_frontier_load` only calls when
   *both* frontier intervals are absent. A graph that already ran Phase 1
   standalone before Phase 2a lands (exactly this project's own situation —
   Phase 1 is merged and Phase 2a is not) already has
   `:ingestion/frontier-low`, so that migration branch never runs for it and
   `lineage-confirmed-through` stays unset forever, breaking the "complete
   standalone predicate" property for precisely the graphs most likely to
   need it. **Fixed** by decoupling the seeding from
   `_frontier_seed_from_watermark` entirely: a new, self-contained
   `_lineage_confirmed_through_migrate` catches up from
   `:ingestion/frontier-low`'s *current* `:hi-hash` whenever
   `lineage-confirmed-through` is unset — regardless of whether
   `frontier-low` was just created this call or already existed from an
   earlier run — called unconditionally from `_frontier_load` (see below).
6. **Medium** — the audit-safety test only covered `:type/lineage-marker`,
   not `:type/candidate-diff`, even though the schema/audit argument applies
   to both equally. **Fixed** by adding a symmetric candidate-diff audit
   test (see Testing).

A third review pass found two more Medium issues, both confirmed real and
fixed in this version:

7. **Medium** — the spec established that unregistered scratch types are
   safe from `minigraf_audit` (point 4/#4 above), but didn't say anything
   about `handle_minigraf_transact` — a *different* code path with its own,
   narrower validation gate. Verified: `handle_minigraf_transact`
   (mcp_server.py:3623) calls `_validate_facts` on every string-valued
   triple (mcp_server.py:3638-3642), which *would* reject
   `[:candidate/... :body-hash "..."]` outright ("unknown entity type
   candidate") since `candidate-diff` isn't in `MINIGRAF_SCHEMA` — unlike
   `minigraf_audit`, this gate runs at write time, not just on a later
   sweep. The internal `_transact` helper (mcp_server.py:3532) has no such
   check. **Fixed** by stating explicitly (see below) that every function
   this spec defines must call the internal `_transact`/`_retract` helpers
   directly, the same way `_watermark_update`/`_frontier_persist_claim`
   already do — never the public `handle_minigraf_transact`/
   `handle_minigraf_retract` MCP tool handlers.
8. **Medium** — the migration idempotency test only proved "doesn't
   duplicate the initial seeded value," not the spec's actual claim
   ("later phases' own sweep updates are never clobbered back to a stale
   value"). A test that re-seeds the *same* value twice cannot distinguish
   a correct implementation from a buggy one that ignores its own guard and
   unconditionally re-derives from `frontier-low`'s `:hi-hash` every call —
   both would show "still one value, unchanged," since re-deriving the same
   stale boundary twice looks identical to leaving an already-correct value
   alone. **Fixed** by adding a test that first advances
   `lineage-confirmed-through` *past* `frontier-low`'s current `:hi-hash`
   (simulating phase 2c's real sweep), then calls `_frontier_load` again
   and asserts the *advanced* value survives, not a clobber back to
   `frontier-low`'s boundary.

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
- A small addition to phase 1's already-merged `_frontier_load` to
  unconditionally call a new self-contained catch-up function seeding
  `lineage-confirmed-through` from `:ingestion/frontier-low`'s current
  `:hi-hash` whenever the watermark is unset (see Revision notes #3 and
  #5) — the only change to existing code in this sub-phase.

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

**Implementation constraint (fixes Revision note #7): internal `_transact`/
`_retract` only, never the public handlers.** Being unregistered protects
these entity types from `minigraf_audit`'s later sweep, but *not* from
`handle_minigraf_transact`'s (mcp_server.py:3623) write-time validation gate
— that handler calls `_validate_facts` on every string-valued triple
(mcp_server.py:3638-3642) and would reject `[:candidate/... :body-hash
"..."]` outright as an unknown entity type, since `candidate-diff` isn't in
`MINIGRAF_SCHEMA` either. The internal `_transact`/`_retract` helpers
(mcp_server.py:3532/3567) have no such check. Every function this spec
defines — `_lineage_mark_provisional`, `_lineage_confirm`,
`_candidate_diff_persist`, `_candidate_diff_clear`, and the watermark
functions below — **must** call `_transact`/`_retract` directly, the same
way `_watermark_update`/`_frontier_persist_claim` already do, never route
through `handle_minigraf_transact`/`handle_minigraf_retract`.

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

**Migration seeding (fixes Revision notes #3 and #5):** rather than hooking
into `_frontier_seed_from_watermark` (which only runs when *neither*
frontier interval exists yet — missing the case where `:ingestion/
frontier-low` already exists from an earlier Phase-1-only run, exactly this
project's own current situation), 2a adds a wholly new, self-contained
catch-up function:

```python
def _lineage_confirmed_through_migrate(db, run_ts_iso, index_con=None) -> None:
    """One-time catch-up: if :ingestion/frontier-low exists (this graph has
    an authoritative region, whether freshly migrated by
    _frontier_seed_from_watermark just now or already established by an
    earlier Phase-1-only run) but :ingestion/lineage-confirmed-through is
    unset, seed the watermark from frontier-low's *current* :hi-hash --
    that whole region was ingested by the original single-stream
    forward-only authoritative walk, so it is already fully
    lineage-confirmed. No-op if frontier-low doesn't exist yet, or
    lineage-confirmed-through is already set (so later phases' own sweep
    updates are never clobbered back to a stale value)."""
    if _lineage_confirmed_through_query(db) is not None:
        return
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is None:
        return
    _, hi_hash = low_bounds
    _lineage_confirmed_through_update(db, hi_hash, run_ts_iso, index_con=index_con)
```

`_frontier_load` (phase 1, already merged) gains one addition: after its
existing migration block runs (whether or not it actually did anything —
the new function's own guards make it safe to call unconditionally), it
calls `_lineage_confirmed_through_migrate(db, run_ts_iso, index_con=index_con)`.
This covers both cases uniformly: a graph migrating for the first time
*and* a graph that already migrated under Phase-1-only code before Phase 2a
existed — in both, `frontier-low` already has *some* `:hi-hash` by the time
this call happens, and that is always the correct trust boundary to seed
from, regardless of how `frontier-low` got there. Once seeded, this
watermark becomes a complete, standalone trust predicate with no
special-casing: an entity's lineage is trustworthy exactly when
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
- **Audit safety test (lineage marker)**: mark an entity provisional, run
  `handle_minigraf_audit()`, and assert the `:type/lineage-marker` entity
  (and, separately, a real `:type/function`/`:type/module` entity carrying
  ordinary schema-valid attributes alongside it) both survive unretracted —
  this directly verifies Revision note #1's fix, not just that the schema
  doesn't reject the write at transact time.
- **Audit safety test (candidate-diff)**: persist a candidate-diff record,
  run `handle_minigraf_audit()`, and assert the `:type/candidate-diff`
  entity's facts survive unretracted — the schema/audit argument in this
  spec applies equally to both new entity types (Revision note #6), and
  this test is what proves it for the one that had no coverage in the
  prior revision.
- **Watermark round-trip**: mirrors phase 1's `_watermark_update`/
  `_watermark_query` tests — persist, re-query, update again, confirm only
  one live `:hash` fact exists via a `(count ...)` query. Unlike the
  lineage-marker/candidate-diff entities, `:ingestion/lineage-confirmed-through`
  uses entity-type `:type/ingestion` — the *same registered, audited* type
  `:ingestion/watermark` already uses (`ingestion` is in `MINIGRAF_SCHEMA`,
  requiring `:description` and allowing `:hash` among its optional attrs).
  This test must additionally assert the entity has the expected constant
  attrs (`:entity-type`, `:ident`, `:description`) — matching
  `_watermark_update`'s own constants block exactly — and/or survives a
  `handle_minigraf_audit()` run unretracted, since (unlike the deliberately
  unregistered types above) this one *is* subject to schema validation and
  must genuinely conform, not merely avoid audit by being invisible to it.
- **Candidate-diff round-trip + idempotency**: persist a candidate record,
  read it back, confirm the correct `(commit, entity)` pair resolves to the
  correct hash and a *different* `(commit, entity)` pair returns `None`;
  persist the same `(commit, entity)` pair again with the same hash and
  assert via raw count that no duplicate was created; persist again with a
  *different* hash and assert the read now returns the new hash with still
  only one live record; clear it; confirm a cleared record reads back as
  `None`, verified via both the accessor and a raw fact-count check that the
  underlying `:type/candidate-diff` entity's facts are actually gone.
- **Migration seeding test, fresh migration**: run phase 1's migration path
  (`_frontier_load` against a graph with only the old `:ingestion/
  watermark`) and assert `_lineage_confirmed_through_query` now returns `W`
  (the watermark hash), not `None`.
- **Migration seeding test, already-migrated graph**: seed a graph with
  `:ingestion/frontier-low` already present (e.g. by calling `_frontier_load`
  once already, simulating an earlier Phase-1-only run) and no
  `lineage-confirmed-through` fact, then call `_frontier_load` again and
  assert `_lineage_confirmed_through_query` now returns frontier-low's
  `:hi-hash` — this is the test that would have caught Revision note #5:
  it fails against the prior revision's `_frontier_seed_from_watermark`-only
  fix, since that branch never runs when `frontier-low` already exists.
- **Migration seeding idempotency (same value)**: call `_frontier_load` a
  third time after the watermark is already seeded and assert (via raw
  count) it's still exactly one live `:hash` fact, unchanged.
- **Migration seeding does not clobber an advanced value**: after the
  watermark is seeded once, directly advance
  `lineage-confirmed-through` to a hash *past* `frontier-low`'s current
  `:hi-hash` (simulating phase 2c's real sweep having progressed further
  than the original migration boundary), then call `_frontier_load` again
  and assert `_lineage_confirmed_through_query` still returns the
  *advanced* hash, not a clobber back to `frontier-low`'s boundary — the
  "same value" idempotency test above cannot distinguish a correct
  implementation from one that ignores its own guard and unconditionally
  re-derives from `frontier-low` every call, since re-deriving an unchanged
  value looks identical to leaving it alone; only advancing past it first
  exposes the difference.
