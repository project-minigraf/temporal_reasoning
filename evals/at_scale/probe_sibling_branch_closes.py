# evals/at_scale/probe_sibling_branch_closes.py
"""#384 exposure probe: how often does the forward walk's per-file running
state disagree with a commit's OWN first parent, and what does that cost in
spurious closes?

The forward walk diffs a modified file's entities against
`state.file_entities[path]` -- whatever the LINEARIZATION last saw for that
path -- not against the commit's own parent. On a linear history the two are
the same blob. With concurrent branches touching one file they are not: a
sibling-branch commit "removes" what the other branch added (spurious close),
and the entity is reborn at the merge.

This probe needs no graph. It replays the walk's view at BLOB granularity
(running blob per path, updated by every linearized commit's diff-tree
exactly as `_git_diff_tree_raw` reports it, merges included) and compares it
to the first-parent blob git reports as the commit's own `old_sha`. Where the
two differ, both blobs plus the new one are parsed with the real extractor
(`_extract_from_source` + `_code_ident`) and the entity sets compared:

  walk_closes     = I(walk_prev) - I(new)            what the walk closes
  genuine_closes  = I(true_prev) - I(new)            what the commit removed
  spurious_closes = walk_closes - I(true_prev)       closed, never removed here
  missed_closes   = genuine_closes - I(walk_prev)    removed, walk never saw it
  inverted        = spurious close at a commit dated BEFORE the walk's own
                    introduction of that ident (valid-to < valid-from)

Approximations, stated: renames ("R") are counted at blob level only (idents
embed the path, and the walk's rename pass is not modelled); an "A" whose
path the walk already holds is counted as `stale_add` (the walk closes nothing
for "A", so the stale entities stay live); in-file renames
(`_fwd_apply_renamed_pairs`) are not excluded from walk_closes. Spurious
closes are therefore an UPPER bound only in that last respect, and the rename
exclusion applies to genuine renames, which are rare among spurious ones.

Run: .venv/bin/python -m evals.at_scale.probe_sibling_branch_closes REPO [--branch B] [--json OUT]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from typing import Dict, List, Optional, Set

import mcp_server as ms
from frontier_registry import build_linearization


class _BlobReader:
    def __init__(self, repo: str):
        self._p = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=repo,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def read(self, sha: str) -> Optional[bytes]:
        self._p.stdin.write(sha.encode() + b"\n")
        self._p.stdin.flush()
        header = self._p.stdout.readline().split()
        if len(header) < 3 or header[1] == b"missing":
            return None
        size = int(header[2])
        data = self._p.stdout.read(size)
        self._p.stdout.read(1)
        return data

    def close(self) -> None:
        self._p.stdin.close()
        self._p.wait()


_ZERO = "0" * 40


def _commit_meta(repo: str, branch: str) -> Dict[str, tuple]:
    out = subprocess.run(
        ["git", "log", "--format=%H %ct %P", branch],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    meta = {}
    for line in out.splitlines():
        parts = line.split()
        meta[parts[0]] = (int(parts[1]), parts[2:])
    return meta


def probe(repo: str, branch: str) -> dict:
    t0 = time.monotonic()
    lin = build_linearization(repo, branch)
    meta = _commit_meta(repo, branch)
    ignore = ms._load_ignore_patterns(repo)
    reader = _BlobReader(repo)
    ident_cache: Dict[tuple, Set[str]] = {}

    def idents(path: str, sha: Optional[str]) -> Set[str]:
        if not sha or sha == _ZERO:
            return set()
        key = (path, sha)
        if key not in ident_cache:
            parser = ms._thread_parser(path)
            data = reader.read(sha)
            out: Set[str] = set()
            if parser is not None and data is not None:
                ex = ms._extract_from_source(data, parser, path)
                out.add(ms._code_ident("module", path))
                out.update(ms._code_ident("function", path, n) for n in ex.get("functions", []))
                out.update(ms._code_ident("class", path, n) for n in ex.get("classes", []))
                out.update(ms._code_ident("variable", path, n) for n in ex.get("globals", []))
                out.update(
                    ms._code_ident("field", path, f"{cls}.{n}")
                    for n, cls, _s in ex.get("fields", [])
                )
            ident_cache[key] = out
        return ident_cache[key]

    running: Dict[str, str] = {}
    intro_ts: Dict[str, int] = {}  # ident -> ts the walk introduced it
    c = Counter()
    spurious_by_ident: Counter = Counter()
    inverted_idents: Set[str] = set()
    samples: List[dict] = []

    for pos, h in enumerate(lin):
        ts, parents = meta[h]
        is_merge = len(parents) > 1
        c["commits"] += 1
        c["merges"] += is_merge
        for status, _om, _nm, old_sha, new_sha, path, old_path, _sim in ms._git_diff_tree_raw(repo, h):
            if status == "R":
                src = old_path
            else:
                src = path
            if ms._is_ignored_path(path, ignore) or ms._thread_parser(path) is None:
                running.pop(src, None)
                continue
            c["file_changes"] += 1
            walk_prev = running.get(src)
            true_prev = None if old_sha == _ZERO else old_sha
            diverged = walk_prev != true_prev
            if diverged:
                c["diverged"] += 1
                c[f"diverged_{status}"] += 1
                c["diverged_at_merge"] += is_merge
            new = None if status == "D" else new_sha

            if status in ("M", "D") and diverged:
                i_walk = idents(src, walk_prev)
                i_true = idents(src, true_prev)
                i_new = idents(path, new)
                walk_closes = i_walk - i_new
                spurious = walk_closes - i_true
                missed = (i_true - i_new) - i_walk
                late = (i_new & i_true) - i_walk
                c["late_intros"] += len(late)
                c["spurious_closes"] += len(spurious)
                c["missed_closes"] += len(missed)
                for ident in spurious:
                    spurious_by_ident[ident] += 1
                    if ident in intro_ts and ts < intro_ts[ident]:
                        inverted_idents.add(ident)
                        c["inverted"] += 1
                if spurious and len(samples) < 20:
                    samples.append({
                        "pos": pos, "commit": h[:12], "merge": is_merge, "path": path,
                        "status": status, "spurious": sorted(spurious)[:5],
                    })
            elif status == "A" and walk_prev is not None:
                c["stale_add"] += 1
                c["stale_add_entities"] += len(idents(src, walk_prev) - idents(path, new))

            # The walk's own introduction timestamps, for the inversion test.
            i_before = idents(src, walk_prev) if walk_prev else set()
            i_after = idents(path, new)
            for ident in i_after - i_before:
                intro_ts[ident] = ts
            if status == "R":
                running.pop(old_path, None)
            if new is None:
                running.pop(path, None)
            else:
                running[path] = new
    # End state: the walk's live set (idents of each path's running blob)
    # against HEAD's own tree. Clean merges are not walked, so a path whose
    # last linearized change was a sibling-branch blob keeps that blob.
    head = {}
    for line in subprocess.run(
        ["git", "ls-tree", "-r", branch], cwd=repo, capture_output=True,
        text=True, check=True,
    ).stdout.splitlines():
        info, _, path = line.partition("\t")
        _mode, kind, sha = info.split()
        if kind == "blob" and not ms._is_ignored_path(path, ignore) and ms._thread_parser(path) is not None:
            head[path] = sha
    stale_live: Set[str] = set()
    missing_live: Set[str] = set()
    for path in set(head) | set(running):
        if head.get(path) == running.get(path):
            continue
        c["head_files_diverged"] += 1
        i_run, i_head = idents(path, running.get(path)), idents(path, head.get(path))
        stale_live |= i_run - i_head
        missing_live |= i_head - i_run
    reader.close()
    multi = sum(1 for n in spurious_by_ident.values() if n >= 2)
    return {
        "repo": repo, "branch": branch,
        "counts": dict(c),
        "entities_spuriously_closed": len(spurious_by_ident),
        "entities_spuriously_closed_twice_or_more": multi,
        "inverted_entities": len(inverted_idents),
        "head_stale_live": len(stale_live),
        "head_missing_live": len(missing_live),
        "head_stale_sample": sorted(stale_live)[:10],
        "head_missing_sample": sorted(missing_live)[:10],
        "inverted_sample": sorted(inverted_idents)[:20],
        "samples": samples,
        "seconds": round(time.monotonic() - t0, 1),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--branch")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    branch = a.branch or ms._default_git_branch(a.repo)
    r = probe(a.repo, branch)
    print(json.dumps(r, indent=2))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(r, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
