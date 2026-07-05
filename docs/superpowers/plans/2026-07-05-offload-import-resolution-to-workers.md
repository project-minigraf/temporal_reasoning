# Offload Import Resolution and Triple Construction to Extraction Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move import-to-module resolution and "if this turns out to be new" triple-string construction out of `_run_ingestion`'s serial main-thread loop and into `_extract_commit`, which already runs on the extraction worker pool — so that work overlaps with other in-flight commits' git-subprocess I/O instead of sitting on the critical path.

**Architecture:** `_extract_commit` (worker thread) gains a new precomputation step per changed file: resolve every import against `git ls-tree -r --name-only <commit_hash>` (a pure function of the commit hash, unlike the incrementally-mutated `file_entities` dict) via the existing `_resolve_module_import`, and build the "candidate" triple strings for the module/function/class idents the file would introduce if they turn out to be genuinely new. The main thread's `_build_code_triples` shrinks to just the diff against `entity_valid_from` (the one piece that's genuinely serial) plus picking pre-built strings instead of formatting them inline.

**Tech Stack:** Python 3.10+, `concurrent.futures.ThreadPoolExecutor` (unchanged), stdlib `subprocess` (`git ls-tree`), pytest + pytest-asyncio.

## Global Constraints

- Source issue: GitHub #100 — every task below implements one part of its "Proposed split."
- `_resolve_module_import`'s own signature and matching logic (mcp_server.py:1161-1253) must NOT change — only what dict gets passed to it as `file_entities`, and from where it's called, changes. Every test in `TestResolveModuleImportTieredMatcher` and `TestUnresolvedImportTagging` (tests/test_mcp_server.py:3551-3675+) must keep passing unmodified.
- `TestRunIngestionConcurrency.test_concurrent_run_matches_sequential_facts` (tests/test_mcp_server.py:2658-2714) is the load-bearing regression test for this refactor — it must keep passing unmodified after every task.
- New behavior, called out explicitly by the issue's "Correctness wrinkle to watch": import resolution must reflect the specific historical commit's tree (`git ls-tree -r --name-only <commit_hash>`, filtered to extensions in `_EXT_TO_LANG`), not the incrementally-built `file_entities` dict (which today is pre-seeded from `git ls-files` at HEAD and therefore can incorrectly resolve an import against a file that doesn't exist yet as of the commit being processed). Task 5 adds a regression test for this.
- No new dependencies — `git ls-tree` follows the exact subprocess convention already used by every other git helper in `mcp_server.py` (`_subprocess.run([...], cwd=repo_path, capture_output=True, text=True, check=True)`).

---

### Task 1: `_known_files_at_commit` git helper

**Files:**
- Modify: `mcp_server.py` — add new function directly after `_git_file_content` (currently mcp_server.py:1391-1397), before `_parse_gitmodules`.
- Test: `tests/test_mcp_server.py` — add new `TestKnownFilesAtCommit` class directly after `TestGitHelpers`/`TestGitDiffTreeRaw` (the git-helper test classes starting around line 1778).

**Interfaces:**
- Produces: `_known_files_at_commit(repo_path: str, commit_hash: str) -> Dict[str, List[str]]`. Task 2's `_precompute_file_triples` and Task 4's updated `_extract_commit` consume this.

- [ ] **Step 1: Write the failing tests**

```python
class TestKnownFilesAtCommit:
    def test_returns_files_present_at_that_commit(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_commit_hash = commits[0][0]
        known = mcp_server._known_files_at_commit(str(git_repo), first_commit_hash)
        assert "auth.py" in known
        # models.py isn't added until the second commit
        assert "models.py" not in known

    def test_second_commit_sees_both_files(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        second_commit_hash = commits[1][0]
        known = mcp_server._known_files_at_commit(str(git_repo), second_commit_hash)
        assert "auth.py" in known
        assert "models.py" in known

    def test_filters_out_unsupported_extensions(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.py").write_text("def f(): pass\n")
        (repo / "README.md").write_text("hello\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(repo), commits[0][0])
        assert "main.py" in known
        assert "README.md" not in known

    def test_returned_dict_shape_matches_file_entities(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(git_repo), commits[0][0])
        assert known["auth.py"] == []
```

This test class needs `_subprocess` imported at module scope in the test file — check the existing `import subprocess as _subprocess` near the top of `tests/test_mcp_server.py` before adding; it is already used by other fixtures (e.g. `git_repo`), so no new import is needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestKnownFilesAtCommit -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_known_files_at_commit'`

- [ ] **Step 3: Implement `_known_files_at_commit`**

Add to `mcp_server.py` directly after `_git_file_content` (after line 1397, before the `_parse_gitmodules` function):

```python
def _known_files_at_commit(repo_path: str, commit_hash: str) -> Dict[str, List[str]]:
    """Return {file_path: []} for every file tracked at commit_hash whose extension
    has a supported tree-sitter grammar (_EXT_TO_LANG).

    A pure function of commit_hash via `git ls-tree -r --name-only`, independent of
    ingestion progress — unlike the incrementally-mutated file_entities dict, this
    reflects the repo's actual state at that specific historical commit, so it can
    run inside _extract_commit on the worker pool instead of waiting for the serial
    main thread to catch up. Shaped like file_entities (dict keyed on path, values
    unused) so it can be passed straight into _resolve_module_import, which only
    ever reads the dict's keys.
    """
    result = _subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    known: Dict[str, List[str]] = {}
    for path in result.stdout.strip().splitlines():
        if Path(path).suffix.lower() in _EXT_TO_LANG:
            known[path] = []
    return known
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestKnownFilesAtCommit -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _known_files_at_commit git helper for per-commit-accurate import resolution"
```

---

### Task 2: `_precompute_file_triples` pure precomputation function

**Files:**
- Modify: `mcp_server.py` — add new function directly before `_build_code_triples` (currently mcp_server.py:2465).
- Test: `tests/test_mcp_server.py` — add new `TestPrecomputeFileTriples` class directly after `TestIngestionWrites` (ends around line 2288, right before `TestPreloadKnownDeps`).

**Interfaces:**
- Consumes: `_code_ident(entity_type, file_path, name=None) -> str`, `_edn_escape(s) -> str`, `_resolve_module_import(import_name, file_entities, importing_file=None) -> Tuple[str, bool]` (all pre-existing, unchanged).
- Produces: `_precompute_file_triples(file_path: str, extracted: Dict[str, List[str]], commit_ident: str, known_files: Dict[str, List[str]]) -> Dict[str, Any]` returning a dict with keys `module_ident: str`, `module_candidate_triples: List[str]`, `function_entries: List[Tuple[str, str, List[str]]]` (fn_ident, fn_name, candidate_triples), `class_entries: List[Tuple[str, str, List[str]]]` (cls_ident, cls_name, candidate_triples), `resolved_imports: List[Tuple[str, str, bool]]` (import_name, dep_ident, is_resolved). Task 3's updated `_build_code_triples` and Task 4's updated `_extract_commit` consume this shape.

- [ ] **Step 1: Write the failing tests**

```python
class TestPrecomputeFileTriples:
    def test_module_candidate_triples_include_introduced_by(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "auth.py",
            {"functions": [], "classes": [], "imports": []},
            ":commit/abc123456789",
            {},
        )
        module_ident = mcp_server._code_ident("module", "auth.py")
        assert result["module_ident"] == module_ident
        assert any(
            f"[{module_ident} :introduced-by :commit/abc123456789]" in t
            for t in result["module_candidate_triples"]
        )

    def test_function_entries_carry_ident_name_and_candidate_triples(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "auth.py",
            {"functions": ["login"], "classes": [], "imports": []},
            ":commit/abc123456789",
            {},
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert len(result["function_entries"]) == 1
        ident, name, triples = result["function_entries"][0]
        assert ident == fn_ident
        assert name == "login"
        assert any(f'[{fn_ident} :description "login"]' in t for t in triples)
        assert any(f"[{fn_ident} :introduced-by :commit/abc123456789]" in t for t in triples)

    def test_class_entries_carry_ident_name_and_candidate_triples(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "auth.py",
            {"functions": [], "classes": ["User"], "imports": []},
            ":commit/abc123456789",
            {},
        )
        cls_ident = mcp_server._code_ident("class", "auth.py", "User")
        assert len(result["class_entries"]) == 1
        ident, name, triples = result["class_entries"][0]
        assert ident == cls_ident
        assert name == "User"
        assert any(f'[{cls_ident} :description "User"]' in t for t in triples)

    def test_resolved_imports_use_known_files_not_file_entities(self):
        import mcp_server
        known_files = {"mod_b.py": []}
        result = mcp_server._precompute_file_triples(
            "mod_a.py",
            {"functions": [], "classes": [], "imports": ["mod_b"]},
            ":commit/abc123456789",
            known_files,
        )
        assert len(result["resolved_imports"]) == 1
        import_name, dep_ident, is_resolved = result["resolved_imports"][0]
        assert import_name == "mod_b"
        assert is_resolved is True
        assert dep_ident == mcp_server._code_ident("module", "mod_b.py")

    def test_unresolved_import_flagged_false(self):
        import mcp_server
        result = mcp_server._precompute_file_triples(
            "main.rs",
            {"functions": [], "classes": [], "imports": ["totally_unknown_crate"]},
            ":commit/abc123456789",
            {},
        )
        import_name, dep_ident, is_resolved = result["resolved_imports"][0]
        assert is_resolved is False
        assert dep_ident == mcp_server._canonical_ident("module", "totally_unknown_crate")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriples -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_precompute_file_triples'`

- [ ] **Step 3: Implement `_precompute_file_triples`**

Add to `mcp_server.py` directly before `_build_code_triples` (before line 2465):

```python
def _precompute_file_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ident: str,
    known_files: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Pure, per-commit-independent precomputation for _build_code_triples.

    Runs inside _extract_commit on the worker pool. Computes everything that does
    NOT depend on the serially-maintained entity_valid_from/file_deps state:
      - the candidate triple strings for the module/function/class idents this file
        would introduce, ready to use verbatim IF the main thread's diff against
        entity_valid_from decides the ident is genuinely new (see _build_code_triples);
      - the resolved dependency ident for every import in the file, via
        _resolve_module_import against known_files (this commit's own git-ls-tree
        state, not the incrementally-mutated file_entities).

    known_files must come from _known_files_at_commit for the SAME commit_hash this
    file was extracted from — it determines what "is_resolved" means here.
    """
    module_ident = _code_ident("module", file_path)
    module_candidate_triples = [
        f"[{module_ident} :entity-type :type/module]",
        f'[{module_ident} :ident "{module_ident}"]',
        f'[{module_ident} :description "{_edn_escape(file_path)}"]',
        f'[{module_ident} :path "{_edn_escape(file_path)}"]',
        f"[{module_ident} :introduced-by {commit_ident}]",
    ]

    function_entries: List[Tuple[str, str, List[str]]] = []
    for fn_name in extracted.get("functions", []):
        fn_ident = _code_ident("function", file_path, fn_name)
        function_entries.append((fn_ident, fn_name, [
            f"[{fn_ident} :entity-type :type/function]",
            f'[{fn_ident} :ident "{fn_ident}"]',
            f'[{fn_ident} :description "{_edn_escape(fn_name)}"]',
            f'[{fn_ident} :file "{_edn_escape(file_path)}"]',
            f"[{module_ident} :contains {fn_ident}]",
            f"[{fn_ident} :introduced-by {commit_ident}]",
        ]))

    class_entries: List[Tuple[str, str, List[str]]] = []
    for cls_name in extracted.get("classes", []):
        cls_ident = _code_ident("class", file_path, cls_name)
        class_entries.append((cls_ident, cls_name, [
            f"[{cls_ident} :entity-type :type/class]",
            f'[{cls_ident} :ident "{cls_ident}"]',
            f'[{cls_ident} :description "{_edn_escape(cls_name)}"]',
            f'[{cls_ident} :file "{_edn_escape(file_path)}"]',
            f"[{module_ident} :contains {cls_ident}]",
            f"[{cls_ident} :introduced-by {commit_ident}]",
        ]))

    resolved_imports: List[Tuple[str, str, bool]] = []
    for import_name in set(extracted.get("imports", [])):
        dep_ident, is_resolved = _resolve_module_import(
            import_name, known_files, importing_file=file_path,
        )
        resolved_imports.append((import_name, dep_ident, is_resolved))

    return {
        "module_ident": module_ident,
        "module_candidate_triples": module_candidate_triples,
        "function_entries": function_entries,
        "class_entries": class_entries,
        "resolved_imports": resolved_imports,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriples -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _precompute_file_triples pure precomputation for worker-pool use"
```

---

### Task 3: Thread precomputed data through `_build_code_triples`

**Files:**
- Modify: `mcp_server.py:2465-2550` (`_build_code_triples`)
- Modify: `tests/test_mcp_server.py:2144-2204` — the three existing direct-call tests (`test_build_code_triples_writes_modified_in_for_preexisting_functions`, `test_build_code_triples_does_not_write_modified_in_for_new_functions`, `test_build_code_triples_populates_entity_descriptions`)

**Interfaces:**
- Consumes: `_precompute_file_triples(...)` from Task 2, to build the `precomputed` argument in tests.
- Produces: `_build_code_triples(file_path, extracted, commit_ts_iso, entity_valid_from, entity_descriptions, file_entities, commit_ident, precomputed) -> List[str]` — same return type as before, one new required positional parameter `precomputed`. Task 5's updated `_run_ingestion` call site passes the `precomputed` dict `_extract_commit` now returns per file (Task 4).

- [ ] **Step 1: Update the three existing tests to the new required signature (will fail until Step 3)**

In `tests/test_mcp_server.py`, replace `test_build_code_triples_writes_modified_in_for_preexisting_functions`:

```python
    def test_build_code_triples_writes_modified_in_for_preexisting_functions(self):
        import mcp_server
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        cls_ident = mcp_server._code_ident("class", "auth.py", "User")
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {
            module_ident: "2025-01-01T00:00:00Z",
            fn_ident: "2025-01-01T00:00:00Z",
            cls_ident: "2025-01-01T00:00:00Z",
        }
        commit_ident = ":commit/deadbeef12345678"
        extracted = {"functions": ["login"], "classes": ["User"], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        triples = mcp_server._build_code_triples(
            "auth.py",
            extracted,
            "2025-02-01T00:00:00Z",
            entity_valid_from,
            {},
            {},
            commit_ident,
            precomputed,
        )
        assert any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)
        assert any(f"[{cls_ident} :modified-in {commit_ident}]" in t for t in triples)
```

Replace `test_build_code_triples_does_not_write_modified_in_for_new_functions`:

```python
    def test_build_code_triples_does_not_write_modified_in_for_new_functions(self):
        import mcp_server
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {module_ident: "2025-01-01T00:00:00Z"}
        commit_ident = ":commit/deadbeef12345678"
        extracted = {"functions": ["new_func"], "classes": [], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        triples = mcp_server._build_code_triples(
            "auth.py",
            extracted,
            "2025-02-01T00:00:00Z",
            entity_valid_from,
            {},
            {},
            commit_ident,
            precomputed,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "new_func")
        assert not any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)
        assert any(f"[{fn_ident} :introduced-by {commit_ident}]" in t for t in triples)
```

Replace `test_build_code_triples_populates_entity_descriptions`:

```python
    def test_build_code_triples_populates_entity_descriptions(self):
        import mcp_server
        entity_valid_from: dict = {}
        entity_descriptions: dict = {}
        file_entities: dict = {}
        commit_ident = ":commit/abc123456789"
        extracted = {"functions": ["login"], "classes": ["User"], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        mcp_server._build_code_triples(
            "auth.py",
            extracted,
            "2025-01-01T00:00:00Z",
            entity_valid_from,
            entity_descriptions,
            file_entities,
            commit_ident,
            precomputed,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        cls_ident = mcp_server._code_ident("class", "auth.py", "User")
        module_ident = mcp_server._code_ident("module", "auth.py")
        assert entity_descriptions.get(fn_ident) == "login"
        assert entity_descriptions.get(cls_ident) == "User"
        assert entity_descriptions.get(module_ident) == "auth.py"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestIngestionWrites -v -k build_code_triples`
Expected: FAIL — `_build_code_triples() missing 1 required positional argument: 'precomputed'`

- [ ] **Step 3: Update `_build_code_triples`**

Replace mcp_server.py:2465-2550 entirely with:

```python
def _build_code_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ts_iso: str,
    entity_valid_from: Dict[str, str],
    entity_descriptions: Dict[str, str],
    file_entities: Dict[str, List[str]],
    commit_ident: str,
    precomputed: Dict[str, Any],
) -> List[str]:
    """Return Datalog triple strings for a file's extracted code entities.

    Stable attributes (:entity-type, :ident, :description, :path/:file,
    :introduced-by, :contains) are written ONCE on first introduction. On
    subsequent modifications only a :modified-in edge is added. This prevents
    bi-temporal fact explosion from N re-assertions of the same attribute
    joining into N² result rows.

    precomputed comes from _precompute_file_triples (see mcp_server.py),
    computed ahead of time in the extraction worker pool — the candidate
    triple strings for a would-be-new entity are a pure function of the
    file's own extracted structure and ident, independent of whether
    entity_valid_from turns out to already know about it. This function's
    only remaining job is the diff against entity_valid_from itself, which
    genuinely needs the serially-maintained state.

    :depends-on edges are written in the commit loop by _run_ingestion as the
    file's imports change, giving them proper bi-temporal bounds.
    """
    triples: List[str] = []
    module_ident = precomputed["module_ident"]

    is_new_module = module_ident not in entity_valid_from
    # Track all idents for this file (for deletion cleanup)
    idents_for_file = file_entities.setdefault(file_path, [])

    if is_new_module:
        triples += precomputed["module_candidate_triples"]
        if module_ident not in idents_for_file:
            idents_for_file.append(module_ident)
        entity_valid_from[module_ident] = commit_ts_iso
        entity_descriptions[module_ident] = file_path
    else:
        # Existing module: only record that this commit modified it
        triples.append(f"[{module_ident} :modified-in {commit_ident}]")

    for fn_ident, fn_name, candidate_triples in precomputed["function_entries"]:
        if fn_ident not in entity_valid_from:
            triples += candidate_triples
            if fn_ident not in idents_for_file:
                idents_for_file.append(fn_ident)
            entity_valid_from[fn_ident] = commit_ts_iso
            entity_descriptions[fn_ident] = fn_name
        else:
            # Pre-existing function: record that this commit modified it
            triples.append(f"[{fn_ident} :modified-in {commit_ident}]")

    for cls_ident, cls_name, candidate_triples in precomputed["class_entries"]:
        if cls_ident not in entity_valid_from:
            triples += candidate_triples
            if cls_ident not in idents_for_file:
                idents_for_file.append(cls_ident)
            entity_valid_from[cls_ident] = commit_ts_iso
            entity_descriptions[cls_ident] = cls_name
        else:
            # Pre-existing class: record that this commit modified it
            triples.append(f"[{cls_ident} :modified-in {commit_ident}]")

    return triples
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestIngestionWrites -v`
Expected: PASS (all tests in this class, including the 3 updated ones)

Note: `_run_ingestion`'s own call to `_build_code_triples` (mcp_server.py:2908-2911) is NOT updated yet — that happens in Task 5. Do not run the full suite yet; `TestRunIngestion*` classes will fail until Task 5 lands. That's expected mid-plan state.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: thread precomputed candidate triples through _build_code_triples"
```

---

### Task 4: Enrich `_extract_commit`'s per-file results with precomputed data

**Files:**
- Modify: `mcp_server.py:2743-2789` (`_extract_commit`)

**Interfaces:**
- Consumes: `_known_files_at_commit` (Task 1), `_precompute_file_triples` (Task 2).
- Produces: `_extract_commit(repo_path, commit_hash) -> Tuple[List[tuple], List[tuple], Dict[str, Dict[str, str]]]` where each entry in the first list is now a 4-tuple `(status, file_path, extracted, precomputed)` — `extracted` and `precomputed` are both `None` for a "D" (deleted) file. Task 5's updated `_run_ingestion` consumes this new 4-tuple shape.

There is no existing direct unit test of `_extract_commit`'s return shape (it's only exercised indirectly through `_run_ingestion` in the `TestRunIngestion*` classes), so this task has no new tests of its own — Task 5's updated `TestRunIngestion*` suite is the verification for this change. This task and Task 5 must land together before running the full suite.

- [ ] **Step 1: Replace `_extract_commit`**

Replace mcp_server.py:2743-2789 entirely with:

```python
def _extract_commit(
    repo_path: str, commit_hash: str
) -> Tuple[List[tuple], List[tuple], Dict[str, Dict[str, str]]]:
    """Read-only, stateless per-commit extraction: diff-tree + git-show + tree-sitter parse,
    plus import resolution and "if this turns out to be new" triple precomputation —
    both pure functions of this commit alone (see _known_files_at_commit and
    _precompute_file_triples), unlike the incrementally-mutated file_entities/
    entity_valid_from state only the serial main thread maintains.

    Runs in a worker thread via the ThreadPoolExecutor in _run_ingestion. Touches no
    shared mutable state and no DB. Returns (file_results, gitlink_changes, gitmodules_map):

      file_results: one entry per changed file that has a supported parser, as
        (status, file_path, extracted, precomputed). A/M files whose content fetch
        fails are omitted entirely, mirroring the previous inline `continue` — same
        as before this pipeline existed. For a "D" (deleted) file, extracted and
        precomputed are both None — the main thread only needs file_path to know
        what to close.
      gitlink_changes: _gitlink_changes' output — gitlink-involving rows, never fed
        through the tree-sitter parser (gitlink paths never have a resolvable extension).
      gitmodules_map: path -> {"name", "url"}, populated only when this commit has at
        least one gitlink "add" — avoids a wasted git-show call on the (overwhelmingly
        common) case of a commit that touches no submodules at all.

    Sources both file_results and gitlink_changes from a single
    `git diff-tree --raw` call (via _git_diff_tree_raw) rather than a --name-status
    call, which discarded file mode entirely.

    known_files (via _known_files_at_commit) is computed lazily, once per commit,
    and shared across every A/M file in this commit — a commit with only deletions
    never pays for it.
    """
    raw_entries = _git_diff_tree_raw(repo_path, commit_hash)
    commit_ident = f":commit/{commit_hash[:12]}"
    results: List[tuple] = []
    known_files: Optional[Dict[str, List[str]]] = None

    for status, old_mode, new_mode, old_sha, new_sha, file_path in raw_entries:
        parser = _thread_parser(file_path)
        if parser is None:
            continue
        if status == "D":
            results.append((status, file_path, None, None))
            continue
        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue
        extracted = _extract_from_source(content, parser, file_path)
        if known_files is None:
            known_files = _known_files_at_commit(repo_path, commit_hash)
        precomputed = _precompute_file_triples(file_path, extracted, commit_ident, known_files)
        results.append((status, file_path, extracted, precomputed))

    gitlink_changes = _gitlink_changes(raw_entries)
    gitmodules_map: Dict[str, Dict[str, str]] = {}
    if any(kind == "add" for kind, _, _ in gitlink_changes):
        gitmodules_map = _git_gitmodules_at(repo_path, commit_hash)

    return results, gitlink_changes, gitmodules_map
```

- [ ] **Step 2: Commit**

This step intentionally leaves the suite red — `_run_ingestion` still unpacks the old 3-tuple shape at this point. Commit anyway so Task 5 is a clean, reviewable diff on top; do not run the full suite until Task 5's Step 4.

```bash
git add mcp_server.py
git commit -m "refactor: enrich _extract_commit results with precomputed import resolution and candidate triples"
```

---

### Task 5: Update `_run_ingestion`'s consumption + regression test for per-commit-accurate resolution

**Files:**
- Modify: `mcp_server.py:2888-2959` (the per-file A/M/D branch inside `_run_ingestion`'s commit loop)
- Test: `tests/test_mcp_server.py` — add new fixture `git_repo_with_future_dep` and `TestPerCommitAccurateImportResolution` class directly after `TestResolveModuleImportTieredMatcher` (find its end — search for the class after it, or append after the last test in that class if it's the last class in the file).

**Interfaces:**
- Consumes: `_build_code_triples(..., precomputed)` (Task 3), the 4-tuple shape from `_extract_commit` (Task 4).

- [ ] **Step 1: Update the per-file loop in `_run_ingestion`**

Replace mcp_server.py:2888-2959 (the `for status, file_path, extracted in extracted_files:` loop body) with:

```python
                    for status, file_path, extracted, precomputed in extracted_files:
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
                                entity_descriptions, file_entities, commit_ident, precomputed,
                            )
                            add_triples.extend(triples)
                            # Detect entities removed from a modified file.
                            # _build_code_triples only appends to file_entities, never removes.
                            # Compare previous idents against the idents derivable from the
                            # current extraction to find what was deleted.
                            if status == "M":
                                module_ident = _code_ident("module", file_path)
                                current_extracted_idents: set = {module_ident}
                                for fn_ident, _fn_name, _fn_triples in precomputed["function_entries"]:
                                    current_extracted_idents.add(fn_ident)
                                for cls_ident, _cls_name, _cls_triples in precomputed["class_entries"]:
                                    current_extracted_idents.add(cls_ident)
                                removed_idents = previous_idents - current_extracted_idents
                                for ident in removed_idents:
                                    orig_ts = entity_valid_from.get(ident, commit_ts_iso)
                                    desc = entity_descriptions.get(ident, "")
                                    close_items.append(
                                        (_build_close_triples(ident, desc, module_ident), orig_ts)
                                    )
                            # Compute dep edges for this file and diff against previous.
                            # Resolution itself already happened in _extract_commit
                            # (precomputed["resolved_imports"]) against that commit's
                            # own git-ls-tree state — nothing left to resolve here.
                            module_ident = _code_ident("module", file_path)
                            current_deps: set = set()
                            for import_name, dep_ident, is_resolved in precomputed["resolved_imports"]:
                                if dep_ident != module_ident:
                                    current_deps.add(dep_ident)
                                    is_relative = import_name.startswith(".")
                                    if not is_resolved and not is_relative and dep_ident not in entity_valid_from:
                                        add_triples.extend([
                                            f"[{dep_ident} :entity-type :type/external-dependency]",
                                            f'[{dep_ident} :ident "{_edn_escape(dep_ident)}"]',
                                            f'[{dep_ident} :description "{_edn_escape(import_name)}"]',
                                        ])
                                        entity_valid_from[dep_ident] = commit_ts_iso
                                        entity_descriptions[dep_ident] = import_name
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
```

- [ ] **Step 2: Run the full pre-existing suite to verify Tasks 3-5 land correctly together**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py -v`
Expected: PASS — every test, including `TestRunIngestion*`, `TestRunIngestionConcurrency::test_concurrent_run_matches_sequential_facts`, `TestRunIngestionBitemporalDeps`, `TestUnresolvedImportTagging`, `TestResolveModuleImportTieredMatcher`.

If anything fails here, the bug is in Task 5's loop body (or a leftover from Task 3/4), not in the tests — do not weaken assertions to make it pass.

- [ ] **Step 3: Add the regression test for per-commit-accurate resolution**

This is the behavior change the issue calls out explicitly ("Correctness wrinkle to watch"): resolving an import must reflect the specific commit's own tree, not the incrementally-built `file_entities` (which is pre-seeded from `git ls-files` at HEAD and could previously resolve an import against a file that doesn't exist yet at the commit being processed).

Add to `tests/test_mcp_server.py`, directly after the `TestResolveModuleImportTieredMatcher` class (find where it ends — it's the last class analyzed in exploration; append after its last method if no class follows, otherwise insert right after its closing test):

```python
@pytest.fixture
def git_repo_with_future_dep(tmp_path):
    """commit 1: mod_a.py imports mod_b, which does not exist yet.
    commit 2: mod_b.py is added.
    Resolving mod_a's import while processing commit 1 must reflect commit 1's
    OWN tree (mod_b.py doesn't exist there yet) — not HEAD's tree, where it does."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    (repo / "mod_a.py").write_text("import mod_b\n\ndef main(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add mod_a importing not-yet-existing mod_b"], cwd=repo, check=True, capture_output=True)

    (repo / "mod_b.py").write_text("def helper(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add mod_b"], cwd=repo, check=True, capture_output=True)

    return repo


class TestPerCommitAccurateImportResolution:
    @pytest.mark.asyncio
    async def test_import_of_not_yet_existing_file_tagged_external_at_introduction(
        self, mock_minigraf_db, git_repo_with_future_dep, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_future_dep / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None,
        }

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(git_repo_with_future_dep), "HEAD")

        mod_b_external_ident = mcp_server._canonical_ident("module", "mod_b")
        assert any(
            f"[{mod_b_external_ident} :entity-type :type/external-dependency]" in t
            for t in transact_calls
        ), (
            "mod_b.py did not exist yet at commit 1, so resolving mod_a's import "
            "must use commit 1's own tree and tag it external — not silently "
            "resolve against HEAD's tree, where mod_b.py exists by the end."
        )
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py::TestPerCommitAccurateImportResolution -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "refactor: consume precomputed resolution/triples in _run_ingestion; add per-commit accuracy regression test"
```

---

### Task 6: Full suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the complete test suite**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest tests/test_mcp_server.py -v`
Expected: PASS, all tests, zero failures/errors.

- [ ] **Step 2: Run the complete repo test suite (in case other test files import mcp_server)**

Run: `cd /home/aditya/Work/AMC/Minigraf/temporal_reasoning && python -m pytest -v`
Expected: PASS, all tests, zero failures/errors.

- [ ] **Step 3: Manually sanity-check the profiling claim (optional, not automatable)**

If a large real repo (e.g. arangodb, per the issue's evidence) is available locally, re-run the profiling command from the issue during an ingestion run to confirm worker threads are no longer idle at ~0% CPU:

```bash
ps -T -p <ingestion_pid> -o pid,tid,pcpu,stat,comm --sort=-pcpu
```

This step is informational only — do not block the plan on having arangodb checked out locally; the correctness test suite (Steps 1-2) is the actual gate.

## Self-Review Notes

- Spec coverage: worker-side import resolution (Tasks 1-2, 4), worker-side triple-string precomputation (Task 2, 4), main-thread-only diff logic preserved (Task 3), correctness wrinkle around extension filtering preserved (`_known_files_at_commit` filters via `_EXT_TO_LANG`, same as `_preload_known_entities`'s existing pre-seed), external-dependency fallback preserved verbatim (Task 5's loop body keeps the exact same `is_resolved`/`is_relative`/`entity_valid_from` conditions, just reads `dep_ident`/`is_resolved` from `precomputed["resolved_imports"]` instead of calling `_resolve_module_import` inline).
- No placeholders: every step has complete, runnable code.
- Type consistency: `precomputed` dict shape (`module_ident`, `module_candidate_triples`, `function_entries`, `class_entries`, `resolved_imports`) is identical across Task 2's producer, Task 3's consumer, and Task 4/5's plumbing.
