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
| 6 MCP tools | `vulcan_query`, `vulcan_transact`, `vulcan_retract`, `vulcan_report_issue`, `memory_prepare_turn`, `memory_finalize_turn` | Complete ✓ |
| Auto-memory hooks (Claude Code) | `UserPromptSubmit` injects context; `Stop` hook extracts facts — `hooks/prepare_hook.py`, `hooks/finalize_hook.py` | Complete ✓ |
| Hook config templates | `hooks/claude-code.json`, `hooks/codex.toml`, `hooks/hermes.yaml`, `hooks/opencode.json` (degraded), `hooks/openclaw.json` (degraded) | Complete ✓ |
| Heuristic extraction | Regex-based signal detection; zero API calls; no configuration required | Complete ✓ |
| LLM extraction | Claude Haiku extracts facts; falls back to agent strategy on API failure | Complete ✓ |
| Agent extraction | MCP sampling requests a memory block from the connected agent | Complete ✓ |
| Bi-temporal writes | `:valid-at` recorded on every write; point-in-time queries correct | Complete ✓ |

## Phase 3 — Finish Line

| Item | Description | Status |
|------|-------------|--------|
| OpenAI/Codex support | `VULCAN_LLM_MODEL=gpt-4o-mini` selects OpenAI client automatically; Codex hook wiring fully enabled. Spec: `docs/superpowers/specs/2026-05-26-openai-llm-strategy-design.md` | Pending |

## Phase 4 — Entity Normalization and Schema-Aware Extraction

Normalization is foundational to memory quality. Every subsequent phase adds more entities to the graph; without normalization they fragment into disconnected synonym clusters. This work precedes code structure ingestion (Phase 6) deliberately — landing normalization first keeps the data clean as volume scales.

### The problem

Without normalization the graph degrades into disconnected synonym clusters:
- Same entity, different names: "the auth service", "the login system", "the SSO module" → three separate entities, queries miss two of three.
- Same relationship, different predicates: `:depends-on` vs `:requires` vs `:uses`.
- Same decision, different phrasings: "use Redis", "use Redis for caching", "Redis-based cache" → split across unconnected datoms.

The bi-temporal model preserves *when* things were said but cannot help when the *what* is fragmented across synonyms.

### Why a vector store is the wrong first move

A vector store is a retrieval tool, not a normalization tool. It can find things *near* "auth service" in embedding space but cannot assert that "auth service" and "login system" are the *same* entity — "Redis" and "Memcached" are also near in embedding space. Fuzzy matching injected upstream of Datalog pattern queries also destroys reproducibility: the same query returns different results depending on embedding model version and similarity threshold.

### Three approaches in increasing complexity

**1. Canonicalization at write time (recommended first move).** The extractor is made schema-aware: before transacting, it receives a list of existing canonical entity names and attribute predicates (a cheap Datalog query: `[:find ?e ?n :where [?e :entity/canonical-name ?n]]`) and is instructed to reuse an existing entity ID where the reference is clear, or create a new canonical name if genuinely new. The bi-temporal model provides a safety net: a wrong merge can be retracted and re-asserted. This handles the large majority of normalization with no new infrastructure.

**2. Alias facts at query time.** Store user phrasings as alias facts — `(:service/auth :alias "the auth service")`, `(:service/auth :alias "login system")`. Queries resolve non-canonical names through the alias index before pattern matching. More forgiving than (1) because the canonical decision is not irrevocable at write time. These aliases are just more datoms in Minigraf; no new infrastructure.

**3. Embedding-based disambiguation as a fallback.** The vector store enters only when the extractor sees a reference it cannot confidently map to an existing entity or a confidently new one. It proposes candidates ("this looks 0.87 similar to `:service/auth`") and either the extractor or the user resolves the ambiguity. The vector store never directly answers Datalog queries; it is a disambiguation aid for the long tail.

In practice (1) + (2) handle 90%+ of cases. (3) catches the long tail. Starting with (3) before trying (1) is a common mistake.

### When a vector store becomes necessary

Wait until at least two of these are true:
- **Volume:** tens of thousands of entities, and schema-injection into the extractor prompt hits context limits.
- **Cross-session resolution:** the user references something from months ago that isn't in the extractor's prompt window and canonical lookup fails.
- **Free-text search:** users want to find facts by approximate description, not exact name ("show me anything related to performance"). This is a feature request, not a normalization problem, but a vector store solves it.

### Shape of the vector store if added

Embedded, co-located with the `.graph` file — `sqlite-vec`, `lancedb`, or a flat index. Not a separate service. Adding Pinecone or Qdrant to a personal-scale local tool would be architecturally inconsistent with the single-file embedded model. For mobile in particular, a separate vector store means more storage, memory, startup time, and battery.

### Implementation note

The normalization problem is fundamentally an *ontology problem*, not a tooling problem. "Are 'the auth service' and 'the login system' the same thing?" depends on project-specific context that only the extractor has (conversation context + existing graph schema). The correct architectural move is to make the extractor schema-aware and treat entity resolution as part of extraction, not a separate post-processing step.

## Phase 5 — Observability and Trust for Automatic Memory

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

## Phase 6 — Code Structure Evolution from Git History

Extend the bi-temporal graph to store code structure extracted from git history, enabling temporal queries over how a codebase evolved and why. By the time this lands, normalization (Phase 4) and observability (Phase 5) are already in place — ingested structural entities resolve against the canonical schema, and confidence tagging applies to auto-ingested edges.

### Why Not Just Read Git History?

An agent with git access can already answer simple temporal questions: `git show <commit>:ROADMAP.md`, `git diff v1..v2`, `git blame`. For small projects with short histories, that is often enough.

This project adds value where git structurally cannot help:

**Cross-cutting semantic queries.** Git is organized by commit — time slices of the whole repo. To answer "when did module A first depend on module B?", an agent must check out every commit, parse the code at each one, and scan. That is O(commits × parse time) and blows the context window on any real codebase. The graph inverts the index — facts are stored by entity, so you query forward from the entity directly.

**Semantic structure survives text changes.** Git sees line diffs. It does not know a function moved from `auth.py` to `middleware/auth.py` — it sees a deletion and an addition. `git blame` breaks on renames. The graph stores `:calls` and `:depends-on` edges that are entity-addressed, not file-addressed. Refactors do not break the history.

**Agent-authored facts do not exist in git.** Decisions, constraints, and observations that an agent logged but never committed to a file have no representation in git. The graph is the only place where these coexist with code structure as queryable facts.

**Cross-layer joins.** There is no way to ask git: "list all dependency changes that happened after the decision to switch databases." The decision lives in agent memory; the structural change lives in git; they are in separate systems with no shared query surface. In the graph, both are datoms and a single Datalog join connects them.

The graph is built *from* git but answers queries git cannot express. Value scales with history length, structural complexity, and frequency of cross-cutting or cross-layer queries.

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

## Backlog (unscheduled)

- WASM bindings (browser + edge) — no spec or concrete driver yet
- Mobile embedding — no spec or concrete driver yet

---

## Marketplace Publishing ✓

Published as a GitHub-hosted Claude Code plugin. Users add the repo to `extraKnownMarketplaces` in `settings.json` — see README for instructions.

Pre-built binary support landed in minigraf v0.19.0 (2026-04-14), removing the `cargo`/Rust installation barrier. `install.py` now downloads the correct binary automatically for Linux x86_64, Linux aarch64, macOS arm64, macOS x86_64, and Windows. Skill description reframed to lead with user benefit.
