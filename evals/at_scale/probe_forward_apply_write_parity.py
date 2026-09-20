"""#346 PR 2: differential write-sequence oracle for the _forward_apply refactor.

Records every _db_execute command issued during an ingestion run, tagged with
the commit being applied, and compares two recordings command-for-command.

The refactor #346 PR 2 performs is PURE: it writes no new facts, changes no
fact shape and bumps no GRAPH_FORMAT_VERSION. Its acceptance criterion is
therefore not "the suite is green" and not "the graphs look equivalent" but
"ingestion issued the identical sequence of database commands". This file is
the instrument that decides that, and it lives in the repo rather than in a
scratchpad for the same reason probe_lease_drop_cost.py does (#281): the
numbers in the task reports came from here.

CAVEATS, all load-bearing:

* PYTHONHASHSEED MUST be 0 in every arm. Several ingestion sites iterate sets
  and dicts of strings (current_deps - previous_deps, submodule_paths,
  renamed_old_paths), so string-hash randomization alone reorders emitted
  triples between two runs of IDENTICAL code. Unpinned, this probe reports
  differences that are not real -- and worse, makes a real difference look
  like ordinary noise. record_run() refuses to run when it is unset or
  non-zero. It is REFUSED rather than set here, because PYTHONHASHSEED is
  read by the interpreter at startup: assigning it to os.environ from inside
  a running process changes nothing at all, so a probe that "fixed it up"
  would be silently unpinned while reporting that it was pinned. It must come
  from the command line.
* Comparison is PER COMMIT, never one global list: the two streams interleave
  through executors, so global order is not stable run to run.
* Two empty recordings are NOT clean -- see compare()'s proved_nothing key.
* Absolute wall-clock numbers are NOT comparable across invocation batches
  (precedent: probe_sweep_window_cost.py, where a baseline drifted 2.4x
  between rounds with no code change). Do not read a timing delta here as a
  finding.

WHAT IS RECORDED, AND WHAT IS NOT. Only commands issued while a
_forward_apply / _reverse_apply call is on the recording thread's stack are
written to the JSONL. Everything else an ingestion run does -- the preload
lease, _frontier_load, the correction sweep's own reads, the status handler --
is counted into the returned `untagged_commands` and then dropped, because
those run on the event-loop thread interleaved with executor work and their
global order is not stable run to run. This is a deliberate scope choice, not
an oversight, and it is sound for THIS refactor for a reason worth stating:
a write that the refactor accidentally moved OUT of _forward_apply into its
caller vanishes from that commit's list and is caught as a shortened list,
and one moved IN appears as an addition. Only an untagged-to-untagged change
is invisible, and no such code is in scope.

_db_checkpoint_gated CALLS are recorded too, as the synthetic command
"(checkpoint-gated)". Flag site 6 of `lifecycle_only` is exactly
`if not lifecycle_only: _db_checkpoint_gated(db)`, so an oracle blind to it
could not see that site change. What is recorded is the CALL, never the
outcome: `_db_checkpoint` itself is deliberately NOT wrapped, because
_CheckpointPolicy gates it on a WALL-CLOCK duty budget
(MINIGRAF_INGEST_CHECKPOINT_DUTY, #241) and whether any given call actually
checkpoints therefore varies run to run on identical code. Measured while
building this probe: recording `_db_checkpoint` made the master-against-itself
control report a difference at one commit (25 commands vs 26, the extra one a
checkpoint) purely from that gate.

Usage:

    S=/path/to/scratchpad
    PYTHONHASHSEED=0 .venv/bin/python \\
        evals/at_scale/probe_forward_apply_write_parity.py \\
        --synthetic --graph $S/a.graph --ratio 1:1 --out $S/a.jsonl
    PYTHONHASHSEED=0 .venv/bin/python \\
        evals/at_scale/probe_forward_apply_write_parity.py \\
        --repo . --truncate-by 300 --graph $S/b.graph --ratio forward-only \\
        --out $S/b.jsonl
    .venv/bin/python evals/at_scale/probe_forward_apply_write_parity.py \\
        --compare $S/a.jsonl $S/b.jsonl

Everything runs under a `__main__` guard, and that is load-bearing rather
than style: extraction uses a SPAWN-context ProcessPoolExecutor, so each
worker re-imports this module. With the body at module level the child re-runs
the repo construction, dies, and takes the pool with it as a BrokenProcessPool
that looks like an ingestion failure.
"""
import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional


# ---------------------------------------------------------------- comparison


def _load(path) -> List[dict]:
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _group(rows: List[dict]) -> Dict[str, List[str]]:
    """Commit hash -> its commands, in issue order.

    A hash can legitimately be applied more than once in one run (Stage A's
    forward or reverse pass, then Stage B's lifecycle pass), and those windows
    concatenate here. That is deterministic -- Stage A completes before Stage
    B -- so it needs no occurrence index.
    """
    grouped: Dict[str, List[str]] = {}
    for row in rows:
        grouped.setdefault(row["commit"], []).append(row["cmd"])
    return grouped


def compare(path_a, path_b) -> dict:
    """Compare two recordings command-for-command, per commit.

    `proved_nothing` is True when EITHER file has zero rows, and `ok` is then
    False. A comparator that reads two empty files as clean fails OPEN: a
    probe that recorded nothing at all would certify the refactor. One empty
    side is treated the same way rather than as a huge real difference,
    because an arm that crashed before its first write is a fact about the
    RUN, not a finding about the code under test.
    """
    rows_a, rows_b = _load(path_a), _load(path_b)
    a, b = _group(rows_a), _group(rows_b)

    proved_nothing = not rows_a or not rows_b

    only_a = [c for c in a if c not in b]
    only_b = [c for c in b if c not in a]

    differing = []
    for commit, cmds_a in a.items():
        cmds_b = b.get(commit)
        if cmds_b is None or cmds_a == cmds_b:
            continue
        idx = None
        for i in range(max(len(cmds_a), len(cmds_b))):
            va = cmds_a[i] if i < len(cmds_a) else None
            vb = cmds_b[i] if i < len(cmds_b) else None
            if va != vb:
                idx = i
                break
        differing.append({
            "commit": commit,
            "first_divergence_index": idx,
            "a": cmds_a[idx] if idx is not None and idx < len(cmds_a) else None,
            "b": cmds_b[idx] if idx is not None and idx < len(cmds_b) else None,
            "len_a": len(cmds_a),
            "len_b": len(cmds_b),
        })

    ok = (not proved_nothing) and not only_a and not only_b and not differing
    return {
        "ok": ok,
        "proved_nothing": proved_nothing,
        "commands_a": len(rows_a),
        "commands_b": len(rows_b),
        "commits_a": len(a),
        "commits_b": len(b),
        "commits_only_in_a": only_a,
        "commits_only_in_b": only_b,
        "differing_commits": differing,
    }


# ----------------------------------------------------------------- recording


_FORWARD_ONLY_RATIO = f"{10**6}:1"


def _resolve_ratio(ratio: str) -> str:
    """'forward-only' is spelled 1000000:1 everywhere in the suite (see
    tests/test_mcp_server.py); _parse_stream_ratio rejects a zero side."""
    return _FORWARD_ONLY_RATIO if ratio == "forward-only" else ratio


def record_run(repo_path, graph_path, ratio, out_path, branch: Optional[str] = None) -> dict:
    """One real _run_ingestion, with every applied command written to out_path.

    Returns {"commands", "commits", "untagged_commands", "elapsed_s",
    "status", "graph_commits"}.
    """
    # FIRST statement, before any work: an unpinned arm is not a slow arm, it
    # is a wrong one, and every later step would be reasoning about noise.
    seed = os.environ.get("PYTHONHASHSEED")
    if seed != "0":
        raise RuntimeError(
            f"PYTHONHASHSEED must be 0 for this probe (got {seed!r}). Several "
            "ingestion sites iterate sets of strings, so hash randomization "
            "reorders emitted triples between two runs of IDENTICAL code. Set "
            "it on the command line -- assigning it here would not take "
            "effect, the interpreter reads it at startup."
        )

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    # Scrubbed for the same reason tests/conftest.py scrubs them: several of
    # these are read into module constants at import, so an ambient one from
    # the developer's shell silently changes what is recorded. Done BEFORE
    # mcp_server is imported, and before this run's own variables are set.
    for key in [k for k in os.environ if k.startswith("MINIGRAF_")]:
        del os.environ[key]
    os.environ["MINIGRAF_GRAPH_PATH"] = str(graph_path)
    os.environ["MINIGRAF_INGEST_STREAM_RATIO"] = _resolve_ratio(ratio)

    import mcp_server

    if branch is None:
        branch = mcp_server._default_git_branch(str(repo_path))

    real_db_execute = mcp_server._db_execute
    real_checkpoint_gated = mcp_server._db_checkpoint_gated
    real_forward = mcp_server._forward_apply
    real_reverse = mcp_server._reverse_apply

    # Thread-local, not a plain global: the apply functions run on
    # write_executor and issue every one of their own commands from that same
    # thread synchronously, so a thread-local tags exactly their commands. A
    # plain global would additionally MIS-tag anything the event-loop thread
    # happened to execute while an apply was in flight.
    tag = threading.local()
    counts = {"commands": 0, "untagged": 0}
    fh = open(out_path, "w")

    def _record(cmd: str) -> None:
        commit = getattr(tag, "commit", None)
        if commit is None:
            counts["untagged"] += 1
            return
        fh.write(json.dumps({"commit": commit, "cmd": cmd}) + "\n")
        counts["commands"] += 1

    def spy_db_execute(db, datalog):
        _record(datalog)
        return real_db_execute(db, datalog)

    def spy_checkpoint_gated(db):
        _record("(checkpoint-gated)")
        return real_checkpoint_gated(db)

    # *args/**kwargs with positional indexing only: every defaulted parameter
    # of both functions is keyword-only as of PR #360, so a wrapper sees
    # exactly 5 (forward) or 6 (reverse) positionals and the commit hash sits
    # at a fixed index that no future keyword parameter can shift.
    def spy_forward(*args, **kwargs):
        previous = getattr(tag, "commit", None)
        tag.commit = args[3][0]          # commit_metadata[pos][0]
        try:
            return real_forward(*args, **kwargs)
        finally:
            tag.commit = previous

    def spy_reverse(*args, **kwargs):
        previous = getattr(tag, "commit", None)
        tag.commit = args[2][args[4]]    # linearization[pos]
        try:
            return real_reverse(*args, **kwargs)
        finally:
            tag.commit = previous

    mcp_server._db_execute = spy_db_execute
    mcp_server._db_checkpoint_gated = spy_checkpoint_gated
    mcp_server._forward_apply = spy_forward
    mcp_server._reverse_apply = spy_reverse

    mcp_server._reset_db_state()
    mcp_server._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
        "error": None, "owner_pid": None, "error_at": None, "phase": None,
    }

    started = time.monotonic()
    try:
        asyncio.run(mcp_server._run_ingestion(str(repo_path), branch))
    finally:
        elapsed = time.monotonic() - started
        mcp_server._db_execute = real_db_execute
        mcp_server._db_checkpoint_gated = real_checkpoint_gated
        mcp_server._forward_apply = real_forward
        mcp_server._reverse_apply = real_reverse
        fh.close()
        mcp_server._reset_db_state()

    status = mcp_server.handle_minigraf_ingest_status()
    with mcp_server.db_lease() as db:
        graph_commits = mcp_server._count_commit_entities(db)
    mcp_server._reset_db_state()

    return {
        "commands": counts["commands"],
        # Recomputed from the file rather than from a counter, so a truncated
        # or unflushed write is visible instead of being reported as recorded.
        "commits": len(_group(_load(out_path))),
        "untagged_commands": counts["untagged"],
        "elapsed_s": round(elapsed, 1),
        "status": status["status"],
        "graph_commits": graph_commits,
    }


# ------------------------------------------------------------- synthetic repo


# `-c`, not a per-repo `git config`: the submodule clone runs as a CHILD git
# process with its cwd inside the new submodule directory, so the
# superproject's local config does not reach it (measured -- `git config
# --get protocol.file.allow` read `always` in the superproject and the clone
# still died with "transport 'file' not allowed"). A `-c` on the command line
# propagates through GIT_CONFIG_PARAMETERS and does.
_GIT_PREAMBLE = ["-c", "protocol.file.allow=always"]


def _git(repo, *args, **kwargs):
    return subprocess.run(["git", *_GIT_PREAMBLE, *args], cwd=repo, check=True,
                          capture_output=True, text=True, **kwargs)


def build_synthetic_repo(dest) -> pathlib.Path:
    """A git repo exercising what this repo's own history cannot.

    MEASURED while drafting the design spec: this repo has NO submodules at
    all and only TWO rename commits in its entire history, so corpus 1 cannot
    cover the gitlink branches of `_forward_apply` or its unmatched-rename-
    child pass. Everything below is therefore the only coverage those paths
    get:

      * a gitlink ADD, BUMP and REMOVE (via a second local repo);
      * a file rename whose children are NOT all matched by the rename
        matcher (one function survives under a new name, one is dropped);
      * an in-file function rename (body preserved, so the matcher matches);
      * a delete-then-re-add of the same path;
      * dependency churn, including an import that never resolves (an
        external-dependency stub) and one that later does.

    `dest` gets `repo/` (the superproject, returned) and `sub/` beside it.
    Deterministic: every commit's author and committer dates are pinned, so
    two invocations produce identical commit hashes -- which is what lets the
    two arms of a comparison be separate processes.
    """
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    sub = dest / "sub"
    repo = dest / "repo"

    clock = {"n": 0}

    def commit(where, message):
        clock["n"] += 1
        stamp = f"2021-01-01T{clock['n'] // 60:02d}:{clock['n'] % 60:02d}:00"
        env = dict(os.environ,
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp,
                   GIT_AUTHOR_NAME="T", GIT_COMMITTER_NAME="T",
                   GIT_AUTHOR_EMAIL="t@t.com", GIT_COMMITTER_EMAIL="t@t.com")
        _git(where, "commit", "-q", "-m", message, env=env)

    def init(where):
        where.mkdir(parents=True, exist_ok=True)
        _git(where, "init", "-q", "-b", "master")
        _git(where, "config", "user.email", "t@t.com")
        _git(where, "config", "user.name", "T")

    # --- the submodule's own repo -------------------------------------
    init(sub)
    (sub / "s.py").write_text("def sub_one():\n    return 1\n")
    _git(sub, "add", ".")
    commit(sub, "sub c0")

    # --- c1: root commit ----------------------------------------------
    init(repo)
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(
        "import os\n"
        "\n"
        "ALPHA_LIMIT = 10\n"
        "\n"
        "def alpha(x):\n"
        "    return x + ALPHA_LIMIT\n"
        "\n"
        "def beta(x):\n"
        "    return alpha(x) * 2\n"
        "\n"
        "class Engine:\n"
        "    speed = 3\n"
        "\n"
        "    def __init__(self):\n"
        "        self.gear = 1\n"
        "\n"
        "    def run(self):\n"
        "        return self.gear\n"
    )
    (pkg / "util.py").write_text(
        "def helper_kept(a, b):\n"
        "    total = a + b\n"
        "    for _ in range(3):\n"
        "        total += 1\n"
        "    return total\n"
        "\n"
        "def helper_dropped(a):\n"
        "    return a - 1\n"
        "\n"
        "def helper_stable(a):\n"
        "    return a\n"
    )
    _git(repo, "add", ".")
    commit(repo, "c1 initial package")

    # --- c2: plain M, adds a function and an unresolvable import -------
    (pkg / "core.py").write_text(
        "import os\n"
        "import requests\n"          # never resolves -> external-dependency stub
        "from pkg import util\n"     # resolves -> real :depends-on edge
        "\n"
        "ALPHA_LIMIT = 10\n"
        "\n"
        "def alpha(x):\n"
        "    return x + ALPHA_LIMIT\n"
        "\n"
        "def beta(x):\n"
        "    return alpha(x) * 2\n"
        "\n"
        "def gamma(x):\n"
        "    return util.helper_kept(x, x)\n"
        "\n"
        "class Engine:\n"
        "    speed = 3\n"
        "\n"
        "    def __init__(self):\n"
        "        self.gear = 1\n"
        "\n"
        "    def run(self):\n"
        "        return self.gear\n"
    )
    _git(repo, "add", ".")
    commit(repo, "c2 add gamma and imports")

    # --- c3: gitlink ADD ----------------------------------------------
    _git(repo, "submodule", "add", "-q", str(sub), "vendor/lib")
    # `git submodule add` writes the ABSOLUTE source path into .gitmodules,
    # so two invocations under different workdirs produce different blobs and
    # every commit from here on gets a different hash -- which the comparator
    # then reports as 10 commits present in one arm and absent from the other.
    # Measured exactly that before this rewrite. The committed url is made
    # relative; the clone's own remote (in .git/modules/..., never committed)
    # keeps the real path, so the c4 fetch still works.
    (repo / ".gitmodules").write_text(
        '[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = ../sub\n'
    )
    _git(repo, "add", ".")
    commit(repo, "c3 add submodule")

    # --- c4: gitlink BUMP ---------------------------------------------
    (sub / "s.py").write_text("def sub_one():\n    return 2\n")
    _git(sub, "add", ".")
    commit(sub, "sub c1")
    new_sha = _git(sub, "rev-parse", "HEAD").stdout.strip()
    vendored = repo / "vendor" / "lib"
    _git(vendored, "fetch", "-q", "origin")
    _git(vendored, "checkout", "-q", new_sha)
    _git(repo, "add", "vendor/lib")
    commit(repo, "c4 bump submodule")

    # --- c5: in-file function rename (body preserved) ------------------
    (pkg / "core.py").write_text(
        "import os\n"
        "import requests\n"
        "from pkg import util\n"
        "\n"
        "ALPHA_LIMIT = 10\n"
        "\n"
        "def alpha_renamed(x):\n"     # renamed, identical body
        "    return x + ALPHA_LIMIT\n"
        "\n"
        "def beta(x):\n"
        "    return alpha_renamed(x) * 2\n"
        "\n"
        "def gamma(x):\n"
        "    return util.helper_kept(x, x)\n"
        "\n"
        "class Engine:\n"
        "    speed = 3\n"
        "\n"
        "    def __init__(self):\n"
        "        self.gear = 1\n"
        "\n"
        "    def run(self):\n"
        "        return self.gear\n"
    )
    _git(repo, "add", ".")
    commit(repo, "c5 rename alpha in place")

    # --- c6: file rename with an UNMATCHED child -----------------------
    # helper_kept keeps its body under a new name (the matcher should pair
    # it), helper_dropped disappears entirely (nothing to pair it with), and
    # helper_stable is unchanged. The dropped one is what reaches
    # _forward_apply's renamed_old_paths pass.
    _git(repo, "mv", "pkg/util.py", "pkg/tools.py")
    (pkg / "tools.py").write_text(
        "def helper_renamed(a, b):\n"
        "    total = a + b\n"
        "    for _ in range(3):\n"
        "        total += 1\n"
        "    return total\n"
        "\n"
        "def helper_stable(a):\n"
        "    return a\n"
    )
    (pkg / "core.py").write_text(
        "import os\n"
        "import requests\n"
        "from pkg import tools\n"
        "\n"
        "ALPHA_LIMIT = 10\n"
        "\n"
        "def alpha_renamed(x):\n"
        "    return x + ALPHA_LIMIT\n"
        "\n"
        "def beta(x):\n"
        "    return alpha_renamed(x) * 2\n"
        "\n"
        "def gamma(x):\n"
        "    return tools.helper_renamed(x, x)\n"
        "\n"
        "class Engine:\n"
        "    speed = 3\n"
        "\n"
        "    def __init__(self):\n"
        "        self.gear = 1\n"
        "\n"
        "    def run(self):\n"
        "        return self.gear\n"
    )
    _git(repo, "add", "-A")
    commit(repo, "c6 rename util to tools, drop one child")

    # --- c7: plain D, plus M removing a class child --------------------
    _git(repo, "rm", "-q", "pkg/tools.py")
    (pkg / "core.py").write_text(
        "import os\n"
        "\n"
        "ALPHA_LIMIT = 10\n"
        "\n"
        "def alpha_renamed(x):\n"
        "    return x + ALPHA_LIMIT\n"
        "\n"
        "class Engine:\n"
        "    speed = 3\n"
        "\n"
        "    def __init__(self):\n"
        "        self.gear = 1\n"
    )
    _git(repo, "add", "-A")
    commit(repo, "c7 delete tools, drop beta/gamma/run")

    # --- c8: re-add the SAME path --------------------------------------
    (pkg / "tools.py").write_text(
        "def helper_renamed(a, b):\n"
        "    return a + b\n"
        "\n"
        "def helper_new(a):\n"
        "    return a * 3\n"
    )
    _git(repo, "add", "-A")
    commit(repo, "c8 re-add tools at the same path")

    # --- c9: gitlink REMOVE --------------------------------------------
    _git(repo, "rm", "-q", "-r", "vendor/lib")
    _git(repo, "add", "-A")
    commit(repo, "c9 remove submodule")

    # --- c10: dependency churn, resolving the previously-unresolved ----
    (pkg / "requests.py").write_text("def get(url):\n    return url\n")
    (pkg / "core.py").write_text(
        "from pkg import requests\n"
        "from pkg import tools\n"
        "\n"
        "ALPHA_LIMIT = 11\n"
        "\n"
        "def alpha_renamed(x):\n"
        "    return x + ALPHA_LIMIT\n"
        "\n"
        "class Engine:\n"
        "    speed = 4\n"
        "\n"
        "    def __init__(self):\n"
        "        self.gear = 2\n"
    )
    _git(repo, "add", "-A")
    commit(repo, "c10 dependency churn")

    # --- c11: a merge, so the linearization is not purely linear -------
    _git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    (pkg / "side.py").write_text("def only_on_side():\n    return 7\n")
    _git(repo, "add", "-A")
    commit(repo, "c11 side branch file")
    _git(repo, "checkout", "-q", "master")
    env = dict(os.environ,
               GIT_AUTHOR_DATE="2021-01-01T00:20:00", GIT_COMMITTER_DATE="2021-01-01T00:20:00",
               GIT_AUTHOR_NAME="T", GIT_COMMITTER_NAME="T",
               GIT_AUTHOR_EMAIL="t@t.com", GIT_COMMITTER_EMAIL="t@t.com")
    _git(repo, "merge", "-q", "--no-ff", "-m", "c12 merge side", "side", env=env)

    return repo


def truncate_repo(source, dest, keep: int, branch: str) -> pathlib.Path:
    """A clone of `source` whose history holds (about) the most recent `keep`
    commits, and nothing older.

    A CLONE, never an operation on `source`: this probe is routinely pointed
    at the working repo and must not be able to touch it.

    NOT `git clone --depth N`, which was tried first and is wrong for this:
    DEPTH is not a commit count once the history has merges, because each
    merge widens the frontier. Measured on this repo, `--depth 40` produced a
    185-commit history -- 4.6x the number asked for, and unpredictably so.

    Instead a cut commit C is binary-searched along the first-parent chain
    for the smallest `C..branch` range holding at least `keep` commits, and
    the clone's boundary commits are re-parented with `git replace --graft`
    so nothing older is reachable. `C..branch` is reachability-closed by
    construction (an ancestor of C cannot be in it, so no path from the tip
    to an in-range commit leaves the range), which is what a
    `git rev-list --topo-order | head -N` set is NOT -- that set truncates to
    16 of 40 commits when grafted, because most of it is only reachable
    through commits the head cut away.

    `git replace --graft`, not `.git/shallow`, and the difference is not
    cosmetic: a shallow entry makes a commit PARENTLESS, all parents at once.
    A merge on the boundary has one parent inside the range and one outside,
    so shallow-grafting it severs the in-range side too -- measured, 299 of
    303 commits survived. `--graft` names the parents to keep, so the
    in-range side stays. Verified on the result: one root, and
    `git diff-tree --root` on it lists the whole tree, which is what
    ingestion's first commit needs.

    The range can overshoot `keep` (a merge brings in a whole side branch at
    once), so the ACTUAL count is what the caller reports; it is not
    silently assumed to be `keep`.
    """
    dest = pathlib.Path(dest)
    source = pathlib.Path(source).resolve()

    # Reuse, so the two arms of one comparison walk the SAME truncated
    # history rather than two independently-cut ones. Keyed on the exact
    # (source, branch, keep) triple: a marker that only said "a clone lives
    # here" would silently hand back a differently-cut history and the
    # comparator would report every commit as one-sided.
    marker = dest / ".truncation"
    key = f"{source}\n{branch}\n{keep}\n"
    if marker.exists() and marker.read_text() == key:
        return dest
    if dest.exists():
        raise RuntimeError(
            f"{dest} already exists and was not cut as ({source}, {branch}, "
            f"{keep}); remove it or pass a different --workdir"
        )

    def rev(*args, cwd=source):
        return subprocess.run(["git", *_GIT_PREAMBLE, *args], cwd=cwd,
                              check=True, capture_output=True, text=True).stdout

    subprocess.run(
        ["git", *_GIT_PREAMBLE, "clone", "--quiet", "--local", "--no-hardlinks",
         "--no-tags", "--branch", branch, str(source), str(dest)],
        check=True, capture_output=True, text=True,
    )

    chain = rev("rev-list", "--first-parent", branch, cwd=dest).split()
    total = len(rev("rev-list", branch, cwd=dest).split())
    if keep >= total:
        marker.write_text(key)
        return dest

    # Monotonic: the first-parent list is a chain, so an older cut can only
    # widen the range. Binary search for the smallest range holding at least
    # `keep` commits.
    lo, hi = 0, len(chain) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        n = int(rev("rev-list", "--count", f"{chain[mid]}..{branch}", cwd=dest).strip())
        if n >= keep:
            hi = mid
        else:
            lo = mid + 1
    cut = chain[lo]

    in_range = set(rev("rev-list", f"{cut}..{branch}", cwd=dest).split())
    for line in rev("rev-list", "--parents", f"{cut}..{branch}", cwd=dest).splitlines():
        parts = line.split()
        commit, parents = parts[0], parts[1:]
        kept = [p for p in parents if p in in_range]
        if len(kept) != len(parents):
            rev("replace", "--graft", commit, *kept, cwd=dest)
    marker.write_text(key)
    return dest


def repo_commit_count(repo, branch: str) -> int:
    return int(subprocess.run(
        ["git", "rev-list", "--count", branch], cwd=repo,
        check=True, capture_output=True, text=True).stdout.strip())


# ----------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"),
                        help="Compare two recordings and exit; no ingestion.")
    parser.add_argument("--repo", help="Repository to ingest.")
    parser.add_argument("--synthetic", action="store_true",
                        help="Build the synthetic repo in a temp dir and ingest that.")
    parser.add_argument("--truncate-by", type=int, default=None,
                        help="With --repo: ingest a shallow clone holding only "
                             "the most recent N commits. The source repo is "
                             "never modified.")
    parser.add_argument("--branch", default=None,
                        help="Branch to ingest. Resolved via _default_git_branch "
                             "when omitted; a literal HEAD is refused (#330).")
    parser.add_argument("--graph", help="Graph path for this arm.")
    parser.add_argument("--ratio", default="1:1",
                        help="MINIGRAF_INGEST_STREAM_RATIO, or 'forward-only'.")
    parser.add_argument("--out", help="JSONL recording to write.")
    parser.add_argument("--workdir", default=None,
                        help="Where the synthetic repo or truncated clone is "
                             "built. A temp dir when omitted.")
    args = parser.parse_args(argv)

    if args.compare:
        result = compare(args.compare[0], args.compare[1])
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if not args.out or not args.graph:
        parser.error("--out and --graph are required unless --compare is given")
    if bool(args.synthetic) == bool(args.repo):
        parser.error("pass exactly one of --synthetic or --repo")
    if args.branch == "HEAD":
        # #330: 'HEAD' is truthy, so it DEFEATS `branch or _default_git_branch(...)`
        # rather than adding to it. _default_git_branch may still RETURN it as
        # its last-resort fallback; that path stays reachable.
        parser.error("--branch HEAD is refused; name the real branch")

    # An existing graph would make this arm a RESUME, not a fresh ingestion,
    # and a resume re-walks already-ingested territory (#325/#326) -- its
    # recording is a baseline for nothing. Refused rather than silently
    # deleted: the file may be someone's real graph.
    if pathlib.Path(args.graph).exists():
        parser.error(f"{args.graph} already exists; a baseline arm must start "
                     "from a fresh graph. Remove it and re-run.")

    workdir = pathlib.Path(args.workdir) if args.workdir else pathlib.Path(
        tempfile.mkdtemp(prefix="fawp-"))
    workdir.mkdir(parents=True, exist_ok=True)

    if args.synthetic:
        repo = build_synthetic_repo(workdir / "synthetic")
        branch = args.branch or "master"
    else:
        repo = pathlib.Path(args.repo).resolve()
        branch = args.branch
        if args.truncate_by:
            if branch is None:
                branch = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                    check=True, capture_output=True, text=True).stdout.strip()
                if branch == "HEAD":
                    parser.error("source repo is on a detached HEAD; pass --branch")
            repo = truncate_repo(repo, workdir / "truncated", args.truncate_by, branch)

    # Reported, never assumed from --truncate-by: the cut range can overshoot
    # (a merge brings in a whole side branch), and a baseline that named a
    # commit count it had not measured would be exactly the "denominator
    # computed from what it validates" trap the rest of this harness refuses.
    repo_commits = repo_commit_count(repo, branch)

    result = record_run(repo, args.graph, args.ratio, args.out, branch=branch)
    result.update({"repo": str(repo), "branch": branch, "ratio": args.ratio,
                   "repo_commits": repo_commits, "truncate_by": args.truncate_by,
                   "graph": args.graph, "out": args.out, "workdir": str(workdir)})
    print(json.dumps(result, indent=2))
    # A baseline whose command count is zero proves nothing, and must not exit 0.
    return 0 if result["commands"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
