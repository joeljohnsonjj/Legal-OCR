# How to Use memory_management as a Proper Library (Plain English)

This doc explains four things: how the Cursor rule helps, what “using the library” really means, and how to make sure every part of your chatbot actually uses it.

---

## 0. How Does Session + Memory Data Get Into the Prompt? (The Data Flow)

When we say "session gets into the prompt," we mean: the conversation history and state you store in the database end up as **text inside the message** your app sends to the model (e.g. Bedrock). With **`process_turn`**, **Mem0** hits are a **second** string so the model does not confuse **this chat** with **stored user facts**. Step by step:

**Step 1 – Session data lives in the database**  
The session manager stores: a **compacted summary** (short summary of older turns), **recent events** (last N user/assistant messages), and **working state** (a small JSON blob like progress or hypotheses). All of that comes from the DB.

**Step 2 – `build_full_context` produces two strings**  
Inside **`process_turn`**, the library calls **`build_full_context(user_id, run_id, question, settings, ...)`**, which:

1. Builds **session_context** via **`build_session_context`** (same sections as before: previous summary, recent messages, working state).
2. Optionally runs Mem0 **`search`** and formats hits as **`--- User Background ---`** … **`--- End User Background ---`** → **user_background** (or empty if none).

So: **DB → `build_session_context` → `session_context`**; **Mem0 search → `user_background`**.

**Step 3 – `generate_fn` receives both strings**  
The host implements:

`generate_fn(question, session_context, user_background, **kwargs)`

and passes them into the RAG/LLM layer **separately** (do not put User Background inside the Session Context wrapper).

**Step 4 – The LLM builds CONTEXT**  
Example pattern (reference HR app): wrap **only** `session_context` in `--- Session Context ---` / `--- End Session Context ---`, append handbook/FAQ chunks, then append **`user_background`** as its own block if non-empty. Add the **system addendum** from **[GETTING_STARTED.md §6b](GETTING_STARTED.md)** so summarize/recap uses session only.

**Summary**  
**DB → session string; Mem0 → user_background string; `generate_fn` → LLM with two labeled regions + your system instructions.**

**Updating `working_state` from the chat model**  
The library **reads** whatever is already in `working_state` when building session context. It does **not** apply **`WORKING_STATE_PATCH`** lines from the model inside `process_turn`. The host adds instructions to the chat prompt, parses the model output (e.g. `parse_llm_response_for_working_state`), and **after** each successful `process_turn` merges the patch with `merge_working_state_patch` and calls `provider.update_working_state`. Full checklist: **[WORKING_STATE_LLM_PATCH.md](WORKING_STATE_LLM_PATCH.md)**.

---

## 1. How the Cursor Rule Adds to Context (Prompt-Wise)

**What’s a “rule” in Cursor?**  
A rule is a short note you keep in `.cursor/rules/`. When you work on certain files, Cursor **automatically adds that note to the AI’s context** (the “prompt”). So the AI is reminded of the steps without you typing them every time.

**When does our rule get added?**  
The rule file says it applies when you have open:

- Any Python file inside `memory_management/`
- Your app’s `api.py`
- Your app’s `endpoints.py` or `main.py`

So when you (or the AI) are editing those files, Cursor **injects the integration instructions** into the conversation. That’s “how it adds to the context”: same idea as pasting a checklist into the chat, but automatic when you’re in the right place.

**In short:**  
Open the files where you integrate or use the library → Cursor includes the rule in the prompt → the AI follows the install/startup/usage steps when suggesting or editing code.

---

## 2. What “Using It as a Proper Library” Means (Step by Step)

Using it “as a library” means: **you don’t copy its code into your app**. You install it once, tell it how to get config and DB once at startup, then everywhere else you **only import and call** it.

Think of it like using a calculator app: you install it, set your preferences once, then you just press buttons (call functions). You don’t rewrite the calculator.

**Step 1 – Install (one time)**  
Run from your project root:

```bash
pip install -e ./memory_management
```

(Or add `memory_management` to your project’s dependency list.)  
After this, your app can `import memory_management`.

**Step 2 – Tell the library how to get config and DB (once at app startup)**  
When your app starts (e.g. when the server starts), you run a small block of code **once**:

- **Memory:** “Here’s my settings object.”  
  `set_memory_config(settings)`  
  So the library knows things like “is memory on?”, “where’s the FAISS path?”, “which Azure/Bedrock keys?”.

- **Session:** “Here’s how to talk to the database.”  
  You run the DB setup script (so tables exist), then you create a `SessionBackend(get_connection)` and call `set_default_backend(backend)` and set `provider_factory._app_session_backend = backend`.  
  So the library knows how to read/write sessions in *your* database.

After startup, you **never** need to pass config or DB again for normal requests.

**Step 3 – In the rest of your app, only “import and call”**  
Everywhere you need session or memory (e.g. in endpoints, in RAG, in background tasks), you:

- **Import** from `memory_management` (or from your own thin shim that re-exports from `memory_management`).
- **Call** the same functions you always did: `get_session_provider()`, `get_memory_provider()`, `build_session_context()`, `provider.search()`, `provider.add()`, etc.

You do **not** copy-paste the library’s code into your app. You do **not** edit the library’s files for app-specific stuff. Config and DB are given once at startup; the rest is just imports and calls.

**Summary:**  
Install once → wire config + DB once at startup → everywhere else: import + call. That’s “using it as a proper library.”

---

## 3. How to Ensure “All Endpoints Are Met”

“Endpoints” here means: every place in your chatbot that does something with **session** or **memory** (e.g. “get session context”, “search memory”, “add to memory”). “All endpoints are met” means: **every such place gets session/memory from the library** (or from your shim that uses the library), and you’ve checked that those code paths work.

**Step 1 – Find every place that uses session or memory**  
Search your codebase for:

- Imports like: `session_manager`, `src.session`, `src.memory`, `get_session_provider`, `get_memory_provider`, `build_session_context`, `session_services`, `run_compaction`, etc.
- Files that often matter: `api.py`, `src/api/endpoints.py`, `src/rag/pipeline.py`, any `main.py` or background workers.

Make a short list: “This file uses session; this file uses memory.”

**Step 2 – Point each place at the library**  
For each of those places:

- If it currently imports from `session_manager` or `src.session`, change it to import from `memory_management.session_manager` (or from your shim, e.g. `src.session` if that shim now re-exports from `memory_management`).
- If it currently imports from `src.memory`, change it to import from `memory_management.memory` (or from your shim that re-exports from `memory_management.memory` and calls `set_memory_config` at startup).

So “all endpoints are met” = every usage of session/memory in your app goes through the library (or through a single shim that uses the library).

**Step 3 – Check startup**  
Ensure that in your app’s startup (e.g. `api.py` lifespan or `main()`):

- `set_memory_config(settings)` is called once.
- Session backend is set once (`set_default_backend`, `provider_factory._app_session_backend`).

**Step 4 – Test the routes that use session and memory**  
Hit the API routes (or run the flows) that:

- Create/get session, build context, run compaction, cleanup.
- Search memory, add to memory.

If those work as before (or as expected), then “all endpoints are met” in practice.

**Quick checklist:**

- [ ] Installed: `pip install -e ./memory_management` (or in deps).
- [ ] Startup: `set_memory_config(settings)` + session backend wiring (and DB setup).
- [ ] No remaining imports from old `session_manager` or `src.memory` for the same behavior; all use `memory_management` (or your shim).
- [ ] Tested: session and memory API/endpoints work.

---

## TL;DR

- **Cursor rule:** When you work on `memory_management`, `api.py`, or `endpoints`/`main`, Cursor adds the integration rule to the AI’s context so it follows the right steps.
- **Using as a library:** Install once, wire config + DB once at startup, then only import and call; don’t copy library code into your app.
- **All endpoints met:** Find every place that uses session/memory, make them import from the library (or your shim), then run the startup wiring and test those routes.
