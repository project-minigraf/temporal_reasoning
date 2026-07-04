# External Dependency Labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Label real git submodules and genuinely-unresolved imports as `:type/external-dependency` instead of silently dropping them or leaving unmarked placeholders, while fixing the import-resolution gaps (Rust-only heuristic, per-language truncation, no relative-import support) that would otherwise make that labeling mislabel vendored in-tree code — plus an unrelated `.tsx` ingestion bug found during design.

**Architecture:** All changes live in `mcp_server.py` (the codebase's existing single-file convention for ingestion logic) and `tests/test_mcp_server.py`. No new modules. Four independently-testable slices, each ending in a green test suite: (1) `.tsx` parser-loading fix, (2) submodule schema + bi-temporal detection, (3) unresolved-import tagging using today's resolver, (4) generalized import resolution + relative imports, which also upgrades the tagging from (3) to stop mislabeling vendored code.

**Tech Stack:** Python 3.10+, tree-sitter grammars, minigraf (bi-temporal Datalog store), pytest + pytest-asyncio.

## Global Constraints

- Every new/changed function needs a docstring in the file's existing style (see `_build_close_triples`, `_preload_known_deps` for the pattern: state *why*, not what).
- No new exception-handling patterns — route through the existing `_ingest_close`/`_ingest_transact` helpers and match the file's `except Exception: pass` best-effort convention for git/parse failures.
- `os`, `re`, `Path`, `Tuple`, `Dict`, `List`, `Optional` are already imported at module level in `mcp_server.py` (lines 13, 14, 21, 22) — do not add local `import os`/`import re` inside functions (some existing functions do this redundantly; don't copy that pattern in new code).
- Run the full suite (`pytest tests/ -x -q`) at the end of every task, not just the new test — several tasks change a shared function's contract and must not silently break unrelated tests.

---

## File Structure

Only two files change in this plan:

- `mcp_server.py` — all production code changes (new helpers + modifications to `_build_parser`, `_LANG_NODE_TYPES`, `_extract_import_name`, `_c_include_name`, `_ruby_require_name`, `_resolve_module_import`, `_extract_commit`, `_preload_known_entities`, `_run_ingestion`).
- `tests/test_mcp_server.py` — new test classes plus updates to existing tests whose assertions encode the *old* (buggy) behavior this plan intentionally changes.

Also touched at the very end: `SKILL.md` (schema docs), for the new `:type/external-dependency` entity type.

---

## Task 1: Fix `.tsx` parser loading

**Files:**
- Modify: `mcp_server.py:103-121` (`_build_parser`)
- Test: `tests/test_mcp_server.py` (new test in a new `TestTsxParserLoading` class, placed after the existing `TestExtractFromSourceCFamily` class around line 1560)

**Interfaces:**
- Produces: `_build_parser(lang_name)` now succeeds for `lang_name == "tsx"`.

- [ ] **Step 1: Write the failing test**

```python
class TestTsxParserLoading:
    """Regression test for the .tsx module-name bug: _build_parser assumed
    the importable module is always tree_sitter_{lang_name}, but tsx's grammar
    ships inside the tree_sitter_typescript package under language_tsx()."""

    def test_tsx_parser_builds_successfully(self):
        pytest.importorskip("tree_sitter_typescript")
        import mcp_server
        mcp_server._grammar_cache.clear()
        parser = mcp_server._get_parser("component.tsx")
        assert parser is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py::TestTsxParserLoading -v`
Expected: FAIL — `assert parser is not None` fails because `_get_parser` catches the `ModuleNotFoundError` for `tree_sitter_tsx` and caches `None`.

- [ ] **Step 3: Write minimal implementation**

Replace `_build_parser` at `mcp_server.py:103-121`:

```python
# Maps lang_name to the actual importable module, for the (currently only)
# case where a single package ships multiple grammar variants. tsx and
# typescript are both exposed by the tree_sitter_typescript package via
# separate language_tsx()/language_typescript() functions — there is no
# separate tree_sitter_tsx module, unlike every other language here.
_LANG_MODULE_OVERRIDES: Dict[str, str] = {
    "tsx": "tree_sitter_typescript",
}


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
    module_name = _LANG_MODULE_OVERRIDES.get(lang_name, f"tree_sitter_{lang_name}")
    mod = __import__(module_name, fromlist=["language"])
    from tree_sitter import Language, Parser  # type: ignore
    # PHP exposes language_php() instead of language(); tsx exposes
    # language_tsx() from within the tree_sitter_typescript module.
    lang_fn = getattr(mod, f"language_{lang_name}", None) or mod.language
    lang_obj = Language(lang_fn())
    return Parser(lang_obj)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py::TestTsxParserLoading -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "fix: resolve tree_sitter_typescript module for tsx grammar (#97)"
```

---

## Task 2: Alias `tsx` to `typescript` for node-type dispatch

**Files:**
- Modify: `mcp_server.py:617-621` (`_walk_ast`) and `mcp_server.py:478-481` (`_extract_import_name`)
- Test: `tests/test_mcp_server.py`, same `TestTsxParserLoading` class from Task 1

**Interfaces:**
- Consumes: `_LANG_MODULE_OVERRIDES` is unrelated; this task only touches the two lookup sites.
- Produces: `.tsx` files now yield functions/classes/imports identical to `.ts` in shape.

- [ ] **Step 1: Write the failing test**

```python
    def test_tsx_extracts_functions_classes_imports(self):
        pytest.importorskip("tree_sitter_typescript")
        import mcp_server
        mcp_server._grammar_cache.clear()
        source = (
            b"import React from 'react';\n"
            b"class Widget extends React.Component {\n"
            b"  render() { return null; }\n"
            b"}\n"
            b"function useThing() { return 1; }\n"
        )
        parser = mcp_server._get_parser("component.tsx")
        result = mcp_server._extract_from_source(source, parser, "component.tsx")
        assert "Widget" in result["classes"]
        assert "useThing" in result["functions"] or "render" in result["functions"]
        assert "react" in result["imports"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py::TestTsxParserLoading::test_tsx_extracts_functions_classes_imports -v`
Expected: FAIL — `result["classes"]`, `result["functions"]`, `result["imports"]` are all empty lists because `_walk_ast` returns immediately (`_LANG_NODE_TYPES.get("tsx")` is `None`).

- [ ] **Step 3: Write minimal implementation**

In `_walk_ast` (`mcp_server.py:617-621`), change:

```python
def _walk_ast(node, results: Dict[str, List[str]], lang_name: str) -> None:
    """Recursively extract code entities from a tree-sitter AST node."""
    node_types = _LANG_NODE_TYPES.get(lang_name)
    if node_types is None:
        return
```

to:

```python
def _walk_ast(node, results: Dict[str, List[str]], lang_name: str) -> None:
    """Recursively extract code entities from a tree-sitter AST node.

    tsx is treated as an alias of typescript here (and in _extract_import_name)
    rather than duplicating every _LANG_NODE_TYPES entry — the TSX grammar is
    a strict superset of TypeScript's node types for the constructs this
    module cares about (functions, classes, imports, calls).
    """
    node_types = _LANG_NODE_TYPES.get("typescript" if lang_name == "tsx" else lang_name)
    if node_types is None:
        return
```

In `_extract_import_name` (`mcp_server.py:478-495`), change the dispatch condition:

```python
    elif lang_name in ("javascript", "typescript"):
```

to:

```python
    elif lang_name in ("javascript", "typescript", "tsx"):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py::TestTsxParserLoading -v`
Expected: PASS (both tests in the class)

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/ -x -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "fix: alias tsx to typescript in node-type and import dispatch (#97)"
```

---

## Task 3: Add `_git_diff_tree_raw` (single-call raw diff-tree helper)

**Files:**
- Modify: `mcp_server.py:1140-1154` (add new function immediately before `_git_changed_files`)
- Test: `tests/test_mcp_server.py`, new `TestGitDiffTreeRaw` class placed directly after the existing `TestGitHelpers` class (ends around line 1751)

**Interfaces:**
- Produces: `_git_diff_tree_raw(repo_path: str, commit_hash: str) -> List[Tuple[str, str, str, str, str, str]]` — `(status_char, old_mode, new_mode, old_sha, new_sha, path)` for every changed path in a commit.

- [ ] **Step 1: Write the failing test**

```python
class TestGitDiffTreeRaw:
    def test_regular_file_add(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(git_repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path = entries[0]
        assert status == "A"
        assert old_mode == "000000"
        assert new_mode == "100644"
        assert path == "auth.py"

    def test_gitlink_add_reports_mode_160000(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

        sub = tmp_path / "sub"
        sub.mkdir()
        _subprocess.run(["git", "init"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "--allow-empty", "-m", "e"], cwd=sub, check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=sub, check=True, capture_output=True, text=True,
        ).stdout.strip()

        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},vendor/lib"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        entries = mcp_server._git_diff_tree_raw(str(repo), commits[0][0])
        assert len(entries) == 1
        status, old_mode, new_mode, old_sha, new_sha, path = entries[0]
        assert status == "A"
        assert new_mode == "160000"
        assert new_sha == sub_hash
        assert path == "vendor/lib"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py::TestGitDiffTreeRaw -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_git_diff_tree_raw'`

- [ ] **Step 3: Write minimal implementation**

Insert immediately before `_git_changed_files` at `mcp_server.py:1140`:

```python
def _git_diff_tree_raw(repo_path: str, commit_hash: str) -> List[tuple]:
    """Return (status_char, old_mode, new_mode, old_sha, new_sha, path) for
    every changed path in a commit, via a single `git diff-tree --raw` call.

    Supersedes running diff-tree a second time just to detect gitlinks:
    --raw already carries file mode (needed to spot submodule paths, mode
    160000) in the same subprocess invocation _extract_commit already makes.
    """
    result = _subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--raw", "--root", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.strip().splitlines():
        if not line.startswith(":"):
            continue
        meta, sep, path = line.partition("\t")
        if not sep:
            continue
        fields = meta[1:].split(" ")
        if len(fields) < 5:
            continue
        old_mode, new_mode, old_sha, new_sha, status = fields[0], fields[1], fields[2], fields[3], fields[4]
        entries.append((status[0], old_mode, new_mode, old_sha, new_sha, path))
    return entries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py::TestGitDiffTreeRaw -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _git_diff_tree_raw for mode-aware diff-tree parsing (#97)"
```

---

## Task 4: Add `_gitlink_changes` (pure filter over raw entries)

**Files:**
- Modify: `mcp_server.py` (add new function + `_GITLINK_MODE` constant directly after `_git_diff_tree_raw`)
- Test: `tests/test_mcp_server.py`, new `TestGitlinkChanges` class placed directly after `TestGitDiffTreeRaw`

**Interfaces:**
- Consumes: same tuple shape as `_git_diff_tree_raw`'s return.
- Produces: `_gitlink_changes(raw_entries: List[tuple]) -> List[Tuple[str, str, str]]` — `(change_kind, sha, path)`, `change_kind` in `{"add", "bump", "remove"}`.

- [ ] **Step 1: Write the failing test**

```python
class TestGitlinkChanges:
    def test_non_gitlink_rows_are_ignored(self):
        import mcp_server
        raw = [("A", "000000", "100644", "0" * 40, "a" * 40, "auth.py")]
        assert mcp_server._gitlink_changes(raw) == []

    def test_add_when_new_mode_is_gitlink(self):
        import mcp_server
        raw = [("A", "000000", "160000", "0" * 40, "b" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("add", "b" * 40, "vendor/lib")]

    def test_bump_when_both_modes_are_gitlink(self):
        import mcp_server
        raw = [("M", "160000", "160000", "b" * 40, "c" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("bump", "c" * 40, "vendor/lib")]

    def test_remove_when_old_mode_is_gitlink(self):
        import mcp_server
        raw = [("D", "160000", "000000", "c" * 40, "0" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("remove", "c" * 40, "vendor/lib")]

    def test_type_change_into_internal_reported_as_remove(self):
        import mcp_server
        raw = [("T", "160000", "100644", "c" * 40, "d" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("remove", "c" * 40, "vendor/lib")]

    def test_type_change_into_external_reported_as_add(self):
        import mcp_server
        raw = [("T", "100644", "160000", "d" * 40, "e" * 40, "vendor/lib")]
        assert mcp_server._gitlink_changes(raw) == [("add", "e" * 40, "vendor/lib")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py::TestGitlinkChanges -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_gitlink_changes'`

- [ ] **Step 3: Write minimal implementation**

```python
_GITLINK_MODE = "160000"


def _gitlink_changes(raw_entries: List[tuple]) -> List[tuple]:
    """Filter _git_diff_tree_raw's output down to gitlink-involving rows,
    collapsed into three cases by mode pair rather than by the raw status
    letter (which varies: A/D/M/T can all represent a gitlink change
    depending on what else happened to the same path):

      "add"    — new_mode is a gitlink, old_mode is not. Covers a plain
                 submodule addition (status A) and a same-path flip from a
                 regular blob into a gitlink (status T).
      "bump"   — both modes are gitlinks (status M): the pinned commit changed.
      "remove" — old_mode is a gitlink, new_mode is not. Covers a plain
                 submodule removal (status D) and a same-path flip from a
                 gitlink back into a regular blob (status T).

    sha is the new pinned commit for "add"/"bump", or the last-known pinned
    commit for "remove" (needed by the caller to close the right fact).
    """
    changes = []
    for status, old_mode, new_mode, old_sha, new_sha, path in raw_entries:
        old_is_link = old_mode == _GITLINK_MODE
        new_is_link = new_mode == _GITLINK_MODE
        if not old_is_link and not new_is_link:
            continue
        if new_is_link and not old_is_link:
            changes.append(("add", new_sha, path))
        elif old_is_link and new_is_link:
            changes.append(("bump", new_sha, path))
        else:
            changes.append(("remove", old_sha, path))
    return changes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py::TestGitlinkChanges -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _gitlink_changes filter over raw diff-tree entries (#97)"
```

---

## Task 5: Add `.gitmodules` parsing (`_parse_gitmodules`, `_git_gitmodules_at`)

**Files:**
- Modify: `mcp_server.py` (add both functions directly after `_git_file_content`, i.e. after line 1168)
- Test: `tests/test_mcp_server.py`, new `TestParseGitmodules` class placed after `TestGitlinkChanges`

**Interfaces:**
- Consumes: `_git_file_content(repo_path, commit_hash, file_path) -> bytes` (existing, unchanged).
- Produces: `_parse_gitmodules(content: bytes) -> Dict[str, Dict[str, str]]` (path → `{"name", "url"}`); `_git_gitmodules_at(repo_path, commit_hash) -> Dict[str, Dict[str, str]]`.

- [ ] **Step 1: Write the failing test**

```python
class TestParseGitmodules:
    def test_parses_single_submodule(self):
        import mcp_server
        content = (
            b'[submodule "abseil-cpp"]\n'
            b'\tpath = 3rdParty/abseil-cpp\n'
            b'\turl = https://github.com/abseil/abseil-cpp.git\n'
        )
        result = mcp_server._parse_gitmodules(content)
        assert result == {
            "3rdParty/abseil-cpp": {
                "name": "abseil-cpp",
                "url": "https://github.com/abseil/abseil-cpp.git",
            }
        }

    def test_parses_multiple_submodules(self):
        import mcp_server
        content = (
            b'[submodule "a"]\n\tpath = vendor/a\n\turl = https://x/a.git\n'
            b'[submodule "b"]\n\tpath = vendor/b\n\turl = https://x/b.git\n'
        )
        result = mcp_server._parse_gitmodules(content)
        assert set(result.keys()) == {"vendor/a", "vendor/b"}

    def test_malformed_content_returns_empty_dict(self):
        import mcp_server
        result = mcp_server._parse_gitmodules(b"not a valid [ini file")
        assert result == {}

    def test_empty_content_returns_empty_dict(self):
        import mcp_server
        assert mcp_server._parse_gitmodules(b"") == {}

    def test_git_gitmodules_at_missing_file_returns_empty(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        result = mcp_server._git_gitmodules_at(str(git_repo), commits[0][0])
        assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py::TestParseGitmodules -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_parse_gitmodules'`

- [ ] **Step 3: Write minimal implementation**

```python
def _parse_gitmodules(content: bytes) -> Dict[str, Dict[str, str]]:
    """Parse .gitmodules content into {path: {"name": ..., "url": ...}}.

    Best-effort: git config's `[section "subsection"]` syntax is a strict
    superset of what configparser expects for ordinary cases, so malformed
    or unusual .gitmodules content fails closed to an empty dict rather
    than raising — matches this file's existing best-effort git/parse
    conventions (see _extract_from_source's bare except).
    """
    import configparser

    result: Dict[str, Dict[str, str]] = {}
    parser = configparser.ConfigParser()
    try:
        parser.read_string(content.decode("utf-8", errors="replace"))
    except configparser.Error:
        return result
    for section in parser.sections():
        m = re.match(r'submodule\s+"(.+)"', section)
        if not m:
            continue
        path = parser.get(section, "path", fallback=None)
        url = parser.get(section, "url", fallback=None)
        if path:
            result[path] = {"name": m.group(1), "url": url or ""}
    return result


def _git_gitmodules_at(repo_path: str, commit_hash: str) -> Dict[str, Dict[str, str]]:
    """Fetch and parse .gitmodules as it exists at commit_hash.

    Empty dict if missing or unparseable — most repos never have a
    .gitmodules file at all, which is the normal case, not an error.
    """
    try:
        content = _git_file_content(repo_path, commit_hash, ".gitmodules")
    except Exception:
        return {}
    return _parse_gitmodules(content)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py::TestParseGitmodules -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add .gitmodules parsing for submodule name/url (#97)"
```

---

## Task 6: Extend `_extract_commit` to return gitlink changes and `.gitmodules` map

**Files:**
- Modify: `mcp_server.py:2425-2450` (`_extract_commit`)
- Modify: `tests/test_mcp_server.py:1753-1799` (4 existing tests in `TestExtractCommit` that unpack `_extract_commit`'s return or monkeypatch its git call)
- Test: same file, new tests appended to `TestExtractCommit`

**Interfaces:**
- Consumes: `_git_diff_tree_raw` (Task 3), `_gitlink_changes` (Task 4), `_git_gitmodules_at` (Task 5).
- Produces: `_extract_commit(repo_path, commit_hash) -> Tuple[List[Tuple[str,str,Optional[Dict]]], List[Tuple[str,str,str]], Dict[str,Dict[str,str]]]` — `(file_results, gitlink_changes, gitmodules_map)`. **This changes the existing return shape from a bare list to a 3-tuple — every caller must be updated in this task.**

- [ ] **Step 1: Update the 4 existing tests that call `_extract_commit` directly**

In `tests/test_mcp_server.py`, replace the `TestExtractCommit` class body (currently lines 1753-1799) with:

```python
class TestExtractCommit:
    def test_added_file_returns_extracted_dict(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]

        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(git_repo), first_hash)

        assert len(results) == 1
        status, file_path, extracted = results[0]
        assert status == "A"
        assert file_path == "auth.py"
        assert "login" in extracted["functions"]
        assert gitlink_changes == []
        assert gitmodules_map == {}

    def test_deleted_file_has_none_extracted(self, git_repo_with_deletion):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_with_deletion), watermark_hash=None)
        delete_hash = commits[-1][0]

        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(git_repo_with_deletion), delete_hash)

        d_entries = [r for r in results if r[0] == "D"]
        assert len(d_entries) == 1
        assert d_entries[0][2] is None

    def test_unsupported_extension_is_omitted(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_diff_tree_raw",
            lambda repo, commit: [("A", "000000", "100644", "0" * 40, "a" * 40, "notes.txt")],
        )
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []

    def test_content_fetch_failure_is_omitted_not_raised(self, git_repo, monkeypatch):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_git_diff_tree_raw",
            lambda repo, commit: [("A", "000000", "100644", "0" * 40, "a" * 40, "auth.py")],
        )

        def boom(repo, commit, path):
            raise mcp_server.MiniGrafError("simulated git-show failure")

        monkeypatch.setattr(mcp_server, "_git_file_content", boom)
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(git_repo), "deadbeef")
        assert results == []

    def test_gitlink_add_is_reported_separately_from_file_results(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

        sub = tmp_path / "sub"
        sub.mkdir()
        _subprocess.run(["git", "init"], cwd=sub, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "--allow-empty", "-m", "e"], cwd=sub, check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=sub, check=True, capture_output=True, text=True,
        ).stdout.strip()

        (repo / ".gitmodules").write_text(
            '[submodule "lib"]\n\tpath = vendor/lib\n\turl = https://example.com/lib.git\n'
        )
        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},vendor/lib"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "add", ".gitmodules"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(repo), commits[0][0])

        # Neither the gitlink path nor .gitmodules itself has a resolvable
        # extension (Path(".gitmodules").suffix == "" per pathlib — a
        # leading-dot-only filename has no extension), so both are omitted
        # from the regular per-file results just like any unsupported file.
        assert results == []
        assert gitlink_changes == [("add", sub_hash, "vendor/lib")]
        assert gitmodules_map == {"vendor/lib": {"name": "lib", "url": "https://example.com/lib.git"}}
```

- [ ] **Step 2: Run the updated/new tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestExtractCommit -v`
Expected: FAIL — `ValueError: too many values to unpack` on the first 4 tests (old `_extract_commit` still returns a bare list), and `AttributeError` on `_git_diff_tree_raw` monkeypatch targets not existing yet in that role, plus the new gitlink test failing since gitlink info isn't returned at all yet.

- [ ] **Step 3: Update `_extract_commit`**

Replace `mcp_server.py:2425-2450`:

```python
def _extract_commit(
    repo_path: str, commit_hash: str
) -> Tuple[List[Tuple[str, str, Optional[Dict[str, List[str]]]]], List[tuple], Dict[str, Dict[str, str]]]:
    """Read-only, stateless per-commit extraction: diff-tree + git-show + tree-sitter parse.

    Runs in a worker thread via the ThreadPoolExecutor in _run_ingestion.
    Touches no shared mutable state and no DB. Returns (file_results,
    gitlink_changes, gitmodules_map):

      file_results: one entry per changed file that has a supported parser;
        A/M files whose content fetch fails are omitted entirely, mirroring
        the previous inline `continue` — same as before this pipeline existed.
      gitlink_changes: _gitlink_changes' output — gitlink-involving rows,
        never fed through the tree-sitter parser (gitlink paths never have
        a resolvable extension).
      gitmodules_map: path -> {"name", "url"}, populated only when this
        commit has at least one gitlink "add" — avoids a wasted git-show
        call on the (overwhelmingly common) case of a commit that touches
        no submodules at all.

    Sources both file_results and gitlink_changes from a single
    `git diff-tree --raw` call (via _git_diff_tree_raw) rather than the
    former --name-status call, which discarded file mode entirely.
    """
    raw_entries = _git_diff_tree_raw(repo_path, commit_hash)
    results: List[Tuple[str, str, Optional[Dict[str, List[str]]]]] = []
    for status, old_mode, new_mode, old_sha, new_sha, file_path in raw_entries:
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

    gitlink_changes = _gitlink_changes(raw_entries)
    gitmodules_map: Dict[str, Dict[str, str]] = {}
    if any(kind == "add" for kind, _, _ in gitlink_changes):
        gitmodules_map = _git_gitmodules_at(repo_path, commit_hash)

    return results, gitlink_changes, gitmodules_map
```

- [ ] **Step 4: Update the one production call site in `_run_ingestion`**

At `mcp_server.py:2524` (inside the `while pending:` loop), change:

```python
                (commit_hash, commit_ts_iso, author, subject), fut = pending.popleft()
                extracted_files = await fut
                submit_next()
```

to:

```python
                (commit_hash, commit_ts_iso, author, subject), fut = pending.popleft()
                extracted_files, gitlink_changes, gitmodules_map = await fut
                submit_next()
```

(`gitlink_changes`/`gitmodules_map` are unused until Task 8 wires them in — that's fine, they're just local variables for now.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestExtractCommit -v && pytest tests/ -x -q`
Expected: PASS, full suite green (in particular `TestRunIngestionBitemporalDeps` and the async ingestion tests, which go through `_run_ingestion`'s unpacking).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: extend _extract_commit to report gitlink changes and .gitmodules (#97)"
```

---

## Task 7: Broaden entity preload + add `_preload_pinned_commits`

**Files:**
- Modify: `mcp_server.py:2279-2329` (`_preload_known_entities`)
- Modify: `mcp_server.py` (add `_preload_pinned_commits` directly after `_preload_known_deps`, i.e. after line 2391)
- Test: `tests/test_mcp_server.py`, new `TestPreloadExternalDependencies` class placed directly after the existing `TestPreloadKnownDeps` class (ends around line 2119)

**Interfaces:**
- Consumes: `_VALID_TIME_FOREVER_MS` (existing constant, `mcp_server.py:2332`).
- Produces: `_preload_known_entities` now also loads `:type/external-dependency` idents into the same `entity_valid_from`/`entity_descriptions`/`file_entities` dicts. New `_preload_pinned_commits(db) -> Dict[str, Tuple[str, str]]` — `{ident: (sha, valid_from_iso)}`.

- [ ] **Step 1: Write the failing tests**

This follows the exact fixture idiom already used by `TestPreloadKnownDeps` at `tests/test_mcp_server.py:2057-2075`: `mock_minigraf_db` patches the `MiniGrafDb` constructor, so tests call `mcp_server.open_db(...)` then `mcp_server.get_db()` to obtain the (mocked) handle — they never construct `MiniGrafDb` directly.

```python
class TestPreloadExternalDependencies:
    def test_preload_known_entities_includes_external_dependency(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        # _preload_known_entities' query shape is [?ident ?path ?desc ?date] per entity_type
        db_instance.execute.return_value = json.dumps({
            "results": [[":module/vendor-lib", "vendor/lib", "lib", "2026-01-01T00:00:00Z"]]
        })
        mcp_server.open_db(str(tmp_path / "memory.graph"))
        db = mcp_server.get_db()

        entity_valid_from, entity_descriptions, file_entities = mcp_server._preload_known_entities(db, str(tmp_path))

        assert ":module/vendor-lib" in entity_valid_from
        assert entity_descriptions[":module/vendor-lib"] == "lib"
        assert "vendor/lib" in file_entities

    def test_preload_pinned_commits_reloads_current_sha(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        # _preload_pinned_commits' query shape is [?e ?sha ?vf] with :any-valid-time
        db_instance.execute.return_value = json.dumps({
            "results": [[":module/vendor-lib", "abc123", 1735689600000]]
        })
        mcp_server.open_db(str(tmp_path / "memory.graph"))
        db = mcp_server.get_db()

        pinned = mcp_server._preload_pinned_commits(db)

        assert pinned[":module/vendor-lib"][0] == "abc123"
        assert pinned[":module/vendor-lib"][1].endswith("Z")

    def test_preload_pinned_commits_returns_empty_on_query_failure(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        from minigraf import MiniGrafError
        mcp_server.open_db(str(tmp_path / "memory.graph"))
        db = mcp_server.get_db()
        db_instance.execute.side_effect = MiniGrafError("boom")

        assert mcp_server._preload_pinned_commits(db) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestPreloadExternalDependencies -v`
Expected: FAIL — `_preload_known_entities` doesn't query `:type/external-dependency` yet, and `_preload_pinned_commits` doesn't exist.

- [ ] **Step 3: Broaden `_preload_known_entities`**

At `mcp_server.py:2307-2308`, change:

```python
    for entity_type in ("module", "function", "class"):
        path_attr = "path" if entity_type == "module" else "file"
```

to:

```python
    for entity_type in ("module", "function", "class", "external-dependency"):
        path_attr = "path" if entity_type in ("module", "external-dependency") else "file"
```

Update the function's docstring (`mcp_server.py:2279-2289`) to mention external-dependency:

```python
def _preload_known_entities(db: Any, repo_path: str) -> tuple:
    """Load all existing module/function/class/external-dependency idents from
    the DB, and pre-seed file_entities with all currently tracked files in the
    repo.

    external-dependency entities share the module ident namespace and use the
    same "path" attribute as modules, so folding them into this same query
    means the existing close/reopen machinery (entity_valid_from,
    entity_descriptions) just works for submodules without new parallel state.
    Unresolved-import placeholders (no :path) are not reloaded by this query —
    nothing in this codebase ever closes one, so the gap is harmless; see the
    design spec's Section 2.

    Pre-seeding from `git ls-files` ensures that _resolve_module_import can
    find any module file even when processing early commits — before those files
    have been introduced in the chronological commit walk.

    Returns (entity_valid_from, entity_descriptions, file_entities).
    entity_valid_from maps ident → git commit timestamp of first introduction.
    entity_descriptions maps ident → human-readable name (function/class/file).
    """
```

- [ ] **Step 4: Add `_preload_pinned_commits`**

Insert directly after `_preload_known_deps` (after `mcp_server.py:2391`):

```python
def _preload_pinned_commits(db: Any) -> Dict[str, tuple]:
    """Reload each external-dependency entity's current :pinned-commit value
    and the timestamp it was set at, mirroring _preload_known_deps's per-fact
    :any-valid-time pattern for :depends-on.

    Needed because :pinned-commit is bi-temporally closed and reopened on
    every bump (see _run_ingestion's gitlink handling) — without this, the
    server would lose track of the prior SHA and valid-from across a restart,
    corrupting the close on the next bump or removal exactly the way
    _preload_known_deps' docstring describes for :depends-on.

    Returns {ident: (sha, valid_from_iso)}.
    """
    pinned: Dict[str, tuple] = {}
    try:
        raw = db.execute(
            "(query [:find ?e ?sha ?vf "
            ":any-valid-time "
            ":where [?e :pinned-commit ?sha] "
            "[?e :db/valid-from ?vf] "
            "[?e :db/valid-to ?vt] "
            f"[(= ?vt {_VALID_TIME_FOREVER_MS})]])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return pinned
    for ident, sha, vf_ms in rows:
        vf_iso = (
            datetime.datetime.fromtimestamp(vf_ms / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        pinned[ident] = (sha, vf_iso)
    return pinned
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestPreloadExternalDependencies -v && pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: preload external-dependency entities and pinned-commit history (#97)"
```

---

## Task 8: Wire gitlink handling into `_run_ingestion`'s commit loop

**Files:**
- Modify: `mcp_server.py:2453-2492` (preload section at the top of `_run_ingestion`)
- Modify: `mcp_server.py:2523-2626` (per-commit body)
- Test: `tests/test_mcp_server.py`, new `TestRunIngestionGitlinks` class placed after `TestRunIngestionBitemporalDeps` (after line 3240)

**Interfaces:**
- Consumes: `_preload_pinned_commits` (Task 7), `_gitlink_changes`/`gitmodules_map` now returned by `_extract_commit` (Task 6), `_build_close_triples`, `_ingest_transact`, `_ingest_close`, `_edn_escape` (all existing).
- Produces: `:type/external-dependency` entities in the DB for submodule add/bump/remove/flip.

- [ ] **Step 1: Write the failing tests**

```python
class TestRunIngestionGitlinks:
    """End-to-end tests for submodule add/bump/remove/flip via _run_ingestion."""

    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    def _add_submodule_commit(self, repo, path="vendor/lib", name="lib", url="https://example.com/lib.git"):
        sub = repo.parent / f"{repo.name}-sub"
        _subprocess.run(["git", "init", "-q", str(sub)], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "config", "user.email", "t@t.com"], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "config", "user.name", "T"], check=True, capture_output=True)
        _subprocess.run(["git", "-C", str(sub), "commit", "--allow-empty", "-m", "e"], check=True, capture_output=True)
        sub_hash = _subprocess.run(
            ["git", "-C", str(sub), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        (repo / ".gitmodules").write_text(f'[submodule "{name}"]\n\tpath = {path}\n\turl = {url}\n')
        _subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sub_hash},{path}"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "add", ".gitmodules"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add submodule"], cwd=repo, check=True, capture_output=True)
        return sub_hash

    @pytest.mark.asyncio
    async def test_submodule_add_creates_external_dependency_entity(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        sub_hash = self._add_submodule_commit(repo)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        ident = mcp_server._code_ident("module", "vendor/lib")
        assert any(f"[{ident} :entity-type :type/external-dependency]" in t for t in transact_calls)
        assert any(f'[{ident} :pinned-commit "{sub_hash}"]' in t for t in transact_calls)
        assert any(f'[{ident} :submodule-name "lib"]' in t for t in transact_calls)
        assert any(f'[{ident} :submodule-url "https://example.com/lib.git"]' in t for t in transact_calls)

    @pytest.mark.asyncio
    async def test_submodule_bump_closes_old_pinned_commit(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        first_sha = self._add_submodule_commit(repo)

        sub_dir = tmp_path / f"{repo.name}-sub"
        _subprocess.run(["git", "-C", str(sub_dir), "commit", "--allow-empty", "-m", "bump"], check=True, capture_output=True)
        second_sha = _subprocess.run(
            ["git", "-C", str(sub_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        _subprocess.run(
            ["git", "update-index", "--cacheinfo", f"160000,{second_sha},vendor/lib"],
            cwd=repo, check=True, capture_output=True,
        )
        _subprocess.run(["git", "commit", "-m", "bump submodule"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )
        await mcp_server._run_ingestion(str(repo), "HEAD")

        ident = mcp_server._code_ident("module", "vendor/lib")
        assert any(f'[{ident} :pinned-commit "{first_sha}"]' in t for t in close_triples_seen)

    @pytest.mark.asyncio
    async def test_submodule_removal_closes_entity(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        self._add_submodule_commit(repo)

        _subprocess.run(["git", "rm", "-f", "vendor/lib"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "remove submodule"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )
        await mcp_server._run_ingestion(str(repo), "HEAD")

        ident = mcp_server._code_ident("module", "vendor/lib")
        assert any(f'[{ident} :ident "{ident}"]' in t for t in close_triples_seen)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestRunIngestionGitlinks -v`
Expected: FAIL — none of the assertions find matching triples since gitlink changes aren't wired into the commit loop yet.

- [ ] **Step 3: Add `pinned_commit_state` to the preload section**

At `mcp_server.py:2472-2473`, change:

```python
        entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
        file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
```

to:

```python
        entity_valid_from, entity_descriptions, file_entities = _preload_known_entities(db, repo_path)
        file_deps, dep_valid_from = _preload_known_deps(db, file_entities)
        pinned_commit_state = _preload_pinned_commits(db)
```

- [ ] **Step 4: Insert gitlink handling into the per-commit body**

At `mcp_server.py:2549`, immediately after the existing per-file `for status, file_path, extracted in extracted_files:` loop closes and before the `# Split :contains triples out before batching.` comment (`mcp_server.py:2611`), insert:

```python
                    for kind, sha, path in gitlink_changes:
                        ext_ident = _code_ident("module", path)
                        if kind == "add":
                            info = gitmodules_map.get(path, {})
                            name = info.get("name", "")
                            url = info.get("url", "")
                            description = name or path
                            ext_triples = [
                                f"[{ext_ident} :entity-type :type/external-dependency]",
                                f'[{ext_ident} :ident "{_edn_escape(ext_ident)}"]',
                                f'[{ext_ident} :description "{_edn_escape(description)}"]',
                                f'[{ext_ident} :path "{_edn_escape(path)}"]',
                                f'[{ext_ident} :pinned-commit "{_edn_escape(sha)}"]',
                                f"[{ext_ident} :introduced-by {commit_ident}]",
                            ]
                            if name:
                                ext_triples.append(f'[{ext_ident} :submodule-name "{_edn_escape(name)}"]')
                            if url:
                                ext_triples.append(f'[{ext_ident} :submodule-url "{_edn_escape(url)}"]')
                            add_triples.extend(ext_triples)
                            entity_valid_from[ext_ident] = commit_ts_iso
                            entity_descriptions[ext_ident] = description
                            pinned_commit_state[ext_ident] = (sha, commit_ts_iso)
                        elif kind == "bump":
                            old_sha, orig_ts = pinned_commit_state.get(ext_ident, (None, commit_ts_iso))
                            if old_sha is not None:
                                close_items.append(
                                    ([f'[{ext_ident} :pinned-commit "{_edn_escape(old_sha)}"]'], orig_ts)
                                )
                            add_triples.append(f'[{ext_ident} :pinned-commit "{_edn_escape(sha)}"]')
                            add_triples.append(f"[{ext_ident} :modified-in {commit_ident}]")
                            pinned_commit_state[ext_ident] = (sha, commit_ts_iso)
                        else:  # "remove"
                            orig_ts = entity_valid_from.get(ext_ident, commit_ts_iso)
                            desc = entity_descriptions.get(ext_ident, "")
                            close_items.append(
                                (_build_close_triples(ext_ident, desc, ext_ident), orig_ts)
                            )
                            old_sha, pin_orig_ts = pinned_commit_state.pop(ext_ident, (None, commit_ts_iso))
                            if old_sha is not None:
                                close_items.append(
                                    ([f'[{ext_ident} :pinned-commit "{_edn_escape(old_sha)}"]'], pin_orig_ts)
                                )

```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestRunIngestionGitlinks -v && pytest tests/ -x -q`
Expected: PASS, full suite green.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: label real git submodules as :type/external-dependency (#97)"
```

---

## Task 9: Tag unresolved imports as external, using today's (Rust-only) resolver

**Files:**
- Modify: `mcp_server.py:1062-1090` (`_resolve_module_import`)
- Modify: `mcp_server.py:2593-2609` (the dependency-diffing block inside `_run_ingestion`'s per-file loop)
- Test: `tests/test_mcp_server.py`, new `TestUnresolvedImportTagging` class placed after `TestRunIngestionBitemporalDeps`

**Interfaces:**
- Produces: `_resolve_module_import(import_name: str, file_entities: Dict[str, List[str]]) -> Tuple[str, bool]` — the bool is `True` when it resolved to a known file, `False` when it fell through to the bare-ident guess. **Return type change — the one call site in `_run_ingestion` must be updated in this same task.**

- [ ] **Step 1: Write the failing tests**

```python
class TestUnresolvedImportTagging:
    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    def test_resolve_module_import_returns_bool_flag(self):
        import mcp_server
        file_entities = {"src/storage.rs": []}
        ident, is_resolved = mcp_server._resolve_module_import("storage", file_entities)
        assert is_resolved is True
        ident, is_resolved = mcp_server._resolve_module_import("totally_unknown_crate", file_entities)
        assert is_resolved is False

    @pytest.mark.asyncio
    async def test_unresolved_import_gets_tagged_external_dependency(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.rs").write_text('use tokio;\nfn main() {}\n')
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add main"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        tokio_ident = mcp_server._canonical_ident("module", "tokio")
        assert any(f"[{tokio_ident} :entity-type :type/external-dependency]" in t for t in transact_calls)
        assert any(f'[{tokio_ident} :description "tokio"]' in t for t in transact_calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestUnresolvedImportTagging -v`
Expected: FAIL — `_resolve_module_import` still returns a bare string (unpacking into `ident, is_resolved` raises `ValueError`), and the tagging isn't wired in.

- [ ] **Step 3: Update `_resolve_module_import`'s return type**

Replace `mcp_server.py:1062-1090`:

```python
def _resolve_module_import(import_name: str, file_entities: Dict[str, List[str]]) -> Tuple[str, bool]:
    """Resolve an import name to a module ident that joins with stored module entities.

    For a name like "storage", tries standard Rust source-root locations first
    (src/storage.rs, src/storage/mod.rs) before falling back to a broader name
    search. The ordered-priority approach prevents e.g. src/graph/storage.rs
    from matching a top-level `use crate::storage` import.

    Returns (ident, is_resolved). is_resolved is True when import_name matched
    a real file in file_entities, False when it fell through to the bare
    _canonical_ident guess — the caller uses this to tag genuinely unresolved
    imports as :type/external-dependency without also tagging real (if not
    yet visited) internal modules.
    """
    # Priority 1: canonical Rust module root paths under common source roots
    for src_root in ("src", "lib", ""):
        prefix = f"{src_root}/" if src_root else ""
        candidate_file = f"{prefix}{import_name}.rs"
        candidate_mod = f"{prefix}{import_name}/mod.rs"
        if candidate_file in file_entities:
            return _code_ident("module", candidate_file), True
        if candidate_mod in file_entities:
            return _code_ident("module", candidate_mod), True

    # Priority 2: broader search — only match files directly under a src root
    # (parent.parent is the source root, not a nested subdir)
    for file_path in file_entities:
        p = Path(file_path)
        if p.stem == "mod" and p.parent.name == import_name:
            return _code_ident("module", file_path), True

    return _canonical_ident("module", import_name), False
```

- [ ] **Step 4: Update the call site to tag first-seen unresolved idents**

At `mcp_server.py:2593-2601`, change:

```python
                            # Compute dep edges for this file and diff against previous
                            module_ident = _code_ident("module", file_path)
                            current_deps: set = set()
                            for import_name in set(extracted.get("imports", [])):
                                dep_ident = _resolve_module_import(import_name, file_entities)
                                if dep_ident != module_ident:
                                    current_deps.add(dep_ident)
```

to:

```python
                            # Compute dep edges for this file and diff against previous
                            module_ident = _code_ident("module", file_path)
                            current_deps: set = set()
                            for import_name in set(extracted.get("imports", [])):
                                dep_ident, is_resolved = _resolve_module_import(import_name, file_entities)
                                if dep_ident != module_ident:
                                    current_deps.add(dep_ident)
                                    if not is_resolved and dep_ident not in entity_valid_from:
                                        add_triples.extend([
                                            f"[{dep_ident} :entity-type :type/external-dependency]",
                                            f'[{dep_ident} :ident "{_edn_escape(dep_ident)}"]',
                                            f'[{dep_ident} :description "{_edn_escape(import_name)}"]',
                                        ])
                                        entity_valid_from[dep_ident] = commit_ts_iso
                                        entity_descriptions[dep_ident] = import_name
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestUnresolvedImportTagging -v && pytest tests/ -x -q`
Expected: PASS. (`TestRunIngestionBitemporalDeps`'s two existing tests still pass unchanged at this point — Python's `mod_b` import still falls through to the same Rust-only fallback ident, now additionally tagged external, which those tests don't check for and so don't break on.)

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: tag genuinely-unresolved imports as :type/external-dependency (#97)"
```

---

## Task 10: Preserve full paths in C/C++, Ruby, PHP, Go import extraction

**Files:**
- Modify: `mcp_server.py:359-371` (`_c_include_name`)
- Modify: `mcp_server.py:392-418` (`_ruby_require_name`)
- Modify: `mcp_server.py:503-516` (Go branch of `_extract_import_name`)
- Modify: `mcp_server.py:542-548` (PHP branch of `_extract_import_name`)
- Modify (existing tests whose assertions encode the old truncated behavior): `tests/test_mcp_server.py` — `test_ruby_require_relative` (~3356-3362), `test_go_grouped_import` (~3291-3298)
- Test: `tests/test_mcp_server.py`, new tests appended to `TestExtractImportName`

**Interfaces:**
- Produces: these four extractors now preserve directory structure (extension stripped, basename-only truncation removed).

- [ ] **Step 1: Write the failing tests**

Append to `TestExtractImportName`:

```python
    def test_c_local_include_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include "unicode/uloc.h"'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["unicode/uloc"]

    def test_c_angle_include_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include <sys/socket.h>'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["sys/socket"]

    def test_ruby_require_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require 'active_support/core_ext/string'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["active_support/core_ext/string"]

    def test_ruby_require_relative_gets_dot_slash_marker(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require_relative 'my_module'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["./my_module"]

    def test_php_require_preserves_subdirectory(self, tmp_path):
        pytest.importorskip("tree_sitter_php")
        import mcp_server
        source = b"<?php\nrequire 'app/config/database.php';"
        node = _parse_import_node("php", source, "require_expression", tmp_path)
        result = mcp_server._extract_import_name(node, "php")
        assert result == ["app/config/database"]

    def test_go_grouped_import_preserves_full_path(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport (\n\t"os"\n\t"github.com/user/pkg"\n)'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert "os" in result
        assert "github.com/user/pkg" in result
```

Also update the two now-outdated existing tests:

`test_ruby_require_relative` (currently ~3356-3362):

```python
    def test_ruby_require_relative(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require_relative 'my_module'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["./my_module"]
```

`test_go_grouped_import` (currently ~3291-3298):

```python
    def test_go_grouped_import(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport (\n\t"os"\n\t"github.com/user/pkg"\n)'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert "os" in result
        assert "github.com/user/pkg" in result
```

(`test_go_single_import`, `test_c_system_include`, `test_cpp_include`, `test_php_require`, `test_php_include`, `test_ruby_require` need no changes — none of their fixtures include a subdirectory, so basename == full path for those and the assertions already hold either way.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestExtractImportName -v`
Expected: FAIL on all the new/updated subdirectory-preserving assertions.

- [ ] **Step 3: Update `_c_include_name`**

Replace `mcp_server.py:359-371`:

```python
def _c_include_name(node) -> Optional[str]:
    """Return the include target (path preserved, extension stripped) from a
    C/C++ preproc_include node.

    Handles both:
      #include <stdio.h>          → system_lib_string → "stdio"
      #include <unicode/uloc.h>   → system_lib_string → "unicode/uloc"
      #include "sub/myheader.h"   → string_literal    → "sub/myheader"

    Path structure is preserved (not reduced to a bare basename) so
    _resolve_module_import can match vendored in-tree headers precisely —
    both angle-bracket and quoted forms commonly carry a real subdirectory
    (<sys/socket.h>, <unicode/uloc.h>, "app/config.h"), not just stdlib-style
    bare names like <vector>.
    """
    for child in node.children:
        if child.type in ("system_lib_string", "string_literal"):
            raw = child.text.decode("utf-8").strip("<>\"'")
            return os.path.splitext(raw)[0]
    return None
```

- [ ] **Step 4: Update `_ruby_require_name`**

Replace `mcp_server.py:392-418`:

```python
def _ruby_require_name(node) -> Optional[str]:
    """Return the required path from a Ruby call node (path preserved,
    extension stripped). A require_relative target is prefixed with "./" so
    it reuses the same relative-import detection _resolve_module_import
    already needs for JS/TS-style "./foo" specifiers, rather than plumbing a
    separate is-relative flag through the whole imports pipeline.

    Handles:
      require 'rails'                            → "rails"
      require 'active_support/core_ext/string'    → "active_support/core_ext/string"
      require_relative 'my_mod'                   → "./my_mod"
    Returns None for non-require calls.
    """
    method = node.child_by_field_name("method")
    if method is None or method.text.decode("utf-8") not in ("require", "require_relative"):
        return None
    is_relative = method.text.decode("utf-8") == "require_relative"
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "string":
            content_node = next(
                (c for c in child.named_children if c.type == "string_content"),
                None,
            )
            if content_node:
                val = content_node.text.decode("utf-8")
            else:
                val = child.text.decode("utf-8").strip("'\"")
            path = os.path.splitext(val)[0]
            return f"./{path}" if is_relative else path
    return None
```

- [ ] **Step 5: Update the Go branch of `_extract_import_name`**

At `mcp_server.py:503-516`, change:

```python
    elif lang_name == "go":
        def _go_spec(spec_node):
            path = spec_node.child_by_field_name("path")
            if path:
                val = path.text.decode("utf-8").strip('"')
                names.append(val.split("/")[-1])

        for child in node.named_children:
            if child.type == "import_spec":
                _go_spec(child)
            elif child.type == "import_spec_list":
                for spec in child.named_children:
                    if spec.type == "import_spec":
                        _go_spec(spec)
```

to:

```python
    elif lang_name == "go":
        def _go_spec(spec_node):
            path = spec_node.child_by_field_name("path")
            if path:
                names.append(path.text.decode("utf-8").strip('"'))

        for child in node.named_children:
            if child.type == "import_spec":
                _go_spec(child)
            elif child.type == "import_spec_list":
                for spec in child.named_children:
                    if spec.type == "import_spec":
                        _go_spec(spec)
```

- [ ] **Step 6: Update the PHP branch of `_extract_import_name`**

At `mcp_server.py:542-548`, change:

```python
    elif lang_name == "php":
        import os
        for child in node.children:
            if child.type in ("string", "encapsed_string", "string_literal"):
                val = child.text.decode("utf-8").strip("'\"")
                names.append(os.path.splitext(os.path.basename(val))[0])
                break
```

to:

```python
    elif lang_name == "php":
        for child in node.children:
            if child.type in ("string", "encapsed_string", "string_literal"):
                val = child.text.decode("utf-8").strip("'\"")
                names.append(os.path.splitext(val)[0])
                break
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestExtractImportName -v && pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "fix: preserve directory structure in C/C++, Ruby, PHP, Go import extraction (#97)"
```

---

## Task 11: Full-name capture for namespace languages

**Files:**
- Modify: `mcp_server.py:481-494` (Python branch of `_extract_import_name`)
- Modify: `mcp_server.py:517-529` (Java branch)
- Modify: `mcp_server.py:534-537` (`_csharp_using_name` call site — modify the helper itself, `mcp_server.py:374-389`)
- Modify: `mcp_server.py:549-561` (Kotlin branch)
- Modify: `mcp_server.py:562-566` (Swift branch)
- Modify: `mcp_server.py:567-571` (Scala branch)
- Modify: `mcp_server.py:572-577` (Haskell branch)
- Modify: `mcp_server.py:450-475` (`_elixir_module_name`)
- Modify existing tests: `test_java_import`, `test_csharp_using_dotted`, `test_kotlin_import`, `test_scala_import`, `test_haskell_import`, `test_elixir_alias`, `test_elixir_import` (all in `TestExtractImportName`, lines ~3300-3450)
- Test: `tests/test_mcp_server.py`, new tests for Python and Swift (whose existing tests use single-segment fixtures that don't exercise the truncation bug)

**Interfaces:**
- Produces: these branches now capture the full dotted/qualified name instead of the first segment.

- [ ] **Step 1: Update the existing tests that assert first-segment truncation**

```python
    def test_java_import(self, tmp_path):
        pytest.importorskip("tree_sitter_java")
        import mcp_server
        source = b'import java.util.List;'
        node = _parse_import_node("java", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "java")
        assert result == ["java.util.List"]

    def test_csharp_using_dotted(self, tmp_path):
        pytest.importorskip("tree_sitter_c_sharp")
        import mcp_server
        source = b'using System.Collections.Generic;'
        node = _parse_import_node("c_sharp", source, "using_directive", tmp_path)
        result = mcp_server._extract_import_name(node, "c_sharp")
        assert result == ["System.Collections.Generic"]

    def test_kotlin_import(self, tmp_path):
        pytest.importorskip("tree_sitter_kotlin")
        import mcp_server
        source = b'import kotlin.collections.List'
        node = _parse_import_node("kotlin", source, "import", tmp_path)
        result = mcp_server._extract_import_name(node, "kotlin")
        assert result == ["kotlin.collections.List"]

    def test_scala_import(self, tmp_path):
        pytest.importorskip("tree_sitter_scala")
        import mcp_server
        source = b'import scala.collection.mutable'
        node = _parse_import_node("scala", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "scala")
        assert result == ["scala.collection.mutable"]

    def test_haskell_import(self, tmp_path):
        pytest.importorskip("tree_sitter_haskell")
        import mcp_server
        source = b'import Data.List'
        node = _parse_import_node("haskell", source, "import", tmp_path)
        result = mcp_server._extract_import_name(node, "haskell")
        assert result == ["Data.List"]

    def test_elixir_alias(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'alias MyApp.Router'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["MyApp.Router"]

    def test_elixir_import(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'import Ecto.Query'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["Ecto.Query"]
```

Add two new tests (existing Python/Swift fixtures are single-segment and don't exercise the bug):

```python
    def test_python_import_from_preserves_full_dotted_name(self):
        import mcp_server
        source = b"from pathlib import Path\n"
        result = mcp_server._extract_from_source(
            source, TestExtractFromSource()._python_parser(), "foo.py"
        )
        assert "pathlib" in result["imports"]

    def test_python_dotted_import_preserves_full_name(self):
        import mcp_server
        source = b"import os.path\n"
        result = mcp_server._extract_from_source(
            source, TestExtractFromSource()._python_parser(), "foo.py"
        )
        assert "os.path" in result["imports"]

    def test_swift_submodule_import_preserves_full_name(self, tmp_path):
        pytest.importorskip("tree_sitter_swift")
        import mcp_server
        source = b'import Foundation.NSString'
        node = _parse_import_node("swift", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "swift")
        assert result == ["Foundation.NSString"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestExtractImportName -v`
Expected: FAIL on every updated/new assertion above.

- [ ] **Step 3: Update the Python branch**

At `mcp_server.py:481-494`, change:

```python
    if lang_name == "python":
        if node.type == "import_from_statement":
            m = node.child_by_field_name("module_name")
            if m:
                names.append(m.text.decode("utf-8").split(".")[0])
        else:
            # import_statement: collect all top-level module names
            for child in node.named_children:
                if child.type == "aliased_import":
                    n = child.child_by_field_name("name")
                    if n:
                        names.append(n.text.decode("utf-8").split(".")[0])
                elif child.type == "dotted_name":
                    names.append(child.text.decode("utf-8").split(".")[0])
```

to:

```python
    if lang_name == "python":
        if node.type == "import_from_statement":
            m = node.child_by_field_name("module_name")
            if m:
                # m.text is the raw specifier as written, including relative
                # forms: "pathlib", ".sub", "..pkg" — see _resolve_module_import
                # for how leading dots get resolved against the importing file.
                names.append(m.text.decode("utf-8"))
        else:
            # import_statement: collect all full dotted module names
            for child in node.named_children:
                if child.type == "aliased_import":
                    n = child.child_by_field_name("name")
                    if n:
                        names.append(n.text.decode("utf-8"))
                elif child.type == "dotted_name":
                    names.append(child.text.decode("utf-8"))
```

- [ ] **Step 4: Update the Java branch**

At `mcp_server.py:517-529`, change:

```python
    elif lang_name == "java":
        def _java_leftmost(n) -> Optional[str]:
            if n.type == "identifier":
                return n.text.decode("utf-8")
            for c in n.named_children:
                result = _java_leftmost(c)
                if result:
                    return result
            return None

        result = _java_leftmost(node)
        if result:
            names.append(result)
```

to:

```python
    elif lang_name == "java":
        # import_declaration's dotted path is one named child, already the
        # full text (e.g. "java.util.List") — scoped_identifier for
        # multi-segment paths, plain identifier for single-segment ones.
        for child in node.named_children:
            if child.type in ("scoped_identifier", "identifier"):
                names.append(child.text.decode("utf-8"))
                break
```

- [ ] **Step 5: Update `_csharp_using_name`**

Replace `mcp_server.py:374-389`:

```python
def _csharp_using_name(node) -> Optional[str]:
    """Return the full dotted namespace from a C# using_directive node.

    using System;                     → "System"
    using System.Collections.Generic; → "System.Collections.Generic"

    The dotted path is one named child (qualified_name for multi-segment
    paths, identifier for single-segment ones) whose own .text is already
    the full joined name.
    """
    for child in node.named_children:
        if child.type in ("qualified_name", "identifier"):
            return child.text.decode("utf-8")
    return None
```

- [ ] **Step 6: Update the Kotlin branch**

At `mcp_server.py:549-561`, change:

```python
    elif lang_name == "kotlin":
        def _kotlin_first_seg(n) -> Optional[str]:
            if n.type in ("simple_identifier", "identifier"):
                return n.text.decode("utf-8")
            for c in n.named_children:
                result = _kotlin_first_seg(c)
                if result:
                    return result
            return None

        result = _kotlin_first_seg(node)
        if result:
            names.append(result)
```

to:

```python
    elif lang_name == "kotlin":
        # import node's dotted path is one named child (qualified_identifier
        # for multi-segment, identifier for single-segment) whose .text is
        # already the full joined name.
        for child in node.named_children:
            if child.type in ("qualified_identifier", "identifier"):
                names.append(child.text.decode("utf-8"))
                break
```

- [ ] **Step 7: Update the Swift branch**

At `mcp_server.py:562-566`, change:

```python
    elif lang_name == "swift":
        for child in node.named_children:
            if child.type in ("identifier", "simple_identifier"):
                names.append(child.text.decode("utf-8"))
                break
```

to:

```python
    elif lang_name == "swift":
        # import_declaration's single "identifier" named child already
        # holds the full dotted text (e.g. "Foundation.NSString") directly —
        # no recursion needed.
        for child in node.named_children:
            if child.type in ("identifier", "simple_identifier"):
                names.append(child.text.decode("utf-8"))
                break
```

(No functional change here beyond the comment — the existing code already captured the full node text since it never recursed into `simple_identifier` children for a top-level `identifier` node. Verified against the real tree-sitter-swift grammar: `import_declaration`'s child is type `identifier` whose whole text is `"Foundation.NSString"`, matched by the first branch of the `in` check before ever considering `simple_identifier`.)

- [ ] **Step 8: Update the Scala branch**

At `mcp_server.py:567-571`, change:

```python
    elif lang_name == "scala":
        for child in node.named_children:
            txt = child.text.decode("utf-8")
            names.append(txt.split(".")[0])
            break
```

to:

```python
    elif lang_name == "scala":
        # import_declaration's path is flattened into individual "identifier"
        # named children (no wrapping scoped node), so join the leading run
        # of identifiers rather than taking the first one's text alone.
        segments = []
        for child in node.named_children:
            if child.type != "identifier":
                break
            segments.append(child.text.decode("utf-8"))
        if segments:
            names.append(".".join(segments))
```

- [ ] **Step 9: Update the Haskell branch**

At `mcp_server.py:572-577`, change:

```python
    elif lang_name == "haskell":
        for child in node.named_children:
            if child.type in ("module", "qualified_module", "constructor"):
                txt = child.text.decode("utf-8")
                names.append(txt.split(".")[0])
                break
```

to:

```python
    elif lang_name == "haskell":
        for child in node.named_children:
            if child.type in ("module", "qualified_module", "constructor"):
                names.append(child.text.decode("utf-8"))
                break
```

- [ ] **Step 10: Update `_elixir_module_name`**

At `mcp_server.py:450-475`, in the final loop, change:

```python
    for child in node.children:
        if child.type == "arguments":
            for arg in child.children:
                if arg.type == "alias":
                    txt = arg.text.decode("utf-8")
                    return txt.split(".")[0]
    return None
```

to:

```python
    for child in node.children:
        if child.type == "arguments":
            for arg in child.children:
                if arg.type == "alias":
                    return arg.text.decode("utf-8")
    return None
```

Update the docstring's example lines too (`mcp_server.py:453-456`):

```python
    alias MyApp.Router     → "MyApp.Router"
    import Ecto.Query      → "Ecto.Query"
    use Phoenix.Controller → "Phoenix.Controller"
    require Logger         → "Logger"
```

- [ ] **Step 11: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestExtractImportName -v && pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 12: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "fix: capture full qualified names for namespace-language imports (#97)"
```

---

## Task 12: Generic tiered matcher in `_resolve_module_import`

**Files:**
- Modify: `mcp_server.py:1062-1090` (`_resolve_module_import`, as last touched in Task 9)
- Test: `tests/test_mcp_server.py`, new `TestResolveModuleImportTieredMatcher` class placed after `TestUnresolvedImportTagging`

**Interfaces:**
- Consumes: `file_entities: Dict[str, List[str]]` (existing, keys are repo-relative file paths).
- Produces: two new small helpers, `_path_segments(path_str) -> List[str]` and `_segments_end_with(full_segments, candidate_segments) -> bool`. `_resolve_module_import` now resolves namespace/path-like imports against `file_entities` via two segment-based suffix-match tiers (file match, parent-directory match) before falling through to the external tag. Rust's existing exact conventions are checked first and are unaffected.

**Design note on why this isn't a naive dot-to-slash replace:** Go import paths routinely contain literal dots that are *not* hierarchy separators (`github.com/user/pkg` — `github.com` is one path segment). Blanket-replacing `.` with `/` before matching would corrupt these into `github/com/user/pkg`. The fix: split on `/` when the specifier already contains one (Go, C/C++, Ruby, PHP — already segment-separated by `/`), otherwise split on `.` (Java, C#, Python, Scala, Kotlin, Swift, Haskell, Elixir — genuinely dot-separated, no literal dots expected within a single segment). Matching is then a **suffix comparison over whole path segments** (not a raw substring) against each candidate file's own path — this uniformly handles an exact match, a vendored path with extra prefix segments (`3rdParty/somelib/com/google/gson/Gson.java` still matches `com.google.gson.Gson`), and a bare single-segment name (degenerates to a basename check) with one algorithm, rather than three separate ad hoc tiers.

- [ ] **Step 1: Write the failing tests**

```python
class TestResolveModuleImportTieredMatcher:
    def test_exact_file_match_java_package(self):
        import mcp_server
        file_entities = {"com/google/gson/Gson.java": []}
        ident, is_resolved = mcp_server._resolve_module_import("com.google.gson.Gson", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "com/google/gson/Gson.java")

    def test_parent_directory_match_java_wildcard_style(self):
        import mcp_server
        # "com.google.gson" (no trailing class name) is a package-level
        # reference — it matches via the file's *parent directory*, not the
        # file's own path, since there's no specific file named exactly that.
        file_entities = {"com/google/gson/JsonElement.java": []}
        ident, is_resolved = mcp_server._resolve_module_import("com.google.gson", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "com/google/gson/JsonElement.java")

    def test_genuinely_external_java_package_not_resolved(self):
        import mcp_server
        file_entities = {"com/mycompany/App.java": []}
        # com.fasterxml.jackson.Foo shares no path with the project's own "com" tree
        ident, is_resolved = mcp_server._resolve_module_import("com.fasterxml.jackson.Foo", file_entities)
        assert is_resolved is False

    def test_exact_file_match_go_full_path(self):
        import mcp_server
        # Vendored Go deps live under a vendor/ prefix that never appears in
        # the import string itself — this must match as a segment suffix,
        # not exact path equality, and "github.com" must survive as one path
        # segment rather than being split on its literal dot.
        file_entities = {"vendor/github.com/user/pkg/pkg.go": []}
        ident, is_resolved = mcp_server._resolve_module_import("github.com/user/pkg/pkg", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "vendor/github.com/user/pkg/pkg.go")

    def test_basename_match_vendored_c_header(self):
        import mcp_server
        file_entities = {"3rdParty/icu/include/unicode/uloc.h": []}
        ident, is_resolved = mcp_server._resolve_module_import("unicode/uloc", file_entities)
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "3rdParty/icu/include/unicode/uloc.h")

    def test_genuinely_external_c_stdlib_header_not_resolved(self):
        import mcp_server
        file_entities = {"src/main.cpp": []}
        ident, is_resolved = mcp_server._resolve_module_import("vector", file_entities)
        assert is_resolved is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestResolveModuleImportTieredMatcher -v`
Expected: FAIL — the generic tiers don't exist yet, so anything beyond Rust's exact conventions falls straight to `is_resolved is False`, including the cases that should now resolve.

- [ ] **Step 3: Add the tiered matcher**

Replace `mcp_server.py:1062-1090` (as it stands after Task 9) with:

```python
def _path_segments(path_str: str) -> List[str]:
    """Split a path into non-empty segments, normalizing os.sep to '/'."""
    return [seg for seg in path_str.replace(os.sep, "/").split("/") if seg]


def _segments_end_with(full_segments: List[str], candidate_segments: List[str]) -> bool:
    """True if full_segments' trailing slice equals candidate_segments exactly.

    A whole-segment suffix comparison, not a raw string suffix — comparing
    strings directly would let e.g. "xyzcom/google" wrongly match a
    candidate of "com/google" (the substring is present but not as its own
    path segment).
    """
    if not candidate_segments or len(candidate_segments) > len(full_segments):
        return False
    return full_segments[-len(candidate_segments):] == candidate_segments


def _resolve_module_import(import_name: str, file_entities: Dict[str, List[str]]) -> Tuple[str, bool]:
    """Resolve an import name to a module ident that joins with stored module entities.

    Tries Rust's exact source-root conventions first (src/storage.rs,
    src/storage/mod.rs), then a generic, language-agnostic segment-suffix
    matcher used by every other language. This exists because every other
    language's import extraction already reduces to a bare or dotted/slashed
    specifier (see _extract_import_name) that would otherwise always fall
    through to the external-dependency fallback — including for real in-tree
    vendored code, which must stay internal per the design spec's Non-goals.

    The specifier is split into segments on "/" if present (Go, C/C++, Ruby,
    PHP already use "/" natively — note Go paths like "github.com/user/pkg"
    contain literal dots inside a segment that must NOT be treated as
    separators), otherwise on "." (Java, C#, Python, Scala, Kotlin, Swift,
    Haskell, Elixir — genuinely dot-separated). Matching a whole-segment
    suffix (not a raw substring) against either a file's own path or its
    parent directory uniformly covers: an exact match, a vendored path with
    extra prefix segments, a package-only/wildcard-style import (matches via
    the parent-directory tier), and a bare single-segment name (degenerates
    to a basename check) — one algorithm instead of separate ad hoc tiers.

    Returns (ident, is_resolved). is_resolved is True when import_name
    matched a real file in file_entities, False when it fell through to the
    bare _canonical_ident guess.
    """
    # Priority 1: canonical Rust module root paths under common source roots
    for src_root in ("src", "lib", ""):
        prefix = f"{src_root}/" if src_root else ""
        candidate_file = f"{prefix}{import_name}.rs"
        candidate_mod = f"{prefix}{import_name}/mod.rs"
        if candidate_file in file_entities:
            return _code_ident("module", candidate_file), True
        if candidate_mod in file_entities:
            return _code_ident("module", candidate_mod), True

    # Priority 2: broader search — only match files directly under a src root
    # (parent.parent is the source root, not a nested subdir)
    for file_path in file_entities:
        p = Path(file_path)
        if p.stem == "mod" and p.parent.name == import_name:
            return _code_ident("module", file_path), True

    # Priority 3: generic segment-suffix matcher for every other language.
    candidate_segments = import_name.split("/") if "/" in import_name else import_name.split(".")

    # 3a. file match (exact, or a vendored path with extra prefix segments), extension stripped
    for file_path in file_entities:
        file_segments = _path_segments(str(Path(file_path).with_suffix("")))
        if _segments_end_with(file_segments, candidate_segments):
            return _code_ident("module", file_path), True

    # 3b. parent-directory match (package-only/wildcard-style imports, e.g.
    # a Java "import com.google.gson.*;" or a bare "com.google.gson" reference
    # with no specific trailing class name)
    for file_path in file_entities:
        parent_segments = _path_segments(str(Path(file_path).parent))
        if _segments_end_with(parent_segments, candidate_segments):
            return _code_ident("module", file_path), True

    return _canonical_ident("module", import_name), False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestResolveModuleImportTieredMatcher -v`
Expected: PASS

- [ ] **Step 5: Run the full suite — expect two known, intentional failures**

Run: `pytest tests/ -x -q`
Expected: `TestRunIngestionBitemporalDeps::test_new_import_writes_depends_on_via_ingest_transact` and `::test_removed_import_closes_depends_on_edge` now FAIL. This is expected and fixed in Task 14 — `mod_a.py`'s `import mod_b` now correctly resolves to the real `mod_b.py` module (via the 3a segment-suffix tier, since a single-segment candidate against `mod_b.py`'s single-segment stem degenerates to a basename match) instead of the old external fallback ident, since `mod_b.py` genuinely exists in `file_entities`. Do not treat this as a regression to revert; do not skip Task 14.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add generic tiered import resolution across all languages (#97)"
```

---

## Task 13: Relative-import resolution

**Files:**
- Modify: `mcp_server.py:1062-1108` (`_resolve_module_import`, as it stands after Task 12) — add `importing_file` parameter
- Modify: `mcp_server.py:2596` (call site inside `_run_ingestion`, updated in Task 9's Step 4) — pass `importing_file=file_path`
- Test: `tests/test_mcp_server.py`, new `TestResolveModuleImportRelative` class placed after `TestResolveModuleImportTieredMatcher`

**Interfaces:**
- Produces: `_resolve_module_import(import_name, file_entities, importing_file: Optional[str] = None) -> Tuple[str, bool]`. When `import_name` starts with `.` and `importing_file` is given, resolves relative to the importing file's directory before running the tiered matcher; an unresolved relative import is never tagged external (handled by the caller already only tagging on `is_resolved is False` for the generic external label, which the spec's Section 3 says to skip for relative specifiers — see Step 4's caller note).

- [ ] **Step 1: Write the failing tests**

```python
class TestResolveModuleImportRelative:
    def test_js_relative_import_resolves_against_importing_file(self):
        import mcp_server
        file_entities = {"src/utils/foo.ts": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "./utils/foo", file_entities, importing_file="src/main.ts",
        )
        assert is_resolved is True
        assert ident == mcp_server._code_ident("module", "src/utils/foo.ts")

    def test_js_parent_relative_import_resolves(self):
        import mcp_server
        file_entities = {"lib.ts": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "../lib", file_entities, importing_file="src/main.ts",
        )
        assert is_resolved is True

    def test_python_single_dot_relative_import_resolves(self):
        import mcp_server
        file_entities = {"pkg/sibling.py": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            ".sibling", file_entities, importing_file="pkg/main.py",
        )
        assert is_resolved is True

    def test_python_double_dot_relative_import_resolves(self):
        import mcp_server
        # For a file at a/b/c/main.py, the containing package is a.b.c: one
        # leading dot means "this package" (a/b/c), two dots means "the
        # parent package" (a/b) — so "..sibling" resolves to a/b/sibling.py,
        # not a top-level sibling.py.
        file_entities = {"a/b/sibling.py": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "..sibling", file_entities, importing_file="a/b/c/main.py",
        )
        assert is_resolved is True

    def test_ruby_require_relative_marker_resolves(self):
        import mcp_server
        file_entities = {"lib/helper.rb": []}
        ident, is_resolved = mcp_server._resolve_module_import(
            "./helper", file_entities, importing_file="lib/main.rb",
        )
        assert is_resolved is True

    def test_unresolved_relative_import_is_not_tagged_external(self):
        import mcp_server
        file_entities: dict = {}
        ident, is_resolved = mcp_server._resolve_module_import(
            "./missing", file_entities, importing_file="src/main.ts",
        )
        assert is_resolved is False
        # Caller-side contract (see _run_ingestion): a relative import is only
        # ever tagged external if the generic (non-relative) tiers would also
        # tag it — this test documents that resolution itself still reports
        # is_resolved=False for a genuinely missing relative target, same as
        # any other unresolved import; the "don't mislabel" guarantee lives in
        # the caller, verified by Task 13's Step 5 integration test below.
```

Add one integration test in `TestUnresolvedImportTagging` (or a new small class) verifying the caller-side contract:

```python
    @pytest.mark.asyncio
    async def test_unresolved_relative_import_not_tagged_external_end_to_end(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.ts").write_text("import { thing } from './missing';\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add main"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        missing_ident = mcp_server._canonical_ident("module", "./missing")
        assert not any(
            f"[{missing_ident} :entity-type :type/external-dependency]" in t for t in transact_calls
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py::TestResolveModuleImportRelative -v`
Expected: FAIL — `_resolve_module_import` doesn't accept `importing_file` yet (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Add relative-import resolution**

Replace the entire `_resolve_module_import` function (as it stands after Task 12) with this complete version — the only change is the new relative-import branch inserted before Priority 1, plus the new `importing_file` parameter; Priorities 1, 2, and 3 are otherwise identical to Task 12:

```python
def _resolve_module_import(
    import_name: str,
    file_entities: Dict[str, List[str]],
    importing_file: Optional[str] = None,
) -> Tuple[str, bool]:
    """Resolve an import name to a module ident that joins with stored module entities.

    Tries a relative-import resolution first (see below), then Rust's exact
    source-root conventions (src/storage.rs, src/storage/mod.rs), then a
    generic, language-agnostic segment-suffix matcher used by every other
    language. This exists because every other language's import extraction
    already reduces to a bare or dotted/slashed specifier (see
    _extract_import_name) that would otherwise always fall through to the
    external-dependency fallback — including for real in-tree vendored code,
    which must stay internal per the design spec's Non-goals.

    When import_name is relative (starts with ".") and importing_file is
    given, resolves against the importing file's own directory before any
    other tier runs. Covers three conventions: JS/TS/Ruby-style "./foo" and
    "../foo/bar" (plain relative filesystem paths — Ruby's require_relative
    results already carry this "./" prefix, added by _ruby_require_name),
    and Python-style leading dots with no slash ("." = same package, each
    extra dot = one directory further up) followed by an optional dotted
    module path.

    The generic matcher splits the specifier into segments on "/" if present
    (Go, C/C++, Ruby, PHP already use "/" natively — note Go paths like
    "github.com/user/pkg" contain literal dots inside a segment that must
    NOT be treated as separators), otherwise on "." (Java, C#, Python, Scala,
    Kotlin, Swift, Haskell, Elixir — genuinely dot-separated). Matching a
    whole-segment suffix (not a raw substring) against either a file's own
    path or its parent directory uniformly covers: an exact match, a
    vendored path with extra prefix segments, a package-only/wildcard-style
    import (via the parent-directory tier), and a bare single-segment name
    (degenerates to a basename check).

    Returns (ident, is_resolved). is_resolved is True when import_name
    matched a real file in file_entities, False when it fell through to the
    bare _canonical_ident guess.
    """
    if importing_file and import_name.startswith("."):
        base_dir = Path(importing_file).parent
        if import_name.startswith("./") or import_name.startswith("../"):
            target = os.path.normpath(str(base_dir / import_name))
        else:
            stripped = import_name.lstrip(".")
            levels_up = len(import_name) - len(stripped) - 1
            target_dir = base_dir
            for _ in range(levels_up):
                target_dir = target_dir.parent
            target = str(target_dir / stripped.replace(".", "/")) if stripped else str(target_dir)
            target = os.path.normpath(target)
        target = target.replace(os.sep, "/")
        for file_path in file_entities:
            if str(Path(file_path).with_suffix("")).replace(os.sep, "/") == target:
                return _code_ident("module", file_path), True
        return _canonical_ident("module", import_name), False

    # Priority 1: canonical Rust module root paths under common source roots
    for src_root in ("src", "lib", ""):
        prefix = f"{src_root}/" if src_root else ""
        candidate_file = f"{prefix}{import_name}.rs"
        candidate_mod = f"{prefix}{import_name}/mod.rs"
        if candidate_file in file_entities:
            return _code_ident("module", candidate_file), True
        if candidate_mod in file_entities:
            return _code_ident("module", candidate_mod), True

    # Priority 2: broader search — only match files directly under a src root
    # (parent.parent is the source root, not a nested subdir)
    for file_path in file_entities:
        p = Path(file_path)
        if p.stem == "mod" and p.parent.name == import_name:
            return _code_ident("module", file_path), True

    # Priority 3: generic segment-suffix matcher for every other language.
    candidate_segments = import_name.split("/") if "/" in import_name else import_name.split(".")

    # 3a. file match (exact, or a vendored path with extra prefix segments), extension stripped
    for file_path in file_entities:
        file_segments = _path_segments(str(Path(file_path).with_suffix("")))
        if _segments_end_with(file_segments, candidate_segments):
            return _code_ident("module", file_path), True

    # 3b. parent-directory match (package-only/wildcard-style imports)
    for file_path in file_entities:
        parent_segments = _path_segments(str(Path(file_path).parent))
        if _segments_end_with(parent_segments, candidate_segments):
            return _code_ident("module", file_path), True

    return _canonical_ident("module", import_name), False
```

- [ ] **Step 4: Update the call site to pass `importing_file`**

At `mcp_server.py:2596` (as it stands after Task 9's Step 4), change:

```python
                                dep_ident, is_resolved = _resolve_module_import(import_name, file_entities)
```

to:

```python
                                dep_ident, is_resolved = _resolve_module_import(
                                    import_name, file_entities, importing_file=file_path,
                                )
```

The existing `if not is_resolved and dep_ident not in entity_valid_from:` tagging guard already does the right thing for relative imports with no further change: an unresolved relative specifier still reports `is_resolved=False` and would, by that guard, get tagged external. Add the guard the spec requires — a relative specifier is never tagged external, resolved or not:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestResolveModuleImportRelative -v tests/test_mcp_server.py::TestUnresolvedImportTagging -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: resolve relative imports against the importing file's path (#97)"
```

---

## Task 14: Update the two pre-existing dependency tests for the new correct resolution

**Files:**
- Modify: `tests/test_mcp_server.py:3181-3239` (`TestRunIngestionBitemporalDeps`, the two tests flagged as expected-to-fail at the end of Task 12)

**Interfaces:**
- Consumes: nothing new — this task only updates test expectations to match the now-correct behavior from Tasks 12-13.

- [ ] **Step 1: Update the two tests**

```python
    @pytest.mark.asyncio
    async def test_new_import_writes_depends_on_via_ingest_transact(
        self, mock_minigraf_db, git_repo_with_deps, monkeypatch
    ):
        """Adding a file with an import must call _ingest_transact with a :depends-on triple
        using the git commit timestamp."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_deps / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)

        await mcp_server._run_ingestion(str(git_repo_with_deps), "HEAD")

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        # mod_b.py genuinely exists in file_entities, so the generalized
        # tiered matcher (Task 12) now resolves "mod_b" to the real internal
        # module via the basename tier, instead of the old Rust-only fallback.
        mod_b_resolved = mcp_server._code_ident("module", "mod_b.py")
        dep_triple = f"{mod_a_ident} :depends-on {mod_b_resolved}"
        assert any(dep_triple in t for t in transact_calls), (
            f"Expected _ingest_transact to be called with '{dep_triple}' during commit loop, "
            f"got: {transact_calls}"
        )

    @pytest.mark.asyncio
    async def test_removed_import_closes_depends_on_edge(
        self, mock_minigraf_db, git_repo_with_dep_removal, monkeypatch
    ):
        """Removing an import in a modified file must call _ingest_close with the
        :depends-on triple so the edge gets a :valid-to bound."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo_with_dep_removal / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        close_triples_seen: list = []
        monkeypatch.setattr(
            mcp_server, "_ingest_close",
            lambda db, triples, orig_ts, commit_ts, reason: close_triples_seen.extend(triples),
        )

        await mcp_server._run_ingestion(str(git_repo_with_dep_removal), "HEAD")

        mod_a_ident = mcp_server._code_ident("module", "mod_a.py")
        mod_b_resolved = mcp_server._code_ident("module", "mod_b.py")
        dep_triple = f"{mod_a_ident} :depends-on {mod_b_resolved}"
        assert any(dep_triple in t for t in close_triples_seen), (
            f"Expected _ingest_close to be called with '{dep_triple}' when import removed, "
            f"got: {close_triples_seen}"
        )
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py::TestRunIngestionBitemporalDeps -v`
Expected: PASS

- [ ] **Step 3: Run the entire suite**

Run: `pytest tests/ -q`
Expected: PASS, zero failures, zero unexpected skips (importorskip-guarded tests skip only if a grammar package is genuinely absent).

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test: update dependency-edge tests for the now-correct mod_b resolution (#97)"
```

---

## Task 15: Document `:type/external-dependency` in SKILL.md

**Files:**
- Modify: `SKILL.md` (insert a new subsection into "Git-Ingested Data Schema", after the existing `:type/tag` subsection at line ~363, before "**Supported languages...**" at line 365)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Add the new schema subsection**

Insert after `SKILL.md:363` (the `:tagged-commit` row of the `:type/tag` table) and before line 365 (`**Supported languages for AST extraction:**`):

```markdown

#### `:type/external-dependency` — real git submodules and genuinely-unresolved imports
Ident: `:module/<slugified-path-or-import-name>` (shares the module ident namespace — only `:entity-type` distinguishes internal from external)

| Attribute | Notes |
|---|---|
| `:description` | submodule's declared name from `.gitmodules` if resolvable, else raw path (submodules); raw import specifier (unresolved imports) |
| `:path` | submodule's repo path (submodules only; absent for unresolved-import placeholders — they have no path) |
| `:pinned-commit` | pinned commit SHA the submodule currently points to (submodules only); bi-temporally closed and reopened on every bump — point-in-time queries see the SHA pinned at that time |
| `:submodule-name` / `:submodule-url` | from `.gitmodules`, when parseable (submodules only) |
| `:introduced-by` (keyword ref) | commit that first introduced this dependency |
| `:modified-in` (keyword ref) | one edge per commit that bumped a submodule's pinned commit |

Vendored-in-tree code checked in as regular files (not a git submodule) is parsed as ordinary `:type/module`/`:function`/`:class` entities like any first-party code — only real gitlinks (mode `160000`) and genuinely-unresolved imports get the external marker.
```

- [ ] **Step 2: Commit**

```bash
git add SKILL.md
git commit -m "docs: document :type/external-dependency schema (#97)"
```

---

## Final Self-Review Checklist (for whoever executes this plan)

- [ ] Every task's tests pass in isolation and the full suite (`pytest tests/ -q`) is green after Task 14.
- [ ] `git log --oneline` shows one commit per task (15 commits), each independently revertable.
- [ ] Confirm no task left a `TODO`/placeholder in `mcp_server.py` — grep for `TODO` and `FIXME` before closing out.
- [ ] Spec coverage: Section 1 (schema) → Tasks 8, 15. Section 2 (detection/control flow) → Tasks 3-9. Section 3 (generalized resolution + relative imports) → Tasks 10-14. Section 4 (TSX fix) → Tasks 1-2.
