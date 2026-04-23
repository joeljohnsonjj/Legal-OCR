# Environment & config reference

**Read [`GETTING_STARTED.md`](GETTING_STARTED.md) first** — it explains how compaction vs Mem0 vs RAG LLMs relate. This file is a **lookup table**. Index: [`README.md`](README.md).

---

## How config enters the library

1. **`os.environ`** — Used by `session_config_from_env()`, `get_cleanup_config_from_env()`, `WORKING_STATE_TRACE_FILE`, Opik SDK, boto3. Call **`load_dotenv()`** in your app before relying on these.
2. **`set_memory_config(x)`** — `x` is a **dict** or any object with attributes below (`memory_*`, `aws_*`, …). `initialize()` calls `set_memory_config(settings)` for you.
3. **`process_turn(..., settings=...)`** — `getattr(settings, "session_enabled")`, etc. Usually the **same** object you passed to `initialize`, often Pydantic `BaseSettings` loaded from `.env`.

There is **no** `TRACING_ENABLED` in this library. Use **`OPIK_TRACK_DISABLE`** (Opik) or omit Opik packages.

---

## Session & compaction (environment)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SESSION_ENABLED` | `true` | `false` → session provider becomes noop when using env-only provider config. |
| `SESSION_PROVIDER` | `postgres` | `postgres`, `sqlite`, or `noop`. |
| `SESSION_DB_PATH` | empty | SQLite db file path when `SESSION_PROVIDER=sqlite`. |
| `SESSION_TTL_SECONDS` | `1800` | Idle TTL (seconds). |
| `SESSION_CLEANUP_INTERVAL_SECONDS` | `300` | Cleanup loop sleep (seconds). |
| `COMPACTION_ENABLED` | `true` | Master switch for compaction pipeline. |
| `COMPACTION_TURNS_THRESHOLD` | `10` | Queue compaction when turn counter ≥ this. |
| `COMPACTION_KEEP_LAST_N` | `20` | Events kept after compaction slice. |
| `COMPACTION_MAX_TURNS` | `10` | Recent-turn window for **context text**. |
| `COMPACTION_TOKEN_BUDGET` | empty | Optional extra trigger when estimated tokens exceed. |
| `COMPACTION_USE_LLM` | `true` | `false` → **truncate only** for compaction (no `llm_summarize` use for summarization). |
| `COMPACTION_LLM_MODEL` | empty | **Host app only**: Bedrock model id if **your code** builds a dedicated compaction client; **not read by the library**. |

---

## Cleanup / archive (environment)

| Variable | Default | Meaning |
|----------|---------|---------|
| `ARCHIVE_ON_EXPIRY` | `true` | Archive vs delete expired sessions. |
| `CLEANUP_ARCHIVE_LIMIT` | `100` | Max sessions per cleanup batch. |

---

## Debug file (environment)

| Variable | Default | Meaning |
|----------|---------|---------|
| `WORKING_STATE_TRACE_FILE` | `working_state_trace.jsonl` | JSONL path; `0` / `false` / `off` = **off**. Debug for failed `merge_working_state_patch` / parse. Integrator flow: **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**. |

---

## Memory (`set_memory_config` — dict keys or settings attributes)

| Key | Purpose |
|-----|---------|
| `memory_enabled` | `false` → noop memory. |
| `memory_provider` | `mem0` or `noop`. |
| `memory_vector_store` | `qdrant` (default) or `faiss`. |
| `memory_qdrant_path` | empty | Local Qdrant path mode for Mem0 (uses QdrantClient(path=...)). |
| `qdrant_host`, `qdrant_port` | Qdrant HTTP endpoint when using `qdrant` server mode (default `localhost` / `6333`). |
| `qdrant_url`, `qdrant_api_key` | Optional Qdrant Cloud; Mem0 requires **both** if you use URL mode. |
| `mem0_qdrant_collection` | Qdrant collection name; empty → use `mem0_default_collection`. |
| `mem0_default_collection` | Used when `mem0_qdrant_collection` is empty (default `mem0_memories`). |
| `mem0_default_rerank_model` | Used when `memory_rerank_model` / `MEM0_RERANK_MODEL` is empty. |
| `mem0_patch_cross_encoder_reranker` | `true` (default): patch Mem0 so `cross-encoder/*` rerank models load with `CrossEncoder`. |
| `qdrant_on_disk` | Optional `true` / `false` — passed to Mem0 Qdrant config. |
| `memory_faiss_path` | FAISS directory when `memory_vector_store=faiss` (relative = relative to **cwd**). |
| `memory_rerank_enabled` | `true` / `false` — Mem0 `search(..., rerank=...)`. |
| `memory_rerank_model` | Cross-encoder id for sentence-transformers (default `cross-encoder/ms-marco-MiniLM-L-6-v2`). |
| `memory_rerank_device`, `memory_rerank_batch_size`, `memory_rerank_show_progress` | Optional reranker tuning. |
| `memory_llm_provider` | `langchain` (default path) or `litellm`. |
| `memory_litellm_model` | e.g. `bedrock/us.amazon.nova-pro-v1:0` when `litellm`. |
| `memory_infer` | Fact-extraction behaviour (see Mem0 docs). |
| `azure_openai_endpoint`, `azure_openai_key`, `azure_openai_deployment_name`, `azure_openai_api_version` | If endpoint+key+deployment set → Mem0 uses **Azure** for extraction LLM. |
| `aws_access_key_id`, `aws_secret_access_key`, `aws_default_region` | Bedrock; adapter may copy into `os.environ` if missing. |
| `aws_bedrock_model` | Chat model for LangChain Mem0 path. |
| `embedding_model_id` | Bedrock embedder for Mem0 vectors. |

---

## Opik (environment — tracing)

| Variable | Meaning |
|----------|---------|
| `OPIK_API_KEY` | Auth for Opik Cloud. |
| `OPIK_WORKSPACE` | Workspace. |
| `OPIK_PROJECT_NAME` | Project name for traces. |
| `OPIK_URL_OVERRIDE` | Self-hosted URL. |
| `OPIK_CONFIG_PATH` | Config file path. |
| `OPIK_TRACK_DISABLE` | Set to disable tracing (`true` / `1`). |
| `OPIK_DEFAULT_FLUSH_TIMEOUT`, `OPIK_CHECK_TLS_CERTIFICATE`, `OPIK_CONSOLE_LOGGING_LEVEL`, `OPIK_FILE_LOGGING_LEVEL`, `OPIK_DEFAULT_LLM` | See [Opik SDK](https://www.comet.com/docs/opik/tracing/sdk_configuration). |

**Disable tracing:** `OPIK_TRACK_DISABLE=true` **or** uninstall `opik` (decorators no-op).

---

## Database (host app — not read by library directly)

Your **`get_connection`** uses whatever env your app already uses, e.g. `DATABASE_URL`, `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_PORT`, `DB_SSLMODE`.

---

## Copy-paste template

See **`memory_management/.env.example`**.
