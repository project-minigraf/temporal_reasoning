import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import report_issue
from report_issue import (
    BUG_ISSUE_TYPE_NAME,
    BUG_LABEL_NAME,
    MINIGRAF_REPO,
    TEMPORAL_REASONING_REPO,
    _get_target_repo,
    _is_minigraf_related,
    report_issue as report_issue_fn,
)


class TestIsMinigrafRelated:
    def test_datalog_error_content_is_minigraf(self):
        assert _is_minigraf_related("query fails", "parse error", ":where clause") is True

    def test_wrapper_error_content_is_not_minigraf(self):
        assert _is_minigraf_related(
            "subprocess crashed calling minigraf.py wrapper", "import error", ""
        ) is False


class TestGetTargetRepo:
    def test_minigraf_bug_routes_to_minigraf_repo(self):
        assert _get_target_repo(True) == {"owner": "project-minigraf", "name": "minigraf"}

    def test_skill_bug_routes_to_temporal_reasoning_repo(self):
        assert _get_target_repo(False) == {
            "owner": "project-minigraf",
            "name": "temporal_reasoning",
        }


class TestReportIssueRouting:
    """Regression tests for #87: issue_type=="minigraf_bug" must not short-circuit
    content-based routing, and routing must never depend on the caller's cwd."""

    def test_minigraf_bug_type_with_wrapper_content_routes_to_skill_repo(self):
        with patch("report_issue._check_gh_available", return_value=True), \
             patch("report_issue._get_repo_metadata", return_value={}), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/project-minigraf/temporal_reasoning/issues/1",
            )
            result = report_issue_fn(
                issue_type="minigraf_bug",
                description="subprocess crashed calling minigraf.py wrapper, import error",
            )
        assert result["repo"] == TEMPORAL_REASONING_REPO

    def test_minigraf_bug_type_with_core_content_routes_to_minigraf_repo(self):
        with patch("report_issue._check_gh_available", return_value=True), \
             patch("report_issue._get_repo_metadata", return_value={}), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="https://github.com/project-minigraf/minigraf/issues/1"
            )
            result = report_issue_fn(
                issue_type="minigraf_bug",
                description="datalog execution error in query engine transaction",
            )
        assert result["repo"] == MINIGRAF_REPO

    def test_non_minigraf_bug_type_never_shells_out_to_gh_repo_view(self):
        with patch("report_issue._check_gh_available", return_value=True), \
             patch("report_issue._get_repo_metadata", return_value={}), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/x/y/issues/1")
            report_issue_fn(issue_type="invalid_query", description="bad query")
        calls = [call.args[0] for call in mock_run.call_args_list]
        assert not any(call[:3] == ["gh", "repo", "view"] for call in calls)


class TestIssueLabelingAndType:
    def test_creates_issue_with_label_and_type_via_graphql_when_available(self):
        metadata = {"repo_id": "R_1", "label_id": "L_1", "issue_type_id": "IT_1"}
        graphql_response = json.dumps(
            {"data": {"createIssue": {"issue": {"url": "https://github.com/x/y/issues/2"}}}}
        )
        with patch("report_issue._check_gh_available", return_value=True), \
             patch("report_issue._get_repo_metadata", return_value=metadata), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=graphql_response)
            result = report_issue_fn(issue_type="invalid_query", description="bad query")

        assert result["ok"] is True
        assert result["result"] == "https://github.com/x/y/issues/2"

        args = mock_run.call_args.args[0]
        assert args[:3] == ["gh", "api", "graphql"]
        joined = " ".join(args)
        assert "labelIds[]=L_1" in joined
        assert "issueTypeId=IT_1" in joined

    def test_falls_back_to_plain_issue_create_when_metadata_unavailable(self):
        with patch("report_issue._check_gh_available", return_value=True), \
             patch("report_issue._get_repo_metadata", return_value={}), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="https://github.com/x/y/issues/3"
            )
            result = report_issue_fn(issue_type="invalid_query", description="bad query")

        assert result["ok"] is True
        args = mock_run.call_args.args[0]
        assert args[:3] == ["gh", "issue", "create"]

    def test_repo_metadata_requests_bug_label_and_picks_bug_issue_type(self):
        response = json.dumps({
            "data": {
                "repository": {
                    "id": "R_1",
                    "label": {"id": "L_1"},
                    "issueTypes": {
                        "nodes": [
                            {"id": "IT_task", "name": "Task"},
                            {"id": "IT_bug", "name": BUG_ISSUE_TYPE_NAME},
                        ]
                    },
                }
            }
        })
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=response)
            metadata = report_issue._get_repo_metadata("project-minigraf", "temporal_reasoning")

        args = mock_run.call_args.args[0]
        assert f"label={BUG_LABEL_NAME}" in " ".join(args)
        assert metadata == {"repo_id": "R_1", "label_id": "L_1", "issue_type_id": "IT_bug"}
