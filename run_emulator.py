"""
Standalone GCS Emulator Runner
Runs the Google Cloud Storage emulator as a standalone Python process.
This allows local GCS emulation without Docker.
"""

import os
import sys
import signal
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        pass


# Load environment variables when python-dotenv is installed
load_dotenv()

# Add the gcp-storage-emulator to the path if using local version
emulator_src_path = Path(__file__).parent / "gcp-storage-emulator-main" / "src"
if emulator_src_path.exists():
    sys.path.insert(0, str(emulator_src_path))

try:
    from gcp_storage_emulator.server import create_server
except ImportError:
    # Try installing from local directory
    try:
        import subprocess
        emulator_dir = Path(__file__).parent / "gcp-storage-emulator-main"
        if emulator_dir.exists():
            print(f"Installing gcp-storage-emulator from {emulator_dir}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-e", str(emulator_dir)])
            from gcp_storage_emulator.server import create_server
        else:
            print("ERROR: gcp-storage-emulator not found!")
            print("Please install it with: pip install gcp-storage-emulator")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to import or install gcp-storage-emulator: {e}")
        print("Please install it with: pip install gcp-storage-emulator")
        sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global server instance for cleanup
server = None


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully"""
    logger.info("\nReceived shutdown signal. Stopping emulator...")
    if server:
        server.stop()
    sys.exit(0)


def _register_signal_handlers():
    """Register handlers; skip signals the platform does not support."""
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal_handler)
        except (OSError, ValueError) as e:
            logger.warning("Could not register handler for %s: %s", sig_name, e)


def main():
    """Start the GCS emulator server"""
    global server

    _register_signal_handlers()

    # Get configuration from environment variables
    host = os.getenv("GCS_EMULATOR_HOST", "localhost")
    port_raw = os.getenv("GCS_EMULATOR_PORT", "4443")
    try:
        port = int(port_raw)
    except ValueError:
        logger.error("Invalid GCS_EMULATOR_PORT %r; must be an integer.", port_raw)
        sys.exit(1)
    default_bucket = os.getenv("GCS_BUCKET", "heb-legal")
    data_dir = os.getenv("GCS_EMULATOR_DATA_DIR", "./fake-gcs-data")
    in_memory = os.getenv("GCS_EMULATOR_IN_MEMORY", "false").lower() == "true"
    
    # Convert data_dir to absolute path
    data_dir = str(Path(data_dir).resolve())
    
    # Create data directory if it doesn't exist
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    
    logger.info("=" * 80)
    logger.info("Google Cloud Storage Emulator")
    logger.info("=" * 80)
    logger.info(f"Host: {host}")
    logger.info(f"Port: {port}")
    logger.info(f"Default Bucket: {default_bucket}")
    logger.info(f"Data Directory: {data_dir}")
    logger.info(f"In-Memory: {in_memory}")
    logger.info("=" * 80)
    
    # Set STORAGE_EMULATOR_HOST environment variable
    storage_emulator_url = f"http://{host}:{port}"
    os.environ["STORAGE_EMULATOR_HOST"] = storage_emulator_url
    logger.info(f"STORAGE_EMULATOR_HOST set to: {storage_emulator_url}")
    
    try:
        # Create and start the server
        logger.info("Starting GCS emulator server...")
        server = create_server(
            host=host,
            port=port,
            in_memory=in_memory,
            default_bucket=default_bucket,
            data_dir=data_dir
        )
        
        logger.info(f"GCS emulator is running at {storage_emulator_url}")
        logger.info("Press CTRL+C to stop the server")
        logger.info("=" * 80)
        
        # Run the server (blocks until interrupted)
        server.run()
        
    except KeyboardInterrupt:
        logger.info("\nReceived keyboard interrupt")
    except Exception as e:
        logger.error(f"Error running emulator: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if server:
            logger.info("Stopping emulator...")
            server.stop()
        logger.info("Emulator stopped.")


if __name__ == "__main__":
    main()
