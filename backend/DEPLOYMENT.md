# Server deployment guide — Legal OCR backend

Use this when deploying to a Linux or Windows server so `pip install` pulls everything needed and the API starts correctly.

---

## What gets installed

| Component | How |
|-----------|-----|
| Python packages | `backend/requirements.txt` (FastAPI, ChromaDB, Qdrant client, litellm, mem0ai, torch, etc.) |
| Chat memory library | `pip install -e ./memory_management` |
| spaCy English model | `python -m spacy download en_core_web_sm` (for Mem0 NLP) |

Application **source code** comes from your git clone — it is not installed via pip.

---

## Option A — Linux server (recommended script)

```bash
# Clone the repo on the server, then:
cd Legal-OCR/backend
bash scripts/install_server.sh
```

This script:

1. Creates `backend/.venv`
2. Runs `pip install -r requirements.txt`
3. Installs `memory_management` in editable mode
4. Downloads the spaCy model
5. Creates `docs/`, `output/`, `logs/`, etc.
6. Runs `scripts/verify_install.py` to confirm imports

---

## Option B — Manual install (Linux or Windows)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip wheel "setuptools>=65.0.0,<82"
pip install -r requirements.txt
pip install -e ./memory_management
python -m spacy download en_core_web_sm

mkdir -p docs output logs ocr_cache query_results
python scripts/verify_install.py
```

---

## Option C — Docker

```bash
cd backend
docker compose up --build -d
```

Requires a `.env` file (or environment variables) for LLM keys. Persistent data is mounted from `./docs`, `./output`, `./logs`.

---

## System packages (non-Python)

Install on the **host** (or use the Docker image, which includes them):

| Package | Purpose |
|---------|---------|
| `tesseract-ocr` | OCR for scanned PDFs |
| `poppler-utils` | PDF → image (`pdf2image`) |
| `libgomp1` | OpenMP for ML libraries |

**Ubuntu/Debian:**

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng poppler-utils libgomp1
```

---

## Configuration (`.env`)

```bash
cp .env.example .env
# Edit .env — minimum for production:
```

| Variable | Purpose |
|----------|---------|
| `USE_AZURE_OPENAI` / `GEMINI_API_KEY` / Bedrock vars | LLM provider |
| `DOCS_FOLDER` | Input PDFs (default `docs`) |
| `OUTPUT_FOLDER` | JSON + vector indexes (default `output`) |
| `RAG_INDEX_QDRANT=true` | Index Qdrant when processing (required for `/chat`) |
| `API_HOST=0.0.0.0` | Listen on all interfaces |
| `API_RELOAD=false` | Disable auto-reload in production |
| `SESSION_PROVIDER=sqlite` | Simple sessions without Postgres |
| `SESSION_DB_PATH=output/session_store.sqlite` | SQLite session file |
| `MEMORY_ENABLED=true` | Mem0 chat memory |
| `QDRANT_HOST` / `QDRANT_PORT` | Qdrant for Mem0 (or use `MEMORY_QDRANT_PATH`) |
| `QDRANT_PATH=output/qdrant_rag` | Local Qdrant folder for legal RAG (default) |

For chatbot + memory, run **Qdrant** (Docker) or use local path mode:

```bash
docker run -d -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest
```

---

## Start the API

```bash
cd backend
source .venv/bin/activate
python run_api.py
```

Health check: `GET http://<server>:8000/health`  
Swagger UI: `http://<server>:8000/docs`

---

## Process documents (first run)

1. Copy lease PDFs into `backend/docs/`
2. Ensure `RAG_INDEX_QDRANT=true` in `.env`
3. Call `POST /process` or use Swagger UI
4. Test chat: `POST /chat` with `{"message": "Who pays for HVAC?"}`

---

## Run as a systemd service (Linux example)

`/etc/systemd/system/legal-ocr.service`:

```ini
[Unit]
Description=Legal OCR API
After=network.target

[Service]
Type=simple
User=legalocr
WorkingDirectory=/opt/Legal-OCR/backend
EnvironmentFile=/opt/Legal-OCR/backend/.env
ExecStart=/opt/Legal-OCR/backend/.venv/bin/python run_api.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now legal-ocr
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `litellm` Rust/Cargo error on Windows | Use updated `requirements.txt` (`litellm<1.92`) |
| pip installs to `AppData\...\Python312\Lib\site-packages` | Wrong venv — use **`backend\.venv` only**. Run `.\scripts\install_server.ps1` or always `.\.venv\Scripts\python.exe -m pip install ...` |
| Dependency conflicts (`embedchain`, old `langchain`) | Those are from **other global packages**, not Legal OCR. Use an isolated `backend/.venv`; ignore conflicts if `verify_install.py` passes |
| `Ignoring invalid distribution ~oogle-adk` | Corrupted global package — safe to ignore in venv, or remove that folder under global `site-packages` |
| `verify_install` fails on `memory_management` | Run `pip install -e ./memory_management` inside `backend/.venv` |
| `/chat` returns empty | Set `RAG_INDEX_QDRANT=true`, re-run `/process` |
| OCR fails | Install `tesseract-ocr` on the server |
| PDF page images fail | Install `poppler-utils` |
| Mem0 / session errors | Start Qdrant or set `MEMORY_QDRANT_PATH`; use `SESSION_PROVIDER=sqlite` for simple setup |

---

## Frontend (optional)

Build and serve separately:

```bash
cd AIAutofillFeatureDesign
npm install
npm run build
# Serve build/ with nginx or set VITE_API_BASE_URL to your backend URL
```
