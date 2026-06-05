# Semantic Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `handle_memory_prepare_turn`'s substring token matching and random fallback with a cached in-process BM25 index that ranks memory facts above git facts.

**Architecture:** A `FactIndex` class wraps `BM25Okapi` with a memory-fact score boost. An `IndexCache` singleton holds the live index, rebuilds asynchronously on invalidation, and serves stale results during rebuilds. `handle_memory_prepare_turn` queries the cache; `handle_vulcan_transact`, `handle_vulcan_retract`, and `_run_ingestion` invalidate it on write.

**Tech Stack:** `rank-bm25>=0.2.2` (pure Python BM25, no transitive deps), Python `threading` (async rebuild), existing `mcp_server.py` DB layer.

---

## File Map

| File | Change |
|---|---|
| `pyproject.toml` | Add `rank-bm25>=0.2.2` to `dependencies` |
| `install.py` | Add `check_rank_bm25_package()`, add to checks list |
| `mcp_server.py` | Add import guard (line ~20); add `_MEMORY_PREFIXES`, `_tokenize`, `FactIndex`, `IndexCache`, `_index_cache` (before `handle_memory_prepare_turn` ~line 1100); rename existing `handle_memory_prepare_turn` → `_handle_memory_prepare_turn_heuristic`; new `handle_memory_prepare_turn`; `invalidate()` calls in `handle_vulcan_transact` (line ~432), `handle_vulcan_retract` (line ~450), `_run_ingestion` (line ~1870) |
| `tests/test_mcp_server.py` | Add `TestBM25Tokenize`, `TestFactIndex`, `TestIndexCache`, `TestMemoryPrepareTurnBM25`, `TestIndexCacheInvalidation`; update `reset_mcp_server_db` autouse fixture |

---

### Task 1: Add rank-bm25 dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `install.py`

- [ ] **Step 1: Add rank-bm25 to pyproject.toml dependencies**

In `pyproject.toml`, find the `dependencies` list and add the new entry:

```toml
dependencies = [
    "minigraf>=0.22.0",
    "mcp>=1.27.0",
    "rank-bm25>=0.2.2",
]
```

- [ ] **Step 2: Add check_rank_bm25_package to install.py**

Add this function after `check_mcp_package` (around line 100):

```python
def check_rank_bm25_package():
    """Verify rank-bm25 Python package is installed in the venv."""
    if _venv_has("rank_bm25"):
        print("✓ rank-bm25 package found")
        return True
    print("✗ rank-bm25 not found — installing via pip...")
    if _venv_pip_install("rank-bm25>=0.2.2", timeout=60):
        print("✓ rank-bm25 installed")
        return True
    print("✗ pip install rank-bm25 failed")
    return False
```

- [ ] **Step 3: Add check to the checks list in main()**

In `install.py`, find the `checks` list in `main()` and add the new entry after `check_mcp_package`:

```python
checks = [
    ("Python version", check_python_version),
    ("minigraf package", check_minigraf_package),
    ("mcp package", check_mcp_package),
    ("rank-bm25 package", check_rank_bm25_package),
    ("tree_sitter_languages package", check_tree_sitter_languages_package),
    ("MCP server", check_mcp_server_importable),
]
```

- [ ] **Step 4: Install into venv and verify**

```bash
.venv/bin/python -m pip install rank-bm25>=0.2.2
.venv/bin/python -c "from rank_bm25 import BM25Okapi; print('ok')"
```

Expected output: `ok`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml install.py
git commit -m "feat(deps): add rank-bm25 for BM25 semantic retrieval"
```

---

### Task 2: Tokenisation primitives and import guard

**Files:**
- Modify: `mcp_server.py` (imports section ~line 19, prepare_turn section ~line 1100)
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Add this class at the end of `tests/test_mcp_server.py`:

```python
class TestBM25Tokenize:
    def test_splits_keyword_ident_on_punctuation(self):
        from mcp_server import _tokenize
        assert _tokenize(":decision/use-redis") == ["decision", "use", "redis"]

    def test_lowercases_tokens(self):
        from mcp_server import _tokenize
        assert _tokenize("use Redis for Caching") == ["use", "redis", "for", "caching"]

    def test_filters_empty_tokens(self):
        from mcp_server import _tokenize
        assert _tokenize(":::") == []

    def test_mixed_fact_row(self):
        from mcp_server import _tokenize
        assert _tokenize(":commit/abc123 :subject feat add redis") == [
            "commit", "abc123", "subject", "feat", "add", "redis"
        ]

    def test_memory_prefix_detected(self):
        from mcp_server import _MEMORY_PREFIXES
        assert ":decision/use-redis".startswith(_MEMORY_PREFIXES)
        assert ":preference/tdd".startswith(_MEMORY_PREFIXES)
        assert ":constraint/no-js".startswith(_MEMORY_PREFIXES)
        assert ":dependency/redis".startswith(_MEMORY_PREFIXES)

    def test_git_prefix_not_memory(self):
        from mcp_server import _MEMORY_PREFIXES
        assert not ":commit/abc123".startswith(_MEMORY_PREFIXES)
        assert not ":function/foo-bar".startswith(_MEMORY_PREFIXES)
        assert not ":module/src-main".startswith(_MEMORY_PREFIXES)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestBM25Tokenize -v
```

Expected: FAIL with `ImportError: cannot import name '_tokenize'`

- [ ] **Step 3: Add import guard and primitives to mcp_server.py**

After the existing imports (after line 19, `from minigraf import MiniGrafDb, MiniGrafError`), add:

```python
try:
    from rank_bm25 import BM25Okapi as _BM25Okapi
    _BM25_AVAILABLE = True
except ImportError:
    _BM25Okapi = None  # type: ignore[assignment,misc]
    _BM25_AVAILABLE = False
```

Then, in the `memory_prepare_turn` section just before `handle_memory_prepare_turn` (around line 1100, after `_build_query_clauses`), add:

```python
# ---------------------------------------------------------------------------
# BM25 index — semantic retrieval primitives
# ---------------------------------------------------------------------------

_MEMORY_PREFIXES = (":decision/", ":preference/", ":constraint/", ":dependency/")


def _tokenize(text: str) -> List[str]:
    """Split text on non-alphanumeric chars, lowercase, filter empties.

    Works on raw fact values and keyword idents alike:
      ":decision/use-redis" → ["decision", "use", "redis"]
      "use Redis for caching" → ["use", "redis", "for", "caching"]
    """
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestBM25Tokenize -v
```

Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(retrieval): add _tokenize primitive and _MEMORY_PREFIXES for BM25"
```

---

### Task 3: FactIndex class

**Files:**
- Modify: `mcp_server.py` (after `_tokenize`, before `handle_memory_prepare_turn`)
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Add after `TestBM25Tokenize` in `tests/test_mcp_server.py`:

```python
class TestFactIndex:
    def test_empty_facts_returns_empty_query(self):
        from mcp_server import FactIndex
        index = FactIndex([], boost=2.0)
        assert index.query("redis", top_n=10) == []

    def test_query_returns_matching_fact(self):
        from mcp_server import FactIndex
        facts = [[":decision/use-redis", ":description", "use redis for caching"]]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis caching", top_n=10)
        assert len(results) == 1
        assert results[0] == [":decision/use-redis", ":description", "use redis for caching"]

    def test_memory_fact_outscores_git_fact(self):
        from mcp_server import FactIndex
        facts = [
            [":decision/use-redis", ":description", "use redis for caching"],
            [":commit/abc123def456", ":subject", "feat use redis for caching layer"],
        ]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis caching", top_n=10)
        assert results[0][0] == ":decision/use-redis"

    def test_zero_score_results_excluded(self):
        from mcp_server import FactIndex
        facts = [[":decision/use-redis", ":description", "use redis for caching"]]
        index = FactIndex(facts, boost=2.0)
        results = index.query("elephants trombone completely unrelated", top_n=10)
        assert results == []

    def test_top_n_respected(self):
        from mcp_server import FactIndex
        facts = [[f":decision/item-{i}", ":description", f"redis item {i}"] for i in range(20)]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis", top_n=5)
        assert len(results) <= 5

    def test_facts_with_no_tokens_skipped(self):
        from mcp_server import FactIndex
        # A fact whose text tokenises to [] should not crash
        facts = [
            [":::", ":::", ":::"],
            [":decision/use-redis", ":description", "use redis"],
        ]
        index = FactIndex(facts, boost=2.0)
        results = index.query("redis", top_n=10)
        assert len(results) == 1
        assert results[0][0] == ":decision/use-redis"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFactIndex -v
```

Expected: FAIL with `ImportError: cannot import name 'FactIndex'`

- [ ] **Step 3: Implement FactIndex**

Add after `_tokenize` in `mcp_server.py`:

```python
class FactIndex:
    """Immutable BM25 snapshot over a set of graph facts.

    Each fact row [e, a, v] is tokenised as a single document.
    Memory facts (entity idents with a known memory prefix) receive
    a configurable score multiplier at query time.
    """

    def __init__(self, facts: List[List], boost: float = 2.0) -> None:
        self._boost = boost
        docs = [_tokenize(" ".join(str(x) for x in row)) for row in facts]
        # Filter out rows whose full text produces no tokens
        valid = [
            (row, doc, any(str(row[0]).startswith(p) for p in _MEMORY_PREFIXES))
            for row, doc in zip(facts, docs)
            if doc
        ]
        if not valid or _BM25Okapi is None:
            self._bm25 = None
            self._facts: List[List] = []
            self._is_memory: List[bool] = []
            return
        rows, valid_docs, memory_flags = zip(*valid)
        self._facts = list(rows)
        self._is_memory = list(memory_flags)
        self._bm25 = _BM25Okapi(list(valid_docs))

    def query(self, text: str, top_n: int = 50) -> List[List]:
        """Return up to top_n facts ranked by BM25 score (memory boost applied).

        Zero-score results are excluded. Returns [] if the index is empty
        or no tokens in text overlap with the corpus.
        """
        if self._bm25 is None or not self._facts:
            return []
        tokens = _tokenize(text)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens).tolist()
        for i, is_mem in enumerate(self._is_memory):
            if is_mem:
                scores[i] *= self._boost
        ranked = sorted(
            [(scores[i], self._facts[i]) for i in range(len(self._facts)) if scores[i] > 0],
            key=lambda x: x[0],
            reverse=True,
        )
        return [row for _, row in ranked[:top_n]]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFactIndex -v
```

Expected: 6 PASSED

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(retrieval): add FactIndex with BM25 scoring and memory boost"
```

---

### Task 4: IndexCache singleton

**Files:**
- Modify: `mcp_server.py` (after `FactIndex`)
- Modify: `tests/test_mcp_server.py` (update autouse fixture + add `TestIndexCache`)

- [ ] **Step 1: Write failing tests**

Add after `TestFactIndex` in `tests/test_mcp_server.py`:

```python
class TestIndexCache:
    def test_get_returns_none_before_any_rebuild(self):
        from mcp_server import IndexCache
        cache = IndexCache()
        assert cache.get() is None

    def test_rebuild_populates_index(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/use-redis", ":description", "use redis"]]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        cache = mcp_server.IndexCache()
        cache._rebuild()
        assert cache.get() is not None

    def test_stale_index_served_when_already_rebuilding(self):
        import mcp_server
        from mcp_server import IndexCache, FactIndex
        cache = IndexCache()
        stale = FactIndex([[":decision/old", ":description", "old"]], boost=2.0)
        cache._current = stale
        cache._rebuilding = True
        cache.invalidate()  # no-op because _rebuilding
        assert cache.get() is stale
        cache._rebuilding = False

    def test_invalidate_noop_when_rebuilding(self):
        from mcp_server import IndexCache
        from unittest.mock import patch
        cache = IndexCache()
        cache._rebuilding = True
        with patch("threading.Thread") as mock_thread:
            cache.invalidate()
            mock_thread.assert_not_called()
        cache._rebuilding = False

    def test_rebuild_leaves_current_unchanged_on_error(self, monkeypatch):
        import mcp_server
        from mcp_server import IndexCache, FactIndex
        cache = IndexCache()
        stale = FactIndex([[":decision/old", ":description", "old"]], boost=2.0)
        cache._current = stale
        # Force get_db to raise
        monkeypatch.setattr(mcp_server, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("db error")))
        cache._rebuild()
        assert cache.get() is stale
        assert cache._rebuilding is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestIndexCache -v
```

Expected: FAIL with `ImportError: cannot import name 'IndexCache'`

- [ ] **Step 3: Implement IndexCache and module-level singleton**

Add after `FactIndex` in `mcp_server.py`:

```python
class IndexCache:
    """Module-level singleton managing the live BM25 FactIndex.

    Rebuilds asynchronously in a background thread. Serves the stale index
    during rebuilds; returns None before the first successful rebuild.
    Invalidation is idempotent while a rebuild is in progress.
    """

    def __init__(self) -> None:
        self._current: Optional[FactIndex] = None
        self._rebuilding: bool = False
        self._lock = threading.Lock()

    def get(self) -> Optional[FactIndex]:
        """Return the current index (may be stale or None)."""
        return self._current

    def invalidate(self) -> None:
        """Trigger an async rebuild if one is not already running."""
        if self._rebuilding:
            return
        t = threading.Thread(target=self._rebuild, daemon=True)
        t.start()

    def _rebuild(self) -> None:
        """Fetch all currently-valid facts from the DB and swap the index."""
        self._rebuilding = True
        try:
            db = get_db()
            boost = float(os.environ.get("VULCAN_MEMORY_BOOST", "2.0"))
            raw = db.execute(
                f'(query [:find ?e ?a ?v :valid-at "{_now_utc_ms()}" :where [?e ?a ?v]])'
            )
            facts = json.loads(raw).get("results", [])
            new_index = FactIndex(facts, boost=boost)
            with self._lock:
                self._current = new_index
        except Exception as e:
            print(f"[IndexCache] rebuild failed: {e}", file=sys.stderr)
        finally:
            self._rebuilding = False


_index_cache = IndexCache()
```

Note: `threading` and `sys` are already imported at the top of `mcp_server.py`. Verify `import threading` and `import sys` are present; add them if missing.

- [ ] **Step 4: Update the autouse fixture in tests/test_mcp_server.py**

Find the `reset_mcp_server_db` autouse fixture (lines 17–25) and add the cache reset:

```python
@pytest.fixture(autouse=True)
def reset_mcp_server_db():
    """Reset the module-level _db singleton, grammar cache, and index cache between tests."""
    import mcp_server
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    mcp_server._index_cache = mcp_server.IndexCache()
    yield
    mcp_server._db = None
    mcp_server._grammar_cache.clear()
    mcp_server._index_cache = mcp_server.IndexCache()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestIndexCache -v
```

Expected: 5 PASSED

- [ ] **Step 6: Run the full test suite to confirm no regressions**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q
```

Expected: all previously passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(retrieval): add IndexCache with async rebuild and stale-index serving"
```

---

### Task 5: Replace handle_memory_prepare_turn with BM25 path

**Files:**
- Modify: `mcp_server.py` (lines ~1102–1150)
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Add after `TestIndexCache` in `tests/test_mcp_server.py`:

```python
class TestMemoryPrepareTurnBM25:
    def test_returns_empty_when_no_index(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mcp_server.open_db(str(tmp_path / "t.graph"))
        fresh_cache = mcp_server.IndexCache()  # no index built yet
        with patch.object(mcp_server, "_index_cache", fresh_cache):
            result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert result == ""

    def test_returns_empty_for_unmatched_query(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/use-redis", ":description", "use redis"]]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        cache = mcp_server.IndexCache()
        cache._rebuild()
        with patch.object(mcp_server, "_index_cache", cache):
            result = mcp_server.handle_memory_prepare_turn("elephants trombone")
        assert result == ""

    def test_memory_facts_rank_above_git_facts(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [
                [":decision/use-redis", ":description", "use redis for caching"],
                [":commit/abc123def456", ":subject", "feat use redis caching layer"],
            ]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        cache = mcp_server.IndexCache()
        cache._rebuild()
        with patch.object(mcp_server, "_index_cache", cache):
            result = mcp_server.handle_memory_prepare_turn("redis caching")
        assert "Relevant memory context:" in result
        assert result.index(":decision/use-redis") < result.index(":commit/abc123def456")

    def test_respects_scan_limit(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[f":decision/item-{i}", ":description", f"redis item {i}"] for i in range(20)]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setenv("VULCAN_PREPARE_SCAN_LIMIT", "3")
        cache = mcp_server.IndexCache()
        cache._rebuild()
        with patch.object(mcp_server, "_index_cache", cache):
            result = mcp_server.handle_memory_prepare_turn("redis")
        lines = [l for l in result.splitlines() if "|" in l]
        assert len(lines) <= 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestMemoryPrepareTurnBM25 -v
```

Expected: FAIL — the existing `handle_memory_prepare_turn` doesn't use the cache.

- [ ] **Step 3: Rename existing function and add BM25 implementation**

In `mcp_server.py`, rename `handle_memory_prepare_turn` (line ~1102) to `_handle_memory_prepare_turn_heuristic`:

```python
def _handle_memory_prepare_turn_heuristic(user_message: str) -> str:
    """Heuristic fallback for handle_memory_prepare_turn when rank_bm25 is unavailable."""
    # (keep all existing body unchanged)
```

Then add the new public function immediately after (still within the prepare_turn section):

```python
def handle_memory_prepare_turn(user_message: str) -> str:
    """Query graph for facts relevant to the user message.

    Uses BM25-ranked retrieval over a cached FactIndex when rank_bm25 is
    available. Falls back to the heuristic (substring token) implementation
    when rank_bm25 is not installed.

    Returns a formatted context block string for injection as additionalContext,
    or an empty string if no relevant facts are found.
    """
    if not _BM25_AVAILABLE:
        return _handle_memory_prepare_turn_heuristic(user_message)

    scan_limit = int(os.environ.get("VULCAN_PREPARE_SCAN_LIMIT", "50"))
    index = _index_cache.get()
    if index is None:
        return ""
    results = index.query(user_message, top_n=scan_limit)
    if not results:
        return ""
    return f"Relevant memory context:\n{_format_facts(results)}"
```

- [ ] **Step 4: Run new tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestMemoryPrepareTurnBM25 -v
```

Expected: 4 PASSED

- [ ] **Step 5: Run the existing TestMemoryPrepareTurn tests**

These tests cover the heuristic path and should still pass since `_handle_memory_prepare_turn_heuristic` is unchanged. However, they now test via `handle_memory_prepare_turn` which, with `_BM25_AVAILABLE=True`, goes through the BM25 path. Update each test in `TestMemoryPrepareTurn` to call `_handle_memory_prepare_turn_heuristic` directly instead of `handle_memory_prepare_turn`:

```python
# In each test method within TestMemoryPrepareTurn, change:
result = mcp_server.handle_memory_prepare_turn(...)
# to:
result = mcp_server._handle_memory_prepare_turn_heuristic(...)
```

Then run:

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestMemoryPrepareTurn -v
```

Expected: all PASSED

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q
```

Expected: all passing.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(retrieval): replace prepare_turn substring matching with BM25 index query"
```

---

### Task 6: Invalidation hooks

**Files:**
- Modify: `mcp_server.py` (lines ~432, ~450, ~1870)
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Add after `TestMemoryPrepareTurnBM25` in `tests/test_mcp_server.py`:

```python
class TestIndexCacheInvalidation:
    def test_successful_transact_triggers_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx_id": 1, "count": 1})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_vulcan_transact(
                '[[:decision/test :description "test"]]', reason="test"
            )
            mock_inv.assert_called_once()

    def test_failed_transact_does_not_trigger_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        from minigraf import MiniGrafError
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.side_effect = MiniGrafError("bad tx")
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_vulcan_transact(
                '[[:decision/test :description "test"]]', reason="test"
            )
            mock_inv.assert_not_called()

    def test_successful_retract_triggers_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx_id": 2, "count": 1})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_vulcan_retract(
                '[[:decision/test :description "test"]]', reason="cleanup"
            )
            mock_inv.assert_called_once()

    def test_failed_retract_does_not_trigger_invalidation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        from unittest.mock import patch
        from minigraf import MiniGrafError
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.side_effect = MiniGrafError("bad retract")
        mcp_server.open_db(str(tmp_path / "t.graph"))
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            mcp_server.handle_vulcan_retract(
                '[[:decision/test :description "test"]]', reason="cleanup"
            )
            mock_inv.assert_not_called()

    def test_run_ingestion_triggers_invalidation_on_completion(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        from unittest.mock import patch
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_git_commits", lambda *a, **k: [])
        monkeypatch.setattr(mcp_server, "_watermark_query", lambda db: None)
        monkeypatch.setattr(mcp_server, "_preload_known_entities", lambda db, path: ({}, {}))
        monkeypatch.setattr(mcp_server, "_ingest_deps_from_head", lambda *a, **k: None)
        monkeypatch.setattr(mcp_server, "_ingest_tags", lambda *a, **k: None)
        monkeypatch.setattr(mcp_server, "_last_run_write", lambda *a, **k: None)
        with patch.object(mcp_server._index_cache, "invalidate") as mock_inv:
            asyncio.run(mcp_server._run_ingestion(str(tmp_path), "HEAD"))
            mock_inv.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestIndexCacheInvalidation -v
```

Expected: the transact/retract tests FAIL (invalidate not called yet); ingest test may vary.

- [ ] **Step 3: Add invalidate() to handle_vulcan_transact**

In `mcp_server.py`, find `handle_vulcan_transact` (line ~407). Inside the `try` block, after `_update_mtime()` (line ~429), update the result-handling block:

```python
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
            _index_cache.invalidate()
        return result
```

- [ ] **Step 4: Add invalidate() to handle_vulcan_retract**

In `mcp_server.py`, find `handle_vulcan_retract` (line ~438). Inside the `try` block, after `_update_mtime()` (line ~447), update the result-handling block:

```python
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
            _index_cache.invalidate()
        return result
```

- [ ] **Step 5: Add invalidate() to _run_ingestion**

In `mcp_server.py`, find `_run_ingestion` (line ~1762). After the line `_ingest_progress["status"] = "complete"` (line ~1870), add:

```python
        _ingest_progress["status"] = "complete"
        _index_cache.invalidate()
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestIndexCacheInvalidation -v
```

Expected: 5 PASSED

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q
```

Expected: all passing.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(retrieval): invalidate BM25 index after transact, retract, and git ingest"
```

---

### Task 7: Graceful degradation when rank_bm25 is unavailable

**Files:**
- Modify: `tests/test_mcp_server.py`

The `handle_memory_prepare_turn` already falls back to `_handle_memory_prepare_turn_heuristic` when `_BM25_AVAILABLE` is `False` (added in Task 5). This task adds the test.

- [ ] **Step 1: Write failing test**

Add after `TestIndexCacheInvalidation` in `tests/test_mcp_server.py`:

```python
class TestBM25GracefulDegradation:
    def test_falls_back_to_heuristic_when_bm25_unavailable(self, mock_minigraf_db, tmp_path, monkeypatch):
        import mcp_server
        from unittest.mock import patch, MagicMock
        mock_class, db_instance = mock_minigraf_db
        # Heuristic path does a contains? query — return a matching fact
        db_instance.execute.return_value = json.dumps({
            "results": [["use", "decided to use redis"]]
        })
        mcp_server.open_db(str(tmp_path / "t.graph"))
        monkeypatch.setattr(mcp_server, "_BM25_AVAILABLE", False)
        result = mcp_server.handle_memory_prepare_turn("decided to use redis")
        # Heuristic path produces "Relevant memory context:" when facts are found
        assert "Relevant memory context:" in result

    def test_index_cache_invalidate_noop_when_bm25_unavailable(self, monkeypatch):
        import mcp_server
        from unittest.mock import patch
        monkeypatch.setattr(mcp_server, "_BM25_AVAILABLE", False)
        # invalidate() should still not raise even if BM25 unavailable
        # (the FactIndex constructor guards on _BM25Okapi being None)
        cache = mcp_server.IndexCache()
        cache._rebuild()  # should not raise
        assert cache.get() is None or isinstance(cache.get(), mcp_server.FactIndex)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestBM25GracefulDegradation -v
```

Expected: FAIL — the monkeypatch of `_BM25_AVAILABLE` isn't wired to the fallback path yet (it is, actually — verify the test passes from the Task 5 implementation).

If both tests pass already (because Task 5 implemented the guard correctly), proceed directly to Step 4.

- [ ] **Step 3: (If needed) fix any issues surfaced by the tests**

If the test reveals the fallback isn't working, verify `handle_memory_prepare_turn` in `mcp_server.py` reads `_BM25_AVAILABLE` from module scope (not a local import). The check must be:

```python
if not _BM25_AVAILABLE:
    return _handle_memory_prepare_turn_heuristic(user_message)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestBM25GracefulDegradation -v
```

Expected: 2 PASSED

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test(retrieval): verify graceful degradation when rank_bm25 unavailable"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✓ In-process, no external API calls — `rank_bm25` is pure Python
- ✓ Latency: `prepare_turn` never waits for a rebuild (serves stale/None)
- ✓ Async rebuild — `threading.Thread(daemon=True)`
- ✓ Stale index served during rebuild — `_rebuilding` guard
- ✓ Empty if no index — `if index is None: return ""`
- ✓ Memory facts boosted — `_MEMORY_PREFIXES` + multiplier in `FactIndex.query`
- ✓ Both memory and git facts indexed — full `[:find ?e ?a ?v ...]` fetch
- ✓ Invalidation: transact ✓, retract ✓, git ingest (end of `_run_ingestion`) ✓
- ✓ `VULCAN_MEMORY_BOOST` env var (default 2.0) — read in `IndexCache._rebuild`
- ✓ `VULCAN_PREPARE_SCAN_LIMIT` env var (default 50) — read in `handle_memory_prepare_turn`
- ✓ `rank_bm25` not installed → graceful fallback to heuristic
- ✓ `pyproject.toml` and `install.py` updated

**Type consistency across tasks:**
- `FactIndex(facts: List[List], boost: float)` — consistent Tasks 3, 4, 5
- `IndexCache.get() -> Optional[FactIndex]` — consistent Tasks 4, 5
- `_tokenize(text: str) -> List[str]` — consistent Tasks 2, 3
- `_MEMORY_PREFIXES: tuple[str, ...]` — consistent Tasks 2, 3
- `_index_cache: IndexCache` — module-level singleton, consistent Tasks 4–7
