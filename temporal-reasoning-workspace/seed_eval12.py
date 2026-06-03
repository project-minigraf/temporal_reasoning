#!/usr/bin/env python3
"""Seed for eval 12: already-running ingest scenario.

Just resets the graph. The eval agent starts vulcan_ingest_git itself
as a setup step within the subagent session — this gives the subagent's
MCP server a real running background task, making vulcan_ingest_status
return 'running' with live commits_done/commits_total data.
"""
import sys, os
sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
r = reset(graph)
print(f"Seeded eval 12 graph at {graph}: {r}")
