#!/usr/bin/env python3
"""Code A/B: two source TREES of this repo, one interpreter, interleaved A B A B.

`probe_minigraf_upgrade_cost.py batch` compares two INTERPRETERS over one
tree. This compares two builds of mcp_server (e.g. a `git worktree` of master
against a branch): each arm runs THAT probe's own `arm` mode from its own
tree, so it imports that tree's mcp_server, with PYTHONHASHSEED=0 and its own
process group killed on exit (docs/design-notes.md, #313). The verdict is the upgrade
probe's pure `verdict()` -- parity fingerprints across every run, each arm's
spread beside every delta. Both trees must carry the upgrade probe. First used
for #369 (`results/369-sweep-retract-ab.json`).

    git worktree add ~/.cache/ab/master master
    .venv/bin/python evals/at_scale/probe_code_ab.py \\
        --tree-a ~/.cache/ab/master --tree-b . --repo . --ref daf1b8d \\
        --workroot ~/.cache/ab/runs --repeats 2 --out results/X.json
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PROBE = "evals/at_scale/probe_minigraf_upgrade_cost.py"


def run_arm(python, tree, repo, sha, wd):
    wd.mkdir(parents=True, exist_ok=True)
    out = wd / "result.json"
    out.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGRAF_")}
    env["PYTHONHASHSEED"] = "0"
    cmd = [python, PROBE, "arm", "--repo", repo, "--ref", sha, "--workdir", str(wd), "--out", str(out), "--lineage-cache", "on"]
    with open(wd / "console.log", "w") as log:
        p = subprocess.Popen(cmd, cwd=tree, env=env, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait()
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if rc != 0 or not out.exists():
        raise RuntimeError(f"arm failed rc={rc}; see {wd / 'console.log'}")
    return json.loads(out.read_text())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--tree-a", required=True)
    p.add_argument("--tree-b", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--workroot", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--issue", type=int)
    args = p.parse_args(argv)
    if args.ref == "HEAD":
        print("refusing a literal HEAD ref (docs/design-notes.md, #330)", file=sys.stderr)
        return 2
    repo = str(pathlib.Path(args.repo).resolve())
    trees = {a: str(pathlib.Path(t).expanduser().resolve())
             for a, t in (("A", args.tree_a), ("B", args.tree_b))}
    for t in trees.values():
        if not (pathlib.Path(t) / PROBE).exists():
            print(f"{t} has no {PROBE}", file=sys.stderr)
            return 2
    sha = subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "--verify", f"{args.ref}^{{commit}}"], text=True).strip()
    heads = {a: subprocess.check_output(["git", "-C", t, "rev-parse", "HEAD"], text=True).strip()
             for a, t in trees.items()}
    root = pathlib.Path(args.workroot).expanduser()
    runs = []
    for rep in range(args.repeats):
        for arm in ("A", "B"):
            print(f"[{time.strftime('%H:%M:%S')}] rep {rep} arm {arm}", flush=True)
            res = run_arm(args.python, trees[arm], repo, sha, root / f"{rep}-{arm}")
            print(f"    wall {res['wall_s']:.1f}s", flush=True)
            runs.append({"rep": rep, "arm": arm, "tree_head": heads[arm], "result": res})

    sys.path.insert(0, str(_REPO_ROOT))
    from evals.at_scale.probe_minigraf_upgrade_cost import verdict

    v = verdict(runs)
    pathlib.Path(args.out).write_text(json.dumps({
        "issue": args.issue, "mode": "code-ab", "tree_heads": heads, "repo": repo,
        "ref": args.ref, "sha": sha, "order": [f"{r['rep']}-{r['arm']}" for r in runs],
        "verdict": v, "runs": runs,
    }, indent=2))
    print(json.dumps({k: v[k] for k in ("parity_ok", "parity_problems", "no_regression")}, indent=2))
    for key in ("wall_s", "db_exec_total_s", "drops_total_s"):
        c = v[key]
        print(f"{key:18s} A {c['a_median']:9.1f}  B {c['b_median']:9.1f}  B/A {c['b_over_a']}  "
              f"spread A {c['a_spread']} B {c['b_spread']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
