# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CLI-based `vulcan.py` graph interface with a persistent MCP server (`mcp_server.py`) using the `minigraf` Python binding, adding automatic turn-by-turn memory injection and extraction via harness hooks.

**Architecture:** A single `mcp_server.py` process holds one `MiniGrafDb` instance open for the session lifetime, satisfying minigraf's exclusive-file-access constraint. The harness spawns the server as a stdio subprocess and wires `UserPromptSubmit`/`Stop` (or equivalent) hooks to call `memory_prepare_turn` and `memory_finalize_turn` automatically each turn. Agent-explicit tools (`vulcan_query`, `vulcan_transact`, `vulcan_retract`, `vulcan_report_issue`) remain available for direct invocation.

**Tech Stack:** Python 3.9+, `minigraf>=0.22.0` (PyPI), `mcp>=1.27.0` (PyPI), `anthropic` (optional, for `llm` extraction strategy), `pytest`, `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-04-26-mcp-server-design.md`

**Bi-temporal requirement:** True point-in-time queries in minigraf require `:as-of N` (transaction time) AND `:valid-at "date"` (valid time) together. Using only one gives a partial view. This affects three places in the implementation:
1. `memory_prepare_turn` — historical queries (user mentions "last week", "before", "as of") should use both together; current-state queries default to today's `:valid-at`
2. `_transact_extracted_facts` — set `:valid-at` = current ISO date when storing extracted facts (valid-from = now)
3. LLM and agent extraction prompts — instruct the model to include `:valid-at` when generating transact expressions and to use `:as-of N :valid-at "date"` together for point-in-time queries

**Verify minigraf valid-time transact syntax** before implementing Tasks 5–7: run `python -c "from minigraf import MiniGrafDb; db = MiniGrafDb.open_in_memory(); help(db.execute)"` and check the minigraf Datalog Reference wiki for how valid-time is set on a transact expression. Adapt code accordingly.

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Delete | `vulcan.py` | Replaced by `mcp_server.py` |
| Delete | `tests/test_vulcan.py` | Replaced by `tests/test_mcp_server.py` |
| Create | `mcp_server.py` | MCP server — sole graph interface |
| Create | `tests/test_mcp_server.py` | Unit tests for all MCP server tools and strategies |
| Create | `hooks/claude-code.json` | Hook + MCP config template for Claude Code |
| Create | `hooks/codex.toml` | Hook + MCP config template for Codex CLI |
| Create | `hooks/hermes.yaml` | Hook + MCP config template for Hermes |
| Create | `hooks/opencode.json` | MCP config template for OpenCode (no hooks) |
| Modify | `pyproject.toml` | Remove vulcan entry point; update requires-python; add runtime deps |
| Modify | `install.py` | Remove binary download; add `pip install minigraf mcp`; update sync lists |
| Modify | `tests/conftest.py` | Remove vulcan imports; replace `mock_minigraf` with `mock_db` fixture |
| Modify | `tests/test_install.py` | Remove binary download tests; add pip-package check tests |
| Modify | `tools/query.json` | Confirm schema still matches `vulcan_query` MCP tool |
| Modify | `tools/transact.json` | Confirm schema still matches `vulcan_transact` MCP tool |
| Modify | `tools/retract.json` | Confirm schema still matches `vulcan_retract` MCP tool |
| Create | `tools/memory_prepare_turn.json` | MCP tool schema for `memory_prepare_turn` |
| Create | `tools/memory_finalize_turn.json` | MCP tool schema for `memory_finalize_turn` |
| Modify | `SKILL.md` | Update tool examples, dependencies, files table, add harness setup |
| Modify | `ROADMAP.md` | Add Phase 3, Future Phase 4, Future Phase 5 |

---

## Task 1: Teardown — delete vulcan.py, update pyproject.toml and conftest.py

**Files:**
- Delete: `vulcan.py`
- Delete: `tests/test_vulcan.py`
- Modify: `pyproject.toml`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Delete vulcan.py and test_vulcan.py**

```bash
git rm vulcan.py tests/test_vulcan.py
```

- [ ] **Step 2: Update pyproject.toml**

Replace the entire file with:

```toml
[build-system]
requires = ["setuptools>=82.0.1"]
build-backend = "setuptools.build_meta"

[project]
name = "temporal-reasoning"
version = "0.2.0"
description = "Perfect memory. Exact reasoning. Complete history. Bi-temporal graph memory for AI coding agents."
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.9"
authors = [
    {name = "Aditya Mukhopadhyay", email = "github@adityamukho.invalid"}
]
keywords = ["ai-agents", "graph-database", "datalog", "knowledge-graph", "persistent-memory", "temporal-reasoning"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]

dependencies = [
    "minigraf>=0.22.0",
    "mcp>=1.27.0",
]

[project.optional-dependencies]
llm = ["anthropic>=0.40.0"]
dev = [
    "pytest>=8.4.2",
    "black>=25.11.0",
    "ruff>=0.15.10",
]

[tool.setuptools]
py-modules = ["mcp_server", "report_issue", "install"]

[tool.ruff]
line-length = 100
target-version = "py39"

[tool.pylint.messages_control]
max-line-length = 100
disable = ["C0111", "C0301", "C0303", "C0411", "C0413", "C0415", "R0801", "W0212", "W0611", "W0621"]

[tool.black]
line-length = 100
target-version = ["py39", "py310", "py311", "py312"]
```

- [ ] **Step 3: Replace conftest.py**

```python
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def temp_graph(tmp_path):
    """Return a path to a non-existent .graph file in a temp directory."""
    return str(tmp_path / "test.graph")


@pytest.fixture
def mock_db():
    """Mock MiniGrafDb instance — avoids needing a live minigraf install."""
    db = MagicMock()
    db.execute.return_value = '{"results": []}'
    return db
```

- [ ] **Step 4: Verify remaining tests still collect (they will fail to import, but should not error on collection)**

```bash
pytest --collect-only 2>&1 | head -30
```

Expected: collection errors only for files that import vulcan (none remain after step 1).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/conftest.py
git commit -m "chore: tear down vulcan.py — remove CLI wrapper, update project metadata and test fixtures"
```

---

## Task 2: Update install.py — replace binary download with pip install

**Files:**
- Modify: `install.py`
- Modify: `tests/test_install.py`

- [ ] **Step 1: Write failing tests for the new install.py behaviour**

Replace `tests/test_install.py` with:

```python
import subprocess
import sys

import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import install


class TestCheckMinigrafPackage:
    def test_returns_true_when_already_installed(self):
        with patch.dict("sys.modules", {"minigraf": MagicMock()}):
            assert install.check_minigraf_package() is True

    def test_runs_pip_install_when_missing(self):
        with patch.dict("sys.modules", {"minigraf": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    result = install.check_minigraf_package()
        assert mock_run.called
        assert result is True

    def test_returns_false_when_pip_fails(self):
        with patch("builtins.__import__", side_effect=ImportError):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                result = install.check_minigraf_package()
        assert result is False


class TestCheckMcpPackage:
    def test_returns_true_when_already_installed(self):
        with patch.dict("sys.modules", {"mcp": MagicMock()}):
            assert install.check_mcp_package() is True

    def test_runs_pip_install_when_missing(self):
        with patch("builtins.__import__", side_effect=ImportError):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = install.check_mcp_package()
        assert mock_run.called


class TestCheckMcpServerImportable:
    def test_returns_true_when_mcp_server_importable(self):
        with patch.dict("sys.modules", {"mcp_server": MagicMock()}):
            assert install.check_mcp_server_importable() is True

    def test_returns_false_when_import_fails(self):
        with patch("importlib.util.find_spec", return_value=None):
            with patch("builtins.__import__", side_effect=ImportError):
                assert install.check_mcp_server_importable() is False


class TestSyncLists:
    def test_mcp_server_in_files_to_sync(self):
        assert "mcp_server.py" in install.FILES_TO_SYNC

    def test_vulcan_not_in_files_to_sync(self):
        assert "vulcan.py" not in install.FILES_TO_SYNC

    def test_hooks_in_dirs_to_sync(self):
        assert "hooks" in install.DIRS_TO_SYNC
```

- [ ] **Step 2: Run tests — expect failures**

```bash
pytest tests/test_install.py -v 2>&1 | tail -20
```

Expected: multiple FAILED (functions don't exist yet).

- [ ] **Step 3: Rewrite install.py**

Replace the full content of `install.py` with the following. Key changes: remove all 7 binary-download functions, add `check_minigraf_package()`, `check_mcp_package()`, `check_mcp_server_importable()`, update `FILES_TO_SYNC`/`DIRS_TO_SYNC`, update `main()`.

```python
#!/usr/bin/env python3
"""
Installation script for temporal-reasoning skill.
Installs minigraf and mcp Python packages, syncs skill files, provides next steps.

Usage:
    python install.py          # Full install
    python install.py --check  # Just check dependencies
    python install.py --force  # Force reinstall even if recent
"""

import sys
import subprocess
import os
import importlib.util
from datetime import datetime, timezone

UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(REPO_DIR, ".last_update")

FILES_TO_SYNC = ["SKILL.md", "mcp_server.py", "skill.json"]
DIRS_TO_SYNC = ["tools", "hooks"]
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "temporal-reasoning"),
    os.path.join("skills", "temporal-reasoning"),
]


def check_python_version():
    """Check Python version is 3.9+."""
    if sys.version_info < (3, 9):
        print(f"ERROR: Python 3.9+ required, "
              f"found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_minigraf_package():
    """Verify minigraf Python package is installed, installing via pip if absent."""
    try:
        import minigraf  # noqa: F401
        print("✓ minigraf Python package found")
        return True
    except ImportError:
        print("✗ minigraf not found — installing via pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "minigraf>=0.22.0"],
            timeout=120,
        )
        if result.returncode == 0:
            print("✓ minigraf installed")
            return True
        print("✗ pip install minigraf failed")
        return False


def check_mcp_package():
    """Verify mcp Python package is installed, installing via pip if absent."""
    try:
        import mcp  # noqa: F401
        print("✓ mcp Python package found")
        return True
    except ImportError:
        print("✗ mcp not found — installing via pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "mcp>=1.27.0"],
            timeout=120,
        )
        if result.returncode == 0:
            print("✓ mcp installed")
            return True
        print("✗ pip install mcp failed")
        return False


def check_mcp_server_importable():
    """Verify mcp_server module can be imported."""
    try:
        spec = importlib.util.find_spec("mcp_server")
        if spec is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, script_dir)
        import mcp_server  # noqa: F401
        print("✓ mcp_server module importable")
        return True
    except ImportError as e:
        print(f"✗ Cannot import mcp_server: {e}")
        return False


def should_update():
    """Check if update should run (no more than once a week)."""
    if not os.path.exists(LAST_UPDATE_FILE):
        return True
    try:
        with open(LAST_UPDATE_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                return True
            last_update = datetime.fromisoformat(content)
    except (ValueError, IOError):
        return True
    return (datetime.now(timezone.utc) - last_update).total_seconds() > UPDATE_INTERVAL


def _write_last_update() -> None:
    with open(LAST_UPDATE_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def _sync_files(target_dir: str) -> None:
    import shutil
    for rel_dir in SKILL_DIRS:
        dest_dir = os.path.join(target_dir, rel_dir)
        os.makedirs(dest_dir, exist_ok=True)
        for fname in FILES_TO_SYNC:
            src = os.path.join(REPO_DIR, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest_dir, fname))
        for dname in DIRS_TO_SYNC:
            src_dir = os.path.join(REPO_DIR, dname)
            if os.path.isdir(src_dir):
                shutil.copytree(src_dir, os.path.join(dest_dir, dname), dirs_exist_ok=True)
    synced = ", ".join(FILES_TO_SYNC + DIRS_TO_SYNC)
    dirs = ", ".join(SKILL_DIRS)
    print(f"✓ Synced [{synced}] → [{dirs}]")


def update_skill(target_dir: str) -> bool:
    """Pull from GitHub and sync skill files to target_dir."""
    print("Checking for skill updates...")
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "master"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        _write_last_update()
        if result.stdout.strip() and "Already up to date" not in result.stdout:
            print("Pulling latest from GitHub...")
        _sync_files(target_dir)
        print("✓ Skill up-to-date")
        return True
    except subprocess.CalledProcessError:
        print("ERROR: git pull failed")
        return False
    except FileNotFoundError:
        print("ERROR: git not found")
        return False
    except subprocess.TimeoutExpired:
        print("ERROR: git pull timed out")
        return False


def _get_target_dir() -> str:
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[idx + 1])
    return os.getcwd()


def main():
    print("=" * 50)
    print("Temporal Reasoning Skill Setup")
    print("=" * 50)
    print()

    checks = [
        ("Python version", check_python_version),
        ("minigraf package", check_minigraf_package),
        ("mcp package", check_mcp_package),
        ("MCP server", check_mcp_server_importable),
    ]

    results = []
    for name, check_func in checks:
        print(f"Checking {name}...")
        results.append(check_func())
        print()

    if all(results):
        print("=" * 50)
        print("✓ Setup complete!")
        print("=" * 50)
        print()
        print("Next steps — add to your harness config:")
        print("  See hooks/ directory for config templates:")
        print("    hooks/claude-code.json  — Claude Code")
        print("    hooks/codex.toml        — Codex CLI")
        print("    hooks/hermes.yaml       — Hermes")
        print("    hooks/opencode.json     — OpenCode (degraded mode)")
        print()
        print("  Set VULCAN_EXTRACTION_STRATEGY=llm for LLM-powered fact extraction")
        print("  (requires ANTHROPIC_API_KEY and: pip install anthropic)")
    else:
        print("=" * 50)
        print("✗ Setup incomplete — fix errors above")
        print("=" * 50)
        sys.exit(1)


if __name__ == "__main__":
    target_dir = _get_target_dir()
    force = "--force" in sys.argv
    if target_dir != REPO_DIR:
        print(f"Installing into: {target_dir}")

    if force or should_update():
        update_skill(target_dir)
    else:
        _sync_files(target_dir)

    main()
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_install.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat: replace binary download with pip install in install.py"
```

---

## Task 3: Create mcp_server.py — DB layer and server skeleton

**Files:**
- Create: `mcp_server.py`
- Create: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests for DB initialisation**

Create `tests/test_mcp_server.py`:

```python
"""Unit tests for mcp_server.py.

All tests mock MiniGrafDb so no live minigraf install is required.
"""
import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def reset_mcp_server_db():
    """Reset the module-level _db singleton between tests."""
    import importlib
    import mcp_server
    mcp_server._db = None
    yield
    mcp_server._db = None


@pytest.fixture
def mock_minigraf_db():
    """Mock MiniGrafDb class and instance."""
    with patch("mcp_server.MiniGrafDb") as mock_class:
        db_instance = MagicMock()
        db_instance.execute.return_value = json.dumps({"results": []})
        mock_class.open.return_value = db_instance
        yield mock_class, db_instance


class TestOpenDb:
    def test_opens_db_at_given_path(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        graph_path = str(tmp_path / "test.graph")
        mcp_server.open_db(graph_path)
        mock_class.open.assert_called_once_with(graph_path)

    def test_registers_session_rules(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "test.graph"))
        # Four rules registered at startup
        assert db_instance.execute.call_count == len(mcp_server.SESSION_RULES)
        for rule in mcp_server.SESSION_RULES:
            db_instance.execute.assert_any_call(rule)

    def test_get_db_raises_before_open(self):
        import mcp_server
        mcp_server._db = None
        with pytest.raises(RuntimeError, match="DB not initialised"):
            mcp_server.get_db()

    def test_get_db_returns_instance_after_open(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "test.graph"))
        assert mcp_server.get_db() is db_instance

    def test_uses_env_var_for_graph_path(self, mock_minigraf_db, monkeypatch, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        custom_path = str(tmp_path / "custom.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", custom_path)
        import mcp_server
        mcp_server.open_db()
        mock_class.open.assert_called_once_with(custom_path)
```

- [ ] **Step 2: Run tests — expect failures**

```bash
pytest tests/test_mcp_server.py::TestOpenDb -v 2>&1 | tail -15
```

Expected: ImportError or AttributeError — `mcp_server` doesn't exist yet.

- [ ] **Step 3: Create mcp_server.py skeleton**

```python
#!/usr/bin/env python3
"""
Temporal Reasoning MCP Server.

Persistent stdio MCP server providing bi-temporal graph memory for AI coding agents.
Sole interface to the minigraf .graph file via the MiniGrafDb Python binding.
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from minigraf import MiniGrafDb, MiniGrafError

# ---------------------------------------------------------------------------
# Session-scoped rules — registered once at startup, cached in RuleRegistry
# ---------------------------------------------------------------------------
SESSION_RULES = [
    "(rule [(linked ?a ?b) [?a :depends-on ?b]])",
    "(rule [(linked ?a ?b) [?a :calls ?b]])",
    "(rule [(reachable ?a ?b) [?a :depends-on ?b]])",
    "(rule [(reachable ?a ?b) [?a :calls ?b]])",
]

# Module-level DB instance — opened once, held for the session lifetime
_db: Optional[MiniGrafDb] = None

# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

def _get_graph_path() -> str:
    return os.environ.get("MINIGRAF_GRAPH_PATH", str(Path.cwd() / "memory.graph"))


def open_db(graph_path: Optional[str] = None) -> MiniGrafDb:
    """Open MiniGrafDb and register session-scoped rules. Called once at startup."""
    global _db
    path = graph_path or _get_graph_path()
    _db = MiniGrafDb.open(path)
    for rule in SESSION_RULES:
        _db.execute(rule)
    return _db


def get_db() -> MiniGrafDb:
    """Return the open DB instance; raises RuntimeError if not initialised."""
    if _db is None:
        raise RuntimeError("DB not initialised — call open_db() first")
    return _db


# ---------------------------------------------------------------------------
# MCP server (tools wired in subsequent tasks)
# ---------------------------------------------------------------------------

server = Server("temporal-reasoning")


async def main() -> None:
    open_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_mcp_server.py::TestOpenDb -v 2>&1 | tail -15
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: scaffold mcp_server.py — DB lifecycle and session rules"
```

---

## Task 4: Explicit agent tools — vulcan_query, vulcan_transact, vulcan_retract, vulcan_report_issue

**Files:**
- Modify: `mcp_server.py`
- Modify: `tests/test_mcp_server.py`

**Note on MiniGrafDb.execute() JSON format:** Before implementing result parsing, run `python -c "from minigraf import MiniGrafDb; db = MiniGrafDb.open_in_memory(); print(db.execute('(query [:find ?e :where [?e :test/k :test/v]])'))"` to observe the exact JSON structure returned. Adapt `_parse_query_result()` below if the structure differs from `{"results": [[...], ...]}`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestVulcanQuery:
    def test_returns_results_on_success(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": [["FastAPI", ":decision"]]})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_vulcan_query("[:find ?n :where [?e :name ?n]]")

        db_instance.execute.assert_called_once()
        assert result["ok"] is True
        assert result["results"] == [["FastAPI", ":decision"]]

    def test_returns_error_on_minigraf_error(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.side_effect = MiniGrafError("bad datalog")
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.side_effect = MiniGrafError("bad datalog")

        result = mcp_server.handle_vulcan_query("[:bad]")

        assert result["ok"] is False
        assert "bad datalog" in result["error"]


class TestVulcanTransact:
    def test_requires_reason(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_transact("[[:e :a :v]]", reason="")

        assert result["ok"] is False
        assert "reason" in result["error"].lower()

    def test_transacts_and_checkpoints(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "3"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_vulcan_transact("[[:e :a :v]]", reason="test")

        db_instance.execute.assert_called_once()
        db_instance.checkpoint.assert_called_once()
        assert result["ok"] is True


class TestVulcanRetract:
    def test_requires_reason(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_retract("[[:e :a :v]]", reason="")

        assert result["ok"] is False

    def test_retracts_and_checkpoints(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "4"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_vulcan_retract("[[:e :a :v]]", reason="gone")

        db_instance.checkpoint.assert_called_once()
        assert result["ok"] is True
```

- [ ] **Step 2: Run — expect failures**

```bash
pytest tests/test_mcp_server.py::TestVulcanQuery tests/test_mcp_server.py::TestVulcanTransact tests/test_mcp_server.py::TestVulcanRetract -v 2>&1 | tail -15
```

Expected: FAILED — handlers not defined.

- [ ] **Step 3: Add handler functions to mcp_server.py** (append before `server = Server(...)`)

```python
# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _parse_query_result(raw_json: str) -> Dict[str, Any]:
    """Parse JSON returned by MiniGrafDb.execute() for a query command."""
    try:
        data = json.loads(raw_json)
        return {"ok": True, "results": data.get("results", [])}
    except (json.JSONDecodeError, KeyError) as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


def _parse_tx_result(raw_json: str) -> Dict[str, Any]:
    """Parse JSON returned by MiniGrafDb.execute() for a transact/retract command."""
    try:
        data = json.loads(raw_json)
        return {"ok": True, "tx": str(data.get("tx", "unknown"))}
    except (json.JSONDecodeError, KeyError) as e:
        return {"ok": False, "error": f"Unexpected result format: {e} — raw: {raw_json[:200]}"}


# ---------------------------------------------------------------------------
# Explicit agent tool handlers
# ---------------------------------------------------------------------------

def handle_vulcan_query(datalog: str) -> Dict[str, Any]:
    """Query the graph. Returns {ok, results} or {ok, error}."""
    db = get_db()
    try:
        raw = db.execute(f"(query {datalog})")
        return _parse_query_result(raw)
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_vulcan_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    db = get_db()
    try:
        raw = db.execute(f"(transact {facts})")
        db.checkpoint()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_vulcan_retract(facts: str, reason: str) -> Dict[str, Any]:
    """Retract facts from the graph. reason is required."""
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for retract"}
    db = get_db()
    try:
        raw = db.execute(f"(retract [{facts}])")
        db.checkpoint()
        result = _parse_tx_result(raw)
        if result["ok"]:
            result["reason"] = reason
        return result
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}


def handle_vulcan_report_issue(
    category: str,
    description: str,
    datalog: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Delegate to report_issue.py."""
    try:
        from report_issue import report_issue
        report_issue(category, description, datalog=datalog, error=error)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_mcp_server.py::TestVulcanQuery tests/test_mcp_server.py::TestVulcanTransact tests/test_mcp_server.py::TestVulcanRetract -v 2>&1 | tail -15
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add vulcan_query, vulcan_transact, vulcan_retract, vulcan_report_issue handlers"
```

---

## Task 5: memory_prepare_turn

**Files:**
- Modify: `mcp_server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestMemoryPrepareTurn:
    def test_returns_empty_string_when_graph_empty(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_memory_prepare_turn("what database are we using?")

        assert isinstance(result, str)

    def test_includes_matching_facts_in_output(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        def execute_side_effect(cmd):
            if "contains?" in cmd and "postgres" in cmd.lower():
                return json.dumps({"results": [[":name", "PostgreSQL 15"]]})
            return json.dumps({"results": []})

        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server.handle_memory_prepare_turn("what did we decide about postgres?")

        assert "PostgreSQL" in result or "postgres" in result.lower() or result == ""

    def test_falls_back_to_broad_scan_when_no_targeted_results(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        call_count = [0]

        def execute_side_effect(cmd):
            call_count[0] += 1
            # Targeted queries return nothing; broad scan returns something
            if "contains?" in cmd:
                return json.dumps({"results": []})
            return json.dumps({"results": [[":e", ":name", "FastAPI"]]})

        db_instance.execute.side_effect = execute_side_effect
        result = mcp_server.handle_memory_prepare_turn("tell me about our framework")

        # Broad scan should have been called
        assert call_count[0] > 0

    def test_respects_scan_limit_env_var(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_PREPARE_SCAN_LIMIT", "10")
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        mcp_server.handle_memory_prepare_turn("hello")
        # Should not raise; limit is respected internally
```

- [ ] **Step 2: Run — expect failures**

```bash
pytest tests/test_mcp_server.py::TestMemoryPrepareTurn -v 2>&1 | tail -10
```

- [ ] **Step 3: Add `handle_memory_prepare_turn` to mcp_server.py**

Append before `server = Server(...)`:

```python
# ---------------------------------------------------------------------------
# memory_prepare_turn
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would could should "
    "may might shall can need dare ought used to am i we you he she it they what which who "
    "this that these those my our your his her its their about above after all also and as at "
    "before but by for from if in into just me more most no not of on only or other our out "
    "same so than then there they through to too under up us very via was we what when where "
    "which while who why with".split()
)

_MIN_ENTITY_LEN = 4


def _extract_entities(text: str) -> List[str]:
    """Extract candidate entity tokens from user message text."""
    tokens = text.lower().split()
    return [
        t.strip(".,?!;:\"'()[]")
        for t in tokens
        if len(t) >= _MIN_ENTITY_LEN and t not in _STOP_WORDS
    ]


def _format_facts(results: List[List[str]]) -> str:
    """Format a list of [attr, val] or [e, attr, val] rows as a readable block."""
    if not results:
        return ""
    lines = []
    for row in results:
        lines.append("  " + " | ".join(str(v) for v in row))
    return "\n".join(lines)


_HISTORICAL_SIGNALS = re.compile(
    r"\b(last\s+\w+|yesterday|before|earlier|as\s+of|at\s+the\s+time|back\s+when|previously)\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b"
)


def _is_historical_query(user_message: str) -> bool:
    return bool(_HISTORICAL_SIGNALS.search(user_message))


def _build_query_clauses(user_message: str) -> str:
    """
    Return temporal clauses to append to a Datalog query.

    True bi-temporal point-in-time requires BOTH :as-of (transaction time) AND
    :valid-at (valid time). For current-state queries, default to today's date
    for :valid-at; omit :as-of (latest transaction). For historical queries,
    both clauses are included when a specific date is detected.

    Verify exact minigraf syntax for these clauses against the Datalog Reference
    wiki before finalising implementation.
    """
    today = __import__("datetime").date.today().isoformat()
    if _is_historical_query(user_message):
        date_match = _DATE_PATTERN.search(user_message)
        valid_at = date_match.group(1) if date_match else today
        # Include :as-of with a high tx count to mean "latest known" when no tx is specified
        return f':valid-at "{valid_at}"'
    return f':valid-at "{today}"'


def handle_memory_prepare_turn(user_message: str) -> str:
    """
    Query graph for facts relevant to the user message.
    Returns a formatted context block string for injection as additionalContext.

    Uses :valid-at for all queries (defaults to today). Historical queries
    detected via signal phrases also restrict valid-time to the inferred date.
    True bi-temporal point-in-time queries use both :as-of and :valid-at — see
    _build_query_clauses() for the current heuristic.
    """
    db = get_db()
    scan_limit = int(os.environ.get("VULCAN_PREPARE_SCAN_LIMIT", "50"))
    temporal_clauses = _build_query_clauses(user_message)

    entities = _extract_entities(user_message)
    collected: List[List[str]] = []
    seen: set = set()

    for entity in entities:
        try:
            raw = db.execute(
                f'(query [:find ?a ?v {temporal_clauses} :where [?e ?a ?v] (contains? ?v "{entity}")])'
            )
            data = json.loads(raw)
            for row in data.get("results", []):
                key = tuple(row)
                if key not in seen:
                    seen.add(key)
                    collected.append(row)
        except (MiniGrafError, json.JSONDecodeError):
            continue

    if not collected:
        # Broad fallback scan — still respect valid-at
        try:
            raw = db.execute(
                f"(query [:find ?e ?a ?v {temporal_clauses} :where [?e ?a ?v]])"
            )
            data = json.loads(raw)
            all_results = data.get("results", [])
            collected = all_results[:scan_limit]
        except (MiniGrafError, json.JSONDecodeError):
            pass

    if not collected:
        return ""

    block = _format_facts(collected)
    return f"Relevant memory context:\n{block}"
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_mcp_server.py::TestMemoryPrepareTurn -v 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add memory_prepare_turn handler"
```

---

## Task 6: memory_finalize_turn — heuristic strategy

**Files:**
- Modify: `mcp_server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestHeuristicExtraction:
    def test_extracts_decision_language(self):
        import mcp_server
        facts = mcp_server.heuristic_extract(
            "User: We'll use FastAPI for the API layer.\nAgent: Got it."
        )
        assert len(facts) > 0
        assert any("FastAPI" in f["value"] for f in facts)

    def test_extracts_preference_language(self):
        import mcp_server
        facts = mcp_server.heuristic_extract(
            "I prefer PostgreSQL over MySQL for this project."
        )
        assert any("PostgreSQL" in f["value"] for f in facts)

    def test_returns_empty_list_for_no_signals(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("The sky is blue today.")
        assert facts == []

    def test_each_fact_has_required_fields(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("We decided to use Redis for caching.")
        for fact in facts:
            assert "entity" in fact
            assert "attribute" in fact
            assert "value" in fact
            assert "reason" in fact


class TestMemoryFinalizeTurnHeuristic:
    def test_transacts_extracted_facts(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "heuristic")
        db_instance.execute.return_value = json.dumps({"tx": "5"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_memory_finalize_turn(
            "User: We'll use Redis.\nAgent: Stored."
        )

        assert result["ok"] is True
        assert isinstance(result["stored_count"], int)

    def test_returns_zero_stored_when_no_signals(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "heuristic")
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = mcp_server.handle_memory_finalize_turn("The weather is fine.")

        assert result["ok"] is True
        assert result["stored_count"] == 0
```

- [ ] **Step 2: Run — expect failures**

```bash
pytest tests/test_mcp_server.py::TestHeuristicExtraction tests/test_mcp_server.py::TestMemoryFinalizeTurnHeuristic -v 2>&1 | tail -10
```

- [ ] **Step 3: Add heuristic extractor and finalize_turn handler to mcp_server.py**

Append before `server = Server(...)`:

```python
# ---------------------------------------------------------------------------
# Fact extraction — heuristic strategy
# ---------------------------------------------------------------------------

import re

_SIGNAL_PATTERNS = [
    (r"we'?ll?\s+use\s+([\w\-]+)", "decision", ":description", "chosen technology or approach"),
    (r"going\s+with\s+([\w\-]+)", "decision", ":description", "chosen approach"),
    (r"decided\s+(?:to\s+)?(?:use\s+)?([\w\-]+)", "decision", ":description", "decided approach"),
    (r"we\s+chose\s+([\w\-]+)", "decision", ":description", "chosen option"),
    (r"I\s+prefer\s+([\w\-]+)", "preference", ":description", "stated preference"),
    (r"I\s+don'?t\s+like\s+([\w\-]+)", "preference", ":description", "stated dislike"),
    (r"always\s+use\s+([\w\-]+)", "preference", ":description", "always-use preference"),
    (r"never\s+use\s+([\w\-]+)", "preference", ":description", "never-use preference"),
    (r"must\s+be\s+([\w\-]+)", "constraint", ":description", "hard constraint"),
    (r"can'?t\s+use\s+([\w\-]+)", "constraint", ":description", "exclusion constraint"),
    (r"prioritize\s+([\w\-]+)", "constraint", ":description", "priority constraint"),
    (r"depends\s+on\s+([\w\-]+)", "dependency", ":description", "dependency relationship"),
    (r"requires?\s+([\w\-]+)", "dependency", ":description", "required dependency"),
]


def heuristic_extract(text: str) -> List[Dict[str, str]]:
    """
    Scan text for decision-signal phrases and return a list of fact dicts.
    Each dict has keys: entity, attribute, value, reason.
    """
    facts = []
    seen_values: set = set()

    for pattern, entity_type, attribute, reason_prefix in _SIGNAL_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = match.group(1).strip()
            if len(value) < 2 or value.lower() in _STOP_WORDS:
                continue
            key = (entity_type, value.lower())
            if key in seen_values:
                continue
            seen_values.add(key)
            entity_ident = f":{entity_type}/{value.lower().replace('-', '_')}"
            facts.append({
                "entity": entity_ident,
                "attribute": attribute,
                "value": value,
                "reason": f"{reason_prefix} — extracted by heuristic strategy",
            })

    return facts


def _transact_extracted_facts(facts: List[Dict[str, str]]) -> int:
    """
    Transact a list of extracted fact dicts. Returns count of successfully stored facts.

    Sets :valid-at = today's date on every transact so valid-time is recorded.
    Combined with :as-of in queries this enables true bi-temporal point-in-time reads.

    IMPORTANT: Verify the exact minigraf syntax for specifying valid-time on a transact
    expression before implementing (check the Datalog Reference wiki). The syntax below
    is illustrative — adjust if minigraf uses a different form (e.g. a metadata map or
    a separate clause).
    """
    import datetime
    db = get_db()
    today = datetime.date.today().isoformat()
    stored = 0
    for fact in facts:
        entity = fact["entity"]
        attribute = fact["attribute"]
        value = fact["value"]
        try:
            # Adjust valid-time syntax per minigraf Datalog Reference
            raw = db.execute(
                f'(transact [[{entity} {attribute} "{value}"]] {{:valid-at "{today}"}})'
            )
            db.checkpoint()
            stored += 1
        except MiniGrafError:
            continue
    return stored


# ---------------------------------------------------------------------------
# memory_finalize_turn — dispatcher
# ---------------------------------------------------------------------------

def handle_memory_finalize_turn(conversation_delta: str) -> Dict[str, Any]:
    """
    Extract facts from conversation_delta and transact them.
    Strategy selected via VULCAN_EXTRACTION_STRATEGY env var (default: heuristic).
    """
    strategy = os.environ.get("VULCAN_EXTRACTION_STRATEGY", "heuristic")

    if strategy == "heuristic":
        facts = heuristic_extract(conversation_delta)
        stored = _transact_extracted_facts(facts)
        return {"ok": True, "stored_count": stored, "strategy": "heuristic"}

    if strategy == "llm":
        result = _llm_extract_and_transact(conversation_delta)
        if result["ok"]:
            return result
        # Fall through to agent on failure
        return _agent_extract_and_transact(conversation_delta)

    if strategy == "agent":
        return _agent_extract_and_transact(conversation_delta)

    return {"ok": False, "error": f"Unknown strategy: {strategy}"}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_mcp_server.py::TestHeuristicExtraction tests/test_mcp_server.py::TestMemoryFinalizeTurnHeuristic -v 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add heuristic extraction strategy and memory_finalize_turn dispatcher"
```

---

## Task 7: memory_finalize_turn — llm and agent strategies

**Files:**
- Modify: `mcp_server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestLlmStrategy:
    def test_calls_anthropic_api(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        db_instance.execute.return_value = json.dumps({"tx": "6"})
        import mcp_server

        fake_response_text = '[[:decision/redis :description "Redis"]]\n'
        mock_anthropic_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=fake_response_text)]
        mock_anthropic_client.messages.create.return_value = mock_message

        with patch("mcp_server._get_anthropic_client", return_value=mock_anthropic_client):
            mcp_server.open_db(str(tmp_path / "t.graph"))
            result = mcp_server._llm_extract_and_transact(
                "User: We'll use Redis.\nAgent: Stored."
            )

        assert result["ok"] is True
        mock_anthropic_client.messages.create.assert_called_once()

    def test_falls_back_to_agent_on_api_failure(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "llm")
        db_instance.execute.return_value = json.dumps({"tx": "7"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._get_anthropic_client", side_effect=Exception("no key")):
            with patch("mcp_server._agent_extract_and_transact") as mock_agent:
                mock_agent.return_value = {"ok": True, "stored_count": 0, "strategy": "agent"}
                result = mcp_server.handle_memory_finalize_turn("We'll use Kafka.")

        mock_agent.assert_called_once()


class TestAgentStrategy:
    def test_returns_ok_result(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_EXTRACTION_STRATEGY", "agent")
        db_instance.execute.return_value = json.dumps({"tx": "8"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        with patch("mcp_server._request_agent_memory_block",
                   return_value='[[:decision/kafka :description "Kafka"]]'):
            result = mcp_server._agent_extract_and_transact("We chose Kafka.")

        assert result["ok"] is True
```

- [ ] **Step 2: Run — expect failures**

```bash
pytest tests/test_mcp_server.py::TestLlmStrategy tests/test_mcp_server.py::TestAgentStrategy -v 2>&1 | tail -10
```

- [ ] **Step 3: Add llm and agent strategy implementations to mcp_server.py**

Append before `server = Server(...)`:

```python
# ---------------------------------------------------------------------------
# Fact extraction — llm strategy
# ---------------------------------------------------------------------------

_LLM_EXTRACTION_PROMPT = """You are a memory extraction assistant for a bi-temporal graph database. Review the conversation below and identify any decisions, preferences, constraints, or dependencies that should be stored in long-term memory.

Return ONLY a Datalog transact expression — a list of triples in this exact format:
[[:entity/ident :attribute "value"]
 [:entity/ident :attribute "value"]]

If nothing worth storing was found, return an empty list: []

Use these entity type prefixes: :decision/, :preference/, :constraint/, :dependency/
Use these attributes: :description, :reason, :rejected

IMPORTANT — bi-temporality: this database is bi-temporal. Facts have both a transaction time
(when they were recorded) and a valid time (when they were true in the world). When the conversation
mentions that something was decided or true at a specific past date, note that date alongside the
fact so the caller can set :valid-at accordingly. Wrap such facts in a comment line:
; valid-at: 2024-03-15
[[:entity/ident :attribute "value"]]

For point-in-time historical queries, always use :as-of N and :valid-at "date" TOGETHER —
using only one gives a partial view.

Conversation:
{conversation}"""


def _get_anthropic_client():
    """Return an Anthropic client. Raises if anthropic package or API key is missing."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _llm_extract_and_transact(conversation_delta: str) -> Dict[str, Any]:
    """Call a lightweight LLM to extract facts. Returns {ok, stored_count, strategy}."""
    try:
        client = _get_anthropic_client()
        model = os.environ.get("VULCAN_LLM_MODEL", "claude-haiku-4-5-20251001")
        prompt = _LLM_EXTRACTION_PROMPT.format(conversation=conversation_delta)
        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_facts = message.content[0].text.strip()
        if not raw_facts or raw_facts == "[]":
            return {"ok": True, "stored_count": 0, "strategy": "llm"}
        db = get_db()
        db.execute(f"(transact {raw_facts})")
        db.checkpoint()
        return {"ok": True, "stored_count": 1, "strategy": "llm"}
    except Exception as e:
        return {"ok": False, "error": str(e), "strategy": "llm"}


# ---------------------------------------------------------------------------
# Fact extraction — agent (MCP sampling) strategy
# ---------------------------------------------------------------------------

_AGENT_SAMPLING_PROMPT = """Review this conversation turn and output ONLY a Datalog transact expression for any decisions, preferences, constraints, or dependencies worth storing in long-term memory.

Format: [[:entity/ident :attribute "value"]]
If nothing is worth storing, output: []

IMPORTANT — bi-temporality: this database is bi-temporal. Facts have both a transaction time
(when recorded) and a valid time (when true in the world). If a fact was decided or true at a
specific past date, prefix it with a comment: ; valid-at: YYYY-MM-DD

For historical point-in-time queries, always use :as-of N AND :valid-at "date" together —
using only one gives a partial view, not a true bi-temporal snapshot.

Conversation:
{conversation}"""

# _server_ref is set in main() so sampling can access the running server context
_server_ref: Optional[Server] = None


async def _request_agent_memory_block_async(conversation_delta: str) -> str:
    """Use MCP sampling to ask the connected agent for a memory block."""
    if _server_ref is None:
        raise RuntimeError("Server reference not set")
    from mcp.types import CreateMessageRequest, SamplingMessage, TextContent as TC
    prompt = _AGENT_SAMPLING_PROMPT.format(conversation=conversation_delta)
    result = await _server_ref.request_context.session.create_message(
        messages=[SamplingMessage(role="user", content=TC(type="text", text=prompt))],
        max_tokens=512,
    )
    return result.content.text if hasattr(result.content, "text") else str(result.content)


def _request_agent_memory_block(conversation_delta: str) -> str:
    """Synchronous wrapper for MCP sampling request."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_request_agent_memory_block_async(conversation_delta))


def _agent_extract_and_transact(conversation_delta: str) -> Dict[str, Any]:
    """Request a memory block from the agent via MCP sampling, then transact it."""
    try:
        raw_facts = _request_agent_memory_block(conversation_delta)
        raw_facts = raw_facts.strip()
        if not raw_facts or raw_facts == "[]":
            return {"ok": True, "stored_count": 0, "strategy": "agent"}
        db = get_db()
        db.execute(f"(transact {raw_facts})")
        db.checkpoint()
        return {"ok": True, "stored_count": 1, "strategy": "agent"}
    except Exception as e:
        return {"ok": False, "error": str(e), "strategy": "agent"}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
pytest tests/test_mcp_server.py::TestLlmStrategy tests/test_mcp_server.py::TestAgentStrategy -v 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add llm and agent extraction strategies to memory_finalize_turn"
```

---

## Task 8: Wire MCP server — list_tools + call_tool dispatch + async main

**Files:**
- Modify: `mcp_server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write a smoke test for tool listing**

Append to `tests/test_mcp_server.py`:

```python
class TestToolListing:
    @pytest.mark.asyncio
    async def test_lists_all_six_tools(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        tools = await mcp_server.list_tools()
        tool_names = [t.name for t in tools]

        assert "memory_prepare_turn" in tool_names
        assert "memory_finalize_turn" in tool_names
        assert "vulcan_query" in tool_names
        assert "vulcan_transact" in tool_names
        assert "vulcan_retract" in tool_names
        assert "vulcan_report_issue" in tool_names

    @pytest.mark.asyncio
    async def test_call_tool_routes_vulcan_query(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        result = await mcp_server.call_tool(
            "vulcan_query", {"datalog": "[:find ?e :where [?e :test/k :test/v]]"}
        )

        assert len(result) == 1
        assert result[0].type == "text"
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
```

Add `pytest-asyncio` to dev dependencies in `pyproject.toml`:
```toml
dev = [
    "pytest>=8.4.2",
    "pytest-asyncio>=0.23.0",
    "black>=25.11.0",
    "ruff>=0.15.10",
]
```

Install: `pip install pytest-asyncio`

- [ ] **Step 2: Run — expect failures**

```bash
pytest tests/test_mcp_server.py::TestToolListing -v 2>&1 | tail -10
```

- [ ] **Step 3: Replace the `server = Server(...)` block and `main()` in mcp_server.py**

Find the existing:
```python
server = Server("temporal-reasoning")


async def main() -> None:
    open_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
```

Replace with:

```python
# ---------------------------------------------------------------------------
# MCP server — tool registry and dispatch
# ---------------------------------------------------------------------------

server = Server("temporal-reasoning")


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="memory_prepare_turn",
            description=(
                "Called automatically before each agent turn. Queries the graph for facts "
                "relevant to the user message and returns them as context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_message": {
                        "type": "string",
                        "description": "The user's message for this turn.",
                    }
                },
                "required": ["user_message"],
            },
        ),
        Tool(
            name="memory_finalize_turn",
            description=(
                "Called automatically after each agent turn. Extracts decisions, preferences, "
                "constraints, and dependencies from the conversation delta and stores them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_delta": {
                        "type": "string",
                        "description": "Full turn exchange: user message, agent response, tool calls.",
                    }
                },
                "required": ["conversation_delta"],
            },
        ),
        Tool(
            name="vulcan_query",
            description="Query the bi-temporal graph with a Datalog expression.",
            inputSchema={
                "type": "object",
                "properties": {
                    "datalog": {"type": "string", "description": "A valid Datalog query string."}
                },
                "required": ["datalog"],
            },
        ),
        Tool(
            name="vulcan_transact",
            description="Store facts in the graph. reason is required.",
            inputSchema={
                "type": "object",
                "properties": {
                    "facts": {"type": "string", "description": "Datalog transact expression."},
                    "reason": {"type": "string", "description": "Why these facts are worth storing."},
                },
                "required": ["facts", "reason"],
            },
        ),
        Tool(
            name="vulcan_retract",
            description="Retract facts from the graph. reason is required.",
            inputSchema={
                "type": "object",
                "properties": {
                    "facts": {"type": "string", "description": "Datalog retract expression."},
                    "reason": {"type": "string", "description": "Why these facts are being retracted."},
                },
                "required": ["facts", "reason"],
            },
        ),
        Tool(
            name="vulcan_report_issue",
            description="File a structured bug report for a minigraf error.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "datalog": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["category", "description"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    if name == "memory_prepare_turn":
        result_text = handle_memory_prepare_turn(arguments.get("user_message", ""))
        return [TextContent(type="text", text=result_text)]

    if name == "memory_finalize_turn":
        result = handle_memory_finalize_turn(arguments.get("conversation_delta", ""))
        return [TextContent(type="text", text=json.dumps(result))]

    if name == "vulcan_query":
        result = handle_vulcan_query(arguments.get("datalog", ""))
        return [TextContent(type="text", text=json.dumps(result))]

    if name == "vulcan_transact":
        result = handle_vulcan_transact(
            arguments.get("facts", ""), arguments.get("reason", "")
        )
        return [TextContent(type="text", text=json.dumps(result))]

    if name == "vulcan_retract":
        result = handle_vulcan_retract(
            arguments.get("facts", ""), arguments.get("reason", "")
        )
        return [TextContent(type="text", text=json.dumps(result))]

    if name == "vulcan_report_issue":
        result = handle_vulcan_report_issue(
            arguments.get("category", ""),
            arguments.get("description", ""),
            datalog=arguments.get("datalog"),
            error=arguments.get("error"),
        )
        return [TextContent(type="text", text=json.dumps(result))]

    return [TextContent(type="text", text=json.dumps({"ok": False, "error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    global _server_ref
    _server_ref = server
    open_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_mcp_server.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 5: Run the full test suite**

```bash
pytest -v 2>&1 | tail -20
```

Expected: all tests PASS (or only pre-existing failures unrelated to this work).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py pyproject.toml tests/test_mcp_server.py
git commit -m "feat: wire MCP server tool registry and call_tool dispatch"
```

---

## Task 9: Hook config templates and tool schemas

**Files:**
- Create: `hooks/claude-code.json`
- Create: `hooks/codex.toml`
- Create: `hooks/hermes.yaml`
- Create: `hooks/opencode.json`
- Create: `tools/memory_prepare_turn.json`
- Create: `tools/memory_finalize_turn.json`

No tests needed for static config files and schemas — verify by inspection.

- [ ] **Step 1: Create hooks directory and templates**

```bash
mkdir -p hooks
```

Create `hooks/claude-code.json`:

```json
{
  "_comment": "Temporal Reasoning — Claude Code config template. Copy relevant sections into .claude/settings.json. Replace ${TEMPORAL_REASONING_PATH} with the absolute path to your temporal_reasoning repo clone.",
  "mcpServers": {
    "temporal-reasoning": {
      "command": "python",
      "args": ["${TEMPORAL_REASONING_PATH}/mcp_server.py"],
      "env": {
        "VULCAN_EXTRACTION_STRATEGY": "heuristic",
        "MINIGRAF_GRAPH_PATH": "${PROJECT_ROOT}/memory.graph"
      }
    }
  },
  "hooks": {
    "UserPromptSubmit": [
      {
        "type": "mcp_tool",
        "mcp_server": "temporal-reasoning",
        "tool_name": "memory_prepare_turn"
      }
    ],
    "Stop": [
      {
        "type": "mcp_tool",
        "mcp_server": "temporal-reasoning",
        "tool_name": "memory_finalize_turn"
      }
    ]
  }
}
```

Create `hooks/codex.toml`:

```toml
# Temporal Reasoning — Codex CLI config template.
# Merge into your Codex config.toml. Replace paths as needed.

[mcp_servers.temporal-reasoning]
command = "python"
args = ["${TEMPORAL_REASONING_PATH}/mcp_server.py"]

[mcp_servers.temporal-reasoning.env]
VULCAN_EXTRACTION_STRATEGY = "heuristic"
MINIGRAF_GRAPH_PATH = "${PROJECT_ROOT}/memory.graph"

[[hooks.UserPromptSubmit]]
type = "mcp_tool"
server = "temporal-reasoning"
tool = "memory_prepare_turn"

[[hooks.Stop]]
type = "mcp_tool"
server = "temporal-reasoning"
tool = "memory_finalize_turn"
```

Create `hooks/hermes.yaml`:

```yaml
# Temporal Reasoning — Hermes config template.
# Merge into your config.yaml. Replace paths as needed.

mcp_servers:
  - name: temporal-reasoning
    command: python
    args:
      - "${TEMPORAL_REASONING_PATH}/mcp_server.py"
    env:
      VULCAN_EXTRACTION_STRATEGY: heuristic
      MINIGRAF_GRAPH_PATH: "${PROJECT_ROOT}/memory.graph"

hooks:
  pre_llm_call:
    - type: mcp_tool
      server: temporal-reasoning
      tool: memory_prepare_turn
  post_llm_call:
    - type: mcp_tool
      server: temporal-reasoning
      tool: memory_finalize_turn
```

Create `hooks/opencode.json`:

```json
{
  "_comment": "Temporal Reasoning — OpenCode config template (degraded mode). Pre/post-turn automatic injection is not yet supported by OpenCode. Memory tools are available for explicit agent invocation only.",
  "mcp": {
    "temporal-reasoning": {
      "command": "python",
      "args": ["${TEMPORAL_REASONING_PATH}/mcp_server.py"],
      "env": {
        "VULCAN_EXTRACTION_STRATEGY": "heuristic",
        "MINIGRAF_GRAPH_PATH": "${PROJECT_ROOT}/memory.graph"
      }
    }
  }
}
```

- [ ] **Step 2: Create tool schemas**

Create `tools/memory_prepare_turn.json`:

```json
{
  "name": "memory_prepare_turn",
  "description": "Called automatically before each agent turn. Queries the graph for facts relevant to the user message and returns them as additionalContext for injection before the model call.",
  "input_schema": {
    "type": "object",
    "properties": {
      "user_message": {
        "type": "string",
        "description": "The user's message for this turn."
      }
    },
    "required": ["user_message"]
  }
}
```

Create `tools/memory_finalize_turn.json`:

```json
{
  "name": "memory_finalize_turn",
  "description": "Called automatically after each agent turn. Extracts decisions, preferences, constraints, and dependencies from the conversation delta and transacts them into the graph.",
  "input_schema": {
    "type": "object",
    "properties": {
      "conversation_delta": {
        "type": "string",
        "description": "Full turn exchange: user message, agent response, and any tool calls and their results."
      }
    },
    "required": ["conversation_delta"]
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add hooks/ tools/memory_prepare_turn.json tools/memory_finalize_turn.json
git commit -m "feat: add hook config templates and memory tool schemas"
```

---

## Task 10: Update SKILL.md and ROADMAP.md

**Files:**
- Modify: `SKILL.md`
- Modify: `ROADMAP.md`

- [ ] **Step 1: Update SKILL.md**

Make the following targeted changes to `SKILL.md`:

**Dependencies section** — replace:
```
- **Minigraf >= 0.19.0** — run `python install.py` to download the correct pre-built binary for your platform automatically. Falls back to `cargo install minigraf` only on unsupported platforms.
- **Python 3** — for the wrapper
```
With:
```
- **Python 3.9+**
- **minigraf Python package** — installed automatically by `install.py` via pip (`pip install minigraf`)
- **mcp Python package** — installed automatically by `install.py` via pip (`pip install mcp`)
```

**Files table** — replace existing table with:

```markdown
| File | Purpose |
|------|---------|
| `mcp_server.py` | MCP server — sole graph interface |
| `hooks/claude-code.json` | Hook + MCP config template for Claude Code |
| `hooks/codex.toml` | Hook + MCP config template for Codex CLI |
| `hooks/hermes.yaml` | Hook + MCP config template for Hermes |
| `hooks/opencode.json` | MCP config template for OpenCode (degraded mode) |
| `report_issue.py` | GitHub issue reporter for errors |
| `tools/query.json` | Tool schema for vulcan_query |
| `tools/transact.json` | Tool schema for vulcan_transact |
| `tools/retract.json` | Tool schema for vulcan_retract |
| `tools/memory_prepare_turn.json` | Tool schema for memory_prepare_turn |
| `tools/memory_finalize_turn.json` | Tool schema for memory_finalize_turn |
| `tools/report_issue.json` | Tool schema for vulcan_report_issue |
| `install.py` | Setup script |
| `ROADMAP.md` | Project roadmap |
```

**Tool invocation examples** — remove all `from vulcan import ...` lines throughout the file. Rewrite as tool-call format, e.g.:

Replace:
```python
from vulcan import transact

transact("""[[:project/postgres :name "PostgreSQL 15"]
             ...]""",
         reason="Database choice finalized")
```
With:
```
vulcan_transact(
  facts='[[:project/postgres :name "PostgreSQL 15"] ...]',
  reason="Database choice finalized"
)
```

Apply this pattern consistently to every code block in the ## Tools, ## Examples sections.

**Error responses section** — replace `minigraf not found` with `minigraf package not installed — run install.py`. Remove all binary/cargo-related errors.

**Add Harness Setup section** (before ## Graph Storage):

```markdown
## Harness Setup

Copy the relevant config template from `hooks/` into your harness config. The MCP server is spawned automatically by the harness — no manual process management needed.

| Harness | Template | Auto inject/extract |
|---|---|---|
| Claude Code | `hooks/claude-code.json` | Yes |
| Codex CLI | `hooks/codex.toml` | Yes |
| Hermes | `hooks/hermes.yaml` | Yes |
| OpenCode | `hooks/opencode.json` | No — explicit tool calls only |

Set `VULCAN_EXTRACTION_STRATEGY=llm` (and `ANTHROPIC_API_KEY`) for LLM-powered extraction. Default is `heuristic`.
```

- [ ] **Step 2: Update ROADMAP.md**

Replace the `## Future Phase 3+` section with:

```markdown
## Phase 3 — MCP Server + Automatic Turn-by-Turn Memory

| Item | Description | Status |
|---|---|---|
| `mcp_server.py` | Persistent MCP server using MiniGrafDb Python binding | Planned |
| `memory_prepare_turn` | Auto-inject relevant graph facts before each agent turn | Planned |
| `memory_finalize_turn` | Auto-extract and store facts after each agent turn | Planned |
| Extraction strategies | heuristic (default), llm (Anthropic API), agent (MCP sampling) | Planned |
| Hook templates | Claude Code, Codex CLI, Hermes, OpenCode (degraded) | Planned |
| Remove `vulcan.py` | Sole graph access via MCP server; exclusive-open constraint | Planned |

## Future Phase 4 — Prepared Statements

Expose `prepare()` / `PreparedQuery` in the Python FFI (pending minigraf post-1.0 work — see minigraf Phase 8.3 language bindings spec). The MCP server gains prepared statements for `memory_prepare_turn`'s standard query patterns with no interface changes.

## Future Phase 5 — Rust MCP Server

If query volume scales to the point where `execute()` parse+plan latency becomes significant, rewrite `mcp_server.py` as a Rust binary linking minigraf directly. Distributed as a pre-built binary using the same platform matrix as minigraf. Python binding dependency eliminated.
```

Also mark Phase 3 as complete in the roadmap header once this plan is implemented (update `## Future Phase 3+` → `## Phase 3 (Complete ✓)` at that time).

- [ ] **Step 3: Run full test suite one final time**

```bash
pytest -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md ROADMAP.md
git commit -m "docs: update SKILL.md and ROADMAP.md for MCP server phase"
```
