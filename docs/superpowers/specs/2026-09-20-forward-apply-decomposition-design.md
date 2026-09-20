# `_forward_apply` decomposition — design (#346, PR 2)

Date: 2026-09-20. Issue: #346. Prerequisite: PR #360 (merged, master `428ac7e`)
made every defaulted parameter of `_forward_apply`/`_reverse_apply`
keyword-only, so a parameter inserted ahead of a flag now raises `TypeError`
instead of silently rebinding it.

This is a **pure refactor**. It writes no new facts, changes no fact shape,
bumps no `GRAPH_FORMAT_VERSION` and needs no migration. Its acceptance
criterion is that ingestion issues the *identical sequence of database
commands* before and after.

## Why

`_forward_apply` (`mcp_server.py:11878`, 594-line body) threads five distinct
behaviours through one `lifecycle_only` flag across six sites, and its per-file
loop holds 16 in-place mutations of `_ForwardWalkState` across 8 of its 12
dicts. #346 measured the seams and declined a mechanical extraction. This
design takes the direction that issue recommends — split by **file status**
(D / R / A-M), the axis the mutations actually partition on — rather than by
the flag, which strands 183 lines belonging to neither side.

The repetition is the other half of the motivation. The three-step "close an
entity" sequence (`_build_close_triples` → `_forget_closed_entity` →
`closed_idents.append`) appears **six** times (verified: 6 `_build_close_triples` and 6 `_forget_closed_entity` calls in the body); the ":depends-on edge close"
sequence appears **three** times, two of which are the same operation.
Both collapse into one helper each — but see the correction below: the M
branch's dep close iterates `previous_deps - current_deps` rather than the
whole recorded set, so `_fwd_close_dep_edges` covers two of the three sites and
the M one stays inline.

## Decisions taken before design work (each chosen deliberately)

1. **Behaviour-preserving means an identical write sequence**, verified
   differentially against master, not merely graph-equivalent output and not
   merely a green suite.
2. **Scope is the per-file loop plus the three post-loop passes.** The write
   tail stays in `_forward_apply`, so #342's "all three watermarks move
   together" rationale keeps its single copy site — this repo has already paid
   for a duplicated mechanism drifting.
3. **Helpers mutate `_ForwardWalkState` in place** (approach A), with each
   helper's docstring declaring the fields it reads and the fields it writes.
   Rejected alternatives are recorded under "Alternatives considered".

## Architecture

### Two new carriers

```python
@dataclass(frozen=True)
class _FwdCommitCtx:
    """Per-commit constants every helper needs. Frozen: nothing mutates these."""
    commit_hash: str
    commit_ident: str
    commit_ts_iso: str
    reason: str
    index_con: Optional[Any]

@dataclass
class _ForwardCommitWrites:
    """The five accumulators the body threads through every branch.

    Extracting any helper means extracting these first: `add_triples` alone is
    appended to or extended at 11 sites spread across the whole body, which is
    #346's stated obstacle at site 1.
    """
    add_triples: List[str]
    dep_add_triples: List[str] = field(default_factory=list)
    close_items: List[tuple] = field(default_factory=list)   # (triples, original_ts_iso)
    closed_idents: List[str] = field(default_factory=list)
    renamed_old_paths: set = field(default_factory=set)
```

`add_triples` is seeded by the caller — the commit's own seven triples, or
empty under `lifecycle_only` (flag site 1).

### Two shared helpers

`_fwd_close_entity(db, ctx, state, writes, ident, description, module_ident,
**close_kwargs)`

Performs the three-step close: appends `(_build_close_triples(...), orig_ts)`
to `close_items` where `orig_ts = state.entity_valid_from.get(ident,
ctx.commit_ts_iso)`, calls `_forget_closed_entity`, appends to
`closed_idents`. `**close_kwargs` passes straight through to
`_build_close_triples`, so each of the six call sites keeps the exact arguments
it has today:

| Site | Distinguishing kwargs |
|------|----------------------|
| D children | `extra_contains_parent`, `close_entity_type=True`, `file_value`, `is_static` |
| R old module | `close_entity_type=True`, `file_value`; **`extra_contains_parent=None` passed explicitly** |
| M removed children | as D children |
| `renamed_pairs` | as D children, with the old file's module as parent |
| `renamed_old_paths` | as D children, under the old path |
| gitlink remove | `entity_type_kw=":type/external-dependency"`, `file_value`; **no `close_entity_type`** |

The R old-module site passes `extra_contains_parent=None` explicitly rather
than inheriting it from a `field_class_ident.get(module_ident)` that returns
`None` only because module idents are never keys there. Same value, but the
reason becomes visible instead of accidental.

`_fwd_close_dep_edges(ctx, state, writes, module_ident, file_path, *, pop)`

Two of the three `:depends-on` close sites: the D branch and the renamed-away
path, both of which close every recorded edge and drop the `file_deps` key
(`pop=True`). **The M branch is NOT one of them** — it closes only
`previous_deps - current_deps` and then rewrites `file_deps[file_path]`, a
different operation, and it stays inline in `_fwd_diff_dependencies`.

### Status handlers and passes

Each takes `(db, ctx, state, writes, …)`:

| Helper | Replaces | State fields written |
|--------|----------|---------------------|
| `_fwd_apply_deleted_file` | the `status == "D"` branch | `file_entities`, `file_deps`, + those `_forget_closed_entity` touches |
| `_fwd_apply_renamed_head` | the `status == "R"` old-module block | `entity_valid_from`, `entity_descriptions`, `file_entities`, `field_*` |
| `_fwd_reconcile_and_build` | flag sites 2 **and** 3 | `entity_valid_from` (the pop), plus everything `_build_code_triples` maintains |
| `_fwd_close_removed_children` | the `status == "M"` removal diff | as `_forget_closed_entity` |
| `_fwd_diff_dependencies` | the import diff, stubs, `:resolves-to` | `entity_valid_from`, `entity_descriptions`, `unresolved_dep_idents`, `dep_valid_from`, `file_deps` |
| `_fwd_apply_renamed_pairs` | the `renamed_pairs` pass | as `_forget_closed_entity` |
| `_fwd_close_renamed_old_paths` | the `renamed_old_paths` pass | `file_entities`, `file_deps` |
| `_fwd_apply_gitlinks` | the `gitlink_changes` pass | `entity_valid_from`, `entity_descriptions`, `pinned_commit_state`, `submodule_paths` |

`_forward_apply` becomes: build `ctx` and `writes` → per file, dispatch by
status → the three passes → the write tail.

### `_fwd_reconcile_and_build` is one indivisible unit

Flag sites 2 and 3 are a single function and must stay one. Site 2's
`state.entity_valid_from.pop(ident, None)` exists **precisely so that** site
3's `_build_code_triples` treats that ident as newly introduced — the pop is
the purpose of the block, not incidental cleanup. Separating them, or
extracting the gate without the pop, mints a second `:introduced-by` alongside
the reverse stream's provisional guess (#235). The desync `RuntimeError`
between `_forward_candidate_idents` and `_forward_structural_triples_by_ident`
moves verbatim; it must stay a raise, never a silent `[]`.

### Flag flow after the split

`lifecycle_only` reaches exactly one helper, `_fwd_reconcile_and_build`, which
takes `status` and `lifecycle_only` and keeps **both gates verbatim**:

```python
candidates = _forward_candidate_idents(precomputed) if (not lifecycle_only or status == "R") else []
...
if not lifecycle_only or status == "R":
    writes.add_triples.extend(triples)
```

They are the same predicate today and are deliberately **not** fused into one
boolean: fusing is behaviour-neutral but removes the ability to change one
without the other, and the two gates exist for different documented reasons
(reconciliation ownership vs. emission).

The other four sites never move — site 1 is the `add_triples` seed, sites 4-6
(`:parent` edges, the three watermarks, the checkpoint) are in the retained
write tail. After this change the flag appears in two functions and the
per-file loop does not mention it at all.

### Docstring correction

`_forward_apply`'s docstring is currently wrong about its own structure: its
five bullets do not map onto the six flag sites — **site 6 (checkpoint
suppression) has no bullet**, and one bullet ("D files, R files and gitlink
changes are processed unchanged") describes behaviour the flag does not affect.
The rewrite enumerates the six sites, says which helper each now lives in, and
states the **call-order contract** below.

## Call-order contract (load-bearing)

1. Per file, in `extracted_files` order. D and A/M/R are mutually exclusive.
2. Within A/M/R: `_fwd_apply_renamed_head` (R only) → capture
   `previous_idents` → `_fwd_reconcile_and_build` → `_fwd_close_removed_children`
   (M only) → `_fwd_diff_dependencies`.
   `previous_idents` **must** be captured before the build, which appends to
   `file_entities`.
3. `_fwd_apply_renamed_pairs` **before** `_fwd_close_renamed_old_paths`: the
   latter excludes idents the matcher already closed with `:renamed-to`
   linkage. Reversing them double-closes.
4. `_fwd_apply_gitlinks` after both, then the write tail.

Two copy-then-iterate sites must survive extraction verbatim: the D branch and
`_fwd_close_renamed_old_paths` both iterate `list(state.file_entities.get(path,
…))` because `_forget_closed_entity` mutates that list as they walk it.
Dropping the `list(...)` inside a helper is the most likely way to break this
silently.

## Verification

### The differential oracle

`evals/at_scale/probe_forward_apply_write_parity.py`, run once per arm from two
worktrees (master, branch).

* **Recording point: `_db_execute`** — the one choke point every read and write
  passes through. Recording `_ingest_transact`/`_ingest_close` instead would
  miss the writes `_forward_reconcile_provisional` issues internally.
* **Tagging:** a thin wrapper around `_forward_apply`/`_reverse_apply` sets a
  module-global naming the commit being applied; every recorded command is
  tagged with it.
* **Comparison: per commit**, not one global list — the two streams interleave
  through executors, so global order is not guaranteed stable run to run. The
  set of commits must match, and each commit's command list must be equal.
* **`PYTHONHASHSEED=0` in both arms.** Several sites iterate sets/dicts of
  strings (`current_deps - previous_deps`, `submodule_paths`,
  `renamed_old_paths`), so hash randomization alone reorders emitted triples
  between two runs of *identical* code. Unpinned, the oracle reports
  differences that are not real — and worse, makes a real difference look like
  ordinary noise.
* **Positive control, run first: master against itself, twice, zero diff.** If
  that is not clean, no arm comparison means anything.
* **Corpus, two repos.**
  1. This repo's history, truncated for cost (a full 998-commit run took
     3441 s in #352, and this needs two arms).
  2. A **mandatory** synthetic repo: gitlink add / bump / remove, a file rename
     with unmatched children, an in-file rename, delete-then-re-add. Measured
     while drafting: this repo has **no submodules at all** and only **2**
     rename commits in its entire history, so corpus 1 cannot cover the gitlink
     branches or the unmatched-rename-child pass.
* **Both ratios**, 1:1 and forward-only: `lifecycle_only=True` only ever runs
  in the mixed mode.

The probe stays in the repo (precedent: `probe_lease_drop_cost.py`, #281) with
its caveats in its docstring.

### Suite gates

The four parity classes by name — `TestMultiStreamParityWithForwardOnly`,
`TestStageBRepairsLifecycleFacts`, `TestReverseFillValidTimeParity`,
`TestForwardStructuralTriplesByIdent` — then the full suite. `-k parity` is
**not** an acceptable substitute: that substring filter misses
`TestStageBRepairsLifecycleFacts`, the one class exercising `lifecycle_only=True`.

No new regression tests are added for the refactor itself: there is no new
behaviour to pin, and the oracle plus the existing parity classes are the
gate. Any helper found to need a behaviour decision during implementation
means the refactor is no longer pure — stop and re-open the design.

## Alternatives considered

**B. Helpers return deltas the caller applies.** The strongest form of what
#346 asks for, and rejected as dishonest at this scope: `_build_code_triples`
and `_forget_closed_entity` both mutate `_ForwardWalkState` deep inside the
helpers and neither is in scope. Some mutations would flow through the returned
delta while others still happened in place, so the "explicit boundary" would be
a half-truth. An honest version means rewriting both functions — a much larger
change carrying real lineage risk.

**C. A narrowed view object per helper**, raising on any field outside its
declared set. Enforces the contract at runtime rather than by convention, but
costs a wrapper class and per-access indirection on a hot path, and fights the
two helpers above, which take raw dicts positionally. Can be layered on top of
A later without redoing A.

**Splitting by `lifecycle_only`** — rejected by #346's own measurement: the
mutations partition by file status, not by the flag, which is why the flag
leaves 183 lines belonging to neither side.

## Residuals (stated, not fixed)

* **State mutation stays in place.** With approach A the "explicit boundary" is
  a documented contract, not an enforced one. This design does **not** claim to
  have made `_ForwardWalkState` mutation explicit; it makes it *declared*.
* **The oracle proves equality on the corpus it runs, not in general.** Paths
  neither repo exercises stay unproven — an extensioned gitlink path (already
  documented as unreachable-in-practice), multiple roots, and any file status
  the corpus lacks.
* **Performance is unmeasured.** A handful of extra calls per file per commit
  against parse and transact costs; expected negligible. The probe records
  per-arm wall clock as a by-product, but comparable probes on this machine
  drifted 2.4x between batches with no code change, so no timing delta will be
  reported as a finding.
* **`_forward_apply` remains large** after the split — the write tail and
  dispatch skeleton. Shrinking it further means moving #342's watermark
  rationale, which decision 2 declines.

## Out of scope

Changing `_build_code_triples` or `_forget_closed_entity`; touching
`_reverse_apply` or `_correction_sweep_apply`; any fact-model, schema or
performance change; runtime enforcement of the state contract (approach C).
