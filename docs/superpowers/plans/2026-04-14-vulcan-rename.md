# Vulcan Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the project from "temporal-reasoning" to "Vulcan" across all files — Python module, tool schemas, skill manifests, docs, synced directories, and GitHub repo — and update all messaging to lead with concrete reasoning superpowers rather than generic memory claims.

**Architecture:** Every user-facing surface (imports, tool names, skill name, README hero, SKILL.md) becomes "Vulcan". The underlying minigraf CLI stays as-is — it's an engine dependency, not the brand. Synced skill copies in `skills/` and `.opencode/skills/` are renamed and refreshed to match root.

**Tech Stack:** Python 3, git, gh CLI (for GitHub repo rename)

---

## File Map

| Action | Path |
|--------|------|
| Rename | `minigraf_tool.py` → `vulcan.py` |
| Rename | `tests/test_minigraf_tool.py` → `tests/test_vulcan.py` |
| Rename dir | `skills/temporal-reasoning/` → `skills/vulcan/` |
| Rename dir | `.opencode/skills/temporal_reasoning/` → `.opencode/skills/vulcan/` |
| Modify | `vulcan.py` (docstring + logger name) |
| Modify | `pyproject.toml` (name, script, py-modules) |
| Modify | `tools/query.json` (tool name + description) |
| Modify | `tools/transact.json` (tool name + description) |
| Modify | `tools/retract.json` (tool name + description) |
| Modify | `tools/report_issue.json` (tool name) |
| Modify | `skill.json` (name + description) |
| Modify | `.claude-plugin/plugin.json` (name + description) |
| Modify | `.claude-plugin/marketplace.json` (name + description) |
| Modify | `SKILL.md` (full rebrand + tool names) |
| Modify | `README.md` (full rebrand + hero messaging) |
| Modify | `CLAUDE.md` (project description + imports) |
| Modify | `AGENTS.md` (project description + file refs) |
| Modify | `install.py` (FILES_TO_SYNC, SKILL_DIRS, print strings) |
| Modify | `report_issue.py` (docstring + logger name) |
| Modify | `tests/conftest.py` (mock patch path) |
| Modify | `tests/test_vulcan.py` (imports + mock patch) |
| Modify | `tests/test_harness.py` (import) |
| Modify | `tests/test_advanced.py` (import + mock patch) |
| Modify | synced `vulcan.py`, `SKILL.md`, `skill.json`, `tools/*.json` in both skill dirs |

---

### Task 1: Rename minigraf_tool.py → vulcan.py and update module internals

**Files:**
- Rename: `minigraf_tool.py` → `vulcan.py`
- Modify: `vulcan.py` lines 1–19

- [ ] **Step 1: Rename the file**

```bash
git mv minigraf_tool.py vulcan.py
```

- [ ] **Step 2: Update module docstring and logger name**

In `vulcan.py`, replace lines 1–19:

```python
#!/usr/bin/env python3
"""
Vulcan — bi-temporal graph memory for AI coding agents.

Provides query, transact, and retract functions for persistent graph memory
powered by the minigraf CLI.
"""

import re
import subprocess
import json
import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Union

logger = logging.getLogger("vulcan")
logger.addHandler(logging.NullHandler())
```

- [ ] **Step 3: Update the main() usage string (line ~379)**

In `vulcan.py`, find the `main()` function and replace the usage print:

```python
# old
print("Usage: minigraf_tool.py <command> [args]")
print("Commands: query, transact, retract, reset, path")
print(f"Mode: {mode} (set MINIGRAF_MODE=http for HTTP server)")

# new
print("Usage: vulcan.py <command> [args]")
print("Commands: query, transact, retract, reset, path")
print(f"Mode: {mode} (set MINIGRAF_MODE=http for HTTP server)")
```

- [ ] **Step 4: Commit**

```bash
git add vulcan.py
git commit -m "feat: rename minigraf_tool.py to vulcan.py"
```

---

### Task 2: Update pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Replace the entire file content**

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "vulcan"
version = "0.1.0"
description = "Perfect memory. Exact reasoning. Complete history. Bi-temporal graph memory for AI coding agents."
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.8"
authors = [
    {name = "Aditya Mukhopadhyay", email = "github@adityamukho.invalid"}
]
keywords = ["ai-agents", "graph-database", "datalog", "knowledge-graph", "persistent-memory", "temporal-reasoning", "vulcan"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.8",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]

dependencies = []

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "black>=23.0",
    "ruff>=0.1.0",
]

[project.scripts]
vulcan = "vulcan:main"

[tool.setuptools]
py-modules = ["vulcan"]

[tool.ruff]
line-length = 100
target-version = "py38"

[tool.pylint.messages_control]
max-line-length = 100
disable = ["C0111", "C0301", "C0303", "C0411", "C0413", "C0415", "R0801", "W0212", "W0611", "W0621"]

[tool.black]
line-length = 100
target-version = ["py38", "py39", "py310", "py311", "py312"]
```

- [ ] **Step 2: Commit**

```bash
git add pyproject.toml
git commit -m "chore: rename package to vulcan in pyproject.toml"
```

---

### Task 3: Update tool schemas

**Files:**
- Modify: `tools/query.json`
- Modify: `tools/transact.json`
- Modify: `tools/retract.json`
- Modify: `tools/report_issue.json`

- [ ] **Step 1: Replace tools/query.json**

```json
{
  "name": "vulcan_query",
  "description": "Query Vulcan's persistent bi-temporal graph memory using Datalog. Call this BEFORE answering anything about past decisions, architecture, dependencies, or preferences. Supports :as-of for temporal queries to see what the graph contained at a past transaction time.",
  "parameters": {
    "type": "object",
    "properties": {
      "datalog": {
        "type": "string",
        "description": "A valid Datalog query, e.g. [:find ?name :where [?e :component/name ?name]]"
      },
      "as_of": {
        "type": "integer",
        "description": "Optional transaction count to query as of. If not provided, queries current state."
      }
    },
    "required": ["datalog"]
  }
}
```

- [ ] **Step 2: Replace tools/transact.json**

```json
{
  "name": "vulcan_transact",
  "description": "Store a durable fact in Vulcan's graph memory. Only call this for decisions, architecture, dependencies, constraints, or preferences — NOT for transient observations or intermediate reasoning.",
  "parameters": {
    "type": "object",
    "properties": {
      "facts": {
        "type": "string",
        "description": "A Datalog transact block, e.g. [[:component/auth :calls :component/jwt]] or [[:decision/cache-strategy :decision/description \"use Redis\"]]"
      },
      "reason": {
        "type": "string",
        "description": "Why this fact deserves long-term storage. This forces you to justify writes — only store facts worth remembering."
      }
    },
    "required": ["facts", "reason"]
  }
}
```

- [ ] **Step 3: Replace tools/retract.json**

```json
{
  "name": "vulcan_retract",
  "description": "Retract a fact from Vulcan's graph memory. Retraction records a new fact with asserted=false — the original stays in history for bi-temporal auditing.",
  "parameters": {
    "type": "object",
    "properties": {
      "facts": {
        "type": "string",
        "description": "A Datalog retract block, e.g. [[:component/auth :calls :component/jwt]]"
      },
      "reason": {
        "type": "string",
        "description": "Why this fact is being retracted. This forces you to justify the removal."
      }
    },
    "required": ["facts", "reason"]
  }
}
```

- [ ] **Step 4: Replace tools/report_issue.json**

```json
{
  "name": "vulcan_report_issue",
  "description": "Report an issue with Vulcan query or transact operations. Use this when Vulcan returns errors to file a GitHub issue for tracking.",
  "parameters": {
    "type": "object",
    "properties": {
      "issue_type": {
        "type": "string",
        "description": "Type of issue to report",
        "enum": ["invalid_query", "transact_failure", "parse_error", "minigraf_bug"]
      },
      "description": {
        "type": "string",
        "description": "Human-readable description of the issue"
      },
      "datalog": {
        "type": "string",
        "description": "Optional Datalog query or transact that failed"
      },
      "error": {
        "type": "string",
        "description": "Optional error message returned by Vulcan"
      }
    },
    "required": ["issue_type", "description"]
  }
}
```

- [ ] **Step 5: Commit**

```bash
git add tools/
git commit -m "feat: rename tool schemas to vulcan_query/transact/retract/report_issue"
```

---

### Task 4: Update skill.json and plugin files

**Files:**
- Modify: `skill.json`
- Modify: `.claude-plugin/plugin.json`
- Modify: `.claude-plugin/marketplace.json`

- [ ] **Step 1: Replace skill.json**

```json
{
  "name": "vulcan",
  "version": "0.1.0",
  "description": "Vulcan gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.",
  "tools": [
    "tools/query.json",
    "tools/transact.json",
    "tools/retract.json",
    "tools/report_issue.json"
  ],
  "requires": {
    "minigraf": ">=0.18.0"
  },
  "languages": ["python"],
  "environments": ["claude-code", "opencode", "codex"]
}
```

- [ ] **Step 2: Replace .claude-plugin/plugin.json**

```json
{
  "name": "vulcan",
  "description": "Perfect memory. Exact reasoning. Complete history. Bi-temporal graph memory for AI coding agents — query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change.",
  "version": "0.1.0",
  "author": {
    "name": "Aditya Mukhopadhyay",
    "email": "github@adityamukho.invalid"
  },
  "license": "MIT",
  "keywords": [
    "memory",
    "temporal",
    "graph",
    "datalog",
    "persistent-memory",
    "ai-agents",
    "vulcan"
  ]
}
```

- [ ] **Step 3: Replace .claude-plugin/marketplace.json**

```json
{
  "name": "vulcan-local",
  "description": "Local marketplace for the Vulcan plugin",
  "owner": {
    "name": "Aditya Mukhopadhyay",
    "email": "github@adityamukho.invalid"
  },
  "plugins": [
    {
      "name": "vulcan",
      "description": "Perfect memory. Exact reasoning. Complete history. Bi-temporal graph memory for AI coding agents — query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change.",
      "source": "./",
      "category": "development"
    }
  ]
}
```

- [ ] **Step 4: Commit**

```bash
git add skill.json .claude-plugin/
git commit -m "feat: rename skill and plugin manifests to vulcan"
```

---

### Task 5: Update SKILL.md

**Files:**
- Modify: `SKILL.md`

- [ ] **Step 1: Replace the frontmatter and title**

Replace the top of `SKILL.md` (lines 1–14):

```markdown
---
name: vulcan
description: >
  Use this skill whenever the user mentions decisions ("we'll use X", "going with Y", "decided to Z"),
  preferences ("I prefer", "I don't like", "always use", "never use"), constraints ("must be", "can't use",
  "prioritize"), dependencies ("depends on", "requires"), or references past context ("what did we",
  "last time", "before", "earlier", "what was our"). Also use before any code modification that might
  conflict with past decisions — if you're about to touch an area where architectural choices might apply,
  query first. When in doubt, query.
---

# Vulcan

Perfect memory. Exact reasoning. Complete history.

Vulcan gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.
```

- [ ] **Step 2: Replace the "When to Write" and "When to Read" section headings and tool references**

Replace every occurrence of `minigraf_transact` with `vulcan_transact`, `minigraf_query` with `vulcan_query`, `minigraf_retract` with `vulcan_retract`, and `minigraf_report_issue` with `vulcan_report_issue` throughout SKILL.md.

Run to verify no old names remain:
```bash
grep -n "minigraf_query\|minigraf_transact\|minigraf_retract\|minigraf_report_issue" SKILL.md
```
Expected: no output.

- [ ] **Step 3: Replace `from minigraf_tool import` with `from vulcan import` throughout SKILL.md**

```bash
grep -n "minigraf_tool" SKILL.md
```
Expected: no output after edits.

- [ ] **Step 4: Replace CLI usage strings**

Replace `python minigraf_tool.py` with `python vulcan.py` in the CLI examples under the Tools section.

- [ ] **Step 5: Update the ## Tools section headings**

```markdown
### vulcan_transact
```python
from vulcan import transact
...
```

### vulcan_query
```python
from vulcan import query
...
```

### vulcan_retract
```python
from vulcan import retract
...
```

- [ ] **Step 6: Update the ## Files table at the bottom of SKILL.md**

```markdown
## Files

| File | Purpose |
|------|---------|
| `vulcan.py` | Python wrapper (import or CLI) |
| `report_issue.py` | GitHub issue reporter for errors |
| `tools/query.json` | Tool schema for vulcan_query |
| `tools/transact.json` | Tool schema for vulcan_transact |
| `tools/retract.json` | Tool schema for vulcan_retract |
| `tools/report_issue.json` | Tool schema for vulcan_report_issue |
| `install.py` | Setup script |
| `ROADMAP.md` | Project roadmap |
```

- [ ] **Step 7: Update the error response section**

Replace:
```
If an error persists after checking syntax and installation, use `minigraf_report_issue` to file a structured bug report...
```
With:
```
If an error persists after checking syntax and installation, use `vulcan_report_issue` to file a structured bug report...
```

And replace:
```python
from report_issue import report_issue
```
(unchanged — `report_issue.py` filename stays)

- [ ] **Step 8: Verify and commit**

```bash
grep -c "minigraf_tool\|minigraf_query\|minigraf_transact\|minigraf_retract\|minigraf_report_issue\|Temporal Reasoning Skill" SKILL.md
```
Expected: `0`

```bash
git add SKILL.md
git commit -m "feat: rebrand SKILL.md to Vulcan"
```

---

### Task 6: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace the header and intro**

Replace the current `# Temporal Reasoning` through the end of the `## Problem Scope` section with:

```markdown
# Vulcan

**Perfect memory. Exact reasoning. Complete history.**

Vulcan gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.

## Questions Only Vulcan Can Answer

These queries are impossible with git log, vector search, or key-value memory:

```datalog
; What did the dependency graph look like before the auth refactor?
[:find ?caller ?callee
 :as-of 30
 :where [?caller :calls ?callee]]

; When did this coupling first appear — and what decision caused it?
[:find ?reason
 :where [:project/service-a :depends-on :project/service-b]
        [?d :motivated-by ?c]
        [?c :description ?reason]]

; Which modules were coupled to the payment service when we made the DB decision?
[:find ?module
 :as-of 15
 :where [?module :depends-on :service/payment]]
```

Vulcan is the only tool where both the decision and the structural change live as datoms in the same graph and can be joined in a single query. See [Phase 4](ROADMAP.md) for code structure evolution from git history.
```

- [ ] **Step 2: Rename "Why minigraf?" → "Why Vulcan?" and update its opening**

Replace the `## Why minigraf?` heading and the paragraph before the first code block with:

```markdown
## Why Vulcan?

Most memory tools for agents are key-value stores or vector databases. They answer "what do you know now?" Vulcan answers a harder question: **"what did you know then, and what changed?"**
```

Keep the rest of the section (Time travel, Retraction, Exact Datalog, Graph traversal, Local and offline) — just update the heading.

- [ ] **Step 3: Update the architecture diagram**

Replace `## Architecture` section's diagram title line:

```
│                   AI Coding Agent                        │
│              (Claude Code, OpenCode, Codex)            │
...
│              Python Skill Layer                          │
│         (vulcan.py - this repo)                         │
│   - query(), transact() functions                     │
│   - CLI mode                                           │
│   - Backup/restore utilities                           │
...
│              Minigraf CLI (>= 0.18.0)                   │
│         (https://github.com/adityamukho/minigraf)       │
│         (Vulcan's storage engine)                       │
```

- [ ] **Step 4: Update Install section**

The install section stays the same except the repo URL once GitHub rename is done (handled in Task 12). No changes needed here yet.

- [ ] **Step 5: Update Quick Start imports**

Replace:
```python
from minigraf_tool import query, transact
```
With:
```python
from vulcan import query, transact
```

(Both occurrences in README.md.)

- [ ] **Step 6: Update the Files table**

```markdown
| File | Purpose |
|------|---------|
| `vulcan.py` | Python CLI wrapper |
| `report_issue.py` | GitHub issue reporter |
| `install.py` | Setup script |
| `pyproject.toml` | Python packaging |
| `tools/*.json` | Tool schemas |
| `prompts/*.txt` | Behavioral prompts |
| `tests/test_harness.py` | Validation tests |
```

- [ ] **Step 7: Update the Tools section**

Replace tool names:
```markdown
- **vulcan_query** — Query memory with Datalog
- **vulcan_transact** — Store facts (reason required)
- **vulcan_retract** — Retract facts (original stays in history)
- **vulcan_report_issue** — File GitHub issues
```

- [ ] **Step 8: Update the Install In Agent Environments section**

Replace the skill name references:
```markdown
Claude Code / Codex:
- Install the local skill from this repository as `vulcan`.
- Use [SKILL.md](/SKILL.md) and [skill.json](/skill.json) as the primary skill files.

OpenCode:
- Run `python install.py` from the repository root.
- This syncs the skill into `.opencode/skills/vulcan`.
```

- [ ] **Step 9: Update Phases section**

```markdown
## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — Write policy, report_issue, install, skill benchmarks ✓
- **Phase 3** — WASM bindings, MCP integration (future)
- **Phase 4** — Code structure evolution from git history (future)
```

- [ ] **Step 10: Verify no old name remains**

```bash
grep -n "Temporal Reasoning\|temporal-reasoning\|temporal_reasoning\|minigraf_tool\|minigraf_query\|minigraf_transact\|minigraf_retract" README.md
```
Expected: no output.

- [ ] **Step 11: Commit**

```bash
git add README.md
git commit -m "feat: rebrand README.md to Vulcan with superpowers messaging"
```

---

### Task 7: Update CLAUDE.md and AGENTS.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Replace CLAUDE.md**

```markdown
# Vulcan — AI Coding Agent Memory

Vulcan provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill
python install.py

# Use in code
from vulcan import query, transact

transact("[[:decision/cache :decision/description \"use Redis\"]]", reason="Caching strategy")
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Key Files

- `vulcan.py` - Python wrapper for minigraf CLI
- `SKILL.md` - Skill definition with all query syntax
- `install.py` - Setup script (runs weekly updates)

## Graph Storage

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# With temporal
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Count
query("[:find (count ?e) :where [?e :decision/description ?d]]")
```
```

- [ ] **Step 2: Replace AGENTS.md**

```markdown
# Vulcan Repository

Persistent bi-temporal graph memory skill for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.

## Architecture

```
[ Agent (Claude Code / OpenCode / Codex) ]
        ↓
[ Python Skill Layer ]              ← this repo
        ↓
[ Minigraf CLI ]                   ← must be on PATH (>= 0.18.0)
        ↓
[ .graph file on disk ]
```

## Dependencies

- **Minigraf >= 0.18.0** — install via: `cargo install minigraf`
- **Python 3** — for the CLI wrapper

## Files

| File | Purpose |
|------|---------|
| `vulcan.py` | Python CLI wrapper (import or run as CLI) |
| `tools/query.json` | Tool schema for `vulcan_query` |
| `tools/transact.json` | Tool schema for `vulcan_transact` |
| `skill.json` | Portable skill manifest |

## Usage

### As Python module:
```python
from vulcan import query, transact

transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]",
         reason="Architecture decision for low-latency caching")
result = query("[:find ?desc :where [?e :decision/description ?desc]]")
```

### As CLI:
```bash
python vulcan.py transact "[[:test :person/name \"Alice\"]]"
python vulcan.py query "[:find ?name :where [:test :person/name ?name]]"
```

### With minigraf directly (REPL):
```bash
echo "(transact [[:alice :person/name \"Alice\"]])" | minigraf --file memory.graph
echo "(query [:find ?name :where [:alice :person/name ?name]])" | minigraf --file memory.graph
```

## Key Conventions

- **QUERY before answering**: Always query memory before answering questions about past decisions, architecture, dependencies
- **TRANSACT with reason**: Every write should include a reason explaining why it's worth keeping
- **Only store durable facts**: decisions, architecture, dependencies, constraints, user preferences — NOT transient observations
- **Use namespaces**: `:component/`, `:module/`, `:file/`, `:decision/`, `:arch/`, `:user/`, `:task/`, `:fact/`
```

- [ ] **Step 3: Verify and commit**

```bash
grep -n "minigraf_tool\|temporal-reasoning\|temporal_reasoning\|Temporal Reasoning" CLAUDE.md AGENTS.md
```
Expected: no output.

```bash
git add CLAUDE.md AGENTS.md
git commit -m "feat: rebrand CLAUDE.md and AGENTS.md to Vulcan"
```

---

### Task 8: Update install.py

**Files:**
- Modify: `install.py`

- [ ] **Step 1: Update FILES_TO_SYNC**

Replace line 21:
```python
FILES_TO_SYNC = ["SKILL.md", "minigraf_tool.py", "skill.json"]
```
With:
```python
FILES_TO_SYNC = ["SKILL.md", "vulcan.py", "skill.json"]
```

- [ ] **Step 2: Update SKILL_DIRS**

Replace lines 23–26:
```python
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "vulcan"),
    os.path.join("skills", "vulcan"),
]
```

- [ ] **Step 3: Update check_tool_import()**

Replace the `check_tool_import` function body:

```python
def check_tool_import():
    """Verify vulcan module can be imported."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("vulcan")
        if spec is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, script_dir)
        import vulcan
        print("✓ vulcan module can be imported")
        return True
    except ImportError as e:
        print(f"✗ Cannot import vulcan: {e}")
        return False
```

- [ ] **Step 4: Update main() header and usage strings**

Replace the header block inside `main()`:
```python
print("=" * 50)
print("Vulcan Skill Setup")
print("=" * 50)
```

Replace usage strings:
```python
msg = "from vulcan import query, transact; "
msg += "print(query('[:find ?e :where [?e :test/name]]'))"
print(f"  python -c \"{msg}\"")
print()
print("  # As CLI:")
print("  python vulcan.py query '[:find ?e :where [?e :test/name]]'")
print("  python vulcan.py transact '[[:test :person/name \\\"Alice\\\"]]'")
print()
print("  # Import and use in code:")
print("  from vulcan import query, transact")
tx_msg = "transact('[[:decision :arch/cache-strategy \"Redis\"]]', "
tx_msg += "reason='fast in-memory caching')"
print(f"  {tx_msg}")
q_msg = "result = query('[:find ?s :where [_ :arch/cache-strategy ?s]]')"
print(f"  {q_msg}")
```

- [ ] **Step 5: Verify and commit**

```bash
grep -n "minigraf_tool\|temporal_reasoning\|temporal-reasoning\|Temporal-Reasoning" install.py
```
Expected: no output.

```bash
git add install.py
git commit -m "feat: update install.py for Vulcan rename"
```

---

### Task 9: Update report_issue.py and test files

**Files:**
- Modify: `report_issue.py`
- Rename: `tests/test_minigraf_tool.py` → `tests/test_vulcan.py`
- Modify: `tests/test_vulcan.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_harness.py`
- Modify: `tests/test_advanced.py`

- [ ] **Step 1: Update report_issue.py docstring and logger**

Replace lines 1–20 of `report_issue.py`:

```python
#!/usr/bin/env python3
"""
report_issue.py - Report Vulcan errors as GitHub issues.

Provides a tool to file issues when Vulcan queries/transacts fail.
Uses GitHub CLI (gh) if available, otherwise falls back to logging.

Automatically routes issues to the correct repo:
- minigraf core bugs -> https://github.com/adityamukho/minigraf
- Vulcan skill bugs -> current repo
"""

import subprocess
import sys
import logging
import json
from typing import Dict, Optional

logger = logging.getLogger("vulcan.report_issue")
logger.addHandler(logging.NullHandler())
```

Also update the issue title in `report_issue.py`:
```python
# old
title = f"[minigraf] {issue_type}: {description[:50]}"

# new
title = f"[vulcan] {issue_type}: {description[:50]}"
```

Also update the wrapper indicators list to include `vulcan.py`:
```python
wrapper_indicators = [
    "vulcan.py",
    "python wrapper",
    "import error",
    "subprocess",
    "cli wrapper",
]
```

- [ ] **Step 2: Rename test_minigraf_tool.py**

```bash
git mv tests/test_minigraf_tool.py tests/test_vulcan.py
```

- [ ] **Step 3: Update imports in tests/test_vulcan.py**

Replace every occurrence of `import minigraf_tool` and `from minigraf_tool import` with `import vulcan` and `from vulcan import` respectively.

Replace mock patch paths:
```python
# old
with patch("minigraf_tool.subprocess.run") as mock_run:

# new
with patch("vulcan.subprocess.run") as mock_run:
```

- [ ] **Step 4: Update tests/conftest.py**

Replace the mock patch path on line 21:
```python
# old
with patch("minigraf_tool.subprocess.run") as mock_run:

# new
with patch("vulcan.subprocess.run") as mock_run:
```

- [ ] **Step 5: Update tests/test_harness.py**

Replace line 17:
```python
# old
from minigraf_tool import query, transact, reset

# new
from vulcan import query, transact, reset
```

- [ ] **Step 6: Update tests/test_advanced.py**

Replace the import line (near top, after pytest import):
```python
# old — uses importlib to load minigraf_tool
# Find and replace all occurrences of "minigraf_tool" in this file
```

Run:
```bash
grep -n "minigraf_tool" tests/test_advanced.py
```

Replace each found occurrence: `minigraf_tool` → `vulcan` in both import statements and mock patch strings.

- [ ] **Step 7: Verify and commit**

```bash
grep -rn "minigraf_tool" tests/ report_issue.py
```
Expected: no output.

```bash
git add report_issue.py tests/
git commit -m "feat: update report_issue.py and tests for Vulcan rename"
```

---

### Task 10: Run the test suite

- [ ] **Step 1: Run tests**

```bash
pytest tests/ -q 2>&1 | head -50
```

Expected: all tests pass (same count as before rename). If any fail with `ModuleNotFoundError: No module named 'minigraf_tool'`, re-check steps in Tasks 1, 9 for missed references.

- [ ] **Step 2: Run grep to confirm no stale references in source files**

```bash
grep -rn "minigraf_tool\|temporal-reasoning\|temporal_reasoning" \
  vulcan.py pyproject.toml skill.json SKILL.md README.md CLAUDE.md AGENTS.md \
  install.py report_issue.py tools/ tests/ .claude-plugin/
```
Expected: no output.

---

### Task 11: Rename synced skill directories

**Files:**
- Rename dir: `skills/temporal-reasoning/` → `skills/vulcan/`
- Rename dir: `.opencode/skills/temporal_reasoning/` → `.opencode/skills/vulcan/`
- Modify: all files within those dirs to match root

- [ ] **Step 1: Rename the skill directories**

```bash
git mv skills/temporal-reasoning skills/vulcan
git mv .opencode/skills/temporal_reasoning .opencode/skills/vulcan
```

- [ ] **Step 2: Rename minigraf_tool.py within the synced dirs**

```bash
git mv skills/vulcan/minigraf_tool.py skills/vulcan/vulcan.py
git mv .opencode/skills/vulcan/minigraf_tool.py .opencode/skills/vulcan/vulcan.py
```

- [ ] **Step 3: Sync updated root files into both directories**

Run install.py to push the updated root files into the renamed dirs:

```bash
python install.py --force
```

Expected output includes: `✓ Synced [SKILL.md, vulcan.py, skill.json, tools] → [.opencode/skills/vulcan, skills/vulcan]`

- [ ] **Step 4: Verify synced files have updated tool names**

```bash
grep "minigraf_query\|minigraf_transact\|minigraf_tool\|temporal_reasoning" \
  skills/vulcan/SKILL.md skills/vulcan/skill.json \
  .opencode/skills/vulcan/SKILL.md .opencode/skills/vulcan/skill.json
```
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add skills/ .opencode/skills/
git commit -m "feat: rename synced skill directories to vulcan"
```

---

### Task 12: Rename GitHub repo and update remote URL

- [ ] **Step 1: Rename the GitHub repository**

```bash
gh repo rename vulcan --yes
```

Expected output: `✓ Renamed repository to adityamukho/vulcan`

- [ ] **Step 2: Update the local remote URL**

```bash
git remote set-url origin git@github.com:adityamukho/vulcan.git
```

- [ ] **Step 3: Verify remote**

```bash
git remote -v
```

Expected:
```
origin  git@github.com:adityamukho/vulcan.git (fetch)
origin  git@github.com:adityamukho/vulcan.git (push)
```

- [ ] **Step 4: Push to confirm the new remote works**

```bash
git push origin master
```

---

### Task 13: Update minigraf wiki reference in SKILL.md

The SKILL.md currently links to the minigraf wiki for Datalog reference. This link stays valid (minigraf repo is not renamed). No change needed — just verify it's still present:

```bash
grep "minigraf/wiki" SKILL.md
```

Expected: one line pointing to `https://github.com/adityamukho/minigraf/wiki/Datalog-Reference`

That link is correct and should not be changed.

---

## Self-Review Checklist

- **Spec coverage:** All sections of the design spec are covered: Python module rename (Task 1), pyproject.toml (Task 2), tool schemas (Task 3), skill/plugin manifests (Task 4), SKILL.md (Task 5), README.md (Task 6), CLAUDE.md/AGENTS.md (Task 7), install.py (Task 8), report_issue/tests (Task 9), test run (Task 10), synced dirs (Task 11), GitHub rename (Task 12). ✓
- **Placeholders:** None. Every step has exact file content or exact commands. ✓
- **Type consistency:** No type/method signature changes — this is a rename, not a refactor. All function signatures in `vulcan.py` are identical to `minigraf_tool.py`. ✓
- **Eval files left alone:** `temporal-reasoning-workspace/` is not touched, per spec. ✓
