"""
FastAPI Server Launcher for Legal Obligation Query System
Run this file to start the API server
"""

import uvicorn
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# When using Gemini (not Azure): force API-key-only auth by unsetting ADC so the client uses GEMINI_API_KEY only.
if not os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes"):
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() != "true":
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


def main():
    """Start the FastAPI server"""
    
    # Initialize fake GCS bucket if emulator is available
    # Check if emulator is running (either via STORAGE_EMULATOR_HOST or default port)
    storage_emulator_host = os.getenv("STORAGE_EMULATOR_HOST")
    if not storage_emulator_host:
        # Try to detect if emulator is running on default port
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', 4443))
            sock.close()
            if result == 0:
                # Port is open, assume emulator is running
                storage_emulator_host = "http://localhost:4443"
                os.environ["STORAGE_EMULATOR_HOST"] = storage_emulator_host
                print(f"Detected GCS emulator on localhost:4443")
        except Exception:
            pass
    
    if storage_emulator_host:
        try:
            from init_fake_gcs import init_fake_gcs_bucket
            print("Initializing fake GCS bucket...")
            init_fake_gcs_bucket()
        except Exception as e:
            print(f"Warning: Failed to initialize fake GCS bucket: {e}")
            print("The application will continue, but GCS operations may fail if the bucket is needed.")
    
    # Get configuration from environment or use defaults
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    reload = os.getenv("API_RELOAD", "true").lower() == "true"
    
    print("=" * 80)
    print("Legal Obligation Query System API")
    print("=" * 80)
    print(f"Starting server on http://{host}:{port}")
    print(f"API Documentation: http://{host}:{port}/docs")
    print(f"ReDoc Documentation: http://{host}:{port}/redoc")
    print("=" * 80)
    print("\nPress CTRL+C to stop the server\n")
    
    # Start the server
    uvicorn.run(
        "query_system:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()

