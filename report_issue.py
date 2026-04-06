#!/usr/bin/env python3
"""
report_issue.py - Report minigraf errors as GitHub issues.

Provides a tool to file issues when minigraf queries/transacts fail.
Uses GitHub CLI (gh) if available, otherwise falls back to logging.
"""

import subprocess
import sys
from typing import Dict, Optional

VALID_ISSUE_TYPES = ["invalid_query", "transact_failure", "parse_error"]


def _check_gh_available() -> bool:
    """Check if gh CLI is available."""
    try:
        subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            timeout=5
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_repo_info() -> Optional[Dict[str, str]]:
    """Get current repo info using gh."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "owner,name"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            return {
                "owner": data.get("owner", {}).get("login", ""),
                "name": data.get("name", "")
            }
    except Exception:
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
        issue_type: One of "invalid_query", "transact_failure", "parse_error"
        description: Human-readable description of the issue
        datalog: Optional Datalog query or transact that failed
        error: Optional error message from minigraf

    Returns:
        Dict with 'ok', 'method' (gh or log), and 'result'
    """
    if issue_type not in VALID_ISSUE_TYPES:
        return {
            "ok": False,
            "error": f"Invalid issue_type. Must be one of: {VALID_ISSUE_TYPES}"
        }

    body_parts = [f"**Description:** {description}"]
    
    if datalog:
        body_parts.append(f"\n**Datalog:**\n```\n{datalog}\n```")
    
    if error:
        body_parts.append(f"\n**Error:**\n```\n{error}\n```")

    body = "\n".join(body_parts)
    title = f"[minigraf] {issue_type}: {description[:50]}"

    gh_available = _check_gh_available()

    if not gh_available:
        print("=" * 50)
        print("GitHub CLI (gh) not available. Issue not filed.")
        print("=" * 50)
        print(f"Title: {title}")
        print(f"Body:\n{body}")
        print("=" * 50)
        return {
            "ok": True,
            "method": "log",
            "result": "gh not available, logged to stdout"
        }

    repo = _get_repo_info()
    if not repo:
        print("Not in a GitHub repository. Issue not filed.")
        return {
            "ok": True,
            "method": "log",
            "result": "not in github repo, logged to stdout"
        }

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", f"{repo['owner']}/{repo['name']}",
                "--title", title,
                "--body", body
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            issue_url = result.stdout.strip()
            return {
                "ok": True,
                "method": "gh",
                "result": issue_url
            }
        else:
            return {
                "ok": False,
                "error": result.stderr.strip() or "gh issue create failed"
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "gh command timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: report_issue.py <issue_type> <description> [--datalog <datalog>] [--error <error>]")
        print(f"Valid issue types: {VALID_ISSUE_TYPES}")
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
