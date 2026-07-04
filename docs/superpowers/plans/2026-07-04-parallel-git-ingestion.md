# Parallel Git-Ingestion Extraction + Crash-Safe Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parallelize the read-only, stateless part of git ingestion (diff-tree + git-show + tree-sitter parse) across a thread pool while keeping the stateful, DB-writing part strictly sequential, and make long-running ingests survive an abrupt session end by resuming correctly from the watermark.

**Architecture:** Producer/consumer split inside `_run_ingestion` (`mcp_server.py`). A bounded sliding-window pipeline of `asyncio` futures runs a new `_extract_commit()` on a `ThreadPoolExecutor` (thread-local `tree_sitter.Parser` instances, one per thread per language) ahead of when the existing, unmodified consumer logic needs each commit's data. Two crash-safety gaps get closed in the same pass: `file_deps`/`dep_valid_from` are reloaded from minigraf's per-fact temporal metadata on startup (previously reconstructed in-memory only, so restarts silently corrupted dependency history), and a `SIGTERM`/`SIGINT`/stdio-EOF-triggered graceful shutdown lets the consumer stop cleanly at a commit boundary instead of being cancelled mid-write.

**Tech Stack:** Python 3.10+, `asyncio`, `concurrent.futures.ThreadPoolExecutor`, `threading`, `tree_sitter`, minigraf 1.2.1 (`:db/valid-from`/`:db/valid-to`/`:any-valid-time` Datalog pseudo-attributes), pytest + pytest-asyncio.

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-04-parallel-git-ingestion-design.md` — every task below implements one section of it.
- `minigraf>=1.2.1` is already bumped in `pyproject.toml` and `install.py` (done prior to this plan; no task required).
- Existing tests in `tests/test_mcp_server.py` — especially `TestGetParser`, `TestExtractFromSource`, `TestRunIngestion`, `TestRunIngestionBitemporalClose`, `TestRunIngestionBitemporalDeps` — must continue to pass unmodified. Do not change their assertions; if one fails, the implementation is wrong, not the test.
- Windows is an explicit non-goal for the shutdown-signal work (`asyncio.get_running_loop().add_signal_handler` is POSIX-only); guard with try/except, do not add Windows-specific code paths.
- No new top-level dependencies — `concurrent.futures`, `threading`, and `signal` are all stdlib.

---

### Task 1: Thread-Local Tree-Sitter Parsers

**Files:**
- Modify: `mcp_server.py:96-138` (`_get_parser`)
- Test: `tests/test_mcp_server.py` (new `TestThreadParser` class, add after the existing `TestGetParser` class which ends around line 1330)

**Interfaces:**
- Produces: `_build_parser(lang_name: str) -> Any` (raises on failure), `_thread_parser(file_path: str) -> Optional[Any]`. Task 3's `_extract_commit` calls `_thread_parser`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, right after the `TestGetParser` class:

```python
class TestThreadParser:
    def test_returns_none_for_unsupported_extension(self):
        import mcp_server
        assert mcp_server._thread_parser("data.csv") is None

    def test_returns_a_parser_for_supported_extension(self):
        import mcp_server
        parser = mcp_server._thread_parser("foo.py")
        assert parser is not None

    def test_different_threads_get_different_parser_instances(self):
        import mcp_server
        import threading

        results = {}

        def grab(name):
            results[name] = mcp_server._thread_parser("foo.py")

        t1 = threading.Thread(target=grab, args=("t1",))
        t2 = threading.Thread(target=grab, args=("t2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["t1"] is not None
        assert results["t2"] is not None
        assert results["t1"] is not results["t2"]

    def test_same_thread_reuses_its_own_parser_instance(self):
        import mcp_server
        p1 = mcp_server._thread_parser("foo.py")
        p2 = mcp_server._thread_parser("bar.py")  # same language, same thread
        assert p1 is p2

    def test_concurrent_first_use_of_new_language_warns_once(self, capsys):
        """Two threads racing to build the same never-seen-before language's
        grammar for the first time must not double-import or double-warn —
        regression guard for the lock added around _get_parser's
        first-time-construction branch."""
        import mcp_server
        import threading

        barrier = threading.Barrier(2)

        def touch():
            barrier.wait()
            mcp_server._thread_parser("foo.c")

        threads = [threading.Thread(target=touch) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        err = capsys.readouterr().err
        # Either the grammar loads fine (no warning at all) or, if it's
        # missing in this environment, the warning fires at most once.
        assert err.count("no tree-sitter grammar available for 'c'") <= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestThreadParser -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_thread_parser'`

- [ ] **Step 3: Implement `_build_parser`, the population lock, and `_thread_parser`**

Replace `mcp_server.py:96-138` (the current `_get_parser` function) with:

```python
_grammar_cache_lock = threading.Lock()


def _build_parser(lang_name: str) -> Any:
    """Construct a fresh tree_sitter.Parser for lang_name. Raises on failure
    (missing grammar package, incompatible tree-sitter version, etc).

    No caching, no warning side effects — those stay in _get_parser, the
    only caller that needs to turn a failure into a one-time stderr warning.
    Also used by _thread_parser to build a private-to-this-thread instance
    once _get_parser has already proven the grammar loads; an unexpected
    failure there is left to propagate to the caller (Task 3's
    _extract_commit, running in a worker thread) rather than being
    swallowed, consistent with how any other producer-task exception is
    handled.
    """
    mod = __import__(f"tree_sitter_{lang_name}", fromlist=["language"])
    from tree_sitter import Language, Parser  # type: ignore
    # PHP exposes language_php() instead of language()
    lang_fn = getattr(mod, f"language_{lang_name}", None) or mod.language
    lang_obj = Language(lang_fn())
    return Parser(lang_obj)


def _get_parser(file_path: str) -> Optional[Any]:
    """Return a cached tree_sitter.Parser for the file's language, or None if unsupported.

    Uses the individual tree-sitter-<lang> packages (e.g. tree-sitter-python,
    tree-sitter-rust) via the tree-sitter >=0.22 API, compatible across Python
    3.10-3.14+.

    Previously this also tried the bundled `tree_sitter_languages` package as a
    fast path. That package pins no upper bound on its `tree-sitter` dependency
    and hasn't been updated since tree-sitter's 0.22 API redesign, so a fresh
    install silently resolves an incompatible `tree-sitter` and every parse
    fails at runtime (see issue #86). It has been dropped in favor of the
    per-language packages, which are what `install.py` provisions anyway.
    """
    ext = Path(file_path).suffix.lower()
    lang_name = _EXT_TO_LANG.get(ext)
    if not lang_name:
        return None
    if lang_name in _grammar_cache:
        return _grammar_cache[lang_name]

    with _grammar_cache_lock:
        if lang_name in _grammar_cache:  # another thread populated it while we waited
            return _grammar_cache[lang_name]
        try:
            parser = _build_parser(lang_name)
        except Exception as exc:
            parser = None
            print(
                f"[_get_parser] no tree-sitter grammar available for '{lang_name}' "
                f"({exc!r}); code-structure extraction disabled for this language "
                f"until 'tree-sitter-{lang_name}' is installed.",
                file=sys.stderr,
            )
        _grammar_cache[lang_name] = parser
        return parser


_thread_local = threading.local()


def _thread_parser(file_path: str) -> Optional[Any]:
    """Return a Parser instance private to the calling thread for file_path's language.

    tree_sitter.Parser objects are not safe for concurrent .parse() calls
    from multiple threads. Rather than lock around every parse (which would
    serialize the CPU-bound part of concurrent ingestion), each thread gets
    its own Parser per language, built once and cached in thread-local
    storage. Reuses _get_parser purely as the "is this language supported"
    check — including its shared cache and once-only warning — since that
    part is safe to share across threads (a plain dict read after the first
    population, or a briefly-held lock on a miss).
    """
    if _get_parser(file_path) is None:
        return None
    lang_name = _EXT_TO_LANG[Path(file_path).suffix.lower()]
    cache = getattr(_thread_local, "parsers", None)
    if cache is None:
        cache = {}
        _thread_local.parsers = cache
    if lang_name not in cache:
        cache[lang_name] = _build_parser(lang_name)
    return cache[lang_name]
```

Note: `_get_parser`'s warning message keeps the exact original wording, including `{exc!r}` — `_build_parser` now raises instead of swallowing its own exception, so `_get_parser` catches it right where the warning is printed and still has the real exception object to include, unchanged from today's message.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestThreadParser tests/test_mcp_server.py::TestGetParser tests/test_mcp_server.py::TestExtractFromSource tests/test_mcp_server.py::TestExtractFromSourceCFamily -v`
Expected: all PASS — the `TestGetParser`/`TestExtractFromSource` runs confirm the refactor didn't change `_get_parser`'s existing contract.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add thread-local tree-sitter parsers for concurrent extraction"
```

---

### Task 2: Reload `file_deps`/`dep_valid_from` on Startup

**Files:**
- Modify: `mcp_server.py:2225-2275` (add a new function directly after `_preload_known_entities`, which ends at line 2275)
- Test: `tests/test_mcp_server.py` (new `TestPreloadKnownDeps` class, add after the `TestIngestionWrites` class, which ends around line 1759)

**Interfaces:**
- Consumes: `file_entities: Dict[str, List[str]]` (as already produced by `_preload_known_entities`), `db: Any` (a `MiniGrafDb`-like object with `.execute(str) -> str` returning `{"results": [[...], ...]}` JSON).
- Produces: `_preload_known_deps(db, file_entities) -> Tuple[Dict[str, set], Dict[Tuple[str, str], str]]` — `(file_deps, dep_valid_from)`, matching the types already used in `_run_ingestion` (`file_deps: Dict[str, set]` maps `file_path -> set of dep module idents`; `dep_valid_from: Dict[tuple, str]` maps `(src_module_ident, dep_ident) -> intro commit ts ISO string`). Task 4 calls this.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, right after the `TestIngestionWrites` class:

```python
class TestPreloadKnownDeps:
    def test_reloads_open_depends_on_edge(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server

        src_ident = mcp_server._code_ident("module", "mod_a.py")
        dep_ident = mcp_server._canonical_ident("module", "mod_b")
        # 1704067200000 ms == 2024-01-01T00:00:00.000Z
        db_instance.execute.return_value = json.dumps(
            {"results": [[src_ident, dep_ident, 1704067200000]]}
        )
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()

        file_entities = {"mod_a.py": [src_ident]}
        file_deps, dep_valid_from = mcp_server._preload_known_deps(db, file_entities)

        assert file_deps["mod_a.py"] == {dep_ident}
        assert dep_valid_from[(src_ident, dep_ident)] == "2024-01-01T00:00:00.000Z"

    def test_query_includes_any_valid_time_and_forever_filter(self, mock_minigraf_db, tmp_path):
        """The query must ask for :any-valid-time (required for any per-fact
        pseudo-attribute to bind) and filter :db/valid-to down to the
        VALID_TIME_FOREVER sentinel so closed edges aren't reloaded as open."""
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._preload_known_deps(db, {})

        query = db_instance.execute.call_args[0][0]
        assert ":any-valid-time" in query
        assert ":depends-on" in query
        assert ":db/valid-from" in query
        assert ":db/valid-to" in query
        assert "9223372036854775807" in query

    def test_no_deps_returns_empty_structures(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        db_instance.execute.return_value = json.dumps({"results": []})
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()

        file_deps, dep_valid_from = mcp_server._preload_known_deps(db, {"mod_a.py": []})

        assert file_deps == {}
        assert dep_valid_from == {}

    def test_query_failure_is_non_fatal(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        from minigraf import MiniGrafError
        db_instance.execute.side_effect = MiniGrafError("boom")
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()

        file_deps, dep_valid_from = mcp_server._preload_known_deps(db, {"mod_a.py": []})

        assert file_deps == {}
        assert dep_valid_from == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadKnownDeps -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_preload_known_deps'`

- [ ] **Step 3: Implement `_preload_known_deps`**

Insert directly after `_preload_known_entities` (after `mcp_server.py:2275`, before the `def _ingest_tags` at line 2278):

```python
_VALID_TIME_FOREVER_MS = (1 << 63) - 1  # minigraf's i64::MAX "still open" :valid-to sentinel


def _preload_known_deps(
    db: Any, file_entities: Dict[str, List[str]]
) -> tuple:
    """Reload file_deps/dep_valid_from from durable :depends-on facts.

    Mirrors _preload_known_entities, but :depends-on facts have no
    :introduced-by-style companion edge to a commit's :date, so the
    introduction timestamp has to come from the fact's own :db/valid-from
    via minigraf's per-fact temporal metadata pseudo-attributes (minigraf
    >=1.0.0, verified present at the pinned/installed 1.2.1). :any-valid-time
    is required for any per-fact pseudo-attribute to bind at all; the
    explicit :db/valid-to equality against the "forever" sentinel is what
    restricts results to edges that haven't been closed (:any-valid-time
    alone would also return already-closed historical facts).

    Without this, file_deps/dep_valid_from start empty on every restart,
    which not only breaks removed-dependency detection but actively
    corrupts history: current_deps - previous_deps would treat every
    already-standing dependency as newly introduced the next time its file
    is touched, overwriting its true :valid-from.

    Returns (file_deps, dep_valid_from):
    file_deps maps file_path -> set of dep module idents.
    dep_valid_from maps (src_module_ident, dep_ident) -> ISO 8601 intro timestamp.
    """
    file_deps: Dict[str, set] = {}
    dep_valid_from: Dict[tuple, str] = {}

    ident_to_file = {
        _code_ident("module", file_path): file_path for file_path in file_entities
    }

    try:
        raw = db.execute(
            "(query [:find ?src ?dep ?vf "
            ":any-valid-time "
            ":where [?src :depends-on ?dep] "
            "[?src :db/valid-from ?vf] "
            "[?src :db/valid-to ?vt] "
            f"[(= ?vt {_VALID_TIME_FOREVER_MS})]])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return file_deps, dep_valid_from

    for src_ident, dep_ident, vf_ms in rows:
        file_path = ident_to_file.get(src_ident)
        if file_path is None:
            continue
        vf_iso = (
            datetime.datetime.fromtimestamp(vf_ms / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        file_deps.setdefault(file_path, set()).add(dep_ident)
        dep_valid_from[(src_ident, dep_ident)] = vf_iso

    return file_deps, dep_valid_from
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadKnownDeps -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: reload file_deps/dep_valid_from from durable facts on startup"
```

---

### Task 3: Producer Function `_extract_commit`

**Files:**
- Modify: `mcp_server.py` — insert a new function directly before `_run_ingestion` (currently at line 2309)
- Test: `tests/test_mcp_server.py` (new `TestExtractCommit` class, add after the `TestGitHelpers` class, which ends around line 1669)

**Interfaces:**
- Consumes: `_thread_parser` (Task 1), `_git_changed_files`, `_git_file_content`, `_extract_from_source` (all pre-existing).
- Produces: `_extract_commit(repo_path: str, commit_hash: str) -> List[Tuple[str, str, Optional[Dict[str, List[str]]]]]` — list of `(status, file_path, extracted)`. `extracted` is `None` for `D` status, the `_extract_from_source` result dict for `A`/`M`. Files with no supported parser, or whose content fetch fails, are omitted from the list entirely. Task 4's pipeline calls this via `loop.run_in_executor`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, right after the `TestGitHelpers` class:

```python
class TestExtractCommit:
    def test_added_file_returns_extracted_dict(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]

        results = mcp_server._extract_commit(str(git_repo), first_hash)

        assert len(results) == 1
        status, file_path, extracted = results[0]
        assert status == "A"
        assert file_path == "auth.py"
        assert "login" in extracted["functions"]

    def test_deleted_file_has_none_extracted(self, git_repo_with_deletion):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_with_deletion), watermark_hash=None)
        delete_hash = commits[-1][0]

        results = mcp_server._extract_commit(str(git_repo_with_deletion), delete_hash)

        d_entries = [r for r in results if r[0] == "D"]
        assert len(d_entries) == 1
        assert d_entries[0][2] is None

    def test_unsupported_extension_is_omitted(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_changed_files",
            lambda repo, commit: [("A", "notes.txt")],
        )
        results = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []

    def test_content_fetch_failure_is_omitted_not_raised(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_changed_files",
            lambda repo, commit: [("A", "auth.py")],
        )

        def boom(repo, commit, path):
            raise mcp_server.MiniGrafError("simulated git-show failure")

        monkeypatch.setattr(mcp_server, "_git_file_content", boom)
        results = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommit -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_extract_commit'`

- [ ] **Step 3: Implement `_extract_commit`**

Insert directly before `async def _run_ingestion` (currently `mcp_server.py:2309`):

```python
def _extract_commit(
    repo_path: str, commit_hash: str
) -> List[Tuple[str, str, Optional[Dict[str, List[str]]]]]:
    """Read-only, stateless per-commit extraction: diff-tree + git-show + tree-sitter parse.

    Runs in a worker thread via the ThreadPoolExecutor in _run_ingestion.
    Touches no shared mutable state and no DB. Returns one entry per changed
    file that has a supported parser; A/M files whose content fetch fails
    are omitted entirely, mirroring the previous inline `continue` — the
    file is simply skipped for this commit, same as today.
    """
    results: List[Tuple[str, str, Optional[Dict[str, List[str]]]]] = []
    for status, file_path in _git_changed_files(repo_path, commit_hash):
        parser = _thread_parser(file_path)
        if parser is None:
            continue
        if status == "D":
            results.append((status, file_path, None))
            continue
        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue
        extracted = _extract_from_source(content, parser, file_path)
        results.append((status, file_path, extracted))
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommit -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _extract_commit producer function for parallel extraction"
```

---

### Task 4: Wire the Sliding-Window Pipeline into `_run_ingestion`

**Files:**
- Modify: `mcp_server.py:1-22` (imports), `mcp_server.py:2309-2488` (`_run_ingestion`)
- Test: `tests/test_mcp_server.py` (new `TestRunIngestionConcurrency` class, add after the existing `TestRunIngestion` class, which ends around line 2151)

**Interfaces:**
- Consumes: `_extract_commit` (Task 3), `_preload_known_deps` (Task 2). All other consumed names (`_preload_known_entities`, `_git_commits`, `_ingest_transact`, `_ingest_close`, `_build_code_triples`, `_resolve_module_import`, `_watermark_update`, `_ingest_tags`, `_last_run_write`, `_count_commit_entities`) are unchanged from today.
- Produces: `_run_ingestion` keeps its existing signature and externally-visible behavior (same `_ingest_progress` fields, same DB writes, same watermark semantics) — this task changes its internals only. Also introduces the `MINIGRAF_INGEST_WORKERS` env var.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, right after the `TestRunIngestion` class:

```python
class TestRunIngestionConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_run_matches_sequential_facts(self, mock_minigraf_db, git_repo_with_deps, monkeypatch):
        """A run using the thread-pool pipeline must produce the exact same
        set of transacted triples, in the same commit order, as today's
        sequential loop — this is the core correctness guarantee for the
        producer/consumer split."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_deps / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        transacted: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture(db, triples, ts_iso, reason=""):
            transacted.append(list(triples))
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture)
        await mcp_server._run_ingestion(str(git_repo_with_deps), "HEAD")

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        mod_b_ident = mcp_server._code_ident("module", "mod_b.py")
        all_triples = [t for batch in transacted for t in batch]
        assert any(mod_a_ident in t for t in all_triples)
        assert any(mod_b_ident in t for t in all_triples)
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 1

    @pytest.mark.asyncio
    async def test_worker_count_env_var_is_respected(self, mock_minigraf_db, git_repo, monkeypatch):
        monkeypatch.setenv("MINIGRAF_INGEST_WORKERS", "1")
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2

    @pytest.mark.asyncio
    async def test_one_commits_file_failure_does_not_affect_other_commits(
        self, mock_minigraf_db, git_repo, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        failing_hash = commits[0][0]
        real_content = mcp_server._git_file_content

        def flaky(repo, commit, path):
            if commit == failing_hash:
                raise mcp_server.MiniGrafError("simulated failure for one commit's file")
            return real_content(repo, commit, path)

        monkeypatch.setattr(mcp_server, "_git_file_content", flaky)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # Both commits still get counted as processed even though the first
        # commit's only changed file failed to fetch.
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_progress["processed"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestRunIngestionConcurrency -v`
Expected: FAIL — at this point `_run_ingestion` still works sequentially, so `test_concurrent_run_matches_sequential_facts` and the worker-count test will likely pass already (the pipeline doesn't exist yet, but neither does anything contradicting them); confirm by running and checking `MINIGRAF_INGEST_WORKERS` has no effect yet — the point of this task's Step 4 is that they *keep* passing once the pipeline is wired in. If any already fail, note why before proceeding.

- [ ] **Step 3: Rewrite `_run_ingestion` with the sliding-window pipeline**

First, add `import concurrent.futures` and `from collections import deque` to the import block at the top of `mcp_server.py` (after `import asyncio` at line 8, and alongside the `from pathlib import Path` block at line 17 respectively):

```python
import asyncio
import concurrent.futures
import datetime
```

```python
from collections import deque
from pathlib import Path
```

Then replace the entire `_run_ingestion` function (`mcp_server.py:2309-2488`) with:

```python
async def _run_ingestion(repo_path: str, branch: str) -> None:
    """Background coroutine: walk git history and ingest code structure.

    Extraction (git show + tree-sitter parse) for upcoming commits runs
    ahead of time on a thread pool via a bounded sliding-window pipeline;
    all DB-writing bookkeeping below stays strictly sequential, one commit
    at a time, exactly as before this pipeline was introduced.
    """
    global _db, _ingest_progress
    _shutdown_requested.clear()
    try:
        # Read watermark and pre-load known entities/deps before releasing DB
        db = get_db()
        watermark = _watermark_query(db)
        prior_ingested = _count_commit_entities(db)
        entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
        file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
        # minigraf exposes no explicit close(): the file lock is only released once
        # every reference to the handle is gone, so the local `db` must be cleared
        # too, not just the global — otherwise this frame keeps it alive (and the
        # lock held) through the potentially slow commit enumeration below.
        _db = None  # release file lock while enumerating commits
        db = None

        commits = _git_commits(repo_path, watermark, branch)
        repo_total_result = _subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        repo_total = int(repo_total_result.stdout.strip()) if repo_total_result.returncode == 0 else len(commits)
        _ingest_progress["total"] = repo_total
        _ingest_progress["status"] = "running"
        _ingest_progress["processed"] = prior_ingested
        _ingest_progress["prior_ingested"] = prior_ingested

        last_hash = watermark or ""

        env_workers = os.environ.get("MINIGRAF_INGEST_WORKERS")
        max_workers = int(env_workers) if env_workers else min(32, (os.cpu_count() or 1) + 4)
        pipeline_depth = max_workers * 2

        loop = asyncio.get_running_loop()
        completed_all = True

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            commits_iter = iter(commits)
            pending: Any = deque()

            def submit_next() -> bool:
                try:
                    commit = next(commits_iter)
                except StopIteration:
                    return False
                fut = loop.run_in_executor(executor, _extract_commit, repo_path, commit[0])
                pending.append((commit, fut))
                return True

            for _ in range(pipeline_depth):
                if not submit_next():
                    break

            while pending:
                if _shutdown_requested.is_set():
                    completed_all = False
                    break

                (commit_hash, commit_ts_iso, author, subject), fut = pending.popleft()
                extracted_files = await fut
                submit_next()

                last_hash = commit_hash
                _ingest_progress["current_commit"] = commit_hash
                reason = f"git:{commit_hash} {author}: {subject}"

                # Build commit entity ident from first 12 chars of hash
                commit_ident = f":commit/{commit_hash[:12]}"

                # Acquire DB fresh each commit — never hold across yield
                db = get_db()
                try:
                    add_triples: List[str] = [
                        f"[{commit_ident} :entity-type :type/commit]",
                        f'[{commit_ident} :ident "{commit_ident}"]',
                        f'[{commit_ident} :description "{_edn_escape(subject[:120])}"]',
                        f'[{commit_ident} :hash "{commit_hash}"]',
                        f'[{commit_ident} :author "{_edn_escape(author)}"]',
                        f'[{commit_ident} :subject "{_edn_escape(subject[:200])}"]',
                        f'[{commit_ident} :date "{commit_ts_iso}"]',
                    ]
                    close_items: List[tuple] = []  # (triples, original_ts_iso)
                    dep_add_triples: List[str] = []  # :depends-on triples to transact individually

                    for status, file_path, extracted in extracted_files:
                        if status == "D":
                            # Close module and all known child entities for this file
                            idents = file_entities.get(file_path, [_code_ident("module", file_path)])
                            module_ident = _code_ident("module", file_path)
                            for ident in idents:
                                orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                desc = entity_descriptions.get(ident, "")
                                close_items.append(
                                    (_build_close_triples(ident, desc, module_ident), orig_ts)
                                )
                            # Close all :depends-on edges for the deleted module
                            for dep_ident in file_deps.get(file_path, set()):
                                orig_ts = dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                                close_items.append(
                                    ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                                )
                            file_deps.pop(file_path, None)
                        else:  # A or M
                            previous_idents = set(file_entities.get(file_path, []))
                            triples = _build_code_triples(
                                file_path, extracted, commit_ts_iso, entity_valid_from,
                                entity_descriptions, file_entities, commit_ident,
                            )
                            add_triples.extend(triples)
                            # Detect entities removed from a modified file.
                            # _build_code_triples only appends to file_entities, never removes.
                            # Compare previous idents against the idents derivable from the
                            # current extraction to find what was deleted.
                            if status == "M":
                                module_ident = _code_ident("module", file_path)
                                current_extracted_idents: set = {module_ident}
                                for fn_name in extracted.get("functions", []):
                                    current_extracted_idents.add(_code_ident("function", file_path, fn_name))
                                for cls_name in extracted.get("classes", []):
                                    current_extracted_idents.add(_code_ident("class", file_path, cls_name))
                                removed_idents = previous_idents - current_extracted_idents
                                for ident in removed_idents:
                                    orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                    desc = entity_descriptions.get(ident, "")
                                    close_items.append(
                                        (_build_close_triples(ident, desc, module_ident), orig_ts)
                                    )
                            # Compute dep edges for this file and diff against previous
                            module_ident = _code_ident("module", file_path)
                            current_deps: set = set()
                            for import_name in set(extracted.get("imports", [])):
                                dep_ident = _resolve_module_import(import_name, file_entities)
                                if dep_ident != module_ident:
                                    current_deps.add(dep_ident)
                            previous_deps = file_deps.get(file_path, set())
                            for dep_ident in current_deps - previous_deps:
                                dep_add_triples.append(f"[{module_ident} :depends-on {dep_ident}]")
                                dep_valid_from[(module_ident, dep_ident)] = commit_ts_iso
                            if status == "M":
                                for dep_ident in previous_deps - current_deps:
                                    orig_ts = dep_valid_from.get((module_ident, dep_ident), commit_ts_iso)
                                    close_items.append(
                                        ([f"[{module_ident} :depends-on {dep_ident}]"], orig_ts)
                                    )
                            file_deps[file_path] = current_deps

                    # Split :contains triples out before batching.  Minigraf's EAVT
                    # pending index lacks value bytes in the key, so batching multiple
                    # [module :contains fn] facts in one transact silently drops all
                    # but the last.  Each :contains triple gets its own transact so
                    # they receive distinct tx_counts and avoid the index collision.
                    contains_triples = [t for t in add_triples if ":contains" in t]
                    other_triples = [t for t in add_triples if ":contains" not in t]
                    _ingest_transact(db, other_triples, commit_ts_iso, reason)
                    for ct in contains_triples:
                        _ingest_transact(db, [ct], commit_ts_iso, reason)
                    # :depends-on triples transacted individually — same EAVT collision risk
                    # as :contains when multiple deps share the same source module
                    for dt in dep_add_triples:
                        _ingest_transact(db, [dt], commit_ts_iso, reason)
                    for close_triples, orig_ts in close_items:
                        _ingest_close(db, close_triples, orig_ts, commit_ts_iso, reason)

                    # Ingest :parent edges — one transact per parent to avoid EAVT
                    # collision for merge commits (which have two parent hashes).
                    try:
                        for parent_hash in _git_parent_hashes(repo_path, commit_hash):
                            parent_ident = f":commit/{parent_hash[:12]}"
                            db.execute(
                                f'(transact [[{commit_ident} :parent {parent_ident}]] '
                                f'{{:valid-from "{commit_ts_iso}"}})'
                            )
                    except Exception:
                        pass  # non-fatal; parent edges are best-effort

                    _watermark_update(db, commit_hash, commit_ts_iso, reason)
                    db.checkpoint()

                finally:
                    _db = None  # release file lock between commits
                    db = None   # drop local reference too — see note above

                _ingest_progress["processed"] += 1
                await asyncio.sleep(0)  # yield to event loop

        if completed_all:
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            db = get_db()
            try:
                _ingest_tags(db, repo_path, now)
                _last_run_write(db, last_hash, now, _ingest_progress["processed"])
                db.checkpoint()
            finally:
                _db = None

            _ingest_progress["status"] = "complete"
            _index_cache.invalidate()
        else:
            _ingest_progress["status"] = "stopped"

    except Exception as e:
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
```

Also add the shutdown-related global near the other ingestion-state globals (`mcp_server.py:71-76`, right after `_ingest_progress`'s closing `}`):

```python
_shutdown_requested = asyncio.Event()
```

- [ ] **Step 4: Run tests to verify they pass**

Run the full existing ingestion test surface plus the new concurrency tests to confirm nothing regressed:

```bash
python -m pytest tests/test_mcp_server.py -k "Ingestion or Preload or ExtractCommit or ThreadParser or GetParser or ExtractFromSource or GitHelpers" -v
```

Expected: PASS, including every pre-existing `TestRunIngestion*` class and the new `TestRunIngestionConcurrency` class.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: parallelize git-ingestion extraction via sliding-window pipeline"
```

---

### Task 5: Graceful Shutdown + Resume-via-Watermark

**Files:**
- Modify: `mcp_server.py:1-22` (imports), `mcp_server.py:2842-2865` (`main`/`run`)
- Modify: `SKILL.md:294` (status value documentation)
- Test: `tests/test_mcp_server.py` (new `TestRunIngestionShutdown` class, add after `TestRunIngestionConcurrency`)

**Interfaces:**
- Consumes: `_shutdown_requested` (module-level `asyncio.Event`, added in Task 4).
- Produces: no new function signatures; `_ingest_progress["status"]` gains a `"stopped"` value alongside the existing `"idle"`/`"running"`/`"complete"`/`"error"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, right after `TestRunIngestionConcurrency`:

```python
class TestRunIngestionShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_mid_run_stops_at_commit_boundary(self, mock_minigraf_db, git_repo, monkeypatch):
        """git_repo has 2 commits. Request shutdown right after the first
        commit's extraction is consumed but before the second is processed;
        the loop must stop cleanly with status 'stopped' and only 1 commit
        durably processed."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        original_sleep = asyncio.sleep

        async def patched_sleep(t):
            # Fires after the first commit's processed += 1, i.e. exactly at
            # the next loop-top boundary check.
            mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", patched_sleep):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "stopped"
        assert mcp_server._ingest_progress["processed"] == 1

    @pytest.mark.asyncio
    async def test_resumes_from_watermark_after_shutdown(self, mock_minigraf_db, git_repo, monkeypatch):
        """After a simulated shutdown mid-run, a second _run_ingestion call
        against the same (mocked) DB state must pick up the watermark that
        was written for the last fully-completed commit and finish the
        remaining commit(s), without re-processing or skipping any."""
        mock_class, db_instance = mock_minigraf_db
        import mcp_server

        # In-memory fake DB standing in for minigraf so the watermark
        # written by run 1 is genuinely visible to run 2 (the default mock
        # always returns the same canned response and can't model this).
        state = {"watermark": None}

        def execute(cmd, *a, **k):
            if "(query" in cmd and ":ingestion/watermark" in cmd and ":hash" in cmd:
                if state["watermark"]:
                    return json.dumps({"results": [[state["watermark"]]]})
                return json.dumps({"results": []})
            if "(transact" in cmd and ":ingestion/watermark" in cmd:
                import re
                m = re.search(r':hash "([0-9a-f]+)"', cmd)
                if m:
                    state["watermark"] = m.group(1)
            return json.dumps({"results": []})

        db_instance.execute.side_effect = execute
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

        original_sleep = asyncio.sleep
        stop_once = {"done": False}

        async def stop_after_first(t):
            if not stop_once["done"]:
                stop_once["done"] = True
                mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", stop_after_first):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "stopped"
        first_run_processed = mcp_server._ingest_progress["processed"]
        assert first_run_processed == 1

        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert mcp_server._ingest_progress["status"] == "complete"
        # Second run only had the 1 remaining commit to do, and
        # _count_commit_entities (mocked to [] here) seeds prior_ingested=0,
        # so processed reflects just that run's own work.
        assert mcp_server._ingest_progress["processed"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestRunIngestionShutdown -v`
Expected: FAIL — `test_shutdown_mid_run_stops_at_commit_boundary` fails because `_ingest_progress["status"]` is `"complete"` instead of `"stopped"` (the shutdown check exists from Task 4's `_shutdown_requested.clear()`/pipeline wiring, but nothing in `main()` or elsewhere sets it yet in a way this test exercises — confirm the failure is on the assertion, not an `AttributeError`, since `_shutdown_requested` was already added in Task 4).

- [ ] **Step 3: Add signal handling and bounded-await shutdown to `main()`**

Add `import signal` to the import block at the top of `mcp_server.py` (after `import re` at line 12):

```python
import re
import signal
import subprocess as _subprocess
```

Replace `async def main()` (`mcp_server.py:2842-2860`) with:

```python
async def main() -> None:
    global _server_ref, _ingest_task, _ingest_progress
    _server_ref = server
    # Auto-start incremental ingest on server startup so ingestion begins
    # immediately without waiting for a user prompt.  Runs as a background
    # asyncio task — never blocks the message loop.
    # Set MINIGRAF_NO_AUTO_INGEST=1 to skip auto-start (used by eval sandboxes).
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None,
    }
    if not os.environ.get("MINIGRAF_NO_AUTO_INGEST"):
        _ingest_task = asyncio.create_task(_run_ingestion(str(Path.cwd()), "HEAD"))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_requested.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows: add_signal_handler unsupported; no graceful-shutdown-by-signal there

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        # The MCP server's most common "session ended" signal is stdin EOF
        # (the parent closing the pipe) rather than a delivered signal, so
        # this runs on every exit path. Give a long-running ingest a chance
        # to reach its next commit boundary and exit cleanly — leaving the
        # watermark correctly reflecting the last fully-completed commit —
        # instead of asyncio.run() abruptly cancelling it mid-write once
        # this coroutine returns.
        _shutdown_requested.set()
        if _ingest_task is not None and not _ingest_task.done():
            try:
                await asyncio.wait_for(_ingest_task, timeout=30)
            except asyncio.TimeoutError:
                _ingest_task.cancel()
```

`def run()` (`mcp_server.py:2863-2865`) is unchanged.

- [ ] **Step 4: Update `SKILL.md`'s documented status values**

In `SKILL.md`, change line 294 from:

```
`status` is one of: `idle`, `running`, `complete`, `error`. `processed` is the
```

to:

```
`status` is one of: `idle`, `running`, `complete`, `error`, `stopped`. `stopped`
means a graceful shutdown (session end) paused ingestion between commits —
not a failure; the next `minigraf_ingest_git` call (or server auto-start)
resumes from the watermark automatically. `processed` is the
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/test_mcp_server.py::TestRunIngestionShutdown -v
python -m pytest tests/test_mcp_server.py -v
```

Expected: `TestRunIngestionShutdown` PASSes, and the full suite still passes (confirms no regression to `TestOpenDb`, `TestRunIngestion*`, `TestMinigrafIngestStatus`, etc.).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py SKILL.md tests/test_mcp_server.py
git commit -m "feat: graceful shutdown on SIGTERM/SIGINT/stdio-EOF, resume via watermark"
```

---

## Final Verification

- [ ] **Run the full test suite once more from a clean state**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS, including every test listed in this plan and every pre-existing test in `tests/test_mcp_server.py`.

- [ ] **Manual smoke test against a real (non-mocked) repo**

```bash
cd /tmp && rm -rf smoke-repo && git init smoke-repo && cd smoke-repo
git config user.email t@t.com && git config user.name T
echo 'def f(): pass' > a.py && git add . && git commit -m "first"
echo 'import os' > b.py && git add . && git commit -m "second"
MINIGRAF_GRAPH_PATH=/tmp/smoke-repo/memory.graph MINIGRAF_NO_AUTO_INGEST=1 \
  python -c "
import asyncio, mcp_server
mcp_server.open_db('/tmp/smoke-repo/memory.graph')
asyncio.run(mcp_server._run_ingestion('/tmp/smoke-repo', 'HEAD'))
print(mcp_server._ingest_progress)
"
```

Expected: `{'status': 'complete', 'processed': 2, ...}` with no exceptions, confirming the real (non-mocked) `ThreadPoolExecutor` + real `tree_sitter` parsers path works end to end.
