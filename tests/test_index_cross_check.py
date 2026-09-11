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
