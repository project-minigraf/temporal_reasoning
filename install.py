#!/usr/bin/env python3
"""
Installation script for temporal-reasoning skill.
Installs minigraf and mcp Python packages, syncs skill files, provides next steps.

--harness is required and selects exactly one target; only that harness's
skill directory (and, for claude-code, Claude Code's config files) is
created or updated. Supported values: claude-code, opencode, codex.

Usage:
    python install.py --harness claude-code            # Full install for Claude Code
    python install.py --harness opencode                # Sync skill files for OpenCode
    python install.py --harness codex                   # Sync skill files for Codex CLI
    python install.py --harness claude-code --check      # Just check dependencies
    python install.py --harness claude-code --force      # Force reinstall even if recent
    python install.py --harness claude-code --target DIR # Install into a specific project directory
"""

import sys
import subprocess
import os
import re
import tempfile
import importlib.util
from datetime import datetime, timezone

UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(REPO_DIR, ".last_update")
VENV_DIR = os.path.join(REPO_DIR, ".venv")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python")

def _plugin_version() -> str:
    """Read the canonical version from .claude-plugin/plugin.json."""
    import json
    path = os.path.join(REPO_DIR, ".claude-plugin", "plugin.json")
    try:
        return json.load(open(path))["version"]
    except Exception:
        return "0.3.0"

PLUGIN_VERSION = _plugin_version()

FILES_TO_SYNC = ["SKILL.md", "mcp_server.py", "skill.json"]
DIRS_TO_SYNC = ["tools", "hooks"]
SUPPORTED_HARNESSES = ("claude-code", "opencode", "codex")
# Per-harness project-local skill directory. Verified against each harness's own
# docs (2026-07-17), not assumed from prior code:
#   - claude-code: Claude Code discovers project skills under .claude/skills/
#     (https://opencode.ai/docs/skills/ independently documents this as the
#     "Project Claude-compatible" path, corroborating Claude Code's own docs).
#   - opencode:    .opencode/skills/ is OpenCode's documented project scope
#     (https://opencode.ai/docs/skills/).
#   - codex:       Codex CLI scans .agents/skills/ from cwd up to the repo root
#     — NOT .codex/skills/, which was an unverified assumption in earlier code
#     (https://developers.openai.com/codex/skills, confirmed 2026-07-17: "Codex
#     scans .agents/skills in every directory from your current working
#     directory up to the repository root").
HARNESS_SKILL_DIRS = {
    "codex": os.path.join(".agents", "skills", "temporal-reasoning"),
    "opencode": os.path.join(".opencode", "skills", "temporal-reasoning"),
    "claude-code": os.path.join(".claude", "skills", "temporal-reasoning"),
}
_MANUAL_CONFIG_TEMPLATE = {
    "opencode": os.path.join("hooks", "opencode.json"),
    "codex": os.path.join("hooks", "codex.toml"),
}
_HARNESS_FLAG_VALUE_ARGS = ("--harness", "--target")


def _resolve_harness(argv):
    """Parse and validate the required --harness argument from *argv*.

    Returns the harness string on success. On a missing or invalid value,
    prints actionable usage text and returns None — callers must treat None
    as "stop before writing any files."
    """
    if "--harness" not in argv:
        print("✗ --harness is required.")
        print(f"  Supported harnesses: {', '.join(SUPPORTED_HARNESSES)}")
        print(f"  Usage: python install.py --harness <{'|'.join(SUPPORTED_HARNESSES)}>")
        return None
    idx = argv.index("--harness")
    if idx + 1 >= len(argv):
        print("✗ --harness requires a value.")
        print(f"  Supported harnesses: {', '.join(SUPPORTED_HARNESSES)}")
        return None
    value = argv[idx + 1]
    if value not in SUPPORTED_HARNESSES:
        print(f"✗ Unknown harness {value!r}.")
        print(f"  Supported harnesses: {', '.join(SUPPORTED_HARNESSES)}")
        return None
    return value


def ensure_venv() -> bool:
    """Create the virtualenv at VENV_DIR if it doesn't already exist."""
    if os.path.exists(VENV_PYTHON):
        print(f"✓ Virtualenv found at {VENV_DIR}")
        return True
    print(f"  Creating virtualenv at {VENV_DIR}...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", VENV_DIR],
        timeout=60,
    )
    if result.returncode == 0:
        print(f"✓ Virtualenv created at {VENV_DIR}")
        return True
    print(f"✗ Could not create virtualenv — {result.returncode}")
    return False


def check_python_version():
    """Check Python version is 3.9+."""
    if sys.version_info < (3, 9):
        print(f"ERROR: Python 3.9+ required, "
              f"found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def _venv_has(module: str) -> bool:
    """Return True if *module* is importable inside the venv."""
    result = subprocess.run(
        [VENV_PYTHON, "-c", f"import {module}"],
        capture_output=True,
    )
    return result.returncode == 0


def _venv_pip_install(*specs: str, timeout: int = 300) -> bool:
    """Install one or more pip specs into the venv. Returns True on success."""
    result = subprocess.run(
        [VENV_PYTHON, "-m", "pip", "install"] + list(specs),
        timeout=timeout,
    )
    return result.returncode == 0


def check_minigraf_package():
    """Verify minigraf Python package is installed in the venv."""
    if _venv_has("minigraf"):
        print("✓ minigraf Python package found")
        return True
    print("✗ minigraf not found — installing via pip...")
    if _venv_pip_install("minigraf>=1.2.1", timeout=120):
        print("✓ minigraf installed")
        return True
    print("✗ pip install minigraf failed")
    return False


def check_mcp_package():
    """Verify mcp Python package is installed in the venv."""
    if _venv_has("mcp"):
        print("✓ mcp Python package found")
        return True
    print("✗ mcp not found — installing via pip...")
    if _venv_pip_install("mcp>=1.27.0", timeout=120):
        print("✓ mcp installed")
        return True
    print("✗ pip install mcp failed")
    return False


def check_tree_sitter_packages():
    """Verify tree-sitter grammar support, installing packages if absent.

    Required for git ingestion to extract code structure (functions, classes,
    imports) from source files. Without it, ingestion runs silently but stores
    no code entities.

    Installs the individual tree-sitter-<lang> packages (tree-sitter-rust,
    tree-sitter-python, ...) via the tree-sitter >=0.22 API, compatible across
    Python 3.10-3.14+.

    This previously tried the bundled `tree-sitter-languages` package first as
    a fast path, but that package pins no upper bound on its `tree-sitter`
    dependency and hasn't been updated since tree-sitter's 0.22 API redesign —
    a fresh `pip install tree-sitter-languages` silently resolves an
    incompatible `tree-sitter` and every parse fails at runtime with no error
    surfaced (see issue #86). Individual packages are the only supported path now.
    """
    if _venv_has("tree_sitter_python"):
        print("✓ tree-sitter language packages found")
        return True

    print("  Installing tree-sitter language packages...")
    individual = [
        "tree-sitter>=0.22.0",
        "tree-sitter-rust", "tree-sitter-python", "tree-sitter-javascript",
        "tree-sitter-typescript", "tree-sitter-go", "tree-sitter-java",
        "tree-sitter-c", "tree-sitter-cpp",
        "tree-sitter-c-sharp", "tree-sitter-ruby", "tree-sitter-php",
        "tree-sitter-kotlin", "tree-sitter-swift", "tree-sitter-scala",
        "tree-sitter-haskell", "tree-sitter-lua", "tree-sitter-elixir",
    ]
    if _venv_pip_install(*individual):
        print("✓ tree-sitter language packages installed")
        return True

    print("✗ Could not install tree-sitter grammar support — code ingestion will be disabled")
    return False


def check_mcp_server_importable():
    """Verify mcp_server module can be imported inside the venv."""
    result = subprocess.run(
        [VENV_PYTHON, "-c", "import sys; sys.path.insert(0, ''); import mcp_server"],
        capture_output=True,
        cwd=REPO_DIR,
    )
    if result.returncode == 0:
        print("✓ mcp_server module importable")
        return True
    stderr = result.stderr.decode(errors="replace").strip()
    print(f"✗ Cannot import mcp_server: {stderr}")
    return False


def _pyproject_py_modules() -> list:
    """Parse the `py-modules` list from pyproject.toml's [tool.setuptools] table.

    Regex parse rather than a TOML library dependency — the value is always a
    flat array of quoted strings, matching this project's other lightweight
    config-parsing (e.g. _plugin_version).
    """
    path = os.path.join(REPO_DIR, "pyproject.toml")
    try:
        with open(path) as f:
            content = f.read()
    except IOError:
        return []
    match = re.search(r"py-modules\s*=\s*\[([^\]]*)\]", content)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def _editable_install_present() -> bool:
    """Return True if the venv already has an editable install of this package."""
    result = subprocess.run(
        [VENV_PYTHON, "-m", "pip", "show", "temporal-reasoning"],
        capture_output=True,
    )
    return result.returncode == 0


def check_editable_install_current() -> bool:
    """Verify an existing editable install resolves every top-level module
    declared in pyproject.toml from outside REPO_DIR, refreshing it if not.

    setuptools' editable-install finder bakes a module-name -> path MAPPING at
    `pip install -e .` time. Pulling changes that add a new top-level module
    (e.g. fact_index, added by #118) doesn't update an already-baked mapping —
    the module then only resolves when the importing process's cwd happens to
    be REPO_DIR, which is why mcp_server and the hooks (which add REPO_DIR to
    sys.path themselves) are unaffected but any other out-of-repo entry point
    would silently break (see issue #151). Skipped entirely if no editable
    install exists yet — this only heals drift in installs that already
    opted in, it doesn't create one.
    """
    if not _editable_install_present():
        print("✓ No editable install of temporal-reasoning found — nothing to check")
        return True

    modules = _pyproject_py_modules()
    if not modules:
        print("✓ No py-modules declared — nothing to check")
        return True

    def _probe() -> bool:
        result = subprocess.run(
            [VENV_PYTHON, "-c", "; ".join(f"import {m}" for m in modules)],
            capture_output=True,
            cwd=tempfile.gettempdir(),
        )
        return result.returncode == 0

    if _probe():
        print(f"✓ Editable install up to date ({', '.join(modules)})")
        return True

    print("  Editable install predates a new top-level module — refreshing...")
    if _venv_pip_install("-e", REPO_DIR, timeout=120) and _probe():
        print("✓ Editable install refreshed")
        return True

    print("✗ Could not refresh editable install — re-run `pip install -e .` in the venv manually")
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


def _sync_files(target_dir: str, harness: str) -> None:
    import shutil
    rel_dir = HARNESS_SKILL_DIRS[harness]
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
    print(f"✓ Synced [{synced}] → [{rel_dir}]")


def update_skill(target_dir: str, harness: str) -> bool:
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
        _sync_files(target_dir, harness)
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
    argv = sys.argv[1:]
    if "--target" in argv:
        idx = argv.index("--target")
        if idx + 1 < len(argv):
            return os.path.abspath(argv[idx + 1])
    # Accept a bare positional path argument (e.g. `python install.py /path/to/project`),
    # skipping over recognized flags and the values that belong to them.
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in _HARNESS_FLAG_VALUE_ARGS:
            skip_next = True
            continue
        if not arg.startswith("-"):
            return os.path.abspath(arg)
    return os.getcwd()


_PLACEHOLDER_KEY = "your-api-key-here"


def setup_mcp_json(target_dir: str) -> bool:
    """Idempotently write the temporal-reasoning MCP server block into .mcp.json.

    - Creates the file if absent.
    - Merges into existing content if present (other servers are preserved).
    - Always updates MINIGRAF_GRAPH_PATH and MINIGRAF_INDEX_PATH to reflect
      the target project path.
    - Uses `uvx temporal-reasoning[git-ingestion]` so the published PyPI
      package is invoked directly — no local venv path baked in. `[git-ingestion]`
      is required so uvx's ephemeral venv actually has the tree-sitter
      packages code-structure extraction depends on (see issue #93 — a bare
      `uvx temporal-reasoning` resolves none of them, silently disabling
      code-structure extraction). handle_memory_prepare_turn queries a
      persisted SQLite FTS5 index (fact_index.py) instead of scanning the
      graph on every turn -- FTS5 is compiled into stdlib sqlite3 on every
      mainstream Python build this project targets, so unlike the rank-bm25
      cache it replaced, there is no missing-dependency fallback path to
      keep working (see issues #96, #117, #118).
    - Only MINIGRAF_GRAPH_PATH and MINIGRAF_INDEX_PATH are set here;
      ANTHROPIC_API_KEY and MINIGRAF_EXTRACTION_STRATEGY belong in
      .claude/settings.local.json so they are available to hook subprocesses
      as well as the MCP server.
    """
    import json

    mcp_json_path = os.path.join(target_dir, ".mcp.json")
    graph_path = os.path.join(target_dir, "memory.graph")
    index_path = f"{graph_path}.fts.sqlite3"

    existing: dict = {}
    file_existed = os.path.exists(mcp_json_path)
    if file_existed:
        try:
            with open(mcp_json_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = {}

    existing.setdefault("mcpServers", {})["temporal-reasoning"] = {
        "type": "stdio",
        "command": "uvx",
        "args": ["temporal-reasoning[git-ingestion]"],
        "env": {
            "MINIGRAF_GRAPH_PATH": graph_path,
            "MINIGRAF_INDEX_PATH": index_path,
        },
    }

    try:
        with open(mcp_json_path, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
    except IOError as e:
        print(f"✗ Could not write .mcp.json: {e}")
        return False

    verb = "Updated" if file_existed else "Created"
    print(f"✓ {verb} {mcp_json_path}")
    print(f"    command = uvx temporal-reasoning[git-ingestion]")
    print(f"    MINIGRAF_GRAPH_PATH = {graph_path}")
    print(f"    MINIGRAF_INDEX_PATH = {index_path}")
    return True


def setup_claude_settings_json(target_dir: str) -> bool:
    """Idempotently write enabledPlugins, extraKnownMarketplaces, and
    enabledMcpjsonServers into .claude/settings.json.

    - Creates .claude/ and the file if absent.
    - Merges into existing content (other keys are preserved).
    - Always sets the marketplace path to the current REPO_DIR.
    """
    import json

    claude_dir = os.path.join(target_dir, ".claude")
    settings_path = os.path.join(claude_dir, "settings.json")

    existing: dict = {}
    file_existed = os.path.exists(settings_path)
    if file_existed:
        try:
            with open(settings_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = {}

    # enabledPlugins
    plugins = existing.setdefault("enabledPlugins", {})
    plugins.pop("minigraf@temporal-reasoning-local", None)  # remove stale key
    plugins["temporal-reasoning@temporal-reasoning-local"] = True

    # extraKnownMarketplaces — point at the stub, not REPO_DIR, so Claude Code's
    # internal mc$() copier doesn't choke on .venv/ when syncing to the plugin cache.
    stub_path = os.path.join(
        os.path.expanduser("~"), ".claude", "plugins", "stubs", "temporal-reasoning-local",
    )
    marketplaces = existing.setdefault("extraKnownMarketplaces", {})
    marketplaces["temporal-reasoning-local"] = {
        "source": {
            "source": "directory",
            "path": stub_path,
        }
    }

    # enabledMcpjsonServers
    mcp_servers = existing.setdefault("enabledMcpjsonServers", [])
    if "temporal-reasoning" not in mcp_servers:
        mcp_servers.append("temporal-reasoning")

    # Hooks belong in settings.local.json, not here — remove any stale entry
    existing.pop("hooks", None)

    os.makedirs(claude_dir, exist_ok=True)
    try:
        with open(settings_path, "w") as f:
            json.dump(existing, f, indent=4)
            f.write("\n")
    except IOError as e:
        print(f"✗ Could not write {settings_path}: {e}")
        return False

    verb = "Updated" if file_existed else "Created"
    print(f"✓ {verb} {settings_path}")
    print(f"    enabledPlugins.temporal-reasoning@temporal-reasoning-local = true")
    print(f"    extraKnownMarketplaces.temporal-reasoning-local → {stub_path}")
    print(f"    enabledMcpjsonServers += temporal-reasoning")
    return True


def setup_claude_settings(target_dir: str) -> bool:
    """Idempotently write hooks and env vars into .claude/settings.local.json.

    - Creates .claude/ and the file if absent.
    - Merges into existing content (permissions and other keys are preserved).
    - For hooks: searches existing UserPromptSubmit/Stop arrays for an entry
      that already references our hook scripts and updates the command path;
      appends a new entry only if none is found.
    - Preserves ANTHROPIC_API_KEY if already set to a real value.
    - Sets MINIGRAF_EXTRACTION_STRATEGY=llm (default); preserves existing value.
    - Hook commands use the venv python so they share the same environment.
    - These env vars are written here (not in .mcp.json) so that hook
      subprocesses inherit them from the Claude Code process environment.
    """
    import json

    prepare_cmd = f"{VENV_PYTHON} {os.path.join(REPO_DIR, 'hooks', 'prepare_hook.py')}"
    finalize_cmd = f"{VENV_PYTHON} {os.path.join(REPO_DIR, 'hooks', 'finalize_hook.py')}"

    claude_dir = os.path.join(target_dir, ".claude")
    settings_path = os.path.join(claude_dir, "settings.local.json")

    existing: dict = {}
    file_existed = os.path.exists(settings_path)
    if file_existed:
        try:
            with open(settings_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = {}

    # --- env block ---
    env_block = existing.setdefault("env", {})
    prev_key = env_block.get("ANTHROPIC_API_KEY", "")
    key_is_real = bool(prev_key) and prev_key != _PLACEHOLDER_KEY
    if not key_is_real:
        env_block["ANTHROPIC_API_KEY"] = _PLACEHOLDER_KEY
    if "MINIGRAF_EXTRACTION_STRATEGY" not in env_block:
        env_block["MINIGRAF_EXTRACTION_STRATEGY"] = "heuristic"

    # --- hooks ---
    hooks_block = existing.setdefault("hooks", {})

    def _upsert_hook(event: str, script_marker: str, command: str, timeout: int) -> str:
        """Insert or update a hook command for the given event. Returns 'added'/'updated'."""
        entries = hooks_block.setdefault(event, [])
        # Search for an existing entry whose hook command references our script
        for entry in entries:
            for hook in entry.get("hooks", []):
                if script_marker in hook.get("command", ""):
                    old_cmd = hook["command"]
                    hook["command"] = command
                    hook["timeout"] = timeout
                    return "updated" if old_cmd != command else "unchanged"
        # Not found — append a new matcher entry
        entries.append({
            "matcher": "",
            "hooks": [{"type": "command", "command": command, "timeout": timeout}],
        })
        return "added"

    prepare_status = _upsert_hook("UserPromptSubmit", "prepare_hook.py", prepare_cmd, 5000)
    finalize_status = _upsert_hook("Stop", "finalize_hook.py", finalize_cmd, 10000)

    os.makedirs(claude_dir, exist_ok=True)
    try:
        with open(settings_path, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
    except IOError as e:
        print(f"✗ Could not write {settings_path}: {e}")
        return False

    verb = "Updated" if file_existed else "Created"
    print(f"✓ {verb} {settings_path}")
    print(f"    UserPromptSubmit hook ({prepare_status}): {prepare_cmd}")
    print(f"    Stop hook ({finalize_status}): {finalize_cmd}")
    print(f"    env.MINIGRAF_EXTRACTION_STRATEGY = {env_block['MINIGRAF_EXTRACTION_STRATEGY']}")
    if key_is_real:
        print("    env.ANTHROPIC_API_KEY = (preserved)")
    else:
        print(f"    env.ANTHROPIC_API_KEY = {_PLACEHOLDER_KEY}  ← replace with your key")
    return True


def _build_plugin_stub() -> str:
    """Create a minimal stub directory that Claude Code can safely copy to cache.

    Claude Code's internal loader (mc$) resolves the plugin source path as:
        path.join(extraKnownMarketplaces[marketplace].source.path, plugin.source)
    and copies that entire tree into:
        ~/.claude/plugins/cache/{marketplace}/{plugin}/{version}/

    When that source path is REPO_DIR (which contains a multi-hundred-MB .venv/)
    the copy fails silently.  We solve this by:
      1. Building a stub directory (~/.claude/plugins/stubs/…) with only the
         files Claude Code needs (.claude-plugin/, skills/).
      2. Pointing extraKnownMarketplaces at the stub, not REPO_DIR.
      3. The stub → cache copy is small and succeeds.

    Returns the stub directory path.
    """
    import shutil

    home_claude = os.path.join(os.path.expanduser("~"), ".claude")
    stub_dir = os.path.join(
        home_claude, "plugins", "stubs", "temporal-reasoning-local",
    )

    # Only the directories Claude Code needs:
    #   .claude-plugin/  — plugin.json & marketplace.json (identity)
    #   skills/          — SKILL.md discovery
    #   .mcp.json        — MCP server config (uvx-based, no local paths)
    essential = [".claude-plugin", "skills"]

    os.makedirs(stub_dir, exist_ok=True)

    for name in essential:
        src = os.path.join(REPO_DIR, name)
        dst = os.path.join(stub_dir, name)
        if not os.path.exists(src):
            continue
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    # Write a clean .mcp.json using uvx — no local paths baked in.
    # This is what marketplace installs use; the project-level .mcp.json
    # (written by setup_mcp_json) adds MINIGRAF_GRAPH_PATH on top of this.
    import json as _json
    stub_mcp = os.path.join(stub_dir, ".mcp.json")
    with open(stub_mcp, "w") as f:
        _json.dump({
            "mcpServers": {
                "temporal-reasoning": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["temporal-reasoning[git-ingestion]"],
                },
            },
        }, f, indent=2)
        f.write("\n")

    # Remove stale versioned cache directories (other than the current version).
    cache_plugin_dir = os.path.join(
        home_claude, "plugins", "cache", "temporal-reasoning-local", "temporal-reasoning",
    )
    if os.path.isdir(cache_plugin_dir):
        for entry in os.listdir(cache_plugin_dir):
            if entry != PLUGIN_VERSION:
                stale = os.path.join(cache_plugin_dir, entry)
                if os.path.isdir(stale):
                    shutil.rmtree(stale)
                    print(f"  Removed stale cache {stale}")

    print(f"✓ Plugin stub built at {stub_dir}")
    return stub_dir


def register_plugin_with_claude() -> bool:
    """Register the plugin in Claude Code's user-level config files so it appears
    in /skills globally (not just for the current project).

    Files updated:
    - ~/.claude/plugins/stubs/…/  — minimal stub Claude Code can copy from
    - ~/.claude/settings.json     — enabledPlugins + extraKnownMarketplaces → stub
    - ~/.claude/plugins/installed_plugins.json — installPath → expected cache dir
    - ~/.claude/plugins/known_marketplaces.json — refresh timestamp
    """
    import json

    home_claude = os.path.join(os.path.expanduser("~"), ".claude")
    plugins_dir = os.path.join(home_claude, "plugins")
    user_settings_path = os.path.join(home_claude, "settings.json")
    installed_path = os.path.join(plugins_dir, "installed_plugins.json")
    marketplaces_path = os.path.join(plugins_dir, "known_marketplaces.json")

    plugin_key = "temporal-reasoning@temporal-reasoning-local"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
          f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

    # --- Build minimal stub so Claude Code's loader doesn't choke on .venv ---
    stub_path = _build_plugin_stub()

    # Expected cache dir (where mc$ will copy the stub on next startup)
    cache_path = os.path.join(
        plugins_dir, "cache",
        "temporal-reasoning-local", "temporal-reasoning", PLUGIN_VERSION,
    )

    # --- ~/.claude/settings.json: enable plugin + point marketplace at stub ---
    user_settings: dict = {}
    if os.path.exists(user_settings_path):
        try:
            with open(user_settings_path) as f:
                user_settings = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    user_settings.setdefault("enabledPlugins", {})[plugin_key] = True
    user_settings.setdefault("extraKnownMarketplaces", {})["temporal-reasoning-local"] = {
        "source": {
            "source": "directory",
            "path": stub_path,
        }
    }

    try:
        with open(user_settings_path, "w") as f:
            json.dump(user_settings, f, indent=2)
            f.write("\n")
        print(f"✓ Enabled plugin in {user_settings_path}")
        print(f"    enabledPlugins.{plugin_key} = true")
        print(f"    extraKnownMarketplaces.temporal-reasoning-local → {stub_path}")
    except IOError as e:
        print(f"✗ Could not write {user_settings_path}: {e}")
        return False

    # --- installed_plugins.json: pre-register with expected cache path ---
    installed: dict = {"version": 2, "plugins": {}}
    if os.path.exists(installed_path):
        try:
            with open(installed_path) as f:
                installed = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    existing_entries = installed.setdefault("plugins", {}).get(plugin_key, [])
    user_entry = next((e for e in existing_entries if e.get("scope") == "user"), None)
    if user_entry:
        user_entry["installPath"] = cache_path
        user_entry["version"] = PLUGIN_VERSION
        user_entry["lastUpdated"] = now
        action = "updated"
    else:
        existing_entries.insert(0, {
            "scope": "user",
            "installPath": cache_path,
            "version": PLUGIN_VERSION,
            "installedAt": now,
            "lastUpdated": now,
        })
        installed["plugins"][plugin_key] = existing_entries
        action = "registered"

    try:
        with open(installed_path, "w") as f:
            json.dump(installed, f, indent=2)
            f.write("\n")
    except IOError as e:
        print(f"✗ Could not write {installed_path}: {e}")
        return False

    print(f"✓ Plugin {action} in {installed_path}")
    print(f"    {plugin_key} → {cache_path}")

    # --- known_marketplaces.json: update source.path and installLocation to stub ---
    # This is the authoritative store Claude Code reads at startup; settings.json
    # changes only propagate here on the next full marketplace sync.  We write it
    # directly so the stub path takes effect immediately.
    marketplaces: dict = {}
    if os.path.exists(marketplaces_path):
        try:
            with open(marketplaces_path) as f:
                marketplaces = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    marketplaces["temporal-reasoning-local"] = {
        "source": {
            "source": "directory",
            "path": stub_path,
        },
        "installLocation": stub_path,
        "lastUpdated": now,
    }
    try:
        with open(marketplaces_path, "w") as f:
            json.dump(marketplaces, f, indent=2)
            f.write("\n")
        print(f"✓ Updated marketplace in {marketplaces_path}")
        print(f"    temporal-reasoning-local → {stub_path}")
    except IOError as e:
        print(f"  (could not update {marketplaces_path}: {e})")

    return True


def main(target_dir: str = "", harness: str = "claude-code", update_ok: bool = True) -> None:
    print("=" * 50)
    print("Temporal Reasoning Skill Setup")
    print("=" * 50)
    print()

    if not target_dir:
        target_dir = _get_target_dir()

    print("Checking virtualenv...")
    venv_ok = ensure_venv()
    print()
    if not venv_ok:
        print("=" * 50)
        print("✗ Setup incomplete — fix errors above")
        print("=" * 50)
        sys.exit(1)

    checks = [
        ("Python version", check_python_version),
        ("minigraf package", check_minigraf_package),
        ("mcp package", check_mcp_package),
        ("tree-sitter language packages", check_tree_sitter_packages),
        ("MCP server", check_mcp_server_importable),
        ("editable install freshness", check_editable_install_current),
    ]

    results = []
    for name, check_func in checks:
        print(f"Checking {name}...")
        results.append(check_func())
        print()

    if harness != "claude-code":
        ok = all(results) and update_ok
        print("=" * 50)
        print("✓ Setup complete!" if ok else "✗ Setup incomplete — fix errors above")
        print("=" * 50)
        print()
        print(f"Skill files synced to your {harness} skill directory.")
        print(f"Manual MCP + hook configuration required — see {_MANUAL_CONFIG_TEMPLATE[harness]} for a template.")
        if not ok:
            sys.exit(1)
        return

    print("Configuring .mcp.json...")
    mcp_ok = setup_mcp_json(target_dir)
    print()

    print("Configuring .claude/settings.json...")
    settings_json_ok = setup_claude_settings_json(target_dir)
    print()

    print("Configuring .claude/settings.local.json...")
    settings_ok = setup_claude_settings(target_dir)
    print()

    print("Registering plugin with Claude Code...")
    plugin_ok = register_plugin_with_claude()
    print()

    if all(results) and mcp_ok and settings_json_ok and settings_ok and plugin_ok and update_ok:
        print("=" * 50)
        print("✓ Setup complete!")
        print("=" * 50)
        print()
        print("Replace any 'your-api-key-here' placeholders in:")
        print("  .claude/settings.local.json    — hooks + Claude Code env (ANTHROPIC_API_KEY)")
    else:
        print("=" * 50)
        print("✗ Setup incomplete — fix errors above")
        print("=" * 50)
        sys.exit(1)


if __name__ == "__main__":
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        sys.exit(0)

    harness = _resolve_harness(sys.argv[1:])
    if harness is None:
        sys.exit(2)

    target_dir = _get_target_dir()
    force = "--force" in sys.argv
    if target_dir != REPO_DIR:
        print(f"Installing into: {target_dir}")

    if force or should_update():
        update_ok = update_skill(target_dir, harness)
    else:
        _sync_files(target_dir, harness)
        update_ok = True

    main(target_dir, harness, update_ok=update_ok)
