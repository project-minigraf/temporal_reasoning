# Eval 3 — With Skill

## Tool calls

### Call 1 — memory_prepare_turn
Input:
```
user_message: "Can you add a test for the user registration endpoint? Make sure it fits with how we do things."
```
Output:
```
Relevant memory context:
  e540bbf7-dd00-54e3-896b-7c90b76ffb63 | :description | Do not use mocks for database tests — use real connections
  e540bbf7-dd00-54e3-896b-7c90b76ffb63 | :reason      | mocked tests passed but prod migration failed last quarter
  e540bbf7-dd00-54e3-896b-7c90b76ffb63 | :entity-type | :type/preference
```

### Call 2 — vulcan_query
Input:
```
datalog: "[:find ?e ?a ?v :where [?e ?a ?v]]"
```
Output:
```json
{
  "ok": true,
  "results": [
    ["e540bbf7-dd00-54e3-896b-7c90b76ffb63", ":reason", "mocked tests passed but prod migration failed last quarter"],
    ["e540bbf7-dd00-54e3-896b-7c90b76ffb63", ":description", "Do not use mocks for database tests — use real connections"],
    ["e540bbf7-dd00-54e3-896b-7c90b76ffb63", ":entity-type", ":type/preference"]
  ]
}
```

### Call 3 — memory_finalize_turn
Input:
```
conversation_delta: "User: Can you add a test for the user registration endpoint? Make sure it fits with how we do things.\n\nAgent: Memory surfaced a stored preference: do not use mocks for database tests — use real connections (reason: mocked tests passed but prod migration failed last quarter). The test was written using a real database connection, consistent with this preference. No mocks were used for the DB layer."
```
Output:
```json
{"ok": true, "stored_count": 1, "strategy": "llm"}
```

## Response to user

Let me check memory before writing anything.

Memory has a stored preference that applies directly here:

> **Do not use mocks for database tests — use real connections.**
> Reason: mocked tests passed but a prod migration failed last quarter because of mock/prod divergence.

Here's a user registration endpoint test that follows that preference — it uses a real database connection rather than mocking the DB layer:

```python
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from myapp.database import Base
from myapp.models import User
from myapp.api import app
from fastapi.testclient import TestClient

# Real DB — no mocks. Preference stored in memory: mock/prod divergence
# caused a silent migration failure last quarter.
DATABASE_URL = "postgresql://testuser:testpass@localhost:5432/testdb"

@pytest.fixture(scope="function")
def db_session():
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()
    Base.metadata.drop_all(engine)

@pytest.fixture(scope="function")
def client(db_session):
    # Override the app's DB dependency with the real test session
    from myapp.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()

def test_register_user_success(client, db_session):
    payload = {
        "username": "alice",
        "email": "alice@example.com",
        "password": "securepassword123",
    }
    response = client.post("/users/register", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["username"] == "alice"
    assert data["email"] == "alice@example.com"
    assert "id" in data

    # Verify the record actually landed in the real DB
    user = db_session.query(User).filter_by(email="alice@example.com").first()
    assert user is not None
    assert user.username == "alice"

def test_register_user_duplicate_email(client, db_session):
    payload = {
        "username": "alice",
        "email": "alice@example.com",
        "password": "securepassword123",
    }
    client.post("/users/register", json=payload)  # first registration
    response = client.post("/users/register", json=payload)  # duplicate

    assert response.status_code == 409
    assert "already registered" in response.json()["detail"].lower()

def test_register_user_invalid_email(client):
    payload = {
        "username": "bob",
        "email": "not-an-email",
        "password": "securepassword123",
    }
    response = client.post("/users/register", json=payload)
    assert response.status_code == 422
```

Key points matching how we do things:

- **Real database connection** — a live PostgreSQL test DB is spun up and torn down per test. No `MagicMock`, no `patch("db.session")`.
- **Actual persistence verified** — `test_register_user_success` queries the DB directly after the HTTP call to confirm the row exists, not just that the response looked right.
- **Rollback-based isolation** — each test rolls back rather than truncating, keeping tests fast without sacrificing real-DB fidelity.

Adjust `DATABASE_URL`, the model import path, and the dependency override pattern to match your actual app structure. If you have a fixture that already manages the test DB connection, point these tests at it instead of defining a new one.
