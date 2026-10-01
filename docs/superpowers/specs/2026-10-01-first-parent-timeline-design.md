# First-parent timeline for git ingestion (#384)

## Problem

Ingestion walks `git log --topo-order --reverse`, which interleaves every
branch into one sequence, and the forward walk diffs a modified file against
`state.file_entities[path]` -- whatever that sequence last saw for the path --
rather than the commit's own parent. On concurrent history that produces three
measured defects (`evals/at_scale/probe_sibling_branch_closes.py`,
`results/384-sibling-branch-exposure.json`):

| repo | commits / merges | spurious closes | inverted windows | stale-live at HEAD |
|---|---|---|---|---|
| this repo | 1059 / 99 | 2 | 2 | 0 |
| nedb | 681 / 33 | 82 | 3 | 0 |
| minigraf | 1103 / 141 | 82 | 41 | 0 |
| pallets/flask | 5557 / 1732 | 955 | 132 | 1020 |

1. **Spurious close / rebirth.** A sibling-branch commit "removes" what the
   other branch added; the entity is reborn at the merge. Inverted when the
   sibling commit is dated earlier. Each cycle is a life for #383 to wipe.
2. **Resurrection (wrong at HEAD).** Mainline deletes or moves a path; a
   branch forked earlier edits the old path; the linearization places that
   edit after the deletion, so the walk re-introduces the old path's entities.
   Nothing ever closes them: at the merge the path's absence matches the first
   parent, so `--cc` omits it and #191's missed-removal supplement treats it as
   old news. Confirmed by a real ingest in both stream modes.
3. Diffing against the commit's own parent fixes (1) but NOT (2): the side
   edit genuinely modifies a file that exists on its branch.

## Decision (user, 2026-10-01)

**Valid time is the first-parent (mainline) tree.** "Live at t" means "present
in the branch tip's tree at t". Concretely:

- **Positions are the first-parent chain only.** `build_linearization` and the
  positionally aligned `commit_metadata` both use `--first-parent`. This repo
  goes 1060 -> 431 positions, flask 5557 -> 2276.
- **Every position diffs against its first parent**, merges included:
  `git diff-tree -r -M --raw <P1> <C>` (`--root <C>` for the root). A merge's
  row set is then the whole branch's net effect, conflict resolution included.
  The #185 `--cc` path and #191's missed-removal supplement exist only to
  approximate exactly this diff for merges, and are removed.
- **Attribution: the merge commit.** Entities a merge brings in carry
  `:introduced-by`/`:modified-in` = the merge, consistent with their
  valid-from. Authoring-commit attribution is a possible later attribute; it
  does not change liveness.
- **Side commits are metadata-only `:type/commit` entities.** At a merge
  position, every commit in `git rev-list <C> ^<P1>` other than C itself (each
  belongs to exactly one mainline merge) is written with the same seven commit
  triples a position gets, its `:parent` edges, and a new
  `[side :merged-in <merge>]`, all at the side commit's own date. No parse,
  no diff, no code effect. Written inside the merge position's apply, before
  `_frontier_persist_claim`, so they sit inside the same completion witness;
  a #313 re-walk re-writes identical triples at identical valid-from and they
  collapse. Written by the forward stream and the reverse stream alike (not
  by Stage B's `lifecycle_only`, which already skips commit triples).
  `:merged-in` is registered under the `commit` schema type (audit safety).

## Consumers that change

- `_git_diff_tree_raw` / `_extract_commit`: two-tree diff against the first
  parent, read from the commit's parents (one `git rev-parse` or the
  metadata). Rename detection and old-side content (`old_sha`) come from P1
  for free.
- `_count_commit_entities` (seeds `prior_ingested`): count mainline commit
  entities only -- those without `:merged-in`. Otherwise `walk_vs_graph` reads
  every side commit as a lost walk.
- `repo_total`: `git rev-list --first-parent --count`.
- `_orphaned_commit_count`: the ref's history set is the FULL `rev-list`, not
  `set(linearization)`, or every side commit reads as an orphan.
- `commit_census` (#317): `repo_commits` becomes the first-parent count, and a
  second, side-commit census compares `rev-list --count - --first-parent
  --count` against graph commit entities carrying `:merged-in`, gated with
  the same zero tolerance and the same "measure the clean baseline first" rule.
- `GRAPH_FORMAT_VERSION` 1 -> 2. Stored frontier hashes, watermarks and every
  entity's windows mean something different; per the standing decision, old
  graphs are rebuilt into a fresh path, never migrated.

## Verification

- Probe gets a `--first-parent` mode that replays the new walk; it must read
  0 spurious / 0 inverted / 0 stale-live on all four repos, while the default
  mode still reproduces the table above (positive control).
- Regression tests through the real write path: the issue's sibling-branch
  shape (no close between sibling and merge, no inverted window), the
  resurrection shape (old path not live at HEAD), and side commits present
  with `:merged-in` and both `:parent` edges. Each ablation-proven against the
  topo-order linearization.
- Existing merge tests (#185/#191) are rewritten to the new semantics rather
  than deleted where they still describe a behaviour (conflict-resolution
  content lands at the merge; content dropped at the merge never appears).
- Full suite, then the at-scale tier on this repo: fact_audit divergence 0,
  both `:introduced-by` checks 0, both censuses clean.

## Out of scope

- Authoring-commit attribution for merged changes.
- #383 (second close wipes first life): fewer spurious lives reduce its
  exposure; the defect itself still waits on minigraf#435.
- Foxtrot merges (feature branch as first parent) make the mainline zig-zag;
  that is git's own `--first-parent` convention and is accepted.
