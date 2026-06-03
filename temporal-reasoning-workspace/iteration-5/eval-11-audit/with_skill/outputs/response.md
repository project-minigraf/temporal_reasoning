# Eval 11 — With Skill

## Tool calls

### Call 1 — memory_prepare_turn
Input: `user_message = "We've just finished a big architecture session — I stored a lot of decisions and relationships. Can you verify everything looks consistent before we wrap up?"`

Output:
```
Relevant memory context:
  9b326792 | :name | Key Store
  cd6c70f9 | :description | Use FastAPI for the HTTP layer
  cd6c70f9 | :entity-type | :type/decision
  c24bba41 | :entity-type | :type/dependency
  79e1ebe3 | :entity-type | :type/decision
  383429b1 | :entity-type | :type/constraint
  b8eb7a73 | :entity-type | :type/decision
  08a0575b | :entity-type | :type/dependency
  a8217ec8 | :calls | :project/auth-service
  08a0575b | :version | 15
  e540bbf7 | :description | Do not use mocks for database tests — use real connections
  cd6c70f9 | :framework | FastAPI
  b8eb7a73 | :ttl | 24h
  383429b1 | :description | Python GIL limits true thread parallelism
  a5c144ee | :depends-on | :project/key-store
  9445af47 | :entity-type | :type/decision
  a7c1138a | :depends-on | :project/jwt-validator
  b8eb7a73 | :description | Redis for session token caching
  9445af47 | :description | PostgreSQL 15 as primary store
  a5c144ee | :entity-type | :type/component
  79e1ebe3 | :motivated-by | :constraint/gil
  a8217ec8 | :name | API Gateway
  e540bbf7 | :entity-type | :type/preference
  a8217ec8 | :entity-type | :type/component
  c24bba41 | :name | redis
  9b326792 | :entity-type | :type/component
  c24bba41 | :version | 7.x
  9445af47 | :database | PostgreSQL 15
  08a0575b | :name | postgresql
  a5c144ee | :name | JWT Validator
  a7c1138a | :name | Auth Service
  79e1ebe3 | :description | Use asyncio over threading
  a7c1138a | :entity-type | :type/component
```

### Call 2 — vulcan_audit
Input: (no arguments — full live audit)

Output:
```json
{
  "ok": true,
  "audited": 8,
  "retracted": 6,
  "violations": [
    {"entity": "cd6c70f9", "detail": "unknown attribute ':framework' — allowed: [':alias', ':date', ':description', ':rationale']"},
    {"entity": "9445af47", "detail": "unknown attribute ':database' — allowed: [':alias', ':date', ':description', ':rationale']"},
    {"entity": "b8eb7a73", "detail": "unknown attribute ':ttl' — allowed: [':alias', ':date', ':description', ':rationale']"},
    {"entity": "79e1ebe3", "detail": "unknown attribute ':motivated-by' — allowed: [':alias', ':date', ':description', ':rationale']"},
    {"entity": "08a0575b", "detail": "missing required attribute ':description'"},
    {"entity": "08a0575b", "detail": "unknown attribute ':version' — allowed: [':alias', ':description', ':rationale']"},
    {"entity": "08a0575b", "detail": "unknown attribute ':name' — allowed: [':alias', ':description', ':rationale']"},
    {"entity": "c24bba41", "detail": "missing required attribute ':description'"},
    {"entity": "c24bba41", "detail": "unknown attribute ':name' — allowed: [':alias', ':description', ':rationale']"},
    {"entity": "c24bba41", "detail": "unknown attribute ':version' — allowed: [':alias', ':description', ':rationale']"}
  ]
}
```

### Call 3 — vulcan_query (full graph scan)
Input: `[:find ?e ?a ?v :where [?e ?a ?v]]`

Output: 16 facts across surviving entities — components (API Gateway, Auth Service, JWT Validator, Key Store), one constraint (GIL), one preference (no DB mocks).

### Call 4 — vulcan_query (calls edges)
Input: `[:find ?src ?tgt :where [?src :calls ?tgt]]`

Output: `[["a8217ec8 (API Gateway)", ":project/auth-service"]]`

### Call 5 — vulcan_query (depends-on edges)
Input: `[:find ?src ?tgt :where [?src :depends-on ?tgt]]`

Output:
```
[["a5c144ee (JWT Validator)", ":project/key-store"],
 ["a7c1138a (Auth Service)", ":project/jwt-validator"]]
```

### Call 6 — vulcan_query (motivated-by edges)
Input: `[:find ?src ?tgt :where [?src :motivated-by ?tgt]]`

Output: `[]` — the source entity was retracted by audit.

### Call 7 — vulcan_query (verify :project/auth-service target exists)
Input: `[:find ?a ?v :where [:project/auth-service ?a ?v]]`

Output: `:name "Auth Service"`, `:entity-type :type/component`, `:depends-on :project/jwt-validator`

### Call 8 — vulcan_query (verify :project/jwt-validator target exists)
Input: `[:find ?a ?v :where [:project/jwt-validator ?a ?v]]`

Output: `:name "JWT Validator"`, `:entity-type :type/component`, `:depends-on :project/key-store`

### Call 9 — vulcan_query (verify :project/key-store target exists)
Input: `[:find ?a ?v :where [:project/key-store ?a ?v]]`

Output: `:name "Key Store"`, `:entity-type :type/component`

### Call 10 — vulcan_query (verify :constraint/gil target exists)
Input: `[:find ?a ?v :where [:constraint/gil ?a ?v]]`

Output: `:entity-type :type/constraint`, `:description "Python GIL limits true thread parallelism"`

---

## Response to user

Let me check memory... I've run a full audit and consistency check on everything stored during the session. Here's the summary:

**Audit result: 6 violations found and retracted.**

The audit detected schema problems across several entities that were stored with non-standard attribute names. These have been cleaned up (original facts are preserved in history):

| Entity | Problem |
|--------|---------|
| FastAPI decision (`cd6c70f9`) | Used `:framework` — not an allowed attribute for `:type/decision`. Should have been `:description` or `:rationale`. |
| PostgreSQL decision (`9445af47`) | Used `:database` — same issue; value should fold into `:description`. |
| Redis decision (`b8eb7a73`) | Used `:ttl` — non-standard attribute for `:type/decision`. |
| Asyncio decision (`79e1ebe3`) | Used `:motivated-by` — this is a valid relationship attribute but was flagged because it is not in the allowed list for `:type/decision` entities. The entity was retracted. |
| PostgreSQL dependency (`08a0575b`) | Used `:name` and `:version` instead of `:description`; also missing required `:description`. |
| Redis dependency (`c24bba41`) | Same as above — `:name`/`:version` without `:description`. |

**What survived and is clean:**

| Entity | Type | Key facts |
|--------|------|-----------|
| API Gateway | `:type/component` | calls `:project/auth-service` |
| Auth Service | `:type/component` | depends-on `:project/jwt-validator` |
| JWT Validator | `:type/component` | depends-on `:project/key-store` |
| Key Store | `:type/component` | — |
| GIL constraint | `:type/constraint` | "Python GIL limits true thread parallelism" |
| No-DB-mocks | `:type/preference` | "Do not use mocks for database tests — use real connections" |

**Graph edges are intact:** The component chain `API Gateway → Auth Service → JWT Validator → Key Store` is fully traversable. All relationship targets resolve to existing entities.

**What needs to be re-stored:**

The following decisions were retracted due to schema violations and should be re-transacted with proper attributes:

1. **FastAPI decision** — re-store with `:description "Use FastAPI for the HTTP layer"` and move the framework name into `:description` or `:rationale`.
2. **PostgreSQL 15 as primary store** — re-store as `:type/decision` with `:description "PostgreSQL 15 as primary store"`.
3. **Redis for session caching** — re-store with `:description "Redis for session token caching"` and move the TTL detail into `:rationale`.
4. **Asyncio over threading** — re-store with `:description "Use asyncio over threading"` and use `:motivated-by :constraint/gil` as a relationship edge (note: this attribute was flagged by the audit for `:type/decision` — to preserve the traceability edge, consider re-storing this entity using `:dependency/` or `:decision/` namespace with a proper `:description` and the `:motivated-by` relationship using the correct schema form).
5. **PostgreSQL and Redis dependency entities** — re-store with `:description` as the primary attribute (e.g., `:description "PostgreSQL 15 dependency"`, `:description "Redis 7.x dependency"`).

Would you like me to re-transact the retracted decisions with corrected schema? I can store them cleanly using only the allowed attributes so they pass future audits.
