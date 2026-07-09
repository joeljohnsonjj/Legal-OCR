#!/usr/bin/env python3
"""
Post-processing module for citation fixing
Automatically fixes citations in consolidated files using pagewise data

This module should be integrated into the main processing pipeline
"""

import json
import re
import logging
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
from difflib import SequenceMatcher
from citation_utils import _parse_page_numbers, _parse_sections
from processing_results import normalize_responsible_party_for_obligation

logger = logging.getLogger(__name__)

def normalize_text(text: str) -> str:
    """Normalize text for comparison"""
    text = re.sub(r'[^\w\s]', ' ', text.lower())
    text = ' '.join(text.split())
    return text

def calculate_similarity(text1: str, text2: str) -> float:
    """Calculate similarity between two texts"""
    norm1 = normalize_text(text1)
    norm2 = normalize_text(text2)
    return SequenceMatcher(None, norm1, norm2).ratio()

def _party_category_lookup_keys(
    party_canon: str,
    party_raw: str,
    category: str,
) -> List[str]:
    """
    Build one or two index keys so consolidated obligations match whether
    ``Responsible Party`` is a role label, a legal name, or both.
    """
    cat_norm = normalize_text(category)
    keys: List[str] = []
    k_canon = f"{normalize_text(party_canon)}|{cat_norm}"
    keys.append(k_canon)
    raw = (party_raw or "").strip()
    if raw:
        k_raw = f"{normalize_text(raw)}|{cat_norm}"
        if k_raw not in keys:
            keys.append(k_raw)
    return keys


def extract_pagewise_responsibility_citations(pagewise_data: Dict[str, Any]) -> Dict[str, List[Dict]]:
    """
    Extract pagewise responsibilities with citations, grouped by (party, category).
    Returns: { "<party_norm>|<category_norm>": [ { text_norm, original_text, page, section }, ... ] }

    Each row is indexed under both canonical party (for role-style rows) and raw
    ``Responsible Party`` (for legal-name rows) so downstream merge can match either.
    """
    responsibility_citations: Dict[str, List[Dict[str, Any]]] = {}
    party_meta = pagewise_data.get("party_metadata")

    page_results = pagewise_data.get("page_results", {})
    
    for page_num, page_data in page_results.items():
        if not page_data:
            continue
            
        for category_group in page_data:
            category = str(category_group.get("category", "Unknown") or "Unknown").strip()
            obligations = category_group.get("obligations", [])
            
            for obligation in obligations:
                party_canon = normalize_responsible_party_for_obligation(
                    obligation, party_metadata=party_meta
                )
                party_raw = str(obligation.get("Responsible Party") or "").strip()
                lookup_keys = _party_category_lookup_keys(party_canon, party_raw, category)
                responsibilities = obligation.get("Owner Responsibility", [])
                citations = obligation.get("citations", [])
                
                for i, responsibility in enumerate(responsibilities):
                    if i < len(citations):
                        citation = citations[i]
                    elif len(citations) == 1:
                        citation = citations[0]
                    elif len(citations) > 0:
                        citation = citations[-1]
                    else:
                        continue
                    
                    if isinstance(citation, dict):
                        pages_from_pn = _parse_page_numbers(
                            citation.get("pageNumbers") or citation.get("page_numbers")
                        )
                        if pages_from_pn:
                            section_vals = _parse_sections(citation.get("section"))
                            if not section_vals:
                                section_vals = [
                                    str(citation.get("section", "Document") or "Document").strip()
                                    or "Document"
                                ]
                            for page_i in pages_from_pn:
                                for sec_one in section_vals:
                                    row = {
                                        "page": page_i,
                                        "section": str(sec_one).strip() or "Document",
                                        "original_text": str(responsibility or "").strip(),
                                        "text_norm": normalize_text(str(responsibility or "")),
                                    }
                                    for key in lookup_keys:
                                        responsibility_citations.setdefault(key, []).append(row.copy())
                            continue
                        try:
                            page_i = int(citation.get("page", 1))
                        except (TypeError, ValueError):
                            page_i = 1
                        row = {
                            "page": page_i,
                            "section": str(citation.get("section", "Document") or "Document").strip() or "Document",
                            "original_text": str(responsibility or "").strip(),
                            "text_norm": normalize_text(str(responsibility or "")),
                        }
                        for key in lookup_keys:
                            responsibility_citations.setdefault(key, []).append(row.copy())
    
    return responsibility_citations

def _collapse_citations_to_compact(citations: List[Dict[str, Any]]) -> Dict[str, Any]:
    pages: List[int] = []
    secs: List[str] = []
    seen_pages: set = set()
    seen_secs: set = set()
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        try:
            p = int(c.get("page") or 0)
        except (TypeError, ValueError):
            p = 0
        s = str(c.get("section") or "").strip()
        if p > 0 and p not in seen_pages:
            seen_pages.add(p)
            pages.append(p)
        if s and s.lower() not in seen_secs:
            seen_secs.add(s.lower())
            secs.append(s)
    return {"pageNumbers": pages or [1], "section": secs or ["Document"]}


def find_contributing_citations(
    consolidated_resp: str,
    party_norm: str,
    category_norm: str,
    pagewise_index: Dict[str, List[Dict[str, Any]]],
    *,
    min_similarity: float = 0.3,
) -> List[Dict[str, Any]]:
    """
    Hardcoded, multi-source citation selection:
    - Collect contributors if similarity > 0.6 OR containment for short clauses (>=3 tokens).
    - If none, fall back to single best match if similarity >= min_similarity.
    Returns list of {page, section}.
    """
    cons_text = str(consolidated_resp or "").strip()
    cons_norm = normalize_text(cons_text)
    cons_tokens = cons_norm.split()
    cons_has_base_rent = ("base" in cons_tokens) and ("rent" in cons_tokens)
    key = f"{party_norm}|{category_norm}"
    candidates = list(pagewise_index.get(key) or [])

    contributing: List[Dict[str, Any]] = []
    best: Optional[Tuple[float, Dict[str, Any]]] = None

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        orig_text = str(cand.get("original_text") or "")
        orig_norm = str(cand.get("text_norm") or normalize_text(orig_text))
        orig_tokens = orig_norm.split()
        sim = calculate_similarity(cons_text, orig_text)
        if best is None or sim > best[0]:
            best = (sim, cand)
        token_len = len(orig_norm.split())
        containment = bool(orig_norm) and token_len >= 3 and (orig_norm in cons_norm)
        # Token subsequence: allow extra words inserted in consolidated text
        subseq = False
        if token_len >= 3 and cons_tokens:
            i = 0
            matched = 0
            for t in cons_tokens:
                if i < len(orig_tokens) and t == orig_tokens[i]:
                    matched += 1
                    i += 1
                    if i >= len(orig_tokens):
                        break
            coverage = matched / max(1, len(orig_tokens))
            subseq = matched >= 3 and coverage >= 0.8

        # Token-overlap heuristic for partial merges (paraphrases / inserted qualifiers):
        # include as contributing when many "content tokens" overlap, even if sim < 0.6.
        stop = {
            "the","a","an","and","or","of","to","in","on","with","without","for","as","at","by","if",
            "within","after","before","during","over","under","up","including","per","month","months",
            "day","days","any","all","be","become","due","when","made","make","pay","paid",
        }
        cons_set = {t for t in cons_tokens if t and t not in stop}
        orig_set = {t for t in orig_tokens if t and t not in stop}
        overlap = cons_set.intersection(orig_set)
        overlap_count = len(overlap)
        overlap_cov = (overlap_count / max(1, len(orig_set))) if orig_set else 0.0
        overlap_ok = (overlap_count >= 3 and overlap_cov >= 0.5 and sim >= 0.38)

        # Anchor: if consolidated mentions "base rent", prefer contributors that also mention it
        anchor_ok = True
        if cons_has_base_rent and not (("base" in orig_tokens) and ("rent" in orig_tokens)):
            anchor_ok = sim >= 0.6  # only allow via strong similarity

        if anchor_ok and (sim > 0.6 or containment or subseq or overlap_ok):
            contributing.append({"page": cand.get("page", 1), "section": cand.get("section", "Document")})

    if contributing:
        # de-dupe by (page, section)
        seen = set()
        out: List[Dict[str, Any]] = []
        for c in contributing:
            try:
                p = int(c.get("page") or 1)
            except (TypeError, ValueError):
                p = 1
            s = str(c.get("section") or "Document").strip() or "Document"
            k = (p, s.lower())
            if k in seen:
                continue
            seen.add(k)
            out.append({"page": p, "section": s})
        return out

    if best and best[0] >= min_similarity:
        cand = best[1]
        return [{"page": int(cand.get("page") or 1), "section": str(cand.get("section") or "Document")}]

    return []

def fix_consolidated_citations_in_place(
    consolidated_file_path: str,
    pagewise_file_path: str,
    logger_obj = None
) -> Tuple[bool, Dict[str, Any]]:
    """
    Fix citations in consolidated file using pagewise data
    
    Args:
        consolidated_file_path: Path to consolidated JSON file
        pagewise_file_path: Path to pagewise JSON file
        logger_obj: Optional logger object
    
    Returns:
        (success: bool, stats: dict)
    """
    
    if logger_obj is None:
        logger_obj = logger
    
    try:
        logger_obj.info(f"Post-processing citations for: {consolidated_file_path}")
        
        # Load both files
        with open(pagewise_file_path, 'r', encoding='utf-8') as f:
            pagewise_data = json.load(f)
        
        with open(consolidated_file_path, 'r', encoding='utf-8') as f:
            consolidated_data = json.load(f)
        
        # Extract pagewise citations
        logger_obj.info("Extracting citation mappings from pagewise data...")
        party_meta = pagewise_data.get("party_metadata")
        pagewise_citations = extract_pagewise_responsibility_citations(pagewise_data)
        logger_obj.info(f"Found {len(pagewise_citations)} unique responsibility-citation mappings")
        
        # Fix citations in consolidated data
        logger_obj.info("Fixing consolidated citations...")
        
        fixed_count = 0
        total_responsibilities = 0
        pages_found = set()
        
        for category_group in consolidated_data.get("results", []):
            category = str(category_group.get("category", "Unknown") or "Unknown").strip()
            category_norm = normalize_text(category)
            
            for obligation in category_group.get("obligations", []):
                party_norm = normalize_text(
                    normalize_responsible_party_for_obligation(
                        obligation, party_metadata=party_meta
                    )
                )
                responsibilities = obligation.get("Owner Responsibility", [])
                citations = obligation.get("citations", [])
                
                new_citations = []
                
                for i, responsibility in enumerate(responsibilities):
                    total_responsibilities += 1
                    
                    # Find contributing citations from pagewise data (multi-source for merged lines)
                    cites = find_contributing_citations(
                        responsibility, party_norm, category_norm, pagewise_citations
                    )
                    if cites:
                        new_citations.append(_collapse_citations_to_compact(cites))
                        fixed_count += 1
                        for c in cites:
                            pages_found.add(c.get("page", 1))
                        logger_obj.debug(f"Fixed: {party_norm} | {responsibility[:50]}... -> {new_citations[-1]}")
                    else:
                        # Keep original citation if no match found, but normalize to compact format
                        if i < len(citations):
                            existing = citations[i]
                            # existing could be old [[{page,section}]] or [{page,section}] or compact dict
                            if isinstance(existing, dict) and ("pageNumbers" in existing or isinstance(existing.get("section"), list)):
                                new_citations.append(existing)
                            elif isinstance(existing, list):
                                flat: List[Dict[str, Any]] = []
                                for item in existing:
                                    if isinstance(item, dict):
                                        flat.append(item)
                                    elif isinstance(item, list):
                                        flat.extend(x for x in item if isinstance(x, dict))
                                new_citations.append(_collapse_citations_to_compact(flat))
                            elif isinstance(existing, dict):
                                new_citations.append(_collapse_citations_to_compact([existing]))
                            else:
                                new_citations.append({"pageNumbers": [1], "section": ["Document"]})
                        else:
                            new_citations.append({"pageNumbers": [1], "section": ["Document"]})
                        logger_obj.debug(f"No match: {party_norm} | {responsibility[:50]}...")
                
                # Update citations
                obligation["citations"] = new_citations
        
        # Save fixed file back (in-place)
        with open(consolidated_file_path, 'w', encoding='utf-8') as f:
            json.dump(consolidated_data, f, indent=2, ensure_ascii=False)
        
        success_rate = (fixed_count / total_responsibilities * 100) if total_responsibilities > 0 else 0
        
        stats = {
            "total_responsibilities": total_responsibilities,
            "citations_fixed": fixed_count,
            "success_rate": success_rate,
            "pages_found": sorted(list(pages_found))
        }
        
        logger_obj.info(f"Citation fixing complete!")
        logger_obj.info(f"  Total responsibilities: {total_responsibilities}")
        logger_obj.info(f"  Citations fixed: {fixed_count}")
        logger_obj.info(f"  Success rate: {success_rate:.1f}%")
        logger_obj.info(f"  Pages found: {stats['pages_found']}")
        
        return True, stats
        
    except Exception as e:
        logger_obj.error(f"Error fixing citations: {e}", exc_info=True)
        return False, {"error": str(e)}


# Integration function for use in main processing pipeline
def apply_citation_post_processing(output_folder: str, document_name: str, logger_obj=None):
    """
    Apply citation post-processing to a processed document
    
    Call this AFTER consolidated JSON is saved in the main processing pipeline
    
    Args:
        output_folder: Output folder where files are saved
        document_name: Name of the document (e.g., "mydoc.pdf")
        logger_obj: Optional logger object
    """
    
    if logger_obj is None:
        logger_obj = logger
    
    try:
        # Find the most recent pagewise and consolidated files
        output_path = Path(output_folder)
        
        # Get all pagewise and consolidated files for this document
        base_name = document_name.replace('.pdf', '')
        
        pagewise_files = list(output_path.glob(f"{base_name}*_pagewise.json"))
        consolidated_files = list(output_path.glob(f"{base_name}*_consolidated.json"))
        
        if not pagewise_files or not consolidated_files:
            logger_obj.warning(f"Could not find pagewise or consolidated files for {document_name}")
            return False, {}
        
        # Use the most recent files (sorted by modification time)
        pagewise_file = sorted(pagewise_files, key=lambda x: x.stat().st_mtime)[-1]
        consolidated_file = sorted(consolidated_files, key=lambda x: x.stat().st_mtime)[-1]
        
        logger_obj.info(f"Applying citation post-processing...")
        logger_obj.info(f"  Pagewise: {pagewise_file.name}")
        logger_obj.info(f"  Consolidated: {consolidated_file.name}")
        
        success, stats = fix_consolidated_citations_in_place(
            str(consolidated_file),
            str(pagewise_file),
            logger_obj
        )
        
        if success:
            logger_obj.info(f"Citation post-processing completed successfully")
            logger_obj.info(f"Fixed consolidated file: {consolidated_file.name}")
        else:
            logger_obj.warning(f"Citation post-processing failed: {stats.get('error', 'Unknown error')}")
        
        return success, stats
        
    except Exception as e:
        logger_obj.error(f"Error in citation post-processing: {e}", exc_info=True)
        return False, {"error": str(e)}


if __name__ == "__main__":
    # For standalone testing
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python citation_post_processor.py <consolidated_file> <pagewise_file>")
        sys.exit(1)
    
    consolidated = sys.argv[1]
    pagewise = sys.argv[2]
    
    logging.basicConfig(level=logging.INFO)
    success, stats = fix_consolidated_citations_in_place(consolidated, pagewise)
    
    if success:
        print(f"\nSuccess! Fixed {stats['citations_fixed']}/{stats['total_responsibilities']} citations")
        print(f"Success rate: {stats['success_rate']:.1f}%")
    else:
        print(f"\nFailed: {stats.get('error', 'Unknown error')}")
        sys.exit(1)
