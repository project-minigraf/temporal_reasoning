# Run-start EAVT/AEVT Cross-check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse to ingest into a graph whose EAVT and AEVT indexes disagree (project-minigraf/minigraf#370), before any read that the damage would falsify and before any write.

**Architecture:** One new read-only function, `_graph_index_cross_check(db)`, in `mcp_server.py`. It enumerates live entities through AEVT, probes every ingestion-control entity plus a random sample of 512 others through EAVT, and raises `GraphIndexDamageError` on a disagreement that survives a re-read. It runs as the first statement of `_load_ingestion_preload_state`'s lease, ahead of `_graph_format_version_verify`, and its report lands in `_ingest_progress["index_cross_check"]`.

**Tech Stack:** Python 3.10–3.14, minigraf 2.0.x (`MiniGrafDb`), pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-11-index-cross-check-design.md` — read it first; this plan argues from it.

## Global Constraints

- ALWAYS run Python and pytest as `.venv/bin/python` / `.venv/bin/python -m pytest`. System python has minigraf 1.1.1 and fakes ~122 failures.
- Real backend only (`docs/testing-conventions.md`): no `MagicMock` of `MiniGrafDb`. Damage is built on a real file, never faked.
- Every regression test is ablation-proven: revert the guarded code, watch the test go red, restore. Record each ablation's output in the commit message.
- At most one live `MiniGrafDb` handle per graph file per process. There is no `close()`; release a raw handle with `del db`. Call `mcp_server._reset_db_state()` before opening a raw handle on a file `_run_ingestion` used.
- No `GRAPH_FORMAT_VERSION` bump, no migration. The check only reads.
- Commit messages and the PR body say "Part of #336" and must contain NO closing keyword (`close`/`fix`/`resolve` + `#N`) — #336 stays open. Scan every commit message after writing it.
- Commit trailer, on every commit:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
  ```
- Branch: `336-index-cross-check` (already checked out, spec committed). Work in place; no worktree.
- Known pre-existing flake, NOT caused by this work: `TestMcpToolWiring::test_call_tool_lock_retry_does_not_block_event_loop` (#334). If it fails in a full run, rerun it alone before investigating.

---

## File Structure

- **Modify `mcp_server.py`**
  - imports (lines 8–32): add `import random`, `import uuid`.
  - after `_graph_format_version_verify` (ends ~line 6154): `GraphIndexDamageError`, three constants, `_index_cross_check_fixed_idents`, `_index_cross_check_population`, `_index_cross_check_probe`, `_index_damage_message`, `_graph_index_cross_check`.
  - `_load_ingestion_preload_state` (~line 10399): call the check first in the lease; rewrite the "FIRST thing" comment above `_graph_format_version_verify`.
  - `_run_ingestion` (~line 12746): reset `_ingest_progress["index_cross_check"] = None` at the top.
- **Create `tests/test_index_cross_check.py`** — damage-construction helpers, unit tests for the function, run-level tests through `_run_ingestion`. Kept out of the 26k-line `tests/test_mcp_server.py`.
- **Modify `SKILL.md`** (after line 311) and **`CLAUDE.md`** (Graph Storage section) — docs.

---

### Task 1: `_graph_index_cross_check` and its unit tests

**Files:**
- Modify: `mcp_server.py` (imports; new block after `_graph_format_version_verify`, ~line 6155)
- Create: `tests/test_index_cross_check.py`

**Interfaces:**
- Consumes: `_db_execute(db, datalog) -> str`, `_FORMAT_VERSION_IDENT`, `_FRONTIER_LOW_IDENT`, `_FRONTIER_HIGH_IDENT`, `_LINEAGE_CONFIRMED_THROUGH_IDENT`, `_CORRECTION_SWEEP_THROUGH_IDENT`, `_COMPLETED_REGION_ENTITY_TYPE` (all existing in `mcp_server.py`).
- Produces (Task 2 and the tests rely on these exact names):
  - `class GraphIndexDamageError(RuntimeError)`
  - `_INDEX_CROSS_CHECK_POPULATION_QUERY: str`
  - `_INDEX_CROSS_CHECK_CONTROL_TYPES: frozenset`
  - `_INDEX_CROSS_CHECK_SAMPLE_SIZE = 512`
  - `_index_cross_check_fixed_idents() -> Tuple[str, ...]`
  - `_index_cross_check_population(db) -> Dict[str, Set[str]]` (entity UUID string → set of `:entity-type` values)
  - `_index_cross_check_probe(db, entity_uuid: str) -> Set[str]`
  - `_graph_index_cross_check(db, sample_size: int = 512, rng: Optional[random.Random] = None) -> Dict[str, int]` returning `{"population", "probed", "control_probed"}`

- [ ] **Step 1: Write the test file with helpers and the unit tests**

Create `tests/test_index_cross_check.py`:

```python
"""#336: the run-start EAVT/AEVT cross-check (_graph_index_cross_check).

Real backend throughout (docs/testing-conventions.md). Index damage is built
the way project-minigraf/minigraf#370 hypothesizes it arises -- a partial
index tree under a valid header -- by redirecting one index's root page in the
file header to that tree's rightmost leaf. See _keep_rightmost_leaf.

Every damaged-graph test asserts its PRECONDITION (which reads the damage
falsifies) before exercising the check, so a construction that silently stops
producing damage fails as a precondition, never as a pass.
"""
import os
import random
import struct
import subprocess
import uuid
import zlib

import pytest
from minigraf import MiniGrafDb

import mcp_server

# minigraf v2.0.0 on-disk layout: src/storage/mod.rs FileHeader (v7) and
# src/storage/btree_v6.rs page types. _keep_rightmost_leaf asserts each fact it
# uses, so a format change fails there loudly instead of writing garbage.
_PAGE_SIZE = 4096
_PAGE_TYPE_LEAF = 0x21
_PAGE_TYPE_INTERNAL = 0x22
_HEADER_LEN = 84
_EAVT_ROOT_OFFSET = 32
_AEVT_ROOT_OFFSET = 40
_HEADER_CHECKSUM_OFFSET = 80


def _keep_rightmost_leaf(graph_path, root_offset):
    """Point one index's root at that tree's rightmost leaf, so the index keeps
    only that leaf's entries while every fact page stays intact.

    minigraf accepts the result: index_checksum covers pages 1..page_count, not
    the header page, so open() still trusts the on-disk indexes instead of
    rebuilding them from facts. Measured on the EAVT side: 400 entities visible
    through AEVT, 20 through EAVT, no error on open.
    """
    with open(graph_path, "r+b") as f:
        header = bytearray(f.read(_HEADER_LEN))
        assert bytes(header[:4]) == b"MGRF"
        assert struct.unpack_from("<I", header, 4)[0] == 7, (
            "minigraf header layout changed; re-derive _keep_rightmost_leaf"
        )
        page_id = struct.unpack_from("<Q", header, root_offset)[0]
        f.seek(page_id * _PAGE_SIZE)
        page = f.read(_PAGE_SIZE)
        assert page[0] == _PAGE_TYPE_INTERNAL, (
            "index root is already a leaf: the graph is too small to damage"
        )
        while page[0] == _PAGE_TYPE_INTERNAL:
            page_id = struct.unpack_from("<Q", page, 4)[0]  # rightmost_child
            f.seek(page_id * _PAGE_SIZE)
            page = f.read(_PAGE_SIZE)
        assert page[0] == _PAGE_TYPE_LEAF
        struct.pack_into("<Q", header, root_offset, page_id)
        struct.pack_into(
            "<I", header, _HEADER_CHECKSUM_OFFSET,
            zlib.crc32(bytes(header[:_HEADER_CHECKSUM_OFFSET])),
        )
        f.seek(0)
        f.write(header)


def _entity_uuid(ident):
    """The entity id minigraf derives from a keyword ident."""
    return uuid.uuid5(uuid.NAMESPACE_OID, ident)


def _filler_idents(n, keep=lambda entity_uuid: True):
    """n deterministic filler idents, optionally restricted by entity UUID."""
    idents, i = [], 0
    while len(idents) < n:
        ident = f":filler/f{i}"
        i += 1
        if keep(_entity_uuid(ident)):
            idents.append(ident)
    return idents


def _filler_facts(idents):
    return [
        fact
        for x in idents
        for fact in (
            f"[{x} :entity-type :type/filler]",
            f'[{x} :ident "{x}"]',
            f'[{x} :description "filler"]',
        )
    ]


# Three control entities, all of them fixed idents.
_CONTROL_FACTS = [
    "[:ingestion/format-version :entity-type :type/ingestion]",
    '[:ingestion/format-version :ident ":ingestion/format-version"]',
    '[:ingestion/format-version :description "graph format version"]',
    "[:ingestion/format-version :version 1]",
    "[:ingestion/watermark :entity-type :type/ingestion]",
    '[:ingestion/watermark :ident ":ingestion/watermark"]',
    '[:ingestion/watermark :description "ingestion watermark"]',
    '[:ingestion/watermark :hash "abc"]',
    "[:ingestion/frontier-low :entity-type :type/ingest-interval]",
    '[:ingestion/frontier-low :lo-hash "abc"]',
    '[:ingestion/frontier-low :hi-hash "abc"]',
]


def _write_facts(graph_path, facts):
    """Transact facts through a raw handle, checkpoint, and release it."""
    db = MiniGrafDb.open(str(graph_path))
    db.execute("(transact [" + " ".join(facts) + "])")
    db.checkpoint()
    del db


def _fixed_count():
    return len(mcp_server._index_cross_check_fixed_idents())


class TestGraphIndexCrossCheck:
    def test_healthy_graph_passes_and_reports_its_denominators(self, tmp_path):
        graph = tmp_path / "g.graph"
        _write_facts(graph, _CONTROL_FACTS + _filler_facts(_filler_idents(50)))
        db = MiniGrafDb.open(str(graph))
        report = mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        # The three control facts' entities are all fixed idents, so the
        # control set is exactly the fixed set; the 50 fillers are all sampled.
        assert report == {
            "population": 53,
            "probed": _fixed_count() + 50,
            "control_probed": _fixed_count(),
        }

    def test_empty_graph_passes_and_says_it_saw_nothing(self, tmp_path):
        db = MiniGrafDb.open(str(tmp_path / "g.graph"))
        report = mcp_server._graph_index_cross_check(db)
        assert report == {
            "population": 0,
            "probed": _fixed_count(),
            "control_probed": _fixed_count(),
        }

    def test_sample_is_bounded_by_sample_size(self, tmp_path):
        graph = tmp_path / "g.graph"
        _write_facts(graph, _CONTROL_FACTS + _filler_facts(_filler_idents(50)))
        db = MiniGrafDb.open(str(graph))
        report = mcp_server._graph_index_cross_check(
            db, sample_size=10, rng=random.Random(0)
        )
        assert report["control_probed"] == _fixed_count()
        assert report["probed"] == 10 + report["control_probed"]

    def test_damaged_eavt_is_refused(self, tmp_path):
        graph = tmp_path / "g.graph"
        _write_facts(graph, _CONTROL_FACTS + _filler_facts(_filler_idents(400)))
        _keep_rightmost_leaf(graph, _EAVT_ROOT_OFFSET)
        db = MiniGrafDb.open(str(graph))
        # Precondition: #370's shape -- AEVT lists everything, EAVT has lost
        # most of it.
        population = mcp_server._index_cross_check_population(db)
        assert len(population) == 403
        visible = sum(
            1 for e in population if mcp_server._index_cross_check_probe(db, e)
        )
        assert visible < len(population) // 2
        with pytest.raises(mcp_server.GraphIndexDamageError) as exc:
            mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        message = str(exc.value)
        assert "minigraf#370" in message
        assert "FRESH graph path" in message

    def test_damaged_aevt_does_not_fail_open(self, tmp_path):
        graph = tmp_path / "g.graph"
        _write_facts(graph, _CONTROL_FACTS + _filler_facts(_filler_idents(400)))
        _keep_rightmost_leaf(graph, _AEVT_ROOT_OFFSET)
        db = MiniGrafDb.open(str(graph))
        # Precondition: the fail-open shape -- AEVT lost the whole
        # :entity-type block, so the population is empty, while EAVT still
        # answers for the stamp.
        assert mcp_server._index_cross_check_population(db) == {}
        assert mcp_server._graph_format_version_read(db) == 1
        with pytest.raises(mcp_server.GraphIndexDamageError):
            mcp_server._graph_index_cross_check(db, rng=random.Random(0))

    def test_entity_retracted_mid_check_is_not_reported_as_damage(
        self, tmp_path, monkeypatch
    ):
        """call_tool can join the preload's lease, so a concurrent retract can
        land between the population scan and a probe. That must re-confirm,
        not refuse: a false refusal tells the user to discard a healthy graph.
        """
        graph = tmp_path / "g.graph"
        _write_facts(graph, _CONTROL_FACTS + _filler_facts(_filler_idents(20)))
        db = MiniGrafDb.open(str(graph))
        real_execute = mcp_server._db_execute
        raced = []

        def racing_execute(handle, datalog):
            out = real_execute(handle, datalog)
            if datalog == mcp_server._INDEX_CROSS_CHECK_POPULATION_QUERY and not raced:
                raced.append(True)
                real_execute(
                    handle, "(retract [[:filler/f0 :entity-type :type/filler]])"
                )
            return out

        monkeypatch.setattr(mcp_server, "_db_execute", racing_execute)
        report = mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        assert raced
        # The race really happened, and f0 was probed (sample_size > 20).
        assert mcp_server._index_cross_check_probe(
            db, str(_entity_uuid(":filler/f0"))
        ) == set()
        assert report["population"] == 23
        assert report["probed"] == _fixed_count() + 20

    def test_control_types_include_the_region_type_constant(self):
        assert (
            mcp_server._COMPLETED_REGION_ENTITY_TYPE
            in mcp_server._INDEX_CROSS_CHECK_CONTROL_TYPES
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py -q`
Expected: every test FAILS with `AttributeError: module 'mcp_server' has no attribute ...` (`_graph_index_cross_check`, `_index_cross_check_population`, etc.).

- [ ] **Step 3: Add the imports**

In `mcp_server.py`'s import block, add `import random` directly after `import os`, and `import uuid` directly after `import traceback`.

- [ ] **Step 4: Implement the check**

In `mcp_server.py`, insert immediately after the end of `_graph_format_version_verify` (the `raise GraphFormatVersionError(...)` block ending ~line 6154) and before `def _graph_format_version_stamp_if_new`:

```python
class GraphIndexDamageError(RuntimeError):
    """Raised when the graph's EAVT and AEVT indexes disagree about an entity.

    project-minigraf/minigraf#370: a process killed mid-save can leave a graph
    that opens without error, answers every attribute-driven scan and count
    correctly, and returns [] for entity-bound lookups, because its EAVT index
    lost most entities' entries while AEVT kept them. Deliberately a hard
    failure (#336): this module has >=14 entity-bound point-query sites --
    frontier bounds, :introduced-by, :ident liveness, the watermarks, the format
    stamp -- and every one misreads on such a graph, so no part of ingestion is
    safe to run on it.
    """


# #336. Lists every live entity through AEVT. minigraf's executor
# (executor.rs selective_fact_fetch) serves an attribute-only pattern from
# FactStorage::get_facts_by_attribute (AEVT) and an entity-literal pattern
# from get_facts_by_entity (EAVT), so this query and _index_cross_check_probe
# read the same facts through the two different indexes. Module-level so a
# test can recognise it.
_INDEX_CROSS_CHECK_POPULATION_QUERY = (
    "(query [:find ?e ?t :where [?e :entity-type ?t]])"
)
# Ingestion control state, probed exhaustively rather than sampled: the format
# stamp, the watermarks, frontier intervals and archived regions. They are few,
# and misreading them is what makes a damaged mature graph look brand new.
# Literals rather than the constants because _COMPLETED_REGION_ENTITY_TYPE is
# defined further down this module; a test pins the two together.
_INDEX_CROSS_CHECK_CONTROL_TYPES = frozenset({
    ":type/ingestion", ":type/ingest-interval", ":type/completed-region",
})
# Uniform random sample of the non-control population. Misses damage covering
# a fraction f of entities with probability (1 - f) ** 512: 0.6% at f = 1%.
_INDEX_CROSS_CHECK_SAMPLE_SIZE = 512


def _index_cross_check_fixed_idents() -> Tuple[str, ...]:
    """The fixed-ident control entities, probed whether or not AEVT lists them.

    This is what keeps the check from failing OPEN on AEVT damage: a graph
    whose AEVT lost the whole :entity-type block returns an empty population
    (measured) while EAVT still answers for these, so an empty population alone
    must not be read as "nothing to check". An ident absent from both indexes
    compares empty to empty, so a fresh graph passes.

    Built at call time, not as a module constant, because
    _LINEAGE_CONFIRMED_THROUGH_IDENT and _CORRECTION_SWEEP_THROUGH_IDENT are
    defined further down this module.
    """
    return (
        _FORMAT_VERSION_IDENT,
        ":ingestion/watermark",
        _FRONTIER_LOW_IDENT,
        _FRONTIER_HIGH_IDENT,
        _LINEAGE_CONFIRMED_THROUGH_IDENT,
        _CORRECTION_SWEEP_THROUGH_IDENT,
        ":ingestion/last-run-at",
    )


def _index_cross_check_population(db: Any) -> Dict[str, Set[str]]:
    """Every live entity's :entity-type values, read through AEVT."""
    raw = _db_execute(db, _INDEX_CROSS_CHECK_POPULATION_QUERY)
    population: Dict[str, Set[str]] = {}
    for entity, entity_type in json.loads(raw).get("results", []):
        population.setdefault(entity, set()).add(entity_type)
    return population


def _index_cross_check_probe(db: Any, entity_uuid: str) -> Set[str]:
    """One entity's :entity-type values, read through EAVT."""
    raw = _db_execute(
        db, f'(query [:find ?t :where [#uuid "{entity_uuid}" :entity-type ?t]])'
    )
    return {row[0] for row in json.loads(raw).get("results", [])}


def _index_damage_message(
    entity_uuid: str, ident: Optional[str], aevt: Set[str], eavt: Set[str]
) -> str:
    name = f"{ident} ({entity_uuid})" if ident else entity_uuid
    return (
        f"Graph index damage: entity {name} has :entity-type {sorted(aevt)} "
        f"through the attribute index (AEVT) but {sorted(eavt)} through the "
        "entity index (EAVT). This is project-minigraf/minigraf#370, an index "
        "left partial by a process killed mid-save. Every entity-bound read "
        "misreads on such a graph, so ingestion refuses to run rather than "
        "write from wrong answers. Nothing repairs it in place: re-running "
        "ingestion and checkpoint() both copy the damaged index forward. "
        "Re-ingest into a FRESH graph path (set MINIGRAF_GRAPH_PATH to a new "
        "file, or delete the existing graph and its .fts.sqlite3 index first)."
    )


def _graph_index_cross_check(
    db: Any,
    sample_size: int = _INDEX_CROSS_CHECK_SAMPLE_SIZE,
    rng: Optional[random.Random] = None,
) -> Dict[str, int]:
    """Refuse a graph whose EAVT and AEVT indexes disagree (#336).

    READ-ONLY, and must be the FIRST read of a run: _graph_format_version_verify
    and _graph_has_ingestion_state are themselves entity-bound reads, so on a
    damaged graph they read the stamp, watermark and frontiers as absent and
    adopt a mature graph as fresh (measured on a real ingested graph: 863
    commits through AEVT, has_state False through EAVT). See
    docs/superpowers/specs/2026-09-11-index-cross-check-design.md.

    Probes every control entity (the fixed idents plus everything carrying a
    control type) and a uniform random sample of the rest. An entity's EAVT set
    must EQUAL its AEVT set, which catches loss in either index for the entities
    probed. A disagreement is re-read once before it counts, because call_tool
    can join this lease and retract a sampled entity between scan and probe --
    and a false refusal tells the user to discard a healthy graph.

    Returns {"population", "probed", "control_probed"}. A population of 0 is
    not a verification; it is reported so nobody reads it as one (#316's
    code_entities_scanned idiom).
    """
    population = _index_cross_check_population(db)
    fixed = {
        str(uuid.uuid5(uuid.NAMESPACE_OID, ident)): ident
        for ident in _index_cross_check_fixed_idents()
    }
    control = set(fixed) | {
        entity for entity, types in population.items()
        if types & _INDEX_CROSS_CHECK_CONTROL_TYPES
    }
    rest = sorted(entity for entity in population if entity not in control)
    if len(rest) > sample_size:
        rest = (rng or random.Random()).sample(rest, sample_size)
    selected = sorted(control) + rest
    for entity in selected:
        if _index_cross_check_probe(db, entity) == population.get(entity, set()):
            continue
        aevt = _index_cross_check_population(db).get(entity, set())
        eavt = _index_cross_check_probe(db, entity)
        if eavt != aevt:
            raise GraphIndexDamageError(
                _index_damage_message(entity, fixed.get(entity), aevt, eavt)
            )
    return {
        "population": len(population),
        "probed": len(selected),
        "control_probed": len(control),
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py -q`
Expected: 7 passed.

If `test_damaged_eavt_is_refused` or `test_damaged_aevt_does_not_fail_open` fails on a **precondition** assert (not on the check), the damage construction changed, not the code: print the counts and stop to report — do not loosen the precondition.

- [ ] **Step 6: Ablate the re-confirm**

Temporarily replace the two re-read lines in `_graph_index_cross_check`:
```python
        aevt = _index_cross_check_population(db).get(entity, set())
        eavt = _index_cross_check_probe(db, entity)
```
with:
```python
        aevt = population.get(entity, set())
        eavt = _index_cross_check_probe(db, entity)
```
Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py::TestGraphIndexCrossCheck::test_entity_retracted_mid_check_is_not_reported_as_damage -q`
Expected: FAIL with `GraphIndexDamageError` naming `:filler/f0`'s UUID. Restore the original two lines; rerun, expect PASS. Save the failure line for the commit message.

- [ ] **Step 7: Ablate the fixed-ident union**

Temporarily change `control = set(fixed) | {` to `control = set() | {`.
Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py::TestGraphIndexCrossCheck::test_damaged_aevt_does_not_fail_open -q`
Expected: FAIL with `DID NOT RAISE` (population 0 passes). Also expect `test_healthy_graph_passes_and_reports_its_denominators` and `test_empty_graph_passes_and_says_it_saw_nothing` to fail on the counts — that is the counts changing, not a second finding. Restore; rerun the whole file, expect 7 passed.

- [ ] **Step 8: Ablate the probe comparison entirely**

Temporarily insert `return {"population": len(population), "probed": 0, "control_probed": 0}` as the first line after `population = _index_cross_check_population(db)`.
Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py::TestGraphIndexCrossCheck::test_damaged_eavt_is_refused -q`
Expected: FAIL with `DID NOT RAISE`. Restore; rerun the file, expect 7 passed.

- [ ] **Step 9: Commit**

```bash
git add mcp_server.py tests/test_index_cross_check.py
git commit -F - <<'EOF'
Add _graph_index_cross_check: EAVT/AEVT disagreement check (part of #336)

<one paragraph: what it probes, why the fixed idents, why the re-confirm>

Ablations (each restored after):
- re-confirm removed -> test_entity_retracted_mid_check... FAILED: <line>
- fixed-ident union removed -> test_damaged_aevt_does_not_fail_open FAILED: DID NOT RAISE
- comparison short-circuited -> test_damaged_eavt_is_refused FAILED: DID NOT RAISE

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
EOF
git log -1 --format=%B | grep -niE '(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^#]*#[0-9]+' && echo "CLOSING KEYWORD FOUND - amend" || echo "no closing keywords"
```

---

### Task 2: Run the check first in every ingestion run

**Files:**
- Modify: `mcp_server.py` — `_load_ingestion_preload_state` (~line 10399), `_run_ingestion` (~line 12746)
- Modify: `tests/test_index_cross_check.py` (append run-level tests)

**Interfaces:**
- Consumes: `_graph_index_cross_check(db) -> Dict[str, int]`, `GraphIndexDamageError`, `_EAVT_ROOT_OFFSET`, `_keep_rightmost_leaf`, `_filler_idents`, `_filler_facts`, `_write_facts`, `_entity_uuid`, `_fixed_count` (Task 1).
- Produces: `_ingest_progress["index_cross_check"]` — `None` from the start of every `_run_ingestion` until the check passes, then the report dict.

- [ ] **Step 1: Append the run-level tests**

Append to `tests/test_index_cross_check.py`:

```python
# Fixed identity and dates, and no user/system git config (a global
# commit.gpgsign would add a timestamped signature): commit hashes, hence
# :commit/<hash> entity UUIDs, hence which entities survive in the kept leaf,
# are then identical on every run and every machine.
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, env=_GIT_ENV
        )

    git("init", "-q")
    (repo / "auth.py").write_text("def login(): pass\n")
    git("add", ".")
    git("commit", "-q", "-m", "add auth")
    (repo / "models.py").write_text("class User: pass\n")
    git("add", ".")
    git("commit", "-q", "-m", "add models")
    return repo


async def _ingest(repo, graph_path):
    """One real _run_ingestion against graph_path; returns its final progress.

    Releases every handle afterwards, so the caller may open a raw MiniGrafDb
    on the same file (single-handle invariant).
    """
    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph_path))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0,
        "current_commit": "", "error": None,
    }
    await mcp_server._run_ingestion(str(repo), "HEAD")
    progress = dict(mcp_server._ingest_progress)
    mcp_server._reset_db_state()
    return progress


class TestIndexCrossCheckAtRunStart:
    async def test_every_run_reports_the_check(self, repo, tmp_path):
        graph = tmp_path / "memory.graph"
        first = await _ingest(repo, graph)
        assert first["status"] == "complete", first.get("error")
        # A fresh graph: nothing to disagree about, and the report says so.
        assert first["index_cross_check"]["population"] == 0
        second = await _ingest(repo, graph)
        assert second["status"] == "complete", second.get("error")
        assert second["index_cross_check"]["population"] > 0
        assert second["index_cross_check"]["control_probed"] >= _fixed_count()

    async def test_damaged_mature_graph_is_refused_not_adopted_as_fresh(
        self, repo, tmp_path
    ):
        graph = tmp_path / "memory.graph"
        assert (await _ingest(repo, graph))["status"] == "complete"
        _write_facts(graph, _filler_facts(_filler_idents(400)))
        _keep_rightmost_leaf(graph, _EAVT_ROOT_OFFSET)
        # Precondition: #336's misread. Through EAVT the graph looks never
        # ingested; through AEVT it still holds both commits.
        db = MiniGrafDb.open(str(graph))
        assert mcp_server._graph_has_ingestion_state(db) is False
        assert mcp_server._graph_format_version_read(db) is None
        assert mcp_server._count_commit_entities(db) == 2
        del db

        progress = await _ingest(repo, graph)
        assert progress["status"] == "error"
        assert "minigraf#370" in progress["error"]
        assert progress["index_cross_check"] is None

    async def test_partial_damage_is_named_as_index_damage_not_format(
        self, repo, tmp_path
    ):
        """Damage that loses the stamp but keeps the watermark makes
        _graph_format_version_verify blame the #263 ident rule. Running the
        cross-check first is what names the real cause."""
        graph = tmp_path / "memory.graph"
        assert (await _ingest(repo, graph))["status"] == "complete"
        watermark = _entity_uuid(":ingestion/watermark")
        # Filler strictly below the watermark's UUID, so the watermark's own
        # facts are the highest EAVT keys and land in the kept leaf.
        _write_facts(
            graph, _filler_facts(_filler_idents(400, keep=lambda u: u < watermark))
        )
        _keep_rightmost_leaf(graph, _EAVT_ROOT_OFFSET)
        # Precondition: stamp lost, watermark kept -- the shape in which
        # _graph_format_version_verify raises GraphFormatVersionError.
        db = MiniGrafDb.open(str(graph))
        assert mcp_server._watermark_query(db) is not None
        assert mcp_server._graph_format_version_read(db) is None
        del db

        progress = await _ingest(repo, graph)
        assert progress["status"] == "error"
        assert "minigraf#370" in progress["error"]
        assert "graph format version" not in progress["error"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py::TestIndexCrossCheckAtRunStart -q`
Expected:
- `test_every_run_reports_the_check` FAILS with `KeyError: 'index_cross_check'`.
- `test_damaged_mature_graph_is_refused_not_adopted_as_fresh` FAILS: `status` is not `"error"` with `minigraf#370` (the run proceeds).
- `test_partial_damage_is_named_as_index_damage_not_format` FAILS: the error is the `GraphFormatVersionError` text, which lacks `minigraf#370`.

If either damaged test fails on a **precondition** instead, stop and report the observed values — the construction needs adjusting, not the assertion. (`_count_commit_entities == 2` depends only on the repo; the other preconditions depend on UUID order, which `_GIT_ENV` makes deterministic.)

- [ ] **Step 3: Reset the report at the top of `_run_ingestion`**

In `_run_ingestion`, directly after the line `_reset_introduced_by_ambiguity_log_budget()` (~line 12746), add:

```python
    # #336. None until this run's index cross-check passes, so a run that was
    # refused, or failed before reaching it, never reports a previous run's
    # clean check. Here rather than in the _ingest_progress initializers:
    # tests and the at-scale harness call _run_ingestion with their own dicts.
    _ingest_progress["index_cross_check"] = None
```

- [ ] **Step 4: Call the check first in the preload lease**

In `_load_ingestion_preload_state`, replace:

```python
    with db_lease(extended=True) as db:
        # FIRST thing after the handle exists, and deliberately here rather than
        # anywhere later: this is the earliest point in a run that has a db, and
        # everything below it (and every write in _frontier_load and the walks
        # after it) would be written under the current ident rule. A refusal that
        # fired later would leave a graph half-written under two rules. Read-only;
        # the matching stamp write is _run_ingestion's first write. Raises
        # GraphFormatVersionError, which _run_ingestion surfaces as a failed run.
        _graph_format_version_verify(db)
```

with:

```python
    with db_lease(extended=True) as db:
        # FIRST thing after the handle exists (#336), ahead even of the format
        # check below, because that check is itself an entity-bound read: on a
        # graph with a damaged EAVT index (project-minigraf/minigraf#370) the
        # stamp, watermark and frontiers all read as absent and a mature graph
        # is adopted as fresh -- or, with partial damage, refused for an ident-
        # rule problem it does not have. Read-only. Raises GraphIndexDamageError,
        # which _run_ingestion surfaces as a failed run.
        _ingest_progress["index_cross_check"] = _graph_index_cross_check(db)
        # Second, and still ahead of everything else: this is the earliest point
        # after the index check, and everything below it (and every write in
        # _frontier_load and the walks after it) would be written under the
        # current ident rule. A refusal that fired later would leave a graph
        # half-written under two rules. Read-only; the matching stamp write is
        # _run_ingestion's first write. Raises GraphFormatVersionError, which
        # _run_ingestion surfaces as a failed run.
        _graph_format_version_verify(db)
```

- [ ] **Step 5: Run the run-level tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py -q`
Expected: 10 passed.

- [ ] **Step 6: Ablate the call**

Temporarily delete the line `_ingest_progress["index_cross_check"] = _graph_index_cross_check(db)`.
Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py::TestIndexCrossCheckAtRunStart::test_damaged_mature_graph_is_refused_not_adopted_as_fresh -q`
Expected: FAIL (the run is not refused with `minigraf#370`). Record what `status`/`error` it reached. Restore.

- [ ] **Step 7: Ablate the ordering**

Temporarily move the `_ingest_progress["index_cross_check"] = _graph_index_cross_check(db)` line to directly AFTER `_graph_format_version_verify(db)`.
Run: `.venv/bin/python -m pytest tests/test_index_cross_check.py::TestIndexCrossCheckAtRunStart::test_partial_damage_is_named_as_index_damage_not_format -q`
Expected: FAIL — the error names the graph format version (#263), not `minigraf#370`. Restore; rerun the file, expect 10 passed.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass except possibly the #334 flake (see Global Constraints). Any other failure is this change's: a likely cause is a test that counts or orders `_db_execute` calls during the preload, which now sees `1 + 7 + (sampled)` extra queries first. Read the failing assertion before changing anything, and report rather than loosen it if it guards something real.

- [ ] **Step 9: Commit**

```bash
git add mcp_server.py tests/test_index_cross_check.py
git commit -F - <<'EOF'
Run the index cross-check first in every ingestion run (part of #336)

<one paragraph: placement ahead of the format check and why; the report key>

Ablations (each restored after):
- call removed -> test_damaged_mature_graph_is_refused... FAILED: <status/error reached>
- call moved after _graph_format_version_verify -> test_partial_damage... FAILED: <error text>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
EOF
git log -1 --format=%B | grep -niE '(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^#]*#[0-9]+' && echo "CLOSING KEYWORD FOUND - amend" || echo "no closing keywords"
```

---

### Task 3: Document the refusal and its recovery

**Files:**
- Modify: `SKILL.md` (after line 311, the "There is **no migration**." paragraph)
- Modify: `CLAUDE.md` (Graph Storage section, directly after the paragraph ending "See `docs/superpowers/specs/2026-08-14-ident-rule-r3-and-format-version-design.md`.")

**Interfaces:**
- Consumes: the behaviour from Tasks 1–2; the error text in `_index_damage_message`.
- Produces: docs only.

- [ ] **Step 1: Add the SKILL.md paragraph**

Insert after SKILL.md's "There is **no migration**. …" paragraph (line 311), as its own paragraph:

```markdown
**Index damage.** Before any other read, ingestion checks that the graph's two indexes agree: it lists entities through the attribute index and re-reads the ingestion-state entities plus a random sample of the rest by entity. If they disagree, the run fails with `status: error` and a message naming project-minigraf/minigraf#370. That is what a process killed mid-save can leave behind: the graph opens cleanly, every count and scan looks healthy, and lookups by ident silently return nothing — so without the check a run would read its own watermark as missing and re-walk, or adopt a mature graph as new. Nothing repairs it in place; re-running ingestion does not. The only recovery is re-ingesting into a **fresh graph path**, as above. `minigraf_ingest_status` reports what the check covered as `index_cross_check` (`population`, `probed`, `control_probed`); `population: 0` means the graph had nothing to check, not that it was verified.
```

- [ ] **Step 2: Add the CLAUDE.md section**

Insert in CLAUDE.md after the paragraph ending "See `docs/superpowers/specs/2026-08-14-ident-rule-r3-and-format-version-design.md`." (in Graph Storage, the "Graph format version — there is no migration" block), as a new paragraph block:

```markdown
**Ingestion refuses a graph whose EAVT and AEVT disagree, and it checks that
FIRST (#336, partial).** project-minigraf/minigraf#370: a kill mid-save can
leave EAVT missing most entities' entries while AEVT keeps them — the graph
opens clean, scans and counts are right, and every entity-bound lookup returns
`[]`. `_graph_index_cross_check` (mcp_server.py) is the first read in
`_load_ingestion_preload_state`'s lease: it lists live entities through AEVT
(`[?e :entity-type ?t]`) and re-reads each selected one through EAVT
(`[#uuid "…" :entity-type ?t]`) — minigraf's `selective_fact_fetch` routes an
entity-literal pattern to `get_facts_by_entity` and an attribute-only one to
`get_facts_by_attribute`, so the two queries are an exact two-index comparison.
It probes every control entity and 512 random others, and raises
`GraphIndexDamageError` on a disagreement that survives one re-read.

**It must precede `_graph_format_version_verify`, not merely the first
write.** That check and `_graph_has_ingestion_state` are entity-bound reads
too. Measured on a copy of this repo's 179 MB graph with EAVT damaged: stamp,
watermark and both frontiers read `None`, `has_state` read False, the format
check PASSED as "genuinely new" — while AEVT still counted 863 commits. With
partial damage (stamp lost, watermark kept) the format check instead raises
`GraphFormatVersionError`, blaming an ident-rule problem the graph does not
have.

**The fixed control idents are probed whether or not AEVT lists them, and that
is not redundancy.** Damaging AEVT the same way left the population query
returning 0 rows while EAVT still answered for the stamp — so a check that
sampled only what AEVT listed would pass an AEVT-damaged graph as "nothing to
probe". `index_cross_check.population == 0` is reported, never read as
verified (#316's denominator idiom).

The tests build real damage, not a fake: `tests/test_index_cross_check.py`'s
`_keep_rightmost_leaf` points one index's root page in the v7 header at that
tree's rightmost leaf and re-CRCs the header. minigraf trusts it because
`index_checksum` covers pages 1..page_count, never the header page. The helper
asserts every layout fact it uses, so a minigraf format change fails it loudly.

Cost: 0.10 s for the population scan at 6,148 entities, 0.56 ms per EAVT probe
(~0.4 s total); the scan is linear in entities (~25 s extrapolated at 1.6M,
not measured). Residuals, stated rather than fixed: an entity AEVT lost is
never sampled unless it is a fixed control ident; only `:entity-type` is
compared, so partial loss inside one entity's EAVT range passes; light damage
can escape a 512 sample ((1 − f)^512, 0.6% at f = 1%); and readers outside
ingestion — `minigraf_query`, the memory hooks, `minigraf_ingest_status`'s own
`:ingestion/last-run-at` read — are unguarded. The rest of #336 (per-file
batched marker probe → #239; watermark singletons → minigraf#323) is open.
```

- [ ] **Step 3: Verify the skill-doc guard still passes**

Run: `.venv/bin/python -m pytest tests/test_skill_doc.py tests/test_tool_schemas.py -q`
Expected: all pass (no tool-call examples were added, no `_TOOLS` change).

- [ ] **Step 4: Commit**

```bash
git add SKILL.md CLAUDE.md
git commit -F - <<'EOF'
Document the index-damage refusal and its only recovery (part of #336)

Covers #336's fourth suggestion: the recovery from a kill-during-save is a
fresh graph path, and the symptom is otherwise silent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
EOF
git log -1 --format=%B | grep -niE '(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^#]*#[0-9]+' && echo "CLOSING KEYWORD FOUND - amend" || echo "no closing keywords"
```

---

## After all tasks

- Scan every commit on the branch, not just the last: `git log master..HEAD --format=%B | grep -niE '(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^#]*#[0-9]+'` — must print nothing.
- The PR body says "Part of #336" and lists what remains open; check `gh pr view --json closingIssuesReferences` is empty after opening it.
