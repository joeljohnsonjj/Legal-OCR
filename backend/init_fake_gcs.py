"""
Initialize Fake GCS Server with default bucket
This script creates the default bucket in fake GCS if it doesn't exist.
Run this on startup to ensure the bucket is available.
"""

import os
import logging
from google.cloud import storage
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def init_fake_gcs_bucket():
    """
    Initialize the default bucket in fake GCS server.
    The bucket name is read from GCS_BUCKET environment variable (default: heb-legal).
    """
    try:
        # Check if STORAGE_EMULATOR_HOST is set (required for fake GCS)
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
                    logger.info(f"Detected emulator on localhost:4443, setting STORAGE_EMULATOR_HOST")
                else:
                    logger.warning(
                        "STORAGE_EMULATOR_HOST not set and emulator not detected on localhost:4443. "
                        "Skipping fake GCS bucket initialization. "
                        "This is normal if you're not using fake GCS."
                    )
                    return False
            except Exception:
                logger.warning(
                    "STORAGE_EMULATOR_HOST not set. Skipping fake GCS bucket initialization. "
                    "This is normal if you're not using fake GCS."
                )
                return False
        
        # Get bucket name from environment variable
        bucket_name = os.getenv("GCS_BUCKET", "heb-legal")
        
        logger.info(f"Initializing fake GCS bucket: {bucket_name}")
        logger.info(f"Fake GCS endpoint: {storage_emulator_host}")
        
        # Create storage client (will automatically use STORAGE_EMULATOR_HOST)
        client = storage.Client()
        
        # Check if bucket exists, create if it doesn't
        try:
            bucket = client.bucket(bucket_name)
            # Try to get bucket metadata to check if it exists
            bucket.reload()
            logger.info(f"Bucket '{bucket_name}' already exists in fake GCS")
            return True
        except Exception as e:
            # Bucket doesn't exist, create it
            logger.info(f"Bucket '{bucket_name}' not found. Creating...")
            bucket = client.create_bucket(bucket_name)
            logger.info(f"Successfully created bucket '{bucket_name}' in fake GCS")
            return True
            
    except Exception as e:
        logger.error(f"Error initializing fake GCS bucket: {e}")
        logger.warning(
            "Fake GCS bucket initialization failed. "
            "The application will continue, but GCS operations may fail if the bucket is needed."
        )
        return False


if __name__ == "__main__":
    """Run bucket initialization when script is executed directly"""
    success = init_fake_gcs_bucket()
    exit(0 if success else 1)
