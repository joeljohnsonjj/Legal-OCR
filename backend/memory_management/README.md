# memory-management

Postgres **sessions** (context, compaction, cleanup) + **Mem0** long-term memory. No host-app imports inside the package code.

## Documentation (single place)

**→ [docs/README.md](docs/README.md)** — index of all guides.

**Quick path:** [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) (install → SQL → `.env` → LLMs → **§6b** two-string `generate_fn` + system addendum → tracing). **LLM working-state patches:** [docs/WORKING_STATE_LLM_PATCH.md](docs/WORKING_STATE_LLM_PATCH.md).

## Package root files

| File | Purpose |
|------|---------|
| [requirements.txt](requirements.txt) | Pip dependencies |
| [.env.example](.env.example) | Copy into your app’s `.env` |
| [pyproject.toml](pyproject.toml) | Package metadata (`pip install -e ./memory_management`) |

## Install

```bash
pip install -r memory_management/requirements.txt
pip install -e ./memory_management
```

## Minimal API

```python
from memory_management import initialize, process_turn
initialize(settings, get_connection, llm_summarize=..., start_cleanup_worker=True)
result = process_turn(question, user_id, run_id, generate_fn=..., settings=settings, llm_summarize=...)
# generate_fn(question, session_context, user_background, **kwargs) — see docs/GETTING_STARTED.md §6b
```

## Cursor rules

Copy **`memory_management/docs/cursor/*.mdc`** into the workspace **`.cursor/rules/`** (see [docs/cursor/](docs/cursor/)).

## Historical / planning docs

Moved to **`docs/archive_memory_management/`** at the project root (outside this folder). See that folder’s `README.md`.

## Smoke test

```bash
python memory_management/test_smoke.py
```

MIT License — see `pyproject.toml`.
