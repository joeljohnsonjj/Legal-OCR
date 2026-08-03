"""
Delete every object in the configured GCS bucket (live + version archives).

Uses STORAGE_EMULATOR_HOST from .env when set (local emulator); otherwise uses real GCP credentials.

Usage:
  python clear_gcs_bucket.py --yes              # delete all objects via API (emulator must be running)
  python clear_gcs_bucket.py --yes --wipe-local-disk   # stop emulator first: remove fake-gcs-data/.cloudstorage
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def clear_via_api(bucket_name: str) -> int:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    deleted = 0
    for blob in bucket.list_blobs():
        blob.delete()
        deleted += 1
    return deleted


def wipe_emulator_disk() -> None:
    raw = os.getenv("GCS_EMULATOR_DATA_DIR", "./fake-gcs-data").strip()
    data_dir = Path(raw).expanduser()
    if not data_dir.is_absolute():
        data_dir = (_repo_root() / data_dir).resolve()
    cloud = data_dir / ".cloudstorage"
    if cloud.exists():
        shutil.rmtree(cloud)
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"Removed emulator storage: {cloud}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear GCS bucket objects or wipe local emulator files.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required to actually delete (safety).",
    )
    parser.add_argument(
        "--wipe-local-disk",
        action="store_true",
        help="Delete fake-gcs-data/.cloudstorage (stop the emulator first to avoid corruption).",
    )
    args = parser.parse_args()
    if not args.yes:
        print("Refusing to run without --yes", file=sys.stderr)
        return 2

    bucket_name = os.getenv("GCS_BUCKET", "legal-ocr-documents")

    if args.wipe_local_disk:
        wipe_emulator_disk()
        print("Start the emulator again; bucket will be empty until you re-upload or run init_fake_gcs.")
        return 0

    try:
        n = clear_via_api(bucket_name)
    except Exception as e:
        print(f"API delete failed: {e}", file=sys.stderr)
        print(
            "If you use the local emulator: start it, then retry. "
            "Or stop it and run: python clear_gcs_bucket.py --yes --wipe-local-disk",
            file=sys.stderr,
        )
        return 1

    print(f"Deleted {n} object(s) from bucket {bucket_name!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
