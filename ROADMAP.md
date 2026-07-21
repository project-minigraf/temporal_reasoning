# Temporal Reasoning - Roadmap

## Phase 1 (Complete ✓)
- Python CLI wrapper (`minigraf_tool.py`)
- Tool schemas (query.json, transact.json)
- Operational prompts (system.txt, fewshots.txt)
- Test harness

## Phase 2 (Complete ✓)

| Priority | Item | Description | Effort | Status |
|----------|------|-------------|--------|--------|
| 1 | Write policy enforcer | Validate reason required before transact | Low | Complete ✓ |
| 3 | report_issue tool | Auto-file GitHub issues on failures | Low | Complete ✓ |
| 4 | install.py | One-command setup script | Low | Complete ✓ |

## Phase 3 (Complete ✓)

| Item | Description | Status |
|------|-------------|--------|
| Persistent MCP server | `mcp_server.py` — replaces CLI wrapper; single-process, stdio, minigraf Python binding | Complete ✓ |
| 6 MCP tools | `minigraf_query`, `minigraf_transact`, `minigraf_retract`, `minigraf_report_issue`, `memory_prepare_turn`, `memory_finalize_turn` | Complete ✓ |
| Auto-memory hooks (Claude Code) | `UserPromptSubmit` injects context; `Stop` hook extracts facts — `hooks/prepare_hook.py`, `hooks/finalize_hook.py` | Complete ✓ |
| Hook config templates | `hooks/claude-code.json`, `hooks/codex.toml`, `hooks/hermes.yaml`, `hooks/opencode.json` (degraded), `hooks/openclaw.json` (degraded) | Complete ✓ |
| Heuristic extraction | Regex-based signal detection; zero API calls; no configuration required | Complete ✓ |
| LLM extraction | Claude Haiku extracts facts; falls back to agent strategy on API failure | Complete ✓ |
| Agent extraction | MCP sampling requests a memory block from the connected agent | Complete ✓ |
| Bi-temporal writes | `:valid-from` recorded on every write; point-in-time queries via `:valid-at` correct | Complete ✓ |

## Phase 3 — Finish Line (Complete ✓)

| Item | Description | Status |
|------|-------------|--------|
| OpenAI/Codex support | `MINIGRAF_LLM_MODEL=gpt-4o-mini` selects OpenAI client automatically; Codex hook wiring fully enabled. Spec: `docs/superpowers/specs/2026-05-26-openai-llm-strategy-design.md` | Complete ✓ |

## Phase 4 (Complete ✓) — Entity Normalization and Schema-Aware Extraction

| Item | Description | Status |
|------|-------------|--------|
| Slug canonicalization | `_canonical_ident` + `_keyword_uuid`; heuristic extractor updated | Complete ✓ |
| Closed-world schema | `MINIGRAF_SCHEMA` (4 entity types) + `_validate_facts`; pre-transact enforcement in extraction pipeline and `handle_minigraf_transact` | Complete ✓ |
| Alias datoms | `:alias` declared as optional attribute on all entity types | Complete ✓ |
| Schema-aware prompts | `_query_canonical_entities` injects existing idents into LLM and agent extraction prompts | Complete ✓ |
| `minigraf_audit` | 7th MCP tool; audits all entities against schema, retracts violators (bi-temporal — history preserved) | Complete ✓ |
| Entity Resolution section | `SKILL.md` updated with resolution guidance and `minigraf_audit` instructions | Complete ✓ |

## Phase 5 (Feature-complete; production-hardening shipped, not yet re-validated at reported production scale) — Code Structure Evolution from Git History

Extend the bi-temporal graph to store code structure extracted from git history, enabling temporal queries over how a codebase evolved and why. Ingested structural entities resolve against the canonical schema from Phase 4. Phase 6 observability will add confidence tagging on top of the ingested edges.

### Why Not Just Read Git History?

An agent with git access can already answer simple temporal questions: `git show <commit>:ROADMAP.md`, `git diff v1..v2`, `git blame`. For small projects with short histories, that is often enough.

This project adds value where git structurally cannot help:

**Cross-cutting semantic queries.** Git is organized by commit — time slices of the whole repo. To answer "when did module A first depend on module B?", an agent must check out every commit, parse the code at each one, and scan. That is O(commits × parse time) and blows the context window on any real codebase. The graph inverts the index — facts are stored by entity, so you query forward from the entity directly.

**Semantic structure survives text changes.** Git sees line diffs. It does not know a function moved from `auth.py` to `middleware/auth.py` — it sees a deletion and an addition. `git blame` breaks on renames. The graph stores `:calls` and `:depends-on` edges that are entity-addressed, not file-addressed. Refactors do not break the history.

**Agent-authored facts do not exist in git.** Decisions, constraints, and observations that an agent logged but never committed to a file have no representation in git. The graph is the only place where these coexist with code structure as queryable facts.

**Cross-layer joins.** There is no way to ask git: "list all dependency changes that happened after the decision to switch databases." The decision lives in agent memory; the structural change lives in git; they are in separate systems with no shared query surface. In the graph, both are datoms and a single Datalog join connects them.

The graph is built *from* git but answers queries git cannot express. Value scales with history length, structural complexity, and frequency of cross-cutting or cross-layer queries.

### Validated positioning

A competitive scan (mem0, Zep/Graphiti, Cognee, Letta, the MCP reference memory
server) done while investigating #119 confirmed this niche is real and
unoccupied: the nearest neighbor, Zep/Graphiti, is bi-temporal but relies on LLM
extraction, an opaque hybrid-search API, no Datalog, and no code-structure
ingestion. The deterministic, bi-temporal, Datalog-queryable structural model of
a codebase — not conversational-memory parity with mem0/Zep/Letta — is this
project's differentiator, and the roadmap should keep leaning into it. The
problem #119 raised was never that this positioning was wrong, only that Phase 5
overstated how production-ready the feature occupying it was; see Phase 5.5.

### Ingestion

- Walk git log and replay commits in order, extracting AST-level structure at each commit (functions, classes, modules, call edges, dependency edges) using tree-sitter or equivalent
- Transact each commit as a minigraf transaction: new edges added, removed edges retracted, with the commit hash, author, and message stored as the reason
- Support incremental re-ingestion (only process commits since last-known transaction) for use in CI or post-commit hooks
- Map git commit timestamps to minigraf valid-time so wall-clock `as-of` queries work alongside transaction-number queries

### Queries this unlocks

- Point-in-time structure: what did the call graph / dependency graph look like at commit X or date Y?
- Delta queries: which edges appeared or disappeared between two commits?
- Coupled evolution: which modules changed together most frequently (implicit coupling)?
- Decision correlation: which structural changes happened after a given agent decision was logged?
- Regression tracing: when did a specific dependency or coupling first appear?

### Reasoning layer

- Agent-facing query patterns (fewshots / skill prompts) for common insights: circular dependency detection, high-churn modules, blast radius of a proposed change
- Cross-layer queries that join code structure edges with agent decision datoms in the same graph — e.g., "show dependency changes that occurred after the database migration decision"
- Natural-language question templates mapped to Datalog patterns so agents can ask structural questions without writing raw Datalog

## Phase 5.5 (Complete ✓) — Ingestion Hardening

A full-scale ingestion run against a real repo (ArangoDB, 52,948 HEAD commits)
surfaced that Phase 5 was feature-complete but not production-viable: ingestion sat
at ~100% CPU for 4+ hours and reached only ~21,134 / 52,948 commits before being
stopped, with the graph reaching ~3.8 GB / ~460k entities, most of it vendored
third-party code. Filed as #119, which named four blocking problems directly
(#103, #111, #115, #116) and, in the same write-up, flagged two further
retrieval-path gaps (#117, #118) that made the roadmap's BM25 backlog note
inaccurate — folded into the same table below since all six belong to the same
production-viability story:

| Issue | Problem | Fix |
|---|---|---|
| #103 | Synchronous startup work blocks the MCP handshake on large graphs | Offloaded `_run_ingestion`'s startup preload phase to a worker thread (`run_in_executor`) |
| #111 (+#113) | Renames/moves not tracked — identity is path-derived, so history fractures into duplicate disconnected entities; also, fields/static members/globals were never extracted as entities at all | Rename/move tracking added (entity identity survives path changes); field/static-member/global-variable extraction added to the AST pass |
| #115 | Vendored/3rd-party subtrees (V8, ICU, boost, node_modules) ingested as first-class entities, bloating the graph and drowning retrieval in noise | Path-ignore config for git ingestion |
| #116 | AST extraction ran on a GIL-bound thread pool — a single mega-commit could peg CPU for hours and make the MCP server unresponsive | Moved extraction off the GIL-bound thread pool |
| #117 | `rank-bm25` optional-extra absence silently degrades hook-side retrieval to an ~82s/7GB path | First promoted `rank-bm25` to a core dependency; superseded days later when #118's persisted index shipped and the in-memory BM25 path (`FactIndex`/`IndexCache`, `rank-bm25` itself) was deleted outright as unreachable (#148) |
| #118 | In-memory BM25 index is a per-process singleton; the `UserPromptSubmit` hook is a fresh short-lived process every turn, so retrieval was always cold and returned nothing | Persisted, mmap-able on-disk SQLite FTS5 index (`fact_index.py`), bi-temporal, shared across processes — the in-memory BM25 path this replaced no longer exists in the codebase |

Fixing these surfaced further correctness bugs in the same area, tracked and
resolved in the same hardening pass rather than deferred: #146 (Datalog injection
via unescaped string interpolation), #153 (agent-strategy extraction losing its
keyword ident), #152 (fact-index duplicate rows on idempotent re-writes), #156
(`_ingest_tags` creating genuine graph-level duplicate facts, not just index
drift), #147 (eager startup backfill needed, plus a DB-lock leak the review for
it caught), #150 (batched index-writer commit failures needed the same
fault-isolation per-triple writes already had), #149 (numeric-valued triples
silently skipped during index auto-derivation), #148 (dead heuristic-fallback
code removed once #117/#118 made it unreachable), #151 (installer needed to
refresh a stale editable-install module mapping after the `fact_index` module was
added).

**Validation status:** #120 added `evals/at_scale/`, a repeatable in-process
benchmark tier measuring both git-ingestion performance and query
correctness+latency against real repo history (see `evals/at_scale/benchmark.md`).
Its current baseline is this repo's own history — 498 commits ingested in 78.87s —
which validates the harness and confirms no regressions, but is roughly two orders
of magnitude smaller than the 52,948-commit run that motivated this issue.
Re-running the benchmark against a comparably large real-world repo is the
natural next step to independently confirm production-viability at that scale;
it has not been done as of this update.

## Phase 6 — Observability and Trust for Automatic Memory

### Context

Turn-by-turn automatic memory injection and extraction (Phase 3) is the right direction for solving agent memory loss, but the pattern has well-known failure modes when deployed without observability:

1. **Opaque injection** — memory is silently retrieved and prepended to the prompt; the agent treats injected facts as context without knowing their provenance, making behavior non-reproducible and hard to debug.
2. **Extraction corrupts memory** — the post-turn extractor is itself an LLM call and can hallucinate. A misread sarcastic remark becomes a permanent fact that compounds across future turns.
3. **Latency and cost** — each turn triggers at minimum two additional LLM calls (prepare + finalize); without parallelism and model-tier selection this doubles per-turn cost.
4. **Trust and consent** — users may not know which parts of their messages are stored, or be able to inspect or correct the stored form.

The bi-temporal model partially addresses (2): wrong facts can be retracted without losing audit history, and point-in-time queries can recover the pre-corruption state. But structural observability tooling is still needed before this pattern is appropriate in any hosted or multi-user deployment.

### Proposed work

- **Injection trace logging** — for each `memory_prepare_turn` call, log which facts were retrieved, how they were ranked, and how they were formatted. Queryable via a `memory_audit_query` tool.
- **Extraction confidence tagging** — tag every auto-extracted datom with `{:source "heuristic"|"llm"|"agent", :confidence 0.0–1.0, :model "...", :turn N}`. Low-confidence datoms can be auto-flagged for review rather than silently committed.
- **Provisional extraction mode** — store extracted facts in a staging namespace (`:staged/...`) for a configurable number of turns before promoting to permanent; reversals during the staging window are low-cost.
- **Periodic correction pass** — a background task (or agent-invocable tool) that scans recent extractions for internal contradictions and flags them. Leverages the bi-temporal graph's ability to show the full history of an entity's values.
- **User-visible memory summary** — a `memory_summarize` tool that returns a human-readable summary of what is currently known about the session, queryable by topic, so users can spot and correct errors.
- **Scoped injection** — allow `memory_prepare_turn` to be restricted to specific namespaces or entity types (e.g. `:decision/...` only, not `:user-preference/...`) to limit the blast radius of bad extractions.

### When this matters

For a local single-user developer tool (the current Phase 5 target), stored data stays on the user's machine, the user can inspect the `.graph` file directly, and wrong facts can be corrected manually. The risk profile is manageable without this work. For any hosted or multi-tenant deployment the observability layer is a prerequisite. Phase 5 git ingestion clarified the actual observability pain points and this phase will be designed in detail after Phase 5 production use.

## Backlog (unscheduled)

- **Port Datalog grammar additions from minigraf 1.2.0 into `SKILL.md`** — minigraf#288 and its child issue #289 are fixed and will land in v1.2.0. That release adds two new Datalog query sections (`max-derived-facts-section` and `max-results-section`) that make the `ancestor` recursive rule usable on real repos. The inline Datalog reference in `SKILL.md` must be updated to document these sections once 1.2.0 is published. **Blocked on minigraf 1.2.0 release; no action until then.**

- `minigraf_ingest_docs` (experiment) — ingest plain text/markdown files from git history using the existing heuristic/llm/agent extraction strategies, with commit timestamps as `:valid-from`. Enables backdated decision entities from committed ADRs and design docs. Risk: duplication against conversation-extracted entities; quality depends on extraction strategy. Spec before building.
- WASM bindings (browser + edge) — no spec or concrete driver yet
- Mobile embedding — no spec or concrete driver yet
- **Embedding-based disambiguation** — add only when at least two of: (a) entity volume exceeds prompt injection limits, (b) cross-session resolution fails on canonical lookup, (c) free-text search is explicitly requested. Sub-points (a) and (c) were assumed to already be addressed by an in-memory BM25 index (`FactIndex` / `IndexCache`, `rank-bm25`) — #118 invalidated that for the primary deployment surface: the index lived in a per-process module-level singleton, but the `UserPromptSubmit` hook is a fresh short-lived process every turn, so the cache was always cold and `handle_memory_prepare_turn` returned empty. A persisted, mmap-able on-disk index is therefore a **prerequisite** for hook-side retrieval to function at all, not an optional embedding add-on — shipped via #118's SQLite FTS5 index (`fact_index.py`), which replaced the in-memory BM25 path (`FactIndex`/`IndexCache`, `rank-bm25`) entirely rather than sitting alongside it; see Phase 5.5. The remaining open question is (b): cross-session resolution failures where the user references an entity by description rather than ident (e.g. "Redis cache" → `:decision/redis`) — BM25 keyword overlap does not handle this; it would require semantic embedding. Add embedding support only if (b) becomes a demonstrated pain point. Preferred shape remains an embedded co-located index (`sqlite-vec` or `lancedb`), not a separate service.

---

## Marketplace Publishing ✓

Published as a GitHub-hosted Claude Code plugin. Users add the repo to `extraKnownMarketplaces` in `settings.json` — see README for instructions.

`install.py` installs the `minigraf` Python package via pip (`>=0.22.0`, which introduced the Python binding). Rust/`cargo` is no longer required. Supported platforms: Linux x86_64, Linux aarch64, macOS arm64, macOS x86_64, Windows.
