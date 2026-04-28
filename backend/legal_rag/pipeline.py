"""
End-to-end chat retrieval (router → embed → Qdrant search → parent fetch → assembled context).

Wire this to your FastAPI chat endpoint; keep legacy /query keyword path separate.
"""

from __future__ import annotations

import json
import os
import re
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from legal_rag.embeddings import embed_text
from legal_rag.qdrant_store import (
    fetch_document_ids,
    fetch_extracted_obligations,
    fetch_raw_page,
    search,
)
from legal_rag.retrieval import (
    RetrievedBlock,
    assemble_chat_context,
    assemble_page_context,
    filter_hits_relative_cutoff,
)
from legal_rag.router import RouteResult, route_chat_query
from legal_rag.schemas import RECORD_TYPE_EXTRACTED_OBLIGATION


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


_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "for",
    "in",
    "on",
    "with",
    "about",
    "all",
    "list",
    "show",
    "give",
    "me",
    "please",
    "obligation",
    "obligations",
}


def _tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    return [t for t in tokens if t and t not in _STOPWORDS]


def _obligation_keyword_text(obligation: Dict[str, Any]) -> str:
    parts = [
        str(obligation.get("DutyType") or ""),
        str(obligation.get("Responsible Party") or ""),
        str(obligation.get("Owner Responsibility") or ""),
        str(obligation.get("Reasoning") or ""),
        str(obligation.get("related_keywords") or ""),
    ]
    return " ".join(p for p in parts if p).strip()


def _bm25_filter_hits(
    hits: List[Dict[str, Any]],
    *,
    keywords: List[str],
    fallback_query: str,
) -> List[Dict[str, Any]]:
    if not hits:
        logging.info("[bm25] empty hits; skipping")
        return []
    keyword_tokens: List[str] = []
    query_tokens = set(_tokenize(fallback_query))
    strict_keywords: List[str] = []
    for kw in keywords:
        kw_tokens = _tokenize(kw)
        if not kw_tokens:
            continue
        if any(t in query_tokens for t in kw_tokens):
            strict_keywords.append(kw)
            keyword_tokens.extend(kw_tokens)
    if strict_keywords != keywords:
        logging.info(
            "[bm25] strict keyword filter before=%s after=%s",
            keywords,
            strict_keywords,
        )
    if not keyword_tokens:
        logging.info("[bm25] empty keywords; returning full set size=%s", len(hits))
        return hits

    from rank_bm25 import BM25Okapi

    corpus_tokens: List[List[str]] = []
    for hit in hits:
        payload = hit.get("payload") or {}
        ej = payload.get("exact_json_payload") or "{}"
        try:
            ob = json.loads(ej) if isinstance(ej, str) else ej
        except json.JSONDecodeError:
            ob = {}
        corpus_tokens.append(_tokenize(_obligation_keyword_text(ob)))

    bm25 = BM25Okapi(corpus_tokens)
    scores = bm25.get_scores(keyword_tokens)
    logging.info(
        "[bm25] tokens=%s corpus_size=%s",
        keyword_tokens,
        len(corpus_tokens),
    )
    scored: List[Dict[str, Any]] = []
    for hit, score in zip(hits, scores):
        if score <= 0:
            continue
        scored.append(
            {
                "id": hit.get("id"),
                "score": float(score),
                "payload": hit.get("payload") or {},
            }
        )
    if not scored:
        logging.info("[bm25] no positive scores; returning full set size=%s", len(hits))
        return hits
    scored.sort(key=lambda h: h.get("score", 0.0), reverse=True)
    logging.info(
        "[bm25] scored_hits=%s top_score=%.4f",
        len(scored),
        scored[0].get("score", 0.0),
    )
    return scored


def _count_obligation_hits(hits: List[Dict[str, Any]]) -> int:
    total = 0
    for hit in hits:
        payload = hit.get("payload") or {}
        if payload.get("record_type") == RECORD_TYPE_EXTRACTED_OBLIGATION:
            total += 1
    return total


def _response_guidance(route: RouteResult, obligation_count: int) -> str:
    if route.intent == "document_summary":
        return (
            "Response guidance: Provide a concise high-level summary of the document using only the "
            "retrieved pages. Do not mention which pages were used. Do not list obligations; "
            "keep it short and structured."
        )
    if route.summary_mode and route.record_type_filter == RECORD_TYPE_EXTRACTED_OBLIGATION:
        if obligation_count > 30:
            return (
                "Response guidance: There are more than 30 relevant obligations. "
                "You MUST state the approximate count and ask the user to narrow by topic "
                "(e.g., rent, maintenance, insurance, repairs, notices) or specify a clause. "
                "Do NOT list obligations."
            )
        if obligation_count >= 9:
            return (
                "Response guidance: There are 9–30 relevant obligations. "
                "You MUST summarize all obligations as themes (do not list each obligation). "
                "Group by category and give a short overview with citations where possible."
            )
        return (
            "Response guidance: There are 8 or fewer relevant obligations. "
            "You MUST list all obligations with clear citations. Keep it concise."
        )
    return ""


def run_chat_retrieval(
    user_message: str,
    *,
    top_k: int = 8,
    document_id: Optional[str] = None,
    router_model: Optional[str] = None,
) -> Tuple[
    RouteResult,
    str,
    List[RetrievedBlock],
    List[Dict[str, Any]],
    Dict[str, Any],
]:
    """
    1) LLM router picks record_type filter.
    2) Embed user message.
    3) Qdrant search with filter.
    4) Parent-fetch raw pages for obligation hits.

    Returns: route, assembled_context, blocks, raw_hits
    """
    route = route_chat_query(user_message, model=router_model)
    logging.info(
        "[chat_retrieval] route record_type=%s intent=%s summary_mode=%s",
        route.record_type_filter,
        route.intent,
        route.summary_mode,
    )
    if route.intent == "document_summary":
        doc_ids = [document_id] if document_id else fetch_document_ids()
        logging.info(
            "[chat_retrieval] document_summary mode document_ids=%s",
            doc_ids,
        )
        all_blocks: List[RetrievedBlock] = []
        all_parts: List[str] = []
        block_idx = 1
        for doc_id in doc_ids:
            page_texts: Dict[int, str] = {}
            for page_num in range(1, 4):
                page_text = fetch_raw_page(doc_id, page_num)
                if page_text:
                    page_texts[page_num] = page_text
            logging.info(
                "[chat_retrieval] document_summary document_id=%s pages_loaded=%s",
                doc_id,
                sorted(page_texts.keys()),
            )
            if not page_texts:
                continue
            assembled, blocks = assemble_page_context(doc_id, page_texts)
            for b in blocks:
                all_blocks.append(b)
            if assembled:
                all_parts.append(f"--- Document {block_idx} | document={doc_id} ---\n{assembled}\n")
                block_idx += 1
        combined = "\n".join(all_parts).strip()
        meta = {"relevant_obligation_count": 0, "filtered_hit_count": 0, "used_hit_count": len(all_blocks)}
        return route, combined, all_blocks, [], meta

    if route.intent == "list_all_obligations" and route.summary_mode:
        logging.info(
            "[chat_retrieval] list_all_obligations using BM25 keywords=%s document_id=%s",
            route.keywords,
            document_id or "",
        )
        all_hits = fetch_extracted_obligations(document_id=document_id)
        filtered_hits = _bm25_filter_hits(
            all_hits,
            keywords=route.keywords,
            fallback_query=user_message,
        )
        include_parent_pages = False
        obligation_count = _count_obligation_hits(filtered_hits)
        hits_for_context = filtered_hits
        if obligation_count > 30:
            hits_for_context = filtered_hits[:8]
        logging.info(
            "[chat_retrieval] bm25 hits=%s filtered=%s obligation_count=%s hits_for_context=%s",
            len(all_hits),
            len(filtered_hits),
            obligation_count,
            len(hits_for_context),
        )
        assembled, blocks = assemble_chat_context(
            hits_for_context,
            fetch_raw_page,
            include_parent_pages=include_parent_pages,
        )
        meta = {
            "relevant_obligation_count": obligation_count,
            "filtered_hit_count": len(filtered_hits),
            "used_hit_count": len(hits_for_context),
            "bm25_used": True,
        }
        return route, assembled, blocks, all_hits, meta

    qvec = embed_text(user_message)
    requested_limit = 200 if route.summary_mode else top_k
    logging.info(
        "[chat_retrieval] vector_search record_type=%s top_k=%s summary_mode=%s document_id=%s",
        route.record_type_filter,
        requested_limit,
        route.summary_mode,
        document_id or "",
    )
    hits = search(
        qvec,
        record_type=route.record_type_filter,
        limit=requested_limit,
        document_id=document_id,
    )
    logging.info("[chat_retrieval] vector_search hits=%s", len(hits))

    filtered_hits = hits
    include_parent_pages = True
    if route.summary_mode and route.record_type_filter == RECORD_TYPE_EXTRACTED_OBLIGATION:
        ratio_raw = os.getenv("RAG_RELATIVE_SCORE_RATIO", "0.85").strip()
        try:
            ratio = float(ratio_raw) if ratio_raw else 0.85
        except ValueError:
            ratio = 0.85
        logging.info("[chat_retrieval] relative_cutoff ratio=%s", ratio)
        filtered_hits = filter_hits_relative_cutoff(
            hits,
            score_ratio=ratio,
            record_type=RECORD_TYPE_EXTRACTED_OBLIGATION,
        )
        include_parent_pages = False
        logging.info(
            "[chat_retrieval] relative_cutoff filtered_hits=%s",
            len(filtered_hits),
        )

    obligation_count = _count_obligation_hits(filtered_hits)
    hits_for_context = filtered_hits
    if route.summary_mode and obligation_count > 30:
        hits_for_context = filtered_hits[:8]
    logging.info(
        "[chat_retrieval] obligation_count=%s hits_for_context=%s include_parent_pages=%s",
        obligation_count,
        len(hits_for_context),
        include_parent_pages,
    )

    assembled, blocks = assemble_chat_context(
        hits_for_context,
        fetch_raw_page,
        include_parent_pages=include_parent_pages,
    )
    meta = {
        "relevant_obligation_count": obligation_count,
        "filtered_hit_count": len(filtered_hits),
        "used_hit_count": len(hits_for_context),
    }
    return route, assembled, blocks, hits, meta


async def run_chat_turn(
    user_message: str,
    *,
    document_id: Optional[str] = None,
    top_k: int = 8,
    answer_model: Optional[str] = None,
    router_model: Optional[str] = None,
    session_context: str = "",
    user_background: str = "",
) -> Dict[str, Any]:
    """
    Full chat: retrieval (router → Qdrant → context) then answer LLM.

    Uses the same ``llm_client.generate_content_async`` stack as the rest of the project.
    """
    from llm_client import generate_content_async, get_default_model

    from legal_rag.retrieval import CHAT_SYSTEM_PROMPT

    t0 = time.perf_counter()
    route, assembled, blocks, hits, meta = run_chat_retrieval(
        user_message,
        top_k=top_k,
        document_id=document_id,
        router_model=router_model,
    )
    t_retrieval = time.perf_counter() - t0

    context_was_empty = not (assembled or "").strip()
    if context_was_empty:
        assembled = (
            "(No matching passages were retrieved. Index the document into Qdrant: "
            "set RAG_INDEX_QDRANT=true when processing PDFs, or call "
            "legal_rag.qdrant_store.upsert_document_pages_and_obligations.)"
        )

    response_guidance = _response_guidance(route, meta.get("relevant_obligation_count", 0))
    logging.info(
        "[chat_retrieval] response_guidance applied=%s",
        bool(response_guidance),
    )
    guidance_block = f"{response_guidance}\n\n" if response_guidance else ""
    prompt = (
        f"{CHAT_SYSTEM_PROMPT}\n\n"
        f"{guidance_block}"
        f"---\nSession Context:\n{session_context}\n---\n"
        f"User Background:\n{user_background}\n---\n"
        f"Retrieved Legal Context:\n{assembled}\n---\n\n"
        f"User question:\n{user_message}"
    )

    model = answer_model or get_default_model()
    t_llm = time.perf_counter()
    resp = await generate_content_async(
        prompt,
        model=model,
        temperature=0.2,
        response_mime_type="text/plain",
        max_output_tokens=int(os.getenv("RAG_CHAT_MAX_OUTPUT_TOKENS", "4096")),
    )
    t_llm_elapsed = time.perf_counter() - t_llm
    logging.info(
        "[chat_timing] retrieval=%.3fs, llm=%.3fs, hits=%s, blocks=%s",
        t_retrieval,
        t_llm_elapsed,
        len(hits),
        len(blocks),
    )

    return {
        "answer": _format_chat_answer(resp.text or ""),
        "route": {
            "record_type_filter": route.record_type_filter,
            "tool_name": route.tool_name,
            "rationale": route.rationale,
            "intent": route.intent,
            "summary_mode": route.summary_mode,
        },
        "hit_count": len(hits),
        "block_count": len(blocks),
        "relevant_obligation_count": meta.get("relevant_obligation_count", 0),
        "context_was_empty": context_was_empty,
    }
