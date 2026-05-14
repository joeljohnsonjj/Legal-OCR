# Migrating from AWS Bedrock to Google AI Studio (Gemini)

This guide documents the **architectural shift** applied in this repository: replacing **AWS Bedrock** (Anthropic Claude via `boto3` + IAM) with **Google AI Studio** (Gemini API via REST + API key). Use it to reproduce the same migration in another branch or project, or to verify this codebase.

---

## 1. Architecture comparison

| Aspect | AWS Bedrock (before) | Google AI Studio / Gemini (after) |
|--------|----------------------|-----------------------------------|
| **Auth** | IAM user/role, `AWS_ACCESS_KEY_ID` / STS assume-role, optional SSM for role ARN | API key: `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| **HTTP client** | `boto3` → `bedrock-runtime` `invoke_model` / `invoke_model_with_response_stream` | `requests` / `httpx` → `generativelanguage.googleapis.com` |
| **Model ID** | e.g. `anthropic.claude-3-haiku-20240307-v1:0` or `bedrock/` prefix in env | e.g. `gemini-3.1-flash-lite-preview` via `GEMINI_MODEL` |
| **Request body** | Anthropic Messages schema on Bedrock | Gemini `generateContent` / `streamGenerateContent` JSON |
| **Streaming** | Bedrock response stream → token deltas | SSE `alt=sse` → text fragments in `candidates[].content.parts[].text` |
| **Credentials in app** | None in prompt; ambient AWS chain | Key in header `x-goog-api-key` (AI Studio) or query param (Vertex patterns in `gemini_client.py`) |

---

## 2. Central dispatcher: `llm_client.py`

**Goal:** All synchronous/async/stream LLM entry points route to **either** Azure OpenAI **or** Gemini—not Bedrock.

### 2.1 Remove Bedrock routing

Previously, logic similar to this selected Bedrock:

- `USE_BEDROCK=true` or `LLM_MODEL=bedrock/<model-id>`
- `use_bedrock_llm()` returning `True` when Azure was off
- Branches importing `bedrock_client` for `generate_content`, `generate_content_async`, `generate_content_stream`

**After migration:**

- **Azure:** if `USE_AZURE_OPENAI` is true → `azure_openai_client`.
- **Else:** always `gemini_client` (no `bedrock_client` import).

### 2.2 Default model for Gemini

Define a constant (this repo uses):

```text
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"
```

`get_default_model()` should return:

- Azure deployment name(s) from env when Azure is on.
- `os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)` when Gemini is on.

### 2.3 Streaming parity

`generate_content_stream` must route to:

- Azure: existing SSE/chat stream.
- Gemini: `gemini_client.generate_content_stream` (SSE `streamGenerateContent?alt=sse`), **not** raise “Gemini does not support stream”.

---

## 3. Gemini client: `gemini_client.py`

### 3.1 Non-streaming

- **URL:** `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
- **Headers:** `x-goog-api-key: <key>`, `Content-Type: application/json`
- **Body:** `contents`, `generationConfig` (`temperature`, `responseMimeType`, `maxOutputTokens`)

Optional: Vertex / express mode behind `GOOGLE_GENAI_USE_VERTEXAI` (this repo supports it).

### 3.2 Streaming (required for query merge/rank)

- **URL:** `.../models/{model}:streamGenerateContent?alt=sse`
- Parse SSE lines (`data: ...`), extract `text` from each `GenerateContentResponse`, yield fragments.
- For incremental JSON parsing downstream, **slice** very large `text` parts (env `GEMINI_STREAM_TEXT_SLICE_CHARS`) so `ijson` sees data before one giant chunk ends.

### 3.3 Response object

Expose a small wrapper with `.text` (e.g. `GeminiResponse`) so callers match Azure/Bedrock call sites.

---

## 4. Remove or isolate Bedrock implementation

| Item | Action |
|------|--------|
| `bedrock_client.py` | **Remove** from imports, or **keep file** unused for reference only. |
| `boto3` in `requirements.txt` | **Optional:** remove if nothing else uses AWS APIs; keep if S3/SSM/etc. remain. |
| Env vars | Remove or ignore: `USE_BEDROCK`, `LLM_MODEL=bedrock/...`, `BEDROCK_*`, `LLM_PARAMETER_PATH` (if only used for Bedrock assume-role). |

---

## 5. Call-site and UX updates

### 5.1 `process_legal_documents.py`

- Replace `use_bedrock_llm()` in `_configured_llm_label()` and `GeminiAnalyzer` init with **Azure vs Gemini** only.
- Docstrings: drop “AWS creds for Bedrock”; require `GEMINI_API_KEY` / `GOOGLE_API_KEY` when not Azure.
- If you added **document LLM rate limiting**, keep it on the wrapper that calls `llm_client.generate_content` (not inside `gemini_client`).

### 5.2 `query_system.py`

- Imports: remove `use_bedrock_llm`.
- `ObligationQuerySystem.__init__`: validate Gemini key when not Azure; remove `elif use_bedrock_llm(): pass`.
- Docstrings: “Azure OpenAI or Gemini” only.

### 5.3 `run_api.py` (optional)

When not using Azure and not using Vertex with ADC, clear `GOOGLE_APPLICATION_CREDENTIALS` so the process does not pick up gcloud ADC instead of the API key (this repo does that for Gemini-only local dev).

---

## 6. Environment variables

### 6.1 Disable Bedrock (user / deployment)

```env
# Remove or set false
# USE_BEDROCK=false
# LLM_MODEL=bedrock/anthropic.claude-3-haiku-20240307-v1:0
```

### 6.2 Enable Gemini (Google AI Studio)

```env
USE_AZURE_OPENAI=false
GEMINI_API_KEY=AIza...   # from https://aistudio.google.com/app/apikey
GEMINI_MODEL=gemini-3.1-flash-lite-preview
# Optional:
# GEMINI_MAX_OUTPUT_TOKENS=8192
# GEMINI_STREAM_TEXT_SLICE_CHARS=128
```

### 6.3 Keep Azure for some deployments

```env
USE_AZURE_OPENAI=true
# ... AZURE_OPENAI_* vars
```

Azure still takes precedence over Gemini when `USE_AZURE_OPENAI=true`.

---

## 7. Streaming query pipeline (`query_system.py`)

After migration, `generate_content_stream` must work on Gemini so:

- `POST /query/stream` and `POST /query/stream/raw` keep working.
- `merge_and_rank_results_stream` uses `parse_obligations_stream` + `ijson` for incremental NDJSON (install **`ijson`**).

See **`STREAMING_API.md`** for API contract and frontend notes.

---

## 8. Duplicate tree: `Legal-OCR/`

If your repo maintains **`Legal-OCR/`** as a copy of the same app, apply the **same** edits to:

- `Legal-OCR/llm_client.py`
- `Legal-OCR/gemini_client.py`
- `Legal-OCR/process_legal_documents.py`
- `Legal-OCR/query_system.py`

(or script a sync) so both entry points behave identically.

---

## 9. Verification checklist

1. **Imports:** `grep -r "bedrock\|BEDROCK\|use_bedrock"` → no matches in app code (only optional `bedrock_client.py` file).
2. **Env:** `USE_AZURE_OPENAI=false`, `GEMINI_API_KEY` set, no `USE_BEDROCK`.
3. **Smoke:** Run `python run_api.py`, call `POST /query` with a small query; expect 200 and JSON.
4. **Stream:** `curl -N -X POST http://127.0.0.1:8000/query/stream -H "Content-Type: application/json" -d "{\"query\":\"test\"}"` — NDJSON lines should arrive over time if `ijson` is installed.
5. **Process PDF:** Run document pipeline; logs should show “Gemini API”, not Bedrock.
6. **Dependencies:** `pip install -r requirements.txt` includes `google-*`, `httpx`, `requests`; `ijson` for streaming parse.

---

## 10. Rollback (short term)

- Restore `llm_client.py` Bedrock branches and `use_bedrock_llm()`.
- Set `USE_BEDROCK=true` and valid AWS credentials / model access.
- Re-point `generate_content_stream` to Bedrock if you had removed streaming there.

---

## 11. Reference files in this repo (post-migration)

| File | Role |
|------|------|
| `llm_client.py` | Provider switch: Azure vs Gemini; default model; stream dispatch |
| `gemini_client.py` | REST `generateContent` / `streamGenerateContent` (AI Studio + optional Vertex) |
| `azure_openai_client.py` | Unchanged pattern for Azure path |
| `bedrock_client.py` | **Legacy / unused** in default flow; safe to delete if desired |
| `STREAMING_API.md` | NDJSON streaming contract for `/query/stream` |
| `.env example` | Commented Gemini + rate-limit hints |

---

*This document describes the migration pattern used here; model names and env defaults may change as Google releases new Gemini versions.*
