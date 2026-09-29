"""#239: the in-run cache of lineage point queries.

Every test runs against a real file-backed graph, and every cache HIT is
checked against the real query by `_LINEAGE_CACHE_VERIFY` (switched on for the
whole suite in conftest.py). So the assertions here are mostly about WHEN the
cache serves, because verify mode already fails any hit that serves the wrong
thing -- see test_verify_mode_catches_a_stale_entry, which is what proves that.
"""

import asyncio
import json
import os
import struct
import subprocess
import sys
import threading
import time

import pytest

import mcp_server as m

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TS = "2020-01-01T00:00:00Z"


@pytest.fixture
def graph(tmp_path, monkeypatch):
    path = tmp_path / "g.graph"
    monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(path))
    m._reset_db_state()
    m.open_db(str(path))
    m._lineage_cache.enable()
    m._lineage_cache.stats.update(hits=0, misses=0, clears=0)
    yield path
    m._lineage_cache.disable()
    m._reset_db_state()


def _read(entity, attr=":introduced-by"):
    with m.db_lease() as db:
        return m._point_query_values(db, entity, attr)


def _write(facts, valid_from=_TS, valid_to=None):
    with m.db_lease() as db:
        m._transact(db, facts, valid_from, valid_to=valid_to)


def _retract(facts):
    with m.db_lease() as db:
        m._retract(db, facts)


def _stats():
    return dict(m._lineage_cache.stats)


def _foreign(graph, body):
    """Run `body` in a separate process holding its own handle on the graph."""
    script = (
        "import sys\n"
        "from minigraf import MiniGrafDb\n"
        f"db = MiniGrafDb.open({str(graph)!r})\n"
        + body
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)


class TestWriteThrough:
    def test_a_read_after_a_transact_is_served_from_the_cache(self, graph):
        with m.db_lease() as db:
            assert m._point_query_values(db, ":function/f", ":introduced-by") == []
            m._transact(db, "[[:function/f :introduced-by :commit/c1]]", _TS)
            before = _stats()
            assert m._point_query_values(db, ":function/f", ":introduced-by") == [":commit/c1"]
            assert _stats()["hits"] == before["hits"] + 1

    def test_a_retract_removes_the_value_from_the_cache(self, graph):
        with m.db_lease() as db:
            # one per call: minigraf#287 keeps only the last of a batch
            m._transact(db, "[[:function/f :modified-in :commit/c1]]", _TS)
            m._transact(db, "[[:function/f :modified-in :commit/c2]]", _TS)
            assert m._point_query_values(db, ":function/f", ":modified-in") == [":commit/c1", ":commit/c2"]
            m._retract(db, "[[:function/f :modified-in :commit/c1]]")
            before = _stats()
            assert m._point_query_values(db, ":function/f", ":modified-in") == [":commit/c2"]
            assert _stats()["hits"] == before["hits"] + 1

    def test_a_retract_of_a_twice_asserted_value_removes_it(self, graph):
        # Measured: minigraf's retract removes the value even when it was
        # transacted twice at different valid-froms. Write-through's discard
        # mirrors that; verify mode fails the hit if it ever stops being true.
        with m.db_lease() as db:
            m._transact(db, "[[:function/f :introduced-by :commit/c1]]", _TS)
            m._transact(db, "[[:function/f :introduced-by :commit/c1]]", "2021-01-01T00:00:00Z")
            assert m._point_query_values(db, ":function/f", ":introduced-by") == [":commit/c1"]
            m._retract(db, "[[:function/f :introduced-by :commit/c1]]")
            assert m._point_query_values(db, ":function/f", ":introduced-by") == []

    @pytest.mark.parametrize("valid_from,valid_to", [
        ("2099-01-01T00:00:00Z", None),           # future: not visible now
        ("2019-01-01T00:00:00Z", "2019-06-01T00:00:00Z"),  # bounded: closed window
    ])
    def test_a_write_the_cache_cannot_model_drops_the_key(self, graph, valid_from, valid_to):
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":modified-in")
            m._transact(db, "[[:function/f :modified-in :commit/x]]", valid_from, valid_to=valid_to)
            before = _stats()
            assert m._point_query_values(db, ":function/f", ":modified-in") == []
            assert _stats()["misses"] == before["misses"] + 1

    def test_a_string_value_with_an_escape_drops_the_key(self, graph):
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":ident")
            m._transact(db, '[[:function/f :ident "a\\"b"]]', _TS)
            before = _stats()
            assert m._point_query_values(db, ":function/f", ":ident") == ['a"b']
            assert _stats()["misses"] == before["misses"] + 1

    def test_a_uuid_entity_write_drops_every_key_on_that_attribute(self, graph):
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":modified-in")
            m._point_query_values(db, ":function/f", ":introduced-by")
            m._transact(db, '[[#uuid "00000000-0000-0000-0000-000000000001" :modified-in :commit/x]]', _TS)
            before = _stats()
            m._point_query_values(db, ":function/f", ":modified-in")
            m._point_query_values(db, ":function/f", ":introduced-by")
            after = _stats()
            assert after["misses"] == before["misses"] + 1
            assert after["hits"] == before["hits"] + 1

    def test_a_write_that_raises_drops_the_touched_keys(self, graph, monkeypatch):
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":modified-in")
            real = m._db_execute

            def boom(d, datalog):
                if datalog.startswith("(transact"):
                    raise RuntimeError("injected")
                return real(d, datalog)

            monkeypatch.setattr(m, "_db_execute", boom)
            with pytest.raises(RuntimeError):
                m._transact(db, "[[:function/f :modified-in :commit/x]]", _TS)
            monkeypatch.setattr(m, "_db_execute", real)
            before = _stats()
            m._point_query_values(db, ":function/f", ":modified-in")
            assert _stats()["misses"] == before["misses"] + 1

    def test_a_watched_triple_the_pattern_cannot_parse_clears_the_cache(self, graph):
        """_FACTS_TRIPLE_PATTERN needs `]` right after the value, so a spaced
        triple is a real write the cache cannot attribute to a key."""
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":modified-in")
            m._point_query_values(db, ":function/g", ":introduced-by")
            m._transact(db, "[[:function/f :modified-in :commit/x ]]", _TS)
            before = _stats()
            assert m._point_query_values(db, ":function/f", ":modified-in") == [":commit/x"]
            m._point_query_values(db, ":function/g", ":introduced-by")
            assert _stats()["misses"] == before["misses"] + 2

    def test_a_watched_attribute_inside_a_string_value_is_not_a_write(self, graph):
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":modified-in")
            m._transact(db, '[[:commit/c :description "fix :modified-in handling"]]', _TS)
            before = _stats()
            m._point_query_values(db, ":function/f", ":modified-in")
            assert _stats()["hits"] == before["hits"] + 1

    def test_an_unwatched_attribute_is_never_cached(self, graph):
        with m.db_lease() as db:
            m._point_query_values(db, ":function/f", ":description")
            m._point_query_values(db, ":function/f", ":description")
        assert _stats()["hits"] == 0

    def test_inactive_cache_stores_nothing(self, graph):
        m._lineage_cache.disable()
        _write("[[:function/f :introduced-by :commit/c1]]")
        assert _read(":function/f") == [":commit/c1"]
        assert _read(":function/f") == [":commit/c1"]
        assert _stats()["hits"] == 0
        assert not m._lineage_cache.entries


class TestVerifyMode:
    def test_verify_mode_catches_a_stale_entry(self, graph):
        """The positive control for every other test in this file: a poisoned
        entry must fail loudly, or verify mode proves nothing."""
        assert m._LINEAGE_CACHE_VERIFY, "conftest must switch verify mode on"
        _write("[[:function/f :introduced-by :commit/c1]]")
        assert _read(":function/f") == [":commit/c1"]
        m._lineage_cache.entries[(":function/f", ":introduced-by")] = {":commit/wrong"}
        with pytest.raises(AssertionError, match="lineage cache"):
            _read(":function/f")


class TestAcrossHandles:
    def test_the_cache_survives_our_own_drop_and_reopen(self, graph):
        """Written and read in ONE lease, so the drop has a pending WAL entry.
        That is what makes the explicit pre-drop checkpoint observable: a
        stamp taken with the entry still in the WAL no longer matches the
        header the drop-time checkpoint then writes, and the cache would be
        thrown away on every window boundary (a performance loss, not a
        correctness one -- the correctness half, no foreign write between
        checkpoint and stamp, holds by construction and cannot be timed)."""
        with m.db_lease() as db:
            m._transact(db, "[[:function/f :introduced-by :commit/c1]]", _TS)
            assert m._point_query_values(db, ":function/f", ":introduced-by") == [":commit/c1"]
        assert m._lease_manager.lease_count == 0  # the handle really dropped
        before = _stats()
        assert _read(":function/f") == [":commit/c1"]
        assert _stats()["hits"] == before["hits"] + 1

    def test_a_foreign_read_keeps_the_cache(self, graph):
        _write("[[:function/f :introduced-by :commit/c1]]")
        _read(":function/f")
        _foreign(graph, "db.execute('(query [:find ?c :where [:function/f :introduced-by ?c]])')\n")
        before = _stats()
        _read(":function/f")
        assert _stats()["hits"] == before["hits"] + 1

    @pytest.mark.parametrize("body", [
        # checkpointed on the foreign handle's drop: the header count moves
        "db.execute('(transact {:valid-from \"2020-01-01T00:00:00Z\"} [[:function/f :introduced-by :commit/c2]])')\n",
        # retract only: the header count moves too
        "db.execute('(retract [[:function/f :introduced-by :commit/c1]])')\n",
        # killed before its drop: the write survives only in the WAL
        "db.execute('(transact {:valid-from \"2020-01-01T00:00:00Z\"} [[:function/f :introduced-by :commit/c2]])')\n"
        "import os; os._exit(0)\n",
    ], ids=["transact", "retract", "killed-wal-only"])
    def test_a_foreign_write_clears_the_cache(self, graph, body):
        _write("[[:function/f :introduced-by :commit/c1]]")
        _read(":function/f")
        _foreign(graph, body)
        before = _stats()
        # verify mode would also fail a stale hit; the miss is the direct claim
        _read(":function/f")
        assert _stats()["misses"] == before["misses"] + 1
        assert _stats()["hits"] == before["hits"]

    def test_an_unrecognised_header_clears_the_cache(self, graph, monkeypatch):
        _write("[[:function/f :introduced-by :commit/c1]]")
        _read(":function/f")
        monkeypatch.setattr(m, "_GRAPH_HEADER_FORMAT_VERSION", 999)
        before = _stats()
        _read(":function/f")
        assert _stats()["misses"] == before["misses"] + 1


class TestGraphStamp:
    def test_stamp_reads_the_v7_header(self, graph):
        _write("[[:function/f :introduced-by :commit/c1]]")
        stamp = m._graph_stamp(str(graph))
        raw = open(graph, "rb").read(32)
        assert raw[:4] == b"MGRF"
        assert stamp == (struct.unpack_from("<Q", raw, 24)[0], None)
        assert stamp[0] >= 1

    def test_stamp_moves_on_a_foreign_write_and_not_on_a_read(self, graph):
        """Pins the minigraf behaviour the whole cross-handle design rests on."""
        _write("[[:function/f :introduced-by :commit/c1]]")
        s0 = m._graph_stamp(str(graph))
        _foreign(graph, "db.execute('(query [:find ?c :where [?e :introduced-by ?c]])')\n")
        assert m._graph_stamp(str(graph)) == s0
        _foreign(graph, "db.execute('(retract [[:function/f :introduced-by :commit/c1]])')\n")
        s1 = m._graph_stamp(str(graph))
        assert s1 != s0 and s1[0] > s0[0]

    def test_stamp_is_none_for_a_missing_or_foreign_file(self, tmp_path):
        assert m._graph_stamp(str(tmp_path / "absent.graph")) is None
        junk = tmp_path / "junk.graph"
        junk.write_bytes(b"NOPE" + b"\0" * 60)
        assert m._graph_stamp(str(junk)) is None


class TestFillIsAtomicWithWrites:
    def test_a_write_racing_a_fill_cannot_leave_a_stale_entry(self, graph, monkeypatch):
        """A read's query, and the store of its result, must be one critical
        section with every write. Otherwise a write landing between them is
        written through to a key that is not cached yet (a no-op), and the
        fill then stores the PRE-write result."""
        _write("[[:function/f :modified-in :commit/c1]]")
        real = m._db_execute
        writer_done = threading.Event()
        errors = []

        def writer():
            try:
                _write("[[:function/f :modified-in :commit/c2]]")
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)
            finally:
                writer_done.set()

        def racing(db, datalog):
            out = real(db, datalog)
            if datalog.startswith("(query") and ":modified-in" in datalog and not writer_done.is_set():
                t = threading.Thread(target=writer)
                t.start()
                # Give the writer every chance to finish inside the fill.
                writer_done.wait(0.5)
            return out

        with m.db_lease() as db:
            monkeypatch.setattr(m, "_db_execute", racing)
            m._point_query_values(db, ":function/f", ":modified-in")
            monkeypatch.setattr(m, "_db_execute", real)
        assert writer_done.wait(10)
        assert not errors
        assert _read(":function/f", ":modified-in") == [":commit/c1", ":commit/c2"]


class TestIngestion:
    def _repo(self, tmp_path, n=8):
        repo = tmp_path / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
        run("git", "init", "-b", "master")
        run("git", "config", "user.email", "t@t.com")
        run("git", "config", "user.name", "T")
        for i in range(n):
            body = f"def login():\n    return {i}\n"
            if i % 3 != 1:  # remove and re-add `helper`: rebirths (#349)
                body += f"\ndef helper():\n    return {i}\n"
            (repo / "auth.py").write_text(body)
            (repo / f"m{i % 2}.py").write_text(f"X = {i}\n")
            run("git", "add", ".")
            run("git", "commit", "-m", f"c{i}")
        return repo

    async def _ingest(self, repo, graph, monkeypatch):
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph))
        m._reset_db_state()
        m.open_db(str(graph))
        m._ingest_progress = {
            "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
            "error": None, "owner_pid": None, "error_at": None, "phase": None,
        }
        await m._run_ingestion(str(repo), "master")
        assert m._ingest_progress["status"] == "complete", m._ingest_progress
        m._reset_db_state()

    def _dump(self, graph):
        from minigraf import MiniGrafDb
        db = MiniGrafDb.open(str(graph))
        try:
            out = {}
            for label, q in {
                "now": "(query [:find ?e ?a ?v :where [?e ?a ?v]])",
                "history": "(query [:find ?e ?a ?v :any-valid-time :where [?e ?a ?v]])",
            }.items():
                rows = json.loads(m._db_execute(db, q)).get("results", [])
                out[label] = sorted(
                    tuple(str(x) for x in r) for r in rows
                    # run bookkeeping carries wall-clock values
                    if not str(r[0]).startswith((":ingestion/",))
                    and r[1] not in (":last-run-at",)
                )
            return out
        finally:
            db = None

    @pytest.mark.asyncio
    async def test_ingestion_uses_the_cache_and_writes_what_it_writes_without_it(
        self, tmp_path, monkeypatch
    ):
        repo = self._repo(tmp_path)
        m._lineage_cache.stats.update(hits=0, misses=0)
        await self._ingest(repo, tmp_path / "on.graph", monkeypatch)
        hits_on = m._lineage_cache.stats["hits"]
        # positive control: the cache really served this run, under verify mode
        assert hits_on > 0
        assert not m._lineage_cache.active and not m._lineage_cache.entries

        monkeypatch.setattr(m, "_LINEAGE_CACHE_ENABLED", False)
        m._lineage_cache.stats.update(hits=0, misses=0)
        await self._ingest(repo, tmp_path / "off.graph", monkeypatch)
        assert m._lineage_cache.stats["hits"] == 0

        on, off = self._dump(tmp_path / "on.graph"), self._dump(tmp_path / "off.graph")
        assert on["now"], "the parity comparison compared nothing"
        for label in on:
            assert on[label] == off[label], label

    @pytest.mark.asyncio
    async def test_the_cache_is_disabled_after_a_failed_run(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, n=3)

        def boom(*a, **kw):
            raise RuntimeError("injected")

        monkeypatch.setattr(m, "_correction_sweep_select_position", boom)
        graph = tmp_path / "g.graph"
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph))
        m._reset_db_state()
        m.open_db(str(graph))
        m._ingest_progress = {
            "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
            "error": None, "owner_pid": None, "error_at": None, "phase": None,
        }
        await m._run_ingestion(str(repo), "master")
        assert not m._lineage_cache.active and not m._lineage_cache.entries
        m._reset_db_state()
