"""
Copy all objects from one GCS bucket to another (emulator or real GCP).

Usage:
  python scripts/migrate_gcs_bucket.py --from heb-legal --to legal-ocr-documents
  python scripts/migrate_gcs_bucket.py --from heb-legal --to legal-ocr-documents --seed-docs

Requires GCS_BUCKET / STORAGE_EMULATOR_HOST in .env when using the local emulator.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gcs_document_versioning import publish_new_version  # noqa: E402


def _copy_bucket(source: str, dest: str, *, dry_run: bool) -> int:
    from google.cloud import storage
    from google.cloud.exceptions import NotFound

    client = storage.Client()

    try:
        client.bucket(source).reload()
    except NotFound as e:
        print(f"Source bucket {source!r} not found: {e}", file=sys.stderr)
        return 1

    dest_bucket = client.bucket(dest)
    try:
        dest_bucket.reload()
    except NotFound:
        if dry_run:
            print(f"Destination bucket {dest!r} would be created")
        else:
            print(f"Creating destination bucket {dest!r}...")
            dest_bucket = client.create_bucket(dest)

    copied = 0
    for blob in client.list_blobs(source):
        target_name = blob.name
        if dry_run:
            print(f"  would copy: {target_name}")
            copied += 1
            continue

        source_bucket = client.bucket(source)
        source_bucket.copy_blob(blob, dest_bucket, target_name)
        print(f"  copied: {target_name}")
        copied += 1

    return copied


def _seed_local_docs(*, dry_run: bool) -> int:
    import os

    docs_dir = Path(os.getenv("DOCS_FOLDER", "docs"))
    if not docs_dir.is_absolute():
        docs_dir = (_REPO_ROOT / docs_dir).resolve()

    gcs_docs = os.getenv("GCS_DOCS_FOLDER", "Documents").strip().strip("/") or "Documents"
    seeded = 0

    if not docs_dir.is_dir():
        print(f"No local docs folder at {docs_dir}", file=sys.stderr)
        return 0

    for path in sorted(docs_dir.glob("*.pdf")):
        live_object_name = f"{gcs_docs}/{path.name}"
        if dry_run:
            print(f"  would seed: {path} -> {live_object_name}")
            seeded += 1
            continue

        data = path.read_bytes()
        ct = mimetypes.guess_type(path.name)[0] or "application/pdf"
        publish_new_version(live_object_name, data, content_type=ct)
        print(f"  seeded: {path.name} -> {live_object_name}")
        seeded += 1

    return seeded


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate GCS bucket objects to a new bucket.")
    parser.add_argument("--from", dest="source", default="heb-legal", help="Source bucket name")
    parser.add_argument("--to", dest="dest", default="legal-ocr-documents", help="Destination bucket name")
    parser.add_argument(
        "--seed-docs",
        action="store_true",
        help="After copy, upload local DOCS_FOLDER/*.pdf into the destination bucket",
    )
    parser.add_argument("--dry-run", action="store_true", help="List actions without writing")
    args = parser.parse_args()

    print(f"Migrating {args.source!r} -> {args.dest!r}")
    copied = _copy_bucket(args.source, args.dest, dry_run=args.dry_run)
    print(f"Copied {copied} object(s).")

    if args.seed_docs:
        print("Seeding local PDFs into destination bucket...")
        seeded = _seed_local_docs(dry_run=args.dry_run)
        print(f"Seeded {seeded} file(s).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
