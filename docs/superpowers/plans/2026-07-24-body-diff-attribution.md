# Per-Function Body-Diff Attribution (#221) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `:modified-in`'s current file-broadcast behavior (every entity in a changed file gets flagged, regardless of whether its own body changed) with a real per-entity signal, using a whitespace-insensitive hash comparison of the entity's old (parent-blob) vs. new (this-commit) tree-sitter span — no persisted state required.

**Architecture:** `_extract_commit` already re-parses both the old and new blob for every M/R file to support rename matching, producing `old_entity_nodes`/`new_entity_nodes` (live tree-sitter nodes keyed by name, per category: function/class/variable/field). This plan threads those same nodes into `_precompute_file_triples`, which computes a new `unchanged_idents` set via a whitespace-insensitive hash compare, which `_build_code_triples` then uses to skip `:modified-in` for entities that provably didn't change. Purely additive — no new persisted state, no schema/version bump, no opt-in flag.

**Tech Stack:** Python, tree-sitter (18 language grammars already wired up), pytest, real-backend `minigraf` test fixtures (no mocks — see `docs/testing-conventions.md`).

## Global Constraints

- Design doc: `docs/superpowers/specs/2026-07-24-body-diff-attribution-design.md` — read it first for full rationale; this plan implements it as written.
- No persisted hash sidecar (git's diff-tree parent blob already gives the correct prior-body reference per commit).
- Whitespace-insensitive hashing only for v1 — no comment-stripping, no per-language special-casing.
- Forward-only: no backfill for already-ingested graphs, no opt-in flag. Ships as default behavior.
- Module entities are excluded from this gating — file-level churn on the module entity remains unconditional (the module *is* the file).
- All new tests follow `docs/testing-conventions.md`: real `minigraf`/tree-sitter backends, no mocks. Existing tests must continue to pass unmodified — every new parameter this plan adds must default such that pre-existing call sites (which don't pass old/new node maps) behave exactly as today.
- Branch: work happens on `design-221-body-diff-attribution` (already created off `master`, currently holds only the design-spec commit).

---

### Task 1: `_normalized_body_hash` helper

**Files:**
- Modify: `mcp_server.py:18` (import block — add `import hashlib`)
- Modify: `mcp_server.py:2944` (insert new function after `_collect_entity_nodes`, before the `# DB lifecycle` section header at line 2946)
- Test: `tests/test_mcp_server.py` (new `TestNormalizedBodyHash` class, placed after `TestExtractGlobalsAndFields` or any convenient existing class in that file)

**Interfaces:**
- Produces: `_normalized_body_hash(node: Any) -> str` — takes a live tree-sitter node, returns a hex digest string. Pure function, no side effects, never raises for a valid node (may raise `AttributeError`/`RecursionError` only for a malformed/None node — callers in later tasks are responsible for catching that).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestNormalizedBodyHash:
    def _python_parser(self):
        import mcp_server
        import tree_sitter
        import tree_sitter_python
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(tree_sitter_python.language())
        real_parser = tree_sitter.Parser(real_lang)
        mcp_server._grammar_cache["python"] = real_parser
        return real_parser

    def _login_node(self, parser, source: bytes):
        import mcp_server
        root = parser.parse(source).root_node
        return mcp_server._collect_entity_nodes(root, "python")["function"]["login"]

    def test_whitespace_only_change_produces_identical_hash(self):
        import mcp_server
        parser = self._python_parser()
        node_a = self._login_node(parser, b"def login(user):\n    return user.ok\n")
        node_b = self._login_node(parser, b"def login(user):\n\n    return   user.ok\n\n")
        assert mcp_server._normalized_body_hash(node_a) == mcp_server._normalized_body_hash(node_b)

    def test_real_change_produces_different_hash(self):
        import mcp_server
        parser = self._python_parser()
        node_a = self._login_node(parser, b"def login(user):\n    return user.ok\n")
        node_b = self._login_node(parser, b"def login(user):\n    return user.active\n")
        assert mcp_server._normalized_body_hash(node_a) != mcp_server._normalized_body_hash(node_b)

    def test_comment_only_change_still_counts_as_different(self):
        """v1 scope: only whitespace is normalized, not comments (see the
        design doc's Scope section) -- a comment-only edit still registers
        as a body change. This test locks in that scope decision so a future
        change to it is deliberate, not accidental."""
        import mcp_server
        parser = self._python_parser()
        node_a = self._login_node(parser, b"def login(user):\n    return user.ok\n")
        node_b = self._login_node(parser, b"def login(user):\n    # checks auth\n    return user.ok\n")
        assert mcp_server._normalized_body_hash(node_a) != mcp_server._normalized_body_hash(node_b)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestNormalizedBodyHash -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_normalized_body_hash'`

- [ ] **Step 3: Add the `hashlib` import**

In `mcp_server.py`, the import block currently reads (lines 8-23):

```python
import asyncio
import concurrent.futures
import concurrent.futures.process
import configparser
import contextlib
import datetime
import fnmatch
import json
```

Change to:

```python
import asyncio
import concurrent.futures
import concurrent.futures.process
import configparser
import contextlib
import datetime
import fnmatch
import hashlib
import json
```

- [ ] **Step 4: Implement `_normalized_body_hash`**

In `mcp_server.py`, `_collect_entity_nodes` currently ends at line 2943 with:

```python
    walk(root_node)
    return result


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------
```

Change to:

```python
    walk(root_node)
    return result


def _normalized_body_hash(node: Any) -> str:
    """Whitespace-insensitive content hash of a tree-sitter node's span.

    Joins the text of every leaf token (a node with no children) in
    document order, then hashes the result -- so a purely cosmetic reformat
    (e.g. this repo's own periodic clang-format sweeps) hashes identically
    to the original, while any change to the token stream itself changes
    the hash. No per-language handling needed: leaf-token walking is
    generic across every tree-sitter grammar. Comment text is NOT stripped
    (see #221 design doc's Scope section) -- a comment-only edit still
    counts as a body change in v1.
    """
    leaves: List[bytes] = []

    def walk(n: Any) -> None:
        if len(n.children) == 0:
            leaves.append(n.text)
        else:
            for child in n.children:
                walk(child)

    walk(node)
    return hashlib.sha256(b"\x00".join(leaves)).hexdigest()


# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestNormalizedBodyHash -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _normalized_body_hash helper for #221 body-diff attribution"
```

---

### Task 2: Extend `_precompute_file_triples` with `unchanged_idents`

**Files:**
- Modify: `mcp_server.py:5844-5850` (function signature) and `mcp_server.py:5957-5967` (return statement)
- Test: `tests/test_mcp_server.py` (new `TestPrecomputeFileTriplesBodyDiff` class, placed after `TestPrecomputeGlobalsAndFields`)

**Interfaces:**
- Consumes: `_normalized_body_hash(node: Any) -> str` from Task 1.
- Produces: `_precompute_file_triples(..., old_entity_nodes: Optional[Dict[str, Dict[str, Any]]] = None, new_entity_nodes: Optional[Dict[str, Dict[str, Any]]] = None)` — two new optional keyword params, both defaulting to `None` (treated as `{}`). Return dict gains a new key `"unchanged_idents": Set[str]` — idents whose old-vs-new normalized hash matched. Every existing caller (there are ~15 in `tests/test_mcp_server.py` alone, none passing these params) continues to work unchanged, and gets `unchanged_idents == set()` — i.e. no gating, matching today's behavior exactly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestPrecomputeFileTriplesBodyDiff:
    def _python_parser(self):
        import mcp_server
        import tree_sitter
        import tree_sitter_python
        mcp_server._grammar_cache.clear()
        real_lang = tree_sitter.Language(tree_sitter_python.language())
        real_parser = tree_sitter.Parser(real_lang)
        mcp_server._grammar_cache["python"] = real_parser
        return real_parser

    def _all_nodes(self, parser, source: bytes):
        """Mirrors _extract_commit's own local collect_all_nodes helper:
        _collect_entity_nodes (function/class) widened with
        _extract_globals_and_fields' global/field live nodes."""
        import mcp_server
        root = parser.parse(source).root_node
        base = mcp_server._collect_entity_nodes(root, "python")
        gf = mcp_server._extract_globals_and_fields(root, "python")
        base["variable"] = dict(gf.get("global_nodes", {}))
        base["field"] = dict(gf.get("field_nodes", {}))
        return base

    def test_unchanged_function_body_is_flagged_unchanged(self):
        import mcp_server
        parser = self._python_parser()
        old_nodes = self._all_nodes(parser, b"def login(user):\n    return user.ok\n")
        new_nodes = self._all_nodes(parser, b"def login(user):\n\n    return   user.ok\n")
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result = mcp_server._precompute_file_triples(
            "auth.py", extracted, ":commit/c1", {},
            old_entity_nodes=old_nodes, new_entity_nodes=new_nodes,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert fn_ident in result["unchanged_idents"]

    def test_changed_function_body_is_not_flagged_unchanged(self):
        import mcp_server
        parser = self._python_parser()
        old_nodes = self._all_nodes(parser, b"def login(user):\n    return user.ok\n")
        new_nodes = self._all_nodes(parser, b"def login(user):\n    return user.active\n")
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result = mcp_server._precompute_file_triples(
            "auth.py", extracted, ":commit/c1", {},
            old_entity_nodes=old_nodes, new_entity_nodes=new_nodes,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert fn_ident not in result["unchanged_idents"]

    def test_absent_old_and_new_nodes_default_to_empty_set(self):
        """Every pre-#221 caller of _precompute_file_triples (~15 call sites
        in this test file alone) doesn't pass old_entity_nodes/new_entity_nodes
        at all -- unchanged_idents must default to empty, never suppressing
        :modified-in for callers with no diff context."""
        import mcp_server
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result = mcp_server._precompute_file_triples("auth.py", extracted, ":commit/c1", {})
        assert result["unchanged_idents"] == set()

    def test_class_body_unchanged_is_flagged_unchanged(self):
        import mcp_server
        parser = self._python_parser()
        old_nodes = self._all_nodes(parser, b"class Foo:\n    pass\n")
        new_nodes = self._all_nodes(parser, b"class Foo:\n\n    pass\n")
        extracted = {"functions": [], "classes": ["Foo"], "imports": []}
        result = mcp_server._precompute_file_triples(
            "models.py", extracted, ":commit/c1", {},
            old_entity_nodes=old_nodes, new_entity_nodes=new_nodes,
        )
        cls_ident = mcp_server._code_ident("class", "models.py", "Foo")
        assert cls_ident in result["unchanged_idents"]

    def test_global_variable_unchanged_is_flagged_unchanged(self):
        import mcp_server
        parser = self._python_parser()
        old_nodes = self._all_nodes(parser, b"CONF = 1\n")
        new_nodes = self._all_nodes(parser, b"CONF  =  1\n")
        extracted = {
            "functions": [], "classes": [], "imports": [], "calls": [],
            "globals": ["CONF"], "fields": [],
        }
        result = mcp_server._precompute_file_triples(
            "config.py", extracted, ":commit/c1", {}, segment_index=None,
            old_entity_nodes=old_nodes, new_entity_nodes=new_nodes,
        )
        gvar_ident = mcp_server._code_ident("variable", "config.py", "CONF")
        assert gvar_ident in result["unchanged_idents"]

    def test_field_qualified_name_unchanged_is_flagged_unchanged(self):
        import mcp_server
        parser = self._python_parser()
        old_nodes = self._all_nodes(parser, b"class Foo:\n    def __init__(self):\n        self.bar = 1\n")
        new_nodes = self._all_nodes(parser, b"class Foo:\n    def __init__(self):\n        self.bar  =  1\n")
        extracted = {
            "functions": ["__init__"], "classes": ["Foo"], "imports": [], "calls": [],
            "globals": [], "fields": [("bar", "Foo", False)],
        }
        result = mcp_server._precompute_file_triples(
            "models.py", extracted, ":commit/c1", {}, segment_index=None,
            old_entity_nodes=old_nodes, new_entity_nodes=new_nodes,
        )
        field_ident = mcp_server._code_ident("field", "models.py", "Foo.bar")
        assert field_ident in result["unchanged_idents"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriplesBodyDiff -v`
Expected: FAIL — `test_absent_old_and_new_nodes_default_to_empty_set` fails with `KeyError: 'unchanged_idents'`; the other five fail with `TypeError: _precompute_file_triples() got an unexpected keyword argument 'old_entity_nodes'`

- [ ] **Step 3: Extend the function signature and docstring**

In `mcp_server.py`, the function currently starts (lines 5844-5869):

```python
def _precompute_file_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ident: str,
    known_files: Dict[str, List[str]],
    segment_index: Optional[_SegmentSuffixIndex] = None,
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

    segment_index, if given, must be a _SegmentSuffixIndex built from that same
    known_files — _extract_commit builds it once per commit and passes it here for
    every A/M file so _resolve_module_import's tiers 3a/3b aren't rebuilding it (or
    linear-scanning known_files) once per import.
    """
```

Change to:

```python
def _precompute_file_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ident: str,
    known_files: Dict[str, List[str]],
    segment_index: Optional[_SegmentSuffixIndex] = None,
    old_entity_nodes: Optional[Dict[str, Dict[str, Any]]] = None,
    new_entity_nodes: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Pure, per-commit-independent precomputation for _build_code_triples.

    Runs inside _extract_commit on the worker pool. Computes everything that does
    NOT depend on the serially-maintained entity_valid_from/file_deps state:
      - the candidate triple strings for the module/function/class idents this file
        would introduce, ready to use verbatim IF the main thread's diff against
        entity_valid_from decides the ident is genuinely new (see _build_code_triples);
      - the resolved dependency ident for every import in the file, via
        _resolve_module_import against known_files (this commit's own git-ls-tree
        state, not the incrementally-mutated file_entities);
      - (#221) unchanged_idents: idents whose body provably did NOT change in
        this commit, via old_entity_nodes/new_entity_nodes.

    known_files must come from _known_files_at_commit for the SAME commit_hash this
    file was extracted from — it determines what "is_resolved" means here.

    segment_index, if given, must be a _SegmentSuffixIndex built from that same
    known_files — _extract_commit builds it once per commit and passes it here for
    every A/M file so _resolve_module_import's tiers 3a/3b aren't rebuilding it (or
    linear-scanning known_files) once per import.

    old_entity_nodes/new_entity_nodes, if given, are the SAME category-keyed
    ("function"/"class"/"variable"/"field") live tree-sitter node maps
    _extract_commit's own collect_all_nodes already produces from the old
    (parent-blob) and new (this commit's) parse of this file, for rename
    matching. Reused here (#221) as a per-entity body-diff signal: a name
    present in both maps with a matching _normalized_body_hash did NOT
    actually change in this commit, even though the file did. Both default
    to None (treated as {}), so every caller that doesn't have diff context
    gets an empty unchanged_idents -- the same (safe, if overzealous)
    unconditional :modified-in behavior as before this parameter existed.
    """
```

- [ ] **Step 4: Add the `unchanged_idents` computation and return it**

In `mcp_server.py`, the function currently ends (lines 5957-5967):

```python
        field_entries.append((field_ident, qualified_name, candidate_triples))

    return {
        "module_ident": module_ident,
        "module_candidate_triples": module_candidate_triples,
        "function_entries": function_entries,
        "class_entries": class_entries,
        "global_entries": global_entries,
        "field_entries": field_entries,
        "field_class_map": field_class_map,
        "field_static_map": field_static_map,
        "resolved_imports": resolved_imports,
    }
```

Change to:

```python
        field_entries.append((field_ident, qualified_name, candidate_triples))

    # #221: per-entity body-diff signal for _build_code_triples' "already
    # known" branches. A name present in both old_entity_nodes and
    # new_entity_nodes, with an identical normalized (whitespace-insensitive)
    # hash, did NOT actually change here, even though the file did. Absent
    # from either side (a genuinely new/removed entity, a parse failure, or
    # simply no diff context passed) is treated conservatively as changed --
    # this only ever NARROWS which idents get :modified-in, never widens it.
    # Category keys ("function"/"class"/"variable"/"field") are identical to
    # the entity_type string _code_ident expects for each, by construction.
    unchanged_idents: Set[str] = set()
    try:
        old_nodes = old_entity_nodes or {}
        new_nodes = new_entity_nodes or {}
        for category in ("function", "class", "variable", "field"):
            old_cat = old_nodes.get(category, {})
            new_cat = new_nodes.get(category, {})
            for name in old_cat.keys() & new_cat.keys():
                if _normalized_body_hash(old_cat[name]) == _normalized_body_hash(new_cat[name]):
                    unchanged_idents.add(_code_ident(category, file_path, name))
    except Exception:
        unchanged_idents = set()

    return {
        "module_ident": module_ident,
        "module_candidate_triples": module_candidate_triples,
        "function_entries": function_entries,
        "class_entries": class_entries,
        "global_entries": global_entries,
        "field_entries": field_entries,
        "field_class_map": field_class_map,
        "field_static_map": field_static_map,
        "resolved_imports": resolved_imports,
        "unchanged_idents": unchanged_idents,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriplesBodyDiff -v`
Expected: 6 passed

- [ ] **Step 6: Run the full existing `_precompute_file_triples` test suite to confirm no regressions**

Run: `python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriples tests/test_mcp_server.py::TestPrecomputeGlobalsAndFields tests/test_mcp_server.py::TestFieldClassContainment -v`
Expected: all pass, unchanged from before this task (these classes call `_precompute_file_triples` without the new params — confirms the default-to-empty behavior is fully backward compatible)

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Extend _precompute_file_triples with #221 unchanged_idents signal"
```

---

### Task 3: Gate `_build_code_triples`'s `:modified-in` emission

**Files:**
- Modify: `mcp_server.py:6001-6071`
- Test: `tests/test_mcp_server.py::TestIngestionWrites` (add tests near the existing `test_build_code_triples_writes_modified_in_for_preexisting_functions` at line 7340)

**Interfaces:**
- Consumes: `precomputed["unchanged_idents"]` (a `Set[str]`) from Task 2's return dict.
- Produces: no signature change to `_build_code_triples` — same 8 positional params plus `field_class_ident`/`field_static_ident` as today. Behavior change only: the four non-module `else` branches skip appending `:modified-in` when the ident is in `unchanged_idents`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, inside `TestIngestionWrites` (right after `test_build_code_triples_writes_modified_in_for_preexisting_functions`, which must keep passing unmodified as the backward-compat proof):

```python
    def test_build_code_triples_skips_modified_in_for_unchanged_ident(self):
        import mcp_server
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {
            module_ident: "2025-01-01T00:00:00Z",
            fn_ident: "2025-01-01T00:00:00Z",
        }
        commit_ident = ":commit/deadbeef12345678"
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        precomputed["unchanged_idents"] = {fn_ident}
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
        assert not any(f"[{fn_ident} :modified-in {commit_ident}]" in t for t in triples)

    def test_build_code_triples_module_ignores_unchanged_idents(self):
        """Module-level churn is deliberately NOT gated by unchanged_idents --
        the module IS the file, so any file change is legitimate module
        churn (see design doc's Scope section: 'module is deliberately
        excluded'). Even if unchanged_idents (hypothetically, incorrectly)
        contained the module ident, it must still get :modified-in."""
        import mcp_server
        module_ident = mcp_server._code_ident("module", "auth.py")
        entity_valid_from = {module_ident: "2025-01-01T00:00:00Z"}
        commit_ident = ":commit/deadbeef12345678"
        extracted = {"functions": [], "classes": [], "imports": []}
        precomputed = mcp_server._precompute_file_triples("auth.py", extracted, commit_ident, {})
        precomputed["unchanged_idents"] = {module_ident}
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
        assert any(f"[{module_ident} :modified-in {commit_ident}]" in t for t in triples)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestIngestionWrites::test_build_code_triples_skips_modified_in_for_unchanged_ident tests/test_mcp_server.py::TestIngestionWrites::test_build_code_triples_module_ignores_unchanged_idents -v`
Expected: `test_build_code_triples_skips_modified_in_for_unchanged_ident` FAILs (the `:modified-in` triple is present — nothing gates it yet); `test_build_code_triples_module_ignores_unchanged_idents` PASSes already (module branch is untouched so far) — that's expected, it's a regression-guard for the next step, not a RED test.

- [ ] **Step 3: Gate the four non-module `else` branches**

In `mcp_server.py`, `_build_code_triples` currently reads (lines 6001-6071):

```python
    triples: List[str] = []
    module_ident = precomputed["module_ident"]
    field_class_map = precomputed.get("field_class_map", {})
    field_static_map = precomputed.get("field_static_map", {})

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
            # Record the field's real owning-class parent so every close path
            # can retract the [class :contains field] edge alongside the module
            # one. Only fields with an extracted owning class appear in the map.
            if field_class_ident is not None and field_ident in field_class_map:
                field_class_ident[field_ident] = field_class_map[field_ident]
            # Record the field's :static value so its close site can retract it
            # (see _build_close_triples / issue #134) without re-deriving it.
            if field_static_ident is not None and field_ident in field_static_map:
                field_static_ident[field_ident] = field_static_map[field_ident]
        else:
            triples.append(f"[{field_ident} :modified-in {commit_ident}]")

    return triples
```

Change to:

```python
    triples: List[str] = []
    module_ident = precomputed["module_ident"]
    field_class_map = precomputed.get("field_class_map", {})
    field_static_map = precomputed.get("field_static_map", {})
    # #221: idents whose body provably did NOT change this commit (empty for
    # every caller that doesn't pass old_entity_nodes/new_entity_nodes to
    # _precompute_file_triples, preserving today's unconditional behavior).
    unchanged_idents = precomputed.get("unchanged_idents", set())

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
        # Existing module: only record that this commit modified it. NOT
        # gated by unchanged_idents (#221) -- the module IS the file, so any
        # file change is legitimate module-level churn.
        triples.append(f"[{module_ident} :modified-in {commit_ident}]")

    for fn_ident, fn_name, candidate_triples in precomputed["function_entries"]:
        if fn_ident not in entity_valid_from:
            triples += candidate_triples
            if fn_ident not in idents_for_file:
                idents_for_file.append(fn_ident)
            entity_valid_from[fn_ident] = commit_ts_iso
            entity_descriptions[fn_ident] = fn_name
        elif fn_ident not in unchanged_idents:
            # Pre-existing function whose body actually changed (#221):
            # record that this commit modified it.
            triples.append(f"[{fn_ident} :modified-in {commit_ident}]")

    for cls_ident, cls_name, candidate_triples in precomputed["class_entries"]:
        if cls_ident not in entity_valid_from:
            triples += candidate_triples
            if cls_ident not in idents_for_file:
                idents_for_file.append(cls_ident)
            entity_valid_from[cls_ident] = commit_ts_iso
            entity_descriptions[cls_ident] = cls_name
        elif cls_ident not in unchanged_idents:
            # Pre-existing class whose body actually changed (#221): record
            # that this commit modified it.
            triples.append(f"[{cls_ident} :modified-in {commit_ident}]")

    for gvar_ident, gvar_name, candidate_triples in precomputed["global_entries"]:
        if gvar_ident not in entity_valid_from:
            triples += candidate_triples
            if gvar_ident not in idents_for_file:
                idents_for_file.append(gvar_ident)
            entity_valid_from[gvar_ident] = commit_ts_iso
            entity_descriptions[gvar_ident] = gvar_name
        elif gvar_ident not in unchanged_idents:
            triples.append(f"[{gvar_ident} :modified-in {commit_ident}]")

    for field_ident, field_name, candidate_triples in precomputed["field_entries"]:
        if field_ident not in entity_valid_from:
            triples += candidate_triples
            if field_ident not in idents_for_file:
                idents_for_file.append(field_ident)
            entity_valid_from[field_ident] = commit_ts_iso
            entity_descriptions[field_ident] = field_name
            # Record the field's real owning-class parent so every close path
            # can retract the [class :contains field] edge alongside the module
            # one. Only fields with an extracted owning class appear in the map.
            if field_class_ident is not None and field_ident in field_class_map:
                field_class_ident[field_ident] = field_class_map[field_ident]
            # Record the field's :static value so its close site can retract it
            # (see _build_close_triples / issue #134) without re-deriving it.
            if field_static_ident is not None and field_ident in field_static_map:
                field_static_ident[field_ident] = field_static_map[field_ident]
        elif field_ident not in unchanged_idents:
            triples.append(f"[{field_ident} :modified-in {commit_ident}]")

    return triples
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestIngestionWrites -v`
Expected: all pass, including both new tests and the pre-existing
`test_build_code_triples_writes_modified_in_for_preexisting_functions` /
`test_build_code_triples_does_not_write_modified_in_for_new_functions` (both call
`_precompute_file_triples` without old/new node maps, so `unchanged_idents` is empty
and behavior is unchanged from before this task)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Gate _build_code_triples :modified-in on #221 unchanged_idents"
```

---

### Task 4: Wire `_extract_commit` and add end-to-end tests

**Files:**
- Modify: `mcp_server.py:6638-6674`
- Test: `tests/test_mcp_server.py` (new `TestExtractCommitBodyDiff` class after `TestExtractCommitRename`, and new tests in `TestRunIngestion`)

**Interfaces:**
- Consumes: `_precompute_file_triples(..., old_entity_nodes=..., new_entity_nodes=...)` from Task 2.
- Produces: no new public interface — this task only reorders existing computation inside `_extract_commit` and threads it through. `_extract_commit`'s own return signature (`Tuple[List[tuple], ...]`) is unchanged; `precomputed["unchanged_idents"]` (already in every `results` entry's 4th tuple element since Task 2) is now populated with real data end-to-end instead of always being empty.

- [ ] **Step 1: Write the failing `_extract_commit`-level test**

Add to `tests/test_mcp_server.py`, after `TestExtractCommitRename`:

```python
class TestExtractCommitBodyDiff:
    def test_rename_does_not_populate_unchanged_idents(self, tmp_path):
        """A renamed entity gets a brand-new ident (different name ->
        different _code_ident), so old_entity_nodes/new_entity_nodes never
        share that name -- unchanged_idents must stay empty. Confirms the
        design doc's architectural claim empirically, not just by
        inspection: rename linkage and #221's body-diff gating don't
        interact."""
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
        results, _, _, renamed_pairs = mcp_server._extract_commit(str(repo), commits[1][0])
        status, file_path, extracted, precomputed, old_path = results[0]
        assert status == "R"
        assert precomputed["unchanged_idents"] == set()
```

Add to `tests/test_mcp_server.py`, inside `TestRunIngestion`:

```python
    @pytest.mark.asyncio
    async def test_whitespace_reformat_commit_produces_no_modified_in_fact(self, real_db, tmp_path):
        """The core #221 repro: a reformat-only commit must not flag the
        function as modified."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login(user):\n    return user.ok\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login(user):\n\n    return   user.ok\n")
        _subprocess.run(["git", "commit", "-am", "reformat"], cwd=repo, check=True, capture_output=True)

        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(repo), "main")

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        result = json.loads(real_db.execute(
            f'(query [:find ?c :where [{fn_ident} :modified-in ?c]])'
        ))
        assert result["results"] == [], (
            "a whitespace-only reformat must not produce a :modified-in "
            "fact -- this is the core repro #221 exists to fix"
        )

    @pytest.mark.asyncio
    async def test_genuine_change_commit_still_produces_modified_in_fact(self, real_db, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login(user):\n    return user.ok\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login(user):\n    return user.active\n")
        _subprocess.run(["git", "commit", "-am", "real change"], cwd=repo, check=True, capture_output=True)

        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(repo), "main")

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        result = json.loads(real_db.execute(
            f'(query [:find ?c :where [{fn_ident} :modified-in ?c]])'
        ))
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_only_the_changed_function_gets_modified_in_others_do_not(self, real_db, tmp_path):
        """The issue's own motivating scenario: a file with multiple
        functions where only one actually changed must flag only that one,
        not every function in the file (the pre-#221 file-broadcast bug)."""
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text(
            "def login(user):\n    return user.ok\n\ndef logout(user):\n    return None\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text(
            "def login(user):\n    return user.active\n\ndef logout(user):\n    return None\n"
        )
        _subprocess.run(["git", "commit", "-am", "change login only"], cwd=repo, check=True, capture_output=True)

        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(repo), "main")

        login_ident = mcp_server._code_ident("function", "auth.py", "login")
        logout_ident = mcp_server._code_ident("function", "auth.py", "logout")
        login_result = json.loads(real_db.execute(
            f'(query [:find ?c :where [{login_ident} :modified-in ?c]])'
        ))
        logout_result = json.loads(real_db.execute(
            f'(query [:find ?c :where [{logout_ident} :modified-in ?c]])'
        ))
        assert len(login_result["results"]) == 1
        assert logout_result["results"] == [], (
            "logout's body did not change in the second commit -- the "
            "pre-#221 file-broadcast bug would have wrongly flagged it too"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommitBodyDiff tests/test_mcp_server.py::TestRunIngestion::test_whitespace_reformat_commit_produces_no_modified_in_fact tests/test_mcp_server.py::TestRunIngestion::test_genuine_change_commit_still_produces_modified_in_fact tests/test_mcp_server.py::TestRunIngestion::test_only_the_changed_function_gets_modified_in_others_do_not -v`
Expected: `test_rename_does_not_populate_unchanged_idents` PASSes already (nothing to wire yet, but `unchanged_idents` already defaults to empty from Task 2 — not a useful RED signal on its own, so verify it stays green after Step 3 instead). `test_whitespace_reformat_commit_produces_no_modified_in_fact` FAILs (`result["results"]` is `[[":commit/..."]]`, not `[]`, since `_extract_commit` isn't passing old/new nodes into `_precompute_file_triples` yet). `test_genuine_change_commit_still_produces_modified_in_fact` PASSes already (today's unconditional behavior already flags real changes). `test_only_the_changed_function_gets_modified_in_others_do_not` FAILs (`logout_result["results"]` is non-empty — the file-broadcast bug this issue exists to fix).

- [ ] **Step 3: Reorder and wire `_extract_commit`**

In `mcp_server.py`, the per-file loop currently reads (lines 6638-6674):

```python
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
        # fetched above; a second parse of the same bytes is a deliberate
        # simplicity/cost tradeoff over threading Node references through
        # _extract_from_source's return value, which must stay plain-data-only
        # for other callers.
        #
        # Wrapped best-effort, same as the old-side call above: a
        # pathologically nested file parses fine under tree-sitter but can
        # blow the Python recursion limit inside _collect_entity_nodes's
        # own recursive walk() (RecursionError). Left unguarded, that
        # exception would propagate out of _extract_commit and (in the real
        # ProcessPoolExecutor pipeline) abort the entire ingestion run
        # rather than just this one commit — contradicting this function's
        # own contract that ordinary exceptions fail only the one commit.
        new_lang = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        try:
            new_tree = parser.parse(content)
            new_entity_nodes = collect_all_nodes(new_tree.root_node, new_lang)
        except Exception:
            new_entity_nodes = {
                "function": {}, "class": {}, "variable": {}, "field": {},
            }  # best-effort: matching degrades to no-match
```

Change to:

```python
        try:
            content = _git_file_content(repo_path, commit_hash, file_path)
        except Exception:
            continue

        # Live nodes for the NEW side come from re-parsing (extracted only
        # carries text, per Task 6) — cheap, since this is the same content
        # already fetched above; a second parse of the same bytes is a
        # deliberate simplicity/cost tradeoff over threading Node references
        # through _extract_from_source's return value, which must stay
        # plain-data-only for other callers. Computed here, BEFORE
        # _precompute_file_triples, so #221's body-diff hash-compare can use
        # it alongside old_entity_nodes; still reused further below for its
        # original rename-matching purpose too.
        #
        # Wrapped best-effort, same as the old-side call above: a
        # pathologically nested file parses fine under tree-sitter but can
        # blow the Python recursion limit inside _collect_entity_nodes's
        # own recursive walk() (RecursionError). Left unguarded, that
        # exception would propagate out of _extract_commit and (in the real
        # ProcessPoolExecutor pipeline) abort the entire ingestion run
        # rather than just this one commit — contradicting this function's
        # own contract that ordinary exceptions fail only the one commit.
        new_lang = _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "")
        try:
            new_tree = parser.parse(content)
            new_entity_nodes = collect_all_nodes(new_tree.root_node, new_lang)
        except Exception:
            new_entity_nodes = {
                "function": {}, "class": {}, "variable": {}, "field": {},
            }  # best-effort: matching degrades to no-match

        extracted = _extract_from_source(content, parser, file_path)
        if known_files is None:
            known_files = _known_files_at_commit(repo_path, commit_hash, ignore_patterns)
            segment_index = _SegmentSuffixIndex(known_files)
        precomputed = _precompute_file_triples(
            file_path, extracted, commit_ident, known_files, segment_index=segment_index,
            old_entity_nodes=old_entity_nodes, new_entity_nodes=new_entity_nodes,
        )
        results.append((status, file_path, extracted, precomputed, old_path if status == "R" else ""))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommitBodyDiff tests/test_mcp_server.py::TestRunIngestion -v`
Expected: all pass, including the previously-RED
`test_whitespace_reformat_commit_produces_no_modified_in_fact` and
`test_only_the_changed_function_gets_modified_in_others_do_not`

- [ ] **Step 5: Run the full existing `_extract_commit`/rename test suites to confirm no regressions**

Run: `python -m pytest tests/test_mcp_server.py::TestExtractCommit tests/test_mcp_server.py::TestExtractCommitRename tests/test_mcp_server.py::TestMatchRenamedEntities -v`
Expected: all pass, unchanged from before this task — the reorder moves code, it doesn't change what `old_entity_nodes`/`new_entity_nodes` contain or how rename matching consumes them

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: no new failures relative to the pre-existing baseline (check via `git stash` + rerun if any unexpected failures appear, per this project's established convention — some pre-existing failures may exist from missing tree-sitter grammar packages in the sandbox, unrelated to this change)

- [ ] **Step 7: Run lint checks**

Run: `ruff check mcp_server.py tests/test_mcp_server.py && black --check mcp_server.py tests/test_mcp_server.py`
Expected: no new findings relative to the pre-existing baseline (compare via `git stash` if anything is flagged)

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Wire #221 body-diff nodes through _extract_commit end-to-end"
```

---

## Self-Review Notes

- **Spec coverage:** `_normalized_body_hash` (Task 1) ✓, `_precompute_file_triples` extension + `unchanged_idents` (Task 2) ✓, `_build_code_triples` gating with module exclusion (Task 3) ✓, `_extract_commit` reorder/wiring (Task 4) ✓, error-handling fail-open behavior (covered by Task 2's `try/except` + Task 2's `test_absent_old_and_new_nodes_default_to_empty_set`, which is the same code path a parse failure falls back to) ✓, all four entity categories (Task 2's function/class/variable/field tests) ✓, rename non-interaction (Task 4's `test_rename_does_not_populate_unchanged_idents`) ✓, no backfill/no flag (nothing in this plan adds either) ✓.
- **Placeholder scan:** none — every step has complete, verified-against-the-real-file code.
- **Type consistency:** `unchanged_idents: Set[str]` is introduced in Task 2's return dict and consumed identically (`precomputed.get("unchanged_idents", set())`) in Task 3; `old_entity_nodes`/`new_entity_nodes` types (`Optional[Dict[str, Dict[str, Any]]]`) match between Task 2's signature and Task 4's call site, which passes the exact same-shaped dicts `_extract_commit` already builds for rename matching.
