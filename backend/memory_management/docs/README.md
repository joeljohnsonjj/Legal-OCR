# memory_management — documentation

All user-facing docs for the library live **here** (`memory_management/docs/`). The package root keeps **`README.md`** (short overview), **`requirements.txt`**, **`.env.example`**, and **`pyproject.toml`**.

## Read order

1. **[GETTING_STARTED.md](GETTING_STARTED.md)** — Install → DB → `.env` → LLM roles → **§6b** (`generate_fn` two strings + system addendum) → tracing  
2. **[QDRANT_SETUP.md](QDRANT_SETUP.md)** — Docker / env / verify Qdrant for Mem0 (self-hosted vector DB)  
3. **[ENV_VARS.md](ENV_VARS.md)** — Environment variables and memory config keys  
4. **[INTEGRATION.md](INTEGRATION.md)** — Extra host-app wiring detail  
5. **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)** — LLM `WORKING_STATE_PATCH`: prompt, parse, post-`process_turn` merge (optional)  
6. **[STRUCTURE.md](STRUCTURE.md)** — Per-module code map  
7. **[DATABASE_SETUP.md](DATABASE_SETUP.md)** — Postgres / session tables (detailed)  
8. **[LIBRARY_INDEPENDENCE.md](LIBRARY_INDEPENDENCE.md)** — Reuse / audit checklist  
9. **[HOW_TO_USE_AS_LIBRARY.md](HOW_TO_USE_AS_LIBRARY.md)** — Plain-English data flow  

## Cursor rules (copy into workspace `.cursor/rules/`)

- **[cursor/memory-management-library.mdc](cursor/memory-management-library.mdc)** — Step-by-step checklist  
- **[cursor/memory-management-integration.mdc](cursor/memory-management-integration.mdc)** — Unified `initialize` / `process_turn` + checklist  

## Archive (historical / planning)

Design notes and old plans were moved to **`docs/archive_memory_management/`** at the **chatbot_test** project root (outside this package). Nothing was deleted.
