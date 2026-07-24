# Provisional/Authoritative Lineage Fact Model + Candidate-Diff Persistence — Design Spec

**Issue:** #222 (Phase 2, sub-phase 2a of 4)
**Date:** 2026-07-24

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

## Scope (2a only)

In scope:

- A per-entity provisional marker for lineage (`:introduced-by`), following
  phase 1's migration precedent: absent = authoritative, so every entity
  ingested by today's existing forward-only walk (which never writes this
  marker) reads as authoritative with zero migration required.
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

Explicitly deferred: the actual reverse-walk diffing logic that decides
what counts as a "candidate" (2b), the rename-spanning-the-gap resolution
and confirm/reject replay logic (2c), and the concurrency wiring (2d). 2a
provides only the read/write primitives those will call.

## Design

### Provisional marker

A companion fact per entity, written only by Stream 2 (2b) alongside its
`:introduced-by`:

```
[<entity-ident> :lineage-status :provisional]
```

Absence of this fact means authoritative — matching phase 1's "unflagged =
authoritative" migration default exactly. When Stream 1's correction sweep
(2c) later confirms an entity's `:introduced-by` is correct as-is, it
retracts `:lineage-status :provisional` (no re-assertion — absence is the
authoritative state). When the sweep finds the provisional guess was
*wrong* (e.g. a rename-spanning-the-gap case, resolved in 2c), it retracts
both `:lineage-status :provisional` and the incorrect `:introduced-by`,
then asserts the correct `:introduced-by` — still no new marker, since the
entity is now authoritative.

2a's primitives (no caller yet):

```python
def _lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=None) -> None:
    """Assert [entity_ident :lineage-status :provisional]."""

def _lineage_confirm(db, entity_ident, index_con=None) -> None:
    """Retract :lineage-status :provisional if present; no-op if absent,
    so callers (2c) can call this unconditionally without checking first --
    same idempotent-by-design style as _frontier_seed_from_watermark's guard."""

def _lineage_is_provisional(db, entity_ident) -> bool:
    """Plain existence query for the marker."""
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

Absent means "nothing confirmed past `C0` by this watermark" — correct
both for pre-phase-2 graphs and, notably, for the region phase 1's
migration already seeded as `[C0, W]` authoritative: that region was never
provisional in the first place (no `:lineage-status` facts were ever
written for it), so it needs no confirmation sweep, and this watermark
starting unset for it does not imply that region is untrustworthy — it
implies "the confirmation *sweep* hasn't started," which is a distinct
question from "is this region's lineage authoritative," already answered by
the per-entity marker's absence. Sub-phase 2a's own tests must cover this
distinction explicitly (see Testing).

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

The ident is deterministic: `f":candidate/{commit_hash[:12]}-{entity_ident.lstrip(':').replace('/', '-')}"` — strip the entity ident's leading `:` and replace its `/` with `-`, then prefix with the commit hash's existing 12-char short form (matching `commit_ident = f":commit/{commit_hash[:12]}"`'s existing truncation elsewhere in `_run_ingestion`). No separate ID-generation scheme needed, and naturally idempotent if a caller ever revisits the same (commit, entity) pair.

2a's primitives (2b writes, 2c reads/clears — no caller in this sub-phase):

```python
def _candidate_diff_persist(db, commit_hash, entity_ident, body_hash, commit_ts_iso, index_con=None) -> None:
    """Mint/write one candidate-diff record for (commit_hash, entity_ident)."""

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

- **Provisional marker round-trip**: an entity with no marker reads as
  authoritative (`_lineage_is_provisional` returns `False`); mark it
  provisional, assert `True`; confirm it, assert `False` again; confirm an
  already-authoritative entity a second time (never marked, or already
  confirmed) and assert it's a genuine no-op — verified via a raw
  fact-count query on `:lineage-status`, not just the derived boolean, per
  the row-collapsing lesson from phase 1's Tasks 3/4.
- **Watermark round-trip**: mirrors phase 1's `_watermark_update`/
  `_watermark_query` tests — persist, re-query, update again, confirm only
  one live `:hash` fact exists via a `(count ...)` query.
- **Candidate-diff round-trip**: persist a candidate record, read it back,
  confirm the correct `(commit, entity)` pair resolves to the correct hash
  and a *different* `(commit, entity)` pair returns `None`; clear it;
  confirm a cleared record reads back as `None`, verified via both the
  accessor and a raw fact-count check that the underlying
  `:type/candidate-diff` entity's facts are actually gone.
- **Migration interaction test**: after phase 1's
  `_frontier_seed_from_watermark` migration seeds `[C0, W]` as authoritative
  with zero provisional facts, confirm `_lineage_confirmed_through_query`
  still correctly reads as unset (`None`) for that region, and that this is
  understood as "sweep hasn't run," not "region untrustworthy" (already
  answered by the per-entity marker's absence, not this watermark).
