"""
Vector DB payload schemas (Qdrant-oriented; compatible with Pinecone metadata with minor renames).

Per rag_architecture.md §2: two record types per logical page — raw_page (general chat)
and extracted_obligation (UI + financial chat).
"""

from __future__ import annotations

import json
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator


# --- Constants (also used in Qdrant payload filters) ---------------------------------

RECORD_TYPE_RAW_PAGE = "raw_page"
RECORD_TYPE_EXTRACTED_OBLIGATION = "extracted_obligation"


class RecordType(str, Enum):
    raw_page = RECORD_TYPE_RAW_PAGE
    extracted_obligation = RECORD_TYPE_EXTRACTED_OBLIGATION


# --- Payload models (stored in Qdrant `payload`; vector is separate) ----------------


class RawPageRecord(BaseModel):
    """
    Track A — general chat / full page context.
    Embed: full page OCR/text. Payload must allow parent fetch lookup by (document_id, page_number).
    """

    record_type: str = Field(default=RECORD_TYPE_RAW_PAGE)
    document_id: str = Field(..., description="Stable document id (e.g. filename or UUID).")
    page_number: int = Field(..., ge=1, description="1-based page index.")
    page_text: str = Field(
        ...,
        description="Full raw text of the page (same string used for embedding).",
    )

    model_config = {"extra": "forbid"}


class ExtractedObligationRecord(BaseModel):
    """
    Track B — obligation UI + semantic search over duties.
    Embed: English summary string (obligation_embedding_text).
    Payload carries full JSON for UI and legacy keyword paths.
    """

    record_type: str = Field(default=RECORD_TYPE_EXTRACTED_OBLIGATION)
    document_id: str = Field(..., description="Same id as RawPageRecord for this document.")
    page_number: int = Field(
        ...,
        ge=1,
        description="Page where this obligation appears (for parent fetch).",
    )
    obligation_index: int = Field(
        default=0,
        ge=0,
        description="Index within consolidated_results for this document (stable ordering).",
    )
    exact_json_payload: str = Field(
        ...,
        description="Stringified JSON of the full obligation object (DutyType, Responsible Party, Citation, related_keywords, ...).",
    )

    model_config = {"extra": "forbid"}

    @field_validator("exact_json_payload")
    @classmethod
    def _must_be_json(cls, v: str) -> str:
        json.loads(v)  # validate
        return v


# --- Embedding text (spec §2.2) -----------------------------------------------------


def obligation_embedding_text(obligation: Dict[str, Any]) -> str:
    """
    Format for the vector text field (semantic matching).
    Spec: "Duty Type: [DutyType]. [Responsible Party] is responsible. Action: [Owner Responsibility]. Reasoning: [Reasoning]."
    """
    duty = obligation.get("DutyType") or ""
    if isinstance(duty, list):
        duty = " ".join(str(x) for x in duty)
    party = obligation.get("Responsible Party") or ""
    if isinstance(party, list):
        party = " ".join(str(x) for x in party)
    owner = obligation.get("Owner Responsibility")
    if isinstance(owner, list):
        owner = "; ".join(str(x).strip() for x in owner if x)
    else:
        owner = str(owner or "")
    reasoning = obligation.get("Reasoning")
    if isinstance(reasoning, list):
        reasoning = "; ".join(str(x).strip() for x in reasoning if x)
    else:
        reasoning = str(reasoning or "")
    return (
        f"Duty Type: {duty}. {party} is responsible. "
        f"Action: {owner}. Reasoning: {reasoning}."
    )


def _obligation_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(x).strip() for x in value if x is not None and str(x).strip())
    return str(value).strip()


_IMPERATIVE_LEADS = frozenset(
    {
        "pay",
        "maintain",
        "repair",
        "replace",
        "provide",
        "keep",
        "ensure",
        "carry",
        "hold",
        "obtain",
        "return",
        "give",
        "notify",
        "comply",
    }
)


def _party_action_sentence(party: str, owner_clause: str) -> str:
    """One readable sentence; avoids 'responsible for Pay rates' style duplication with short lists."""
    o = owner_clause.strip()
    if not o:
        return ""
    if not party:
        return f"The agreement calls for {o}."
    words = o.split()
    w0 = words[0].rstrip(".,;:").lower() if words else ""
    if w0 in _IMPERATIVE_LEADS:
        tail = " ".join(words[1:]).strip()
        core = f"{words[0].rstrip('.,;:').lower()}"
        if tail:
            core = f"{core} {tail}"
        return f"The {party} must {core}."
    return f"The {party} is responsible for {o}."


def obligation_prose_for_chat(obligation: Dict[str, Any]) -> str:
    """
    Plain-language summary for RAG *answer* context only.
    Avoids schema-style labels so the model is less likely to echo JSON/field names in replies.
    """
    duty = _obligation_scalar(obligation.get("DutyType"))
    party = _obligation_scalar(obligation.get("Responsible Party"))
    owner = _obligation_scalar(obligation.get("Owner Responsibility"))
    reasoning = _obligation_scalar(obligation.get("Reasoning"))
    citation = _obligation_scalar(obligation.get("Citation"))

    parts: List[str] = []
    if owner and party:
        clauses = [c.strip() for c in owner.split(";") if c.strip()]
        for c in clauses:
            parts.append(_party_action_sentence(party, c))
    elif owner:
        parts.append(f"The agreement calls for {owner}.")
    if duty:
        combined = " ".join(parts).lower()
        if duty.lower() not in combined:
            parts.append(f"This relates to {duty}.")
    if reasoning:
        parts.append(reasoning.rstrip(".") + ".")
    if citation and len(citation) < 400:
        parts.append(f"Document reference: {citation}")
    return " ".join(parts).strip()


# --- Stable point IDs (deterministic upserts) ---------------------------------------


def stable_point_id(document_id: str, record_type: str, page_number: int, suffix: str = "") -> str:
    """Deterministic UUID string (Qdrant 1.x local/server expects UUID, not arbitrary hex)."""
    raw = f"{document_id}|{record_type}|{page_number}|{suffix}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def build_raw_page_point(
    document_id: str,
    page_number: int,
    page_text: str,
) -> Tuple[str, RawPageRecord]:
    """Returns (point_id, record) for Qdrant upsert."""
    rid = stable_point_id(document_id, RECORD_TYPE_RAW_PAGE, page_number)
    return rid, RawPageRecord(
        document_id=document_id,
        page_number=page_number,
        page_text=page_text,
    )


def build_extracted_obligation_point(
    document_id: str,
    page_number: int,
    obligation_index: int,
    obligation: Dict[str, Any],
) -> Tuple[str, ExtractedObligationRecord]:
    """Returns (point_id, record) for Qdrant upsert."""
    rid = stable_point_id(
        document_id,
        RECORD_TYPE_EXTRACTED_OBLIGATION,
        page_number,
        f"ob{obligation_index}",
    )
    payload = ExtractedObligationRecord(
        document_id=document_id,
        page_number=page_number,
        obligation_index=obligation_index,
        exact_json_payload=json.dumps(obligation, ensure_ascii=False),
    )
    return rid, payload


def payload_to_qdrant_dict(record: RawPageRecord | ExtractedObligationRecord) -> Dict[str, Any]:
    """Flat dict for Qdrant payload (all values JSON-serializable)."""
    return record.model_dump()
