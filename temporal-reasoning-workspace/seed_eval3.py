#!/usr/bin/env python3
"""Seed for eval 3: cross-session preference enforcement.

Pre-seeds the no-mocks preference from a 'prior session'.
The eval prompt does NOT mention mocks — Claude must discover it from memory.
"""
import sys, os
sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:preference/no-db-mocks :entity-type :type/preference]'
    ' [:preference/no-db-mocks :description "Do not use mocks for database tests — use real connections"]'
    ' [:preference/no-db-mocks :reason "mocked tests passed but prod migration failed last quarter"]]',
    reason="Testing preference stored from prior session",
    graph_path=graph,
)
print(f"Seeded eval 3 graph at {graph}")
