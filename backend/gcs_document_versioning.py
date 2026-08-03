"""
Versioned uploads for Google Cloud Storage (including the local gcp-storage-emulator).

Layout (single stable "live" object name; archives are copies under a hidden prefix):

  Live:     {live_object_name}                     e.g. Documents/Lease.pdf
  Archive:  {archive_prefix}/{content_key}/{version_id}/{basename}

content_key is the first 16 hex chars of SHA-256(live_object_name) so archives for a
given live path are listable without collisions between different live paths.

Environment:
  GCS_BUCKET                  — bucket name (default: legal-ocr-documents)
  GCS_DOCS_FOLDER             — used only to build default archive prefix (default: Documents)
  GCS_VERSION_ARCHIVE_PREFIX  — override archive root (default: {GCS_DOCS_FOLDER}/.versions)
  STORAGE_EMULATOR_HOST       — set by run_emulator.py / .env for fake GCS
  GCS_SYNC_LIVE_TO_DOCS       — if true, mirror the live object into DOCS_FOLDER after upload/restore.
                                If unset and STORAGE_EMULATOR_HOST is set, sync defaults to on (local dev).
  DOCS_FOLDER                 — local folder for sync (same default as /process: docs)
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.cloud import storage
from google.cloud.exceptions import NotFound

logger = logging.getLogger(__name__)


def _blob_updated_to_str(blob: Any) -> Optional[str]:
    """GCS client returns datetime for Blob.updated; API models expect a string."""
    raw = getattr(blob, "updated", None)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.isoformat()
    return str(raw)


def _normalize_live_object_name(name: str) -> str:
    n = (name or "").strip().lstrip("/")
    if not n:
        raise ValueError("live_object_name must be non-empty")
    return n


def content_key_for_live_path(live_object_name: str) -> str:
    """Stable short key for a live object path (used for archive folder names)."""
    normalized = _normalize_live_object_name(live_object_name)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def default_archive_prefix() -> str:
    docs = os.getenv("GCS_DOCS_FOLDER", "Documents").strip().strip("/") or "Documents"
    override = os.getenv("GCS_VERSION_ARCHIVE_PREFIX", "").strip()
    if override:
        return override.lstrip("/")
    return f"{docs}/.versions"


def version_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sync_live_to_docs_enabled() -> bool:
    raw = os.getenv("GCS_SYNC_LIVE_TO_DOCS", "").strip()
    if raw:
        return raw.lower() in ("1", "true", "yes", "on")
    # Unset: mirror when using the local emulator so /gcs/versioned-upload updates docs/ without extra .env lines.
    return bool(os.getenv("STORAGE_EMULATOR_HOST", "").strip())


def _project_root() -> Path:
    """Directory containing this module (Legal-OCR repo root when installed flat)."""
    return Path(__file__).resolve().parent


def _docs_folder_path() -> Path:
    raw = (os.getenv("DOCS_FOLDER") or "docs").strip()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    # Relative paths must not depend on the shell CWD when the API is started elsewhere.
    return (_project_root() / p).resolve()


def _local_docs_target_file(live_object_name: str) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (basename, error). basename is safe for a single file under DOCS_FOLDER.
    """
    live = _normalize_live_object_name(live_object_name)
    base = os.path.basename(live.replace("\\", "/"))
    if not base or base in (".", ".."):
        return None, "live_object_name must end with a file name (e.g. Documents/Lease.pdf)"
    return base, None


def maybe_sync_live_bytes_to_docs(
    live_object_name: str,
    data: bytes,
) -> tuple[Optional[str], Optional[str]]:
    """
    If GCS_SYNC_LIVE_TO_DOCS is set, write bytes to DOCS_FOLDER / basename(live_object_name).

    Returns (absolute_path_str, None) on success, (None, err) on failure, (None, None) if disabled.
    """
    if not _sync_live_to_docs_enabled():
        return None, None
    base, err = _local_docs_target_file(live_object_name)
    if err:
        logger.warning("GCS sync to docs skipped: %s", err)
        return None, err
    assert base is not None
    dest_dir = _docs_folder_path()
    dest = dest_dir / base
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info("Synced live object to local docs: %s", dest)
        return str(dest), None
    except OSError as e:
        msg = f"Failed to write {dest}: {e}"
        logger.error(msg)
        return None, msg


def maybe_sync_live_from_bucket_to_docs(
    live_object_name: str,
    *,
    bucket: Optional[storage.Bucket] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Download the current live blob and mirror into DOCS_FOLDER when sync is enabled.
    Used after restore (or whenever the live object changed without local bytes in hand).
    """
    if not _sync_live_to_docs_enabled():
        return None, None
    live = _normalize_live_object_name(live_object_name)
    if bucket is None:
        bucket = get_bucket()
    blob = bucket.blob(live)
    try:
        blob.reload()
    except NotFound:
        msg = f"Live object not found for sync: {live}"
        logger.warning(msg)
        return None, msg
    try:
        data = blob.download_as_bytes()
    except Exception as e:
        msg = f"Failed to download live object for sync: {e}"
        logger.error(msg)
        return None, msg
    return maybe_sync_live_bytes_to_docs(live_object_name, data)


def get_bucket(client: Optional[storage.Client] = None) -> storage.Bucket:
    bucket_name = os.getenv("GCS_BUCKET", "legal-ocr-documents")
    if client is None:
        client = storage.Client()
    return client.bucket(bucket_name)


@dataclass
class PublishResult:
    live_object_name: str
    archived_previous: bool
    archive_object_name: Optional[str]
    version_id: Optional[str]
    bucket: str


def publish_new_version(
    live_object_name: str,
    data: bytes,
    *,
    content_type: Optional[str] = None,
    bucket: Optional[storage.Bucket] = None,
    archive_prefix: Optional[str] = None,
) -> PublishResult:
    """
    If an object already exists at live_object_name, copy it to the archive tree, then
    upload `data` to live_object_name (replacing the live object).
    """
    live = _normalize_live_object_name(live_object_name)
    if bucket is None:
        bucket = get_bucket()
    archive_prefix = (archive_prefix or default_archive_prefix()).strip().rstrip("/")
    key = content_key_for_live_path(live)
    base = os.path.basename(live)
    vid: Optional[str] = None
    archive_name: Optional[str] = None
    archived = False

    live_blob = bucket.blob(live)
    if live_blob.exists():
        vid = version_id_now()
        archive_name = f"{archive_prefix}/{key}/{vid}/{base}"
        bucket.copy_blob(live_blob, bucket, archive_name)
        archived = True
        logger.info("Archived previous live object to gs://%s/%s", bucket.name, archive_name)

    if content_type:
        live_blob.content_type = content_type
    live_blob.upload_from_string(data, content_type=content_type)

    return PublishResult(
        live_object_name=live,
        archived_previous=archived,
        archive_object_name=archive_name,
        version_id=vid,
        bucket=bucket.name,
    )


@dataclass
class ArchivedVersionInfo:
    archive_object_name: str
    version_id: str
    size: Optional[int] = None
    updated: Optional[str] = None


def list_archived_versions(
    live_object_name: str,
    *,
    bucket: Optional[storage.Bucket] = None,
    archive_prefix: Optional[str] = None,
) -> List[ArchivedVersionInfo]:
    """List archived blobs for the given live object path (newest first by version_id string)."""
    live = _normalize_live_object_name(live_object_name)
    if bucket is None:
        bucket = get_bucket()
    archive_prefix = (archive_prefix or default_archive_prefix()).strip().rstrip("/")
    key = content_key_for_live_path(live)
    prefix = f"{archive_prefix}/{key}/"

    out: List[ArchivedVersionInfo] = []
    for b in bucket.list_blobs(prefix=prefix):
        name = b.name
        parts = name[len(prefix) :].split("/", 2)
        if len(parts) < 2:
            continue
        vid, _rest = parts[0], parts[1]
        out.append(
            ArchivedVersionInfo(
                archive_object_name=name,
                version_id=vid,
                size=getattr(b, "size", None),
                updated=_blob_updated_to_str(b),
            )
        )

    out.sort(key=lambda x: x.version_id, reverse=True)
    return out


def restore_archived_to_live(
    live_object_name: str,
    archive_object_name: str,
    *,
    bucket: Optional[storage.Bucket] = None,
    archive_prefix: Optional[str] = None,
) -> PublishResult:
    """
    Make the archived blob the new live object. The current live object is archived first
    (if it exists), then the specified archive is copied to the live path.
    """
    live = _normalize_live_object_name(live_object_name)
    arch = _normalize_live_object_name(archive_object_name)
    if bucket is None:
        bucket = get_bucket()
    archive_prefix = archive_prefix or default_archive_prefix()
    key = content_key_for_live_path(live)
    expected_root = f"{archive_prefix.strip().rstrip('/')}/{key}/"
    if not arch.startswith(expected_root):
        raise ValueError(
            f"archive_object_name must be under the version store for this live path: {expected_root}"
        )

    src = bucket.blob(arch)
    try:
        src.reload()
    except NotFound:
        raise FileNotFoundError(f"Archived object not found: {arch}") from None

    data = src.download_as_bytes()
    ct = src.content_type
    return publish_new_version(live, data, content_type=ct, bucket=bucket, archive_prefix=archive_prefix)


def restore_version_by_id(
    live_object_name: str,
    version_id: str,
    *,
    bucket: Optional[storage.Bucket] = None,
    archive_prefix: Optional[str] = None,
) -> PublishResult:
    """Restore using the version_id folder name (from list_archived_versions)."""
    live = _normalize_live_object_name(live_object_name)
    base = os.path.basename(live)
    archive_prefix = (archive_prefix or default_archive_prefix()).strip().rstrip("/")
    key = content_key_for_live_path(live)
    archive_name = f"{archive_prefix}/{key}/{version_id.strip()}/{base}"
    return restore_archived_to_live(live, archive_name, bucket=bucket, archive_prefix=archive_prefix)


def publish_result_to_dict(r: PublishResult) -> Dict[str, Any]:
    return {
        "bucket": r.bucket,
        "live_object_name": r.live_object_name,
        "archived_previous": r.archived_previous,
        "archive_object_name": r.archive_object_name,
        "version_id": r.version_id,
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python gcs_document_versioning.py publish <local_file> <live_object_name>\n"
            "  python gcs_document_versioning.py list <live_object_name>\n"
            "  python gcs_document_versioning.py restore <live_object_name> <version_id>\n",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = sys.argv[1].lower()
    if cmd == "publish" and len(sys.argv) == 4:
        path, live = sys.argv[2], sys.argv[3]
        with open(path, "rb") as f:
            data = f.read()
        r = publish_new_version(live, data)
        out = publish_result_to_dict(r)
        sp, se = maybe_sync_live_bytes_to_docs(live, data)
        if sp:
            out["synced_local_path"] = sp
        if se:
            out["sync_error"] = se
        print(out)
    elif cmd == "list" and len(sys.argv) == 3:
        for v in list_archived_versions(sys.argv[2]):
            print(v.version_id, v.archive_object_name)
    elif cmd == "restore" and len(sys.argv) == 4:
        live = sys.argv[2]
        r = restore_version_by_id(live, sys.argv[3])
        out = publish_result_to_dict(r)
        sp, se = maybe_sync_live_from_bucket_to_docs(live)
        if sp:
            out["synced_local_path"] = sp
        if se:
            out["sync_error"] = se
        print(out)
    else:
        print("Invalid arguments", file=sys.stderr)
        sys.exit(1)
