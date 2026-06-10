# Cumulative Ingest Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vulcan_ingest_status` report cumulative `processed`/`total` across all ingestion runs, not just the current one.

**Architecture:** Persist a `:total-ingested` count on the `:ingestion/last-run-at` graph entity at run end. At run start, read it back to seed `_ingest_progress["processed"]`; set `_ingest_progress["total"]` from `git rev-list --count HEAD`. The existing `+= 1` loop then accumulates to a real-time cumulative total naturally.

**Tech Stack:** Python, minigraf (Datalog graph DB), pytest, asyncio

---

## File Map

- Modify: `mcp_server.py` — all logic changes
- Modify: `tests/test_mcp_server.py` — update broken tests, add new tests

---

### Task 1: Add `_total_ingested_query` and write failing tests

**Files:**
- Modify: `tests/test_mcp_server.py`
- Modify: `mcp_server.py`

- [ ] **Step 1: Write the failing tests** in `tests/test_mcp_server.py`, inside a new `class TestTotalIngestedQuery:` placed after the existing `class TestWatermarkQuery:` block:

```python
class TestTotalIngestedQuery:
    def test_returns_zero_when_absent(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        assert mcp_server._total_ingested_query(db) == 0

    def test_returns_stored_count(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [[462]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        assert mcp_server._total_ingested_query(db) == 462
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/aditya/workspaces/pycharm/temporal_reasoning
python -m pytest tests/test_mcp_server.py::TestTotalIngestedQuery -v
```

Expected: `AttributeError: module 'mcp_server' has no attribute '_total_ingested_query'`

- [ ] **Step 3: Add `_total_ingested_query` to `mcp_server.py`** — insert immediately after `_watermark_query` (after line 861):

```python
def _total_ingested_query(db: Any) -> int:
    """Return the cumulative number of commits ingested across all runs, or 0."""
    raw = db.execute("(query [:find ?n :where [:ingestion/last-run-at :total-ingested ?n]])")
    results = json.loads(raw).get("results", [])
    return int(results[0][0]) if results else 0
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
python -m pytest tests/test_mcp_server.py::TestTotalIngestedQuery -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add _total_ingested_query helper"
```

---

### Task 2: Extend `_last_run_write` to persist `total_ingested`

**Files:**
- Modify: `mcp_server.py:878-886`
- Modify: `tests/test_mcp_server.py` (update `test_last_run_write_transacts_correct_fields`)

- [ ] **Step 1: Update the existing test** — `test_last_run_write_transacts_correct_fields` currently calls `_last_run_write(db, "deadbeef", "2026-05-27T10:00:00Z")`. Update it to pass `total_ingested` and assert the new field:

```python
def test_last_run_write_transacts_correct_fields(self, mock_minigraf_db, tmp_path):
    mock_class, db_instance = mock_minigraf_db
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    db = mcp_server.get_db()
    db_instance.execute.reset_mock()

    mcp_server._last_run_write(db, "deadbeef", "2026-05-27T10:00:00Z", 1017)

    call_args = db_instance.execute.call_args[0][0]
    assert ":ingestion/last-run-at" in call_args
    assert ":last-run-at" in call_args
    assert "2026-05-27T10:00:00Z" in call_args
    assert ":last-commit" in call_args
    assert "deadbeef" in call_args
    assert ":type/ingestion" in call_args
    assert ":total-ingested" in call_args
    assert "1017" in call_args
    assert ":valid-from" not in call_args
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_mcp_server.py -k "test_last_run_write_transacts_correct_fields" -v
```

Expected: `AssertionError` on `:total-ingested` not in call_args (old signature still accepted)

- [ ] **Step 3: Update `_last_run_write` in `mcp_server.py`** — replace the function body (lines 878–886):

```python
def _last_run_write(db: Any, commit_hash: str, run_at: str, total_ingested: int) -> None:
    """Record the wall-clock time, final commit hash, and cumulative ingested count."""
    db.execute(
        f'(transact [[:ingestion/last-run-at :entity-type :type/ingestion] '
        f'[:ingestion/last-run-at :ident ":ingestion/last-run-at"] '
        f'[:ingestion/last-run-at :description "last ingestion run timestamp"] '
        f'[:ingestion/last-run-at :last-run-at "{run_at}"] '
        f'[:ingestion/last-run-at :last-commit "{commit_hash}"] '
        f'[:ingestion/last-run-at :total-ingested {total_ingested}]])'
    )
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
python -m pytest tests/test_mcp_server.py -k "test_last_run_write_transacts_correct_fields" -v
```

Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): persist :total-ingested in _last_run_write"
```

---

### Task 3: Seed `_ingest_progress` from prior count + repo total at run start

**Files:**
- Modify: `mcp_server.py:1903-1920` (start of `_run_ingestion`)
- Modify: `mcp_server.py:2054-2060` (end of `_run_ingestion`, call to `_last_run_write`)
- Modify: `tests/test_mcp_server.py` — update two tests that patch `_last_run_write` with wrong arity + add cumulative seeding test

- [ ] **Step 1: Update two tests that patch `_last_run_write`** — `test_run_ingestion_writes_last_run_on_completion` and `test_run_ingestion_writes_last_run_when_no_commits` both use `lambda db, h, t:`. Update both lambdas to accept the new `total_ingested` arg:

In `test_run_ingestion_writes_last_run_on_completion` (around line 1534):
```python
monkeypatch.setattr(
    mcp_server, "_last_run_write",
    lambda db, h, t, n: last_run_calls.append((h, t, n))
)
```
And update the assertion:
```python
assert len(last_run_calls) == 1
assert last_run_calls[0][0] == "abc123"
assert last_run_calls[0][1].endswith("Z")
assert last_run_calls[0][2] == 1  # 1 commit processed
```

In `test_run_ingestion_writes_last_run_when_no_commits` (around line 1554):
```python
monkeypatch.setattr(
    mcp_server, "_last_run_write",
    lambda db, h, t, n: last_run_calls.append((h, t, n))
)
```
And update the assertion:
```python
assert len(last_run_calls) == 1
assert last_run_calls[0][0] == "abc123"
assert last_run_calls[0][1].endswith("Z")
assert last_run_calls[0][2] == 0  # no commits processed this run, prior was 0
```

Also add a new test for cumulative seeding in `class TestRunIngestion:`:

```python
@pytest.mark.asyncio
async def test_processed_seeded_from_prior_ingested(self, mock_minigraf_db, git_repo, monkeypatch):
    """processed starts at prior_ingested and increments cumulatively."""
    mock_class, db_instance = mock_minigraf_db
    # _total_ingested_query returns 462 (prior runs), all other queries return []
    def execute_side_effect(query, *args, **kwargs):
        if ":total-ingested" in query:
            return json.dumps({"results": [[462]]})
        return json.dumps({"results": []})
    db_instance.execute.side_effect = execute_side_effect
    import mcp_server
    mcp_server.open_db(str(git_repo / "memory.graph"))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0,
        "current_commit": "", "error": None,
    }
    await mcp_server._run_ingestion(str(git_repo), "HEAD")
    # git_repo fixture has 2 commits; prior was 462 → final should be 464
    assert mcp_server._ingest_progress["processed"] == 464
    assert mcp_server._ingest_progress["total"] == 2  # git_repo has 2 commits
```

- [ ] **Step 2: Run to confirm failures**

```bash
python -m pytest tests/test_mcp_server.py -k "test_run_ingestion_writes_last_run or test_processed_seeded" -v
```

Expected: failures due to wrong arity and missing seeding logic

- [ ] **Step 3: Update `_run_ingestion` in `mcp_server.py`**

At the top of the function, after `watermark = _watermark_query(db)` and before `_db = None` (around lines 1909–1913), add the prior count + repo total reads:

```python
        watermark = _watermark_query(db)
        prior_ingested = _total_ingested_query(db)
        entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
        file_deps: Dict[str, set] = {}
        dep_valid_from: Dict[tuple, str] = {}
        _db = None  # release file lock while enumerating commits

        commits = _git_commits(repo_path, watermark, branch)
        repo_total_result = _subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        repo_total = int(repo_total_result.stdout.strip()) if repo_total_result.returncode == 0 else len(commits)
        _ingest_progress["total"] = repo_total
        _ingest_progress["status"] = "running"
        _ingest_progress["processed"] = prior_ingested
```

(Remove the old `_ingest_progress["total"] = len(commits)` and `_ingest_progress["status"] = "running"` lines that were there before.)

At the end of `_run_ingestion`, update the `_last_run_write` call (around line 2058) to pass the final count:

```python
            _ingest_tags(db, repo_path, now)
            _last_run_write(db, last_hash, now, _ingest_progress["processed"])
            db.checkpoint()
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
python -m pytest tests/test_mcp_server.py -k "test_run_ingestion_writes_last_run or test_processed_seeded or test_ingestion_processes_all_commits" -v
```

Expected: all pass. (`test_ingestion_processes_all_commits` checks `processed == 2`; with `prior_ingested=0` from the mock returning `[]`, this still holds.)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): seed processed/total from cumulative count and repo size at run start"
```

---

### Task 4: Expose `total_ingested` from `handle_vulcan_ingest_status`

**Files:**
- Modify: `mcp_server.py:2089-2110`
- Modify: `tests/test_mcp_server.py` — update `test_returns_last_run_at_from_graph`, add new test

- [ ] **Step 1: Update `test_returns_last_run_at_from_graph`** — it currently uses a fixed `return_value` for all `execute` calls. After this task's change, `handle_vulcan_ingest_status` makes a second DB call for `total-ingested`, which would get the same two-element list and fail `int()` conversion. Switch it to a `side_effect`:

```python
def test_returns_last_run_at_from_graph(self, mock_minigraf_db, tmp_path):
    mock_class, db_instance = mock_minigraf_db
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0,
        "current_commit": "", "error": None,
    }
    def execute_side_effect(query, *args, **kwargs):
        if ":last-run-at" in query and ":last-commit" in query:
            return json.dumps({"results": [["2026-05-27T10:00:00Z", "deadbeef"]]})
        return json.dumps({"results": []})
    db_instance.execute.side_effect = execute_side_effect
    result = mcp_server.handle_vulcan_ingest_status()
    assert result["last_run_at"] == "2026-05-27T10:00:00Z"
    assert result["last_commit"] == "deadbeef"
```

- [ ] **Step 2: Add tests for the new `total_ingested` field** in `class TestVulcanIngestStatus:`:

```python
def test_returns_total_ingested_from_graph(self, mock_minigraf_db, tmp_path):
    mock_class, db_instance = mock_minigraf_db
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0,
        "current_commit": "", "error": None,
    }
    def execute_side_effect(query, *args, **kwargs):
        if ":last-run-at" in query and ":last-commit" in query:
            return json.dumps({"results": [["2026-05-27T10:00:00Z", "deadbeef"]]})
        if ":total-ingested" in query:
            return json.dumps({"results": [[1017]]})
        return json.dumps({"results": []})
    db_instance.execute.side_effect = execute_side_effect
    result = mcp_server.handle_vulcan_ingest_status()
    assert result["total_ingested"] == 1017

def test_total_ingested_absent_returns_none(self, mock_minigraf_db, tmp_path):
    mock_class, db_instance = mock_minigraf_db
    db_instance.execute.return_value = json.dumps({"results": []})
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0,
        "current_commit": "", "error": None,
    }
    result = mcp_server.handle_vulcan_ingest_status()
    assert result["total_ingested"] is None
```

- [ ] **Step 3: Run to confirm failures**

```bash
python -m pytest tests/test_mcp_server.py -k "test_returns_total_ingested or test_total_ingested_absent or test_returns_last_run_at_from_graph" -v
```

Expected: `KeyError: 'total_ingested'`

- [ ] **Step 4: Update `handle_vulcan_ingest_status`** — extend the non-running branch to also query `total-ingested` via `_total_ingested_query`:

```python
def handle_vulcan_ingest_status() -> Dict[str, Any]:
    """Return current ingestion progress, augmented with graph-backed last-run info."""
    result: Dict[str, Any] = {"ok": True, **_ingest_progress}
    if _ingest_progress["status"] != "running":
        try:
            db = get_db()
            raw = db.execute(
                "(query [:find ?t ?h :any-valid-time "
                ":where [:ingestion/last-run-at :last-run-at ?t] "
                "[:ingestion/last-run-at :last-commit ?h]])"
            )
            rows = json.loads(raw).get("results", [])
            if rows:
                result["last_run_at"] = rows[0][0]
                result["last_commit"] = rows[0][1]
            else:
                result["last_run_at"] = None
                result["last_commit"] = None
            n = _total_ingested_query(db)
            result["total_ingested"] = n if n > 0 else None
        except Exception:
            result["last_run_at"] = None
            result["last_commit"] = None
            result["total_ingested"] = None
    return result
```

- [ ] **Step 5: Run all status tests**

```bash
python -m pytest tests/test_mcp_server.py::TestVulcanIngestStatus -v
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): expose total_ingested in vulcan_ingest_status"
```

---

### Task 5: Full test suite green

- [ ] **Step 1: Run full test suite**

```bash
cd /home/aditya/workspaces/pycharm/temporal_reasoning
python -m pytest tests/ -v
```

Expected: all tests pass, no regressions

- [ ] **Step 2: If any failures, fix them** — the most likely regressions are other tests that patch `_last_run_write` with a 3-arg lambda. Search for any remaining occurrences:

```bash
grep -n "_last_run_write" tests/test_mcp_server.py
```

Update any remaining `lambda db, h, t:` patches to `lambda db, h, t, n:`.

- [ ] **Step 3: Final commit if fixes were needed**

```bash
git add tests/test_mcp_server.py
git commit -m "fix(tests): update remaining _last_run_write patches to 4-arg signature"
```
