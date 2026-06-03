#!/usr/bin/env python3
"""Seed for eval 5: entity-ref-storage.

Just resets the graph. No pre-existing data needed —
the agent stores the architecture described by the user.
"""
import sys, os

sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
r = reset(graph)
print(f"Seeded eval 5 graph at {graph}: {r}")
