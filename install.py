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
from datetime import datetime, timezone

UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(REPO_DIR, ".last_update")

FILES_TO_SYNC = ["SKILL.md", "minigraf_tool.py", "skill.json"]
DIRS_TO_SYNC = ["tools"]
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "temporal_reasoning"),
    os.path.join("skills", "temporal-reasoning"),
]


def _get_target_dir() -> str:
    """Return install target: --target arg if provided, else cwd."""
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[idx + 1])
    return os.getcwd()


def check_python_version():
    """Check Python version is 3.8+."""
    if sys.version_info < (3, 8):
        print(f"ERROR: Python 3.8+ required, "
              f"found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_minigraf():
    """Check if minigraf CLI is installed, prompt to install if missing."""
    try:
        subprocess.run(
            ["minigraf"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=True
        )
        print("✓ minigraf CLI: found")
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
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
        msg = "from minigraf_tool import query, transact; "
        msg += "print(query('[:find ?e :where [?e :test/name]]'))"
        print(f"  python -c \"{msg}\"")
        print()
        print("  # As CLI:")
        print("  python minigraf_tool.py query '[:find ?e :where [?e :test/name]]'")
        print("  python minigraf_tool.py transact '[[:test :person/name \\\"Alice\\\"]]'")
        print()
        print("  # Import and use in code:")
        print("  from minigraf_tool import query, transact")
        tx_msg = "transact('[[:decision :arch/cache-strategy \"Redis\"]]', "
        tx_msg += "reason='fast in-memory caching')"
        print(f"  {tx_msg}")
        q_msg = "result = query('[:find ?s :where [_ :arch/cache-strategy ?s]]')"
        print(f"  {q_msg}")
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
    except ValueError:
        # Legacy float epoch or corrupt file — treat as expired and let the
        # next successful update_skill() write a fresh ISO 8601 timestamp.
        return True
    except IOError:
        return True

    return (datetime.now(timezone.utc) - last_update).total_seconds() > UPDATE_INTERVAL


def _write_last_update() -> None:
    """Write the current UTC time as ISO 8601 to the last-update file."""
    with open(LAST_UPDATE_FILE, 'w') as f:
        f.write(datetime.now(timezone.utc).isoformat())


def _sync_files(target_dir: str) -> None:
    """Copy skill files and directories into each agent skill folder under target_dir."""
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
            check=True
        )
        # Always record the check time so the weekly throttle resets,
        # regardless of whether git pull fetched new commits.
        _write_last_update()

        if result.stdout.strip() and "Already up to date" not in result.stdout:
            print("Pulling latest from GitHub...")
            _sync_files(target_dir)
            print("✓ Skill updated!")
        else:
            _sync_files(target_dir)
            print("✓ Skill already up-to-date")
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


if __name__ == "__main__":
    target_dir = _get_target_dir()
    force = "--force" in sys.argv
    if target_dir != REPO_DIR:
        print(f"Installing into: {target_dir}")

    # Pull from GitHub when forced or when weekly interval has elapsed
    if force or should_update():
        update_skill(target_dir)
    else:
        # Still sync files even if we skip git pull (e.g. fresh project install)
        _sync_files(target_dir)

    main()
