"""
FastAPI Server Launcher for Legal Obligation Query System
Run this file to start the API server
"""

import uvicorn
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Load LLM config from AWS Parameter Store (AssumeRole flow) if LLM_PARAMETER_PATH is set
import logging as _logging
_ra_logger = _logging.getLogger(__name__)
try:
    from aws_parameter_loader import init_llm_env
    _llm_cfg = init_llm_env()
    if _llm_cfg:
        _ra_logger.info("AWS Bedrock AssumeRole OK: model=%s", _llm_cfg.get("model"))
    else:
        _ra_logger.warning("init_llm_env returned None — falling back to LLM_MODEL from .env")
except Exception as _e:
    _ra_logger.warning("init_llm_env failed: %s", _e)

# Fallback: if AssumeRole failed but LLM_MODEL is set in .env, still route to LiteLLM/Bedrock
if not os.getenv("LITELLM_MODEL") and os.getenv("LLM_MODEL"):
    _lm = (os.getenv("LLM_MODEL") or "").strip()
    if _lm:
        os.environ["LITELLM_MODEL"] = _lm
        _ra_logger.info("Fallback: LITELLM_MODEL set from LLM_MODEL=%s", _lm)

# Sanitize credentials: remove ALL newlines/carriage returns (prevents "Newline in headers" with Bedrock/LiteLLM)
def _sanitize_header_value(s: str) -> str:
    if not s or not isinstance(s, str):
        return s or ""
    return s.replace("\r", "").replace("\n", "").strip()


for _var in (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "API_KEY", "AWS_REGION", "AWS_REGION_NAME",
    "LITELLM_MODEL", "LLM_MODEL",
):
    _val = os.environ.get(_var)
    if _val is not None and isinstance(_val, str):
        _clean = _sanitize_header_value(_val)
        if _clean != _val:
            os.environ[_var] = _clean

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

