"""#379: ingestion holds the graph for its whole run; auto-memory hooks spool.

Real backend throughout (docs/testing-conventions.md): a real file-backed
graph, real git repos, and -- where the claim is cross-process -- a real
hook process."""
import asyncio
import json
import os
import socket
import subprocess as _subprocess
import sys
import time

import pytest

import hook_spool

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _repo(tmp_path, n=12):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "t@t.com"], ["config", "user.name", "T"]):
        _subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    for i in range(n):
        (repo / "auth.py").write_text(f"def login():\n    return {i}\n\ndef f{i}():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def graph(tmp_path, monkeypatch):
    import mcp_server
    g = tmp_path / "g.graph"
    monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(g))
    mcp_server._reset_db_state()
    mcp_server.open_db(str(g))
    monkeypatch.setattr(mcp_server, "_ingest_progress", {
        "status": "idle", "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None,
        "error_at": None, "phase": None,
    })
    yield g
    mcp_server._reset_db_state()


def _graph_descriptions(ident, valid_at=None):
    import mcp_server
    va = f' :valid-at "{valid_at}"' if valid_at else ""
    with mcp_server.db_lease() as db:
        out = json.loads(mcp_server._db_execute(
            db, f"(query [:find ?d{va} :where [{ident} :description ?d]])"
        ))
    return [r[0] for r in out.get("results", [])]


def _index_rows(graph_path, ident):
    import fact_index
    con = fact_index.open_reader(fact_index.index_path_for(str(graph_path)))
    try:
        return con.execute(
            "select value, valid_from from facts_fts where entity = ? and attribute = ':description'",
            (ident,),
        ).fetchall()
    finally:
        con.close()


def _fact(name, desc):
    return {"entity": f":decision/{name}", "entity_type": "decision",
            "attribute": ":description", "value": desc}


def _hold_graph_in_subprocess(graph_path, ready, release):
    """A separate process holding the graph's kernel lock until `release`
    exists. Publishes NO ownership hint -- the hint-less holder case."""
    script = (
        "import os, sys, time\n"
        "from minigraf import MiniGrafDb\n"
        f"db = MiniGrafDb.open({str(graph_path)!r})\n"
        f"open({str(ready)!r}, 'w').close()\n"
        f"while not os.path.exists({str(release)!r}):\n"
        "    time.sleep(0.01)\n"
    )
    proc = _subprocess.Popen([sys.executable, "-c", script])
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None
        assert time.monotonic() < deadline, "the holder never opened the graph"
        time.sleep(0.01)
    return proc


# ---------------------------------------------------------------------------
# hook_spool: file layer
# ---------------------------------------------------------------------------

class TestSpoolFiles:
    def test_round_trip_oldest_first(self, tmp_path):
        g = str(tmp_path / "g.graph")
        a = hook_spool.write_record(g, [_fact("a", "first")], "2026-01-01T00:00:00.000Z")
        b = hook_spool.write_record(g, [_fact("b", "second")], "2026-01-02T00:00:00.000Z")
        got = list(hook_spool.pending(g))
        assert [p for p, _ in got] == [a, b]
        assert got[0][1] == ([_fact("a", "first")], "2026-01-01T00:00:00.000Z")
        hook_spool.remove(a)
        hook_spool.remove(a)  # at-least-once: a second remove is fine
        assert [p for p, _ in hook_spool.pending(g)] == [b]

    def test_no_spool_dir_yields_nothing(self, tmp_path):
        assert list(hook_spool.pending(str(tmp_path / "g.graph"))) == []

    def test_a_record_still_being_written_is_invisible(self, tmp_path):
        g = str(tmp_path / "g.graph")
        os.makedirs(hook_spool.spool_dir_for(g))
        with open(os.path.join(hook_spool.spool_dir_for(g), ".x.tmp"), "w") as f:
            f.write("{")
        assert list(hook_spool.pending(g)) == []

    @pytest.mark.parametrize("content", [
        "{not json",
        json.dumps({"v": 99, "valid_from": "x", "facts": []}),
        json.dumps({"v": 1, "valid_from": "x", "facts": [{"entity": ":a/b"}]}),
        json.dumps({"v": 1, "facts": []}),
    ])
    def test_malformed_record_parses_to_none(self, tmp_path, content):
        g = str(tmp_path / "g.graph")
        os.makedirs(hook_spool.spool_dir_for(g))
        path = os.path.join(hook_spool.spool_dir_for(g), "1.json")
        with open(path, "w") as f:
            f.write(content)
        assert list(hook_spool.pending(g)) == [(path, None)]
        hook_spool.quarantine(path)
        assert list(hook_spool.pending(g)) == []
        assert os.path.exists(path + ".bad")


# ---------------------------------------------------------------------------
# the drain
# ---------------------------------------------------------------------------

class TestDrain:
    def test_drain_applies_at_the_records_own_valid_time(self, graph):
        """A fact spooled during a run is stored with the time it was
        learned, not the time it was drained."""
        import mcp_server
        hook_spool.write_record(str(graph), [_fact("spoolvt", "learned early")],
                                "2026-01-01T00:00:00.000Z")
        with mcp_server.db_lease() as db:
            got = mcp_server._drain_hook_spool(db)
        assert got == {"records": 1, "facts": 1, "quarantined": 0}
        assert _graph_descriptions(":decision/spoolvt", "2026-01-02T00:00:00Z") == ["learned early"]
        assert _graph_descriptions(":decision/spoolvt", "2025-12-31T00:00:00Z") == []
        assert list(hook_spool.pending(str(graph))) == []
        assert [r[0] for r in _index_rows(graph, ":decision/spoolvt")] == ["learned early"]

    def test_redraining_an_applied_record_writes_nothing_new(self, graph):
        """At-least-once delivery: a crash between apply and remove re-applies
        the record. Same (e, a, v, valid-from): one index row, and the graph
        still answers with one value at every valid time."""
        import mcp_server
        facts, vf = [_fact("redrain", "once")], "2026-01-01T00:00:00.000Z"
        with mcp_server.db_lease() as db:
            assert mcp_server._apply_extracted_facts(db, facts, vf) == 1
            assert mcp_server._apply_extracted_facts(db, facts, vf) == 1
            # (count ?d) counts matching ROWS, so a duplicated fact reads 2.
            rows = json.loads(mcp_server._db_execute(
                db, "(query [:find (count ?d) :where [:decision/redrain :description ?d]])"
            ))
        assert len(_index_rows(graph, ":decision/redrain")) == 1
        assert rows.get("results") == [[1]], rows

    def test_a_bad_record_is_quarantined_and_the_rest_still_drain(self, graph):
        import mcp_server
        spool = hook_spool.spool_dir_for(str(graph))
        hook_spool.write_record(str(graph), [_fact("before", "a")], "2026-01-01T00:00:00.000Z")
        bad = os.path.join(spool, "00000000000000000001-0-bad.json")
        with open(bad, "w") as f:
            f.write("{oops")
        hook_spool.write_record(str(graph), [_fact("after", "b")], "2026-01-01T00:00:00.000Z")
        with mcp_server.db_lease() as db:
            got = mcp_server._drain_hook_spool(db)
        assert got == {"records": 2, "facts": 2, "quarantined": 1}
        assert os.path.exists(bad + ".bad")
        assert _graph_descriptions(":decision/before") == ["a"]
        assert _graph_descriptions(":decision/after") == ["b"]

    def test_schema_invalid_facts_are_validated_at_drain(self, graph):
        """The spool stores the batch unvalidated; the drain must apply the
        same closed-world check the direct path does."""
        import mcp_server
        bad = {"entity": ":decision/norequired", "entity_type": "decision",
               "attribute": ":rationale", "value": "no description anywhere"}
        hook_spool.write_record(str(graph), [bad], "2026-01-01T00:00:00.000Z")
        with mcp_server.db_lease() as db:
            got = mcp_server._drain_hook_spool(db)
        assert got == {"records": 1, "facts": 0, "quarantined": 0}


# ---------------------------------------------------------------------------
# finalize: when to spool
# ---------------------------------------------------------------------------

class TestFinalizeSpools:
    _TEXT = "we will use valkeyspool379"

    def _ident(self):
        import mcp_server
        assert mcp_server.heuristic_extract(self._TEXT), "extracts nothing -- proves nothing"
        return mcp_server._canonical_ident("decision", "valkeyspool379")

    async def test_a_fresh_ingestion_hint_spools_without_touching_the_graph(
        self, graph, monkeypatch
    ):
        import mcp_server
        ident = self._ident()
        with open(mcp_server._owner_hint_path(str(graph)), "w") as f:
            json.dump({"pid": os.getpid() + 1, "host": socket.gethostname(),
                       "purpose": "ingestion"}, f)
        monkeypatch.setattr(mcp_server, "_hook_lease_deadline", 5.0)
        acquires = []
        # Patch the CLASS, never the _lease_manager singleton (#272).
        real = mcp_server._DbLeaseManager.try_acquire
        monkeypatch.setattr(mcp_server._DbLeaseManager, "try_acquire",
                            lambda self_, *a, **kw: acquires.append(1) or real(self_, *a, **kw))
        started = time.monotonic()
        out = await mcp_server.handle_memory_finalize_turn(self._TEXT)
        assert out["ok"] and out["stored_count"] >= 1, out
        assert time.monotonic() - started < 2.0, "the hook waited instead of spooling"
        assert acquires == [], "the hook tried the graph although ingestion owns it"
        assert mcp_server._spool_target is None, "spool mode leaked past the call"
        records = list(hook_spool.pending(str(graph)))
        assert len(records) == 1
        assert {f["entity"] for f in records[0][1][0]} == {ident}

    async def test_a_held_graph_without_a_hint_spools_and_the_next_turn_drains_it(
        self, graph, tmp_path, monkeypatch
    ):
        """The hint-less fallback: the lease times out, the turn's facts are
        spooled rather than lost (the hook's `except: pass` used to eat them),
        and the next turn that does get the graph drains them -- graph AND
        index."""
        import mcp_server
        ident = self._ident()
        monkeypatch.setattr(mcp_server, "_hook_lease_deadline", 0.5)
        ready, release = tmp_path / "ready", tmp_path / "release"
        holder = _hold_graph_in_subprocess(graph, ready, release)
        try:
            out = await mcp_server.handle_memory_finalize_turn(self._TEXT)
        finally:
            release.touch()
            holder.wait(timeout=30)
        assert out["ok"], out
        assert len(list(hook_spool.pending(str(graph)))) == 1
        assert _graph_descriptions(ident) == []

        out = await mcp_server.handle_memory_finalize_turn("nothing to store here")
        assert out["ok"], out
        assert list(hook_spool.pending(str(graph))) == []
        assert _graph_descriptions(ident), "the stranded record was never drained"
        assert _index_rows(graph, ident), "drained into the graph but not the index"

    async def test_the_server_process_never_spools_and_never_drains(self, graph, monkeypatch):
        """No hook deadline = the MCP server. It may be running ingestion in
        this very process, holding the index's write transaction, so a drain
        here would open a second SQLite writer and block on it."""
        import mcp_server
        ident = self._ident()
        assert mcp_server._hook_lease_deadline is None
        hook_spool.write_record(str(graph), [_fact("leftover", "x")], "2026-01-01T00:00:00.000Z")
        out = await mcp_server.handle_memory_finalize_turn(self._TEXT)
        assert out["ok"], out
        assert _graph_descriptions(ident), "the server's own write did not land"
        assert len(list(hook_spool.pending(str(graph)))) == 1


# ---------------------------------------------------------------------------
# ingestion: the run hold and the drains
# ---------------------------------------------------------------------------

class TestRunHold:
    async def test_no_handle_is_dropped_between_stage_a_start_and_sweep_end(
        self, graph, tmp_path, monkeypatch
    ):
        """Every window boundary used to drop the handle -- a full
        O(graph size) checkpoint on minigraf 2.x. Under the run hold no
        boundary drops anything. One-commit windows make every commit a
        boundary, so the old code drops once per commit here. Ablation:
        remove the run hold's enter_async_context and this reddens."""
        import ingest_progress
        import mcp_server
        repo = _repo(tmp_path)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 1)
        marks = {}
        real_started = ingest_progress.RunProgress.stage_a_started
        real_ended = ingest_progress.RunProgress.sweep_ended
        swept = []
        real_sweep = mcp_server._correction_sweep_apply

        def started(self_, *a, **kw):
            marks["a"] = mcp_server._lease_manager.drops
            return real_started(self_, *a, **kw)

        def ended(self_, *a, **kw):
            marks["b"] = mcp_server._lease_manager.drops
            return real_ended(self_, *a, **kw)

        monkeypatch.setattr(ingest_progress.RunProgress, "stage_a_started", started)
        monkeypatch.setattr(ingest_progress.RunProgress, "sweep_ended", ended)
        monkeypatch.setattr(mcp_server, "_correction_sweep_apply",
                            lambda *a, **kw: swept.append(1) or real_sweep(*a, **kw))
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress
        assert len(swept) >= 3, "Stage B swept too little to have crossed a boundary"
        assert "a" in marks and "b" in marks
        assert marks["b"] - marks["a"] == 0, (
            f"{marks['b'] - marks['a']} handle drops inside the walk -- each one "
            f"is a full checkpoint on minigraf 2.x (#379)"
        )
        drops = mcp_server._ingest_progress["handle_drops"]
        assert 1 <= drops["count"] <= 3, drops  # preload leases + the hold itself

    async def test_a_record_spooled_mid_sweep_is_drained_at_a_stage_b_boundary(
        self, graph, tmp_path, monkeypatch
    ):
        """Not merely at the run's final drain. Ablation: drop the Stage B
        boundary drain and the record is still pending when the sweep ends."""
        import ingest_progress
        import mcp_server
        repo = _repo(tmp_path)
        monkeypatch.setattr(mcp_server, "_SWEEP_YIELD_COMMITS", 1)
        seen = {}
        real_planned = ingest_progress.RunProgress.sweep_planned
        real_ended = ingest_progress.RunProgress.sweep_ended

        def planned(self_, *a, **kw):
            hook_spool.write_record(str(graph), [_fact("midsweep", "b")], mcp_server._now_utc_ms())
            return real_planned(self_, *a, **kw)

        def ended(self_, *a, **kw):
            seen["pending_at_sweep_end"] = len(list(hook_spool.pending(str(graph))))
            return real_ended(self_, *a, **kw)

        monkeypatch.setattr(ingest_progress.RunProgress, "sweep_planned", planned)
        monkeypatch.setattr(ingest_progress.RunProgress, "sweep_ended", ended)
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress
        assert seen.get("pending_at_sweep_end") == 0, seen
        assert mcp_server._ingest_progress["hook_spool"]["records"] == 1
        assert _graph_descriptions(":decision/midsweep") == ["b"]
        assert _index_rows(graph, ":decision/midsweep")

    async def test_a_record_left_after_the_last_boundary_is_drained_at_run_end(
        self, graph, tmp_path, monkeypatch
    ):
        import ingest_progress
        import mcp_server
        repo = _repo(tmp_path, n=3)
        real_ended = ingest_progress.RunProgress.sweep_ended

        def ended(self_, *a, **kw):
            hook_spool.write_record(str(graph), [_fact("atend", "c")], mcp_server._now_utc_ms())
            return real_ended(self_, *a, **kw)

        monkeypatch.setattr(ingest_progress.RunProgress, "sweep_ended", ended)
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress
        assert list(hook_spool.pending(str(graph))) == []
        assert _graph_descriptions(":decision/atend") == ["c"]
        assert _index_rows(graph, ":decision/atend")


class TestRealFinalizeHookDuringIngestion:
    """hooks/finalize_hook.py itself, as Claude Code runs it, mid-Stage A.

    Replaces #366's TestHooksCatchLeaseWindowReleases: the hook no longer
    lands in a window release (there is none), it spools on ingestion's
    ownership hint and a Stage A boundary drains it. Shipped window and hook
    constants; only each Stage A write is slowed so Stage A outlasts a
    boundary."""

    async def test_the_hook_spools_fast_and_a_stage_a_boundary_lands_its_fact(
        self, graph, tmp_path, monkeypatch
    ):
        import ingest_progress
        import mcp_server
        repo = _repo(tmp_path)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({"role": "user", "content": "we will use valkeyhook379"}) + "\n")
        ident = mcp_server._canonical_ident("decision", "valkeyhook379")
        assert mcp_server.heuristic_extract("we will use valkeyhook379")
        hook = os.path.join(_REPO_ROOT, "hooks", "finalize_hook.py")
        go, ready = tmp_path / "go", tmp_path / "ready"
        script = (
            "import io, json, os, runpy, sys, time\n"
            f"sys.path.insert(0, {_REPO_ROOT!r})\n"
            "import mcp_server\n"
            f"open({str(ready)!r}, 'w').close()\n"
            f"while not os.path.exists({str(go)!r}):\n"
            "    time.sleep(0.002)\n"
            "t0 = time.time()\n"
            f"sys.stdin = io.StringIO(json.dumps({{'transcript_path': {str(transcript)!r}}}))\n"
            f"runpy.run_path({hook!r}, run_name='__main__')\n"
            "sys.stderr.write('HOOKTIME %f %f\\n' % (t0, time.time()))\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGRAF_")}
        env["MINIGRAF_GRAPH_PATH"] = str(graph)
        env["MINIGRAF_EXTRACTION_STRATEGY"] = "heuristic"
        proc = _subprocess.Popen([sys.executable, "-c", script], stdout=_subprocess.PIPE,
                                 stderr=_subprocess.PIPE, text=True, env=env)
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)

        state = {}
        real_fwd, real_rev = mcp_server._forward_apply, mcp_server._reverse_apply
        real_finished = ingest_progress.RunProgress.stage_a_finished

        def slow(real):
            def spy(*a, **kw):
                out = real(*a, **kw)
                if not kw.get("lifecycle_only"):
                    go.touch()
                    time.sleep(0.4)
                return out
            return spy

        def finished(self_, *a, **kw):
            state["stage_a_end"] = time.time()
            state["drained_in_stage_a"] = mcp_server._ingest_progress["hook_spool"]["records"]
            return real_finished(self_, *a, **kw)

        monkeypatch.setattr(mcp_server, "_forward_apply", slow(real_fwd))
        monkeypatch.setattr(mcp_server, "_reverse_apply", slow(real_rev))
        monkeypatch.setattr(ingest_progress.RunProgress, "stage_a_finished", finished)
        try:
            await mcp_server._run_ingestion(str(repo), "master")
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress
        times = [ln.split() for ln in err.splitlines() if ln.startswith("HOOKTIME")]
        assert times, f"the hook never ran to completion: {err[-2000:]!r}"
        t0, t1 = float(times[-1][1]), float(times[-1][2])
        assert t1 < state["stage_a_end"], "the hook finished after Stage A -- proved nothing"
        assert t1 - t0 < 3.0, f"the hook took {t1 - t0:.2f}s: it waited instead of spooling"
        assert state["drained_in_stage_a"] >= 1, (
            "the spooled record was not drained at a Stage A boundary"
        )
        assert _graph_descriptions(ident), f"fact never reached the graph: {err[-2000:]!r}"
        assert _index_rows(graph, ident), "fact reached the graph but not the index (#302)"
