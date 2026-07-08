# Legal OCR

Legal OCR is a system that reads legal PDF documents (mainly commercial leases and similar real-estate contracts) and turns them into **structured obligation snippets** that teams can search and ask questions about.

Instead of manually reading long PDFs, the system:

- Extracts who must do what
- Groups duties into clear categories (HVAC, rent, insurance, utilities, and more)
- Links every duty back to the **page and section** in the original document

---

## What problem does it solve?

Legal documents are long and hard to search. A simple keyword search in a PDF often misses important duties or returns too much noise.

Legal OCR solves this by:

1. Reading the document (including scanned PDFs via OCR)
2. Using AI to pull out legal obligations in a clean, structured format
3. Letting users **search** for relevant obligations or **chat** with the document in plain English

---

## How the system is split

This project has two main parts:

| Part | Where it lives | What it does |
|------|----------------|--------------|
| **Processing + Query API** | This branch/repo | Extracts obligations from PDFs and answers structured search queries |
| **Chatbot** | Separate branch | Answers open-ended questions in a conversational way |

Both parts use the same processed document data, but they are built for different use cases (see [Chatbot](#chatbot-separate-branch) below).

---

## How it works (simple overview)

```
PDF Upload  →  Read Text (OCR if needed)  →  AI extracts obligations  →  Save structured JSON
                                                                              ↓
                                                                    Index for search (ChromaDB)
                                                                              ↓
User asks a question  →  Find similar obligations  →  AI filters & ranks  →  Return results
```

---

## Part 1 — Processing (turning PDFs into snippets)

When a legal PDF is processed, the system goes through these steps:

### 1. Read the PDF

- If the PDF has selectable text, it reads it directly.
- If the PDF is scanned (image-only), it uses **OCR** (Tesseract) to extract text.
- OCR results are cached so the same file does not need to be scanned again.

### 2. Break the document into pieces

By default, the document is analyzed **page by page**.

Optionally, it can be split by **sections** (for example `1. Rent`, `2. Maintenance`) when section-based mode is turned on.

### 3. Find the parties

On the first few pages, the system identifies who the parties are (for example, mapping "Tenant" to the real company name). This helps obligations show the correct responsible party.

### 4. Extract obligations with AI

Each page (or section) is sent to an AI model along with a detailed legal prompt (`prompt.txt`).

The AI looks for **explicit legal duties** in the text and returns them in a fixed JSON structure.

Each obligation includes:

- **Responsible Party** — who must perform the duty
- **Obligation text** — what they must do (complete sentence, with amounts and deadlines when present)
- **Reasoning** — a short explanation of why this duty exists
- **Citation** — document name, page number, and section
- **Keywords** — short phrases to help with search later

### 5. Assign categories

Every obligation is placed into one category from a predefined list, such as:

- HVAC
- Plumbing
- Utilities
- Insurance & Risk Management
- Financial & Cost Allocation (rent, CAM, holdover, etc.)
- Legal & Indemnification
- Maintenance & Repairs
- And more (about 37 categories in total)

Each obligation belongs to **one category only**. The system avoids putting the same duty in multiple places.

### 6. Consolidate into one file

Results from all pages/sections are merged into a single **consolidated JSON** file per document. This is the main "snippet library" for that lease.

The system also saves a pagewise (or section-based) file for debugging and traceability.

### 7. Build a search index (ChromaDB)

After consolidation, each obligation is indexed in **ChromaDB** (a local vector database).

This index is used by the **query API in this repo** to find obligations that match a user's search question.

### 8. Feed the chatbot (separate branch)

When processing finishes, the processed data is also sent to the **chatbot service** (on another branch), which stores information in **Qdrant** for conversational Q&A.

This repo does not contain the chatbot code, but processing is designed to support it.

---

## Part 2 — Querying (searching obligations)

The query API lets users search across processed documents and get back only the obligations that matter.

### How a search works

1. **User sends a question** — for example: *"Who is responsible for HVAC maintenance?"*
2. **Vector search** — the system finds the most similar obligations in ChromaDB.
3. **Load full details** — it pulls the complete obligation from the consolidated JSON (not just the search snippet).
4. **AI filter and rank** — an AI step removes irrelevant lines and ranks what is left by relevance to the question.
5. **Return structured results** — grouped by category, with party, duty text, reasoning, and citations.

### Why both vector search and AI?

- **Vector search** is fast and finds obligations that are *similar in meaning* to the question, even if the exact words differ.
- **AI filtering** removes noise and keeps only obligations that truly answer the question.

Together, this gives more accurate results than keyword search or vector search alone.

---

## Chatbot (separate branch)

The **chatbot is not in this branch**. It lives on another branch/repo and works alongside Legal OCR.

| | Query API (this repo) | Chatbot (other branch) |
|--|----------------------|------------------------|
| **Purpose** | Return structured obligation snippets | Answer questions in natural conversation |
| **Best for** | "Show me all HVAC duties for this lease" | "Compare tenant vs landlord maintenance duties" |
| **Vector database** | ChromaDB | Qdrant |
| **Output** | JSON with categories, parties, citations | Conversational text answer |

### How the chatbot connects

1. A PDF is processed by Legal OCR (this repo).
2. Structured obligations are saved as JSON.
3. The chatbot service (other branch) indexes that data into **Qdrant**.
4. Users ask free-form questions in the chat UI.
5. The chatbot retrieves relevant content from Qdrant and uses AI to write an answer.

So: **this repo builds the knowledge base; the chatbot branch makes it conversational.**

---

## Features

### Document processing

- Supports text-based and scanned PDFs (with OCR)
- Page-wise or section-based extraction
- Party name detection (Tenant, Landlord, etc. → real legal names)
- 37+ legal categories for obligation grouping
- Page-level and section-level citations on every obligation
- Citation post-processing to improve accuracy
- OCR text caching for faster re-runs
- Optional Google Cloud Storage (GCS) document versioning

### Search and query

- Semantic search across all processed documents
- Filter search by specific document(s)
- AI-powered filtering and ranking of results
- Streaming responses for real-time UI updates
- Category-level search mode for broad topics
- List all processed documents with obligation counts

### AI provider support

The system can use different AI providers (configured in `.env`):

- Azure OpenAI
- AWS Bedrock (Claude)
- Google Gemini

### API

REST API built with FastAPI. Main endpoints:

| Endpoint | What it does |
|----------|--------------|
| `POST /process` | Process PDFs from the docs folder |
| `POST /query` | Search obligations by question |
| `POST /query/categories` | Search by category-level matching |
| `POST /query/stream` | Same as query, but streams results |
| `GET /documents` | List all processed documents |
| `GET /health` | Health check |
| `POST /gcs/versioned-upload` | Upload a document with version history |

API docs are available at `http://localhost:8000/docs` when the server is running.

---

## The logic behind it (in plain terms)

### Why extract obligations instead of just storing the PDF text?

A lease might say "Tenant shall maintain all HVAC equipment serving the Premises" buried inside a 60-page document. Storing raw text makes it hard to answer "What are the tenant's HVAC duties?" reliably.

By extracting **atomic obligations** (one duty per item), the system creates a clean library where every item has a party, a duty, and a source citation.

### Why use categories?

Categories (HVAC, Insurance, Rent, etc.) match how legal and property teams think about leases. They make browsing and filtering easier and keep results organized.

### Why atomic obligations (one duty per line)?

If one sentence says "Pay rent monthly and maintain HVAC," those are two different legal duties. Splitting them makes search and chat more accurate.

### Why citations on every obligation?

Legal work requires proof. Every snippet links back to the exact page and section so a human can verify the AI output.

### Why two vector databases (ChromaDB and Qdrant)?

- **ChromaDB** (this repo) is tuned for precise obligation retrieval tied to the query API.
- **Qdrant** (chatbot branch) is used by the separate chatbot for broader conversational RAG.

They serve different products with different needs, even though both use the same underlying processed data.

---

## Project structure (main files)

```
Legal-OCR/
├── process_legal_documents.py   # PDF processing and obligation extraction
├── query_system.py              # FastAPI query API
├── vector_store.py              # ChromaDB indexing and search
├── obligation_keywords.py       # Auto-keyword generation for search
├── prompt.txt                   # AI prompt for obligation extraction
├── llm_client.py                # Routes AI calls to Azure / Bedrock / Gemini
├── run_api.py                   # Start the API server
├── docs/                        # Place PDF files here for processing
├── output/                      # Processed JSON files and ChromaDB index
└── .env                         # Configuration (API keys, folders, model settings)
```

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Copy or edit `.env` with your AI provider keys and folder paths (`DOCS_FOLDER`, `OUTPUT_FOLDER`, etc.).

### 3. Add a PDF

Place a legal PDF in the `docs/` folder.

### 4. Process documents

**Option A — API:**

```bash
python run_api.py
```

Then call `POST /process` (or use the Swagger UI at `/docs`).

**Option B — Command line:**

```bash
python process_legal_documents.py
```

### 5. Search obligations

```bash
POST /query
{
  "query": "Who is responsible for HVAC maintenance?"
}
```

---

## Output example

After processing, you get a consolidated JSON file like this (simplified):

```json
{
  "document_name": "Commercial_Triple_Net_Lease.pdf",
  "total_obligations_found": 142,
  "results": [
    {
      "category": "HVAC",
      "obligations": [
        {
          "Responsible Party": "ABC Retailers, Inc.",
          "Owner Responsibility": [
            "Maintain and repair all HVAC equipment serving the Premises"
          ],
          "Reasoning": [
            "Tenant bears responsibility for interior systems per Section 12(a)"
          ],
          "Citation": "Commercial_Triple_Net_Lease.pdf, Page 8, Section 12(a)"
        }
      ]
    }
  ]
}
```

---

## Summary

**Legal OCR** reads legal PDFs, extracts structured obligation snippets with citations, and lets users search them through an API. A separate **chatbot branch** uses the same processed data (stored in Qdrant) to answer conversational questions.

- **Processing** = PDF → text → AI extraction → categorized snippets → search index
- **Querying** = question → vector search → AI filter → structured results
- **Chatbot** = same processed data → Qdrant → conversational answers (other branch)
