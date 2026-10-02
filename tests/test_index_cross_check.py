"""#336: the run-start EAVT/AEVT cross-check (_graph_index_cross_check).

Real backend throughout (docs/testing-conventions.md). Index damage is built
the way project-minigraf/minigraf#370 hypothesizes it arises -- a partial
index tree under a valid header -- by redirecting one index's root page in the
file header to that tree's rightmost leaf. See _keep_rightmost_leaf.

Every damaged-graph test asserts its PRECONDITION (which reads the damage
falsifies) before exercising the check, so a construction that silently stops
producing damage fails as a precondition, never as a pass.
"""
import json
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


def _eavt_leaf_chain(graph_path, root_offset):
    """Page ids of one index's leaves, left to right: first child (offset 12
    of an internal page) down to the leftmost leaf, then next_leaf (offset 4
    of a leaf, 0 = last)."""
    with open(graph_path, "rb") as f:
        header = f.read(_HEADER_LEN)
        assert bytes(header[:4]) == b"MGRF"
        assert struct.unpack_from("<I", header, 4)[0] == 7, (
            "minigraf header layout changed; re-derive _eavt_leaf_chain"
        )
        page_id = struct.unpack_from("<Q", header, root_offset)[0]
        f.seek(page_id * _PAGE_SIZE)
        page = f.read(_PAGE_SIZE)
        assert page[0] == _PAGE_TYPE_INTERNAL, (
            "index root is already a leaf: the graph is too small to damage"
        )
        while page[0] == _PAGE_TYPE_INTERNAL:
            page_id = struct.unpack_from("<Q", page, 12)[0]
            f.seek(page_id * _PAGE_SIZE)
            page = f.read(_PAGE_SIZE)
        chain = []
        while True:
            assert page[0] == _PAGE_TYPE_LEAF
            chain.append(page_id)
            page_id = struct.unpack_from("<Q", page, 4)[0]
            if page_id == 0:
                return chain
            f.seek(page_id * _PAGE_SIZE)
            page = f.read(_PAGE_SIZE)


def _point_root_at(graph_path, root_offset, page_id):
    with open(graph_path, "r+b") as f:
        header = bytearray(f.read(_HEADER_LEN))
        struct.pack_into("<Q", header, root_offset, page_id)
        struct.pack_into(
            "<I", header, _HEADER_CHECKSUM_OFFSET,
            zlib.crc32(bytes(header[:_HEADER_CHECKSUM_OFFSET])),
        )
        f.seek(0)
        f.write(header)


def _keep_the_leaf_where(graph_path, root_offset, shape_holds):
    """_keep_rightmost_leaf, but keeping the rightmost leaf whose single-leaf
    index makes `shape_holds(db)` true, found by trying each leaf on a copy.

    Which leaf an entity's entries land in is not a property of the graph's
    content: minigraf 2.0.2's save() copies untouched leaves and repacks only
    those receiving new entries (#315), so leaf boundaries depend on how many
    checkpoints the writes were split across -- and ingestion's checkpoints
    are clock-gated (_CheckpointPolicy). Since #379 a run no longer
    checkpoints on every lease drop, and slower CI runners produced layouts
    where the rightmost leaf held only :ingestion/last-run-at, then (choosing
    the last leaf holding the watermark's UUID) one holding the watermark's
    other attributes but not :hash. Searching for the shape the caller needs
    removes the layout assumption entirely; the caller still asserts that
    shape, as its positive control.
    """
    import shutil
    probe = str(graph_path) + ".leafprobe"
    for page_id in reversed(_eavt_leaf_chain(graph_path, root_offset)):
        shutil.copyfile(graph_path, probe)
        _point_root_at(probe, root_offset, page_id)
        db = MiniGrafDb.open(probe)
        try:
            ok = shape_holds(db)
        finally:
            del db
        os.remove(probe)
        if ok:
            _point_root_at(graph_path, root_offset, page_id)
            return
    raise AssertionError("no single EAVT leaf yields the requested shape")


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


def _eavt_types(db, ident):
    """ident's :entity-type values through a keyword-literal entity pattern
    (EAVT) -- an independent witness, not the function under test."""
    raw = db.execute(f"(query [:find ?t :where [{ident} :entity-type ?t]])")
    return {row[0] for row in json.loads(raw)["results"]}


def _aevt_types(db, ident):
    """ident's :entity-type values through an attribute-only scan (AEVT),
    filtered in Python -- an independent witness, not the function under
    test."""
    raw = db.execute("(query [:find ?e ?t :where [?e :entity-type ?t]])")
    entity = str(_entity_uuid(ident))
    return {t for e, t in json.loads(raw)["results"] if e == entity}


def _build_same_transaction_types_graph(graph, tag):
    """A HEALTHY graph, written through the public handler in one transact:
    30 single-typed :decision/ fillers plus one entity typed both
    :type/decision and :type/constraint. Returns that entity's ident."""
    x = f":decision/x{tag}"
    facts = [
        f"[{x} :entity-type :type/decision]",
        f"[{x} :entity-type :type/constraint]",
        f'[{x} :description "two types"]',
    ]
    for i in range(30):
        facts.append(f"[:decision/f{tag}-{i} :entity-type :type/decision]")
        facts.append(f'[:decision/f{tag}-{i} :description "f"]')
    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph))
    try:
        result = mcp_server.handle_minigraf_transact(
            "[" + " ".join(facts) + "]", "same-transaction types"
        )
    finally:
        mcp_server._reset_db_state()
    assert result["ok"], result
    return x


def _open_graph_with_same_transaction_types(tmp_path):
    """Open a healthy graph holding one entity typed :type/decision AND
    :type/constraint in ONE transact; returns (handle, ident)."""
    graph = tmp_path / "h.graph"
    x = _build_same_transaction_types_graph(graph, "h0")
    return MiniGrafDb.open(str(graph)), x


def _inject_single_eavt_value(monkeypatch, x, keep):
    """Make the check's EAVT probe of x return only `keep`, as minigraf 2.0.0
    did for a same-transaction multi-valued attribute.

    2.0.0's selective_fact_fetch deduped on (entity, attribute, tx_count,
    asserted), a key with no value bytes, so each index kept whichever
    same-transaction value its unstable sort put first and AEVT and EAVT could
    each return a DIFFERENT single value on a healthy graph (21 of 40 graphs
    measured). 2.0.2 (minigraf #380) dropped that dedup, so a real graph no
    longer produces the disagreement and it has to be injected. The answer is
    minigraf's REAL response with rows filtered out, so the check's own
    parsing still runs. Returns the list of population scans observed.
    """
    probe = (
        f'(query [:find ?t :where [#uuid "{_entity_uuid(x)}" :entity-type ?t]])'
    )
    real_execute = mcp_server._db_execute
    population_scans = []

    def execute(handle, datalog):
        raw = real_execute(handle, datalog)
        if datalog == mcp_server._INDEX_CROSS_CHECK_POPULATION_QUERY:
            population_scans.append(datalog)
        if datalog == probe:
            body = json.loads(raw)
            assert {r[0] for r in body["results"]} == {
                ":type/decision", ":type/constraint"
            }, "precondition: EAVT no longer returns both values"
            body["results"] = [r for r in body["results"] if r[0] == keep]
            return json.dumps(body)
        return raw

    monkeypatch.setattr(mcp_server, "_db_execute", execute)
    return population_scans


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

    def test_same_transaction_types_read_identically_through_both_indexes(
        self, tmp_path, capsys
    ):
        """Pins minigraf >= 2.0.2 (#380): both indexes return BOTH values of a
        same-transaction multi-valued attribute, so a healthy graph produces
        no disagreement at all. 2.0.0 returned one arbitrary value per index;
        if a minigraf upgrade brings that back, this goes red and the
        differing-values branch below is reachable again on real graphs.
        """
        db, x = _open_graph_with_same_transaction_types(tmp_path)
        both = {":type/decision", ":type/constraint"}
        assert _eavt_types(db, x) == both
        assert _aevt_types(db, x) == both
        capsys.readouterr()
        report = mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        assert report["population"] == 31
        assert report["probed"] == _fixed_count() + 31
        assert capsys.readouterr().err == ""

    def test_same_transaction_types_read_differently_are_not_refused(
        self, tmp_path, capsys, monkeypatch
    ):
        """Both non-empty but DIFFERENT is what minigraf 2.0.0 produced on a
        healthy graph (see _inject_single_eavt_value). Refusing it tells the
        user to discard a healthy graph. Unreachable on real graphs since
        2.0.2, kept because a minigraf regression would make it reachable
        again, and the cost of being wrong is a refused healthy graph.
        """
        db, x = _open_graph_with_same_transaction_types(tmp_path)
        _inject_single_eavt_value(monkeypatch, x, ":type/constraint")
        capsys.readouterr()
        report = mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        assert report["population"] == 31
        assert report["probed"] == _fixed_count() + 31
        assert capsys.readouterr().err == (
            "[_graph_index_cross_check] 1 entities read different "
            ":entity-type values through AEVT and EAVT; not index damage -- "
            "minigraf keeps one of several same-transaction values per index "
            "(not refused)\n"
        )

    def test_same_transaction_types_cost_no_population_rescan(
        self, tmp_path, monkeypatch
    ):
        """Both-non-empty-but-different can never refuse, so re-reading it
        buys nothing -- and the re-read is a full population scan, ~25 s at
        1.6M entities (extrapolated), paid per such entity on a HEALTHY run.
        Only an empty-vs-non-empty disagreement may cost a rescan.
        """
        db, x = _open_graph_with_same_transaction_types(tmp_path)
        population_scans = _inject_single_eavt_value(
            monkeypatch, x, ":type/constraint"
        )
        mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        assert len(population_scans) == 1

    def test_damage_confined_to_sampled_entities_is_refused(self, tmp_path):
        """Every other damaged-graph test also damages a control entity,
        which is probed first -- so a check that stopped probing the random
        sample would still pass them. Here only sampled entities are damaged.
        """
        graph = tmp_path / "g.graph"
        lowest_fixed = min(
            _entity_uuid(ident)
            for ident in mcp_server._index_cross_check_fixed_idents()
        )
        # Every filler sorts below every fixed ident in EAVT, so the kept
        # rightmost leaf holds the control entities' facts, not the fillers'.
        fillers = _filler_idents(400, keep=lambda u: u < lowest_fixed)
        _write_facts(graph, _CONTROL_FACTS + _filler_facts(fillers))
        _keep_rightmost_leaf(graph, _EAVT_ROOT_OFFSET)
        db = MiniGrafDb.open(str(graph))
        # Precondition, through keyword-literal EAVT reads: every control
        # ident that has facts still reads intact...
        assert mcp_server._graph_format_version_read(db) == 1
        assert mcp_server._watermark_query(db) == "abc"
        for ident in (
            ":ingestion/format-version", ":ingestion/watermark",
            ":ingestion/frontier-low",
        ):
            assert _eavt_types(db, ident) == _aevt_types(db, ident) != set()
        # ...while most fillers are invisible through EAVT, though AEVT
        # still lists them.
        visible = sum(1 for x in fillers if _eavt_types(db, x))
        assert visible < len(fillers) // 2
        assert all(_aevt_types(db, x) == {":type/filler"} for x in fillers[:5])
        with pytest.raises(mcp_server.GraphIndexDamageError) as exc:
            mcp_server._graph_index_cross_check(db, rng=random.Random(0))
        message = str(exc.value)
        assert ":ingestion/" not in message
        assert any(str(_entity_uuid(x)) in message for x in fillers)

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
        "status": "idle", "total": 0,
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
        # Non-control entities were probed, not just the control set.
        assert (
            second["index_cross_check"]["probed"]
            > second["index_cross_check"]["control_probed"]
        )

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
        _keep_the_leaf_where(
            graph, _EAVT_ROOT_OFFSET,
            lambda db: mcp_server._watermark_query(db) is not None
            and mcp_server._graph_format_version_read(db) is None,
        )
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
