# Getting started — read this first

One linear path from zero to a working integration. Everything else (`STRUCTURE.md`, `ENV_VARS.md`) is reference.

---

## 1. Install the package

From your project root (parent of `memory_management/`):

```bash
pip install -r memory_management/requirements.txt
pip install -e ./memory_management
```

You now have `memory_management` importable in Python.

---

## 2. Create database tables

The library supports **PostgreSQL** or **SQLite** for session storage.

- **PostgreSQL:** `memory_management/session_manager/setup_database.sql`
- **SQLite:** `memory_management/session_manager/setup_database_sqlite.sql`

Run the script once against the DB your app will use (psql, DBeaver, sqlite3, or your migration tool):

```bash
# example — adjust URL
psql "$DATABASE_URL" -f memory_management/session_manager/setup_database.sql
```

No `hr_` or app-specific names: tables are `session_registry`, `session_working_state`, `session_conversation_turns`, etc.

---

## 3. Load environment variables

1. Copy **`memory_management/.env.example`** into your app as **`.env`** (or merge keys into your existing file).
2. At process startup, **before** importing code that builds sessions from env:

```python
from dotenv import load_dotenv
load_dotenv()
```

3. Fill **Postgres** (`DB_*` or `DATABASE_URL`) and **AWS** (for Bedrock / Mem0 embeddings) at minimum.

---

## 4. Understand the three different “LLM” roles

People get confused because **three separate things** can call an LLM. They are independent. A **fourth** item is optional: structured **working state** from the chat model — see **§10**.

| # | Role | Who owns it | How it is configured |
|---|------|-------------|----------------------|
| **A** | **Your chat / RAG answer** | **Your app** | Your `generate_fn` in `process_turn` (e.g. Bedrock RAG). Not configured by this library’s env beyond what you pass in code. |
| **B** | **Compaction** (summarize old turns in the **session**) | **Library** calls a function **you pass in** | You pass `llm_summarize=lambda prompt: ...` to `initialize()` and `process_turn()`. **Env `COMPACTION_*` only toggles behaviour** (thresholds, `COMPACTION_USE_LLM`); it does **not** create a Bedrock client inside the library. |
| **C** | **Mem0** (extract/search **user facts**) | **Library** via Mem0 | Configured through **`set_memory_config`** (same keys as in `.env.example`): Azure **or** LangChain Bedrock **or** LiteLLM — see §6. |
| **(opt)** | **LLM `WORKING_STATE_PATCH`** (structured session JSON) | **Your app** (prompt + parse + merge after `process_turn`) | Full checklist: **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**. Not configured by library env; `process_turn` does **not** persist these patches. |

**Important:** `COMPACTION_LLM_MODEL` in **env** is meaningful only if **your host application** reads it and uses it to build the **`llm_summarize`** callable (e.g. instantiate a small Bedrock model for compaction only). The **library** does not read `COMPACTION_LLM_MODEL` by itself; it only calls whatever function you pass.

---

## 5. Wire startup (minimal)

```python
from dotenv import load_dotenv
load_dotenv()

from memory_management import initialize, process_turn

# `settings` = your Pydantic BaseSettings or SimpleNamespace with attributes
# listed in ENV_VARS.md (session_*, memory_*, compaction_*, aws_*, …)
initialize(
    settings,
    get_connection,  # callable: () -> psycopg2 connection (or context manager contract your SessionBackend expects)
    llm_summarize=build_compaction_summarizer(settings),  # see §7
    start_cleanup_worker=True,
)
```

`initialize` internally calls **`set_memory_config(settings)`**, so all **`memory_*`** keys must be readable from `settings` (or use a dict — see `set_memory_config`).

---

## 6. Mem0: which path am I on?

Mem0 needs an **LLM for fact extraction** and **Bedrock for embeddings** (in the default setup). Pick **one** LLM path:

| You set | Mem0 uses for extraction |
|---------|---------------------------|
| `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_KEY` + `AZURE_OPENAI_DEPLOYMENT_NAME` | **Azure OpenAI** |
| Default (no Azure) + `MEMORY_LLM_PROVIDER=langchain` or unset | **LangChain + Bedrock** (`AWS_BEDROCK_MODEL`, region) |
| `MEMORY_LLM_PROVIDER=litellm` | **LiteLLM** — set `MEMORY_LITELLM_MODEL` e.g. `bedrock/us.amazon.nova-pro-v1:0` |

Also set:

- `MEMORY_ENABLED=true`
- `MEMORY_VECTOR_STORE` — `qdrant` (default) or `faiss`. For Qdrant: `QDRANT_HOST`, `QDRANT_PORT`, optional `MEM0_QDRANT_COLLECTION`. For FAISS: `MEMORY_FAISS_PATH` — writable folder (relative to **cwd** unless absolute). **Docker + verification:** **[QDRANT_SETUP.md](QDRANT_SETUP.md)**.

If `MEMORY_ENABLED=false` or `MEMORY_PROVIDER=noop`, Mem0 is not used; no Mem0 LLM path applies.

**Reranker (cross-encoder):** upstream Mem0 may load `cross-encoder/*` models with `SentenceTransformer`. This library applies a one-time **`CrossEncoder` patch** at Mem0 init when `MEM0_PATCH_CROSS_ENCODER_RERANKER` is true (default). Upgrade the SDK with `pip install -U mem0ai` from PyPI; you do **not** need a fork unless you prefer an upstream fix over the patch.

### 6b. Host chat LLM: two context strings + suggested system addendum

`process_turn` calls your **`generate_fn` with three positional arguments:**

`generate_fn(question, session_context, user_background, **kwargs)`

| Argument | Meaning |
|----------|---------|
| **`session_context`** | This **`run_id`** only: compacted summary, recent user/assistant lines, working state (from `build_session_context`). |
| **`user_background`** | Mem0 search hits for this **`user_id`**, formatted as a `--- User Background ---` … `--- End User Background ---` block, or empty if none. |

**Host responsibilities:** Pass both strings into your LLM/RAG as **separate labeled regions** (do **not** put User Background inside the Session Context wrapper). The library also exposes **`result.full_context`** as the legacy join of the two strings for logging only—prefer the two arguments for generation.

**Suggested system prompt addendum** (copy into your chat model system instructions; matches the intent in the reference HR app’s `generate_hr_response`):

```text
Context roles (do not merge):
- Session Context = this conversation thread only (messages/summary/working state for this chat).
- User Background = stored facts about this user from memory; use when relevant to answer, but do not treat as lines spoken in this thread unless they also appear in Session Context.
- Retrieved handbook/FAQ chunks = company policy sources; not part of "our chat" unless the user discussed them in Session Context.

If the user asks to summarize, recap, or review this chat or our conversation: summarize only from Session Context. Do not recast handbook content or User Background as prior dialogue. If Session Context has no earlier thread content, say there is nothing to summarize yet in this thread.
```

If models still treat retrieved documents as “the conversation,” add a **pipeline guard** (e.g. skip or reduce RAG for pure summarize intent); prompt alone may not be enough.

---

## 7. Compaction summarizer: how to set `COMPACTION_LLM_MODEL` “for real”

1. **`COMPACTION_USE_LLM=false`** → no LLM for compaction; truncation only. **`llm_summarize` can still be passed but will not be used for summarization** when the library chooses truncate-only paths — keep `COMPACTION_USE_LLM=true` to use the callable.

2. **`COMPACTION_USE_LLM=true`** → the library calls **`llm_summarize(prompt)`** when compaction runs. You implement that callable:
   - **Option A — one model for everything:** use the same Bedrock client as your RAG.
   - **Option B — cheaper model for compaction:** in your app, read `COMPACTION_LLM_MODEL` from settings/env and construct a **second** client (e.g. `BedrockLLMService(model_id=settings.compaction_llm_model)`), and pass `llm_summarize` that delegates to it.

Example pattern (pseudo-code):

```python
def build_compaction_summarizer(settings):
    if not settings.compaction_use_llm:
        return lambda p: ""  # library still may truncate; see compaction worker
    if settings.compaction_llm_model:
        small = BedrockLLMService(model_id=settings.compaction_llm_model)
        return lambda prompt: small.generate_response(prompt=prompt, system_prompt="...", temperature=0.1) or ""
    return default_rag_llm_summarize  # same as chat model
```

The **reference HR app** in this repo does exactly that in `src/api/endpoints.py` (`_get_compaction_config`). Copy that pattern into your app.

---

## 8. Tracing and debug output

There is **no** single `TRACING_ENABLED` env var inside `memory_management`. Use these instead:

| What you want | What to set |
|----------------|-------------|
| **Opik** cloud/self-hosted tracing | Install `opik` (in `requirements.txt`). Set **`OPIK_API_KEY`**, **`OPIK_WORKSPACE`**, **`OPIK_PROJECT_NAME`** as per [Opik SDK docs](https://www.comet.com/docs/opik/tracing/sdk_configuration). Optional: `opik_template` for `@trace` decorators in some modules. |
| **Turn off Opik** | **`OPIK_TRACK_DISABLE=true`** (or `1`) per Opik docs — no spans sent. |
| **Working-state merge debug JSONL** | **`WORKING_STATE_TRACE_FILE`** — path to a `.jsonl` file, or `0` / `false` / `off` to disable. |

If `opik` is not installed, tracing decorators in the library **no-op** — your app still runs.

---

## 9. Per request

```python
result = process_turn(
    question,
    user_id,
    run_id,
    generate_fn=lambda q, sc, ub: your_pipeline.answer(
        q, session_context=sc, user_background=ub
    ),
    settings=settings,
    llm_summarize=same_summarizer_as_initialize,
)
```

Return **`result.run_id`** and **`result.user_id`** in your HTTP response so the client sends them on the next turn.

---

## 10. Next step: LLM `WORKING_STATE_PATCH` (optional)

Do not stop here if you want the model to **update structured working state** (progress, investigation, hypotheses, constraints) stored in Postgres and shown in the next turn’s session context.

- **`process_turn` does not** parse or save `WORKING_STATE_PATCH` lines from the model. The **host** must handle that after a successful turn.
- Add the prompt instruction and **`WORKING_STATE_PATCH:`** protocol to your **chat** LLM prompt (copy-paste block in the guide below).
- In **`generate_fn`**: call the LLM → `parse_llm_response_for_working_state` → return an object whose **`.content`** (or `str(...)`) is the **clean** user-visible answer, with **`working_state_patch`** set when present.
- **After** `process_turn` succeeds: **`merge_working_state_patch`** + **`result.provider.update_working_state`** (session `version`), same pattern as the reference app.

**Full steps, canonical prompt, imports, troubleshooting:** **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**

---

## 11. If something is wrong

| Problem | Check |
|---------|--------|
| No session rows | `SESSION_ENABLED`, DB connection, SQL script applied, same DB as `get_connection` |
| Mem0 errors | `set_memory_config` ran, AWS/Azure keys, Qdrant reachable (`QDRANT_HOST`/`QDRANT_PORT`) or `MEMORY_FAISS_PATH` writable when using FAISS |
| Compaction never runs | Turn thresholds (`COMPACTION_TURNS_THRESHOLD`), `COMPACTION_USE_LLM`, and that **`llm_summarize`** is not empty when compaction runs |
| Wrong session config | `get_session_provider()` with no args uses **only `os.environ`** — call `load_dotenv()` first, or pass `session_config_from_settings(settings)` explicitly |

---

## Doc map

All under **`memory_management/docs/`** (see **[docs/README.md](README.md)**).

| File | Use when |
|------|----------|
| **GETTING_STARTED.md** (this file) | First-time integration |
| **ENV_VARS.md** | Lookup every variable and key |
| **STRUCTURE.md** | Code navigation |
| **INTEGRATION.md** | Deeper host responsibilities |
| **WORKING_STATE_LLM_PATCH.md** | LLM `WORKING_STATE_PATCH` prompt, parse, post-`process_turn` merge |
| **LIBRARY_INDEPENDENCE.md** | Audit / packaging |
| **docs/cursor/*.mdc** | Cursor rules — copy to workspace `.cursor/rules/` |
