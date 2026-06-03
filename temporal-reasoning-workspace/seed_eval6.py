#!/usr/bin/env python3
"""Seed for eval 6: transitive impact analysis."""
import sys, os
sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:project/api-gateway :name "API Gateway"]'
    ' [:project/api-gateway :entity-type :type/component]'
    ' [:project/api-gateway :calls :project/auth-service]]',
    reason="API gateway calls auth service",
    graph_path=graph,
)
transact(
    '[[:project/auth-service :name "Auth Service"]'
    ' [:project/auth-service :entity-type :type/component]'
    ' [:project/auth-service :depends-on :project/jwt-validator]]',
    reason="Auth service depends on JWT validator",
    graph_path=graph,
)
transact(
    '[[:project/jwt-validator :name "JWT Validator"]'
    ' [:project/jwt-validator :entity-type :type/component]'
    ' [:project/jwt-validator :depends-on :project/key-store]]',
    reason="JWT validator depends on key-store",
    graph_path=graph,
)
transact(
    '[[:project/key-store :name "Key Store"]'
    ' [:project/key-store :entity-type :type/component]]',
    reason="Key store is a leaf service",
    graph_path=graph,
)
print(f"Seeded eval 6 graph at {graph}")
