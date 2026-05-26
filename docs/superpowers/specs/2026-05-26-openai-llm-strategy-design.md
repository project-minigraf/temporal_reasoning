# OpenAI Support for LLM Extraction Strategy + Codex Hook Wiring

**Date:** 2026-05-26
**Scope:** `mcp_server.py`, `hooks/codex.toml`, tests

---

## Problem

The `llm` extraction strategy is Anthropic-only. The Codex hook config has auto-memory hooks commented out. Codex users running against OpenAI models have no supported path.

## Goal

- `VULCAN_LLM_MODEL=gpt-4o-mini` (or any OpenAI model name) selects the OpenAI client automatically — no separate strategy or provider env var needed.
- Codex hook wiring is fully documented and ready to use.

---

## Architecture

### Provider detection

```python
_OPENAI_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")

def _is_openai_model(model: str) -> bool:
    return any(model.startswith(p) for p in _OPENAI_MODEL_PREFIXES)
```

Model names not matching any prefix fall through to the Anthropic client. This is a closed set of current OpenAI prefixes; new providers are added by extending the tuple.

### New helpers

**`_get_openai_client()`** — symmetric to `_get_anthropic_client()`. Requires `openai` package (`pip install openai`) and `OPENAI_API_KEY` env var. Raises `RuntimeError` with a clear message if either is missing.

**`_call_llm(model: str, prompt: str) -> str`** — dispatches to the right client based on `_is_openai_model(model)`:
- Anthropic path: `client.messages.create(model=model, max_tokens=1024, messages=[...])` → `message.content[0].text`
- OpenAI path: `client.chat.completions.create(model=model, max_tokens=1024, messages=[...])` → `response.choices[0].message.content`

Both paths return the raw text string. All subsequent logic (valid-at hint parsing, transact, fallback) is unchanged.

### `_llm_extract_and_transact` change

Replace the inline Anthropic client call:
```python
client = _get_anthropic_client()
message = client.messages.create(...)
raw_facts = message.content[0].text.strip()
```
with:
```python
raw_facts = _call_llm(model, prompt)
```

Everything else in the function is unchanged.

---

## `hooks/codex.toml` changes

Uncomment the hooks block. Add `OPENAI_API_KEY`, `VULCAN_LLM_MODEL`, and update `VULCAN_EXTRACTION_STRATEGY` to `llm`.

```toml
[mcp_servers."temporal-reasoning"]
command = ["python", "PATH_TO_REPO/mcp_server.py"]

[mcp_servers."temporal-reasoning".env]
MINIGRAF_GRAPH_PATH = "PATH_TO_PROJECT/memory.graph"
VULCAN_EXTRACTION_STRATEGY = "llm"
VULCAN_LLM_MODEL = "gpt-4o-mini"
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"

[hooks.pre_turn]
command = ["python", "PATH_TO_REPO/hooks/prepare_hook.py"]
timeout_ms = 5000

[hooks.post_turn]
command = ["python", "PATH_TO_REPO/hooks/finalize_hook.py"]
timeout_ms = 10000
```

No changes to `prepare_hook.py` or `finalize_hook.py` — they are already provider-agnostic.

---

## Tests

New `TestCallLlm` class:
- `test_is_openai_model_gpt` — `gpt-4o-mini` → True
- `test_is_openai_model_o_series` — `o1`, `o3-mini`, `o4` → True
- `test_is_openai_model_claude` — `claude-haiku-4-5-20251001` → False
- `test_call_llm_anthropic_path` — patches `_get_anthropic_client`, asserts `messages.create` called
- `test_call_llm_openai_path` — patches `_get_openai_client`, asserts `chat.completions.create` called

New `TestLlmStrategyOpenAI` class (mirrors existing `TestLlmStrategy`):
- `test_calls_openai_api` — sets `VULCAN_LLM_MODEL=gpt-4o-mini`, patches `_get_openai_client`, verifies transact succeeds and `stored_count > 0`
- `test_falls_back_to_agent_on_openai_failure` — OpenAI client raises, verifies fallback to `agent` strategy

---

## Files changed

| File | Change |
|------|--------|
| `mcp_server.py` | Add `_OPENAI_MODEL_PREFIXES`, `_is_openai_model`, `_get_openai_client`, `_call_llm`; simplify `_llm_extract_and_transact` |
| `hooks/codex.toml` | Uncomment hooks; add OpenAI env vars |
| `tests/test_mcp_server.py` | Add `TestCallLlm` and `TestLlmStrategyOpenAI` |

No other files change.

---

## Non-goals

- No `VULCAN_LLM_PROVIDER` env var — model name is sufficient.
- No changes to `heuristic` or `agent` strategies.
- No changes to `prepare_hook.py` or `finalize_hook.py`.
- No SKILL.md or README changes (Codex hook wiring is already documented as "uncomment when supported").
