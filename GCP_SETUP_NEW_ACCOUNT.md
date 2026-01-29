# GCP Setup Guide: Migrating to a New Account

This guide walks you through setting up the HEB-Legal-OCR project with a **new GCP (Google Cloud Platform) account**.

---

## Prerequisites

- A Google account (for the new GCP account)
- [Google Cloud SDK (gcloud)](https://cloud.google.com/sdk/docs/install) installed on your machine
- Python 3.x (if running locally, not Docker)

---

## Step 1: Create a New GCP Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Sign in with your **new** Google account
3. Click the project dropdown at the top → **New Project**
4. Enter a **Project name** (e.g., `heb-legal-ocr`)
5. Note your **Project ID** (e.g., `heb-legal-ocr-123456`) — you will need this
6. Click **Create**

---

## Step 2: Enable Required APIs

Enable these APIs for your new project:

1. In Cloud Console, go to **APIs & Services** → **Library**
2. Enable:
   - **Vertex AI API** (for Gemini when using Vertex AI)
   - **Google Cloud Storage JSON API** (for GCS bucket access)
   - **Cloud Storage** (bucket access)

Or use gcloud:

```powershell
# Set your new project
gcloud config set project YOUR_NEW_PROJECT_ID

# Enable APIs
gcloud services enable aiplatform.googleapis.com
gcloud services enable storage-api.googleapis.com
gcloud services enable storage.googleapis.com
```

---

## Step 3: Create a GCS Bucket

1. Go to **Cloud Console** → **Cloud Storage** → **Buckets**
2. Click **Create bucket**
3. Choose a **bucket name** (e.g., `heb-legal` or `your-org-heb-legal`) — must be globally unique
4. Choose a **location** (e.g., `europe-west4` or `us-central1`)
5. Leave other settings as default (or set as needed), then **Create**
6. Inside the bucket, create folders (if needed):
   - **Documents** — for input PDFs
   - **Output** — for consolidated JSON results

Or via gcloud:

```powershell
# Create bucket
gsutil mb -l europe-west4 gs://YOUR_NEW_BUCKET_NAME

# Create folders (folders are implicit when you upload; you can upload a placeholder)
# Or just upload your first file to gs://YOUR_NEW_BUCKET_NAME/Documents/ and gs://YOUR_NEW_BUCKET_NAME/Output/
```

---

## Step 4: Set Up Authentication

### Option A: Application Default Credentials (recommended for local/dev)

```powershell
# Login with your NEW Google account
gcloud auth login

# Set the new project
gcloud config set project YOUR_NEW_PROJECT_ID

# Create application default credentials (used by the app)
gcloud auth application-default login
```

Credentials file location:
- **Windows**: `C:\Users\<YourUsername>\AppData\Roaming\gcloud\application_default_credentials.json`
- **Linux/Mac**: `~/.config/gcloud/application_default_credentials.json`

### Option B: Service Account Key (for CI/CD or when ADC is not possible)

1. Go to **IAM & Admin** → **Service Accounts**
2. Click **Create Service Account**
3. Name it (e.g., `heb-legal-ocr-sa`) → **Create and Continue**
4. Grant roles:
   - **Vertex AI User** (for Gemini/Vertex AI)
   - **Storage Object Admin** (for GCS read/write)
   - Or **Storage Admin** if you prefer
5. Create a **JSON key** for this service account and download it
6. Store the JSON file somewhere safe (e.g., `C:\secure\gcp-key.json`)
7. Set the env var:
   ```powershell
   $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\secure\gcp-key.json"
   ```

---

## Step 5: Enable Vertex AI for Your Region

Vertex AI (and Gemini) must be enabled in the region you use:

1. Go to **Vertex AI** in the Cloud Console
2. Select the same **region** you use (e.g., `europe-west4`)
3. Accept terms if prompted; the API is enabled via Step 2

Ensure the **Vertex AI API** is enabled for the project (Step 2).

---

## Step 6: Update Your Project Configuration

### 6.1 Update `.env` file

Edit the `.env` file in the project root:

```env
# ========== GCP / Vertex AI ==========
GOOGLE_GENAI_USE_VERTEXAI=True
GOOGLE_CLOUD_PROJECT=YOUR_NEW_PROJECT_ID
GOOGLE_CLOUD_LOCATION=europe-west4
GEMINI_MODEL=gemini-2.5-flash-lite

# ========== GCS ==========
GCS_BUCKET=YOUR_NEW_BUCKET_NAME
GCS_DOCS_FOLDER=Documents
GCS_OUTPUT_FOLDER=Output
```

Replace:
- `YOUR_NEW_PROJECT_ID` — Project ID from Step 1
- `YOUR_NEW_BUCKET_NAME` — Bucket name from Step 3
- `europe-west4` — Use your bucket/model region if different

### 6.2 If Using Gemini API Key Instead of Vertex AI

If you use the Gemini Developer API instead of Vertex AI:

```env
GOOGLE_GENAI_USE_VERTEXAI=False
GEMINI_API_KEY=your_api_key_here
# or
GOOGLE_API_KEY=your_api_key_here
```

Get an API key from [Google AI Studio](https://aistudio.google.com/app/apikey).

### 6.3 If Using Docker

- Set `GCP_CREDENTIALS_FILE_HOST` in `.env` (or in your shell) to the path of your credentials file (see Step 4).
- Ensure `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GCS_BUCKET` in `.env` match the new project and bucket.

---

## Step 7: Verify Setup

### Local run

```powershell
# From project root
python -c "
from dotenv import load_dotenv
import os
load_dotenv()
print('Project:', os.getenv('GOOGLE_CLOUD_PROJECT'))
print('Bucket:', os.getenv('GCS_BUCKET'))
print('Location:', os.getenv('GOOGLE_CLOUD_LOCATION'))
"
```

Then start the API and hit health:

```powershell
python run_api.py
# In another terminal or browser: http://localhost:8000/health
```

### GCS

- Upload a test PDF to `gs://YOUR_NEW_BUCKET_NAME/Documents/`
- Or use the **Upload** button in Cloud Console → Storage → your bucket → **Documents**

### Vertex AI / Gemini

- A successful call to `/query` or `/process` that uses Gemini confirms Vertex AI and credentials are working.

---

## Checklist Summary

| Step | Action |
|------|--------|
| 1 | Create new GCP project, note **Project ID** |
| 2 | Enable Vertex AI, Storage APIs |
| 3 | Create GCS bucket, note **Bucket name**; use **Documents** and **Output** |
| 4 | Run `gcloud auth login` and `gcloud auth application-default login` (or create and use a service account key) |
| 5 | Ensure Vertex AI is enabled for your region |
| 6 | Update `.env`: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GCS_BUCKET` (and optional `GEMINI_MODEL`) |
| 7 | Run app and test `/health`, then query/process |

---

## Environment Variables Quick Reference

| Variable | Description | Example |
|----------|-------------|---------|
| `GOOGLE_GENAI_USE_VERTEXAI` | Use Vertex AI (`True`) or Gemini API (`False`) | `True` |
| `GOOGLE_CLOUD_PROJECT` | GCP Project ID | `my-project-123` |
| `GOOGLE_CLOUD_LOCATION` | Region for Vertex AI | `europe-west4` |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.5-flash-lite` |
| `GCS_BUCKET` | GCS bucket name | `heb-legal` |
| `GCS_DOCS_FOLDER` | Folder for input PDFs | `Documents` |
| `GCS_OUTPUT_FOLDER` | Folder for output JSON | `Output` |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Only if **not** using Vertex AI | - |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to service account JSON (if not using ADC) | `C:\secure\gcp-key.json` |
| `GCP_CREDENTIALS_FILE_HOST` | Path to credentials on host (for Docker) | `C:/Users/You/.../application_default_credentials.json` |

---

## Troubleshooting

- **Permission denied / 403**: Check IAM roles for your user or service account (Vertex AI User, Storage Object Admin or Storage Admin).
- **Bucket or project not found**: Double-check project ID and bucket name in `.env` and in the console.
- **Vertex AI not available in region**: Use a [supported region](https://cloud.google.com/vertex-ai/docs/general/locations) and the same value in `GOOGLE_CLOUD_LOCATION`.
- **Invalid credentials**: Run `gcloud auth application-default login` again, or ensure `GOOGLE_APPLICATION_CREDENTIALS` points to a valid service account JSON.
