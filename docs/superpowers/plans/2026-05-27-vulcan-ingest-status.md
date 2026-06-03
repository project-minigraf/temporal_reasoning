# Fix `vulcan_ingest_status` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vulcan_ingest_status` report the wall-clock time and final commit hash of the last completed ingestion run, whether it ran in-process or via the hook subprocess.

**Architecture:** Add a `_last_run_write` helper that persists a named `:ingestion/last-run-at` entity to the graph after each successful run. Update `handle_vulcan_ingest_status` to query this entity when not running, augmenting the existing in-memory fields with `last_run_at` and `last_commit`. Update `_run_ingestion` to call `_last_run_write` on completion.

**Tech Stack:** Python, minigraf (MiniGrafDb), pytest, unittest.mock

---

### Task 1: Update `handle_vulcan_ingest_status` to return graph-backed fields

**Files:**
- Modify: `tests/test_mcp_server.py` (class `TestVulcanIngestStatus`, lines 1222–1246)
- Modify: `mcp_server.py` (function `handle_vulcan_ingest_status`, lines 1465–1467)

- [ ] **Step 1: Write failing tests**

In `tests/test_mcp_server.py`, replace the `TestVulcanIngestStatus` class with:

```python
class TestVulcanIngestStatus:
    def test_returns_idle_before_ingestion(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = mcp_server.handle_vulcan_ingest_status()
        assert result["ok"] is True
        assert result["status"] == "idle"
        assert result["processed"] == 0
        assert result["last_run_at"] is None
        assert result["last_commit"] is None

    def test_returns_last_run_at_from_graph(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        db_instance.execute.return_value = json.dumps({
            "results": [["2026-05-27T10:00:00Z", "deadbeef"]]
        })
        result = mcp_server.handle_vulcan_ingest_status()
        assert result["last_run_at"] == "2026-05-27T10:00:00Z"
        assert result["last_commit"] == "deadbeef"

    def test_running_status_skips_graph_query(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "running", "processed": 3, "total": 10,
            "current_commit": "abc123", "error": None,
        }
        db_instance.execute.reset_mock()
        result = mcp_server.handle_vulcan_ingest_status()
        assert result["status"] == "running"
        assert result["processed"] == 3
        assert result["total"] == 10
        assert result["current_commit"] == "abc123"
        # Must not query the graph while running
        db_instance.execute.assert_not_called()
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /home/aditya/workspaces/pycharm/temporal_reasoning
pytest tests/test_mcp_server.py::TestVulcanIngestStatus -v
```

Expected: all three tests FAIL — `last_run_at`/`last_commit` keys missing, and `assert_not_called` may pass or fail depending on current impl.

- [ ] **Step 3: Update `handle_vulcan_ingest_status` in `mcp_server.py`**

Replace lines 1465–1467:

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
        except Exception:
            result["last_run_at"] = None
            result["last_commit"] = None
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestVulcanIngestStatus -v
```

Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest-status): query graph for last_run_at and last_commit when idle"
```

---

### Task 2: Add `_last_run_write` helper

**Files:**
- Modify: `tests/test_mcp_server.py` (add to class `TestIngestionWrites`)
- Modify: `mcp_server.py` (add after `_watermark_update`, around line 632)

- [ ] **Step 1: Write failing test**

Add to `TestIngestionWrites` in `tests/test_mcp_server.py`:

```python
def test_last_run_write_transacts_correct_fields(self, mock_minigraf_db, tmp_path):
    mock_class, db_instance = mock_minigraf_db
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    db = mcp_server.get_db()
    db_instance.execute.reset_mock()

    mcp_server._last_run_write(db, "deadbeef", "2026-05-27T10:00:00Z")

    call_args = db_instance.execute.call_args[0][0]
    assert ":ingestion/last-run-at" in call_args
    assert ":last-run-at" in call_args
    assert "2026-05-27T10:00:00Z" in call_args
    assert ":last-commit" in call_args
    assert "deadbeef" in call_args
    assert ":type/ingestion" in call_args
    assert ":valid-from" not in call_args
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_mcp_server.py::TestIngestionWrites::test_last_run_write_transacts_correct_fields -v
```

Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_last_run_write'`

- [ ] **Step 3: Add `_last_run_write` to `mcp_server.py`**

Add immediately after `_watermark_update` (after line 631):

```python
def _last_run_write(db: Any, commit_hash: str, run_at: str) -> None:
    """Record the wall-clock time and final commit hash of the last ingestion run."""
    db.execute(
        f'(transact [[:ingestion/last-run-at :entity-type :type/ingestion] '
        f'[:ingestion/last-run-at :ident ":ingestion/last-run-at"] '
        f'[:ingestion/last-run-at :description "last ingestion run timestamp"] '
        f'[:ingestion/last-run-at :last-run-at "{run_at}"] '
        f'[:ingestion/last-run-at :last-commit "{commit_hash}"]])'
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_mcp_server.py::TestIngestionWrites::test_last_run_write_transacts_correct_fields -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest-status): add _last_run_write helper"
```

---

### Task 3: Call `_last_run_write` from `_run_ingestion`

**Files:**
- Modify: `tests/test_mcp_server.py` (add to class `TestIngestionWrites`)
- Modify: `mcp_server.py` (`_run_ingestion`, around line 1440)

- [ ] **Step 1: Write failing test**

Add to `TestIngestionWrites` in `tests/test_mcp_server.py`:

```python
def test_run_ingestion_writes_last_run_on_completion(self, mock_minigraf_db, tmp_path, monkeypatch):
    mock_class, db_instance = mock_minigraf_db
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))

    # Stub out git helpers so _run_ingestion completes with one fake commit
    monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: None)
    monkeypatch.setattr(
        mcp_server, "_git_commits",
        lambda repo, watermark, branch: [("abc123", "2025-01-01T00:00:00Z", "author", "msg")]
    )
    monkeypatch.setattr(mcp_server, "_git_changed_files", lambda repo, commit: [])
    monkeypatch.setattr(mcp_server, "_watermark_update", lambda db, h, ts, r: None)

    last_run_calls = []
    monkeypatch.setattr(
        mcp_server, "_last_run_write",
        lambda db, h, t: last_run_calls.append((h, t))
    )

    asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))

    assert len(last_run_calls) == 1
    assert last_run_calls[0][0] == "abc123"
    # run_at should be an ISO 8601 UTC string
    assert last_run_calls[0][1].endswith("Z")

def test_run_ingestion_writes_last_run_when_no_commits(self, mock_minigraf_db, tmp_path, monkeypatch):
    mock_class, db_instance = mock_minigraf_db
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))

    monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: "abc123")
    monkeypatch.setattr(mcp_server, "_git_commits", lambda repo, watermark, branch: [])

    last_run_calls = []
    monkeypatch.setattr(
        mcp_server, "_last_run_write",
        lambda db, h, t: last_run_calls.append((h, t))
    )

    asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))

    assert len(last_run_calls) == 1
    # No commits processed — hash comes from watermark
    assert last_run_calls[0][0] == "abc123"
    assert last_run_calls[0][1].endswith("Z")
```

- [ ] **Step 2: Run to verify tests fail**

```bash
pytest tests/test_mcp_server.py::TestIngestionWrites::test_run_ingestion_writes_last_run_on_completion tests/test_mcp_server.py::TestIngestionWrites::test_run_ingestion_writes_last_run_when_no_commits -v
```

Expected: both FAIL — `_last_run_write` never called.

- [ ] **Step 3: Update `_run_ingestion` in `mcp_server.py`**

The current loop ends at line 1440: `_ingest_progress["status"] = "complete"`. Replace that block with:

```python
        # Track the last commit hash for _last_run_write
        last_hash = watermark or ""
        for commit_hash, commit_ts_iso, author, subject in commits:
            _ingest_progress["current_commit"] = commit_hash
            last_hash = commit_hash
            reason = f"git:{commit_hash} {author}: {subject}"

            db = get_db()
            try:
                changed = _git_changed_files(repo_path, commit_hash)
                add_triples: List[str] = []
                close_items: List[tuple] = []

                for status, file_path in changed:
                    parser = _get_parser(file_path)
                    if parser is None:
                        continue

                    module_ident = _code_ident("module", file_path)

                    if status == "D":
                        idents = file_entities.get(file_path, [_code_ident("module", file_path)])
                        for ident in idents:
                            orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                            close_items.append(
                                ([f'[{ident} :description ""]'], orig_ts)
                            )
                    else:
                        try:
                            content = _git_file_content(repo_path, commit_hash, file_path)
                        except Exception:
                            continue
                        extracted = _extract_from_source(content, parser, file_path)
                        triples = _build_code_triples(
                            file_path, extracted, commit_ts_iso, entity_valid_from, file_entities
                        )
                        add_triples.extend(triples)

                _ingest_transact(db, add_triples, commit_ts_iso, reason)
                for close_triples, orig_ts in close_items:
                    _ingest_close(db, close_triples, orig_ts, commit_ts_iso, reason)
                _watermark_update(db, commit_hash, commit_ts_iso, reason)
                db.checkpoint()

            finally:
                _db = None

            _ingest_progress["processed"] += 1
            await asyncio.sleep(0)

        now = datetime.utcnow().isoformat() + "Z"
        db = get_db()
        try:
            _last_run_write(db, last_hash, now)
            db.checkpoint()
        finally:
            _db = None

        _ingest_progress["status"] = "complete"
```

> Note: also add `from datetime import datetime` near the top of `mcp_server.py` if not already imported. Check with `grep "from datetime" mcp_server.py`.

- [ ] **Step 4: Verify `datetime` is imported**

```bash
grep "from datetime\|import datetime" /home/aditya/workspaces/pycharm/temporal_reasoning/mcp_server.py
```

If not present, add at the top of `mcp_server.py` with the other imports:

```python
from datetime import datetime
```

- [ ] **Step 5: Run new tests**

```bash
pytest tests/test_mcp_server.py::TestIngestionWrites::test_run_ingestion_writes_last_run_on_completion tests/test_mcp_server.py::TestIngestionWrites::test_run_ingestion_writes_last_run_when_no_commits -v
```

Expected: both PASS.

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/test_mcp_server.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest-status): write last-run-at entity on ingestion completion"
```
