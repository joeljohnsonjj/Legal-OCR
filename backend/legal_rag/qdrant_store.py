"""
Qdrant collection management + search + parent-page fetch.

Uses a single collection with `record_type` in payload for filtering (rag_architecture.md §2–3).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from legal_rag.schemas import (
    RECORD_TYPE_EXTRACTED_OBLIGATION,
    RECORD_TYPE_RAW_PAGE,
    obligation_embedding_text,
    payload_to_qdrant_dict,
    build_raw_page_point,
    build_extracted_obligation_point,
)

logger = logging.getLogger(__name__)

# Local Qdrant (path=) allows only one client per folder per process; creating a new
# QdrantClient for delete then again for upsert causes: "already accessed by another instance".
_local_qdrant_client = None
_local_qdrant_path: Optional[str] = None
_remote_qdrant_client = None
_remote_qdrant_key: Optional[Tuple[str, str]] = None

_UPSERT_BATCH = 128


def _client():
    from qdrant_client import QdrantClient

    global _local_qdrant_client, _local_qdrant_path
    global _remote_qdrant_client, _remote_qdrant_key

    url = os.getenv("QDRANT_URL", "").strip()
    if url:
        api_key = (os.getenv("QDRANT_API_KEY") or "").strip()
        key = (url, api_key)
        if _remote_qdrant_client is None or _remote_qdrant_key != key:
            _remote_qdrant_client = QdrantClient(url=url, api_key=api_key or None)
            _remote_qdrant_key = key
        return _remote_qdrant_client
    path = os.getenv("QDRANT_PATH", "").strip() or str(
        os.path.join(os.getenv("OUTPUT_FOLDER", "output"), "qdrant_rag")
    )
    if _local_qdrant_client is None or _local_qdrant_path != path:
        _local_qdrant_client = QdrantClient(path=path)
        _local_qdrant_path = path
    return _local_qdrant_client


def _collection_name() -> str:
    return os.getenv("RAG_QDRANT_COLLECTION", "legal_rag").strip() or "legal_rag"


def _existing_vector_size(client, name: str) -> Optional[int]:
    """Return configured vector size for a single-vector collection, or None if unknown."""
    try:
        info = client.get_collection(collection_name=name)
        vectors = info.config.params.vectors
        if vectors is None:
            return None
        if isinstance(vectors, dict):
            if not vectors:
                return None
            v = next(iter(vectors.values()))
        else:
            v = vectors
        return int(getattr(v, "size", 0)) or None
    except Exception:
        return None


def _ensure_payload_indexes(client, name: str) -> None:
    """Keyword/integer indexes for filtered search on Qdrant server (skipped for local path mode)."""
    if not os.getenv("QDRANT_URL", "").strip():
        return

    from qdrant_client.models import PayloadSchemaType

    for field_name, schema in (
        ("record_type", PayloadSchemaType.KEYWORD),
        ("document_id", PayloadSchemaType.KEYWORD),
        ("page_number", PayloadSchemaType.INTEGER),
    ):
        try:
            client.create_payload_index(
                collection_name=name,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "duplicate" in msg:
                continue
            logger.debug("payload index %s on %s: %s", field_name, name, e)


def ensure_collection(vector_size: int) -> None:
    """Create collection if missing; recreate if vector dimension mismatches embeddings."""
    from qdrant_client.models import Distance, VectorParams

    client = _client()
    name = _collection_name()
    existing = _existing_vector_size(client, name)
    if existing is not None and existing != vector_size:
        logger.warning(
            "Qdrant collection %s has vector size %s but current embeddings are %s; "
            "deleting and recreating collection (re-index all documents).",
            name,
            existing,
            vector_size,
        )
        try:
            client.delete_collection(collection_name=name)
        except Exception as e:
            logger.warning("Could not delete Qdrant collection %s: %s", name, e)

    try:
        client.get_collection(collection_name=name)
        # Idempotent: ensures filters stay fast on older collections created before indexes.
        _ensure_payload_indexes(client, name)
        return
    except Exception:
        pass

    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )
    logger.info("Created Qdrant collection %s (dim=%s)", name, vector_size)
    _ensure_payload_indexes(client, name)


def upsert_document_pages_and_obligations(
    document_id: str,
    page_texts: Dict[int, str],
    consolidated_obligations: List[Dict[str, Any]],
    page_for_obligation: Optional[List[int]] = None,
) -> Dict[str, int]:
    """
    Index one document: raw_page points per page + extracted_obligation per obligation.

    :param document_id: Stable id (e.g. PDF filename).
    :param page_texts: Map 1-based page -> full raw text.
    :param consolidated_obligations: Same order as processing (index = obligation_index).
    :param page_for_obligation: Optional parallel list of page numbers per obligation; if None, uses page `1` for all (caller should pass real pages when available).
    """
    from qdrant_client.models import PointStruct

    from legal_rag.embeddings import embed_texts

    client = _client()
    name = _collection_name()

    ids: List[str] = []
    texts_to_embed: List[str] = []
    payloads: List[Dict[str, Any]] = []

    # Raw pages
    for page_no in sorted(page_texts.keys()):
        text = page_texts[page_no] or ""
        pid, rec = build_raw_page_point(document_id, page_no, text)
        ids.append(pid)
        texts_to_embed.append(text)
        payloads.append(payload_to_qdrant_dict(rec))

    # Obligations
    if page_for_obligation is None:
        page_for_obligation = [1] * len(consolidated_obligations)
    for i, ob in enumerate(consolidated_obligations):
        pnum = page_for_obligation[i] if i < len(page_for_obligation) else 1
        pid, rec = build_extracted_obligation_point(document_id, pnum, i, ob)
        etext = obligation_embedding_text(ob)
        ids.append(pid)
        texts_to_embed.append(etext)
        payloads.append(payload_to_qdrant_dict(rec))

    if not ids:
        return {"raw_pages": 0, "obligations": 0}

    vectors = embed_texts(texts_to_embed)
    dim = len(vectors[0])
    ensure_collection(dim)

    # PointStruct accepts int or str; str must be a valid UUID (see stable_point_id in schemas).
    points = [
        PointStruct(id=ids[i], vector=vectors[i], payload=payloads[i])
        for i in range(len(ids))
    ]
    for start in range(0, len(points), _UPSERT_BATCH):
        batch = points[start : start + _UPSERT_BATCH]
        client.upsert(collection_name=name, points=batch)
    n_raw = len(page_texts)
    n_ob = len(consolidated_obligations)
    logger.info("Qdrant upsert %s: raw_pages=%s obligations=%s", document_id, n_raw, n_ob)
    return {"raw_pages": n_raw, "obligations": n_ob}


def search(
    query_vector: List[float],
    record_type: str,
    limit: int = 8,
    document_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Semantic search with mandatory record_type filter.
    Returns list of {id, score, payload}.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = _client()
    name = _collection_name()

    must = [FieldCondition(key="record_type", match=MatchValue(value=record_type))]
    if document_id:
        must.append(FieldCondition(key="document_id", match=MatchValue(value=document_id)))

    flt = Filter(must=must)

    # qdrant-client 1.12+ uses query_points; local client may not expose legacy .search()
    resp = client.query_points(
        collection_name=name,
        query=query_vector,
        query_filter=flt,
        limit=limit,
        with_payload=True,
    )
    out = []
    for h in resp.points or []:
        out.append(
            {
                "id": str(h.id),
                "score": float(h.score) if h.score is not None else 0.0,
                "payload": h.payload or {},
            }
        )
    return out


def fetch_raw_page(document_id: str, page_number: int) -> Optional[str]:
    """Secondary query: get raw page text for parent fetch (exact document_id + page + record_type)."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = _client()
    name = _collection_name()

    flt = Filter(
        must=[
            FieldCondition(key="record_type", match=MatchValue(value=RECORD_TYPE_RAW_PAGE)),
            FieldCondition(key="document_id", match=MatchValue(value=document_id)),
            FieldCondition(key="page_number", match=MatchValue(value=page_number)),
        ]
    )
    points, _ = client.scroll(collection_name=name, scroll_filter=flt, limit=2, with_payload=True)
    if not points:
        return None
    pl = points[0].payload or {}
    return pl.get("page_text") or None


def fetch_extracted_obligations(
    document_id: Optional[str] = None,
    *,
    batch_size: int = 512,
) -> List[Dict[str, Any]]:
    """Fetch all extracted obligation payloads (optionally for a single document)."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = _client()
    name = _collection_name()

    must = [FieldCondition(key="record_type", match=MatchValue(value=RECORD_TYPE_EXTRACTED_OBLIGATION))]
    if document_id:
        must.append(FieldCondition(key="document_id", match=MatchValue(value=document_id)))

    flt = Filter(must=must)
    out: List[Dict[str, Any]] = []
    total_batches = 0
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=name,
            scroll_filter=flt,
            limit=batch_size,
            with_payload=True,
            offset=offset,
        )
        total_batches += 1
        for p in points or []:
            out.append(
                {
                    "id": str(p.id),
                    "score": 0.0,
                    "payload": p.payload or {},
                }
            )
        if next_offset is None:
            break
        offset = next_offset
    logger.info(
        "[qdrant] fetch_extracted_obligations document_id=%s batches=%s results=%s",
        document_id or "",
        total_batches,
        len(out),
    )
    return out


def fetch_document_ids(
    *,
    batch_size: int = 512,
) -> List[str]:
    """Fetch distinct document_id values from raw_page records."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = _client()
    name = _collection_name()

    flt = Filter(must=[FieldCondition(key="record_type", match=MatchValue(value=RECORD_TYPE_RAW_PAGE))])
    out: List[str] = []
    seen: set[str] = set()
    offset = None
    total_batches = 0
    while True:
        points, next_offset = client.scroll(
            collection_name=name,
            scroll_filter=flt,
            limit=batch_size,
            with_payload=True,
            offset=offset,
        )
        total_batches += 1
        for p in points or []:
            pl = p.payload or {}
            doc_id = str(pl.get("document_id") or "").strip()
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                out.append(doc_id)
        if next_offset is None:
            break
        offset = next_offset
    logger.info(
        "[qdrant] fetch_document_ids batches=%s results=%s",
        total_batches,
        len(out),
    )
    return out


def delete_document(document_id: str) -> None:
    """Remove all points for a document (both tracks)."""
    from qdrant_client.models import Filter, FieldCondition, FilterSelector, MatchValue

    client = _client()
    name = _collection_name()
    try:
        client.delete(
            collection_name=name,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="document_id",
                            match=MatchValue(value=document_id),
                        )
                    ]
                )
            ),
        )
    except Exception as e:
        logger.warning("Qdrant delete_document %s: %s", document_id, e)
