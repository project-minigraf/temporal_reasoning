# Temporal Reasoning — AI Coding Agent Memory

Persistent bi-temporal graph memory for AI coding agents.

**This file holds standing rules only. The reasoning, measurements and corrected
mistakes behind each one are in `docs/design-notes.md` — grep it for the issue
number (e.g. `#326`) before changing anything a rule here touches.**

## Quick Start

```bash
python install.py --harness claude-code   # or opencode / codex; --harness is required
```

Everything goes through the MCP tools — there is no wrapper module:

```
minigraf_transact(facts='[[:decision/cache :description "use Redis"]]', reason="Caching strategy")
minigraf_query(datalog='[:find ?d :where [?e :description ?d]]')
minigraf_query(datalog="[:find ?x :as-of 5 :where [?e :attr ?x]]")
minigraf_query(datalog="[:find (count ?e) :where [?e :description ?d]]")
```

`from minigraf import query, transact` has never worked (no `minigraf.py` ever
existed). The `minigraf` package exports `MiniGrafDb`, `MiniGrafError`,
`minigraf_ffi`; to read a graph outside the server, open a `MiniGrafDb` directly,
subject to the single-handle invariant.

## Key Files

- `mcp_server.py` — persistent MCP server, the only runtime interface to the graph
- `fact_index.py` — SQLite FTS5 fact index: retrieval, and the graph's only independent witness (#302)
- `frontier_registry.py` — per-position claim registry for the two ingestion streams
- `hook_spool.py` — hook-side spool used while ingestion holds the graph (#379)
- `SKILL.md` — skill doc; every example is an MCP call checked against `mcp_server._TOOLS` by `tests/test_skill_doc.py`
- `skill.json` + `tools/*.json` — generated from `mcp_server._TOOLS`, guarded by `tests/test_tool_schemas.py`
- `install.py` — setup script (weekly updates); mirrors `pyproject.toml`, which is canonical
- `docs/testing-conventions.md` — real-backend-only test conventions
- `hooks/claude-code.json` — MCP + auto-memory hook config
- `evals/at_scale/` — at-scale ingestion benchmark, gates, and one-off probes (`results/` are analysis artifacts)

## Python & Dependencies

- **Python 3.10–3.14, `requires-python` unbounded above.** CI matrix
  (`pytest.yml`), classifiers and floor agree by construction; add a new
  interpreter as a matrix row + classifier, never a cap (cap only on a recorded
  failure). `install.py`'s `check_python_version` mirrors the floor.
- **minigraf `>=2.0.2,<3.0.0`.** The upper cap is deliberate — a major bump is a
  decision, not a resolver outcome (#286). `mcp<2.0.0` until the handler-API
  migration.
- **PyPI metadata is stamped at release.** A dependency bound change protects
  only developers until a release is cut.
- **`[tool.setuptools] py-modules` is hand-maintained** (flat layout). A new
  top-level module imported by `mcp_server.py` must be added;
  `tests/test_packaging.py` checks it.
- **`serverInfo.version`**: `importlib.metadata` → `.claude-plugin/plugin.json`
  → `unknown`; `0.0.0` (dev placeholder) counts as absent. Verify changes against
  a built, stamped wheel.
- Lint (`pylint.yml`) is ruff, non-blocking, no version matrix.
- Always use `.venv/bin/python`.

## Test Environment Traps

- `.claude/settings.local.json` exports `MINIGRAF_NO_AUTO_INGEST=1` into every
  session here. `tests/conftest.py` scrubs every `MINIGRAF_*` var before each
  test; a test that needs one sets it with `monkeypatch.setenv`.
- **Module-level constants read env at import** and the scrub cannot reach them:
  `_OWNER_HINT_TTL`, `_MAX_MATCH_POOL_SIZE`, `_MAX_FACT_VALUE_LENGTH`,
  `_SWEEP_YIELD_*`. Tests patch the CONSTANT, not the variable. Same for kill
  switches `_LINEAGE_CACHE_ENABLED`, `_SWEEP_PREFETCH`.
- The suite runs the lineage cache in verify mode (`_LINEAGE_CACHE_VERIFY`).
- Blocking `time.sleep` on the event loop is forbidden (#99);
  `_forbid_blocking_sleep_on_event_loop` enforces it. Use `asyncio.sleep`.
- Regression tests must be ablation-proven: watch them fail without the fix.

## Storage & Configuration

| Variable | Meaning |
|---|---|
| `MINIGRAF_GRAPH_PATH` | graph file (default `memory.graph` in cwd) |
| `MINIGRAF_INDEX_PATH` | fact index (default `<graph>.fts.sqlite3`) |
| `MINIGRAF_INGEST_CHECKPOINT_DUTY` | fraction of wall clock for `db.checkpoint()` (default 0.05; O(graph size), #241) |
| `MINIGRAF_INGEST_TRACE_PATH` | per-commit JSONL cost trace (unset = none; not a commit census). Read with `probe_per_commit_cost.py` |
| `MINIGRAF_OWNER_HINT_TTL` | `<graph>.owner` hint freshness (default 30 s) |

The fact index is bi-temporal (historical facts included, labelled with validity).

## Public Write Semantics

- **Booleans are indexed** in EDN spelling `true`/`false` (#303).
- **`nil` is refused** by `handle_minigraf_transact` (`_has_nil_valued_triple`),
  all-or-nothing, before the graph is touched (#306). Ingestion's internal
  `_transact` is deliberately unguarded.
- **`valid_at`** (arg or `; valid-at:` line) is STRICT: unparseable, future or
  conflicting → refused, never defaulted (#375).
- **`minigraf_retract` CLOSES by default**; only `mode="correct"` does minigraf's
  "was wrong" retract (#380). Close refuses a non-live fact and `valid_at <= valid-from`.
- **minigraf `retract` removes EVERY window of an `[e a v]`.** Both close paths
  (`_retract_closing_at`, `_ingest_close`) read all windows first and restore the
  non-live ones (#383), writing the closing window last. When the cap moves to
  minigraf 3.x: redo both as one `transact` with `:valid-to` and delete the
  restore step (minigraf#435).

## minigraf Behaviours to Respect

- **Single-handle invariant**: at most one live `MiniGrafDb` per process, enforced
  through `_DbLeaseManager` (refcount authoritative; open 0→1, drop 1→0). Read the
  comment above `_db_native_lock` before touching DB lifecycle. Never reuse a
  handle via weakref (#253).
- **minigraf#287 (open)**: facts sharing `(entity, attribute, valid_from)` in one
  transact collapse to the last. `:contains`, `:depends-on`, `:parent` are
  transacted ONE PER CALL — never batch those loops. Retracts collapse the same
  way; batch only across distinct `(entity, attribute)` pairs.
- **Dropping a handle runs a full O(graph) checkpoint**; minigraf's 1000-WAL-entry
  auto-checkpoint counts CALLS. Fewer write calls = fewer checkpoints.
- **Locking is the kernel's `flock` on `.graph`** — no PID sidecar. `<graph>.owner`
  is our advisory hint (heartbeat mtime, never PID liveness); correctness rests on
  the kernel lock.
- `(count ?e)` counts ROWS; use `count-distinct` for censuses.
- minigraf Rust source is at `~/Work/AMC/Minigraf/minigraf` — check it before
  trusting a comment about runtime behaviour.

## Graph Format & Rebuild Policy

- **No migration, by design.** `GRAPH_FORMAT_VERSION` (currently 2) is stamped as
  `:ingestion/format-version`; ingestion refuses any other version, including an
  absent stamp. Bump it when stored facts become unreadable by current code (e.g.
  the ident rule in `_canonical_ident`, the first-parent timeline).
- **Re-running ingestion repairs nothing.** An affected graph is REBUILT into a
  fresh graph path. Never propose a migration or in-place repair.
- Ingestion first runs `_graph_index_cross_check` (EAVT vs AEVT presence, #336) —
  it must precede `_graph_format_version_verify`. Refuses only when exactly one
  side is empty.

## Ingestion Invariants

**Timeline.** "Live at t" = in the branch tip's tree at t: the first-parent
chain (#384). Every position diffs against its first parent; merges tree-to-tree.
Merged code is attributed to the merge; side commits are `:type/commit` entities
with `:merged-in`, written by `_write_side_commits` before the claim persists.
Readers counting commits must distinguish mainline from side commits.

**A commit's write is a sequence of transacts, not atomic.** Resume must tolerate
every kill point:
- live `:ident` + zero `:introduced-by` = torn entity → `_reverse_apply` treats it
  as new (#313); zero values + NOT live in Stage B = rebirth (#349).
- a provisional guess and its lineage marker go down in ONE transact (#390).
- no watermark AND no frontier-low ⇒ fresh preload, not unbounded (#391).
- `_ingest_close` retracts in ONE call (#392); falls back per-triple only when a
  `(entity, attribute)` pair repeats.
- Stage B resumed mid-region calls `_forward_walk_state_reanchor` (#393).
- The three forward "contiguous from C0" markers (`:ingestion/watermark`,
  frontier-low, `:ingestion/lineage-confirmed-through`) move together, in one
  retract + one transact (#342, #377).
- Kill-test harnesses must kill the **process group**, and measure over many
  interrupts, never one.

**Frontier.** (#325/#326/#329/#342)
- Completion witness = membership in a persisted interval, never the commit
  entity's presence. Failures withhold BOOKKEEPING, never WORK: reverse uses a
  per-interval floor (`rev_claim_floor`, merges carry absorbed floors), forward a
  scalar ceiling (`fwd_claim_ceiling`). Don't "optimize" into skipping work.
- Retaining any interval requires: both bounds resolve, `lo <= hi`, stored
  `:pos-count == hi - lo + 1`. No `:pos-count` ⇒ not retained.
- `claim_low()` is contiguity-bound (adjacent to the authoritative edge only).
- Missing base is promoted on disk (`_frontier_promote_base_if_missing`) before
  load-time coalescing; Stage B declines while the provisional side is fragmented.
- The end-of-walk flush refuses on `lo_pos > floor or len(sources) > 1`.
- #326's skip fast path is vestigial since #325; a skipped position is still retired.
- Done = `visibility.complete and lineage.complete`, never `status: complete` alone.

**Leases & hooks.** (#347, #366, #379)
- `_run_ingestion` holds one extended lease for the whole run; inner leases join.
  Hooks (`use_hook_lease_deadline`, 5 s) SPOOL to `<graph>.spool/` while ingestion
  owns the graph; ingestion drains at window boundaries. The server process never
  spools or drains. Don't lengthen any other holder's lease without an escape hatch.
- Every lease that writes index rows commits `index_con` before release — use
  `_db_lease_async_committing_index`. Window boundaries fall only between fully
  swept commits.

**`_forward_apply` / `_reverse_apply`.** (#346)
- Every defaulted parameter is KEYWORD-ONLY; dispatch via `functools.partial`.
  New parameters go after the `*`; test spies read flags from `kwargs`.
- Call order: `_fwd_apply_renamed_head` → capture `previous_idents` →
  `_fwd_reconcile_and_build` (indivisible: its `entity_valid_from.pop` is what makes
  the build treat the ident as new) → `_fwd_close_removed_children` →
  `_fwd_diff_dependencies`; after the loop, `_fwd_apply_renamed_pairs` before
  `_fwd_close_renamed_old_paths`, then `_fwd_apply_gitlinks`.
- One file defining one name twice yields the FIRST entry (`_first_entry_per_ident`, #351).

**Lineage cache** (#239): write-through lives only in `_transact`/`_retract` — a
new graph writer that bypasses them breaks it silently. `_db_native_lock` is an
RLock held across query+store. Graph stamp is taken BEFORE the drop. Active only
inside `_run_ingestion`.

**Other standing constraints.**
- **#232 accepted residual**: eight sites classify triples by substring
  (`":contains" in t`). Safe only while code-entity string values contain no `:`.
  Before adding any free-text attribute to a code entity (or ingesting paths with
  `:`), switch them to an anchored attribute-slot match first.
- `_lineage_marker_ident` is injective only over `_code_ident` output (one `/`,
  hyphen-free type prefix: module/function/class/variable/field).
- `#369`'s `guess_unchanged` map is run-scoped, never persisted; updated AFTER the
  retroactive loop. `#372`'s `_SweepPrefetch.discard()` runs in a `finally`.
- Ident collisions (#263/#267): three guards answering different questions;
  `probe_ident_collision_census.py` is FROZEN at the pre-#263 rule — never re-point
  it. The new-history census has deliberately no `--since`.
- Unregistered internal types (`:type/ingest-interval`, `:type/completed-region`)
  stay out of `MINIGRAF_SCHEMA` and are written via internal `_transact`;
  `handle_minigraf_audit` retracts attributes outside registered types.

## At-Scale Gates (zero tolerance)

Each was measured clean before wiring; each ships its own denominator, and a zero
denominator means "proved nothing" (not failed). An absent key in old metrics stays clean.

| Gate | Catches |
|---|---|
| `fact_audit` `divergence` | graph vs index disagreement (#302) |
| `introduced_by_duplicates` | entity with ≥2 `:introduced-by` (#287) — separate from divergence |
| `entities_without_introduced_by` | torn entities (#316); excludes `:type/external-dependency`; liveness = `:ident` + `:entity-type` fact |
| `commit_census` | lost/never-walked commits (#317): first-parent `rev-list` vs walk vs graph; ref is the resolved branch, never `HEAD` |
| `orphaned_commits` | force-push orphans; gated only when `:ingestion/branch` matches (read BEFORE write) |
| `stderr_capture` `skipped_commits`, `error_signals` | logged failures |

- Never add `:ingestion/branch` to `_graph_has_ingestion_state`.
- A wrong #326 skip is caught by no gate.
- `probe_resume_census.py` gates on `repo_vs_graph == 0`, not `walk_vs_graph`.

## Measurement Discipline

- A/B runs interleaved (A B A B) in one batch; absolute numbers are not comparable
  across batches.
- `probe_forward_apply_write_parity.py` requires `PYTHONHASHSEED=0` from the command line.
- Don't edit files a background run imports.
- An issue's proposed mechanism is a hypothesis — instrument before implementing.

## Claude Code Plugin Publishing

`install.py` builds a stub at `~/.claude/plugins/stubs/temporal-reasoning-local/`
(only `.claude-plugin/` and `skills/`), because Claude Code's copier fails
silently on the repo's `.venv/`. Five files it must get right:

1. stub `.claude-plugin/marketplace.json` — needs `owner`; plugin `source: "./"`
2. stub `.claude-plugin/plugin.json` — identity and version
3. `~/.claude/settings.json` — `enabledPlugins` + `extraKnownMarketplaces` → stub
4. `~/.claude/plugins/installed_plugins.json` — `installPath` → versioned cache dir
5. `~/.claude/plugins/known_marketplaces.json` — **authoritative**; `source.path`
   and `installLocation` → stub

Canonical version: `.claude-plugin/plugin.json` (`PLUGIN_VERSION`). The read has
no fallback and exits on failure — a guessed version would delete the working
cache. Diagnose with `claude plugin list` / `claude plugin validate <stub-dir>`;
"not found in marketplace" means marketplace.json failed validation. Set
`CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` for offline resilience. Plugins
are copied to cache, so `../` paths break — use symlinks.
