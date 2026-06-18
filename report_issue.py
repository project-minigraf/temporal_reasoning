#!/usr/bin/env python3
"""
report_issue.py - Report Minigraf errors as GitHub issues.

Provides a tool to file issues when Minigraf queries/transacts fail.
Uses GitHub CLI (gh) if available, otherwise falls back to logging.

Automatically routes issues to the correct repo:
- minigraf core bugs -> https://github.com/project-minigraf/minigraf
- Minigraf skill bugs -> current repo
"""

import subprocess
import sys
import logging
import json
from typing import Dict, Optional

logger = logging.getLogger("minigraf.report_issue")
logger.addHandler(logging.NullHandler())

VALID_ISSUE_TYPES = ["invalid_query", "transact_failure", "parse_error", "minigraf_bug"]

MINIGRAF_REPO = "project-minigraf/minigraf"


def _is_minigraf_related(description: str, error: str = "", datalog: str = "") -> bool:
    """Determine if issue is related to minigraf core vs the skill wrapper."""
    combined = f"{description} {error} {datalog}".lower()

    minigraf_indicators = [
        "execution error",
        "parse error",
        "datalog",
        "query engine",
        "transaction",
        "transact",
        "retract",
        "temporal",
        ":where clause",
        "empty result",
        "no results found",
    ]

    wrapper_indicators = [
        "minigraf.py",
        "python wrapper",
        "import error",
        "subprocess",
        "cli wrapper",
    ]

    minigraf_score = sum(1 for ind in minigraf_indicators if ind in combined)
    wrapper_score = sum(1 for ind in wrapper_indicators if ind in combined)

    return minigraf_score > wrapper_score


def _get_target_repo(is_minigraf_bug: bool) -> Optional[Dict[str, str]]:
    """Get the target repo for the issue."""
    if is_minigraf_bug:
        parts = MINIGRAF_REPO.split("/")
        return {"owner": parts[0], "name": parts[1]}

    return _get_current_repo()


def _check_gh_available() -> bool:
    """Check if gh CLI is available."""
    try:
        subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            timeout=5,
            check=True
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False


def _get_current_repo() -> Optional[Dict[str, str]]:
    """Get current repo info using gh."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "owner,name"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "owner": data.get("owner", {}).get("login", ""),
                "name": data.get("name", "")
            }
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None



def report_issue(
    issue_type: str,
    description: str,
    datalog: Optional[str] = None,
    error: Optional[str] = None
) -> Dict:
    """
    Report an issue with minigraf operations.

    Args:
        issue_type: One of "invalid_query", "transact_failure", "parse_error", "minigraf_bug"
        description: Human-readable description of the issue
        datalog: Optional Datalog query or transact that failed
        error: Optional error message from minigraf

    Returns:
        Dict with 'ok', 'method' (gh or log), 'repo', and 'result'
    """
    if issue_type not in VALID_ISSUE_TYPES:
        return {
            "ok": False,
            "error": f"Invalid issue_type. Must be one of: {VALID_ISSUE_TYPES}"
        }

    is_minigraf_bug = _is_minigraf_related(description, error or "", datalog or "")
    if issue_type == "minigraf_bug":
        is_minigraf_bug = True

    target_repo = _get_target_repo(is_minigraf_bug)
    if target_repo:
        repo_name = f"{target_repo['owner']}/{target_repo['name']}"
    else:
        repo_name = "unknown"

    body_parts = [f"**Description:** {description}"]

    if datalog:
        body_parts.append(f"\n**Datalog:**\n```\n{datalog}\n```")

    if error:
        body_parts.append(f"\n**Error:**\n```\n{error}\n```")

    if is_minigraf_bug:
        body_parts.append(f"\n*Auto-routed to minigraf repo based on content*")

    body = "\n".join(body_parts)
    title = f"[minigraf] {issue_type}: {description[:50]}"

    gh_available = _check_gh_available()

    if not gh_available:
        logger.warning(
            "GitHub CLI (gh) not available. Issue not filed.\n"
            "Target repo: %s\nTitle: %s\nBody:\n%s",
            repo_name, title, body
        )
        return {
            "ok": True,
            "method": "log",
            "repo": repo_name,
            "result": "gh not available, logged"
        }

    if not target_repo:
        logger.warning("Not in a GitHub repository. Issue not filed.")
        return {
            "ok": True,
            "method": "log",
            "repo": "unknown",
            "result": "not in github repo, logged to stdout"
        }

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", f"{target_repo['owner']}/{target_repo['name']}",
                "--title", title,
                "--body", body
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True
        )

        if result.returncode == 0:
            issue_url = result.stdout.strip()
            return {
                "ok": True,
                "method": "gh",
                "repo": repo_name,
                "result": issue_url
            }
        else:
            return {
                "ok": False,
                "error": result.stderr.strip() or "gh issue create failed"
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "gh command timed out"}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": e.stderr.strip() if e.stderr else "gh command failed"}
    except FileNotFoundError:
        return {"ok": False, "error": "gh not found"}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: report_issue.py <type> <desc> [--datalog X] [--error Y]")
        print(f"Valid types: {VALID_ISSUE_TYPES}")
        sys.exit(1)

    issue_type = sys.argv[1]
    description = sys.argv[2]
    datalog = None
    error = None

    if "--datalog" in sys.argv:
        idx = sys.argv.index("--datalog")
        if idx + 1 < len(sys.argv):
            datalog = sys.argv[idx + 1]

    if "--error" in sys.argv:
        idx = sys.argv.index("--error")
        if idx + 1 < len(sys.argv):
            error = sys.argv[idx + 1]

    result = report_issue(issue_type, description, datalog, error)
    print(result)
