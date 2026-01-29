# GCS Emulator Setup Guide

This guide explains how to run the Google Cloud Storage (GCS) emulator for local development without Docker.

## Quick Start

### Option 1: Standalone Python Emulator (Recommended)

1. **Install the emulator** (if not already installed):
   ```bash
   # Option A: Install from local directory
   pip install -e ./gcp-storage-emulator-main
   
   # Option B: Install from PyPI
   pip install gcp-storage-emulator
   ```

2. **Start the emulator**:
   ```bash
   python run_emulator.py
   ```

   The emulator will:
   - Start on `http://localhost:4443` (default)
   - Create default bucket `heb-legal` (from `GCS_BUCKET` env var)
   - Store data in `./fake-gcs-data` directory
   - Set `STORAGE_EMULATOR_HOST` automatically

3. **In another terminal, start the API**:
   ```bash
   python run_api.py
   ```

   The API will automatically detect the emulator and initialize the bucket.

### Option 2: Docker-based Emulator

If you prefer using Docker:

1. **Uncomment the fake-gcs-server service** in `docker-compose.yml`

2. **Start with Docker Compose**:
   ```bash
   docker-compose up fake-gcs-server
   ```

## Configuration

### Environment Variables

You can configure the emulator using environment variables in your `.env` file:

```env
# GCS Emulator Configuration
GCS_EMULATOR_HOST=localhost          # Host to bind to (default: localhost)
GCS_EMULATOR_PORT=4443              # Port to run on (default: 4443)
GCS_EMULATOR_DATA_DIR=./fake-gcs-data  # Directory for persistent storage
GCS_EMULATOR_IN_MEMORY=false        # Use in-memory storage (default: false)

# GCS Bucket Configuration
GCS_BUCKET=heb-legal                # Default bucket name

# Storage Emulator Host (auto-set by run_emulator.py)
STORAGE_EMULATOR_HOST=http://localhost:4443
```

### Command Line Options

You can also run the emulator directly using the gcp-storage-emulator CLI:

```bash
# Basic usage
gcp-storage-emulator start

# Custom port and host
gcp-storage-emulator start --host=localhost --port=4443

# With default bucket
gcp-storage-emulator start --default-bucket=heb-legal

# In-memory mode (no persistence)
gcp-storage-emulator start --in-memory

# Custom data directory
gcp-storage-emulator start --data-dir=./my-storage
```

## Usage

### Starting the Emulator

**Standalone mode:**
```bash
python run_emulator.py
```

**Using gcp-storage-emulator CLI:**
```bash
gcp-storage-emulator start --host=localhost --port=4443 --default-bucket=heb-legal
```

### Stopping the Emulator

Press `CTRL+C` in the terminal where the emulator is running.

### Wiping Data

To clear all emulator data:

```bash
gcp-storage-emulator wipe
```

To wipe data but keep buckets:

```bash
gcp-storage-emulator wipe --keep-buckets
```

## Integration with Application

The application automatically detects and uses the emulator when:

1. `STORAGE_EMULATOR_HOST` environment variable is set, OR
2. An emulator is detected running on `localhost:4443`

The `run_api.py` script will:
- Automatically detect if emulator is running
- Initialize the default bucket if needed
- Set `STORAGE_EMULATOR_HOST` if not already set

## Testing the Emulator

### Check if emulator is running:

```bash
curl http://localhost:4443/storage/v1/b
```

### Test with Python:

```python
import os
from google.cloud import storage

# Set emulator host
os.environ["STORAGE_EMULATOR_HOST"] = "http://localhost:4443"

# Create client
client = storage.Client()

# List buckets
buckets = list(client.list_buckets())
print("Buckets:", [b.name for b in buckets])

# Create a test blob
bucket = client.bucket("heb-legal")
blob = bucket.blob("test.txt")
blob.upload_from_string("Hello, World!")
print("Uploaded test.txt")

# Download it
content = blob.download_as_text()
print("Content:", content)
```

## Troubleshooting

### Port Already in Use

If port 4443 is already in use:

1. Change the port in `.env`:
   ```env
   GCS_EMULATOR_PORT=4444
   ```

2. Update `STORAGE_EMULATOR_HOST`:
   ```env
   STORAGE_EMULATOR_HOST=http://localhost:4444
   ```

### Emulator Not Detected

If the API doesn't detect the emulator:

1. Make sure the emulator is running
2. Check that `STORAGE_EMULATOR_HOST` is set correctly
3. Verify the port matches (default: 4443)

### Import Errors

If you get import errors for `gcp_storage_emulator`:

```bash
# Install from local directory
pip install -e ./gcp-storage-emulator-main

# Or install from PyPI
pip install gcp-storage-emulator
```

## Differences: Standalone vs Docker

| Feature | Standalone (`run_emulator.py`) | Docker (`fake-gcs-server`) |
|---------|-------------------------------|---------------------------|
| **Setup** | Requires Python package | Requires Docker |
| **Port** | localhost:4443 | localhost:4443 (mapped) |
| **Data Storage** | `./fake-gcs-data` | `./fake-gcs-data` (volume) |
| **Auto-start** | Manual or via script | Via docker-compose |
| **Dependencies** | Python + gcp-storage-emulator | Docker + image |
| **Best for** | Local development | Containerized environments |

## Next Steps

- The emulator is ready to use once started
- Your application will automatically connect to it
- Buckets are created automatically on first use
- Data persists in `./fake-gcs-data` directory
