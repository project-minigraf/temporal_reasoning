#!/usr/bin/env python3
"""
Uninstallation script for temporal-reasoning skill.
Reverses all changes made by install.py.

Usage:
    python uninstall.py              # Uninstall from current directory
    python uninstall.py /path/to/project  # Uninstall from specific project
    python uninstall.py --dry-run    # Show what would be removed without doing it
"""

import sys
import os
import json
import shutil
from typing import Callable

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)
import install

# Sourced from install.py's HARNESS_SKILL_DIRS (not hand-duplicated) so this list
# can't drift out of sync with the per-harness paths install.py actually writes —
# a hand-maintained copy here previously went stale when #132 changed those paths.
SKILL_DIRS = list(install.HARNESS_SKILL_DIRS.values())

PLUGIN_KEY = "temporal-reasoning@temporal-reasoning-local"
MARKETPLACE_KEY = "temporal-reasoning-local"
MCP_SERVER_KEY = "temporal-reasoning"


def _get_target_dir() -> str:
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[idx + 1])
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            return os.path.abspath(arg)
    return os.getcwd()


DRY_RUN = "--dry-run" in sys.argv


def _remove_dir(path: str) -> None:
    if not os.path.isdir(path):
        print(f"  (skip) {path} — not found")
        return
    if DRY_RUN:
        print(f"  [dry-run] would remove dir: {path}")
        return
    shutil.rmtree(path)
    print(f"✓ Removed dir: {path}")


def _remove_file(path: str) -> None:
    if not os.path.isfile(path):
        print(f"  (skip) {path} — not found")
        return
    if DRY_RUN:
        print(f"  [dry-run] would remove file: {path}")
        return
    os.remove(path)
    print(f"✓ Removed file: {path}")


def _remove_empty_parents(path: str, stop_at: str) -> None:
    """Walk up from path removing empty directories until stop_at."""
    current = os.path.dirname(path)
    while current and current != stop_at and current != os.path.dirname(current):
        if not os.path.isdir(current) or os.listdir(current):
            break
        if DRY_RUN:
            print(f"  [dry-run] would remove empty dir: {current}")
        else:
            os.rmdir(current)
            print(f"✓ Removed empty dir: {current}")
        current = os.path.dirname(current)


def _edit_json(path: str, mutate: Callable[[dict], bool], description: str) -> None:
    """Load a JSON file, apply mutate(data) → changed, write back if changed."""
    if not os.path.isfile(path):
        print(f"  (skip) {path} — not found")
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  (skip) {path} — could not read: {e}")
        return
    changed = mutate(data)
    if not changed:
        print(f"  (skip) {path} — {description} not present")
        return
    if DRY_RUN:
        print(f"  [dry-run] would update {path} — {description}")
        return
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print(f"✓ Updated {path} — {description}")
    except IOError as e:
        print(f"✗ Could not write {path}: {e}")


# ---------------------------------------------------------------------------
# Project-level removals
# ---------------------------------------------------------------------------

def remove_skill_dirs(target_dir: str) -> None:
    """Remove temporal-reasoning skill dirs from .codex, .opencode, and skills/."""
    print("Removing skill directories...")
    for rel_dir in SKILL_DIRS:
        full = os.path.join(target_dir, rel_dir)
        _remove_dir(full)
        # Remove the parent (e.g. .opencode/skills/) if now empty
        _remove_empty_parents(full, target_dir)
    print()


def remove_mcp_json(target_dir: str) -> None:
    """Remove the temporal-reasoning entry from .mcp.json; delete file if empty."""
    path = os.path.join(target_dir, ".mcp.json")
    print("Updating .mcp.json...")

    def mutate(data: dict) -> bool:
        servers = data.get("mcpServers", {})
        if MCP_SERVER_KEY not in servers:
            return False
        del servers[MCP_SERVER_KEY]
        if not servers:
            data.pop("mcpServers", None)
        return True

    _edit_json(path, mutate, f"removed mcpServers.{MCP_SERVER_KEY}")

    # If the file is now empty (or only has empty keys), remove it
    if not DRY_RUN and os.path.isfile(path):
        try:
            with open(path) as f:
                remaining = json.load(f)
            if not remaining:
                os.remove(path)
                print(f"✓ Removed empty {path}")
        except (json.JSONDecodeError, IOError):
            pass
    print()


def remove_project_settings_json(target_dir: str) -> None:
    """Remove temporal-reasoning entries from .claude/settings.json."""
    path = os.path.join(target_dir, ".claude", "settings.json")
    print("Updating .claude/settings.json...")

    def mutate(data: dict) -> bool:
        changed = False
        plugins = data.get("enabledPlugins", {})
        if PLUGIN_KEY in plugins:
            del plugins[PLUGIN_KEY]
            changed = True
        markets = data.get("extraKnownMarketplaces", {})
        if MARKETPLACE_KEY in markets:
            del markets[MARKETPLACE_KEY]
            changed = True
        servers = data.get("enabledMcpjsonServers", [])
        if MCP_SERVER_KEY in servers:
            servers.remove(MCP_SERVER_KEY)
            changed = True
        return changed

    _edit_json(path, mutate,
               f"removed {PLUGIN_KEY}, {MARKETPLACE_KEY}, enabledMcpjsonServers entry")
    print()


_PLACEHOLDER_KEY = "your-api-key-here"


def remove_project_settings_local_json(target_dir: str) -> None:
    """Remove temporal-reasoning hooks and env vars from .claude/settings.local.json.

    Only removes ANTHROPIC_API_KEY if it is still the install.py placeholder — a
    real key the user set must never be deleted.  MINIGRAF_GRAPH_PATH is not
    touched here because install.py writes it to .mcp.json, not this file.
    """
    path = os.path.join(target_dir, ".claude", "settings.local.json")
    print("Updating .claude/settings.local.json...")

    def mutate(data: dict) -> bool:
        changed = False

        env = data.get("env", {})
        # MINIGRAF_EXTRACTION_STRATEGY — always ours, safe to remove
        if "MINIGRAF_EXTRACTION_STRATEGY" in env:
            del env["MINIGRAF_EXTRACTION_STRATEGY"]
            changed = True
        # ANTHROPIC_API_KEY — only remove the placeholder we wrote; never a real key
        if env.get("ANTHROPIC_API_KEY") == _PLACEHOLDER_KEY:
            del env["ANTHROPIC_API_KEY"]
            changed = True

        # Remove hook entries that reference our scripts
        hooks = data.get("hooks", {})
        for event in ("UserPromptSubmit", "Stop"):
            entries = hooks.get(event, [])
            filtered = [
                e for e in entries
                if not any(
                    "prepare_hook.py" in h.get("command", "") or
                    "finalize_hook.py" in h.get("command", "")
                    for h in e.get("hooks", [])
                )
            ]
            if len(filtered) != len(entries):
                hooks[event] = filtered
                changed = True

        return changed

    _edit_json(path, mutate, "removed hooks and env vars")
    print()


# ---------------------------------------------------------------------------
# Global (user-level) removals
# ---------------------------------------------------------------------------

def remove_global_plugin_registration() -> None:
    """Undo register_plugin_with_claude(): stub dir, settings.json, installed_plugins,
    known_marketplaces."""
    home_claude = os.path.join(os.path.expanduser("~"), ".claude")
    plugins_dir = os.path.join(home_claude, "plugins")

    print("Removing plugin stub...")
    stub_dir = os.path.join(plugins_dir, "stubs", "temporal-reasoning-local")
    _remove_dir(stub_dir)
    _remove_empty_parents(stub_dir, plugins_dir)
    print()

    print("Removing plugin cache...")
    cache_dir = os.path.join(
        plugins_dir, "cache", "temporal-reasoning-local",
    )
    _remove_dir(cache_dir)
    _remove_empty_parents(cache_dir, plugins_dir)
    print()

    print("Updating ~/.claude/settings.json...")
    user_settings_path = os.path.join(home_claude, "settings.json")

    def mutate_user_settings(data: dict) -> bool:
        changed = False
        plugins = data.get("enabledPlugins", {})
        if PLUGIN_KEY in plugins:
            del plugins[PLUGIN_KEY]
            changed = True
        markets = data.get("extraKnownMarketplaces", {})
        if MARKETPLACE_KEY in markets:
            del markets[MARKETPLACE_KEY]
            changed = True
        return changed

    _edit_json(user_settings_path, mutate_user_settings,
               f"removed {PLUGIN_KEY} and {MARKETPLACE_KEY}")
    print()

    print("Updating ~/.claude/plugins/installed_plugins.json...")
    installed_path = os.path.join(plugins_dir, "installed_plugins.json")

    def mutate_installed(data: dict) -> bool:
        plugins = data.get("plugins", {})
        if PLUGIN_KEY not in plugins:
            return False
        del plugins[PLUGIN_KEY]
        return True

    _edit_json(installed_path, mutate_installed, f"removed {PLUGIN_KEY}")
    print()

    print("Updating ~/.claude/plugins/known_marketplaces.json...")
    marketplaces_path = os.path.join(plugins_dir, "known_marketplaces.json")

    def mutate_marketplaces(data: dict) -> bool:
        if MARKETPLACE_KEY not in data:
            return False
        del data[MARKETPLACE_KEY]
        return True

    _edit_json(marketplaces_path, mutate_marketplaces, f"removed {MARKETPLACE_KEY}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 50)
    print("Temporal Reasoning Skill Uninstall")
    print("=" * 50)
    if DRY_RUN:
        print("(dry-run mode — no files will be changed)")
    print()

    target_dir = _get_target_dir()
    if target_dir != REPO_DIR:
        print(f"Uninstalling from: {target_dir}")
        print()

    remove_skill_dirs(target_dir)
    remove_mcp_json(target_dir)
    remove_project_settings_json(target_dir)
    remove_project_settings_local_json(target_dir)
    remove_global_plugin_registration()

    print("=" * 50)
    if DRY_RUN:
        print("Dry-run complete. Re-run without --dry-run to apply.")
    else:
        print("✓ Uninstall complete.")
        print()
        print("The following were NOT removed (delete manually if desired):")
        print(f"  {os.path.join(target_dir, 'memory.graph')}  — your graph data")
        print(f"  {os.path.join(REPO_DIR, '.venv')}            — Python virtualenv")
    print("=" * 50)


if __name__ == "__main__":
    main()
