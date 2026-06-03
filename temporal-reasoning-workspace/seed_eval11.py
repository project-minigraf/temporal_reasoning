#!/usr/bin/env python3
"""Seed for eval 11: audit-after-heavy-writes.

Pre-seeds 12 entities across decisions, constraints, and components — simulating
a heavy architecture session. All data is schema-valid so vulcan_audit should
return a clean bill of health (or surface any inconsistencies from the session).
"""
import sys, os

sys.path.insert(0, "/home/aditya/workspaces/pycharm/temporal_reasoning/.opencode/skills/temporal-reasoning")
from vulcan import transact, reset

graph = os.environ.get("MINIGRAF_GRAPH_PATH", "/home/aditya/workspaces/pycharm/temporal_reasoning/memory.graph")
reset(graph)

transact(
    '[[:project/api-layer :entity-type :type/decision]'
    ' [:project/api-layer :description "Use FastAPI for the HTTP layer"]'
    ' [:project/api-layer :framework "FastAPI"]]',
    reason="API framework decision",
    graph_path=graph,
)
transact(
    '[[:project/db-choice :entity-type :type/decision]'
    ' [:project/db-choice :description "PostgreSQL 15 as primary store"]'
    ' [:project/db-choice :database "PostgreSQL 15"]]',
    reason="Database selection",
    graph_path=graph,
)
transact(
    '[[:project/cache-layer :entity-type :type/decision]'
    ' [:project/cache-layer :description "Redis for session token caching"]'
    ' [:project/cache-layer :ttl "24h"]]',
    reason="Caching strategy",
    graph_path=graph,
)
transact(
    '[[:constraint/gil :entity-type :type/constraint]'
    ' [:constraint/gil :description "Python GIL limits true thread parallelism"]]',
    reason="GIL constraint documented",
    graph_path=graph,
)
transact(
    '[[:project/concurrency-model :entity-type :type/decision]'
    ' [:project/concurrency-model :description "Use asyncio over threading"]'
    ' [:project/concurrency-model :motivated-by :constraint/gil]]',
    reason="Concurrency model decision with motivated-by edge",
    graph_path=graph,
)
transact(
    '[[:project/api-gateway :entity-type :type/component]'
    ' [:project/api-gateway :name "API Gateway"]'
    ' [:project/api-gateway :calls :project/auth-service]]',
    reason="API gateway component",
    graph_path=graph,
)
transact(
    '[[:project/auth-service :entity-type :type/component]'
    ' [:project/auth-service :name "Auth Service"]'
    ' [:project/auth-service :depends-on :project/jwt-validator]]',
    reason="Auth service component",
    graph_path=graph,
)
transact(
    '[[:project/jwt-validator :entity-type :type/component]'
    ' [:project/jwt-validator :name "JWT Validator"]'
    ' [:project/jwt-validator :depends-on :project/key-store]]',
    reason="JWT validator component",
    graph_path=graph,
)
transact(
    '[[:project/key-store :entity-type :type/component]'
    ' [:project/key-store :name "Key Store"]]',
    reason="Key store leaf component",
    graph_path=graph,
)
transact(
    '[[:dependency/postgresql-15 :entity-type :type/dependency]'
    ' [:dependency/postgresql-15 :name "postgresql"]'
    ' [:dependency/postgresql-15 :version "15"]]',
    reason="PostgreSQL dependency pinned",
    graph_path=graph,
)
transact(
    '[[:dependency/redis :entity-type :type/dependency]'
    ' [:dependency/redis :name "redis"]'
    ' [:dependency/redis :version "7.x"]]',
    reason="Redis dependency pinned",
    graph_path=graph,
)
transact(
    '[[:preference/no-db-mocks :entity-type :type/preference]'
    ' [:preference/no-db-mocks :description "Do not use mocks for database tests — use real connections"]]',
    reason="Testing preference: no DB mocks",
    graph_path=graph,
)
print(f"Seeded eval 11 graph at {graph} (12 entities across 5 entity types)")
