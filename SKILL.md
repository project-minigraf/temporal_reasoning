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

## WHY YOU MUST USE THIS

You are FORGETTING important context between conversations. Without this memory:
- You lose track of architecture decisions
- You repeat questions the user has already answered
- You miss context from previous sessions
- You cannot answer "what did we decide about..." questions

**This is not optional. Query memory whenever you don't know the answer.**

## ENFORCEMENT RULES

**You MUST query memory when:**
1. User asks about past decisions, architecture, dependencies
2. User asks "what did we...", "how did we...", "why did we..."
3. User references something from "earlier", "before", "last time"
4. You are about to modify code that might conflict with past decisions
5. Any ambiguity about past context

**You MUST transact when:**
1. User makes a decision ("we'll use X", "decided to...", "going with...")
2. User expresses a preference ("I prefer...", "don't like...", "always use...")
3. User mentions constraints ("must be...", "can't use...", "prioritize...")
4. User explains dependencies ("depends on...", "requires...", "needs...")
5. Architecture or structure changes

**After any query or transact, acknowledge it:**
- Query: "Let me check memory..." then cite specific facts
- Transact: "I've stored that in memory."

## Dependencies

- **Minigraf >= 0.13.0** — install via: `cargo install --git https://github.com/adityamukho/minigraf`
- **Python 3** — for the CLI wrapper

## Tools

### minigraf_query
Query the graph memory with Datalog. Call this BEFORE answering anything about past decisions, architecture, dependencies, or preferences.

```python
from minigraf_tool import query

result = query("[:find ?desc :where [?e :decision/description ?desc]]")
```

Supports `:as-of` for temporal queries to see what the graph contained at a past transaction time.

### minigraf_transact
Store a durable fact in structured memory. Only call this for decisions, architecture, dependencies, constraints, or preferences — NOT for transient observations or intermediate reasoning.

```python
from minigraf_tool import transact

transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]", reason="Caching strategy decision made in architecture review")
```

## Key Conventions

1. **QUERY before answering**: Always query memory before answering questions about past decisions, architecture, dependencies
2. **TRANSACT with reason**: Every write should include a reason explaining why it's worth keeping
3. **Only store durable facts**: decisions, architecture, dependencies, constraints, user preferences — NOT transient observations
4. **Use namespaces**: `:component/`, `:module/`, `:file/`, `:decision/`, `:arch/`, `:user/`, `:task/`, `:fact/`

## QUICK REFERENCE

### Correct Syntax:
- transact: `(transact [[:entity :attr "value"] [:entity2 :attr2 "value2"]])`
- query: `(query [:find ?x :where [?e :attr ?x]])`

### Aggregations:
- `(count ?e)` — total row count
- `(count-distinct ?e)` — distinct value count
- `(sum ?n)` — sum of numeric values
- `(min ?x)` / `(max ?x)` — minimum/maximum
- Group by: `[:find ?dept (count ?e) :where [?e :person/dept ?dept]]`
- With grouping: `[:find ?dept (sum ?salary) :with ?e :where ...]`

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

transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]", reason="Caching decision")
result = query("[:find ?desc :where [?e :decision/description ?desc]]")
```

### As CLI:
```bash
python minigraf_tool.py transact "[[:test :person/name \"Alice\"]]"
python minigraf_tool.py query "[:find ?name :where [:test :person/name ?name]]"
```

### With minigraf directly (REPL):
```bash
echo "(transact [[:alice :person/name \"Alice\"]])" | minigraf --file memory.graph
echo "(query [:find ?name :where [:alice :person/name ?name]])" | minigraf --file memory.graph
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
| `tools/report_issue.json` | Tool schema for `minigraf_report_issue` |
| `skill.json` | Portable skill manifest |
| `prompts/system.txt` | Operational contract (when to store/query) |
| `prompts/fewshots.txt` | Coding-specific examples |
| `tests/test_harness.py` | Validation tests |
| `ROADMAP.md` | Project roadmap |
