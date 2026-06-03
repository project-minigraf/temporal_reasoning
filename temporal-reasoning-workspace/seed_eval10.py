#!/usr/bin/env python3
"""Seed for eval 10: hook-injected memory processing.

Pre-seeds the FastAPI decision so the prepare_hook can retrieve and inject it
as additionalContext before Claude sees the user's message.
"""
import sys, os

sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:project/api-layer :entity-type :type/decision]'
    ' [:project/api-layer :description "API framework selection"]'
    ' [:project/api-layer :framework "FastAPI"]'
    ' [:project/api-layer :reason "async support preferred over Flask; aligns with asyncio concurrency model"]]',
    reason="API framework decision stored in prior session",
    graph_path=graph,
)
print(f"Seeded eval 10 graph at {graph}")
