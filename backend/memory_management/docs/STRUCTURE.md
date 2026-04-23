# memory_management — file-by-file reference

**New integrator? Read [`GETTING_STARTED.md`](GETTING_STARTED.md) first** (install, DB, env, LLM roles, optional §10 working-state patch, tracing). Full index: [`README.md`](README.md).

This document lists **every Python module** in the package, what it does, and how pieces connect. Non-Python assets: `setup_database.sql`, `requirements.txt`, `.env.example`, `pyproject.toml`, markdown docs.

---

## Why “working state” if we already have summary + recent messages?

| Layer | Role |
|--------|------|
| **Compacted summary** | Textual roll-up of *older* dialogue (after compaction). Reduces tokens; loses fine structure. |
| **Recent messages** | Raw last *N* user/assistant turns from `session_conversation_turns` (sliding window). |
| **Working state** | Small **structured JSON** (`SessionState`: optional investigation, hypotheses, progress, **compaction** counters like `turns_since_last_compaction`, `needs_compaction`). |

**Why working state still matters**

1. **Compaction bookkeeping** — Tracks when to summarize, what was already compacted, and merges patches without parsing free text.
2. **Agent / tool flows** — If your pipeline writes **structured** facts (plans, slots, investigation scope), that lives in working state; the summary is prose, not a machine-mergeable object.
3. **State merge** — `merge_working_state_patch` validates and deep-merges JSON so concurrent updates don’t corrupt the session row.

For a **plain Q&A bot** with no tools and no structured state, working state can stay **empty**; summary + recent messages still drive the prompt. The schema is there for **richer** chatbots and for **internal** session/compaction logic.

---

## How the summarizer works (compaction & archive)

- **Who calls the LLM:** The host passes **`llm_summarize: Callable[[str], str]`** into `process_turn()` and `initialize()` (cleanup/archive). The library does **not** import your Bedrock/OpenAI client.
- **Compaction (`workers/compaction.py`):** When triggered, it builds a **large prompt string** (conversation text + rules) and calls `llm_summarize(prompt)`. If `llm_summarize` is missing or `COMPACTION_USE_LLM` is false, it falls back to **truncation** (no LLM).
- **Optional dedicated model:** Host `Settings` / env can set `COMPACTION_LLM_MODEL`; the **API layer** (outside this library) may construct a separate Bedrock client for compaction. Inside the library, only the **injected callable** runs.
- **Mem0 “memory” summarization / extraction:** Uses Mem0 + Azure/LangChain/LiteLLM per `memory/config.py` (fact-extraction prompts are **generic user-fact** prompts, not HR-specific).

---

## Root package

| File | Responsibility |
|------|----------------|
| `__init__.py` | Public exports: `initialize`, `process_turn`, `resolve_ids`, memory + session APIs (`__all__`). |
| `orchestrator.py` | `resolve_ids`, `build_full_context` (returns **`session_context`**, **`user_background`**, then compaction/ids/provider/counts), `join_session_and_user_background`, `process_turn` / `process_turn_async` (**`generate_fn(question, session_context, user_background, **kwargs)`**), `_bump_turns_since_compaction`, `initialize`, `shutdown_cleanup_worker`. **`ProcessTurnResult`** includes **`session_context`**, **`user_background`**, and legacy **`full_context`** (joined). Reads **host `settings`** via `getattr` (duck-typed). |
| `test_smoke.py` | Smoke import / minimal checks. |

---

## `memory/`

| File | Responsibility |
|------|----------------|
| `__init__.py` | Re-exports memory operations (`set_memory_config`, `get_memory_provider`, `add`, `search`, …). |
| `config_holder.py` | `set_memory_config`, `get_memory_config`, `_get(key)` — dict or object attributes. |
| `config.py` | Builds Mem0 config: Azure vs LiteLLM vs LangChain Bedrock; embedder; FAISS path; `_litellm_fact_extraction_prompt`. |
| `adapter.py` | Mem0 init, FAISS search patch, keyword fallbacks, optional **Opik** span metadata (`opik`, `opik_template` optional). |
| `mem0_provider.py` | `MemoryProvider` implementation wrapping adapter. |
| `noop_provider.py` | No-op memory when disabled. |
| `provider_factory.py` | Returns Mem0 or noop from `_get("memory_enabled")`, `_get("memory_provider")`. |
| `protocol.py` | `MemoryProvider` protocol. |

---

## `session_manager/`

| File | Responsibility |
|------|----------------|
| `__init__.py` | Re-exports core config, schema, services, storage facades, cleanup. |

### `session_manager/core/`

| File | Responsibility |
|------|----------------|
| `config.py` | `SessionConfig` Pydantic model; `session_config_from_settings(any)`; `session_config_from_env()` (reads `SESSION_*`, `COMPACTION_*` from **os.environ**). |
| `schema.py` | Pydantic `SessionState`, `CompactionState`, investigation/progress types. |
| `protocol.py` | `SessionProvider` interface. |
| `__init__.py` | Package marker. |

*(Legacy duplicates at `session_manager/config.py`, `schema.py`, `protocol.py` mirror `core/` for old import paths.)*

### `session_manager/services/`

| File | Responsibility |
|------|----------------|
| `context_builder.py` | `build_session_context`: load events, sliding window, defer compaction (`needs_compaction`), assemble prompt sections (summary + recent + **working state** string). Optional Opik. |
| `provider_factory.py` | `get_session_provider(config=None)`: if None, uses **`session_config_from_env()`** (no parent app). |
| `state_merge.py` | `merge_working_state_patch` — validate with `SessionState`. Integrator guide: [`WORKING_STATE_LLM_PATCH.md`](WORKING_STATE_LLM_PATCH.md). |
| `working_state_extract.py` | Parse LLM output for optional structured patches (`PATCH_MARKER`, etc.). Integrator guide: [`WORKING_STATE_LLM_PATCH.md`](WORKING_STATE_LLM_PATCH.md). |
| `__init__.py` | Exports. |

### `session_manager/storage/`

| File | Responsibility |
|------|----------------|
| `backend.py` | `SessionBackend`: Postgres CRUD for `session_registry`, `session_working_state`, `session_conversation_turns`, archive, TTL, list. **Injected** `get_connection`. |
| `postgres_provider.py` | `PostgresSessionProvider`: `get_or_create_session`, append events, `update_working_state`, load events. |
| `session_services.py` | Module-level facade over backend (default backend injectable). |
| `noop_provider.py` | No-op session provider. |
| `__init__.py` | Exports. |

### `session_manager/workers/`

| File | Responsibility |
|------|----------------|
| `compaction.py` | `run_compaction`, `apply_sliding_window`, `apply_state_collapse`; calls **injected** `llm_summarize(prompt)` with **built-in** summarization prompt text. Optional Opik. |
| `cleanup.py` | `run_cleanup_once`, `run_cleanup_loop`, expired session archive/delete; `get_cleanup_config_from_env()` (**os.environ only**); `get_cleanup_config_from_settings` (duck-typed object). CLI `__main__` does **not** import host app. |
| `__init__.py` | Exports. |

### `session_manager/observability/`

| File | Responsibility |
|------|----------------|
| `db_metrics.py` | Contextvar-scoped DB timing per request. |
| `working_state_trace.py` | JSONL trace file; path from **`WORKING_STATE_TRACE_FILE`** env only. |
| `activity_log.py` | Optional activity helpers. |
| `__init__.py` | Exports. |

### Other `session_manager/` files

| File | Responsibility |
|------|----------------|
| `setup_db.py` | Apply/verify DB schema. |
| `example_usage.py` | Examples (env-based DB connect). |
| `test_import.py` | Import smoke without optional tracing. |
| `build.py` | Legacy build helper (if present). |

---

## Data flow (one request)

1. `process_turn` → `resolve_ids` → `build_full_context` → `get_or_create_session` + `build_session_context` + Mem0 `search` → **`session_context`**, **`user_background`**.
2. Append user event → **`generate_fn(question, session_context, user_background)`** (**host** LLM/RAG).
3. Append assistant event → bump compaction turn counter → Mem0 `add`.
4. If compaction queued → `run_compaction` with **`llm_summarize`** from host.

---

## See also

- `ENV_VARS.md` — every environment variable and memory key.
- `LIBRARY_INDEPENDENCE.md` — checklist (config, imports, prompts, tests).
- `INTEGRATION.md` — host wiring.
