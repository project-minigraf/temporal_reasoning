# Git Ingestion Rename/Move Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make git ingestion track file/function/class/global/field renames and moves as continuous history instead of fracturing them into disconnected entities, and add global/field/static extraction (16 languages) as the entity types renames need to attach to.

**Architecture:** Four components sharing a single `:renamed-from`/`:renamed-to` schema addition: (1) file-level rename detection via git's own `-M`, (2) a custom AST-lockstep matcher with local-variable bijection for function/class/global/field renames (below git's diff granularity), (3) new `:type/variable`/`:type/field` entity extraction across 16 languages via a new scope-aware traversal (not the existing naive full-tree walker), (4) rename tracking for the new entity types, reusing component 2's matcher.

**Tech Stack:** Python 3.10+, tree-sitter (16 grammar packages, already installed in `.venv`), pytest/pytest-asyncio, minigraf (Rust-backed Datalog graph store via `mcp_server.py`'s `MiniGrafDb` wrapper).

## Global Constraints

- All new code lives in `mcp_server.py` — this codebase keeps everything in one file by established precedent (confirmed via the #115 path-ignore feature, which added its new functions directly into `mcp_server.py` rather than a new module). Do not create new files for source code.
- Tests live in `tests/test_mcp_server.py` — same single-file precedent.
- No new third-party dependencies. Everything here is buildable with the stdlib + already-installed `tree-sitter` + already-installed per-language grammar packages (`pyproject.toml`'s `git-ingestion` extra).
- `minigraf`'s Datalog grammar has real boolean literals (`true`/`false`, unquoted) — confirmed via `minigraf/src/query/datalog/parser.rs:209-210,402-404`. `:static` must be declared `bool` in `MINIGRAF_SCHEMA` and written as a bare literal (e.g. `f"[{ident} :static {'true' if is_static else 'false'}]"`), matching the existing bare-int pattern already used for `:total-ingested` at `mcp_server.py:1886`. Do not quote it as a string.
- `_extract_commit` (mcp_server.py:3115-3191) runs in a `ProcessPoolExecutor` worker (spawn context, see #116) — anything it returns must be plain, picklable data (strings, ints, bools, lists, dicts, tuples). Never return a `tree_sitter.Node`/`Tree` object across that boundary.
- Every new git subprocess call must follow the existing pattern: `_subprocess.run([...], cwd=repo_path, capture_output=True, ...)`, matching every existing helper (`_git_commits`, `_git_diff_tree_raw`, `_git_file_content`, etc.).
- Follow TDD: write the failing test first, run it, confirm the failure reason, then implement, then confirm green. Commit after each task.
- Run the full suite (`pytest tests/test_mcp_server.py -q`) at the end of every phase (not just every task) to catch cross-task regressions early — the phases are listed in the task table below.

## File structure

Everything is additive or modifies existing functions/tests in-place:

| File | Role |
|---|---|
| `mcp_server.py` | All new functions, schema entries, and modifications to existing pipeline functions |
| `tests/test_mcp_server.py` | All new/modified tests |
| `SKILL.md` | Docs for the new entity types and `:renamed-from`/`:renamed-to` attributes |

## Task Overview (execute in this order — later tasks depend on earlier ones)

| # | Task | Phase |
|---|---|---|
| 1 | Schema: `:renamed-from`/`:renamed-to` + `:type/variable`/`:type/field` | Foundations |
| 2 | `_git_blob_content` helper | Foundations |
| 3 | `-M` flag + raw-diff parser fix (7-tuple with `old_path`) | Component 1 |
| 4 | `_extract_commit` R-status branch | Component 1 |
| 5 | Module-level rename triples in `_run_ingestion` + flip existing test | Component 1 |
| 6 | Capture function/class body text in extraction | Component 2 |
| 7 | AST-lockstep bijective matcher (single-pair core) | Component 2 |
| 8 | Round-based match orchestration | Component 2 |
| 9 | Wire matcher into `_extract_commit` (fetch old blobs, build pools) | Component 2 |
| 10 | Consume confirmed pairs in `_run_ingestion`, emit rename triples | Component 2 |
| 11 | Generic `:type/variable`/`:type/field` plumbing (language-agnostic) | Component 3 |
| 12 | Scope-aware traversal skeleton + per-language dispatch table | Component 3 |
| 13 | Python globals/fields | Component 3 |
| 14 | JavaScript + TypeScript globals/fields | Component 3 |
| 15 | Rust + Go + C globals/fields | Component 3 |
| 16 | Java + C# globals/fields | Component 3 |
| 17 | C++ globals/fields | Component 3 |
| 18 | Ruby globals/fields | Component 3 |
| 19 | PHP globals/fields | Component 3 |
| 20 | Kotlin globals/fields | Component 3 |
| 21 | Swift globals/fields | Component 3 |
| 22 | Scala globals/fields | Component 3 |
| 23 | Haskell globals/fields | Component 3 |
| 24 | Lua globals (no fields) | Component 3 |
| 25 | Elixir fields (no globals) | Component 3 |
| 26 | Extend matcher pools to globals/fields | Component 4 |
| 27 | Wire global/field rename triples | Component 4 |
| 28 | SKILL.md docs | Wrap-up |
| 29 | Full suite regression + PR | Wrap-up |

---

## Task 1: Schema — `:renamed-from`/`:renamed-to` and new entity types

**Files:**
- Modify: `mcp_server.py:1894-1947` (`MINIGRAF_SCHEMA`)
- Test: `tests/test_mcp_server.py` (new test near existing schema tests — search `MINIGRAF_SCHEMA` in the test file to find the right neighborhood)

**Interfaces:**
- Produces: `MINIGRAF_SCHEMA["variable"]`, `MINIGRAF_SCHEMA["field"]`, and `:renamed-from`/`:renamed-to` optional keys on `"module"`, `"function"`, `"class"`, `"variable"`, `"field"`. Every later task that writes a rename triple relies on these existing.

- [ ] **Step 1: Write the failing test**

```python
def test_schema_has_renamed_from_and_to_on_code_entities():
    import mcp_server
    for entity_type in ("module", "function", "class", "variable", "field"):
        optional = mcp_server.MINIGRAF_SCHEMA[entity_type]["optional"]
        assert optional[":renamed-from"] is str
        assert optional[":renamed-to"] is str

def test_schema_has_variable_and_field_types():
    import mcp_server
    assert mcp_server.MINIGRAF_SCHEMA["variable"]["required"][":description"] is str
    field_optional = mcp_server.MINIGRAF_SCHEMA["field"]["optional"]
    assert field_optional[":static"] is bool
    assert field_optional[":class"] is str
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "schema_has_renamed_from or schema_has_variable_and_field" -v`
Expected: FAIL with `KeyError: 'variable'` (or similar — the type doesn't exist yet).

- [ ] **Step 3: Implement**

Replace `mcp_server.py:1911-1934` (the `module`/`function`/`class` blocks) and insert two new blocks after `class`, before `ingestion`:

```python
    "module": {
        "required": {":description": str},
        "optional": {
            ":path": str, ":alias": str,
            # graph edges (keyword-valued, stored as strings)
            ":contains": str, ":depends-on": str, ":calls": str,
            # commit cross-references
            ":introduced-by": str, ":modified-in": str,
            # rename/move continuity (see 2026-07-14 rename-tracking design doc)
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "function": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "class": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "variable": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
    "field": {
        "required": {":description": str},
        "optional": {
            ":file": str, ":alias": str, ":class": str, ":static": bool,
            ":introduced-by": str, ":modified-in": str,
            ":renamed-from": str, ":renamed-to": str,
        },
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "schema_has_renamed_from or schema_has_variable_and_field" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add :type/variable, :type/field, and rename-tracking schema attrs"
```

---

## Task 2: `_git_blob_content` helper

**Files:**
- Modify: `mcp_server.py` (add near `_git_file_content`, line 1577-1583)
- Test: `tests/test_mcp_server.py` (near existing `_git_file_content` tests — grep `_git_file_content` in the test file)

**Interfaces:**
- Produces: `_git_blob_content(repo_path: str, blob_sha: str) -> bytes`. Used by Task 9 to fetch a file's *old* content directly by blob SHA (already returned by `_git_diff_tree_raw` as `old_sha`), without needing to know the parent commit hash.

- [ ] **Step 1: Write the failing test**

```python
def test_git_blob_content_returns_raw_bytes(git_repo):
    import mcp_server
    commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
    entries = mcp_server._git_diff_tree_raw(str(git_repo), commits[0][0])
    _, _, _, _, new_sha, _ = entries[0][:6]
    content = mcp_server._git_blob_content(str(git_repo), new_sha)
    assert b"def login" in content
```

(Reuses the existing `git_repo` fixture, defined earlier in the file — grep `def git_repo(` to confirm its exact contents; it's the repo with `auth.py` containing `def login()`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_git_blob_content_returns_raw_bytes -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_git_blob_content'`

- [ ] **Step 3: Implement**

Insert directly after `_git_file_content` (mcp_server.py:1577-1583):

```python
def _git_blob_content(repo_path: str, blob_sha: str) -> bytes:
    """Return raw bytes of a blob by its own SHA, independent of any commit/path.

    Used to fetch a file's *old* content directly from _git_diff_tree_raw's
    old_sha field (a plain blob SHA) when comparing pre/post rename or
    modification content — cheaper than resolving a parent commit hash and
    re-deriving the old path, and correct even when the old path no longer
    exists at any reachable commit-ish (e.g. mid-history rewrites).
    """
    result = _subprocess.run(
        ["git", "cat-file", "blob", blob_sha],
        cwd=repo_path, capture_output=True, check=True,
    )
    return result.stdout
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_git_blob_content_returns_raw_bytes -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _git_blob_content helper for direct blob-SHA fetches"
```

---

## Task 3: Enable `-M` and fix two-path raw-diff parsing

**Files:**
- Modify: `mcp_server.py:1489-1513` (`_git_diff_tree_raw`)
- Modify: `mcp_server.py:1519-1548` (`_gitlink_changes` — unpack gains a 7th field)
- Modify: `mcp_server.py:3164` (`_extract_commit`'s loop — unpack gains a 7th field; behavior change is Task 4)
- Test: `tests/test_mcp_server.py:2210-2253` (`TestGitDiffTreeRaw` — existing 2 tests need the unpack updated; new rename tests added)

**Interfaces:**
- Produces: `_git_diff_tree_raw` now returns 7-tuples `(status, old_mode, new_mode, old_sha, new_sha, path, old_path)`. `old_path` is `""` for every status except `"R"`, where it holds the pre-rename path and `path` holds the post-rename path. `status` for a rename is still just `"R"` (the numeric similarity suffix, e.g. `R100`/`R057`, is dropped the same way it always was for other statuses — Task 4 needs the exact similarity score, so also return it: extend to an 8-tuple with `similarity: Optional[int]`, `None` for non-rename statuses).
- Consumes (must update): `_gitlink_changes`, `_extract_commit`'s per-file loop.

- [ ] **Step 1: Write the failing tests**

Add to `TestGitDiffTreeRaw` (mcp_server.py:2210), and update the two existing tests' unpacking:

```python
    def test_regular_file_add(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(git_repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "A"
        assert old_mode == "000000"
        assert new_mode == "100644"
        assert path == "auth.py"
        assert old_path == ""
        assert similarity is None

    def test_gitlink_add_reports_mode_160000(self, tmp_path):
        # ... (unchanged body up to the assertions) ...
        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "A"
        assert new_mode == "160000"
        assert new_sha == sub_hash
        assert path == "vendor/lib"
        assert old_path == ""

    def test_pure_rename_reports_both_paths_and_similarity(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text("def login():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[1][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "R"
        assert path == "new_name.py"
        assert old_path == "old_name.py"
        assert similarity == 100

    def test_rename_with_content_change_reports_partial_similarity(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text(
            "def login():\n    pass\n\ndef a():\n    pass\n\ndef b():\n    pass\n\ndef c():\n    pass\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, capture_output=True)
        (repo / "new_name.py").write_text(
            "def login():\n    pass\n\ndef a():\n    pass\n\ndef extra():\n    pass\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename and edit"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[1][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity = entries[0]
        assert status == "R"
        assert old_path == "old_name.py"
        assert path == "new_name.py"
        assert similarity is not None and 0 < similarity < 100

    def test_unrelated_add_and_delete_not_reported_as_rename(self, tmp_path):
        """Below git's default 50% similarity threshold, -M must NOT report a rename."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text("def login():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "rm", "old_name.py"], cwd=repo, check=True, capture_output=True)
        (repo / "unrelated.py").write_text("class Widget:\n    def render(self):\n        return 42\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "unrelated churn"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[1][0])
        statuses = {e[0] for e in entries}
        assert statuses == {"A", "D"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestGitDiffTreeRaw -v`
Expected: FAIL — `test_regular_file_add`/`test_gitlink_add_reports_mode_160000` fail with `ValueError: not enough values to unpack` (currently 6-tuples); the new rename tests fail with the same or `AssertionError` since `-M` isn't enabled yet (renames currently show as separate `A`/`D`).

- [ ] **Step 3: Implement**

Replace `_git_diff_tree_raw` (mcp_server.py:1489-1513):

```python
def _git_diff_tree_raw(repo_path: str, commit_hash: str) -> List[tuple]:
    """Return (status_char, old_mode, new_mode, old_sha, new_sha, path, old_path,
    similarity) for every changed path in a commit, via a single
    `git diff-tree --raw` call.

    -M enables git's own content-similarity rename detection (default 50%
    threshold, unchanged — see the 2026-07-14 rename-tracking design doc's
    "Component 1" for why no additional threshold filtering is applied on
    top of git's own judgment). Deliberately no -C (copy detection) — a copy
    leaves the original in place *and* creates a new, independent entity;
    treating it as a rename would misrepresent history.

    A rename/copy raw line has TWO tab-separated paths (old, then new), not
    one, e.g. ":100644 100644 <sha> <sha> R100\told.py\tnew.py" — naively
    keeping the old single-partition parse would fold both paths into one
    bogus string. old_path is "" for every non-rename status. similarity is
    the numeric suffix of the status (e.g. 100 for "R100", 57 for "R057"),
    None for non-rename statuses.

    Supersedes running diff-tree a second time just to detect gitlinks:
    --raw already carries file mode (needed to spot submodule paths, mode
    160000) in the same subprocess invocation _extract_commit already makes.
    """
    result = _subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "-M", "--raw", "--root", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        if not line.startswith(":"):
            continue
        meta, sep, rest = line.partition("\t")
        if not sep:
            continue
        fields = meta[1:].split(" ")
        if len(fields) < 5:
            continue
        old_mode, new_mode, old_sha, new_sha, status_field = fields[0], fields[1], fields[2], fields[3], fields[4]
        status = status_field[0]
        similarity = int(status_field[1:]) if len(status_field) > 1 and status_field[1:].isdigit() else None
        if status in ("R", "C"):
            old_path, _, path = rest.partition("\t")
        else:
            old_path, path = "", rest
        entries.append((status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity))
    return entries
```

Update `_gitlink_changes` (mcp_server.py:1519-1548) — only the loop header changes, body is untouched (gitlinks are never renamed via this path in practice; `old_path`/`similarity` are simply unused here):

```python
    for status, old_mode, new_mode, old_sha, new_sha, path, old_path, similarity in raw_entries:
```

Update `_extract_commit`'s loop header (mcp_server.py:3164) the same way — the body's behavior change (handling `status == "R"`) is Task 4, this step only fixes the unpack so the function doesn't crash:

```python
    for status, old_mode, new_mode, old_sha, new_sha, file_path, old_path, similarity in raw_entries:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestGitDiffTreeRaw or TestExtractCommit or Gitlink" -v`
Expected: PASS. Also run the broader suite once to catch any other 6-tuple unpack site: `.venv/bin/pytest tests/test_mcp_server.py -q 2>&1 | tail -30` — if anything else unpacks a 6-tuple from `_git_diff_tree_raw`'s output, it will now raise `ValueError`; grep `_git_diff_tree_raw` across `mcp_server.py` and `tests/test_mcp_server.py` to find and fix every call site before moving on.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: enable git -M rename detection, fix two-path raw-diff parsing"
```

---

## Task 4: `_extract_commit` R-status branch

**Files:**
- Modify: `mcp_server.py:3115-3191` (`_extract_commit`)
- Test: near existing `_extract_commit` tests — grep `class TestExtractCommit` in the test file

**Interfaces:**
- Consumes: Task 3's 8-tuple raw entries.
- Produces: `_extract_commit`'s returned `results` list gains a 5th tuple element for R-status entries: `(status, file_path, extracted, precomputed, old_path)`. For A/M/D entries, `old_path` is `""` (keeps the tuple shape uniform across all entries — simpler for `_run_ingestion` to consume than a variable-arity tuple).

- [ ] **Step 1: Write the failing test**

```python
class TestExtractCommitRename:
    def test_rename_status_extracts_new_path_and_tags_old_path(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "old_name.py").write_text("def login():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(repo), commits[1][0])[:3]
        assert len(results) == 1
        status, file_path, extracted, precomputed, old_path = mcp_server._extract_commit(str(repo), commits[1][0])[0][0]
        assert status == "R"
        assert file_path == "new_name.py"
        assert old_path == "old_name.py"
        assert "login" in extracted["functions"]

    def test_non_rename_status_has_empty_old_path(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        results = mcp_server._extract_commit(str(git_repo), commits[0][0])[0]
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "A"
        assert old_path == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestExtractCommitRename -v`
Expected: FAIL — `ValueError: not enough values to unpack` (results tuples are still 4-wide) or the rename simply isn't extracted at all (falls into the generic add/modify branch's `git show <hash>:new_name.py` call, which actually still works fine by coincidence since `file_path` is already just the new path post-Task-3-fix — but `old_path` won't be threaded through, so the 5-tuple unpack fails).

- [ ] **Step 3: Implement**

Replace the loop body in `_extract_commit` (mcp_server.py:3164-3184):

```python
    for status, old_mode, new_mode, old_sha, new_sha, file_path, old_path, similarity in raw_entries:
        if _is_ignored_path(file_path, ignore_patterns):
            continue
        parser = _thread_parser(file_path)
        if parser is None:
            continue
        if status == "D":
            results.append((status, file_path, None, None, ""))
            continue
        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue
        extracted = _extract_from_source(content, parser, file_path)
        if known_files is None:
            known_files = _known_files_at_commit(repo_path, commit_hash, ignore_patterns)
            segment_index = _SegmentSuffixIndex(known_files)
        precomputed = _precompute_file_triples(
            file_path, extracted, commit_ident, known_files, segment_index=segment_index,
        )
        results.append((status, file_path, extracted, precomputed, old_path if status == "R" else ""))
```

(Only two lines actually changed: the loop header gains `old_path, similarity`, and both `results.append` calls gain a 5th element. Everything else — the ignored-path skip, the parser lookup, the `git show` fetch, extraction, and precomputation — is identical to today's add/modify handling; a rename's new-path content is extracted exactly like a plain add, which is correct: the entity really does need to be introduced under its new ident.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestExtractCommitRename or TestExtractCommit" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract rename entries with new-path content and old_path tag"
```

---

## Task 5: Module-level rename triples in `_run_ingestion` + flip existing test

**Files:**
- Modify: `mcp_server.py:3194-3563` (`_run_ingestion` — specifically the per-file loop around 3353-3424, and the unpack at line 3328)
- Modify: `tests/test_mcp_server.py:4572-4597` (`test_renamed_file_closes_old_entities_and_opens_new` — flip assertions)

**Interfaces:**
- Consumes: Task 4's 5-tuple `extracted_files` entries.
- Produces: for a status-`"R"` file, `add_triples` gains `[{new_module_ident} :renamed-from {old_module_ident}]`, and the corresponding close entry for the old module ident gains `:renamed-to {new_module_ident}`. This is the first end-to-end rename-triple emission in the plan — later tasks (10, 27) follow the identical pattern for functions/classes/globals/fields.

- [ ] **Step 1: Write the failing test**

Replace `test_renamed_file_closes_old_entities_and_opens_new` (tests/test_mcp_server.py:4572-4597):

```python
    @pytest.mark.asyncio
    async def test_renamed_file_links_old_and_new_via_rename_edges(
        self, mock_minigraf_db, git_repo_with_rename, monkeypatch
    ):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_rename / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_rename), "HEAD")

        old_module_ident = mcp_server._code_ident("module", "old_auth.py")
        new_module_ident = mcp_server._code_ident("module", "new_auth.py")
        new_fn_ident = mcp_server._code_ident("function", "new_auth.py", "login")

        assert any(old_module_ident in t for t in close_triples_seen), \
            "Old module entities must still be closed when file is renamed"
        assert any(f"{old_module_ident} :renamed-to {new_module_ident}" in t for t in close_triples_seen), \
            "Old module's close triples must include :renamed-to pointing at the new ident"

        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert new_fn_ident in transact_calls, \
            "New module's entities must still be created after file is renamed"
        assert f"{new_module_ident} :renamed-from {old_module_ident}" in transact_calls, \
            "New module's open triples must include :renamed-from pointing at the old ident"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_renamed_file_links_old_and_new_via_rename_edges -v`
Expected: FAIL — no `:renamed-to`/`:renamed-from` triples exist anywhere yet.

- [ ] **Step 3: Implement**

In `_run_ingestion`, the per-file loop unpack (mcp_server.py:3353, currently `for status, file_path, extracted, precomputed in extracted_files:`) becomes:

```python
                        for status, file_path, extracted, precomputed, old_path in extracted_files:
```

Immediately after the existing `else:  # A or M` branch's body finishes (i.e., right after the dependency-edge block that ends at mcp_server.py:3424, still inside the same `for` iteration, so it only runs for this one file), add the module-rename branch. The cleanest insertion point is right after the initial `if status == "D": ... else:  # A or M` split — change the condition to a three-way branch. Concretely, mcp_server.py:3353-3371 (the `if status == "D":` block) stays as-is; change line 3371's `else:  # A or M` to also handle rename linkage. Insert this block as the FIRST statement inside the existing `else:  # A or M` branch (mcp_server.py:3371), before `previous_idents = set(...)`:

```python
                            else:  # A or M or R
                                if status == "R" and old_path:
                                    old_module_ident = _code_ident("module", old_path)
                                    new_module_ident = _code_ident("module", file_path)
                                    add_triples.append(f"[{new_module_ident} :renamed-from {old_module_ident}]")
                                    old_desc = entity_descriptions.get(old_module_ident, old_path)
                                    orig_ts = entity_valid_from.get(old_module_ident, commit_ts_iso)
                                    close_items.append((
                                        _build_close_triples(old_module_ident, old_desc, old_module_ident)
                                        + [f"[{old_module_ident} :renamed-to {new_module_ident}]"],
                                        orig_ts,
                                    ))
                                previous_idents = set(file_entities.get(file_path, []))
```

(The rest of the `else` branch — `triples = _build_code_triples(...)` through the end of the dependency-edge handling — is unchanged. Note this only closes/links the *module* ident; the old module's child functions/classes are NOT separately closed here — Task 4 already ensures `file_entities[old_path]` still holds their idents from when the file was ingested under its old name, and since nothing in this task touches `file_entities[old_path]`, those child idents are simply left dangling as still-open in the graph, attached to a now-renamed-away module ident. This is intentionally deferred to Task 10, which is the point where function/class-level continuity (including for entities that were simply carried along in a renamed file, untouched) gets handled uniformly for both the "file renamed" and "file merely modified" cases via the same matcher — don't try to close old-path child entities here; Task 10 supersedes this gap.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_renamed_file_links_old_and_new_via_rename_edges -v`
Expected: PASS

- [ ] **Step 5: Run the full bitemporal-close test class to check no regression**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestRunIngestionBitemporalClose -v`
Expected: All PASS (including the untouched deletion/intra-file-deletion tests).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: emit :renamed-from/:renamed-to edges for renamed modules"
```

---

## Task 6: Capture function/class body text in extraction

**Files:**
- Modify: `mcp_server.py:672-725` (`_walk_ast`, `_extract_from_source`)
- Modify: `tests/test_mcp_server.py:1835-1839` (`test_parse_error_returns_empty` — gains new empty keys)

**Interfaces:**
- Produces: `_extract_from_source`'s returned dict gains two new keys, additive (existing `"functions"`/`"classes"`/`"imports"`/`"calls"` keys are untouched): `"function_bodies": Dict[str, str]` and `"class_bodies": Dict[str, str]`, mapping entity name → its full source text (via the tree-sitter node's own `.text`, decoded). Consumed by Task 9's matcher.

- [ ] **Step 1: Write the failing tests**

```python
    def test_extracts_function_bodies(self):
        import mcp_server
        source = b"def login(user):\n    return user.ok\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "auth.py")
        assert "login" in result["function_bodies"]
        assert "return user.ok" in result["function_bodies"]["login"]

    def test_extracts_class_bodies(self):
        import mcp_server
        source = b"class User:\n    def ok(self):\n        return True\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "models.py")
        assert "User" in result["class_bodies"]
        assert "def ok" in result["class_bodies"]["User"]
```

Also update `test_parse_error_returns_empty` (tests/test_mcp_server.py:1835-1839):

```python
    def test_parse_error_returns_empty(self):
        import mcp_server
        result = mcp_server._extract_from_source(b"def foo(): pass", None, "x.py")
        assert result == {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "function_bodies": {}, "class_bodies": {},
        }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "test_extracts_function_bodies or test_extracts_class_bodies or test_parse_error_returns_empty" -v`
Expected: FAIL — `KeyError: 'function_bodies'` for the first two; the third fails on dict `==` (extra keys missing).

- [ ] **Step 3: Implement**

In `_walk_ast` (mcp_server.py:672-709), extend the two branches that already extract names:

```python
    if node.type in node_types.get("functions", set()):
        if lang_name in ("c", "cpp"):
            name = _c_family_function_name(node)
            if name:
                results["functions"].append(name)
                results["function_bodies"][name] = node.text.decode("utf-8", errors="replace")
        else:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                results["functions"].append(name)
                results["function_bodies"][name] = node.text.decode("utf-8", errors="replace")

    elif node.type in node_types.get("classes", set()):
        name_node = node.child_by_field_name("name")
        if name_node:
            name = name_node.text.decode("utf-8")
            results["classes"].append(name)
            results["class_bodies"][name] = node.text.decode("utf-8", errors="replace")
```

And `_extract_from_source` (mcp_server.py:712-725), extend the initial `results` dict:

```python
def _extract_from_source(
    source: bytes, parser: Any, file_path: str
) -> Dict[str, Any]:
    """Parse source bytes and extract functions, classes, imports, calls, and
    (for functions/classes) their own full source text — the latter used by
    the rename matcher (see _match_renamed_entities) to compare old vs. new
    bodies. Body text is captured here, inside the worker process, because
    tree_sitter Node objects themselves cannot cross the ProcessPoolExecutor
    boundary (see #116) — only the decoded text can.
    """
    results: Dict[str, Any] = {
        "functions": [], "classes": [], "imports": [], "calls": [],
        "function_bodies": {}, "class_bodies": {},
    }
    try:
        tree = parser.parse(source)
        lang_name = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        _walk_ast(tree.root_node, results, lang_name)
    except Exception:
        pass  # best-effort; parse failures are non-fatal
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "test_extracts_function_bodies or test_extracts_class_bodies or test_parse_error_returns_empty" -v`
Expected: PASS. Also run the full `TestExtractFromSource*` classes to confirm the additive change didn't break anything relying on exact dict equality elsewhere: `.venv/bin/pytest tests/test_mcp_server.py -k "ExtractFromSource" -v`

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: capture function/class body text during extraction"
```

---

## Task 7: AST-lockstep bijective matcher (single-pair core)

**Files:**
- Modify: `mcp_server.py` (new function, place near `_walk_ast`/`_extract_from_source`, e.g. directly after `_extract_from_source`)
- Test: `tests/test_mcp_server.py` (new `TestMatchCandidatePair` class)

**Interfaces:**
- Produces: `_match_candidate_pair(old_node, new_node, tracked_names: Dict[str, Optional[str]]) -> Optional[Dict[str, str]]`. `tracked_names` maps every entity name known in this commit's context to either `None` (unchanged, must appear identically) or a confirmed new name (must appear renamed). Returns the discovered local-identifier bijection (old→new) on a full match, or `None` if the two nodes don't match. This is a pure function operating on live tree-sitter nodes — must be called from the same process that parsed them (never crosses the `ProcessPoolExecutor` boundary itself; only its caller's *result*, in Task 9, does).
- Consumes: two `tree_sitter.Node` objects from the same or different parses of the same language.

- [ ] **Step 1: Write the failing tests**

```python
class TestMatchCandidatePair:
    def _parse(self, source: str):
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        tree = parser.parse(source.encode())
        # first top-level statement's node (a function_definition, in every fixture below)
        return tree.root_node.children[0]

    def test_identical_bodies_match_with_empty_bijection(self):
        import mcp_server
        old = self._parse("def foo(x):\n    return x + 1\n")
        new = self._parse("def foo(x):\n    return x + 1\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result == {}

    def test_renamed_local_variable_matches_via_bijection(self):
        import mcp_server
        old = self._parse("def foo(x):\n    y = x + 1\n    return y\n")
        new = self._parse("def foo(x):\n    z = x + 1\n    return z\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result == {"y": "z"}

    def test_inconsistent_local_rename_does_not_match(self):
        """y is renamed to z in one spot but stays y in another -> not a valid bijection."""
        import mcp_server
        old = self._parse("def foo(x):\n    y = x + 1\n    return y + y\n")
        new = self._parse("def foo(x):\n    z = x + 1\n    return z + y\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result is None

    def test_tracked_entity_with_confirmed_rename_must_match_new_name(self):
        """Body calls a helper that was itself confirmed renamed this round."""
        import mcp_server
        old = self._parse("def foo(x):\n    return helper_old(x)\n")
        new = self._parse("def foo(x):\n    return helper_new(x)\n")
        result = mcp_server._match_candidate_pair(old, new, {"helper_old": "helper_new"})
        assert result == {}

    def test_tracked_entity_without_rename_must_match_exactly(self):
        old = self._parse("def foo(x):\n    return helper(x)\n")
        new = self._parse("def foo(x):\n    return other(x)\n")
        import mcp_server
        result = mcp_server._match_candidate_pair(old, new, {"helper": None})
        assert result is None

    def test_structurally_different_bodies_do_not_match(self):
        import mcp_server
        old = self._parse("def foo(x):\n    return x + 1\n")
        new = self._parse("def foo(x):\n    if x:\n        return x\n    return 1\n")
        result = mcp_server._match_candidate_pair(old, new, {})
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestMatchCandidatePair -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_match_candidate_pair'`

- [ ] **Step 3: Implement**

Insert directly after `_extract_from_source` (mcp_server.py, end of Task 6's edit):

```python
def _match_candidate_pair(
    old_node: Any, new_node: Any, tracked_names: Dict[str, Optional[str]]
) -> Optional[Dict[str, str]]:
    """Lockstep-walk two tree-sitter nodes, allowing local (untracked)
    identifiers to differ under a one-to-one bijective mapping.

    tracked_names maps every entity name known in this commit's context to
    either None (must appear unchanged) or a confirmed new name (must appear
    renamed to exactly that). Any identifier NOT a key in tracked_names is
    treated as local/unresolved and is free to differ, as long as the
    mapping stays consistent (same old token always maps to the same new
    token) and injective (no two distinct old tokens collapse onto one new
    token) for THIS candidate pair only — the mapping is never reused across
    other pairs or persisted as an entity.

    Returns the discovered bijection dict on a full match (empty dict if no
    local identifiers were involved — plain exact match is the case where
    the bijection happens to be the identity mapping), or None if the nodes
    don't match structurally or a tracked/bijection constraint is violated.
    """
    mapping: Dict[str, str] = {}
    reverse: Dict[str, str] = {}

    def walk(a: Any, b: Any) -> bool:
        if a.type != b.type:
            return False
        if a.child_count == 0 and b.child_count == 0:
            a_text = a.text.decode("utf-8", "replace")
            b_text = b.text.decode("utf-8", "replace")
            if a.type == "identifier" or a.type.endswith("_identifier"):
                if a_text in tracked_names:
                    expected = tracked_names[a_text]
                    return b_text == (expected if expected is not None else a_text)
                if a_text in mapping:
                    return mapping[a_text] == b_text
                if b_text in reverse:
                    return False
                mapping[a_text] = b_text
                reverse[b_text] = a_text
                return True
            return a_text == b_text
        if a.child_count != b.child_count:
            return False
        return all(walk(ac, bc) for ac, bc in zip(a.children, b.children))

    return mapping if walk(old_node, new_node) else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestMatchCandidatePair -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add AST-lockstep bijective matcher for a single candidate pair"
```

---

## Task 8: Round-based match orchestration

**Files:**
- Modify: `mcp_server.py` (new function, directly after `_match_candidate_pair`)
- Test: `tests/test_mcp_server.py` (new `TestMatchRenamedEntities` class)

**Interfaces:**
- Produces: `_match_renamed_entities(removed: Dict[str, List[Tuple[str, Any]]], added: Dict[str, List[Tuple[str, Any]]]) -> List[Tuple[str, str, str]]`. `removed`/`added` map a category string (`"function"`, `"class"`, `"variable"`, `"field"` — Tasks 26-27 add the latter two) to a list of `(name, tree_sitter_node)` pairs. Returns confirmed `(category, old_name, new_name)` triples. Mutates its inputs (removes matched entries) — callers that need the leftover unmatched entries after the call can still read them from the now-shrunk `removed`/`added` dicts.
- Consumes: `_match_candidate_pair` (Task 7).

- [ ] **Step 1: Write the failing tests**

```python
class TestMatchRenamedEntities:
    def _parse_fn(self, source: str):
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        tree = parser.parse(source.encode())
        return tree.root_node.children[0]

    def test_simple_rename_matched(self):
        import mcp_server
        old = self._parse_fn("def foo(x):\n    return x + 1\n")
        new = self._parse_fn("def bar(x):\n    return x + 1\n")
        removed = {"function": [("foo", old)]}
        added = {"function": [("bar", new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == [("function", "foo", "bar")]
        assert removed["function"] == []
        assert added["function"] == []

    def test_cascading_mutual_rename_resolves_across_rounds(self):
        """A calls B; both A and B are renamed in the same commit."""
        import mcp_server
        old_a = self._parse_fn("def a(x):\n    return b(x) + 1\n")
        old_b = self._parse_fn("def b(x):\n    return x * 2\n")
        new_a1 = self._parse_fn("def a1(x):\n    return b1(x) + 1\n")
        new_b1 = self._parse_fn("def b1(x):\n    return x * 2\n")
        removed = {"function": [("a", old_a), ("b", old_b)]}
        added = {"function": [("a1", new_a1), ("b1", new_b1)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert ("function", "a", "a1") in matches
        assert ("function", "b", "b1") in matches
        assert len(matches) == 2

    def test_ambiguous_duplicate_bodies_not_matched(self):
        import mcp_server
        old1 = self._parse_fn("def stub1():\n    pass\n")
        old2 = self._parse_fn("def stub2():\n    pass\n")
        new1 = self._parse_fn("def stub3():\n    pass\n")
        new2 = self._parse_fn("def stub4():\n    pass\n")
        removed = {"function": [("stub1", old1), ("stub2", old2)]}
        added = {"function": [("stub3", new1), ("stub4", new2)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []
        assert len(removed["function"]) == 2
        assert len(added["function"]) == 2

    def test_below_minimum_size_not_matched(self):
        import mcp_server
        old = self._parse_fn("def x():\n    pass\n")
        new = self._parse_fn("def y():\n    pass\n")
        removed = {"function": [("x", old)]}
        added = {"function": [("y", new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []

    def test_cross_category_no_match(self):
        """A function and a class with coincidentally-matchable text never match across categories."""
        import mcp_server
        old = self._parse_fn("def foo(x):\n    return x + 1\n")
        new = self._parse_fn("def bar(x):\n    return x + 1\n")
        removed = {"function": [("foo", old)], "class": []}
        added = {"function": [], "class": [("bar", new)]}
        matches = mcp_server._match_renamed_entities(removed, added)
        assert matches == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestMatchRenamedEntities -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_match_renamed_entities'`

- [ ] **Step 3: Implement**

```python
_MAX_MATCH_ROUNDS = 10
_MIN_MATCH_BODY_LEN = 20  # normalized chars; avoids matching trivial boilerplate stubs


def _normalize_body_for_matching(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _match_renamed_entities(
    removed: Dict[str, List[Tuple[str, Any]]],
    added: Dict[str, List[Tuple[str, Any]]],
) -> List[Tuple[str, str, str]]:
    """Round-based rename matching across entity categories, scoped to a
    single commit's touched files (callers build removed/added from just
    that commit — see _extract_commit's use in Task 9).

    A rename confirmed in one category (e.g. a function) becomes available
    as a "tracked, confirmed-renamed" name for other not-yet-matched pairs
    (in the same or a different category) evaluated in a later round — this
    resolves cascading/mutual renames within one commit regardless of
    dependency order. Capped at _MAX_MATCH_ROUNDS as a defensive bound.

    Mutates removed/added in place, removing matched entries.
    """
    matches: List[Tuple[str, str, str]] = []
    confirmed: Dict[str, str] = {}  # old_name -> new_name, shared across all categories

    all_names: set = set()
    for pool in (removed, added):
        for entries in pool.values():
            all_names.update(name for name, _node in entries)

    for _round in range(_MAX_MATCH_ROUNDS):
        changed = False
        tracked_names: Dict[str, Optional[str]] = {
            name: confirmed.get(name) for name in all_names
        }
        for category in list(removed.keys()):
            r_list = removed.get(category, [])
            a_list = added.get(category, [])
            for r_name, r_node in list(r_list):
                r_text = _normalize_body_for_matching(r_node.text.decode("utf-8", "replace"))
                if len(r_text) < _MIN_MATCH_BODY_LEN:
                    continue
                candidates = []
                for a_name, a_node in a_list:
                    if _match_candidate_pair(r_node, a_node, tracked_names) is not None:
                        candidates.append((a_name, a_node))
                if len(candidates) == 1:
                    a_name, a_node = candidates[0]
                    matches.append((category, r_name, a_name))
                    confirmed[r_name] = a_name
                    r_list.remove((r_name, r_node))
                    a_list.remove((a_name, a_node))
                    changed = True
        if not changed:
            break
    return matches
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestMatchRenamedEntities -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add round-based rename matching across entity categories"
```

---

## Task 9: Wire matcher into `_extract_commit`

**Files:**
- Modify: `mcp_server.py` (new function `_collect_entity_nodes`, and changes inside `_extract_commit`, mcp_server.py:3115-3191)
- Test: `tests/test_mcp_server.py` (new `TestCollectEntityNodes` class; extend `TestExtractCommitRename`)

**Interfaces:**
- Produces: `_collect_entity_nodes(root_node: Any, lang_name: str) -> Dict[str, Dict[str, Any]]` — a sibling of `_walk_ast` that returns live `{category: {name: node}}` instead of text, for use only within a single worker process call (never crosses the `ProcessPoolExecutor` boundary). `_extract_commit`'s return signature gains a 4th tuple element: `renamed_pairs: List[Tuple[str, str, str, str, str]]` = `(category, old_file_path, old_name, new_file_path, new_name)` — plain strings, picklable, safe to cross the process boundary. Full return becomes `(results, gitlink_changes, gitmodules_map, renamed_pairs)`.
- Consumes: `_match_renamed_entities` (Task 8), `_git_blob_content` (Task 2), `_LANG_NODE_TYPES`/`_c_family_function_name` (existing).

- [ ] **Step 1: Write the failing tests**

```python
class TestCollectEntityNodes:
    def test_collects_function_and_class_nodes_by_name(self):
        import mcp_server
        parser = mcp_server._get_parser("test.py")
        source = b"def foo():\n    pass\n\nclass Bar:\n    pass\n"
        tree = parser.parse(source)
        result = mcp_server._collect_entity_nodes(tree.root_node, "python")
        assert "foo" in result["function"]
        assert result["function"]["foo"].type == "function_definition"
        assert "Bar" in result["class"]
        assert result["class"]["Bar"].type == "class_definition"
```

Extend `TestExtractCommitRename` with a cross-file-move end-to-end case:

```python
    def test_cross_file_move_produces_renamed_pair(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "fileA.py").write_text(
            "def stayHere(x):\n    return x + 1\n\ndef moveMe(x):\n    return x * 2 + 7\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "fileA.py").write_text("def stayHere(x):\n    return x + 1\n")
        (repo / "fileB.py").write_text("def moveMe(x):\n    return x * 2 + 7\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "move function"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        _, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        assert ("function", "fileA.py", "moveMe", "fileB.py", "moveMe") in renamed_pairs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestCollectEntityNodes or test_cross_file_move_produces_renamed_pair" -v`
Expected: FAIL — `_collect_entity_nodes` doesn't exist; `_extract_commit` still returns a 3-tuple.

- [ ] **Step 3: Implement**

Insert `_collect_entity_nodes` directly after `_match_renamed_entities` (Task 8):

```python
def _collect_entity_nodes(root_node: Any, lang_name: str) -> Dict[str, Dict[str, Any]]:
    """Like _walk_ast, but returns live nodes keyed by name instead of text —
    for use only inside a single worker-process call (_extract_commit), never
    returned across the ProcessPoolExecutor boundary. Only functions/classes
    are collected here; Task 26 extends this for globals/fields once those
    categories exist.
    """
    node_types = _LANG_NODE_TYPES.get("typescript" if lang_name == "tsx" else lang_name)
    result: Dict[str, Dict[str, Any]] = {"function": {}, "class": {}}
    if node_types is None:
        return result

    def walk(node: Any) -> None:
        if node.type in node_types.get("functions", set()):
            if lang_name in ("c", "cpp"):
                name = _c_family_function_name(node)
                if name:
                    result["function"][name] = node
            else:
                name_node = node.child_by_field_name("name")
                if name_node:
                    result["function"][name_node.text.decode("utf-8")] = node
        elif node.type in node_types.get("classes", set()):
            name_node = node.child_by_field_name("name")
            if name_node:
                result["class"][name_node.text.decode("utf-8")] = node
        for child in node.children:
            walk(child)

    walk(root_node)
    return result
```

Now modify `_extract_commit` (mcp_server.py:3115-3191). The signature's return type annotation and docstring gain the 4th element; the body needs three changes: (1) fetch+parse old content for D/M/R status files, (2) build removed/added pools per the rules below, (3) call the matcher and translate results into `renamed_pairs`.

Replace the full function body from `raw_entries = _git_diff_tree_raw(...)` through the final `return` (mcp_server.py:3158-3191):

```python
    raw_entries = _git_diff_tree_raw(repo_path, commit_hash)
    commit_ident = f":commit/{commit_hash[:12]}"
    results: List[tuple] = []
    known_files: Optional[Dict[str, List[str]]] = None
    segment_index: Optional[_SegmentSuffixIndex] = None

    # removed/added pools for _match_renamed_entities, scoped to this commit.
    # Populated alongside the existing per-file loop below; matched entirely
    # inside this worker process (nodes never cross the process boundary —
    # only the plain-string renamed_pairs derived from matches does).
    removed_pool: Dict[str, List[Tuple[str, Any]]] = {"function": [], "class": []}
    added_pool: Dict[str, List[Tuple[str, Any]]] = {"function": [], "class": []}
    # (category, old_file_path, old_name, new_file_path, new_name) is only
    # knowable once we know which FILE each pooled node came from — track
    # that alongside the pool itself.
    node_origin: Dict[int, str] = {}  # id(node) -> file_path

    for status, old_mode, new_mode, old_sha, new_sha, file_path, old_path, similarity in raw_entries:
        if _is_ignored_path(file_path, ignore_patterns):
            continue
        parser = _thread_parser(file_path)
        if parser is None:
            continue

        old_lang_path = old_path if status == "R" else file_path
        old_entity_nodes: Dict[str, Dict[str, Any]] = {"function": {}, "class": {}}
        if status in ("D", "M", "R") and old_sha and old_sha != "0" * len(old_sha):
            try:
                old_content = _git_blob_content(repo_path, old_sha)
                old_tree = parser.parse(old_content)
                old_lang = _EXT_TO_LANG.get(Path(old_lang_path).suffix.lower(), "")
                old_entity_nodes = _collect_entity_nodes(old_tree.root_node, old_lang)
            except Exception:
                pass  # best-effort: matching degrades to no-match, not a hard failure

        if status == "D":
            for category in ("function", "class"):
                for name, node in old_entity_nodes[category].items():
                    removed_pool[category].append((name, node))
                    node_origin[id(node)] = old_lang_path
            results.append((status, file_path, None, None, ""))
            continue

        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue
        extracted = _extract_from_source(content, parser, file_path)
        if known_files is None:
            known_files = _known_files_at_commit(repo_path, commit_hash, ignore_patterns)
            segment_index = _SegmentSuffixIndex(known_files)
        precomputed = _precompute_file_triples(
            file_path, extracted, commit_ident, known_files, segment_index=segment_index,
        )
        results.append((status, file_path, extracted, precomputed, old_path if status == "R" else ""))

        # Build this file's contribution to the removed/added pools. Live
        # nodes for the NEW side come from re-parsing (extracted only carries
        # text, per Task 6) — cheap, since this is the same content already
        # fetched and parsed once above for tree-sitter extraction; a second
        # parse of the same bytes is a deliberate simplicity/cost tradeoff
        # over threading Node references through _extract_from_source's
        # return value, which must stay plain-data-only for other callers.
        new_lang = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        new_tree = parser.parse(content)
        new_entity_nodes = _collect_entity_nodes(new_tree.root_node, new_lang)

        if status == "A":
            for category in ("function", "class"):
                for name, node in new_entity_nodes[category].items():
                    added_pool[category].append((name, node))
                    node_origin[id(node)] = file_path
        elif status == "R":
            # Ident changes for every entity in a renamed file, even ones
            # whose text is byte-identical — pool everything on both sides,
            # not just the local diff (unlike "M" below).
            for category in ("function", "class"):
                for name, node in old_entity_nodes[category].items():
                    removed_pool[category].append((name, node))
                    node_origin[id(node)] = old_lang_path
                for name, node in new_entity_nodes[category].items():
                    added_pool[category].append((name, node))
                    node_origin[id(node)] = file_path
        else:  # "M" — same path, only the local diff needs matching
            for category in ("function", "class"):
                old_names = set(old_entity_nodes[category].keys())
                new_names = set(new_entity_nodes[category].keys())
                for name in old_names - new_names:
                    node = old_entity_nodes[category][name]
                    removed_pool[category].append((name, node))
                    node_origin[id(node)] = old_lang_path
                for name in new_names - old_names:
                    node = new_entity_nodes[category][name]
                    added_pool[category].append((name, node))
                    node_origin[id(node)] = file_path

    raw_matches = _match_renamed_entities(removed_pool, added_pool)
    # raw_matches only carries (category, old_name, new_name) — recover the
    # file each side came from via node_origin, captured above.
    renamed_pairs: List[Tuple[str, str, str, str, str]] = []
    all_removed_nodes = {
        (category, name): node
        for category, entries in removed_pool.items()
        for name, node in entries
    }
    # removed_pool/added_pool are mutated (matched entries removed) by
    # _match_renamed_entities, so look up origin via a snapshot taken before
    # the call — rebuild from node_origin's id()-keyed map instead, which
    # survives the mutation since it was populated from the same node objects.
    for category, old_name, new_name in raw_matches:
        renamed_pairs.append((category, "", old_name, "", new_name))
    # Second pass to fill in file paths: node_origin is keyed by id(node), and
    # matched nodes are gone from removed_pool/added_pool by now, so paths
    # must be captured at match time instead — see Step 3 refinement below.

    gitlink_changes = _gitlink_changes(raw_entries)
    gitmodules_map: Dict[str, Dict[str, str]] = {}
    if any(kind == "add" for kind, _, _ in gitlink_changes):
        gitmodules_map = _git_gitmodules_at(repo_path, commit_hash)

    return results, gitlink_changes, gitmodules_map, renamed_pairs
```

**Fix the file-path gap before running tests**: the sketch above loses each match's file path because `_match_renamed_entities` only returns `(category, old_name, new_name)`, and by the time it returns, the matched nodes are already removed from the pools. Resolve this by capturing `(file_path, node)` pairs (not bare nodes) in the pools instead, and adjusting `_match_renamed_entities` (Task 8) is NOT the right place to add file-path plumbing — it's meant to be file-path-agnostic and reusable for Task 26. Instead, keep `node_origin: Dict[int, str]` (already present in the sketch above) and change the final translation loop to look up each match's origin **before** the match removes the node from the pool — i.e., capture `node_origin` lookups eagerly right after building `raw_matches`, using the ORIGINAL node objects, which `_match_renamed_entities` doesn't return but which are still reachable from the `(category, name)` pair only if names are unique per category per commit. Since two different removed entities could coincidentally share a name (e.g. `helper` removed from two different deleted files in one commit), name alone is not a safe lookup key.

The robust fix: change `_match_renamed_entities`'s return type to include the matched nodes themselves — `List[Tuple[str, str, Any, str, Any]]` = `(category, old_name, old_node, new_name, new_node)` — since it already has direct references to both matched nodes at the moment of match (mcp_server.py, the `matches.append(...)` line inside Task 8's implementation). Update Task 8's `matches.append((category, r_name, a_name))` to `matches.append((category, r_name, r_node, a_name, a_node))`, update Task 8's tests' assertions accordingly (`assert matches[0][:2] == ("function", "foo")` style, or unpack and check the node's `.text` instead of comparing tuples directly), and then here in Task 9, the final translation becomes straightforward:

```python
    raw_matches = _match_renamed_entities(removed_pool, added_pool)
    renamed_pairs: List[Tuple[str, str, str, str, str]] = []
    for category, old_name, old_node, new_name, new_node in raw_matches:
        renamed_pairs.append((
            category, node_origin[id(old_node)], old_name, node_origin[id(new_node)], new_name,
        ))
```

Go back and apply this signature change to Task 8 now (both the implementation and its tests) before proceeding — this is a real interface correction discovered while wiring Task 8 into a real caller, not a deferred TODO.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestCollectEntityNodes or TestMatchRenamedEntities or TestExtractCommitRename or TestExtractCommit" -v`
Expected: PASS

- [ ] **Step 5: Update every other caller of `_extract_commit`/`_match_renamed_entities`**

Grep both names across `mcp_server.py` and `tests/test_mcp_server.py`; any remaining 3-tuple unpack of `_extract_commit`'s return, or 3-tuple-match unpack from Task 8's tests, needs updating to match the new signatures. Run the full suite to catch stragglers: `.venv/bin/pytest tests/test_mcp_server.py -q 2>&1 | tail -40`

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: wire AST-lockstep matcher into _extract_commit, return renamed_pairs"
```

---

## Task 10: Consume confirmed pairs in `_run_ingestion`, emit rename triples for functions/classes

**Files:**
- Modify: `mcp_server.py:3194-3563` (`_run_ingestion` — the `await fut` unpack at line 3328, and a new block after the per-file loop)
- Test: `tests/test_mcp_server.py` (new test in `TestRunIngestionBitemporalClose`)

**Interfaces:**
- Consumes: Task 9's `renamed_pairs` (4th element of `_extract_commit`'s return, threaded through the `await fut` unpack).
- Produces: for every `(category, old_file, old_name, new_file, new_name)` in `renamed_pairs`, the new ident gets `:renamed-from`, and the old ident's close entry gets `:renamed-to` — same pattern as Task 5's module-level handling, generalized to functions/classes (and reused verbatim by Task 27 for globals/fields).

- [ ] **Step 1: Write the failing test**

```python
    @pytest.mark.asyncio
    async def test_in_file_function_rename_links_via_rename_edges(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def oldName(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def newName(x):\n    return x + 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename fn"], cwd=repo, check=True, capture_output=True)

        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(repo), "HEAD")

        old_fn_ident = mcp_server._code_ident("function", "auth.py", "oldName")
        new_fn_ident = mcp_server._code_ident("function", "auth.py", "newName")

        assert any(f"{old_fn_ident} :renamed-to {new_fn_ident}" in t for t in close_triples_seen)
        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert f"{new_fn_ident} :renamed-from {old_fn_ident}" in transact_calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_in_file_function_rename_links_via_rename_edges -v`
Expected: FAIL — no rename triples emitted for functions yet.

- [ ] **Step 3: Implement**

The `await fut` unpack (mcp_server.py:3328) gains the 4th element:

```python
                    extracted_files, gitlink_changes, gitmodules_map, renamed_pairs = await fut
```

Add a new block immediately after the per-file `for status, file_path, extracted, precomputed, old_path in extracted_files:` loop ends (i.e., after the dependency-edge handling that closes out around mcp_server.py:3424, at the same indentation level as that `for` loop itself — NOT nested inside it, since `renamed_pairs` covers the whole commit, not one file), and before the `# Process gitlink changes` comment:

```python
                        # Function/class rename linkage (Task 9's renamed_pairs).
                        # Module-level linkage is handled separately per-file
                        # above (Task 5) since it comes from git's own -M
                        # detection, not this commit-wide matcher.
                        for category, old_file, old_name, new_file, new_name in renamed_pairs:
                            old_ident = _code_ident(category, old_file, old_name)
                            new_ident = _code_ident(category, new_file, new_name)
                            add_triples.append(f"[{new_ident} :renamed-from {old_ident}]")
                            old_desc = entity_descriptions.get(old_ident, old_name)
                            old_module_ident = _code_ident("module", old_file)
                            orig_ts = entity_valid_from.get(old_ident, commit_ts_iso)
                            close_items.append((
                                _build_close_triples(old_ident, old_desc, old_module_ident)
                                + [f"[{old_ident} :renamed-to {new_ident}]"],
                                orig_ts,
                            ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_in_file_function_rename_links_via_rename_edges -v`
Expected: PASS

- [ ] **Step 5: Run the full ingestion/bitemporal-close test suite**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestRunIngestionBitemporalClose or Ingestion" -v`
Expected: All PASS. This is also the natural checkpoint to run the **entire** suite once, since Component 2 (Tasks 6-10) is the riskiest, most structurally invasive part of the whole plan: `.venv/bin/pytest tests/test_mcp_server.py -q`

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: emit :renamed-from/:renamed-to edges for renamed functions/classes"
```

---

## Task 11: Generic `:type/variable`/`:type/field` plumbing (language-agnostic)

**Files:**
- Modify: `mcp_server.py:2748-2820` (`_precompute_file_triples`)
- Modify: `mcp_server.py:2823-2891` (`_build_code_triples`)
- Modify: `mcp_server.py:2894-2954` (`_preload_known_entities`)
- Test: `tests/test_mcp_server.py` (new tests in the existing `TestIngestionWrites`-area classes)

**Interfaces:**
- Consumes: assumes `extracted` dicts passed in carry two new keys — `"globals": List[str]` and `"fields": List[Tuple[str, str, bool]]` (name, owning_class_name, is_static). This task hand-constructs these dicts in its own tests; Tasks 13-25 are what actually populate them from real source via `_extract_from_source`.
- Produces: `_precompute_file_triples`'s returned dict gains `"global_entries"`/`"field_entries"` (same `(ident, name, candidate_triples)` shape as `function_entries`/`class_entries`). `_build_code_triples` gains matching open/modify loops. `_preload_known_entities` reloads `"variable"`/`"field"` idents too.
- Field idents disambiguate same-named fields across different classes in one file by using `f"{class_name}.{field_name}"` as `_code_ident`'s `name` parameter (module-level globals use the bare name — they have no owning class).

- [ ] **Step 1: Write the failing tests**

```python
class TestPrecomputeGlobalsAndFields:
    def test_global_entries_shape(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "function_bodies": {}, "class_bodies": {},
            "globals": ["GLOBAL_X"], "global_bodies": {"GLOBAL_X": "GLOBAL_X = 5"},
            "fields": [], "field_info": {},
        }
        result = mcp_server._precompute_file_triples(
            "config.py", extracted, ":commit/abc123", {}, segment_index=None,
        )
        assert len(result["global_entries"]) == 1
        ident, name, triples = result["global_entries"][0]
        assert ident == mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        assert name == "GLOBAL_X"
        assert f"[{ident} :entity-type :type/variable]" in triples
        assert f"[{ident} :introduced-by :commit/abc123]" in triples

    def test_field_entries_shape_disambiguates_by_class(self):
        import mcp_server
        extracted = {
            "functions": [], "classes": ["Foo"], "imports": [], "calls": [],
            "function_bodies": {}, "class_bodies": {"Foo": "class Foo: ..."},
            "globals": [], "global_bodies": {},
            "fields": [("staticField", "Foo", True)],
            "field_info": {"staticField": {"class": "Foo", "static": True, "body": "staticField = 1"}},
        }
        result = mcp_server._precompute_file_triples(
            "models.py", extracted, ":commit/abc123", {}, segment_index=None,
        )
        assert len(result["field_entries"]) == 1
        ident, name, triples = result["field_entries"][0]
        expected_ident = mcp_server._code_ident("field", "models.py", "Foo.staticField")
        assert ident == expected_ident
        assert f"[{ident} :entity-type :type/field]" in triples
        assert f"[{ident} :static true]" in triples
        class_ident = mcp_server._code_ident("class", "models.py", "Foo")
        assert f"[{ident} :class {class_ident}]" in triples

class TestBuildCodeTriplesGlobalsAndFields:
    def test_new_global_writes_full_triples(self):
        import mcp_server
        extracted = {"functions": [], "classes": [], "imports": [], "calls": [],
                     "function_bodies": {}, "class_bodies": {},
                     "globals": ["GLOBAL_X"], "global_bodies": {"GLOBAL_X": "GLOBAL_X = 5"},
                     "fields": [], "field_info": {}}
        precomputed = mcp_server._precompute_file_triples("config.py", extracted, ":commit/c1", {})
        entity_valid_from, entity_descriptions, file_entities = {}, {}, {}
        triples = mcp_server._build_code_triples(
            "config.py", extracted, "2024-01-01T00:00:00Z", entity_valid_from,
            entity_descriptions, file_entities, ":commit/c1", precomputed,
        )
        ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        assert any(f"{ident} :entity-type :type/variable" in t for t in triples)
        assert ident in entity_valid_from

    def test_preexisting_global_only_gets_modified_in(self):
        import mcp_server
        extracted = {"functions": [], "classes": [], "imports": [], "calls": [],
                     "function_bodies": {}, "class_bodies": {},
                     "globals": ["GLOBAL_X"], "global_bodies": {"GLOBAL_X": "GLOBAL_X = 6"},
                     "fields": [], "field_info": {}}
        precomputed = mcp_server._precompute_file_triples("config.py", extracted, ":commit/c2", {})
        ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        entity_valid_from = {ident: "2024-01-01T00:00:00Z"}
        entity_descriptions = {ident: "GLOBAL_X"}
        file_entities = {"config.py": [ident]}
        triples = mcp_server._build_code_triples(
            "config.py", extracted, "2024-01-02T00:00:00Z", entity_valid_from,
            entity_descriptions, file_entities, ":commit/c2", precomputed,
        )
        assert triples == [f"[{ident} :modified-in :commit/c2]"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestPrecomputeGlobalsAndFields or TestBuildCodeTriplesGlobalsAndFields" -v`
Expected: FAIL — `KeyError: 'global_entries'` (not populated yet).

- [ ] **Step 3: Implement**

In `_precompute_file_triples` (mcp_server.py:2748-2820), insert after the existing `class_entries` loop (mcp_server.py:2795-2805), before `resolved_imports`:

```python
    global_entries: List[Tuple[str, str, List[str]]] = []
    for gvar_name in extracted.get("globals", []):
        gvar_ident = _code_ident("variable", file_path, gvar_name)
        global_entries.append((gvar_ident, gvar_name, [
            f"[{gvar_ident} :entity-type :type/variable]",
            f'[{gvar_ident} :ident "{gvar_ident}"]',
            f'[{gvar_ident} :description "{_edn_escape(gvar_name)}"]',
            f'[{gvar_ident} :file "{_edn_escape(file_path)}"]',
            f"[{module_ident} :contains {gvar_ident}]",
            f"[{gvar_ident} :introduced-by {commit_ident}]",
        ]))

    field_entries: List[Tuple[str, str, List[str]]] = []
    for field_name, owning_class, is_static in extracted.get("fields", []):
        qualified_name = f"{owning_class}.{field_name}"
        field_ident = _code_ident("field", file_path, qualified_name)
        class_ident = _code_ident("class", file_path, owning_class)
        static_literal = "true" if is_static else "false"
        field_entries.append((field_ident, qualified_name, [
            f"[{field_ident} :entity-type :type/field]",
            f'[{field_ident} :ident "{field_ident}"]',
            f'[{field_ident} :description "{_edn_escape(qualified_name)}"]',
            f'[{field_ident} :file "{_edn_escape(file_path)}"]',
            f"[{field_ident} :class {class_ident}]",
            f"[{field_ident} :static {static_literal}]",
            f"[{module_ident} :contains {field_ident}]",
            f"[{field_ident} :introduced-by {commit_ident}]",
        ]))
```

And extend the final `return` dict (mcp_server.py:2814-2820) with `"global_entries": global_entries, "field_entries": field_entries,`.

In `_build_code_triples` (mcp_server.py:2823-2891), insert after the existing `class_entries` loop (mcp_server.py:2880-2889), before the final `return triples`:

```python
    for gvar_ident, gvar_name, candidate_triples in precomputed["global_entries"]:
        if gvar_ident not in entity_valid_from:
            triples += candidate_triples
            if gvar_ident not in idents_for_file:
                idents_for_file.append(gvar_ident)
            entity_valid_from[gvar_ident] = commit_ts_iso
            entity_descriptions[gvar_ident] = gvar_name
        else:
            triples.append(f"[{gvar_ident} :modified-in {commit_ident}]")

    for field_ident, field_name, candidate_triples in precomputed["field_entries"]:
        if field_ident not in entity_valid_from:
            triples += candidate_triples
            if field_ident not in idents_for_file:
                idents_for_file.append(field_ident)
            entity_valid_from[field_ident] = commit_ts_iso
            entity_descriptions[field_ident] = field_name
        else:
            triples.append(f"[{field_ident} :modified-in {commit_ident}]")
```

In `_preload_known_entities` (mcp_server.py:2894-2954), change line 2931's entity-type tuple:

```python
    for entity_type in ("module", "function", "class", "variable", "field", "external-dependency"):
```

(The loop body already generalizes via `path_attr = "path" if entity_type in ("module", "external-dependency") else "file"` — `"variable"`/`"field"` fall into the `"file"` branch automatically, correct since both carry `:file` not `:path`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestPrecomputeGlobalsAndFields or TestBuildCodeTriplesGlobalsAndFields" -v`
Expected: PASS. Also re-run the existing function/class precompute/build-triples tests to confirm no regression: `.venv/bin/pytest tests/test_mcp_server.py -k "TestIngestionWrites or PrecomputeFileTriples" -v`

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: generic :type/variable and :type/field triple-writing plumbing"
```

---

## Task 12: Scope-aware traversal skeleton + per-language dispatch table

**Files:**
- Modify: `mcp_server.py` (new function `_extract_globals_and_fields`, new dispatch table `_GLOBAL_FIELD_EXTRACTORS`, and a call site inside `_extract_from_source`)
- Test: `tests/test_mcp_server.py` (new `TestExtractGlobalsAndFields` class — only the dispatch/fallback behavior; per-language extraction logic is Tasks 13-25)

**Interfaces:**
- Produces: `_extract_globals_and_fields(root_node: Any, lang_name: str) -> Dict[str, Any]` returning `{"globals": [...], "global_bodies": {...}, "fields": [(name, class_name, is_static), ...], "field_info": {...}}`. Dispatches to a per-language function via `_GLOBAL_FIELD_EXTRACTORS: Dict[str, Callable]`, defaulting to an empty-result stub for any language not yet in the table — this is what lets Tasks 13-25 land independently, one language at a time, without any of them being a hard blocker for the others.
- `_extract_from_source` (mcp_server.py, as modified by Task 6) calls this and merges its output into the returned dict, so `extracted["globals"]`/`extracted["fields"]` are always present (possibly empty) for every file, consistent with how `"functions"`/`"classes"` already always exist.

**Why this is NOT a naive full-tree walk (see the design spec's Component 3 correction):** unlike `_walk_ast`, which safely recurses into every node because function/class definitions mean the same thing wherever they appear, an assignment-like node appears on nearly every line of every function body too. Each per-language extractor function (Tasks 13-25) is responsible for only descending into module-level statements and class-body-level statements — explicitly never into a function/method body (except a narrow, per-language, deliberate exception like Python's `self.x = ...` inside `__init__`, handled inside that language's own task, not here).

- [ ] **Step 1: Write the failing tests**

```python
class TestExtractGlobalsAndFields:
    def test_unsupported_language_returns_empty(self):
        import mcp_server
        result = mcp_server._extract_globals_and_fields(None, "nonexistent_lang")
        assert result == {"globals": [], "global_bodies": {}, "fields": [], "field_info": {}}

    def test_dispatches_to_registered_language_extractor(self):
        import mcp_server
        sentinel = {"globals": ["X"], "global_bodies": {"X": "X = 1"}, "fields": [], "field_info": {}}
        mcp_server._GLOBAL_FIELD_EXTRACTORS["_test_lang"] = lambda root: sentinel
        try:
            result = mcp_server._extract_globals_and_fields("fake_root", "_test_lang")
            assert result == sentinel
        finally:
            del mcp_server._GLOBAL_FIELD_EXTRACTORS["_test_lang"]

    def test_extract_from_source_merges_globals_and_fields(self):
        import mcp_server
        source = b"def foo(): pass"
        result = mcp_server._extract_from_source(source, self._python_parser(), "x.py")
        assert result["globals"] == []
        assert result["fields"] == []
```

(`self._python_parser()` — this test needs a real parser fixture; place it inside whichever existing test class already defines `_python_parser` — e.g. `TestExtractFromSource` — rather than redefining it, or copy the two-line helper if a standalone class is preferred: `import tree_sitter_python; from tree_sitter import Language, Parser; return Parser(Language(tree_sitter_python.language()))`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestExtractGlobalsAndFields -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_extract_globals_and_fields'`

- [ ] **Step 3: Implement**

```python
def _extract_globals_and_fields(root_node: Any, lang_name: str) -> Dict[str, Any]:
    """Scope-aware extraction of module-level globals and class fields.

    Deliberately NOT a _walk_ast-style full-tree recursion: an assignment-
    like node is ubiquitous (appears inside every function body too), so a
    naive table-driven walk would misclassify every local variable as a
    global. Each per-language function in _GLOBAL_FIELD_EXTRACTORS is
    responsible for only descending into module-level and class-body-level
    statements, never into a function/method body (barring a narrow,
    per-language, deliberate exception — see each language's own extractor).
    """
    empty: Dict[str, Any] = {"globals": [], "global_bodies": {}, "fields": [], "field_info": {}}
    extractor = _GLOBAL_FIELD_EXTRACTORS.get(lang_name)
    if extractor is None or root_node is None:
        return empty
    return extractor(root_node)


_GLOBAL_FIELD_EXTRACTORS: Dict[str, Callable[[Any], Dict[str, Any]]] = {}
```

Place `_GLOBAL_FIELD_EXTRACTORS` right after `_extract_globals_and_fields` (forward-referenced inside the function body, which is fine in Python since the lookup happens at call time, not definition time — but keep the dict defined at module scope, not nested, so Tasks 13-25 can each register into it with a top-level `_GLOBAL_FIELD_EXTRACTORS["python"] = _extract_python_globals_and_fields`-style line).

In `_extract_from_source` (as modified by Task 6), add the merge right before `return results`:

```python
    try:
        tree = parser.parse(source)
        lang_name = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        _walk_ast(tree.root_node, results, lang_name)
        gf = _extract_globals_and_fields(tree.root_node, "typescript" if lang_name == "tsx" else lang_name)
        results["globals"] = gf["globals"]
        results["global_bodies"] = gf["global_bodies"]
        results["fields"] = gf["fields"]
        results["field_info"] = gf["field_info"]
    except Exception:
        pass
    return results
```

And extend `_extract_from_source`'s initial `results` dict (from Task 6) with the same four keys defaulted empty, so a parse failure still returns a complete, consistently-shaped dict:

```python
    results: Dict[str, Any] = {
        "functions": [], "classes": [], "imports": [], "calls": [],
        "function_bodies": {}, "class_bodies": {},
        "globals": [], "global_bodies": {}, "fields": [], "field_info": {},
    }
```

(This also means Task 6's `test_parse_error_returns_empty` update needs these four extra keys too — go back and add them now.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "TestExtractGlobalsAndFields or test_parse_error_returns_empty" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add scope-aware globals/fields extraction dispatch skeleton"
```

---

## Task 13: Python globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_python_globals_and_fields`, registered in `_GLOBAL_FIELD_EXTRACTORS`); Test: `tests/test_mcp_server.py` (new `TestPythonGlobalsAndFields`)

**Grammar facts** (verified via live parse, see design conversation): module root is `module`; a top-level `x = 5` is `expression_statement > assignment` with `field:left` = `identifier`; `class Foo:` is `class_definition` with `field:body` = `block`; a class-body-level `class_var = 10` is the same `expression_statement > assignment` shape, one level inside the class's `block`; `self.instance_var = 1` inside `__init__` is `expression_statement > assignment` where `field:left` is an `attribute` node (fields `object`=`self` identifier, `attribute`=the field name identifier).

- [ ] **Step 1: Write the failing tests**

```python
class TestPythonGlobalsAndFields:
    def _parser(self):
        import tree_sitter_python
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_python.language()))

    def test_module_level_global(self):
        import mcp_server
        tree = self._parser().parse(b"GLOBAL_X = 5\n")
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        assert "GLOBAL_X" in result["globals"]
        assert "GLOBAL_X = 5" in result["global_bodies"]["GLOBAL_X"]

    def test_class_variable_is_static_field(self):
        import mcp_server
        source = b"class Foo:\n    class_var = 10\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        names = [n for n, _c, _s in result["fields"]]
        assert "class_var" in names
        info = result["field_info"]["class_var"]
        assert info["class"] == "Foo"
        assert info["static"] is True

    def test_self_attribute_in_init_is_instance_field(self):
        import mcp_server
        source = b"class Foo:\n    def __init__(self):\n        self.instance_var = 1\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        names = [n for n, _c, _s in result["fields"]]
        assert "instance_var" in names
        info = result["field_info"]["instance_var"]
        assert info["class"] == "Foo"
        assert info["static"] is False

    def test_local_variable_inside_function_not_captured(self):
        import mcp_server
        source = b"def foo():\n    local_x = 1\n    return local_x\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_self_attribute_outside_init_not_captured(self):
        """Scoped deliberately to __init__ only — see design plan's stated limitation."""
        import mcp_server
        source = b"class Foo:\n    def other(self):\n        self.dynamic = 1\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_python_globals_and_fields(tree.root_node)
        assert result["fields"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestPythonGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_python_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def plain_assignment_name(stmt_node: Any) -> Optional[Tuple[str, Any]]:
        if stmt_node.type != "expression_statement" or stmt_node.child_count == 0:
            return None
        assign = stmt_node.children[0]
        if assign.type != "assignment":
            return None
        left = assign.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            return left.text.decode("utf-8"), assign
        return None

    def self_attr_assignment_name(stmt_node: Any) -> Optional[Tuple[str, Any]]:
        if stmt_node.type != "expression_statement" or stmt_node.child_count == 0:
            return None
        assign = stmt_node.children[0]
        if assign.type != "assignment":
            return None
        left = assign.child_by_field_name("left")
        if left is None or left.type != "attribute":
            return None
        obj = left.child_by_field_name("object")
        attr = left.child_by_field_name("attribute")
        if obj is not None and obj.type == "identifier" and obj.text == b"self" and attr is not None:
            return attr.text.decode("utf-8"), assign
        return None

    for stmt in root_node.children:
        if stmt.type == "class_definition":
            class_name_node = stmt.child_by_field_name("name")
            class_name = class_name_node.text.decode("utf-8") if class_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                match = plain_assignment_name(member)
                if match:
                    field_name, assign_node = match
                    fields.append((field_name, class_name, True))
                    field_info[field_name] = {
                        "class": class_name, "static": True,
                        "body": assign_node.text.decode("utf-8", "replace"),
                    }
                elif member.type == "function_definition":
                    fn_name_node = member.child_by_field_name("name")
                    if fn_name_node is not None and fn_name_node.text == b"__init__":
                        fn_body = member.child_by_field_name("body")
                        if fn_body is not None:
                            for fn_stmt in fn_body.children:
                                self_match = self_attr_assignment_name(fn_stmt)
                                if self_match:
                                    field_name, assign_node = self_match
                                    fields.append((field_name, class_name, False))
                                    field_info[field_name] = {
                                        "class": class_name, "static": False,
                                        "body": assign_node.text.decode("utf-8", "replace"),
                                    }
        else:
            match = plain_assignment_name(stmt)
            if match:
                name, assign_node = match
                globals_.append(name)
                global_bodies[name] = assign_node.text.decode("utf-8", "replace")

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["python"] = _extract_python_globals_and_fields
```

**Known limitation** (document, don't chase): only `__init__`'s own direct statements are scanned for `self.x =` assignments — a field first assigned in some other method (a dynamically-added attribute) is not captured. This is a deliberate, bounded heuristic (see the design plan's Component 3 discussion), not an oversight.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestPythonGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Python module globals and class fields"
```

---

## Task 14: JavaScript + TypeScript globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_js_family_globals_and_fields`, registered for both `"javascript"` and `"typescript"`); Test: `tests/test_mcp_server.py` (new `TestJsFamilyGlobalsAndFields`)

**Grammar facts:** root is `program`; a top-level `const X = 5;`/`let x = 5;` is `lexical_declaration` (or `variable_declaration` for `var`) containing one or more `variable_declarator` children, each with `field:name`. `class Foo { ... }` is `class_declaration` with `field:body` = `class_body`; each member is a `field_definition` (JS) or `public_field_definition` (TS), with `field:property`/`field:name` respectively holding the member's identifier, and a literal `static` child token when the modifier is present (check via `any(c.type == "static" for c in member.children)`).

- [ ] **Step 1: Write the failing tests**

```python
class TestJsFamilyGlobalsAndFields:
    def _js_parser(self):
        import tree_sitter_javascript
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_javascript.language()))

    def _ts_parser(self):
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_typescript.language_typescript()))

    def test_js_module_level_global(self):
        import mcp_server
        tree = self._js_parser().parse(b"const GLOBAL_X = 5;\n")
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert "GLOBAL_X" in result["globals"]

    def test_js_static_and_instance_fields(self):
        import mcp_server
        source = b"class Foo {\n  static staticField = 1;\n  instanceField = 2;\n}\n"
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_ts_public_field_definition_static(self):
        import mcp_server
        source = b"class Foo {\n  static staticField: number = 1;\n  instanceField: number = 2;\n}\n"
        tree = self._ts_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)

    def test_local_variable_not_captured(self):
        import mcp_server
        source = b"function foo() {\n  const localX = 1;\n  return localX;\n}\n"
        tree = self._js_parser().parse(source)
        result = mcp_server._extract_js_family_globals_and_fields(tree.root_node)
        assert result["globals"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestJsFamilyGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_js_family_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    for stmt in root_node.children:
        if stmt.type in ("lexical_declaration", "variable_declaration"):
            for child in stmt.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    if name_node is not None and name_node.type == "identifier":
                        name = name_node.text.decode("utf-8")
                        globals_.append(name)
                        global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "class_declaration":
            class_name_node = stmt.child_by_field_name("name")
            class_name = class_name_node.text.decode("utf-8") if class_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type not in ("field_definition", "public_field_definition"):
                    continue
                name_node = member.child_by_field_name("property") or member.child_by_field_name("name")
                if name_node is None or name_node.type not in ("property_identifier",):
                    continue
                field_name = name_node.text.decode("utf-8")
                is_static = any(c.type == "static" for c in member.children)
                fields.append((field_name, class_name, is_static))
                field_info[field_name] = {
                    "class": class_name, "static": is_static,
                    "body": member.text.decode("utf-8", "replace"),
                }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["javascript"] = _extract_js_family_globals_and_fields
_GLOBAL_FIELD_EXTRACTORS["typescript"] = _extract_js_family_globals_and_fields
```

(`tsx` is handled the same way as elsewhere in the file — `_extract_from_source`'s Task 12 dispatch already aliases `"tsx"` to `"typescript"` before calling `_extract_globals_and_fields`, so no separate `"tsx"` registration is needed here, consistent with how `_walk_ast` and `_extract_import_name` both already treat tsx as a typescript alias.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestJsFamilyGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract JS/TS module globals and class fields"
```

---

## Task 15: Rust + Go + C globals/fields

**Files:** Modify `mcp_server.py` (three new functions); Test: `tests/test_mcp_server.py` (new `TestRustGoCGlobalsAndFields`)

**Grammar facts:** none of these three languages has a "static struct field" concept — struct/field declarations are always instance-only, so every field in these languages is captured with `is_static=False` unconditionally.
- Rust: `static X: T = v;`/`const X: T = v;` at module level are `static_item`/`const_item` (`field:name`). `struct Foo { field: T }` is `struct_item` with `field:body` = `field_declaration_list`, each member a `field_declaration` (`field:name`). An `impl Foo { const ASSOC: T = v; }` block's `const_item` (nested inside `impl_item > declaration_list`) is treated as a **static** field of `Foo` (the closest Rust analog to a class-static constant) — the one exception to "always instance" in this task.
- Go: `var X = v`/`const X = v` at file level are `var_declaration`/`const_declaration`, each containing a `var_spec`/`const_spec` (`field:name`). `type Foo struct { Field T }` is `type_declaration > type_spec` (`field:name`=`Foo`, `field:type`=`struct_type` with `field:body`=`field_declaration_list`, each member `field_declaration` with `field:name`).
- C: top-level `int x = 5;` is `declaration` directly under `translation_unit` (`field:declarator` → `init_declarator` → `field:declarator` = plain `identifier`, or a bare `identifier` declarator with no initializer). `struct Foo { int field; };` is `struct_specifier` with `field:body` = `field_declaration_list`, each member `field_declaration` with `field:declarator` = `field_identifier`.

- [ ] **Step 1: Write the failing tests**

```python
class TestRustGoCGlobalsAndFields:
    def _rust_parser(self):
        import tree_sitter_rust
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_rust.language()))

    def _go_parser(self):
        import tree_sitter_go
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_go.language()))

    def _c_parser(self):
        import tree_sitter_c
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_c.language()))

    def test_rust_static_and_const_are_globals(self):
        import mcp_server
        source = b"static GLOBAL_X: i32 = 5;\nconst GLOBAL_Y: i32 = 10;\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"GLOBAL_X", "GLOBAL_Y"}

    def test_rust_struct_field_is_instance_only(self):
        import mcp_server
        source = b"struct Foo {\n    instance_field: i32,\n}\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instance_field"] == ("Foo", False)

    def test_rust_impl_const_is_static_field(self):
        import mcp_server
        source = b"impl Foo {\n    const ASSOC_CONST: i32 = 1;\n}\n"
        tree = self._rust_parser().parse(source)
        result = mcp_server._extract_rust_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["ASSOC_CONST"] == ("Foo", True)

    def test_go_package_level_var_and_const_are_globals(self):
        import mcp_server
        source = b"package main\n\nvar GlobalX = 5\nconst GlobalY = 10\n"
        tree = self._go_parser().parse(source)
        result = mcp_server._extract_go_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"GlobalX", "GlobalY"}

    def test_go_struct_field_is_instance_only(self):
        import mcp_server
        source = b"package main\n\ntype Foo struct {\n    InstanceField int\n}\n"
        tree = self._go_parser().parse(source)
        result = mcp_server._extract_go_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["InstanceField"] == ("Foo", False)

    def test_c_file_scope_declaration_is_global(self):
        import mcp_server
        source = b"int global_x = 5;\nstatic int file_static_x = 10;\n"
        tree = self._c_parser().parse(source)
        result = mcp_server._extract_c_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"global_x", "file_static_x"}

    def test_c_struct_field_is_instance_only(self):
        import mcp_server
        source = b"struct Foo {\n    int instance_field;\n};\n"
        tree = self._c_parser().parse(source)
        result = mcp_server._extract_c_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instance_field"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestRustGoCGlobalsAndFields -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement**

```python
def _extract_rust_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    for stmt in root_node.children:
        if stmt.type in ("static_item", "const_item"):
            name_node = stmt.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "struct_item":
            struct_name_node = stmt.child_by_field_name("name")
            struct_name = struct_name_node.text.decode("utf-8") if struct_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    if member.type == "field_declaration":
                        fname_node = member.child_by_field_name("name")
                        if fname_node is not None:
                            fname = fname_node.text.decode("utf-8")
                            fields.append((fname, struct_name, False))
                            field_info[fname] = {
                                "class": struct_name, "static": False,
                                "body": member.text.decode("utf-8", "replace"),
                            }
        elif stmt.type == "impl_item":
            type_node = stmt.child_by_field_name("type")
            type_name = type_node.text.decode("utf-8") if type_node is not None else ""
            body = stmt.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    if member.type == "const_item":
                        cname_node = member.child_by_field_name("name")
                        if cname_node is not None:
                            cname = cname_node.text.decode("utf-8")
                            fields.append((cname, type_name, True))
                            field_info[cname] = {
                                "class": type_name, "static": True,
                                "body": member.text.decode("utf-8", "replace"),
                            }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


def _extract_go_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    for stmt in root_node.children:
        if stmt.type in ("var_declaration", "const_declaration"):
            spec_type = "var_spec" if stmt.type == "var_declaration" else "const_spec"
            for spec in stmt.children:
                if spec.type == spec_type:
                    name_node = spec.child_by_field_name("name")
                    if name_node is not None:
                        name = name_node.text.decode("utf-8")
                        globals_.append(name)
                        global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "type_declaration":
            for type_spec in stmt.children:
                if type_spec.type != "type_spec":
                    continue
                type_name_node = type_spec.child_by_field_name("name")
                type_name = type_name_node.text.decode("utf-8") if type_name_node else ""
                struct_type = type_spec.child_by_field_name("type")
                if struct_type is None or struct_type.type != "struct_type":
                    continue
                body = struct_type.child_by_field_name("body")
                if body is None:
                    continue
                for member in body.children:
                    if member.type == "field_declaration":
                        fname_node = member.child_by_field_name("name")
                        if fname_node is not None:
                            fname = fname_node.text.decode("utf-8")
                            fields.append((fname, type_name, False))
                            field_info[fname] = {
                                "class": type_name, "static": False,
                                "body": member.text.decode("utf-8", "replace"),
                            }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


def _extract_c_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def declarator_name(node: Any) -> Optional[str]:
        if node.type == "identifier":
            return node.text.decode("utf-8")
        if node.type == "init_declarator":
            inner = node.child_by_field_name("declarator")
            return declarator_name(inner) if inner is not None else None
        return None

    for stmt in root_node.children:
        if stmt.type == "declaration":
            declarator = stmt.child_by_field_name("declarator")
            if declarator is not None:
                name = declarator_name(declarator)
                if name:
                    globals_.append(name)
                    global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "struct_specifier":
            struct_name_node = stmt.child_by_field_name("name")
            struct_name = struct_name_node.text.decode("utf-8") if struct_name_node else ""
            body = stmt.child_by_field_name("body")
            if body is not None:
                for member in body.children:
                    if member.type == "field_declaration":
                        declarator = member.child_by_field_name("declarator")
                        if declarator is not None and declarator.type == "field_identifier":
                            fname = declarator.text.decode("utf-8")
                            fields.append((fname, struct_name, False))
                            field_info[fname] = {
                                "class": struct_name, "static": False,
                                "body": member.text.decode("utf-8", "replace"),
                            }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["rust"] = _extract_rust_globals_and_fields
_GLOBAL_FIELD_EXTRACTORS["go"] = _extract_go_globals_and_fields
_GLOBAL_FIELD_EXTRACTORS["c"] = _extract_c_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestRustGoCGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Rust/Go/C module globals and struct fields"
```

---

## Task 16: Java + C# globals/fields

**Files:** Modify `mcp_server.py` (two new functions); Test: `tests/test_mcp_server.py` (new `TestJavaCSharpGlobalsAndFields`)

**Grammar facts:** neither language has true top-level globals (all state lives inside a class) — both extractors return `"globals": []` unconditionally. Java: `class_declaration` field:body=`class_body`; members are `field_declaration`, optionally preceded by a `modifiers` node (one wrapper node containing `static`/`public`/etc. as separate children) — static iff any descendant of that `modifiers` node has type `"static"`; name via `field:declarator` → `variable_declarator` → `field:name`. C#: `class_declaration` field:body=`declaration_list`; members are `field_declaration`, but modifiers are direct sibling children of type `modifier` (not wrapped) — static iff any child has type `"modifier"` and text `b"static"`; name via `field:declarator` (well, C#'s `field_declaration` wraps a `variable_declaration` child, itself containing `variable_declarator` with `field:name` — no field-name on the `variable_declaration` step itself per the verified dump, so locate it by type, not field).

- [ ] **Step 1: Write the failing tests**

```python
class TestJavaCSharpGlobalsAndFields:
    def _java_parser(self):
        import tree_sitter_java
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_java.language()))

    def _csharp_parser(self):
        import tree_sitter_c_sharp
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_c_sharp.language()))

    def test_java_static_and_instance_fields(self):
        import mcp_server
        source = b"public class Foo {\n    static int staticField = 1;\n    int instanceField = 2;\n}\n"
        tree = self._java_parser().parse(source)
        result = mcp_server._extract_java_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
        assert result["globals"] == []

    def test_csharp_static_and_instance_fields(self):
        import mcp_server
        source = b"public class Foo {\n    static int staticField = 1;\n    int instanceField = 2;\n}\n"
        tree = self._csharp_parser().parse(source)
        result = mcp_server._extract_csharp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
        assert result["globals"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestJavaCSharpGlobalsAndFields -v`
Expected: FAIL — functions don't exist. If the C# `variable_declaration`/`variable_declarator` lookup-by-type below turns out to need adjustment against the real grammar version installed, dump the AST for the fixture source first (`tree.root_node.sexp()` or a manual recursive print, same technique used to ground this plan's other language facts) rather than guessing further.

- [ ] **Step 3: Implement**

```python
def _extract_java_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def walk_class(class_node: Any) -> None:
        name_node = class_node.child_by_field_name("name")
        class_name = name_node.text.decode("utf-8") if name_node else ""
        body = class_node.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type != "field_declaration":
                continue
            is_static = False
            for child in member.children:
                if child.type == "modifiers":
                    is_static = any(mod.type == "static" for mod in child.children)
            declarator = member.child_by_field_name("declarator")
            if declarator is not None and declarator.type == "variable_declarator":
                fname_node = declarator.child_by_field_name("name")
                if fname_node is not None:
                    fname = fname_node.text.decode("utf-8")
                    fields.append((fname, class_name, is_static))
                    field_info[fname] = {
                        "class": class_name, "static": is_static,
                        "body": member.text.decode("utf-8", "replace"),
                    }

    def walk(node: Any) -> None:
        if node.type == "class_declaration":
            walk_class(node)
        for child in node.children:
            walk(child)

    walk(root_node)
    return {"globals": [], "global_bodies": {}, "fields": fields, "field_info": field_info}


def _extract_csharp_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def walk_class(class_node: Any) -> None:
        name_node = class_node.child_by_field_name("name")
        class_name = name_node.text.decode("utf-8") if name_node else ""
        body = class_node.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type != "field_declaration":
                continue
            is_static = any(c.type == "modifier" and c.text == b"static" for c in member.children)
            var_decl = next((c for c in member.children if c.type == "variable_declaration"), None)
            if var_decl is None:
                continue
            for declarator in var_decl.children:
                if declarator.type == "variable_declarator":
                    fname_node = declarator.child_by_field_name("name")
                    if fname_node is not None:
                        fname = fname_node.text.decode("utf-8")
                        fields.append((fname, class_name, is_static))
                        field_info[fname] = {
                            "class": class_name, "static": is_static,
                            "body": member.text.decode("utf-8", "replace"),
                        }

    def walk(node: Any) -> None:
        if node.type == "class_declaration":
            walk_class(node)
        for child in node.children:
            walk(child)

    walk(root_node)
    return {"globals": [], "global_bodies": {}, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["java"] = _extract_java_globals_and_fields
_GLOBAL_FIELD_EXTRACTORS["c_sharp"] = _extract_csharp_globals_and_fields
```

Note both `walk` helpers recurse into every node (not just direct children of the root), unlike Tasks 13-15's strictly-direct-children approach — this is still safe scope-wise because `class_declaration` is itself a structural node (same non-ambiguity argument as functions/classes in `_walk_ast`), and nested classes are a real, valid Java/C# construct worth capturing fields from; the recursion only ever *enters* a `field_declaration`'s own member list, never a method body, so the "don't misclassify locals" invariant still holds.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestJavaCSharpGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Java/C# class fields (static via modifier inspection)"
```

---

## Task 17: C++ globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_cpp_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestCppGlobalsAndFields`)

**Grammar facts:** top-level `int x = 5;` is a `declaration` directly under `translation_unit`, same shape as C (reuse the same `declarator_name` logic as Task 15's C extractor). `class Foo { ... };`/`struct Foo { ... };` are `class_specifier`/`struct_specifier` with `field:body` = `field_declaration_list`; each member `field_declaration` optionally has a `storage_class_specifier` child with text `"static"`; the declarator is directly a `field_identifier` (not wrapped in `init_declarator`, per the verified dump — unlike C's free variable declarations).

- [ ] **Step 1: Write the failing tests**

```python
class TestCppGlobalsAndFields:
    def _cpp_parser(self):
        import tree_sitter_cpp
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_cpp.language()))

    def test_top_level_declaration_is_global(self):
        import mcp_server
        tree = self._cpp_parser().parse(b"int global_x = 5;\n")
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        assert "global_x" in result["globals"]

    def test_static_and_instance_class_fields(self):
        import mcp_server
        source = b"class Foo {\npublic:\n    static int staticField;\n    int instanceField;\n};\n"
        tree = self._cpp_parser().parse(source)
        result = mcp_server._extract_cpp_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestCppGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_cpp_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def declarator_name(node: Any) -> Optional[str]:
        if node.type == "identifier":
            return node.text.decode("utf-8")
        if node.type == "init_declarator":
            inner = node.child_by_field_name("declarator")
            return declarator_name(inner) if inner is not None else None
        return None

    for stmt in root_node.children:
        if stmt.type == "declaration":
            declarator = stmt.child_by_field_name("declarator")
            if declarator is not None:
                name = declarator_name(declarator)
                if name:
                    globals_.append(name)
                    global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type in ("class_specifier", "struct_specifier"):
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type != "field_declaration":
                    continue
                is_static = any(c.type == "storage_class_specifier" and c.text == b"static" for c in member.children)
                declarator = member.child_by_field_name("declarator")
                if declarator is not None and declarator.type == "field_identifier":
                    fname = declarator.text.decode("utf-8")
                    fields.append((fname, class_name, is_static))
                    field_info[fname] = {
                        "class": class_name, "static": is_static,
                        "body": member.text.decode("utf-8", "replace"),
                    }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["cpp"] = _extract_cpp_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestCppGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract C++ module globals and class/struct fields"
```

---

## Task 18: Ruby globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_ruby_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestRubyGlobalsAndFields`)

**Grammar facts:** the grammar itself distinguishes these via distinct node types (no heuristic needed): top-level `assignment` with `field:left` of type `global_variable` (`$x`) or `constant` (`CONST_VAR`) → global. Inside a `class`'s `field:body` (`body_statement`), a direct-child `assignment` with `field:left` of type `class_variable` (`@@x`) → static field. Inside a `method` named `initialize` (itself a direct child of the class's `body_statement`), its own `field:body`'s direct-child `assignment`s with `field:left` of type `instance_variable` (`@x`) → instance field.

- [ ] **Step 1: Write the failing tests**

```python
class TestRubyGlobalsAndFields:
    def _parser(self):
        import tree_sitter_ruby
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_ruby.language()))

    def test_global_variable_and_constant_are_globals(self):
        import mcp_server
        source = b"$global_var = 5\nCONST_VAR = 10\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        assert set(result["globals"]) == {"$global_var", "CONST_VAR"}

    def test_class_variable_is_static_instance_variable_in_initialize_is_not(self):
        import mcp_server
        source = b"class Foo\n  @@class_var = 1\n  def initialize\n    @instance_var = 2\n  end\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_ruby_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["@@class_var"] == ("Foo", True)
        assert info["@instance_var"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestRubyGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_ruby_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    for stmt in root_node.children:
        if stmt.type == "assignment":
            left = stmt.child_by_field_name("left")
            if left is not None and left.type in ("global_variable", "constant"):
                name = left.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "class":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type == "assignment":
                    left = member.child_by_field_name("left")
                    if left is not None and left.type == "class_variable":
                        fname = left.text.decode("utf-8")
                        fields.append((fname, class_name, True))
                        field_info[fname] = {
                            "class": class_name, "static": True,
                            "body": member.text.decode("utf-8", "replace"),
                        }
                elif member.type == "method":
                    method_name_node = member.child_by_field_name("name")
                    if method_name_node is not None and method_name_node.text == b"initialize":
                        method_body = member.child_by_field_name("body")
                        if method_body is not None:
                            for inner in method_body.children:
                                if inner.type == "assignment":
                                    left = inner.child_by_field_name("left")
                                    if left is not None and left.type == "instance_variable":
                                        fname = left.text.decode("utf-8")
                                        fields.append((fname, class_name, False))
                                        field_info[fname] = {
                                            "class": class_name, "static": False,
                                            "body": inner.text.decode("utf-8", "replace"),
                                        }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["ruby"] = _extract_ruby_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestRubyGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Ruby globals/constants and class/instance variables"
```

---

## Task 19: PHP globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_php_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestPhpGlobalsAndFields`)

**Grammar facts:** top-level `$x = 5;` is `expression_statement > assignment_expression` with `field:left` = `variable_name`. `class Foo { ... }` is `class_declaration` with `field:body` = `declaration_list`; each member is a `property_declaration` containing an optional `static_modifier` child and one or more `property_element` children, each with `field:name` = `variable_name`.

- [ ] **Step 1: Write the failing tests**

```python
class TestPhpGlobalsAndFields:
    def _parser(self):
        import tree_sitter_php
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_php.language_php()))

    def test_top_level_variable_is_global(self):
        import mcp_server
        source = b"<?php\n$globalVar = 5;\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        assert "$globalVar" in result["globals"]

    def test_static_and_instance_properties(self):
        import mcp_server
        source = b"<?php\nclass Foo {\n    public static $staticField = 1;\n    public $instanceField = 2;\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_php_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["$staticField"] == ("Foo", True)
        assert info["$instanceField"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestPhpGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_php_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    for stmt in root_node.children:
        if stmt.type == "expression_statement" and stmt.child_count > 0:
            expr = stmt.children[0]
            if expr.type == "assignment_expression":
                left = expr.child_by_field_name("left")
                if left is not None and left.type == "variable_name":
                    name = left.text.decode("utf-8")
                    globals_.append(name)
                    global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "class_declaration":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type != "property_declaration":
                    continue
                is_static = any(c.type == "static_modifier" for c in member.children)
                for elem in member.children:
                    if elem.type == "property_element":
                        name_node = elem.child_by_field_name("name")
                        if name_node is not None:
                            fname = name_node.text.decode("utf-8")
                            fields.append((fname, class_name, is_static))
                            field_info[fname] = {
                                "class": class_name, "static": is_static,
                                "body": member.text.decode("utf-8", "replace"),
                            }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["php"] = _extract_php_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestPhpGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract PHP globals and class properties"
```

---

## Task 20: Kotlin globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_kotlin_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestKotlinGlobalsAndFields`)

**Grammar facts:** top-level `val`/`var` is a `property_declaration` directly under `source_file`, with a `variable_declaration` child (itself containing a plain `identifier` child — neither step exposes a named field per the verified dump, so both must be located by node type). `class Foo { ... }` is `class_declaration` containing a `class_body` child (also not exposed via a named field in this grammar version); a `property_declaration` that is a **direct** child of `class_body` is an instance field; one nested inside a `companion_object`'s own `class_body` is Kotlin's static-equivalent.

- [ ] **Step 1: Write the failing tests**

```python
class TestKotlinGlobalsAndFields:
    def _parser(self):
        import tree_sitter_kotlin
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_kotlin.language()))

    def test_top_level_property_is_global(self):
        import mcp_server
        tree = self._parser().parse(b"val globalX = 5\n")
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_companion_object_property_is_static_plain_is_instance(self):
        import mcp_server
        source = b"class Foo {\n    companion object {\n        val staticField = 1\n    }\n    val instanceField = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_kotlin_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestKotlinGlobalsAndFields -v`
Expected: FAIL — function doesn't exist. If `variable_declaration`'s inner `identifier` lookup-by-type doesn't match the installed grammar version exactly, dump the AST for `"val globalX = 5\n"` first and adjust — this task's field-name gaps were flagged explicitly above because the earlier live dump didn't show named fields at this level.

- [ ] **Step 3: Implement**

```python
def _extract_kotlin_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def property_name(prop_node: Any) -> Optional[str]:
        for child in prop_node.children:
            if child.type == "variable_declaration":
                for inner in child.children:
                    if inner.type == "identifier":
                        return inner.text.decode("utf-8")
        return None

    for stmt in root_node.children:
        if stmt.type == "property_declaration":
            name = property_name(stmt)
            if name:
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "class_declaration":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            class_body = next((c for c in stmt.children if c.type == "class_body"), None)
            if class_body is None:
                continue
            for member in class_body.children:
                if member.type == "property_declaration":
                    fname = property_name(member)
                    if fname:
                        fields.append((fname, class_name, False))
                        field_info[fname] = {
                            "class": class_name, "static": False,
                            "body": member.text.decode("utf-8", "replace"),
                        }
                elif member.type == "companion_object":
                    companion_body = next((c for c in member.children if c.type == "class_body"), None)
                    if companion_body is None:
                        continue
                    for inner_member in companion_body.children:
                        if inner_member.type == "property_declaration":
                            fname = property_name(inner_member)
                            if fname:
                                fields.append((fname, class_name, True))
                                field_info[fname] = {
                                    "class": class_name, "static": True,
                                    "body": inner_member.text.decode("utf-8", "replace"),
                                }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["kotlin"] = _extract_kotlin_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestKotlinGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Kotlin top-level properties and class/companion fields"
```

---

## Task 21: Swift globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_swift_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestSwiftGlobalsAndFields`)

**Grammar facts:** top-level `let`/`var` is `property_declaration` directly under `source_file`; `field:name` → `pattern` → `field:bound_identifier` → `simple_identifier` holds the actual name. `class Foo { ... }` is `class_declaration` with `field:body` = `class_body`; members are `property_declaration`, static iff it has a `modifiers` child containing a `property_modifier` child with text `"static"`.

- [ ] **Step 1: Write the failing tests**

```python
class TestSwiftGlobalsAndFields:
    def _parser(self):
        import tree_sitter_swift
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_swift.language()))

    def test_top_level_let_is_global(self):
        import mcp_server
        tree = self._parser().parse(b"let globalX = 5\n")
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_static_and_instance_properties(self):
        import mcp_server
        source = b"class Foo {\n    static var staticField = 1\n    var instanceField = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_swift_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["staticField"] == ("Foo", True)
        assert info["instanceField"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestSwiftGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_swift_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def property_name(prop_node: Any) -> Optional[str]:
        pattern = prop_node.child_by_field_name("name")
        if pattern is None:
            return None
        bound = pattern.child_by_field_name("bound_identifier")
        return bound.text.decode("utf-8") if bound is not None else None

    for stmt in root_node.children:
        if stmt.type == "property_declaration":
            name = property_name(stmt)
            if name:
                globals_.append(name)
                global_bodies[name] = stmt.text.decode("utf-8", "replace")
        elif stmt.type == "class_declaration":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type != "property_declaration":
                    continue
                is_static = False
                for child in member.children:
                    if child.type == "modifiers":
                        is_static = any(
                            m.type == "property_modifier" and m.text == b"static"
                            for m in child.children
                        )
                fname = property_name(member)
                if fname:
                    fields.append((fname, class_name, is_static))
                    field_info[fname] = {
                        "class": class_name, "static": is_static,
                        "body": member.text.decode("utf-8", "replace"),
                    }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["swift"] = _extract_swift_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestSwiftGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Swift top-level and class properties"
```

---

## Task 22: Scala globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_scala_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestScalaGlobalsAndFields`)

**Grammar facts:** `class Foo { val instanceField = 2 }` is `class_definition` with `field:body` = `template_body`, containing `val_definition`/`var_definition` members (`field:pattern` holds the plain `identifier`) — always instance (`:static=False`), since Scala classes have no static-member concept.

**Deliberate simplification** (document, don't chase companion-object pairing): Scala's closest analog to "static" is a companion `object` sharing a class's name, but matching an `object_definition` to its companion `class_definition` by name — and only then treating its members as that class's static fields — is real extra complexity for a niche pattern. Instead: a top-level `object_definition` (direct child of `compilation_unit`) is treated as a **globals namespace**, not a fields-owner — its `val_definition`/`var_definition` members are extracted as plain module-level globals (no `:class` edge, no `:static` concept invoked at all), sidestepping the need to resolve companion pairing or risk a dangling `:class` edge to a non-existent class ident. A *nested* `object_definition` (inside a class) is out of scope for this task.

- [ ] **Step 1: Write the failing tests**

```python
class TestScalaGlobalsAndFields:
    def _parser(self):
        import tree_sitter_scala
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_scala.language()))

    def test_top_level_object_members_are_globals(self):
        import mcp_server
        source = b"object Globals {\n  val globalX = 5\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_class_members_are_instance_fields(self):
        import mcp_server
        source = b"class Foo {\n  val instanceField = 2\n}\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_scala_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["instanceField"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestScalaGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_scala_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def definition_name(defn_node: Any) -> Optional[str]:
        pattern = defn_node.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "identifier":
            return pattern.text.decode("utf-8")
        return None

    for stmt in root_node.children:
        if stmt.type == "object_definition":
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type in ("val_definition", "var_definition"):
                    name = definition_name(member)
                    if name:
                        globals_.append(name)
                        global_bodies[name] = member.text.decode("utf-8", "replace")
        elif stmt.type == "class_definition":
            name_node = stmt.child_by_field_name("name")
            class_name = name_node.text.decode("utf-8") if name_node else ""
            body = stmt.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type in ("val_definition", "var_definition"):
                    fname = definition_name(member)
                    if fname:
                        fields.append((fname, class_name, False))
                        field_info[fname] = {
                            "class": class_name, "static": False,
                            "body": member.text.decode("utf-8", "replace"),
                        }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["scala"] = _extract_scala_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestScalaGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Scala top-level object globals and class fields"
```

---

## Task 23: Haskell globals/fields

**Files:** Modify `mcp_server.py` (new `_extract_haskell_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestHaskellGlobalsAndFields`)

**Grammar facts:** the root node's `field:declarations` holds a `declarations` node whose children include `signature`/`bind`/`data_type` (among others). A zero-argument `bind` node (`field:name` = `variable`) is cleanly distinguished by the grammar itself from a parameterized `function` node (which `_LANG_NODE_TYPES["haskell"]["functions"]` already targets) — this is the "global value" case. `data Foo = Foo { fieldA :: Int }` is `data_type` (`field:name` = the type name) → `field:constructors` → `data_constructors` → each `data_constructor` (`field:constructor`) → if that constructor is a `record` node → `field:fields` → `fields` → each `field` child (`field:field`) → `field:name` → `field_name` → `variable` (the actual field name text). Always `:static=False` (no OOP concept in Haskell).

- [ ] **Step 1: Write the failing tests**

```python
class TestHaskellGlobalsAndFields:
    def _parser(self):
        import tree_sitter_haskell
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_haskell.language()))

    def test_zero_arg_bind_is_global(self):
        import mcp_server
        source = b"globalX :: Int\nglobalX = 5\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_record_fields_extracted(self):
        import mcp_server
        source = b"data Foo = Foo { fieldA :: Int, fieldB :: String }\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_haskell_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["fieldA"] == ("Foo", False)
        assert info["fieldB"] == ("Foo", False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestHaskellGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_haskell_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    declarations = root_node.child_by_field_name("declarations")
    if declarations is None:
        return {"globals": [], "global_bodies": {}, "fields": [], "field_info": {}}

    for decl in declarations.children:
        if decl.type == "bind":
            name_node = decl.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                globals_.append(name)
                global_bodies[name] = decl.text.decode("utf-8", "replace")
        elif decl.type == "data_type":
            type_name_node = decl.child_by_field_name("name")
            type_name = type_name_node.text.decode("utf-8") if type_name_node else ""
            constructors = decl.child_by_field_name("constructors")
            if constructors is None:
                continue
            for ctor_wrapper in constructors.children:
                if ctor_wrapper.type != "data_constructor":
                    continue
                ctor = ctor_wrapper.child_by_field_name("constructor")
                if ctor is None or ctor.type != "record":
                    continue
                fields_node = ctor.child_by_field_name("fields")
                if fields_node is None:
                    continue
                for field_wrapper in fields_node.children:
                    if field_wrapper.type != "field":
                        continue
                    field_name_node = field_wrapper.child_by_field_name("name")
                    if field_name_node is None:
                        continue
                    variable_node = next(
                        (c for c in field_name_node.children if c.type == "variable"), None
                    )
                    if variable_node is not None:
                        fname = variable_node.text.decode("utf-8")
                        fields.append((fname, type_name, False))
                        field_info[fname] = {
                            "class": type_name, "static": False,
                            "body": field_wrapper.text.decode("utf-8", "replace"),
                        }

    return {"globals": globals_, "global_bodies": global_bodies, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["haskell"] = _extract_haskell_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestHaskellGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Haskell top-level bindings and record fields"
```

---

## Task 24: Lua globals (no fields — no class concept)

**Files:** Modify `mcp_server.py` (new `_extract_lua_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestLuaGlobalsAndFields`)

**Grammar facts:** `_LANG_NODE_TYPES["lua"]["classes"]` is already `set()` — Lua has no class node type at all (table-based OOP is a library convention, not a grammar construct), so fields are always empty for Lua, consistent with that existing precedent. A true top-level global is `assignment_statement` directly under `chunk` whose `variable_list` holds a single plain `identifier` (not a `dot_index_expression`, which is a table-field write like `Foo.staticField = 1`, deliberately excluded — no class entity exists for it to attach to) and which is **not** wrapped in a `variable_declaration` (that wrapper node is what `local` produces — its presence means the name is function/file-local, not a true global).

- [ ] **Step 1: Write the failing tests**

```python
class TestLuaGlobalsAndFields:
    def _parser(self):
        import tree_sitter_lua
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_lua.language()))

    def test_true_global_assignment(self):
        import mcp_server
        tree = self._parser().parse(b"globalX = 5\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert "globalX" in result["globals"]

    def test_local_declaration_not_captured(self):
        import mcp_server
        tree = self._parser().parse(b"local localY = 10\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == []

    def test_table_field_assignment_not_captured(self):
        import mcp_server
        tree = self._parser().parse(b"Foo = {}\nFoo.staticField = 1\n")
        result = mcp_server._extract_lua_globals_and_fields(tree.root_node)
        assert result["globals"] == ["Foo"]
        assert result["fields"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestLuaGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_lua_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    globals_: List[str] = []
    global_bodies: Dict[str, str] = {}

    for stmt in root_node.children:
        if stmt.type != "assignment_statement":
            continue
        var_list = next((c for c in stmt.children if c.type == "variable_list"), None)
        if var_list is None:
            continue
        name_node = var_list.child_by_field_name("name")
        if name_node is not None and name_node.type == "identifier":
            name = name_node.text.decode("utf-8")
            globals_.append(name)
            global_bodies[name] = stmt.text.decode("utf-8", "replace")

    return {"globals": globals_, "global_bodies": global_bodies, "fields": [], "field_info": {}}


_GLOBAL_FIELD_EXTRACTORS["lua"] = _extract_lua_globals_and_fields
```

(A `local x = 5` statement parses as a `variable_declaration` wrapping an `assignment_statement`, per the verified dump — since this loop only matches `assignment_statement` nodes that are *direct* children of `chunk`, a `local`-wrapped one is automatically excluded without needing an explicit check, because its actual `assignment_statement` is one level deeper, inside the `variable_declaration` wrapper.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestLuaGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Lua true global variable assignments"
```

---

## Task 25: Elixir fields (no globals — module attributes only)

**Files:** Modify `mcp_server.py` (new `_extract_elixir_globals_and_fields`); Test: `tests/test_mcp_server.py` (new `TestElixirGlobalsAndFields`)

**Grammar facts:** no top-level mutable globals exist in Elixir outside module attributes, so `"globals"` is always empty. A module is `call` with `field:target` `identifier` text `"defmodule"`, `field:arguments` holding an `alias` node (the module name), and a `do_block` child. A module attribute (`@module_attr 5`) is a `unary_operator` with `field:operator` = `"@"` and `field:operand` = a `call` node whose `field:target` holds the attribute's name — treated as a `:static=True` field of the enclosing module (the closest Elixir analog to compile-time class-scoped state).

- [ ] **Step 1: Write the failing tests**

```python
class TestElixirGlobalsAndFields:
    def _parser(self):
        import tree_sitter_elixir
        from tree_sitter import Language, Parser
        return Parser(Language(tree_sitter_elixir.language()))

    def test_module_attribute_is_static_field(self):
        import mcp_server
        source = b"defmodule Foo do\n  @module_attr 5\nend\n"
        tree = self._parser().parse(source)
        result = mcp_server._extract_elixir_globals_and_fields(tree.root_node)
        info = {n: (c, s) for n, c, s in result["fields"]}
        assert info["module_attr"] == ("Foo", True)
        assert result["globals"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestElixirGlobalsAndFields -v`
Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement**

```python
def _extract_elixir_globals_and_fields(root_node: Any) -> Dict[str, Any]:
    fields: List[Tuple[str, str, bool]] = []
    field_info: Dict[str, Dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if node.type == "call":
            target = node.child_by_field_name("target")
            if target is not None and target.type == "identifier" and target.text == b"defmodule":
                arguments = node.child_by_field_name("arguments")
                module_name = ""
                if arguments is not None:
                    alias_node = next((c for c in arguments.children if c.type == "alias"), None)
                    if alias_node is not None:
                        module_name = alias_node.text.decode("utf-8")
                do_block = next((c for c in node.children if c.type == "do_block"), None)
                if do_block is not None:
                    for member in do_block.children:
                        if member.type == "unary_operator":
                            op = member.child_by_field_name("operator")
                            operand = member.child_by_field_name("operand")
                            if op is not None and op.text == b"@" and operand is not None and operand.type == "call":
                                attr_target = operand.child_by_field_name("target")
                                if attr_target is not None:
                                    fname = attr_target.text.decode("utf-8")
                                    fields.append((fname, module_name, True))
                                    field_info[fname] = {
                                        "class": module_name, "static": True,
                                        "body": member.text.decode("utf-8", "replace"),
                                    }
        for child in node.children:
            walk(child)

    walk(root_node)
    return {"globals": [], "global_bodies": {}, "fields": fields, "field_info": field_info}


_GLOBAL_FIELD_EXTRACTORS["elixir"] = _extract_elixir_globals_and_fields
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k TestElixirGlobalsAndFields -v`
Expected: PASS

- [ ] **Step 5: Run the full Component 3 test sweep**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "GlobalsAndFields" -v`
Expected: All 13 language-task test classes PASS. Also run the full suite once, since Component 3 touched `_extract_from_source`'s shared return shape (Task 12) which every other language's existing function/class tests also exercise: `.venv/bin/pytest tests/test_mcp_server.py -q`

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extract Elixir module attributes as static fields"
```

---

## Task 26: Extend matcher pools to globals/fields

**Files:**
- Modify: all 13 per-language extractor functions from Tasks 13-25 (retrofit — see below)
- Modify: `mcp_server.py` (`_extract_commit`'s pool-building, from Task 9)
- Test: `tests/test_mcp_server.py` (extend `TestExtractCommitRename` with a global-rename case)

**Required retrofit before this task's own work**: Tasks 13-25's extractor functions return body *text* only (`global_bodies`/`field_info[...]["body"]`), by design (Task 6 established that `_extract_from_source`'s return must stay plain-data since it crosses the `ProcessPoolExecutor` boundary). But the matcher (Tasks 7-9) needs live tree-sitter *nodes*, and those nodes only exist transiently, inside this same worker process, at the moment each extractor walks its tree. Go back to **each of the 13 functions from Tasks 13-25** and add two more keys to their returned dict, captured for free during the same walk (the node is already in hand right before each `.text.decode(...)` call):

```python
    return {
        "globals": globals_, "global_bodies": global_bodies,
        "fields": fields, "field_info": field_info,
        "global_nodes": global_nodes,   # Dict[str, Any] — name -> node, parallel to global_bodies
        "field_nodes": field_nodes,     # Dict[str, Any] — qualified "Class.name" -> node, parallel to field_info
    }
```

(`global_nodes[name] = <the same node whose .text was decoded into global_bodies[name]>`, populated at the same call site; `field_nodes[f"{class_name}.{field_name}"] = <the field's own node>`, matching the qualified-name convention already used for field idents in Task 11.) Also update `_extract_globals_and_fields`'s empty-result stub (Task 12) to include the two new empty keys, and its dispatch-test fixture (`test_dispatches_to_registered_language_extractor`) sentinel dict to include them too.

**Interfaces:**
- Produces: `_extract_commit`'s removed/added pools (Task 9) gain `"variable"`/`"field"` categories alongside `"function"`/`"class"`, populated the same way (D/A get full pools, M gets the local diff, R gets both sides in full) — reusing `_extract_globals_and_fields` (called directly, not through `_extract_from_source`, so its `_nodes` keys are available) for both the new-content and old-content (via `_git_blob_content`, same as Task 9's function/class handling) sides.

- [ ] **Step 1: Write the failing test**

```python
    def test_global_rename_produces_renamed_pair(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "config.py").write_text("GLOBAL_X = 12345\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "config.py").write_text("GLOBAL_Y = 12345\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename global"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        _, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        assert ("variable", "config.py", "GLOBAL_X", "config.py", "GLOBAL_Y") in renamed_pairs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_global_rename_produces_renamed_pair -v`
Expected: FAIL — globals aren't pooled into the matcher yet.

- [ ] **Step 3: Implement**

In `_extract_commit` (as modified by Task 9), extend the pool initialization:

```python
    removed_pool: Dict[str, List[Tuple[str, Any]]] = {"function": [], "class": [], "variable": [], "field": []}
    added_pool: Dict[str, List[Tuple[str, Any]]] = {"function": [], "class": [], "variable": [], "field": []}
```

Add a small local helper right before the main `for status, ... in raw_entries:` loop, and call it everywhere Task 9 currently calls `_collect_entity_nodes` for the old/new sides:

```python
    def collect_all_nodes(root: Any, lang: str) -> Dict[str, Dict[str, Any]]:
        base = _collect_entity_nodes(root, lang)
        gf = _extract_globals_and_fields(root, "typescript" if lang == "tsx" else lang)
        base["variable"] = dict(gf.get("global_nodes", {}))
        base["field"] = dict(gf.get("field_nodes", {}))
        return base
```

Replace every `_collect_entity_nodes(...)` call inside the loop (both the old-content and new-content sides, per Task 9's D/A/R/M branches) with `collect_all_nodes(...)`, same arguments. Then extend every `for category in ("function", "class"):` loop in those same branches to `for category in ("function", "class", "variable", "field"):` — this covers the D-status pooling, the A-status pooling, the R-status both-sides pooling, and the M-status local-diff pooling, all four of which already iterate `("function", "class")` from Task 9 and just need the tuple widened.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_global_rename_produces_renamed_pair -v`
Expected: PASS. Also re-run all of Component 3's language tests plus Component 2's matcher tests to confirm the retrofit didn't break anything: `.venv/bin/pytest tests/test_mcp_server.py -k "GlobalsAndFields or TestMatchRenamedEntities or TestExtractCommit" -v`

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extend rename matcher pools to globals and fields"
```

---

## Task 27: Wire global/field rename triples

**Files:**
- Modify: `mcp_server.py:3194-3563` (`_run_ingestion`)
- Test: `tests/test_mcp_server.py` (new test in `TestRunIngestionBitemporalClose`)

**Interfaces:**
- Consumes: Task 26's widened `renamed_pairs` (now includes `"variable"`/`"field"` categories).
- Produces: no new code needed beyond what Task 10 already wrote — Task 10's `for category, old_file, old_name, new_file, new_name in renamed_pairs:` loop is already category-agnostic (`_code_ident(category, ...)` works identically for `"variable"`/`"field"` as it does for `"function"`/`"class"`). This task is verification-only: confirm end-to-end that a global/field rename produces the same `:renamed-from`/`:renamed-to` triples as a function rename, through the full `_run_ingestion` path (not just `_extract_commit` in isolation, as Task 26 tested).

- [ ] **Step 1: Write the failing test**

```python
    @pytest.mark.asyncio
    async def test_global_rename_links_via_rename_edges_end_to_end(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "config.py").write_text("GLOBAL_X = 12345\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add"], cwd=repo, check=True, capture_output=True)
        (repo / "config.py").write_text("GLOBAL_Y = 12345\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "rename global"], cwd=repo, check=True, capture_output=True)

        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(repo), "HEAD")

        old_ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_X")
        new_ident = mcp_server._code_ident("variable", "config.py", "GLOBAL_Y")

        assert any(f"{old_ident} :renamed-to {new_ident}" in t for t in close_triples_seen)
        transact_calls = " ".join(str(c) for c in db_instance.execute.call_args_list)
        assert f"{new_ident} :renamed-from {old_ident}" in transact_calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_global_rename_links_via_rename_edges_end_to_end -v`
Expected: Most likely PASSES already given Task 10 + Task 26's work — if so, this confirms the design (Task 27 needs no new production code). If it fails, the gap is almost certainly that `entity_descriptions`/`entity_valid_from` lookups in Task 10's loop assume a function/class-shaped ident that doesn't quite match a variable's — re-read Task 10's block and Task 11's `_code_ident("variable", ...)` construction side by side to find the mismatch before adding new code.

- [ ] **Step 3: (Conditional) Fix any gap found in Step 2**

Only if Step 2 failed: the most likely fix is in Task 10's block — `old_module_ident = _code_ident("module", old_file)` is computed unconditionally there for use in `_build_close_triples`'s third argument, which is correct for any category (a variable's `:contains` edge, like a function's, comes from its owning module) — verify this is actually what's failing before changing anything else.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k test_global_rename_links_via_rename_edges_end_to_end -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "test: verify end-to-end global/field rename linkage through _run_ingestion"
```

---

## Task 28: SKILL.md docs

**Files:** Modify `SKILL.md`

**Interfaces:** none (docs-only).

- [ ] **Step 1: Update entity-type documentation**

Find the section of `SKILL.md` documenting entity types (grep `:type/module`/`:type/function`/`:type/class` in `SKILL.md` to locate it) and add `:type/variable` and `:type/field` alongside the existing ones, including `:field`'s `:static`/`:class` attributes and the `:renamed-from`/`:renamed-to` attributes now present on all five code entity types (module, function, class, variable, field). Follow the existing doc's format exactly (same style used for `:type/module`'s attribute list).

- [ ] **Step 2: Commit**

```bash
git add SKILL.md
git commit -m "docs: document :type/variable, :type/field, and rename-tracking attributes"
```

---

## Task 29: Full suite regression + PR

**Files:** none (verification + PR only)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/pytest tests/test_mcp_server.py -q`
Expected: all tests pass, 0 regressions vs. the pre-feature baseline (352 passed, 38 skipped per the most recent prior baseline recorded in project memory — a higher passed-count is expected here given how many new tests this plan adds; investigate immediately if anything *fails* or if the skip count changes unexpectedly).

- [ ] **Step 2: Run with the wasm feature flag too, matching this repo's CI matrix**

Run: `.venv/bin/pytest tests/test_mcp_server.py -q --deselect ""` — actually just re-run plain `pytest -q` a second time if the project's CI config runs a `--features wasm`-equivalent Python-side flag; check `.github/workflows/*.yml` for the exact second CI invocation this repo uses (referenced in project memory as "6/6 checks... across Python 3.8-3.12") and mirror it locally before pushing.

- [ ] **Step 3: Open the PR**

Per the design spec's delivery decision (single PR closing both #111 and #113): create a branch, push, and open a PR with `Fixes #111` and `Fixes #113` both in the body so merging auto-closes both issues (see project memory's recorded lesson from #108's closure — a PR that resolves an issue must have the closing keyword in its own body, merging alone does not auto-close). Follow this repo's established pattern from the most recent several merges: branch name `fix-rename-tracking-111-113` (or similar), PR title referencing both issues, and expect the `REVIEW_REQUIRED` branch-protection gate this repo has hit on every recent PR — do not merge with `--admin` without the user's explicit go-ahead in that specific conversation.

---

## Self-Review

**Spec coverage:** every section of `docs/superpowers/specs/2026-07-14-git-ingestion-rename-tracking-design.md` maps to a task — Schema changes → Task 1; Component 1 → Tasks 3-5; Component 2 → Tasks 6-10; Component 3 → Tasks 11-25; Component 4 → Tasks 26-27; Docs → Task 28. All stated Non-goals (copy detection, `:modified-in` fix, retroactive backfill, local-variable entities, true lexical scope resolution) are honored by omission — no task introduces any of them.

**Placeholder scan:** no task contains "TBD"/"implement later"/"similar to Task N without code" — every step shows real code or a real, runnable command. Two tasks (9, 26) explicitly instruct going back to retrofit an earlier task's interface once a real caller exposed a gap — flagged inline as "Required retrofit," not hidden or deferred as future work.

**Type consistency:** `_code_ident(entity_type, file_path, name)` is used identically across Tasks 1, 5, 10, 11, 27 for all five entity type strings (`"module"`, `"function"`, `"class"`, `"variable"`, `"field"`). `renamed_pairs`' 5-tuple shape `(category, old_file, old_name, new_file, new_name)` is established in Task 9 and consumed unchanged by Tasks 10, 26, 27. The `(name, node)` pool-entry shape from Task 9 is reused unchanged by Task 26's widened categories. `_match_renamed_entities`'s return shape is corrected once, explicitly, in Task 9 (from `(category, old_name, new_name)` to `(category, old_name, old_node, new_name, new_node)`) — Task 8's own tests need that same correction applied before Task 9 can pass; this is called out explicitly rather than left as a silent mismatch.

---

**Plan complete and saved to `docs/superpowers/plans/2026-07-14-git-ingestion-rename-tracking.md`.** Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
