# Eval 10 — With Skill
## Tool calls
### Call 1 — memory_prepare_turn
Input: `user_message="What framework are we using for the API layer?"`
Output:
```
Relevant memory context:
  cd6c70f9-7b80-5bf2-aedc-161f5825118f | :entity-type | :type/decision
  cd6c70f9-7b80-5bf2-aedc-161f5825118f | :framework    | FastAPI
  cd6c70f9-7b80-5bf2-aedc-161f5825118f | :reason       | async support preferred over Flask; aligns with asyncio concurrency model
  cd6c70f9-7b80-5bf2-aedc-161f5825118f | :description  | API framework selection
```

## Response to user
Let me check memory...

We are using **FastAPI** for the API layer. Memory records this as an explicit decision: FastAPI was chosen over Flask because async support was required — it aligns with our asyncio concurrency model.
