# memory_management – Integration Guide

**Start with [`GETTING_STARTED.md`](GETTING_STARTED.md)** (install → DB → env → LLM roles → optional §10 working-state patch → tracing).

This document adds detail for host apps. For variable lookup use [`ENV_VARS.md`](ENV_VARS.md). Full index: [`README.md`](README.md).

## Library independence (important)

- The package **must not** import host modules such as `src.config`. Configuration is supplied by:
  - **`initialize(settings, get_connection, ...)`** and **`set_memory_config(...)`** (recommended), and/or
  - **Environment variables** read by `session_config_from_env()` and `get_cleanup_config_from_env()`.
- If you call **`get_session_provider()` with no `config` argument**, the library builds config **only from `os.environ`** (`SESSION_*`, `COMPACTION_*`, …). Call `load_dotenv()` in the host app first so `.env` values are visible.
- Prefer **`get_session_provider(session_config_from_settings(your_settings))`** when you already have a Pydantic settings object.
- The cleanup CLI **does not** import your app’s LLM; pass a custom wrapper if you need LLM-based archive summaries.

---

## 0. Simplified Integration (Preferred)

Use two calls: `initialize()` at startup and `process_turn()` per request.

**Startup:**
```python
from memory_management import initialize
initialize(settings, db_manager.get_connection, llm_summarize=lambda p: llm.generate_response(prompt=p) or "")
```

**Per request:**
```python
from memory_management import process_turn
result = process_turn(question, user_id, run_id, generate_fn, settings, llm_summarize=lambda p: llm.generate_response(prompt=p) or "")
# result.success, result.answer, result.run_id, result.user_id
```

**`generate_fn(question, session_context, user_background, **kwargs)`** must return the answer (`str` or object with `.content`). The library handles ID resolution, context build (two strings), append user/assistant, Mem0 add, compaction, and cleanup worker. Pass **`session_context`** and **`user_background`** into your LLM as **separate labeled blocks**; see **[GETTING_STARTED.md](GETTING_STARTED.md) §6b** for the suggested system prompt addendum. **`ProcessTurnResult.full_context`** is still the joined string for logging/backward compatibility.

**LLM working-state patches:** `process_turn` **does not** parse `WORKING_STATE_PATCH` from the model or write those updates to Postgres. The host must add the prompt, parse the reply (or attach a patch from structured output), and **after** a successful `process_turn` call `merge_working_state_patch` + `provider.update_working_state`. See **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**.

**Migration from manual integration:** Replace the multi-step wiring in your pipeline with a single `process_turn()` call. Your RAG/generate step becomes the `generate_fn`. See §1–§9 for details of what the library does internally.

---

## 1. Overview of library responsibilities

The **memory_management** library is fully responsible for:

| Responsibility | What the library does |
|----------------|------------------------|
| **Session management** | Get/create session by (user_id, run_id), build session context (summary + recent messages + working state), append user/assistant events. |
| **Chat persistence** | Append user message before the LLM response and assistant message after; events are stored in the session DB so the next request sees the full turn. |
| **Mem0 (read + write)** | **Search**: fetch user memories for the current question and inject into the prompt. **Add**: after each response, add the turn (user + assistant) to Mem0 so facts persist across conversations. |
| **ID handling** | The app resolves `user_id` and `run_id` via `resolve_ids()` so the system works even when the frontend sends no IDs; the library uses whatever IDs the app passes. |

**Host-only (not done inside `process_turn`):**

| Responsibility | What the host does |
|----------------|-------------------|
| **LLM-driven `working_state` updates** | Prompt with `WORKING_STATE_PATCH:` protocol; parse; expose patch on `generate_fn` return value; after `process_turn`, merge and `update_working_state`. See **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**. |

The host app is responsible for: calling the library at the right points (startup, before routing, after response), passing config and a DB connection factory, returning `run_id` and `user_id` in the API response, and (if used) persisting LLM working-state patches as above.

---

## 2. ID handling strategy (auto-generation + fallback)

**Function:** `resolve_ids(user_id: Optional[str], run_id: Optional[str]) -> (user_id, run_id)`

- **If run_id is missing or empty** → generate a new UUID (new conversation).
- **If user_id is missing or empty** → fall back to `run_id` so the conversation is still scoped (session and Mem0 work without the frontend sending user_id).
- **If both are provided** → use them as-is; do not override.

This removes the frontend dependency: the backend always has valid IDs. The app returns `run_id` and `user_id` in the response so the client can send them back for the next request and maintain session continuity.

---

## 3. Session management flow

1. **Startup (once)**  
   - App calls `set_memory_config(settings)` for Mem0.  
   - App creates `SessionBackend(get_connection)`, calls `set_default_backend(backend)`, and sets `provider_factory._app_session_backend = backend`.  
   - Session tables must exist (`session_registry`, `session_working_state`, `session_conversation_turns`).

2. **Per request**  
   - App resolves IDs: `user_id, run_id = resolve_ids(request.user_id, request.run_id)`.  
   - **`process_turn`** (recommended) calls **`build_full_context`** internally and yields **`session_context`** and **`user_background`** to **`generate_fn`**. If you integrate manually, `build_full_context(...)` returns **`(session_context, user_background, ...)`**—pass both into the LLM separately; do not nest User Background inside the Session Context wrapper.  
   - The library appends the **user** event before the LLM and the **assistant** event after.  
   - If compaction was queued, run **`run_compaction(...)`** (sync is fine).

The same **two** strings should be passed to **both** standard RAG and SuperRAG (complex) paths so meta-questions behave consistently. See **GETTING_STARTED.md §6b** for prompt guidance.

---

## 4. Mem0 integration (search + add)

**Search (read)**  
- Inside **`build_full_context`**, after building session context, the library calls **`get_memory_provider().search(question, user_id=user_id)`**.  
- Hits are formatted as a **`--- User Background ---`** … **`--- End User Background ---`** block returned as the second string (**`user_background`**). **`session_context`** contains only this thread. Both are passed to **`generate_fn`**; the host should forward them as **separate** labeled sections in the chat prompt.

**Add (write)**  
- After generating the response, the app calls `add(messages=[{"role":"user","content":question},{"role":"assistant","content":answer.content}], user_id=user_id)`.  
- This persists the turn so future searches can return stored facts (e.g. for "what do you know about me").  
- If the app never called `add()`, memory would never be stored; this step is required for persistence.

---

## 5. Chat persistence flow

- **Append user event** → **before** the LLM response (so this turn’s user message is in the session before we generate).  
- **Generate response** → standard RAG or SuperRAG with **`session_context`** and **`user_background`** (not a single merged blob into the session wrapper).  
- **Append assistant event** → **after** the response (so the full turn is in the session).  
- **Mem0 add** → after the response (so the turn is stored in memory).  
- **Compaction** → if `compaction_queued` from `build_session_context`, run `run_compaction(...)` so history is summarized and token usage stays bounded.

This order ensures session continuity: the next request with the same `run_id` and `user_id` will see the prior turns in `build_session_context`.

---

## 6. Compaction behavior

- `build_session_context` may return `compaction_queued=True` when the turn count (or token budget) exceeds the configured threshold.  
- The app must then call `run_compaction(run_id, user_id, provider, llm_summarize=..., turns_threshold=..., token_budget=..., keep_last_n=...)`.  
- Compaction summarizes older events into a compacted summary and keeps the last N events; it prevents unbounded growth and keeps context usable.  
- Previously, the app only marked compaction as queued and never ran it, so compaction was ineffective. Running it (sync is fine) fixes that.

---

## 7. Expected app responsibilities (minimal)

- **Startup:** Load env, run DB setup for session tables if needed, call `set_memory_config(settings)`, wire session backend (`SessionBackend(get_connection)`, `set_default_backend`, `provider_factory._app_session_backend`).  
- **Per request:** Prefer **`process_turn`** (handles context build, append user/assistant, Mem0 add, compaction flags). **`generate_fn`** receives **`session_context`** and **`user_background`** separately; wire both into your LLM. Return **`run_id`** and **`user_id`** in the response.  
- **Resilience:** On session backend or memory provider failure, log and continue without session/memory so the request still succeeds.

---

## 8. Example request/response

**Request (new conversation; no IDs):**
```json
{
  "question": "What is the leave policy?",
  "user_id": null,
  "run_id": null
}
```

**Response:**
```json
{
  "success": true,
  "answer": "...",
  "answer_id": "...",
  "run_id": "a1b2c3d4-...",
  "user_id": "a1b2c3d4-...",
  "sources": [...],
  "generation_time_ms": 1200
}
```

**Next request (continuation):**
```json
{
  "question": "What about maternity leave?",
  "user_id": "a1b2c3d4-...",
  "run_id": "a1b2c3d4-..."
}
```

The frontend **must** send back the same `run_id` and `user_id` for the same conversation. If the client never sends `run_id`, every request gets a new conversation (new session), so the model cannot resolve "it"/"that", summarize this chat, or use prior turns. IDs can be sent in the request body or via headers `X-Run-Id` and `X-User-Id`.

---

## 9. Session expiry and cleanup

- **TTL:** Sessions have a TTL (`session_ttl_seconds`; default 1800). After that period of inactivity, the session is considered expired.
- **Cleanup worker:** The app should run the library’s cleanup periodically (e.g. `run_cleanup_once` every `session_cleanup_interval_seconds`). This archives (or deletes) expired sessions so the `session_working_state` table does not accumulate endless "active" rows.
- **This app:** A background task in the API lifespan runs `run_cleanup_once` on the configured interval, with `archive_on_expiry` and `cleanup_archive_limit` from settings. Expired sessions are archived (summary stored, status set to `archived`) so they no longer appear as active.
