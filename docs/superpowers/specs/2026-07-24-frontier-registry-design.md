# Frontier/Interval Registry + Shared-Gap Allocator — Design Spec

**Issue:** #222 (Phase 1 of 5)
**Date:** 2026-07-24

## Background

For large repos (ArangoDB `origin/4.0`: ~53k commits, multi-day ingest), today's
forward-only ingestion (`_run_ingestion` in `mcp_server.py`, walking a single
scalar watermark `:ingestion/watermark` from `C0` to `HEAD`) makes the most
valuable data — current structure and recent history, which the "where do I
work / fix this bug" navigation use case depends on (#219, #220) — land last,
after the entire historical bulk. #222 fixes this with a converging
multi-stream backfill: a forward-truth stream (unchanged authoritative walk)
running concurrently with a reverse-bulk-fill stream that provisionally
back-fills from `HEAD` downward, so recent history is visible almost
immediately while the forward stream still owns lineage correctness.

#222 is too large for one design/implementation cycle. It decomposes into five
phases:

1. **Frontier/interval registry + shared-gap allocator** (this spec) — the
   foundational data structure both streams claim work from and record
   progress into. Nothing else can run concurrently without this.
2. **Streams 1+2 converging** (forward-truth + reverse-bulk-fill) — the actual
   recent-first delivery, built on this phase's allocator, including the
   provisional→authoritative correction-sweep and migration from old
   single-watermark graphs.
3. **Stream 3 (tip-liveness)** — reverse tip-fill for an actively-advancing
   branch; a separable liveness concern per #222's own "Scope" section.
4. **Status/observability extension** — granular + aggregated
   `minigraf_ingest_status` view (visibility coverage vs. lineage-authority
   coverage, per-stream state), per #222's first comment.
5. **Hardening pass** — DAG diamonds, force-push/rebase detection, octopus
   merges, multiple roots, non-monotonic committer dates.

## Scope (Phase 1 only)

In scope:

- A fixed topological linearization of commits and a hash→position map,
  replacing the plain chronological order `_git_commits` uses today.
- An in-memory `FrontierAllocator` holding a set of disjoint
  `(lo_pos, hi_pos, tag)` intervals (`tag ∈ {authoritative, provisional}`),
  with `claim_low()`/`claim_high()` atomic operations and degenerate-case
  handling (gap already empty, single-commit repo, `lo == hi`).
- Graph persistence for the interval set (new `:type/ingest-interval`
  entities, following the existing `:ingestion/watermark` retract/reassert
  pattern) plus load-on-start reconstruction into the in-memory allocator.
- Migration: on first run against a graph that only has the old scalar
  `:ingestion/watermark`, seed one authoritative interval `[C0, W]` from it.

Explicitly deferred to later phases: Stream 2/3 walking logic themselves
(phase 2/3), status/observability changes (phase 4), DAG diamonds /
force-push / octopus-merge / multiple-roots handling (phase 5 — this phase
assumes a single linear branch with one root and no history rewrite).

## Design

### Linearization

`_git_commits` switches from `git log --reverse` to
`git log --topo-order --reverse` (still oldest→newest in the output list).
This is a real, if latent, bug fix: the issue requires strict topological
ordering ("order strictly by topo, never by timestamp — clock skew and
rebases make dates non-monotonic and would corrupt any date-based frontier
comparison"), and plain `git log` without `--topo-order` does not guarantee
parent-before-child in all cases, only `--topo-order` does.

The linearization is computed once per server-process run against current
HEAD, producing `linearization: List[str]` (commit hashes, oldest first) and
`hash_to_pos: Dict[str, int]` built via `enumerate(linearization)`. Position 0
is always `C0`; the last index is `Ht`'s position at the moment the
linearization was built. Both are held in memory only — never persisted
directly, since they're cheap to rebuild deterministically from `git log` on
every start, and persisting them would just be a cache (a stale one, at that,
if the branch has moved since).

### In-memory interval registry

`FrontierAllocator` wraps a sorted list of disjoint
`Interval(lo_pos: int, hi_pos: int, tag: Literal["authoritative", "provisional"])`
tuples covering the already-processed subset of `[0, len(linearization)-1]`.
Adjacent intervals of the *same* tag are merged into one on every claim (so
each side stays exactly one interval, growing monotonically); adjacent
intervals of *differing* tags are deliberately kept separate rather than
merged — once the gap closes, the low-anchored authoritative interval and the
high-anchored provisional interval become adjacent, and that boundary itself
is the lineage-authority frontier (what phase 4's status reporting and
phase 2's Stream 1 correction-sweep both key off), so it must stay
addressable, not collapsed away.

Two readouts are derived on demand, never stored separately:

- `gap_lo` = `(interval covering position 0).hi_pos + 1` if such an interval
  exists, else `0`.
- `gap_hi` = `(interval covering the last position).lo_pos - 1` if such an
  interval exists, else `len(linearization) - 1`.
- The gap is empty when `gap_lo > gap_hi`.

This directly implements the issue's "never watch a moving watermark"
principle: `gap_lo`/`gap_hi` are read-outs of the single interval list, not
values a stream reads from another stream and compares against — there is
exactly one source of truth, mutated atomically (see below).

### Graph persistence schema

Each maximal interval is one `:type/ingest-interval` entity:

```
[:ingestion/interval-<lo_hash>-<hi_hash> :entity-type :type/ingest-interval]
[:ingestion/interval-<lo_hash>-<hi_hash> :lo-hash "<lo_hash>"]
[:ingestion/interval-<lo_hash>-<hi_hash> :hi-hash "<hi_hash>"]
[:ingestion/interval-<lo_hash>-<hi_hash> :tag :authoritative|:provisional]
```

Persisted by commit hash, not position — positions are only meaningful
against one in-memory linearization snapshot, but hashes are the durable
identity that survives across restarts and (within this phase's
linear-history assumption) across the branch advancing.

On load: every `:type/ingest-interval` fact is read back, its hashes resolved
through the freshly-built `hash_to_pos` map, and the in-memory
`FrontierAllocator` is reconstructed from the resulting `(lo_pos, hi_pos,
tag)` set.

When an interval's bound moves (a stream extends it by one claimed commit),
only that entity's `:lo-hash` or `:hi-hash` is retracted+reasserted — same
cost and pattern as today's per-commit `:hash` update on
`:ingestion/watermark`, just scoped to whichever one bound actually moved,
not the whole registry.

### Migration from single-watermark graphs

On load, if zero `:type/ingest-interval` facts exist but `:ingestion/watermark`
does, synthesize one `[C0, W]` interval tagged `authoritative` (`lo-hash` =
`linearization[0]`, `hi-hash` = the watermark's hash) and persist it before
proceeding. This is a one-time write — once `:type/ingest-interval` facts
exist, this synthesis step is skipped entirely on subsequent loads. A graph
with neither watermark nor intervals (fresh/empty repo) skips migration and
starts with an empty `FrontierAllocator`.

### Allocator mechanics

```python
def claim_low(self) -> Optional[int]:
    """Forward stream claims the next unclaimed position from the low end."""
    if self.gap_lo > self.gap_hi:
        return None  # gap already empty — caller no-ops
    pos = self.gap_lo
    self._extend_or_create(pos, pos, tag="authoritative", from_low=True)
    return pos

def claim_high(self) -> Optional[int]:
    """Reverse stream claims the next unclaimed position from the high end."""
    if self.gap_lo > self.gap_hi:
        return None
    pos = self.gap_hi
    self._extend_or_create(pos, pos, tag="provisional", from_low=False)
    return pos
```

`_extend_or_create` either widens the existing interval touching `pos` from
the corresponding side, or creates a new one-position interval if none abuts
yet (only possible at the very first claim from that side). Each caller
re-checks `gap_lo > gap_hi` on every call — this is what makes each stream's
own claim loop terminate correctly, not a race-avoidance measure (see
atomicity below).

**Atomicity.** Both streams run as asyncio tasks in one process (confirmed
scope decision — no separate OS processes/workers for streams). `claim_low`/
`claim_high` are correct as long as they never contain an `await` — Python's
cooperative scheduler cannot preempt a coroutine mid-synchronous-function, so
a claim-and-mutate is atomic by construction, with no `asyncio.Lock` needed.
Each stream calls `claim_*()` synchronously right before submitting that
commit's extraction work to the shared `ProcessPoolExecutor`, never after an
`await` — so there is no window where two tasks both observe the same
unclaimed position.

**The `lo == hi` case** (one commit left in the gap): whichever stream calls
`claim_low`/`claim_high` first gets `pos`; that call's mutation immediately
closes the gap (`gap_lo` becomes `pos+1`, exceeding `gap_hi = pos`), so the
other stream's very next `claim_*()` observes `gap_lo > gap_hi` and returns
`None` — exactly-once, no special-cased branch.

**Degenerate cases handled in this phase:**

- Gap already empty at construction (`low_wm >= high_wm` on resume, or a
  repo small enough forward ingestion already covered it): both `claim_low`/
  `claim_high` return `None` immediately — a caller (Phase 2's Stream 2)
  never even starts its loop.
- Single-commit repo: linearization has length 1, `gap_lo == gap_hi == 0`;
  one claim closes it.
- Empty repo: linearization is empty; both claims return `None`.

**Persistence timing.** Matches today's per-commit cadence: after a claimed
commit's DB writes complete, the owning stream calls a
`_interval_persist(db, pos, tag, ...)` that retracts+reasserts only the moved
bound's fact — not the whole registry — mirroring `_watermark_update`'s
existing per-commit cost profile.

## Testing

Following `docs/testing-conventions.md` (real backend, no mocked
`MiniGrafDb`):

- **Pure allocator unit tests** (no DB/git involved): `FrontierAllocator`
  constructed directly from hand-built interval lists — claim_low/claim_high
  sequencing, gap-closing on `lo == hi`, gap-already-empty no-op,
  single-position repo.
- **Linearization tests**: a real small git fixture repo (matching existing
  ingestion test patterns in `test_mcp_server.py`) with a deliberately
  clock-skewed commit (`GIT_COMMITTER_DATE` set out of order) verifying
  `--topo-order` still yields parent-before-child, where plain chronological
  order would not.
- **Persistence round-trip**: file-backed `MiniGrafDb.open()` (Pattern 2,
  since this needs to survive a close/reopen cycle) — persist an interval,
  reopen, confirm `FrontierAllocator` reconstructs the identical
  `(lo_pos, hi_pos, tag)` set via the freshly-rebuilt `hash_to_pos` map.
- **Migration test**: seed a graph with only the old `:ingestion/watermark`
  fact (no `:type/ingest-interval`), run the load path, assert exactly one
  `[C0, W]` authoritative interval is synthesized and persisted, and that
  re-running the load path a second time doesn't duplicate it.
