# Semantic Retrieval for `memory_prepare_turn`

**Date:** 2026-06-04
**Status:** Approved

## Problem

`handle_memory_prepare_turn` currently retrieves context using two mechanisms:

1. Per-token `contains?` substring queries against the DB — weak lexical matching, misses multi-word concepts, sensitive to tokenization.
2. A broad fallback scan capped at 50 rows — essentially random when no token matches.

Neither produces relevantly ranked results. The context injected into the model is often noise.

## Goal

Replace both mechanisms with BM25-ranked retrieval over a cached in-process index, with a scoring boost for memory facts over git-ingested structural facts.

## Constraints

- In-process only — no external API calls, no network round-trips.
- Latency-sensitive: `prepare_hook` has a 5-second timeout.
- Index rebuild must be async; `prepare_turn` must never block on a rebuild.
- Serve stale index during rebuild; return empty context if no index exists yet.
- Both memory facts (decisions, preferences, constraints, dependencies) and git facts (commits, functions, modules, etc.) are indexed; memory facts rank higher.

## Architecture

### Components

**`FactIndex`** (new class in `mcp_server.py`)

Owns a single BM25 snapshot:
- `BM25Okapi` instance from `rank_bm25`
- Parallel list of raw fact rows `[e, a, v]` for result mapping
- Boolean array `is_memory` — one entry per fact, set at build time

**`IndexCache`** (module-level singleton in `mcp_server.py`)

Manages index lifecycle:
- `_current: Optional[FactIndex]` — the live index (may be stale)
- `_rebuilding: bool` — prevents concurrent rebuild threads
- `_lock: threading.Lock` — guards atomic swap of `_current`
- `invalidate()` — spawns background thread if not already rebuilding
- `_rebuild()` — fetches all current facts, builds new `FactIndex`, atomically swaps `_current`
- `get()` — returns `_current` (possibly `None`)

**Updated `handle_memory_prepare_turn`**

Drops all `contains?` queries and the broad scan fallback. Calls `IndexCache.get()`, scores via BM25 + memory boost, returns top-N formatted results.

### Dependency

`rank_bm25` — pure Python, ~10KB, no transitive deps. Added to `pyproject.toml` and installed by `install.py` alongside `minigraf` and `mcp`.

## Index Build

### Document Tokenization

Each fact row `[e, a, v]` is tokenized by splitting on non-alphanumeric characters:

```
":decision/use-redis" + ":description" + "use Redis for caching"
→ ["decision", "use", "redis", "description", "use", "redis", "for", "caching"]
```

Keyword punctuation (`:`, `/`, `-`) acts as a natural separator. No stop-word removal — BM25 IDF handles low-signal tokens.

### Memory Fact Detection

Checked once at build time. A fact is a **memory fact** if its entity ident starts with any of:
- `:decision/`
- `:preference/`
- `:constraint/`
- `:dependency/`

All other facts (`:commit/`, `:function/`, `:module/`, `:class/`, `:file/`, `:ingestion/`, etc.) are git facts.

### Temporal Scope

The index is built over **currently-valid facts only**, using `:valid-at "<now>"` at build time. Retracted facts linger until the next rebuild, which is triggered immediately after any retract via the invalidation hook — the stale window is bounded by async rebuild time (typically milliseconds).

## Scoring

At query time:
1. User message is tokenized identically to documents.
2. `BM25Okapi.get_scores(query_tokens)` produces a raw score per fact.
3. Memory facts have their score multiplied by `VULCAN_MEMORY_BOOST` (env var, default `2.0`).
4. Results are sorted by adjusted score descending.
5. Zero-score results are excluded.
6. Top-N results returned, where N = `VULCAN_PREPARE_SCAN_LIMIT` (env var, default `50`).

## Cache Invalidation

`IndexCache.invalidate()` is called:

1. **After `handle_vulcan_transact`** — immediately after a successful transact, before returning to the caller.
2. **After `handle_vulcan_retract`** — immediately after a successful retract, before returning to the caller.
3. **After `handle_vulcan_ingest_git`** — once at the end of the full ingest loop, not per-commit.

Concurrent invalidations while a rebuild is in progress are no-ops (`_rebuilding` flag). The next `invalidate()` after the rebuild completes will trigger a fresh rebuild.

### Rebuild Thread

```
_rebuild():
  try:
    _rebuilding = True
    facts = fetch all current facts from DB (:valid-at now)
    new_index = FactIndex(facts)
    with lock:
      _current = new_index
  except Exception:
    log to stderr, leave _current unchanged
  finally:
    _rebuilding = False
```

## Error Handling

- **Rebuild failure:** Logged to stderr, `_current` left unchanged (stale or `None`). `_rebuilding` cleared in `finally` so future invalidations can retry.
- **No index yet (`None`):** `handle_memory_prepare_turn` returns `""` immediately.
- **`rank_bm25` not installed:** Caught at module load; `IndexCache` methods become no-ops; `handle_memory_prepare_turn` falls back to current heuristic implementation. Graceful degradation, no server crash.

## Testing

New test classes in `tests/test_mcp_server.py`:

**`TestFactIndex`**
- Keyword ident tokenization splits on `:`, `/`, `-` correctly
- Memory fact detection: `:decision/x` → `True`, `:commit/x` → `False`
- Score boost: memory facts outscore git facts for the same query tokens
- Zero-score exclusion: query with no token overlap returns empty

**`TestIndexCache`**
- `get()` returns `None` before first build
- `invalidate()` triggers rebuild; `get()` returns index after rebuild completes
- Stale index served during rebuild (mock slow rebuild)
- Concurrent `invalidate()` calls do not spawn multiple threads

**`TestMemoryPrepareTurnBM25`**
- End-to-end: transact memory facts + git facts, call `handle_memory_prepare_turn`, verify memory facts appear before git facts in output
- Empty return when index is `None`
- Fallback to heuristic when `rank_bm25` unavailable (monkeypatch import)

All tests use the existing `temp_graph` fixture. `rank_bm25` is used directly (pure Python, no mocking needed).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VULCAN_PREPARE_SCAN_LIMIT` | `50` | Max results returned by prepare_turn |
| `VULCAN_MEMORY_BOOST` | `2.0` | Score multiplier for memory facts |
