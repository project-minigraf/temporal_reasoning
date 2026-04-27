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

## Future Phase 3+
- WASM bindings (browser + edge)
- Mobile embedding
- Claude Code MCP integration
- Codex/OpenAI adapters

## Why Not Just Read Git History?

An agent with git access can already answer simple temporal questions: `git show <commit>:ROADMAP.md`, `git diff v1..v2`, `git blame`. For small projects with short histories, that is often enough.

This project adds value where git structurally cannot help:

**Cross-cutting semantic queries.** Git is organized by commit — time slices of the whole repo. To answer "when did module A first depend on module B?", an agent must check out every commit, parse the code at each one, and scan. That is O(commits × parse time) and blows the context window on any real codebase. The graph inverts the index — facts are stored by entity, so you query forward from the entity directly.

**Semantic structure survives text changes.** Git sees line diffs. It does not know a function moved from `auth.py` to `middleware/auth.py` — it sees a deletion and an addition. `git blame` breaks on renames. The graph stores `:calls` and `:depends-on` edges that are entity-addressed, not file-addressed. Refactors do not break the history.

**Agent-authored facts do not exist in git.** Decisions, constraints, and observations that an agent logged but never committed to a file have no representation in git. The graph is the only place where these coexist with code structure as queryable facts.

**Cross-layer joins.** There is no way to ask git: "list all dependency changes that happened after the decision to switch databases." The decision lives in agent memory; the structural change lives in git; they are in separate systems with no shared query surface. In the graph, both are datoms and a single Datalog join connects them.

The graph is built *from* git but answers queries git cannot express. Value scales with history length, structural complexity, and frequency of cross-cutting or cross-layer queries.

## Future Phase 4+ — Code Structure Evolution

Extend the bi-temporal graph to store code structure extracted from git history, enabling temporal queries over how a codebase evolved and why.

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

## Future Phase 5+ — Observability and Trust for Automatic Memory

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

For a local single-user developer tool (the current Phase 3 target), stored data stays on the user's machine, the user can inspect the `.graph` file directly, and wrong facts can be corrected manually. The risk profile is manageable without this work. For any hosted or multi-tenant deployment the observability layer is a prerequisite.

---

## Marketplace Publishing ✓

Published as a GitHub-hosted Claude Code plugin. Users add the repo to `extraKnownMarketplaces` in `settings.json` — see README for instructions.

Pre-built binary support landed in minigraf v0.19.0 (2026-04-14), removing the `cargo`/Rust installation barrier. `install.py` now downloads the correct binary automatically for Linux x86_64, Linux aarch64, macOS arm64, macOS x86_64, and Windows. Skill description reframed to lead with user benefit.
