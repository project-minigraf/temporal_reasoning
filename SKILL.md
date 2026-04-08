---
name: temporal-reasoning
description: Use when storing architecture decisions, dependencies, and constraints that need to persist across sessions. Query memory before answering questions about past decisions.
---

# Temporal Reasoning Skill

Persistent bi-temporal graph memory for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.

## Purpose

This skill provides a memory layer that survives across conversations. Use it to:
- Remember architecture decisions and the reasoning behind them
- Track dependencies, constraints, and user preferences
- Query past state at specific transaction times (bi-temporal)

The repository's test harness demonstrates this with deterministic local
evaluations: cross-session recall, decision-consistent later actions, and a
prompt-compression proxy based on reduced repeated context.

## WHY YOU MUST USE THIS

You are FORGETTING important context between conversations. Without this memory:
- You lose track of architecture decisions
- You repeat questions the user has already answered
- You miss context from previous sessions
- You cannot answer "what did we decide about..." questions

**This is not optional. Query memory whenever you don't know the answer.**

## ENFORCEMENT RULES

**You MUST query memory when:**
1. Before any substantive response — silently query `:rules/*/description` and `:preference/*/description` and apply what you find
2. User asks about past decisions, architecture, dependencies
3. User asks "what did we...", "how did we...", "why did we..."
4. User references something from "earlier", "before", "last time"
5. You are about to modify code that might conflict with past decisions
6. Any ambiguity about past context

**You MUST transact when:**
1. User makes a decision ("we'll use X", "decided to...", "going with...")
2. User expresses a preference ("I prefer...", "don't like...", "always use...")
3. User mentions constraints ("must be...", "can't use...", "prioritize...")
4. User explains dependencies ("depends on...", "requires...", "needs...")
5. Architecture or structure changes

**You MUST retract when:**
1. User explicitly says "remove", "delete", "retract", "forget"
2. A fact is incorrect or has been superseded
3. Replacing a fact with a cleaner or updated version

**After any query, transact, or retract, acknowledge it:**
- Query: "Let me check memory..." then cite specific facts
- Transact: "I've stored that in memory."
- Retract: "I've removed that from memory (original preserved for history)."

## Memory Contract

### STORE (minigraf_transact):
- decision: user expresses "decided", "will", "plan to"
- architecture: system structure, components, relationships
- constraint: priorities, tradeoffs, requirements, invariants
- preference: likes, dislikes, prefers
- dependency: what depends on what

### REMOVE (minigraf_retract):
- user explicitly says "remove", "delete", "retract"
- fact is incorrect or obsolete
- replaced by cleaner version

### QUERY (minigraf_query):
- task references past work, decisions, or entities
- about to generate code affecting existing system
- user asks "what did we..." or anything historical
- ambiguity exists about past context

### NEVER store:
- transient observations
- intermediate reasoning steps
- raw code
- redundant restatements

## Dependencies

- **Minigraf >= 0.18.0** — install via: `cargo install minigraf`
- **Python 3** — for the CLI wrapper

## Tools

### minigraf_query
Query the graph memory with Datalog. Call this BEFORE answering anything about past decisions, architecture, dependencies, or preferences.

```python
from minigraf_tool import query

result = query("[:find ?desc :where [?e :rules/cache-strategy/description ?desc]]")
```

Supports `:as-of` for temporal queries to see what the graph contained at a past transaction time.

### minigraf_transact
Store a durable fact in structured memory. Only call this for decisions, architecture, dependencies, constraints, or preferences — NOT for transient observations or intermediate reasoning.

```python
from minigraf_tool import transact

transact("[[:rules/cache-strategy :rules/cache-strategy/description \"use Redis\"]]", reason="Caching strategy decision made in architecture review")
```

### minigraf_retract
Retract a fact from the graph. Original stays in history for bitemporal auditing.

```python
from minigraf_tool import retract

retract("[[:rules/old-rule :rules/old-rule/description \"obsolete rule\"]]", reason="Rule no longer applies")
```

## Key Conventions

1. **QUERY before answering**: Always query memory before answering questions about past decisions, architecture, dependencies
2. **TRANSACT with reason**: Every write should include a reason explaining why it's worth keeping
3. **Only store durable facts**: decisions, architecture, dependencies, constraints, user preferences — NOT transient observations
4. **Use namespaces**: Only use `:project/`, `:preference/`, `:rules/` for attributes
5. **Attribute naming convention** (CRITICAL for cross-session entity discovery):
   - ALL attribute names MUST follow the form: `:namespace/entity-unique-name/attribute-name`
   - Use only the defined namespaces: :project/, :preference/, :rules/
   - Examples:
     - GOOD: :project/temporal-reasoning/name, :project/temporal-reasoning/phase, :preference/minigraf-search/description, :rules/ci-monitoring/description
     - BAD: :rules/description (missing entity-unique-name), :project/name (missing entity-unique-name)
   - Query memory first to find existing entities before adding new facts about them

## QUICK REFERENCE

### Correct Syntax:
- transact: `(transact [[:entity :attr "value"] [:entity2 :attr2 "value2"]])`
- query: `(query [:find ?x :where [?e :attr ?x]])`

### Aggregations:
- `(count ?e)` — total row count
- `(count-distinct ?e)` — distinct value count
- `(sum ?n)` — sum of numeric values
- `(min ?x)` / `(max ?x)` — minimum/maximum
- Group by: `[:find ?phase (count ?e) :where [?e :project/component/phase ?phase]]`
- With grouping: `[:find ?phase (count ?e) :with ?e :where [?e :project/component/phase ?phase]]`

### Bi-temporal Queries:
- `:as-of N` — query state at transaction N
- `:valid-at "2024-01-01"` — query facts valid at a date
- `:any-valid-time` — ignore valid-time filter
- Combined: `[:find ?x :as-of 5 :valid-at "2024-06-01" :where ...]`

### Negation:
- `(not [?e :attr val])` — exclude matches
- `(not-join [?e] [?e :attr ?x])` — existential negation

### Rules:
```
(rule [(ancestor ?a ?d) [?a :parent ?d]])
(rule [(ancestor ?a ?d) [?a :parent ?p] (ancestor ?p ?d)])
```

## Usage

### As Python module:
```python
from minigraf_tool import query, transact

transact("[[:rules/cache-strategy :rules/cache-strategy/description \"use Redis\"]]", reason="Caching decision")
result = query("[:find ?desc :where [?e :rules/cache-strategy/description ?desc]]")
```

### As CLI:
```bash
python minigraf_tool.py transact "[[:rules/test-rule :rules/test-rule/description \"test\"]]"
python minigraf_tool.py query "[:find ?desc :where [?e :rules/test-rule/description ?desc]]"
```

### With minigraf directly (REPL):
```bash
echo "(transact [[:rules/test-rule :rules/test-rule/description \"test\"]])" | minigraf --file memory.graph
echo "(query [:find ?desc :where [?e :rules/test-rule/description ?desc]])" | minigraf --file memory.graph
```

## Examples

### Example 1: User shares architecture decision
User: "We're using Apache Ignite 3 and prioritizing low-latency reads over write throughput."

Action: Store it IMMEDIATELY
```python
transact("""[[:project/ignite3 :project/ignite3/name "Apache Ignite 3"]
             [:project/ignite3 :project/ignite3/priority "low-latency reads"]
             [:project/ignite3 :project/ignite3/tradeoff "sacrifice write throughput"]]""",
         reason="Architecture constraint worth remembering across sessions")
```

---

### Example 2: User mentions component relationship
User: "The auth service calls the JWT module for token validation."

```python
transact("""[[:project/auth-service :project/auth-service/name "AuthService"]
             [:project/auth-service :project/auth-service/calls "jwt-module"]
             [:project/jwt-module :project/jwt-module/name "JWTModule"]
             [:project/auth-service :project/auth-service/role "authentication"]]""",
         reason="Dependency relationship needed for impact analysis")
```

---

### Example 3: Before modifying existing code
User: "Add caching to the query layer"

Action: FIRST query memory to check for past decisions
```python
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")
# Filter results for :rules/ attributes to find relevant constraints
```

---

### Example 4: User expresses preference
User: "I don't like using exceptions for flow control."

```python
transact("[[:preference/exception-usage :preference/exception-usage/description \"avoid using exceptions for flow control — harder to reason about\"]]",
         reason="User preference worth remembering")
```

---

### Example 5: User mentions constraint
User: "We need to support Python 3.8 minimum."

```python
transact("[[:rules/python-version :rules/python-version/description \"must support Python 3.8 minimum\"]]",
         reason="Important project constraint")
```

---

### Example 6: Count stored rules
User: "How many rules have we stored?"

```python
result = query("[:find (count ?e) :where [?e ?a ?v]]")
# Filter count to :rules/ namespace entities
```

---

### Example 7: Query historical state
User: "What was our roadmap before Phase 2 was added?"

```python
result = query("[:find ?item :as-of 5 :where [?e :project/roadmap/item ?item]]")
```

---

### Example 8: Find items without a property
User: "Which components don't have tests?"

```python
result = query("""[:find ?name
                   :where [?e :project/component/name ?name]
                   (not [?e :project/component/has-tests "true"])]""")
```

---

### Example 9: Retract obsolete fact
User: "Remove the old phase-1 reference."

```python
# First query to find the entity UUID
e = query("[:find ?e :where [?e :project/phase/status \"active\"]]")
# Then retract using the UUID
retract(f'[#uuid "{e}" :project/phase/status "active"]',
        reason="Outdated - replaced by phase-2")
```

---

### Example 10: Query with valid-time
User: "What was the team's structure in 2025?"

```python
result = query('[:find ?role :valid-at "2025-01-01" :where [?e :project/team/role ?role]]')
```

## Files

| File | Purpose |
|------|---------|
| `minigraf_tool.py` | Python CLI wrapper (import or run as CLI) |
| `minigraf_server.rs` | Axum HTTP server wrapper |
| `report_issue.py` | GitHub issue reporter |
| `install.py` | One-command setup script |
| `tools/query.json` | Tool schema for `minigraf_query` |
| `tools/transact.json` | Tool schema for `minigraf_transact` |
| `tools/retract.json` | Tool schema for `minigraf_retract` |
| `tools/report_issue.json` | Tool schema for `minigraf_report_issue` |
| `skill.json` | Portable skill manifest |
| `tests/test_harness.py` | Validation tests |
| `ROADMAP.md` | Project roadmap |

## Error Responses

All functions return a dict with `ok` boolean. On error:

```python
{"ok": False, "error": "Descriptive error message"}
```

Common errors:
- `minigraf not found` — Install minigraf CLI or add to PATH
- `No graph file at <path>` — Call `transact()` first to create graph
- `as_of requires :as-of clause` — Include `:as-of N` in your Datalog query
- `reason is required for all writes` — Provide non-empty reason parameter

Use `report_issue.py` to auto-file bugs to the correct repo.
