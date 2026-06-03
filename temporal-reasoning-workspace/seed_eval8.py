#!/usr/bin/env python3
"""Seed for eval 8: no-redundant-ingest-call.

Just resets the graph to a clean state. The ingest_hook fires at subagent session
start and begins vulcan_ingest_git automatically. No pre-existing data needed.
"""
import sys, os

sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
r = reset(graph)
print(f"Seeded eval 8 graph at {graph}: {r}")
