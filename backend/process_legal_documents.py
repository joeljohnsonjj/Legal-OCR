"""
Legal Document Processing System
Extracts text from PDFs and analyzes financial obligations using Google Gemini AI
"""

import os
import re
import json
import logging
from pathlib import Path
from datetime import datetime
from force_terminal_logger import force_logger
from typing import Any, Dict, List, Optional
import io
import tempfile
import time
from functools import wraps

# PDF Processing
import PyPDF2
from pdf2image import convert_from_path
from PIL import Image
import pytesseract

# LLM API (Azure OpenAI or Gemini via llm_client)
from llm_client import generate_content as llm_generate_content, get_default_model, use_bedrock_llm

from processing_results import (
    broaden_related_keywords,
    build_root_citations_api_shape,
    count_obligations_in_results,
    flatten_processing_results_for_index,
    merge_duplicate_party_within_category,
    normalize_results_categories,
    _merge_str_lists,
)
from processing_taxonomy import merge_extraction_category_groups
from citation_utils import combine_citations_to_comma_separated, _parse_page_numbers, _parse_sections

# Environment Variables
from dotenv import load_dotenv
load_dotenv()

_proc_logger = logging.getLogger(__name__)
try:
    from aws_parameter_loader import init_llm_env
    init_llm_env()
except Exception:
    pass

if not os.getenv("LITELLM_MODEL") and os.getenv("LLM_MODEL"):
    _lm = (os.getenv("LLM_MODEL") or "").strip()
    if _lm:
        os.environ["LITELLM_MODEL"] = _lm
        _proc_logger.info("Fallback: LITELLM_MODEL set from LLM_MODEL=%s", _lm)

_REPO_ROOT = Path(__file__).resolve().parent


def _obligation_pages_for_rag(consolidated_results: List[Dict[str, Any]]) -> List[int]:
    """
    Best-effort page number per obligation for Qdrant indexing (_source_page, Citation.pageNumbers, or regex).
    Falls back to 1.
    """
    out: List[int] = []
    for ob in consolidated_results:
        sp = ob.get("_source_page")
        if sp is not None:
            try:
                out.append(int(sp))
                continue
            except (TypeError, ValueError):
                pass
        cit = ob.get("Citation")
        found: Optional[int] = None
        if isinstance(cit, list):
            for c in cit:
                if isinstance(c, dict):
                    pgs = c.get("pageNumbers") or []
                    if pgs:
                        try:
                            found = int(pgs[0])
                            break
                        except (TypeError, ValueError, IndexError):
                            pass
        if found is None:
            m = re.search(r"[Pp]age\s+(\d+)", str(cit))
            if m:
                try:
                    found = int(m.group(1))
                except ValueError:
                    pass
        out.append(found if found is not None else 1)
    return out


def _configured_llm_label() -> str:
    """Human-readable LLM provider for logs (matches llm_client routing)."""
    if os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes"):
        return "Azure OpenAI"
    if use_bedrock_llm():
        return "AWS Bedrock"
    return "Gemini API"


def _analyze_extraction_max_output_tokens() -> Optional[int]:
    """
    Optional cap for obligation extraction (analyze_page / analyze_section).

    Set LEGAL_OCR_ANALYZE_PAGE_MAX_OUTPUT_TOKENS (or LEGAL_OCR_ANALYZE_SECTION_MAX_OUTPUT_TOKENS)
    to raise the limit when JSON is truncated or empty. If unset, each provider uses its own
    default (e.g. GEMINI_MAX_OUTPUT_TOKENS, BEDROCK_MAX_TOKENS, AZURE_OPENAI_MAX_TOKENS / OPENAI_MAX_OUTPUT_TOKENS).
    """
    for key in ("LEGAL_OCR_ANALYZE_PAGE_MAX_OUTPUT_TOKENS", "LEGAL_OCR_ANALYZE_SECTION_MAX_OUTPUT_TOKENS"):
        raw = (os.getenv(key) or "").strip()
        if not raw:
            continue
        try:
            return max(256, int(raw))
        except ValueError:
            continue
    return None


def _resolve_workspace_path(path_str: Optional[str], env_key: str, default: str) -> Path:
    """Resolve DOCS_FOLDER/OUTPUT_FOLDER relative to repo root, not the process CWD."""
    raw = (path_str or os.getenv(env_key) or default).strip()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (_REPO_ROOT / p).resolve()


def _is_category_group_block(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if not str(obj.get("category") or "").strip():
        return False
    return isinstance(obj.get("obligations"), list)


def _parse_extraction_category_groups(parsed: Any) -> List[Dict[str, Any]]:
    """Parse LLM JSON into [{ category, obligations }, ...]."""
    out: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if _is_category_group_block(item):
                out.append(dict(item))
        return out
    if isinstance(parsed, dict):
        if _is_category_group_block(parsed):
            return [dict(parsed)]
        for key in ("extraction_results", "results", "categories", "category_groups", "data"):
            inner = parsed.get(key)
            if isinstance(inner, list):
                for item in inner:
                    if _is_category_group_block(item):
                        out.append(dict(item))
                if out:
                    return out
    return []


def _count_inner_obligation_rows(category_groups: List[Dict[str, Any]]) -> int:
    return sum(len(g.get("obligations") or []) for g in category_groups if isinstance(g, dict))


def _enrich_category_groups_citations(
    groups: List[Dict[str, Any]],
    *,
    page_num: Optional[int],
    document_name: str,
) -> None:
    """Ensure docId and per-page references exist for consolidation."""
    doc = (document_name or "").strip() or "document.pdf"
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        for ob in grp.get("obligations") or []:
            if not isinstance(ob, dict):
                continue
            if ob.get("citations") is None and isinstance(ob.get("Citation"), list):
                ob["citations"] = ob.pop("Citation")
            blocks = ob.get("citations")
            if not isinstance(blocks, list):
                blocks = []
            # Clean citation format: only page and section, no docId or references
            new_blocks: List[Dict[str, Any]] = []
            for c in blocks:
                if not isinstance(c, dict):
                    continue
                
                # Extract page and section only
                page = c.get("page", page_num if page_num is not None else 1)
                section = c.get("section", "")
                
                try:
                    page = int(page)
                except (TypeError, ValueError):
                    page = page_num if page_num is not None else 1
                
                clean_citation = {
                    "page": page,
                    "section": section
                }
                new_blocks.append(clean_citation)
                
            if not new_blocks and page_num is not None:
                new_blocks = [{"page": int(page_num), "section": ""}]
            elif not new_blocks:
                new_blocks = [{"page": 1, "section": ""}]
            
            ob["citations"] = new_blocks


def _strip_llm_json_response(text: str) -> str:
    t = (text or "").strip()
    for prefix in ("```json", "```"):
        if t.startswith(prefix):
            t = t[len(prefix) :].lstrip()
            break
    if t.endswith("```"):
        t = t[:-3].strip()
    return t.strip()


def _is_lazy_reasoning(s: Any) -> bool:
    t = str(s or "").strip().lower()
    if not t:
        return True
    if t in {"legal obligation", "standard obligation", "standard contractual obligation", "not specified"}:
        return True
    # Meta / prompt-defending patterns
    meta_markers = (
        "each responsibility represents",
        "distinct financial scenario",
        "maintaining granular language",
        "kept separate",
        "kept distinct",
        "consolidates",
        "merged",
        "highly similar",
        "output",
        "input",
        "prompt",
        "rule",
        "unique financial",
        "granular language",
        "clear contractual expectations",
    )
    return any(m in t for m in meta_markers)


def _auto_reasoning_from_responsibility(resp: Any, *, party: str = "", category: str = "") -> str:
    r = str(resp or "").strip()
    low = r.lower()
    p = str(party or "").strip() or "the responsible party"
    cat = str(category or "").strip().lower()

    # Financial / Rent / Expenses
    if "base rent" in low or (("rent" in low) and ("pay" in low)):
        return f"Allocates {p}'s duty to pay rent as consideration for occupying the premises under the lease."
    if "operating expense" in low or "operating expenses" in low or "cam" in low or "common area" in low:
        return f"Allocates {p}'s responsibility for operating/common area costs as part of the lease's cost-allocation structure."
    if "real estate tax" in low or "taxes" in low or "assessment" in low:
        return f"Assigns {p} the obligation to pay property-related taxes/assessments identified in the lease."
    if "insurance" in low or "policy" in low or "coverage" in low:
        return f"Requires {p} to maintain the specified insurance coverage to manage risk and financial exposure during the lease term."
    if "holdover" in low or "125%" in low:
        return f"Imposes a holdover charge on {p} to compensate for occupancy beyond the lease term and deter overstaying."

    # Indemnity / reimbursement / remedies
    if "indemnif" in low or "hold harmless" in low:
        return f"Shifts thirdΓÇæparty claim costs and related financial exposure to {p} through an indemnity obligation."
    if "reimburse" in low or "refund" in low or "credit" in low:
        return f"Requires {p} to reimburse/refund amounts to correct cost allocations or compensate the other party for covered expenditures."
    if "offset" in low:
        return f"Provides {p} a financial remedy (offset) to recover amounts spent or owed under the lease."

    # Maintenance / Repairs / Utilities / Alterations / Signs
    if cat in {"maintenance & repairs", "building structure & envelope"} or "repair" in low or "maintain" in low:
        return f"Assigns maintenance/repair cost responsibility to {p} for the systems/areas described in the lease."
    if cat == "utilities" or any(x in low for x in ("water", "gas", "electric", "electricity", "sewer", "telephone", "utility")):
        return f"Allocates utility service provision/payment responsibility to {p} for the premises."
    if "alteration" in low or "improvement" in low or "restore" in low:
        return f"Allocates alteration/improvement and restoration costs to {p} for changes made to the premises."
    if "sign" in low or "signage" in low:
        return f"Allocates signage installation and maintenance costs to {p} as part of branding/use of the premises."

    # Compliance / Environmental
    if any(x in low for x in ("hazard", "contamination", "environment", "laws", "comply", "ordinance")):
        return f"Assigns compliance/environmental cost and liability responsibility to {p} for the risks described in the lease."

    return f"Allocates the financial responsibility described to {p} under the lease."


def _sanitize_reasonings_in_obligation(ob: Dict[str, Any], *, category: str = "") -> None:
    """Make Reasoning responsibility-specific; remove filler/meta entries."""
    if not isinstance(ob, dict):
        return
    party = str(ob.get("Responsible Party") or "").strip()
    resps = ob.get("Owner Responsibility") or []
    reasons = ob.get("Reasoning") or []
    if not isinstance(resps, list):
        return
    if not isinstance(reasons, list):
        reasons = []

    new_reasons: List[str] = []
    for i, r in enumerate(reasons):
        s = str(r).strip()
        if not s:
            continue
        s_norm = s.lower()
        # Keep explicit "not specified" if pagewise provided it.
        if _is_lazy_reasoning(s) and s_norm not in {"not specified", "not specified."}:
            continue
        new_reasons.append(s)

    # No auto-fallbacks: keep only what pagewise provided.
    ob["Reasoning"] = new_reasons


# Exponential backoff retry decorator
def retry_with_exponential_backoff(
    max_retries: int = 5,
    initial_delay: float = 1.0,
    exponential_base: float = 2.0,
    jitter: bool = True
):
    """
    Decorator to retry a function with exponential backoff
    
    Args:
        max_retries: Maximum number of retry attempts (default: 5)
        initial_delay: Initial delay in seconds (default: 1.0)
        exponential_base: Base for exponential backoff (default: 2.0)
        jitter: Whether to add random jitter to delay (default: True)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger = logging.getLogger(__name__)
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    error_str = str(e).lower()
                    
                    # Check if it's a rate limiting error
                    is_rate_limit = any(keyword in error_str for keyword in [
                        'rate limit', 'quota', 'resource exhausted', 'resource_exhausted',
                        'too many requests', '429', 'throttl'
                    ])
                    
                    if not is_rate_limit or attempt == max_retries - 1:
                        # If not a rate limit error or last attempt, raise the exception
                        raise
                    
                    # Calculate delay with exponential backoff
                    delay = initial_delay * (exponential_base ** attempt)
                    
                    # Add jitter to prevent thundering herd
                    if jitter:
                        import random
                        delay = delay * (0.5 + random.random())
                    
                    logger.warning(
                        f"Rate limit hit (attempt {attempt + 1}/{max_retries}). "
                        f"Retrying in {delay:.2f} seconds... Error: {e}"
                    )
                    
                    time.sleep(delay)
            
            # This should never be reached, but just in case
            raise Exception(f"Max retries ({max_retries}) exceeded")
        
        return wrapper
    return decorator


class PDFProcessor:
    """Handles PDF text extraction - both text-based and scanned documents"""
    
    def __init__(self, tesseract_cmd: Optional[str] = None, cache_folder: str = "ocr_cache", poppler_path: Optional[str] = None):
        """
        Initialize PDF Processor
        
        Args:
            tesseract_cmd: Path to tesseract executable (optional)
            cache_folder: Folder to cache OCR results
            poppler_path: Path to poppler binaries (optional, for PDF to image conversion)
        """
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        
        self.poppler_path = poppler_path
        self.logger = logging.getLogger(__name__)
        self.cache_folder = Path(cache_folder)
        self.cache_folder.mkdir(exist_ok=True)
    
    def is_text_based_pdf(self, pdf_path: str) -> bool:
        """
        Check if PDF contains extractable text
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            True if PDF contains text, False if scanned
        """
        try:
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                
                # Check first few pages for text
                pages_to_check = min(3, len(pdf_reader.pages))
                
                for i in range(pages_to_check):
                    text = pdf_reader.pages[i].extract_text()
                    # If we find meaningful text (more than 50 chars), it's text-based
                    if text and len(text.strip()) > 50:
                        return True
                
                return False
        except Exception as e:
            error_msg = str(e).lower()
            # If error is due to encryption/crypto, assume text-based and let extract_text_from_pdf handle it
            if 'pycryptodome' in error_msg or 'crypto' in error_msg or 'aes' in error_msg:
                self.logger.warning(f"Encryption detected during PDF type check, assuming text-based: {e}")
                return True
            self.logger.error(f"Error checking PDF type: {e}")
            # Default to text-based to avoid unnecessary OCR attempts
            return True
    
    def extract_text_from_pdf(self, pdf_path: str) -> Dict[int, str]:
        """
        Extract text from text-based PDF
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            Dictionary mapping page numbers to extracted text
        """
        page_texts = {}
        
        try:
            with open(pdf_path, 'rb') as file:
                pdf_reader = PyPDF2.PdfReader(file)
                
                for page_num in range(len(pdf_reader.pages)):
                    text = pdf_reader.pages[page_num].extract_text()
                    page_texts[page_num + 1] = text  # 1-indexed pages
                    
            self.logger.info(f"Extracted text from {len(page_texts)} pages")
            return page_texts
            
        except Exception as e:
            self.logger.error(f"Error extracting text from PDF: {e}")
            return {}
    
    def extract_text_from_scanned_pdf(self, pdf_path: str) -> Dict[int, str]:
        """
        Extract text from scanned PDF using OCR
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            Dictionary mapping page numbers to extracted text
        """
        page_texts = {}
        
        try:
            # Convert PDF to images
            self.logger.info("Converting PDF to images for OCR...")
            if self.poppler_path:
                images = convert_from_path(pdf_path, poppler_path=self.poppler_path)
            else:
                images = convert_from_path(pdf_path)
            
            # Process each page
            for page_num, image in enumerate(images, start=1):
                self.logger.info(f"Processing page {page_num} with OCR...")
                text = pytesseract.image_to_string(image)
                page_texts[page_num] = text
            
            self.logger.info(f"OCR completed for {len(page_texts)} pages")
            return page_texts
            
        except Exception as e:
            self.logger.error(f"Error performing OCR on PDF: {e}")
            return {}
    
    def get_cache_path(self, pdf_path: str) -> Path:
        """Get the cache file path for a PDF"""
        pdf_name = Path(pdf_path).stem
        return self.cache_folder / f"{pdf_name}_ocr.txt"
    
    def load_cached_text(self, pdf_path: str) -> Optional[Dict[int, str]]:
        """Load cached OCR text if available"""
        cache_path = self.get_cache_path(pdf_path)
        
        if cache_path.exists():
            try:
                page_texts = {}
                with open(cache_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Parse the text file format: === PAGE X === followed by text
                current_page = None
                current_text = []
                
                for line in content.split('\n'):
                    if line.startswith('=== PAGE ') and line.endswith(' ==='):
                        # Save previous page if exists
                        if current_page is not None:
                            page_texts[current_page] = '\n'.join(current_text)
                        
                        # Start new page
                        page_num_str = line.replace('=== PAGE ', '').replace(' ===', '').strip()
                        current_page = int(page_num_str)
                        current_text = []
                    else:
                        current_text.append(line)
                
                # Save last page
                if current_page is not None:
                    page_texts[current_page] = '\n'.join(current_text)
                
                self.logger.info(f"Loaded cached OCR text from: {cache_path}")
                return page_texts
            except Exception as e:
                self.logger.warning(f"Failed to load cache: {e}")
                return None
        
        return None
    
    def save_cached_text(self, pdf_path: str, page_texts: Dict[int, str]):
        """Save OCR text to cache"""
        cache_path = self.get_cache_path(pdf_path)
        
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                for page_num in sorted(page_texts.keys()):
                    f.write(f"=== PAGE {page_num} ===\n")
                    f.write(page_texts[page_num])
                    f.write("\n\n")
            
            self.logger.info(f"Saved OCR text to cache: {cache_path}")
        except Exception as e:
            self.logger.warning(f"Failed to save cache: {e}")
    
    def process_pdf(self, pdf_path: str) -> Dict[int, str]:
        """
        Process PDF and extract text (auto-detects text-based vs scanned)
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            Dictionary mapping page numbers to extracted text
        """
        message = f"≡ƒöä PROCESSING PDF: {pdf_path}"
        self.logger.info(message)
        force_logger.info(message)  # Force terminal output
        
        # Check for cached OCR text first
        cached_text = self.load_cached_text(pdf_path)
        if cached_text:
            self.logger.info("Using cached OCR text (skipping OCR)")
            return cached_text
        
        if self.is_text_based_pdf(pdf_path):
            self.logger.info("PDF is text-based, using direct text extraction")
            page_texts = self.extract_text_from_pdf(pdf_path)
        else:
            self.logger.info("PDF is scanned, using OCR")
            page_texts = self.extract_text_from_scanned_pdf(pdf_path)
            
            # Save OCR results to cache
            if page_texts:
                self.save_cached_text(pdf_path, page_texts)
        
        return page_texts

    # Top-level section headers only: 1. 2. 3. (no Section 1.1 / Article dotted-number hierarchy)
    _SECTION_TOP = re.compile(r"^\s*(\d+)\.\s*(.*)$", re.MULTILINE)
    # Subsection patterns (one level per section, type from first occurrence)
    _LV1_LETTER_PAREN = re.compile(r"^\s*\(([a-z])\)\s*(.*)$", re.MULTILINE)
    _LV1_LETTER_DOT = re.compile(r"^\s*([a-z])\.\s*(.*)$", re.MULTILINE)
    _LV2_ROMAN = re.compile(r"^\s*\((i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)\)\s*(.*)$", re.MULTILINE)
    _LV3_UPERCASE_PAREN = re.compile(r"^\s*\(([A-Z])\)\s*(.*)$", re.MULTILINE)
    _LV3_UPERCASE_DOT = re.compile(r"^\s*([A-Z])\.\s*(.*)$", re.MULTILINE)
    _LV4_NUM = re.compile(r"^\s*\((\d+)\)\s*(.*)$", re.MULTILINE)

    def extract_document_structure(
        self, full_text: str
    ) -> Dict[str, Any]:
        """
        Extract sections from lines starting with 1. 2. 3. etc. (one level only).
        Leaf sections get (a),(b) or (1),(2) etc. subsections (one type per section, from first marker).
        """
        # Collect headers: (start, end, level, section_number, section_title); level always 0
        raw = []
        for m in self._SECTION_TOP.finditer(full_text):
            raw.append((m.start(), m.end(), 0, m.group(1), (m.group(2) or "").strip()))
        raw.sort(key=lambda x: x[0])
        seen_start = set()
        merged = []
        for start, end, level, num, title in raw:
            if start in seen_start:
                continue
            seen_start.add(start)
            merged.append((start, end, level, num, title))

        if not merged:
            return {"sections": []}

        # Content end for each: next header at same or shallower level
        content_ends = []
        for i, (start, end, level, _num, _title) in enumerate(merged):
            j = i + 1
            while j < len(merged):
                if merged[j][2] <= level:
                    content_ends.append(merged[j][0])
                    break
                j += 1
            else:
                content_ends.append(len(full_text))

        # Build tree: stack, pop until parent level < current, then append and push
        root = {"section_number": "", "section_title": "", "content": "", "level": -1, "subsections": []}
        stack = [root]
        for i, (start, end, level, sec_num, title) in enumerate(merged):
            content = full_text[end:content_ends[i]].strip()
            node = {
                "section_number": sec_num,
                "section_title": title,
                "content": content,
                "start_offset": start,
                "end_offset": content_ends[i],
                "level": level,
                "subsections": [],
            }
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            stack[-1]["subsections"].append(node)
            stack.append(node)

        # Top-level nodes (level 0) are the sections list
        sections = root["subsections"]
        # For each section, add (a),(b) or (1),(2) etc. subsections from content (one type per section)
        for sec in self._iter_sections(sections):
            if not sec.get("subsections") and sec.get("content", "").strip():
                sec["subsections"] = self._parse_subsections_hierarchical(sec["content"])
        return {"sections": sections}

    def _iter_sections(self, sections: List[Dict[str, Any]]):
        """Yield every section node in the tree (for applying (a),(b) to leaves)."""
        for s in sections:
            yield s
            for sub in self._iter_sections(s.get("subsections") or []):
                yield sub

    def flatten_sections_for_processing(self, sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Flatten section list for per-block obligation extraction. (a),(b) subsections are not separate blocks; only top-level 1., 2., ... sections are used.)"""
        out = []
        for s in sections:
            out.append({
                "section_number": s.get("section_number", ""),
                "section_title": s.get("section_title", ""),
                "content": s.get("content", ""),
            })
        return out

    # Subsection pattern types: first occurrence in a section's content decides which type is used for that section
    _SUB_PATTERNS = [
        (_LV1_LETTER_PAREN, lambda m: f"({m.group(1)})"),
        (_LV1_LETTER_DOT, lambda m: f"{m.group(1)}."),
        (_LV2_ROMAN, lambda m: f"({m.group(1)})"),
        (_LV3_UPERCASE_PAREN, lambda m: f"({m.group(1)})"),
        (_LV3_UPERCASE_DOT, lambda m: f"{m.group(1)}."),
        (_LV4_NUM, lambda m: f"({m.group(1)})"),
    ]

    def _parse_subsections_hierarchical(self, block: str) -> List[Dict[str, Any]]:
        """
        After a main section, detect which subsection type appears first: (a), (A), (i), (1), a., A.
        Only that type is used for subsections in this section (e.g. if (A) comes first, (A),(B),(C) only).
        Returns a flat list of subsections, each with label, content, subsections: [].
        """
        # Find the earliest match across all subsection patterns
        first_start = len(block) + 1
        chosen_pattern = None
        chosen_get_label = None
        for pattern, get_label in self._SUB_PATTERNS:
            m = pattern.search(block)
            if m and m.start() < first_start:
                first_start = m.start()
                chosen_pattern = pattern
                chosen_get_label = get_label
        if chosen_pattern is None:
            return []

        # Collect all matches of the chosen pattern only
        matches = []
        for m in chosen_pattern.finditer(block):
            matches.append((m.start(), m.end(), chosen_get_label(m)))
        matches.sort(key=lambda x: x[0])

        subs = []
        for i, (start, end, label) in enumerate(matches):
            content_start = end
            content_end = matches[i + 1][0] if i + 1 < len(matches) else len(block)
            content = block[content_start:content_end].strip()
            subs.append({"label": label, "content": content, "level": 1, "subsections": []})
        return subs

    def _parse_subsections(
        self,
        block: str,
        _block_start: int,
        pattern: re.Pattern,
        kind: str,
    ) -> List[Dict[str, Any]]:
        """Parse (a)/(b) or (1)/(2) subsections within a content block. Returns list of {label, content, subsections}."""
        matches = list(pattern.finditer(block))
        subs = []
        for i, m in enumerate(matches):
            label = f"({m.group(1)})"
            content_start = m.end()
            content_end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
            content = block[content_start:content_end].strip()
            subs.append({
                "label": label,
                "content": content,
                "subsections": [],
            })
        return subs


class GeminiAnalyzer:
    """Handles interaction with Google Gemini API for legal document analysis"""
    
    def __init__(self, prompt_file: str, model: str = "gemini-2.5-flash"):
        """
        Initialize Gemini Analyzer
        
        Args:
            prompt_file: Path to file containing analysis prompt
            model: Gemini model to use
        """
        self.model = model
        self.logger = logging.getLogger(__name__)
        
        # Load prompt
        with open(prompt_file, 'r', encoding='utf-8') as f:
            self.prompt_template = f.read()

        # Initialize party metadata tracking
        self.party_metadata = {}
        # Per-page (or per-section) raw party extractions before LLM merge (avoids last-write-wins on disk)
        self._party_metadata_snapshots: List[Dict[str, Any]] = []
        self._party_metadata_llm_finalized: bool = False
        _use_azure = os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")
        if _use_azure:
            if not os.getenv("AZURE_OPENAI_ENDPOINT") or not (os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")):
                raise ValueError("USE_AZURE_OPENAI is set; AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or AZURE_OPENAI_KEY) must be set in .env")
            if not os.getenv("AZURE_OPENAI_DEPLOYMENT") and not os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") and not os.getenv("OPENAI_DEPLOYMENT_NAME"):
                raise ValueError("USE_AZURE_OPENAI is set; AZURE_OPENAI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT_NAME must be set in .env")
        elif use_bedrock_llm():
            self.logger.info("LLM client: AWS Bedrock (credentials from env / default chain)")
        else:
            if not os.getenv('GEMINI_API_KEY') and not os.getenv('GOOGLE_API_KEY'):
                raise ValueError("GEMINI_API_KEY must be set in .env (Gemini API key from https://aistudio.google.com/app/apikey)")
        self.logger.info("LLM client (Azure OpenAI, Bedrock, or Gemini) ready")
    
    def reset_party_metadata(self):
        """
        Reset party metadata for a new document
        
        This should be called at the start of processing each new document
        to prevent party information from one document leaking into another.
        """
        self.party_metadata = {}
        self._party_metadata_snapshots = []
        self._party_metadata_llm_finalized = False
        self.logger.info("Party metadata reset for new document")

    def _heuristic_party_metadata_from_snapshots(self) -> Dict[str, Dict[str, str]]:
        """Best-effort merge of snapshots for prompts before/without successful LLM consolidation."""
        by_label: Dict[str, List[Any]] = {}
        for snap in self._party_metadata_snapshots:
            if not isinstance(snap, dict):
                continue
            pn = snap.get("page_num", 0)
            for party in snap.get("parties") or []:
                if not isinstance(party, dict):
                    continue
                ref = str(party.get("reference_label") or "").strip()
                if not ref:
                    continue
                an = str(party.get("actual_name") or "").strip()
                ai = str(party.get("additional_info") or "").strip()
                by_label.setdefault(ref, []).append((an, ai, pn))
        out: Dict[str, Dict[str, str]] = {}
        ref_l_map = {ref: ref.lower() for ref in by_label}
        for ref, rows in by_label.items():
            ref_l = ref_l_map[ref]
            best_an = ""
            for an, _ai, _pn in rows:
                if not an:
                    continue
                is_role_only = an.strip().lower() == ref_l
                cur_is_role_only = (not best_an) or (best_an.strip().lower() == ref_l)
                better = False
                if not best_an:
                    better = True
                elif not is_role_only and cur_is_role_only:
                    better = True
                elif is_role_only == (best_an.strip().lower() == ref_l) and len(an) > len(best_an):
                    better = True
                if better:
                    best_an = an
            extras: List[str] = []
            for _an, ai, _pn in rows:
                if ai and ai not in extras:
                    extras.append(ai)
            out[ref] = {
                "actual_name": best_an,
                "additional_info": "; ".join(extras)[:4000],
            }
        return out

    def _apply_parties_list_to_party_metadata(self, parties: List[Dict[str, Any]]) -> None:
        """Replace ``party_metadata`` from a merged ``parties`` array (reference_label -> dict)."""
        self.party_metadata = {}
        for party in parties or []:
            if not isinstance(party, dict):
                continue
            ref_label = str(party.get("reference_label") or "").strip()
            if not ref_label:
                continue
            self.party_metadata[ref_label] = {
                "actual_name": str(party.get("actual_name") or "").strip(),
                "additional_info": str(party.get("additional_info") or "").strip(),
            }

    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def _consolidate_party_metadata_snapshots_llm(self) -> None:
        """Merge all per-page party JSON extractions into one authoritative ``party_metadata`` via LLM."""
        if not self._party_metadata_snapshots:
            self.party_metadata = {}
            self._party_metadata_llm_finalized = True
            return
        ordered = sorted(
            self._party_metadata_snapshots,
            key=lambda s: int(s.get("page_num") or 0) if isinstance(s, dict) else 0,
        )
        payload = json.dumps(ordered, ensure_ascii=False, indent=2)
        merge_prompt = f"""You merge several JSON extractions of party definitions from different pages of the same legal document.
Each snapshot is: {{"page_num": <int>, "parties": [{{"reference_label", "actual_name", "additional_info"}}, ...]}}

INPUT (all snapshots, oldest to newest):
{payload}

Rules:
- Output ONE merged "parties" list: one entry per distinct contract role (reference_label).
- "actual_name" must be the full legal name as written in the document when ANY snapshot provides it. Do NOT output only the role word (e.g. "Tenant", "Landlord") as actual_name if a real entity or person name appears in any snapshot for that role.
- If snapshots disagree, prefer extractions that include a concrete legal name; use later page_num only when it clearly corrects an error.
- Merge "additional_info" into a single concise line without duplicating the legal name.

Return ONLY valid JSON in this exact shape (no markdown fences):
{{"parties": [{{"actual_name": "...", "reference_label": "...", "additional_info": "..."}}]}}"""

        self.logger.info(
            "Consolidating party metadata from %d snapshot(s) via LLM...",
            len(self._party_metadata_snapshots),
        )
        try:
            response = self._generate_content(
                prompt=merge_prompt,
                temperature=0.1,
                response_mime_type="application/json",
            )
            result_text = (response.text or "").strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()
            merged = json.loads(result_text)
            parties = merged.get("parties") if isinstance(merged, dict) else None
            if isinstance(parties, list) and parties:
                self._apply_parties_list_to_party_metadata(parties)
                self._party_metadata_llm_finalized = True
                self.logger.info("Party metadata consolidated: %d role(s)", len(self.party_metadata))
                return
            self.logger.warning("LLM party merge returned no parties; using heuristic merge")
        except json.JSONDecodeError as e:
            self.logger.warning("LLM party merge JSON error: %s; using heuristic merge", e)
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ("rate limit", "quota", "resource exhausted", "429", "throttl")):
                raise
            self.logger.warning("LLM party merge failed: %s; using heuristic merge", e)
        self.party_metadata = self._heuristic_party_metadata_from_snapshots()
        self._party_metadata_llm_finalized = True

    def finalize_party_metadata_if_needed(self) -> None:
        """After the last party-scanned page/section, merge snapshots if we never hit the in-stream threshold."""
        if self._party_metadata_llm_finalized:
            return
        if not self._party_metadata_snapshots:
            self.party_metadata = {}
            self._party_metadata_llm_finalized = True
            return
        self._consolidate_party_metadata_snapshots_llm()
    
    def _generate_content(
        self,
        prompt: str,
        temperature: float = 0.1,
        response_mime_type: str = "application/json",
        max_output_tokens: Optional[int] = None,
        *,
        require_json_object: bool = True,
    ):
        """Call configured LLM (Azure OpenAI or Gemini) via llm_client."""
        return llm_generate_content(
            prompt,
            model=self.model,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
            require_json_object=require_json_object,
        )
    
    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def extract_party_metadata(
        self, page_num: int, page_text: str, *, max_party_pages: int = 5
    ) -> Dict[str, Any]:
        """
        Extract party definitions and identifiers from a page
        
        Args:
            page_num: Page number
            page_text: Text content of the page
            max_party_pages: After this many snapshots, run LLM merge (see LEGAL_OCR_PARTY_METADATA_PAGES).
        
        Returns:
            Dictionary containing party metadata
        """
        try:
            metadata_prompt = f"""You are a legal document analyst. Your task is to identify party definitions in legal documents.

Extract information about the parties to the agreement from the provided text. Look for:

1. Actual party names (company names, individual names, legal entities)
2. Reference labels assigned to them (e.g., "Tenant", "Landlord", "Grantor", "Grantee", "Guarantor", "Owner", etc.)
3. Any additional identifiers or descriptions

Common patterns to look for:
- "ABC Company, a Delaware corporation ('Tenant')"
- "John Doe ('Guarantor')"
- "LANDLORD: XYZ Properties, LLC"
- "between H-E-B, L.P. (hereinafter 'Grantee')"

CRITICAL ΓÇö Field meanings (do not swap):
- "actual_name" MUST be the full legal name of the entity **as written in the document** (e.g. "Fidelity Funding Company, a Nevada corporation" or at minimum the distinct company/person name). It must NOT be the contract-defined role word alone ("Landlord", "Tenant", "Lessee") unless the document literally names the party only that way.
- "reference_label" MUST be the quoted or defined role label from the lease (e.g. "Landlord", "Tenant") used elsewhere in the instrument.
- "additional_info" is optional context (entity type, state of incorporation); do not put the legal name only here while leaving actual_name as the role label.

Output a JSON object with the following structure:
{{
  "parties": [
    {{
      "actual_name": "The actual legal name of the party",
      "reference_label": "The reference label used (e.g., Tenant, Landlord)",
      "additional_info": "Any additional descriptive information"
    }}
  ]
}}

If no party definitions are found, return {{"parties": []}}.

--- PAGE {page_num} TEXT ---

{page_text}

Extract party metadata as JSON:"""

            self.logger.info(f"Extracting party metadata from page {page_num}...")
            
            response = self._generate_content(
                prompt=metadata_prompt,
                temperature=0.1,
                response_mime_type="application/json"
            )
            
            result_text = response.text.strip()
            
            # Handle markdown code blocks
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            
            result_text = result_text.strip()
            
            metadata = json.loads(result_text)
            parties_list = metadata.get("parties") if isinstance(metadata, dict) else None
            if not isinstance(parties_list, list):
                parties_list = []

            # Snapshot for later LLM merge (replace same page_num on retry to avoid duplicates)
            snap = {
                "page_num": int(page_num),
                "parties": [dict(p) for p in parties_list if isinstance(p, dict)],
            }
            self._party_metadata_snapshots = [
                s for s in self._party_metadata_snapshots if isinstance(s, dict) and s.get("page_num") != page_num
            ]
            self._party_metadata_snapshots.append(snap)

            for party in parties_list:
                if not isinstance(party, dict):
                    continue
                ref_label = str(party.get("reference_label") or "").strip()
                if ref_label:
                    self.logger.info(
                        "  Found party (page %s): %s -> %s",
                        page_num,
                        ref_label,
                        party.get("actual_name", ""),
                    )

            try:
                cap = max(1, min(50, int(max_party_pages)))
            except (TypeError, ValueError):
                cap = 5
            if self._party_metadata_llm_finalized:
                pass
            elif len(self._party_metadata_snapshots) >= cap:
                self._consolidate_party_metadata_snapshots_llm()
            else:
                self.party_metadata = self._heuristic_party_metadata_from_snapshots()

            return metadata
            
        except json.JSONDecodeError as e:
            # JSON parsing errors should not trigger retry
            self.logger.warning(f"Error parsing party metadata JSON from page {page_num}: {e}")
            return {"parties": []}
        except Exception as e:
            # Check if it's a rate limiting error - if so, re-raise to trigger retry
            error_str = str(e).lower()
            is_rate_limit = any(keyword in error_str for keyword in [
                'rate limit', 'quota', 'resource exhausted', 'resource_exhausted',
                'too many requests', '429', 'throttl'
            ])
            
            if is_rate_limit:
                # Re-raise to trigger retry decorator
                raise
            else:
                # Non-rate-limit errors should not trigger retry
                self.logger.warning(f"Error extracting party metadata from page {page_num}: {e}")
                return {"parties": []}
    
    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def analyze_page(
        self,
        page_num: int,
        page_text: str,
        extract_parties: bool = True,
        *,
        document_name: str = "",
        party_metadata_max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Analyze a single page: returns a list of {{ "category", "obligations" }} blocks per the extraction prompt.
        """
        result_text = ""
        try:
            if extract_parties:
                self.extract_party_metadata(
                    page_num, page_text, max_party_pages=party_metadata_max_pages
                )

            metadata_context = ""
            if self.party_metadata:
                metadata_context = (
                    "\n\n--- PARTY METADATA (use to map contract labels to actual legal names ΓÇö "
                    "follow `Responsible Party` field rules in the instructions above) ---\n\n"
                )
                metadata_context += "The following parties have been identified in this document:\n\n"
                for ref_label, info in self.party_metadata.items():
                    actual_name = info.get("actual_name", "")
                    additional_info = info.get("additional_info", "")
                    if actual_name:
                        metadata_context += f"- '{ref_label}' refers to: {actual_name}"
                        if additional_info:
                            metadata_context += f" ({additional_info})"
                        metadata_context += "\n"
                metadata_context += (
                    "\nUse these mappings so `Responsible Party` contains **actual legal names** where the "
                    "document and metadata support it. Do not invent parties or obligations.\n"
                )

            doc_label = (document_name or "document.pdf").strip() or "document.pdf"
            full_prompt = f"""{self.prompt_template}{metadata_context}

--- DOCUMENT FILE NAME (use as docId in every citation block) ---
{doc_label}

CRITICAL: Respond with ONLY valid JSON: a single JSON object matching the "Strict JSON Output Format" above (top-level object with a "results" array of category objects, plus docId/citations as in that example). Do not use a top-level JSON array. No markdown fences, no commentary before or after the JSON.

--- PAGE {page_num} TEXT ---

{page_text}"""

            message = f"≡ƒôä ANALYZING PAGE {page_num} with {_configured_llm_label()}..."
            self.logger.info(message)
            force_logger.info(message)  # Force terminal output

            max_out = _analyze_extraction_max_output_tokens()
            response = self._generate_content(
                prompt=full_prompt,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=max_out,
                require_json_object=True,
            )

            result_text = (response.text or "").strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()

            parsed = json.loads(result_text)
            groups = _parse_extraction_category_groups(parsed)
            _enrich_category_groups_citations(groups, page_num=page_num, document_name=doc_label)

            n_inner = sum(len(g.get("obligations") or []) for g in groups if isinstance(g, dict))
            if not groups or n_inner == 0:
                self.logger.warning(
                    "Page %s: 0 category blocks or 0 inner obligations ΓÇö response (first 600 chars): %r",
                    page_num,
                    result_text[:600],
                )
            self.logger.info("Page %s: %d category group(s), %d obligation row(s)", page_num, len(groups), n_inner)
            return groups

        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing JSON response for page {page_num}: {e}")
            self.logger.error(f"Response text: {result_text[:500]}")
            return []
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = any(
                keyword in error_str
                for keyword in [
                    "rate limit",
                    "quota",
                    "resource exhausted",
                    "resource_exhausted",
                    "too many requests",
                    "429",
                    "throttl",
                ]
            )
            if is_rate_limit:
                raise
            self.logger.error(f"Error analyzing page {page_num}: {e}")
            return []

    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def analyze_section(
        self,
        section_number: str,
        section_title: str,
        section_content: str,
        extract_parties: bool = False,
        *,
        document_name: str = "",
        party_metadata_max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Section-based extraction: same JSON shape as analyze_page (list of category blocks).
        """
        result_text = ""
        display_num = (section_number or "").strip() or "(unnumbered)"
        try:
            if extract_parties and section_content.strip():
                self.extract_party_metadata(
                    1,
                    f"Section {section_number}. {section_title}\n\n{section_content}",
                    max_party_pages=party_metadata_max_pages,
                )
            metadata_context = ""
            if self.party_metadata:
                metadata_context = (
                    "\n\n--- PARTY METADATA (use to map contract labels to actual legal names ΓÇö "
                    "follow `Responsible Party` field rules in the instructions above) ---\n\n"
                )
                metadata_context += "The following parties have been identified in this document:\n\n"
                for ref_label, info in self.party_metadata.items():
                    actual_name = info.get("actual_name", "")
                    additional_info = info.get("additional_info", "")
                    if actual_name:
                        metadata_context += f"- '{ref_label}' refers to: {actual_name}"
                        if additional_info:
                            metadata_context += f" ({additional_info})"
                        metadata_context += "\n"
                metadata_context += (
                    "\nUse these mappings so `Responsible Party` contains **actual legal names** where the "
                    "document and metadata support it. Do not invent parties or obligations.\n"
                )
            doc_label = (document_name or "document.pdf").strip() or "document.pdf"
            full_prompt = f"""{self.prompt_template}{metadata_context}

--- DOCUMENT FILE NAME (use as docId in every citation block) ---
{doc_label}

CRITICAL: Respond with ONLY valid JSON: a single JSON object matching the "Strict JSON Output Format" above (top-level object with a "results" array of category objects, plus docId/citations as in that example). Do not use a top-level JSON array. No markdown fences, no commentary.

--- SECTION {section_number}. {section_title} ---

{section_content}"""

            self.logger.info(f"Analyzing section {display_num} with LLM...")
            max_out = _analyze_extraction_max_output_tokens()
            response = self._generate_content(
                prompt=full_prompt,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=max_out,
                require_json_object=True,
            )
            result_text = (response.text or "").strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()
            parsed = json.loads(result_text)
            groups = _parse_extraction_category_groups(parsed)
            _enrich_category_groups_citations(groups, page_num=None, document_name=doc_label)
            n_inner = sum(len(g.get("obligations") or []) for g in groups if isinstance(g, dict))
            self.logger.info("Section %s: %d category group(s), %d obligation row(s)", display_num, len(groups), n_inner)
            return groups
        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing JSON for section {display_num}: {e}")
            return []
        except Exception as e:
            error_str = str(e).lower()
            if any(k in error_str for k in ["rate limit", "quota", "resource exhausted", "429", "throttl"]):
                raise
            self.logger.error(f"Error analyzing section {display_num}: {e}")
            return []

    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def consolidate_results_to_json(
        self, all_category_groups: List[Dict[str, Any]], *, document_name: str = ""
    ) -> Dict[str, Any]:
        """
        Merge per-page extraction category blocks into document-level ``results`` and root ``citations``.
        """
        empty_out: Dict[str, Any] = {
            "results": [],
            "citations": [],
            "flat_for_vector": [],
            "consolidated_obligations_count": 0,
        }
        try:
            self.logger.info("Consolidating extraction category groups into JSON...")
            if not all_category_groups:
                return dict(empty_out)

            # Map-reduce page batching fallback for large pagewise inputs.
            # This prevents LLM overload/truncation and enforces stable 1:1 Reasoning inheritance.
            try:
                import os

                page_batch_size = int(os.environ.get("CONSOLIDATION_PAGE_BATCH_SIZE", "5"))
            except Exception:
                page_batch_size = 5

            if len(all_category_groups) > max(1, page_batch_size):
                return self._consolidate_results_to_json_map_reduce(
                    all_category_groups=all_category_groups,
                    document_name=document_name,
                    page_batch_size=page_batch_size,
                    empty_out=empty_out,
                )

            results = self._consolidate_category_groups_to_results(all_category_groups, document_name=document_name)
            if not results:
                self.logger.warning("Consolidation produced no category groups from %d input blocks", len(all_category_groups))
                return dict(empty_out)

            root_citations = build_root_citations_api_shape(results, document_name)
            flat_for_vector = flatten_processing_results_for_index(results)
            count_inner = count_obligations_in_results(results)
            self.logger.info(
                "Consolidated and reclassified into grouped JSON (%d category groups, %d obligation rows)",
                len(results),
                count_inner,
            )
            return {
                "results": results,
                "citations": root_citations,
                "flat_for_vector": flat_for_vector,
                "consolidated_obligations_count": count_inner,
            }
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = any(
                k in error_str
                for k in [
                    "rate limit",
                    "quota",
                    "resource exhausted",
                    "resource_exhausted",
                    "too many requests",
                    "429",
                    "throttl",
                ]
            )
            if is_rate_limit:
                raise
            self.logger.error(f"Error consolidating to JSON (consolidated output will be empty): {e}", exc_info=True)
            return dict(empty_out)

    def _consolidate_category_groups_to_results(
        self, all_category_groups: List[Dict[str, Any]], *, document_name: str
    ) -> List[Dict[str, Any]]:
        """
        Deterministic consolidation pipeline:
        - merge per-page category groups
        - normalize + merge duplicate parties within category
        - consolidate responsibilities by party (LLM merge-plan allowed; Reasoning strictly inherited)
        """
        merged = merge_extraction_category_groups([dict(g) for g in all_category_groups if isinstance(g, dict)])
        normalize_results_categories(merged)
        doc = (document_name or "").strip() or "document.pdf"
        results = merge_duplicate_party_within_category(
            merged, default_doc=doc, party_metadata=self.party_metadata
        )
        if not results:
            return []

        message = "[CONSOLIDATION] APPLYING CONSOLIDATION..."
        self.logger.info(message)
        force_logger.info(message)  # Force terminal output
        results = self._simple_consolidation(results, document_name)
        return results

    def _consolidate_results_to_json_map_reduce(
        self,
        *,
        all_category_groups: List[Dict[str, Any]],
        document_name: str,
        page_batch_size: int,
        empty_out: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Map-reduce fallback:
        - Map: consolidate per N-page batches independently
        - Reduce: merge the batch-level consolidated results and run one final consolidation pass
        """
        batches = self._chunk_list([g for g in all_category_groups if isinstance(g, dict)], max(1, int(page_batch_size or 5)))
        if not batches:
            return dict(empty_out)

        mapped_results: List[Dict[str, Any]] = []
        for b in batches:
            mapped_results.extend(self._consolidate_category_groups_to_results(b, document_name=document_name))

        if not mapped_results:
            return dict(empty_out)

        # Reduce pass across batches (merge + final consolidation)
        reduced_results = self._consolidate_category_groups_to_results(mapped_results, document_name=document_name)
        if not reduced_results:
            reduced_results = mapped_results

        root_citations = build_root_citations_api_shape(reduced_results, document_name)
        flat_for_vector = flatten_processing_results_for_index(reduced_results)
        count_inner = count_obligations_in_results(reduced_results)
        return {
            "results": reduced_results,
            "citations": root_citations,
            "flat_for_vector": flat_for_vector,
            "consolidated_obligations_count": count_inner,
        }

    def _map_citations_by_similarity(self, consolidated_responsibilities: List[str], 
                                   original_responsibilities: List[str], 
                                   original_citations: List[Dict],
                                   party: str, category_name: str) -> List[List[Dict]]:
        """
        Smart code-based citation mapping (no LLM dependency):
        - Non-merged responsibilities: Direct 1:1 citation from pagewise
        - Merged responsibilities: Multi-source citations via similarity
        """
        from difflib import SequenceMatcher
        
        def normalize_text(text):
            import re
            return ' '.join(re.sub(r'[^\w\s]', ' ', text.lower()).split())
        
        def calculate_similarity(text1, text2):
            return SequenceMatcher(None, normalize_text(text1), normalize_text(text2)).ratio()

        def is_token_subsequence(needle: str, hay: str, *, min_tokens: int = 3, min_coverage: float = 0.8) -> bool:
            """
            True if most tokens in `needle` appear in-order within `hay`, allowing extra tokens in-between.
            This captures cases where consolidated text inserts extra qualifiers mid-phrase.
            """
            n_tokens = normalize_text(needle).split()
            h_tokens = normalize_text(hay).split()
            if len(n_tokens) < min_tokens or not h_tokens:
                return False
            i = 0
            matched = 0
            for t in h_tokens:
                if i < len(n_tokens) and t == n_tokens[i]:
                    matched += 1
                    i += 1
                    if i >= len(n_tokens):
                        break
            coverage = matched / max(1, len(n_tokens))
            return matched >= min_tokens and coverage >= min_coverage

        def token_overlap_ok(needle: str, hay: str, *, min_overlap: int = 3, min_cov: float = 0.5, min_sim: float = 0.38) -> bool:
            """Content-token overlap for partial merges (more tolerant than SequenceMatcher)."""
            stop = {
                "the","a","an","and","or","of","to","in","on","with","without","for","as","at","by","if",
                "within","after","before","during","over","under","up","including","per","month","months",
                "day","days","any","all","be","become","due","when","made","make","pay","paid",
            }
            n = [t for t in normalize_text(needle).split() if t and t not in stop]
            h = [t for t in normalize_text(hay).split() if t and t not in stop]
            if len(n) < 3 or not h:
                return False
            ns = set(n)
            hs = set(h)
            overlap = ns.intersection(hs)
            cov = (len(overlap) / max(1, len(ns))) if ns else 0.0
            s = calculate_similarity(needle, hay)
            return len(overlap) >= min_overlap and cov >= min_cov and s >= min_sim
        
        grouped_citations = []
        self.logger.info(f"SMART CITATION MAPPING: {party} in '{category_name}'")
        
        for consolidated_resp in consolidated_responsibilities:
            citation_group = []
            contributing_sources = []
            best_match = (0, -1, 0.0)  # (index, position, similarity)
            cons_norm = normalize_text(consolidated_resp or "")
            cons_tokens = cons_norm.split()
            cons_has_base_rent = ("base" in cons_tokens) and ("rent" in cons_tokens)
            
            # Find all original responsibilities that contributed to this consolidated one
            for i, original_resp in enumerate(original_responsibilities):
                similarity = calculate_similarity(consolidated_resp, original_resp)
                orig_norm = normalize_text(original_resp or "")
                
                # Track the best match
                if similarity > best_match[2]:
                    best_match = (i, i, similarity)
                
                # Contribution heuristics (hardcoded, no LLM):
                # - Similarity > 60% is a strong signal
                # - Containment is important for short definitional clauses that get expanded
                #   (e.g., "Pay Base Rent of $___ per month" becomes a longer sentence)
                token_len = len(orig_norm.split())
                containment = bool(orig_norm) and (token_len >= 3) and (orig_norm in cons_norm)
                subseq = is_token_subsequence(original_resp or "", consolidated_resp or "")
                overlap_ok = token_overlap_ok(original_resp or "", consolidated_resp or "")
                # Anchor: if consolidated mentions base rent, weak matches must also mention it.
                anchor_ok = True
                if cons_has_base_rent and not (("base" in orig_norm.split()) and ("rent" in orig_norm.split())):
                    anchor_ok = similarity >= 0.6
                if anchor_ok and (similarity > 0.6 or containment or subseq or overlap_ok):
                    contributing_sources.append((i, similarity))
                    
                    # Add citation from this contributing source
                    cite = original_citations[i]
                    citation_entry = {
                        "page": cite.get("page", 1),
                        "section": cite.get("section", "Document")
                    }
                    
                    # Avoid duplicate citations (same page + section)
                    if citation_entry not in citation_group:
                        citation_group.append(citation_entry)
            
            # Smart citation logic based on match type
            if len(contributing_sources) == 1 and contributing_sources[0][1] > 0.85:
                # NON-MERGED: Single high-similarity match (>85%) ΓåÆ Use direct pagewise citation
                cite_index = contributing_sources[0][0]
                cite = original_citations[cite_index]
                citation_group = [{
                    "page": cite.get("page", 1),
                    "section": cite.get("section", "Document")
                }]
                self.logger.debug(f"Direct citation: \"{consolidated_resp[:40]}...\" ΓåÆ Page {cite.get('page')} (sim:{contributing_sources[0][1]:.2f})")
                
            elif len(contributing_sources) > 1:
                # MERGED: Multiple sources ΓåÆ Keep all citations (already collected above)
                pages = [str(c["page"]) for c in citation_group]
                self.logger.info(f"Multi-source citation: \"{consolidated_resp[:50]}...\" ΓåÆ Pages {', '.join(pages)}")
                
            elif not citation_group and best_match[2] > 0.4:
                # FALLBACK: Best match with reasonable similarity ΓåÆ Use single citation
                cite = original_citations[best_match[0]]
                citation_group = [{
                    "page": cite.get("page", 1),
                    "section": cite.get("section", "Document")
                }]
                self.logger.debug(f"Fallback citation: \"{consolidated_resp[:40]}...\" ΓåÆ Page {cite.get('page')} (sim:{best_match[2]:.2f})")
                
            else:
                # LAST RESORT: Positional fallback
                resp_index = consolidated_responsibilities.index(consolidated_resp)
                if resp_index < len(original_citations):
                    cite = original_citations[resp_index]
                    citation_group = [{
                        "page": cite.get("page", 1),
                        "section": cite.get("section", "Document")
                    }]
                else:
                    citation_group = [{"page": 1, "section": "Document"}]
                self.logger.warning(f"Positional citation: \"{consolidated_resp[:40]}...\" ΓåÆ Page {citation_group[0]['page']}")
            
            grouped_citations.append(citation_group)
        
        return grouped_citations

    def _collapse_citation_group_to_one(self, group: Any) -> Dict[str, Any]:
        """
        Convert a citation group (list of {page, section}) into a single citation object that
        still preserves 1:1 mapping to responsibilities:
          { "pageNumbers": [2,3], "section": ["1(e) Base Rent", "5. Rent"] }
        """
        pages: List[int] = []
        sections: List[str] = []
        seen_pages: set = set()
        seen_secs: set = set()

        def add_one(page_val: Any, sec_val: Any) -> None:
            try:
                p = int(page_val) if page_val is not None else 0
            except (TypeError, ValueError):
                p = 0
            s = str(sec_val or "").strip()
            if p > 0 and p not in seen_pages:
                seen_pages.add(p)
                pages.append(p)
            if s and s.lower() not in seen_secs:
                seen_secs.add(s.lower())
                sections.append(s)

        # Allow group to be dict, list-of-dict, or nested list
        if isinstance(group, dict):
            add_one(group.get("page"), group.get("section"))
        elif isinstance(group, list):
            for item in group:
                if isinstance(item, dict):
                    add_one(item.get("page"), item.get("section"))
                elif isinstance(item, list):
                    for sub in item:
                        if isinstance(sub, dict):
                            add_one(sub.get("page"), sub.get("section"))

        if not pages:
            pages = [1]
        if not sections:
            sections = ["Document"]
        return {"pageNumbers": pages, "section": sections}

    def _collapse_grouped_citations(self, grouped: Any) -> List[Dict[str, Any]]:
        """Collapse list-of-groups citations to list of single citation dicts."""
        if not isinstance(grouped, list):
            return []
        return [self._collapse_citation_group_to_one(g) for g in grouped]

    def _dedup_strings_preserve_order(self, values: List[Any]) -> List[str]:
        out: List[str] = []
        seen: set = set()
        for v in values or []:
            s = str(v or "").strip()
            if not s:
                continue
            key = " ".join(s.lower().split())
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    def _coerce_related_keywords_list(
        self,
        raw: Any,
        *,
        category: str = "",
        owner_responsibility: Any = None,
    ) -> List[str]:
        if isinstance(raw, list):
            base = self._dedup_strings_preserve_order(raw)
        elif raw is None or raw == "":
            base = []
        else:
            base = self._dedup_strings_preserve_order([raw])
        return broaden_related_keywords(
            category=category,
            owner_responsibility=owner_responsibility,
            base_keywords=base,
            max_items=64,
        )

    def _merge_related_keywords_from_item_dicts(
        self, items: List[Dict[str, Any]], *, cap: int = 64, category_name: str = ""
    ) -> List[str]:
        acc: List[str] = []
        duty_texts: List[str] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            acc = _merge_str_lists(acc, it.get("related_keywords"))
            t = str(it.get("text") or "").strip()
            if t:
                duty_texts.append(t)
        return broaden_related_keywords(
            category=category_name,
            owner_responsibility=duty_texts if duty_texts else None,
            base_keywords=acc,
            max_items=cap,
        )

    def _merge_reasoning_list_to_string(self, reasonings: Any) -> str:
        """
        Merge reasoning strings from multiple pagewise sources.
        Strategy:
        1. Split each incoming reasoning string by ';' to recover atomic phrases.
        2. Deduplicate using token-level Jaccard similarity (threshold 0.75) to
           remove semantically near-identical phrases, not just exact duplicates.
        3. Rejoin with '; '.
        """
        def normalize_phrase(t: str) -> str:
            return re.sub(r"[^\w\s]", " ", (t or "").lower()).strip()

        def token_jaccard(a: str, b: str) -> float:
            ta = set(normalize_phrase(a).split())
            tb = set(normalize_phrase(b).split())
            if not ta or not tb:
                return 0.0
            union = len(ta | tb)
            return (len(ta & tb) / union) if union else 0.0

        if isinstance(reasonings, str):
            raw: List[Any] = [reasonings]
        elif isinstance(reasonings, list):
            raw = reasonings
        else:
            raw = []

        phrases: List[str] = []
        for r in raw:
            if not r:
                continue
            if isinstance(r, list):
                for item in r:
                    parts = [p.strip() for p in str(item or "").split(";") if p.strip()]
                    phrases.extend(parts)
            else:
                parts = [p.strip() for p in str(r).split(";") if p.strip()]
                phrases.extend(parts)

        kept: List[str] = []
        for phrase in phrases:
            is_duplicate = False
            for existing in kept:
                if token_jaccard(phrase, existing) >= 0.75:
                    is_duplicate = True
                    break
            if not is_duplicate:
                kept.append(phrase)

        return "; ".join(kept)

    def _chunk_list(self, items: List[Any], chunk_size: int) -> List[List[Any]]:
        if not items:
            return []
        size = max(1, int(chunk_size or 1))
        return [items[i : i + size] for i in range(0, len(items), size)]

    def _estimate_tokens_approx(self, text: str) -> float:
        """
        Rough input-token estimate for routing/batching (no tiktoken dependency).
        Uses max(word-based heuristic, char/4) so long JSON / legal text does not underestimate.
        """
        if not text:
            return 0.0
        words = len(str(text).split())
        return max(words * 1.3, len(str(text)) / 4.0)

    def _build_merge_plan_prompt(
        self, party: str, category_name: str, items: List[Dict[str, Any]]
    ) -> str:
        """Single source of truth for merge-plan prompt text (used for LLM call + token sizing)."""
        item_payload = [{"id": str(it.get("id")), "text": str(it.get("text") or "")} for it in items]
        return f"""You are deduplicating extracted legal obligations for a single responsible party in one category.

CRITICAL RULE ΓÇö MERGE ONLY NEAR-IDENTICAL SENTENCES:
Merge two obligations into one ONLY when they express the exact same legal duty with minor wording
differences. The test is: could a lawyer substitute one for the other without changing legal meaning?

ALLOWED to merge (near-identical phrasing of the same duty):
  Γ£à "pay base rent monthly" + "pay base rent in equal monthly installments" ΓåÆ one item
  Γ£à "maintain fire insurance" + "obtain and maintain fire insurance" ΓåÆ one item

FORBIDDEN to merge (different duties, even in the same category):
  Γ¥î "pay base rent" + "pay real estate taxes" ΓÇö different financial obligations
  Γ¥î "pay base rent" + "pay operating expenses" ΓÇö different line items
  Γ¥î "pay base rent" + "pay for alterations" ΓÇö completely different topics
  Γ¥î "maintain insurance" + "pay base rent" ΓÇö unrelated
  Γ¥î Any two obligations about different topics, even if the same party

FORBIDDEN to merge (same topic but different actions):
  Γ¥î "Pay Base Rent in advance monthly" + "Send all Rent payments to Landlord's Address"
     ΓåÆ These are different actions (payment schedule vs. payment method)
  Γ¥î "Pay Base Rent monthly" + "Pay prorated Rent for fractional months"
     ΓåÆ These cover different payment scenarios
  Γ¥î "Pay Base Rent monthly" + "Pay Real Estate Taxes as they become due"
     ΓåÆ These are completely different financial obligations
  Γ¥î "Pay first monthly installment on Effective Date" + "Pay subsequent installments on first day"
     ΓåÆ These are different timing conditions for the same payment type, keep separate

ALLOWED to merge (truly identical duty, minor wording only):
  Γ£à "Keep HVAC systems in good condition and repair" + "Keep and maintain HVAC systems"
     ΓåÆ Same duty, different phrasing ΓåÆ merge to longer/more complete version
  Γ£à "Pay Base Rent monthly without demand" + "Pay Base Rent in advance without demand monthly"
     ΓåÆ Same duty, minor wording difference ΓåÆ merge

COUNTING RULE: If the input has N items and your output has fewer than N/2 groups, you have
over-merged. Review each group where source_ids has more than 2 items and verify each
source obligation expresses the exact same duty before keeping them grouped.

WHEN IN DOUBT: keep them as separate single-element groups. It is always safer to preserve
granularity than to merge. Over-merging destroys information.

OUTPUT SCHEMA (JSON array only, no markdown, no commentary):
[
  {{
    "merged_responsibility": "<one consolidated responsibility text>",
    "source_ids": ["<id1>", "<id2>"]
  }}
]

HARD CONSTRAINTS:
- Every input id MUST appear EXACTLY ONCE across all "source_ids".
- Do NOT introduce ids not in the input.
- "merged_responsibility" must preserve all legal/financial details from the source texts.
- ATOMICITY: "merged_responsibility" must describe ONE single legal duty. Never join two
  obligations using semicolons (;) even if merging multiple source_ids. If you must merge,
  choose the more complete phrasing ΓÇö do NOT concatenate with semicolons or "and".
- Single-item groups (no merge) are the correct default for most obligations.

Context:
- Category: "{category_name}"
- Responsible Party: "{party}"

Input obligations (id + responsibility text):
{json.dumps(item_payload, indent=2)}
"""

    def _estimate_merge_plan_prompt_tokens(
        self, party: str, category_name: str, items: List[Dict[str, Any]]
    ) -> float:
        return self._estimate_tokens_approx(self._build_merge_plan_prompt(party, category_name, items))

    def _renumber_consolidation_items(self, items: List[Dict[str, Any]], prefix: str = "r") -> List[Dict[str, Any]]:
        """Stable ids across map-reduce rounds (avoids duplicate m1/m2 from separate chunk passes)."""
        out: List[Dict[str, Any]] = []
        for i, it in enumerate(items or [], 1):
            if not isinstance(it, dict):
                continue
            d = dict(it)
            d["id"] = f"{prefix}{i}"
            out.append(d)
        return out

    def _chunk_items_for_merge_plan_token_budget(
        self,
        *,
        party: str,
        category_name: str,
        items: List[Dict[str, Any]],
        max_input_tokens: float,
    ) -> List[List[Dict[str, Any]]]:
        """
        Greedy batching: pack successive items into a chunk until adding the next item would push
        the full merge-plan prompt over ``max_input_tokens``.
        """
        if not items:
            return []
        chunks: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for it in items:
            trial = current + [it]
            est_trial = self._estimate_merge_plan_prompt_tokens(party, category_name, trial)
            if current and est_trial > max_input_tokens:
                chunks.append(current)
                one = [it]
                est_one = self._estimate_merge_plan_prompt_tokens(party, category_name, one)
                if est_one > max_input_tokens:
                    self.logger.warning(
                        "Single responsibility text exceeds merge-plan token budget (~%.0f > %.0f) for %s / %s ΓÇö "
                        "processing alone (may rely on programmatic fallback if LLM truncates).",
                        est_one,
                        max_input_tokens,
                        party,
                        category_name,
                    )
                current = one
            else:
                current = trial
        if current:
            chunks.append(current)
        return chunks

    def _collapse_sources_to_citation(self, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Collapse a list of {page, section} dicts into one {pageNumbers:[...], section:[...]} dict.
        Preserves insertion order and deduplicates.
        """
        if not sources:
            return {"pageNumbers": [1], "section": ["Document"]}
        pages: List[int] = []
        sections: List[str] = []
        seen_pages: set = set()
        seen_secs: set = set()
        for c in sources:
            if not isinstance(c, dict):
                continue
            try:
                p = int(c.get("page") or 0)
            except (TypeError, ValueError):
                p = 0
            s = str(c.get("section") or "").strip() or "Document"
            if p > 0 and p not in seen_pages:
                seen_pages.add(p)
                pages.append(p)
            sk = " ".join(s.lower().split())
            if sk and sk not in seen_secs:
                seen_secs.add(sk)
                sections.append(s)
        return {"pageNumbers": pages or [1], "section": sections or ["Document"]}

    def _llm_merge_plan_for_party(self, *, party: str, category_name: str, items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        """
        Ask the LLM for a merge plan ONLY (no reasoning, no meta). Returns a list of:
          { "merged_responsibility": str, "source_ids": [str, ...] }
        """
        if not items:
            return []

        item_payload = [{"id": str(it.get("id")), "text": str(it.get("text") or "")} for it in items]
        ids = [x["id"] for x in item_payload if x.get("id")]
        if not ids:
            return None

        prompt = self._build_merge_plan_prompt(party, category_name, items)

        try:
            response = self._generate_content(
                prompt,
                max_output_tokens=3000,
                response_mime_type="text/plain",
                require_json_object=False,
                temperature=0.0,
            )
            if hasattr(response, "text"):
                response_text = (response.text or "").strip()
            elif isinstance(response, str):
                response_text = response.strip()
            else:
                response_text = str(response).strip()

            # Clean common wrappers
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

            # Extract first JSON array
            import re
            m = re.search(r"\[\s*\{.*\}\s*\]", response_text, re.DOTALL)
            json_str = m.group(0) if m else response_text
            plan = json.loads(json_str)
            if not isinstance(plan, list) or not plan:
                return None

            # Validate coverage and id integrity
            input_ids = set(ids)
            seen: List[str] = []
            for g in plan:
                if not isinstance(g, dict):
                    return None
                src = g.get("source_ids")
                if not isinstance(src, list) or not src:
                    return None
                for sid in src:
                    seen.append(str(sid))
            if set(seen) != input_ids:
                return None
            # ensure no duplicates
            if len(seen) != len(input_ids):
                return None

            return plan
        except Exception as e:
            self.logger.warning(f"LLM merge-plan failed for {party} in '{category_name}': {e}")
            return None

    def _programmatic_similarity_merge_items(
        self,
        *,
        items: List[Dict[str, Any]],
        similarity_threshold: float = 0.90,
        category_name: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Code-only fallback: groups highly similar responsibility texts and merges their
        citations/reasonings via pure concatenation/dedup (no summarization).
        """
        if not items:
            return []

        from difflib import SequenceMatcher
        import re

        def norm(t: str) -> str:
            return " ".join(re.sub(r"[^\w\s]", " ", (t or "").lower()).split())

        def sim(a: str, b: str) -> float:
            return SequenceMatcher(None, norm(a), norm(b)).ratio()

        used: set = set()
        groups: List[List[Dict[str, Any]]] = []
        for i, it in enumerate(items):
            iid = it.get("id")
            if iid in used:
                continue
            used.add(iid)
            g = [it]
            for j, other in enumerate(items):
                oid = other.get("id")
                if oid in used:
                    continue
                if sim(str(it.get("text") or ""), str(other.get("text") or "")) >= similarity_threshold:
                    used.add(oid)
                    g.append(other)
            groups.append(g)

        merged: List[Dict[str, Any]] = []
        for gi, g in enumerate(groups):
            # choose primary as longest text
            primary = max(g, key=lambda x: len(str(x.get("text") or "")))
            merged_text = str(primary.get("text") or "").strip()

            merged_reason = self._merge_reasoning_list_to_string([x.get("reasoning") for x in g])
            merged_kw = self._merge_related_keywords_from_item_dicts(g, cap=64, category_name=category_name)
            # Merge citation sources
            sources: List[Dict[str, Any]] = []
            for x in g:
                srcs = x.get("citation_sources") or []
                if isinstance(srcs, list):
                    for s in srcs:
                        if isinstance(s, dict):
                            sources.append(s)
            merged.append(
                {
                    "id": f"m{gi+1}",
                    "text": merged_text,
                    "reasoning": merged_reason,
                    "citation_sources": sources,
                    "related_keywords": merged_kw,
                }
            )

        return merged

    def _apply_merge_plan_to_items(
        self,
        *,
        plan: List[Dict[str, Any]],
        items_by_id: Dict[str, Dict[str, Any]],
        category_name: str = "",
    ) -> List[Dict[str, Any]]:
        merged_items: List[Dict[str, Any]] = []
        for idx, g in enumerate(plan or [], 1):
            if not isinstance(g, dict):
                continue
            merged_text = str(g.get("merged_responsibility") or "").strip()
            src_ids = g.get("source_ids") or []
            if not merged_text or not isinstance(src_ids, list) or not src_ids:
                continue

            src_items = [items_by_id.get(str(sid)) for sid in src_ids]
            src_items = [x for x in src_items if isinstance(x, dict)]
            if not src_items:
                continue

            merged_reason = self._merge_reasoning_list_to_string([x.get("reasoning") for x in src_items])
            merged_kw = self._merge_related_keywords_from_item_dicts(src_items, cap=64, category_name=category_name)
            merged_sources: List[Dict[str, Any]] = []
            for x in src_items:
                srcs = x.get("citation_sources") or []
                if isinstance(srcs, list):
                    for s in srcs:
                        if isinstance(s, dict):
                            merged_sources.append(s)

            merged_items.append(
                {
                    "id": f"m{idx}",
                    "text": merged_text,
                    "reasoning": merged_reason,
                    "citation_sources": merged_sources,
                    "related_keywords": merged_kw,
                }
            )
        return merged_items

    def _consolidate_party_items_map_reduce(
        self,
        *,
        party: str,
        category_name: str,
        items: List[Dict[str, Any]],
        document_name: str,
        llm_batch_size: int = 12,
        max_rounds: int = 3,
        orphan_reasonings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Map-reduce consolidation at the responsibility level.
        LLM is used ONLY for merge grouping/merged responsibility text, never for Reasoning.
        Reasoning is strictly inherited + merged from pagewise inputs in Python.

        ``orphan_reasonings``: pagewise ``Reasoning`` entries with no matching duty by index
        (len(Reasoning) > len(Owner Responsibility) for that party bucket). They are appended
        to the final ``Reasoning`` array as-is, after merged per-row reasonings.

        Batching is driven primarily by **estimated merge-plan prompt tokens** (not a fixed row count):
        if the prompt would exceed ``CONSOLIDATION_MERGE_PLAN_MAX_INPUT_TOKENS``, inputs are split into
        smaller batches, each processed independently, then merged in further rounds. ``llm_batch_size``
        is only a fallback ceiling when token env vars are absent (backward compatibility).
        """
        import os

        try:
            max_input_tokens = float(os.environ.get("CONSOLIDATION_MERGE_PLAN_MAX_INPUT_TOKENS", "4500"))
        except Exception:
            max_input_tokens = 4500.0
        try:
            max_map_rounds = int(os.environ.get("CONSOLIDATION_MERGE_PLAN_MAX_ROUNDS", "12"))
        except Exception:
            max_map_rounds = 12
        if max_map_rounds < 1:
            max_map_rounds = 1
        # Allow call-site override when env is not set
        if not os.environ.get("CONSOLIDATION_MERGE_PLAN_MAX_ROUNDS") and max_rounds and int(max_rounds) > max_map_rounds:
            max_map_rounds = int(max_rounds)

        current: List[Dict[str, Any]] = list(items or [])

        # Ensure every item has the expected internal structure.
        normalized: List[Dict[str, Any]] = []
        for i, it in enumerate(current, 1):
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or f"r{i}")
            txt = str(it.get("text") or "").strip()
            if not txt:
                continue
            reasoning = str(it.get("reasoning") or "").strip()
            src = it.get("citation_sources")
            if not isinstance(src, list):
                src = []
            kw = self._coerce_related_keywords_list(
                it.get("related_keywords"),
                category=category_name,
                owner_responsibility=txt,
            )
            normalized.append(
                {
                    "id": iid,
                    "text": txt,
                    "reasoning": reasoning,
                    "citation_sources": src,
                    "related_keywords": kw,
                }
            )
        current = self._renumber_consolidation_items(normalized)

        def run_one_round(input_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            plan = self._llm_merge_plan_for_party(party=party, category_name=category_name, items=input_items)
            if plan:
                items_by_id = {str(x.get("id")): x for x in input_items if isinstance(x, dict) and x.get("id")}
                out = self._apply_merge_plan_to_items(
                    plan=plan, items_by_id=items_by_id, category_name=category_name
                )
                # If something went wrong, fall back programmatically.
                return out if out else self._programmatic_similarity_merge_items(
                    items=input_items, category_name=category_name
                )
            return self._programmatic_similarity_merge_items(items=input_items, category_name=category_name)

        round_idx = 0
        while len(current) > 1 and round_idx < max_map_rounds:
            round_idx += 1
            est_all = self._estimate_merge_plan_prompt_tokens(party, category_name, current)
            if est_all <= max_input_tokens:
                merged = run_one_round(current)
                current = self._renumber_consolidation_items(merged) if merged else self._renumber_consolidation_items(
                    self._programmatic_similarity_merge_items(items=current, category_name=category_name)
                )
                break

            self.logger.info(
                "Merge-plan prompt ~%.0f tokens > budget %.0f for %s / %s (%d items) ΓÇö token batching.",
                est_all,
                max_input_tokens,
                party,
                category_name,
                len(current),
            )

            chunks = self._chunk_items_for_merge_plan_token_budget(
                party=party,
                category_name=category_name,
                items=current,
                max_input_tokens=max_input_tokens,
            )
            if len(chunks) <= 1:
                current = self._renumber_consolidation_items(
                    self._programmatic_similarity_merge_items(items=current, category_name=category_name)
                )
                continue

            next_items: List[Dict[str, Any]] = []
            for ch in chunks:
                next_items.extend(run_one_round(ch))
            prev_n = len(current)
            if not next_items:
                current = self._renumber_consolidation_items(
                    self._programmatic_similarity_merge_items(items=current, category_name=category_name)
                )
                break
            current = self._renumber_consolidation_items(next_items)
            if len(current) >= prev_n:
                self.logger.info(
                    "Merge round did not shrink item count for %s / %s (token batching); stopping early.",
                    party,
                    category_name,
                )
                break

        # Final single-shot merge when everything fits one prompt but multiple rows remain
        if (
            len(current) > 1
            and self._estimate_merge_plan_prompt_tokens(party, category_name, current) <= max_input_tokens
        ):
            merged = run_one_round(current)
            if merged:
                current = self._renumber_consolidation_items(merged)

        # Legacy fallback: cap by item count if token env never triggers (tiny max_input_tokens edge case)
        _lbs = max(2, int(llm_batch_size or 12))
        if len(current) > _lbs and self._estimate_merge_plan_prompt_tokens(party, category_name, current) > max_input_tokens:
            next_items = []
            for ch in self._chunk_list(current, _lbs):
                next_items.extend(run_one_round(ch))
            if next_items:
                current = self._renumber_consolidation_items(next_items)

        owner_resps = [str(x.get("text") or "").strip() for x in current if str(x.get("text") or "").strip()]
        reasons: List[str] = []
        for x in current:
            if not str(x.get("text") or "").strip():
                continue
            raw_r = str(x.get("reasoning") or "").strip()
            phrase_list = [p.strip() for p in raw_r.split(";") if p.strip()]
            reasons.append(phrase_list[0] if phrase_list else "")
        cites = [self._collapse_sources_to_citation(list(x.get("citation_sources") or [])) for x in current if str(x.get("text") or "").strip()]

        # Citations stay aligned 1:1 with consolidated responsibility lines (same count).
        while len(cites) < len(owner_resps):
            cites.append({"pageNumbers": [1], "section": ["Document"]})
        cites = cites[: len(owner_resps)]

        # Reasoning length may differ from Owner Responsibility (extras appended separately; no padding).
        orphan_extra = [str(x or "").strip() for x in (orphan_reasonings or [])]
        orphan_extra = [x for x in orphan_extra if x]
        full_reasoning = reasons + orphan_extra

        merged_keywords = self._merge_related_keywords_from_item_dicts(
            [x for x in current if isinstance(x, dict)], cap=64, category_name=category_name
        )
        if not merged_keywords:
            merged_keywords = broaden_related_keywords(
                category=category_name,
                owner_responsibility=owner_resps,
                base_keywords=[],
                max_items=64,
            )

        return {
            "Responsible Party": party,
            "Owner Responsibility": owner_resps,
            "Reasoning": full_reasoning,
            "related_keywords": merged_keywords,
            "citations": cites,
            "docId": document_name,
        }

    def _batch_llm_consolidation(self, party: str, responsibilities: List[str],
                                reasonings: List[str], citations: List[Dict],
                                category_name: str, document_name: str) -> Dict[str, Any]:
        """
        Batch LLM consolidation: Split responsibilities into smaller batches and process with LLM.
        Used when full LLM consolidation fails or exceeds loss threshold.
        """
        self.logger.info(f"BATCH LLM CONSOLIDATION: {party} in '{category_name}' - processing in smaller batches")
        
        # Determine batch size based on input size
        total_items = len(responsibilities)
        if total_items <= 3:
            batch_size = total_items  # Don't batch if already small
        elif total_items <= 8:
            batch_size = 3  # Small batches for medium inputs
        else:
            batch_size = 4  # Slightly larger batches for big inputs
        
        self.logger.info(f"Processing {total_items} responsibilities in batches of {batch_size}")
        
        # Split into batches
        batches = []
        for i in range(0, total_items, batch_size):
            end_idx = min(i + batch_size, total_items)
            batch = {
                "responsibilities": responsibilities[i:end_idx],
                "reasonings": reasonings[i:end_idx], 
                "citations": citations[i:end_idx],
                "start_idx": i
            }
            batches.append(batch)
        
        # Process each batch with LLM
        all_consolidated_resps = []
        all_consolidated_reasons = []
        all_consolidated_citations = []
        
        for batch_num, batch in enumerate(batches, 1):
            self.logger.info(f"Processing batch {batch_num}/{len(batches)} ({len(batch['responsibilities'])} items)")
            
            # Create batch-specific prompt
            batch_prompt = f"""Consolidate these {len(batch['responsibilities'])} responsibilities for {party} in "{category_name}" category.
ONLY merge items that are 90%+ similar. Keep distinct obligations separate.
All items are for Responsible Party "{party}" ΓÇö use that **exact** string (from pagewise extraction) in the output; do not rewrite to generic roles.

INPUT RESPONSIBILITIES FOR {party.upper()} (Batch {batch_num}):
"""
            
            for i, (resp, reason, cite) in enumerate(zip(batch['responsibilities'], batch['reasonings'], batch['citations']), 1):
                page = cite.get("page", 1)
                section = cite.get("section", "Document")
                batch_prompt += f"{i}. {resp} | {reason} | Page {page}, {section}\n"
            
            batch_prompt += f"""
OUTPUT FORMAT - GROUPED BY PARTY (JSON only):
{{
  "{party}": {{
    "responsibilities": ["responsibility 1", "responsibility 2"],
    "reasoning": ["reasoning 1", "reasoning 2"]
  }},
  "docId": "{document_name}"
}}

IMPORTANT - CITATION HANDLING:
ΓÇó Do NOT include citation_groups in your response
ΓÇó Citations will be mapped automatically using code logic
ΓÇó Focus only on proper content consolidation
ΓÇó Return valid JSON object only"""

            try:
                # Call LLM for this batch
                response = self._generate_content(
                    batch_prompt, 
                    max_output_tokens=2000,
                    response_mime_type="text/plain",
                    require_json_object=False,
                    temperature=0.1
                )
                
                response_text = ""
                if hasattr(response, 'text'):
                    response_text = response.text
                elif isinstance(response, str):
                    response_text = response
                else:
                    response_text = str(response)
                
                if response_text and response_text.strip():
                    import json
                    import re
                    
                    json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                    if json_match:
                        json_str = json_match.group(0)
                        batch_parsed = json.loads(json_str)
                        
                        # Handle both old format and new grouped format
                        batch_resps = []
                        batch_reasons = []
                        
                        if isinstance(batch_parsed, dict):
                            # Check for new grouped format
                            if party in batch_parsed and isinstance(batch_parsed[party], dict):
                                party_data = batch_parsed[party]
                                batch_resps = party_data.get("responsibilities", [])
                                batch_reasons = party_data.get("reasoning", [])
                            # Check for old format
                            elif "Owner Responsibility" in batch_parsed:
                                batch_resps = batch_parsed.get("Owner Responsibility", [])
                                batch_reasons = batch_parsed.get("Reasoning", [])
                        
                        if batch_resps:  # Process if we found valid data
                            # Hardcoded (code-based) citation mapping only: ignore any LLM citation grouping.
                            batch_citations = self._map_citations_by_similarity(
                                consolidated_responsibilities=batch_resps,
                                original_responsibilities=batch["responsibilities"],
                                original_citations=batch["citations"],
                                party=party,
                                category_name=category_name,
                            )
                            batch_citations = self._collapse_grouped_citations(batch_citations)
                            while len(batch_citations) < len(batch_resps):
                                batch_citations.append({"pageNumbers": [1], "section": ["Document"]})
                            
                            all_consolidated_resps.extend(batch_resps)
                            all_consolidated_reasons.extend(batch_reasons[:len(batch_resps)])
                            all_consolidated_citations.extend(batch_citations[:len(batch_resps)])
                            
                            batch_reduction = len(batch['responsibilities']) - len(batch_resps)
                            self.logger.info(f"Batch {batch_num} processed: {len(batch['responsibilities'])} ΓåÆ {len(batch_resps)} ({batch_reduction} merged)")
                            
                            continue  # Success, move to next batch
                
                # If we reach here, batch processing failed
                self.logger.warning(f"Batch {batch_num} LLM processing failed, using simple consolidation")
                
            except Exception as e:
                self.logger.warning(f"Batch {batch_num} error: {e}, using simple consolidation")
            
            # Fallback: just keep batch items as-is with simple citations
            all_consolidated_resps.extend(batch['responsibilities'])
            all_consolidated_reasons.extend(batch['reasonings'])
            for cite in batch['citations']:
                all_consolidated_citations.append({
                    "pageNumbers": [cite.get("page", 1)],
                    "section": [cite.get("section", "Document")]
                })
        
        # Final result
        final_count = len(all_consolidated_resps)
        reduction_percent = ((total_items - final_count) / total_items) * 100
        
        self.logger.info(
            f"BATCH LLM CONSOLIDATION COMPLETE: {party} in '{category_name}' - "
            f"{total_items} ΓåÆ {final_count} responsibilities ({reduction_percent:.1f}% reduction)"
        )
        
        # Count multi-source citations
        multi_source_count = sum(1 for c in all_consolidated_citations if len(c.get("pageNumbers") or []) > 1)
        if multi_source_count > 0:
            self.logger.info(f"Created {multi_source_count} multi-source citations in batch processing")
        
        return {
            "Responsible Party": party,
            "Owner Responsibility": all_consolidated_resps,
            "Reasoning": all_consolidated_reasons,
            "citations": all_consolidated_citations,
            "docId": document_name
        }

    def _batch_consolidation_fallback(self, party: str, responsibilities: List[str],
                                    reasonings: List[str], citations: List[Dict],
                                    category_name: str, document_name: str) -> Dict[str, Any]:
        """
        Batch consolidation fallback: merge highly similar obligations using text similarity.
        Used when LLM fails or exceeds loss threshold.
        """
        self.logger.info(f"BATCH CONSOLIDATION: {party} in '{category_name}' - using similarity-based merging")
        
        from difflib import SequenceMatcher
        
        def calculate_similarity(text1: str, text2: str) -> float:
            """Calculate text similarity"""
            import re
            # Normalize texts
            norm1 = re.sub(r'[^\w\s]', ' ', text1.lower()).strip()
            norm2 = re.sub(r'[^\w\s]', ' ', text2.lower()).strip()
            norm1 = ' '.join(norm1.split())
            norm2 = ' '.join(norm2.split())
            return SequenceMatcher(None, norm1, norm2).ratio()
        
        # Group similar responsibilities
        consolidated_groups = []
        used_indices = set()
        
        for i, (resp, reason, cite) in enumerate(zip(responsibilities, reasonings, citations)):
            if i in used_indices:
                continue
            
            # Start new group
            current_group = {
                "responsibilities": [resp],
                "reasonings": [reason],
                "citations": [cite],
                "indices": [i]
            }
            used_indices.add(i)
            
            # Find highly similar items (90%+ similarity)
            for j, (other_resp, other_reason, other_cite) in enumerate(zip(responsibilities, reasonings, citations)):
                if j in used_indices:
                    continue
                
                similarity = calculate_similarity(resp, other_resp)
                if similarity >= 0.90:  # 90%+ similarity threshold
                    current_group["responsibilities"].append(other_resp)
                    current_group["reasonings"].append(other_reason)
                    current_group["citations"].append(other_cite)
                    current_group["indices"].append(j)
                    used_indices.add(j)
                    self.logger.debug(f"Merged similar items ({similarity:.2f}): {resp[:40]}... + {other_resp[:40]}...")
            
            consolidated_groups.append(current_group)
        
        # Build consolidated result
        final_responsibilities = []
        final_reasonings = []
        final_citations = []
        
        for group in consolidated_groups:
            if len(group["responsibilities"]) == 1:
                # Single item - keep as is
                final_responsibilities.append(group["responsibilities"][0])
                final_reasonings.append(group["reasonings"][0])
                final_citations.append({
                    "pageNumbers": [group["citations"][0].get("page", 1)],
                    "section": [group["citations"][0].get("section", "Document")],
                })
            else:
                # Multiple similar items - merge intelligently
                # Use the most detailed/longest text as primary
                primary_resp = max(group["responsibilities"], key=len)
                
                # Combine unique reasonings
                unique_reasons = []
                seen_reasons = set()
                for reason in group["reasonings"]:
                    reason_norm = reason.lower().strip()
                    if reason_norm and reason_norm not in seen_reasons:
                        unique_reasons.append(reason)
                        seen_reasons.add(reason_norm)
                
                combined_reasoning = "; ".join(unique_reasons) if unique_reasons else "Consolidated obligation"
                
                # SIMPLE CITATION LOGIC: Collect all unique citations for merged responsibility
                pages: List[int] = []
                secs: List[str] = []
                seen_citations = set()
                
                for cite in group["citations"]:
                    page = cite.get("page", 1)
                    section = cite.get("section", "Document")
                    cite_key = (page, section)
                    
                    if cite_key not in seen_citations:
                        try:
                            p = int(page) if page is not None else 0
                        except (TypeError, ValueError):
                            p = 0
                        s = str(section or "").strip() or "Document"
                        if p > 0 and p not in pages:
                            pages.append(p)
                        if s and s not in secs:
                            secs.append(s)
                        seen_citations.add(cite_key)
                
                # Log multi-source citation creation
                if len(pages) > 1:
                    self.logger.info(
                        f"Multi-source citation created: {len(group['responsibilities'])} responsibilities ΓåÆ Pages {', '.join(str(x) for x in pages)}"
                    )
                
                final_responsibilities.append(primary_resp)
                final_reasonings.append(combined_reasoning) 
                final_citations.append({"pageNumbers": pages or [1], "section": secs or ["Document"]})
                
                self.logger.info(f"Batch merged group of {len(group['responsibilities'])} similar items")
        
        reduction_percent = ((len(responsibilities) - len(final_responsibilities)) / len(responsibilities)) * 100
        self.logger.info(
            f"BATCH CONSOLIDATION COMPLETE: {party} in '{category_name}' - "
            f"{len(responsibilities)} ΓåÆ {len(final_responsibilities)} responsibilities "
            f"({reduction_percent:.1f}% reduction, {100-reduction_percent:.1f}% retention)"
        )
        
        return {
            "Responsible Party": party,
            "Owner Responsibility": final_responsibilities,
            "Reasoning": final_reasonings,
            "citations": final_citations,
            "docId": document_name
        }

    def _simple_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Simple consolidation approach:
        1. Code: Group by category and responsible party
        2. LLM: Merge similar responsibilities with proper citation handling
        3. Ensure one entry per responsible party per category
        """
        consolidated_results = []

        def _normalize_party_for_grouping(party_raw: Any) -> str:
            """Use Responsible Party as extracted (trimmed only); no canonical role rewriting."""
            s = str(party_raw or "").strip()
            return s if s else "Unknown"

        def _coerce_citation_to_page_section(cite: Any) -> Dict[str, Any]:
            """
            Normalize citation input into {page:int, section:str}.
            Handles:
            - {page, section}
            - {docId, references:[{page, section}, ...]}
            - [[{page, section}, ...], ...] (take first item)
            """
            if isinstance(cite, list) and cite:
                return _coerce_citation_to_page_section(cite[0])
            if isinstance(cite, dict):
                # Collapsed 1:1 citation object: {pageNumbers:[...], section:[...]}
                if "pageNumbers" in cite or isinstance(cite.get("section"), list):
                    pns = cite.get("pageNumbers")
                    pages = _parse_page_numbers(pns)
                    if not pages and isinstance(pns, int):
                        pages = [pns] if pns else []
                    page = pages[0] if pages else 1
                    sec_raw = cite.get("section")
                    sec_list = _parse_sections(sec_raw)
                    sec0 = sec_list[0] if sec_list else (str(sec_raw).strip() if sec_raw not in (None, "") else "Document")
                    try:
                        page = int(page) if page else 1
                    except (TypeError, ValueError):
                        page = 1
                    return {"page": page, "section": str(sec0).strip() or "Document"}
                if "page" in cite or "section" in cite:
                    try:
                        page = int(cite.get("page") or 1)
                    except (TypeError, ValueError):
                        page = 1
                    return {"page": page, "section": str(cite.get("section") or "").strip() or "Document"}
                refs = cite.get("references")
                if isinstance(refs, list) and refs:
                    ref0 = refs[0] if isinstance(refs[0], dict) else {}
                    try:
                        page = int(ref0.get("page") or 1)
                    except (TypeError, ValueError):
                        page = 1
                    return {"page": page, "section": str(ref0.get("section") or "").strip() or "Document"}
            return {"page": 1, "section": "Document"}
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            self.logger.info(f"Simple consolidation for category '{category_name}': {len(obligations)} obligations")
            
            # Group all responsibilities by responsible party
            party_groups = {}
            for obligation in obligations:
                party = _normalize_party_for_grouping(obligation.get("Responsible Party", "Unknown"))
                responsibilities = obligation.get("Owner Responsibility", [])
                reasonings_raw = obligation.get("Reasoning", [])
                reas_list = reasonings_raw if isinstance(reasonings_raw, list) else ([reasonings_raw] if reasonings_raw else [])
                reas_list = [str(x or "").strip() for x in reas_list]
                citations = obligation.get("citations", [])
                
                if party not in party_groups:
                    party_groups[party] = {
                        "responsibilities": [],
                        "reasonings": [],
                        "citations": [],
                        "keyword_rows": [],
                    }

                party_groups[party]["responsibilities"].extend(responsibilities)
                party_groups[party]["reasonings"].extend(reas_list)
                for one_duty in responsibilities:
                    duty_txt = str(one_duty or "").strip()
                    ob_kw = self._coerce_related_keywords_list(
                        obligation.get("related_keywords"),
                        category=category_name,
                        owner_responsibility=duty_txt,
                    )
                    party_groups[party]["keyword_rows"].append(list(ob_kw))

                # Citations: one entry per responsibility row (aligned with duties, not with reasoning count).
                for i in range(len(responsibilities)):
                    # Better citation handling - try to use available citations or keep original structure
                    if i < len(citations):
                        party_groups[party]["citations"].append(_coerce_citation_to_page_section(citations[i]))
                    elif len(citations) == 1:
                        party_groups[party]["citations"].append(_coerce_citation_to_page_section(citations[0]))
                    elif len(citations) > 0:
                        party_groups[party]["citations"].append(_coerce_citation_to_page_section(citations[-1]))
                    else:
                        # Only use fallback if no citations exist at all
                        party_groups[party]["citations"].append({"page": 1, "section": "Document"})
            
            # Use LLM to consolidate each party group (limit size to avoid citation loss)
            consolidated_obligations = []
            for party, party_data in party_groups.items():
                responsibilities = party_data["responsibilities"]
                reasonings = party_data["reasonings"]
                citations = party_data["citations"]
                keyword_rows = party_data.get("keyword_rows") or []

                consolidated_obligation = self._consolidate_party_responsibilities(
                    party,
                    responsibilities,
                    reasonings,
                    citations,
                    category_name,
                    document_name,
                    keyword_rows=keyword_rows,
                )
                if consolidated_obligation:
                    consolidated_obligations.append(consolidated_obligation)

            # GUARANTEE: one obligation per party per category (merge chunk outputs)
            merged_by_party: Dict[str, Dict[str, Any]] = {}
            for ob in consolidated_obligations:
                if not isinstance(ob, dict):
                    continue
                pkey = _normalize_party_for_grouping(ob.get("Responsible Party", "Unknown"))
                if pkey not in merged_by_party:
                    o2 = dict(ob)
                    o2["Responsible Party"] = pkey
                    merged_by_party[pkey] = o2
                    continue
                acc = merged_by_party[pkey]
                acc["Owner Responsibility"] = list(acc.get("Owner Responsibility") or []) + list(ob.get("Owner Responsibility") or [])
                acc["Reasoning"] = list(acc.get("Reasoning") or []) + list(ob.get("Reasoning") or [])
                merged_kw = _merge_str_lists(acc.get("related_keywords"), ob.get("related_keywords"))
                acc["related_keywords"] = broaden_related_keywords(
                    category=str(category_name or ""),
                    owner_responsibility=acc.get("Owner Responsibility"),
                    base_keywords=merged_kw if isinstance(merged_kw, list) else [],
                    max_items=64,
                )
                acc["citations"] = list(acc.get("citations") or []) + list(ob.get("citations") or [])
                if not acc.get("docId") and ob.get("docId"):
                    acc["docId"] = ob.get("docId")
            consolidated_obligations = list(merged_by_party.values())

            # Citations stay 1:1 with Owner Responsibility lines; Reasoning may be shorter or longer.
            for ob in consolidated_obligations:
                if not isinstance(ob, dict):
                    continue
                resps = list(ob.get("Owner Responsibility") or [])
                cites = list(ob.get("citations") or [])
                while len(cites) < len(resps):
                    cites.append({"pageNumbers": [1], "section": ["Document"]})
                ob["citations"] = cites[: len(resps)]
            
            if consolidated_obligations:
                consolidated_results.append({
                    "category": category_name,
                    "obligations": consolidated_obligations
                })
            else:
                # Fallback to original
                consolidated_results.append(category_group)
        
        return consolidated_results
    
    def _llm_consolidate_by_party(self, party_groups: Dict[str, Dict[str, List]], category_name: str, document_name: str) -> List[Dict[str, Any]]:
        """
        Use LLM to consolidate responsibilities grouped by party.
        Ensures one entry per responsible party per category.
        """
        
        consolidated_obligations = []
        
        for party, party_data in party_groups.items():
            responsibilities = party_data["responsibilities"]
            reasonings = party_data["reasonings"]
            citations = party_data["citations"]
            
            if not responsibilities:
                continue
            
            # Create consolidated entry for this party
            consolidated_obligation = self._consolidate_party_responsibilities(
                party,
                responsibilities,
                reasonings,
                citations,
                category_name,
                document_name,
                keyword_rows=party_data.get("keyword_rows") or [],
            )
            
            if consolidated_obligation:
                consolidated_obligations.append(consolidated_obligation)
        
        return consolidated_obligations
    
    def _consolidate_party_responsibilities_legacy(self, party: str, responsibilities: List[str], 
                                          reasonings: List[str], citations: List[Dict], 
                                          category_name: str, document_name: str) -> Dict[str, Any]:
        """
        Consolidate all responsibilities for a single party using LLM.
        """

        # Preserve original inputs for hardcoded (code-based) citation mapping.
        input_responsibilities = list(responsibilities or [])
        input_reasonings = list(reasonings or [])
        input_citations = list(citations or [])
        
        prompt = f"""Consolidate responsibilities for {party} in "{category_name}" category by merging HIGHLY SIMILAR obligations only.

SMART CONSOLIDATION RULES:
ΓÇó Merge obligations that are 90%+ similar in meaning and content
ΓÇó Keep distinct obligations separate - different amounts, timing, or conditions stay separate
ΓÇó For example: "Pay $500 estimated" vs "Pay actual amount if exceeds" are DIFFERENT obligations
ΓÇó Only merge near-duplicates like "Pay rent monthly" + "Pay monthly rent" 
ΓÇó Maintain reasonable consolidation ratio - don't over-reduce the total count
ΓÇó Preserve specific details like amounts, percentages, timeframes, and conditions
ΓÇó Clean citations (remove document names, keep only page + section)

OUTPUT INTEGRITY RULES (CRITICAL):
ΓÇó Do NOT drop obligations. Every distinct input obligation must appear in output, either unchanged or merged.
ΓÇó Do NOT write meta-reasoning about consolidation (avoid ΓÇ£merged/kept separateΓÇ¥, ΓÇ£uniqueΓÇ¥, ΓÇ£distinct scenarioΓÇ¥, ΓÇ£granular languageΓÇ¥, ΓÇ£clear expectationsΓÇ¥).
ΓÇó Reasoning must describe the responsibility itself (e.g., rent payment, tax allocation, insurance coverage, indemnity scope) and why it exists in the contract.
ΓÇó Reasoning does NOT need to be 1:1 with responsibilities. Provide a small set of bullets (1ΓÇô8) that collectively support all listed responsibilities.
ΓÇó Ensure no responsibility is left unsupported; if needed, add another reasoning bullet rather than forcing 1:1 alignment.

INPUT RESPONSIBILITIES FOR {party.upper()}:
"""
        
        for i, (resp, reason, cite) in enumerate(zip(input_responsibilities, input_reasonings, input_citations), 1):
            page = cite.get("page", 1)
            section = cite.get("section", "Document")
            prompt += f"{i}. {resp} | {reason} | Page {page}, {section}\n"
        
        prompt += f"""
OUTPUT FORMAT - GROUPED BY PARTY (JSON only):
{{
  "{party}": {{
    "responsibilities": ["responsibility 1", "responsibility 2"],
    "reasoning": ["reasoning 1", "reasoning 2"]
  }},
  "docId": "{document_name}"
}}

BENEFITS OF GROUPING:
ΓÇó Eliminates repetition of party names
ΓÇó Cleaner, more efficient format
ΓÇó Easier to parse and process

IMPORTANT - CITATION HANDLING:
ΓÇó Do NOT include citation_groups in your response
ΓÇó Citations will be handled automatically by code logic
ΓÇó Focus only on consolidating the content properly
ΓÇó Return valid JSON object only"""

        # Check token count - if too large, use batch processing immediately
        estimated_tokens = len(prompt.split()) * 1.3  # Rough estimation (1.3x word count)
        token_limit = 3000  # Conservative limit to ensure good LLM performance
        
        if estimated_tokens > token_limit:
            message = f"≡ƒÜ¿ TOKEN LIMIT EXCEEDED: Estimated {estimated_tokens:.0f} tokens > {token_limit} limit - Switching to BATCH LLM processing for {party} in '{category_name}'"
            self.logger.warning(message)
            print(message, flush=True)  # Force terminal output with flush
            return self._batch_llm_consolidation(
                party, input_responsibilities, input_reasonings, input_citations, category_name, document_name
            )

        try:
            response = self._generate_content(
                prompt, 
                max_output_tokens=4000,
                response_mime_type="text/plain",
                require_json_object=False,
                temperature=0.1
            )
            
            response_text = ""
            if hasattr(response, 'text'):
                response_text = response.text
            elif isinstance(response, str):
                response_text = response
            else:
                response_text = str(response)
            
            if response_text and response_text.strip():
                # Parse JSON response - expecting single object now
                import json
                import re
                
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    json_str = json_match.group(0)
                    parsed_data = json.loads(json_str)
                    
                    if isinstance(parsed_data, dict):
                        # Handle both old format and new grouped format
                        party_data = None
                        
                        # Check for new grouped format
                        if party in parsed_data and isinstance(parsed_data[party], dict):
                            party_data = parsed_data[party]
                            output_responsibilities = party_data.get("responsibilities", [])
                            output_reasonings = party_data.get("reasoning", [])

                            if not isinstance(output_responsibilities, list):
                                output_responsibilities = []
                            if not isinstance(output_reasonings, list):
                                output_reasonings = []
                            
                            # Convert to old format for compatibility
                            parsed_data = {
                                "Responsible Party": party,
                                "Owner Responsibility": output_responsibilities,
                                "Reasoning": output_reasonings,
                                "docId": parsed_data.get("docId", document_name)
                            }
                        
                        # Use CODE-BASED CITATION MAPPING (no LLM dependency)
                        if "Owner Responsibility" in parsed_data:
                            output_responsibilities = parsed_data.get("Owner Responsibility", [])

                            grouped_citations = self._map_citations_by_similarity(
                                consolidated_responsibilities=output_responsibilities,
                                original_responsibilities=input_responsibilities,
                                original_citations=input_citations,
                                party=party,
                                category_name=category_name
                            )
                            parsed_data["citations"] = self._collapse_grouped_citations(grouped_citations)
                            
                            # Validate consolidation quality - check for content loss (not just count reduction)
                            input_count = len(input_responsibilities)
                            output_count = len(parsed_data.get("Owner Responsibility", []))
                            output_responsibilities = parsed_data.get("Owner Responsibility", [])
                            
                            # Content-based loss detection using similarity matching
                            from difflib import SequenceMatcher
                            
                            def normalize_text(text):
                                import re
                                return ' '.join(re.sub(r'[^\w\s]', ' ', text.lower()).split())
                            
                            def calculate_similarity(text1, text2):
                                return SequenceMatcher(None, normalize_text(text1), normalize_text(text2)).ratio()
                            
                            # Check if each input responsibility has a reasonable match in output
                            unmatched_inputs = []
                            similarity_threshold = 0.6  # 60% similarity threshold for content matching
                            
                            for i, input_resp in enumerate(input_responsibilities):
                                best_match_score = 0
                                for output_resp in output_responsibilities:
                                    similarity = calculate_similarity(input_resp, output_resp)
                                    best_match_score = max(best_match_score, similarity)
                                
                                if best_match_score < similarity_threshold:
                                    unmatched_inputs.append((i, input_resp[:60] + "..."))
                            
                            content_loss_count = len(unmatched_inputs)
                            content_loss_percent = (content_loss_count / input_count) * 100 if input_count > 0 else 0
                            
                            if content_loss_count > 0:
                                self.logger.warning(
                                    f"CONTENT LOSS DETECTED: {party} in '{category_name}' - "
                                    f"Input: {input_count}, Output: {output_count}, "
                                    f"Unmatched content: {content_loss_count} items ({content_loss_percent:.1f}%)"
                                )
                                
                                # Log which specific content was lost
                                for idx, resp_preview in unmatched_inputs[:3]:  # Show first 3
                                    self.logger.warning(f"  Lost content {idx+1}: {resp_preview}")
                                
                                # Threshold for content loss - if >15% of distinct content is lost, use batch processing
                                if content_loss_percent > 15:
                                    message = f"≡ƒÜ¿ CONTENT LOSS THRESHOLD EXCEEDED ({content_loss_percent:.1f}% > 15%) - Trying BATCH LLM processing for {party} in '{category_name}'"
                                    self.logger.warning(message)
                                    print(message, flush=True)  # Force terminal output with flush
                                    return self._batch_llm_consolidation(
                                        party, responsibilities, reasonings, citations, category_name, document_name
                                    )
                            else:
                                # Good consolidation - all content preserved in some form
                                reduction_percent = ((input_count - output_count) / input_count) * 100
                                message = f"Γ£à GOOD CONSOLIDATION: {party} in '{category_name}' - {input_count} ΓåÆ {output_count} ({reduction_percent:.1f}% reduction, no content loss)"
                                self.logger.info(message)
                                print(message, flush=True)  # Force terminal output with flush
                            
                            # Check for responsibility expansion
                            if output_count > input_count:
                                self.logger.info(
                                    f"Responsibility expansion: {party} in '{category_name}' - "
                                    f"Input: {input_count}, Output: {output_count} (detailed breakdown)"
                                )
                        
                        # Validate structure
                        if self._validate_grouped_obligation(parsed_data, document_name):
                            self.logger.info(f"Successfully consolidated {party} obligations for '{category_name}' (LLM)")
                            return parsed_data

            self.logger.warning(f"Could not parse LLM response for {party} in category '{category_name}', using fallback")
            
            # Check loss percentage from parsing failure
            input_count = len(responsibilities)
            expected_output_count = max(1, int(input_count * 0.75))  # Expect at least 75% retention
            
            self.logger.warning(
                f"LLM parsing failed for {party} in '{category_name}' - "
                f"Trying BATCH LLM processing"
            )
            return self._batch_llm_consolidation(
                party, responsibilities, reasonings, citations, category_name, document_name
            )
            
            # Smart fallback for non-critical categories: if we have fewer responsibilities than citations, 
            # it means some were merged
            grouped_citations = []
            
            if len(responsibilities) < len(citations):
                # Responsibilities were merged - distribute citations appropriately
                self.logger.info(
                    f"Fallback: {len(citations)} citations ΓåÆ {len(responsibilities)} "
                    f"responsibilities (some merged). Grouping citations intelligently..."
                )
                
                # Strategy: distribute citations across responsibilities proportionally
                citations_per_resp = len(citations) // len(responsibilities)
                remaining = len(citations) % len(responsibilities)
                
                cite_idx = 0
                for resp_idx in range(len(responsibilities)):
                    citation_group = []
                    
                    # How many citations for this responsibility?
                    num_cites = citations_per_resp + (1 if resp_idx < remaining else 0)
                    
                    for _ in range(num_cites):
                        if cite_idx < len(citations):
                            cite = citations[cite_idx]
                            citation_group.append({
                                "page": cite.get("page", 1),
                                "section": cite.get("section", "Document")
                            })
                            cite_idx += 1
                    
                    if citation_group:
                        grouped_citations.append(citation_group)
                    else:
                        grouped_citations.append([{"page": 1, "section": "Document"}])
                
                multi_source = sum(1 for cg in grouped_citations if len(cg) > 1)
                self.logger.info(
                    f"Fallback citation grouping: {[len(cg) for cg in grouped_citations]} citations per responsibility, "
                    f"{multi_source} responsibilities with multi-source citations"
                )
            else:
                # No merging - simple 1:1 mapping
                for cite in citations:
                    grouped_citations.append([{
                        "page": cite.get("page", 1),
                        "section": cite.get("section", "Document")
                    }])
            
            fallback_result = {
                "Responsible Party": party,
                "Owner Responsibility": responsibilities,
                "Reasoning": reasonings,
                "citations": grouped_citations,
                "docId": document_name
            }
            
            self.logger.info(f"Using fallback for {party} obligations in '{category_name}' (citations: {len(grouped_citations)})")
            return fallback_result
                
        except Exception as e:
            self.logger.error(f"Error consolidating {party} in category '{category_name}': {e}")
            
            # Check if this is a critical category that requires preservation
            is_critical_category = category_name in [
                "Financial & Cost Allocation Systems", 
                "Compliance & Regulatory",
                "Building Structure & Envelope"
            ]
            
            self.logger.warning(
                f"LLM exception for {party} in '{category_name}' - "
                f"Trying BATCH LLM processing"
            )
            return self._batch_llm_consolidation(
                party, responsibilities, reasonings, citations, category_name, document_name
            )
            
            # Smart fallback for non-critical categories: apply same logic as above
            grouped_citations = []
            
            if len(responsibilities) < len(citations):
                # Responsibilities were merged - distribute citations appropriately
                self.logger.info(
                    f"Exception fallback: {len(citations)} citations ΓåÆ {len(responsibilities)} "
                    f"responsibilities (some merged). Grouping citations..."
                )
                
                citations_per_resp = len(citations) // len(responsibilities)
                remaining = len(citations) % len(responsibilities)
                
                cite_idx = 0
                for resp_idx in range(len(responsibilities)):
                    citation_group = []
                    num_cites = citations_per_resp + (1 if resp_idx < remaining else 0)
                    
                    for _ in range(num_cites):
                        if cite_idx < len(citations):
                            cite = citations[cite_idx]
                            citation_group.append({
                                "page": cite.get("page", 1),
                                "section": cite.get("section", "Document")
                            })
                            cite_idx += 1
                    
                    if citation_group:
                        grouped_citations.append(citation_group)
                    else:
                        grouped_citations.append([{"page": 1, "section": "Document"}])
                
                multi_source = sum(1 for cg in grouped_citations if len(cg) > 1)
                self.logger.info(
                    f"Exception fallback citation grouping: {[len(cg) for cg in grouped_citations]} "
                    f"citations per responsibility, {multi_source} multi-source"
                )
            else:
                # No merging - simple 1:1 mapping
                for cite in citations:
                    grouped_citations.append([{
                        "page": cite.get("page", 1),
                        "section": cite.get("section", "Document")
                    }])
            
            fallback_result = {
                "Responsible Party": party,
                "Owner Responsibility": responsibilities,
                "Reasoning": reasonings,
                "citations": grouped_citations,
                "docId": document_name
            }
            
            self.logger.info(f"Exception fallback for {party} obligations in '{category_name}' (citations: {len(grouped_citations)})")
            return fallback_result
    
    def _consolidate_party_responsibilities(
        self,
        party: str,
        responsibilities: List[str],
        reasonings: List[str],
        citations: List[Dict],
        category_name: str,
        document_name: str,
        keyword_rows: Optional[List[List[str]]] = None,
    ) -> Dict[str, Any]:
        """
        Consolidate all responsibilities for a single party.

        STRICT REASONING INHERITANCE (CRITICAL):
        - The final `Reasoning` array MUST come ONLY from the pagewise input.
        - The LLM is forbidden from generating or editing `Reasoning`.
        - This function uses the LLM (optionally) only to propose a merge plan (grouping + merged responsibility text).
        - Python merges/deduplicates pagewise reasonings for the grouped sources without summarizing or ranking.
        """
        input_responsibilities = list(responsibilities or [])
        ir = [str(x or "").strip() for x in (reasonings or [])]
        input_citations = list(citations or [])
        kw_rows = keyword_rows or []
        n_resp = len(input_responsibilities)
        # Index correspondence for merge: duty i uses reasoning i when present; no repeat-last padding.
        paired = ir[:n_resp] if n_resp else []
        orphan_tail = ir[n_resp:] if len(ir) > n_resp else []

        items: List[Dict[str, Any]] = []
        for i, resp in enumerate(input_responsibilities, 1):
            r = paired[i - 1] if (i - 1) < len(paired) else ""
            c = input_citations[i - 1] if (i - 1) < len(input_citations) else (input_citations[-1] if input_citations else {"page": 1, "section": "Document"})
            if not isinstance(c, dict):
                c = {"page": 1, "section": "Document"}
            raw_kw = kw_rows[i - 1] if (i - 1) < len(kw_rows) else []
            row_kw = self._coerce_related_keywords_list(
                raw_kw,
                category=category_name,
                owner_responsibility=str(resp or "").strip(),
            )
            items.append(
                {
                    "id": f"r{i}",
                    "text": str(resp or "").strip(),
                    "reasoning": r,
                    "citation_sources": [{"page": c.get("page", 1), "section": c.get("section", "Document")}],
                    "related_keywords": row_kw,
                }
            )

        return self._consolidate_party_items_map_reduce(
            party=party,
            category_name=category_name,
            items=items,
            document_name=document_name,
            llm_batch_size=12,
            orphan_reasonings=orphan_tail,
        )

    def _validate_simple_obligation(self, item: Dict[str, Any], document_name: str) -> bool:
        """Validate simple obligation structure."""
        required_fields = ["Responsible Party", "Owner Responsibility", "Reasoning", "citations"]
        
        for field in required_fields:
            if field not in item:
                return False
        
        if not isinstance(item["Owner Responsibility"], list) or not item["Owner Responsibility"]:
            return False
            
        if not isinstance(item["citations"], list):
            return False
        
        # Ensure docId is set
        item["docId"] = document_name
        
        return True
    
    def _validate_grouped_obligation(self, item: Dict[str, Any], document_name: str) -> bool:
        """Validate obligation with grouped citations structure."""
        required_fields = ["Responsible Party", "Owner Responsibility", "Reasoning", "citations"]
        
        for field in required_fields:
            if field not in item:
                return False
        
        responsibilities = item["Owner Responsibility"]
        citations = item["citations"]
        reasoning = item["Reasoning"]
        
        if not isinstance(responsibilities, list) or not responsibilities:
            return False
        
        if not isinstance(citations, list):
            return False
            
        if not isinstance(reasoning, list):
            return False
        
        # Check that citations is a list of lists (grouped structure)
        for citation_group in citations:
            if isinstance(citation_group, dict):
                if "pageNumbers" in citation_group:
                    pn = citation_group.get("pageNumbers")
                    if pn is None:
                        return False
                elif "page" in citation_group:
                    pass
                else:
                    return False
                continue
            if not isinstance(citation_group, list):
                return False
            for cite in citation_group:
                if not isinstance(cite, dict):
                    return False
                if "page" not in cite and "pageNumbers" not in cite:
                    return False
        
        # Ensure 1:1 correspondence between responsibilities and citation groups
        if len(citations) != len(responsibilities):
            return False
        
        # Ensure docId is set
        item["docId"] = document_name
        
        return True

    def _llm_consolidate_obligations(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Use LLM to intelligently consolidate and deduplicate obligations within each category.
        
        Args:
            results: List of category groups with obligations
            document_name: Name of the source document
            
        Returns:
            Consolidated results with deduplicated obligations
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                # Skip empty categories
                continue
            
            self.logger.info(f"Consolidating {len(obligations)} obligations in category: {category_name}")
            
            # Convert obligations to flat format for LLM consolidation
            flat_obligations = []
            for obligation in obligations:
                party = obligation.get("Responsible Party", "Unknown")
                responsibilities = obligation.get("Owner Responsibility", [])
                reasonings = obligation.get("Reasoning", [])
                citations = obligation.get("citations", [])
                doc_id = obligation.get("docId", document_name)
                
                # Create individual obligation entries for each responsibility
                for i, responsibility in enumerate(responsibilities):
                    reasoning = reasonings[i] if i < len(reasonings) else _auto_reasoning_from_responsibility(responsibility, party=party, category=str(category_name or ""))
                    citation_info = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                    
                    # Format citation as string
                    if isinstance(citation_info, dict):
                        citation_str = f"Page {citation_info.get('page', 1)}, Section {citation_info.get('section', 'Document')}"
                    else:
                        citation_str = str(citation_info)
                    
                    flat_obligations.append({
                        "DutyType": category_name,
                        "Responsible Party": party,
                        "Owner Responsibility": [responsibility],
                        "Reasoning": [reasoning],
                        "Citation": citation_str,
                        "related_keywords": self._coerce_related_keywords_list(
                            [],
                            category=str(category_name or ""),
                            owner_responsibility=responsibility,
                        )
                    })
            
            # If only few obligations, skip LLM consolidation to avoid overhead
            if len(flat_obligations) <= 5:
                # Convert back to original format
                consolidated_category = {
                    "category": category_name,
                    "obligations": self._convert_flat_to_grouped(flat_obligations, document_name)
                }
                consolidated_results.append(consolidated_category)
                continue
            
            # Use LLM to consolidate obligations within this category
            try:
                consolidated_obligations = self._llm_consolidate_category_obligations(
                    flat_obligations, category_name, document_name
                )
                
                if consolidated_obligations:
                    consolidated_category = {
                        "category": category_name,
                        "obligations": consolidated_obligations
                    }
                    consolidated_results.append(consolidated_category)
                    self.logger.info(f"Category {category_name}: {len(flat_obligations)} -> {len(consolidated_obligations)} obligations")
                else:
                    # Fallback if LLM consolidation fails
                    self.logger.warning(f"LLM consolidation failed for {category_name}, using original data")
                    consolidated_results.append(category_group)
                    
            except Exception as e:
                self.logger.error(f"Error consolidating category {category_name}: {e}")
                # Fallback to original data
                consolidated_results.append(category_group)
        
        return consolidated_results
    
    def _llm_consolidate_category_obligations(self, flat_obligations: List[Dict[str, Any]], category_name: str, document_name: str) -> List[Dict[str, Any]]:
        """Use LLM to consolidate obligations within a single category."""
        
        num_input = len(flat_obligations)
        
        # Create metadata context
        parties = list(set(obj.get("Responsible Party", "") for obj in flat_obligations))
        party_info = ", ".join(parties) if parties else "Various parties"
        
        metadata_context = f"""
Document: {document_name}
Category: {category_name}
Parties: {party_info}
"""
        
        # Build consolidation prompt
        consolidation_prompt = f"""You are a legal analyst. You have been provided with {num_input} financial obligations extracted from a legal document within the "{category_name}" category.

Your task is to perform MINIMAL DEDUPLICATION ONLY - remove only EXACT duplicates while preserving ALL unique obligations.

CRITICAL: You MUST output a non-empty JSON array. Your output should have nearly the same number of items as input (95%+ preservation rate). Never return an empty array [].

ULTRA-CONSERVATIVE Consolidation Rules:
1. ONLY remove EXACT WORD-FOR-WORD duplicates with identical Responsible Party.
2. PRESERVE ALL unique obligations - treat each as distinct unless they are truly identical.
3. NEVER merge different obligations - even if they seem related, keep separate if they have:
   - Different dollar amounts or percentages
   - Different timeframes or deadlines
   - Different conditions or triggers
   - Different parties or roles
   - Different legal consequences
   - Different document sections or pages
4. Each element must have: "Responsible Party", "Owner Responsibility" (array of strings), "Reasoning" (array of strings), "docId", "citations" (array of objects with page/section).

STRICT Preservation Guidelines:
- Base Rent vs Operating Expenses = keep separate
- Tenant insurance vs Landlord insurance = keep separate  
- Pre-lease vs during-lease vs post-lease obligations = keep separate
- Purchase option vs lease obligations = keep separate
- Attorney fees vs other costs = keep separate
- Holdover penalties vs regular rent = keep separate
- Audit rights vs payment obligations = keep separate
- Default remedies vs regular obligations = keep separate
- Commission indemnification vs other indemnification = keep separate
- When in doubt, ALWAYS keep separate

Output Format Requirements:
- Responsible Party: For every output row, copy the **exact** `Responsible Party` string from the corresponding input row(s) (pagewise extraction). Preserve full legal entity names, contract-defined labels, and spelling as in the input. Do **not** substitute generic role words such as "Tenant" or "Landlord" unless the input literally used only those words.
- Owner Responsibility: array with typically 1 responsibility per obligation (avoid grouping unless truly identical)
- Reasoning: array of explanations matching each responsibility 
- docId: "{document_name}"
- citations: array of objects with "page" (number) and "section" (string) matching responsibility order

Citation Handling:
- Each Owner Responsibility must have a corresponding citation at the same index
- Preserve original page numbers and section names
- Do not merge citations unless obligations are truly identical
- For every citation, carry over "page" only from the input row(s) that support that responsibility: never omit a page that the source cited, and never add or fabricate page numbers.

{metadata_context}

Input obligations to consolidate:
{json.dumps(flat_obligations, indent=2)}

Output ONLY a valid JSON array of consolidated obligations, no other text."""

        try:
            # Call LLM for consolidation
            consolidated_response = llm_generate_content(consolidation_prompt, require_json_object=False)
            
            # Extract text from LLMResponse object
            if hasattr(consolidated_response, 'text'):
                response_text = consolidated_response.text.strip()
            elif hasattr(consolidated_response, 'content'):
                response_text = consolidated_response.content.strip()
            else:
                response_text = str(consolidated_response).strip()
                
            # Clean response
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Parse response
            consolidated_data = json.loads(response_text)
            
            if not isinstance(consolidated_data, list):
                raise ValueError("LLM response is not a JSON array")
            
            if not consolidated_data:
                raise ValueError("LLM returned empty array")
                
            # Validate and enhance the consolidated data
            validated_obligations = []
            for obj in consolidated_data:
                if self._validate_consolidated_obligation(obj, document_name):
                    validated_obligations.append(obj)
            
            return validated_obligations
            
        except Exception as e:
            self.logger.error(f"LLM consolidation failed: {e}")
            return []
    
    def _validate_consolidated_obligation(self, obligation: Dict[str, Any], document_name: str) -> bool:
        """Validate a consolidated obligation object."""
        required_fields = ["Responsible Party", "Owner Responsibility", "Reasoning", "citations"]
        
        for field in required_fields:
            if field not in obligation:
                return False
        
        # Ensure arrays are properly structured
        if not isinstance(obligation["Owner Responsibility"], list) or not obligation["Owner Responsibility"]:
            return False
            
        if not isinstance(obligation["Reasoning"], list) or not obligation["Reasoning"]:
            return False
            
        # Add docId if missing
        if "docId" not in obligation:
            obligation["docId"] = document_name
            
        return True
    
    def _convert_flat_to_grouped(self, flat_obligations: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Convert flat obligations back to grouped format by party."""
        party_groups = {}
        
        for obj in flat_obligations:
            party = obj.get("Responsible Party", "Unknown")
            if party not in party_groups:
                party_groups[party] = {
                    "Responsible Party": party,
                    "Owner Responsibility": [],
                    "Reasoning": [],
                    "docId": document_name,
                    "citations": []
                }
            
            party_groups[party]["Owner Responsibility"].extend(obj.get("Owner Responsibility", []))
            party_groups[party]["Reasoning"].extend(obj.get("Reasoning", []))
            
            # Parse citation back to object format
            citation_str = obj.get("Citation", "Page 1, Document")
            citation_obj = self._parse_citation_string(citation_str)
            party_groups[party]["citations"].append(citation_obj)
        
        return list(party_groups.values())
    
    def _parse_citation_string(self, citation_str: str) -> Dict[str, Any]:
        """Parse a citation string back to object format."""
        # Extract page and section from "Page X, Section Y" format
        import re
        
        page_match = re.search(r'Page\s+(\d+)', citation_str, re.IGNORECASE)
        section_match = re.search(r'Section\s+(.+?)(?:,|$)', citation_str, re.IGNORECASE)
        
        page = int(page_match.group(1)) if page_match else 1
        section = section_match.group(1).strip() if section_match else "Document"
        
        return {"page": page, "section": section}
    
    def _generate_keywords_for_responsibility(self, responsibility: str, category: str) -> List[str]:
        """Generate semantic search keywords for a responsibility (taxonomy + duty-derived phrases)."""
        return broaden_related_keywords(
            category=str(category or ""),
            owner_responsibility=responsibility,
            base_keywords=[],
            max_items=32,
        )
    
    def _improved_consolidate_obligations(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Improved consolidation that preserves data while removing exact duplicates.
        Much faster than LLM-based approach but still effective.
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            # Fast deduplication within category
            deduplicated_obligations = self._fast_deduplicate_obligations(obligations, category_name)
            
            if deduplicated_obligations:
                consolidated_category = {
                    "category": category_name,
                    "obligations": deduplicated_obligations
                }
                consolidated_results.append(consolidated_category)
        
        return consolidated_results
    
    def _fast_deduplicate_obligations(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Fast deduplication that only removes exact duplicates."""
        
        # Group by responsible party first
        party_groups = {}
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            if party not in party_groups:
                party_groups[party] = []
            party_groups[party].append(obligation)
        
        consolidated_obligations = []
        
        for party, party_obligations in party_groups.items():
            # Collect all unique responsibilities for this party
            unique_responsibilities = []
            unique_reasonings = []
            unique_citations = []
            
            seen_responsibilities = set()
            
            for obligation in party_obligations:
                responsibilities = obligation.get("Owner Responsibility", [])
                reasonings = obligation.get("Reasoning", [])
                citations = obligation.get("citations", [])
                
                for i, resp in enumerate(responsibilities):
                    # Normalize responsibility for comparison
                    normalized_resp = self._normalize_for_comparison(resp)
                    
                    if normalized_resp not in seen_responsibilities:
                        seen_responsibilities.add(normalized_resp)
                        unique_responsibilities.append(resp)
                        
                        # Add corresponding reasoning and citation
                        reasoning = (
                            reasonings[i]
                            if i < len(reasonings)
                            else _auto_reasoning_from_responsibility(resp, party=party, category="")
                        )
                        citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                        
                        unique_reasonings.append(reasoning)
                        unique_citations.append(citation)
            
            # Create consolidated obligation for this party
            if unique_responsibilities:
                consolidated_obligation = {
                    "Responsible Party": party,
                    "Owner Responsibility": unique_responsibilities,
                    "Reasoning": unique_reasonings,
                    "docId": party_obligations[0].get("docId", "document.pdf"),
                    "citations": unique_citations
                }
                consolidated_obligations.append(consolidated_obligation)
        
        return consolidated_obligations
    
    def _normalize_for_comparison(self, text: str) -> str:
        """Normalize text for duplicate comparison."""
        # Remove extra whitespace and convert to lowercase
        normalized = re.sub(r'\s+', ' ', text.lower().strip())
        
        # Remove common variations that shouldn't affect comparison
        normalized = re.sub(r'\b(the|a|an)\b', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        
        return normalized
    
    def _hybrid_consolidation_approach(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Hybrid approach: Programmatic deduplication + minimal LLM for true duplicates only.
        Prioritizes maximum data preservation while removing exact duplicates.
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            self.logger.info(f"Hybrid consolidation for {category_name}: {len(obligations)} obligations")
            
            # Step 1: Programmatic exact duplicate removal
            deduplicated_obligations = self._remove_exact_duplicates_only(obligations, category_name)
            
            consolidated_results.append({
                "category": category_name,
                "obligations": deduplicated_obligations
            })
            
            self.logger.info(f"{category_name}: Preserved {len(deduplicated_obligations)} obligations (minimal dedup only)")
        
        return consolidated_results
    
    def _remove_exact_duplicates_only(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Remove only exact word-for-word duplicates while preserving all unique obligations."""
        
        # Track seen responsibility texts for exact duplicate detection
        seen_combinations = set()
        unique_obligations = []
        
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            responsibilities = obligation.get("Owner Responsibility", [])
            reasonings = obligation.get("Reasoning", [])
            citations = obligation.get("citations", [])
            doc_id = obligation.get("docId", "document.pdf")
            
            # Process each responsibility individually to preserve granularity
            for i, responsibility in enumerate(responsibilities):
                # Create normalized key for exact duplicate detection
                normalized_key = (
                    party.lower().strip(),
                    responsibility.lower().strip().replace("  ", " ")
                )
                
                if normalized_key not in seen_combinations:
                    seen_combinations.add(normalized_key)
                    
                    # Create individual obligation for this responsibility
                    reasoning = reasonings[i] if i < len(reasonings) else _auto_reasoning_from_responsibility(responsibility, party=party, category=str(category_name or ""))
                    citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                    
                    unique_obligation = {
                        "Responsible Party": party,
                        "Owner Responsibility": [responsibility],  # Keep as individual item
                        "Reasoning": [reasoning],
                        "docId": doc_id,
                        "citations": [citation]
                    }
                    unique_obligations.append(unique_obligation)
        
        # Group by party to create final consolidated format
        return self._group_by_party_minimal(unique_obligations, category_name)
    
    def _group_by_party_minimal(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Group obligations by party while maintaining all unique responsibilities."""
        
        party_groups = {}
        
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            
            if party not in party_groups:
                party_groups[party] = {
                    "Responsible Party": party,
                    "Owner Responsibility": [],
                    "Reasoning": [],
                    "docId": obligation.get("docId", "document.pdf"),
                    "citations": []
                }
            
            # Add all responsibilities, reasonings, and citations
            party_groups[party]["Owner Responsibility"].extend(obligation.get("Owner Responsibility", []))
            party_groups[party]["Reasoning"].extend(obligation.get("Reasoning", []))
            party_groups[party]["citations"].extend(obligation.get("citations", []))
        
        return list(party_groups.values())
    
    def _smart_llm_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Smart LLM consolidation that merges similar obligations while preserving unique content.
        Balances intelligent merging with data preservation.
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            # Convert to flat format for LLM processing
            flat_obligations = []
            for obligation in obligations:
                party = obligation.get("Responsible Party", "Unknown")
                responsibilities = obligation.get("Owner Responsibility", [])
                reasonings = obligation.get("Reasoning", [])
                citations = obligation.get("citations", [])
                doc_id = obligation.get("docId", document_name)
                
                for i, responsibility in enumerate(responsibilities):
                    reasoning = reasonings[i] if i < len(reasonings) else _auto_reasoning_from_responsibility(responsibility, party=party, category=str(category_name or ""))
                    citation_info = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                    
                    flat_obligations.append({
                        "Responsible Party": party,
                        "Owner Responsibility": responsibility,
                        "Reasoning": reasoning,
                        "Citation": self._format_citation_for_llm(citation_info),
                        "docId": doc_id
                    })
            
            self.logger.info(f"Smart consolidation for {category_name}: {len(flat_obligations)} obligations")
            
            # Use LLM for smart consolidation
            try:
                consolidated_obligations = self._smart_consolidate_category(
                    flat_obligations, category_name, document_name
                )
                
                if consolidated_obligations:
                    consolidated_results.append({
                        "category": category_name,
                        "obligations": consolidated_obligations
                    })
                    self.logger.info(f"{category_name}: {len(flat_obligations)} -> {len(consolidated_obligations)} obligations")
                else:
                    # Fallback to original
                    consolidated_results.append(category_group)
                    
            except Exception as e:
                self.logger.error(f"Smart consolidation failed for {category_name}: {e}")
                consolidated_results.append(category_group)
        
        return consolidated_results
    
    def _smart_consolidate_category(self, flat_obligations: List[Dict[str, Any]], category: str, document_name: str) -> List[Dict[str, Any]]:
        """Use LLM for intelligent consolidation that merges similar while preserving unique obligations."""
        
        if len(flat_obligations) <= 3:
            # Skip LLM for very small sets
            return self._convert_flat_to_consolidated_format(flat_obligations, document_name)
        
        num_input = len(flat_obligations)
        
        # Create accuracy-focused consolidation prompt
        consolidation_prompt = f"""You are a legal analyst. You have {num_input} financial obligations from the "{category}" category.

Your task: PRESERVE ACCURACY and MEANING above all else.

CONSOLIDATION RULES:
1. **Very similar responsibilities** ΓåÆ Drop one duplicate
   - Example: "Pay rent monthly" + "Pay monthly rent" ΓåÆ Keep one

2. **Little bit different** ΓåÆ Merge but preserve BOTH meanings
   - Example: "Pay rent monthly" + "Pay rent in advance" ΓåÆ "Pay rent monthly in advance"
   - CRITICAL: Both concepts (monthly + in advance) must be preserved

3. **Different meanings** ΓåÆ Keep separate
   - Different amounts, parties, conditions, or legal contexts

ACCURACY REQUIREMENTS:
- NEVER lose legal meaning or nuance
- NEVER change the substance of any obligation
- NEVER combine obligations that have different legal consequences
- PRESERVE all specific details (amounts, timeframes, conditions)

MERGE ONLY WHEN:
- Same responsible party
- Same basic obligation type  
- Combining enhances clarity WITHOUT losing meaning
- You can fit both meanings into one clear statement

KEEP SEPARATE ALWAYS:
- Different dollar amounts or percentages
- Different parties (Landlord vs Tenant)
- Different timeframes or triggers
- Different obligation types (rent vs taxes vs insurance)
- Different legal contexts (regular vs holdover vs purchase option)

OUTPUT FORMAT:
Return a JSON array where each element has:
- "Responsible Party": exact same party string as in the input obligations (from pagewise extraction); preserve legal names and labels, not generic roles unless the input used only those.
- "Owner Responsibility": array of responsibility statements
- "Reasoning": array explaining each responsibility  
- "docId": "{document_name}"
- "citations": array of citation objects matching responsibilities

PRIORITY: Legal accuracy and meaning preservation over consolidation ratios.

INPUT OBLIGATIONS:
{json.dumps(flat_obligations, indent=2)}

Return ONLY the JSON array, no other text."""

        try:
            # Call LLM
            response = llm_generate_content(consolidation_prompt, require_json_object=False)
            
            # Extract text
            if hasattr(response, 'text'):
                response_text = response.text.strip()
            elif hasattr(response, 'content'):
                response_text = response.content.strip()
            else:
                response_text = str(response).strip()
            
            # Clean response
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Parse JSON
            consolidated_data = json.loads(response_text)
            
            if not isinstance(consolidated_data, list) or not consolidated_data:
                raise ValueError("Invalid LLM response")
            
            # Convert to final format
            return self._convert_smart_llm_response(consolidated_data, document_name)
            
        except Exception as e:
            self.logger.error(f"Smart LLM consolidation failed: {e}")
            return self._convert_flat_to_consolidated_format(flat_obligations, document_name)
    
    def _convert_smart_llm_response(self, llm_data: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Convert smart LLM response to required format."""
        converted = []
        
        for item in llm_data:
            if not isinstance(item, dict):
                continue
            
            party = item.get("Responsible Party", "Unknown")
            responsibilities = item.get("Owner Responsibility", [])
            reasonings = item.get("Reasoning", [])
            
            if not responsibilities:
                continue
            
            # Ensure reasonings match responsibilities count
            while len(reasonings) < len(responsibilities):
                reasonings.append(
                    _auto_reasoning_from_responsibility(
                        responsibilities[len(reasonings)],
                        party=party,
                        category=str(item.get("DutyType") or item.get("category") or ""),
                    )
                )
            
            # Create citations for each responsibility
            citations = []
            for _ in responsibilities:
                citations.append({"page": 1, "section": "Document"})
            
            converted.append({
                "Responsible Party": party,
                "Owner Responsibility": responsibilities,
                "Reasoning": reasonings[:len(responsibilities)],
                "docId": document_name,
                "citations": citations
            })
        
        return converted
    
    def _hybrid_code_llm_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Hybrid approach as suggested by user:
        1. Use code to shortlist responsibilities under same category  
        2. Use LLM for intelligent consolidation within each category
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            self.logger.info(f"Processing {category_name}: {len(obligations)} obligations")
            
            # Step 1: Code-based shortlisting - extract all responsibilities in this category
            category_responsibilities = self._extract_category_responsibilities(obligations, category_name)
            
            if len(category_responsibilities) <= 10:
                # Use conservative programmatic consolidation for smaller/medium sets 
                # to preserve more legal concepts
                programmatic_result = self._conservative_programmatic_consolidation(obligations, category_name)
                consolidated_results.append({
                    "category": category_name,
                    "obligations": programmatic_result
                })
                continue
            
            # Step 2: Use LLM for intelligent consolidation
            try:
                consolidated_obligations = self._llm_consolidate_responsibilities(
                    category_responsibilities, category_name, document_name
                )
                
                if consolidated_obligations:
                    consolidated_results.append({
                        "category": category_name,
                        "obligations": consolidated_obligations
                    })
                    self.logger.info(f"{category_name}: LLM consolidation successful")
                else:
                    # Fallback to original
                    consolidated_results.append(category_group)
                    
            except Exception as e:
                self.logger.error(f"LLM consolidation failed for {category_name}: {e}")
                consolidated_results.append(category_group)
        
        return consolidated_results
    
    def _extract_category_responsibilities(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Code-based step: Extract and shortlist all responsibilities in this category."""
        
        all_responsibilities = []
        
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            responsibilities = obligation.get("Owner Responsibility", [])
            reasonings = obligation.get("Reasoning", [])
            citations = obligation.get("citations", [])
            doc_id = obligation.get("docId", "document.pdf")
            
            # Extract each responsibility as individual item
            for i, responsibility in enumerate(responsibilities):
                reasoning = reasonings[i] if i < len(reasonings) else _auto_reasoning_from_responsibility(responsibility, party=party, category=str(category_name or ""))
                citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                
                all_responsibilities.append({
                    "Responsible Party": party,
                    "Owner Responsibility": responsibility,
                    "Reasoning": reasoning,
                    "Citation": self._format_citation_for_llm(citation),
                    "docId": doc_id
                })
        
        return all_responsibilities
    
    def _llm_consolidate_responsibilities(self, responsibilities: List[Dict[str, Any]], category_name: str, document_name: str) -> List[Dict[str, Any]]:
        """LLM-based step: Intelligent consolidation of responsibilities."""
        
        num_input = len(responsibilities)
        
        # Create LLM consolidation prompt
        consolidation_prompt = f"""You are a legal expert. You have {num_input} financial obligations from the "{category_name}" category.

Your task: Apply intelligent consolidation following these EXACT rules:

CONSOLIDATION RULES:
1. **Exact duplicates only** ΓåÆ Drop one duplicate
2. **Very similar with same purpose** ΓåÆ Merge but keep CONCISE and READABLE
3. **Different legal purposes or rights** ΓåÆ ALWAYS keep separate

CRITICAL PRESERVATION REQUIREMENTS:
- NEVER lose distinct legal rights or obligations
- PRESERVE all unique financial concepts (audit rights, abatement, indemnification, etc.)
- Keep different types of obligations separate (payments vs rights vs remedies)
- When in doubt, keep separate to preserve legal meaning

EXAMPLES:
Γ£à MERGE: "Pay Base Rent monthly" + "Pay Base Rent in advance" ΓåÆ "Pay Base Rent monthly in advance"
Γ£à DROP: "Pay Base Rent" + "Pay monthly Base Rent" ΓåÆ Keep one (identical)  
Γ¥î NEVER MERGE: "Pay expenses" vs "Audit expense records" (different purposes: payment vs rights)
Γ¥î NEVER MERGE: "Pay rent" vs "Rent abatement" (different: obligation vs protection)
Γ¥î NEVER MERGE: "Insurance required" vs "Indemnification" (different legal concepts)

PRESERVATION PRIORITIES:
1. Payment obligations (rent, taxes, insurance, etc.)
2. Legal rights (audit, abatement, offset, termination)
3. Remedies and penalties (default, holdover, deficiency)
4. Indemnification and liability protections
5. Purchase option and closing obligations

MERGING STYLE:
- Only merge truly similar payment obligations
- Keep all legal rights as separate responsibilities
- Preserve all unique legal concepts
- Better to have more responsibilities than lose legal meaning

OUTPUT FORMAT - JSON array where each element has:
- "Responsible Party": exact same string as in the input (pagewise extraction); preserve legal entity names, not generic roles unless the input used only those.
- "Owner Responsibility": array of CONCISE responsibility statements
- "Reasoning": array explaining each responsibility
- "docId": "{document_name}"  
- "citations": array of citation objects

PRIORITY: Clarity and readability over detailed consolidation.

Input responsibilities:
{json.dumps(responsibilities, indent=2)}

Return ONLY the JSON array, no other text."""

        try:
            # Call LLM
            response = llm_generate_content(consolidation_prompt, require_json_object=False)
            
            # Extract response text
            if hasattr(response, 'text'):
                response_text = response.text.strip()
            elif hasattr(response, 'content'):
                response_text = response.content.strip()
            else:
                response_text = str(response).strip()
            
            # Clean response
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Parse JSON
            consolidated_data = json.loads(response_text)
            
            if not isinstance(consolidated_data, list):
                raise ValueError("LLM response is not a JSON array")
            
            if not consolidated_data:
                raise ValueError("LLM returned empty array")
            
            # Convert to final format
            return self._convert_llm_consolidated_response(consolidated_data, document_name)
            
        except Exception as e:
            self.logger.error(f"LLM consolidation error: {e}")
            return []
    
    def _convert_llm_consolidated_response(self, llm_data: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Convert LLM consolidated response to required format."""
        converted = []
        
        for item in llm_data:
            if not isinstance(item, dict):
                continue
            
            party = item.get("Responsible Party", "Unknown")
            responsibilities = item.get("Owner Responsibility", [])
            reasonings = item.get("Reasoning", [])
            
            if not responsibilities:
                continue
            
            # Ensure reasonings match responsibilities count
            while len(reasonings) < len(responsibilities):
                reasonings.append(
                    _auto_reasoning_from_responsibility(
                        responsibilities[len(reasonings)],
                        party=party,
                        category=str(item.get("DutyType") or item.get("category") or ""),
                    )
                )
            
            # Create citations matching responsibilities
            citations = []
            for _ in responsibilities:
                citations.append({"page": 1, "section": "Document"})
            
            converted.append({
                "Responsible Party": party,
                "Owner Responsibility": responsibilities,
                "Reasoning": reasonings[:len(responsibilities)],
                "docId": document_name,
                "citations": citations
            })
        
        return converted
    
    def _conservative_programmatic_consolidation(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Conservative programmatic consolidation that preserves legal concepts."""
        
        # Group by party and merge only very similar obligations
        party_groups = {}
        
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            responsibilities = obligation.get("Owner Responsibility", [])
            reasonings = obligation.get("Reasoning", [])
            citations = obligation.get("citations", [])
            doc_id = obligation.get("docId", "document.pdf")
            
            if party not in party_groups:
                party_groups[party] = []
            
            # Keep each responsibility separate to preserve legal concepts
            for i, responsibility in enumerate(responsibilities):
                reasoning = reasonings[i] if i < len(reasonings) else _auto_reasoning_from_responsibility(responsibility, party=party, category=str(category_name or ""))
                citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                
                party_groups[party].append({
                    "responsibility": responsibility,
                    "reasoning": reasoning,
                    "citation": citation,
                    "docId": doc_id
                })
        
        # Convert back to obligation format with minimal merging
        result = []
        for party, resp_list in party_groups.items():
            result.append({
                "Responsible Party": party,
                "Owner Responsibility": [item["responsibility"] for item in resp_list],
                "Reasoning": [item["reasoning"] for item in resp_list],
                "docId": resp_list[0]["docId"] if resp_list else "document.pdf",
                "citations": [item["citation"] for item in resp_list]
            })
        
        return result
    
    def _llm_consolidation_with_preservation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        LLM consolidation with strict semantic preservation requirements.
        Uses LLM intelligence but with very conservative merging to prevent concept loss.
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            # Step 1: Code-based extraction of responsibilities
            category_responsibilities = self._extract_category_responsibilities(obligations, category_name)
            
            self.logger.info(f"LLM consolidation for {category_name}: {len(category_responsibilities)} responsibilities")
            
            # Step 2: LLM consolidation with strict preservation
            try:
                consolidated_obligations = self._llm_conservative_consolidation(
                    category_responsibilities, category_name, document_name
                )
                
                if consolidated_obligations:
                    consolidated_results.append({
                        "category": category_name,
                        "obligations": consolidated_obligations
                    })
                else:
                    # Fallback to original if LLM fails
                    consolidated_results.append(category_group)
                    
            except Exception as e:
                self.logger.error(f"LLM consolidation failed for {category_name}: {e}")
                consolidated_results.append(category_group)
        
        return consolidated_results
    
    def _llm_conservative_consolidation(self, responsibilities: List[Dict[str, Any]], category_name: str, document_name: str) -> List[Dict[str, Any]]:
        """LLM consolidation with very strict preservation requirements."""
        
        # For large sets, use programmatic to avoid LLM overload
        if len(responsibilities) > 20:
            return self._programmatic_dedup_only(responsibilities, document_name)
        
        num_input = len(responsibilities)
        
        # Simplified, focused LLM prompt
        consolidation_prompt = f"""You are a legal analyst. Consolidate {num_input} financial obligations.

RULES:
1. Remove ONLY exact duplicates (same party + nearly identical text)
2. Keep ALL unique legal obligations separate
3. When merging similar obligations, keep it concise but preserve meaning

NEVER MERGE:
- Different amounts or percentages
- Different parties (Tenant vs Landlord)  
- Different obligation types (payment vs rights vs remedies)
- Payment obligations vs audit/inspection rights
- Regular obligations vs special circumstances (holdover, default, purchase)

OUTPUT: JSON array with "Responsible Party", "Owner Responsibility" (array), "Reasoning" (array), "docId", "citations" (array)
- "Responsible Party" must match the input rows' party strings from pagewise extraction (full names as given); do not normalize to generic "Tenant"/"Landlord" unless the input did.

Input:
{json.dumps(responsibilities[:15], indent=1)}

Return ONLY the JSON array."""

        try:
            # Call LLM
            response = llm_generate_content(consolidation_prompt, require_json_object=False)
            
            # Extract response text
            if hasattr(response, 'text'):
                response_text = response.text.strip()
            elif hasattr(response, 'content'):
                response_text = response.content.strip()
            else:
                response_text = str(response).strip()
            
            # Clean response
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Parse JSON
            consolidated_data = json.loads(response_text)
            
            if not isinstance(consolidated_data, list):
                raise ValueError("LLM response is not a JSON array")
            
            if not consolidated_data:
                raise ValueError("LLM returned empty array")
            
            # Validate and fix character arrays
            consolidated_data = self._fix_character_arrays(consolidated_data)
            
            # Convert to final format
            return self._convert_conservative_llm_response(consolidated_data, document_name)
            
        except Exception as e:
            self.logger.error(f"Conservative LLM consolidation error: {e}")
            return []
    
    def _convert_conservative_llm_response(self, llm_data: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Convert conservative LLM response to required format."""
        converted = []
        
        for item in llm_data:
            if not isinstance(item, dict):
                continue
            
            party = item.get("Responsible Party", "Unknown")
            responsibilities = item.get("Owner Responsibility", [])
            reasonings = item.get("Reasoning", [])
            
            if not responsibilities:
                continue
            
            # Ensure reasoning alignment
            if len(reasonings) != len(responsibilities):
                self.logger.warning(f"Reasoning mismatch: {len(responsibilities)} resp vs {len(reasonings)} reasons")
                # Align arrays by extending reasonings or truncating
                if len(reasonings) < len(responsibilities):
                    # Extend reasonings
                    while len(reasonings) < len(responsibilities):
                        reasonings.append(
                            _auto_reasoning_from_responsibility(
                                responsibilities[len(reasonings)],
                                party=party,
                                category=str(item.get("DutyType") or item.get("category") or ""),
                            )
                        )
                else:
                    # Truncate reasonings
                    reasonings = reasonings[:len(responsibilities)]
            
            # Create citations for each responsibility
            citations = []
            for _ in responsibilities:
                citations.append({"page": 1, "section": "Document"})
            
            converted.append({
                "Responsible Party": party,
                "Owner Responsibility": responsibilities,
                "Reasoning": reasonings,  # Use exactly what LLM provided
                "docId": document_name,
                "citations": citations
            })
        
        return converted
    
    def _programmatic_dedup_only(self, responsibilities: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Programmatic deduplication that only removes exact duplicates."""
        
        unique_responsibilities = {}
        
        for resp_dict in responsibilities:
            party = resp_dict.get("Responsible Party", "Unknown")
            responsibilities_list = resp_dict.get("Owner Responsibility", [])
            reasonings = resp_dict.get("Reasoning", [])
            
            # Create unique key for deduplication
            for i, responsibility in enumerate(responsibilities_list):
                # Clean responsibility for comparison
                clean_resp = ' '.join(responsibility.strip().split())
                
                # Create unique key
                key = f"{party}::{clean_resp.lower()}"
                
                if key not in unique_responsibilities:
                    reasoning = reasonings[i] if i < len(reasonings) else "Not specified"
                    unique_responsibilities[key] = {
                        "party": party,
                        "responsibility": responsibility,
                        "reasoning": reasoning
                    }
        
        # Group by party
        party_groups = {}
        for item in unique_responsibilities.values():
            party = item["party"]
            if party not in party_groups:
                party_groups[party] = {
                    "responsibilities": [],
                    "reasonings": []
                }
            party_groups[party]["responsibilities"].append(item["responsibility"])
            party_groups[party]["reasonings"].append(item["reasoning"])
        
        # Convert to final format
        result = []
        for party, data in party_groups.items():
            citations = [{"page": 1, "section": "Document"} for _ in data["responsibilities"]]
            
            result.append({
                "Responsible Party": party,
                "Owner Responsibility": data["responsibilities"],
                "Reasoning": data["reasonings"],
                "docId": document_name,
                "citations": citations
            })
        
        return result
    
    def _fix_character_arrays(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fix character-by-character arrays that LLM sometimes returns."""
        
        def fix_array(arr):
            """Convert character array to proper string array."""
            if not isinstance(arr, list):
                return arr
            
            # Check if this looks like a character array
            if len(arr) > 10 and all(isinstance(x, str) and len(x) <= 1 for x in arr):
                # Reconstruct as single string, then split properly
                full_text = ''.join(arr)
                # Try to identify sentence boundaries and split appropriately
                sentences = []
                current = ""
                
                for char in full_text:
                    current += char
                    if char in '.;' and len(current.strip()) > 20:
                        sentences.append(current.strip())
                        current = ""
                
                if current.strip():
                    sentences.append(current.strip())
                
                return sentences if sentences else [full_text.strip()]
            
            return arr
        
        fixed_data = []
        for item in data:
            if isinstance(item, dict):
                fixed_item = {}
                for key, value in item.items():
                    if isinstance(value, list):
                        fixed_item[key] = fix_array(value)
                    else:
                        fixed_item[key] = value
                fixed_data.append(fixed_item)
            else:
                fixed_data.append(item)
        
        return fixed_data
    
    def _intelligent_rule_based_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Intelligent consolidation using LLM-inspired rules but programmatic implementation.
        This provides the intelligence of LLM consolidation without the parsing/formatting issues.
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            # Step 1: Extract all responsibilities
            category_responsibilities = self._extract_category_responsibilities(obligations, category_name)
            
            self.logger.info(f"Intelligent consolidation for {category_name}: {len(category_responsibilities)} responsibilities")
            
            # Step 2: Apply LLM-inspired consolidation rules
            consolidated_obligations = self._apply_llm_inspired_consolidation(
                category_responsibilities, category_name, document_name
            )
            
            if consolidated_obligations:
                consolidated_results.append({
                    "category": category_name,
                    "obligations": consolidated_obligations
                })
            else:
                # Fallback to original
                consolidated_results.append(category_group)
        
        return consolidated_results
    
    def _apply_llm_inspired_consolidation(self, responsibilities: List[Dict[str, Any]], category_name: str, document_name: str) -> List[Dict[str, Any]]:
        """Apply LLM-inspired intelligent consolidation rules."""
        
        # Group by responsible party
        party_groups = {}
        for resp_dict in responsibilities:
            party = resp_dict.get("Responsible Party", "Unknown")
            if party not in party_groups:
                party_groups[party] = []
            party_groups[party].extend([
                {
                    "responsibility": resp,
                    "reasoning": resp_dict.get("Reasoning", ["Not specified"] * len(resp_dict.get("Owner Responsibility", [])))[i] if i < len(resp_dict.get("Reasoning", [])) else "Not specified",
                    "original_dict": resp_dict
                }
                for i, resp in enumerate(resp_dict.get("Owner Responsibility", []))
            ])
        
        consolidated_obligations = []
        
        for party, party_responsibilities in party_groups.items():
            # Apply intelligent merging rules
            merged_responsibilities = self._intelligent_merge_responsibilities(party_responsibilities)
            
            if merged_responsibilities:
                consolidated_obligations.append({
                    "Responsible Party": party,
                    "Owner Responsibility": [item["responsibility"] for item in merged_responsibilities],
                    "Reasoning": [item["reasoning"] for item in merged_responsibilities],
                    "docId": document_name,
                    "citations": [{"page": 1, "section": "Document"} for _ in merged_responsibilities]
                })
        
        return consolidated_obligations
    
    def _intelligent_merge_responsibilities(self, responsibilities: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Apply intelligent merging rules inspired by what an LLM would do.
        Rules:
        1. Remove exact duplicates
        2. Merge very similar obligations (e.g., "Pay rent monthly" + "Pay monthly rent")
        3. Keep distinct concepts separate (payments vs rights vs remedies)
        4. Preserve different amounts, timeframes, conditions
        """
        
        if not responsibilities:
            return []
        
        merged = []
        processed_indices = set()
        
        for i, resp1 in enumerate(responsibilities):
            if i in processed_indices:
                continue
                
            # Start with current responsibility
            current_resp = resp1["responsibility"]
            current_reasoning = resp1["reasoning"]
            
            # Look for mergeable responsibilities
            for j, resp2 in enumerate(responsibilities[i+1:], start=i+1):
                if j in processed_indices:
                    continue
                
                similarity = self._calculate_merge_similarity(resp1["responsibility"], resp2["responsibility"])
                
                # Only merge if very similar (>90% similarity) and meet merge criteria
                if similarity > 0.90 and self._can_merge_responsibilities(resp1["responsibility"], resp2["responsibility"]):
                    # Merge the responsibilities
                    current_resp = self._merge_similar_responsibilities(current_resp, resp2["responsibility"])
                    processed_indices.add(j)
            
            merged.append({
                "responsibility": current_resp,
                "reasoning": current_reasoning
            })
            processed_indices.add(i)
        
        return merged
    
    def _calculate_merge_similarity(self, resp1: str, resp2: str) -> float:
        """Calculate similarity for merging decisions (more conservative than semantic similarity)."""
        
        # Normalize for comparison
        norm1 = ' '.join(resp1.lower().strip().split())
        norm2 = ' '.join(resp2.lower().strip().split())
        
        # Exact match
        if norm1 == norm2:
            return 1.0
        
        # Word overlap similarity
        words1 = set(norm1.split())
        words2 = set(norm2.split())
        
        if len(words1) == 0 or len(words2) == 0:
            return 0.0
        
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        
        return intersection / union if union > 0 else 0.0
    
    def _can_merge_responsibilities(self, resp1: str, resp2: str) -> bool:
        """Check if two responsibilities can be safely merged based on LLM-inspired rules."""
        
        # Never merge if different amounts/numbers are mentioned
        if self._contains_different_amounts(resp1, resp2):
            return False
        
        # Never merge if different parties mentioned
        if self._contains_different_parties(resp1, resp2):
            return False
        
        # Never merge payment obligations with rights/remedies
        if self._different_obligation_types(resp1, resp2):
            return False
        
        # Never merge if different timeframes
        if self._contains_different_timeframes(resp1, resp2):
            return False
        
        return True
    
    def _contains_different_amounts(self, resp1: str, resp2: str) -> bool:
        """Check if responsibilities contain different amounts."""
        import re
        
        # Extract dollar amounts and percentages
        amounts1 = re.findall(r'\$[\d,]+|\d+%|\d+\.\d+%', resp1)
        amounts2 = re.findall(r'\$[\d,]+|\d+%|\d+\.\d+%', resp2)
        
        if amounts1 and amounts2 and amounts1 != amounts2:
            return True
        
        return False
    
    def _contains_different_parties(self, resp1: str, resp2: str) -> bool:
        """Check if responsibilities reference different parties."""
        
        parties = ['tenant', 'landlord', 'lessor', 'lessee', 'owner', 'occupant']
        
        parties1 = [p for p in parties if p in resp1.lower()]
        parties2 = [p for p in parties if p in resp2.lower()]
        
        if parties1 and parties2 and parties1 != parties2:
            return True
        
        return False
    
    def _different_obligation_types(self, resp1: str, resp2: str) -> bool:
        """Check if responsibilities are different types (payment vs rights vs remedies)."""
        
        payment_keywords = ['pay', 'reimburse', 'charge', 'cost', 'fee', 'rent', 'expense']
        rights_keywords = ['audit', 'inspect', 'access', 'review', 'examine', 'right']
        remedy_keywords = ['default', 'breach', 'remedy', 'cure', 'notice', 'terminate']
        
        def get_type(resp):
            resp_lower = resp.lower()
            if any(kw in resp_lower for kw in payment_keywords):
                return 'payment'
            elif any(kw in resp_lower for kw in rights_keywords):
                return 'rights'
            elif any(kw in resp_lower for kw in remedy_keywords):
                return 'remedies'
            return 'other'
        
        type1 = get_type(resp1)
        type2 = get_type(resp2)
        
        return type1 != type2 and type1 != 'other' and type2 != 'other'
    
    def _contains_different_timeframes(self, resp1: str, resp2: str) -> bool:
        """Check if responsibilities contain different timeframes."""
        
        timeframes = ['monthly', 'annually', 'quarterly', 'daily', 'weekly', 'advance', 'arrears', 'within 30 days', 'within 60 days', 'immediately']
        
        times1 = [t for t in timeframes if t in resp1.lower()]
        times2 = [t for t in timeframes if t in resp2.lower()]
        
        if times1 and times2 and times1 != times2:
            return True
        
        return False
    
    def _merge_similar_responsibilities(self, resp1: str, resp2: str) -> str:
        """Merge two similar responsibilities into a cleaner version."""
        
        # For very similar responsibilities, keep the more complete one
        if len(resp1) >= len(resp2):
            return resp1
        else:
            return resp2
    
    def _semantic_preservation_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Semantic preservation approach - prioritizes preserving all legal concepts over consolidation.
        Only removes true exact duplicates while maintaining all distinct legal obligations.
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            # Minimal consolidation - preserve all unique legal concepts
            preserved_obligations = self._preserve_all_legal_concepts(obligations, category_name)
            
            consolidated_results.append({
                "category": category_name,
                "obligations": preserved_obligations
            })
            
            self.logger.info(f"{category_name}: Semantic preservation completed")
        
        return consolidated_results
    
    def _preserve_all_legal_concepts(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Preserve all legal concepts - only remove exact text duplicates."""
        
        # Track all unique responsibilities by normalized text
        seen_responsibilities = set()
        preserved_items = []
        
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            responsibilities = obligation.get("Owner Responsibility", [])
            reasonings = obligation.get("Reasoning", [])
            citations = obligation.get("citations", [])
            doc_id = obligation.get("docId", "document.pdf")
            
            for i, responsibility in enumerate(responsibilities):
                # Normalize for duplicate detection (very conservative)
                normalized = responsibility.lower().strip().replace("  ", " ")
                
                # Only skip if EXACTLY the same text with same party
                duplicate_key = f"{party}:{normalized}"
                
                if duplicate_key not in seen_responsibilities:
                    seen_responsibilities.add(duplicate_key)
                    
                    reasoning = reasonings[i] if i < len(reasonings) else _auto_reasoning_from_responsibility(responsibility, party=party, category=str(category_name or ""))
                    citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                    
                    preserved_items.append({
                        "party": party,
                        "responsibility": responsibility,
                        "reasoning": reasoning,
                        "citation": citation,
                        "docId": doc_id
                    })
        
        # Group by party for final format
        party_groups = {}
        for item in preserved_items:
            party = item["party"]
            if party not in party_groups:
                party_groups[party] = []
            party_groups[party].append(item)
        
        # Convert to final obligation format
        result = []
        for party, items in party_groups.items():
            result.append({
                "Responsible Party": party,
                "Owner Responsibility": [item["responsibility"] for item in items],
                "Reasoning": [item["reasoning"] for item in items],
                "docId": items[0]["docId"] if items else "document.pdf",
                "citations": [item["citation"] for item in items]
            })
        
        return result
    
    def _accuracy_focused_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        Accuracy-focused consolidation per user requirements:
        1. Very similar responsibilities ΓåÆ Drop duplicate
        2. Little bit different ΓåÆ Merge but preserve both meanings  
        3. Always maintain accuracy and legal meaning
        """
        consolidated_results = []
        
        for category_group in results:
            if not isinstance(category_group, dict):
                consolidated_results.append(category_group)
                continue
                
            category_name = category_group.get("category", "Unknown")
            obligations = category_group.get("obligations", [])
            
            if not obligations:
                continue
            
            self.logger.info(f"Accuracy consolidation for {category_name}: {len(obligations)} obligations")
            
            # Use programmatic approach with semantic similarity
            consolidated_obligations = self._semantic_consolidation(obligations, category_name)
            
            consolidated_results.append({
                "category": category_name,
                "obligations": consolidated_obligations
            })
            
            self.logger.info(f"{category_name}: Accuracy-focused consolidation completed")
        
        return consolidated_results
    
    def _semantic_consolidation(self, obligations: List[Dict[str, Any]], category_name: str) -> List[Dict[str, Any]]:
        """Programmatic consolidation based on semantic similarity and user requirements."""
        
        # Group by responsible party first
        party_groups = {}
        
        for obligation in obligations:
            party = obligation.get("Responsible Party", "Unknown")
            responsibilities = obligation.get("Owner Responsibility", [])
            reasonings = obligation.get("Reasoning", [])
            citations = obligation.get("citations", [])
            doc_id = obligation.get("docId", "document.pdf")
            
            if party not in party_groups:
                party_groups[party] = []
            
            # Add each responsibility as individual item for processing
            for i, responsibility in enumerate(responsibilities):
                reasoning = (
                    reasonings[i]
                    if i < len(reasonings)
                    else _auto_reasoning_from_responsibility(
                        responsibility, party=party, category=str(category_name or "")
                    )
                )
                citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                
                party_groups[party].append({
                    "responsibility": responsibility,
                    "reasoning": reasoning,
                    "citation": citation,
                    "docId": doc_id
                })
        
        # Process each party's responsibilities
        consolidated_obligations = []
        
        for party, resp_list in party_groups.items():
            # Apply user's consolidation rules
            consolidated_resp_list = self._apply_user_consolidation_rules(resp_list)
            
            if consolidated_resp_list:
                # Group into final obligation format
                final_obligation = {
                    "Responsible Party": party,
                    "Owner Responsibility": [item["responsibility"] for item in consolidated_resp_list],
                    "Reasoning": [item["reasoning"] for item in consolidated_resp_list],
                    "docId": consolidated_resp_list[0]["docId"],
                    "citations": [item["citation"] for item in consolidated_resp_list]
                }
                consolidated_obligations.append(final_obligation)
        
        return consolidated_obligations
    
    def _apply_user_consolidation_rules(self, resp_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Apply user's specific rules:
        1. Very similar ΓåÆ Drop duplicate
        2. Little bit different ΓåÆ Merge but preserve meanings
        3. Always maintain accuracy
        """
        consolidated = []
        processed_indices = set()
        
        for i, item in enumerate(resp_list):
            if i in processed_indices:
                continue
                
            responsibility = item["responsibility"]
            
            # Look for similar responsibilities to merge or drop
            merged_item = self._find_and_merge_similar(item, resp_list, i, processed_indices)
            consolidated.append(merged_item)
        
        return consolidated
    
    def _find_and_merge_similar(self, base_item: Dict[str, Any], resp_list: List[Dict[str, Any]], 
                               base_index: int, processed_indices: set) -> Dict[str, Any]:
        """Find similar responsibilities and apply user rules."""
        
        base_resp = base_item["responsibility"].lower().strip()
        merged_responsibility = base_item["responsibility"]
        
        # Look for similar items to merge
        for j, other_item in enumerate(resp_list):
            if j <= base_index or j in processed_indices:
                continue
                
            other_resp = other_item["responsibility"].lower().strip()
            
            # Check similarity
            similarity = self._calculate_semantic_similarity(base_resp, other_resp)
            
            if similarity > 0.97:  # True duplicate (only punctuation/whitespace difference) ΓåÆ Drop
                processed_indices.add(j)
                continue

            elif similarity > 0.92:  # Near-identical phrasing of the same duty ΓåÆ Merge
                merged_responsibility = self._merge_preserving_meanings(
                    merged_responsibility, other_item["responsibility"]
                )
                processed_indices.add(j)
            # else: similarity <= 0.92 ΓåÆ keep as separate obligation (DO NOT MERGE)
        
        return {
            "responsibility": merged_responsibility,
            "reasoning": base_item["reasoning"],
            "citation": base_item["citation"],
            "docId": base_item["docId"]
        }
    
    def _calculate_semantic_similarity(self, text1: str, text2: str) -> float:
        """
        Jaccard word overlap only (no subject-based boosting ΓÇö that caused false merges).
        Programmatic merge uses 0.97 (drop duplicate) and 0.92 (merge near-identical) thresholds.
        """

        def word_set(t: str) -> set:
            return {w for w in re.sub(r"[^\w\s]", " ", (t or "").lower()).split() if w}

        words1 = word_set(text1)
        words2 = word_set(text2)
        if not words1 or not words2:
            return 0.0
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        return intersection / union if union > 0 else 0.0
    
    def _merge_preserving_meanings(self, resp1: str, resp2: str) -> str:
        """Merge two responsibilities while preserving both meanings."""
        resp1_lower = resp1.lower()
        resp2_lower = resp2.lower()
        
        # Smart merging for base rent obligations
        if "base rent" in resp1_lower and "base rent" in resp2_lower:
            # Collect all payment terms and details
            terms = []
            
            # Extract amount if specified
            if "$" in resp1 or "$" in resp2:
                if "$_____" in resp1:
                    amount = "$_____ per month"
                elif "$[BLANK]" in resp1:
                    amount = "$[BLANK] per month"
                elif "$" in resp1:
                    amount = "specified amount"
                else:
                    amount = "per month"
                terms.append(amount)
            
            # Extract payment frequency and timing
            if "monthly" in resp1_lower or "monthly" in resp2_lower or "month" in resp1_lower or "month" in resp2_lower:
                if "advance" in resp1_lower or "advance" in resp2_lower:
                    terms.append("in advance")
                if "installment" in resp1_lower or "installment" in resp2_lower:
                    terms.append("in equal monthly installments")
                if "first day" in resp1_lower or "first day" in resp2_lower:
                    terms.append("on or before the first day of each calendar month")
            
            # Create merged responsibility
            if terms:
                unique_terms = []
                seen = set()
                for term in terms:
                    if term.lower() not in seen:
                        unique_terms.append(term)
                        seen.add(term.lower())
                return f"Pay Base Rent {' '.join(unique_terms)}"
            else:
                return f"Pay Base Rent as specified in lease terms"
        
        # Smart merging for tax obligations
        elif "tax" in resp1_lower and "tax" in resp2_lower:
            if "personal property" in resp1_lower or "personal property" in resp2_lower:
                if "real estate" in resp1_lower or "real estate" in resp2_lower:
                    return "Pay all Real Estate Taxes and personal property taxes as they become due"
            # Safe fallback for tax merging
            return f"Pay all applicable taxes as required by lease terms"
        
        # Check if one is more comprehensive
        if len(resp2) > len(resp1) * 1.3 and resp1.lower().replace("pay ", "").strip() in resp2_lower:
            return resp2
        elif len(resp1) > len(resp2) * 1.3 and resp2.lower().replace("pay ", "").strip() in resp1_lower:
            return resp1
        
        # Default: combine both meanings clearly
        return f"{resp1.strip()} and {resp2.strip()}"
    
    def _llm_based_consolidation(self, results: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """
        LLM-based consolidation as requested by user.
        
        Step 1: Initial Filtering (Deterministic) - Group by category
        Step 2: LLM-Based Consolidation - Smart deduplication within each category
        """
        consolidated_results = []
        
        # Step 1: Deterministic filtering - group by category
        category_groups = {}
        for result in results:
            category = result.get("category", "Unknown")
            if category not in category_groups:
                category_groups[category] = []
            
            # Flatten obligations into individual items for LLM processing
            for obligation in result.get("obligations", []):
                party = obligation.get("Responsible Party", "Unknown")
                responsibilities = obligation.get("Owner Responsibility", [])
                reasonings = obligation.get("Reasoning", [])
                citations = obligation.get("citations", [])
                doc_id = obligation.get("docId", document_name)
                
                # Create flat obligation entries
                for i, responsibility in enumerate(responsibilities):
                    reasoning = (
                        reasonings[i]
                        if i < len(reasonings)
                        else _auto_reasoning_from_responsibility(
                            responsibility, party=party, category=str(category or "")
                        )
                    )
                    citation = citations[i] if i < len(citations) else {"page": 1, "section": "Document"}
                    
                    category_groups[category].append({
                        "DutyType": category,
                        "Responsible Party": party,
                        "Owner Responsibility": [responsibility],
                        "Reasoning": [reasoning],
                        "Citation": self._format_citation_for_llm(citation),
                        "docId": doc_id
                    })
        
        # Step 2: LLM consolidation for each category
        for category, flat_obligations in category_groups.items():
            if not flat_obligations:
                continue
                
            self.logger.info(f"LLM consolidating {len(flat_obligations)} obligations in {category}")
            
            try:
                # Use LLM to consolidate this category
                consolidated_category_obligations = self._llm_consolidate_category(
                    flat_obligations, category, document_name
                )
                
                if consolidated_category_obligations:
                    consolidated_results.append({
                        "category": category,
                        "obligations": consolidated_category_obligations
                    })
                    self.logger.info(f"{category}: {len(flat_obligations)} -> {len(consolidated_category_obligations)} obligations")
                
            except Exception as e:
                self.logger.error(f"LLM consolidation failed for {category}: {e}")
                # Fallback to original data
                original_category_data = next((r for r in results if r.get("category") == category), None)
                if original_category_data:
                    consolidated_results.append(original_category_data)
        
        return consolidated_results
    
    def _llm_consolidate_category(self, flat_obligations: List[Dict[str, Any]], category: str, document_name: str) -> List[Dict[str, Any]]:
        """Use LLM to consolidate obligations within a single category."""
        
        if len(flat_obligations) <= 8:
            # Skip LLM for smaller sets to avoid over-consolidation, just convert format  
            return self._convert_flat_to_consolidated_format(flat_obligations, document_name)
        
        # Create consolidation prompt following user's specifications
        num_input = len(flat_obligations)
        
        # Extract parties for metadata
        parties = list(set(obj.get("Responsible Party", "") for obj in flat_obligations))
        party_info = ", ".join(parties) if parties else "Various parties"
        
        metadata_context = f"""Document: {document_name}
Category: {category}
Parties: {party_info}"""

        consolidation_prompt = f"""You are a legal analyst. You have been provided with {num_input} financial obligations extracted from multiple pages of a legal document.

Your task is to consolidate these into a single, deduplicated JSON array. The consolidated JSON must be produced directly from this input.

CRITICAL: You MUST output a non-empty JSON array. The input contains {num_input} obligations; your output must be a deduplicated/merged array of those obligations (fewer items after merging duplicates). Never return an empty array [].

Rules:
1. ONLY merge obligations that express the exact same legal duty with minor wording differences (e.g. "pay base rent monthly" and "pay base rent in equal monthly installments" are the same duty). NEVER merge obligations about different topics ΓÇö rent payment, real estate taxes, operating expenses, insurance, alterations, holdover, and broker commissions are ALWAYS separate items even if the same party. When in doubt, keep as separate items.
2. Preserve all unique obligations.
3. Each element must have: "Responsible Party", "Owner Responsibility" (array of strings), "Reasoning" (array of strings), "Citation" (string), "docId", and "related_keywords" (array of strings).
4. DutyType: short, precise label (e.g. Rent Payment, Security Deposit, Property Tax Payment).
5. Responsible Party: Copy **exactly** from each input obligation's `Responsible Party` (pagewise extraction): use the actual legal entity or contract label as shown in the JSON below. Do not replace with generic "Tenant" or "Landlord" unless the input already used only those words.
6. When merging, combine citations (e.g., "Page 3, Section A; Page 7, Section B").
7. related_keywords: for each obligation, add an array of 8ΓÇô20 search keywords/phrases. You MUST include the DutyType itself (and normalised variations, e.g. "Rent Payment" ΓåÆ "rent payment", "rent") in this array. Add synonyms and related concepts so semantic search can find this obligation. Example: for DutyType "Property Insurance", related_keywords must include "Property Insurance" or "property insurance", plus e.g. ["insurance", "property insurance", "liability", "coverage", "premium", "tenant insurance"].
8. Maintain legal accuracy. Output ONLY a valid JSON array, no other text.

{metadata_context}

Extracted obligations from the document:
{json.dumps(flat_obligations, indent=2)}

Output ONLY a valid JSON array of consolidated obligations, no other text."""

        try:
            # Call LLM for consolidation
            response = llm_generate_content(consolidation_prompt, require_json_object=False)
            
            # Extract text from response
            if hasattr(response, 'text'):
                response_text = response.text.strip()
            elif hasattr(response, 'content'):
                response_text = response.content.strip()
            else:
                response_text = str(response).strip()
            
            # Clean response
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()
            
            # Parse JSON
            consolidated_data = json.loads(response_text)
            
            if not isinstance(consolidated_data, list) or not consolidated_data:
                raise ValueError("Invalid LLM response format")
            
            # Convert to required format and validate
            return self._convert_llm_response_to_format(consolidated_data, document_name)
            
        except Exception as e:
            self.logger.error(f"LLM consolidation error: {e}")
            # Fallback to simple conversion
            return self._convert_flat_to_consolidated_format(flat_obligations, document_name)
    
    def _convert_llm_response_to_format(self, llm_data: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Convert LLM response to required obligation format."""
        converted = []
        
        for item in llm_data:
            if not isinstance(item, dict):
                continue
            
            # Ensure required fields
            party = item.get("Responsible Party", "Unknown")
            responsibilities = item.get("Owner Responsibility", [])
            reasonings = item.get("Reasoning", [])
            citation_str = item.get("Citation", "Page 1, Document")
            
            if not responsibilities:
                continue
            
            # Convert citation string back to citation objects
            citations = self._parse_citation_string_to_objects(citation_str, len(responsibilities))
            
            # Ensure arrays match length, but respect LLM's reasoning decisions
            # If LLM provided fewer reasonings, it might be intentional (e.g., "Not specified")
            if len(reasonings) != len(responsibilities):
                # Log the mismatch but don't auto-fix - this indicates LLM consolidation issue
                self.logger.warning(f"Array length mismatch: {len(responsibilities)} responsibilities vs {len(reasonings)} reasonings")
                
                # Only truncate if we have MORE reasonings than responsibilities
                if len(reasonings) > len(responsibilities):
                    reasonings = reasonings[:len(responsibilities)]
                # If fewer reasonings, leave as-is - let downstream handle the mismatch
            
            # Ensure citations match responsibilities count
            while len(citations) < len(responsibilities):
                citations.append({"page": 1, "section": "Document"})
            
            converted.append({
                "Responsible Party": party,
                "Owner Responsibility": responsibilities,
                "Reasoning": reasonings,
                "docId": document_name,
                "citations": citations
            })
        
        return converted
    
    def _convert_flat_to_consolidated_format(self, flat_obligations: List[Dict[str, Any]], document_name: str) -> List[Dict[str, Any]]:
        """Convert flat obligations to consolidated format without LLM processing."""
        party_groups = {}
        
        for obligation in flat_obligations:
            party = obligation.get("Responsible Party", "Unknown")
            if party not in party_groups:
                party_groups[party] = {
                    "responsibilities": [],
                    "reasonings": [],
                    "citations": []
                }
            
            # Add to party group
            resp = obligation.get("Owner Responsibility", [])
            reason = obligation.get("Reasoning", [])
            citation_str = obligation.get("Citation", "Page 1, Document")
            
            if resp:
                party_groups[party]["responsibilities"].extend(resp)
                party_groups[party]["reasonings"].extend(reason)
                party_groups[party]["citations"].append(self._parse_citation_string(citation_str))
        
        # Convert to final format
        result = []
        for party, data in party_groups.items():
            if data["responsibilities"]:
                result.append({
                    "Responsible Party": party,
                    "Owner Responsibility": data["responsibilities"],
                    "Reasoning": data["reasonings"],
                    "docId": document_name,
                    "citations": data["citations"]
                })
        
        return result
    
    def _format_citation_for_llm(self, citation: Dict[str, Any]) -> str:
        """Format citation object as string for LLM input."""
        if isinstance(citation, dict):
            page = citation.get("page", 1)
            section = citation.get("section", "Document")
            return f"Page {page}, Section {section}"
        return str(citation)
    
    def _parse_citation_string_to_objects(self, citation_str: str, count: int) -> List[Dict[str, Any]]:
        """Parse citation string back to objects for multiple responsibilities."""
        citations = []
        
        # Try to extract multiple citations from combined string
        parts = citation_str.split(";")
        
        for i in range(count):
            if i < len(parts):
                citation_part = parts[i].strip()
                citations.append(self._parse_citation_string(citation_part))
            else:
                # Use the first citation as fallback
                if citations:
                    citations.append(citations[0])
                else:
                    citations.append({"page": 1, "section": "Document"})
        
        return citations


class LegalDocumentProcessor:
    """Legal document processing: local docs folder -> output folder. Uses Azure OpenAI or Gemini via llm_client."""

    def __init__(self,
                 local_docs_folder: Optional[str] = None,
                 local_output_folder: Optional[str] = None,
                 logs_folder: str = "logs",
                 cache_folder: str = "ocr_cache",
                 prompt_file: str = "prompt.txt",
                 model: Optional[str] = None,
                 tesseract_cmd: Optional[str] = None,
                 poppler_path: Optional[str] = None):
        """Initialize with local docs and output folders. Uses get_default_model() when model is None."""
        self.logs_folder = Path(logs_folder)
        self.cache_folder = Path(cache_folder)
        self.logs_folder.mkdir(exist_ok=True)
        self.cache_folder.mkdir(exist_ok=True)
        self._setup_logging()
        self.logger = logging.getLogger(__name__)
        self.local_docs_folder = str(_resolve_workspace_path(local_docs_folder, "DOCS_FOLDER", "docs"))
        self.local_output_folder = str(_resolve_workspace_path(local_output_folder, "OUTPUT_FOLDER", "output"))
        Path(self.local_output_folder).mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Legal Document Processor (local): docs={self.local_docs_folder}, output={self.local_output_folder}")
        self.pdf_processor = PDFProcessor(tesseract_cmd, str(self.cache_folder), poppler_path)
        self.gemini_analyzer = GeminiAnalyzer(prompt_file, model or get_default_model())
    
    def _setup_logging(self):
        """Setup logging configuration - force console and file output"""
        import sys
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.logs_folder / f"processing_{timestamp}.log"

        # Get the root logger and force complete reconfiguration
        root_logger = logging.getLogger()
        
        # Clear ALL existing handlers to avoid conflicts
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            handler.close()
        
        # Set up our custom logging with forced stdout
        root_logger.setLevel(logging.INFO)
        
        # Create formatters
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        # File handler for logs
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        
        # Console handler for terminal output - force to sys.stdout
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        
        # Add both handlers to root logger
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        # Also configure our specific logger
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = True  # Ensure it propagates to root logger
        
        # Force flush stdout to ensure messages appear immediately
        sys.stdout.flush()
        
        # Force a test message to verify logging works
        test_message = f"[SETUP] LOGGING CONFIGURED: Terminal + File ({log_file.name})"
        self.logger.info(test_message)
        print(test_message, flush=True)  # Direct print as backup with flush
        sys.stdout.flush()
    
    def get_first_pdf(self) -> Optional[str]:
        """Return path to first PDF in docs folder."""
        root = Path(self.local_docs_folder)
        if not root.exists():
            self.logger.warning(f"Docs folder does not exist: {root}")
            return None
        pdfs = sorted(root.glob("*.pdf"), key=lambda p: p.name)
        if not pdfs:
            self.logger.warning(f"No PDF files in {root}")
            return None
        return str(pdfs[0].resolve())

    def get_all_pdfs(self) -> List[str]:
        """Return paths to all PDFs in docs folder."""
        root = Path(self.local_docs_folder)
        if not root.exists():
            return []
        pdfs = sorted(root.glob("*.pdf"), key=lambda p: p.name)
        self.logger.info(f"Found {len(pdfs)} PDFs in {root}")
        return [str(p.resolve()) for p in pdfs]

    def process_document(self, pdf_path: str) -> Optional[str]:
        """Process a single PDF from local path. Returns path to consolidated JSON or None."""
        try:
            doc_name = Path(pdf_path).name
            doc_stem = doc_name.rsplit('.', 1)[0]
            message_start = f"\n{'='*80}\n≡ƒÜÇ PROCESSING DOCUMENT: {doc_name}\n≡ƒôü From: {pdf_path}\n{'='*80}"
            self.logger.info(message_start)
            force_logger.info(message_start)  # Force terminal output
            self.gemini_analyzer.reset_party_metadata()
            page_texts = self.pdf_processor.process_pdf(str(pdf_path))
            if not page_texts:
                self.logger.error("No text extracted from PDF")
                return None

            sorted_pages = sorted(page_texts.keys())
            page_delay = float(os.getenv("PAGE_PROCESSING_DELAY", "3.5"))
            use_sections = (os.getenv("LEGAL_OCR_USE_SECTION_EXTRACTION", "").lower() in ("1", "true", "yes"))
            try:
                n_party_meta_pages = int(os.getenv("LEGAL_OCR_PARTY_METADATA_PAGES", "5"))
            except ValueError:
                n_party_meta_pages = 5
            n_party_meta_pages = max(1, min(50, n_party_meta_pages))
            all_category_groups: List[Dict[str, Any]] = []
            sections: List[Dict[str, Any]] = []
            section_results: Dict[str, Any] = {}

            if use_sections:
                self.logger.info(
                    "Obligation extraction: section-based (set LEGAL_OCR_USE_SECTION_EXTRACTION=false to use page-wise)"
                )
                full_text = "\n\n".join(page_texts[p] for p in sorted_pages)
                structure = self.pdf_processor.extract_document_structure(full_text)
                sections_tree = structure.get("sections", [])
                sections = self.pdf_processor.flatten_sections_for_processing(sections_tree)
                total_sections = len(sections)
                self.logger.info(
                    f"Extracted {total_sections} sections (flattened); running obligation extraction per section"
                )
                for i, sec in enumerate(sections):
                    section_index = str(i + 1)
                    sec_num = (sec.get("section_number") or "").strip() or section_index
                    sec_title = (sec.get("section_title") or "").strip()
                    content = (sec.get("content") or "").strip()
                    title_snippet = (sec_title[:50] + "ΓÇª") if len(sec_title) > 50 else sec_title
                    self.logger.info(
                        f"--- Section [{i + 1}/{total_sections}]: {sec_num} ΓÇö {title_snippet or '(no title)'} ---"
                    )
                    if not content:
                        self.logger.info(f"  Skipping section {sec_num} (no content)")
                        section_results[section_index] = {
                            "section_number": sec_num,
                            "section_title": sec_title,
                            "category_groups": [],
                        }
                        continue
                    extract_parties = i == 0
                    groups = self.gemini_analyzer.analyze_section(
                        sec_num,
                        sec_title,
                        content,
                        extract_parties=extract_parties,
                        document_name=doc_name,
                        party_metadata_max_pages=n_party_meta_pages,
                    )
                    n_in = _count_inner_obligation_rows(groups)
                    self.logger.info(
                        "  Section %s: %d category group(s), %d obligation row(s)",
                        sec_num,
                        len(groups),
                        n_in,
                    )
                    section_results[section_index] = {
                        "section_number": sec_num,
                        "section_title": sec_title,
                        "category_groups": groups,
                    }
                    all_category_groups.extend(groups)
                    if i < len(sections) - 1:
                        time.sleep(page_delay)
            else:
                self.logger.info(
                    "Obligation extraction: page-wise (default). Set LEGAL_OCR_USE_SECTION_EXTRACTION=true for section-based."
                )
                self.logger.info(
                    f"Party metadata extraction on first {n_party_meta_pages} non-empty pages "
                    f"(override with LEGAL_OCR_PARTY_METADATA_PAGES); first batch merged via LLM when full."
                )
                page_results: Dict[str, List[Dict[str, Any]]] = {}
                n_pages = len(sorted_pages)
                pages_party_scanned = 0
                for idx, page_num in enumerate(sorted_pages):
                    text = page_texts[page_num] or ""
                    self.logger.info(f"--- Page [{idx + 1}/{n_pages}]: page {page_num} ---")
                    if not str(text).strip():
                        self.logger.info(f"  Skipping page {page_num} (no text)")
                        page_results[str(page_num)] = []
                        continue
                    # Section-based mode often sees parties in a long first "section"; page 1 alone is often cover.
                    # Scan party definitions on the first N pages (default 5), same intent as analyze_page docstring.
                    extract_parties = pages_party_scanned < n_party_meta_pages
                    groups = self.gemini_analyzer.analyze_page(
                        page_num,
                        text,
                        extract_parties=extract_parties,
                        document_name=doc_name,
                        party_metadata_max_pages=n_party_meta_pages,
                    )
                    page_results[str(page_num)] = [dict(g) for g in groups if isinstance(g, dict)]
                    all_category_groups.extend(page_results[str(page_num)])
                    self.logger.info(
                        "  Page %s: %d category group(s), %d obligation row(s)",
                        page_num,
                        len(groups),
                        _count_inner_obligation_rows(groups),
                    )
                    if extract_parties:
                        pages_party_scanned += 1
                    if idx < n_pages - 1:
                        time.sleep(page_delay)

            self.gemini_analyzer.finalize_party_metadata_if_needed()

            bundle = self.gemini_analyzer.consolidate_results_to_json(all_category_groups, document_name=doc_name)
            processing_results = bundle.get("results") or []
            processing_citations = bundle.get("citations") or []
            flat_for_vector_index = bundle.get("flat_for_vector") or []
            consolidated_ob_count = int(bundle.get("consolidated_obligations_count") or 0)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = Path(self.local_output_folder)
            out_dir.mkdir(parents=True, exist_ok=True)
            consolidated_json_path = out_dir / f"{doc_stem}_{timestamp}_consolidated.json"

            pagewise_path_str: Optional[str] = None
            if use_sections:
                section_based_data = {
                    "document_name": doc_name,
                    "processed_at": datetime.now().isoformat(),
                    "total_sections": len(sections),
                    "total_obligations_found": _count_inner_obligation_rows(all_category_groups),
                    "party_metadata": self.gemini_analyzer.party_metadata,
                    "section_results": section_results,
                }
                section_based_path = out_dir / f"{doc_stem}_{timestamp}_section_based.json"
                with open(section_based_path, "w", encoding="utf-8") as f:
                    json.dump(section_based_data, f, indent=2, ensure_ascii=False)
                self.logger.info(f"Section-based results saved to: {section_based_path}")
            else:
                pagewise_data = {
                    "document_name": doc_name,
                    "processed_at": datetime.now().isoformat(),
                    "total_pages": len(page_texts),
                    "total_obligations_found": _count_inner_obligation_rows(all_category_groups),
                    "party_metadata": self.gemini_analyzer.party_metadata,
                    "page_results": page_results,
                }
                pagewise_path = out_dir / f"{doc_stem}_{timestamp}_pagewise.json"
                pagewise_path_str = str(pagewise_path.resolve())
                with open(pagewise_path, "w", encoding="utf-8") as f:
                    json.dump(pagewise_data, f, indent=2, ensure_ascii=False)
                self.logger.info(f"Pagewise results saved to: {pagewise_path}")

            consolidated_data: Dict[str, Any] = {
                "document_name": doc_name,
                "processed_at": datetime.now().isoformat(),
                "total_pages": len(page_texts),
                "total_obligations_found": _count_inner_obligation_rows(all_category_groups),
                "consolidated_obligations_count": consolidated_ob_count,
                "party_metadata": self.gemini_analyzer.party_metadata,
                "results": processing_results,
                "citations": processing_citations,
            }
            with open(consolidated_json_path, "w", encoding="utf-8") as f:
                json.dump(consolidated_data, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Consolidated JSON saved to: {consolidated_json_path}")
            
            # Apply citation post-processing to fix any citations that failed during LLM consolidation
            try:
                from citation_post_processor import apply_citation_post_processing
                self.logger.info("Applying citation post-processing...")
                success, stats = apply_citation_post_processing(
                    str(out_dir),
                    doc_name,
                    self.logger
                )
                if success:
                    self.logger.info(f"Citation post-processing: {stats['citations_fixed']}/{stats['total_responsibilities']} fixed ({stats['success_rate']:.1f}%)")
                else:
                    self.logger.warning(f"Citation post-processing skipped")
            except ImportError:
                self.logger.info("Citation post-processor not available; skipping citation fixing")
            except Exception as e:
                self.logger.warning(f"Citation post-processing error (non-fatal): {e}")

            # Final output formatting: obligation-level combined citations (comma-separated),
            # derived from pagewise source-of-truth (preferred) or from existing citations as fallback.
            try:
                with open(consolidated_json_path, "r", encoding="utf-8") as f:
                    _final = json.load(f)
                doc_fallback = str(_final.get("document_name") or doc_name or "").strip()

                # Preferred: compute combined citations from pagewise by matching each consolidated
                # responsibility to pagewise responsibilities (ensures pageNumbers are present).
                pagewise_index = None
                party_meta_cite: Optional[Dict[str, Any]] = None
                if pagewise_path_str and not use_sections:
                    try:
                        from citation_post_processor import (
                            extract_pagewise_responsibility_citations,
                            find_contributing_citations,
                            normalize_text as _norm_text,
                        )
                        with open(pagewise_path_str, "r", encoding="utf-8") as pf:
                            _pw = json.load(pf)
                        pm = _pw.get("party_metadata")
                        if isinstance(pm, dict):
                            party_meta_cite = pm
                        pagewise_index = extract_pagewise_responsibility_citations(_pw)
                    except Exception as e:
                        self.logger.warning(f"Could not build pagewise citation index: {e}")

                for grp in _final.get("results") or []:
                    if not isinstance(grp, dict):
                        continue
                    cat = str(grp.get("category") or "Unknown").strip()
                    for ob in grp.get("obligations") or []:
                        if not isinstance(ob, dict):
                            continue
                        _sanitize_reasonings_in_obligation(ob, category=cat)
                        fb = str(ob.get("docId") or doc_fallback).strip()

                        # Try pagewise-driven combined citation first.
                        combined = []
                        if pagewise_index is not None:
                            try:
                                from processing_results import (
                                    normalize_responsible_party_for_obligation,
                                )

                                pm_use = party_meta_cite
                                if not isinstance(pm_use, dict):
                                    pm_use = _final.get("party_metadata")
                                if not isinstance(pm_use, dict):
                                    pm_use = None
                                party_raw = str(ob.get("Responsible Party") or "").strip()
                                party_norms: List[str] = []
                                if party_raw:
                                    party_norms.append(_norm_text(party_raw))
                                party_canon = normalize_responsible_party_for_obligation(
                                    ob, party_metadata=pm_use
                                )
                                cn = _norm_text(
                                    str(party_canon or "").strip() or "Unknown"
                                )
                                if cn not in party_norms:
                                    party_norms.append(cn)
                                if not party_norms:
                                    party_norms.append(_norm_text("Unknown"))

                                cat_norm = _norm_text(cat)
                                refs: List[Dict[str, Any]] = []
                                for r in ob.get("Owner Responsibility") or []:
                                    cites: List[Dict[str, Any]] = []
                                    for pn in party_norms:
                                        cites = find_contributing_citations(
                                            str(r or "").strip(),
                                            pn,
                                            cat_norm,
                                            pagewise_index,
                                        )
                                        if cites:
                                            break
                                    if cites:
                                        refs.extend(cites)
                                # Convert refs -> compact per-doc combined format.
                                combined = combine_citations_to_comma_separated(
                                    [{"docId": fb, "references": refs}],
                                    fallback_doc_id=fb,
                                )
                            except Exception as e:
                                self.logger.warning(f"Pagewise citation combine failed for {fb}: {e}")

                        # Fallback: combine whatever is already attached.
                        if not combined:
                            raw_cit = ob.get("citations") if ob.get("citations") is not None else ob.get("Citation")
                            combined = combine_citations_to_comma_separated(raw_cit, fallback_doc_id=fb)

                        if combined:
                            ob["Citation"] = combined
                        ob.pop("citations", None)
                        ob.pop("docId", None)
                # Root citations are not needed when each obligation carries its own combined Citation.
                _final["citations"] = []
                with open(consolidated_json_path, "w", encoding="utf-8") as f:
                    json.dump(_final, f, indent=2, ensure_ascii=False)
                self.logger.info("Applied combined obligation Citation format to consolidated JSON")
            except Exception as e:
                self.logger.warning(f"Final citation formatting step failed (non-fatal): {e}")
            
            # NEW: Enhanced individual obligation indexing with auto-generated keywords
            # This provides fine-grained retrieval at the obligation level instead of category level
            try:
                from obligation_keywords import (
                    flatten_obligations_for_individual_indexing,
                    process_consolidated_results_with_auto_keywords
                )
                from vector_store import index_individual_obligations
                
                chroma_path = str(out_dir / "chroma_db")
                category_results = _final.get("results", [])
                
                if category_results:
                    self.logger.info("≡ƒöä Processing obligations with auto-generated keywords...")
                    enhanced_results = process_consolidated_results_with_auto_keywords(category_results)
                    
                    # Flatten to individual obligations
                    individual_obligations = flatten_obligations_for_individual_indexing(enhanced_results)
                    self.logger.info(f"≡ƒôï Flattened to {len(individual_obligations)} individual searchable obligations")
                    
                    # Index individual obligations (this is the main search index now)
                    num_individual_indexed = index_individual_obligations(
                        document_name=doc_name,
                        individual_obligations=individual_obligations,
                        chroma_path=chroma_path,
                    )
                    self.logger.info(f"≡ƒÄ» Individual obligation index: {num_individual_indexed} obligations with auto-generated keywords indexed")
                    
                    # Also keep legacy indexing for backward compatibility (can be removed later)
                    try:
                        from vector_store import index_obligations, index_categories
                        chroma_path_legacy = str(out_dir / "chroma_db_legacy")
                        
                        # Legacy obligation indexing
                        num_legacy_indexed = index_obligations(
                            document_name=doc_name,
                            consolidated_results=flat_for_vector_index,
                            chroma_path=chroma_path_legacy,
                        )
                        self.logger.info(f"≡ƒôª Legacy index: {num_legacy_indexed} chunks (old approach) at {chroma_path_legacy}")
                        
                        # Legacy category indexing
                        num_cat_indexed = index_categories(
                            document_name=doc_name,
                            category_results=category_results,
                            chroma_path=chroma_path_legacy,
                        )
                        self.logger.info(f"≡ƒôé Legacy category index: {num_cat_indexed} categories at {chroma_path_legacy}")
                        
                    except Exception as legacy_e:
                        self.logger.warning(f"Legacy indexing failed (non-critical): {legacy_e}")
                else:
                    self.logger.warning("No category results found for indexing")
                
            except ImportError:
                self.logger.warning("Enhanced indexing not available, falling back to legacy approach")
                # Fallback to original indexing
                self._fallback_to_legacy_indexing(doc_name, flat_for_vector_index, consolidated_json_data, out_dir)
            except Exception as e:
                self.logger.error(f"Enhanced indexing failed: {e}")
                # Fallback to original indexing
                self._fallback_to_legacy_indexing(doc_name, flat_for_vector_index, _final, out_dir)

            # Optional: dual-track RAG index in Qdrant (pages + obligations) for POST /chat
            if os.getenv("RAG_INDEX_QDRANT", "").lower() in ("true", "1", "yes"):
                try:
                    from legal_rag.qdrant_store import delete_document, upsert_document_pages_and_obligations

                    delete_document(doc_name)
                    page_map = {int(k): v for k, v in page_texts.items()}
                    stats = upsert_document_pages_and_obligations(
                        document_id=doc_name,
                        page_texts=page_map,
                        consolidated_obligations=flat_for_vector_index,
                        page_for_obligation=_obligation_pages_for_rag(flat_for_vector_index),
                    )
                    self.logger.info(
                        "Qdrant RAG index updated for %s: %s (set RAG_INDEX_QDRANT=false to skip)",
                        doc_name,
                        stats,
                    )
                except ImportError:
                    self.logger.warning(
                        "RAG_INDEX_QDRANT set but legal_rag / qdrant-client missing; pip install qdrant-client"
                    )
                except Exception as e:
                    self.logger.warning("Qdrant RAG indexing failed (processing succeeded): %s", e)

            result_path = str(consolidated_json_path.resolve())
            if use_sections:
                message = f"≡ƒÄë PROCESSING COMPLETE! Sections: {len(sections)}, obligations: {_count_inner_obligation_rows(all_category_groups)}, consolidated: {consolidated_ob_count}"
                self.logger.info(message)
                force_logger.info(message)  # Force terminal output
            else:
                message = f"≡ƒÄë PROCESSING COMPLETE! Pages: {len(page_texts)}, obligations: {_count_inner_obligation_rows(all_category_groups)}, consolidated: {consolidated_ob_count}"
                self.logger.info(message)
                force_logger.info(message)  # Force terminal output
            return result_path
        except Exception as e:
            self.logger.error(f"Error processing document: {e}", exc_info=True)
            return None
    
    def _fallback_to_legacy_indexing(self, doc_name: str, flat_for_vector_index: List, consolidated_json_data: Dict, out_dir: Path) -> None:
        """
        Fallback to the original indexing approach when the new enhanced indexing fails.
        """
        try:
            from vector_store import index_obligations, index_categories
            chroma_path = str(out_dir / "chroma_db")
            
            # Original obligation indexing
            num_indexed = index_obligations(
                document_name=doc_name,
                consolidated_results=flat_for_vector_index,
                chroma_path=chroma_path,
            )
            self.logger.info(f"Fallback vector index: {num_indexed} obligation chunks indexed in ChromaDB at {chroma_path}")
            
            # Original category indexing
            try:
                category_results = consolidated_json_data.get("results", [])
                if category_results:
                    num_cat_indexed = index_categories(
                        document_name=doc_name,
                        category_results=category_results,
                        chroma_path=chroma_path,
                    )
                    self.logger.info(f"Fallback category index: {num_cat_indexed} categories indexed")
                else:
                    self.logger.info("No categories to index")
            except Exception as cat_e:
                self.logger.warning(f"Fallback category indexing failed (non-fatal): {cat_e}")
                
        except ImportError:
            self.logger.warning("ChromaDB/sentence-transformers not installed; skipping vector indexing. pip install chromadb sentence-transformers")
        except Exception as e:
            err_msg = str(e).lower()
            if "deploymentnotfound" in err_msg or "404" in err_msg or "does not exist" in err_msg:
                self.logger.warning(
                    "Vector indexing failed: Azure embedding deployment not found. "
                    "In Azure Portal (portal.azure.com or ai.azure.com) create a deployment for model 'text-embedding-3-small' "
                    "and set AZURE_OPENAI_EMBEDDING_DEPLOYMENT to that deployment name. "
                    "Or leave AZURE_OPENAI_EMBEDDING_DEPLOYMENT unset to use local embeddings (no Azure deployment needed)."
                )
            else:
                self.logger.warning(f"Vector indexing failed (processing succeeded): {e}")
    
    def process_all_documents(self) -> Dict[str, Any]:
        """Process all PDFs in docs folder. Returns summary dict."""
        try:
            pdf_paths = self.get_all_pdfs()
            
            if not pdf_paths:
                self.logger.error("No PDF files found to process")
                return {
                    "status": "error",
                    "message": "No PDF files found",
                    "total_documents": 0,
                    "successful": 0,
                    "failed": 0,
                    "results": []
                }
            self.logger.info("=" * 80)
            self.logger.info(f"Processing {len(pdf_paths)} documents")
            self.logger.info("=" * 80)
            results = []
            successful = 0
            failed = 0
            for idx, path in enumerate(pdf_paths, 1):
                doc_name = Path(path).name
                self.logger.info(f"\n{'=' * 80}")
                self.logger.info(f"Document {idx}/{len(pdf_paths)}: {doc_name}")
                self.logger.info("=" * 80)
                try:
                    out_path = self.process_document(path)
                    if out_path:
                        successful += 1
                        results.append({
                            "document_name": doc_name,
                            "status": "success",
                            "output_path": out_path
                        })
                        message = f"Γ£à Successfully processed: {doc_name}"
                        self.logger.info(message)
                        print(message, flush=True)  # Force terminal output with flush
                    else:
                        failed += 1
                        results.append({"document_name": doc_name, "status": "failed", "error": "Processing returned None"})
                        self.logger.error(f"Γ£ù Failed to process: {doc_name}")
                except Exception as e:
                    failed += 1
                    error_msg = str(e)
                    results.append({
                        "document_name": doc_name,
                        "status": "failed",
                        "error": error_msg
                    })
                    self.logger.error(f"Γ£ù Error processing {doc_name}: {error_msg}", exc_info=True)
            
            # Summary
            self.logger.info("\n" + "=" * 80)
            self.logger.info("BATCH PROCESSING COMPLETE")
            self.logger.info("=" * 80)
            self.logger.info(f"Total documents: {len(pdf_paths)}")
            self.logger.info(f"Successful: {successful}")
            self.logger.info(f"Failed: {failed}")
            self.logger.info("=" * 80)
            
            return {
                "status": "completed",
                "message": f"Processed {len(pdf_paths)} documents",
                "total_documents": len(pdf_paths),
                "successful": successful,
                "failed": failed,
                "results": results
            }
            
        except Exception as e:
            self.logger.error(f"Fatal error in batch processing: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Fatal error: {str(e)}",
                "total_documents": 0,
                "successful": 0,
                "failed": 0,
                "results": []
            }
    
    def run(self):
        """Process the first PDF in docs folder."""
        try:
            path = self.get_first_pdf()
            if not path:
                self.logger.error("No PDF files in docs folder")
                return
            out = self.process_document(path)
            if out:
                message = "≡ƒÄë Processing completed successfully!"
                self.logger.info(message)
                print(message, flush=True)  # Force terminal output with flush
            else:
                message = "Γ¥î Processing failed"
                self.logger.error(message)
                print(message, flush=True)  # Force terminal output with flush
        except Exception as e:
            self.logger.error(f"Fatal error: {e}", exc_info=True)


def main():
    """Main entry point: process PDF(s); default is page-wise extraction + pagewise + consolidated JSON."""
    import argparse
    parser = argparse.ArgumentParser(
        description=(
            "Process legal PDFs: by default (1) *_pagewise.json, (2) *_consolidated.json. "
            "Set env LEGAL_OCR_USE_SECTION_EXTRACTION=true for (1) *_section_based.json instead of pagewise."
        )
    )
    parser.add_argument(
        "pdf_path",
        nargs="?",
        default=None,
        help="Optional path to a single PDF. If omitted, all PDFs in docs/ are processed.",
    )
    args = parser.parse_args()

    processor = LegalDocumentProcessor(
        local_docs_folder=os.getenv('DOCS_FOLDER', 'docs'),
        local_output_folder=os.getenv('OUTPUT_FOLDER', 'output'),
        logs_folder=os.getenv('LOGS_FOLDER', 'logs'),
        cache_folder=os.getenv('CACHE_FOLDER', 'ocr_cache'),
        prompt_file=os.getenv('PROMPT_FILE', 'prompt.txt'),
        model=get_default_model(),
        tesseract_cmd=os.getenv('TESSERACT_CMD'),
        poppler_path=os.getenv('POPPLER_PATH')
    )

    if args.pdf_path:
        path = Path(args.pdf_path)
        if not path.is_file():
            processor.logger.error(f"File not found: {args.pdf_path}")
            return
        out = processor.process_document(str(path.resolve()))
        if out:
            processor.logger.info(
                "Done. Output: pagewise + consolidated (or section-based + consolidated if LEGAL_OCR_USE_SECTION_EXTRACTION=true)."
            )
        else:
            processor.logger.error("Processing failed.")
    else:
        processor.process_all_documents()


if __name__ == "__main__":
    main()

