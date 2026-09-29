"""Shared pytest configuration. All other fixtures live in test_mcp_server.py."""

import os

import pytest


@pytest.fixture(autouse=True)
def _scrub_ambient_minigraf_env(monkeypatch):
    """Remove every ``MINIGRAF_*`` variable inherited from the developer's shell.

    Each of these is a knob on production behaviour, so inheriting one only ever
    changes what a test measures, silently. A test that wants a value sets it
    itself with ``monkeypatch.setenv`` -- which runs in the test body, after this
    fixture, and therefore still wins.

    This is not hypothetical. ``.claude/settings.local.json`` exports
    ``MINIGRAF_NO_AUTO_INGEST=1`` into every Claude Code session in this repo, and
    every pytest run started inside one inherited it. ``main()`` gates its whole
    auto-ingest and backfill startup block on that variable, so with it set
    ``_ingest_progress["status"]`` stays "idle" and ``_ingest_task`` /
    ``_backfill_task`` stay None -- exactly what TestMainAutoIngestLockCheck and
    TestMainStartupBackfill assert against. Six tests failed for the length of
    PR #328 and were misread as a Python 3.14 incompatibility, because CI carries
    no such variable and was green throughout (#331).

    The tests needing MINIGRAF_NO_AUTO_INGEST *present* always set it explicitly;
    it was the ones needing it *absent* that rested on an unstated precondition.
    That asymmetry is what this removes.

    ``MINIGRAF_EXTRACTION_STRATEGY`` is the more expensive instance of the same
    class: ``handle_memory_finalize_turn`` defaults to "heuristic", so a test
    exercising it under a session exporting "llm" would take the LLM path and a
    real network call.

    What this CANNOT reach: module-level reads, which happen at import, before
    any fixture runs. ``_OWNER_HINT_TTL``, ``_MAX_MATCH_POOL_SIZE`` and
    ``_MAX_FACT_VALUE_LENGTH`` bake an ambient value into a module constant and
    no autouse fixture can undo that. A new test depending on one of those
    defaults must patch the constant, not the variable.
    """
    for name in [key for key in os.environ if key.startswith("MINIGRAF_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _verify_every_lineage_cache_hit(monkeypatch):
    """Make the whole suite an oracle for the #239 lineage point-query cache.

    With ``_LINEAGE_CACHE_VERIFY`` on, every cache HIT also runs the real
    query and raises on a difference, so each ingestion test re-proves what
    ``evals/at_scale/probe_lineage_cache_hit_rate.py`` measured once (zero
    mismatches). A constant, not a variable: mcp_server reads it at call time.

    Imported lazily and tolerated when absent, because a regression test runs
    pytest in a subprocess against a COPY of this file (#331), where
    mcp_server may not be importable. Teardown disables the cache so a test
    that fails mid-ingestion cannot leave it active for its successor.
    """
    try:
        import mcp_server
    except ImportError:
        yield
        return
    monkeypatch.setattr(mcp_server, "_LINEAGE_CACHE_VERIFY", True)
    yield
    mcp_server._lineage_cache.disable()
