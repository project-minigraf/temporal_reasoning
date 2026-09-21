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
* Comparison is PER APPLY WINDOW, never one global list: the two streams
  interleave through executors, so global order is not stable run to run. A
  window is (commit hash, occurrence), not the hash alone -- one hash gets a
  second window when Stage B's `_forward_apply(lifecycle_only=True)` sweeps
  a commit Stage A already applied, and a write moved from the tail of the
  first to the head of the second concatenates identically under a hash-only
  key.
* Two arms must walk the SAME history. The recording's header carries a
  sha256 over the linearization and compare() REFUSES across corpora rather
  than reporting every window as one-sided.
* Two empty recordings are NOT clean -- see compare()'s proved_nothing key.
* Absolute wall-clock numbers are NOT comparable across invocation batches
  (precedent: probe_sweep_window_cost.py, where a baseline drifted 2.4x
  between rounds with no code change). Do not read a timing delta here as a
  finding.

WHAT IS RECORDED. Four choke points, in the order they matter:

  * `_db_execute` -- every graph read and write.
  * `_index_write` -- every fact-index insert/delete, WITH its triples. The
    index is the graph's only independent witness (#302) and it is written
    from inside _forward_apply.
  * `_commit_index_writer_safe` -- literally the last statement of
    _forward_apply's write tail. Without it, a task that moved or dropped
    that commit read `ok: true`.
  * `_db_checkpoint_gated` CALLS, as "(checkpoint-gated)". Flag site 6 of
    `lifecycle_only` is exactly `if not lifecycle_only:
    _db_checkpoint_gated(db)`, so an oracle blind to it could not see that
    site change. The CALL, never the outcome: `_db_checkpoint` itself is
    deliberately NOT wrapped, because _CheckpointPolicy gates it on a
    WALL-CLOCK duty budget (MINIGRAF_INGEST_CHECKPOINT_DUTY, #241) and
    whether a given call actually checkpoints varies run to run on identical
    code. Measured while building this probe: recording `_db_checkpoint`
    made the master-against-itself control report a difference at one commit
    (25 commands vs 26, the extra one a checkpoint) purely from that gate.

WHAT IS NOT. Only calls issued while a _forward_apply / _reverse_apply frame
is on the recording thread's stack are written to the JSONL. Everything else
an ingestion run does -- the preload lease, _frontier_load, the correction
sweep's own reads, the status handler -- is counted and dropped, because
those run on the event-loop thread interleaved with executor work and their
global order is not stable run to run. This is a deliberate scope choice, and
it is sound for a refactor confined to the apply functions: a write moved OUT
of _forward_apply into its caller vanishes from that window's list and is
caught as a shortened list, and one moved IN appears as an addition.

The dropped count is written into the recording's own trailer and compare()
reports any difference as `untagged_mismatch` -- but as of the measurement
below, that count is INFORMATIONAL, not gating. Measured directly: recording
the same 303-commit corpus, same 1:1 ratio, same PYTHONHASHSEED=0, produced
untagged_commands 475641 on one batch and 475623 on a LATER batch of BOTH an
edited arm and an unmodified-master control run in that same later batch
(total `commands` identical at 566360 in all three) -- so the count drifts
between invocation batches on completely unmodified code, and gating on it
false-fails every task whose baseline was recorded in an earlier batch than
its comparison arm. `untagged_mismatch` cannot be the thing that catches code
moving between two untagged sites either: a write moving OUT of an apply
frame disappears from that window's tagged sequence and the strict per-window
comparison below already fails on exactly that movement. What it cannot see
-- a reshuffle between two untagged call sites, neither of which ever enters
an apply frame -- was already a documented invisible residual, not something
this count newly covered.

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
import hashlib
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


PROBE_FORMAT = 2
"""Recording format. Bump whenever a change makes an older JSONL
uncomparable to a newer one -- compare() REFUSES across formats rather
than reporting the difference as a finding about the code.

  1  bare {"commit", "cmd"} rows.
  2  header + trailer rows, an occurrence index on each row, and
     fact-index traffic (_index_write / _commit_index_writer_safe)
     recorded alongside the graph commands.
"""


def _read(path):
    """Stream one recording into (grouped, n_rows, header, trailer).

    Streamed rather than materialized: a repo-scale recording is ~250 MB of
    JSONL and compare() holds TWO of them, so building an intermediate list
    of row dicts on top of the grouping doubles peak memory for nothing.

    The grouping key is `<commit>` for the first apply of that hash and
    `<commit>#<n>` for each later one. One hash is legitimately applied more
    than once in a run -- Stage A's forward or reverse pass, then Stage B's
    `_forward_apply(lifecycle_only=True)` -- and an earlier version of this
    function concatenated those windows into one list. That is exactly the
    blind spot this refactor is most likely to produce: a write moved from
    the TAIL of Stage A's apply to the HEAD of Stage B's lifecycle apply ON
    THE SAME HASH concatenates identically and reports clean. The occurrence
    index makes the two windows distinct sequences.

    A row with no `occ` (format 1, and the comparator's own unit fixtures)
    reads as occurrence 0, which renders as the bare hash -- so the key is
    unambiguous either way, since a commit hash cannot contain `#`.
    """
    grouped = {}
    header = trailer = None
    n_rows = 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "header" in row:
                header = row
                continue
            if "trailer" in row:
                trailer = row
                continue
            occ = row.get("occ", 0)
            key = row["commit"] if not occ else f"{row['commit']}#{occ}"
            grouped.setdefault(key, []).append(row["cmd"])
            n_rows += 1
    return grouped, n_rows, header, trailer


def compare(path_a, path_b) -> dict:
    """Compare two recordings command-for-command, per apply window.

    `proved_nothing` is True when EITHER file has zero command rows, and `ok`
    is then False. A comparator that reads two empty files as clean fails
    OPEN: a probe that recorded nothing at all would certify the refactor.
    One empty side is treated the same way rather than as a huge real
    difference, because an arm that crashed before its first write is a fact
    about the RUN, not a finding about the code under test.

    Three whole-recording checks run before the per-window diff, each with
    its own key so a reader can tell them apart:

    * `corpus_mismatch` -- the two arms walked different histories. REFUSED,
      not diffed: every window would read as one-sided and the output would
      look like catastrophic divergence in the code under test. The corpus id
      is a sha256 over the linearization, so it is exact rather than a label
      anyone could get wrong by hand. `proved_nothing` is set with it, because
      a refused comparison proved nothing about parity.
    * `format_mismatch` -- same reasoning, across PROBE_FORMAT versions.
    * `untagged_mismatch` -- the counts of commands issued OUTSIDE any apply
      frame differ. Computed and reported, but INFORMATIONAL -- it does not
      set `ok` to False. Measured before this was decided: two arms recording
      the SAME 303-commit corpus, SAME code, on separate invocation batches
      reported 475641 vs 475623 untagged commands (total `commands` identical
      at 566360), so this count drifts with the batch on unmodified code and
      gating on it would false-fail every task whose baseline predates its
      comparison arm. It is also not the thing that would catch a write
      crossing the apply-frame boundary -- a write moved OUT of an apply frame
      vanishes from that window's tagged sequence, which the strict per-window
      comparison below already fails on. The only thing this count could ever
      catch on its own is a reshuffle between two untagged call sites, which
      never touches an apply frame -- an already-documented invisible residual,
      not a new guarantee this count buys.

    A `ratio` difference is reported and deliberately does NOT refuse: the
    probe's own negative control compares a 1:1 recording against a
    forward-only one to prove the comparator can see a real difference.
    """
    a, rows_a, head_a, tail_a = _read(path_a)
    b, rows_b, head_b, tail_b = _read(path_b)

    def field(h, key):
        return h.get(key) if h else None

    corpus_a, corpus_b = field(head_a, "corpus_id"), field(head_b, "corpus_id")
    corpus_mismatch = bool(corpus_a and corpus_b and corpus_a != corpus_b)
    fmt_a = field(head_a, "probe_format")
    fmt_b = field(head_b, "probe_format")
    format_mismatch = bool(fmt_a and fmt_b and fmt_a != fmt_b)

    untagged_a, untagged_b = field(tail_a, "untagged_commands"), field(tail_b, "untagged_commands")
    untagged_mismatch = (
        untagged_a is not None and untagged_b is not None and untagged_a != untagged_b
    )
    # Exactly one side carrying a trailer means one recording was truncated
    # (the trailer is the last thing record_run writes) or the two came from
    # different probe versions. Neither is a finding about the code.
    trailer_mismatch = (tail_a is None) != (tail_b is None)

    refused = corpus_mismatch or format_mismatch
    proved_nothing = (not rows_a) or (not rows_b) or refused

    result = {
        "ok": False,
        "proved_nothing": proved_nothing,
        "corpus_mismatch": corpus_mismatch,
        "format_mismatch": format_mismatch,
        "untagged_mismatch": untagged_mismatch,
        "trailer_mismatch": trailer_mismatch,
        "corpus_a": field(head_a, "corpus_label"),
        "corpus_b": field(head_b, "corpus_label"),
        "ratio_a": field(head_a, "ratio"),
        "ratio_b": field(head_b, "ratio"),
        "ratio_mismatch": bool(
            field(head_a, "ratio") and field(head_b, "ratio")
            and field(head_a, "ratio") != field(head_b, "ratio")
        ),
        "untagged_a": untagged_a,
        "untagged_b": untagged_b,
        "commands_a": rows_a,
        "commands_b": rows_b,
        # WINDOWS, not commits: one hash gets a second window when Stage B
        # sweeps a commit Stage A already applied, so on a 303-commit corpus
        # these read 454. A key named `commits_a` holding 454 is the kind of
        # mislabelled number a later reader takes at face value, so both are
        # shipped and each says what it counts.
        "windows_a": len(a),
        "windows_b": len(b),
        "commits_a": len({k.split("#", 1)[0] for k in a}),
        "commits_b": len({k.split("#", 1)[0] for k in b}),
        "windows_only_in_a": [],
        "windows_only_in_b": [],
        "commits_only_in_a": [],
        "commits_only_in_b": [],
        "differing_commits": [],
    }
    if refused:
        return result

    # `commits_only_in_*` keeps its name and its meaning -- a WINDOW key,
    # which is the bare hash for the first window and `<hash>#<n>` after --
    # because that is what the per-window diff is keyed on and what a reader
    # needs to look the finding up. `windows_only_in_*` is an alias kept
    # beside it so the two count-keys above are not the only place the
    # distinction is stated.
    result["commits_only_in_a"] = [c for c in a if c not in b]
    result["commits_only_in_b"] = [c for c in b if c not in a]
    result["windows_only_in_a"] = result["commits_only_in_a"]
    result["windows_only_in_b"] = result["commits_only_in_b"]

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
    result["differing_commits"] = differing

    result["ok"] = (
        not proved_nothing
        and not result["commits_only_in_a"]
        and not result["commits_only_in_b"]
        and not differing
        and not trailer_mismatch
        # untagged_mismatch is deliberately NOT in this conjunction -- see the
        # docstring's `untagged_mismatch` bullet. It stays in `result` so a
        # reader can still see it; it must never gate `ok`.
    )
    return result


# ----------------------------------------------------------------- recording


_FORWARD_ONLY_RATIO = f"{10**6}:1"


def _resolve_ratio(ratio: str) -> str:
    """'forward-only' is spelled 1000000:1 everywhere in the suite (see
    tests/test_mcp_server.py); _parse_stream_ratio rejects a zero side."""
    return _FORWARD_ONLY_RATIO if ratio == "forward-only" else ratio


def arm_problems(result: dict, repo_commits: int) -> List[str]:
    """Why this arm is not a usable baseline, or [] if it is.

    All three conditions, not just the command count: a run can report
    `error` after writing plenty of commands, and one can report `complete`
    while having lost commits -- which is the entire reason #317's census
    exists. A degraded arm that exits 0 feeds a red comparison that gets
    misattributed to the code under test.
    """
    problems = []
    if not result.get("commands"):
        problems.append("recorded zero commands")
    if result.get("status") != "complete":
        problems.append(f"ingestion status is {result.get('status')!r}, not 'complete'")
    if result.get("graph_commits") != repo_commits:
        problems.append(
            f"graph holds {result.get('graph_commits')} commit entities but the "
            f"repo has {repo_commits}"
        )
    return problems


def corpus_identity(repo, branch: str):
    """(corpus_id, corpus_label) for the history `branch` names.

    The id is a sha256 over the linearization itself, not a name anyone types:
    two arms of one comparison must walk the SAME history, and a label like
    "repo, truncated to 300" is exactly the kind of thing that stays true
    while the history underneath it changes (this repo's `master` advancing
    between two tasks re-cuts a different 303 commits). The label is carried
    beside it for the reader, never for the check.
    """
    linearization = subprocess.run(
        ["git", "rev-list", "--topo-order", "--reverse", branch], cwd=repo,
        check=True, capture_output=True, text=True).stdout.split()
    digest = hashlib.sha256("\n".join(linearization).encode()).hexdigest()
    tip = linearization[-1] if linearization else "empty"
    return digest, f"{pathlib.Path(repo).name}@{branch}@{tip[:12]}@{len(linearization)}commits"


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
    real_index_write = mcp_server._index_write
    real_index_commit = mcp_server._commit_index_writer_safe
    real_forward = mcp_server._forward_apply
    real_reverse = mcp_server._reverse_apply

    # Thread-local, not a plain global: the apply functions run on
    # write_executor and issue every one of their own commands from that same
    # thread synchronously, so a thread-local tags exactly their commands. A
    # plain global would additionally MIS-tag anything the event-loop thread
    # happened to execute while an apply was in flight.
    tag = threading.local()
    counts = {"commands": 0, "untagged": 0}
    # How many apply windows this hash has already had. Stage A's apply is
    # occurrence 0, Stage B's lifecycle apply of the same hash is 1.
    occurrences: Dict[str, int] = {}
    fh = open(out_path, "w")

    corpus_id, corpus_label = corpus_identity(repo_path, branch)
    fh.write(json.dumps({
        "header": 1,
        "probe_format": PROBE_FORMAT,
        "corpus_id": corpus_id,
        "corpus_label": corpus_label,
        "ratio": ratio,
        "resolved_ratio": _resolve_ratio(ratio),
        "branch": branch,
    }) + "\n")

    def _record(cmd: str) -> None:
        commit = getattr(tag, "commit", None)
        if commit is None:
            counts["untagged"] += 1
            return
        row = {"commit": commit, "cmd": cmd}
        occ = getattr(tag, "occ", 0)
        if occ:
            row["occ"] = occ
        fh.write(json.dumps(row) + "\n")
        counts["commands"] += 1

    def spy_db_execute(db, datalog):
        _record(datalog)
        return real_db_execute(db, datalog)

    def spy_checkpoint_gated(db):
        _record("(checkpoint-gated)")
        return real_checkpoint_gated(db)

    # The fact index is the graph's only independent witness (#302), and it
    # is written from inside _forward_apply -- `_commit_index_writer_safe`
    # is literally the last statement of its write tail. Without these two
    # wrappers a task that moved or dropped that commit, or that changed
    # which triples reach the index, read `ok: true`.
    def spy_index_write(action, triples, index_con=None):
        _record(f"(index-{action} {json.dumps(triples, default=str)})")
        return real_index_write(action, triples, index_con=index_con)

    def spy_index_commit(index_con):
        _record(f"(index-commit {index_con is not None})")
        return real_index_commit(index_con)

    # *args/**kwargs with positional indexing only: every defaulted parameter
    # of both functions is keyword-only as of PR #360, so a wrapper sees
    # exactly 5 (forward) or 6 (reverse) positionals and the commit hash sits
    # at a fixed index that no future keyword parameter can shift.
    def _enter(commit_hash):
        previous = (getattr(tag, "commit", None), getattr(tag, "occ", 0))
        tag.commit = commit_hash
        # Counted on ENTRY, so the index names this apply window rather than
        # the number of windows that have finished.
        tag.occ = occurrences.get(commit_hash, 0)
        occurrences[commit_hash] = tag.occ + 1
        return previous

    def _leave(previous):
        tag.commit, tag.occ = previous

    def spy_forward(*args, **kwargs):
        previous = _enter(args[3][0])    # commit_metadata[pos][0]
        try:
            return real_forward(*args, **kwargs)
        finally:
            _leave(previous)

    def spy_reverse(*args, **kwargs):
        previous = _enter(args[2][args[4]])   # linearization[pos]
        try:
            return real_reverse(*args, **kwargs)
        finally:
            _leave(previous)

    mcp_server._db_execute = spy_db_execute
    mcp_server._db_checkpoint_gated = spy_checkpoint_gated
    mcp_server._index_write = spy_index_write
    mcp_server._commit_index_writer_safe = spy_index_commit
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
        mcp_server._index_write = real_index_write
        mcp_server._commit_index_writer_safe = real_index_commit
        mcp_server._forward_apply = real_forward
        mcp_server._reverse_apply = real_reverse
        # The trailer is the LAST thing written, so its absence means the
        # recording was truncated -- which compare() reads as an integrity
        # failure rather than as a finding about the code.
        fh.write(json.dumps({
            "trailer": 1,
            "commands": counts["commands"],
            "untagged_commands": counts["untagged"],
        }) + "\n")
        fh.close()
        mcp_server._reset_db_state()

    status = mcp_server.handle_minigraf_ingest_status()
    with mcp_server.db_lease() as db:
        graph_commits = mcp_server._count_commit_entities(db)
    mcp_server._reset_db_state()

    grouped, file_rows, _header, _trailer = _read(out_path)
    return {
        # Recomputed from the FILE rather than from the counter, so a
        # truncated or unflushed write is visible instead of being reported
        # as recorded.
        "commands": file_rows,
        "apply_windows": len(grouped),
        "commits": len({k.split("#", 1)[0] for k in grouped}),
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
    # The TIP SHA is part of the key, not just (source, branch, keep): those
    # three stay identical while `master` advances underneath them, and the
    # same `keep` then cuts a DIFFERENT set of commits. A reused workdir must
    # be rejected in that case, or two arms of one comparison silently walk
    # two histories -- which compare() would then report as every window
    # being one-sided.
    tip = subprocess.run(
        ["git", "rev-parse", branch], cwd=source,
        check=True, capture_output=True, text=True).stdout.strip()
    key = f"{source}\n{branch}\n{keep}\n{tip}\n"
    if marker.exists() and marker.read_text() == key:
        return dest
    if dest.exists():
        raise RuntimeError(
            f"{dest} already exists and was not cut as ({source}, {branch}, "
            f"{keep}, tip {tip[:12]}); remove it or pass a different --workdir"
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

    # A DEGRADED arm must not exit 0. Its recording is short for a reason
    # that has nothing to do with the code under test, and the comparison it
    # feeds would come back red and be misattributed to the refactor. All
    # three conditions are checked, not just the command count: a run can
    # report `error` after writing plenty of commands, and one can report
    # `complete` while having lost commits (the whole reason #317's census
    # exists).
    problems = arm_problems(result, repo_commits)
    result["arm_ok"] = not problems
    result["problems"] = problems
    print(json.dumps(result, indent=2))
    if problems:
        print("ARM DEGRADED, not a usable baseline: " + "; ".join(problems),
              file=sys.stderr)
    return 0 if result["arm_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
