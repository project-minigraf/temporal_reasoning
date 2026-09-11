# Run-start EAVT/AEVT cross-check (#336, partial)

Status: design approved 2026-09-11. Covers ONE part of #336 — its second
suggestion ("cross-check the two indexes at run start"), plus its fourth (the
recovery docs). The per-file batched marker probe folds into #239; the
watermark-singleton item waits on project-minigraf/minigraf#323. The PR says
"Part of #336" and carries no closing keyword.

## Problem

project-minigraf/minigraf#370: after a kill mid-save, a graph can reopen with
its EAVT index missing most entities' entries while AEVT still holds every
fact. It opens without error, answers every attribute-driven scan and count
correctly, and returns `[]` for every entity-bound lookup. No rebuild path
exists — `checkpoint()` streams the existing indexes forward, hole included.

#336 reported what that costs `_lineage_is_provisional`. The per-ident marker
probe is only one of >=14 entity-bound point-query sites in `mcp_server.py`
(`[{ident} :attr ?v]`): frontier `:lo-hash`/`:hi-hash`/`:pos-count`,
`:introduced-by`, `:ident` liveness, the three watermarks, the format-version
stamp. Every one misreads on a damaged EAVT, so no per-site fix restores
correctness. Only refusing the whole run covers the class.

### The misread that makes a mature graph look new

Measured on a copy of this repo's own 179 MB ingested graph, damaged as in
"Constructing the damage" below:

```
before {'stamp': 1, 'watermark': '32cab239…', 'low': (…), 'high': (…), 'has_state': True}
after  {'stamp': None, 'watermark': None, 'low': None, 'high': None, 'has_state': False}
_graph_format_version_verify: PASSED (treated as fresh)
_count_commit_entities (AEVT): 863
```

`_graph_format_version_read` and `_graph_has_ingestion_state` are both
entity-bound reads. On a damaged graph the stamp, the watermark and both
frontiers read as absent, so `_graph_format_version_verify` takes its
"genuinely new" branch, `_graph_format_version_stamp_if_new` would stamp it,
and the run would walk the whole history again over a graph that already
holds 863 commits. **The cross-check must therefore run before
`_graph_format_version_stamp_if_new`**, the run's first write.

It runs before `_graph_format_version_verify` too, for the diagnosis rather
than the refusal. Damage can be partial: if the stamp entity lost its EAVT
entries while the watermark kept its own, the stamp reads absent,
`_graph_has_ingestion_state` reads True, and the format check raises
`GraphFormatVersionError` — "this graph predates #263". Both refuse and both
recoveries are a fresh graph path, but the second sends the reader after an
ident-rule problem the graph does not have.

## How minigraf routes a query (source-verified, minigraf v2.0.0-1-gccdc85e)

`executor.rs` `filter_facts_for_query` → `selective_fact_fetch` (no `:as-of`,
no rules, at most 4 distinct lookups):

* a pattern with a literal entity (`#uuid` or a keyword, which is
  `uuid5(NAMESPACE_OID, ident)`) → `FactStorage::get_facts_by_entity` → **EAVT**
  range scan (pending BTreeMap + on-disk B+tree);
* otherwise a pattern with a bound attribute → `get_facts_by_attribute` →
  **AEVT**;
* anything else → `get_all_facts` → `read_all_from_pages`, the fact pages
  themselves. This is the only index-free path, and it materializes every fact
  in RAM — prohibitive at 1.6M entities, so it is not used here.

So `[:find ?e ?t :where [?e :entity-type ?t]]` is an AEVT read and
`[:find ?t :where [#uuid "<e>" :entity-type ?t]]` is an EAVT read of the same
facts. Comparing them is an exact two-index comparison **of presence** — but
not of which value each index returns when an entity holds several
same-transaction `:entity-type` values (amendment, 2026-09-11, measured; see
the same-transaction dedup amendment under step 3).

## Design

### `_graph_index_cross_check(db, sample_size=512, rng=None) -> dict`

1. **Population.** One AEVT query: `[:find ?e ?t :where [?e :entity-type ?t]]`.
   Group into `{entity_uuid: set(types)}`.
2. **Selection.**
   * **Control entities, all of them:** every entity carrying
     `:type/ingestion`, `:type/ingest-interval` or `:type/completed-region` —
     the format stamp, watermark, lineage-confirmed-through,
     correction-sweep-through, frontier intervals and archived regions. Few,
     and their misread is the one above.
   * **Plus the fixed control idents, whether or not AEVT listed them:**
     `:ingestion/format-version`, `:ingestion/watermark`,
     `:ingestion/frontier-low`, `:ingestion/frontier-high`,
     `:ingestion/lineage-confirmed-through`,
     `:ingestion/correction-sweep-through`, `:ingestion/last-run-at`, each at
     `uuid5(NAMESPACE_OID, ident)`. AEVT's view of one it did not list is the
     empty set. This is what keeps the check from failing open on AEVT damage
     (amendment, 2026-09-11, measured): redirecting `aevt_root_page` to its
     rightmost leaf on a stamped graph left the population query returning
     **0 rows** while EAVT still answered `:type/ingestion` for the stamp —
     so without the fixed idents, a graph whose AEVT lost the whole
     `:entity-type` block would pass as "nothing to probe". An ident absent
     from both indexes compares empty to empty and passes, so a fresh graph is
     unaffected. The tuple is built at call time: several of these constants
     are defined further down `mcp_server.py` than the check.
   * **A uniform random sample** of `sample_size` from the rest (or all of them
     if fewer). `rng` defaults to a fresh `random.Random()`, so successive runs
     cover different entities; tests inject a seeded one.
3. **Probe.** Per selected entity, `[:find ?t :where [#uuid "<e>" :entity-type ?t]]`.
   ~~The EAVT set must EQUAL the AEVT set for that entity. Both directions come
   free: EAVT missing a type is #370; EAVT holding a type AEVT lacks is AEVT
   damage for that entity.~~

   **Amendment, 2026-09-11 (final review, measured): same-transaction dedup
   makes set equality refuse healthy graphs, so refusal is empty-vs-non-empty
   only.** An entity given two `:entity-type` values IN ONE TRANSACT holds two
   facts sharing `(entity, attribute, tx_count, asserted)`. minigraf's
   `EavtKey`/`AevtKey` carry no value bytes, `build_sorted_index_entries` sorts
   each index with `sort_unstable_by` (`storage/persistent_facts.rs:1197,1215`),
   and `selective_fact_fetch` dedups on exactly that tuple, keeping whichever
   fact comes first (`query/datalog/executor.rs:411`). So AEVT and EAVT can
   each return a DIFFERENT single value for the same healthy entity. Measured:
   **3 of 6** healthy graphs built through `handle_minigraf_transact` (30
   single-typed `:decision/` fillers plus one entity typed both `:type/decision`
   and `:type/constraint`) read e.g. AEVT `{:type/decision}` against EAVT
   `{:type/constraint}`, and the equality rule refused all three; a raw
   transact reproduces it too. Deterministic per graph content (the same three
   tags refuse on every rerun).

   The rule is therefore: refuse ONLY when exactly one side is empty — EAVT
   empty with AEVT non-empty (#370's shape) or AEVT empty with EAVT non-empty
   (the fixed-ident AEVT-damage shape, step 2). Both empty agrees. Both
   non-empty but different is NOT damage: such entities are counted and, if
   any, summarized in one stderr line (`[_graph_index_cross_check] N entities
   read different :entity-type values through AEVT and EAVT; not index damage
   -- …`). It has no `stderr_capture` pattern, deliberately — it fires on
   healthy graphs. The return dict is unchanged.
4. **Re-confirm before refusing.** On the first disagreement, re-run the step-1
   query once and re-probe that entity. Refuse only if the disagreement
   survives. This excludes an in-process race: `call_tool` can join the
   preload lease, so a concurrent `minigraf_retract` of the sampled entity
   between steps 1 and 3 would otherwise read as damage. minigraf rejects
   `[(= ?e #uuid "…")]` (`PRS-070 unsupported expression argument: Uuid`), so
   the re-confirm is a full re-scan. (Amendment, 2026-09-11: the fresh scan
   replaces the population, so later entities compare against it and a race
   costs one re-scan, not one per entity.) (Amendment, 2026-09-11, after the
   final re-review: only an EMPTY-vs-non-empty disagreement is re-read, since
   it is the only shape that can refuse. The first version re-read every
   disagreement, so a healthy same-transaction multi-typed entity paid a full
   re-scan on the SUCCESS path — ~25 s each at 1.6M entities, extrapolated —
   while this step claimed the re-scan was failure-path only. Pinned by
   `test_same_transaction_types_cost_no_population_rescan`.)
5. **Refuse** with `GraphIndexDamageError(RuntimeError)` on a confirmed
   empty-vs-non-empty disagreement (step 3's amendment) — stop at the first
   one. The message names the entity UUID,
   its AEVT and EAVT answers, minigraf#370, and the recovery: re-ingest into a
   fresh graph path (`MINIGRAF_GRAPH_PATH` to a new file, or delete the graph
   and its `.fts.sqlite3`). It states that re-running ingestion and
   `checkpoint()` repair nothing.
6. **Return** `{"population": N, "probed": n, "control_probed": c}`.

A population of 0 passes only if every fixed control ident also reads empty
through EAVT: a graph with no entities has nothing to disagree about. It is not reported as verified — the returned `population: 0` says it
proved nothing, the `code_entities_scanned` idiom from #316.

### Placement

First statement inside `_load_ingestion_preload_state`'s `db_lease`, before
`_graph_format_version_verify`. That is the earliest point a run holds a
handle, and nothing has been written yet: `_graph_format_version_stamp_if_new`
is `_run_ingestion`'s first write and runs after the preload returns. A refused
run therefore leaves the graph untouched.

This covers every path into ingestion: `minigraf_ingest_git`, the server-start
auto-ingest and the startup backfill all go through `_run_ingestion`. Scope is
ingestion only (decided 2026-09-11): memory reads through `minigraf_query`
stay unguarded — they return wrong answers but write nothing from them — and a
per-server-start check would pay the population scan on every launch.

### Surfacing

* The raise reaches `_run_ingestion`'s run-level `except`: `status: error`,
  `error` = the message, and the traceback printed to fd 2 as
  `[_run_ingestion] ingestion failed: …`, which `stderr_capture`'s existing
  `ingestion_failed` pattern already matches. No new pattern.
* On success the returned dict is stored as
  `_ingest_progress["index_cross_check"]`, which `handle_minigraf_ingest_status`
  already spreads into its result. `_run_ingestion` sets the key to `None` as
  its first action, so a run that never reached the check — refused, or failed
  earlier — reads as "not run", never as a clean zero or a previous run's
  report. (Amended from "both initializers gain the key": tests and the
  at-scale harness call `_run_ingestion` directly with their own dicts, so
  only a reset inside the run covers every caller.)

### Cost

Measured on the 179 MB graph (6,148 entities): population scan 0.10 s, EAVT
probe 0.56 ms each on the healthy graph, so ~0.4 s total at 512 + controls. On
the damaged copy the first probe failed and 512 probes took 0.065 s. The scan
is linear in entities: ~25 s extrapolated to the 1.6M-entity ArangoDB graph,
once per run. The probe count is fixed.

### Detection power

With damage fraction f, 512 uniform probes miss with probability
(1 − f)^512: 0.6% at f = 1%, 3.9e-12 at f = 5%, effectively zero at the ~67%
#370 observed. Controls are probed exhaustively, so the specific
mature-reads-as-fresh misread is caught whenever it applies.

## Constructing the damage (tests)

A real file-backed graph, damaged the way #370 hypothesizes (a partially
written index tree with a valid header):

1. Build and `checkpoint()` a graph large enough that its EAVT tree has an
   internal root (400 three-fact entities is enough), then drop the handle.
2. Read the 84-byte v7 header. Assert `version == 7`. Follow `eavt_root_page`
   (bytes 32..40) down `rightmost_child` (internal page bytes 4..12) to the
   rightmost leaf (`page[0] == 0x21`; internal is `0x22`).
3. Write that leaf's page id into `eavt_root_page` and recompute
   `header_checksum` (bytes 80..84, CRC-32 of bytes 0..80 — `zlib.crc32`).

Why minigraf accepts it: `index_checksum` covers pages 1..page_count, not the
header page, so the stored checksum still matches and `open` trusts the
on-disk indexes instead of rebuilding them. Measured: 400 entities visible via
AEVT, 20 via EAVT, `[:module/m0 :description ?d]` → `[]`, no error on open.

The helper asserts every layout fact it depends on (header version, page
types), so a minigraf format change fails the test loudly rather than writing
garbage. Real backend, per `docs/testing-conventions.md`; nothing is faked.

## Tests

In a new `tests/test_index_cross_check.py` (not the 26k-line
`tests/test_mcp_server.py`), each proven by ablation (revert the guarded code,
watch it fail, restore):

1. **A damaged mature graph is refused before any write, not adopted as
   fresh.** A graph carrying a format stamp and ingestion state, damaged so the
   control entities lost their EAVT entries. `_run_ingestion` ends
   `status: error` naming `GraphIndexDamageError`, and
   `_ingest_progress["index_cross_check"]` stays `None`. "Before any write" is
   structural — the call precedes the read-only format check, which precedes
   every write — so the test asserts the refusal, not a byte comparison. File
   bytes are unusable (dropping a handle runs a checkpoint), and fact counts
   are an unreliable witness: commit triples re-written at their own
   `commit_ts_iso` collapse, while run-stamped facts written at a new
   valid-from duplicate (#156), so what a count shows depends on which facts a
   run happens to touch. Ablation: remove the call — the test must go red
   (the run is no longer refused).
2. **Diagnosis: partial damage names the index, not the format.** Damage that
   keeps the watermark's EAVT entries but loses the stamp's. Deterministic:
   filler idents are chosen with UUIDs below the watermark's, so the watermark
   sits in the rightmost leaf and the stamp does not. Expect
   `GraphIndexDamageError`. Ablation: move the call after
   `_graph_format_version_verify` — the run then fails with
   `GraphFormatVersionError` instead.
3. **Healthy graph passes** with `population > 0`, `probed > 0` (positive
   control), and `control_probed` equal to the number of control entities.
4. **Empty graph passes** with `population == 0`.
5. **Retract race does not refuse.** Retract a sampled entity between the
   population scan and its probe (by wrapping `_db_execute` to retract once,
   right after the population query returns — real backend, a timing seam, not
   a fake); the check passes.
   Ablation: remove the re-confirm — it must refuse.
6. **Sample bound.** A population larger than `sample_size` probes exactly
   `sample_size + control_probed`.
7. **AEVT damage does not fail open.** A stamped graph with
   `aevt_root_page` redirected to its rightmost leaf (precondition asserted:
   the population query returns 0 rows). The check raises. Ablation: drop the
   fixed-ident union — it passes as population 0.
8. **Same-transaction types that read differently are not refused**
   (amendment, 2026-09-11). The healthy multi-type graph from step 3's
   amendment, built through `handle_minigraf_transact`; precondition, through
   independent witnesses (a keyword-literal EAVT read and an attribute-only
   AEVT read filtered in Python): both sets non-empty and different. The check
   passes and prints the one-line stderr summary. Ablation: restore the
   `eavt != aevt` refusal — it raises `GraphIndexDamageError`.
9. **Damage confined to sampled entities is refused** (amendment,
   2026-09-11). Tests 1, 2 and 7 all damage a control entity, which is probed
   first, so a check that stopped probing the random sample would pass them.
   Fillers all below the lowest fixed-ident UUID, EAVT redirected to its
   rightmost leaf; precondition: every control ident with facts still reads
   through EAVT, most fillers do not. The check raises, naming a filler.
   Ablation: iterate only the control set — it passes.

Tests 1–2 need deterministic commit hashes (which entities survive in the
kept leaf depends on UUID order), so their repo fixes
`GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`, and every damaged-graph test asserts
its precondition (which reads are misread) before exercising the check.

## Docs

* `SKILL.md`, after the "no migration" paragraph in the ingestion section: the
  refusal, why the damage is otherwise silent, and that the only recovery is a
  fresh graph path. This is #336's fourth suggestion.
* `CLAUDE.md`: a section under Graph Storage — the routing facts, the
  format-check misread, measured cost, and the residuals below.

## Residuals (stated, not fixed)

* **AEVT damage, beyond the fixed control idents.** An entity missing from
  AEVT is never in the population, so it is never sampled. The fixed control
  idents are probed regardless; nothing else. (Amended 2026-09-11: the
  original text also claimed the "both-directions comparison" caught AEVT loss
  on any sampled entity. A sampled entity is in the population, so AEVT is
  non-empty for it, and under step 3's empty-vs-non-empty rule a PARTIAL AEVT
  loss on it is not refused.)
* **Same-transaction types, later retracted** (amendment, 2026-09-11,
  widened after the final re-review). An entity given two `:entity-type`
  values in one transact that later has ANY of them retracted — one, or both
  in a single retract, whose retractions share `(e, a, tx, false)` and dedup
  the same way — can read empty through one index and a value through the
  other, and is then refused although the graph is healthy. Measured, raw
  transact, 8 graphs each: retracting one refused 1 (h5); retracting both in
  one retract refused 1 (h3). `handle_minigraf_retract` reaches the second
  shape directly.
* **Partial within-entity loss.** Only `:entity-type` is compared. An EAVT
  range that lost some of an entity's facts but kept `:entity-type` passes.
  And since refusal needs one side EMPTY (step 3), an entity holding
  `:entity-type` values from DIFFERENT transactions that loses some but not
  all of them, in either index, passes too — a case the original equality
  rule caught.
* **Light damage** can escape the sample (see Detection power). Random
  selection means repeated runs compound coverage.
* **Linear scan cost** (~25 s at 1.6M entities, extrapolated, not measured).
* **Unguarded readers outside ingestion**: `minigraf_query`, the memory hooks,
  and `handle_minigraf_ingest_status`'s own `[:ingestion/last-run-at …]` read.
* **No `GRAPH_FORMAT_VERSION` bump, no migration.** The check only reads.
