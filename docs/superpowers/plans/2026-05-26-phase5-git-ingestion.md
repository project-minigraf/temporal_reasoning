# Phase 5 — Git Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Walk git history and ingest code structure (modules, functions, classes, edges) into the bi-temporal graph via two new MCP tools: `vulcan_ingest_git` (background async task) and `vulcan_ingest_status` (progress poll).

**Architecture:** A background `asyncio.Task` walks commits chronologically using `git log` / `git diff-tree`, parses changed files with tree-sitter, and transacts code-structure facts with the commit's ISO 8601 timestamp as `:valid-from`. Deletions re-transact with `:valid-to` to close the valid window correctly. The task releases `_db` (sets it to `None`) between every commit so the prepare_hook subprocess can open the graph file — the same pattern as the existing per-call file-lock release in `call_tool`'s `finally` block.

**Tech Stack:** Python 3.9+, `tree-sitter`, `tree-sitter-languages` (bundled grammars), `subprocess` (git), `asyncio`, `minigraf` Python binding.

---

## File Structure

| File | Role |
|------|------|
| `mcp_server.py` | All new code lives here — schema extension, state, helpers, handlers, dispatch |
| `tests/test_mcp_server.py` | All new tests live here, following existing mock pattern |
| `hooks/claude-code.json` | Add `vulcan_ingest_git` to `UserPromptSubmit` |
| `hooks/codex.toml` | Add `vulcan_ingest_git` to `pre_turn` hook |
| `hooks/hermes.yaml` | Add `vulcan_ingest_git` to `pre_turn` hook |
| `SKILL.md` | New tool docs + code-structure query fewshots |
| `ROADMAP.md` | Mark Phase 5 in-progress |
| `install.py` | Add `tree-sitter` and `tree-sitter-languages` dependency checks |

---

### Task 1: Add dependencies and extend VULCAN_SCHEMA + SESSION_RULES

**Files:**
- Modify: `mcp_server.py` (VULCAN_SCHEMA dict, SESSION_RULES list)
- Modify: `install.py` (dependency checks)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests for new schema types**

```python
class TestPhase5Schema:
    def test_module_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":module/src-auth-py", "entity_type": "module",
                  "attribute": ":description", "value": "src/auth.py"}]
        assert mcp_server._validate_facts(facts) == []

    def test_function_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":function/src-auth-py-login", "entity_type": "function",
                  "attribute": ":description", "value": "login"}]
        assert mcp_server._validate_facts(facts) == []

    def test_class_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":class/src-auth-py-user", "entity_type": "class",
                  "attribute": ":description", "value": "User"}]
        assert mcp_server._validate_facts(facts) == []

    def test_ingestion_entity_passes_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":ingestion/watermark", "entity_type": "ingestion",
                  "attribute": ":description", "value": "git ingestion watermark"}]
        assert mcp_server._validate_facts(facts) == []

    def test_unknown_code_attr_fails_validation(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        facts = [{"entity": ":module/foo", "entity_type": "module",
                  "attribute": ":unknown-attr", "value": "x"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert "unknown-attr" in violations[0]

    def test_contains_rule_registered_at_startup(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        executed = [call.args[0] for call in db_instance.execute.call_args_list]
        assert any("contains" in r for r in executed)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/aditya/workspaces/pycharm/temporal_reasoning
python -m pytest tests/test_mcp_server.py::TestPhase5Schema -v
```

Expected: FAIL — `module`, `function`, `class`, `ingestion` not in `VULCAN_SCHEMA`.

- [ ] **Step 3: Extend VULCAN_SCHEMA in mcp_server.py**

Locate the `VULCAN_SCHEMA` dict (around line 350) and add four new entity types:

```python
VULCAN_SCHEMA: Dict[str, Dict[str, Dict[str, type]]] = {
    "decision": { ... },   # unchanged
    "preference": { ... }, # unchanged
    "constraint": { ... }, # unchanged
    "dependency": { ... }, # unchanged
    "module": {
        "required": {":description": str},
        "optional": {":path": str, ":alias": str},
    },
    "function": {
        "required": {":description": str},
        "optional": {":file": str, ":alias": str},
    },
    "class": {
        "required": {":description": str},
        "optional": {":file": str, ":alias": str},
    },
    "ingestion": {
        "required": {":description": str},
        "optional": {":hash": str, ":alias": str},
    },
}
```

- [ ] **Step 4: Extend SESSION_RULES**

Locate `SESSION_RULES` (around line 20) and add two entries:

```python
SESSION_RULES = [
    "(rule [(linked ?a ?b) [?a :depends-on ?b]])",
    "(rule [(linked ?a ?b) [?a :calls ?b]])",
    "(rule [(reachable ?a ?b) [?a :depends-on ?b]])",
    "(rule [(reachable ?a ?b) [?a :calls ?b]])",
    "(rule [(linked ?a ?b) [?a :contains ?b]])",    # NEW
    "(rule [(reachable ?a ?b) [?a :contains ?b]])", # NEW
]
```

- [ ] **Step 5: Add tree-sitter dependency checks to install.py**

Find the existing `check_minigraf_package` function and add two similar functions after it:

```python
def check_tree_sitter():
    """Verify tree-sitter is installed."""
    try:
        import tree_sitter
        print("✓ tree-sitter found")
        return True
    except ImportError:
        print("✗ tree-sitter not found — installing...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "tree-sitter"], timeout=120)
        return result.returncode == 0

def check_tree_sitter_languages():
    """Verify tree-sitter-languages is installed."""
    try:
        import tree_sitter_languages
        print("✓ tree-sitter-languages found")
        return True
    except ImportError:
        print("✗ tree-sitter-languages not found — installing...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "tree-sitter-languages"], timeout=180)
        return result.returncode == 0
```

Then add both to the `checks` list in `main()`:

```python
checks = [
    ("Python version", check_python_version),
    ("minigraf package", check_minigraf_package),
    ("MCP server", check_mcp_server_importable),
    ("tree-sitter", check_tree_sitter),           # NEW
    ("tree-sitter-languages", check_tree_sitter_languages),  # NEW
]
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestPhase5Schema -v
```

Expected: all 6 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py install.py tests/test_mcp_server.py
git commit -m "feat(schema): extend VULCAN_SCHEMA and SESSION_RULES for Phase 5 code entities"
```

---

### Task 2: Ingestion state + status tool

**Files:**
- Modify: `mcp_server.py` (module-level state, `handle_vulcan_ingest_status`, `_TOOLS`, `call_tool`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

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

    def test_returns_running_status(self, mock_minigraf_db, tmp_path):
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        mcp_server._ingest_progress = {
            "status": "running", "processed": 3, "total": 10,
            "current_commit": "abc123", "error": None,
        }
        result = mcp_server.handle_vulcan_ingest_status()
        assert result["status"] == "running"
        assert result["processed"] == 3
        assert result["total"] == 10
        assert result["current_commit"] == "abc123"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestVulcanIngestStatus -v
```

Expected: FAIL — `_ingest_progress` and `handle_vulcan_ingest_status` not defined.

- [ ] **Step 3: Add module-level state and handler**

Add after the `_server_ref` declaration (around line 40):

```python
# Ingestion state
_ingest_task: Optional[asyncio.Task] = None
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0,
    "current_commit": "", "error": None,
}
```

Add the handler function (before `_TOOLS`):

```python
def handle_vulcan_ingest_status() -> Dict[str, Any]:
    """Return current ingestion progress."""
    return {"ok": True, **_ingest_progress}
```

- [ ] **Step 4: Register vulcan_ingest_status in _TOOLS**

Append to the `_TOOLS` list:

```python
Tool(
    name="vulcan_ingest_git",
    description=(
        "Ingest code structure from git history into the bi-temporal graph. "
        "Starts a background task and returns immediately. "
        "Call vulcan_ingest_status to poll progress."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Absolute path to the git repo root. Defaults to cwd.",
            },
            "branch": {
                "type": "string",
                "description": "Branch or ref to walk. Defaults to HEAD.",
            },
        },
        "required": [],
    },
),
Tool(
    name="vulcan_ingest_status",
    description=(
        "Return the current git ingestion progress. "
        "status is one of: idle, running, complete, error."
    ),
    inputSchema={"type": "object", "properties": {}, "required": []},
),
```

- [ ] **Step 5: Add dispatch in call_tool**

In `call_tool`, add before the `raise ValueError` line:

```python
if name == "vulcan_ingest_git":
    result = await handle_vulcan_ingest_git(
        repo_path=arguments.get("repo_path"),
        branch=arguments.get("branch", "HEAD"),
    )
    return [TextContent(type="text", text=json.dumps(result))]

if name == "vulcan_ingest_status":
    result = handle_vulcan_ingest_status()
    return [TextContent(type="text", text=json.dumps(result))]
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestVulcanIngestStatus -v
```

Expected: both tests PASS.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add ingestion state, vulcan_ingest_status handler and tool registration"
```

---

### Task 3: Language detection and grammar caching

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
class TestGetParser:
    def test_python_file_returns_parser(self):
        import mcp_server
        mcp_server._grammar_cache.clear()
        parser = mcp_server._get_parser("src/auth.py")
        assert parser is not None

    def test_unknown_extension_returns_none(self):
        import mcp_server
        parser = mcp_server._get_parser("data.csv")
        assert parser is None

    def test_parser_is_cached_on_second_call(self):
        import mcp_server
        mcp_server._grammar_cache.clear()
        p1 = mcp_server._get_parser("foo.py")
        p2 = mcp_server._get_parser("bar.py")
        assert p1 is p2  # same cached parser instance

    def test_unsupported_grammar_returns_none(self):
        import mcp_server
        # Simulate a language in the ext map but whose grammar fails to load
        mcp_server._grammar_cache.clear()
        mcp_server._grammar_cache["python"] = None
        parser = mcp_server._get_parser("foo.py")
        assert parser is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestGetParser -v
```

Expected: FAIL — `_get_parser` not defined.

- [ ] **Step 3: Add _EXT_TO_LANG, _grammar_cache, and _get_parser**

Add after the imports section in `mcp_server.py`:

```python
# ---------------------------------------------------------------------------
# Language detection and grammar caching
# ---------------------------------------------------------------------------

_EXT_TO_LANG: Dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".cs": "c_sharp", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".swift": "swift", ".scala": "scala",
    ".hs": "haskell", ".lua": "lua", ".ex": "elixir", ".exs": "elixir",
}

_grammar_cache: Dict[str, Any] = {}  # lang_name → Parser or None


def _get_parser(file_path: str) -> Optional[Any]:
    """Return a cached tree_sitter.Parser for the file's language, or None."""
    ext = Path(file_path).suffix.lower()
    lang_name = _EXT_TO_LANG.get(ext)
    if not lang_name:
        return None
    if lang_name in _grammar_cache:
        return _grammar_cache[lang_name]
    try:
        import tree_sitter_languages  # type: ignore
        import tree_sitter            # type: ignore
        lang = tree_sitter_languages.get_language(lang_name)
        parser = tree_sitter.Parser()
        parser.set_language(lang)
        _grammar_cache[lang_name] = parser
    except Exception:
        _grammar_cache[lang_name] = None
    return _grammar_cache[lang_name]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestGetParser -v
```

Expected: all 4 tests PASS. (Requires `pip install tree-sitter tree-sitter-languages` in the env.)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add language detection and lazy grammar cache"
```

---

### Task 4: AST extraction

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
class TestExtractFromSource:
    def _python_parser(self):
        import mcp_server
        mcp_server._grammar_cache.clear()
        return mcp_server._get_parser("x.py")

    def test_extracts_function_names(self):
        import mcp_server
        source = b"def login(user):\n    pass\ndef logout():\n    pass\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "auth.py")
        assert "login" in result["functions"]
        assert "logout" in result["functions"]

    def test_extracts_class_names(self):
        import mcp_server
        source = b"class User:\n    pass\nclass Admin(User):\n    pass\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "models.py")
        assert "User" in result["classes"]
        assert "Admin" in result["classes"]

    def test_extracts_from_imports(self):
        import mcp_server
        source = b"import os\nfrom pathlib import Path\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "foo.py")
        assert "os" in result["imports"]
        assert "pathlib" in result["imports"]

    def test_extracts_call_names(self):
        import mcp_server
        source = b"def foo():\n    bar()\n    baz(1, 2)\n"
        result = mcp_server._extract_from_source(source, self._python_parser(), "foo.py")
        assert "bar" in result["calls"]
        assert "baz" in result["calls"]

    def test_parse_error_returns_empty(self):
        import mcp_server
        parser = self._python_parser()
        result = mcp_server._extract_from_source(b"\x00\xff\xfe", parser, "bad.py")
        assert result == {"functions": [], "classes": [], "imports": [], "calls": []}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestExtractFromSource -v
```

Expected: FAIL — `_extract_from_source` not defined.

- [ ] **Step 3: Add _LANG_NODE_TYPES and extraction functions**

```python
# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------

_LANG_NODE_TYPES: Dict[str, Dict[str, set]] = {
    "python": {
        "functions": {"function_definition", "async_function_definition"},
        "classes": {"class_definition"},
        "imports": {"import_statement", "import_from_statement"},
        "calls": {"call"},
    },
    "javascript": {
        "functions": {"function_declaration", "function_expression", "method_definition"},
        "classes": {"class_declaration"},
        "imports": {"import_statement"},
        "calls": {"call_expression"},
    },
    "typescript": {
        "functions": {"function_declaration", "function_expression", "method_definition"},
        "classes": {"class_declaration"},
        "imports": {"import_statement"},
        "calls": {"call_expression"},
    },
    "rust": {
        "functions": {"function_item"},
        "classes": {"struct_item", "impl_item"},
        "imports": {"use_declaration"},
        "calls": {"call_expression"},
    },
    "go": {
        "functions": {"function_declaration", "method_declaration"},
        "classes": {"type_declaration"},
        "imports": {"import_declaration"},
        "calls": {"call_expression"},
    },
}


def _extract_import_name(node, lang_name: str) -> Optional[str]:
    """Extract the top-level module name from an import node."""
    if lang_name == "python":
        if node.type == "import_from_statement":
            m = node.child_by_field_name("module_name")
            return m.text.decode("utf-8").split(".")[0] if m else None
        # import_statement: first named child is dotted_name or aliased_import
        for child in node.named_children:
            if child.type == "aliased_import":
                n = child.child_by_field_name("name")
                return n.text.decode("utf-8").split(".")[0] if n else None
            if child.type == "dotted_name":
                return child.text.decode("utf-8").split(".")[0]
    elif lang_name in ("javascript", "typescript"):
        src = node.child_by_field_name("source")
        if src:
            return src.text.decode("utf-8").strip("'\"")
    return None


def _extract_call_name(node, lang_name: str) -> Optional[str]:
    """Extract the function name from a call node (best-effort, identifiers only)."""
    fn = node.child_by_field_name("function")
    if fn and fn.type == "identifier":
        return fn.text.decode("utf-8")
    return None


def _walk_ast(node, results: Dict[str, List[str]], lang_name: str) -> None:
    """Recursively extract code entities from a tree-sitter AST node."""
    node_types = _LANG_NODE_TYPES.get(lang_name)
    if node_types is None:
        return

    if node.type in node_types.get("functions", set()):
        name_node = node.child_by_field_name("name")
        if name_node:
            results["functions"].append(name_node.text.decode("utf-8"))

    elif node.type in node_types.get("classes", set()):
        name_node = node.child_by_field_name("name")
        if name_node:
            results["classes"].append(name_node.text.decode("utf-8"))

    elif node.type in node_types.get("imports", set()):
        name = _extract_import_name(node, lang_name)
        if name:
            results["imports"].append(name)

    elif node.type in node_types.get("calls", set()):
        name = _extract_call_name(node, lang_name)
        if name:
            results["calls"].append(name)

    for child in node.children:
        _walk_ast(child, results, lang_name)


def _extract_from_source(
    source: bytes, parser: Any, file_path: str
) -> Dict[str, List[str]]:
    """Parse source bytes and extract functions, classes, imports, calls."""
    results: Dict[str, List[str]] = {
        "functions": [], "classes": [], "imports": [], "calls": []
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

```bash
python -m pytest tests/test_mcp_server.py::TestExtractFromSource -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add AST extraction via tree-sitter"
```

---

### Task 5: Code ident canonicalization

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
class TestCodeIdent:
    def test_module_ident_from_path(self):
        import mcp_server
        assert mcp_server._code_ident("module", "src/auth.py") == ":module/src-auth-py"

    def test_function_ident_distinct_from_module(self):
        import mcp_server
        module_ident = mcp_server._code_ident("module", "src/auth.py")
        fn_ident = mcp_server._code_ident("function", "src/auth.py", "login")
        assert module_ident == ":module/src-auth-py"
        assert fn_ident == ":function/src-auth-py-login"
        # Key: src/auth_login.py module would be :module/src-auth-login-py — different
        assert mcp_server._code_ident("module", "src/auth_login.py") != fn_ident

    def test_class_ident(self):
        import mcp_server
        assert mcp_server._code_ident("class", "src/auth.py", "User") == ":class/src-auth-py-user"

    def test_name_is_lowercased(self):
        import mcp_server
        assert mcp_server._code_ident("function", "Foo.py", "MyFunc") == ":function/foo-py-myfunc"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestCodeIdent -v
```

Expected: FAIL — `_code_ident` not defined.

- [ ] **Step 3: Add _code_ident**

Add after the existing `_canonical_ident` function:

```python
def _code_ident(entity_type: str, file_path: str, name: Optional[str] = None) -> str:
    """Return a canonical ident for a code entity.

    Uses '::' as separator between file path and name so slugging produces
    a distinct result from a file whose path contains the name literally.
    e.g. "src/auth.py::login" → ":function/src-auth-py-login"
         "src/auth_login.py"  → ":module/src-auth-login-py"  (different)
    """
    if name:
        value = f"{file_path}::{name}"
    else:
        value = file_path
    return _canonical_ident(entity_type, value)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestCodeIdent -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add _code_ident for collision-safe code entity slugs"
```

---

### Task 6: Git helpers

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

These tests use a real temporary git repo (no mocking) to verify correct subprocess calls.

- [ ] **Step 1: Write failing tests**

```python
import subprocess as _subprocess

@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with two commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    # Commit 1
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)

    # Commit 2
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)

    return repo


class TestGitHelpers:
    def test_git_commits_full_history(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        assert len(commits) == 2
        hash_, ts_iso, author, subject = commits[0]
        assert len(hash_) == 40
        assert "T" in ts_iso or ts_iso.endswith("Z")
        assert subject == "add auth"

    def test_git_commits_incremental(self, git_repo):
        import mcp_server
        all_commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = all_commits[0][0]
        incremental = mcp_server._git_commits(str(git_repo), watermark_hash=first_hash)
        assert len(incremental) == 1
        assert incremental[0][3] == "add models"

    def test_git_changed_files(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        second_hash = commits[1][0]
        changes = mcp_server._git_changed_files(str(git_repo), second_hash)
        assert ("A", "models.py") in changes

    def test_git_file_content(self, git_repo):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo), watermark_hash=None)
        first_hash = commits[0][0]
        content = mcp_server._git_file_content(str(git_repo), first_hash, "auth.py")
        assert b"def login" in content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestGitHelpers -v
```

Expected: FAIL — functions not defined.

- [ ] **Step 3: Add git helper functions**

```python
# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
import subprocess as _subprocess


def _git_commits(
    repo_path: str,
    watermark_hash: Optional[str],
    branch: str = "HEAD",
) -> List[tuple]:
    """Return list of (hash, ts_iso, author_email, subject) in chronological order."""
    range_spec = f"{watermark_hash}..{branch}" if watermark_hash else branch
    result = _subprocess.run(
        ["git", "log", "--reverse", "--format=%H %at %ae %s", range_spec],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    commits = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 3)
        hash_ = parts[0]
        ts_unix = int(parts[1])
        author = parts[2]
        subject = parts[3] if len(parts) > 3 else ""
        ts_iso = datetime.datetime.utcfromtimestamp(ts_unix).strftime("%Y-%m-%dT%H:%M:%SZ")
        commits.append((hash_, ts_iso, author, subject))
    return commits


def _git_changed_files(repo_path: str, commit_hash: str) -> List[tuple]:
    """Return list of (status_char, path) for files changed in this commit."""
    result = _subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--name-status", commit_hash],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    changes = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status_char = parts[0][0]  # A, M, D, R, C → take first char
            changes.append((status_char, parts[1]))
    return changes


def _git_file_content(repo_path: str, commit_hash: str, file_path: str) -> bytes:
    """Return raw bytes of a file at the given commit."""
    result = _subprocess.run(
        ["git", "show", f"{commit_hash}:{file_path}"],
        cwd=repo_path, capture_output=True, check=True,
    )
    return result.stdout
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestGitHelpers -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add git enumeration helpers (commits, changed files, file content)"
```

---

### Task 7: Bi-temporal write helpers and watermark

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
class TestIngestionWrites:
    def test_ingest_transact_uses_valid_from(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._ingest_transact(
            db,
            ['[:module/foo :description "foo.py"]'],
            "2025-03-01T10:00:00Z",
            "git:abc test",
        )
        call_args = db_instance.execute.call_args[0][0]
        assert ':valid-from "2025-03-01T10:00:00Z"' in call_args
        assert ":valid-to" not in call_args

    def test_ingest_close_uses_valid_from_and_valid_to(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._ingest_close(
            db,
            ['[:module/foo :description "foo.py"]'],
            "2025-01-01T00:00:00Z",
            "2025-03-01T10:00:00Z",
            "git:abc delete",
        )
        call_args = db_instance.execute.call_args[0][0]
        assert ':valid-from "2025-01-01T00:00:00Z"' in call_args
        assert ':valid-to "2025-03-01T10:00:00Z"' in call_args

    def test_watermark_update_transacts_hash(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()

        mcp_server._watermark_update(db, "deadbeef", "2025-03-01T10:00:00Z", "git:deadbeef x: y")
        call_args = db_instance.execute.call_args[0][0]
        assert "deadbeef" in call_args
        assert ":ingestion/watermark" in call_args

    def test_watermark_query_returns_none_when_absent(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        result = mcp_server._watermark_query(db)
        assert result is None

    def test_watermark_query_returns_hash_when_present(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["abc123"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        result = mcp_server._watermark_query(db)
        assert result == "abc123"

    def test_ingest_transact_noop_for_empty_triples(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        db_instance.execute.reset_mock()
        mcp_server._ingest_transact(db, [], "2025-03-01T10:00:00Z", "r")
        db_instance.execute.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestIngestionWrites -v
```

Expected: FAIL — functions not defined.

- [ ] **Step 3: Add the write helpers and watermark functions**

```python
# ---------------------------------------------------------------------------
# Bi-temporal write helpers
# ---------------------------------------------------------------------------


def _ingest_transact(
    db: Any,
    triples: List[str],
    commit_ts_iso: str,
    reason: str,
) -> None:
    """Transact code-structure facts with :valid-from set to the commit timestamp."""
    if not triples:
        return
    facts_str = "[" + " ".join(triples) + "]"
    db.execute(f'(transact {{:valid-from "{commit_ts_iso}"}} {facts_str})')


def _ingest_close(
    db: Any,
    triples: List[str],
    original_ts_iso: str,
    commit_ts_iso: str,
    reason: str,
) -> None:
    """Close a fact's valid window at the deletion commit timestamp.

    retract has no temporal options, so deletions are expressed as a
    re-transact with explicit :valid-from (creation) and :valid-to (deletion).
    """
    if not triples:
        return
    facts_str = "[" + " ".join(triples) + "]"
    db.execute(
        f'(transact {{:valid-from "{original_ts_iso}" :valid-to "{commit_ts_iso}"}} {facts_str})'
    )


def _watermark_query(db: Any) -> Optional[str]:
    """Return the hash of the last ingested commit, or None if no watermark exists."""
    raw = db.execute("[:find ?h :where [:ingestion/watermark :hash ?h]]")
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _watermark_update(db: Any, commit_hash: str, commit_ts_iso: str, reason: str) -> None:
    """Record the last successfully ingested commit hash in the graph."""
    db.execute(
        f'(transact {{:valid-from "{commit_ts_iso}"}} '
        f'[[:ingestion/watermark :entity-type :type/ingestion] '
        f'[:ingestion/watermark :ident ":ingestion/watermark"] '
        f'[:ingestion/watermark :description "git ingestion watermark"] '
        f'[:ingestion/watermark :hash "{commit_hash}"]])'
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestIngestionWrites -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add bi-temporal write helpers and watermark query/update"
```

---

### Task 8: Background ingestion task and vulcan_ingest_git handler

**Files:**
- Modify: `mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

```python
class TestRunIngestion:
    @pytest.mark.asyncio
    async def test_ingestion_processes_all_commits(self, mock_minigraf_db, git_repo):
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
    async def test_watermark_updated_after_each_commit(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        # watermark update is a transact call containing :ingestion/watermark
        watermark_calls = [
            c for c in db_instance.execute.call_args_list
            if ":ingestion/watermark" in str(c) and "transact" in str(c)
        ]
        assert len(watermark_calls) >= 2  # one per commit

    @pytest.mark.asyncio
    async def test_db_released_between_commits(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._db = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        db_none_snapshots = []

        original_sleep = asyncio.sleep
        async def patched_sleep(t):
            db_none_snapshots.append(mcp_server._db is None)
            await original_sleep(t)

        with patch("asyncio.sleep", patched_sleep):
            await mcp_server._run_ingestion(str(git_repo), "HEAD")

        # _db must be None at every yield point
        assert all(db_none_snapshots), f"_db was not None at yield: {db_none_snapshots}"

    @pytest.mark.asyncio
    async def test_handle_vulcan_ingest_git_returns_immediately(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        result = await mcp_server.handle_vulcan_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is True
        assert "job_id" in result

    @pytest.mark.asyncio
    async def test_second_call_while_running_returns_error(self, mock_minigraf_db, git_repo):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server._ingest_task = None
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server.handle_vulcan_ingest_git(repo_path=str(git_repo))
        result = await mcp_server.handle_vulcan_ingest_git(repo_path=str(git_repo))
        assert result["ok"] is False
        assert "already in progress" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_mcp_server.py::TestRunIngestion -v
```

Expected: FAIL — `_run_ingestion` and `handle_vulcan_ingest_git` not defined.

- [ ] **Step 3: Add _build_code_triples helper**

This helper converts extracted AST data for a file into Datalog triple strings:

```python
def _build_code_triples(
    file_path: str,
    extracted: Dict[str, List[str]],
    commit_ts_iso: str,
    entity_valid_from: Dict[str, str],
) -> List[str]:
    """Return Datalog triple strings for a file's extracted code entities."""
    triples: List[str] = []
    module_ident = _code_ident("module", file_path)

    triples += [
        f"[{module_ident} :entity-type :type/module]",
        f'[{module_ident} :ident "{module_ident}"]',
        f'[{module_ident} :description "{file_path}"]',
        f'[{module_ident} :path "{file_path}"]',
    ]
    entity_valid_from.setdefault(module_ident, commit_ts_iso)

    for fn_name in extracted["functions"]:
        fn_ident = _code_ident("function", file_path, fn_name)
        triples += [
            f"[{fn_ident} :entity-type :type/function]",
            f'[{fn_ident} :ident "{fn_ident}"]',
            f'[{fn_ident} :description "{fn_name}"]',
            f'[{fn_ident} :file "{file_path}"]',
            f"[{module_ident} :contains {fn_ident}]",
        ]
        entity_valid_from.setdefault(fn_ident, commit_ts_iso)

    for cls_name in extracted["classes"]:
        cls_ident = _code_ident("class", file_path, cls_name)
        triples += [
            f"[{cls_ident} :entity-type :type/class]",
            f'[{cls_ident} :ident "{cls_ident}"]',
            f'[{cls_ident} :description "{cls_name}"]',
            f'[{cls_ident} :file "{file_path}"]',
            f"[{module_ident} :contains {cls_ident}]",
        ]
        entity_valid_from.setdefault(cls_ident, commit_ts_iso)

    for import_name in set(extracted["imports"]):
        dep_ident = _canonical_ident("module", import_name)
        triples.append(f"[{module_ident} :depends-on {dep_ident}]")

    for call_name in set(extracted["calls"]):
        callee_ident = _canonical_ident("function", call_name)
        triples.append(f"[{module_ident} :calls {callee_ident}]")

    return triples
```

- [ ] **Step 4: Add _run_ingestion coroutine**

```python
async def _run_ingestion(repo_path: str, branch: str) -> None:
    """Background coroutine: walk git history and ingest code structure."""
    global _db, _ingest_progress
    try:
        # Read watermark before releasing DB
        db = get_db()
        watermark = _watermark_query(db)
        _db = None  # release file lock while enumerating commits

        commits = _git_commits(repo_path, watermark, branch)
        _ingest_progress["total"] = len(commits)
        _ingest_progress["status"] = "running"

        # Track valid-from timestamps for entities seen this session
        entity_valid_from: Dict[str, str] = {}

        for commit_hash, commit_ts_iso, author, subject in commits:
            _ingest_progress["current_commit"] = commit_hash
            reason = f"git:{commit_hash} {author}: {subject}"

            # Acquire DB fresh each commit — never hold across yield
            db = get_db()
            try:
                changed = _git_changed_files(repo_path, commit_hash)
                add_triples: List[str] = []
                close_items: List[tuple] = []  # (triples, original_ts_iso)

                for status, file_path in changed:
                    parser = _get_parser(file_path)
                    if parser is None:
                        continue

                    module_ident = _code_ident("module", file_path)

                    if status == "D":
                        # Close the module and all known child entities
                        for ident, orig_ts in list(entity_valid_from.items()):
                            if ident == module_ident or (
                                ident.startswith(":function/") or ident.startswith(":class/")
                            ) and _code_ident("module", file_path) == module_ident:
                                close_items.append(
                                    ([f'[{ident} :description ""]'], orig_ts)
                                )
                    else:  # A or M
                        try:
                            content = _git_file_content(repo_path, commit_hash, file_path)
                        except Exception:
                            continue
                        extracted = _extract_from_source(content, parser, file_path)
                        triples = _build_code_triples(
                            file_path, extracted, commit_ts_iso, entity_valid_from
                        )
                        add_triples.extend(triples)

                _ingest_transact(db, add_triples, commit_ts_iso, reason)
                for close_triples, orig_ts in close_items:
                    _ingest_close(db, close_triples, orig_ts, commit_ts_iso, reason)
                _watermark_update(db, commit_hash, commit_ts_iso, reason)
                db.checkpoint()

            finally:
                _db = None  # release file lock between commits

            _ingest_progress["processed"] += 1
            await asyncio.sleep(0)  # yield to event loop

        _ingest_progress["status"] = "complete"

    except Exception as e:
        _ingest_progress["status"] = "error"
        _ingest_progress["error"] = str(e)
        _db = None
```

- [ ] **Step 5: Add handle_vulcan_ingest_git**

```python
async def handle_vulcan_ingest_git(
    repo_path: Optional[str] = None,
    branch: str = "HEAD",
) -> Dict[str, Any]:
    """Start background git ingestion. Returns immediately."""
    global _ingest_task, _ingest_progress
    if _ingest_task and not _ingest_task.done():
        return {"ok": False, "error": "ingestion already in progress"}
    repo = repo_path or str(Path.cwd())
    _ingest_progress = {
        "status": "idle", "processed": 0, "total": 0,
        "current_commit": "", "error": None,
    }
    _ingest_task = asyncio.create_task(_run_ingestion(repo, branch))
    return {"ok": True, "job_id": "git-ingest", "message": f"Ingestion started for {repo}"}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_mcp_server.py::TestRunIngestion -v
```

Expected: all 5 tests PASS.

- [ ] **Step 7: Run full test suite to check for regressions**

```bash
python -m pytest tests/test_mcp_server.py -v
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingestion): add background ingestion task and vulcan_ingest_git handler"
```

---

### Task 9: Hook configs, SKILL.md, and ROADMAP

**Files:**
- Modify: `hooks/claude-code.json`
- Modify: `hooks/codex.toml`
- Modify: `hooks/hermes.yaml`
- Modify: `SKILL.md`
- Modify: `ROADMAP.md`

- [ ] **Step 1: Update hooks/claude-code.json**

Add `vulcan_ingest_git` as a second command in the `UserPromptSubmit` hooks array:

```json
"UserPromptSubmit": [
  {
    "matcher": "",
    "hooks": [
      {
        "type": "command",
        "command": "python PATH_TO_REPO/hooks/prepare_hook.py",
        "timeout": 5000
      },
      {
        "type": "command",
        "command": "python PATH_TO_REPO/mcp_server.py --ingest-git",
        "timeout": 2000
      }
    ]
  }
]
```

Wait — hooks call shell commands, not MCP tools directly in this config. The existing pattern uses `prepare_hook.py` as a subprocess that calls MCP internally. The simplest approach is a new thin shell script `hooks/ingest_hook.py` that calls `vulcan_ingest_git` via MCP, following the same pattern as `prepare_hook.py`.

Read `hooks/prepare_hook.py` to understand the pattern before editing:

```bash
cat /home/aditya/workspaces/pycharm/temporal_reasoning/hooks/prepare_hook.py
```

Then create `hooks/ingest_hook.py` mirroring the pattern but calling `vulcan_ingest_git` with no arguments.

Then add to `claude-code.json` UserPromptSubmit hooks:

```json
{
  "type": "command",
  "command": "python PATH_TO_REPO/hooks/ingest_hook.py",
  "timeout": 2000
}
```

- [ ] **Step 2: Update hooks/codex.toml**

Add the same `ingest_hook.py` invocation to `[hooks.pre_turn]`:

```toml
[hooks.pre_turn]
command = ["python", "PATH_TO_REPO/hooks/prepare_hook.py"]
timeout_ms = 5000

[hooks.pre_turn_ingest]
command = ["python", "PATH_TO_REPO/hooks/ingest_hook.py"]
timeout_ms = 2000
```

- [ ] **Step 3: Update hooks/hermes.yaml**

Uncomment and extend the hooks block to include ingestion:

```yaml
hooks:
  pre_turn:
    - command: python PATH_TO_REPO/hooks/prepare_hook.py
      timeout_ms: 5000
    - command: python PATH_TO_REPO/hooks/ingest_hook.py
      timeout_ms: 2000
  post_turn:
    command: python PATH_TO_REPO/hooks/finalize_hook.py
    timeout_ms: 10000
```

- [ ] **Step 4: Update SKILL.md**

Add `vulcan_ingest_git` and `vulcan_ingest_status` to the tools table. Add a note that `ingestion` entity type is system-only. Add the code-structure query examples from the spec's "Queries this Unlocks" section.

- [ ] **Step 5: Update ROADMAP.md**

Mark Phase 5 as in-progress:

```markdown
## Phase 5 (In Progress) — Code Structure Evolution from Git History
```

- [ ] **Step 6: Commit**

```bash
git add hooks/ SKILL.md ROADMAP.md
git commit -m "feat(ingestion): wire auto-invocation hooks and update SKILL.md + ROADMAP"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| 4 new VULCAN_SCHEMA entity types | Task 1 |
| 2 new SESSION_RULES (`:contains`) | Task 1 |
| `vulcan_ingest_status` tool | Task 2 |
| Language detection + lazy grammar cache | Task 3 |
| AST extraction (functions, classes, imports, calls) | Task 4 |
| `_code_ident` with `::` separator for collision safety | Task 5 |
| `_git_commits`, `_git_changed_files`, `_git_file_content` | Task 6 |
| `:valid-from` on additions, `:valid-from/:valid-to` on deletions | Task 7 |
| Background `asyncio.Task`, `_db = None` between commits, `await asyncio.sleep(0)` | Task 8 |
| `vulcan_ingest_git` handler — returns immediately, "already in progress" guard | Task 8 |
| Watermark query + update per commit | Task 7 + 8 |
| Hook config updates (claude-code, codex, hermes) | Task 9 |
| SKILL.md and ROADMAP updates | Task 9 |
| `install.py` dependency checks | Task 1 |

**No gaps found.**

**Type consistency check:** `_ingest_transact` and `_ingest_close` both take `List[str]` triples and `str` timestamps — consistent with `_build_code_triples` return type. `_watermark_query` returns `Optional[str]` — consistent with `_git_commits` watermark parameter type. `_get_parser` returns `Optional[Any]` — consistent with `_extract_from_source` parser parameter.

**Note on spec deviation:** The spec describes an `asyncio.Lock` (`_db_lock`) for DB coordination. The existing codebase uses `_db = None` in `call_tool`'s `finally` to release the minigraf file lock between calls (commit f6d9bde). The background task uses the same pattern: set `_db = None` after each commit + `await asyncio.sleep(0)`. This achieves the same interleaving guarantee without introducing a new locking primitive that doesn't match the existing architecture.
