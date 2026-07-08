# Legal OCR — Chatbot

This repo is the **chatbot branch** of Legal OCR. It lets users ask questions about processed legal documents (leases, contracts) in plain English and get answers grounded in the document — not generic legal advice from the internet.

It includes:

- **`backend/`** — API, document processing, chat (RAG), and Qdrant indexing
- **`frontend/`** — React UI for documents, snippet search, and chat

---

## What the chatbot does

After a PDF is processed, the system stores two kinds of content in **Qdrant**:

| Type | What it is | Example questions |
|------|------------|-------------------|
| **Obligations** | Structured duties extracted from the lease (rent, maintenance, insurance, etc.) | "Who pays for HVAC?" / "What are the rent obligations?" |
| **Raw pages** | Full text of each page | "Are pets allowed?" / "What is the termination notice period?" |

When a user asks a question:

1. A **router** decides whether to search **financial/obligations** or **general document text**.
2. The question is turned into a **vector** and matched in **Qdrant**.
3. For obligation hits, the system also loads the **full page text** for extra context.
4. An **AI model** writes the answer using **only** what was retrieved.

Answers include traceability back to the document content that was indexed during processing.

---

## How this relates to snippet search

This repo also supports **structured obligation search** (`POST /query`) using **ChromaDB** and consolidated JSON. That path is for the snippet/UI search bar.

The **chatbot** (`POST /chat`) uses **Qdrant** and the `legal_rag/` package. Both are fed when you process documents with the right flags enabled.

| Feature | Endpoint | Vector store |
|---------|----------|--------------|
| Snippet / obligation search | `POST /query` | ChromaDB |
| Chatbot | `POST /chat` | Qdrant |

---

## Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for the frontend)
- An **LLM API key** — Azure OpenAI, AWS Bedrock, or Gemini (see backend `.env`)
- **Tesseract** (optional, only for scanned PDFs)
- **Docker** (optional but recommended for Mem0 session memory on Qdrant server)

---

## Quick start (local)

### 1. Clone and enter the repo

```bash
cd Legal-OCR
```

### 2. Backend — install dependencies

```bash
cd backend
pip install -r requirements.txt
pip install -e ./memory_management
```

### 3. Backend — configure environment

Create `backend/.env` (merge keys from `memory_management/.env.example` and your LLM provider settings).

**Minimum for chat to work:**

```env
# Folders
DOCS_FOLDER=docs
OUTPUT_FOLDER=output

# Index into Qdrant when processing PDFs (required for /chat)
RAG_INDEX_QDRANT=true

# LLM — pick one provider (examples)
# Azure:
# USE_AZURE_OPENAI=true
# AZURE_OPENAI_ENDPOINT=...
# AZURE_OPENAI_API_KEY=...
# AZURE_OPENAI_DEPLOYMENT=...

# Or Gemini:
# GEMINI_API_KEY=...

# Or Bedrock:
# USE_BEDROCK=true
# AWS_ACCESS_KEY_ID=...
# AWS_SECRET_ACCESS_KEY=...
# LLM_MODEL=bedrock/anthropic.claude-3-haiku-20240307-v1:0

# Chat RAG — Qdrant (legal_rag uses local folder by default)
# QDRANT_PATH=output/qdrant_rag
# RAG_QDRANT_COLLECTION=legal_rag

# Session + Mem0 memory (simpler local setup — SQLite, no Postgres)
SESSION_ENABLED=true
SESSION_PROVIDER=sqlite
SESSION_DB_PATH=output/session_store.sqlite

MEMORY_ENABLED=true
MEMORY_VECTOR_STORE=qdrant
# Option A: Qdrant server (run docker — see step 4)
QDRANT_HOST=localhost
QDRANT_PORT=6333
# Option B: local file store for Mem0 instead of server
# MEMORY_QDRANT_PATH=output/qdrant_mem0

# API
API_HOST=0.0.0.0
API_PORT=8000
```

> **Note:** Legal RAG (`legal_rag`) and Mem0 can use **different** Qdrant locations. By default, RAG uses `output/qdrant_rag` (file mode). Mem0 uses `localhost:6333` unless you set `MEMORY_QDRANT_PATH`.

### 4. Start Qdrant (for Mem0 memory)

If you use `QDRANT_HOST` / `QDRANT_PORT` for Mem0:

```bash
docker run -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest
```

Check: open `http://localhost:6333/dashboard`

Skip this step if you only use `MEMORY_QDRANT_PATH` (local file mode for Mem0).

### 5. Add a PDF and process it

Put a lease PDF in `backend/docs/`, then start the API:

```bash
cd backend
python run_api.py
```

In another terminal, or via Swagger UI (`http://localhost:8000/docs`), run:

```bash
curl -X POST http://localhost:8000/process
```

With `RAG_INDEX_QDRANT=true`, processing will:

- Extract obligations and save consolidated JSON
- Index obligations + page text into **Qdrant** for chat
- Index obligations into **ChromaDB** for `/query` search

### 6. Test the chatbot

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Who is responsible for HVAC maintenance?\"}"
```

Optional fields:

```json
{
  "message": "Your question here",
  "document_id": "Commercial_Lease.pdf",
  "top_k": 8,
  "user_id": null,
  "run_id": null
}
```

- `document_id` — limit search to one PDF (filename as stored during processing)
- `top_k` — how many vector hits to retrieve (default 8)

### 7. Frontend

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

Open `http://localhost:5173`. The UI calls the backend at `VITE_API_BASE_URL` (default `http://localhost:8000`).

---

## Main API endpoints (chatbot)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/process` | Process PDFs in `docs/` folder |
| `POST` | `/chat` | Ask a question (main chatbot) |
| `POST` | `/chat/stream` | Same as chat, streams answer tokens |
| `POST` | `/chat/reset` | Reset chat session / memory |
| `POST` | `/query` | Structured obligation search (Chroma, not chat) |
| `GET` | `/documents` | List processed documents |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger API documentation |

---

## How the chatbot works (simple logic)

```
User question
    ↓
Router (AI picks: financial vs general)
    ↓
Embed question → search Qdrant (filtered by record type)
    ↓
Top matching obligations and/or pages
    ↓
For obligations: also fetch full page text (parent context)
    ↓
AI writes answer from retrieved context only
    ↓
(Optional) Session memory stores facts for follow-up questions
```

**Router**

- **Financial track** — rent, fees, insurance costs, who pays whom, deposits, operating expenses
- **General track** — pets, smoking, termination, definitions, use of premises, notices

If money is central to the question, financial track wins.

**Why two record types in Qdrant?**

- Obligations are great for precise financial duties but may miss narrative clauses.
- Raw pages capture everything on the page but are noisier to search.
- The router sends each question to the right lane so answers stay focused.

**Why parent page fetch?**

An obligation snippet might say "Tenant shall maintain HVAC." Loading the full page gives the AI surrounding lease language so it does not misread or oversimplify the clause.

More detail: `backend/docs/CHATBOT_OVERVIEW.md` and `backend/rag_architecture.md`.

---

## Project structure

```
Legal-OCR/
├── backend/
│   ├── run_api.py              # Start the API server
│   ├── query_system.py         # FastAPI app (/chat, /query, /process)
│   ├── process_legal_documents.py
│   ├── legal_rag/              # Chatbot RAG (Qdrant, router, retrieval)
│   │   ├── pipeline.py         # End-to-end chat turn
│   │   ├── qdrant_store.py     # Qdrant index + search
│   │   ├── router.py           # Financial vs general routing
│   │   └── retrieval.py        # Context assembly
│   ├── memory_management/      # Sessions + Mem0 long-term memory
│   ├── docs/                   # Input PDFs
│   └── output/                 # JSON results, ChromaDB, Qdrant data
├── frontend/
│   ├── src/services/apiService.ts   # Calls /chat, /query, etc.
│   └── package.json
└── README.md                   # This file
```

---

## Docker (backend only)

From `backend/`:

```bash
docker compose up --build
```

API runs on port **8000**. Mount `./docs` and `./output` for persistent data. You still need to configure `.env` and process documents before chat works.

---

## Troubleshooting

| Problem | What to check |
|---------|----------------|
| Chat says "not enough information" | PDF was processed with `RAG_INDEX_QDRANT=true`; check `output/qdrant_rag` or Qdrant dashboard for collection `legal_rag` |
| `/chat` returns 503 | `legal_rag` or `qdrant-client` not installed — run `pip install -r requirements.txt` |
| Mem0 / session errors | Qdrant running on 6333, or set `MEMORY_QDRANT_PATH`; for Postgres sessions set `SESSION_PROVIDER=postgres` and DB credentials |
| Empty search results | Wrong `document_id` — use exact PDF filename from `GET /documents` |
| Slow first response | Normal on cold start (model warmup, embedding load); `CHAT_WARMUP_ENABLED=true` helps |

**Reset chat session:**

```bash
curl -X POST http://localhost:8000/chat/reset
```

---

## Environment variables (chatbot)

| Variable | Purpose | Default |
|----------|---------|---------|
| `RAG_INDEX_QDRANT` | Index into Qdrant when processing | `false` — set `true` for chat |
| `QDRANT_PATH` | Local Qdrant folder for legal RAG | `output/qdrant_rag` |
| `QDRANT_URL` / `QDRANT_API_KEY` | Qdrant Cloud (optional) | — |
| `RAG_QDRANT_COLLECTION` | RAG collection name | `legal_rag` |
| `RAG_CHAT_MAX_OUTPUT_TOKENS` | Max answer length | `4096` |
| `MEMORY_ENABLED` | Mem0 user memory | `true` |
| `QDRANT_HOST` / `QDRANT_PORT` | Mem0 Qdrant server | `localhost` / `6333` |
| `SESSION_PROVIDER` | `sqlite` or `postgres` | `postgres` |
| `USE_LOCAL_EMBEDDING` | Local embeddings (no OpenAI key) | — |

Full memory/session keys: `backend/memory_management/.env.example` and `backend/memory_management/docs/ENV_VARS.md`.

---

## Further reading

| Document | Contents |
|----------|----------|
| `backend/docs/CHATBOT_OVERVIEW.md` | Chatbot behavior in plain language |
| `backend/rag_architecture.md` | Technical RAG design (dual-track, Qdrant schema) |
| `backend/memory_management/docs/GETTING_STARTED.md` | Session + Mem0 setup |
| `backend/memory_management/docs/QDRANT_SETUP.md` | Running Qdrant with Docker |
| `frontend/README.md` | Frontend-only setup |

---

## Summary

1. **Process** PDFs with `RAG_INDEX_QDRANT=true` → data goes into Qdrant.  
2. **Ask** questions via `POST /chat` or the frontend chat UI.  
3. The **router** picks financial vs general search, **Qdrant** finds relevant content, and the **LLM** answers from that context only.

Snippet search (`/query`) uses ChromaDB and is separate from the chatbot path, but both share the same document processing step.
