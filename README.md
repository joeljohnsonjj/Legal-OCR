# Legal OCR — Unified Backend

This branch combines **category/obligation extraction** (`category_obligation_ai_team`) with the **chatbot + frontend** stack (`chatbot_with_frontend_ai`).

## Repository layout

- **`backend/`** — API, document processing, obligation search (ChromaDB), chatbot RAG (Qdrant), memory management
- **`AIAutofillFeatureDesign/`** — React UI for documents, snippet search, and chat (deploy this frontend)

---

## Features

### Obligation & category extraction
# Legal OCR

Legal OCR is a system that reads legal PDF documents (mainly commercial leases and similar real-estate contracts) and turns them into **structured obligation snippets** that teams can search and ask questions about.

Instead of manually reading long PDFs, the system:

- Extracts who must do what
- Groups duties into clear categories (HVAC, rent, insurance, utilities, and more)
- Links every duty back to the **page and section** in the original document

---



### Chatbot (RAG)


After a PDF is processed, the system stores two kinds of content in **Qdrant**:

| Type | What it is | Example questions |
|

---

## Quick start

### Backend (development)

```bash
cd backend
bash scripts/install_server.sh    # Linux
# or: .\scripts\install_server.ps1   # Windows
```

Or manually:

```bash
pip install -r requirements.txt
pip install -e ./memory_management
python -m spacy download en_core_web_sm
```

Create `backend/.env` from `.env.example` (LLM keys, `RAG_INDEX_QDRANT=true` for chat).

```bash
python run_api.py
```

### Server deployment

See **`backend/DEPLOYMENT.md`** for Linux systemd, Docker, system packages (Tesseract, Poppler), and production `.env` settings.

### Frontend

```bash
cd AIAutofillFeatureDesign
npm install
npm run dev
```

---

## API overview

| Feature | Endpoint | Vector store |
|---------|----------|--------------|
| Snippet / obligation search | `POST /query` | ChromaDB |
| Category-level search | `POST /query/categories` | ChromaDB |
| Chatbot | `POST /chat` | Qdrant |
| Document processing | `POST /process` | — |
| Rebuild RAG index | `POST /rag/index` | Qdrant |

See `/docs` when the API is running for full OpenAPI documentation.

---

## Merged from

- `category_obligation_ai_team` — category JSON, obligation consolidation, streaming query endpoints
- `chatbot_with_frontend_ai` — legal RAG chat, Mem0 memory, React frontend
