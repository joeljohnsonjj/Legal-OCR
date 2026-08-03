# Legal OCR backend — production image (API + processing + chatbot RAG)
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    API_RELOAD=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel "setuptools>=65.0.0,<82" \
    && pip install --no-cache-dir -r requirements.txt

COPY memory_management ./memory_management
RUN pip install --no-cache-dir -e ./memory_management

COPY . .

RUN if [ -f scripts/install_server.sh ]; then sed -i 's/\r$//' scripts/install_server.sh && chmod +x scripts/install_server.sh; fi \
    && if [ -f scripts/install_server.ps1 ]; then sed -i 's/\r$//' scripts/install_server.ps1; fi \
    && python -m spacy download en_core_web_sm || true \
    && mkdir -p logs ocr_cache output docs query_results credentials \
    && chmod -R 755 logs ocr_cache output docs query_results credentials

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "run_api.py"]
