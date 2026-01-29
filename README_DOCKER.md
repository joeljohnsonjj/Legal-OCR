# Docker Setup Guide for HEB Legal OCR

This guide explains how to run the HEB Legal OCR application using Docker with direct Gemini API (no GCP credentials required).

## Prerequisites

- Docker and Docker Compose installed
- Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey)

## Setup Instructions

### Step 1: Get Your Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Sign in with your Google account
3. Click "Create API Key" or use an existing key
4. Copy your API key (starts with `AIza...`)

### Step 2: Configure Environment Variables

Create a `.env` file in the project root (docker-compose will automatically load it):

```env
# Required: Gemini API Key
GEMINI_API_KEY=your-api-key-here

# Optional: Gemini Configuration
GOOGLE_GENAI_USE_VERTEXAI=False
GEMINI_MODEL=gemini-2.5-flash

# Optional: GCS Configuration (for future GCS integration)
GCS_BUCKET=heb-legal
GCS_DOCS_FOLDER=Documents
GCS_OUTPUT_FOLDER=Output

# Optional: Processing Configuration
DOCS_FOLDER=docs
OUTPUT_FOLDER=output
LOGS_FOLDER=logs
CACHE_FOLDER=ocr_cache
```

**Important Notes:**
- The `GEMINI_API_KEY` is required for the application to work
- Set `GOOGLE_GENAI_USE_VERTEXAI=False` to use direct Gemini API (recommended)
- No GCP credentials are needed when using direct API mode
- Fake GCS server is automatically started for local storage emulation


## Building and Running

### Method 1: Using Docker Compose (Recommended)

```powershell
# Build the image
docker-compose build

# Start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down
```

## Accessing the Application

Once the container is running:

- **API Base URL**: http://localhost:8000
- **Interactive API Docs**: http://localhost:8000/docs
- **ReDoc Documentation**: http://localhost:8000/redoc
- **Health Check**: http://localhost:8000/health


## Fake GCS Server

The Docker Compose setup includes a fake GCS server for local Google Cloud Storage emulation. This allows you to test GCS functionality locally without connecting to real GCS.

- **Service**: `fake-gcs-server`
- **Port**: `4443` (internal), accessible at `http://fake-gcs-server:4443` from within containers
- **Storage**: Data persists in `./fake-gcs-data` directory on the host
- **Initialization**: The default bucket is automatically created on first use

The fake GCS server is automatically configured via the `STORAGE_EMULATOR_HOST` environment variable. The `google-cloud-storage` library automatically detects this and connects to the emulator instead of real GCS.

## Environment Variables Reference

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `GEMINI_API_KEY` | Gemini API key from AI Studio | - | **Yes** |
| `GOOGLE_GENAI_USE_VERTEXAI` | Use Vertex AI instead of direct API | `false` | No |
| `GEMINI_MODEL` | Gemini model to use | `gemini-2.5-flash` | No |
| `GOOGLE_CLOUD_PROJECT` | GCP Project ID (only if using Vertex AI) | - | No |
| `GOOGLE_CLOUD_LOCATION` | GCP Region (only if using Vertex AI) | `us-central1` | No |
| `GCS_BUCKET` | GCS bucket name (for future GCS integration) | `heb-legal` | No |
| `GCS_DOCS_FOLDER` | Documents folder in GCS | `Documents` | No |
| `GCS_OUTPUT_FOLDER` | Output folder in GCS | `Output` | No |
| `STORAGE_EMULATOR_HOST` | Fake GCS endpoint (auto-set) | `http://fake-gcs-server:4443` | Auto-set |
| `FAKE_GCS_ENDPOINT` | Fake GCS endpoint URL | `http://fake-gcs-server:4443` | Auto-set |
| `DOCS_FOLDER` | Local docs folder | `docs` | No |
| `OUTPUT_FOLDER` | Local output folder | `output` | No |
| `LOGS_FOLDER` | Local logs folder | `logs` | No |
| `CACHE_FOLDER` | Local OCR cache folder | `ocr_cache` | No |


### Docker Hub (Public or Private Registry)

**Best for**: Regular updates, team collaboration, or when multiple people need access.

**Step 1: Push to Docker Hub**

```powershell
# Login to Docker Hub
docker login

# Tag the image with your Docker Hub username
docker tag heb-legal-ocr-api:latest your-dockerhub-username/heb-legal-ocr:latest

# Push to Docker Hub
docker push your-dockerhub-username/heb-legal-ocr:latest
```

**Step 2: Share with your colleague**

Your colleague can pull and run:

```powershell
# Pull the image
docker pull your-dockerhub-username/heb-legal-ocr:latest

# Update docker-compose.yml to use the pulled image
# Change the build section to:
# image: your-dockerhub-username/heb-legal-ocr:latest

# Or run directly
docker run -d `
  --name heb-legal-ocr-api `
  -p 8000:8000 `
  -e GOOGLE_GENAI_USE_VERTEXAI=false `
  -e GEMINI_API_KEY=your-api-key-here `
  your-dockerhub-username/heb-legal-ocr:latest
```

**For private repositories:**
```powershell
# Create a private repository on Docker Hub, then:
docker login
docker tag heb-legal-ocr-api:latest your-dockerhub-username/heb-legal-ocr:latest
docker push your-dockerhub-username/heb-legal-ocr:latest

# Your colleague needs to login first:
docker login
docker pull your-dockerhub-username/heb-legal-ocr:latest
```

**Advantages:**
- ✅ Easy to update and share new versions
- ✅ No large file transfers
- ✅ Version tagging support

**Disadvantages:**
- ❌ Requires Docker Hub account (free tier available)
- ❌ Requires internet connection

---

