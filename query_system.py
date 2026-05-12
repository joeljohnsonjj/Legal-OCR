"""
Interactive Query System for Legal Document Obligations
Searches through consolidated JSON files and returns relevant obligations based on user queries
"""

import asyncio
import copy
import os
import sys
import json
import logging
import re
import time
import importlib.util
from pathlib import Path
from typing import AsyncIterator, List, Dict, Any, Optional, Set, Tuple
from datetime import datetime
from urllib.parse import unquote, urlparse
from collections import OrderedDict


# Increase recursion limit to handle deep nested module calls in transformers/torch
# This prevents RecursionError during embedding initialization
sys.setrecursionlimit(10000)

# LLM API (Azure OpenAI or Gemini via llm_client)
from llm_client import (
    generate_content as llm_generate_content,
    generate_content_stream,
    get_default_model,
    use_bedrock_llm,
)

# FastAPI
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict

# Environment Variables
from dotenv import load_dotenv

from citation_utils import merge_structured_citations_by_doc_id
from processing_results import (
    count_obligations_in_results,
    normalize_party_fields_in_groups,
    obligations_from_consolidated_json,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

from gcs_document_versioning import (
    default_archive_prefix,
    list_archived_versions,
    maybe_sync_live_bytes_to_docs,
    maybe_sync_live_from_bucket_to_docs,
    publish_new_version,
    publish_result_to_dict,
    restore_archived_to_live,
    restore_version_by_id,
)


# Post-merge: keep only Owner Responsibility lines that match the user's topic (substring + light stemming).
_QUERY_SCOPE_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "as",
        "by",
        "with",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "this",
        "that",
        "these",
        "those",
        "any",
        "all",
        "each",
        "such",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "about",
        "under",
        "over",
        "into",
        "onto",
        "per",
        "via",
        "than",
        "then",
        "not",
        "no",
        "also",
        "only",
        "just",
        "both",
        "either",
        "some",
        "including",
        "related",
        "other",
        "etc",
    }
)
# Broad multi-keyword queries (e.g. default utilities string): skip automated line filtering.
_QUERY_SCOPE_MAX_SIGNIFICANT_TERMS = 9


def _party_bucket_for_retrieval(party: str) -> str:
    """Normalize party label for diversification (broad landlord vs tenant split)."""
    p = (party or "").strip().lower()
    if "landlord" in p or "lessor" in p or "owner" in p:
        return "landlord"
    if "tenant" in p or "lessee" in p:
        return "tenant"
    return p or "_other"


def _vector_obligation_doc_key(ob: Dict[str, Any]) -> str:
    """Stable per-document key so two leases with the same party/first-line duty are not deduped."""
    dn = (ob.get("document_name") or "").strip().lower()
    if dn:
        return dn
    c = str(ob.get("Citation") or "")
    if "Document:" in c:
        return c.split("Document:")[1].split("|")[0].strip().lower()
    return ""


def _vector_obligation_dedupe_sig(ob: Dict[str, Any]) -> Tuple[str, str, str, str]:
    doc = _vector_obligation_doc_key(ob)
    cit = str(ob.get("Citation") or "")[:140]
    party = (ob.get("Responsible Party") or "")[:80].lower().strip()
    o0 = ""
    or_list = ob.get("Owner Responsibility")
    if isinstance(or_list, list) and or_list:
        o0 = str(or_list[0])[:120].lower().strip()
    elif isinstance(or_list, str):
        o0 = or_list[:120].lower().strip()
    return (doc, party, o0, cit)


def diversify_vector_obligations_by_party(
    scored: List[Tuple[float, Dict[str, Any]]],
    max_total: int,
) -> List[Dict[str, Any]]:
    """
    When the query does not force a single party, avoid returning only the single party that
    happens to dominate embedding distance for topic queries (e.g. utilities).
    """
    if max_total <= 0 or not scored:
        return []
    scored = sorted(scored, key=lambda x: x[0])
    parties_present = {_party_bucket_for_retrieval(ob.get("Responsible Party") or "") for _, ob in scored}
    parties_present.discard("")
    if len(parties_present) <= 1:
        seen: Set[Tuple[str, str, str]] = set()
        out: List[Dict[str, Any]] = []
        for _, ob in scored:
            sig = _vector_obligation_dedupe_sig(ob)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(ob)
            if len(out) >= max_total:
                break
        return out

    per_party_cap = max(6, max_total // max(3, len(parties_present)))
    counts: Dict[str, int] = {p: 0 for p in parties_present}
    seen: Set[Tuple[str, str, str]] = set()
    out: List[Dict[str, Any]] = []

    for dist, ob in scored:
        if len(out) >= max_total:
            break
        sig = _vector_obligation_dedupe_sig(ob)
        if sig in seen:
            continue
        bucket = _party_bucket_for_retrieval(ob.get("Responsible Party") or "")
        if counts.get(bucket, 0) >= per_party_cap:
            continue
        seen.add(sig)
        out.append(ob)
        counts[bucket] = counts.get(bucket, 0) + 1

    for dist, ob in scored:
        if len(out) >= max_total:
            break
        sig = _vector_obligation_dedupe_sig(ob)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(ob)
    return out


def diversify_vector_obligations_for_merge_input(
    scored: List[Tuple[float, Dict[str, Any]]],
    max_total: int,
) -> List[Dict[str, Any]]:
    """
    Balance **across documents** so one indexed lease cannot consume every Chroma slot, then apply
    existing party balancing. Preserves duplicate-distance ordering via stable sort when handing off.
    """
    if max_total <= 0 or not scored:
        return []
    scored = sorted(scored, key=lambda x: x[0])
    by_doc: "OrderedDict[str, List[Tuple[float, Dict[str, Any]]]]" = OrderedDict()
    for item in scored:
        dk = _vector_obligation_doc_key(item[1]) or "_unknown"
        by_doc.setdefault(dk, []).append(item)

    if len(by_doc) <= 1:
        return diversify_vector_obligations_by_party(scored, max_total)

    seen: Set[Tuple[str, str, str, str]] = set()
    interleaved: List[Dict[str, Any]] = []
    indices: Dict[str, int] = {k: 0 for k in by_doc.keys()}
    doc_rotation = list(by_doc.keys())

    while len(interleaved) < max_total:
        progressed = False
        for dk in doc_rotation:
            if len(interleaved) >= max_total:
                break
            pool = by_doc[dk]
            i = indices[dk]
            while i < len(pool):
                dist, ob = pool[i]
                sig = _vector_obligation_dedupe_sig(ob)
                i += 1
                indices[dk] = i
                if sig in seen:
                    continue
                seen.add(sig)
                interleaved.append(ob)
                progressed = True
                break
        if not progressed:
            break

    if len(interleaved) < max_total:
        for dist, ob in scored:
            if len(interleaved) >= max_total:
                break
            sig = _vector_obligation_dedupe_sig(ob)
            if sig in seen:
                continue
            seen.add(sig)
            interleaved.append(ob)

    paired = [(0.0, ob) for ob in interleaved]
    return diversify_vector_obligations_by_party(paired, max_total)


def _normalize_reasoning_dedupe_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def sanitize_reasoning_duplication_across_obligations(results: List[Dict[str, Any]]) -> None:
    """
    In-place: remove duplicate Reasoning strings within each obligation only.

    Cross-obligation global dedupe was removed: many real duties (e.g. same lease section) share
    identical reasoning text; capping duplicates globally replaced them with
    "Not specified in document".
    """
    if not isinstance(results, list):
        return
    for grp in results:
        if not isinstance(grp, dict):
            continue
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            raw_rs = _str_list_field(ob.get("Reasoning"))
            local_seen: Set[str] = set()
            kept: List[str] = []
            for r in raw_rs:
                k = _normalize_reasoning_dedupe_key(r)
                if not k:
                    continue
                if k in local_seen:
                    continue
                local_seen.add(k)
                kept.append(r)
            if not kept:
                kept = ["Not specified in document"]
            ob["Reasoning"] = kept


def _significant_query_terms_for_scope(user_query: str) -> List[str]:
    q = (user_query or "").lower()
    words = re.findall(r"[a-z][a-z0-9'-]*", q)
    out: List[str] = []
    for w in words:
        w = w.strip("'")
        if len(w) < 3 or w in _QUERY_SCOPE_STOPWORDS:
            continue
        out.append(w)
    return out


def _match_tokens_for_query_scope(terms: List[str]) -> List[str]:
    """Query tokens only — no hardcoded synonym tables (synonyms live in related_keywords on each obligation)."""
    return sorted({t for t in terms if t}, key=len, reverse=True)


def _line_matches_query_scope(line: str, match_tokens: List[str]) -> bool:
    low = line.lower()
    for tok in match_tokens:
        if len(tok) < 3:
            continue
        if tok in low:
            return True
        if len(tok) >= 5 and tok.endswith("ing"):
            root = tok[:-3]
            if len(root) >= 4 and root in low:
                return True
    return False


def _obligation_topic_match_blob(ob: Dict[str, Any], category_label: str = "") -> str:
    """Lowercased text blob for matching user queries to an obligation (incl. related_keywords)."""
    parts: List[str] = []
    for x in (category_label, ob.get("category"), ob.get("_processing_category")):
        if x is not None and str(x).strip():
            parts.append(str(x).strip())
    for key in ("DutyType", "Owner Responsibility", "Reasoning"):
        v = ob.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x is not None)
        elif v is not None:
            parts.append(str(v))
    rk = ob.get("related_keywords")
    if isinstance(rk, list):
        parts.extend(str(x).strip() for x in rk if x is not None and str(x).strip())
    return " ".join(parts).lower()


def _query_matches_obligation_topic(
    user_query: str, ob: Dict[str, Any], category_label: str = ""
) -> bool:
    """
    True if the query aligns with this obligation's category, body text, or related_keywords.
    Used to avoid stripping vector/merge rows when the user uses wording that does not appear
    verbatim in Owner Responsibility but does appear in related_keywords or the taxonomy label.
    """
    terms = _significant_query_terms_for_scope(user_query)
    if not terms or len(terms) > _QUERY_SCOPE_MAX_SIGNIFICANT_TERMS:
        return True
    uq_low = (user_query or "").lower()
    blob = _obligation_topic_match_blob(ob, category_label)
    match_tokens = _match_tokens_for_query_scope(terms)
    if _line_matches_query_scope(blob, match_tokens):
        return True
    rk = ob.get("related_keywords")
    if isinstance(rk, list):
        for kw in rk:
            kl = str(kw).lower().strip()
            if len(kl) < 3:
                continue
            if kl in uq_low:
                return True
            if any(len(t) >= 3 and t in kl for t in terms):
                return True
    return False


def _split_owner_responsibility_clauses(s: str) -> List[str]:
    s = (s or "").strip()
    if not s:
        return []
    parts = re.split(r"\s*;\s*|\n+", s)
    return [p.strip() for p in parts if p.strip()]


def _join_owner_clauses(parts: List[str]) -> str:
    return "; ".join(parts)


def trim_obligation_owner_responsibility_to_query(user_query: str, ob: Dict[str, Any]) -> bool:
    """
    Restrict Owner Responsibility to clauses that match the query topic (substring match on
    responsibility text, or keep all clauses when category / related_keywords match the query).
    Returns False if nothing remains (caller should drop the obligation from API results).
    """
    if _query_matches_obligation_topic(user_query, ob, str(ob.get("category") or "")):
        return True
    terms = _significant_query_terms_for_scope(user_query)
    if not terms or len(terms) > _QUERY_SCOPE_MAX_SIGNIFICANT_TERMS:
        return True
    match_set = _match_tokens_for_query_scope(terms)
    or_field = ob.get("Owner Responsibility")
    kept: List[str] = []
    if isinstance(or_field, list):
        for item in or_field:
            s = str(item).strip()
            if s and _line_matches_query_scope(s, match_set):
                kept.append(str(item))
    elif isinstance(or_field, str) and or_field.strip():
        clauses = _split_owner_responsibility_clauses(or_field)
        if len(clauses) <= 1:
            if _line_matches_query_scope(or_field, match_set):
                kept = [or_field.strip()]
        else:
            for c in clauses:
                if _line_matches_query_scope(c, match_set):
                    kept.append(c)
    if not kept:
        return False
    if isinstance(or_field, list):
        ob["Owner Responsibility"] = kept
    else:
        ob["Owner Responsibility"] = _join_owner_clauses(kept) if len(kept) > 1 else kept[0]
    return True


def is_nested_category_query_results(results: Any) -> bool:
    """True when results are grouped as [{category, obligations}, ...]."""
    if not isinstance(results, list) or not results:
        return False
    first = results[0]
    return isinstance(first, dict) and "obligations" in first


def apply_query_scope_trim_to_results(user_query: str, payload: Dict[str, Any]) -> None:
    """
    Legacy flat ``results``: trim ``Owner Responsibility`` per obligation and drop empty rows.

    Nested ``[{category, obligations}]`` payloads are left unchanged — query-scoped duty/reasoning
    lines are produced only by the merge/rank LLM, not by Python post-processing.
    """
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return
    if is_nested_category_query_results(results):
        return
    kept: List[Dict[str, Any]] = []
    for ob in results:
        if not isinstance(ob, dict):
            continue
        if trim_obligation_owner_responsibility_to_query(user_query, ob):
            kept.append(ob)
    payload["results"] = kept
    payload["total_obligations_found"] = len(kept)


# Full-query coherence: specific topic words must appear in sources and in final output (not only generic lease terms).
_GENERIC_LEASE_QUERY_TERMS = frozenset(
    {
        "tenant",
        "tenants",
        "landlord",
        "landlords",
        "lessee",
        "lessor",
        "rent",
        "lease",
        "obligation",
        "obligations",
        "duty",
        "duties",
        "payment",
        "payments",
        "pay",
        "paid",
        "paying",
        "additional",
        "supplemental",
        "extra",
        "fees",
        "fee",
        "charges",
        "charge",
        "cost",
        "costs",
        "financial",
        "monetary",
        "money",
        "party",
        "parties",
        "responsible",
        "responsibility",
        "agreement",
        "contract",
        "premises",
        "building",
        "property",
        "due",
        "payable",
        "amount",
        "amounts",
        "monthly",
        "annual",
        "base",
        "triple",
        "nnn",
        "net",
        "gross",
        "commercial",
        "total",
        "sum",
        "sums",
    }
)


def _content_terms_for_coherence(user_query: str) -> List[str]:
    """Words that must be reflected in the corpus/output; excludes stopwords and generic lease phrasing."""
    sig = _significant_query_terms_for_scope(user_query)
    if not sig or len(sig) > _QUERY_SCOPE_MAX_SIGNIFICANT_TERMS:
        return []
    return [t for t in sig if t not in _GENERIC_LEASE_QUERY_TERMS]


def _collect_obligation_text_for_coherence(ob: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("DutyType", "_processing_category", "Owner Responsibility", "Reasoning"):
        v = ob.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x is not None)
        elif v is not None:
            parts.append(str(v))
    rk = ob.get("related_keywords")
    if isinstance(rk, list):
        parts.extend(str(x) for x in rk if x is not None)
    return " ".join(parts).lower()


def _sources_blob_from_merge_input(merge_input: List[Dict[str, Any]]) -> str:
    chunks: List[str] = []
    for fr in merge_input:
        if not isinstance(fr, dict):
            continue
        if isinstance(fr.get("results"), list) and fr["results"]:
            for grp in fr["results"]:
                if not isinstance(grp, dict):
                    continue
                chunks.append(str(grp.get("category") or "").lower())
                for ob in grp.get("obligations") or []:
                    if isinstance(ob, dict):
                        chunks.append(_collect_obligation_text_coherence_inner(ob))
                        chunks.append(_collect_obligation_text_for_coherence(ob))
            continue
        for ob in fr.get("consolidated_results") or []:
            if isinstance(ob, dict):
                chunks.append(_collect_obligation_text_for_coherence(ob))
    return " ".join(chunks)


def _coherence_subphrase_in_blob(sub: str, blob: str) -> bool:
    """Whether a single topic sub-phrase (already lowercased) matches blob text."""
    blob = blob or ""
    if not sub:
        return False
    t = sub.lower().strip()
    if len(t) <= 4:
        return re.search(rf"\b{re.escape(t)}\b", blob) is not None
    if t in blob:
        return True
    if len(t) >= 5 and t.endswith("ing"):
        root = t[:-3]
        if len(root) >= 4 and root in blob:
            return True
    return False


def _term_in_coherence_blob(term: str, blob: str) -> bool:
    """Whether term appears in blob (substring / light stemming only — no synonym table)."""
    tl = (term or "").lower().strip()
    if not tl:
        return False
    return _coherence_subphrase_in_blob(tl, blob or "")


def coherence_query_unsupported_by_sources(user_query: str, merge_input: List[Dict[str, Any]]) -> bool:
    """
    Always False: do not skip merge based on literal token presence in the corpus.
    Retrieval and related_keywords already scope candidates; literal checks caused false-empty
    answers for synonyms and paraphrases (e.g. HVAC vs climate control).
    """
    return False


def _collect_obligation_text_coherence_inner(ob: Dict[str, Any]) -> str:
    """Text from a grouped obligation row (no DutyType)."""
    parts: List[str] = []
    for key in ("Responsible Party", "Owner Responsibility", "Reasoning"):
        v = ob.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x is not None)
        elif v is not None:
            parts.append(str(v))
    rk = ob.get("related_keywords")
    if isinstance(rk, list):
        parts.extend(str(x) for x in rk if x is not None)
    return " ".join(parts).lower()


def _collect_nested_results_text_for_coherence(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    for grp in payload.get("results") or []:
        if not isinstance(grp, dict):
            continue
        parts.append(str(grp.get("category") or "").lower())
        for ob in grp.get("obligations") or []:
            if isinstance(ob, dict):
                parts.append(_collect_obligation_text_coherence_inner(ob))
    return " ".join(parts)


def coherence_output_missing_content_terms(user_query: str, payload: Dict[str, Any]) -> bool:
    """True if merged rows omit a required content term (LLM drift / keyword spam)."""
    terms = _content_terms_for_coherence(user_query)
    if not terms or not (payload.get("results") or []):
        return False
    parts: List[str] = []
    res = payload.get("results")
    if is_nested_category_query_results(res):
        blob = _collect_nested_results_text_for_coherence(payload)
        return any(not _term_in_coherence_blob(t, blob) for t in terms)
    for ob in res or []:
        if not isinstance(ob, dict):
            continue
        parts.append(_collect_obligation_text_for_coherence(ob))
        cit = ob.get("Citation")
        if isinstance(cit, list):
            parts.append(json.dumps(cit).lower())
        elif cit:
            parts.append(str(cit).lower())
    blob = " ".join(parts)
    return any(not _term_in_coherence_blob(t, blob) for t in terms)


def apply_query_coherence_to_payload(user_query: str, payload: Dict[str, Any]) -> None:
    """
    No-op: previously cleared all results when literal query tokens were missing from merged text,
    which removed valid answers after merge (synonyms, legal paraphrases, LLM rewrites).
    """
    return


def _normalize_ob_key(ob: Dict[str, Any]) -> tuple:
    """Build a key for deduplication: (duty, party, first 80 chars of key obligation)."""
    duty = (ob.get("DutyType") or "")
    duty = " ".join(str(duty).split()).lower() if duty else ""
    party = (ob.get("Responsible Party") or "")
    party = " ".join(str(party).split()).lower() if party else ""
    key_ob = ob.get("Owner Responsibility")
    if isinstance(key_ob, list):
        key_ob = " ".join(str(x) for x in key_ob if x)[:80]
    else:
        key_ob = (str(key_ob or ""))[:80]
    key_ob = " ".join(key_ob.split()).lower()
    return (duty, party, key_ob)


def _citation_parts_from_ob(ob: Dict[str, Any]) -> List[str]:
    """Extract all citation parts (page/section strings) from one obligation, including _source_page."""
    parts = []
    src_page = ob.get("_source_page")
    if src_page is not None:
        parts.append(f"Page {src_page}")
    citation = ob.get("citations") if ob.get("citations") is not None else ob.get("Citation")
    if citation is None:
        pass
    elif isinstance(citation, str) and citation.strip():
        parts.append(citation.strip())
    elif isinstance(citation, list):
        for c in citation:
            if isinstance(c, dict) and "references" in c:
                for ref in c.get("references") or []:
                    if isinstance(ref, dict):
                        p, s = ref.get("page"), ref.get("section")
                        if p or s:
                            sec = str(s or "").strip()
                            parts.append(f"Page {p}, Section {sec}".strip().rstrip(",").strip())
                continue
            if isinstance(c, dict):
                pages = _parse_page_numbers_field(c.get("pageNumbers"))
                sections = _parse_section_field(c.get("section"))
                if pages or sections:
                    page_part = f"Page {', '.join(map(str, pages))}" if pages else ""
                    if sections:
                        section_part = "; ".join(str(x) for x in sections)
                    else:
                        section_part = ""
                    parts.append(", ".join(filter(None, [page_part, section_part])))
            else:
                parts.append(str(c))
    return parts


def _document_prefixed_citation_string(full_ob: Dict[str, Any], doc_name: str) -> str:
    """Human-readable citation line for merge prompts and parsers (uses structured ``citations`` when needed)."""
    parts = _citation_parts_from_ob(full_ob)
    body = "; ".join(parts) if parts else ""
    d = (doc_name or "").strip()
    if d:
        return f"Document: {d} | {body}" if body else f"Document: {d} |"
    return body


def _parse_page_numbers_field(val: Any) -> List[int]:
    """Normalize pageNumbers from consolidated JSON (list, int, or comma-separated string)."""
    out: List[int] = []
    if val is None:
        return out
    if isinstance(val, bool):
        return out
    if isinstance(val, int):
        return [val] if val > 0 else []
    if isinstance(val, float):
        try:
            iv = int(val)
            return [iv] if iv > 0 else []
        except (ValueError, TypeError):
            return out
    if isinstance(val, str):
        for part in re.split(r"[\s,;]+", val):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                continue
        return sorted(set(out))
    if isinstance(val, list):
        for x in val:
            try:
                xi = int(x)
                if xi > 0:
                    out.append(xi)
            except (TypeError, ValueError):
                continue
        return sorted(set(out))
    return out


def _parse_section_field(val: Any) -> List[str]:
    """Normalize section from consolidated JSON (list or comma-separated string)."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"\s*,\s*", s) if p.strip()]
    return parts if parts else [s]


def _parse_chroma_stored_citation_field(val: Any) -> Optional[List[Dict[str, Any]]]:
    """
    Chroma metadata stores ``Citation`` as a string. New rows use JSON (from ``json.dumps``);
    older rows used ``str(list[dict])`` (Python repr). Return a list of dicts when parseable.
    """
    if val is None:
        return None
    if isinstance(val, list):
        if val and all(isinstance(x, dict) for x in val):
            return val
        return None
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s.startswith("["):
        return None
    try:
        data = json.loads(s)
        if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
            return data
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        import ast

        data = ast.literal_eval(s)
        if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
            return data
    except (ValueError, SyntaxError, TypeError, MemoryError):
        pass
    return None


def _parse_one_citation_segment(segment: str) -> Optional[Dict[str, Any]]:
    """Parse a single citation segment (e.g. 'Document: X.pdf | Page 4, Section 7(a)') into one { docId, pageNumbers, section }."""
    s = (segment or "").strip()
    if not s:
        return None
    doc_name = ""
    rest = s
    if "Document:" in s and "|" in s:
        idx = s.find("Document:")
        pipe = s.find("|", idx)
        if pipe > idx:
            doc_name = s[idx + len("Document:"):pipe].strip()
            rest = s[pipe + 1:].strip()
    page_numbers = []
    sections = []
    for part in re.split(r';\s*', rest):
        part = part.strip()
        if not part:
            continue
        page_m = re.search(r'Page\s*(\d+)', part, re.I)
        if page_m:
            page_numbers.append(int(page_m.group(1)))
        section_matches = re.findall(r'Section\s*([^;,]+?)(?=\s*(?:and\s+Section|;|,?\s*Page|\s*$))', part, re.I)
        if not section_matches:
            section_matches = re.findall(r'Section\s*(\S+)', part, re.I)
        for sec in section_matches:
            sec = sec.strip().rstrip('.,')
            if sec and sec not in sections:
                sections.append(sec)
    if doc_name or page_numbers or sections:
        return {"docId": doc_name, "pageNumbers": sorted(set(page_numbers)), "section": sections}
    return None


def citation_string_to_structured(citation: Any) -> List[Dict[str, Any]]:
    """
    Convert citation string to structured format [{ docId, pageNumbers, section }].
    Example: "Document: Commercial Lease Agreement.pdf | Page 4, Section 7(a); Page 5, Section 8(c)"
    -> [{ "docId": "Commercial Lease Agreement.pdf", "pageNumbers": [4, 5], "section": ["7(a)", "8(c)"] }]
    Multiple documents (merged obligation): "Document: A.pdf | Page 1, Section 2 ; Document: B.pdf | Page 3, Section 4"
    -> two objects in list. Separator between documents: " ; " (space-semicolon-space).
    Accepts already-structured list (normalizes and returns). Returns [] for None/empty.
    Multiple objects with the same docId are merged into one (combined pages and sections).
    """
    if citation is None:
        return []
    if isinstance(citation, list):
        out = []
        for c in citation:
            if isinstance(c, dict):
                out.append({
                    "docId": c.get("docId") or c.get("doc_id") or "",
                    "pageNumbers": list(c.get("pageNumbers") or c.get("page_numbers") or []),
                    "section": list(c.get("section") or []),
                })
            else:
                out.append({"docId": "", "pageNumbers": [], "section": [str(c)]})
        return merge_structured_citations_by_doc_id(out)
    s = (citation or "").strip()
    if not s:
        return []
    structured = _parse_chroma_stored_citation_field(s)
    if structured is not None:
        # #region agent log
        try:
            import time as _agent_time

            with open(
                r"c:\Users\AmithKrishnan(G1)XIN\Downloads\Legal-OCR\debug-fe1e15.log",
                "a",
                encoding="utf-8",
            ) as _agent_f:
                _agent_f.write(
                    json.dumps(
                        {
                            "sessionId": "fe1e15",
                            "hypothesisId": "H4",
                            "location": "query_system.citation_string_to_structured",
                            "message": "parsed Chroma JSON/repr citation metadata",
                            "data": {"n_items": len(structured)},
                            "timestamp": int(_agent_time.time() * 1000),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass
        # #endregion
        out: List[Dict[str, Any]] = []
        for c in structured:
            if not isinstance(c, dict):
                continue
            out.append(
                {
                    "docId": str(c.get("docId") or c.get("doc_id") or "").strip(),
                    "pageNumbers": _parse_page_numbers_field(c.get("pageNumbers")),
                    "section": _parse_section_field(c.get("section")),
                }
            )
        return merge_structured_citations_by_doc_id(out)
    # Multiple documents: "Document: A | ... ; Document: B | ..." (split only when ; is followed by "Document:")
    if re.search(r'\s+;\s+Document:\s*', s, re.I):
        segments = re.split(r'\s+;\s+(?=Document:\s*)', s, flags=re.I)
        out = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            one = _parse_one_citation_segment(seg)
            if one:
                out.append(one)
        if out:
            return merge_structured_citations_by_doc_id(out)
    # Single document
    one = _parse_one_citation_segment(s)
    return merge_structured_citations_by_doc_id([one]) if one else []


def _ensure_obligation_category_fields(ob: Dict[str, Any]) -> None:
    """Ensure API-facing obligation dicts include string category (subcategory is not exposed)."""
    if not isinstance(ob, dict):
        return
    c = ob.get("category")
    ob["category"] = "" if c is None else str(c).strip()
    ob.pop("subcategory", None)


def strip_subcategory_from_api_results(results: Optional[List[Dict[str, Any]]]) -> None:
    """Remove subcategory from each result row (smaller payloads; not used by clients)."""
    if not results:
        return
    for ob in results:
        if isinstance(ob, dict):
            ob.pop("subcategory", None)


def convert_result_citations_to_structured(
    final_result: Dict[str, Any],
    filtered_results: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """In-place: structured Citation on flat legacy rows; nested grouped results unchanged."""
    res = final_result.get("results")
    if not isinstance(res, list):
        return
    if is_nested_category_query_results(res):
        return
    for ob in res:
        if not isinstance(ob, dict):
            continue
        ob["Citation"] = citation_string_to_structured(ob.get("Citation"))
    for ob in res:
        if isinstance(ob, dict):
            _ensure_obligation_category_fields(ob)
    strip_subcategory_from_api_results(res)


def _str_list_field(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if x is not None and str(x).strip()]
    s = str(val).strip()
    return [s] if s else []


def _structured_citations_substantive(citations: Any) -> bool:
    """True when citations carry at least one doc id or usable page number (not empty shells)."""
    if not isinstance(citations, list) or not citations:
        return False
    for c in citations:
        if not isinstance(c, dict):
            continue
        if str(c.get("docId") or "").strip():
            return True
        pn = c.get("pageNumbers")
        if isinstance(pn, int) and pn > 0:
            return True
        if isinstance(pn, list) and any(
            (isinstance(x, int) and x > 0) or (isinstance(x, str) and x.strip().isdigit() and int(x) > 0)
            for x in pn
        ):
            return True
    return False


def _normalize_inner_api_obligation(ob: Dict[str, Any]) -> Dict[str, Any]:
    """API shape: Responsible Party, Owner Responsibility[], Reasoning[], citations (preferred) or Citation."""
    party = str(ob.get("Responsible Party") or ob.get("Responsible party") or "").strip() or "Unknown"
    out: Dict[str, Any] = {
        "Responsible Party": party,
        "Owner Responsibility": _str_list_field(ob.get("Owner Responsibility")),
        "Reasoning": _str_list_field(ob.get("Reasoning")),
    }
    dn = str(ob.get("document_name") or "").strip()
    if dn:
        out["document_name"] = dn
    raw_citations = ob.get("citations")
    if isinstance(raw_citations, list) and raw_citations and _structured_citations_substantive(raw_citations):
        out["citations"] = json.loads(json.dumps(raw_citations))
        return out
    cit = ob.get("Citation")
    if isinstance(cit, list) and cit:
        norm: List[Dict[str, Any]] = []
        for c in cit:
            if not isinstance(c, dict):
                continue
            doc = str(c.get("docId") or c.get("doc_id") or "").strip()
            pn_out = _parse_page_numbers_field(c.get("pageNumbers"))
            sec_out = _parse_section_field(c.get("section"))
            norm.append({"docId": doc, "pageNumbers": pn_out, "section": sec_out})
        if norm:
            out["citations"] = norm
            return out
    if isinstance(cit, str) and cit.strip():
        parsed = _parse_chroma_stored_citation_field(cit)
        if parsed is not None:
            norm2: List[Dict[str, Any]] = []
            for c in parsed:
                if not isinstance(c, dict):
                    continue
                doc = str(c.get("docId") or c.get("doc_id") or "").strip()
                norm2.append(
                    {
                        "docId": doc,
                        "pageNumbers": _parse_page_numbers_field(c.get("pageNumbers")),
                        "section": _parse_section_field(c.get("section")),
                    }
                )
            if norm2:
                out["citations"] = merge_structured_citations_by_doc_id(norm2)
                return out
        out["citations"] = citation_string_to_structured(cit)
        return out
    return out


def strip_related_keywords_from_api_payload(payload: Dict[str, Any]) -> None:
    """related_keywords support indexing/semantic retrieval only — omit from client-facing JSON."""
    results = payload.get("results")
    if not isinstance(results, list):
        return
    for grp in results:
        if not isinstance(grp, dict):
            continue
        for ob in grp.get("obligations") or []:
            if isinstance(ob, dict) and "related_keywords" in ob:
                ob.pop("related_keywords", None)


_RETRIEVAL_SHORT_QUERY_PAD = (
    "commercial lease obligations duties responsibilities landlord tenant premises agreement terms"
)

# Short-query vector expansion for rent-themed searches (keyword-only embeddings rely on related_keywords).
_RENT_THEME_VECTOR_SYNONYMS = (
    "base rent additional rent rent payment lease payment monthly rent holdover rent percentage rent "
    "rent abatement proration utility rents and charges rents for utilities purchase option rent closing rent"
)


def _expand_query_for_vector_retrieval(user_query: str) -> str:
    """
    Few-word queries yield thin embeddings compared to long indexed chunks. For retrieval only,
    pad with generic lease vocabulary; for rent-themed queries also append rent synonyms so
    keyword-only vectors retrieve matching obligations.
    """
    q = (user_query or "").strip()
    if not q:
        return q
    terms = _significant_query_terms_for_scope(q)
    if len(terms) >= 4:
        return q
    low = q.lower()
    if "rent" in low or "rental" in low:
        return f"{q}. {_RENT_THEME_VECTOR_SYNONYMS}. {_RETRIEVAL_SHORT_QUERY_PAD}"
    return f"{q}. {q}. {_RETRIEVAL_SHORT_QUERY_PAD}"


def _is_short_focused_query(user_query: str) -> bool:
    """≤3 significant tokens → vector under-match risk; give retrieval + merge more headroom."""
    q = (user_query or "").strip()
    if not q:
        return False
    return len(_significant_query_terms_for_scope(q)) <= 3


def _legacy_flat_results_to_category_groups(flat: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse-fallback: one 'Other' category; merge rows by Responsible Party."""
    by_party: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for ob in flat:
        if not isinstance(ob, dict):
            continue
        party = str(ob.get("Responsible Party") or "Unknown").strip()
        slot = by_party.setdefault(
            party,
            {"Responsible Party": party, "Owner Responsibility": [], "Reasoning": []},
        )
        slot["Owner Responsibility"].extend(_str_list_field(ob.get("Owner Responsibility")))
        slot["Reasoning"].extend(_str_list_field(ob.get("Reasoning")))
    obligations = list(by_party.values())
    if not obligations:
        return []
    return [{"category": "Other", "obligations": obligations}]


def _normalize_citation_dict_for_merge_struct(c: Dict[str, Any], doc_fallback: str) -> Dict[str, Any]:
    d = str(c.get("docId") or doc_fallback).strip() or doc_fallback
    pages = _parse_page_numbers_field(c.get("pageNumbers"))
    sections = _parse_section_field(c.get("section"))
    return {"docId": d, "pageNumbers": pages, "section": sections}


def _collect_structured_citations_from_merge_in(merge_in: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten all obligation citations from merge input into structured dicts."""
    out: List[Dict[str, Any]] = []
    for fr in merge_in or []:
        if not isinstance(fr, dict):
            continue
        doc_fallback = str(fr.get("document_name") or "").strip()
        if isinstance(fr.get("results"), list) and fr["results"]:
            for grp in fr["results"]:
                if not isinstance(grp, dict):
                    continue
                for ob in grp.get("obligations") or []:
                    if not isinstance(ob, dict):
                        continue
                    cit = ob.get("citations") if ob.get("citations") is not None else ob.get("Citation")
                    if isinstance(cit, str):
                        out.extend(citation_string_to_structured(cit))
                    elif isinstance(cit, list) and cit:
                        if isinstance(cit[0], dict) and "references" in cit[0]:
                            for block in cit:
                                if not isinstance(block, dict):
                                    continue
                                d0 = str(block.get("docId") or doc_fallback).strip()
                                for ref in block.get("references") or []:
                                    if not isinstance(ref, dict):
                                        continue
                                    try:
                                        p = int(ref.get("page") or 0)
                                    except (TypeError, ValueError):
                                        p = 0
                                    sec = str(ref.get("section") or "").strip()
                                    if p > 0 or sec:
                                        out.append(
                                            {"docId": d0, "pageNumbers": [p] if p else [], "section": [sec] if sec else []}
                                        )
                            continue
                        for c in cit:
                            if isinstance(c, dict):
                                out.append(_normalize_citation_dict_for_merge_struct(c, doc_fallback))
            continue
        for ob in fr.get("consolidated_results") or []:
            if not isinstance(ob, dict):
                continue
            cit = ob.get("citations") if ob.get("citations") is not None else ob.get("Citation")
            if isinstance(cit, str):
                out.extend(citation_string_to_structured(cit))
            elif isinstance(cit, list) and cit:
                if isinstance(cit[0], dict) and "references" in cit[0]:
                    for block in cit:
                        if not isinstance(block, dict):
                            continue
                        d0 = str(block.get("docId") or doc_fallback).strip()
                        for ref in block.get("references") or []:
                            if not isinstance(ref, dict):
                                continue
                            try:
                                p = int(ref.get("page") or 0)
                            except (TypeError, ValueError):
                                p = 0
                            sec = str(ref.get("section") or "").strip()
                            if p > 0 or sec:
                                out.append(
                                    {"docId": d0, "pageNumbers": [p] if p else [], "section": [sec] if sec else []}
                                )
                    continue
                for c in cit:
                    if isinstance(c, dict):
                        out.append(_normalize_citation_dict_for_merge_struct(c, doc_fallback))
    return merge_structured_citations_by_doc_id(out)


def _flat_structured_citations_to_api_refs(merged: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn merged [{docId, pageNumbers, section}] into API [{docId, references: [{page, section}]}]."""
    by_doc: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    seen_pairs: set = set()
    for c in merged:
        doc_id = str(c.get("docId") or "").strip()
        if not doc_id:
            continue
        pages: List[int] = []
        for x in c.get("pageNumbers") or []:
            try:
                pages.append(int(x))
            except (TypeError, ValueError):
                continue
        pages = sorted(set(pages))
        secs = [str(s).strip() for s in (c.get("section") or []) if str(s).strip()]
        refs = by_doc.setdefault(doc_id, [])
        if pages:
            for p in pages:
                if secs:
                    for s in secs:
                        key = (doc_id, p, s.lower())
                        if key in seen_pairs:
                            continue
                        seen_pairs.add(key)
                        refs.append({"page": p, "section": s})
                else:
                    key = (doc_id, p, "")
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    refs.append({"page": p, "section": ""})
        elif secs:
            for s in secs:
                key = (doc_id, 0, s.lower())
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                refs.append({"page": 0, "section": s})
    return [{"docId": d, "references": r} for d, r in by_doc.items() if r]


def build_api_citations_from_merge_in(merge_in: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """{ docId, references: [{ page, section }] } per document."""
    merged = _collect_structured_citations_from_merge_in(merge_in)
    return _flat_structured_citations_to_api_refs(merged)


def _collect_structured_citations_from_nested_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Structured citation dicts attached to nested API obligations after enrichment."""
    out: List[Dict[str, Any]] = []
    for grp in results or []:
        if not isinstance(grp, dict):
            continue
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            cit = ob.get("citations") if ob.get("citations") is not None else ob.get("Citation")
            if isinstance(cit, str) and cit.strip():
                out.extend(citation_string_to_structured(cit))
            elif isinstance(cit, list) and cit:
                if isinstance(cit[0], dict) and "references" in cit[0]:
                    for block in cit:
                        if not isinstance(block, dict):
                            continue
                        d0 = str(block.get("docId") or "").strip()
                        for ref in block.get("references") or []:
                            if not isinstance(ref, dict):
                                continue
                            try:
                                p = int(ref.get("page") or 0)
                            except (TypeError, ValueError):
                                p = 0
                            sec = str(ref.get("section") or "").strip()
                            if p > 0 or sec:
                                out.append(
                                    {"docId": d0, "pageNumbers": [p] if p else [], "section": [sec] if sec else []}
                                )
                    continue
                for c in cit:
                    if isinstance(c, dict):
                        out.append(_normalize_citation_dict_for_merge_struct(c, ""))
    return out


def build_api_citations_for_query_response(
    merge_in: List[Dict[str, Any]],
    nested_results: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Root-level citations from merge input plus any structured citations on nested obligations."""
    merged = _collect_structured_citations_from_merge_in(merge_in)
    if nested_results:
        merged.extend(_collect_structured_citations_from_nested_results(nested_results))
    merged = merge_structured_citations_by_doc_id(merged)
    return _flat_structured_citations_to_api_refs(merged)


def _token_bag_for_citation_match(text: str) -> Set[str]:
    return set(re.findall(r"[a-z][a-z0-9'-]{2,}", (text or "").lower()))


def _citation_match_score(blob_a: str, blob_b: str) -> float:
    a, b = _token_bag_for_citation_match(blob_a), _token_bag_for_citation_match(blob_b)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _obligation_citation_struct_for_enrichment(
    ob: Dict[str, Any], doc_fallback: str
) -> List[Dict[str, Any]]:
    """Structured citations list for one obligation (merge input row)."""
    cit_raw = ob.get("citations") if isinstance(ob.get("citations"), list) and ob.get("citations") else None
    if cit_raw:
        return json.loads(json.dumps(cit_raw))
    cs = ob.get("Citation")
    if isinstance(cs, str) and cs.strip():
        return citation_string_to_structured(cs)
    if isinstance(cs, list) and cs:
        return [
            _normalize_citation_dict_for_merge_struct(c, doc_fallback)
            for c in cs
            if isinstance(c, dict)
        ]
    return []


def _flatten_merge_in_for_citation_enrichment(merge_in: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One row per source obligation with category, party, duty lines, and structured citations.
    Includes both flat ``consolidated_results`` and nested ``results`` (vector / grouped merge input).
    """
    rows: List[Dict[str, Any]] = []
    for fr in merge_in or []:
        if not isinstance(fr, dict):
            continue
        doc_fallback = str(fr.get("document_name") or "").strip()
        if isinstance(fr.get("results"), list) and fr["results"]:
            for grp in fr["results"]:
                if not isinstance(grp, dict):
                    continue
                cat = str(grp.get("category") or "").strip().lower()
                for ob in grp.get("obligations") or []:
                    if not isinstance(ob, dict):
                        continue
                    cit_struct = _obligation_citation_struct_for_enrichment(ob, doc_fallback)
                    rows.append(
                        {
                            "category": cat
                            or str(
                                ob.get("category") or ob.get("_processing_category") or ""
                            )
                            .strip()
                            .lower(),
                            "Responsible Party": str(ob.get("Responsible Party") or "")
                            .strip()
                            .lower(),
                            "Owner Responsibility": _str_list_field(
                                ob.get("Owner Responsibility")
                            ),
                            "citations": cit_struct,
                        }
                    )
            continue
        for ob in fr.get("consolidated_results") or []:
            if not isinstance(ob, dict):
                continue
            cit_struct = _obligation_citation_struct_for_enrichment(ob, doc_fallback)
            rows.append(
                {
                    "category": str(
                        ob.get("category") or ob.get("_processing_category") or ""
                    ).strip().lower(),
                    "Responsible Party": str(ob.get("Responsible Party") or "").strip().lower(),
                    "Owner Responsibility": _str_list_field(ob.get("Owner Responsibility")),
                    "citations": cit_struct,
                }
            )
    return rows


def _obligation_citations_richness(citations: Any) -> Tuple[int, int]:
    """Return (total pageNumbers count, total section entries) for structured obligation citations."""
    if not isinstance(citations, list) or not citations:
        return 0, 0
    pages_total = 0
    sections_total = 0
    for c in citations:
        if not isinstance(c, dict):
            continue
        pn = c.get("pageNumbers")
        if isinstance(pn, int):
            pages_total += 1
        elif isinstance(pn, list):
            pages_total += len(pn)
        sec = c.get("section")
        if isinstance(sec, list):
            sections_total += len(sec)
        elif isinstance(sec, str) and sec.strip():
            sections_total += 1
    return pages_total, sections_total


def _citation_match_score_against_merged_blob(blob: str, cand: Dict[str, Any]) -> float:
    """Score how well a merge-input obligation matches a merged LLM output (long blob)."""
    lines = cand.get("Owner Responsibility") or []
    cand_blob = " ".join(lines)
    sc = _citation_match_score(blob, cand_blob) if cand_blob.strip() else 0.0
    blob_l = (blob or "").lower()
    for line in lines:
        t = (line or "").strip()
        if len(t) < 8:
            continue
        sc = max(sc, _citation_match_score(blob, t))
        if len(t) >= 20 and t.lower() in blob_l:
            sc = max(sc, 0.22)
    return sc


def _union_citations_from_contributing_candidates(
    candidates: List[Dict[str, Any]],
    cat: str,
    party: str,
    blob: str,
) -> List[Dict[str, Any]]:
    """
    When the LLM merged many source rows, take structured citations from every candidate whose
    category/party match and at least one duty line appears in the merged text.
    """
    if not blob.strip():
        return []
    blob_l = blob.lower()
    acc: List[Dict[str, Any]] = []
    for cand in candidates:
        if cat and cand.get("category") and cand["category"] != cat:
            continue
        cp = cand.get("Responsible Party") or ""
        if party and cp:
            if not (party == cp or party in cp or cp in party):
                continue
        elif party != cp:
            continue
        contributed = False
        for line in cand.get("Owner Responsibility") or []:
            t = (line or "").strip()
            if len(t) >= 22 and t.lower() in blob_l:
                contributed = True
                break
        if not contributed:
            continue
        cit = cand.get("citations") or []
        if isinstance(cit, list) and cit:
            acc.extend(c for c in cit if isinstance(c, dict))
    return merge_structured_citations_by_doc_id(acc) if acc else []


def enrich_nested_results_citations_from_merge_in(payload: Dict[str, Any], merge_in: List[Dict[str, Any]]) -> None:
    """Copy consolidated citations onto merge LLM output obligations when the model omitted them."""
    candidates = _flatten_merge_in_for_citation_enrichment(merge_in)
    if not candidates:
        return
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return
    if not is_nested_category_query_results(results):
        return
    for grp in results:
        if not isinstance(grp, dict):
            continue
        cat = str(grp.get("category") or "").strip().lower()
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            existing = ob.get("citations")
            if isinstance(existing, list) and existing:
                pages_total, sections_total = _obligation_citations_richness(existing)
                if pages_total > 1 or sections_total > 0:
                    continue
            blob = " ".join(_str_list_field(ob.get("Owner Responsibility")) + _str_list_field(ob.get("Reasoning")))
            if len(blob.strip()) < 6:
                continue
            best_i = -1
            best_sc = 0.0
            party = str(ob.get("Responsible Party") or "").strip().lower()
            for i, cand in enumerate(candidates):
                sc = _citation_match_score_against_merged_blob(blob, cand)
                if cand.get("category") and cat and cand["category"] == cat:
                    sc += 0.08
                cp = cand.get("Responsible Party") or ""
                if party and cp and (party in cp or cp in party):
                    sc += 0.05
                if sc > best_sc:
                    best_sc = sc
                    best_i = i
            if best_i >= 0 and best_sc >= 0.04 and candidates[best_i]["citations"]:
                ob["citations"] = json.loads(json.dumps(candidates[best_i]["citations"]))
                continue
            merged_cits = _union_citations_from_contributing_candidates(candidates, cat, party, blob)
            if merged_cits:
                ob["citations"] = json.loads(json.dumps(merged_cits))


def normalize_query_response_shape(
    payload: Dict[str, Any],
    merge_in: List[Dict[str, Any]],
    *,
    skip_citation_enrichment: bool = False,
) -> None:
    """
    Ensure results are [{category, obligations[]}]; enrich per-obligation citations; totals.
    Mutates payload in place. Does not set a root-level "citations" key.
    """
    raw = payload.get("results")
    if not isinstance(raw, list):
        payload["results"] = []
        raw = []
    if raw and isinstance(raw[0], dict) and "obligations" in raw[0]:
        cleaned: List[Dict[str, Any]] = []
        for grp in raw:
            if not isinstance(grp, dict):
                continue
            cat = str(grp.get("category") or "").strip() or "Other"
            obs_in = grp.get("obligations")
            if not isinstance(obs_in, list):
                continue
            inner: List[Dict[str, Any]] = []
            for ob in obs_in:
                if not isinstance(ob, dict):
                    continue
                inner.append(_normalize_inner_api_obligation(ob))
            if inner:
                cleaned.append({"category": cat, "obligations": inner})
        payload["results"] = cleaned
    elif raw and isinstance(raw[0], dict) and ("DutyType" in raw[0] or "Citation" in raw[0]):
        payload["results"] = _legacy_flat_results_to_category_groups(raw)
    else:
        payload["results"] = []

    if is_nested_category_query_results(payload.get("results") or []) and not skip_citation_enrichment:
        enrich_nested_results_citations_from_merge_in(payload, merge_in)

    payload.pop("citations", None)

    gr = payload.get("results") or []
    if is_nested_category_query_results(gr):
        sanitize_reasoning_duplication_across_obligations(gr)
    strip_related_keywords_from_api_payload(payload)
    payload["total_categories"] = len(gr)
    payload["total_obligations_found"] = sum(len(g.get("obligations") or []) for g in gr if isinstance(g, dict))


def _merge_max_input_obligations() -> int:
    raw = os.getenv("MERGE_MAX_INPUT_OBLIGATIONS", "24").strip()
    try:
        n = int(raw)
        return max(1, min(n, 200))
    except ValueError:
        return 24


def _merge_max_input_obligations_for_query(user_query: str, *, document_blocks: int = 1) -> int:
    """
    Raise merge budget for short queries and when several leases are in one merge call so the
    model still sees enough rows per document before capping. Short focused queries (e.g. "rent")
    are raised to at least 100 after vector topic augmentation and category expansion.
    """
    n = _merge_max_input_obligations()
    cap = n
    if _is_short_focused_query(user_query):
        cap = max(cap, 100)
    db = max(1, int(document_blocks or 1))
    if db > 1:
        cap = max(cap, min(240, 50 + 25 * db))
    return min(300, cap)


def _merge_rank_max_output_tokens() -> Optional[int]:
    raw = os.getenv("MERGE_RANK_MAX_OUTPUT_TOKENS", "").strip()
    if not raw:
        # Increased default to prevent truncation of comprehensive results
        return 20480
    try:
        return max(1024, int(raw))
    except ValueError:
        return 20480


def _skip_merge_and_rank_for_testing() -> bool:
    """SKIP_MERGE_AND_RANK: skip merge/rank LLM and return retrieval-shaped payload (for local testing)."""
    v = (os.getenv("SKIP_MERGE_AND_RANK") or "").strip().lower()
    return v in ("true", "1", "yes", "on")


def _filtered_fr_has_payload(fr: Any) -> bool:
    if not isinstance(fr, dict):
        return False
    res = fr.get("results")
    if isinstance(res, list):
        for g in res:
            if isinstance(g, dict) and isinstance(g.get("obligations"), list) and g["obligations"]:
                return True
    crs = fr.get("consolidated_results")
    return isinstance(crs, list) and len(crs) > 0


def _count_obligations_in_filtered(filtered_results: List[Dict[str, Any]]) -> int:
    n = 0
    for r in filtered_results:
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("results"), list) and r["results"]:
            for g in r["results"]:
                if isinstance(g, dict) and isinstance(g.get("obligations"), list):
                    n += sum(1 for x in g["obligations"] if isinstance(x, dict))
        else:
            n += len(r.get("consolidated_results") or [])
    return n


def _document_name_from_vector_obligation(ob: Dict[str, Any]) -> str:
    cit = ob.get("Citation") or ""
    if "Document:" in cit:
        return cit.split("Document:")[1].split("|")[0].strip()
    return (ob.get("document_name") or "").strip() or "Unknown"


def _consolidated_obligation_to_merge_dict(full_ob: Dict[str, Any], doc_name: str) -> Dict[str, Any]:
    """Same merge/rank row shape as rows built in ``_query_vector_store`` from consolidated JSON."""
    citation = _document_prefixed_citation_string(full_ob, doc_name)
    raw_cit = full_ob.get("citations") or full_ob.get("Citation")
    cit_copy: Optional[List[Dict[str, Any]]] = None
    if isinstance(raw_cit, list) and raw_cit:
        normalized_cits: List[Dict[str, Any]] = []
        for c in raw_cit:
            if not isinstance(c, dict):
                continue
            normalized_cits.append(
                {
                    "docId": str(c.get("docId") or doc_name).strip(),
                    "pageNumbers": _parse_page_numbers_field(c.get("pageNumbers")),
                    "section": _parse_section_field(c.get("section")),
                }
            )
        cit_copy = normalized_cits if normalized_cits else None
    cat_raw = full_ob.get("category") or full_ob.get("_processing_category")
    cat_str = ("" if cat_raw is None else str(cat_raw)).strip() or "Other"
    rk = full_ob.get("related_keywords")
    if isinstance(rk, list) and rk:
        related_kw = [str(x).strip() for x in rk if x is not None and str(x).strip()]
    else:
        related_kw = None
    ob_dict: Dict[str, Any] = {
        "document_name": doc_name,
        "DutyType": full_ob.get("DutyType") or "",
        "Responsible Party": full_ob.get("Responsible Party") or "",
        "Owner Responsibility": full_ob.get("Owner Responsibility")
        if isinstance(full_ob.get("Owner Responsibility"), list)
        else [str(full_ob.get("Owner Responsibility") or "")],
        "Reasoning": full_ob.get("Reasoning")
        if isinstance(full_ob.get("Reasoning"), list)
        else [str(full_ob.get("Reasoning") or "")],
        "Citation": citation,
        "citations": cit_copy,
        "category": cat_str,
    }
    if related_kw:
        ob_dict["related_keywords"] = related_kw
    return ob_dict


def augment_vector_candidates_with_topic_matches(
    candidates: List[Tuple[float, Dict[str, Any]]],
    user_query: str,
    doc_to_results: Dict[str, List[Dict[str, Any]]],
    *,
    logger: Optional[logging.Logger] = None,
) -> List[Tuple[float, Dict[str, Any]]]:
    """
    Embedding retrieval can rank one category (e.g. Financial) far above others even when
    ``related_keywords`` on Utilities / Purchase rows still align with the query. Scan consolidated
    obligations for the same document(s) already present in vector hits and append any row where
    ``_query_matches_obligation_topic`` is True (keyword / body / related_keywords), deduped by
    obligation signature. Ensures ``expand_grouped_vector_merge_to_full_categories`` can pull in
    entire categories that semantic similarity alone missed at chunk rank.
    """
    if not candidates:
        return candidates
    v = (os.getenv("LEGAL_OCR_VECTOR_TOPIC_AUGMENT") or "true").strip().lower()
    if v in ("0", "false", "no", "off"):
        return candidates

    seen_sig: Set[Tuple[str, str, str]] = set()
    for _, ob in candidates:
        seen_sig.add(_vector_obligation_dedupe_sig(ob))

    docs_to_scan: Set[str] = set()
    for _, ob in candidates:
        dn = (ob.get("document_name") or "").strip()
        if dn:
            docs_to_scan.add(dn)

    raw_ad = (os.getenv("LEGAL_OCR_VECTOR_AUGMENT_DISTANCE") or "2.0").strip() or "2.0"
    try:
        aug_dist = float(raw_ad)
    except ValueError:
        aug_dist = 2.0

    out: List[Tuple[float, Dict[str, Any]]] = list(candidates)
    added = 0
    for doc_name in docs_to_scan:
        flat = doc_to_results.get(doc_name)
        if not flat:
            continue
        for full_ob in flat:
            if not isinstance(full_ob, dict):
                continue
            cat = str(full_ob.get("_processing_category") or full_ob.get("category") or "").strip()
            if not _query_matches_obligation_topic(user_query, full_ob, cat):
                continue
            ob_dict = _consolidated_obligation_to_merge_dict(full_ob, doc_name)
            sig = _vector_obligation_dedupe_sig(ob_dict)
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            out.append((aug_dist, ob_dict))
            added += 1

    if added:
        log = logger or logging.getLogger(__name__)
        log.info(
            "Vector topic augment: +%d obligation row(s) from consolidated JSON (query/keywords aligned; embedding rank alone omitted)",
            added,
        )
    return out


def _vector_expand_full_categories_enabled() -> bool:
    v = (os.getenv("LEGAL_OCR_VECTOR_EXPAND_FULL_CATEGORY") or "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def expand_grouped_vector_merge_to_full_categories(
    grouped_docs: List[Dict[str, Any]],
    consolidated_file_entries: List[Dict[str, Any]],
    *,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    After semantic retrieval returns obligation rows, replace each hit category with the **full**
    obligation list for that category from consolidated JSON (same document).

    Vector search + related_keywords then act as **category gatekeepers**; the merge LLM receives
    every duty row in those categories so line-level trimming does not drop semantically related
    bullets that did not appear in the embedding chunk.
    """
    if not _vector_expand_full_categories_enabled():
        return grouped_docs
    doc_to_entry: Dict[str, Dict[str, Any]] = {}
    for entry in consolidated_file_entries or []:
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") or {}
        dn = (entry.get("document_name") or data.get("document_name") or "").strip()
        if dn:
            doc_to_entry[dn] = entry

    expanded: List[Dict[str, Any]] = []
    for fr in grouped_docs:
        if not isinstance(fr, dict):
            expanded.append(fr)
            continue
        doc_name = (fr.get("document_name") or "").strip()
        results_in = fr.get("results")
        if not isinstance(results_in, list) or not results_in:
            expanded.append(fr)
            continue
        cats_hit: Set[str] = set()
        for grp in results_in:
            if isinstance(grp, dict):
                c = str(grp.get("category") or "").strip()
                if c:
                    cats_hit.add(c)
        entry = doc_to_entry.get(doc_name)
        data = (entry or {}).get("data") if entry else None
        res_tree = data.get("results") if isinstance(data, dict) else None
        if not isinstance(res_tree, list) or not res_tree or not cats_hit:
            expanded.append(fr)
            continue

        normalize_party_fields_in_groups(res_tree, party_metadata=data.get("party_metadata"))

        new_results: List[Dict[str, Any]] = []
        for grp in res_tree:
            if not isinstance(grp, dict):
                continue
            cat = str(grp.get("category") or "").strip()
            if cat not in cats_hit:
                continue
            obs_out: List[Dict[str, Any]] = []
            for ob in grp.get("obligations") or []:
                if isinstance(ob, dict):
                    obs_out.append(_consolidated_obligation_to_merge_dict(ob, doc_name))
            if obs_out:
                new_results.append({"category": cat, "obligations": obs_out})
        if new_results:
            log = logger or logging.getLogger(__name__)
            n_before = _count_obligations_in_filtered([fr])
            n_after = sum(len(g.get("obligations") or []) for g in new_results if isinstance(g, dict))
            log.info(
                "Vector category expansion for %r: categories=%s obligations %d -> %d",
                doc_name,
                sorted(cats_hit),
                n_before,
                n_after,
            )
            expanded.append(
                {**{k: v for k, v in fr.items() if k not in ("results", "consolidated_results")},
                 "results": new_results,
                 "consolidated_results": []},
            )
        else:
            expanded.append(fr)
    return expanded


def grouped_merge_input_from_vector_obligations(
    vector_obligations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Build merge input like grouped consolidated JSON: per document,
    results: [ { "category", "obligations" } ] using each hit's stored category
    (category / _processing_category) so merge preserves taxonomy labels (HVAC, Utilities, …).
    """
    doc_to_obligations: Dict[str, List[Dict[str, Any]]] = {}
    for ob in vector_obligations:
        if not isinstance(ob, dict):
            continue
        doc_name = _document_name_from_vector_obligation(ob)
        doc_to_obligations.setdefault(doc_name, []).append(ob)
    out: List[Dict[str, Any]] = []
    for doc_name, ob_list in doc_to_obligations.items():
        by_cat: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        for ob in ob_list:
            cat_raw = ob.get("category") or ob.get("_processing_category")
            cat = str(cat_raw).strip() if cat_raw is not None else ""
            if not cat:
                cat = "Other"
            by_cat.setdefault(cat, []).append(ob)
        results = [{"category": c, "obligations": obs} for c, obs in by_cat.items()]
        out.append(
            {
                "document_name": doc_name,
                "results": results,
                "consolidated_results": [],
            }
        )
    return out


def _iter_grouped_obligations_in_order(fr: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Flatten grouped ``results`` to (category, obligation) in traversal order."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    if not isinstance(fr, dict):
        return out
    for grp in fr.get("results") or []:
        if not isinstance(grp, dict):
            continue
        cat = str(grp.get("category") or "").strip()
        for ob in grp.get("obligations") or []:
            if isinstance(ob, dict):
                out.append((cat, ob))
    return out


def _rebuild_fr_grouped(fr: Dict[str, Any], pairs: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """Rebuild one document's grouped results after fair capping."""
    by_cat: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for cat, ob in pairs:
        key = cat if cat else "Other"
        by_cat.setdefault(key, []).append(ob)
    results = [{"category": c, "obligations": obs} for c, obs in by_cat.items()]
    base = {k: v for k, v in fr.items() if k not in ("results", "consolidated_results")}
    return {**base, "results": results, "consolidated_results": []}


def cap_merge_filtered_results(
    filtered_results: List[Dict[str, Any]],
    max_total: int,
) -> tuple:
    """
    Return (possibly truncated copy of filtered_results, original_count).
    Trims inner obligations across category groups (results[]) or flat consolidated_results.

    When **multiple** documents supply grouped ``results``, obligations are chosen **round-robin**
    across documents so one lease cannot consume the entire merge budget before others contribute.
    """
    orig = _count_obligations_in_filtered(filtered_results)
    if max_total <= 0 or orig <= max_total:
        return filtered_results, orig

    grouped_frs: List[Dict[str, Any]] = []
    for fr in filtered_results:
        if isinstance(fr, dict) and isinstance(fr.get("results"), list) and fr["results"]:
            grouped_frs.append(fr)

    if len(grouped_frs) <= 1:
        out: List[Dict[str, Any]] = []
        n = 0
        for fr in filtered_results:
            if not isinstance(fr, dict):
                continue
            if isinstance(fr.get("results"), list) and fr["results"]:
                if n >= max_total:
                    base = {k: v for k, v in fr.items() if k not in ("results", "consolidated_results")}
                    out.append({**base, "results": [], "consolidated_results": []})
                    continue
                new_results: List[Dict[str, Any]] = []
                for grp in fr["results"]:
                    if not isinstance(grp, dict):
                        continue
                    obs = [x for x in (grp.get("obligations") or []) if isinstance(x, dict)]
                    kept: List[Dict[str, Any]] = []
                    for ob in obs:
                        if n >= max_total:
                            break
                        kept.append(ob)
                        n += 1
                    if kept:
                        new_results.append({**grp, "obligations": kept})
                    if n >= max_total:
                        break
                base = {k: v for k, v in fr.items() if k not in ("results", "consolidated_results")}
                out.append({**base, "results": new_results, "consolidated_results": []})
                continue
            crs = list(fr.get("consolidated_results") or [])
            if n >= max_total:
                out.append({**fr, "consolidated_results": []})
                continue
            take = crs[: max_total - n]
            n += len(take)
            out.append({**fr, "consolidated_results": take})
        return out, orig

    pools = [_iter_grouped_obligations_in_order(fr) for fr in grouped_frs]
    idxs = [0] * len(pools)
    picked: List[List[Tuple[str, Dict[str, Any]]]] = [[] for _ in pools]
    n = 0
    while n < max_total:
        progressed = False
        for i in range(len(pools)):
            if n >= max_total:
                break
            if idxs[i] < len(pools[i]):
                picked[i].append(pools[i][idxs[i]])
                idxs[i] += 1
                n += 1
                progressed = True
        if not progressed:
            break

    id_to_built: Dict[int, Dict[str, Any]] = {
        id(grouped_frs[i]): _rebuild_fr_grouped(grouped_frs[i], picked[i])
        for i in range(len(grouped_frs))
    }

    out: List[Dict[str, Any]] = []
    for fr in filtered_results:
        if not isinstance(fr, dict):
            continue
        if id(fr) in id_to_built:
            out.append(id_to_built[id(fr)])
    n = _count_obligations_in_filtered(out)
    for fr in filtered_results:
        if not isinstance(fr, dict):
            continue
        if id(fr) in id_to_built:
            continue
        if isinstance(fr.get("results"), list) and fr["results"]:
            if n >= max_total:
                base = {k: v for k, v in fr.items() if k not in ("results", "consolidated_results")}
                out.append({**base, "results": [], "consolidated_results": []})
                continue
            new_results: List[Dict[str, Any]] = []
            for grp in fr["results"]:
                if not isinstance(grp, dict):
                    continue
                obs = [x for x in (grp.get("obligations") or []) if isinstance(x, dict)]
                kept: List[Dict[str, Any]] = []
                for ob in obs:
                    if n >= max_total:
                        break
                    kept.append(ob)
                    n += 1
                if kept:
                    new_results.append({**grp, "obligations": kept})
                if n >= max_total:
                    break
            base = {k: v for k, v in fr.items() if k not in ("results", "consolidated_results")}
            out.append({**base, "results": new_results, "consolidated_results": []})
            continue
        crs = list(fr.get("consolidated_results") or [])
        if n >= max_total:
            out.append({**fr, "consolidated_results": []})
            continue
        take = crs[: max_total - n]
        n += len(take)
        out.append({**fr, "consolidated_results": take})
    return out, orig


def parse_llm_json_object(raw: str) -> Dict[str, Any]:
    """
    Parse JSON from merge/rank (and similar) LLM output: fences, trailing commas, optional json-repair.
    """
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    def _loads(s: str) -> Dict[str, Any]:
        obj = json.loads(s)
        if not isinstance(obj, dict):
            raise ValueError("LLM JSON root must be an object")
        return obj

    last_err: Optional[BaseException] = None
    for candidate in (text, re.sub(r",(\s*[\]\}])", r"\1", text)):
        try:
            return _loads(candidate)
        except json.JSONDecodeError as e:
            last_err = e
            continue
    try:
        from json_repair import repair_json

        repaired = repair_json(text)
        if not isinstance(repaired, str):
            repaired = str(repaired)
        return _loads(repaired)
    except ImportError:
        pass
    except Exception as e:
        last_err = e
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        inner = text[i : j + 1]
        for candidate in (inner, re.sub(r",(\s*[\]\}])", r"\1", inner)):
            try:
                return _loads(candidate)
            except json.JSONDecodeError as e:
                last_err = e
    msg = f"invalid LLM JSON ({last_err})" if last_err else "invalid LLM JSON"
    raise ValueError(msg)


_MERGE_PROMPT_DROP_KEYS = frozenset({"subcategory", "related_keywords"})


def merge_prompt_filtered_snapshot(filtered_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Smaller merge prompt: drop subcategory and related_keywords (retrieval/indexing only; not for merge scoping)."""
    out: List[Dict[str, Any]] = []
    for fr in filtered_results:
        if not isinstance(fr, dict):
            continue
        if isinstance(fr.get("results"), list) and fr["results"]:
            base = {k: v for k, v in fr.items() if k not in ("consolidated_results", "results")}
            slim_groups: List[Dict[str, Any]] = []
            for grp in fr["results"]:
                if not isinstance(grp, dict):
                    continue
                slim_obs: List[Dict[str, Any]] = []
                for ob in grp.get("obligations") or []:
                    if isinstance(ob, dict):
                        slim_obs.append({k: v for k, v in ob.items() if k not in _MERGE_PROMPT_DROP_KEYS})
                slim_groups.append({"category": grp.get("category"), "obligations": slim_obs})
            out.append({**base, "results": slim_groups, "consolidated_results": []})
            continue
        base = {k: v for k, v in fr.items() if k != "consolidated_results"}
        crs = fr.get("consolidated_results") or []
        slim: List[Dict[str, Any]] = []
        for ob in crs:
            if isinstance(ob, dict):
                slim.append({k: v for k, v in ob.items() if k not in _MERGE_PROMPT_DROP_KEYS})
        out.append({**base, "consolidated_results": slim})
    return out


def build_rank_response_without_llm_merge(
    user_query: str,
    filtered_results: List[Dict[str, Any]],
    *,
    documents_searched_count: Optional[int] = None,
    merge_fallback: bool = False,
    skip_query_post_filters: bool = False,
) -> Dict[str, Any]:
    """Assemble ranked-shaped response without calling the merge LLM (parse fallback after failed merge)."""
    doc_count = documents_searched_count if documents_searched_count is not None else len(filtered_results)
    grouped_mode = any(
        isinstance(fr, dict) and isinstance(fr.get("results"), list) and fr["results"]
        for fr in filtered_results
    )
    if grouped_mode:
        merged_groups: List[Dict[str, Any]] = []
        for fr in filtered_results:
            if not isinstance(fr, dict):
                continue
            for grp in fr.get("results") or []:
                if isinstance(grp, dict):
                    merged_groups.append(json.loads(json.dumps(grp)))
        out: Dict[str, Any] = {
            "query": user_query,
            "total_documents_searched": doc_count,
            "results": merged_groups,
            "processed_at": datetime.now().isoformat(),
        }
        if merge_fallback:
            out["merge_fallback"] = True
            out["merge_note"] = "Merge LLM output was invalid or truncated; returned unmerged retrieval results."
        if not skip_query_post_filters:
            apply_query_coherence_to_payload(user_query, out)
        normalize_query_response_shape(
            out, filtered_results, skip_citation_enrichment=merge_fallback
        )
        return out

    rows: List[Dict[str, Any]] = []
    for fr in filtered_results:
        if not isinstance(fr, dict):
            continue
        doc_name = (fr.get("document_name") or "").strip()
        for ob in fr.get("consolidated_results") or []:
            if not isinstance(ob, dict):
                continue
            row = json.loads(json.dumps(ob))
            cit = row.get("Citation")
            if isinstance(cit, str) and doc_name and "Document:" not in cit:
                row["Citation"] = f"Document: {doc_name} | {cit}"
            rows.append(row)
    out = {
        "query": user_query,
        "total_documents_searched": doc_count,
        "results": rows,
        "processed_at": datetime.now().isoformat(),
    }
    if merge_fallback:
        out["merge_fallback"] = True
        out["merge_note"] = "Merge LLM output was invalid or truncated; returned unmerged retrieval results."
    convert_result_citations_to_structured(out, filtered_results)
    if not skip_query_post_filters:
        apply_query_scope_trim_to_results(user_query, out)
        apply_query_coherence_to_payload(user_query, out)
    normalize_query_response_shape(
        out, filtered_results, skip_citation_enrichment=merge_fallback
    )
    return out


def merge_rank_fallback_payload(
    user_query: str,
    filtered_results: List[Dict[str, Any]],
    *,
    documents_searched_count: Optional[int] = None,
    skip_query_post_filters: bool = False,
) -> Dict[str, Any]:
    """If merge LLM fails, return grouped shape from raw consolidated rows (category Other)."""
    return build_rank_response_without_llm_merge(
        user_query,
        filtered_results,
        documents_searched_count=documents_searched_count,
        merge_fallback=True,
        skip_query_post_filters=skip_query_post_filters,
    )


def _empty_merge_rank_result(
    user_query: str,
    *,
    documents_searched_count: int,
    merge_note: str,
) -> Dict[str, Any]:
    """Structured empty merge response when merge JSON cannot be parsed (no retrieval fallback)."""
    return {
        "query": user_query,
        "total_documents_searched": documents_searched_count,
        "total_obligations_found": 0,
        "total_categories": 0,
        "results": [],
        "processed_at": datetime.now().isoformat(),
        "merge_note": merge_note,
    }


def deduplicate_and_merge_citations(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate obligations that represent the same duty (same DutyType, Party, and similar obligation text).
    For each merged group, combine Owner Responsibility, Reasoning, and Citation so that Citation lists
    ALL page numbers and sections where that obligation is mentioned.
    """
    if not obligations:
        return []
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for ob in obligations:
        key = _normalize_ob_key(ob)
        groups.setdefault(key, []).append(ob)
    merged = []
    for key, group in groups.items():
        first = group[0].copy()
        # Collect all citation parts from the group (all pages and sections)
        all_citation_parts = []
        seen_citation = set()
        for o in group:
            for p in _citation_parts_from_ob(o):
                if p and p not in seen_citation:
                    seen_citation.add(p)
                    all_citation_parts.append(p)
        first["Citation"] = "; ".join(all_citation_parts) if all_citation_parts else ""
        # Merge Owner Responsibility and Reasoning (unique items)
        owner_set = []
        seen_owner = set()
        for o in group:
            val = o.get("Owner Responsibility")
            for item in (val if isinstance(val, list) else [val] if val is not None else []):
                s = str(item).strip()
                if s and s not in seen_owner:
                    seen_owner.add(s)
                    owner_set.append(s)
        first["Owner Responsibility"] = owner_set
        reason_set = []
        seen_reason = set()
        for o in group:
            val = o.get("Reasoning")
            for item in (val if isinstance(val, list) else [val] if val is not None else []):
                s = str(item).strip()
                if s and s not in seen_reason:
                    seen_reason.add(s)
                    reason_set.append(s)
        first["Reasoning"] = reason_set
        first.pop("_source_page", None)
        merged.append(first)
    return merged


def consolidated_results_to_markdown_table(obligations: List[Dict[str, Any]]) -> str:
    """
    Transform consolidated_results (or results) into a single Markdown table string.
    Deduplicates obligations and merges citations so each row lists all page numbers and sections.
    Flattens Owner Responsibility and Reasoning to semicolon-separated strings;
    escapes pipe characters so they don't break table syntax.
    """
    obligations = deduplicate_and_merge_citations(obligations or [])
    HEADERS = "| Duty Type | Party | Key Obligations | Reasoning | Citation |"
    SEP = "| :--- | :--- | :--- | :--- | :--- |"

    def _cell(s: str) -> str:
        """Replace pipe with slash so Markdown table doesn't break."""
        return (s or "").replace("|", "/").strip()

    def _flatten(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, list):
            parts = [str(x).strip() for x in val if x is not None]
            return "; ".join(parts)
        return str(val).strip()

    def _citation_str(citation: Any) -> str:
        if citation is None:
            return ""
        if isinstance(citation, str):
            return citation
        if isinstance(citation, list):
            out = []
            for c in citation:
                if isinstance(c, dict):
                    pages = c.get("pageNumbers") or []
                    sections = c.get("section") or []
                    if pages or sections:
                        page_part = f"Page {', '.join(map(str, pages))}" if pages else ""
                        section_part = "; ".join(sections) if sections else ""
                        out.append(", ".join(filter(None, [page_part, section_part])))
                else:
                    out.append(str(c))
            return "; ".join(out)
        return str(citation)

    rows = [HEADERS, SEP]
    for ob in obligations:
        duty = _cell(_flatten(ob.get("DutyType")))
        party = _cell(_flatten(ob.get("Responsible Party")))
        key_ob = _cell(_flatten(ob.get("Owner Responsibility")))
        reasoning = _cell(_flatten(ob.get("Reasoning")))
        citation = _cell(_citation_str(ob.get("Citation")))
        rows.append(f"| {duty} | {party} | {key_ob} | {reasoning} | {citation} |")
    return "\n".join(rows)


def parse_markdown_table_to_obligations(md_text: str) -> List[Dict[str, Any]]:
    """
    Parse a consolidated Markdown table (Duty Type | Party | Key Obligations | Reasoning | Citation)
    into a list of obligation dicts in the same JSON shape as before:
    DutyType, Responsible Party, Owner Responsibility (list), Reasoning (list), Citation (string).
    """
    obligations = []
    lines = [ln.strip() for ln in md_text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return obligations
    # Skip header (line 0) and separator (line 1); data rows start at line 2
    for line in lines[2:]:
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.split("|")]
        # Table format: | cell | cell | cell | cell | cell |  -> parts = ['', c1, c2, c3, c4, c5, '']
        if len(parts) < 6:
            continue
        duty_type = (parts[1] or "").replace("/", "|").strip()
        party = (parts[2] or "").replace("/", "|").strip()
        key_ob = (parts[3] or "").replace("/", "|").strip()
        reasoning = (parts[4] or "").replace("/", "|").strip()
        citation = (parts[5] or "").replace("/", "|").strip()
        # Split Key Obligations and Reasoning by semicolon into lists (original format)
        owner_resp = [x.strip() for x in key_ob.split(";") if x.strip()] if key_ob else []
        reasoning_list = [x.strip() for x in reasoning.split(";") if x.strip()] if reasoning else []
        if not owner_resp and key_ob:
            owner_resp = [key_ob]
        if not reasoning_list and reasoning:
            reasoning_list = [reasoning]
        obligations.append({
            "DutyType": duty_type,
            "Responsible Party": party,
            "Owner Responsibility": owner_resp,
            "Reasoning": reasoning_list,
            "Citation": citation,
        })
    return obligations


class ObligationQuerySystem:
    """Query legal obligations from consolidated JSON in output folder. Uses Azure OpenAI or Gemini via llm_client."""

    def __init__(
        self,
        local_output_folder: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize with local output folder. Requires GEMINI_API_KEY, or Azure vars when USE_AZURE_OPENAI, or AWS creds for Bedrock."""
        self.local_output_folder = str(Path(local_output_folder or os.getenv("OUTPUT_FOLDER", "output")).resolve())
        _use_azure = os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")
        self.model = model or get_default_model()
        self.logger = logging.getLogger(__name__)
        self._setup_logging()
        if _use_azure:
            if not os.getenv("AZURE_OPENAI_ENDPOINT") or not (os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")):
                raise ValueError("USE_AZURE_OPENAI is set; AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or AZURE_OPENAI_KEY) must be set in .env")
            if not os.getenv("AZURE_OPENAI_DEPLOYMENT") and not os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") and not os.getenv("OPENAI_DEPLOYMENT_NAME"):
                raise ValueError("USE_AZURE_OPENAI is set; AZURE_OPENAI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT_NAME must be set in .env")
        elif use_bedrock_llm():
            pass
        else:
            if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
                raise ValueError("GEMINI_API_KEY must be set in .env (https://aistudio.google.com/app/apikey)")
        self.logger.info(f"ObligationQuerySystem: output={self.local_output_folder}, model={self.model}")
    
    def _setup_logging(self):
        """Setup logging configuration"""
        log_dir = Path(os.getenv("LOG_DIR", "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "api.log"
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(log_file, encoding="utf-8"),
            ]
        )
        self.logger = logging.getLogger(__name__)

    def _vector_store_available(self) -> bool:
        """True if vector_store module is importable."""
        try:
            return importlib.util.find_spec("vector_store") is not None
        except Exception:
            return False
    
    def _generate_content(self, prompt: str, temperature: float = 0.1, response_mime_type: str = "application/json"):
        """Call configured LLM (Azure OpenAI or Gemini) via llm_client."""
        return llm_generate_content(prompt, model=self.model, temperature=temperature, response_mime_type=response_mime_type)
    
    async def _generate_content_async(
        self,
        prompt: str,
        temperature: float = 0.1,
        response_mime_type: str = "application/json",
        max_output_tokens: Optional[int] = None,
    ):
        """Async wrapper for LLM API calls (Azure OpenAI, Bedrock, or Gemini)."""
        from llm_client import generate_content_async
        return await generate_content_async(
            prompt,
            model=self.model,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        )
    
    def load_consolidated_jsons(self) -> List[Dict[str, Any]]:
        """Load *_consolidated.json first, then *_consolidated.md, then *_pagewise.json. Prefers JSON.
        Consolidated JSON may store obligations under top-level "results" (grouped categories),
        or legacy consolidated_results / bucket keys.
        """
        t0 = time.perf_counter()
        self.logger.info("[TIMING] Step: load_consolidated_jsons - start")
        root = Path(self.local_output_folder)
        if not root.exists():
            self.logger.warning(f"Output folder does not exist: {root}")
            return []
        loaded_data = []
        # 1. Prefer consolidated JSON
        for p in sorted(root.rglob("*_consolidated.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_data.append({
                    "file_path": str(p.resolve()),
                    "file_name": p.name,
                    "document_name": data.get("document_name", "Unknown"),
                    "data": data,
                })
                self.logger.info(f"Loaded: {p.name}")
            except Exception as e:
                self.logger.error(f"Error loading {p}: {e}")
        # 2. Else load consolidated Markdown (parse to same shape)
        if not loaded_data:
            md_files = list(sorted(root.rglob("*_consolidated.md")))
            for p in md_files:
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        md_text = f.read()
                    obligations = parse_markdown_table_to_obligations(md_text)
                    stem = p.stem
                    document_name = re.sub(r"_\d{8}_\d{6}_consolidated$", "", stem)
                    if not document_name:
                        document_name = stem
                    document_name = f"{document_name}.pdf"
                    data = {
                        "document_name": document_name,
                        "consolidated_results": obligations,
                        "total_obligations_found": len(obligations),
                        "party_metadata": {},
                    }
                    loaded_data.append({
                        "file_path": str(p.resolve()),
                        "file_name": p.name,
                        "document_name": document_name,
                        "data": data,
                    })
                    self.logger.info(f"Loaded (markdown): {p.name} -> {len(obligations)} obligations")
                except Exception as e:
                    self.logger.error(f"Error loading {p}: {e}")
        # 3. Else load pagewise and flatten to consolidated_results
        if not loaded_data:
            for p in sorted(root.rglob("*_pagewise.json")):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        pw = json.load(f)
                    page_results = pw.get("page_results") or {}
                    keys_sorted = sorted(page_results.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
                    doc_name = pw.get("document_name", "Unknown")
                    flat: List[Dict[str, Any]] = []
                    for k in keys_sorted:
                        for bucket in page_results.get(k) or []:
                            if not isinstance(bucket, dict):
                                continue
                            category = str(bucket.get("category") or "").strip()
                            for ob in bucket.get("obligations") or []:
                                if not isinstance(ob, dict):
                                    continue
                                ob_copy = json.loads(json.dumps(ob))
                                if category and not str(ob_copy.get("category") or "").strip():
                                    ob_copy["category"] = category
                                citations = ob_copy.get("citations")
                                if isinstance(citations, list) and citations:
                                    normalized: List[Dict[str, Any]] = []
                                    for c in citations:
                                        if not isinstance(c, dict):
                                            continue
                                        if "pageNumbers" in c or "references" in c or "docId" in c:
                                            normalized.append(c)
                                            continue
                                        page = c.get("page")
                                        section = c.get("section")
                                        if page or section:
                                            try:
                                                pn = int(page) if page is not None else None
                                            except (TypeError, ValueError):
                                                pn = None
                                            normalized.append(
                                                {
                                                    "docId": str(ob_copy.get("docId") or doc_name),
                                                    "pageNumbers": [pn] if pn else [],
                                                    "section": [str(section)] if section else [],
                                                }
                                            )
                                    if normalized:
                                        ob_copy["citations"] = normalized
                                flat.append(ob_copy)
                    # #region agent log
                    try:
                        import json as _agent_json, time as _agent_time

                        sample_keys = sorted(list(flat[0].keys()))[:8] if flat else []
                        with open(
                            r"c:\Users\AmithKrishnan(G1)XIN\Downloads\Legal-OCR\debug-fe1e15.log",
                            "a",
                            encoding="utf-8",
                        ) as _agent_f:
                            _agent_f.write(
                                _agent_json.dumps(
                                    {
                                        "sessionId": "fe1e15",
                                        "hypothesisId": "H6",
                                        "location": "query_system.load_consolidated_jsons",
                                        "message": "pagewise flattened to obligations",
                                        "data": {
                                            "doc_name": str(doc_name)[:160],
                                            "flat_count": len(flat),
                                            "sample_keys": sample_keys,
                                        },
                                        "timestamp": int(_agent_time.time() * 1000),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # #endregion
                    data = {
                        "document_name": doc_name,
                        "consolidated_results": flat,
                        "total_obligations_found": len(flat),
                        "party_metadata": pw.get("party_metadata") or {},
                    }
                    loaded_data.append({
                        "file_path": str(p.resolve()),
                        "file_name": p.name,
                        "document_name": data["document_name"],
                        "data": data,
                    })
                    self.logger.info(f"Loaded (pagewise): {p.name}")
                except Exception as e:
                    self.logger.error(f"Error loading {p}: {e}")
        elapsed = time.perf_counter() - t0
        self.logger.info(f"[TIMING] Step: load_consolidated_jsons - done in {elapsed:.3f}s ({len(loaded_data)} files)")
        return loaded_data

    @staticmethod
    def _parse_vector_chunk_text(chunk_text: str) -> Dict[str, Any]:
        """
        Parse chunk text produced by vector_store.obligation_to_chunk_text().
        Format: "DutyType: ... Responsible Party: ... Key Obligations: ... Reasoning: ... Citation: ..."
        Returns dict with 'Owner Responsibility' (list) and 'Reasoning' (list) only.
        """
        if not chunk_text or not isinstance(chunk_text, str):
            return {"Owner Responsibility": [], "Reasoning": []}
        text = chunk_text.strip()
        owner_resp: List[str] = []
        reasoning: List[str] = []
        key_ob_prefix = "Key Obligations:"
        reasoning_prefix = "Reasoning:"
        citation_prefix = "Citation:"
        if key_ob_prefix in text:
            start = text.find(key_ob_prefix) + len(key_ob_prefix)
            end = text.find(reasoning_prefix, start)
            if end == -1:
                end = len(text)
            key_ob_str = text[start:end].strip().rstrip(".").strip()
            if key_ob_str:
                owner_resp = [key_ob_str]
        if reasoning_prefix in text:
            start = text.find(reasoning_prefix) + len(reasoning_prefix)
            end = text.find(citation_prefix, start)
            if end == -1:
                end = len(text)
            reasoning_str = text[start:end].strip().rstrip(".").strip()
            if reasoning_str:
                reasoning = [reasoning_str]
        return {"Owner Responsibility": owner_resp, "Reasoning": reasoning}

    @staticmethod
    def _document_id_allowset(document_ids: Optional[List[str]]) -> Optional[set]:
        """Normalized filename / basename keys for document_ids filter; None means no filter."""
        if not document_ids:
            return None
        normalized_allowed = set()
        for doc_id in document_ids:
            d = (doc_id or "").strip()
            if not d:
                continue
            name = Path(d).name or d
            normalized_allowed.add(name.lower().strip())
            base = name.rsplit(".", 1)[0] if "." in name else name
            if base:
                normalized_allowed.add(base.lower())
        return normalized_allowed

    @staticmethod
    def _doc_matches_allowset(doc_name: str, allowed: Optional[set]) -> bool:
        if allowed is None:
            return True
        if not allowed:
            return False
        doc_norm = doc_name.lower().strip()
        doc_base = doc_norm.rsplit(".", 1)[0] if "." in doc_norm else doc_norm
        return doc_norm in allowed or doc_base in allowed

    def _query_individual_obligations_vector_store(self, user_query: str, n_results: int = 20, document_ids: Optional[List[str]] = None, max_distance: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Query individual obligations vector store (new enhanced approach).
        """
        try:
            from vector_store import query_individual_obligations
            chroma_path = str(Path(self.local_output_folder) / "chroma_db")
            
            all_results = []
            
            if document_ids:
                # Query specific documents
                for doc_id in document_ids:
                    results = query_individual_obligations(
                        query_text=user_query,
                        n_results=n_results,
                        document_name=doc_id,
                        chroma_path=chroma_path,
                        max_distance=max_distance
                    )
                    all_results.extend(results)
            else:
                # Query all documents
                results = query_individual_obligations(
                    query_text=user_query,
                    n_results=n_results,
                    chroma_path=chroma_path,
                    max_distance=max_distance
                )
                all_results.extend(results)
            
            # Sort by distance (best matches first)
            all_results.sort(key=lambda x: x.get('distance', 999))
            
            self.logger.info(f"Found {len(all_results)} individual obligations matching query")
            
            return all_results[:n_results]  # Return top results
            
        except ImportError:
            self.logger.warning("Individual obligations vector store not available")
            return []
        except Exception as e:
            self.logger.error(f"Individual obligations vector query failed: {e}")
            return []

    async def _load_full_obligation_details_async(self, individual_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Load full obligation details from consolidated JSON files based on individual obligation results.
        """
        full_obligations = []
        
        # Group by document for efficient loading
        by_document = {}
        for result in individual_results:
            doc_name = result.get('document_name', '')
            if doc_name:
                if doc_name not in by_document:
                    by_document[doc_name] = []
                by_document[doc_name].append(result)
        
        # Load all consolidated data once (using existing synchronous method)
        all_consolidated_data = self.load_consolidated_jsons()
        
        # Create a lookup by document name for efficient access
        consolidated_by_doc = {}
        for entry in all_consolidated_data:
            doc_name = entry.get("document_name", "")
            payload = entry.get("data") if isinstance(entry, dict) else None
            if doc_name and isinstance(payload, dict):
                consolidated_by_doc[doc_name] = payload
        
        # Load consolidated data for each document
        for doc_name, doc_results in by_document.items():
            try:
                consolidated_data = consolidated_by_doc.get(doc_name)
                if not consolidated_data:
                    self.logger.warning(f"No consolidated data found for document: {doc_name}")
                    continue
                    
                # Find full obligations based on source_category and index
                for result in doc_results:
                    source_category = result.get('source_category', '')
                    chunk_index = result.get('chunk_index', '')
                    
                    # Find the obligation in consolidated data
                    full_obligation = self._find_obligation_in_consolidated_data(
                        consolidated_data, source_category, chunk_index, result
                    )
                    
                    if full_obligation:
                        # Add vector search metadata
                        full_obligation['_vector_metadata'] = {
                            'distance': result.get('distance'),
                            'auto_keywords': result.get('document', ''),
                            'chunk_index': chunk_index
                        }
                        full_obligations.append(full_obligation)
                        
            except Exception as e:
                self.logger.warning(f"Failed to load details for document {doc_name}: {e}")
                
        return full_obligations

    def _find_obligation_in_consolidated_data(self, consolidated_data: Dict, source_category: str, chunk_index: str, vector_result: Dict) -> Optional[Dict[str, Any]]:
        """
        Find the full obligation data from consolidated JSON.

        Returns a deep copy so we never mutate cached consolidated structures. Chroma's embedded
        ``document`` field (keyword blob for vectors) must not be written into Owner Responsibility
        when a real row exists — that produced junk text in API results.
        """
        def _finalize_row(ob: Dict[str, Any], category_label: str) -> Dict[str, Any]:
            o = copy.deepcopy(ob)
            label = (category_label or "").strip() or str(o.get("_processing_category") or o.get("category") or "").strip()
            if (source_category or "").strip():
                o["source_category"] = (source_category or "").strip()
            elif label:
                o["source_category"] = label
            else:
                o["source_category"] = "Uncategorized"
            return o

        def _synthetic_unresolved() -> Dict[str, Any]:
            """Last resort when metadata does not line up with consolidated JSON."""
            duty = (vector_result.get("DutyType") or "").strip()
            cit = vector_result.get("Citation") or ""
            # #region agent log
            try:
                import json as _agent_json, time as _agent_time

                _cs = str(cit)[:200] if cit is not None else ""
                with open(
                    r"c:\Users\AmithKrishnan(G1)XIN\Downloads\Legal-OCR\debug-fe1e15.log",
                    "a",
                    encoding="utf-8",
                ) as _agent_f:
                    _agent_f.write(
                        _agent_json.dumps(
                            {
                                "sessionId": "fe1e15",
                                "hypothesisId": "H2",
                                "location": "query_system._find_obligation._synthetic_unresolved",
                                "message": "synthetic fallback citation shape",
                                "data": {
                                    "cit_type": type(cit).__name__,
                                    "cit_head": _cs,
                                    "looks_like_repr": bool(
                                        isinstance(cit, str) and cit.lstrip().startswith("[{")
                                    ),
                                },
                                "timestamp": int(_agent_time.time() * 1000),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:
                pass
            # #endregion
            owner: List[str] = []
            cit_for_row: Any = cit
            parsed_meta = _parse_chroma_stored_citation_field(cit)
            if parsed_meta is not None:
                from vector_store import _citation_blob_for_chunk as _cit_blob

                blob = (_cit_blob({"citations": parsed_meta}) or "").strip()
                if duty:
                    owner.append(duty)
                if blob:
                    owner.append(blob[:800])
                elif not duty:
                    owner.append("Citation metadata present but could not be formatted as lease text.")
                cit_for_row = parsed_meta
            else:
                if duty:
                    owner.append(duty)
                if cit:
                    owner.append(str(cit)[:800])
            if not owner:
                owner = [
                    "Indexed obligation could not be matched to consolidated JSON (category/index metadata). "
                    "Re-run document processing and individual-obligation indexing."
                ]
            return {
                "Responsible Party": vector_result.get("Responsible_Party", ""),
                "DutyType": vector_result.get("DutyType", ""),
                "Owner Responsibility": owner,
                "Reasoning": [
                    "Vector metadata did not resolve to a consolidated obligation row; "
                    "embedding keywords are not shown as lease text."
                ],
                "Citation": cit_for_row,
                "source_category": (source_category or "").strip() or "Uncategorized",
            }

        try:
            results = consolidated_data.get('results', [])
            source_category_norm = (source_category or "").strip().lower()
            obligation_index = vector_result.get("obligation_index_in_category", "")
            try:
                obligation_index = int(obligation_index) if obligation_index not in ("", None) else None
            except (TypeError, ValueError):
                obligation_index = None
            try:
                global_index = int(chunk_index) if chunk_index not in ("", None) else None
            except (TypeError, ValueError):
                global_index = None

            consolidated_results = consolidated_data.get("consolidated_results") or []
            # #region agent log
            if not results and consolidated_results:
                try:
                    import json as _agent_json, time as _agent_time

                    with open(
                        r"c:\Users\AmithKrishnan(G1)XIN\Downloads\Legal-OCR\debug-fe1e15.log",
                        "a",
                        encoding="utf-8",
                    ) as _agent_f:
                        _agent_f.write(
                            _agent_json.dumps(
                                {
                                    "sessionId": "fe1e15",
                                    "hypothesisId": "H5",
                                    "location": "query_system._find_obligation_in_consolidated_data",
                                    "message": "consolidated results empty; using flat consolidated_results",
                                    "data": {
                                        "doc_name": str(consolidated_data.get("document_name") or "")[:160],
                                        "flat_count": len(consolidated_results),
                                        "chunk_index_raw": str(vector_result.get("chunk_index", ""))[:40],
                                    },
                                    "timestamp": int(_agent_time.time() * 1000),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                except Exception:
                    pass
            # #endregion

            # Try to find by source category and index (prefer per-category index)
            for category_data in results:
                category_name = category_data.get('category', '')
                if category_name.strip().lower() == source_category_norm:
                    obligations = category_data.get('obligations', [])
                    if obligation_index is not None and 0 <= obligation_index < len(obligations):
                        return _finalize_row(obligations[obligation_index], category_name)

            # Secondary pass: use global index across all categories (must match
            # ``flatten_obligations_for_individual_indexing``: only dict obligations are indexed).
            if global_index is not None:
                counter = 0
                for category_data in results:
                    category_name = category_data.get("category", "")
                    for ob in category_data.get('obligations', []):
                        if not isinstance(ob, dict):
                            continue
                        if counter == global_index:
                            return _finalize_row(ob, category_name)
                        counter += 1

            # Flat consolidated_results fallback (pagewise input): resolve by global index.
            if not results and consolidated_results and global_index is not None:
                if 0 <= global_index < len(consolidated_results):
                    ob = consolidated_results[global_index]
                    if isinstance(ob, dict):
                        category_label = (
                            str(ob.get("category") or source_category or "").strip() or "Other"
                        )
                        return _finalize_row(ob, category_label)

            # Match by party + duty type within category
            duty_type = (vector_result.get("DutyType") or "").strip().lower()
            party = (vector_result.get("Responsible_Party") or "").strip().lower()
            if source_category_norm:
                for category_data in results:
                    category_name = category_data.get('category', '')
                    if category_name.strip().lower() == source_category_norm:
                        for ob in category_data.get('obligations', []):
                            ob_party = (ob.get("Responsible Party") or "").strip().lower()
                            ob_duty = (ob.get("DutyType") or "").strip().lower()
                            if party and ob_party == party and (not duty_type or ob_duty == duty_type):
                                return _finalize_row(ob, category_name)

            # Final pass: match by party + duty type across all categories
            if party or duty_type:
                for category_data in results:
                    category_name = category_data.get("category", "")
                    for ob in category_data.get('obligations', []):
                        ob_party = (ob.get("Responsible Party") or "").strip().lower()
                        ob_duty = (ob.get("DutyType") or "").strip().lower()
                        if party and ob_party != party:
                            continue
                        if duty_type and ob_duty != duty_type:
                            continue
                        return _finalize_row(ob, category_name)

            # Flat consolidated_results fallback: match by party + duty.
            if not results and consolidated_results and (party or duty_type):
                for ob in consolidated_results:
                    if not isinstance(ob, dict):
                        continue
                    ob_party = (ob.get("Responsible Party") or "").strip().lower()
                    ob_duty = (ob.get("DutyType") or "").strip().lower()
                    if party and ob_party != party:
                        continue
                    if duty_type and ob_duty != duty_type:
                        continue
                    category_label = str(ob.get("category") or source_category or "").strip() or "Other"
                    return _finalize_row(ob, category_label)

            # #region agent log
            try:
                import json as _agent_json, time as _agent_time

                _n_cat = len(results) if isinstance(results, list) else 0
                with open(
                    r"c:\Users\AmithKrishnan(G1)XIN\Downloads\Legal-OCR\debug-fe1e15.log",
                    "a",
                    encoding="utf-8",
                ) as _agent_f:
                    _agent_f.write(
                        _agent_json.dumps(
                            {
                                "sessionId": "fe1e15",
                                "hypothesisId": "H3",
                                "location": "query_system._find_obligation_in_consolidated_data",
                                "message": "no consolidated match; using synthetic row",
                                "data": {
                                    "source_category": (source_category or "")[:200],
                                    "source_category_norm": source_category_norm[:200],
                                    "obligation_index": obligation_index,
                                    "global_index": global_index,
                                    "chunk_index_raw": str(vector_result.get("chunk_index", ""))[:40],
                                    "duty_type": (vector_result.get("DutyType") or "")[:120],
                                    "party": (vector_result.get("Responsible_Party") or "")[:120],
                                    "consolidated_top_level_categories": _n_cat,
                                },
                                "timestamp": int(_agent_time.time() * 1000),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:
                pass
            # #endregion
            return _synthetic_unresolved()

        except Exception as e:
            self.logger.warning(f"Error finding obligation in consolidated data: {e}")
            return None

    def _group_obligations_by_source_category(self, full_obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Group individual obligations back into categories for LLM processing.
        """
        by_category = {}
        
        for obligation in full_obligations:
            source_category = obligation.get('source_category', 'Uncategorized')
            
            if source_category not in by_category:
                by_category[source_category] = {
                    'category': source_category,
                    'obligations': []
                }
            
            by_category[source_category]['obligations'].append(obligation)
        
        return list(by_category.values())

    async def _process_obligations_with_llm_async(self, user_query: str, categorized_obligations: List[Dict[str, Any]], mode: str = "individual_obligations") -> Dict[str, Any]:
        """
        Process obligations with LLM for final filtering and ranking.
        This method creates the document name mapping and calls the appropriate merge_and_rank method.
        
        Args:
            user_query: The user's search query
            categorized_obligations: List of categorized obligations from _group_obligations_by_source_category
            mode: The processing mode ("individual_obligations" or "category_mode")
            
        Returns:
            Formatted results from LLM processing
        """
        # Create document name to ID mapping 
        document_name_to_id = {}
        for category in categorized_obligations:
            for obligation in category.get('obligations', []):
                doc_name = obligation.get('document_name', '')
                if doc_name and doc_name not in document_name_to_id:
                    document_name_to_id[doc_name] = len(document_name_to_id) + 1
        
        # Call the appropriate merge and rank method
        if mode == "category_mode":
            final_result = await self.merge_and_rank_results_category_mode(
                user_query, categorized_obligations, document_name_to_id
            )
        else:
            # For individual obligations mode, ensure payload matches merge_and_rank expectations.
            normalized_results = categorized_obligations
            if categorized_obligations and not any(
                isinstance(r, dict) and "results" in r for r in categorized_obligations
            ):
                normalized_results = [{
                    "document_name": "individual_obligations",
                    "results": categorized_obligations,
                }]
            final_result = await self.merge_and_rank_results(
                user_query, normalized_results, document_name_to_id
            )
            
        return final_result

    def _query_categories_vector_store(
        self,
        user_query: str,
        n_results: int = 20,  # Increased from 10 to capture more categories
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query categories by semantic similarity to aggregated category keywords.
        Returns matching categories which can then be used to fetch full category data.
        """
        try:
            from vector_store import query_categories
            chroma_path = str(Path(self.local_output_folder) / "chroma_db")
            
            if not Path(chroma_path).exists():
                self.logger.info("Category vector store path does not exist; no category-level search available")
                return []
            
            # Use more aggressive query expansion for category matching
            embed_query = _expand_query_for_vector_retrieval(user_query)
            
            # Additional category-specific expansion for broader matching
            if len(user_query.split()) <= 2:  # For short queries like "rent", "utilities"
                category_expansion_terms = "financial obligations duties responsibilities costs payments charges utilities maintenance"
                embed_query = f"{embed_query} {category_expansion_terms}"
            
            if embed_query != (user_query or "").strip():
                self.logger.info(
                    "Category query expanded for retrieval: %r -> longer phrase (%d chars)",
                    user_query,
                    len(embed_query),
                )
            
            # Category-level distance threshold (more lenient since we're matching broader concepts)
            max_dist_str = os.getenv("CATEGORY_MAX_DISTANCE", "2.0").strip()  # Increased from 1.6 to 2.0
            try:
                max_distance = float(max_dist_str) if max_dist_str else 2.0
            except ValueError:
                max_distance = 2.0
                
            self.logger.info(f"Category query: max_distance={max_distance}")
            
            # Query all documents if no filter, otherwise filter by document names
            matching_categories = []
            
            if document_ids:
                # Query each document separately if filtering by document_ids
                allowed = self._document_id_allowset(document_ids)
                for doc_name in allowed:
                    doc_categories = query_categories(
                        query_text=embed_query,
                        n_results=n_results,
                        document_name=doc_name,
                        chroma_path=chroma_path,
                        max_distance=max_distance,
                    )
                    matching_categories.extend(doc_categories)
            else:
                # Query across all documents  
                matching_categories = query_categories(
                    query_text=embed_query,
                    n_results=n_results * 5,  # Increased multiplier for more candidates across all docs
                    chroma_path=chroma_path,
                    max_distance=max_distance,
                )
            
            # Filter by allowed documents if specified
            if document_ids:
                allowed = self._document_id_allowset(document_ids)
                matching_categories = [
                    cat for cat in matching_categories
                    if self._document_allowed_by_ids(cat.get("document_name", ""), allowed)
                ]
            
            # Sort by distance (best matches first)
            matching_categories.sort(key=lambda x: x.get("distance", float("inf")))
            
            # Enhanced logging for debugging
            category_names_and_distances = [
                f"{cat.get('category_name', 'Unknown')}(d={cat.get('distance', 'N/A'):.3f})" 
                for cat in matching_categories[:10]
            ]
            self.logger.info(
                f"Category search found {len(matching_categories)} matching categories: {category_names_and_distances}"
            )
            
            # Log detailed category match info
            for i, cat in enumerate(matching_categories[:5]):
                self.logger.info(
                    f"Category {i+1}: '{cat.get('category_name')}' (distance: {cat.get('distance', 'N/A'):.3f}, "
                    f"obligations: {cat.get('obligations_count', 'N/A')}, doc: {cat.get('document_name')})"
                )
            
            return matching_categories[:n_results]
            
        except Exception as e:
            self.logger.error(f"Category vector search error: {e}", exc_info=True)
            return []

    def _get_full_categories_from_matches(
        self,
        category_matches: List[Dict[str, Any]],
        consolidated_files: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Convert category match results into full category data with all obligations.
        
        Args:
            category_matches: Results from _query_categories_vector_store
            consolidated_files: Optional pre-loaded consolidated data
            
        Returns:
            List of documents with full category data for matched categories
        """
        if not category_matches:
            return []
        
        if consolidated_files is None:
            consolidated_files = self.load_consolidated_jsons()
        
        # Build lookup: document_name -> consolidated data
        doc_to_data = {}
        for entry in consolidated_files:
            doc_name = entry.get("document_name", "")
            if doc_name:
                doc_to_data[doc_name] = entry
        
        # Group matches by document
        doc_to_matched_categories = {}
        for match in category_matches:
            doc_name = match.get("document_name", "")
            category_name = match.get("category_name", "")
            
            if doc_name and category_name:
                if doc_name not in doc_to_matched_categories:
                    doc_to_matched_categories[doc_name] = set()
                doc_to_matched_categories[doc_name].add(category_name)
        
        # Build result with full category data
        result = []
        for doc_name, matched_cat_names in doc_to_matched_categories.items():
            entry = doc_to_data.get(doc_name)
            if not entry:
                continue
                
            data = entry.get("data", {})
            all_categories = data.get("results", [])
            
            # Find matching categories and include their full obligation data
            matched_categories = []
            for category_data in all_categories:
                if isinstance(category_data, dict):
                    cat_name = category_data.get("category", "")
                    if cat_name in matched_cat_names:
                        # Convert obligations to merge format
                        obligations_for_merge = []
                        for ob in category_data.get("obligations", []):
                            if isinstance(ob, dict):
                                obligations_for_merge.append(_consolidated_obligation_to_merge_dict(ob, doc_name))
                        
                        if obligations_for_merge:
                            matched_categories.append({
                                "category": cat_name,
                                "obligations": obligations_for_merge
                            })
            
            if matched_categories:
                result.append({
                    "document_name": doc_name,
                    "results": matched_categories,
                    "consolidated_results": []
                })
                
                total_obligations = sum(len(cat.get('obligations', [])) for cat in matched_categories)
                self.logger.info(
                    f"Retrieved {len(matched_categories)} full categories from {doc_name}: {[cat['category'] for cat in matched_categories]} "
                    f"(total obligations: {total_obligations})"
                )
        
        return result
    
    def format_category_results_directly(self, user_query: str, full_categories: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Format category results directly without LLM filtering.
        Trust the semantic matching and return all matched categories.
        """
        try:
            if not full_categories:
                return {
                    "query": user_query,
                    "total_documents_searched": 0,
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                    "query_method": "category_semantic_no_llm_filter"
                }
            
            # Collect all categories from all documents
            all_results = []
            total_obligations = 0
            documents_searched = len(full_categories)
            
            for doc_data in full_categories:
                categories = doc_data.get("results", [])
                
                for category_data in categories:
                    if isinstance(category_data, dict):
                        category_name = category_data.get("category", "")
                        obligations = category_data.get("obligations", [])
                        
                        if obligations:  # Only include categories that have obligations
                            # Format obligations properly
                            formatted_obligations = []
                            for obligation in obligations:
                                if isinstance(obligation, dict):
                                    # Ensure proper structure
                                    formatted_ob = {
                                        "Responsible Party": obligation.get("Responsible Party", ""),
                                        "Owner Responsibility": obligation.get("Owner Responsibility", []),
                                        "Reasoning": obligation.get("Reasoning", []),
                                        "citations": obligation.get("citations", [])
                                    }
                                    formatted_obligations.append(formatted_ob)
                            
                            if formatted_obligations:
                                all_results.append({
                                    "category": category_name,
                                    "obligations": formatted_obligations
                                })
                                total_obligations += len(formatted_obligations)
            
            # Build final response
            final_result = {
                "query": user_query,
                "total_documents_searched": documents_searched,
                "total_obligations_found": total_obligations,
                "total_categories": len(all_results),
                "results": all_results,
                "processed_at": datetime.now().isoformat(),
                "query_method": "category_semantic_no_llm_filter"
            }
            
            self.logger.info(f"Direct category formatting: {len(all_results)} categories, {total_obligations} obligations (no LLM filtering)")
            
            return final_result
            
        except Exception as e:
            self.logger.error(f"Direct category formatting error: {e}", exc_info=True)
            return {
                "query": user_query,
                "total_documents_searched": 0,
                "total_obligations_found": 0,
                "total_categories": 0,
                "results": [],
                "processed_at": datetime.now().isoformat(),
                "error": str(e),
                "query_method": "category_semantic_no_llm_filter"
            }

    def _query_vector_store(
        self,
        user_query: str,
        n_results: int = 100,
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Primary retrieval: embed the user query and run semantic search in Chroma against each
        obligation's embedded chunk text (see vector_store.obligation_to_keyword_chunk_text;
        default: related_keywords only when present).
        Hits with distance <= VECTOR_MAX_DISTANCE; optional Responsible_Party filter for short
        queries that mention only tenant or only landlord (matches lowercase/Title Case metadata).
        Resolves full rows from consolidated JSON (document_name +
        chunk_index). Then **topic-augment**: append any consolidated obligation for the same
        document where ``_query_matches_obligation_topic`` (related_keywords + body) matches, so
        low-ranked chunks are not the only path into merge. **Category expansion** (separate
        step) then replaces each hit category with the full category from consolidated JSON.
        Returns [] if Chroma is missing, errors, or finds
        no hits — the pipeline then uses per-document LLM filter + merge.
        """
        query_lower = user_query.lower()
        detected_party = None
        word_count = len(query_lower.split())
        # Short queries: optional party metadata filter in Chroma. Skip when both parties appear
        # (ambiguous scope). Longer queries: never force a single party — topic stays semantic.
        if word_count < 3:
            if "tenant" in query_lower and "landlord" in query_lower:
                self.logger.info(
                    "Query mentions both tenant and landlord; skipping Responsible_Party metadata filter"
                )
            elif "tenant" in query_lower:
                detected_party = "Tenant"
                self.logger.info("Detected 'tenant' in query → filtering by Responsible_Party=Tenant")
            elif "landlord" in query_lower:
                detected_party = "Landlord"
                self.logger.info("Detected 'landlord' in query → filtering by Responsible_Party=Landlord")
        elif "tenant" in query_lower or "landlord" in query_lower:
            self.logger.info(
                f"Query has {word_count} words; skipping automatic Responsible_Party filter "
                f"(party keywords still in embedding for semantic retrieval)"
            )

        try:
            from vector_store import query_obligations
            chroma_path = str(Path(self.local_output_folder) / "chroma_db")
            if not Path(chroma_path).exists():
                self.logger.info("Vector store path does not exist; skipping semantic retrieval (index after processing)")
                return []

            embed_query = _expand_query_for_vector_retrieval(user_query)
            if embed_query != (user_query or "").strip():
                self.logger.info(
                    "Vector query expanded for retrieval: %r -> longer synonym phrase (%d chars)",
                    user_query,
                    len(embed_query),
                )

            # Distance threshold: only results with distance <= VECTOR_MAX_DISTANCE (default 1.4)
            max_dist_str = os.getenv("VECTOR_MAX_DISTANCE", "1.4").strip()
            try:
                max_distance = float(max_dist_str) if max_dist_str else 1.4
            except ValueError:
                max_distance = 1.4
            self.logger.info(f"Vector query: max_distance={max_distance} (from VECTOR_MAX_DISTANCE)")
            raw = query_obligations(
                query_text=embed_query,
                n_results=500,
                document_name=None,
                chroma_path=chroma_path,
                max_distance=max_distance,
                responsible_party=detected_party,
            )
            allowed = self._document_id_allowset(document_ids)
            if document_ids and allowed is not None:
                filtered = []
                for r in raw:
                    doc_name = (r.get("document_name") or "").strip()
                    if self._doc_matches_allowset(doc_name, allowed):
                        filtered.append(r)
                raw = filtered
            if not raw:
                self.logger.info(
                    "Vector store returned no obligations (re-index output/chroma_db after processing, "
                    "or relax VECTOR_MAX_DISTANCE)."
                )
                return []
            # Load consolidated JSONs to resolve full obligation by document_name + chunk_index
            loaded = self.load_consolidated_jsons()
            doc_to_results: Dict[str, List[Dict[str, Any]]] = {}
            for entry in loaded:
                data = entry.get("data") or {}
                doc_name = (data.get("document_name") or "").strip()
                results = obligations_from_consolidated_json(data)
                if doc_name:
                    doc_to_results[doc_name] = results
            skipped_stale_chroma = 0
            candidates: List[Tuple[float, Dict[str, Any]]] = []
            for r in raw:
                doc_name = (r.get("document_name") or "").strip()
                # Ignore Chroma rows for documents no longer present in output (stale index entries).
                if doc_to_results and doc_name not in doc_to_results:
                    skipped_stale_chroma += 1
                    continue
                try:
                    idx = int(r.get("chunk_index") or 0)
                except (TypeError, ValueError):
                    idx = 0
                try:
                    dist_f = float(r.get("distance")) if r.get("distance") is not None else 1e9
                except (TypeError, ValueError):
                    dist_f = 1e9
                results = doc_to_results.get(doc_name)
                if results and 0 <= idx < len(results):
                    full_ob = results[idx]
                    cat_chk = full_ob.get("category") or full_ob.get("_processing_category")
                    if not (cat_chk and str(cat_chk).strip()):
                        self.logger.warning(
                            "Vector hit obligation missing category (chunk_index=%s doc=%s); defaulting to Other",
                            idx,
                            doc_name,
                        )
                    ob_dict = _consolidated_obligation_to_merge_dict(full_ob, doc_name)
                    candidates.append((dist_f, ob_dict))
                else:
                    # Fallback: build from metadata when consolidated lookup fails
                    citation = r.get("Citation") or ""
                    if doc_name:
                        citation = f"Document: {doc_name} | {citation}"
                    self.logger.warning(
                        "Vector hit could not resolve consolidated obligation (chunk_index=%s doc=%s); defaulting category to Other",
                        idx,
                        doc_name,
                    )
                    candidates.append(
                        (
                            dist_f,
                            {
                                "document_name": doc_name,
                                "DutyType": r.get("DutyType") or "",
                                "Responsible Party": r.get("Responsible_Party") or "",
                                "Owner Responsibility": [r.get("document") or ""] if r.get("document") else [],
                                "Reasoning": [],
                                "Citation": citation,
                                "category": "Other",
                            },
                        )
                    )
            candidates = augment_vector_candidates_with_topic_matches(
                candidates,
                user_query,
                doc_to_results,
                logger=self.logger,
            )
            top_k = max(n_results, 200) if _is_short_focused_query(user_query) else max(n_results, 150)
            obligations = diversify_vector_obligations_for_merge_input(candidates, top_k)
            if skipped_stale_chroma:
                self.logger.info(
                    "Skipped %d vector hit(s) whose document_name is not in loaded consolidated JSON "
                    "(remove output/chroma_db or re-process all documents to clear stale Chroma rows).",
                    skipped_stale_chroma,
                )
            return obligations
        except Exception as e:
            self.logger.warning(f"Vector store query failed; pipeline will use LLM filter if needed: {e}")
            return []

    def _merge_obligations_from_consolidated_topic_only(
        self,
        user_query: str,
        consolidated_entries: List[Dict[str, Any]],
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Used when Chroma returns no rows or errors. Scan consolidated JSON and emit merge-shaped
        obligation dicts where ``_query_matches_obligation_topic`` is True (same matching as vector
        augmentation). Then ``grouped_merge_input_from_vector_obligations`` +
        ``expand_grouped_vector_merge_to_full_categories`` run so merge sees whole categories, not
        the smaller LLM-filter path.

        Disable with LEGAL_OCR_CONSOLIDATED_TOPIC_FALLBACK=false.
        """
        v = (os.getenv("LEGAL_OCR_CONSOLIDATED_TOPIC_FALLBACK") or "true").strip().lower()
        if v in ("0", "false", "no", "off"):
            return []
        allowed = self._document_id_allowset(document_ids)
        out: List[Dict[str, Any]] = []
        for entry in consolidated_entries or []:
            if not isinstance(entry, dict):
                continue
            data = entry.get("data") or {}
            doc_name = (entry.get("document_name") or data.get("document_name") or "").strip()
            if not doc_name:
                continue
            if document_ids and allowed is not None and not self._doc_matches_allowset(doc_name, allowed):
                continue
            flat = obligations_from_consolidated_json(data)
            for full_ob in flat:
                if not isinstance(full_ob, dict):
                    continue
                cat = str(full_ob.get("_processing_category") or full_ob.get("category") or "").strip()
                if not _query_matches_obligation_topic(user_query, full_ob, cat):
                    continue
                out.append(_consolidated_obligation_to_merge_dict(full_ob, doc_name))
        if out:
            self.logger.info(
                "Consolidated topic fallback: %d obligation row(s) aligned with query (Chroma empty/unusable — same pipeline as vector hits)",
                len(out),
            )
        return out

    async def filter_obligations_by_query(self, user_query: str, consolidated_data: Dict[str, Any],
                                   document_name: str) -> Dict[str, Any]:
        """
        Step 1: Filter obligations from a single consolidated JSON based on user query (ASYNC)
        
        Args:
            user_query: User's search query
            consolidated_data: Single consolidated JSON data
            document_name: Name of the source document
            
        Returns:
            Filtered JSON with only relevant obligations
        """
        try:
            t0 = time.perf_counter()
            self.logger.info(f"[TIMING] Step: filter_obligations_by_query - start for doc '{document_name}'")
            res_tree = consolidated_data.get("results")
            grouped_nonempty = (
                isinstance(res_tree, list)
                and bool(res_tree)
                and any(
                    isinstance(g, dict) and isinstance(g.get("obligations"), list) and g["obligations"]
                    for g in res_tree
                )
            )

            if grouped_nonempty:
                uq_json = json.dumps(user_query, ensure_ascii=False)
                doc_json = json.dumps(document_name, ensure_ascii=False)
                filter_inputs = json.dumps(
                    {"document_name": document_name, "results": res_tree},
                    indent=2,
                    ensure_ascii=False,
                )
                filter_prompt = f"""Filter lease obligations by user query. Keep relevant duties and nearby context when it helps interpretation.

RULE: Judge relevance primarily from "Owner Responsibility" and "Reasoning" content. Category names are supportive context, not the main reason to keep.

KEEP obligations if ANY duty relates to the query topic, using broad legal interpretation.
For "rent" queries: include base rent, holdover rent, percentage rent, rent abatement, utility rents and charges, payment timing, etc.

PRESERVE structure exactly - never merge different "Responsible Party" values.

OUTPUT: {{ "document_name": {doc_json}, "query": {uq_json}, "results": [ /* filtered categories with obligations */ ] }}

User query: {uq_json}

INPUT:
{filter_inputs}

Filtered JSON:"""
            else:
                obligations = obligations_from_consolidated_json(consolidated_data)
                if not obligations:
                    self.logger.warning(f"No obligations found in {document_name}")
                    return self._create_empty_response(document_name, consolidated_data)

                filter_prompt = f"""Filter obligations by user query. Keep relevant duties and nearby context when it helps interpretation.

MATCHING: Check if "Owner Responsibility" or "Reasoning" content relates to the query. 
For "rent": include base rent, holdover rent, percentage rent, rent abatement, utility rents and charges, payment methods, etc.

Return obligations exactly as provided - do not modify content.

User Query: "{user_query}"
Document: {document_name}

Obligations:
{json.dumps(obligations, indent=2)}

JSON output:
{{
  "document_name": "{document_name}",
  "query": "{user_query}",
  "consolidated_results": [ /* relevant obligations only */ ]
}}

Output the filtered JSON:"""

            self.logger.info(f"Filtering obligations from {document_name} for query: '{user_query}'")
            
            # Call LLM API asynchronously (Azure OpenAI or Gemini)
            t_gemini = time.perf_counter()
            response = await self._generate_content_async(
                prompt=filter_prompt,
                temperature=0.1,
                response_mime_type="application/json"
            )
            self.logger.info(f"[TIMING] Step: filter_obligations_by_query - LLM API call for '{document_name}' took {time.perf_counter() - t_gemini:.3f}s")
            
            # Parse JSON response
            result_text = response.text.strip()
            
            # Handle markdown code blocks if present
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            
            result_text = result_text.strip()
            
            # Parse JSON
            filtered_result = json.loads(result_text)
            if grouped_nonempty:
                filtered_result.setdefault("results", [])
                filtered_result.setdefault("document_name", document_name)
                filtered_result["consolidated_results"] = []
                num_results = count_obligations_in_results(filtered_result.get("results") or [])
            else:
                if "consolidated_results" not in filtered_result:
                    filtered_result["consolidated_results"] = []
                filtered_result.setdefault("results", [])
                num_results = len(filtered_result.get("consolidated_results", []))
            elapsed = time.perf_counter() - t0
            self.logger.info(f"[TIMING] Step: filter_obligations_by_query - done for '{document_name}' in {elapsed:.3f}s ({num_results} obligations)")
            return filtered_result
            
        except Exception as e:
            self.logger.error(f"Error filtering obligations from {document_name}: {e}")
            return self._create_empty_response(document_name, consolidated_data)
    
    def _create_empty_response(self, document_name: str, consolidated_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create an empty response structure"""
        return {
            "document_name": document_name,
            "results": [],
            "consolidated_results": [],
        }
    
    def _parse_citation(self, citation_str: str, doc_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Parse citation string into structured format
        
        Args:
            citation_str: Citation string like "Page 1, Section 1(e) and Section 2; Page 2, Section 5(a)"
            doc_id: Document ID (URL) to use in the citation
        
        Returns:
            List of citation objects with docId, pageNumbers, and section
        """
        citations = []
        
        # Split by "|" to separate document name from citation details
        parts = citation_str.split("|")
        
        if len(parts) >= 2:
            citation_detail = parts[1].strip()
        else:
            citation_detail = citation_str.strip()
        
        # Extract ALL page numbers - find all occurrences of "Page X"
        page_pattern = r'Page[s]?\s+(\d+)'
        page_matches = re.findall(page_pattern, citation_detail, re.IGNORECASE)
        page_numbers = [int(p) for p in page_matches]
        
        # Split citation by semicolon to handle multiple page groups
        # Format: "Page 1, Section 1(e) and Section 2; Page 2, Section 5(a)"
        page_groups = re.split(r';\s*', citation_detail)
        
        all_sections = []
        
        for group in page_groups:
            group = group.strip()
            if not group:
                continue
            
            # Remove the "Page X, " part to get just the sections part
            group_without_page = re.sub(r'^Page[s]?\s+\d+\s*,?\s*', '', group, flags=re.IGNORECASE).strip()
            
            if group_without_page:
                # Keep all "Section" prefixes - don't remove any
                # Just clean up the text and use as-is
                section_text = group_without_page.strip()
                # Clean up trailing commas
                section_text = section_text.rstrip(',').strip()
                
                if section_text and section_text not in all_sections:
                    all_sections.append(section_text)
        
        # If no sections found with "Section" keyword, try to extract patterns directly
        if not all_sections:
            # Look for patterns like "1(e)", "5(a)", "12. Utilities (a)"
            section_pattern2 = r'(\d+\([a-z]\)|\d+[a-z]|[a-z]\d+|\d+\.\s*[A-Z][^,;|]+)'
            section_matches2 = re.findall(section_pattern2, citation_detail)
            all_sections = [s.strip() for s in section_matches2 if s.strip()]
        
        # Create citation object
        citation_obj = {}
        if doc_id:
            citation_obj["docId"] = doc_id
        if page_numbers:
            citation_obj["pageNumbers"] = sorted(list(set(page_numbers)))  # Remove duplicates and sort
        else:
            citation_obj["pageNumbers"] = []
        if all_sections:
            citation_obj["section"] = all_sections
        else:
            citation_obj["section"] = []
        
        # Always return at least one citation object if we have a doc_id
        if doc_id or page_numbers or all_sections:
            citations.append(citation_obj)
        elif doc_id:
            # Return basic structure even if no page/section info
            citations.append({
                "docId": doc_id,
                "pageNumbers": [],
                "section": []
            })
        
        return citations
    
    def _build_merge_rank_prompt(self, user_query: str, filtered_results: List[Dict[str, Any]], category_mode: bool = False) -> str:
        """Build the merge-and-rank LLM prompt. Shared by /query, /query/stream, and /query/stream/raw so results are consistent."""
        non_empty_results = [r for r in filtered_results if _filtered_fr_has_payload(r)]
        prompt_payload = merge_prompt_filtered_snapshot(non_empty_results)
        uq = json.dumps(user_query, ensure_ascii=False)
        ndocs = len(filtered_results)
        return f"""You are an expert commercial contract analyst and context extraction engine.

INPUT:
Array of per-document objects containing categorized legal obligations.
{{
  "results": [
    {{
      "category": "<string>",
      "obligations": [
        {{
          "Responsible Party": "<string>",
          "Owner Responsibility": [<strings>],
          "Reasoning": [<strings>],
          "citations": [<objects>]
        }}
      ]
    }}
  ]
}}

OBJECTIVE:
Return a comprehensive set of obligations relevant to the user's query. You must capture the ENTIRE functional ecosystem of the queried concept, including related rights, remedies, financial penalties, and execution mechanics.

RELEVANCE GUARDRAILS (STRICT):
1. **Null/Empty Query:** If the user query is "null", "none", empty, or consists only of whitespace, you MUST return a JSON object with an empty `results` array.
2. **Atomic Filtering:** Evaluate every single string inside "Owner Responsibility" independently. If one line is relevant, KEEP it. If the next line in the same object is NOT relevant, you MUST DROP it. Do not keep a line just because it is bundled with a relevant one.
3. **Boilerplate vs. Specificity:** Do not keep generic lease obligations (e.g., general maintenance, HVAC, or insurance) unless they specifically mention the query or its direct functional synonyms.

RELEVANCE RULE (THE "ECOSYSTEM" APPROACH):
Keep an "Owner Responsibility" line ONLY if it touches ANY of the following as it relates specifically to the query:
  1. Direct Subject: Mentions the query or its direct synonyms.
  2. Mechanics & Operations: How the concept is performed, paid, calculated, or enforced.
  3. Rights, Remedies & Offsets: Legal rights (e.g., abatement, withholding) triggered by the concept.
  4. Penalties & Defaults: Consequences of failing the obligation (e.g., late fees, interest).

FILTERING STRATEGY (THE ALGORITHMIC CHECKLIST):
Apply this test to every string in the "Owner Responsibility" array:
1. Is the query "null"? -> DELETE ALL.
2. Does this specific line explicitly contain the query or a term functionally inseparable from it? -> If YES, KEEP.
3. Does this specific line describe the payment, interest, or penalty logic for the query? -> If YES, KEEP.
4. Is this line about a different topic (e.g., HVAC, plumbing, or security) even if it's in a 'Rent' category? -> If YES, DELETE.
5. If the string answers NO to questions 2 and 3 -> DELETE IT.

CRITICAL RULE FOR RUN-ON SENTENCES:
If a single string contains both relevant and irrelevant info (e.g., a sentence covering both Rent and Janitorial services), KEEP the whole string. But if they are separate strings in the array, you MUST filter them individually.

REASONING HANDLING:
- Retain ONLY the "Reasoning" lines that logically support the "Owner Responsibility" lines you chose to KEEP. 
- If you delete a responsibility line about HVAC, you MUST delete the reasoning line about HVAC.

STRUCTURE PRESERVATION:
- Keep category names, parties, and citations EXACTLY as provided.
- If an obligation's "Owner Responsibility" array becomes empty after filtering, remove that entire obligation object.

OUTPUT FORMAT:
{{
  "query": "{uq}",
  "total_documents_searched": {ndocs},
  "total_obligations_found": <int>,
  "total_categories": <int>,
  "results": [
    {{
      "category": "<category name>",
      "obligations": [
        {{
          "Responsible Party": "<party>",
          "Owner Responsibility": ["<filtered line 1>"],
          "Reasoning": ["<relevant reason A>"],
          "citations": [<original citation objects>]
        }}
      ]
    }}
  ]
}}

User query: {uq}

INPUT:
{json.dumps(prompt_payload, indent=2)}

Output only the JSON object:"""
    async def merge_and_rank_results_stream(self, user_query: str, filtered_results: List[Dict[str, Any]], 
                              document_name_to_id: Optional[Dict[str, str]] = None) -> AsyncIterator[Dict[str, Any]]:
        """
        Streams merge JSON: each parsed top-level element of "results" is a category group
        { "category", "obligations" }, then final metadata with counts.
        """
        try:
            # Filter out empty results
            non_empty_results = [r for r in filtered_results if _filtered_fr_has_payload(r)]
            
            if not non_empty_results:
                yield {
                    "type": "metadata",
                    "data": {
                        "query": user_query,
                        "total_documents_searched": len(filtered_results),
                        "total_obligations_found": 0,
                        "total_categories": 0,
                        "processed_at": datetime.now().isoformat(),
                    },
                }
                return

            merge_in, merge_orig = cap_merge_filtered_results(
                non_empty_results,
                _merge_max_input_obligations_for_query(user_query, document_blocks=len(non_empty_results)),
            )
            if merge_orig > _count_obligations_in_filtered(merge_in):
                self.logger.info(
                    "[STREAM] Merge input capped: %d -> %d obligations",
                    merge_orig,
                    _count_obligations_in_filtered(merge_in),
                )

            if coherence_query_unsupported_by_sources(user_query, merge_in):
                yield {
                    "type": "metadata",
                    "data": {
                        "query": user_query,
                        "total_documents_searched": len(filtered_results),
                        "total_obligations_found": 0,
                        "total_categories": 0,
                        "processed_at": datetime.now().isoformat(),
                    },
                }
                return

            if _skip_merge_and_rank_for_testing():
                self.logger.info(
                    "[STREAM] SKIP_MERGE_AND_RANK=true: skipping merge/rank LLM (%d obligations in merge input)",
                    _count_obligations_in_filtered(merge_in),
                )
                payload_stream = build_rank_response_without_llm_merge(
                    user_query,
                    merge_in,
                    documents_searched_count=len(filtered_results),
                    merge_fallback=False,
                )
                note = (payload_stream.get("merge_note") or "").strip()
                skip_msg = "Testing: merge/rank LLM skipped (SKIP_MERGE_AND_RANK)."
                payload_stream["merge_note"] = f"{note} {skip_msg}".strip() if note else skip_msg
                for grp in payload_stream.get("results") or []:
                    if isinstance(grp, dict):
                        yield {"type": "category_group", "data": grp}
                meta: Dict[str, Any] = {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": int(payload_stream.get("total_obligations_found") or 0),
                    "total_categories": int(payload_stream.get("total_categories") or 0),
                    "processed_at": datetime.now().isoformat(),
                }
                if (payload_stream.get("merge_note") or "").strip():
                    meta["merge_note"] = (payload_stream.get("merge_note") or "").strip()
                yield {"type": "metadata", "data": meta}
                return

            merge_prompt = self._build_merge_rank_prompt(user_query, merge_in)
            self.logger.info("[STREAM] Merge/rank LLM call - streaming response from LLM")
            from streaming_json_parser import parse_obligations_stream

            token_stream = generate_content_stream(
                prompt=merge_prompt,
                model=self.model,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=_merge_rank_max_output_tokens(),
            )
            streamed: List[Dict[str, Any]] = []
            async for chunk in parse_obligations_stream(token_stream):
                if isinstance(chunk, dict):
                    streamed.append(chunk)

            payload_stream: Dict[str, Any] = {"results": streamed, "query": user_query}
            apply_query_coherence_to_payload(user_query, payload_stream)
            normalize_query_response_shape(payload_stream, merge_in)
            n_stream = int(payload_stream.get("total_obligations_found") or 0)
            n_in = _count_obligations_in_filtered(merge_in)
            if n_stream == 0 and n_in > 0:
                self.logger.info(
                    "[STREAM] merge produced zero obligations after coherence/normalize (input had %d); leaving merged payload as-is (no retrieval fallback)",
                    n_in,
                )

            for grp in payload_stream.get("results") or []:
                if isinstance(grp, dict):
                    yield {"type": "category_group", "data": grp}

            yield {
                "type": "metadata",
                "data": {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": int(payload_stream.get("total_obligations_found") or 0),
                    "total_categories": int(payload_stream.get("total_categories") or 0),
                    "processed_at": datetime.now().isoformat(),
                },
            }
            
        except Exception as e:
            import traceback
            self.logger.error(f"Error in streaming merge_and_rank: {e}")
            self.logger.error(traceback.format_exc())
            yield {
                "type": "error",
                "message": str(e)
            }

    async def merge_and_rank_results(self, user_query: str, filtered_results: List[Dict[str, Any]], 
                              document_name_to_id: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Step 3: Merge all filtered results and rank by relevance and monetary value (ASYNC)
        
        Args:
            user_query: User's search query
            filtered_results: List of filtered results from all documents
            document_name_to_id: Optional mapping of document names to document IDs (URLs)
            
        Returns:
            Single merged and ranked JSON
        """
        try:
            t0 = time.perf_counter()
            self.logger.info("[TIMING] merge_and_rank: start")
            # 1. Filter out empty results
            t_step = time.perf_counter()
            non_empty_results = [r for r in filtered_results if _filtered_fr_has_payload(r)]
            self.logger.info(f"[TIMING] merge_and_rank: 1. filter_empty_results - {time.perf_counter() - t_step:.3f}s")
            
            if not non_empty_results:
                self.logger.info("No relevant obligations found across all documents")
                return {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                }

            merge_in, merge_orig = cap_merge_filtered_results(
                non_empty_results,
                _merge_max_input_obligations_for_query(user_query, document_blocks=len(non_empty_results)),
            )
            if merge_orig > _count_obligations_in_filtered(merge_in):
                self.logger.info(
                    "[TIMING] merge_and_rank: cap_merge_input %d -> %d obligations",
                    merge_orig,
                    _count_obligations_in_filtered(merge_in),
                )

            if coherence_query_unsupported_by_sources(user_query, merge_in):
                self.logger.info(
                    "merge_and_rank: coherence — query topic not found in merge input; skipping LLM merge"
                )
                return {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                }

            if _skip_merge_and_rank_for_testing():
                self.logger.info(
                    "SKIP_MERGE_AND_RANK=true: skipping merge/rank LLM (%d obligations in merge input)",
                    _count_obligations_in_filtered(merge_in),
                )
                final_result = build_rank_response_without_llm_merge(
                    user_query,
                    merge_in,
                    documents_searched_count=len(filtered_results),
                    merge_fallback=False,
                )
                note = (final_result.get("merge_note") or "").strip()
                skip_msg = "Testing: merge/rank LLM skipped (SKIP_MERGE_AND_RANK)."
                final_result["merge_note"] = f"{note} {skip_msg}".strip() if note else skip_msg
                final_result["total_documents_searched"] = len(filtered_results)
                final_result["processed_at"] = datetime.now().isoformat()
                num_results = int(final_result.get("total_obligations_found") or 0)
                self.logger.info(
                    "[TIMING] merge_and_rank: total - %.3fs (%d obligations, merge LLM skipped)",
                    time.perf_counter() - t0,
                    num_results,
                )
                return final_result

            # 2. Build full merge+rank+filter prompt (no code merge; LLM does filtering, merging, ranking)
            t_step = time.perf_counter()
            merge_prompt = self._build_merge_rank_prompt(user_query, merge_in)
            self.logger.info(f"[TIMING] merge_and_rank: 2. build_prompt - {time.perf_counter() - t_step:.3f}s")
            self.logger.info(f"Merge and rank (LLM) for query: '{user_query}'")
            
            # 3. Call LLM API asynchronously (filter + merge + rank in one call)
            t_step = time.perf_counter()
            response = await self._generate_content_async(
                prompt=merge_prompt,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=_merge_rank_max_output_tokens(),
            )
            self.logger.info(f"[TIMING] merge_and_rank: 3. llm_api_call - {time.perf_counter() - t_step:.3f}s")
            
            # 4. Parse JSON (tolerant) or empty shape on failure (no retrieval fallback)
            t_step = time.perf_counter()
            result_text = response.text.strip()
            try:
                final_result = parse_llm_json_object(result_text)
            except ValueError as e:
                self.logger.warning(
                    "merge/rank JSON parse failed (%s); returning empty merge result (no retrieval fallback). Response tail: %r",
                    e,
                    result_text[-500:],
                )
                final_result = _empty_merge_rank_result(
                    user_query,
                    documents_searched_count=len(filtered_results),
                    merge_note=f"Merge response could not be parsed as JSON: {e}",
                )
            self.logger.info(f"[TIMING] merge_and_rank: 4. parse_response - {time.perf_counter() - t_step:.3f}s")
            final_result.setdefault("query", user_query)
            
            # 5. Convert citations to structured format (in case LLM returned string)
            t_step = time.perf_counter()
            convert_result_citations_to_structured(final_result, merge_in)
            self.logger.info(f"[TIMING] merge_and_rank: 5. citation_to_structured - {time.perf_counter() - t_step:.3f}s")
            
            apply_query_coherence_to_payload(user_query, final_result)
            t_norm = time.perf_counter()
            normalize_query_response_shape(final_result, merge_in)
            self.logger.info(f"[TIMING] merge_and_rank: 6. normalize_shape - {time.perf_counter() - t_norm:.3f}s")

            n_after = int(final_result.get("total_obligations_found") or 0)
            n_in = _count_obligations_in_filtered(merge_in)
            if n_after == 0 and n_in > 0:
                self.logger.info(
                    "merge_and_rank returned zero obligations after coherence/normalize (input had %d); leaving merged result as-is (no retrieval fallback)",
                    n_in,
                )

            final_result["total_documents_searched"] = len(filtered_results)
            final_result["processed_at"] = datetime.now().isoformat()
            num_results = int(final_result.get("total_obligations_found") or 0)
            self.logger.info(f"[TIMING] merge_and_rank: total - {time.perf_counter() - t0:.3f}s ({num_results} obligations)")
            return final_result
            
        except Exception as e:
            self.logger.error(f"Error merging and ranking results: {e}")
            return {
                "query": user_query,
                "total_documents_searched": len(filtered_results),
                "total_obligations_found": 0,
                "total_categories": 0,
                "results": [],
                "processed_at": datetime.now().isoformat(),
                "error": str(e)
            }
    
    async def merge_and_rank_results_category_mode(self, user_query: str, filtered_results: List[Dict[str, Any]],
                                  document_name_to_id: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Category-aware merge and rank that uses more inclusive prompting.
        """
        try:
            # Filter out empty results
            non_empty_results = [r for r in filtered_results if _filtered_fr_has_payload(r)]
            
            if not non_empty_results:
                return {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                }

            merge_in, merge_orig = cap_merge_filtered_results(
                non_empty_results,
                _merge_max_input_obligations_for_query(user_query, document_blocks=len(non_empty_results)),
            )
            if merge_orig > _count_obligations_in_filtered(merge_in):
                self.logger.info(
                    "[TIMING] merge_and_rank: cap_merge_input %d -> %d obligations",
                    merge_orig,
                    _count_obligations_in_filtered(merge_in),
                )

            if coherence_query_unsupported_by_sources(user_query, merge_in):
                self.logger.info(
                    "merge_and_rank: coherence — query topic not found in merge input; skipping LLM merge"
                )
                return {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                }

            if _skip_merge_and_rank_for_testing():
                self.logger.info(
                    "SKIP_MERGE_AND_RANK=true: skipping category merge/rank LLM (%d obligations in merge input)",
                    _count_obligations_in_filtered(merge_in),
                )
                final_result = build_rank_response_without_llm_merge(
                    user_query,
                    merge_in,
                    documents_searched_count=len(filtered_results),
                    merge_fallback=False,
                )
                note = (final_result.get("merge_note") or "").strip()
                skip_msg = "Testing: category merge/rank LLM skipped (SKIP_MERGE_AND_RANK)."
                final_result["merge_note"] = f"{note} {skip_msg}".strip() if note else skip_msg
                final_result["total_documents_searched"] = len(filtered_results)
                final_result["processed_at"] = datetime.now().isoformat()
                apply_query_scope_trim_to_results(user_query, final_result)
                return final_result

            self.logger.info("[TIMING] merge_and_rank: start")
            t_start = time.perf_counter()

            # 1. Filter out empty results
            t_step = time.perf_counter()
            self.logger.info(f"[TIMING] merge_and_rank: 1. filter_empty_results - {time.perf_counter() - t_step:.3f}s")

            # 2. Build merge prompt (CATEGORY MODE)
            t_step = time.perf_counter()
            merge_prompt = self._build_merge_rank_prompt(user_query, merge_in, category_mode=True)
            self.logger.info(f"[TIMING] merge_and_rank: 2. build_prompt - {time.perf_counter() - t_step:.3f}s")
            self.logger.info(f"Merge and rank (LLM CATEGORY MODE) for query: '{user_query}'")
            
            # 3. Call LLM API asynchronously (filter + merge + rank in one call)
            t_step = time.perf_counter()
            response = await self._generate_content_async(
                prompt=merge_prompt,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=_merge_rank_max_output_tokens(),
            )
            self.logger.info(f"[TIMING] merge_and_rank: 3. llm_api_call - {time.perf_counter() - t_step:.3f}s")

            # 4. Parse JSON (tolerant) or empty shape on failure (no retrieval fallback)
            t_step = time.perf_counter()
            result_text = response.text.strip()
            try:
                final_result = parse_llm_json_object(result_text)
            except ValueError as e:
                self.logger.warning(
                    "Category merge/rank JSON parse failed (%s); returning empty merge result (no retrieval fallback). Response tail: %r",
                    e,
                    result_text[-500:],
                )
                final_result = _empty_merge_rank_result(
                    user_query,
                    documents_searched_count=len(filtered_results),
                    merge_note=f"Merge response could not be parsed as JSON: {e}",
                )
            self.logger.info(f"[TIMING] merge_and_rank: 4. parse_response - {time.perf_counter() - t_step:.3f}s")
            final_result.setdefault("query", user_query)

            # 5. Convert citations to structured format (in case LLM returned string)
            t_step = time.perf_counter()
            convert_result_citations_to_structured(final_result, merge_in)
            self.logger.info(f"[TIMING] merge_and_rank: 5. citation_to_structured - {time.perf_counter() - t_step:.3f}s")

            # 6. Normalize response shape
            t_step = time.perf_counter()
            normalize_query_response_shape(final_result, merge_in)
            apply_query_scope_trim_to_results(user_query, final_result)
            apply_query_coherence_to_payload(user_query, final_result)
            self.logger.info(f"[TIMING] merge_and_rank: 6. normalize_shape - {time.perf_counter() - t_step:.3f}s")

            obligations_found = final_result.get("total_obligations_found", 0)
            self.logger.info(f"[TIMING] merge_and_rank: total - {time.perf_counter() - t_start:.3f}s ({obligations_found} obligations)")

            return final_result

        except Exception as e:
            self.logger.error(f"Category merge and rank error: {e}", exc_info=True)
            return {
                "query": user_query,
                "total_documents_searched": len(filtered_results),
                "total_obligations_found": 0,
                "total_categories": 0,
                "results": [],
                "processed_at": datetime.now().isoformat(),
                "error": str(e)
            }
    
    async def query_by_individual_obligations(self, user_query: str, save_output: bool = True, document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        NEW: Individual obligation-level semantic matching approach with auto-generated keywords.
        
        This is the ENHANCED approach that fixes the coarse-grained retrieval problem:
        1. Query individual obligations by semantic similarity to auto-generated keywords
        2. Each obligation is indexed separately with keywords from its responsibility lines  
        3. Return only the specific obligations that match (no noise from category-level retrieval)
        
        This approach provides much higher precision and eliminates irrelevant results.
        """
        try:
            query_start = time.perf_counter()
            self.logger.info("[TIMING] Individual obligation-based query - start")

            if not self._vector_store_available():
                self.logger.warning("vector_store module missing; using consolidated JSON LLM filter")
                return await self.query_legacy(
                    user_query,
                    save_output,
                    document_ids,
                    use_category_mode_override=False,
                )
            
            # Default query handling
            if not user_query or user_query.strip() == "":
                user_query = "utilities including water, gas, heat, light, electricity, telephone service, HVAC, sprinkler system, electrical and plumbing systems"
                self.logger.info("No query provided - defaulting to utilities query")
            
            self.logger.info("=" * 80)
            self.logger.info(f"Processing query (INDIVIDUAL OBLIGATIONS MODE): '{user_query}'")
            if document_ids:
                self.logger.info(f"Filtering by document_ids: {document_ids}")
            self.logger.info("=" * 80)
            
            # Step 1: Query individual obligations via semantic search
            t_individual_search = time.perf_counter()
            self.logger.info("[TIMING] Step: individual obligation semantic search - start")
            
            individual_results = self._query_individual_obligations_vector_store(
                user_query,
                n_results=30,  # Get more candidates for better results
                document_ids=document_ids,
                max_distance=2.0  # Filter for relevance
            )
            dt_individual_search = time.perf_counter() - t_individual_search

            self.logger.info(f"[TIMING] Step: individual obligation semantic search - done in {dt_individual_search:.3f}s ({len(individual_results)} obligations)")
            
            if not individual_results:
                self.logger.info("No matching individual obligations found; falling back to category-based approach")
                return await self.query_by_categories(user_query, save_output, document_ids)
            
            # Step 2: Load full obligation details for the matched individuals
            t_load_details = time.perf_counter()
            self.logger.info("[TIMING] Step: loading full obligation details - start")
            
            full_obligations = await self._load_full_obligation_details_async(individual_results)
            dt_load_details = time.perf_counter() - t_load_details

            self.logger.info(f"[TIMING] Step: loading full obligation details - done in {dt_load_details:.3f}s ({len(full_obligations)} full obligations)")
            
            # Step 3: Group by categories for LLM processing
            categorized_obligations = self._group_obligations_by_source_category(full_obligations)
            
            # Step 4: Process with LLM for final filtering and ranking
            t_llm_processing = time.perf_counter()
            self.logger.info("[TIMING] Step: LLM filtering and ranking - start")

            formatted_results = await self._process_obligations_with_llm_async(
                user_query, categorized_obligations, "individual_obligations"
            )
            dt_llm_processing = time.perf_counter() - t_llm_processing

            self.logger.info(
                "[TIMING] Step: LLM filtering and ranking - done in %.3fs",
                dt_llm_processing,
            )
            
            # Save output if requested
            if save_output:
                output_path = self._save_query_results(user_query, formatted_results, "individual_obligations")
                self.logger.info(f"Results saved to: {output_path}")
            
            query_elapsed = time.perf_counter() - query_start
            self.logger.info(f"[TIMING] Individual obligation-based query - total elapsed: {query_elapsed:.3f}s")
            
            return {
                "query": user_query,
                "results": formatted_results,
                "search_method": "individual_obligations",
                "total_found": len(individual_results),
                "after_llm_filtering": int(formatted_results.get("total_obligations_found") or 0),
                "query_time_seconds": query_elapsed,
                "metadata": {
                    "individual_search_time": dt_individual_search,
                    "load_details_time": dt_load_details,
                    "llm_processing_time": dt_llm_processing,
                    "document_filters": document_ids,
                },
            }
            
        except Exception as e:
            self.logger.error(f"Individual obligation query failed: {e}", exc_info=True)
            # Fallback to category-based approach
            self.logger.info("Falling back to category-based query due to error")
            return await self.query_by_categories(user_query, save_output, document_ids)

    async def query_by_categories(self, user_query: str, save_output: bool = True, document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        NEW: Category-level semantic matching approach.
        
        1. Query categories by semantic similarity to aggregated category keywords
        2. Retrieve full categories that match
        3. Send complete categories to LLM for filtering and ranking
        
        This approach lets the LLM handle fine-grained filtering within semantically relevant categories.
        """
        try:
            query_start = time.perf_counter()
            self.logger.info("[TIMING] Category-based query - start")

            if not self._vector_store_available():
                self.logger.warning("vector_store module missing; using consolidated JSON LLM filter")
                return await self.query_legacy(
                    user_query,
                    save_output,
                    document_ids,
                    use_category_mode_override=False,
                )
            
            # Default query handling
            if not user_query or user_query.strip() == "":
                user_query = "utilities including water, gas, heat, light, electricity, telephone service, HVAC, sprinkler system, electrical and plumbing systems"
                self.logger.info("No query provided - defaulting to utilities query")
            
            self.logger.info("=" * 80)
            self.logger.info(f"Processing query (CATEGORY MODE): '{user_query}'")
            if document_ids:
                self.logger.info(f"Filtering by document_ids: {document_ids}")
            self.logger.info("=" * 80)
            
            # Step 1: Find matching categories via semantic search
            t_category_search = time.perf_counter()
            self.logger.info("[TIMING] Step: category semantic search - start")
            
            category_matches = self._query_categories_vector_store(
                user_query, 
                n_results=50,  # Increased to get even more category candidates
                document_ids=document_ids
            )
            
            self.logger.info(f"[TIMING] Step: category semantic search - done in {time.perf_counter() - t_category_search:.3f}s ({len(category_matches)} categories)")
            
            if not category_matches:
                self.logger.info("No matching categories found; falling back to original query method")
                return await self.query(user_query, save_output, document_ids)
            
            # Step 2: Filter categories by distance (keep most relevant ones)
            # For "rent" queries, keep categories with distance <= 2.0 (adjust as needed)
            distance_threshold = float(os.getenv("CATEGORY_RESULT_DISTANCE_THRESHOLD", "2.0"))
            relevant_categories = [
                cat for cat in category_matches 
                if cat.get('distance', 999) <= distance_threshold
            ]
            
            self.logger.info(f"Filtered categories by distance <= {distance_threshold}: {len(relevant_categories)}/{len(category_matches)} categories")
            if relevant_categories:
                category_names = [cat.get('category_name', 'Unknown') for cat in relevant_categories]
                self.logger.info(f"Relevant categories: {category_names}")
            
            # Step 3: Retrieve full category data
            t_category_expansion = time.perf_counter()
            self.logger.info("[TIMING] Step: category data retrieval - start")
            
            full_categories = self._get_full_categories_from_matches(relevant_categories)
            
            self.logger.info(f"[TIMING] Step: category data retrieval - done in {time.perf_counter() - t_category_expansion:.3f}s")
            
            if not full_categories:
                return {
                    "query": user_query,
                    "total_documents_searched": 0,
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                    "error": "No full category data found for matches"
                }
            
            # Step 4: LLM merge and rank with precise duty-level filtering
            t_format = time.perf_counter()
            self.logger.info("[TIMING] Step: LLM merge_and_rank (precise filtering) - start")
            
            # Build document_name_to_id mapping
            document_name_to_id = {}
            if document_ids:
                for doc_id in document_ids:
                    d = (doc_id or "").strip()
                    if d:
                        name = Path(d).name or d
                        document_name_to_id[name] = doc_id
                        document_name_to_id[name.lower()] = doc_id
            
            # For category-based queries, temporarily increase merge limits to preserve full categories
            original_merge_limit = os.environ.get("MERGE_MAX_INPUT_OBLIGATIONS", "24")
            os.environ["MERGE_MAX_INPUT_OBLIGATIONS"] = "200"  # Much higher limit for category mode
            
            # Log what's being sent to merge/rank
            total_input_obligations = sum(
                len(doc.get("results", []))
                for doc in full_categories
                for cat in doc.get("results", [])
                for _ in cat.get("obligations", [])
            )
            input_categories = []
            for doc in full_categories:
                for cat in doc.get("results", []):
                    input_categories.append(cat.get("category", "Unknown"))
            
            self.logger.info(f"Sending to merge/rank: {len(input_categories)} categories, {total_input_obligations} total obligations")
            self.logger.info(f"Categories being merged: {input_categories}")
            
            # Build document_name_to_id mapping
            document_name_to_id = {}
            if document_ids:
                for doc_id in document_ids:
                    d = (doc_id or "").strip()
                    if d:
                        name = Path(d).name or d
                        document_name_to_id[name] = doc_id
                        document_name_to_id[name.lower()] = doc_id
            
            # Use improved LLM merge/rank with precise duty-level filtering
            final_result = await self.merge_and_rank_results_category_mode(user_query, full_categories, document_name_to_id)
            
            # Log what was returned
            output_categories = []
            if final_result.get("results"):
                for res in final_result["results"]:
                    if isinstance(res, dict):
                        output_categories.append(res.get("category", "Unknown"))
            
            self.logger.info(f"LLM merge/rank returned: {len(output_categories)} categories")
            self.logger.info(f"Categories returned: {output_categories}")
            
            # Log which categories were dropped
            dropped_categories = set(input_categories) - set(output_categories)
            if dropped_categories:
                self.logger.info(f"Categories dropped by LLM filtering: {list(dropped_categories)}")
            
            self.logger.info(f"[TIMING] Step: merge_and_rank (category mode with LLM) - done in {time.perf_counter() - t_format:.3f}s")
            
            # Add metadata
            final_result["processed_at"] = datetime.now().isoformat()
            final_result.setdefault("query_method", "category_semantic_with_precise_llm_filtering")
            
            if save_output:
                self._save_query_result(user_query, final_result)
                
            total_elapsed = time.perf_counter() - query_start
            self.logger.info(f"[TIMING] Category-based query total - done in {total_elapsed:.3f}s")
            self.logger.info(f"Category query complete: Found {final_result.get('total_obligations_found', 0)} obligations from {len(category_matches)} matched categories")
            
            return final_result
            
        except Exception as e:
            self.logger.error(f"Category-based query error: {e}", exc_info=True)
            return {
                "query": user_query,
                "total_documents_searched": 0,
                "total_obligations_found": 0,
                "total_categories": 0,
                "results": [],
                "processed_at": datetime.now().isoformat(),
                "error": str(e)
            }

    async def query(self, user_query: str, save_output: bool = True, document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Main query function - uses ENHANCED individual obligation approach by default
        
        This now uses the new fine-grained retrieval approach that fixes the coarse-grained problem:
        - Retrieves at individual obligation level (not category level)
        - Uses auto-generated keywords from responsibility lines
        - Eliminates noise from irrelevant obligations within categories
        
        Args:
            user_query: User's search query (if empty, returns utility-related obligations)
            save_output: Whether to save the output to a JSON file
            document_ids: Optional list of document URLs to filter by
            
        Returns:
            Final ranked results as JSON
        """
        # Use the individual obligations approach with a softer filter prompt
        return await self.query_by_individual_obligations(user_query, save_output, document_ids)

    async def query_legacy(
        self,
        user_query: str,
        save_output: bool = True,
        document_ids: Optional[List[str]] = None,
        use_category_mode_override: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        LEGACY: Original query function - kept for backward compatibility
        """
        try:
            query_start = time.perf_counter()
            self.logger.info("[TIMING] Query total - start")
            # Check if we should use category-based query
            if use_category_mode_override is None:
                use_category_mode = os.getenv("USE_CATEGORY_SEMANTIC_MATCHING", "true").lower() in ("true", "1", "yes")
            else:
                use_category_mode = bool(use_category_mode_override)
            if use_category_mode:
                self.logger.info("Using category-level semantic matching")
                return await self.query_by_categories(user_query, save_output, document_ids)
        
            # If no query provided, default to utilities query
            if not user_query or user_query.strip() == "":
                user_query = "utilities including water, gas, heat, light, electricity, telephone service, HVAC, sprinkler system, electrical and plumbing systems"
                self.logger.info("No query provided - defaulting to utilities query")
            
            self.logger.info("=" * 80)
            self.logger.info(f"Processing query: '{user_query}'")
            if document_ids:
                self.logger.info(f"Filtering by document_ids: {document_ids}")
            self.logger.info("=" * 80)
            
            # Step 1: Try vector store first (semantic search over obligation chunks)
            t_vector = time.perf_counter()
            self.logger.info("[TIMING] Step: vector store query - start")
            vector_obligations = self._query_vector_store(user_query, n_results=100, document_ids=document_ids)
            self.logger.info(f"[TIMING] Step: vector store query - done in {time.perf_counter() - t_vector:.3f}s ({len(vector_obligations)} results)")
            if not vector_obligations:
                vector_obligations = self._merge_obligations_from_consolidated_topic_only(
                    user_query,
                    self.load_consolidated_jsons(),
                    document_ids,
                )
                if vector_obligations:
                    self.logger.info(
                        "Using consolidated topic fallback (%d rows); Chroma had no usable hits",
                        len(vector_obligations),
                    )
            
            if vector_obligations:
                # Group vector hits by document and extraction category (nested results[] for merge)
                filtered_results_for_merge = grouped_merge_input_from_vector_obligations(vector_obligations)
                filtered_results_for_merge = expand_grouped_vector_merge_to_full_categories(
                    filtered_results_for_merge,
                    self.load_consolidated_jsons(),
                    logger=self.logger,
                )
                # Build document_name_to_id for merge step (Citation -> doc_id)
                document_name_to_id = {}
                if document_ids:
                    for doc_id in document_ids:
                        d = (doc_id or "").strip()
                        if not d:
                            continue
                        name = Path(d).name or d
                        document_name_to_id[name] = doc_id
                        document_name_to_id[name.lower()] = doc_id
                # Run merge and rank (LLM: combine, order by relevance + monetary value, merge similar obligations)
                t_merge = time.perf_counter()
                self.logger.info("[TIMING] Step: merge_and_rank (vector path) - start")
                final_result = await self.merge_and_rank_results(user_query, filtered_results_for_merge, document_name_to_id)
                self.logger.info(f"[TIMING] Step: merge_and_rank (vector path) - done in {time.perf_counter() - t_merge:.3f}s")
                final_result["processed_at"] = datetime.now().isoformat()
                if save_output:
                    self._save_query_result(user_query, final_result)
                total_elapsed = time.perf_counter() - query_start
                self.logger.info(f"[TIMING] Query total - done in {total_elapsed:.3f}s (vector path)")
                self.logger.info(f"Query complete: Found {final_result.get('total_obligations_found', 0)} obligations (vector + merge/rank)")
                return final_result
            
            # Step 2: Fallback to consolidated JSON + LLM filter + merge
            self.logger.info("No vector results; using consolidated JSON + LLM filter")
            consolidated_files = self.load_consolidated_jsons()
            
            if not consolidated_files:
                return {
                    "query": user_query,
                    "total_documents_searched": 0,
                    "total_obligations_found": 0,
                    "total_categories": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                    "error": "No consolidated JSON files or vector store results found"
                }
            
            # Filter by document_ids if provided
            if document_ids:
                t_doc_filter = time.perf_counter()
                self.logger.info("[TIMING] Step: filter by document_ids - start")
                def extract_doc_name_from_url(url: str) -> str:
                    """Extract document name from URL, handling encoding and query params"""
                    try:
                        parsed = urlparse(url)
                        path = parsed.path
                        filename = path.split('/')[-1]
                        filename = unquote(filename)
                        if '?' in filename:
                            filename = filename.split('?')[0]
                        return filename
                    except Exception as e:
                        self.logger.warning(f"Error extracting doc name from URL {url}: {e}")
                        return url
                
                # Accept filenames or paths; normalize to document name for matching
                normalized_doc_ids = {}
                for doc_id in document_ids:
                    d = (doc_id or "").strip()
                    if not d:
                        continue
                    name = Path(d).name or d
                    normalized_doc_ids[name.lower()] = doc_id
                self.logger.info(f"Filtering by {len(normalized_doc_ids)} document(s)")
                
                filtered_files = []
                seen_documents = set()  # Track documents we've already added to prevent duplicates
                
                for file_info in consolidated_files:
                    document_name = file_info.get("document_name", "")
                    file_path = file_info.get("file_path", "")
                    
                    # Normalize document name for comparison (lowercase, remove extra spaces)
                    normalized_doc_name = document_name.lower().strip()
                    
                    # Skip if we've already processed this document
                    if normalized_doc_name in seen_documents:
                        self.logger.warning(f"Skipping duplicate document: {document_name} (already processed)")
                        continue
                    
                    # SECURITY: Only allow exact matches after normalization
                    # This prevents partial/fuzzy matching that could allow unauthorized access
                    matched = False
                    matched_doc_id = None
                    match_reason = None
                    
                    for norm_name, original_doc_id in normalized_doc_ids.items():
                        # SECURITY: Only exact match allowed (after normalization)
                        # This ensures the provided URL's filename exactly matches the document name
                        if norm_name == normalized_doc_name:
                            matched = True
                            matched_doc_id = original_doc_id
                            match_reason = "exact match"
                            break
                        
                        # SECURITY: Also allow exact match without extension (handles .pdf vs no extension)
                        # But only if the base names are exactly equal
                        doc_name_no_ext = norm_name.rsplit('.', 1)[0] if '.' in norm_name else norm_name
                        doc_name_normalized_no_ext = normalized_doc_name.rsplit('.', 1)[0] if '.' in normalized_doc_name else normalized_doc_name
                        
                        if doc_name_no_ext == doc_name_normalized_no_ext and doc_name_no_ext:
                            matched = True
                            matched_doc_id = original_doc_id
                            match_reason = f"exact match (without extension): '{doc_name_no_ext}'"
                            break
                    
                    if matched:
                        # Store the matched doc_id with the file_info for later use
                        file_info["matched_doc_id"] = matched_doc_id
                        filtered_files.append(file_info)
                        seen_documents.add(normalized_doc_name)
                        self.logger.info(f"Matched document: '{document_name}' -> '{matched_doc_id}' (reason: {match_reason})")
                    else:
                        self.logger.debug(f"No match for document: '{document_name}' (checked against {len(normalized_doc_ids)} validated document IDs)")
                
                if not filtered_files:
                    self.logger.warning(
                        f"SECURITY: No documents matched the provided document_ids. "
                        f"This may indicate unauthorized access attempt or incorrect URLs."
                    )
                    self.logger.info(f"Requested document IDs: {document_ids}")
                    self.logger.info(f"Available documents: {[f.get('document_name') for f in consolidated_files]}")
                    return {
                        "query": user_query,
                        "total_documents_searched": 0,
                        "total_obligations_found": 0,
                        "total_categories": 0,
                        "results": [],
                        "processed_at": datetime.now().isoformat(),
                        "error": f"No documents found matching the provided document_ids. Please ensure you provide full URLs pointing to the Documents folder."
                    }
                
                consolidated_files = filtered_files
                self.logger.info(
                    f"SECURITY: Filtered to {len(consolidated_files)} document(s) matching "
                    f"{len(document_ids)} validated document ID(s)"
                )
                if len(consolidated_files) != len(document_ids):
                    self.logger.warning(
                        f"SECURITY: Mismatch - {len(document_ids)} document ID(s) requested, "
                        f"but {len(consolidated_files)} document(s) matched. "
                        f"Some documents may not exist or URLs may be incorrect."
                    )
                self.logger.info(f"Matched documents: {[f.get('document_name') for f in consolidated_files]}")
                self.logger.info(f"[TIMING] Step: filter by document_ids - done in {time.perf_counter() - t_doc_filter:.3f}s")
            
            # Create mapping of document_name to document_id for citation restructuring
            t_map = time.perf_counter()
            self.logger.info("[TIMING] Step: build document_name_to_id - start")
            document_name_to_id = {}
            if document_ids:
                # Use the matched_doc_id stored in file_info
                for file_info in consolidated_files:
                    doc_name = file_info.get("document_name", "")
                    matched_doc_id = file_info.get("matched_doc_id")
                    if matched_doc_id:
                        document_name_to_id[doc_name] = matched_doc_id
                        self.logger.info(f"Mapped document name '{doc_name}' to doc_id '{matched_doc_id}'")
            self.logger.info(f"[TIMING] Step: build document_name_to_id - done in {time.perf_counter() - t_map:.3f}s")
            
            # Step 2: Process each consolidated JSON in PARALLEL
            # OPTIMIZATION: Use asyncio.gather to filter all documents concurrently
            t_filter_all = time.perf_counter()
            self.logger.info(f"[TIMING] Step: filter all documents (parallel) - start ({len(consolidated_files)} docs)")
            self.logger.info(f"Filtering {len(consolidated_files)} documents in parallel...")
            
            filter_tasks = [
                self.filter_obligations_by_query(
                    user_query,
                    file_info["data"],
                    file_info["document_name"]
                )
                for file_info in consolidated_files
            ]
            
            # Wait for all filtering to complete in parallel
            filtered_results = await asyncio.gather(*filter_tasks)
            self.logger.info(f"[TIMING] Step: filter all documents (parallel) - done in {time.perf_counter() - t_filter_all:.3f}s")
            
            # Step 3: Merge and rank all results with document_id mapping
            t_merge = time.perf_counter()
            self.logger.info("[TIMING] Step: merge_and_rank - start")
            final_result = await self.merge_and_rank_results(user_query, filtered_results, document_name_to_id)
            self.logger.info(f"[TIMING] Step: merge_and_rank - done in {time.perf_counter() - t_merge:.3f}s")
            
            # Add timestamp
            final_result["processed_at"] = datetime.now().isoformat()
            
            # Save output if requested
            if save_output:
                t_save = time.perf_counter()
                self.logger.info("[TIMING] Step: save_query_result - start")
                self._save_query_result(user_query, final_result)
                self.logger.info(f"[TIMING] Step: save_query_result - done in {time.perf_counter() - t_save:.3f}s")
            
            total_elapsed = time.perf_counter() - query_start
            self.logger.info("=" * 80)
            self.logger.info(f"[TIMING] Query total - done in {total_elapsed:.3f}s")
            self.logger.info(f"Query complete: Found {final_result.get('total_obligations_found', 0)} relevant obligations")
            self.logger.info("=" * 80)
            
            return final_result
            
        except Exception as e:
            self.logger.error(f"Error processing query: {e}", exc_info=True)
            return {
                "query": user_query,
                "total_documents_searched": 0,
                "total_obligations_found": 0,
                "total_categories": 0,
                "results": [],
                "processed_at": datetime.now().isoformat(),
                "error": str(e)
            }
    
    def _save_query_result(self, user_query: str, result: Dict[str, Any]):
        """Save query result to a JSON file"""
        try:
            # Create query_results folder if it doesn't exist
            query_results_folder = Path("query_results")
            query_results_folder.mkdir(exist_ok=True)
            
            # Create safe filename from query
            safe_query = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in user_query)
            safe_query = safe_query.strip().replace(' ', '_')[:50]  # Limit length
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = query_results_folder / f"query_{safe_query}_{timestamp}.json"
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"Query result saved to: {output_file}")
            
        except Exception as e:
            self.logger.warning(f"Failed to save query result: {e}")


# ============================================================================
# FastAPI Application
# ============================================================================

# Pydantic models for request/response
class QueryRequest(BaseModel):
    """Request model for query endpoint"""
    query: Optional[str] = Field(default="", description="Search query for legal obligations (if empty, returns utility-related obligations)")
    document_ids: Optional[List[str]] = Field(
        default=None, 
        description="Optional list of document identifiers to filter by. Each can be a full URL or a document filename that matches the consolidated document name (e.g. 'Commercial Lease Agreement.pdf'). If omitted, all consolidated documents are searched."
    )
    save_output: bool = Field(default=False, description="Whether to save results to file")
    output_folder: Optional[str] = Field(default=None, description="Local output folder to search for consolidated JSON (if not provided, uses OUTPUT_FOLDER from environment)")

    class Config:
        json_schema_extra = {
            "example": {
                "query": "Landlord HVAC Hazardous Materials",
                "document_ids": ["url1", "url2", "url3"]
            }
        }


class ProcessRequest(BaseModel):
    """Request model for document processing endpoint"""
    docs_folder: Optional[str] = Field(default=None, description="Local folder with PDFs. Defaults from DOCS_FOLDER or request.docs_folder")
    output_folder: Optional[str] = Field(default=None, description="Folder for JSON output. Defaults from OUTPUT_FOLDER / request.output_folder")

    class Config:
        json_schema_extra = {
            "example": {"docs_folder": "docs", "output_folder": "output"}
        }


class ProcessResponse(BaseModel):
    """Response model for document processing endpoint"""
    status: str
    message: str
    total_documents: Optional[int] = None
    successful: Optional[int] = None
    failed: Optional[int] = None
    results: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Processed 3 documents",
                "total_documents": 3,
                "successful": 2,
                "failed": 1,
                "results": [
                    {
                        "document_name": "Commercial Lease Agreement.pdf",
                        "status": "success",
                        "output_path": "output/Commercial_Lease_Agreement_20251209_143000_consolidated.json"
                    }
                ]
            }
        }


class GcsVersionedUploadResponse(BaseModel):
    """Response after uploading a new live version (previous live archived if it existed)."""
    status: str
    bucket: str
    live_object_name: str
    archived_previous: bool
    archive_object_name: Optional[str] = None
    version_id: Optional[str] = None
    error: Optional[str] = None
    synced_local_path: Optional[str] = Field(
        default=None,
        description="Set when GCS_SYNC_LIVE_TO_DOCS is enabled: absolute path written under DOCS_FOLDER.",
    )
    sync_error: Optional[str] = Field(
        default=None,
        description="Local mirror failed (upload/restore to GCS may still have succeeded).",
    )


class GcsArchivedVersionItem(BaseModel):
    archive_object_name: str
    version_id: str
    size: Optional[int] = None
    updated: Optional[str] = None


class GcsListVersionsResponse(BaseModel):
    live_object_name: str
    archive_prefix: str
    versions: List[GcsArchivedVersionItem]


class GcsRestoreVersionRequest(BaseModel):
    """Restore an archived version to become the new live object (current live is archived first)."""
    live_object_name: str = Field(..., description="Stable GCS object name for the live document, e.g. Documents/Lease.pdf")
    archive_object_name: Optional[str] = Field(
        default=None,
        description="Full object path of an archived blob (from list versions). Use this OR version_id.",
    )
    version_id: Optional[str] = Field(
        default=None,
        description="Version folder id (e.g. 20260407T103000Z). Use this OR archive_object_name.",
    )


class QueryResponse(BaseModel):
    """Response model for POST /query: grouped categories and nested obligations (citations per obligation)."""
    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "query": "Plumbing",
                "total_documents_searched": 1,
                "total_obligations_found": 2,
                "total_categories": 1,
                "processed_at": "2025-01-20T10:30:00Z",
                "results": [
                    {
                        "category": "Plumbing",
                        "obligations": [
                            {
                                "Responsible Party": "Landlord",
                                "Owner Responsibility": ["Maintain building plumbing risers"],
                                "Reasoning": ["Landlord scope for common building systems"],
                                "citations": [
                                    {
                                        "docId": "Commercial_Triple_Net_Lease.pdf",
                                        "pageNumbers": [4],
                                        "section": ["6"],
                                    }
                                ],
                            },
                            {
                                "Responsible Party": "Tenant",
                                "Owner Responsibility": ["Pay pro-rata share of plumbing repairs via CAM"],
                                "Reasoning": ["NNN pass-through of operating expenses"],
                                "citations": [
                                    {
                                        "docId": "Commercial_Triple_Net_Lease.pdf",
                                        "pageNumbers": [5],
                                        "section": ["7"],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        },
    )

    query: str
    total_documents_searched: int
    total_obligations_found: int
    total_categories: int = 0
    processed_at: str
    results: List[Dict[str, Any]]
    error: Optional[str] = None


# Initialize FastAPI app
app = FastAPI(
    title="Legal Obligation Query System",
    description="Search and retrieve legal obligations from processed documents",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global query system instance (initialized on startup)
query_system_instance: Optional[ObligationQuerySystem] = None


@app.on_event("startup")
async def startup_event():
    """Initialize the query system on startup (local output folder + Azure OpenAI or Gemini)."""
    global query_system_instance
    try:
        local_out = os.getenv("OUTPUT_FOLDER", "output")
        model = get_default_model()
        query_system_instance = ObligationQuerySystem(local_output_folder=local_out, model=model)
        logging.info(f"Query system initialized (output: {local_out}, model: {model})")
    except Exception as e:
        logging.error(f"Failed to initialize query system: {e}")
        raise


@app.get("/", tags=["Health"])
async def root():
    """Root endpoint - API health check"""
    return {
        "status": "online",
        "service": "Legal Obligation Query System",
        "version": "1.0.0",
        "endpoints": {
            "query_post": "/query (POST)",
            "process": "/process (POST)",
            "documents": "/documents (GET)",
            "health": "/health",
            "docs": "/docs"
        }
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")
    
    try:
        files = query_system_instance.load_consolidated_jsons()
        return {"status": "healthy", "query_system": "initialized", "documents_available": len(files), "output_folder": query_system_instance.local_output_folder}
    except Exception as e:
        return {"status": "degraded", "query_system": "initialized", "error": str(e)}


@app.get("/test-logging", tags=["Debug"])
async def test_logging():
    """Test endpoint to verify logging works"""
    import sys
    from force_terminal_logger import force_logger
    
    test_message = f"🧪 LOGGING TEST at {datetime.now()}"
    
    # Try all possible ways to show the message
    logging.info(test_message)
    force_logger.info(test_message)  # Use our force logger
    print(test_message, flush=True)
    sys.stdout.write(f"{test_message}\n")
    sys.stdout.flush()
    
    return {"status": "test_completed", "message": "Check terminal for log output"}


@app.post("/query/categories", response_model=QueryResponse, tags=["Query"])
async def query_obligations_by_categories(request: Optional[QueryRequest] = Body(default=None)):
    """
    Query legal obligations using category-level semantic matching.
    
    This endpoint uses a different approach:
    1. Semantically matches user query against category-level aggregated keywords
    2. Retrieves full categories that match semantically
    3. Lets the LLM filter and rank obligations within those complete categories
    
    This can provide better results when queries are broad or when you want to ensure
    no related obligations are missed within semantically relevant categories.
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")

    req = request or QueryRequest()
    user_query = (req.query or "") if isinstance(req.query, str) else ""

    try:
        if req.output_folder:
            qs = ObligationQuerySystem(local_output_folder=req.output_folder, model=get_default_model())
            result = await qs.query_by_categories(
                user_query=user_query,
                save_output=req.save_output,
                document_ids=req.document_ids
            )
        else:
            # Use default query system instance
            result = await query_system_instance.query_by_categories(
                user_query=user_query,
                save_output=req.save_output,
                document_ids=req.document_ids
            )
        
        # Check for errors in result
        if "error" in result and result.get("total_obligations_found", 0) == 0:
            raise HTTPException(status_code=500, detail=result["error"])
        
        return JSONResponse(content=result)
        
    except Exception as e:
        logging.error(f"Error processing category query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing category query: {str(e)}")


@app.post("/query", response_model=QueryResponse, tags=["Query"])
async def query_obligations_post(request: Optional[QueryRequest] = Body(default=None)):
    """
    Query legal obligations using POST method

    Searches through all consolidated JSON files and returns relevant obligations
    ranked by relevance and monetary value.

    If no query is provided (empty string), returns all utility-related obligations including:
    water, gas, heat, light, electricity, telephone service, HVAC, sprinkler system,
    electrical and plumbing systems.

    If document_ids are provided, only searches those specific documents.
    document_ids can be URLs or filenames that match consolidated document names.

    Args:
        request: QueryRequest containing the search query, optional document_ids filter, and optional output folder.
                 Body is optional; omit or send {} for defaults.

    Returns:
        QueryResponse with matched obligations
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")

    req = request or QueryRequest()
    user_query = (req.query or "") if isinstance(req.query, str) else ""


    try:
        if req.output_folder:
            qs = ObligationQuerySystem(local_output_folder=req.output_folder, model=get_default_model())
            result = await qs.query(
                user_query=user_query,
                save_output=req.save_output,
                document_ids=req.document_ids
            )
        else:
            # Use default query system instance
            result = await query_system_instance.query(
                user_query=user_query,
                save_output=req.save_output,
                document_ids=req.document_ids
            )
        
        # Check for errors in result
        if "error" in result and result.get("total_obligations_found", 0) == 0:
            raise HTTPException(status_code=500, detail=result["error"])
        
        return JSONResponse(content=result)
        
    except Exception as e:
        logging.error(f"Error processing query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}")


@app.post("/query/stream/raw", tags=["Query"])
async def query_obligations_stream_raw(request: Optional[QueryRequest] = Body(default=None)):
    """
    Stream RAW LLM tokens as they're generated during merge and rank.
    Shows the actual token-by-token generation from GPT-4o-mini.
    
    Returns raw text stream showing LLM generating the JSON response in real-time.
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")
    
    req = request or QueryRequest()
    user_query = (req.query or "") if isinstance(req.query, str) else ""
    
    async def raw_token_generator():
        try:
            # Load and filter documents
            qs = query_system_instance
            if req.output_folder:
                qs = ObligationQuerySystem(local_output_folder=req.output_folder, model=get_default_model())
            consolidated_files = qs.load_consolidated_jsons()
            
            if req.document_ids:
                consolidated_files = [
                    f for f in consolidated_files
                    if any(doc_id in f["document_name"] or doc_id in f["file_name"] for doc_id in req.document_ids)
                ]
            
            def _echo(chunk: str):
                sys.stdout.write(chunk)
                sys.stdout.flush()
                return chunk
            
            if not consolidated_files:
                _echo("[ERROR] No documents found\n")
                yield "[ERROR] No documents found\n"
                return
            
            # Step 1: Vector search for filtering (same as main query())
            _echo(f"[QUERY] {user_query}\n")
            yield f"[QUERY] {user_query}\n"
            _echo("[STEP 1] Vector search...\n")
            yield "[STEP 1] Vector search...\n"
            vector_obligations = qs._query_vector_store(user_query, n_results=100, document_ids=req.document_ids)
            if not vector_obligations:
                vector_obligations = qs._merge_obligations_from_consolidated_topic_only(
                    user_query, consolidated_files, req.document_ids
                )
            _echo(f"[STEP 1] Found {len(vector_obligations)} vector results\n")
            yield f"[STEP 1] Found {len(vector_obligations)} vector results\n"
            
            if vector_obligations:
                _echo("[STEP 2] Using vector results for filtering (no LLM filter)\n")
                yield "[STEP 2] Using vector results for filtering (no LLM filter)\n"
                filtered_results = grouped_merge_input_from_vector_obligations(vector_obligations)
                filtered_results = expand_grouped_vector_merge_to_full_categories(
                    filtered_results,
                    consolidated_files,
                    logger=logging.getLogger(__name__),
                )
            else:
                _echo("[STEP 2] No vector results; LLM filtering each document...\n")
                yield "[STEP 2] No vector results; LLM filtering each document...\n"
                filter_tasks = [
                    qs.filter_obligations_by_query(user_query, f["data"], f["document_name"])
                    for f in consolidated_files
                ]
                filtered_results = await asyncio.gather(*filter_tasks)
            _echo("[STEP 2] Filtering complete\n")
            yield "[STEP 2] Filtering complete\n"
            
            non_empty_fr = [r for r in filtered_results if _filtered_fr_has_payload(r)]
            merge_in, _ = cap_merge_filtered_results(
                non_empty_fr,
                _merge_max_input_obligations_for_query(user_query, document_blocks=len(non_empty_fr)),
            )
            
            _echo("[STEP 3] Merging and ranking (streaming LLM tokens)...\n")
            yield "[STEP 3] Merging and ranking (streaming LLM tokens)...\n"
            _echo("="*80 + "\n")
            yield "="*80 + "\n"
            _echo("RAW LLM OUTPUT (watch it generate in real-time):\n")
            yield "RAW LLM OUTPUT (watch it generate in real-time):\n"
            _echo("="*80 + "\n")
            yield "="*80 + "\n"
            
            # Same merge/rank prompt as /query and /query/stream so results are consistent
            merge_prompt = qs._build_merge_rank_prompt(user_query, merge_in)
            
            # Stream raw tokens (and echo to terminal so you see stream when using Postman)
            token_count = 0
            async for token in generate_content_stream(
                prompt=merge_prompt,
                model=qs.model,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=_merge_rank_max_output_tokens(),
            ):
                token_count += 1
                sys.stdout.write(token)
                sys.stdout.flush()
                yield token
                
                if token_count % 100 == 0:
                    progress = f"\n[{token_count} tokens]\n"
                    sys.stdout.write(progress)
                    sys.stdout.flush()
                    yield progress
            
            tail = "\n" + "="*80 + "\n" + f"[COMPLETE] Generated {token_count} tokens\n" + "="*80 + "\n"
            sys.stdout.write(tail)
            sys.stdout.flush()
            yield tail
            
        except Exception as e:
            err = f"\n[ERROR] {str(e)}\n"
            import traceback
            err += traceback.format_exc()
            sys.stdout.write(err)
            sys.stdout.flush()
            yield err
    
    return StreamingResponse(raw_token_generator(), media_type="text/plain")


@app.post("/query/stream", tags=["Query"])
async def query_obligations_stream(request: Optional[QueryRequest] = Body(default=None)):
    """
    NDJSON stream of merge results: each parsed category group, then metadata with counts.

    Uses the same merge/rank prompt and filtering as POST /query and /query/stream/raw.
    For raw token-by-token streaming, use POST /query/stream/raw.

    Each line is one JSON object followed by newline:
    - {"type": "category_group", "data": {"category", "obligations"}}
    - {"type": "metadata", "data": {query, totals, processed_at}}
    - {"type": "error", "message": "..."}
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")
    
    req = request or QueryRequest()
    user_query = (req.query or "") if isinstance(req.query, str) else ""
    
    async def event_generator():
        try:
            from force_terminal_logger import force_logger
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(line_buffering=True)

            def _write_stream_line(line: str):
                """Write stream output directly to terminal (and flush)."""
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    if sys.__stdout__ is not sys.stdout:
                        sys.__stdout__.write(line)
                        sys.__stdout__.flush()
                except Exception:
                    force_logger.info(line.strip())

            qs = query_system_instance
            if req.output_folder:
                qs = ObligationQuerySystem(local_output_folder=req.output_folder, model=get_default_model())
            
            consolidated_files = qs.load_consolidated_jsons()
            
            if req.document_ids:
                consolidated_files = [
                    f for f in consolidated_files
                    if any(doc_id in f["document_name"] or doc_id in f["file_name"] for doc_id in req.document_ids)
                ]
            
            if not consolidated_files:
                force_logger.info("[STREAM] No documents found for query")
                yield json.dumps({"type": "error", "message": "No documents found"}) + "\n"
                return
            
            document_name_to_id = {}
            for file_info in consolidated_files:
                doc_name = file_info["document_name"]
                doc_id = file_info.get("data", {}).get("document_id") or doc_name
                document_name_to_id[doc_name] = doc_id
            if req.document_ids:
                for doc_id in req.document_ids:
                    d = (doc_id or "").strip()
                    if d:
                        name = Path(d).name or d
                        document_name_to_id[name] = doc_id
                        document_name_to_id[name.lower()] = doc_id
            
            # Step 1: Vector search for filtering (same as main query())
            t_vector = time.perf_counter()
            vector_obligations = qs._query_vector_store(user_query, n_results=100, document_ids=req.document_ids)
            if not vector_obligations:
                vector_obligations = qs._merge_obligations_from_consolidated_topic_only(
                    user_query, consolidated_files, req.document_ids
                )
                if vector_obligations:
                    logging.info("[STREAM] Consolidated topic fallback (%d rows); Chroma empty", len(vector_obligations))
            elapsed_vector = time.perf_counter() - t_vector
            logging.info(
                "[STREAM] Retrieval: %d obligation row(s) after vector + optional consolidated fallback (%.2fs)",
                len(vector_obligations),
                elapsed_vector,
            )
            force_logger.info(
                f"[STREAM] Retrieval complete: {len(vector_obligations)} obligation row(s) "
                f"after vector + optional consolidated fallback ({elapsed_vector:.2f}s)"
            )

            if vector_obligations:
                # Vector path: group by document + extraction category for merge (nested results[])
                logging.info(f"[STREAM] Using vector results for filtering (no per-document LLM filter)")
                force_logger.info("[STREAM] Using vector results for filtering (no per-document LLM filter)")
                filtered_results = grouped_merge_input_from_vector_obligations(vector_obligations)
                filtered_results = expand_grouped_vector_merge_to_full_categories(
                    filtered_results,
                    consolidated_files,
                    logger=logging.getLogger(__name__),
                )
            else:
                # Fallback: LLM filter each document then merge/rank
                logging.info(f"[STREAM] No vector results; using LLM filter per document")
                force_logger.info("[STREAM] No vector results; using LLM filter per document")
                filter_tasks = [
                    qs.filter_obligations_by_query(
                        user_query,
                        file_info["data"],
                        file_info["document_name"]
                    )
                    for file_info in consolidated_files
                ]
                filtered_results = await asyncio.gather(*filter_tasks)
            
            total_for_merge = _count_obligations_in_filtered(filtered_results)
            logging.info(f"[STREAM] Merge and rank LLM call - start (input: {total_for_merge} obligations from {len(filtered_results)} doc(s))")
            force_logger.info(
                f"[STREAM] Merge and rank LLM call - start (input: {total_for_merge} obligations from {len(filtered_results)} doc(s))"
            )
            
            # Step 2: Stream merge and rank results (echo to terminal so you see stream when using Postman)
            obligation_stream_count = 0
            async for event in qs.merge_and_rank_results_stream(user_query, filtered_results, document_name_to_id):
                if event.get("type") == "category_group":
                    obligation_stream_count += len((event.get("data") or {}).get("obligations") or [])
                line = json.dumps(event) + "\n"
                _write_stream_line(line)
                force_logger.info(f"[STREAM EVENT] {line.strip()}")
                yield line
            
            logging.info(f"[STREAM] Merge and rank LLM call - complete (streamed {obligation_stream_count} obligations)")
            force_logger.info(f"[STREAM] Merge and rank LLM call - complete (streamed {obligation_stream_count} obligations)")
            
        except Exception as e:
            logging.error(f"Error in streaming query: {e}", exc_info=True)
            line = json.dumps({"type": "error", "message": str(e)}) + "\n"
            try:
                _write_stream_line(line)
            except Exception:
                pass
            yield line
    
    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.post("/process", response_model=ProcessResponse, tags=["Processing"])
async def process_document(request: ProcessRequest):
    """Process all PDFs from docs folder. Writes (1) section-based obligations JSON, (2) consolidated JSON to output folder. Extraction is section-only (regex: ^\\d+\\. , ^\\([a-z]\\) , ^\\(\\d+\\)). Uses Azure OpenAI or Gemini via llm_client."""
    import sys
    try:
        from process_legal_documents import LegalDocumentProcessor
        from force_terminal_logger import force_logger

        docs_folder = request.docs_folder or os.getenv('DOCS_FOLDER', 'docs')
        out_folder = request.output_folder or os.getenv('OUTPUT_FOLDER', 'output')
        
        # Multiple ways to ensure this message shows up
        message = f"\n🚀 API PROCESSING REQUEST RECEIVED\n📁 Docs: {docs_folder}\n📁 Output: {out_folder}\n" + "="*80
        logging.info(message)
        force_logger.info(message)  # Force terminal output
        processor = LegalDocumentProcessor(
            local_docs_folder=docs_folder,
            local_output_folder=out_folder,
            logs_folder=os.getenv('LOGS_FOLDER', 'logs'),
            cache_folder=os.getenv('CACHE_FOLDER', 'ocr_cache'),
            prompt_file=os.getenv('PROMPT_FILE', 'prompt.txt'),
            model=get_default_model(),
            tesseract_cmd=os.getenv('TESSERACT_CMD'),
            poppler_path=os.getenv('POPPLER_PATH')
        )
        
        result = processor.process_all_documents()
        
        return ProcessResponse(
            status=result["status"],
            message=result["message"],
            total_documents=result["total_documents"],
            successful=result["successful"],
            failed=result["failed"],
            results=result["results"],
            error=result.get("error")
        )
    except Exception as e:
        logging.error(f"Error processing documents: {e}", exc_info=True)
        return ProcessResponse(
            status="error",
            message=str(e),
            total_documents=0,
            successful=0,
            failed=0,
            results=[],
            error=str(e)
        )


@app.get("/documents", tags=["Documents"])
async def list_documents():
    """
    List all available consolidated JSON documents
    
    Returns:
        List of available documents with metadata
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")
    
    try:
        files = query_system_instance.load_consolidated_jsons()
        
        documents = []
        for file_info in files:
            data = file_info["data"]
            documents.append({
                "document_name": file_info["document_name"],
                "file_name": file_info["file_name"],
                "processed_at": data.get("processed_at", "Unknown"),
                "total_pages": data.get("total_pages", 0),
                "total_obligations": data.get("consolidated_obligations_count", 0),
                "party_metadata": data.get("party_metadata", {})
            })
        
        return {
            "total_documents": len(documents),
            "documents": documents
        }
        
    except Exception as e:
        logging.error(f"Error listing documents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error listing documents: {str(e)}")


@app.post("/gcs/versioned-upload", response_model=GcsVersionedUploadResponse, tags=["GCS versioning"])
async def gcs_versioned_upload(
    file: UploadFile = File(...),
    live_object_name: str = Form(
        ...,
        description='Stable object path in the bucket (the "live" document), e.g. Documents/Lease.pdf',
    ),
):
    """
    Upload a new file to the given live object path. If an object already exists there, it is
    copied into the archive tree under `Documents/.versions/...` (see `GCS_VERSION_ARCHIVE_PREFIX`),
    then replaced by this upload. Works with the local GCS emulator when `STORAGE_EMULATOR_HOST` is set.

    When `GCS_SYNC_LIVE_TO_DOCS` is true, the same bytes are written to `DOCS_FOLDER` / basename(live_object_name)
    so `/process` and CLI flows see the new PDF without a manual copy.
    """
    try:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty file")
        ct = file.content_type
        r = publish_new_version(live_object_name, data, content_type=ct if ct else None)
        synced_path, sync_err = maybe_sync_live_bytes_to_docs(r.live_object_name, data)
        return GcsVersionedUploadResponse(
            status="success",
            bucket=r.bucket,
            live_object_name=r.live_object_name,
            archived_previous=r.archived_previous,
            archive_object_name=r.archive_object_name,
            version_id=r.version_id,
            synced_local_path=synced_path,
            sync_error=sync_err,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logging.error(f"GCS versioned upload failed: {e}", exc_info=True)
        return GcsVersionedUploadResponse(
            status="error",
            bucket=os.getenv("GCS_BUCKET", "heb-legal"),
            live_object_name=live_object_name,
            archived_previous=False,
            error=str(e),
            synced_local_path=None,
            sync_error=None,
        )


@app.get("/gcs/versions", response_model=GcsListVersionsResponse, tags=["GCS versioning"])
async def gcs_list_versions(
    live_object_name: str = Query(..., description="Same live path used when uploading, e.g. Documents/Lease.pdf"),
):
    """List archived versions for a live object path (newest `version_id` first)."""
    try:
        items = list_archived_versions(live_object_name)
        return GcsListVersionsResponse(
            live_object_name=live_object_name.strip().lstrip("/"),
            archive_prefix=default_archive_prefix(),
            versions=[
                GcsArchivedVersionItem(
                    archive_object_name=v.archive_object_name,
                    version_id=v.version_id,
                    size=v.size,
                    updated=v.updated,
                )
                for v in items
            ],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logging.error(f"GCS list versions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/gcs/restore-version", response_model=GcsVersionedUploadResponse, tags=["GCS versioning"])
async def gcs_restore_version(body: GcsRestoreVersionRequest):
    """
    Promote an archived blob to become the live object. The current live object is archived
    first (same behavior as a new upload), then the selected archive is written to the live path.
    When `GCS_SYNC_LIVE_TO_DOCS` is true, the new live object is mirrored into `DOCS_FOLDER`.
    """
    if body.archive_object_name and body.version_id:
        raise HTTPException(
            status_code=400,
            detail="Provide only one of archive_object_name or version_id",
        )
    if not body.archive_object_name and not body.version_id:
        raise HTTPException(
            status_code=400,
            detail="Provide archive_object_name or version_id",
        )
    try:
        if body.version_id:
            r = restore_version_by_id(body.live_object_name, body.version_id)
        else:
            assert body.archive_object_name is not None
            r = restore_archived_to_live(body.live_object_name, body.archive_object_name)
        synced_path, sync_err = maybe_sync_live_from_bucket_to_docs(r.live_object_name)
        return GcsVersionedUploadResponse(
            status="success",
            bucket=r.bucket,
            live_object_name=r.live_object_name,
            archived_previous=r.archived_previous,
            archive_object_name=r.archive_object_name,
            version_id=r.version_id,
            synced_local_path=synced_path,
            sync_error=sync_err,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logging.error(f"GCS restore failed: {e}", exc_info=True)
        return GcsVersionedUploadResponse(
            status="error",
            bucket=os.getenv("GCS_BUCKET", "heb-legal"),
            live_object_name=body.live_object_name,
            archived_previous=False,
            error=str(e),
            synced_local_path=None,
            sync_error=None,
        )


# ============================================================================
# Command Line Interface
# ============================================================================

async def main():
    """Main entry point for command-line usage (async)"""
    import sys
    
    # Get query from command line or prompt user
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:])
    else:
        user_query = input("Enter your query: ").strip()
    
    if not user_query:
        print("Error: Query cannot be empty")
        return
    
    query_system = ObligationQuerySystem(local_output_folder=os.getenv('OUTPUT_FOLDER', 'output'), model=get_default_model())
    print(f"Output folder: {query_system.local_output_folder}")
    
    # Execute query (now async)
    result = await query_system.query(user_query)
    
    # Print results
    print("\n" + "=" * 80)
    print("QUERY RESULTS")
    print("=" * 80)
    print(f"Query: {result.get('query', '')}")
    print(f"Documents Searched: {result.get('total_documents_searched', 0)}")
    print(f"Obligations Found: {result.get('total_obligations_found', 0)}")
    print("\n" + json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

