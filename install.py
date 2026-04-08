#!/usr/bin/env python3
"""
Installation script for temporal-reasoning skill.
Checks dependencies, syncs skill files, provides next steps.

Usage:
    python install.py          # Full install with dependencies
    python install.py --check  # Just check dependencies
    python install.py --force  # Force reinstall even if recent
"""

import sys
import subprocess
import os
import time
from datetime import datetime, timezone

UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".opencode", "skills", "temporal-reasoning")
LAST_UPDATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_update")


def check_python_version():
    """Check Python version is 3.8+."""
    if sys.version_info < (3, 8):
        print(f"ERROR: Python 3.8+ required, found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_minigraf():
    """Check if minigraf CLI is installed, prompt to install if missing."""
    try:
        result = subprocess.run(
            ["minigraf", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            version = result.stdout.strip() or "unknown"
            print(f"✓ minigraf CLI: {version}")
            return True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass

    print("✗ minigraf CLI not found")
    print()
    print("To install minigraf:")
    print("  cargo install minigraf")
    print()
    print("Or see README.md for full installation instructions.")
    return False


def check_tool_import():
    """Verify minigraf_tool can be imported."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("minigraf_tool")
        if spec is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, script_dir)
        import minigraf_tool
        print("✓ minigraf_tool module can be imported")
        return True
    except ImportError as e:
        print(f"✗ Cannot import minigraf_tool: {e}")
        return False


def main():
    print("=" * 50)
    print("Temporal-Reasoning Skill Setup")
    print("=" * 50)
    print()

    checks = [
        ("Python version", check_python_version),
        ("minigraf CLI", check_minigraf),
        ("Module import", check_tool_import),
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
        print("Usage:")
        print("  # As Python module:")
        print("  python -c \"from minigraf_tool import query, transact; print(query('[:find ?e :where [?e :test/name]]'))\"")
        print()
        print("  # As CLI:")
        print("  python minigraf_tool.py query '[:find ?e :where [?e :test/name]]'")
        print("  python minigraf_tool.py transact '[[:test :person/name \\\"Alice\\\"]]'")
        print()
        print("  # Import and use in code:")
        print("  from minigraf_tool import query, transact")
        print("  transact('[[:decision :arch/cache-strategy \\\"Redis\\\"]]', reason='fast in-memory caching')")
        print("  result = query('[:find ?s :where [_ :arch/cache-strategy ?s]]')")
    else:
        print("=" * 50)
        print("✗ Setup incomplete - fix errors above")
        print("=" * 50)
        sys.exit(1)


def should_update():
    """Check if update should run (no more than once a week)."""
    if not os.path.exists(LAST_UPDATE_FILE):
        return True
    
    try:
        with open(LAST_UPDATE_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                return True
            last_update = datetime.fromisoformat(content)
    except (ValueError, IOError):
        return True

    return (datetime.now(timezone.utc) - last_update).total_seconds() > UPDATE_INTERVAL


def update_skill():
    """Pull from GitHub and sync skill files."""
    import shutil
    
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.join(repo_dir, ".opencode", "skills", "temporal-reasoning")
    
    print("Checking for skill updates...")
    
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "master"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            print("Pulling latest from GitHub...")
            os.makedirs(skill_dir, exist_ok=True)
            
            # Sync all skill files
            files_to_sync = [
                "SKILL.md",
            ]
            
            for fname in files_to_sync:
                src = os.path.join(repo_dir, fname)
                dst = os.path.join(skill_dir, fname)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    print(f"✓ Synced {fname}")
            
            # Record update time
            with open(LAST_UPDATE_FILE, 'w') as f:
                f.write(datetime.now(timezone.utc).isoformat())
            
            print("✓ Skill updated!")
            return True
        else:
            print("✓ Skill already up-to-date")
            return False
    except Exception as e:
        print(f"ERROR: Could not update skill: {e}")
        return False


if __name__ == "__main__":
    # Check for updates on first run or if week has passed
    if should_update():
        update_skill()
    
    main()
