# At-Scale Code-Graph Benchmark Tier — Design

**Date:** 2026-07-19
**Issue:** #120

## Problem

The existing eval suite (`evals/benchmark.md`, `evals/evals.json`, `evals/run_isolated.py`)
measures the skill's *marginal value to a coding agent*: 12 LLM-judged, conversational
with-skill/without-skill ablation evals on tiny seeded graphs (4-5 facts each). That suite is
solid for what it tests, but it never exercises this project's actual differentiator — a
deterministic, bi-temporal, Datalog-queryable structural model of a *real codebase at scale* —
and it never measures ingestion behavior under real load.

That gap is not academic. A full-scale run against ArangoDB (52,948 commits) surfaced
hours-long ingestion, graph bloat, and MCP-responsiveness starvation (#115, #116, #118), none
of which the seeded evals could have caught, because they never operate at scale and never
touch ingestion at all.

## Goal

Add a second, structurally different benchmark tier — "at-scale code-graph" — that:

- **(Part A) Measures ingestion performance** on a real, git-ingestable repo: wall-clock,
  throughput, peak memory, final graph/index size, and — critically — whether
  `minigraf_query`/`minigraf_ingest_status` stay responsive *while ingestion is running*
  (the concrete failure mode #116 fixed).
- **(Part B) Measures structural/temporal query correctness and latency** against
  hand-authored ground truth, with a head-to-head comparison against the plain
  `git log`/`git blame`/`git diff` an agent would fall back to without this project — the
  project's own claimed differentiator, made falsifiable.

Sequenced: Part A first (no ground-truth-authoring dependency, simpler), Part B as a
fast-follow reusing Part A's harness. Both are **observational** for this first version — a
checked-in report with real numbers, not a pass/fail CI gate. Thresholds require a baseline to
be meaningful, and this project's existing `evals/benchmark.md` is itself observational, not
gated; hard thresholds can be layered on once a few runs exist to calibrate against.

## Non-goals

- No pass/fail regression gate in this version (may follow once a baseline exists).
- No requirement to benchmark against an external multi-thousand-commit repo like ArangoDB —
  the harness is repo-agnostic and configurable, but the checked-in default/CI fixture is this
  repo's own history (see "Which repo" below).
- No embedding-based or fuzzy-match query correctness scoring — Part B's ground truth uses
  exact or narrowly-documented comparison rules per question.
- No change to the existing conversational eval suite (`evals/evals.json`,
  `evals/run_isolated.py`) — this is an additive, separate tier under `evals/at_scale/`.

## Which repo

The issue's own motivating example (ArangoDB, 52,948 commits) took 4+ hours and never
finished — too large for a routine, reproducible benchmark. The harness accepts
`--repo-path`/`--commit`/`--branch` so it can be pointed at any repo, but the **default, and
the one whose results get checked in**, is `temporal_reasoning` itself: already local, no
clone/network/licensing step, small enough to run in well under a minute, and zero drift risk
from an external repo's history changing shape. This trades "true at-scale" for reproducibility
and zero external dependency; a deeper manual run against a larger external repo remains
possible via the CLI flags but isn't part of the checked-in/CI-facing default.

## Architecture: in-process harness, no subprocess/stdio/LLM

`evals/run_isolated.py` drives a real `claude` CLI subprocess over MCP stdio for the
conversational evals, because it needs an actual LLM making tool-call decisions to judge. This
benchmark tier needs neither an LLM nor the MCP transport — git ingestion's structural
extraction is tree-sitter/CPU-bound, not LLM-based, and what's being measured is the behavior
of `mcp_server.py`'s own async handlers under load.

This project's own testing convention (`docs/testing-conventions.md`) already establishes the
right pattern: drive a real (non-mocked) backend by calling `mcp_server.py`'s handler functions
directly, in-process. The benchmark harness follows the same pattern, just as a standalone
script instead of a pytest fixture:

```python
os.environ["MINIGRAF_GRAPH_PATH"] = str(tmp_graph_path)
os.environ["MINIGRAF_NO_AUTO_INGEST"] = "1"
import mcp_server

ingest_task = asyncio.create_task(mcp_server._run_ingestion(repo_path, branch))

# Concurrently, while ingestion is still running:
while not ingest_task.done():
    t0 = time.perf_counter()
    mcp_server.handle_minigraf_ingest_status()
    status_latencies.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    mcp_server.handle_minigraf_query("[:find (count ?e) :where [?e :entity-type _]]")
    query_latencies.append(time.perf_counter() - t0)

    await asyncio.sleep(0.5)

await ingest_task
```

This exercises the real `_run_ingestion` coroutine and its `run_in_executor`-offloaded work
exactly as production does, and directly measures event-loop responsiveness during ingestion —
the actual thing #116-class bugs break — without needing a live LLM in the loop at all.

Each benchmark run uses a fresh temp directory for `MINIGRAF_GRAPH_PATH` (same isolation
approach `run_isolated.py._seed_graph` already uses), so runs never touch the live project
graph.

## Part A — Ingestion performance

**Script:** `evals/at_scale/run_ingestion_benchmark.py`

```
python evals/at_scale/run_ingestion_benchmark.py [--repo-path .] [--branch HEAD] [--compare-ignore]
```

| Metric | How measured |
|---|---|
| Wall-clock to full ingestion | `time.perf_counter()` around the `_run_ingestion` task |
| Throughput (commits/min) | commits ingested ÷ (wall-clock / 60) |
| Peak RSS | `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` — free, no manual sampling needed since the harness process does nothing else |
| Final graph + fact-index size | `os.path.getsize()` on the graph file and `fact_index.index_path_for(...)` post-run |
| MCP responsiveness during ingestion | round-trip latency of `handle_minigraf_ingest_status()` and one representative `handle_minigraf_query(...)` call, polled every 0.5s while `ingest_task` is in flight; reported as min/p50/p99/max |
| With vs without path-ignore bloat (#115) | `--compare-ignore` flag (off by default — doubles runtime): runs ingestion twice, second time with `mcp_server._DEFAULT_IGNORE_PATTERNS` monkeypatched to `()` in-process; diffs final graph size. The default self-repo fixture has no vendored/`node_modules` dirs, so this is mainly useful for manual runs against an external repo via `--repo-path` |

**Output:**
- `evals/at_scale/benchmark.md` — human-readable report, structured like `evals/benchmark.md`:
  each run appended as a new dated section with a metrics table. No pass/fail; numbers only.
- `evals/at_scale/results/<timestamp>.json` — machine-readable metrics for future diffing
  between runs, once enough history exists to define real thresholds.

## Part B — Query correctness & latency (fast-follow)

Reuses Part A's harness to populate a graph from this repo's own history, then runs a fixed set
of labeled structural/temporal questions against it.

**Ground truth:** `evals/at_scale/query_ground_truth.json` — a hand-authored, static fixture
(same spirit as `evals.json`'s `seed` blocks), one entry per category named in the issue:

- Point-in-time structure ("what did module X's dependency set look like at commit Y?")
- Delta ("which edges appeared/disappeared between two commits?")
- Regression tracing ("when did dependency A→B first appear?")
- Transitive impact at depth ("what transitively depends on X as of date D?")
- Cross-layer / decision correlation ("which structural changes followed decision Z?")

Each entry:

```json
{
  "id": 1,
  "category": "point-in-time",
  "question": "...",
  "datalog": "[:find ... :as-of ... :where ...]",
  "expected": "... (exact value or documented comparison rule)",
  "baseline_cmd": "git log --oneline -- path/to/module.py"
}
```

Ground truth is verified by hand against this repo's real history at authoring time and then
frozen — not recomputed live on every run (mirrors `evals.json`'s static seed philosophy).

**Cross-layer question caveat:** no agent-authored decision datom already exists tied to a real
commit in this repo's history, so that one question needs a synthetic decision fact seeded with
`:valid-from` matching a real historical commit's timestamp. The spec and the fixture file both
document this explicitly as synthetic setup, not a claim that the correlation is organically
real.

**Script:** `evals/at_scale/run_query_benchmark.py` — for each question: runs `datalog` via
`handle_minigraf_query` against the Part-A-populated graph and times it; separately shells out
`baseline_cmd` via `subprocess.run` and times that; compares the query result to `expected`
(exact match, or a per-question documented comparison rule for cases like unordered result
sets). Appends a correctness + latency table to the same `evals/at_scale/benchmark.md`.

## Testing

- `run_ingestion_benchmark.py` and `run_query_benchmark.py` are benchmark scripts, not pytest
  suites — no new `tests/test_*.py` file is required for the scripts themselves (their job is
  producing real numbers against a real repo, which is what they're for).
- The `--compare-ignore` monkeypatch (`_DEFAULT_IGNORE_PATTERNS` → `()`) and the harness's
  direct calls into `mcp_server` handlers are exercised implicitly every time the script runs
  against the default self-repo fixture; no separate unit test doubles this.
- If `_load_ignore_patterns`/`_run_ingestion`/`handle_minigraf_query`/`handle_minigraf_ingest_status`
  already have their own unit test coverage (they do, per the existing suite) — this benchmark
  tier is not meant to duplicate that; it measures operational characteristics under real load,
  which unit tests structurally cannot.

## Open questions for implementation

None — scope, repo choice, gating posture, and harness architecture were all resolved during
brainstorming (see decisions above). Part B's exact question wording and `expected` values will
be authored during implementation against this repo's real `git log` output at that time.
