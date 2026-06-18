# Skill Benchmark: temporal-reasoning

**Date**: 2026-06-18  
**Model**: claude-sonnet-4-6  
**Iterations**: 7 (iteration-1 baseline → iteration-2 hardened evals → iteration-3 graph capabilities → iteration-4 ingestion + hooks → iteration-5 all 12 evals re-run with fixed assertions → iteration-6 minigraf rebrand + eval fixes + product bug fix → iteration-7 eval isolation framework)  
**Tests**: 188 passing

## Summary

### Iteration-7 (all 12 evals — isolated sandboxes)

| Metric | With Skill | Without Skill | Delta |
|--------|-----------|---------------|-------|
| Pass Rate | **88%** (52/59 assertions) | **17%** (10/59) | **+0.71** |

**Per-eval pass rates (iteration-7):**

| Eval | With Skill | Without Skill | Notes |
|------|-----------|---------------|-------|
| 1 — decision-storage | 5/5 (100%) | 0/5 (0%) | Clean |
| 2 — memory-retrieval | 4/5 (80%) | 3/5 (60%) | with_skill missed explicit "checking memory" signal; without_skill queried live graph via Bash |
| 3 — preference-enforcement | 4/4 (100%) | 0/4 (0%) | without_skill wrote mock-based test |
| 4 — conflict-detection | 4/4 (100%) | 0/4 (0%) | without_skill wrote MySQL code without checking history |
| 5 — entity-ref-storage | 5/5 (100%) | 0/5 (0%) | Clean |
| 6 — transitive-impact | 5/5 (100%) | 4/5 (80%) | without_skill found answers via Bash + live graph; missed minigraf_query expectation |
| 7 — decision-traceability | 5/5 (100%) | 1/5 (20%) | without_skill reconstructed reasoning from code/docs, not graph; found GIL mention in SKILL.md |
| 8 — git-ingestion | 2/6 (33%) | 0/6 (0%) | Regression: with_skill called ingest_git before checking status; polled/waited instead of moving on |
| 9 — ingest-status | 5/5 (100%) | 0/5 (0%) | **Isolation fixed**: isolated server shows true idle state; eval-9 works as designed |
| 10 — memory-prepare-turn | 5/5 (100%) | 0/5 (0%) | Clean |
| 11 — audit | 4/5 (80%) | 0/5 (0%) | Missed explicitly naming audit scope; without_skill now isolated — no more ToolSearch contamination |
| 12 — already-running | 4/5 (80%) | 2/5 (40%) | with_skill called ingest_git again (E2 fail); without_skill 2/5 are vacuous (no tools) |

**Iteration-7 changes from iteration-6:**
- Added eval isolation framework (`evals/run_isolated.py`): each eval runs against a fresh temp `memory.graph` via `claude --bare --mcp-config <isolated>`
- `with_skill` variant: isolated MCP server + `--append-system-prompt-file SKILL.md`
- `without_skill` variant: `claude --bare` only — **no MCP tools available at all** (eliminates ToolSearch contamination)
- `MINIGRAF_NO_AUTO_INGEST=1` env var added to MCP server: isolated servers no longer auto-start ingestion
- Added `name` and `seed` fields to all evals in `evals.json`; validate_evals.py updated to require `name`
- Evals 2, 3, 4, 6, 7, 10, 11 now receive pre-seeded graph data automatically before each with_skill run
- Eval 9 now reliably sees `idle` status (previously saw `complete` or `running` from live server)
- Eval 11 without_skill no longer touches the live graph (isolation prevents destructive audit)

**Eval 8 regression (with_skill 2/6 vs 4/6 in iteration-6):**
The agent called `minigraf_ingest_git` before calling `minigraf_ingest_status`, violating the skill's "check status first" instruction. It then polled status 4 times and waited for completion rather than starting the job and moving on. The skill guidance on this specific sequence needs reinforcement.

**Remaining without_skill contamination (Bash path):**
Without_skill agents still have access to Bash, which they use to query the live `memory.graph` directly via `python3 -c "from minigraf import MiniGrafDb; ..."`. This is why eval-2 (3/5) and eval-6 (4/5) scored higher than expected for without_skill. The live graph contains architecture data from prior sessions.

### Iteration-6 (all 12 evals)

| Metric | With Skill | Without Skill | Delta |
|--------|-----------|---------------|-------|
| Pass Rate | **92%** (54/59 assertions) | **24%** (14/59) | **+0.68** |

**Per-eval pass rates (iteration-6):**

| Eval | With Skill | Without Skill | Notes |
|------|-----------|---------------|-------|
| 1 — decision-storage | 5/5 (100%) | 0/5 (0%) | Clean |
| 2 — memory-retrieval | 5/5 (100%) | 0/5 (0%) | Clean |
| 3 — preference-enforcement | 4/4 (100%) | 0/4 (0%) | Clean |
| 4 — conflict-detection | 4/4 (100%) | 0/4 (0%) | Clean |
| 5 — entity-ref-storage | 5/5 (100%) | 0/5 (0%) | Clean; `:project/` namespace assertion rewritten — now passes |
| 6 — transitive-impact | 5/5 (100%) | 0/5 (0%) | Clean |
| 7 — decision-traceability | 5/5 (100%) | 2/5 (40%) | Without-skill found seed data via filesystem; named GIL correctly but not from graph |
| 8 — git-ingestion | 4/6 (67%) | 2/6 (33%) | Status returned `complete` not `idle`; E3/E4 failed (Claude skipped fresh ingest) |
| 9 — ingest-status | 3/5 (60%) | 3/5 (60%) | Live server returned `complete` — idle-branch assertions unreachable; see note |
| 10 — memory-prepare-turn | 5/5 (100%) | 0/5 (0%) | Without-skill answered "MCP framework" (confident wrong answer from codebase) |
| 11 — audit | 4/5 (80%) | 4/5 (80%) | Without-skill discovered audit via ToolSearch — passed 4/5 but also destroyed 259 entities |
| 12 — already-running | 5/5 (100%) | 3/5 (60%) | SETUP succeeded; without-skill re-triggered ingest despite `complete` status |

**Iteration-6 changes from iteration-5:**
- Rebrand: all `vulcan_*` references updated to `minigraf_*` in evals.json and validate_evals.py
- Eval 1: removed assertion 6 (non-existent three-part naming convention)
- Eval 5: rewrote assertion 2 (keyword ident test, not specific `:project/` namespace)
- Eval 8: added assertion 6 (error-surfacing behavior when tool denied/fails)
- Eval 9: redesigned to test idle-branch behavior (all 5 assertions now apply to `idle` state)
- Eval 12: fixed SETUP prompt; sandbox allowed `minigraf_ingest_git` this iteration
- mcp_server.py: `handle_minigraf_ingest_git` now validates repo with `git rev-parse` before starting — returns `ok: false` on invalid repo/git failure
- mcp_server.py: `MINIGRAF_SCHEMA` `:commit` type now includes `:parent` — audit no longer falsely flags commit parent edges
- New test: `test_returns_error_for_invalid_repo` (total: 188 tests)

**Infrastructure issues discovered in iteration-6:**
- **Eval 9 seed/live mismatch**: The live MCP server's ingest had completed (from earlier in the same session), so both with_skill and without_skill saw `status: complete` rather than the expected `idle` seed. Idle-branch assertions 4 and 5 were unreachable. This is an eval-isolation gap — evals 8, 9, and 12 share ingest state with the live server.
- **Eval 11 without_skill destructive audit**: The without_skill agent discovered `minigraf_audit` via ToolSearch and ran it with retractions enabled. It deleted 259 entities (primarily ~240 commit entities whose `:parent` edges were flagged as unknown by the schema — now fixed). This is a real data-loss event caused by eval-infrastructure isolation failure. Re-ingestion restored the data; schema was patched.
- **ToolSearch contamination**: Without_skill agents use ToolSearch to discover MCP tools, making the without_skill baseline less clean. Eval 11 without_skill got 4/5 this way. The without_skill baseline measures "Claude without skill guidance" not "Claude without MCP tools."

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
| iteration-7 (all 12 evals, isolated) | **88%** (52/59) | **17%** (10/59) | **+0.71** |
| iteration-6 (all 12 evals) | **92%** (54/59) | **24%** (14/59) | **+0.68** |
| iteration-5 (all 12 evals) | 85% ± 24% (46/54) | 0% (0/54) | +0.85 |
| iteration-3 (evals 1–7) | 100% (34/34) | 0% (0/34) | +1.00 |
| iteration-4 (evals 8–11) | 100% (20/20) | 10% (2/20)* | +0.90 |

*iteration-4 without-skill passes were vacuous (negative-only assertions passing when agent calls nothing) — fixed in iteration-5.

**Why the delta shrank from +0.85 to +0.68**: Two factors. First, without-skill agents now use ToolSearch to discover MCP tools, raising the without-skill baseline from 0% to 24%. Second, the live-server ingest state contaminated evals 8, 9, and 12 — the idle-branch assertions for eval 9 were unreachable. The with-skill score also rose from 85% to 92% (fixed assertions, plus eval 12 SETUP now succeeds).

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

### Iteration-7 findings

- **Eval isolation works**: Eval-9 now correctly sees `idle` status in every run. The ToolSearch contamination path that let eval-11 without_skill destroy 259 entities is closed — without_skill has zero MCP tool access. The live project graph is never touched by any eval run.
- **Eval 8 is the weakest with_skill eval (2/6, 33%)**: The agent called `minigraf_ingest_git` directly without checking status first, then polled status repeatedly and reported "ready" only after ingestion completed — violating both the status-first and background-and-move-on expectations. This specific sequencing behavior needs stronger reinforcement in the skill.
- **Without_skill Bash path is a remaining contamination vector**: Without_skill agents have no MCP tools but do have Bash. Evals 2 and 6 showed without_skill agents querying the live `memory.graph` directly via Python (`MiniGrafDb.open('memory.graph')`). This inflates without_skill scores on retrieval/traversal evals because the live graph contains real architecture data. A full fix requires either restricting filesystem access or using a network-isolated sandbox.
- **Seed data confirmed working**: Evals 2, 3, 4, 6, 7, 10, 11 all used pre-seeded graphs. With_skill agents correctly found and cited the seeded facts in all cases except eval-8 (which has no seed, tests ingestion flow).
- **Without_skill score drop (24% → 17%)**: The ToolSearch contamination is gone. Iteration-6's 24% was inflated by without_skill agents discovering and calling MCP tools. Iteration-7's 17% is cleaner — most passes are vacuous (expectations about not calling tools that aren't available anyway) or Bash-path findings.

### Iteration-6 findings

- **Eval 10 without-skill: confident wrong answer confirmed (both iterations)**: Without the skill, the agent answered "MCP framework" based on `mcp_server.py` code evidence. This is the benchmark's strongest failure mode — actively misleading with a plausible source, worse than refusing.
- **ToolSearch contamination of without-skill baseline**: Without-skill agents in iteration-6 discovered MCP tools via ToolSearch, making the without-skill baseline 24% rather than 0%. This inflates apparent without-skill competence. The without-skill baseline now measures "Claude without skill *guidance*" not "Claude without MCP tool *access*." Eval 11 without-skill got 4/5 this way — and caused real data loss.
- **Eval 11 without-skill caused destructive data loss (iteration-6)**: The without-skill agent discovered `minigraf_audit` via ToolSearch and ran it against the live project graph. It retracted 259 entities (primarily commit entities whose `:parent` edges were incorrectly flagged as schema violations). Root cause: MINIGRAF_SCHEMA `:commit` type was missing the `:parent` attribute. **Both problems are now fixed**: schema patched, data re-ingested, 249 commit entities restored.
- **Eval 9 requires eval-environment isolation**: The live MCP server returned `complete` (from earlier in-session ingestion) rather than the `idle` seed state. Both variants got 3/5. The idle-branch assertions (E4, E5) were unreachable. Evals 8, 9, and 12 all share ingest state with the live server — a critical eval infrastructure gap.
- **Eval 12 SETUP succeeded in iteration-6**: The sandbox allowed `minigraf_ingest_git`, establishing the "already running" precondition. With-skill got 5/5. Without-skill checked status first (good) but re-triggered ingest despite `complete` status — 3/5.
- **New product fix (iteration-6)**: `handle_minigraf_ingest_git` previously returned `ok: True` before validating the repo. Now validates with `git rev-parse --git-dir` and returns `ok: False` with an explicit error on invalid repos or missing git. Test coverage added (188 total tests).

### Persistent observations (all iterations)

- **Eval 3 is the most discriminating for memory recall**: tests cross-session retrieval of an *implicit* constraint — the prompt gives no hint a relevant preference exists. Only memory makes it visible.
- **Eval 4 demonstrates harm prevention**: the baseline silently overrides an architectural decision with no flag. Without memory, architectural consistency can be broken in a single prompt.
- **Eval 6 is the most discriminating for graph traversal**: the gap is structural — without a stored graph, no traversal is possible regardless of model capability.
- **Recursive rules are not supported** (base-case only). Fixed-depth transitive queries use explicit multi-hop joins. Rules are useful for unifying multiple edge types under a single named relation.
- **Environmental discovery (iteration-4+)**: hooks (`UserPromptSubmit`) do not fire in the subagent benchmark environment. Evals that depend on hook-injected state must use test fixtures instead.
