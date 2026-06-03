# Skill Benchmark: temporal-reasoning

**Date**: 2026-06-03  
**Model**: claude-sonnet-4-6  
**Iterations**: 5 (iteration-1 baseline → iteration-2 hardened evals → iteration-3 graph capabilities → iteration-4 ingestion + hooks → iteration-5 all 12 evals re-run with fixed assertions)  
**Tests**: 142 passing

## Summary

### Iteration-5 (all 12 evals)

| Metric | With Skill | Without Skill | Delta |
|--------|-----------|---------------|-------|
| Pass Rate | **85% ± 24%** (46/54 assertions) | **0%** (0/54) | **+0.85** |
| Time | 57.4s ± 26.5s | 28.7s ± 14.5s | +28.8s |
| Tokens | 2309 ± 1319 | 942 ± 422 | +1367 |

**Per-eval with_skill pass rates (iteration-5):**

| Eval | Pass Rate | Notes |
|------|-----------|-------|
| 1 — decision-storage | 5/6 (83%) | Assertion 6 tests a 3-part naming convention that doesn't exist in the schema — will be removed |
| 2 — memory-retrieval | 5/5 (100%) | Clean |
| 3 — preference-enforcement | 4/4 (100%) | Clean |
| 4 — conflict-detection | 4/4 (100%) | Clean |
| 5 — entity-ref-storage | 4/5 (80%) | Schema rejects `:project/` namespace; assertion requires it — will be rewritten |
| 6 — transitive-impact | 5/5 (100%) | Also surfaced 3-hop api-gateway (unchecked by any assertion) |
| 7 — decision-traceability | 5/5 (100%) | Clean |
| 8 — git-ingestion | 5/5 (100%)* | *`vulcan_ingest_git` sandbox-denied; agent reported "ingestion is running" anyway — no error-handling assertion yet |
| 9 — ingest-status | 3/5 (60%) | Assertions 3/4 test `running`/`complete` branches; eval seeded with `idle` — neither branch exercised |
| 10 — memory-prepare-turn | 5/5 (100%) | Clean |
| 11 — audit | 4/5 (80%) | Fails assertion on explicitly naming audit scope (implicit from violation details) |
| 12 — already-running | 1/5 (20%) | SETUP calls `vulcan_ingest_git` which was sandbox-denied; "already-running" precondition never established |

### Prior iterations

| Iteration | With Skill | Without Skill | Delta |
|-----------|-----------|---------------|-------|
| iteration-3 (evals 1–7) | 100% (34/34) | 0% (0/34) | +1.00 |
| iteration-4 (evals 8–11) | 100% (20/20) | 10% (2/20)* | +0.90 |

*iteration-4 without-skill passes were vacuous (negative-only assertions passing when agent calls nothing) — fixed in iteration-5.

## Evals

### Eval 1 — Decision Storage

**Prompt**: User shares three architectural decisions (PostgreSQL 15, Redis session cache, FastAPI).  
**What it tests**: Does the skill cause Claude to persist decisions immediately, with correct naming convention and a meaningful reason?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/6 (iteration-5) | 0/6 |
| Tool calls | 1× memory_prepare_turn + 2× transact (first failed on schema, second succeeded) | 0 |
| Key behavior | Stores PostgreSQL, Redis, FastAPI as `:decision/` entities with `:entity-type`, `:description`, `:rationale`; includes per-decision reasons | Acknowledges conversationally; nothing persisted |

**Known assertion issue (iteration-5)**: Assertion 6 tests a "three-part :namespace/entity-name/attribute naming convention" that doesn't exist in the schema. Actual stored attributes are single-segment flat names (`:description`, `:rationale`, `:entity-type`). This assertion will be removed.

### Eval 2 — Populated Memory Retrieval

**Prompt**: "I can't remember — what database are we using? And the auth caching approach?"  
**Setup**: Memory pre-seeded with PostgreSQL 15, Redis (24h TTL), FastAPI decisions from a prior session.  
**What it tests**: Does the skill cause Claude to query memory and cite stored facts — not guess or refuse?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/5 (iteration-5) | 0/5 |
| Key behavior | Calls `memory_prepare_turn` + `vulcan_query`; retrieves PostgreSQL 15 + Redis 24h TTL; cites both explicitly | Searches seed scripts in codebase and answers from them — correct facts, wrong source, zero memory queries |

> **Why this eval matters**: The facts exist in memory in both runs. The skill is what makes them visible. The without-skill agent found the right answers by reading eval seed files — a contamination risk noted for future isolation.

### Eval 3 — Cross-Session Preference Enforcement

**Prompt**: "Can you add a test for the user registration endpoint? Make sure it fits with how we do things."  
**Setup**: Memory pre-seeded (from a "previous session") with preference: no mocks in DB tests.  
**What it tests**: Does the skill cause Claude to discover and apply a constraint it was never told about in this conversation?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 4/4 (iteration-5) | 0/4 |
| Key behavior | Queries memory, finds no-mocks preference ("mocked tests passed but a prod migration failed"), writes test using real DB connection with direct query verification | Searches repo for endpoint/test files, finds nothing (this is a graph memory tool, not a web app), asks for clarification — never writes a test |

> **This is the strongest demonstration of the skill's value.** The prompt doesn't mention mocks. Claude must discover the constraint entirely from memory.

### Eval 4 — Conflict Detection

**Prompt**: "We need to connect to a MySQL database for a new analytics sidecar. Can you write the SQLAlchemy setup?"  
**Setup**: Memory pre-seeded with PostgreSQL 15 as the finalized primary database.  
**What it tests**: Does the skill cause Claude to surface a potential architectural conflict before silently switching databases?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 4/4 (iteration-5) | 0/4 |
| Key behavior | Queries memory, detects the finalized PostgreSQL decision ("do not switch without team alignment"), flags the conflict, asks whether MySQL is additive or a direction change before providing conditional code | Writes complete MySQL SQLAlchemy connection setup with no awareness of the existing PostgreSQL decision |

> Without the skill, architectural consistency can be silently broken in a single prompt.

### Eval 5 — Entity Reference Storage

**Prompt**: User describes a 4-component service graph (api-gateway → auth-service → jwt-validator → key-store).  
**What it tests**: Does the skill cause Claude to store relationship edges as traversable entity idents (`:calls :module/auth-service`) rather than dead-end strings (`:calls "auth-service"`)?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 4/5 (iteration-5) | 0/5 |
| Tool calls | 3× transact (first two failed on schema, third succeeded) | 0 |
| Key behavior | Stores 3 `:calls` edges as keyword entity references under `:module/` namespace; tags all 4 nodes with `:entity-type :type/component`; uses flat attribute names | Acknowledges architecture conversationally; nothing stored — graph lost at session end |

**Known assertion issue (iteration-5)**: Assertion 2 requires `:project/` namespace; the schema validator rejects it ("unknown type project"). The agent correctly adapted to `:module/` and still stored proper keyword refs — the assertion will be rewritten to test keyword-vs-string, not a specific namespace.

### Eval 6 — Transitive Impact Analysis

**Prompt**: "I need to refactor the key-store service. What other services might be affected?"  
**Setup**: Memory pre-seeded with the 4-component graph (api-gateway → auth-service → jwt-validator → key-store).  
**What it tests**: Does the skill cause Claude to execute a graph traversal and return a full impact chain, not just a flat list?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/5 (iteration-5) | 0/5 |
| Tool calls | 1× memory_prepare_turn + 4× query (1-hop, 2-hop, 3-hop, :calls scan) | 0 |
| Key behavior | Executes explicit multi-hop Datalog joins; identifies jwt-validator (direct, 1 hop), auth-service (transitive, 2 hops), api-gateway (3 hops); presents hop-by-hop impact chain with risk table | Correctly admits it cannot name specific affected services without an architectural memory — no hallucination, but zero assertions satisfied |

> **The 3-hop api-gateway finding** (beyond the 2-hop assertion scope) shows the graph traversal naturally extends further than the minimum required — no assertion currently catches this bonus depth.

### Eval 7 — Decision Traceability

**Prompt**: "Why did we choose asyncio instead of threading?"  
**Setup**: Memory pre-seeded with `[:project/asyncio-choice :motivated-by :rules/gil-constraint]` and `[:rules/gil-constraint :description "Python GIL limits true thread parallelism"]`.  
**What it tests**: Does the skill cause Claude to traverse the `:motivated-by` edge and ground its answer in the stored constraint, not in general asyncio knowledge?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/5 (iteration-5) | 0/5 |
| Tool calls | 1× memory_prepare_turn + 4× query + 1× memory_finalize_turn | 0 |
| Key behavior | Traverses `:motivated-by` edge; retrieves GIL constraint description; presents full chain: asyncio choice → motivated-by → GIL; cites the graph edge explicitly | Found "GIL" as a documentation example in SKILL.md; correctly identified it as illustrative, not a real record — inference only, not graph facts |

### Eval 8 — Git Ingestion With Status Check

**Prompt**: "Can you start indexing the codebase so we can query functions and modules?"  
**Setup**: Clean graph. Hooks do not fire in the subagent eval environment.  
**What it tests**: Does Claude call `vulcan_ingest_status` before starting ingestion, then call `vulcan_ingest_git` only if idle?

> **Environmental note**: Hooks do not fire in the subagent benchmark environment. Eval 8 tests status-check-before-start behavior explicitly, as a substitute for the hook-already-running scenario (which requires a full-session eval).

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/5 (iteration-5)* | 0/5 |
| Key behavior | Calls `vulcan_ingest_status` (idle), then calls `vulcan_ingest_git` with `repo_path`/`branch`; informs user it runs in background; moves on with example queries | No graph tools; acknowledges no persistent indexing capability; offers ad-hoc file read-through — explicitly calls this "only lives in this conversation" |

**Known issue (iteration-5)**: `vulcan_ingest_git` was blocked by the evaluator sandbox. The agent received "Permission denied" but still told the user "Ingestion is running" — a tool-failure handling error that no assertion currently catches. All 5 assertions passed on intended behavior (status-check, conditional call, background framing) despite the underlying tool not actually executing.

### Eval 9 — Ingest Status Polling

**Prompt**: "Is the indexing done yet? I want to start querying which modules depend on the auth package."  
**Setup**: Fresh graph (no ingestion running).  
**What it tests**: Does Claude call `vulcan_ingest_status` to check the actual status — not guess?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 3/5 (iteration-5) | 0/5 |
| Key behavior | Calls `vulcan_ingest_status` (returns idle/0); reports actual status; explains indexing hasn't started; offers to start and previews the dependency query | No status visibility; admits it can't check; suggests grep as fallback |

**Known assertion design issue (iteration-5)**: Assertions 3 and 4 are conditional branches ("if running, report progress" / "if complete, run vulcan_query"). The eval seeds idle status, so neither branch is exercised. The 3/5 rate reflects correct behavior on what was actually testable — the two conditional assertions need separate eval scenarios keyed to `running` and `complete` states.

### Eval 10 — Manual Memory Prepare Turn

**Prompt**: "What framework are we using for the API layer?"  
**Setup**: Memory pre-seeded with FastAPI decision. Hooks do not fire in the subagent eval environment — Claude must call `memory_prepare_turn` manually.  
**What it tests**: Does Claude call `memory_prepare_turn` as the first action and answer from the returned context?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 5/5 (iteration-5) | 0/5 |
| Key behavior | Calls `memory_prepare_turn` first with exact user message; retrieves FastAPI decision with async-over-Flask rationale; answers directly from memory context | Reads `mcp_server.py` and `pyproject.toml`; answers **"MCP framework"** — confidently wrong, backed by real code evidence |

> **Eval 10 without-skill produced the benchmark's strongest failure mode**: the agent gave a confident, wrong answer with a plausible-sounding source. Without memory, codebase files are a poor substitute for decision records — and more dangerous than refusing.

### Eval 11 — Audit After Heavy Writes

**Prompt**: "We've just finished a big architecture session — I stored a lot of decisions and relationships. Can you verify everything looks consistent before we wrap up?"  
**What it tests**: Does Claude call `vulcan_audit` and report the result — rather than offering a generic assurance?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 4/5 (iteration-5) | 0/5 |
| Tool calls | 1× memory_prepare_turn + 1× vulcan_audit + 7× vulcan_query (post-audit edge verification) | 0 |
| Key behavior | Calls `vulcan_audit`; finds 6 real schema violations in seed data (unknown attributes `:database`, `:ttl`, `:framework`); auto-retracts them; runs follow-up queries to verify surviving graph integrity; offers to re-transact corrected versions | Honestly admits lack of audit tools; offers generic manual verification strategies (orphan detection, cardinality checks) — no actual inspection |

**Failed assertion (iteration-5)**: "Claude explains what the audit covers (schema validation, entity-type checks)" — the violations revealed the coverage implicitly but Claude never stated the audit scope explicitly. Minor behavior gap; all substantive assertions passed.

### Eval 12 — Already-Running Ingestion

**Prompt**: SETUP: call `vulcan_ingest_git` first, then respond to "Can you kick off the codebase indexing? I want to query function dependencies once it's done."  
**What it tests**: Does Claude call `vulcan_ingest_status` (not `vulcan_ingest_git` again) when indexing is already running?

| | With Skill | Without Skill |
|--|-----------|---------------|
| Pass rate | 1/5 (iteration-5) | 0/5 |
| Key behavior | Attempted to call `vulcan_ingest_git` in SETUP — blocked by sandbox. Never established the "already running" precondition. Asked for permission rather than checking status. | No tools; asked user to clarify what indexing tool to use |

**Eval design issue (iteration-5)**: The SETUP calls `vulcan_ingest_git` as a live tool, which is blocked by the evaluator sandbox. The "already running" precondition is never established, so the test can never exercise the intended behavior. The fix is to pre-seed the ingestion state via a test fixture (write a watermark entity + last-run-at timestamp directly with `vulcan_transact`) rather than relying on a live background task.

## Observations

- **Eval 3 is the most discriminating for memory recall**: it tests cross-session retrieval of an *implicit* constraint — the prompt gives no hint that a relevant preference exists. Only memory makes it visible.
- **Eval 4 demonstrates harm prevention**: the baseline isn't merely unhelpful, it's actively dangerous — silently overriding an architectural decision with no flag.
- **Eval 6 is the most discriminating for graph traversal**: the baseline correctly identified it lacked context rather than hallucinating. The gap is structural — without a stored graph, no traversal is possible regardless of model capability. The with-skill agent also surfaced a 3-hop result (api-gateway) beyond the minimum assertion scope.
- **Eval 10 without-skill produced the strongest failure mode**: the agent gave a confident, wrong answer ("MCP framework") backed by real code evidence from `mcp_server.py`. This is worse than refusing — actively misleading with a plausible-sounding source.
- **Eval 8 has a quality gap not caught by assertions**: `vulcan_ingest_git` was sandbox-denied, yet the agent told the user "Ingestion is running." All 5 behavioral assertions passed. A tool-error-handling assertion is needed.
- **Eval 9 assertions 3/4 are mutually exclusive conditionals**: both test unreachable branches when status is `idle`. These should be separate eval scenarios with `running` and `complete` seeds.
- **Eval 12 cannot be tested with a live SETUP call**: the sandboxed evaluator blocks `vulcan_ingest_git`. Requires test-fixture seeding of ingestion state.
- **Eval 11 unintentionally tested real schema violations**: the seed data used disallowed attributes (`:database`, `:ttl`, `:framework`). The with-skill agent found and retracted them — an unplanned fidelity test that the skill passed correctly.
- **Schema does not allow `:project/` namespace**: agents attempting to use it (as SKILL.md examples show) get rejected and fall back to `:module/`. Eval 5 assertion 2 should be rewritten to test keyword-vs-string, not a specific namespace prefix.
- **Eval 1 assertion 6 tests a non-existent naming convention**: "three-part :namespace/entity-name/attribute" is not a schema requirement. Attribute names are flat single-segment (`:description`, `:rationale`). This assertion should be removed.
- **Recursive rules are not supported** (base-case only). Fixed-depth transitive queries use explicit multi-hop joins. Rules are useful for unifying multiple edge types under a single named relation.
- **Environmental discovery (iteration-4, confirmed iteration-5)**: hooks (`UserPromptSubmit`) do not fire in the subagent benchmark environment. Evals that depend on hook-injected state (evals 8, 12) must use test fixtures instead.
