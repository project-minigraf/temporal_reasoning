#!/usr/bin/env python3
"""Seed for eval 7: decision traceability."""
import sys, os
sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:rules/gil-constraint :description "Python GIL limits true thread parallelism"]'
    ' [:rules/gil-constraint :entity-type :type/constraint]]',
    reason="GIL constraint stored from prior session",
    graph_path=graph,
)
transact(
    '[[:project/asyncio-choice :description "use asyncio over threading for concurrency"]'
    ' [:project/asyncio-choice :entity-type :type/decision]'
    ' [:project/asyncio-choice :motivated-by :rules/gil-constraint]]',
    reason="Asyncio decision with motivated-by edge to GIL constraint",
    graph_path=graph,
)
print(f"Seeded eval 7 graph at {graph}")
