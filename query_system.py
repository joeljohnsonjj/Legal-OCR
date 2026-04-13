"""
Interactive Query System for Legal Document Obligations
Searches through consolidated JSON files and returns relevant obligations based on user queries
"""

import asyncio
import os
import sys
import json
import logging
import re
import time
from pathlib import Path
from typing import AsyncIterator, List, Dict, Any, Optional
from datetime import datetime
from urllib.parse import unquote, urlparse

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
from pydantic import BaseModel, Field

# Environment Variables
from dotenv import load_dotenv

from citation_utils import merge_structured_citations_by_doc_id
from obligation_categories import obligations_from_consolidated_json

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
    citation = ob.get("Citation")
    if citation is None:
        pass
    elif isinstance(citation, str) and citation.strip():
        parts.append(citation.strip())
    elif isinstance(citation, list):
        for c in citation:
            if isinstance(c, dict):
                pages = c.get("pageNumbers") or []
                sections = c.get("section") or []
                if pages or sections:
                    page_part = f"Page {', '.join(map(str, pages))}" if pages else ""
                    section_part = "; ".join(sections) if sections else ""
                    parts.append(", ".join(filter(None, [page_part, section_part])))
            else:
                parts.append(str(c))
    return parts


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


def convert_result_citations_to_structured(final_result: Dict[str, Any]) -> None:
    """In-place: convert each result's Citation to structured format [{ docId, pageNumbers, section }]."""
    for ob in final_result.get("results", []):
        ob["Citation"] = citation_string_to_structured(ob.get("Citation"))


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
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def _generate_content(self, prompt: str, temperature: float = 0.1, response_mime_type: str = "application/json"):
        """Call configured LLM (Azure OpenAI or Gemini) via llm_client."""
        return llm_generate_content(prompt, model=self.model, temperature=temperature, response_mime_type=response_mime_type)
    
    async def _generate_content_async(self, prompt: str, temperature: float = 0.1, response_mime_type: str = "application/json"):
        """Async wrapper for LLM API calls (Azure OpenAI or Gemini)."""
        from llm_client import generate_content_async
        return await generate_content_async(prompt, model=self.model, temperature=temperature, response_mime_type=response_mime_type)
    
    def load_consolidated_jsons(self) -> List[Dict[str, Any]]:
        """Load *_consolidated.json first, then *_consolidated.md, then *_pagewise.json. Prefers JSON.
        Consolidated JSON may store obligations only under consolidated_results_by_category (no duplicate flat list).
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
                    flat = [ob for k in keys_sorted for ob in page_results[k]]
                    data = {
                        "document_name": pw.get("document_name", "Unknown"),
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

    def _query_vector_store(
        self,
        user_query: str,
        n_results: int = 50,
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query ChromaDB by semantic similarity; only results with distance <= VECTOR_MAX_DISTANCE.
        Auto-detects party in query (tenant/landlord) and filters by Responsible_Party metadata.
        Returns the top n_results most similar obligation chunks; full obligations
        are resolved from consolidated JSON (document_name + chunk_index).
        Returns list of obligation dicts (DutyType, Responsible Party, Owner Responsibility, Reasoning, Citation)
        with Citation prefixed by "Document: {document_name} | ".
        """
        try:
            from vector_store import query_obligations
            chroma_path = str(Path(self.local_output_folder) / "chroma_db")
            if not Path(chroma_path).exists():
                self.logger.info("Vector store path does not exist; skipping vector query")
                return []
            
            # Auto-detect party in query for filtering
            query_lower = user_query.lower()
            detected_party = None
            if "tenant" in query_lower:
                detected_party = "Tenant"
                self.logger.info("Detected 'tenant' in query → filtering by Responsible_Party=Tenant")
            elif "landlord" in query_lower:
                detected_party = "Landlord"
                self.logger.info("Detected 'landlord' in query → filtering by Responsible_Party=Landlord")
            
            # Distance threshold: only results with distance <= VECTOR_MAX_DISTANCE (default 1.4)
            max_dist_str = os.getenv("VECTOR_MAX_DISTANCE", "1.4").strip()
            try:
                max_distance = float(max_dist_str) if max_dist_str else 1.4
            except ValueError:
                max_distance = 1.4
            self.logger.info(f"Vector query: max_distance={max_distance} (from VECTOR_MAX_DISTANCE)")
            raw = query_obligations(
                query_text=user_query,
                n_results=500,
                document_name=None,
                chroma_path=chroma_path,
                max_distance=max_distance,
                responsible_party=detected_party,
            )
            if not raw:
                return []
            if document_ids:
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
                filtered = []
                for r in raw:
                    doc_name = (r.get("document_name") or "").strip()
                    doc_norm = doc_name.lower()
                    doc_base = doc_norm.rsplit(".", 1)[0] if "." in doc_norm else doc_norm
                    if doc_norm in normalized_allowed or doc_base in normalized_allowed:
                        filtered.append(r)
                raw = filtered
            # Load consolidated JSONs to resolve full obligation by document_name + chunk_index
            loaded = self.load_consolidated_jsons()
            doc_to_results: Dict[str, List[Dict[str, Any]]] = {}
            for entry in loaded:
                data = entry.get("data") or {}
                doc_name = (data.get("document_name") or "").strip()
                results = obligations_from_consolidated_json(data)
                if doc_name:
                    doc_to_results[doc_name] = results
            obligations = []
            for r in raw:
                doc_name = (r.get("document_name") or "").strip()
                try:
                    idx = int(r.get("chunk_index") or 0)
                except (TypeError, ValueError):
                    idx = 0
                results = doc_to_results.get(doc_name)
                if results and 0 <= idx < len(results):
                    full_ob = results[idx]
                    citation = full_ob.get("Citation") or ""
                    if isinstance(citation, list):
                        citation = "; ".join(str(c) for c in citation)
                    if doc_name:
                        citation = f"Document: {doc_name} | {citation}"
                    obligations.append({
                        "DutyType": full_ob.get("DutyType") or "",
                        "Responsible Party": full_ob.get("Responsible Party") or "",
                        "Owner Responsibility": full_ob.get("Owner Responsibility") if isinstance(full_ob.get("Owner Responsibility"), list) else [str(full_ob.get("Owner Responsibility") or "")],
                        "Reasoning": full_ob.get("Reasoning") if isinstance(full_ob.get("Reasoning"), list) else [str(full_ob.get("Reasoning") or "")],
                        "Citation": citation,
                    })
                else:
                    # Fallback: build from metadata when consolidated lookup fails
                    citation = r.get("Citation") or ""
                    if doc_name:
                        citation = f"Document: {doc_name} | {citation}"
                    obligations.append({
                        "DutyType": r.get("DutyType") or "",
                        "Responsible Party": r.get("Responsible_Party") or "",
                        "Owner Responsibility": [r.get("document") or ""] if r.get("document") else [],
                        "Reasoning": [],
                        "Citation": citation,
                    })
            return obligations
        except Exception as e:
            self.logger.warning(f"Vector store query failed, will use JSON path: {e}")
            return []

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
            # Extract obligations from consolidated data
            obligations = obligations_from_consolidated_json(consolidated_data)
            
            if not obligations:
                self.logger.warning(f"No obligations found in {document_name}")
                return self._create_empty_response(document_name, consolidated_data)
            
            # Prepare the filtering prompt
            filter_prompt = f"""You are a legal document analyst. You have been provided with financial obligations extracted from a legal document and a user query.

Your task is to filter and return ONLY the obligations that are relevant to the user's query.

HOW TO IDENTIFY MATCHED OBLIGATIONS:

1. ANALYZE THE USER QUERY:
   - Identify the core concept (e.g., "rent", "insurance", "maintenance")
   - Consider related terms and synonyms (e.g., "rent" includes "rental payment", "lease payment", "base rent")
   - Understand the intent (e.g., "payment" could mean any monetary obligation)

2. CHECK EACH OBLIGATION FIELD FOR MATCHES:
   
   a) PRIMARY MATCH - "DutyType" field:
      - Does the DutyType directly match the query? (e.g., query "Rent" matches DutyType "Rent Payment")
      - Does it contain related terms? (e.g., query "Insurance" matches "Insurance Premium", "Insurance Requirement")
      - Consider semantic similarity, not just exact words
   
   b) SECONDARY MATCH - "Owner Responsibility" field:
      - Do any responsibility items mention the query concept?
      - Example: Query "insurance" should match responsibility "Maintain property insurance of $2M"
      - Look for the query term or its variations in the responsibility text
   
   c) TERTIARY MATCH - "Reasoning" field:
      - Does the reasoning explain why this obligation relates to the query?
      - Example: Query "property damage" might match reasoning "To cover property damage costs"
   
   d) CONTEXTUAL MATCH - "Responsible Party" field:
      - If query mentions a specific party name, filter by that party
      - Example: Query "H-E-B obligations" should only return obligations where Responsible Party is "H-E-B, L.P."

3. MATCHING CRITERIA (Include obligation if ANY of these are true):
   - Query term appears in DutyType (exact or semantic match)
   - Query term appears in any Owner Responsibility item
   - Query concept is directly related to the obligation's purpose
   - For broad queries (e.g., "payment", "cost"), include all monetary obligations
   - For specific queries (e.g., "rent payment"), only include closely related obligations

4. EXAMPLES OF MATCHING:

   Query: "Rent payment"
   ✅ MATCH: DutyType = "Rent Payment", "Base Rent", "Monthly Rent", "Additional Rent"
   ✅ MATCH: Owner Responsibility contains "pay rent", "rental payment", "lease payment"
   ❌ NO MATCH: DutyType = "Insurance Premium" (unrelated)
   
   Query: "Insurance"
   ✅ MATCH: DutyType = "Insurance Premium", "Insurance Requirement", "Insurance Cost"
   ✅ MATCH: Owner Responsibility contains "maintain insurance", "insurance coverage"
   ❌ NO MATCH: DutyType = "Property Tax Payment" (different obligation type)
   
   Query: "Maintenance"
   ✅ MATCH: DutyType = "Maintenance Cost", "Repair Obligation", "Property Upkeep"
   ✅ MATCH: Owner Responsibility contains "repair", "maintain", "fix", "replace"
   ❌ NO MATCH: DutyType = "Security Deposit" (unrelated)
   
   Query: "H-E-B" or specific party name
   ✅ MATCH: Responsible Party = "H-E-B, L.P." or contains "H-E-B"
   ❌ NO MATCH: Responsible Party = "Tenant" (different party)

5. WHEN TO EXCLUDE:
   - The obligation is clearly about a different topic (e.g., query "rent" vs obligation about "insurance")
   - No semantic relationship exists between query and obligation
   - Query specifies a party, but obligation is for a different party

CRITICAL GUARDRAILS:
- Use the EXACT same JSON structure as provided - do not modify, add, or remove any fields
- Do not alter the content of any obligation - return them exactly as given
- If NO obligations are relevant to the query, return an empty array: {{"consolidated_results": []}}
- Preserve all fields: "DutyType", "Responsible Party", "Owner Responsibility", "Reasoning", "Citation"
- Do not add commentary, explanations, or any text outside the JSON structure
- Output ONLY valid JSON
- Be inclusive rather than exclusive - if unsure, include the obligation (better to have false positives than miss relevant obligations)

User Query: "{user_query}"

Document: {document_name}

Obligations to filter:
{json.dumps(obligations, indent=2)}

Return a JSON object with this structure:
{{
  "document_name": "{document_name}",
  "query": "{user_query}",
  "consolidated_results": [
    // Array of relevant obligations (exact copies from above)
    // OR empty array [] if no relevant obligations found
  ]
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
            
            # Ensure it has the required structure
            if "consolidated_results" not in filtered_result:
                filtered_result["consolidated_results"] = []
            
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
            "consolidated_results": []
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
    
    def _build_merge_rank_prompt(self, user_query: str, filtered_results: List[Dict[str, Any]]) -> str:
        """Build the merge-and-rank LLM prompt. Shared by /query, /query/stream, and /query/stream/raw so results are consistent."""
        non_empty_results = [r for r in filtered_results if r.get("consolidated_results")]
        return f"""You are a legal document analyst. You have been provided with filtered financial obligations from multiple legal documents, all relevant to a user's query.

FILTERING (apply before merging): Include only obligations clearly related to the user's query. The most similar obligations to the query MUST be included in the result set — never exclude them. If the full query word appears exactly in an obligation's related_keywords, always include that obligation. Include obligations that are semantically similar to the query. Exclude only vaguely or tangentially related obligations. When the query has clear legal meaning and the connection is ambiguous but plausible, include; if the query is nonsensical, gibberish, or off-topic, return results: [] (zero obligations).

Your task is to merge these results into a single JSON and order them by:
1. SIMILARITY TO THE USER QUERY: The most similar obligations to the user query MUST appear in the result set and MUST be ranked first (highest similarity at the top). Do not omit highly similar obligations; include them and place them at the top.
2. RELEVANCE: Then by overall relevance to the query.
3. MONETARY VALUE: Then by highest amounts first.

MERGE SIMILAR OBLIGATIONS FROM DIFFERENT DOCUMENTS (STRICTLY FOLLOW):
- Merge two or more obligations from different documents into ONE row ONLY if they are semantically similar: same or equivalent meaning (e.g. same duty in substance, same responsible party, and equivalent scope or obligation). Do NOT merge based only on same DutyType and Responsible Party if the actual obligation (Owner Responsibility, scope, or meaning) differs.
- If obligations are only superficially similar (e.g. same duty type but different scope, amount, or condition), keep them as separate rows. When in doubt, do not merge.
- For each group of semantically similar obligations (from one or more documents), output exactly ONE row. In the "Citation" field you MUST list every source document that contributed to that obligation. Use this format with " ; " (space-semicolon-space) between documents: "Document: [filename1] | [citation1] ; Document: [filename2] | [citation2]". Include every document; never omit any.
- When you merge two or more such obligations, combine "Owner Responsibility" and "Reasoning" from every contributing obligation efficiently: pull only material, non-redundant information from each source row in the consolidated JSON; drop near-duplicate lines; keep distinct facts, amounts, conditions, or scope from each document so the merged row still reflects what each document said. Prefer short bullet-style strings in those arrays over pasting duplicate prose.
- If an obligation appears in only one document, output one row with one document in Citation.
- Result: one row per distinct obligation; when semantically the same obligation appears in multiple documents, that single row MUST have Citation listing all of those documents. This is mandatory.

CRITICAL INSTRUCTIONS:
1. Combine all obligations from all documents into a single array. Merge into one row ONLY when obligations are semantically similar (see above). For each merged row, Citation MUST list every source document; strictly include all documents.
2. Order by similarity to the user query first (most similar obligations at the top and included in the result set), then by relevance, then by monetary value (highest amounts first).
3. For rows you do not merge (single-document or kept separate), preserve "Owner Responsibility", "Reasoning", and other fields exactly as given. For rows merged across documents per the rules above, intelligently combine "Owner Responsibility" and "Reasoning" from each contributing obligation as described — do not discard unique substance from any source document.
4. Citation: Every obligation must have "Citation" with the source document filename. For one document: "Document: [filename] | [original citation]". For merged (semantically similar) obligations from multiple documents: "Document: [file1] | [citation1] ; Document: [file2] | [citation2]" — you MUST include every source document; do not omit any. Strictly follow. Example: "Document: Commercial Lease Agreement - Buyer Triple Net.pdf | Page 2, Section 'Rent' ; Document: MTNNN.pdf | Page 3, Section 'RENT'".
5. Keep all fields: "DutyType", "Responsible Party", "Owner Responsibility", "Reasoning", "Citation"
6. Do not add, remove, or modify any other fields
7. Output ONLY valid JSON, no commentary

User Query: "{user_query}"

Filtered results from multiple documents:
{json.dumps(non_empty_results, indent=2)}

Return a JSON object with this structure:
{{
  "query": "{user_query}",
  "total_documents_searched": {len(filtered_results)},
  "total_obligations_found": <count of obligations>,
  "results": [
    // One row per obligation. Merged cross-doc rows: combined Owner Responsibility + Reasoning from each source (deduped, material facts only); Citation lists every document with " ; ". Order: similarity to query, relevance, monetary value.
  ]
}}

Output the merged and ranked JSON:"""

    async def merge_and_rank_results_stream(self, user_query: str, filtered_results: List[Dict[str, Any]], 
                              document_name_to_id: Optional[Dict[str, str]] = None) -> AsyncIterator[Dict[str, Any]]:
        """
        Streaming version: yields obligations one-by-one as they arrive from LLM.
        Yields dicts with: {"type": "obligation", "data": {...}} or {"type": "metadata", "data": {...}} or {"type": "error", "message": "..."}.
        """
        try:
            # Filter out empty results
            non_empty_results = [r for r in filtered_results if r.get("consolidated_results")]
            
            if not non_empty_results:
                yield {
                    "type": "metadata",
                    "data": {
                        "query": user_query,
                        "total_documents_searched": len(filtered_results),
                        "total_obligations_found": 0,
                    }
                }
                return
            
            merge_prompt = self._build_merge_rank_prompt(user_query, filtered_results)
            self.logger.info("[STREAM] Merge/rank LLM call - streaming response from LLM")
            from streaming_json_parser import parse_obligations_stream

            token_stream = generate_content_stream(
                prompt=merge_prompt,
                model=self.model,
                temperature=0.1,
                response_mime_type="application/json",
            )
            # Parse the streamed JSON and yield each obligation; same result as merge/rank, streamed to client.
            obligation_count = 0
            async for obligation in parse_obligations_stream(token_stream):
                obligation["Citation"] = citation_string_to_structured(obligation.get("Citation"))
                obligation_count += 1
                yield {
                    "type": "obligation",
                    "data": obligation
                }
            
            # Final metadata
            yield {
                "type": "metadata",
                "data": {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": obligation_count,
                    "processed_at": datetime.now().isoformat(),
                }
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
            non_empty_results = [r for r in filtered_results if r.get("consolidated_results")]
            self.logger.info(f"[TIMING] merge_and_rank: 1. filter_empty_results - {time.perf_counter() - t_step:.3f}s")
            
            if not non_empty_results:
                self.logger.info("No relevant obligations found across all documents")
                return {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": 0,
                    "results": [],
                    "processed_at": datetime.now().isoformat(),
                }
            
            # 2. Build full merge+rank+filter prompt (no code merge; LLM does filtering, merging, ranking)
            t_step = time.perf_counter()
            merge_prompt = self._build_merge_rank_prompt(user_query, filtered_results)
            self.logger.info(f"[TIMING] merge_and_rank: 2. build_prompt - {time.perf_counter() - t_step:.3f}s")
            self.logger.info(f"Merge and rank (LLM) for query: '{user_query}'")
            
            # 3. Call LLM API asynchronously (filter + merge + rank in one call)
            t_step = time.perf_counter()
            response = await self._generate_content_async(
                prompt=merge_prompt,
                temperature=0.1,
                response_mime_type="application/json"
            )
            self.logger.info(f"[TIMING] merge_and_rank: 3. llm_api_call - {time.perf_counter() - t_step:.3f}s")
            
            # 4. Parse JSON response (strip, strip markdown, json.loads)
            t_step = time.perf_counter()
            result_text = response.text.strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()
            final_result = json.loads(result_text)
            self.logger.info(f"[TIMING] merge_and_rank: 4. parse_response - {time.perf_counter() - t_step:.3f}s")
            
            # 5. Convert citations to structured format (in case LLM returned string)
            t_step = time.perf_counter()
            convert_result_citations_to_structured(final_result)
            self.logger.info(f"[TIMING] merge_and_rank: 5. citation_to_structured - {time.perf_counter() - t_step:.3f}s")
            
            # 6. Fix count and finish
            t_step = time.perf_counter()
            num_results = len(final_result.get("results", []))
            final_result["total_obligations_found"] = num_results
            self.logger.info(f"[TIMING] merge_and_rank: 6. fix_count - {time.perf_counter() - t_step:.3f}s")
            
            elapsed = time.perf_counter() - t0
            self.logger.info(f"[TIMING] merge_and_rank: total - {elapsed:.3f}s ({num_results} obligations)")
            return final_result
            
        except Exception as e:
            self.logger.error(f"Error merging and ranking results: {e}")
            return {
                "query": user_query,
                "total_documents_searched": len(filtered_results),
                "total_obligations_found": 0,
                "results": [],
                "processed_at": datetime.now().isoformat(),
                "error": str(e)
            }
    
    async def query(self, user_query: str, save_output: bool = True, document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Main query function - orchestrates the entire search process (ASYNC with parallel document filtering)
        
        Args:
            user_query: User's search query (if empty, returns utility-related obligations)
            save_output: Whether to save the output to a JSON file
            document_ids: Optional list of document URLs to filter by
            
        Returns:
            Final ranked results as JSON
        """
        try:
            query_start = time.perf_counter()
            self.logger.info("[TIMING] Query total - start")
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
            vector_obligations = self._query_vector_store(user_query, n_results=50, document_ids=document_ids)
            self.logger.info(f"[TIMING] Step: vector store query - done in {time.perf_counter() - t_vector:.3f}s ({len(vector_obligations)} results)")
            
            if vector_obligations:
                # Group vector results by document for merge_and_rank (same shape as LLM filter output)
                doc_to_obligations: Dict[str, List[Dict[str, Any]]] = {}
                for ob in vector_obligations:
                    cit = ob.get("Citation") or ""
                    doc_name = ""
                    if "Document:" in cit:
                        doc_name = cit.split("Document:")[1].split("|")[0].strip()
                    if not doc_name:
                        doc_name = "Unknown"
                    doc_to_obligations.setdefault(doc_name, []).append(ob)
                filtered_results_for_merge = [
                    {"document_name": doc_name, "consolidated_results": ob_list}
                    for doc_name, ob_list in doc_to_obligations.items()
                ]
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
    """Response model for query endpoint"""
    query: str
    total_documents_searched: int
    total_obligations_found: int
    processed_at: str
    results: List[Dict[str, Any]]
    error: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "query": "Landlord HVAC Hazardous Materials",
                "total_documents_searched": 3,
                "total_obligations_found": 4,
                "processed_at": "2025-01-20T10:30:00Z",
                "results": [
                    {
                        "DutyType": "Hazardous Materials Remediation",
                        "Responsible Party": "Landlord",
                        "Owner Responsibility": [
                            "Removal of hazardous materials",
                            "Environmental compliance",
                            "Safety inspections"
                        ],
                        "Reasoning": [
                            "Commercial lease standards require landlord compliance"
                        ],
                        "Citation": [
                            {
                                "docId": "url1",
                                "pageNumbers": [10, 11, 12],
                                "section": ["1e", "1c"]
                            },
                            {
                                "docId": "url2",
                                "pageNumbers": [5, 22],
                                "section": ["1d", "1b"]
                            }
                        ]
                    }
                ]
            }
        }


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
            vector_obligations = qs._query_vector_store(user_query, n_results=50, document_ids=req.document_ids)
            _echo(f"[STEP 1] Found {len(vector_obligations)} vector results\n")
            yield f"[STEP 1] Found {len(vector_obligations)} vector results\n"
            
            if vector_obligations:
                _echo("[STEP 2] Using vector results for filtering (no LLM filter)\n")
                yield "[STEP 2] Using vector results for filtering (no LLM filter)\n"
                doc_to_obligations = {}
                for ob in vector_obligations:
                    cit = ob.get("Citation") or ""
                    doc_name = ""
                    if "Document:" in cit:
                        doc_name = cit.split("Document:")[1].split("|")[0].strip()
                    if not doc_name:
                        doc_name = ob.get("document_name") or "Unknown"
                    doc_to_obligations.setdefault(doc_name, []).append(ob)
                filtered_results = [
                    {"document_name": doc_name, "consolidated_results": ob_list}
                    for doc_name, ob_list in doc_to_obligations.items()
                ]
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
            
            _echo("[STEP 3] Merging and ranking (streaming LLM tokens)...\n")
            yield "[STEP 3] Merging and ranking (streaming LLM tokens)...\n"
            _echo("="*80 + "\n")
            yield "="*80 + "\n"
            _echo("RAW LLM OUTPUT (watch it generate in real-time):\n")
            yield "RAW LLM OUTPUT (watch it generate in real-time):\n"
            _echo("="*80 + "\n")
            yield "="*80 + "\n"
            
            # Same merge/rank prompt as /query and /query/stream so results are consistent
            merge_prompt = qs._build_merge_rank_prompt(user_query, filtered_results)
            
            # Stream raw tokens (and echo to terminal so you see stream when using Postman)
            token_count = 0
            async for token in generate_content_stream(
                prompt=merge_prompt,
                model=qs.model,
                temperature=0.1,
                response_mime_type="application/json"
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
    Stream legal obligations one-by-one to the client as the LLM generates them.
    
    Yes, it is streaming: each obligation line is sent as soon as it is parsed from the
    LLM stream (using incremental JSON parsing when ijson is installed). In Postman you
    will see NDJSON lines appear over time, not all at once.
    
    Uses the same merge/rank prompt and filtering as POST /query and /query/stream/raw.
    For raw token-by-token streaming (see the JSON being typed), use POST /query/stream/raw.
    
    Returns NDJSON stream (one JSON object per line):
    - {"type": "obligation", "data": {...}} - individual obligation
    - {"type": "metadata", "data": {...}} - query metadata (total count, etc.)
    - {"type": "error", "message": "..."} - error message
    
    Each line is a JSON object followed by newline.
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")
    
    req = request or QueryRequest()
    user_query = (req.query or "") if isinstance(req.query, str) else ""
    
    async def event_generator():
        try:
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
            vector_obligations = qs._query_vector_store(user_query, n_results=50, document_ids=req.document_ids)
            elapsed_vector = time.perf_counter() - t_vector
            logging.info(f"[STREAM] Vector store returned {len(vector_obligations)} obligations in {elapsed_vector:.2f}s")
            
            if vector_obligations:
                # Use vector results for filtering: group by document and merge/rank (no LLM filter)
                logging.info(f"[STREAM] Using vector results for filtering (no per-document LLM filter)")
                doc_to_obligations: Dict[str, List[Dict[str, Any]]] = {}
                for ob in vector_obligations:
                    cit = ob.get("Citation") or ""
                    doc_name = ""
                    if "Document:" in cit:
                        doc_name = cit.split("Document:")[1].split("|")[0].strip()
                    if not doc_name:
                        doc_name = ob.get("document_name") or "Unknown"
                    doc_to_obligations.setdefault(doc_name, []).append(ob)
                filtered_results = [
                    {"document_name": doc_name, "consolidated_results": ob_list}
                    for doc_name, ob_list in doc_to_obligations.items()
                ]
            else:
                # Fallback: LLM filter each document then merge/rank
                logging.info(f"[STREAM] No vector results; using LLM filter per document")
                filter_tasks = [
                    qs.filter_obligations_by_query(
                        user_query,
                        file_info["data"],
                        file_info["document_name"]
                    )
                    for file_info in consolidated_files
                ]
                filtered_results = await asyncio.gather(*filter_tasks)
            
            total_for_merge = sum(len(r.get("consolidated_results", [])) for r in filtered_results)
            logging.info(f"[STREAM] Merge and rank LLM call - start (input: {total_for_merge} obligations from {len(filtered_results)} doc(s))")
            
            # Step 2: Stream merge and rank results (echo to terminal so you see stream when using Postman)
            obligation_stream_count = 0
            async for event in qs.merge_and_rank_results_stream(user_query, filtered_results, document_name_to_id):
                if event.get("type") == "obligation":
                    obligation_stream_count += 1
                line = json.dumps(event) + "\n"
                sys.stdout.write(line)
                sys.stdout.flush()
                yield line
            
            logging.info(f"[STREAM] Merge and rank LLM call - complete (streamed {obligation_stream_count} obligations)")
            
        except Exception as e:
            logging.error(f"Error in streaming query: {e}", exc_info=True)
            line = json.dumps({"type": "error", "message": str(e)}) + "\n"
            sys.stdout.write(line)
            sys.stdout.flush()
            yield line
    
    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.post("/process", response_model=ProcessResponse, tags=["Processing"])
async def process_document(request: ProcessRequest):
    """Process all PDFs from docs folder. Writes (1) section-based obligations JSON, (2) consolidated JSON to output folder. Extraction is section-only (regex: ^\\d+\\. , ^\\([a-z]\\) , ^\\(\\d+\\)). Uses Azure OpenAI or Gemini via llm_client."""
    try:
        from process_legal_documents import LegalDocumentProcessor

        docs_folder = request.docs_folder or os.getenv('DOCS_FOLDER', 'docs')
        out_folder = request.output_folder or os.getenv('OUTPUT_FOLDER', 'output')
        logging.info(f"Processing documents from: {docs_folder}")
        logging.info(f"Output will be saved to: {out_folder}")
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

