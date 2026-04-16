"""
End-to-end chat retrieval (router → embed → Qdrant search → parent fetch → assembled context).

Wire this to your FastAPI chat endpoint; keep legacy /query keyword path separate.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from legal_rag.embeddings import embed_text
from legal_rag.qdrant_store import fetch_raw_page, search
from legal_rag.retrieval import RetrievedBlock, assemble_chat_context
from legal_rag.router import RouteResult, route_chat_query


def _format_chat_answer(text: str) -> str:
    """Normalize line endings; keep paragraph/list structure for readable multi-line answers."""
    s = (text or "").strip()
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Strip accidental JSON-style escapes if the model emitted them literally
    s = s.replace('\\"', '"')
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s.strip()


def run_chat_retrieval(
    user_message: str,
    *,
    top_k: int = 8,
    document_id: Optional[str] = None,
    router_model: Optional[str] = None,
) -> Tuple[RouteResult, str, List[RetrievedBlock], List[Dict[str, Any]]]:
    """
    1) LLM router picks record_type filter.
    2) Embed user message.
    3) Qdrant search with filter.
    4) Parent-fetch raw pages for obligation hits.

    Returns: route, assembled_context, blocks, raw_hits
    """
    route = route_chat_query(user_message, model=router_model)
    qvec = embed_text(user_message)
    hits = search(
        qvec,
        record_type=route.record_type_filter,
        limit=top_k,
        document_id=document_id,
    )
    assembled, blocks = assemble_chat_context(hits, fetch_raw_page)
    return route, assembled, blocks, hits


async def run_chat_turn(
    user_message: str,
    *,
    document_id: Optional[str] = None,
    top_k: int = 8,
    answer_model: Optional[str] = None,
    router_model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full chat: retrieval (router → Qdrant → context) then answer LLM.

    Uses the same ``llm_client.generate_content_async`` stack as the rest of the project.
    """
    from llm_client import generate_content_async, get_default_model

    from legal_rag.retrieval import CHAT_SYSTEM_PROMPT

    route, assembled, blocks, hits = run_chat_retrieval(
        user_message,
        top_k=top_k,
        document_id=document_id,
        router_model=router_model,
    )

    context_was_empty = not (assembled or "").strip()
    if context_was_empty:
        assembled = (
            "(No matching passages were retrieved. Index the document into Qdrant: "
            "set RAG_INDEX_QDRANT=true when processing PDFs, or call "
            "legal_rag.qdrant_store.upsert_document_pages_and_obligations.)"
        )

    prompt = (
        f"{CHAT_SYSTEM_PROMPT}\n\n---\nContext:\n{assembled}\n---\n\n"
        f"User question:\n{user_message}"
    )

    model = answer_model or get_default_model()
    resp = await generate_content_async(
        prompt,
        model=model,
        temperature=0.2,
        response_mime_type="text/plain",
        max_output_tokens=int(os.getenv("RAG_CHAT_MAX_OUTPUT_TOKENS", "4096")),
    )

    return {
        "answer": _format_chat_answer(resp.text or ""),
        "route": {
            "record_type_filter": route.record_type_filter,
            "tool_name": route.tool_name,
            "rationale": route.rationale,
        },
        "hit_count": len(hits),
        "block_count": len(blocks),
        "context_was_empty": context_was_empty,
    }
