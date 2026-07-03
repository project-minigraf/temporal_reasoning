#!/usr/bin/env python3
"""
report_issue.py - Report Minigraf errors as GitHub issues.

Provides a tool to file issues when Minigraf queries/transacts fail.
Uses GitHub CLI (gh) if available, otherwise falls back to logging.

Automatically routes issues to the correct repo based on content analysis:
- minigraf core bugs -> https://github.com/project-minigraf/minigraf
- Minigraf skill bugs -> https://github.com/project-minigraf/temporal_reasoning
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
TEMPORAL_REASONING_REPO = "project-minigraf/temporal_reasoning"

BUG_LABEL_NAME = "bug"
BUG_ISSUE_TYPE_NAME = "Bug"


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


def _get_target_repo(is_minigraf_bug: bool) -> Dict[str, str]:
    """Get the target repo for the issue.

    Always hardcoded (never derived from the caller's cwd via `gh repo view`)
    so skill-level bugs reported from any downstream consumer project still
    land in the skill's own repo, not the consumer's.
    """
    repo = MINIGRAF_REPO if is_minigraf_bug else TEMPORAL_REASONING_REPO
    owner, name = repo.split("/")
    return {"owner": owner, "name": name}


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


def _get_repo_metadata(owner: str, name: str) -> Dict[str, Optional[str]]:
    """Best-effort lookup of the repo's node id, its 'bug' label id, and its
    'Bug' issue type id, via a single GraphQL query.

    Returns an empty dict if the lookup fails or the repo doesn't have these
    (e.g. Issue Types isn't enabled) - callers must treat every key as optional.
    """
    query = """
    query($owner: String!, $name: String!, $label: String!) {
      repository(owner: $owner, name: $name) {
        id
        label(name: $label) { id }
        issueTypes(first: 50) { nodes { id name } }
      }
    }
    """
    try:
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"query={query}",
                "-f", f"owner={owner}",
                "-f", f"name={name}",
                "-f", f"label={BUG_LABEL_NAME}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True
        )
        data = json.loads(result.stdout)
        repo = (data.get("data") or {}).get("repository") or {}
        label = repo.get("label") or {}
        issue_types = ((repo.get("issueTypes") or {}).get("nodes")) or []
        bug_type = next((t for t in issue_types if t.get("name") == BUG_ISSUE_TYPE_NAME), None)
        return {
            "repo_id": repo.get("id"),
            "label_id": label.get("id"),
            "issue_type_id": bug_type["id"] if bug_type else None,
        }
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return {}


def _create_issue_via_graphql(
    repo_id: str,
    title: str,
    body: str,
    label_id: Optional[str],
    issue_type_id: Optional[str]
) -> str:
    """Create the issue via GraphQL so the label and issue type can be set
    atomically at creation time. Returns the created issue's URL."""
    mutation = """
    mutation($repositoryId: ID!, $title: String!, $body: String!, $labelIds: [ID!], $issueTypeId: ID) {
      createIssue(input: {
        repositoryId: $repositoryId, title: $title, body: $body,
        labelIds: $labelIds, issueTypeId: $issueTypeId
      }) {
        issue { url }
      }
    }
    """
    args = [
        "gh", "api", "graphql",
        "-f", f"query={mutation}",
        "-f", f"repositoryId={repo_id}",
        "-f", f"title={title}",
        "-f", f"body={body}",
    ]
    if label_id:
        args += ["-f", f"labelIds[]={label_id}"]
    if issue_type_id:
        args += ["-f", f"issueTypeId={issue_type_id}"]

    result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(result.stdout)
    return (((data.get("data") or {}).get("createIssue") or {}).get("issue") or {}).get("url", "")


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

    # issue_type is a caller-supplied hint, not the routing signal - content
    # analysis alone decides core-vs-skill (see #87).
    is_minigraf_bug = _is_minigraf_related(description, error or "", datalog or "")

    target_repo = _get_target_repo(is_minigraf_bug)
    repo_name = f"{target_repo['owner']}/{target_repo['name']}"

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

    try:
        metadata = _get_repo_metadata(target_repo["owner"], target_repo["name"])

        if metadata.get("repo_id"):
            issue_url = _create_issue_via_graphql(
                metadata["repo_id"], title, body,
                metadata.get("label_id"), metadata.get("issue_type_id"),
            )
        else:
            # Metadata lookup failed (e.g. no network) - fall back to plain
            # creation without label/issue type rather than losing the report.
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
            issue_url = result.stdout.strip()

        return {
            "ok": True,
            "method": "gh",
            "repo": repo_name,
            "result": issue_url
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
