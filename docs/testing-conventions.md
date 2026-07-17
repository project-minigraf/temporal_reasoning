# Testing Conventions

## Real backend, always

Every test in `tests/test_mcp_server.py` uses a real `minigraf` backend —
never a `MagicMock`-based fake of `MiniGrafDb`. There are two patterns for
getting a real handle, depending on what the test needs.

### Pattern 1: `real_db` (in-memory, the default)

Most tests use the `real_db` fixture, which opens a genuine
`MiniGrafDb.open_in_memory()` instance (redirected via a
`monkeypatch.setattr(MiniGrafDb, "open", ...)` so `mcp_server.open_db()`'s
real code path — session-rule registration, mtime tracking — still runs):

```python
@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Open a real (non-mocked) in-memory MiniGrafDb for the duration of the test.
    Full Datalog parsing, schema validation, and bi-temporal semantics — just
    backed by open_in_memory() instead of a disk file, so tests stay fast."""
    from minigraf import MiniGrafDb
    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()
```

This exists because a `MagicMock`-based fake of `MiniGrafDb` never parses or
validates the Datalog string passed to `execute()` — it just records call
arguments and returns a canned response. That blind spot hid a real bug for
months: `mcp_server.py`'s valid-time-bounded `(transact ...)` calls
constructed the command with facts and options in the wrong order, silently
making minigraf ignore `:valid-from`/`:valid-to` bounds. No mocked test could
catch this, because none of them ever asked minigraf to actually parse the
string.

### Pattern 2: real file-backed DB (multi-commit / persistence tests)

`real_db`'s `open_in_memory()` hands back a brand-new, isolated store on
every open, so it can't model anything that needs to survive across
separate `MiniGrafDb.open()` calls at the same path — e.g. the git-ingestion
tests that check a watermark written in one `_run_ingestion` call is visible
to a later one, or that a lock is genuinely released between commits. For
those, tests open a real, disk-backed `MiniGrafDb.open()` against a
`tmp_path` graph file directly (no `real_db` fixture), so the same on-disk
graph persists across open/close cycles exactly as it would in production.
See `TestRunIngestionShutdown.test_resumes_from_watermark_after_shutdown`
(two `_run_ingestion` calls against the same on-disk graph, second one
resuming from the first's watermark) and `TestClosedEntityLifecyclePurge`
(ingests, drops `mcp_server._db` to release the lock, then reopens the same
path with `MiniGrafDb.open()` to query post-ingestion state) for worked
examples.

## Always verify results

Never assert on mock call arguments (`"transact" in str(call)`,
`assert_called_once()`). Always re-query `real_db` (or the file-backed DB)
after the code under test runs, and assert on the actual persisted or
returned facts. For bi-temporal code specifically, verify with
`:valid-at`/`:as-of` queries at multiple points in time — before, during,
and after the fact's valid-time window — not just "does it exist right
now," which behaves like `:any-valid-time` regardless of bounds and would
not have caught the argument-order bug either.

## The one narrow exception: external, non-minigraf APIs

Mocking survives only for genuinely external network services (or
third-party parsing libraries) unrelated to minigraf: LLM provider clients
(OpenAI/Anthropic, in `TestCallLlm`, `TestLlmStrategyOpenAI`,
`TestLlmStrategy`, and `TestAgentStrategy`) and GitHub API calls (in
`TestMinigrafReportIssue`, via the `report_issue` module).
`TestGetParser` similarly mocks the optional `tree_sitter`/
`tree_sitter_python` packages, since those are a separate third-party
dependency with no minigraf involvement at all. These stay mocked to avoid
real API cost, network dependency, non-deterministic model output, and an
optional native-extension install requirement in CI — the underlying
`MiniGrafDb` in every one of these tests is still always real (or, for
`TestGetParser`, simply not involved).

## Manufacturing real error conditions instead of faking them

The DB lock-retry cluster (`TestGetDbLockRetry`, `TestTryOpenWithSelfHealReuse`,
`TestOpenDbAtWithExtendedRetry`) needs genuine lock contention, which a mock
used to fake via a canned `MiniGrafError`. Locking is inherently file-based,
so these tests use a real file-backed `MiniGrafDb.open()` plus a helper that
spawns a subprocess to hold (or briefly open-then-release, for stale-lock
scenarios) a real lock on the same path — producing the exact real
`MiniGrafError` message (`"Database is locked by another process (lock file:
..., holder PID: ...)"`) that `mcp_server._stale_lock_holder_pid`/
`_pid_is_alive` parse. Only `mcp_server.time.sleep` is monkeypatched, purely
to skip real backoff delays — that's test-speed plumbing, not faking
minigraf's behavior. This is the pattern to reach for whenever a future test
needs a real failure condition rather than a business-logic result: prefer
manufacturing the condition for real over mocking the exception.

## Observing real calls without faking them: `execute_spy()`

Some tests need to assert that `execute()` was or wasn't called (e.g. "must
not query the graph while status is running") without faking any return
value. Use `execute_spy()`, which wraps the real `mcp_server._db_execute` to
record calls while still executing them for real — this is not a mock in
the sense this convention eliminates; it never fakes a return value or
bypasses real parsing.
