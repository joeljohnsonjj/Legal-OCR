# Legal Obligation Query System

A system that extracts text from legal PDFs (including scanned documents via OCR), analyzes them with Google Gemini to identify obligations, and exposes a FastAPI service to search and retrieve those obligations. It supports local development using a **fake Google Cloud Storage (GCS)** emulator so you can run without real GCP.

## Fake GCP (Google Cloud Storage) Emulator

This project uses a local GCS emulator for development and testing. When `STORAGE_EMULATOR_HOST` is set, the app talks to the emulator instead of production Google Cloud Storage.

- **Credits:** The emulator is [**gcp-storage-emulator**](https://github.com/oittaa/gcp-storage-emulator) by [oittaa](https://github.com/oittaa).
- **Repository:** https://github.com/oittaa/gcp-storage-emulator  
- **PyPI:** https://pypi.org/project/gcp-storage-emulator/

You can install it with `pip install gcp-storage-emulator` or use the vendored copy under `gcp-storage-emulator-main/` and run the project’s `run_emulator.py` to start the emulator (which uses that code path).

---

## How the Model Works

End-to-end flow:

1. **Document processing (PDF → obligations)**  
   - PDFs are read from a local `docs` folder (or configured path).  
   - **Text extraction:**  
     - If the PDF has selectable text, text is extracted directly with PyPDF2.  
     - If it’s scanned, pages are converted to images (pdf2image) and OCR’d with Tesseract; results can be cached.  
   - **Per-page analysis:**  
     - Each page’s text is sent to **Google Gemini** (with retries and rate limiting).  
     - Gemini extracts **party metadata** (e.g. Landlord, Tenant) on early pages and **financial/legal obligations** (duty type, responsible party, reasoning, citations).  
   - **Consolidation:**  
     - Obligations from all pages are merged and deduplicated by Gemini into a single list per document.  
   - **Output:**  
     - For each document, two JSON files are written to the output folder:  
       - A page-wise JSON (per-page obligations).  
       - A **consolidated** JSON (merged obligations, party metadata, document name, timestamps).  
   - The system can use **fake GCS** when `STORAGE_EMULATOR_HOST` is set; otherwise it uses the local filesystem for reading/writing.

2. **Query system (search obligations)**  
   - The API loads all `*_consolidated.json` files from the configured output folder.  
   - For a user query:  
     - **Filter:** Gemini filters each document’s consolidated obligations to those relevant to the query (by duty type, responsible party, reasoning, etc.).  
     - **Merge & rank:** Results from all documents are merged and ranked (e.g. by relevance and monetary value).  
   - If the query is empty, the default is “utility-related” obligations (water, gas, heat, electricity, HVAC, etc.).  
   - Optional: restrict search to specific documents via `document_ids`; optional save of query results to file.

3. **APIs**  
   - **POST /process** triggers the pipeline above for PDFs in the docs folder (first PDF when using the default flow) and writes consolidated JSON to the output folder.  
   - **POST /query** runs the query flow above and returns the ranked obligations.  
   - **GET /documents** lists available consolidated documents.  
   - **GET /**, **GET /health** report service and health status.

So: **PDF → (optional fake GCS) + local text/OCR → Gemini (extract + consolidate) → consolidated JSON → query (filter + rank) → API response.**

---

## Setup and Installation

### Prerequisites

- **Python 3.11+**
- **Tesseract OCR** (for scanned PDFs): install on your system (e.g. `tesseract-ocr` and `tesseract-ocr-eng`).
- **Poppler** (for pdf2image): `poppler-utils` on Linux; on Windows, ensure Poppler is on PATH or use a package that bundles it.
- **Google Gemini API key:** from [Google AI Studio](https://aistudio.google.com/app/apikey). Set `GEMINI_API_KEY` in `.env`.

### 1. Clone and install dependencies

```bash
cd HEB-Legal-OCR-two
pip install -r requirements.txt
```

Optional (for local GCS emulation):

```bash
pip install gcp-storage-emulator
# or, from project root:
pip install -e ./gcp-storage-emulator-main
```

### 2. Environment variables

Copy `.env example` to `.env` and fill in at least:

- `GEMINI_API_KEY` – required for Gemini.
- `GOOGLE_GENAI_USE_VERTEXAI=false` – use direct Gemini API (recommended for local dev).

For **fake GCS** (optional):

- `STORAGE_EMULATOR_HOST=http://localhost:4443`
- `GCS_BUCKET=heb-legal` (or your bucket name)

Other optional vars: `GEMINI_MODEL`, `DOCS_FOLDER`, `OUTPUT_FOLDER`, `API_PORT`, etc. See `.env example` for full list.

### 3. Run the app

**Without fake GCS (local files only):**

```bash
python run_api.py
```

**With fake GCS:** start the emulator first, then the API.

Terminal 1 – start GCS emulator (project uses port 4443 by default):

```bash
python run_emulator.py
```

Terminal 2 – start API:

```bash
python run_api.py
```

The API will detect the emulator on `localhost:4443` and, if configured, initialize the bucket (e.g. `heb-legal`) via `init_fake_gcs.py`.

### 4. Put PDFs in the docs folder

Place legal PDFs in the folder set by `DOCS_FOLDER` (default `docs/`). Use **POST /process** (or the processing script) to generate consolidated JSON under `OUTPUT_FOLDER` (default `output/`). Then use **POST /query** and **GET /documents** to search and list obligations.

### 5. Docker (optional)

Build and run with Docker Compose:

```bash
docker-compose up --build
```

Ensure `.env` (or Compose env) has `GEMINI_API_KEY` and, if you use fake GCS in Docker, uncomment and configure the `fake-gcs-server` service and `STORAGE_EMULATOR_HOST` accordingly.

### 6. Where ChromaDB lives and how to share it

**ChromaDB is not a separate Docker service.** It runs inside the same process as the API (embedded). All data is stored on disk in a folder under your output directory:

- **Path:** `{OUTPUT_FOLDER}/chroma_db` (default: `output/chroma_db`).
- **When you run Docker:** The container mounts `./output` from your host into `/app/output`, so the Chroma DB is the folder **on your machine** at `output/chroma_db`. It persists between container restarts.

**To send the index to a friend:**

1. **Zip the `output` folder** (it contains `chroma_db/` and the consolidated JSON files). The query system needs both the vector index and the consolidated JSONs to return full obligation text.
2. Send the zip (e.g. `output.zip`).
3. Your friend extracts it into their project root so that the folder is named `output` (or sets `OUTPUT_FOLDER` to the folder they extracted).
4. **Same embedding model:** They must use the same embedding setup you used when building the index (e.g. `USE_LOCAL_EMBEDDING=true` and the same `SENTENCE_TRANSFORMER_MODEL`, or the same Azure/OpenAI embedding). Otherwise query vectors won’t match the indexed vectors.

They can run the API locally or with Docker; no separate Chroma container is required.

---

## Summary

- **Fake GCP:** We use [gcp-storage-emulator](https://github.com/oittaa/gcp-storage-emulator) for local GCS; credits and link above.  
- **Flow:** PDF → text/OCR → Gemini (extract + consolidate) → consolidated JSON → query (filter + rank) → APIs.  
- **Install:** `pip install -r requirements.txt`, set `GEMINI_API_KEY` in `.env`, optionally run `run_emulator.py` and then `run_api.py`.

For API details, request/response schemas, and examples, see **API.md**.
