# Ingestion Hardening (#222 phase 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close #222's last phase — fix the one remaining silent data-loss hole (frontier-low retained without its denominator), add divergent-ref orphan detection without repair, stop Stage B starving the auto-memory hooks, and clear the hygiene backlog.

**Architecture:** Four independent workstreams against `mcp_server.py` and `evals/at_scale/`. A is a read-side check at frontier load. B adds one fact (`:ingestion/branch`) plus a detector in `commit_census.py`. C restructures Stage B's lease into a bounded yield window. D is behaviour-neutral cleanup.

**Tech Stack:** Python 3.10–3.14, `minigraf>=2.0.0,<3.0.0`, pytest (real backend only), git subprocesses, SQLite FTS5 fact index.

**Spec:** `docs/superpowers/specs/2026-09-14-ingestion-hardening-design.md`

## Global Constraints

- **ALWAYS use `.venv/bin/python`.** System python has minigraf 1.1.1 against a `>=2.0.0` floor; it fakes ~122 test failures and misleads on timing.
- **Real backend only.** No `MagicMock` fake of `MiniGrafDb`. See `docs/testing-conventions.md` — `real_db` (in-memory) for single-handle tests, a real file-backed `MiniGrafDb.open()` against `tmp_path` for anything crossing an open/close cycle.
- **Single-handle invariant.** At most one live `MiniGrafDb` per process. Never open a handle where a lease is available; never hold a lease across a subprocess that also opens the graph.
- **Every regression test is ablation-proven.** Run the experiment: revert the fix, watch the test go red on its own defect-naming assertion, restore. A test that passes against the old code is not a regression test. The counterfactual must be the REAL old code, not a plausible reconstruction.
- **No closing keywords.** Every commit message and PR body says "Part of #222". `closingIssuesReferences` covers only body/title and a whole-message grep covers only commits — run both, and re-scan after every new commit. Negated keywords still close.
- **No `GRAPH_FORMAT_VERSION` bump and no migration** anywhere in this plan.
- **minigraf#287 workaround is permanent.** `:contains`, `:depends-on` and `:parent` are transacted ONE PER CALL. Never batch them.
- **#156 non-idempotency.** Re-transacting the same `(entity, attribute, value)` at a fresh valid-from creates a second live fact, not a no-op. Every repeated write diffs against the current live value first.
- **Existing full-suite baseline:** 2087 passed / 1 xfailed on master `49acbb1`. Any new failure is yours.

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `mcp_server.py` | `_frontier_load`'s frontier-low branch (A); `:ingestion/branch` write + schema (B); Stage B lease window (C); hygiene (D) | 2, 3, 6, 7, 8, 11, 12 |
| `evals/at_scale/commit_census.py` | Orphan detection, beside the census it already collects | 4 |
| `evals/at_scale/run_ingestion_benchmark.py` | Clause 9 wiring + baseline measurement | 5 |
| `tests/test_mcp_server.py` | A, B, C, D regression tests | 2, 3, 6, 7, 8, 9, 10, 11 |
| `tests/test_at_scale_commit_census.py` | Pure orphan-function tests | 4 |
| `docs/superpowers/plans/.../probe-*.md` | Task 1's throwaway measurement, recorded | 1 |

---

### Task 1: Prove the graft loss exists (measurement only, no fix)

The spec asserts this defect from code reading and explicitly refuses to act on it unmeasured. This task is the measurement. **If the loss does NOT reproduce, stop and report — Task 2 is then unjustified as written.**

**Files:**
- Create: `/tmp/claude-*/scratchpad/probe_graft_loss.py` (throwaway — do NOT commit to the repo)

**Interfaces:**
- Consumes: nothing.
- Produces: a recorded finding (reproduced / did not reproduce) plus the grafted commit hash and the frontier-low bounds observed. Task 2's test is built from this scenario.

- [ ] **Step 1: Write the probe**

```python
"""Throwaway probe: does a commit grafted below the forward frontier get lost?

Builds a repo, ingests it fully, then grafts an OLD ancestor in via a merge so
the new commit lands at a position INSIDE frontier-low's retained [lo, hi],
re-ingests, and asks whether that commit reached the graph.
"""
import asyncio, json, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def build(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.email", "t@t.com")
    git(repo, "config", "user.name", "T")
    for i in range(8):
        (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", f"c{i}")


def graft(repo):
    """Branch from an OLD commit, commit there, merge back. git log
    --topo-order places the side commit right after its branch point, i.e.
    INSIDE the already-ingested span, not appended above it."""
    base = git(repo, "rev-list", "--max-parents=0", "HEAD")
    git(repo, "checkout", "-b", "side", base)
    (repo / "grafted.py").write_text("def grafted():\n    return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "GRAFTED")
    grafted = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "master")
    git(repo, "merge", "--no-ff", "-m", "merge side", "side")
    return grafted


async def main():
    import mcp_server, frontier_registry
    tmp = Path(tempfile.mkdtemp())
    repo, graph = tmp / "repo", tmp / "g.graph"
    build(repo)
    mcp_server.open_db(str(graph))
    await mcp_server._run_ingestion(str(repo), "master")

    with mcp_server.db_lease() as db:
        before = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT)
        before_count = mcp_server._frontier_read_pos_count(db, mcp_server._FRONTIER_LOW_IDENT)

    grafted = graft(repo)
    lin = frontier_registry.build_linearization(str(repo), "master")
    await mcp_server._run_ingestion(str(repo), "master")

    with mcp_server.db_lease() as db:
        raw = mcp_server._db_execute(
            db, '(query [:find ?h :where [?e :entity-type :type/commit] [?e :hash ?h]])')
        hashes = {r[0] for r in json.loads(raw).get("results", [])}
        after = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT)

    print(json.dumps({
        "frontier_low_before": before,
        "pos_count_before": before_count,
        "frontier_low_after": after,
        "grafted_hash": grafted,
        "grafted_position": lin.index(grafted),
        "linearization_len": len(lin),
        "grafted_in_graph": grafted in hashes,
        "LOSS_REPRODUCED": grafted not in hashes,
    }, indent=2))

asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && .venv/bin/python <scratchpad>/probe_graft_loss.py`

Expected if the spec is right: `"LOSS_REPRODUCED": true`, with `grafted_position` strictly between frontier-low's `lo` and `hi` positions.

- [ ] **Step 3: Record the finding**

Paste the JSON into the task report. If `LOSS_REPRODUCED` is `false`, **STOP** and report — either the graft did not land inside the bounds (adjust the scenario and retry once) or the defect does not exist as described, which changes Task 2.

- [ ] **Step 4: No commit** — the probe is throwaway and stays out of the repo.

---

### Task 2: frontier-low retention check

**Files:**
- Modify: `mcp_server.py:6438-6444` (the frontier-low branch of `_frontier_load`)
- Test: `tests/test_mcp_server.py` (new class `TestFrontierLowRetentionCheck`)

**Interfaces:**
- Consumes: Task 1's confirmed scenario. Existing helpers `_frontier_read_bounds(db, ident) -> Optional[Tuple[str, str]]`, `_frontier_read_pos_count(db, ident) -> Optional[int]`, `_frontier_discard_interval(db, ident, bounds, index_con=None, pos_count=None, tag=None) -> None`.
- Produces: no new public names. `_frontier_load`'s returned allocator now excludes a frontier-low interval that fails the check.

- [ ] **Step 1: Write the failing test**

```python
class TestFrontierLowRetentionCheck:
    """#222 phase 5 item A. The authoritative interval was retained on bare
    hash bounds -- no lo<=hi guard and no :pos-count check -- while every
    provisional interval gets all three via _load_one_interval. A commit
    grafted below the forward frontier lands INSIDE the retained span, is
    excluded from _unclaimed()'s complement, and is never walked by anyone.
    Every detector reads clean: fact_audit's two witnesses agree (neither
    holds it), both :introduced-by checks only examine entities that EXIST,
    and stderr carries nothing."""

    def _git(self, repo, *args):
        return _subprocess.run(["git", *args], cwd=repo, check=True,
                               capture_output=True, text=True).stdout.strip()

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-b", "master")
        self._git(repo, "config", "user.email", "t@t.com")
        self._git(repo, "config", "user.name", "T")
        for i in range(8):
            (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-m", f"c{i}")
        return repo

    def _graft(self, repo):
        """Branch from the ROOT, commit, merge the MAINLINE INTO the side
        branch, then fast-forward master onto the side branch's tip.

        MEASURED IN TASK 1 -- do not "simplify" this back to the obvious
        recipe. Branching off an old commit and then
        `git checkout master && git merge --no-ff side` does NOT place the
        side commit "right after its branch point": measured, it lands
        SECOND-TO-LAST (position 8 of 10). The merge's FIRST parent is
        master's own tip, so `git log --topo-order` exhausts the entire
        original mainline before the second parent's exclusive ancestors
        become due, and the side commit surfaces just before the merge
        regardless of how old its branch point was. Backdating the grafted
        commit's author and committer dates does not change this (tested).

        Reversing which side is the first parent is what works: merge
        master's tip INTO `side`, so GRAFTED's descendant chain is the
        merge's first parent, then fast-forward master onto it. GRAFTED
        then surfaces at position 1 -- strictly inside frontier-low's
        [0, 4] -- which is CLAUDE.md's own description of the hazard read
        literally ("branch off an old commit, merge the mainline in,
        fast-forward the mainline")."""
        base = self._git(repo, "rev-list", "--max-parents=0", "HEAD")
        mainline_tip = self._git(repo, "rev-parse", "master")
        self._git(repo, "checkout", "-b", "side", base)
        (repo / "grafted.py").write_text("def grafted():\n    return 1\n")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", "GRAFTED")
        grafted = self._git(repo, "rev-parse", "HEAD")
        # Mainline INTO side, so side stays the first parent.
        self._git(repo, "merge", "--no-ff", "-m", "merge mainline into side",
                  mainline_tip)
        self._git(repo, "checkout", "master")
        self._git(repo, "merge", "--ff-only", "side")
        return grafted

    def _commit_hashes(self, db):
        import mcp_server
        raw = mcp_server._db_execute(
            db, '(query [:find ?h :where [?e :entity-type :type/commit] [?e :hash ?h]])')
        return {r[0] for r in json.loads(raw).get("results", [])}

    @pytest.mark.asyncio
    async def test_commit_grafted_inside_frontier_low_is_still_walked(self, tmp_path):
        import mcp_server
        repo = self._repo(tmp_path)
        mcp_server.open_db(str(tmp_path / "g.graph"))
        await mcp_server._run_ingestion(str(repo), "master")

        with mcp_server.db_lease() as db:
            bounds = mcp_server._frontier_read_bounds(
                db, mcp_server._FRONTIER_LOW_IDENT)

        grafted = self._graft(repo)
        lin = frontier_registry.build_linearization(str(repo), "master")
        await mcp_server._run_ingestion(str(repo), "master")

        with mcp_server.db_lease() as db:
            hashes = self._commit_hashes(db)

        # POSITIVE CONTROL, and it is load-bearing -- assert it BEFORE the
        # real assertion. This test is vacuous unless the grafted commit
        # actually lands strictly inside frontier-low's retained span: a
        # graft that lands at the TIP is walked normally by any code, fixed
        # or not, so `grafted in hashes` would pass without the fix and the
        # test would guard nothing. Task 1 measured exactly that failure --
        # the obvious graft recipe put the commit at position 8 of 10.
        lo_pos, hi_pos = lin.index(bounds[0]), lin.index(bounds[1])
        grafted_pos = lin.index(grafted)
        assert lo_pos < grafted_pos < hi_pos, (
            f"the graft landed at position {grafted_pos}, not strictly inside "
            f"frontier-low's retained [{lo_pos}, {hi_pos}] -- this test proves "
            f"nothing in that state, whatever the assertion below does"
        )

        assert grafted in hashes, (
            f"the grafted commit at position {grafted_pos} of {len(lin)} never "
            f"reached the graph -- frontier-low was retained over a span it "
            f"was never claimed under, so the position was excluded from "
            f"_unclaimed()'s complement and handed to no stream"
        )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLowRetentionCheck -v`
Expected: FAIL on the defect-naming assertion — the grafted hash is absent.

- [ ] **Step 3: Implement the check**

Replace `mcp_server.py:6438-6444` with:

```python
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is not None:
        low_lo = hash_to_pos.get(low_bounds[0])
        low_hi = hash_to_pos.get(low_bounds[1])
        low_count = _frontier_read_pos_count(db, _FRONTIER_LOW_IDENT)
        # #222 phase 5 item A. The same three conditions _load_one_interval
        # demands of every PROVISIONAL interval, applied to the authoritative
        # one, which had only the bounds-resolve test. A commit grafted below
        # the forward frontier (#222's "merge grafts old history" edge case)
        # leaves both hash bounds resolving while the SPAN between them grows
        # -- and FrontierAllocator._unclaimed() is the complement of the
        # interval set, so every position inside it is handed to no stream and
        # silently never walked.
        #
        # An interval carrying NO :pos-count is not retained either: "no
        # denominator" and "a denominator that still checks out" must not be
        # the same branch when the failure mode is silent permanent loss.
        # _frontier_pos_count_delta maintains it on the from_low path, so any
        # graph that has taken a forward claim since #326 carries one.
        #
        # Discarded rather than routed through _load_one_interval, which also
        # ARCHIVES a :type/completed-region -- regions are consumed by
        # _skip_claim, which honours PROVISIONAL regions only. The cost of a
        # discard is a forward re-walk from C0: expensive, never lossy.
        if (
            low_lo is not None
            and low_hi is not None
            and low_lo <= low_hi
            and low_count == low_hi - low_lo + 1
        ):
            intervals.append(frontier_registry.Interval(
                low_lo, low_hi,
                frontier_registry.TAG_AUTHORITATIVE, anchor_pos=0, is_base=True,
                ident=_FRONTIER_LOW_IDENT,
            ))
        else:
            _frontier_discard_interval(
                db, _FRONTIER_LOW_IDENT, low_bounds,
                index_con=index_con, pos_count=low_count,
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLowRetentionCheck -v`
Expected: PASS.

- [ ] **Step 5: Ablate**

Temporarily restore the old two-condition branch, re-run the test, confirm it goes RED on the defect-naming assertion, then restore the fix. Record both outputs in the task report.

- [ ] **Step 6: Run the frontier and ingestion suites**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -k "frontier or ingest or sweep" -q`
Expected: no new failures against the 2087/1-xfailed baseline. A retention-related test that now fails is a genuine finding — report it, do not "fix" it by weakening the check.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Check frontier-low's :pos-count at load (part of #222 phase 5)

The authoritative interval was retained on bare hash bounds, with no
lo<=hi guard and no :pos-count check, while every provisional interval
gets all three from _load_one_interval. A commit grafted below the
forward frontier leaves both bounds resolving while the span grows, so
the position falls inside the retained interval, is excluded from
_unclaimed()'s complement, and is never walked.

Reproduced first: <paste grafted position / linearization length>.
Ablation: reverting to the two-condition branch reddens the test.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 3: Record the ingested branch (`:ingestion/branch`)

**Files:**
- Modify: `mcp_server.py` — new `_ingestion_branch_write` near `_last_run_write` (~`:8299`); `MINIGRAF_SCHEMA["ingestion"]["optional"]` (~`:8419`); call site in `_run_ingestion` (~`:13210`, right after `_graph_format_version_stamp_if_new`)
- Test: `tests/test_mcp_server.py` (new class `TestIngestionBranchFact`)

**Interfaces:**
- Consumes: `_transact`, `_retract`, `_db_execute`, `_edn_escape`.
- Produces: `_ingestion_branch_write(db, branch: str, run_ts_iso: str, index_con=None) -> None` and `_ingestion_branch_read(db) -> Optional[str]`. Task 4 and Task 6 both consume `_ingestion_branch_read`.

- [ ] **Step 1: Write the failing tests**

```python
class TestIngestionBranchFact:
    """#222 phase 5 item B. The graph records no ref, so "a :type/commit
    entity absent from the linearization" cannot distinguish a force-push
    orphan from a second branch ingested into the same graph. This fact is
    the discriminator that makes the orphan count interpretable."""

    def test_write_then_read_roundtrips(self, real_db):
        import mcp_server
        mcp_server._ingestion_branch_write(real_db, "master", "2026-09-14T00:00:00.000Z")
        assert mcp_server._ingestion_branch_read(real_db) == "master"

    def test_absent_reads_none(self, real_db):
        import mcp_server
        assert mcp_server._ingestion_branch_read(real_db) is None

    def test_rewriting_the_same_branch_creates_no_duplicate(self, real_db):
        """#156: re-transacting the same (entity, attribute, value) at a fresh
        valid-from creates a second LIVE fact, not a no-op. The diff is what
        stops an unbounded pile-up across runs."""
        import mcp_server
        for ts in ("2026-09-14T00:00:00.000Z", "2026-09-14T00:00:01.000Z"):
            mcp_server._ingestion_branch_write(real_db, "master", ts)
        raw = mcp_server._db_execute(
            real_db, "(query [:find ?b :where [:ingestion/branch :branch ?b]])")
        assert len(json.loads(raw).get("results", [])) == 1

    def test_switching_branch_replaces_the_value(self, real_db):
        import mcp_server
        mcp_server._ingestion_branch_write(real_db, "master", "2026-09-14T00:00:00.000Z")
        mcp_server._ingestion_branch_write(real_db, "develop", "2026-09-14T00:00:01.000Z")
        assert mcp_server._ingestion_branch_read(real_db) == "develop"
        raw = mcp_server._db_execute(
            real_db, "(query [:find ?b :where [:ingestion/branch :branch ?b]])")
        assert len(json.loads(raw).get("results", [])) == 1

    def test_audit_does_not_retract_the_branch_fact(self, real_db):
        """handle_minigraf_audit iterates every REGISTERED type and retracts
        any attribute outside its allowed set, querying the live graph
        directly. `ingestion` is registered, so :branch must be listed in
        MINIGRAF_SCHEMA or an audit run silently deletes the discriminator.
        The :version entry carries a comment saying exactly this; this is the
        second instance of the same trap."""
        import mcp_server
        mcp_server._ingestion_branch_write(real_db, "master", "2026-09-14T00:00:00.000Z")
        mcp_server.handle_minigraf_audit()
        assert mcp_server._ingestion_branch_read(real_db) == "master"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestionBranchFact -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_ingestion_branch_write'`.

- [ ] **Step 3: Implement the helpers**

Add next to `_last_run_write` in `mcp_server.py`:

```python
_INGESTION_BRANCH_IDENT = ":ingestion/branch"


def _ingestion_branch_read(db: Any) -> Optional[str]:
    """The ref this graph was last ingested against, or None if it predates
    #222 phase 5 (or no run has completed its first write yet).

    None is NOT "master" and must never be defaulted to one: the orphan check
    reads an absent branch as "proved nothing", which is the honest answer for
    a graph that never recorded one.
    """
    raw = _db_execute(
        db, f"(query [:find ?b :where [{_INGESTION_BRANCH_IDENT} :branch ?b]])"
    )
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _ingestion_branch_write(
    db: Any, branch: str, run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the ref this run is walking. Written on EVERY run -- the branch
    can change between runs -- which is why it is not a stamp-if-new like
    _graph_format_version_stamp_if_new.

    Value-diffed before writing, exactly as _last_run_write and _ingest_tags
    do: minigraf is NOT idempotent at the graph level for re-transacting the
    same (entity, attribute, value) at a fresh valid-from (#156), so an
    unconditional re-transact accumulates a duplicate live fact per run.

    Not folded into :ingestion/last-run-at, which is written only under
    `if completed_all:` -- an interrupted run would record no branch, and the
    orphan check needs the discriminator MORE on an interrupted graph, not
    less.
    """
    current = _ingestion_branch_read(db)
    if current == branch:
        return
    desired = {
        ":entity-type": ":type/ingestion",
        ":ident": _INGESTION_BRANCH_IDENT,
        ":description": "ref this graph was last ingested against",
        ":branch": branch,
    }
    to_retract: List[str] = []
    to_transact: List[str] = []
    raw = _db_execute(
        db, f"(query [:find ?a ?v :where [{_INGESTION_BRANCH_IDENT} ?a ?v]])"
    )
    live: Dict[str, Any] = dict(json.loads(raw).get("results", []))
    for attr, value in desired.items():
        if live.get(attr) == value:
            continue
        rendered = value if attr == ":entity-type" else f'"{_edn_escape(value)}"'
        if attr in live:
            old = live[attr]
            old_rendered = old if attr == ":entity-type" else f'"{_edn_escape(old)}"'
            to_retract.append(f"[{_INGESTION_BRANCH_IDENT} {attr} {old_rendered}]")
        to_transact.append(f"[{_INGESTION_BRANCH_IDENT} {attr} {rendered}]")
    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    if to_transact:
        _transact(db, "[" + " ".join(to_transact) + "]", run_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Register `:branch` in the schema**

In `MINIGRAF_SCHEMA["ingestion"]["optional"]`, add `":branch": str` and extend the existing comment:

```python
    "ingestion": {
        "required": {":description": str},
        # :version carries the graph format version (#263). It MUST stay listed
        # here: minigraf_audit iterates every registered type and retracts any
        # attribute outside its allowed set, querying the live graph directly,
        # so dropping this line makes an audit run silently delete the very
        # stamp that protects the graph from being read under the wrong ident
        # rule. :branch (#222 phase 5) is load-bearing for the same reason --
        # it is the discriminator that tells a force-push orphan apart from a
        # second branch ingested into the same graph, and an audit that
        # retracted it would make every orphan count uninterpretable.
        "optional": {":hash": str, ":alias": str, ":last-run-at": str, ":last-commit": str,
                     ":total-ingested": int, ":version": int, ":branch": str},
    },
```

- [ ] **Step 5: Call it from `_run_ingestion`**

Immediately after the `_graph_format_version_stamp_if_new` call (~`:13210`), inside the same lease:

```python
            # #222 phase 5 item B. AFTER the format stamp, never before --
            # defensive, not load-bearing: _graph_has_ingestion_state's
            # disjunction is exactly three reads (_watermark_query and
            # _frontier_read_bounds on each fixed frontier), so this fact is
            # invisible to it and the stamp fires correctly either way.
            # Keeping the stamp unambiguously first costs nothing.
            #
            # NEVER add :ingestion/branch to _graph_has_ingestion_state. This
            # is written before any walk, so a run that recorded a branch and
            # then died would afterwards read as "already ingested" --
            # suppressing its own stamp, then being refused by
            # _graph_format_version_verify as a state-present/stamp-absent
            # pre-#263 graph. That condemns a graph holding no ingested data.
            await loop.run_in_executor(
                write_executor, _ingestion_branch_write, db, branch, run_ts_iso, index_con,
            )
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestionBranchFact -v`
Expected: all PASS.

- [ ] **Step 7: Ablate the schema entry**

Remove `":branch": str` from the schema, run `test_audit_does_not_retract_the_branch_fact`, confirm RED, restore. This is the whole point of that test — record the output.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Record the ingested ref as :ingestion/branch (part of #222 phase 5)

The graph recorded no ref, so a :type/commit entity absent from the
current linearization could not be told apart from a force-push orphan
or a second branch ingested into the same graph. Written every run
(the branch can change), value-diffed against the live fact so #156
does not pile up a duplicate per run, and registered in
MINIGRAF_SCHEMA so minigraf_audit does not retract it.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 4: Orphan detection in `commit_census.py`

**Files:**
- Modify: `evals/at_scale/commit_census.py` — add `orphaned_commits` (pure) and extend `collect_commit_census`
- Modify: `tests/test_at_scale_commit_census.py` (it already exists — add a class, do not create a parallel file)

**Interfaces:**
- Consumes: `_ingestion_branch_read` (Task 3).
- Produces: `orphaned_commits(graph_hashes: set[str], repo_hashes: set[str], recorded_branch: Optional[str], audited_ref: str, sample_cap: int = 20) -> dict` returning `{"entities", "commit_entities_scanned", "sample", "recorded_branch", "audited_ref", "proved_nothing"}`; and `collect_commit_census(...)` gains an `orphaned_commits` key in its result dict. Task 5 consumes both.

- [ ] **Step 1: Write the failing tests**

```python
from evals.at_scale.commit_census import orphaned_commits


class TestOrphanedCommits:
    """#222 phase 5 item B. A force-push leaves :type/commit entities for
    commits no longer in history, with every :introduced-by / :modified-in /
    :parent / :tagged-commit reference to them still live."""

    def test_clean_graph_reports_zero_with_a_denominator(self):
        r = orphaned_commits({"a", "b"}, {"a", "b"}, "master", "master")
        assert r["entities"] == 0
        assert r["commit_entities_scanned"] == 2
        assert r["proved_nothing"] is False

    def test_rewritten_commit_is_reported(self):
        r = orphaned_commits({"a", "dead"}, {"a", "new"}, "master", "master")
        assert r["entities"] == 1
        assert r["sample"] == ["dead"]

    def test_branch_mismatch_proves_nothing_and_reports_no_count(self):
        """The decisive false-positive guard. A graph ingested against
        `develop` and audited against `master` legitimately holds commits
        absent from master's linearization -- condemning it would delete a
        real branch's history. #316's denominator idiom: report, never gate."""
        r = orphaned_commits({"a", "b"}, {"a"}, "develop", "master")
        assert r["proved_nothing"] is True
        assert r["entities"] == 0

    def test_absent_branch_proves_nothing(self):
        """A graph predating :ingestion/branch cannot be retro-audited."""
        r = orphaned_commits({"a", "b"}, {"a"}, None, "master")
        assert r["proved_nothing"] is True
        assert r["entities"] == 0

    def test_empty_graph_proves_nothing(self):
        """A check that scanned no commit entities also reports 0."""
        r = orphaned_commits(set(), {"a"}, "master", "master")
        assert r["proved_nothing"] is True
        assert r["commit_entities_scanned"] == 0

    def test_sample_is_capped_and_sorted(self):
        dead = {f"d{i:03d}" for i in range(50)}
        r = orphaned_commits(dead, set(), "master", "master", sample_cap=3)
        assert r["entities"] == 50
        assert r["sample"] == sorted(dead)[:3]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_commit_census.py::TestOrphanedCommits -v`
Expected: FAIL with `ImportError: cannot import name 'orphaned_commits'`.

- [ ] **Step 3: Implement the pure function**

```python
def orphaned_commits(
    graph_hashes: set[str],
    repo_hashes: set[str],
    recorded_branch: Optional[str],
    audited_ref: str,
    sample_cap: int = _ORPHAN_SAMPLE_CAP,
) -> dict[str, Any]:
    """`:type/commit` entities holding a hash the ref's history no longer
    contains (#222 phase 5).

    DETECTION ONLY -- there is deliberately no repair. The standing decision
    throughout this arc is that an affected graph is REBUILT into a fresh
    graph path, never migrated and never repaired in place; #329 established
    separately that shipping a detector does not violate a scope decision
    that excluded repair.

    BESIDE fact_audit's scan rather than riding it, for the reason
    collect_commit_census already states about itself: this holds a reference
    the graph did not produce -- the repo -- and fact_audit deliberately takes
    no repo handle.

    `proved_nothing` IS THE POSITIVE CONTROL, and here it guards a false
    positive with teeth. The graph records no per-commit branch, so a commit
    entity absent from THIS ref's history is either a force-push orphan or a
    commit from ANOTHER branch ingested into the same graph -- indistinguishable
    without `:ingestion/branch`. When the recorded branch is absent or does not
    match the audited ref, the count is not reported as a finding: it would
    condemn a legitimately ingested branch's entire history. That is the
    `:type/external-dependency` trap of #316 exactly, and the fix is the same
    one -- ship the denominator, and refuse to read a number whose denominator
    was never established.

    A graph holding no commit entities reports `proved_nothing` too: a check
    that matched nothing also reports 0.
    """
    scanned = len(graph_hashes)
    interpretable = recorded_branch is not None and recorded_branch == audited_ref
    if not interpretable or scanned == 0:
        return {
            "entities": 0,
            "commit_entities_scanned": scanned,
            "sample": [],
            "recorded_branch": recorded_branch,
            "audited_ref": audited_ref,
            "proved_nothing": True,
        }
    orphans = sorted(graph_hashes - repo_hashes)
    return {
        "entities": len(orphans),
        "commit_entities_scanned": scanned,
        "sample": orphans[:sample_cap],
        "recorded_branch": recorded_branch,
        "audited_ref": audited_ref,
        "proved_nothing": False,
    }
```

Add at module level: `_ORPHAN_SAMPLE_CAP = 20`.

- [ ] **Step 4: Add the two collectors and wire them in**

```python
def repo_commit_hashes(repo_path: str, ref: str) -> set[str]:
    """Every full commit hash reachable from `ref`.

    A THIRD subprocess rather than a reuse of repo_commit_counts' second one:
    that function returns 12-char PREFIXES for the ident-collision check, and
    the orphan check compares FULL hashes (the graph stores the full hash in
    :hash). Deriving one from the other would silently make an orphan check
    prefix-sensitive.
    """
    out = subprocess.run(
        ["git", "rev-list", ref],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return {line for line in out.stdout.split() if line}


def graph_commit_hashes(db: Any) -> set[str]:
    """Every :hash value on a live :type/commit entity."""
    import json as _json
    import mcp_server
    raw = mcp_server._db_execute(
        db,
        "(query [:find ?h :where [?e :entity-type :type/commit] [?e :hash ?h]])",
    )
    return {r[0] for r in _json.loads(raw).get("results", []) if r}
```

In `collect_commit_census`, inside the existing `try:` after `graph_count = graph_commit_entities(db)`:

```python
        repo_hashes = repo_commit_hashes(repo_path, ref)
        graph_hashes = graph_commit_hashes(db)
        recorded_branch = _recorded_branch(db)
```

with a module-level helper:

```python
def _recorded_branch(db: Any) -> Optional[str]:
    import mcp_server
    return mcp_server._ingestion_branch_read(db)
```

Initialise `repo_hashes: set[str] = set()`, `graph_hashes: set[str] = set()`, `recorded_branch: Optional[str] = None` above the `try`, and after the `commit_census(...)` call add:

```python
    # Reported under its own key and NOT folded into `ok`'s deltas: an orphan
    # is a graph holding MORE than the repo, which drives repo_vs_graph
    # NEGATIVE and matches none of the three delta diagnoses. It is gated
    # separately in run_ingestion_benchmark._exit_code (clause 9).
    result["orphaned_commits"] = orphaned_commits(
        graph_hashes, repo_hashes, recorded_branch, ref,
    )
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_at_scale_commit_census.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/commit_census.py tests/test_at_scale_commit_census.py
git commit -m "Detect divergent-ref orphaned commits (part of #222 phase 5)

Commits rewritten out of history keep their :type/commit entities and
every reference to them. Detection only -- no repair, per the standing
decision that an affected graph is rebuilt into a fresh path.

Lives beside the census rather than riding fact_audit's scan, for the
reason that module already documents: it holds a reference the graph
did not produce. Gated on the recorded branch matching the audited ref,
because without that discriminator a second ingested branch's history
is indistinguishable from a force-push orphan.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 5: Gate it (clause 9) and measure the clean baseline

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py` — `_exit_code`
- Test: `tests/test_at_scale_ingestion_benchmark.py` (where `_exit_code`'s existing clause tests live — add these beside them)

**Interfaces:**
- Consumes: `collect_commit_census`'s `orphaned_commits` key (Task 4).
- Produces: no new names; `_exit_code` gains one clause.

- [ ] **Step 1: Write the failing tests**

```python
def test_exit_code_fails_on_orphaned_commits():
    assert _exit_code({
        "commit_census": {"ok": True, "orphaned_commits": {
            "entities": 3, "proved_nothing": False}},
    }) == 1


def test_exit_code_ignores_orphans_when_nothing_was_proved():
    """A branch mismatch or an absent :ingestion/branch means the count is
    uninterpretable, not clean-with-findings."""
    assert _exit_code({
        "commit_census": {"ok": True, "orphaned_commits": {
            "entities": 7, "proved_nothing": True}},
    }) == 0


def test_exit_code_clean_when_key_absent():
    """A metrics file from a harness predating this check cannot be
    retro-failed -- same precedent as stderr_capture_complete and fact_audit."""
    assert _exit_code({"commit_census": {"ok": True}}) == 0
```

- [ ] **Step 2: Run and watch fail**

Run: `.venv/bin/python -m pytest tests/ -k exit_code -v`
Expected: `test_exit_code_fails_on_orphaned_commits` FAILS (returns 0).

- [ ] **Step 3: Add clause 9**

Immediately before `return 0` in `_exit_code`:

```python
    # Clause 9 (#222 phase 5). Orphaned commit entities: the graph holds
    # history the ref no longer contains, after a force-push or rebase. Not
    # foldable into clause 8 -- an orphan drives repo_vs_graph NEGATIVE, which
    # matches none of the census's three delta diagnoses, and `ok` stays True.
    #
    # Gated ONLY when the check could interpret its own number.
    # `proved_nothing` is true when the graph records no :ingestion/branch, or
    # records one that is not the ref being audited: a second branch ingested
    # into the same graph legitimately holds commits absent from this ref's
    # history, and failing on that would condemn real data. The count still
    # SHIPS in both cases -- not gated is not unmeasured.
    #
    # `.get()` throughout, so a metrics file from a harness predating this
    # check stays clean, matching every clause above.
    orphans = (metrics.get("commit_census") or {}).get("orphaned_commits") or {}
    if not orphans.get("proved_nothing") and orphans.get("entities"):
        return 1
    return 0
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -k exit_code -v`
Expected: all PASS.

- [ ] **Step 5: MEASURE the clean baseline before trusting the gate**

Run the at-scale ingestion against this repo and read the census:

```bash
.venv/bin/python evals/at_scale/run_ingestion_benchmark.py \
  --repo-path . --graph-path /tmp/orphan-baseline.graph 2>&1 | tail -40
```

Record `orphaned_commits` verbatim. **The expected clean reading is
`entities: 0`, `proved_nothing: false`, and `commit_entities_scanned` equal to
the repo's commit count.** If `proved_nothing` is `true` on a fresh
ingestion-only graph, the branch fact is not being written or not matching the
audited ref — that is a Task 3 bug, not an acceptable baseline. Do not wire
the gate until the denominator is non-zero and `proved_nothing` is false.

- [ ] **Step 6: Commit with the measurement in the message**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/
git commit -m "Gate on orphaned commits as clause 9 (part of #222 phase 5)

Measured clean first, per the standing rule for a zero-tolerance gate:
<paste entities / commit_entities_scanned / proved_nothing>.

Gated only when the recorded branch matches the audited ref; the count
ships either way, because not gated is not unmeasured.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 6: Surface the orphan count in ingest status

**Files:**
- Modify: `mcp_server.py` — `_run_ingestion` (compute once) and the `_ingest_progress` initialisation in `handle_minigraf_ingest_git`
- Test: `tests/test_mcp_server.py` (extend `TestIngestionBranchFact` or a new class)

**Interfaces:**
- Consumes: `_ingestion_branch_read` (Task 3), `linearization` (already local to `_run_ingestion`).
- Produces: `_ingest_progress["orphaned_commits"]` — an int, or None when nothing was proved.

- [ ] **Step 1: Write the failing test**

```python
    @pytest.mark.asyncio
    async def test_status_reports_orphans_after_a_rewrite(self, tmp_path):
        """Computed ONCE during the run, never queried at poll time: phase 4
        settled that status is not derived from graph queries at poll time
        (lock contention, added latency, staler than the in-memory state)."""
        import mcp_server
        divergent = TestDivergentRefEndToEnd()
        repo = divergent._repo(tmp_path, 8)
        mcp_server.open_db(str(tmp_path / "g.graph"))
        await mcp_server._run_ingestion(str(repo), "master")

        head = _subprocess.run(["git", "rev-parse", "HEAD~3"], cwd=repo,
                               check=True, capture_output=True, text=True).stdout.strip()
        divergent._rewrite_from(repo, head)
        await mcp_server._run_ingestion(str(repo), "master")

        status = mcp_server.handle_minigraf_ingest_status()
        assert status["orphaned_commits"] > 0, (
            "a rewrite left commit entities the ref no longer contains, and "
            "status reported none"
        )
```

- [ ] **Step 2: Run and watch fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -k test_status_reports_orphans -v`
Expected: FAIL with `KeyError: 'orphaned_commits'`.

- [ ] **Step 3: Compute it once in `_run_ingestion`**

After `_ingestion_branch_write` (Task 3's call site), still inside that lease:

```python
            # #222 phase 5 item B. Computed ONCE here, where the linearization
            # and a lease are both already in hand, and stored as a plain
            # _ingest_progress key -- NOT queried at poll time (phase 4:
            # status is never derived from graph queries at poll time, which
            # contends on _db_native_lock and is staler than memory anyway),
            # and NOT put on RunProgress, which is deliberately pure (no DB,
            # no git, injected clocks).
            #
            # None, never 0, when the previously-recorded branch does not
            # match this run's ref: the graph carries no per-commit branch, so
            # commits from another ingested branch are indistinguishable from
            # a rewrite's leftovers. A 0 there would read as "verified clean".
            prior_branch = await loop.run_in_executor(
                write_executor, _ingestion_branch_read, db,
            )
            _ingest_progress["orphaned_commits"] = await loop.run_in_executor(
                write_executor, _orphaned_commit_count, db, linearization, prior_branch, branch,
            )
```

**Order matters:** read `prior_branch` BEFORE `_ingestion_branch_write` overwrites it, or every run compares against its own ref and always reports interpretable. Move the `_ingestion_branch_read` call above the write, and pass the value down.

Add the helper next to `_ingestion_branch_read`:

```python
def _orphaned_commit_count(
    db: Any, linearization: List[str], recorded_branch: Optional[str], ref: str
) -> Optional[int]:
    """How many live :type/commit entities hold a hash this ref's history no
    longer contains, or None when that question cannot be answered.

    None when the graph recorded no branch, or recorded a different one: those
    commits may belong to another ingested branch, and reporting a count would
    invite a reader to treat real history as garbage. Detection only -- nothing
    here retracts anything.
    """
    if recorded_branch is None or recorded_branch != ref:
        return None
    raw = _db_execute(
        db, "(query [:find ?h :where [?e :entity-type :type/commit] [?e :hash ?h]])"
    )
    graph_hashes = {r[0] for r in json.loads(raw).get("results", []) if r}
    if not graph_hashes:
        return None
    return len(graph_hashes - set(linearization))
```

- [ ] **Step 4: Seed the key in `handle_minigraf_ingest_git`**

In the `_ingest_progress = {...}` initialisation, add `"orphaned_commits": None,`.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -k "orphan or IngestionBranch" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Report orphaned commit count in ingest status (part of #222 phase 5)

Computed once per run where the linearization and a lease are already
held, not queried at poll time (phase 4's rule) and not placed on
RunProgress, which is deliberately pure. None rather than 0 when the
recorded branch does not match the run's ref.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 7: Stage B bounded yield window

**Files:**
- Modify: `mcp_server.py` — Stage B in `_run_ingestion` (~`:13969` lease open through ~`:14100`)
- Test: `tests/test_mcp_server.py` (new class `TestStageBYieldsTheLock`)

**Interfaces:**
- Consumes: nothing new.
- Produces: module constants `_SWEEP_YIELD_COMMITS: int` and `_SWEEP_YIELD_SECONDS: float`, both env-overridable.

- [ ] **Step 1: Write the failing test**

```python
class TestStageBYieldsTheLock:
    """#222 phase 5 item C. Stage B held ONE lease across the whole sweep.
    A lease is cheap in-process but EXCLUSIVE out-of-process, and both
    auto-memory hooks are `command` hooks in separate processes with a 0.75 s
    retry budget and `except Exception: pass` -- so the whole-sweep hold did
    not block queries, it SILENTLY DISCARDED every auto-memory write for the
    sweep's duration."""

    @pytest.mark.asyncio
    async def test_another_process_can_open_the_graph_during_the_sweep(
        self, tmp_path, monkeypatch
    ):
        """Never assert on a .lock FILE: that is a tautology under minigraf
        2.0.0 (the sidecar is gone; locking is kernel flock). _another_process_
        can_open actually spawns a process and tries."""
        import mcp_server
        repo = TestDivergentRefEndToEnd()._repo(tmp_path, 12)
        graph = tmp_path / "g.graph"
        mcp_server.open_db(str(graph))

        observed = []
        real_apply = mcp_server._correction_sweep_apply

        def spy(*a, **kw):
            observed.append(_another_process_can_open(str(graph)))
            return real_apply(*a, **kw)

        monkeypatch.setattr(mcp_server, "_correction_sweep_apply", spy)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 1)
        await mcp_server._run_ingestion(str(repo), "master")

        assert observed, "the sweep never ran, so this proved nothing"
        assert any(observed), (
            "no other process could open the graph at any point during the "
            "sweep -- the lease is still held across the whole of Stage B"
        )
```

- [ ] **Step 2: Run and watch fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestStageBYieldsTheLock -v`
Expected: FAIL — `any(observed)` is False, and `_SWEEP_YIELD_COMMITS` does not exist yet.

- [ ] **Step 3: Add the constants**

Near the other ingestion env constants in `mcp_server.py`:

```python
# #222 phase 5 item C. Stage B releases its lease every _SWEEP_YIELD_COMMITS
# swept commits or _SWEEP_YIELD_SECONDS, whichever comes first, so the
# out-of-process auto-memory hooks can win the graph file lock.
#
# NOT a per-commit release: _DbLeaseManager.release() at refcount 1 -> 0 drops
# the handle, and minigraf's `Drop for Inner` then runs a full O(graph size)
# checkpoint -- #280, measured at 47.3% of Stage A's write time and growing
# 3.47x within a 220-commit run. A window amortises that over N commits.
#
# _SWEEP_YIELD_SECONDS is sized against the hooks' own retry budget
# (_LOCK_RETRY_MAX x _LOCK_RETRY_BASE doubling = 0.75 s total): the lock must
# come free often enough that a hook already retrying can win it.
#
# When #280 lands (blocked on upstream minigraf#322), the drop checkpoint is
# suppressed and N can safely go to 1 -- which is why this is a constant to
# lower rather than a structure to rewrite.
_SWEEP_YIELD_COMMITS = int(os.environ.get("MINIGRAF_SWEEP_YIELD_COMMITS", "25"))
_SWEEP_YIELD_SECONDS = float(os.environ.get("MINIGRAF_SWEEP_YIELD_SECONDS", "2.0"))
```

- [ ] **Step 4: Restructure Stage B's lease**

Wrap the existing `while` body so the lease is re-entered per window. The loop becomes an outer "window" loop; `sweep_fragmented`, `skipped` and `nxt` are hoisted ABOVE it so they survive across windows.

```python
                if completed_all:
                    _ingest_progress["phase"] = "sweeping"
                    hash_to_pos = {h: i for i, h in enumerate(linearization)}
                    sweep_pos_by_commit_ident = {
                        f":commit/{h[:12]}": i
                        for i, (h, _t, _a, _s) in enumerate(commit_metadata)
                    }
                    skipped = 0
                    sweep_fragmented = None   # computed once, on the first window
                    nxt = None
                    sweep_done = False
                    while not sweep_done and not _shutdown_requested.is_set():
                        window_started = time.monotonic()
                        window_count = 0
                        async with db_lease_async() as db:
                            if sweep_fragmented is None:
                                # #325 review Finding 3: computed ONCE for the
                                # whole sweep, not per window and not per
                                # commit. Stage B only starts once the gap is
                                # fully claimed, and nothing in this loop
                                # writes an interval fact, so fragmentation
                                # cannot change mid-sweep. Recomputing it per
                                # window would restore a cost that finding
                                # removed.
                                sweep_fragmented = bool(await loop.run_in_executor(
                                    write_executor, _intervals_read_extra, db,
                                ))
                            if nxt is None:
                                nxt = await loop.run_in_executor(
                                    write_executor, _correction_sweep_next,
                                    db, linearization, commit_metadata, hash_to_pos,
                                    sweep_fragmented,
                                )
                                run_progress.sweep_planned(
                                    nxt.reason, nxt.region_lo, nxt.start_pos, nxt.ceiling_pos,
                                )
                            while not _shutdown_requested.is_set():
                                if nxt.selected is None:
                                    run_progress.sweep_ended(nxt.reason)
                                    sweep_done = True
                                    break
                                sweep_hash, sweep_ts = nxt.selected
                                try:
                                    # ... UNCHANGED body: _extract_commit,
                                    # _correction_sweep_apply, _forward_apply
                                    # (lifecycle_only=True),
                                    # _correction_sweep_through_update,
                                    # run_progress.swept, _db_checkpoint_gated
                                    ...
                                except concurrent.futures.process.BrokenProcessPool:
                                    raise
                                except Exception as e:
                                    # ... UNCHANGED abort handling ...
                                    completed_all = False
                                    run_progress.sweep_ended("aborted")
                                    sweep_done = True
                                    break
                                await asyncio.sleep(0)
                                nxt = await loop.run_in_executor(
                                    write_executor, _correction_sweep_next,
                                    db, linearization, commit_metadata, hash_to_pos,
                                    sweep_fragmented,
                                )
                                # #222 phase 5 item C: the ONLY safe place to
                                # end a window. _correction_sweep_apply,
                                # _forward_apply(lifecycle_only=True) and
                                # _correction_sweep_through_update are ONE
                                # unit -- the watermark is deliberately
                                # deferred until both halves land
                                # (update_watermark=False), so a boundary
                                # inside that sequence creates exactly the
                                # half-processed state the deferral prevents.
                                # Here, the previous commit is fully swept and
                                # `nxt` names the next one, so dropping the
                                # lease loses nothing.
                                window_count += 1
                                if (
                                    window_count >= _SWEEP_YIELD_COMMITS
                                    or time.monotonic() - window_started >= _SWEEP_YIELD_SECONDS
                                ):
                                    break
                        if _shutdown_requested.is_set():
                            break
                    if _shutdown_requested.is_set():
                        completed_all = False
                        run_progress.sweep_ended("stopped")
                    _correction_sweep_log_summary(skipped)
                    async with db_lease_async() as db:
                        should_fold = completed_all and await loop.run_in_executor(
                            write_executor, _should_fold_lineage_watermark, db, linearization,
                        )
                        if should_fold:
                            await loop.run_in_executor(
                                write_executor, _lineage_confirmed_through_update,
                                db, linearization[-1], commit_metadata[-1][1], index_con,
                            )
                            run_progress.folded()
                            await loop.run_in_executor(write_executor, _db_checkpoint_gated, db)
```

Keep the inner body byte-identical to what is there now — this task moves code, it does not rewrite it.

- [ ] **Step 5: Run the test**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestStageBYieldsTheLock -v`
Expected: PASS.

- [ ] **Step 6: Run every sweep test**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -k "sweep or stage_b or correction" -q`
Expected: no new failures. Pay special attention to any test asserting the sweep's checkpoint cadence or its ordering.

- [ ] **Step 7: MEASURE the cost**

```bash
MINIGRAF_INGEST_TRACE_PATH=/tmp/sweep-after.jsonl \
  .venv/bin/python evals/at_scale/run_ingestion_benchmark.py \
  --repo-path . --graph-path /tmp/sweep-after.graph 2>&1 | tail -30
```

Record Stage B wall clock and the drop-checkpoint count, and compare against a
master run with the same command. Report both. If Stage B is more than ~2x
slower, raise `_SWEEP_YIELD_COMMITS` and re-measure rather than shipping the
regression.

- [ ] **Step 8: Ablate**

Set `_SWEEP_YIELD_COMMITS` to a number larger than the sweep length (so no window ever closes), re-run the test, confirm RED. Restore.

- [ ] **Step 9: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Yield Stage B's lease on a bounded window (part of #222 phase 5)

Stage B held one lease across the whole sweep. A lease is cheap
in-process but exclusive out-of-process, and both auto-memory hooks are
command hooks in separate processes that swallow failures after a
0.75 s retry budget -- so the hold silently discarded every auto-memory
write for the sweep's duration.

A window rather than a per-commit release: release() at refcount 1->0
drops the handle and minigraf's Drop for Inner runs a full O(graph
size) checkpoint (#280, 47.3% of Stage A's write time). Window
boundaries land only between fully-swept commits.

Measured: <paste Stage B before/after and checkpoint counts>.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 8: Hygiene — the dead walk wrappers

**Files:**
- Modify: `mcp_server.py` (`_reverse_bulk_fill_walk` `:11508`, `_reverse_fill_claim_and_process` `:11469`, `_correction_sweep_walk` `:12657`, `_correction_sweep_claim_and_process` `:12607`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: either the four functions are gone, or a new test `test_run_ingestion_does_not_call_the_legacy_walk_wrappers`.

- [ ] **Step 1: Establish reachability**

Run: `.venv/bin/python -m pytest tests/ -k "reverse_bulk_fill or correction_sweep_walk or claim_and_process" -v --collect-only`

Then grep production callers:

Run: `grep -rn "_reverse_bulk_fill_walk\|_reverse_fill_claim_and_process\|_correction_sweep_walk\|_correction_sweep_claim_and_process" --include=*.py . | grep -v "^./tests"`

Expected: only definitions and docstring mentions in `mcp_server.py`, plus `frontier_registry.py` comments. No production call site.

- [ ] **Step 2: Add the guard test (do this EITHER way — it is cheap and it is the real protection)**

```python
def test_run_ingestion_does_not_call_the_legacy_walk_wrappers():
    """#326's second unfiled follow-up: these always persist claims and have
    NO floor or ceiling concept, so wiring either into a real run
    reintroduces Critical 3 (a failed write swallowed by an interval's range
    semantics) wholesale. They are reachable only from tests; this pins that."""
    import inspect, mcp_server
    src = inspect.getsource(mcp_server._run_ingestion)
    for name in (
        "_reverse_bulk_fill_walk",
        "_reverse_fill_claim_and_process",
        "_correction_sweep_walk",
        "_correction_sweep_claim_and_process",
    ):
        assert name not in src, (
            f"{name} is called from _run_ingestion. It persists claims "
            f"unconditionally and has no floor/ceiling, so a failed write is "
            f"swallowed by the interval range -- #326 Critical 3."
        )
```

- [ ] **Step 3: Decide delete vs. keep**

If every test using them can be retargeted onto `_reverse_apply` / the real Stage B path in under ~30 minutes, DELETE all four and their tests. Otherwise keep them and prepend to each docstring:

```
    NOT REACHABLE FROM _run_ingestion, AND MUST NOT BECOME SO. This persists
    its claim unconditionally and has no floor/ceiling concept, so a write
    that raises is swallowed by the interval's closed-range semantics --
    #326 Critical 3, which is permanent silent commit loss that every
    at-scale detector reads clean. Pinned by
    test_run_ingestion_does_not_call_the_legacy_walk_wrappers.
```

Report which path you took and why.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Pin the legacy walk wrappers out of _run_ingestion (part of #222 phase 5)

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 9: Hygiene — test oracles

**Files:**
- Test: `tests/test_mcp_server.py` (`_SNAPSHOT_QUERIES` at `:24859` and `:25604`; new unit test for `_forward_structural_triples_by_ident`)

**Interfaces:**
- Consumes: `_forward_structural_triples_by_ident(precomputed: Dict[str, Any]) -> Dict[str, List[str]]` (`mcp_server.py:12162`).
- Produces: test coverage only.

- [ ] **Step 1: Write the direct unit test**

```python
class TestForwardStructuralTriplesByIdent:
    """`_forward_structural_triples_by_ident` had no direct unit test -- it
    was only ever exercised through _forward_apply and the correction sweep,
    both of which would keep passing if it silently returned fewer idents."""

    def test_every_candidate_ident_gets_an_entry(self):
        """The two functions read the SAME five sources and must agree about
        which idents exist. _forward_apply asserts on exactly this
        correspondence (mcp_server.py:11789), so a silent disagreement shows
        up there as a hard failure mid-ingestion rather than here."""
        import mcp_server
        # The third element of each *_entries tuple is that entity's own
        # TRIPLE LIST -- not a type string. _forward_candidate_idents unpacks
        # it as `for ident, _name, _t in ...` and discards it; this function
        # is the consumer that actually uses it.
        precomputed = {
            "module_ident": ":module/auth-py",
            "module_candidate_triples": [
                "[:module/auth-py :entity-type :type/module]",
                '[:module/auth-py :path "auth.py"]',
            ],
            "function_entries": [(
                ":function/auth-py-login", "login",
                ["[:function/auth-py-login :entity-type :type/function]",
                 "[:module/auth-py :contains :function/auth-py-login]"],
            )],
            "class_entries": [(
                ":class/auth-py-user", "User",
                ["[:class/auth-py-user :entity-type :type/class]"],
            )],
            "global_entries": [(
                ":variable/auth-py-limit", "LIMIT",
                ["[:variable/auth-py-limit :entity-type :type/variable]"],
            )],
            "field_entries": [(
                ":field/auth-py-user-name", "name",
                ["[:field/auth-py-user-name :entity-type :type/field]"],
            )],
        }
        result = mcp_server._forward_structural_triples_by_ident(precomputed)
        for ident in mcp_server._forward_candidate_idents(precomputed):
            assert ident in result, (
                f"{ident} is a candidate ident but has no structural triples; "
                f"_forward_apply asserts on exactly this correspondence"
            )
            assert result[ident], f"{ident} mapped to an empty triple list"

    def test_a_child_carries_its_own_containment_edge(self):
        """#222 phase 2b1: a child's list carries its [parent :contains child]
        edge, so re-dating the child re-dates the containment with it. Pinned
        because nothing else asserts it directly."""
        import mcp_server
        precomputed = {
            "module_ident": ":module/auth-py",
            "module_candidate_triples": ["[:module/auth-py :entity-type :type/module]"],
            "function_entries": [(
                ":function/auth-py-login", "login",
                ["[:function/auth-py-login :entity-type :type/function]",
                 "[:module/auth-py :contains :function/auth-py-login]"],
            )],
            "class_entries": [], "global_entries": [], "field_entries": [],
        }
        result = mcp_server._forward_structural_triples_by_ident(precomputed)
        assert any(
            ":contains" in t for t in result[":function/auth-py-login"]
        ), "the child's own triple list lost its containment edge"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestForwardStructuralTriplesByIdent -v`
Expected: PASS (this is characterisation coverage, not a bug fix). If it FAILS, you have found a real defect — stop and report it.

- [ ] **Step 3: Add the closed-`:ident` arm to both snapshot oracles**

Both `_SNAPSHOT_QUERIES` dicts bind `[?e :ident ?i]`, so an entity whose `:ident` has been retracted contributes no rows to either snapshot — the oracle cannot see a resurrection or a purge. Add one query per oracle that binds `:entity-type` WITHOUT `:ident`:

```python
        # An entity whose :ident is closed contributes NO rows to any query
        # above -- every one of them binds [?e :ident ?i]. That makes both
        # oracles blind to exactly the states _build_close_triples produces
        # ("live :entity-type, no :ident"), which is where a resurrection or a
        # purge would show up. This arm binds the type alone.
        "entity-type-unidented": ("?e ?v", "[?e :entity-type ?v]"),
```

Note the find-vars differ between the two classes (`("?i ?v", where)` tuples at `:24859`; bare `where` strings at `:25604`) — match each class's existing shape.

- [ ] **Step 4: Run both parity classes**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -k "parity or snapshot" -q`
Expected: PASS. A NEW failure here is a real finding about closed entities — report it rather than dropping the arm.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "Strengthen the forward-structural and snapshot oracles (part of #222 phase 5)

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 10: Hygiene — construct the `_lineage_marker_ident` collision

This task's deliverable is a FINDING, not necessarily a code change.

**Files:**
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_lineage_marker_ident(entity_ident: str) -> str` (`mcp_server.py:7681`), `_canonical_ident` / `_code_ident`.
- Produces: either a guard plus a fix, or a recorded finding that the collision is unreachable.

- [ ] **Step 1: Confirm the raw collision exists**

```python
def test_lineage_marker_ident_is_not_injective_on_raw_input():
    """`entity_ident.lstrip(':').replace('/', '-')` maps both of these to
    `:lineage/module-a-b`. This is about the FUNCTION, not about whether
    ingestion can produce the colliding input -- see the next test."""
    import mcp_server
    assert (mcp_server._lineage_marker_ident(":module/a-b")
            == mcp_server._lineage_marker_ident(":module/a/b"))
```

- [ ] **Step 2: Determine whether ingestion can produce the colliding input**

Read `_code_ident` and `_canonical_ident`. Question to answer with CODE, not
reasoning: can an ident produced by `_code_ident` ever contain a `/` after the
type prefix? Write a test that drives `_code_ident` over adversarial file
paths (`a/b.py`, `a-b.py`, `a/b/c.py`, paths with `-` and `/` interleaved) and
asserts whether any output has more than one `/`.

```python
def test_code_idents_carry_exactly_one_slash():
    """`_lineage_marker_ident` collapses every `/` to `-`, so it is injective
    over its real inputs only if code idents carry exactly one. This is the
    pre-slug-input question that "it is a function of the ident so it cannot
    differ" skips over."""
    import mcp_server
    for path in ("a/b.py", "a-b.py", "a/b/c.py", "a-b/c.py", "a/b-c.py"):
        ident = mcp_server._code_ident("module", path)
        assert ident.count("/") == 1, f"{path} -> {ident}"
```

- [ ] **Step 3: Decide from the result**

- If code idents always carry exactly one `/`: the collision is **unreachable from ingestion**. Keep both tests (they pin the property the safety depends on), add a docstring note to `_lineage_marker_ident` saying so and naming the test, and change nothing else.
- If any path yields two or more `/`: you have found a real collision. **STOP and report** — two entities sharing one lineage marker is a correctness bug that needs its own fix and probably its own issue, not a hygiene commit.

- [ ] **Step 4: Run**

**Name both tests explicitly. Do NOT use a `-k` substring filter here.**
`-k lineage_marker` would select the first test but silently MISS
`test_code_idents_carry_exactly_one_slash`, which contains neither "lineage"
nor "marker" — and that is the test answering this task's actual open
question. A `-k` filter that quietly selects fewer tests than the change
touches already cost Task 9 a fix round, when a prescribed filter failed to
select a class the task modified and the gap was papered over rather than
noticed.

Run:
```bash
.venv/bin/python -m pytest \
  "tests/test_mcp_server.py::test_lineage_marker_ident_is_not_injective_on_raw_input" \
  "tests/test_mcp_server.py::test_code_idents_carry_exactly_one_slash" -v
```

Expected: both PASS. If `test_code_idents_carry_exactly_one_slash` FAILS, you
have found a reachable collision — **stop and report it** (see Step 3); that is
a correctness bug needing its own fix and probably its own issue, not a
hygiene commit.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Pin _lineage_marker_ident's injectivity precondition (part of #222 phase 5)

The slug collapses every '/' to '-', so it is injective over real
inputs only because code idents carry exactly one. Pinned by test
rather than asserted by reasoning.

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 11: Hygiene — the small items

**Files:**
- Modify: `mcp_server.py` (`_entity_introduced_by_set_provisional_batch` `:8058`, `_correction_sweep_apply` `:12372`, `_parse_stream_ratio` `:12767`)

**Interfaces:**
- Consumes: nothing.
- Produces: no signature changes visible to other tasks (see Step 1's caveat).

- [ ] **Step 1: The unused `Set[str]` return**

Both production call sites (`mcp_server.py:11405` and `:11409`) discard the return; tests at `:11447` onward use it. Do NOT delete it — the tests are real coverage of which idents actually moved. Instead document it:

```python
    Returns the set of idents whose guess was actually asserted or moved.
    NO PRODUCTION CONSUMER -- both call sites in _reverse_apply discard it.
    It is kept because the test suite asserts on it to distinguish "moved" from
    "left alone", which is otherwise only observable by re-querying every
    ident. Do not "clean it up" into None without re-pointing those tests.
```

- [ ] **Step 2: Deduplicate `candidate_idents`**

In `_correction_sweep_apply`, where `candidate_idents` is built, wrap the iteration so each ident is visited once:

```python
        # Deduplicated: the collection mirrors _forward_candidate_idents'
        # construction, which can repeat an ident when a file declares the
        # same name in two categories. The per-ident work below is idempotent,
        # so a repeat was harmless -- but it paid for a full
        # _entity_introduced_by_values_query per duplicate.
        for ident in dict.fromkeys(candidate_idents):
```

- [ ] **Step 3: Fix the log tag**

`mcp_server.py:12767` logs under `[_run_ingestion]` from inside `_parse_stream_ratio`. Change the tag to `[_parse_stream_ratio]`.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: no new failures. If a test asserts on the literal `[_run_ingestion]` prefix for the ratio warning, update it — and check `evals/at_scale/stderr_capture.py`'s patterns for that string before changing it.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Small hygiene items (part of #222 phase 5)

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 12: `_forward_apply` decomposition — SCOPE GATE FIRST

**Files:**
- Modify: `mcp_server.py:11555-12160`

**Interfaces:**
- Consumes: nothing.
- Produces: extracted helpers, or an issue.

- [ ] **Step 1: Measure the seams before touching anything**

Read `_forward_apply` end to end. List every block guarded by `if not lifecycle_only` or `if lifecycle_only`, with line ranges. Count how many distinct behaviours the flag actually threads.

- [ ] **Step 2: Decide, and be willing to decline**

Extract ONLY along seams that are already `lifecycle_only`-guarded and that have no cross-block state. If the extraction cannot be done without moving mutable `_ForwardWalkState` handling across a new function boundary, **STOP**: file an issue titled "refactor: decompose `_forward_apply`" describing the seams you found, and close this task as deferred. That is a legitimate outcome — this function carries two load-bearing invariants, `_forward_apply` mutates twelve cross-position preload dicts in place, and #253 records a previous lifecycle-adjacent change that segfaulted the suite.

- [ ] **Step 3: If extracting, one helper at a time**

After each extraction: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`, full suite, no new failures. Commit each helper separately.

- [ ] **Step 4: The parity oracle is the real gate**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -k parity -q`
Expected: PASS. This is what proves a forward-walk refactor changed no facts.

- [ ] **Step 5: Commit or file**

```bash
git add mcp_server.py
git commit -m "Extract <helper> from _forward_apply (part of #222 phase 5)

Part of #222.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 13: Close out

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: ≥ 2087 passed / 1 xfailed, no new failures.

- [ ] **Step 2: At-scale gate**

Run: `.venv/bin/python evals/at_scale/run_ingestion_benchmark.py --repo-path . --graph-path /tmp/phase5-final.graph`
Expected: exit 0. Record `fact_audit`, `commit_census` and `orphaned_commits`.

- [ ] **Step 3: Update `CLAUDE.md`**

Add a phase-5 section covering: frontier-low's retention check and why it is a separate path from `_load_one_interval`; `:ingestion/branch` and the "never add it to `_graph_has_ingestion_state`" rule; orphan detection being beside the census rather than riding the audit's scan, and why `proved_nothing` gates it; Stage B's yield window and its #280 relationship. Follow the file's existing voice — state what was measured, and what is a stated residual.

- [ ] **Step 4: Scan for closing keywords**

```bash
git log master..HEAD --format=%B | grep -inE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^.]{0,40}#[0-9]+'
gh pr view --json closingIssuesReferences
```

Expected: EMPTY from both. Re-run after every new commit, including any added during review.

- [ ] **Step 5: Push and open the PR**

Body says "Part of #222" and carries no closing keyword. #222 is closed BY HAND after the PR merges and is verified.

## Self-Review

**Spec coverage:** A → Tasks 1–2. B → Tasks 3–6. C → Task 7. D → Tasks 8–12. The spec's five measure-don't-assume items map to Task 1 Step 2, Task 2 Step 6, Task 5 Step 5, Task 7 Step 7, Task 10 Step 2. Close-out is Task 13.

**Placeholder scan:** the only deliberately unresolved values are `_SWEEP_YIELD_COMMITS`/`_SWEEP_YIELD_SECONDS` (seeded at 25 / 2.0 s and measured in Task 7 Step 7) and Task 12's scope gate, which is an explicit decline-permitted decision rather than a TODO.

**Type consistency:** `_ingestion_branch_read(db) -> Optional[str]` is used identically in Tasks 3, 4 and 6. `orphaned_commits(...)` returns the same six keys in Task 4's implementation, Task 5's gate and Task 6's status path. `_orphaned_commit_count` returns `Optional[int]` and status reports it directly.

**One ordering hazard worth repeating:** Task 6 must read `_ingestion_branch_read` BEFORE Task 3's `_ingestion_branch_write` overwrites it, or every run compares its ref against itself and always reports interpretable. It is called out in Task 6 Step 3.
