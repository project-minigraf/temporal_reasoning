# `_forward_apply` Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `_forward_apply`'s per-file loop and three post-loop passes into status-based helpers without changing a single database command ingestion issues.

**Architecture:** Two carriers (`_FwdCommitCtx`, `_ForwardCommitWrites`) make extraction possible; two shared helpers absorb the 6 entity-close and 3 dep-edge-close repetitions; eight handlers replace the status branches and passes. Helpers mutate `_ForwardWalkState` in place with a declared per-helper contract. The write tail stays in `_forward_apply`.

**Tech Stack:** Python 3.10-3.14, pytest, minigraf 2.x. Always `.venv/bin/python` — the system interpreter has minigraf 1.1.1 and fakes ~122 failures.

**Spec:** `docs/superpowers/specs/2026-09-20-forward-apply-decomposition-design.md` — read it before Task 1. Executors read both.

## Global Constraints

- **This is a pure refactor.** No new facts, no fact-shape change, no `GRAPH_FORMAT_VERSION` bump, no migration. If any task appears to need a behaviour decision, **STOP and report** — the refactor is no longer pure and the design must be re-opened.
- **Acceptance is an identical write sequence**, verified differentially against master by the Task 1 probe. Not graph-equivalence, not a green suite.
- **Interpreter:** `.venv/bin/python` for every command in this plan.
- **Branch:** `346-forward-apply-decomposition` (already exists, holds the spec commit `7c8baeb`).
- **Parity gate, run by name after every task** (`-k parity` is NOT a substitute — it misses `TestStageBRepairsLifecycleFacts`, the only class exercising `lifecycle_only=True`):
  ```bash
  .venv/bin/python -m pytest tests/test_mcp_server.py -q -k \
    "TestMultiStreamParityWithForwardOnly or TestStageBRepairsLifecycleFacts or TestReverseFillValidTimeParity or TestForwardStructuralTriplesByIdent"
  ```
  Expected: `13 passed`.
- **Never** open, close or lease a DB handle inside a helper — every helper receives `db`. Single-handle invariant; #253 records a lifecycle-adjacent change here that segfaulted the suite.
- **Two copy-then-iterate sites must keep their `list(...)`**: the D branch and the renamed-old-path pass both iterate `list(state.file_entities.get(path, []))` because `_forget_closed_entity` mutates that list as they walk it.
- **No closing keywords** for #346 in any commit message or PR body. This is PR 2 of 2 but #346 also covers follow-on work; the human decides when it closes. Use "Part of #346".
- **Commit trailers** on every commit:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
  ```

## File Structure

| File | Responsibility | Tasks |
|------|---------------|-------|
| `evals/at_scale/probe_forward_apply_write_parity.py` | **Create.** Records every `_db_execute` command per commit; compares two recordings. | 1, 11 |
| `tests/test_forward_apply_write_parity_probe.py` | **Create.** Unit tests for the probe's own comparison logic (the probe is the gate, so its comparator must not fail open). | 1 |
| `mcp_server.py` | **Modify.** Carriers + 10 helpers + slimmed `_forward_apply`. | 2-10 |
| `CLAUDE.md` | **Modify.** Record the decomposition and the call-order contract. | 10 |
| `docs/superpowers/specs/2026-09-20-...-design.md` | Already committed. Read-only for executors. | — |

**Line numbers below are as of master `428ac7e`** and shift as tasks land. Always re-locate a block by its quoted first line, never by line number alone.

---

### Task 1: The differential oracle, and master's baseline

**Files:**
- Create: `evals/at_scale/probe_forward_apply_write_parity.py`
- Create: `tests/test_forward_apply_write_parity_probe.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `record_run(repo_path, graph_path, ratio, out_path) -> dict`, `compare(path_a, path_b) -> dict` with keys `ok: bool`, `commits_only_in_a: list[str]`, `commits_only_in_b: list[str]`, `differing_commits: list[dict]`. A `build_synthetic_repo(dest: Path) -> Path` helper.

**This task must be completed and its baseline recorded BEFORE any change to `mcp_server.py`.** The baseline is master's behaviour; once the refactor starts there is nothing left to compare against.

- [ ] **Step 1: Write the probe's comparator tests**

`tests/test_forward_apply_write_parity_probe.py`:

```python
import json
import pytest
from evals.at_scale import probe_forward_apply_write_parity as probe


def _write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def test_identical_recordings_compare_equal(tmp_path):
    rows = [{"commit": "abc", "cmd": "(transact [[:a :b 1]])"},
            {"commit": "abc", "cmd": "(query [:find ?e])"}]
    a = _write(tmp_path, "a.jsonl", rows)
    b = _write(tmp_path, "b.jsonl", rows)
    assert probe.compare(a, b)["ok"] is True


def test_a_reordered_command_within_one_commit_is_a_difference(tmp_path):
    a = _write(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"},
                                     {"commit": "abc", "cmd": "Y"}])
    b = _write(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "Y"},
                                     {"commit": "abc", "cmd": "X"}])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert [d["commit"] for d in result["differing_commits"]] == ["abc"]


def test_a_missing_commit_is_a_difference(tmp_path):
    a = _write(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"},
                                     {"commit": "def", "cmd": "Y"}])
    b = _write(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "X"}])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["commits_only_in_a"] == ["def"]


def test_empty_recordings_are_not_reported_as_clean(tmp_path):
    """A comparator that reads two empty files as 'ok' fails OPEN: a probe
    that recorded nothing at all would certify the refactor. This is the
    positive control on the gate itself."""
    a = _write(tmp_path, "a.jsonl", [])
    b = _write(tmp_path, "b.jsonl", [])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["proved_nothing"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_forward_apply_write_parity_probe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'evals.at_scale.probe_forward_apply_write_parity'`.

- [ ] **Step 3: Write the probe**

`evals/at_scale/probe_forward_apply_write_parity.py`. Key requirements, each load-bearing:

```python
"""#346 PR 2: differential write-sequence oracle for the _forward_apply refactor.

Records every _db_execute command issued during an ingestion run, tagged with
the commit being applied, and compares two recordings command-for-command.

CAVEATS, all load-bearing:

* PYTHONHASHSEED MUST be 0 in every arm. Several ingestion sites iterate sets
  and dicts of strings (current_deps - previous_deps, submodule_paths,
  renamed_old_paths), so string-hash randomization alone reorders emitted
  triples between two runs of IDENTICAL code. Unpinned, this probe reports
  differences that are not real -- and worse, makes a real difference look
  like ordinary noise. record_run() refuses to run when it is unset or
  non-zero.
* Comparison is PER COMMIT, never one global list: the two streams interleave
  through executors, so global order is not stable run to run.
* Two empty recordings are NOT clean -- see compare()'s proved_nothing key.
* Absolute wall-clock numbers are NOT comparable across invocation batches
  (precedent: probe_sweep_window_cost.py, where a baseline drifted 2.4x
  between rounds with no code change). Do not read a timing delta here as a
  finding.
"""
```

- `record_run(repo_path, graph_path, ratio, out_path)`:
  - Raises `RuntimeError` unless `os.environ.get("PYTHONHASHSEED") == "0"`.
  - Wraps `mcp_server._forward_apply` and `mcp_server._reverse_apply` to set a module-global `_current_commit` (the commit hash: `commit[0]` for forward, `linearization[pos]` for reverse) — these are keyword-safe since PR #360, so wrap with `*args, **kwargs` and read `args[3][0]` / `args[2][args[4]]` by the leading positionals only.
  - Wraps `mcp_server._db_execute` to append `{"commit": _current_commit, "cmd": <command string>}` to a JSONL file before delegating.
  - Sets `MINIGRAF_GRAPH_PATH`, `MINIGRAF_INGEST_STREAM_RATIO=<ratio>`, runs `asyncio.run(mcp_server._run_ingestion(repo, branch))`, then `_reset_db_state()`.
  - Returns `{"commands": n, "commits": m, "elapsed_s": t}`.
- `compare(path_a, path_b)`: groups rows by commit, compares lists elementwise. Returns `ok`, `proved_nothing` (True when either file has zero rows), `commits_only_in_a/b`, and `differing_commits` (each `{"commit", "first_divergence_index", "a", "b"}`).
- `build_synthetic_repo(dest)`: a git repo exercising what this repo's own history cannot — **measured while drafting the spec: this repo has no submodules at all and only 2 rename commits.** Must include: a gitlink add, a gitlink bump, a gitlink remove, a file rename whose children are not all matched by the rename matcher, an in-file function rename, and a delete-then-re-add of the same path.
- `main()`: argparse over `--repo`, `--graph`, `--ratio`, `--out`, `--compare A B`, `--truncate-by`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_forward_apply_write_parity_probe.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Prove the oracle's own positive control — master against itself**

```bash
cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning
S=/tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/82224bb5-021e-4a69-8b71-4fbb0a59ab72/scratchpad
PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_forward_apply_write_parity.py \
  --synthetic --graph $S/self1.graph --ratio 1:1 --out $S/self1.jsonl
PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_forward_apply_write_parity.py \
  --synthetic --graph $S/self2.graph --ratio 1:1 --out $S/self2.jsonl
.venv/bin/python evals/at_scale/probe_forward_apply_write_parity.py --compare $S/self1.jsonl $S/self2.jsonl
```

Expected: `ok: true`, `proved_nothing: false`, a non-zero command count.

**If this is not clean, STOP.** An unstable oracle certifies nothing, and every later task depends on it. Report the instability rather than proceeding or loosening the comparator.

- [ ] **Step 6: Record master's baseline, all four arms**

Same commands, `--ratio 1:1` and `--ratio forward-only`, against both the synthetic repo and this repo truncated (`--repo . --truncate-by 300`). Save as `$S/base-{synthetic,repo}-{1to1,fwd}.jsonl`. Keep them — Task 11 compares against these exact files.

Record the four command counts in the commit message. A baseline whose command count is zero proves nothing.

- [ ] **Step 7: Commit**

```bash
git add evals/at_scale/probe_forward_apply_write_parity.py tests/test_forward_apply_write_parity_probe.py
git commit -m "$(cat <<'EOF'
Add the #346 write-sequence parity probe and record master's baseline

Part of #346. Records every _db_execute command per commit and compares
two recordings command-for-command. PYTHONHASHSEED=0 is refused-on-unset,
not merely documented: several ingestion sites iterate string sets, so
hash randomization reorders triples between two runs of identical code.

Self-comparison on master is clean (<N> commands, <M> commits), which is
the oracle's positive control -- two empty recordings report
proved_nothing rather than ok.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
EOF
)"
```

---

### Task 2: The two carriers

**Files:**
- Modify: `mcp_server.py` — add the dataclasses near `_ForwardWalkState` (currently `:13330`); rewire `_forward_apply`'s body (`:11963` onward).

**Interfaces:**
- Produces: `_FwdCommitCtx(commit_hash, commit_ident, commit_ts_iso, reason, index_con)` (frozen dataclass) and `_ForwardCommitWrites(add_triples, dep_add_triples, close_items, closed_idents, renamed_old_paths)`.

No helper is extracted in this task. The body still reads top-to-bottom; only the local variables move onto the two carriers. This is deliberately the dullest possible first step, because it touches every line that later tasks move.

- [ ] **Step 1: Add both dataclasses**

Place immediately above `class _ForwardWalkState:` in `mcp_server.py`. Copy the definitions from the spec's "Two new carriers" section verbatim, including the docstrings.

- [ ] **Step 2: Rewire `_forward_apply`'s locals onto the carriers**

In `_forward_apply`, replace the five local accumulators (`add_triples` at `:11963`, `close_items`, `closed_idents`, `dep_add_triples`, `renamed_old_paths`) with:

```python
    ctx = _FwdCommitCtx(
        commit_hash=commit_hash,
        commit_ident=commit_ident,
        commit_ts_iso=commit_ts_iso,
        reason=reason,
        index_con=index_con,
    )
    writes = _ForwardCommitWrites(add_triples=[] if lifecycle_only else [
        f"[{commit_ident} :entity-type :type/commit]",
        ...  # the existing seven triples, unchanged
    ])
```

Then mechanically rewrite every use in the body: `add_triples` → `writes.add_triples`, and the same for the other four. The write tail (`:12386` onward) reads `writes.add_triples` / `writes.close_items` / `writes.closed_idents` / `writes.dep_add_triples`.

Do not reorder, merge or "tidy" anything while doing this. A reordered append is exactly what the oracle exists to catch, and catching it here wastes a run.

- [ ] **Step 3: Run the parity gate**

Run the Global Constraints parity command.
Expected: `13 passed`.

- [ ] **Step 4: Run the write-sequence oracle against master's baseline**

```bash
S=/tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/82224bb5-021e-4a69-8b71-4fbb0a59ab72/scratchpad
PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_forward_apply_write_parity.py \
  --synthetic --graph $S/t2.graph --ratio 1:1 --out $S/t2.jsonl
.venv/bin/python evals/at_scale/probe_forward_apply_write_parity.py --compare $S/base-synthetic-1to1.jsonl $S/t2.jsonl
```

Expected: `ok: true`, `proved_nothing: false`.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py
git commit -m "$(cat <<'EOF'
Move _forward_apply's five accumulators onto two carriers (#346)

Part of #346. No extraction yet: _FwdCommitCtx (frozen) carries the
per-commit constants, _ForwardCommitWrites the five accumulators every
branch threads through. Extracting any helper means extracting these
first -- add_triples alone is appended to at 11 sites across the body.

Write-sequence oracle clean against master's baseline (synthetic, 1:1).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
EOF
)"
```

---

### Task 3: The two shared close helpers

**Files:**
- Modify: `mcp_server.py` — new helpers above `_forward_apply`; 6 + 3 call sites inside it.

**Interfaces:**
- Consumes: `_FwdCommitCtx`, `_ForwardCommitWrites` (Task 2).
- Produces:
  ```python
  def _fwd_close_entity(db, ctx, state, writes, ident, description, module_ident, **close_kwargs) -> None
  def _fwd_close_dep_edges(ctx, state, writes, module_ident, file_path, *, pop: bool) -> None
  ```

- [ ] **Step 1: Write `_fwd_close_entity`**

```python
def _fwd_close_entity(
    db: Any,
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    ident: str,
    description: str,
    module_ident: str,
    **close_kwargs: Any,
) -> None:
    """Close one entity: build its close triples, forget it, record the ident.

    The three-step sequence this replaces appeared SIX times (D children, the
    R old module, M removed children, renamed_pairs, renamed_old_paths,
    gitlink remove). **close_kwargs passes straight through to
    _build_close_triples so each site keeps its exact arguments -- the gitlink
    site passes entity_type_kw and NO close_entity_type, unlike the other five.

    Reads state: entity_valid_from (for orig_ts).
    Writes state: entity_valid_from, entity_descriptions, field_class_ident,
    file_entities, field_static_ident, entity_introduced_by (all via
    _forget_closed_entity).
    """
    orig_ts = state.entity_valid_from.get(ident, ctx.commit_ts_iso)
    writes.close_items.append((
        _build_close_triples(
            ident, description, module_ident,
            introduced_by=_resolve_introduced_by(db, state, ident),
            **close_kwargs,
        ),
        orig_ts,
    ))
    _forget_closed_entity(
        ident, close_kwargs.get("file_value"), state.entity_valid_from,
        state.entity_descriptions, state.field_class_ident, state.file_entities,
        state.field_static_ident, state.entity_introduced_by,
    )
    writes.closed_idents.append(ident)
```

**Check each of the six sites against this before converting it.** Three details differ per site and must be preserved exactly: the `file_path` argument to `_forget_closed_entity` (the gitlink site passes `path`, the renamed-old-path site passes `r_old_path`), whether `extra_contains_parent` is passed (the R old-module site passes `None` **explicitly**, per the spec), and `close_entity_type` vs `entity_type_kw`.

If `close_kwargs.get("file_value")` does not equal the `file_path` a site passes to `_forget_closed_entity` today, **do not** paper over it with a default — add an explicit `forget_path=` parameter and pass it at that site.

- [ ] **Step 2: Write `_fwd_close_dep_edges`**

```python
def _fwd_close_dep_edges(
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    module_ident: str,
    file_path: str,
    *,
    pop: bool,
) -> None:
    """Close every :depends-on edge recorded for one module.

    pop=True drops the file_deps key entirely (the D branch and the
    renamed-away path, where the whole file is gone). The M branch closes
    individual edges and must NOT pop -- it rewrites file_deps[file_path]
    with the current set immediately afterwards.

    Reads state: file_deps, dep_valid_from.
    Writes state: file_deps (only when pop=True).
    """
    for dep_ident in state.file_deps.get(file_path, set()):
        orig_ts = state.dep_valid_from.get((module_ident, dep_ident), ctx.commit_ts_iso)
        writes.close_items.append(
            ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
        )
    if pop:
        state.file_deps.pop(file_path, None)
```

**Note:** the M-branch dep close iterates `previous_deps - current_deps`, not the whole `file_deps` set — it does **not** use this helper. Only the D branch (`:11986` area) and the renamed-old-path pass (`:12265` area) do. Two sites, not three; the spec's "three `:depends-on` close sites" counts the M one, which stays inline.

- [ ] **Step 3: Convert the six close sites and two dep sites**

One site at a time, re-locating each by its quoted first line.

- [ ] **Step 4: Run the parity gate**

Expected: `13 passed`.

- [ ] **Step 5: Run the oracle, both repos, both ratios**

Four comparisons against the Task 1 baselines. All four must report `ok: true` and `proved_nothing: false`.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py
git commit -m "$(cat <<'EOF'
Extract _fwd_close_entity and _fwd_close_dep_edges (#346)

Part of #346. The three-step entity close appeared six times and the
whole-module dep-edge close twice; both collapse into one helper each,
with **close_kwargs preserving each site's exact _build_close_triples
arguments (the gitlink site passes entity_type_kw and no
close_entity_type; the R old-module site now passes
extra_contains_parent=None explicitly rather than relying on a .get that
misses).

The M branch's dep close iterates previous_deps - current_deps and stays
inline -- it is not the same operation.

Write-sequence oracle clean on all four arms.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
EOF
)"
```

---

### Task 4: `_fwd_apply_deleted_file` (the D branch)

**Files:**
- Modify: `mcp_server.py` — extract `if status == "D":` (`:11981`-`:12014`).

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: `_fwd_apply_deleted_file(db, ctx, state, writes, file_path) -> None`.

- [ ] **Step 1: Extract the branch verbatim**

```python
def _fwd_apply_deleted_file(
    db: Any,
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    file_path: str,
) -> None:
    """Close a deleted file's module, every child entity and every dep edge.

    Reads state: file_entities, entity_valid_from, entity_descriptions,
    field_class_ident, field_static_ident, file_deps, dep_valid_from.
    Writes state: file_entities (pops the key), file_deps (pops the key),
    plus everything _forget_closed_entity touches.
    """
```

The body moves unchanged, with two things preserved exactly:
- `idents = list(state.file_entities.get(file_path, [_code_ident("module", file_path)]))` — the `list(...)` copy is load-bearing (`_forget_closed_entity` mutates that list while the loop walks it) and so is the module-ident default for a file with no recorded entities.
- `state.file_entities.pop(file_path, None)` runs after the per-ident loop and before the dep close.

- [ ] **Step 2: Replace the branch with the call**

```python
        if status == "D":
            _fwd_apply_deleted_file(db, ctx, state, writes, file_path)
```

- [ ] **Step 3: Run the parity gate** — Expected: `13 passed`.

- [ ] **Step 4: Run the oracle, four arms** — all `ok: true`.

- [ ] **Step 5: Commit** (message: `Extract _fwd_apply_deleted_file (#346)`, trailers as above).

---

### Task 5: `_fwd_apply_renamed_head` (the R old-module block)

**Files:**
- Modify: `mcp_server.py` — extract `if status == "R" and old_path:` (`:12016`-`:12048`).

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: `_fwd_apply_renamed_head(db, ctx, state, writes, file_path, old_path) -> None`.

- [ ] **Step 1: Extract the block verbatim**

```python
def _fwd_apply_renamed_head(
    db: Any,
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    file_path: str,
    old_path: str,
) -> None:
    """Record a file rename's module-level linkage and close the old module.

    :renamed-to and :renamed-from are both brand-new facts that become true at
    the rename commit and stay true forever -- they go through add_triples,
    NOT through the old entity's close window.

    The old path is added to writes.renamed_old_paths; its remaining children
    and dep edges are closed later by _fwd_close_renamed_old_paths, which runs
    after the renamed_pairs pass so confirmed renames can be excluded.

    Reads state: entity_descriptions, entity_valid_from.
    Writes state: writes.renamed_old_paths, plus everything
    _forget_closed_entity touches.
    """
```

- [ ] **Step 2: Replace the block with the call.**

- [ ] **Step 3: Run the parity gate** — Expected: `13 passed`.

- [ ] **Step 4: Run the oracle, four arms** — all `ok: true`. The synthetic repo is the arm that actually exercises this; a clean result on this-repo alone proves little, since its history holds only 2 rename commits.

- [ ] **Step 5: Commit** (`Extract _fwd_apply_renamed_head (#346)`).

---

### Task 6: `_fwd_reconcile_and_build` (flag sites 2 and 3, indivisible)

**Files:**
- Modify: `mcp_server.py` — extract `candidates = (` (`:12091`) through the `if not lifecycle_only or status == "R": writes.add_triples.extend(triples)` block (`:12140`).

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: `_fwd_reconcile_and_build(db, ctx, state, writes, file_path, extracted, precomputed, status, lifecycle_only) -> None`.

**This is the task the spec warns about most.** Sites 2 and 3 are one unit: the `state.entity_valid_from.pop(ident, None)` exists precisely so `_build_code_triples` treats that ident as newly introduced. Splitting them, or extracting the gate without the pop, mints a second `:introduced-by` alongside the reverse stream's provisional guess (#235).

- [ ] **Step 1: Extract, carrying all three comment blocks verbatim**

```python
def _fwd_reconcile_and_build(
    db: Any,
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    file_path: str,
    extracted: Any,
    precomputed: Dict[str, Any],
    status: str,
    lifecycle_only: bool,
) -> None:
    """Reconcile provisional lineage for this file's entities, then build them.

    ONE unit by construction: the entity_valid_from.pop below exists precisely
    so _build_code_triples' own membership gate treats that ident as newly
    introduced. Separating the pop from the build mints a SECOND
    :introduced-by alongside the reverse stream's provisional guess (#235).

    Both lifecycle_only gates are kept verbatim and separate. They are the
    same predicate today and are deliberately NOT fused: they exist for
    different reasons (who owns A/M lineage vs. whether the built triples are
    emitted), and fusing removes the ability to change one without the other.

    Reads state: file_entities (via _build_code_triples), ts_by_commit_ident.
    Writes state: entity_valid_from (the pop, and _build_code_triples'
    introductions), entity_descriptions, file_entities, field_class_ident,
    field_static_ident, entity_introduced_by.
    """
```

The three long comment blocks — the #222 phase 2d pop rationale, the #235 stale-NEGATIVE / stale-POSITIVE analysis, and the `lifecycle_only` Stage B paragraph — move with the code. They are the reason this block is shaped as it is.

The desync `RuntimeError` moves verbatim and **stays a raise**. A silent `[]` there would let `_forward_reconcile_provisional` retract the guess and confirm lineage while skipping the re-dating, stranding structural facts at the wrong valid-time.

- [ ] **Step 2: Replace with the call**

```python
            _fwd_reconcile_and_build(
                db, ctx, state, writes, file_path, extracted, precomputed,
                status, lifecycle_only,
            )
```

- [ ] **Step 3: Run the parity gate** — Expected: `13 passed`. `TestStageBRepairsLifecycleFacts` is the class that exercises `lifecycle_only=True`; if it is not in the passing set, the gate ran wrong.

- [ ] **Step 4: Run the oracle, four arms** — all `ok: true`. **The 1:1 arms are the ones that matter here**: `lifecycle_only=True` never runs in forward-only mode, so a forward-only-only check would certify half the change.

- [ ] **Step 5: Run the lineage-specific tests**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q -k "provisional or introduced_by or lineage"
```
Expected: all pass. Record the count in the commit message.

- [ ] **Step 6: Commit** (`Extract _fwd_reconcile_and_build, sites 2+3 as one unit (#346)`).

---

### Task 7: `_fwd_close_removed_children` (the M removal diff)

**Files:**
- Modify: `mcp_server.py` — extract `if status == "M":` (`:12141`-`:12190`).

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: `_fwd_close_removed_children(db, ctx, state, writes, file_path, precomputed, previous_idents, renamed_pairs) -> None`.

- [ ] **Step 1: Extract verbatim**

```python
def _fwd_close_removed_children(
    db: Any,
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    file_path: str,
    precomputed: Dict[str, Any],
    previous_idents: set,
    renamed_pairs: List[tuple],
) -> None:
    """Close entities that a modified file no longer defines.

    previous_idents MUST be the set captured BEFORE _fwd_reconcile_and_build
    ran for this file -- _build_code_triples only ever appends to
    file_entities, so the diff is against the pre-build snapshot.

    All four entry kinds are counted as still-present (functions, classes,
    globals, fields). Omitting globals/fields would make every surviving one
    look removed on any later edit and wrongly close it (#113).

    In-place renames (old -> new in the same file) are excluded here; the
    renamed_pairs pass closes them with :renamed-to linkage, and closing them
    twice is a double close.

    Reads state: entity_valid_from, entity_descriptions, field_class_ident,
    field_static_ident.
    Writes state: everything _forget_closed_entity touches.
    """
```

- [ ] **Step 2: Replace with the call**, keeping `previous_idents` captured at its current point (`:12049`, before the reconcile/build call).

- [ ] **Step 3: Run the parity gate** — Expected: `13 passed`.

- [ ] **Step 4: Run the oracle, four arms** — all `ok: true`.

- [ ] **Step 5: Commit** (`Extract _fwd_close_removed_children (#346)`).

---

### Task 8: `_fwd_diff_dependencies` (the import diff)

**Files:**
- Modify: `mcp_server.py` — extract from `module_ident = _code_ident("module", file_path)` (`:12191`) through `state.file_deps[file_path] = current_deps` (`:12227`).

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: `_fwd_diff_dependencies(ctx, state, writes, file_path, precomputed, status) -> None`.

- [ ] **Step 1: Extract verbatim**

```python
def _fwd_diff_dependencies(
    ctx: "_FwdCommitCtx",
    state: "_ForwardWalkState",
    writes: "_ForwardCommitWrites",
    file_path: str,
    precomputed: Dict[str, Any],
    status: str,
) -> None:
    """Diff this file's :depends-on edges and open stubs for unresolved imports.

    Resolution already happened in _extract_commit against that commit's own
    git-ls-tree state -- nothing is resolved here.

    An unresolved, non-relative import that names no known entity opens an
    :type/external-dependency stub, and #112 links it to an already-known
    submodule whose path matches. Those stubs carry :entity-type/:ident/
    :description only -- never an :introduced-by, which is why
    :type/external-dependency is excluded from the at-scale orphan check
    (#316).

    Note it takes NO db handle: every decision here reads preloaded state.

    Reads state: entity_valid_from, submodule_paths, file_deps.
    Writes state: entity_valid_from, entity_descriptions,
    unresolved_dep_idents, dep_valid_from, file_deps.
    """
```

The M-only close of `previous_deps - current_deps` stays inside this helper, gated on `status == "M"` exactly as today. It does **not** use `_fwd_close_dep_edges` — different set, and no pop.

- [ ] **Step 2: Replace with the call.**

- [ ] **Step 3: Run the parity gate** — Expected: `13 passed`.

- [ ] **Step 4: Run the oracle, four arms** — all `ok: true`.

- [ ] **Step 5: Commit** (`Extract _fwd_diff_dependencies (#346)`).

---

### Task 9: The three post-loop passes

**Files:**
- Modify: `mcp_server.py` — `for category, old_file, ... in renamed_pairs:` (`:12229`), `if renamed_old_paths:` (`:12265`), `for kind, sha, path in gitlink_changes:` (`:12313`).

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces:
  ```python
  def _fwd_apply_renamed_pairs(db, ctx, state, writes, renamed_pairs) -> None
  def _fwd_close_renamed_old_paths(db, ctx, state, writes, renamed_pairs) -> None
  def _fwd_apply_gitlinks(db, ctx, state, writes, gitlink_changes, gitmodules_map) -> None
  ```

- [ ] **Step 1: Extract all three, preserving the call order**

`_fwd_close_renamed_old_paths` **must** run after `_fwd_apply_renamed_pairs` — it excludes idents the matcher already closed with `:renamed-to` linkage, and reversing them double-closes. State this in both docstrings, not just one.

`_fwd_close_renamed_old_paths` reads `writes.renamed_old_paths` (populated by Task 5's helper) and keeps its `list(state.file_entities.get(r_old_path, []))` copy — same mutation-during-iteration hazard as the D branch.

`_fwd_apply_gitlinks` keeps its three branches (`add` / `bump` / `remove`) and the comment recording that the `remove` case is sound only because real submodule paths are extensionless.

- [ ] **Step 2: Replace all three with calls, in the same order.**

- [ ] **Step 3: Run the parity gate** — Expected: `13 passed`.

- [ ] **Step 4: Run the oracle, four arms** — all `ok: true`. The synthetic repo carries the only gitlink coverage; treat a clean this-repo arm alone as proving nothing about `_fwd_apply_gitlinks`.

- [ ] **Step 5: Commit** (`Extract the three post-loop passes (#346)`).

---

### Task 10: Docstring, call-order contract, CLAUDE.md

**Files:**
- Modify: `mcp_server.py` — `_forward_apply`'s docstring.
- Modify: `CLAUDE.md` — the `_forward_apply` section (search for "positional-argument hazard", now rewritten by PR #360; the decomposition paragraph follows it).

- [ ] **Step 1: Rewrite `_forward_apply`'s docstring**

The existing five bullets are **wrong about the function's own structure**: they do not map onto the six flag sites, site 6 (checkpoint suppression) has no bullet at all, and one bullet ("D files, R files and gitlink changes are processed unchanged") describes behaviour the flag does not affect. The rewrite must:
- enumerate the six `lifecycle_only` sites and name where each lives now (site 1: the `writes` seed; sites 2-3: `_fwd_reconcile_and_build`; sites 4-6: the write tail);
- state the call-order contract from the spec: per-file dispatch order, `previous_idents` captured before the build, `_fwd_apply_renamed_pairs` before `_fwd_close_renamed_old_paths`, gitlinks after both, then the tail;
- keep #342's "all three watermarks move together" paragraph exactly where it is.

- [ ] **Step 2: Update CLAUDE.md**

Replace the "The decomposition itself was DECLINED and filed as #346" paragraph with what was actually built. It must record: the split axis is file status (not the flag); the two carriers; that `_fwd_reconcile_and_build` is indivisible and why; the call-order contract; that state mutation is **declared, not enforced** (approach A — do not overstate this, the spec is explicit); and the oracle plus its `PYTHONHASHSEED=0` requirement.

- [ ] **Step 3: Verify no test reads the changed docs**

`tests/test_mcp_server.py:29418` reads CLAUDE.md for examples. Run:
```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q -k "Example or skill_doc"
```
Expected: `2 passed`.

- [ ] **Step 4: Commit** (`Document the decomposition and its call-order contract (#346)`).

---

### Task 11: Final verification and the PR

**Files:** none modified.

- [ ] **Step 1: Run the full suite**

```bash
.venv/bin/python -m pytest tests/ -q
```
Expected: `2161 passed, 1 xfailed` — the same count as master `428ac7e`. **A pure refactor adds no tests**, so a different passing count is itself a finding worth reporting, not a number to update.

- [ ] **Step 2: Run the oracle on all four arms, final**

Against the Task 1 baselines. Record each arm's command count and `ok` in the PR body. An arm reporting `proved_nothing: true` has certified nothing.

- [ ] **Step 3: Measure the before/after shape**

Record in the PR body THREE reconciling figures, measured with `ast` (FunctionDef
span, minus the leading `Expr(Constant)` span for the docstring) — never `wc -l`,
which is what produced the wrong "594" this plan originally carried:
**executable body** (master 525 -> 171, the headline), **docstring** (67 -> 119,
grown by Task 10's own rewrite), and **def-to-end** (592 -> 290). State the
executable figure first: it is the real result and, unlike def-to-end, it does not
move when someone edits the prose. Also record the number of helpers extracted.
These are descriptive, not a gate.

- [ ] **Step 4: Scan every commit on the branch for closing keywords**

```bash
git log master..HEAD --format=%B | grep -niE "\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^#]*#[0-9]+"
```
Expected: no output. Re-run this after any commit added later — a scan done once before the push misses commits written after it.

- [ ] **Step 5: Push and open the PR**

Body must state: pure refactor; the oracle's four arms with counts; the `PYTHONHASHSEED=0` requirement and the self-comparison positive control; the residuals from the spec (mutation declared not enforced; the oracle proves equality only on the corpus it ran; performance unmeasured). Use "Part of #346", no closing keyword.

Then verify:
```bash
gh pr view --json closingIssuesReferences -q '.closingIssuesReferences'
```
Expected: `[]`.

- [ ] **Step 6: Report to the human and STOP**

Do not merge. Report the suite count, the four oracle arms, and the shape numbers. Merging needs the human — master requires an approving review, and the admin bypass is theirs to authorize.

---

## Self-Review

**Spec coverage:** Two carriers → Task 2. Two shared helpers → Task 3. Eight status handlers/passes → Tasks 4-9 (`_fwd_apply_deleted_file` 4, `_fwd_apply_renamed_head` 5, `_fwd_reconcile_and_build` 6, `_fwd_close_removed_children` 7, `_fwd_diff_dependencies` 8, the three passes 9). Flag flow → Task 6 + Task 10. Docstring correction → Task 10. Oracle with `PYTHONHASHSEED=0`, per-commit comparison, self-comparison control, synthetic corpus, both ratios → Tasks 1 and 11. Call-order contract → Tasks 7, 9, 10. Residuals → Task 11 step 5. No gaps.

**Correction found while writing:** the spec says "the `:depends-on` edge close sequence appears three times" and implies one helper covers all three. It does not: the M branch closes `previous_deps - current_deps` without popping, which is a different operation. Tasks 3 and 8 say so explicitly. The helper covers two sites.

**Type consistency:** `_FwdCommitCtx` and `_ForwardCommitWrites` field names are used identically in Tasks 2-9. Every helper takes `(db, ctx, state, writes, …)` except `_fwd_close_dep_edges` and `_fwd_diff_dependencies`, which take no `db` because they read only preloaded state — called out in both.
