# Git Ingestion Path-Ignore Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let git ingestion skip vendored/third-party/generated paths entirely (no tree-sitter parse, no per-file entities), while in-repo files that import from a now-excluded path still resolve to a single `:type/external-dependency` entity via the existing unresolved-import fallback — the same treatment already given to real external packages and gitlink submodules.

**Architecture:** A pure glob/prefix matcher (`_is_ignored_path`) and a config loader (`_load_ignore_patterns`, merging built-in defaults + `MINIGRAF_INGEST_IGNORE` env var + an optional `.temporalignore` file) plug into two existing chokepoints — `_known_files_at_commit` (excludes ignored paths from the known-files set used for import resolution) and `_extract_commit`'s per-file loop (skips ignored files before `_thread_parser`/tree-sitter run). `_run_ingestion` resolves the pattern list once and threads it through the `ProcessPoolExecutor` submission call. No new entity type or write-path code.

**Tech Stack:** Python stdlib only (`fnmatch`, `pathlib`) — no new dependency.

## Global Constraints

- No new PyPI dependency (rejected `pathspec` in the design — see spec's "Matching" section).
- `MINIGRAF_INGEST_IGNORE` env var naming follows the existing `MINIGRAF_INGEST_WORKERS` convention (mcp_server.py:3173).
- Ignore config is resolved once per ingestion run from the current working tree, not re-read per historical commit.
- Forward-only: no retroactive purge/backfill of already-ingested vendored entities.
- New `ignore_patterns` parameters on `_known_files_at_commit`/`_extract_commit` must default to `()` so none of the 10 existing call sites in `tests/test_mcp_server.py` (lines 2261, 2270, 2287, 2294, 2392, 2410, 2422, 2436, 2468, 2493) need to change.

---

### Task 1: `_is_ignored_path` matcher

**Files:**
- Modify: `mcp_server.py` (add `import fnmatch` near line 16; add `Sequence` to the `typing` import at line 24; add new function immediately before `_known_files_at_commit` at line 1585)
- Test: `tests/test_mcp_server.py` (new `TestIsIgnoredPath` class, placed immediately before `TestKnownFilesAtCommit` at line 2256)

**Interfaces:**
- Produces: `_is_ignored_path(file_path: str, patterns: Sequence[str]) -> bool` — pure function, no I/O. Used by Task 3 and Task 4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` immediately before `class TestKnownFilesAtCommit:` (line 2256):

```python
class TestIsIgnoredPath:
    def test_directory_pattern_matches_nested_path(self):
        import mcp_server
        assert mcp_server._is_ignored_path("src/vendor/foo.js", ["vendor/"]) is True

    def test_directory_pattern_matches_top_level_path(self):
        import mcp_server
        assert mcp_server._is_ignored_path("vendor/bar.js", ["vendor/"]) is True

    def test_directory_pattern_does_not_match_substring(self):
        import mcp_server
        assert mcp_server._is_ignored_path("vendored_thing.js", ["vendor/"]) is False

    def test_glob_pattern_matches_basename(self):
        import mcp_server
        assert mcp_server._is_ignored_path("dist/app.min.js", ["*.min.js"]) is True

    def test_glob_pattern_no_match_on_unrelated_file(self):
        import mcp_server
        assert mcp_server._is_ignored_path("dist/app.js", ["*.min.js"]) is False

    def test_map_glob_pattern_matches(self):
        import mcp_server
        assert mcp_server._is_ignored_path("dist/app.js.map", ["*.map"]) is True

    def test_exact_segment_match(self):
        import mcp_server
        assert mcp_server._is_ignored_path("a/node_modules/pkg/index.js", ["node_modules"]) is True

    def test_exact_basename_match(self):
        import mcp_server
        assert mcp_server._is_ignored_path("some/path/README.md", ["README.md"]) is True

    def test_no_patterns_never_matches(self):
        import mcp_server
        assert mcp_server._is_ignored_path("src/main.py", []) is False

    def test_no_matching_pattern_returns_false(self):
        import mcp_server
        assert mcp_server._is_ignored_path("src/main.py", ["vendor/", "*.min.js"]) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestIsIgnoredPath -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_is_ignored_path'`

- [ ] **Step 3: Implement**

In `mcp_server.py`, change the import line at line 16 from:

```python
import re
```

to:

```python
import fnmatch
import re
```

(alphabetical order — `fnmatch` sorts before `re`, but after `datetime`/`json`/`multiprocessing`/`os`; insert it right after `datetime` at line 12 to keep the existing alphabetical ordering: `asyncio, concurrent.futures, configparser, contextlib, datetime, fnmatch, json, multiprocessing, os, re, signal, ...`)

So the full corrected import block (lines 8-24) becomes:

```python
import asyncio
import concurrent.futures
import configparser
import contextlib
import datetime
import fnmatch
import json
import multiprocessing
import os
import re
import signal
import subprocess as _subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
```

Then, immediately before `def _known_files_at_commit(...)` at line 1585, add:

```python
def _is_ignored_path(file_path: str, patterns: Sequence[str]) -> bool:
    """Simplified .gitignore-style match: no negation, no ** anchoring, no new
    dependency (see 2026-07-14 path-ignore design doc's "Matching" section for
    why full gitignore semantics via pathspec were rejected).

    - Pattern ending in "/": matches if that name is any path segment
      (directory-anywhere-in-path semantics — "vendor/" matches both
      "src/vendor/foo.js" and "vendor/bar.js", but never a bare substring
      like "vendored_thing.js").
    - Pattern containing a glob char (*, ?, [): fnmatch against the
      basename, then the full path.
    - Otherwise: exact match against any path segment or the basename.
    """
    segments = Path(file_path).parts
    basename = segments[-1] if segments else file_path
    for pattern in patterns:
        if pattern.endswith("/"):
            if pattern.rstrip("/") in segments:
                return True
        elif any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(file_path, pattern):
                return True
        elif pattern in segments or pattern == basename:
            return True
    return False


```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestIsIgnoredPath -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _is_ignored_path glob/prefix matcher for git ingestion (#115)"
```

---

### Task 2: `_load_ignore_patterns` config loader

**Files:**
- Modify: `mcp_server.py` (add function immediately after `_is_ignored_path`, before `_known_files_at_commit`)
- Test: `tests/test_mcp_server.py` (new `TestLoadIgnorePatterns` class, placed immediately after `TestIsIgnoredPath`)

**Interfaces:**
- Consumes: nothing from Task 1 (independent pure/IO function).
- Produces: `_load_ignore_patterns(repo_path: str) -> List[str]`. Used by Task 5 (`_run_ingestion`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` immediately after the `TestIsIgnoredPath` class body (before `class TestKnownFilesAtCommit:`):

```python
class TestLoadIgnorePatterns:
    def test_defaults_present_with_no_env_or_file(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "vendor/" in patterns
        assert "third_party/" in patterns
        assert "3rdParty/" in patterns
        assert "node_modules/" in patterns
        assert "dist/" in patterns
        assert "build/" in patterns
        assert "*.min.js" in patterns
        assert "*.map" in patterns

    def test_env_var_patterns_are_appended(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_INGEST_IGNORE", "generated/,*.pb.go")
        repo = tmp_path / "repo"
        repo.mkdir()
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "generated/" in patterns
        assert "*.pb.go" in patterns
        assert "vendor/" in patterns  # defaults still present

    def test_temporalignore_file_patterns_are_merged(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".temporalignore").write_text(
            "# comment line\n\nlegacy/\n*.generated.ts\n"
        )
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "legacy/" in patterns
        assert "*.generated.ts" in patterns
        assert "vendor/" in patterns  # defaults still present
        assert "# comment line" not in patterns

    def test_missing_temporalignore_file_is_not_an_error(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.delenv("MINIGRAF_INGEST_IGNORE", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert isinstance(patterns, list)
        assert len(patterns) > 0

    def test_all_three_sources_merge_together(self, tmp_path, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_INGEST_IGNORE", "from_env/")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".temporalignore").write_text("from_file/\n")
        patterns = mcp_server._load_ignore_patterns(str(repo))
        assert "vendor/" in patterns       # default
        assert "from_env/" in patterns     # env var
        assert "from_file/" in patterns    # .temporalignore
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestLoadIgnorePatterns -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_load_ignore_patterns'`

- [ ] **Step 3: Implement**

Add immediately after `_is_ignored_path` (before `_known_files_at_commit`):

```python
_DEFAULT_IGNORE_PATTERNS: Tuple[str, ...] = (
    "3rdParty/", "third_party/", "vendor/", "node_modules/",
    "dist/", "build/", "*.min.js", "*.map",
)


def _load_ignore_patterns(repo_path: str) -> List[str]:
    """Resolve the effective ignore-pattern list for one ingestion run.

    Merges, in order: built-in defaults, MINIGRAF_INGEST_IGNORE (comma-separated),
    and an optional .temporalignore file (one pattern per line, blank lines and
    "#"-prefixed comments skipped) read once from repo_path's current working
    tree — not re-read per historical commit, since ignore config describes how
    this run should behave, not something that varies commit-to-commit.
    """
    patterns: List[str] = list(_DEFAULT_IGNORE_PATTERNS)

    env_patterns = os.environ.get("MINIGRAF_INGEST_IGNORE")
    if env_patterns:
        patterns.extend(p.strip() for p in env_patterns.split(",") if p.strip())

    ignore_file = Path(repo_path) / ".temporalignore"
    if ignore_file.is_file():
        for line in ignore_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)

    return patterns


```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestLoadIgnorePatterns -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add _load_ignore_patterns config loader for git ingestion (#115)"
```

---

### Task 3: Wire into `_known_files_at_commit`

**Files:**
- Modify: `mcp_server.py:1585` (`_known_files_at_commit` signature + body)
- Test: `tests/test_mcp_server.py` (add tests to existing `TestKnownFilesAtCommit` class)

**Interfaces:**
- Consumes: `_is_ignored_path` (Task 1).
- Produces: `_known_files_at_commit(repo_path: str, commit_hash: str, ignore_patterns: Sequence[str] = ()) -> Dict[str, List[str]]` — new optional third parameter, default `()` (empty — matches current behavior when omitted, so all 4 existing call sites at lines 2261/2270/2287/2294 keep passing unchanged). Consumed by Task 5 (`_extract_commit`'s internal call to this function).

- [ ] **Step 1: Write the failing tests**

Add to the existing `TestKnownFilesAtCommit` class in `tests/test_mcp_server.py` (after `test_returned_dict_shape_matches_file_entities`, before `class TestGitlinkChanges:`):

```python
    def test_ignored_path_excluded_even_with_supported_extension(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "main.py").write_text("def f(): pass\n")
        (repo / "vendor").mkdir()
        (repo / "vendor" / "lib.py").write_text("def g(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(repo), commits[0][0], ["vendor/"])
        assert "main.py" in known
        assert "vendor/lib.py" not in known

    def test_no_ignore_patterns_keeps_default_behavior(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        known = mcp_server._known_files_at_commit(str(git_repo), commits[0][0])
        assert "auth.py" in known
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestKnownFilesAtCommit -v`
Expected: `test_ignored_path_excluded_even_with_supported_extension` FAILs with `TypeError: _known_files_at_commit() takes 2 positional arguments but 3 were given`; the other new test passes already (no signature change yet).

- [ ] **Step 3: Implement**

In `mcp_server.py`, change the `_known_files_at_commit` signature and body (line 1585 onward):

```python
def _known_files_at_commit(
    repo_path: str, commit_hash: str, ignore_patterns: Sequence[str] = ()
) -> Dict[str, List[str]]:
    """Return {file_path: []} for every file tracked at commit_hash whose extension
    has a supported tree-sitter grammar (_EXT_TO_LANG) and that doesn't match
    ignore_patterns (see _is_ignored_path) — excluding a vendored path here means
    any import resolving against it falls through to the external-dependency
    fallback in _resolve_module_import instead of matching internally (#115).

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
        if Path(path).suffix.lower() in _EXT_TO_LANG and not _is_ignored_path(path, ignore_patterns):
            known[path] = []
    return known
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestKnownFilesAtCommit -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full existing test file to check for regressions**

Run: `python -m pytest tests/test_mcp_server.py -k "KnownFiles or ExtractCommit" -v`
Expected: PASS (all existing `_known_files_at_commit`/`_extract_commit` call sites still work since the new param defaults to `()`)

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: exclude ignored paths from _known_files_at_commit (#115)"
```

---

### Task 4: Wire into `_extract_commit`

**Files:**
- Modify: `mcp_server.py:3043` (`_extract_commit` signature + per-file loop, and its internal call to `_known_files_at_commit`)
- Test: `tests/test_mcp_server.py` (add tests to existing `TestExtractCommit` class)

**Interfaces:**
- Consumes: `_is_ignored_path` (Task 1), `_known_files_at_commit(..., ignore_patterns=...)` (Task 3).
- Produces: `_extract_commit(repo_path: str, commit_hash: str, ignore_patterns: Sequence[str] = ()) -> Tuple[List[tuple], List[tuple], Dict[str, Dict[str, str]]]` — new optional third parameter, default `()` (all 6 existing call sites at lines 2392/2410/2422/2436/2468/2493 keep passing unchanged). Consumed by Task 5 (`_run_ingestion`'s submission call).

- [ ] **Step 1: Write the failing tests**

Add to the existing `TestExtractCommit` class in `tests/test_mcp_server.py` (find the class's last test method and add these directly after it, before the next `class` definition):

```python
    def test_ignored_file_produces_no_results_entry(self, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "lib.py").write_text("def vendored_fn(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add vendored lib"], cwd=repo, check=True, capture_output=True)

        commits = mcp_server._git_commits(str(repo), watermark_hash=None)
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(
            str(repo), commits[0][0], ["vendor/"]
        )
        assert results == []

    def test_no_ignore_patterns_keeps_default_behavior(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]
        results, gitlink_changes, gitmodules_map = mcp_server._extract_commit(str(git_repo), first_hash)
        assert len(results) == 1
        assert results[0][1] == "auth.py"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommit -v`
Expected: `test_ignored_file_produces_no_results_entry` FAILs with `TypeError: _extract_commit() takes 2 positional arguments but 3 were given`; the other new test passes already.

- [ ] **Step 3: Implement**

In `mcp_server.py`, change the `_extract_commit` signature (line 3043) and its per-file loop (lines 3087-3105):

```python
def _extract_commit(
    repo_path: str, commit_hash: str, ignore_patterns: Sequence[str] = ()
) -> Tuple[List[tuple], List[tuple], Dict[str, Dict[str, str]]]:
```

(docstring unchanged except adding one sentence — see below), and inside the function body, change:

```python
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
            segment_index = _SegmentSuffixIndex(known_files)
```

to:

```python
    for status, old_mode, new_mode, old_sha, new_sha, file_path in raw_entries:
        if _is_ignored_path(file_path, ignore_patterns):
            continue
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
            known_files = _known_files_at_commit(repo_path, commit_hash, ignore_patterns)
            segment_index = _SegmentSuffixIndex(known_files)
```

Also add one paragraph to the docstring, inserted right after the first paragraph
(`"""Read-only, stateless per-commit extraction: ...` through `...unlike the
incrementally-mutated file_entities/entity_valid_from state only the serial main
thread maintains."""`'s first paragraph) and before the `"Runs in a worker process..."`
paragraph:

```
    ignore_patterns (see _is_ignored_path/_load_ignore_patterns) are checked first,
    before _thread_parser even runs — an ignored file costs zero parse time and is
    also excluded from known_files, so anything importing it falls through to the
    external-dependency fallback instead of resolving internally (#115).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommit -v`
Expected: PASS (all tests in the class, including the two new ones)

- [ ] **Step 5: Run the full existing test file to check for regressions**

Run: `python -m pytest tests/test_mcp_server.py -v 2>&1 | tail -30`
Expected: same pass/skip counts as before this task (no regressions) — this is the last task that touches `_extract_commit`/`_known_files_at_commit` in isolation, so it's the right point to confirm nothing else broke before wiring `_run_ingestion` in Task 5.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: skip ignored files in _extract_commit before parsing (#115)"
```

---

### Task 5: Wire `_run_ingestion` end-to-end + regression tests

**Files:**
- Modify: `mcp_server.py:3115` (`_run_ingestion` — resolve ignore patterns once, pass through the `ProcessPoolExecutor` submission call)
- Test: `tests/test_mcp_server.py` (new `TestGitIngestionPathIgnore` class, placed immediately after `TestUnresolvedImportTagging` at line 4608, i.e. right before `class TestResolveModuleImportTieredMatcher:`)

**Interfaces:**
- Consumes: `_load_ignore_patterns` (Task 2), `_extract_commit(..., ignore_patterns=...)` (Task 4).
- Produces: nothing new consumed by later tasks — this is the final wiring task.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` immediately after the `TestUnresolvedImportTagging` class (before `class TestResolveModuleImportTieredMatcher:`):

```python
class TestGitIngestionPathIgnore:
    def _make_progress(self):
        return {"status": "idle", "processed": 0, "total": 0, "current_commit": "", "error": None}

    @pytest.mark.asyncio
    async def test_default_ignored_directory_produces_no_code_entities(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """A file under a default-ignored directory (vendor/) must not produce
        any :type/module, :type/function, or :type/class triples."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "lib.py").write_text("def vendored_fn(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add vendored lib"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        vendored_module_ident = mcp_server._code_ident("module", "vendor/lib.py")
        assert not any(vendored_module_ident in t for t in transact_calls)
        assert not any(":entity-type :type/function" in t for t in transact_calls)
        assert not any(":entity-type :type/class" in t for t in transact_calls)

    @pytest.mark.asyncio
    async def test_import_into_ignored_path_becomes_external_dependency(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """Before this feature, vendor/foo.py would resolve as a normal in-tree
        module (see _resolve_module_import's segment-suffix matcher) and
        main.py's import of it would create an internal :depends-on edge, not
        an external-dependency entity. Excluding vendor/ from known_files must
        make it fall through to the same fallback used for real external
        packages (see TestUnresolvedImportTagging)."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "vendor").mkdir()
        (repo / "vendor" / "foo.py").write_text("def helper(): pass\n")
        (repo / "main.py").write_text("import vendor.foo\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add vendor and main"], cwd=repo, check=True, capture_output=True)

        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        external_ident = mcp_server._canonical_ident("module", "vendor.foo")
        assert any(
            f"[{external_ident} :entity-type :type/external-dependency]" in t for t in transact_calls
        )

    @pytest.mark.asyncio
    async def test_env_var_ignore_pattern_excludes_custom_directory(
        self, mock_minigraf_db, tmp_path, monkeypatch
    ):
        """MINIGRAF_INGEST_IGNORE must add to the default ignore list, not
        replace it — a custom pattern not in the built-in defaults must still
        be honored."""
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "generated").mkdir()
        (repo / "generated" / "codegen.py").write_text("def generated_fn(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add generated file"], cwd=repo, check=True, capture_output=True)

        monkeypatch.setenv("MINIGRAF_INGEST_IGNORE", "generated/")
        mcp_server.open_db(str(repo / "memory.graph"))
        mcp_server._ingest_progress = self._make_progress()

        transact_calls: list = []
        real_ingest_transact = mcp_server._ingest_transact

        def capture_transact(db, triples, ts_iso, reason=""):
            transact_calls.extend(triples)
            return real_ingest_transact(db, triples, ts_iso, reason)

        monkeypatch.setattr(mcp_server, "_ingest_transact", capture_transact)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        generated_module_ident = mcp_server._code_ident("module", "generated/codegen.py")
        assert not any(generated_module_ident in t for t in transact_calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestGitIngestionPathIgnore -v`
Expected: FAIL — `test_default_ignored_directory_produces_no_code_entities` and
`test_env_var_ignore_pattern_excludes_custom_directory` fail because `_run_ingestion`
doesn't yet resolve/pass ignore patterns (vendor/lib.py and generated/codegen.py still
get ingested as normal modules); `test_import_into_ignored_path_becomes_external_dependency`
fails because `vendor.foo` still resolves internally instead of becoming an
external-dependency.

- [ ] **Step 3: Implement**

In `mcp_server.py`, inside `_run_ingestion` (starting at line 3115), add the ignore-pattern
resolution right after `commits = _git_commits(...)` (line 3160) and before the
`repo_total_result` computation:

```python
        commits = _git_commits(repo_path, watermark, branch)
        ignore_patterns = _load_ignore_patterns(repo_path)
        repo_total_result = _subprocess.run(
```

Then change the extraction submission call inside `submit_next` (line 3232) from:

```python
                    fut = loop.run_in_executor(executor, _extract_commit, repo_path, commit[0])
```

to:

```python
                    fut = loop.run_in_executor(
                        executor, _extract_commit, repo_path, commit[0], ignore_patterns
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestGitIngestionPathIgnore -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `python -m pytest tests/ -v 2>&1 | tail -40`
Expected: same pass/skip counts as the pre-existing baseline plus the new tests from
Tasks 1-5 (10 + 5 + 2 + 2 + 3 = 22 new tests), 0 regressions.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: resolve and apply ignore patterns in _run_ingestion (#115)"
```

---

### Task 6: Docs — SKILL.md

**Files:**
- Modify: `SKILL.md` (insert new paragraph in the `### minigraf_ingest_git` section, immediately after the "Auto-started..." paragraph at line 283)

**Interfaces:**
- Consumes: nothing (docs only).
- Produces: nothing (terminal task).

- [ ] **Step 1: Add the documentation paragraph**

In `SKILL.md`, immediately after this existing line (283):

```
Auto-started at MCP server startup — the server creates a background asyncio task that calls `_run_ingestion(cwd, "HEAD")` immediately. Set `MINIGRAF_NO_AUTO_INGEST=1` to suppress this (useful in eval sandboxes). Incremental: reads the `:ingestion/watermark` entity to determine the last ingested commit, then only processes new commits.
```

insert:

```
Vendored/third-party/generated paths are skipped for AST extraction by default (`3rdParty/`, `third_party/`, `vendor/`, `node_modules/`, `dist/`, `build/`, `*.min.js`, `*.map`) — no per-file entities are created for them, and any in-repo import resolving into an ignored path is tagged `:type/external-dependency` instead of an internal module dependency. Extend the ignore list with `MINIGRAF_INGEST_IGNORE` (comma-separated globs/prefixes, e.g. `MINIGRAF_INGEST_IGNORE=generated/,*.pb.go`) and/or a repo-local `.temporalignore` file (one pattern per line, `#` comments allowed) — both add to the defaults, they don't replace them. Ignore config is resolved once when ingestion starts and applies uniformly across all historical commits; it does not retroactively remove entities from a graph that was already ingested before the ignore list was added.
```

- [ ] **Step 2: Verify the doc renders sensibly**

Run: `grep -n "MINIGRAF_INGEST_IGNORE" SKILL.md`
Expected: one match, in the new paragraph.

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "docs: document MINIGRAF_INGEST_IGNORE and .temporalignore (#115)"
```

---

## Final Verification

- [ ] Run the complete test suite one more time: `python -m pytest tests/ -v 2>&1 | tail -20` — expect 0 failures, 0 regressions vs. the pre-Task-1 baseline.
- [ ] `git log --oneline -6` shows the 6 commits from Tasks 1-6 in order.
- [ ] Open a PR with `Fixes #115` in the body (per this repo's established pattern — see [[project_issue_sequence_2026_07]] memory for why the closing keyword must be in the PR body up front).
