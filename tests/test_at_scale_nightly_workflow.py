# tests/test_at_scale_nightly_workflow.py
"""#330: no step in the at-scale nightly may name `HEAD` as the ref to walk.

`run_ingestion_benchmark` resolves its ref with `branch or
mcp_server._default_git_branch(repo_path)`. `"HEAD"` is truthy, so passing it
explicitly does not select the checked-out ref *in addition to* the resolution
-- it DEFEATS the resolution entirely, and the literal propagates to
`_git_commits`' range spec, `_run_ingestion`'s `repo_total`, and #317's commit
census alike.

That failure is quiet rather than loud, which is the reason this file exists.
Every downstream consumer inherits the same wrong ref, so the counts stay
internally consistent with each other and no gate in the tier disagrees with
another; they simply all describe whatever happens to be checked out instead of
the branch being tracked. #317 caught the identical shape in `_run_ingestion`
only because the harness resolved `master` while the checkout sat on a feature
branch, making two numbers that should have matched differ.

The nightly currently runs on a checkout of the default branch, where the two
refs denote the same commit, so nothing about a scheduled run is wrong today.
It goes wrong on a `workflow_dispatch` against a branch, a tag, or any detached
checkout -- and the CLI guard in `run_ingestion_benchmark.main()` cannot cover
every shape of it, because the resume-census step passes a commit HASH to
`--branch` and merely RESOLVES the linearization to slice separately. So the
workflow itself is checked here.

WHY THIS SCANS COMMANDS AND NOT THE RAW FILE. Every assertion below is an
ABSENCE check, and the forbidden string is exactly what a warning comment at
the call site has to be able to say out loud in order to be worth having -- the
issue's own suggested fix asks for that note. Scanning the raw text would make
the note indistinguishable from the defect, and the note is the thing that
stops the defect coming back. This is `_has_nil_valued_triple`'s reasoning
(mcp_server.py), which blanks quoted strings before scanning for exactly the
same reason: a note written ABOUT a defect carries the offending text as prose.

Only WHOLE-LINE comments are stripped. A trailing `#` on the same line as a
command is not, deliberately -- stripping those needs a real YAML/shell parse,
and the narrower rule is easy to state: do not put the forbidden spelling in a
trailing comment.
"""
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(
    REPO_ROOT, ".github", "workflows", "at-scale-benchmark-nightly.yml"
)

# The two invocations this file is about. Both are asserted present, so a
# renamed step or a moved script cannot turn the absence checks below into
# assertions about a file that no longer runs anything.
_EXPECTED_INVOCATIONS = (
    "evals.at_scale.run_ingestion_benchmark",
    "evals/at_scale/probe_resume_census.py",
)


def _split_comments(text):
    """Return (commands, comments) -- whole-line comments in the second."""
    commands, comments = [], []
    for line in text.splitlines():
        (comments if line.lstrip().startswith("#") else commands).append(line)
    return "\n".join(commands), "\n".join(comments)


@pytest.fixture(scope="module")
def workflow():
    # POSITIVE CONTROL, not ceremony. A grep that matched nothing reports "all
    # clear" just as confidently as one that matched a clean file, so a
    # missing, moved or emptied workflow has to fail HERE rather than sail
    # through every absence check below.
    assert os.path.exists(WORKFLOW), (
        f"{WORKFLOW} is missing. The absence checks in this file would all "
        f"pass vacuously against a workflow that does not exist -- if the "
        f"nightly moved, re-point this test rather than deleting it."
    )
    with open(WORKFLOW) as f:
        text = f.read()
    for invocation in _EXPECTED_INVOCATIONS:
        assert invocation in text, (
            f"{invocation} is no longer invoked by the nightly. The checks in "
            f"this file only mean something while it is."
        )
    commands, comments = _split_comments(text)
    for invocation in _EXPECTED_INVOCATIONS:
        assert invocation in commands, (
            f"{invocation} survives only inside a comment -- the command-vs-"
            f"comment split has drifted and the absence checks are now "
            f"scanning the wrong half."
        )
    return commands, comments


class TestNightlyNeverNamesHeadAsTheRefToWalk:
    def test_the_ingestion_step_does_not_pass_a_literal_head(self, workflow):
        commands, _ = workflow
        assert "--branch HEAD" not in commands, (
            "The at-scale nightly is passing the literal string HEAD as the "
            "ingestion benchmark's branch argument (#330). That is truthy, so "
            "run_ingestion_benchmark's `branch or _default_git_branch(...)` "
            "never resolves and the literal reaches _git_commits, repo_total "
            "and the commit census alike. Omit --branch entirely and let "
            "_default_git_branch resolve it."
        )

    def test_no_step_resolves_the_ref_with_rev_parse_abbrev_ref(self, workflow):
        commands, _ = workflow
        assert "--abbrev-ref" not in commands, (
            "A step is resolving the ref with `git rev-parse --abbrev-ref "
            "HEAD` (#330). That prints the literal string HEAD on a detached "
            "checkout -- the one value probe_resume_census's --branch help "
            "text forbids -- and prints the FEATURE branch on a "
            "workflow_dispatch, which is #317's scenario verbatim. Resolve "
            "with mcp_server._default_git_branch instead, so the nightly "
            "walks the same ref the code under measurement would."
        )

    def test_the_resume_census_resolves_through_the_shipped_helper(self, workflow):
        # The positive half of the check above: dropping --abbrev-ref by
        # hardcoding a branch name would satisfy it while reintroducing the
        # same class of defect one layer down.
        commands, _ = workflow
        assert "_default_git_branch" in commands, (
            "No step resolves its ref through mcp_server._default_git_branch. "
            "That helper is the shipped resolution ingestion itself uses; a "
            "workflow that hardcodes a branch name instead drifts from it "
            "silently."
        )

    def test_the_forbidden_spelling_is_still_named_in_a_comment(self, workflow):
        # This pins the note the issue's suggested fix asked for, and it
        # doubles as the proof that _split_comments actually separates the two
        # halves: the string is asserted PRESENT here and ABSENT from the
        # commands above, which cannot both hold unless the split works.
        #
        # If this fails because the comment was deleted rather than because a
        # command was added, restore the comment. It is the only part of this
        # change that speaks to whoever next edits that line.
        _, comments = workflow
        assert "--branch HEAD" in comments, (
            "The comment naming `--branch HEAD` as the value the ingestion "
            "step must never receive has been removed from the nightly. It is "
            "load-bearing (#330): it is what a reader adding a branch "
            "argument back sees before they add it."
        )
