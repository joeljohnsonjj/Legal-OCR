"""
Dual-track Legal RAG (Qdrant + LLM router + parent fetch).

See rag_architecture.md. Legacy Chroma keyword path in vector_store.py is unchanged.
"""

from .schemas import (
    RECORD_TYPE_RAW_PAGE,
    RECORD_TYPE_EXTRACTED_OBLIGATION,
    RecordType,
    RawPageRecord,
    ExtractedObligationRecord,
    obligation_embedding_text,
    build_raw_page_point,
    build_extracted_obligation_point,
)
from .router import route_chat_query, RAGRouteDecision, RouteResult
from .retrieval import assemble_chat_context, RetrievedBlock, format_chat_messages
from .qdrant_store import (
    upsert_document_pages_and_obligations,
    search,
    fetch_raw_page,
    delete_document,
)
from .pipeline import run_chat_retrieval, run_chat_turn

__all__ = [
    "RECORD_TYPE_RAW_PAGE",
    "RECORD_TYPE_EXTRACTED_OBLIGATION",
    "RecordType",
    "RawPageRecord",
    "ExtractedObligationRecord",
    "obligation_embedding_text",
    "build_raw_page_point",
    "build_extracted_obligation_point",
    "route_chat_query",
    "RAGRouteDecision",
    "RouteResult",
    "assemble_chat_context",
    "format_chat_messages",
    "RetrievedBlock",
    "upsert_document_pages_and_obligations",
    "search",
    "fetch_raw_page",
    "delete_document",
    "run_chat_retrieval",
    "run_chat_turn",
]
