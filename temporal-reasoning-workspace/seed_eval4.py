#!/usr/bin/env python3
"""Seed for eval 4: conflict detection.

Pre-seeds PostgreSQL 15 as the finalized primary database.
Claude must detect the conflict when asked to write MySQL code.
"""
import sys, os
sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:project/database :entity-type :type/decision]'
    ' [:project/database :description "PostgreSQL 15 finalized as primary database — do not switch without team alignment"]'
    ' [:project/database :status "finalized"]'
    ' [:project/database :reason "strong JSON support, ACID compliance, team expertise"]]',
    reason="Database decision finalized in prior session",
    graph_path=graph,
)
print(f"Seeded eval 4 graph at {graph}")
