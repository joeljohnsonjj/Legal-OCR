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
from typing import Any, Callable, Dict, List, Optional, Sequence

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


def filter_hits_relative_cutoff(
    vector_hits: Sequence[Dict[str, Any]],
    *,
    score_ratio: float,
    record_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filter hits to those within a relative ratio of the best score."""
    if not vector_hits:
        return []
    scores = [float(h.get("score") or 0.0) for h in vector_hits]
    best = max(scores) if scores else 0.0
    if best <= 0:
        return list(vector_hits)
    cutoff = best * score_ratio
    filtered: List[Dict[str, Any]] = []
    for h in vector_hits:
        if record_type and _payload(h).get("record_type") != record_type:
            filtered.append(h)
            continue
        score = float(h.get("score") or 0.0)
        if score >= cutoff:
            filtered.append(h)
    return filtered


def assemble_page_context(
    document_id: str,
    page_texts: Dict[int, str],
) -> tuple[str, List[RetrievedBlock]]:
    """Assemble a context string for fixed raw pages (e.g., document summary)."""
    blocks: List[RetrievedBlock] = []
    parts: List[str] = []
    for idx, page_num in enumerate(sorted(page_texts.keys()), start=1):
        text = page_texts[page_num] or ""
        blocks.append(
            RetrievedBlock(
                document_id=document_id,
                page_number=page_num,
                record_type=RECORD_TYPE_RAW_PAGE,
                raw_page_text=text,
            )
        )
        parts.append(
            f"--- Block {idx} | document={document_id} | page={page_num} | type=raw_page ---\n{text}\n"
        )
    assembled = "\n".join(parts).strip()
    return assembled, blocks


def assemble_chat_context(
    vector_hits: List[Dict[str, Any]],
    fetch_raw_page: Callable[[str, int], Optional[str]],
    *,
    include_obligation_summary: bool = True,
    include_parent_pages: bool = True,
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
            if include_parent_pages and doc_id and page_num > 0:
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
            if include_parent_pages:
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


CHAT_SYSTEM_PROMPT ="""You are a Legal Document Assistant specializing in analyzing commercial lease agreements 
and other legal contracts. You answer questions strictly based on retrieved content from 
the indexed documents — you do not fabricate obligations, clauses, or citations.

────────────────────────────────────────
WHAT YOU HAVE ACCESS TO
────────────────────────────────────────
The retrieval system gives you one of two types of context blocks:

1. OBLIGATION BLOCK — A structured duty extracted from the document:
   • DutyType, Responsible Party, Key Obligations, Reasoning, Citation (page + section)
   • A surrounding page excerpt is also provided for full context.
   Use these when the user asks "who is responsible for X?" or "what are the obligations for Y?"

2. PAGE BLOCK — Raw text from a specific page of a legal document.
   Use these for general document questions, definitions, or when the user 
   asks about a clause, term, or section that is not an obligation.

────────────────────────────────────────
HOW TO RESPOND
────────────────────────────────────────
• Ground every answer in the retrieved context. Quote or paraphrase directly.
• Maintain awareness of the full conversation history. For follow-up questions, determine whether
  they relate to prior queries and incorporate relevant details to keep the answer coherent and
  consistent. Only treat a follow-up as isolated when it is explicitly unrelated.
• Always cite your source: document name, page number, and section if available.
  Example: "Per Section 7(a), Page 4 of [Document Name] — the Tenant is required to..."
• Always include relevant citations or references at the end of each response in a consistent format
  that clearly supports the information provided.
• If multiple documents are retrieved, clearly distinguish which obligation 
  comes from which document.
• If the retrieved context partially answers the question, answer what you can 
  and clearly state what was not found.
• Use plain, precise English — avoid unnecessary legal jargon unless quoting directly.
• Default to a crisp, short answer (1–4 sentences) that directly addresses the user’s question.
• If a "Response guidance" instruction appears above the context, follow it exactly.
• For document summaries, do not mention which pages were used. If page counts or document length
  are explicitly present in the retrieved context, you may mention them; otherwise do not invent
  metadata.
• If the user explicitly asks for details, give a longer, structured answer.
• For obligation questions, include the essentials:
   - Party responsible
   - What they must do
   - Citation

────────────────────────────────────────
OUTPUT FORMAT (PLAIN TEXT)
────────────────────────────────────────
• Do not use emojis, icons, or decorative Unicode symbols (no bullets like diamonds or warning signs).
• Respond directly in plain sentences without a title line or heading.
• Keep the answer focused on the user's question.
• Put a blank line between major paragraphs.
• End every response with a single "Citations:" line listing document name, page number, and section
  (if available) in a consistent, comma-separated format. If multiple sources are used, separate
  them with semicolons.
• When listing numbered items (1. 2. 3.), start each main item on a new line. Under each item, use aligned sub-lines with clear labels, for example:
  Responsibility: ...
  Cost flow: ...
  Citation: Page X, Section Y (and document name if multiple documents appear in context)
• For nested points under a number, use a hyphen at the start of each sub-line on its own line, indented consistently with two spaces after the newline.
• For short phrases quoted from the lease, prefer single quotes (e.g. 'free and clear') instead of double quotes, so the text stays readable when returned in JSON.

────────────────────────────────────────
WHEN NOTHING IS RETRIEVED
────────────────────────────────────────
If the context is empty or clearly irrelevant, respond with:
"I could not find relevant information in the indexed documents for your question. 
This may mean the document hasn't been indexed yet, or the topic falls outside 
the indexed content. Please ensure the document is processed via the ingestion pipeline."

Do NOT attempt to answer from general legal knowledge in this case.

────────────────────────────────────────
BOUNDARIES
────────────────────────────────────────
• Do NOT provide legal advice or interpret obligations beyond what is written.
• Do NOT answer questions unrelated to the indexed legal documents.
• Do NOT make up page numbers, section references, or party names.
• If asked to compare two documents, only compare obligations that were actually retrieved.


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
