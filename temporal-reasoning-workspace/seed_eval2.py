#!/usr/bin/env python3
"""Seed for eval 2: populated memory retrieval.

Pre-seeds the three stack decisions (PostgreSQL 15, Redis 24h TTL, FastAPI)
that were 'stored in a prior session'. Claude must query and cite them.
"""
import sys, os
sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:project/database :entity-type :type/decision]'
    ' [:project/database :description "PostgreSQL 15 as primary database"]'
    ' [:project/database :version "15"]'
    ' [:project/database :reason "strong JSON support and ACID compliance"]]',
    reason="Database selection from prior session",
    graph_path=graph,
)
transact(
    '[[:project/session-cache :entity-type :type/decision]'
    ' [:project/session-cache :description "Redis for session token caching with 24-hour TTL"]'
    ' [:project/session-cache :ttl-hours "24"]'
    ' [:project/session-cache :reason "fast in-memory cache for auth tokens"]]',
    reason="Session caching strategy from prior session",
    graph_path=graph,
)
transact(
    '[[:project/api-framework :entity-type :type/decision]'
    ' [:project/api-framework :description "FastAPI for the HTTP layer"]'
    ' [:project/api-framework :reason "async support preferred over Flask"]]',
    reason="API framework decision from prior session",
    graph_path=graph,
)
print(f"Seeded eval 2 graph at {graph}")
