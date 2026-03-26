"""
Context assembly with Parent Fetch (rag_architecture.md §4).

After vector search:
- extracted_obligation hits: attach full raw page text for (document_id, page_number).
- raw_page hits: use page text from payload as-is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from legal_rag.schemas import RECORD_TYPE_EXTRACTED_OBLIGATION, RECORD_TYPE_RAW_PAGE

logger = logging.getLogger(__name__)


@dataclass
class RetrievedBlock:
    """One assembled context section for the final chat LLM."""

    document_id: str
    page_number: int
    record_type: str
    obligation_json: Optional[Dict[str, Any]] = None
    obligation_embedding_text: Optional[str] = None
    raw_page_text: Optional[str] = None
    score: Optional[float] = None


def _payload(hit: Dict[str, Any]) -> Dict[str, Any]:
    return hit.get("payload") or {}


def dedupe_search_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop duplicate Qdrant points (same id can appear if callers merge lists). Preserve score order."""
    seen: set[Any] = set()
    out: List[Dict[str, Any]] = []
    for h in hits:
        hid = h.get("id")
        if hid is not None:
            if hid in seen:
                continue
            seen.add(hid)
        out.append(h)
    return out


def assemble_chat_context(
    vector_hits: List[Dict[str, Any]],
    fetch_raw_page: Callable[[str, int], Optional[str]],
    *,
    include_obligation_summary: bool = True,
) -> tuple[str, List[RetrievedBlock]]:
    """
    Build the `assembled_context` string and structured blocks.

    :param vector_hits: Output of `legal_rag.qdrant_store.search` (id, score, payload).
    :param fetch_raw_page: Typically `legal_rag.qdrant_store.fetch_raw_page` bound or wrapper.
    :return: (assembled_context_string, blocks)

    Spec §4: For each extracted_obligation match, append raw page text below the obligation text.
    """
    vector_hits = dedupe_search_hits(vector_hits)
    blocks: List[RetrievedBlock] = []
    parts: List[str] = []
    # Include full page text only once per (document_id, page_number) to avoid repetition and
    # answers that mirror the same facts twice (summary list + "relevant obligations" list).
    parent_page_included: set[tuple[str, int]] = set()

    for i, hit in enumerate(vector_hits, start=1):
        pl = _payload(hit)
        score = hit.get("score")
        rtype = pl.get("record_type") or ""
        doc_id = pl.get("document_id") or ""
        try:
            page_num = int(pl.get("page_number") or 0)
        except (TypeError, ValueError):
            page_num = 0

        if rtype == RECORD_TYPE_RAW_PAGE:
            text = pl.get("page_text") or ""
            page_key = (doc_id, page_num)
            blocks.append(
                RetrievedBlock(
                    document_id=doc_id,
                    page_number=page_num,
                    record_type=RECORD_TYPE_RAW_PAGE,
                    raw_page_text=text,
                    score=score,
                )
            )
            if text and page_key in parent_page_included:
                parts.append(
                    f"--- Block {i} | document={doc_id} | page={page_num} | type=raw_page ---\n"
                    f"(Full page text for this document and page already appears once above.)\n"
                )
            else:
                if text:
                    parent_page_included.add(page_key)
                parts.append(
                    f"--- Block {i} | document={doc_id} | page={page_num} | type=raw_page ---\n{text}\n"
                )
            continue

        if rtype == RECORD_TYPE_EXTRACTED_OBLIGATION:
            ej = pl.get("exact_json_payload") or "{}"
            try:
                ob = json.loads(ej) if isinstance(ej, str) else ej
            except json.JSONDecodeError:
                ob = {}
            summary = ""
            prose = ""
            if include_obligation_summary and ob:
                from legal_rag.schemas import obligation_embedding_text, obligation_prose_for_chat

                summary = obligation_embedding_text(ob)
                prose = obligation_prose_for_chat(ob)

            parent = None
            if doc_id and page_num > 0:
                parent = fetch_raw_page(doc_id, page_num)
                if parent is None:
                    logger.debug("Parent raw_page not found for %s page %s", doc_id, page_num)

            blocks.append(
                RetrievedBlock(
                    document_id=doc_id,
                    page_number=page_num,
                    record_type=RECORD_TYPE_EXTRACTED_OBLIGATION,
                    obligation_json=ob if isinstance(ob, dict) else None,
                    obligation_embedding_text=summary or None,
                    raw_page_text=parent,
                    score=score,
                )
            )
            chunk = f"--- Block {i} | document={doc_id} | page={page_num} | type=extracted_obligation ---\n"
            if prose:
                chunk += f"Relevant obligation (plain language):\n{prose}\n"
            elif summary:
                chunk += f"Relevant obligation:\n{summary}\n"
            page_key = (doc_id, page_num)
            if parent:
                if page_key in parent_page_included:
                    chunk += (
                        f"\nFull page text for document={doc_id} page={page_num} "
                        f"already appears once above in this context; it is not repeated here.\n"
                    )
                else:
                    parent_page_included.add(page_key)
                    chunk += f"\nFull page text (parent context):\n{parent}\n"
            else:
                chunk += "\n(Full page text unavailable for this page.)\n"
            parts.append(chunk)
            continue

        logger.warning("Unknown record_type in hit: %s", rtype)

    assembled = "\n".join(parts).strip()
    return assembled, blocks


CHAT_SYSTEM_PROMPT = """You are an expert legal document assistant. Answer the user's question using ONLY the context provided below. The context may include short plain-language summaries of extracted obligations plus the full page text from the document.

Write for a human reader: use normal sentences or simple bullets. Do not paste JSON, YAML, or obligation schema field names (for example do not use labels like "Duty Type", "Responsible Party", "Owner Responsibility", or "Reasoning"). Do not echo internal "Block" headers. Only use structured or tabular formatting if the user explicitly asks for it.

Each duty or fact should appear once in your answer. Do not give a short list and then repeat the same items again under a second heading (for example avoid both a numbered list and a separate "relevant obligations" list that restates the same points). When you have page or section references, weave them into that single answer—for instance one bullet per duty that already includes where it appears in the document.

If the answer is not present in the context, clearly state: "I do not have enough information in the provided document to answer that." Do not invent or assume legal clauses."""


def format_chat_messages(
    user_query: str,
    assembled_context: str,
) -> List[Dict[str, str]]:
    """Messages ready for the final answer LLM."""
    return [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT + "\n\nContext:\n" + assembled_context},
        {"role": "user", "content": user_query},
    ]
