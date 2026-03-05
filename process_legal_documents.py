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
from typing import List, Dict, Any, Optional
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
from llm_client import generate_content as llm_generate_content, get_default_model

# Environment Variables
from dotenv import load_dotenv
load_dotenv()


def _normalize_key(s: Any) -> str:
    """Normalize for dedup key: strip, lower, collapse whitespace."""
    if s is None:
        return ""
    return " ".join(str(s).strip().lower().split())


def _merge_duplicate_obligations(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge obligations with same DutyType and Responsible Party; combine Owner Responsibility, Reasoning, Citation."""
    if not obligations:
        return []
    key_to_ob = {}
    for ob in obligations:
        duty = ob.get("DutyType") or ""
        party = ob.get("Responsible Party") or ""
        key = (_normalize_key(duty), _normalize_key(party))
        if key not in key_to_ob:
            key_to_ob[key] = {
                "DutyType": duty if isinstance(duty, str) else (duty[0] if isinstance(duty, list) else str(duty)),
                "Responsible Party": party if isinstance(party, str) else str(party),
                "Owner Responsibility": [],
                "Reasoning": [],
                "Citation": "",
                "related_keywords": [],
            }
        existing = key_to_ob[key]
        for field, default in (
            ("Owner Responsibility", []),
            ("Reasoning", []),
            ("related_keywords", []),
        ):
            val = ob.get(field)
            if isinstance(val, list):
                for v in val:
                    if v and str(v).strip() and str(v).strip() not in {str(x).strip() for x in existing[field]}:
                        existing[field].append(v)
            elif val and str(val).strip():
                if str(val).strip() not in {str(x).strip() for x in existing[field]}:
                    existing[field].append(val)
        cite = ob.get("Citation") or ""
        if cite and isinstance(cite, str) and cite.strip():
            if existing["Citation"]:
                existing["Citation"] = existing["Citation"].rstrip(".;") + "; " + cite.strip()
            else:
                existing["Citation"] = cite.strip()
        elif isinstance(cite, list):
            for c in cite:
                if c and str(c).strip():
                    if existing["Citation"]:
                        existing["Citation"] = existing["Citation"].rstrip(".;") + "; " + str(c).strip()
                    else:
                        existing["Citation"] = str(c).strip()
    out = list(key_to_ob.values())
    for ob in out:
        if not ob.get("related_keywords"):
            duty = ob.get("DutyType")
            if duty:
                ob["related_keywords"] = [duty] if isinstance(duty, str) else duty
    return out


def _extract_obligations_list(parsed: Any) -> List[Dict[str, Any]]:
    """Extract list of obligation dicts from LLM response (array or object with array in known keys or single obligation dict)."""
    def is_obligation_list(lst: Any) -> bool:
        return (
            isinstance(lst, list)
            and len(lst) > 0
            and isinstance(lst[0], dict)
            and ("DutyType" in lst[0] or "Responsible Party" in lst[0])
        )

    def is_single_obligation(d: Any) -> bool:
        return (
            isinstance(d, dict)
            and ("DutyType" in d or "Responsible Party" in d)
        )

    if isinstance(parsed, list):
        if is_obligation_list(parsed):
            return parsed
        return parsed if parsed else []
    if isinstance(parsed, dict):
        # LLM sometimes returns a single obligation object at root instead of an array
        if is_single_obligation(parsed):
            return [parsed]
        for key in (
            "consolidated_results", "results", "obligations", "data", "items",
            "obligations_list", "consolidated", "output", "obligations_array",
        ):
            val = parsed.get(key)
            if is_obligation_list(val):
                return val
        for val in parsed.values():
            if is_obligation_list(val):
                return val
    return []


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
        self.logger.info(f"Processing PDF: {pdf_path}")
        
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
        _use_azure = os.getenv("USE_AZURE_OPENAI", "").lower() in ("true", "1", "yes")
        if _use_azure:
            if not os.getenv("AZURE_OPENAI_ENDPOINT") or not (os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY") or os.getenv("OPENAI_API_KEY")):
                raise ValueError("USE_AZURE_OPENAI is set; AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (or AZURE_OPENAI_KEY) must be set in .env")
            if not os.getenv("AZURE_OPENAI_DEPLOYMENT") and not os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") and not os.getenv("OPENAI_DEPLOYMENT_NAME"):
                raise ValueError("USE_AZURE_OPENAI is set; AZURE_OPENAI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT_NAME must be set in .env")
        else:
            if not os.getenv('GEMINI_API_KEY') and not os.getenv('GOOGLE_API_KEY'):
                raise ValueError("GEMINI_API_KEY must be set in .env (Gemini API key from https://aistudio.google.com/app/apikey)")
        self.logger.info("LLM client (Azure OpenAI or Gemini) ready")
    
    def reset_party_metadata(self):
        """
        Reset party metadata for a new document
        
        This should be called at the start of processing each new document
        to prevent party information from one document leaking into another.
        """
        self.party_metadata = {}
        self.logger.info("Party metadata reset for new document")
    
    def _generate_content(self, prompt: str, temperature: float = 0.1, response_mime_type: str = "application/json", max_output_tokens: Optional[int] = None):
        """Call configured LLM (Azure OpenAI or Gemini) via llm_client."""
        return llm_generate_content(
            prompt,
            model=self.model,
            temperature=temperature,
            response_mime_type=response_mime_type,
            max_output_tokens=max_output_tokens,
        )
    
    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def extract_party_metadata(self, page_num: int, page_text: str) -> Dict[str, Any]:
        """
        Extract party definitions and identifiers from a page
        
        Args:
            page_num: Page number
            page_text: Text content of the page
            
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
            
            # Update cumulative party metadata
            if "parties" in metadata and metadata["parties"]:
                for party in metadata["parties"]:
                    ref_label = party.get("reference_label", "").strip()
                    if ref_label:
                        self.party_metadata[ref_label] = {
                            "actual_name": party.get("actual_name", ""),
                            "additional_info": party.get("additional_info", "")
                        }
                        self.logger.info(f"  Found party: {ref_label} -> {party.get('actual_name', '')}")
            
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
    def analyze_page(self, page_num: int, page_text: str, extract_parties: bool = True) -> List[Dict[str, Any]]:
        """
        Analyze a single page using Gemini API
        
        Args:
            page_num: Page number
            page_text: Text content of the page
            extract_parties: Whether to extract party metadata from this page (default: True)
            
        Returns:
            List of financial obligation records
        """
        try:
            # Extract party metadata from this page if requested (typically first 5 pages only)
            if extract_parties:
                self.extract_party_metadata(page_num, page_text)
            
            # Build metadata context string using ALL parties found so far (including current page)
            metadata_context = ""
            if self.party_metadata:
                metadata_context = "\n\n--- PARTY METADATA (Use actual names in 'Responsible Party' field) ---\n\n"
                metadata_context += "The following parties have been identified in this document:\n\n"
                for ref_label, info in self.party_metadata.items():
                    actual_name = info.get("actual_name", "")
                    additional_info = info.get("additional_info", "")
                    if actual_name:
                        metadata_context += f"- '{ref_label}' refers to: {actual_name}"
                        if additional_info:
                            metadata_context += f" ({additional_info})"
                        metadata_context += "\n"
                
                metadata_context += "\nIMPORTANT: When extracting obligations, use the actual party names (e.g., 'ABC Company') in the 'Responsible Party' field, NOT the reference labels (e.g., 'Tenant'). If the actual name is not yet known, use the reference label.\n"
            
            # Construct the full prompt with metadata (explicit: extract ALL obligations, multiple per page)
            # Ask for object with "obligations" key so Azure response_format json_object is satisfied (root must be object, not array)
            full_prompt = f"""{self.prompt_template}{metadata_context}

CRITICAL — OUTPUT FORMAT: Respond with a single JSON object that has exactly one key: "obligations". The value of "obligations" must be a JSON array listing EVERY financial obligation on this page. One obligation = one object in that array. If the page mentions rent, deposit, taxes, insurance, utilities, maintenance, fees, etc., each must be a separate object in the array. Do NOT combine them into one. Example: {{ "obligations": [ {{ "DutyType": "...", "Responsible Party": "...", ... }}, {{ "DutyType": "...", ... }} ] }}

--- PAGE {page_num} TEXT (extract every financial obligation below; put each as an object in the "obligations" array) ---

{page_text}"""
            
            self.logger.info(f"Analyzing page {page_num} with Gemini API...")
            
            # Call Gemini API
            response = self._generate_content(
                prompt=full_prompt,
                temperature=0.1,  # Low temperature for consistent, factual extraction
                response_mime_type="application/json"
            )
            
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
            
            # Parse JSON (handle array, or object with obligations/results/items/etc., or single obligation object)
            parsed = json.loads(result_text)
            if isinstance(parsed, list):
                obligations = parsed
            elif isinstance(parsed, dict):
                # Unwrap: prefer "obligations" (asked in prompt), then other common keys
                for key in ("obligations", "results", "items", "data", "consolidated_results", "records"):
                    if isinstance(parsed.get(key), list):
                        obligations = parsed[key]
                        break
                else:
                    # Single obligation object (DutyType etc.) — treat as one
                    if any(k in parsed for k in ("DutyType", "Responsible Party", "Owner Responsibility")):
                        obligations = [parsed]
                    else:
                        # Any dict value that is a list of objects with DutyType?
                        for v in parsed.values():
                            if isinstance(v, list) and v and isinstance(v[0], dict) and "DutyType" in v[0]:
                                obligations = v
                                break
                        else:
                            obligations = []
            else:
                obligations = []
            
            if len(obligations) == 0:
                self.logger.warning(f"Page {page_num}: 0 obligations — API response (first 600 chars): {result_text[:600]!r}")
            self.logger.info(f"Page {page_num}: Found {len(obligations)} financial obligations")
            return obligations
            
        except json.JSONDecodeError as e:
            # JSON parsing errors should not trigger retry
            self.logger.error(f"Error parsing JSON response for page {page_num}: {e}")
            self.logger.error(f"Response text: {result_text[:500]}")
            return []
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
                self.logger.error(f"Error analyzing page {page_num}: {e}")
                return []

    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def analyze_section(
        self,
        section_number: str,
        section_title: str,
        section_content: str,
        extract_parties: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Analyze a single document section (regex-extracted) and return obligations.
        extract_parties: run party extraction on this section (typically True only for first section).
        """
        try:
            if extract_parties and section_content.strip():
                self.extract_party_metadata(1, f"Section {section_number}. {section_title}\n\n{section_content}")
            metadata_context = ""
            if self.party_metadata:
                metadata_context = "\n\n--- PARTY METADATA (Use actual names in 'Responsible Party' field) ---\n\n"
                metadata_context += "The following parties have been identified in this document:\n\n"
                for ref_label, info in self.party_metadata.items():
                    actual_name = info.get("actual_name", "")
                    additional_info = info.get("additional_info", "")
                    if actual_name:
                        metadata_context += f"- '{ref_label}' refers to: {actual_name}"
                        if additional_info:
                            metadata_context += f" ({additional_info})"
                        metadata_context += "\n"
                metadata_context += "\nIMPORTANT: When extracting obligations, use the actual party names in 'Responsible Party', NOT reference labels.\n"
            full_prompt = f"""{self.prompt_template}{metadata_context}

CRITICAL — OUTPUT FORMAT: Respond with a single JSON object that has exactly one key: "obligations". The value of "obligations" must be a JSON array listing EVERY financial obligation in this section. One obligation = one object. If the section mentions rent, deposit, taxes, insurance, utilities, maintenance, fees, etc., each must be a separate object. Do NOT combine them. Example: {{ "obligations": [ {{ "DutyType": "...", "Responsible Party": "...", ... }} ] }}

--- SECTION {section_number}. {section_title} (extract every financial obligation below; put each as an object in the "obligations" array) ---

{section_content}"""
            display_num = (section_number or "").strip() or "(unnumbered)"
            self.logger.info(f"Analyzing section {display_num} with LLM...")
            response = self._generate_content(
                prompt=full_prompt,
                temperature=0.1,
                response_mime_type="application/json",
            )
            result_text = response.text.strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()
            parsed = json.loads(result_text)
            if isinstance(parsed, list):
                obligations = parsed
            elif isinstance(parsed, dict):
                for key in ("obligations", "results", "items", "data", "consolidated_results", "records"):
                    if isinstance(parsed.get(key), list):
                        obligations = parsed[key]
                        break
                else:
                    if any(k in parsed for k in ("DutyType", "Responsible Party", "Owner Responsibility")):
                        obligations = [parsed]
                    else:
                        obligations = []
                        for v in parsed.values():
                            if isinstance(v, list) and v and isinstance(v[0], dict) and "DutyType" in v[0]:
                                obligations = v
                                break
            else:
                obligations = []
            display_num = (section_number or "").strip() or "(unnumbered)"
            self.logger.info(f"Section {display_num}: Found {len(obligations)} financial obligations")
            return obligations
        except json.JSONDecodeError as e:
            display_num = (section_number or "").strip() or "(unnumbered)"
            self.logger.error(f"Error parsing JSON for section {display_num}: {e}")
            return []
        except Exception as e:
            error_str = str(e).lower()
            if any(k in error_str for k in ["rate limit", "quota", "resource exhausted", "429", "throttl"]):
                raise
            display_num = (section_number or "").strip() or "(unnumbered)"
            self.logger.error(f"Error analyzing section {display_num}: {e}")
            return []

    @retry_with_exponential_backoff(max_retries=5, initial_delay=5.0, exponential_base=2.0)
    def consolidate_results_to_json(self, all_page_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Consolidate obligations from all pages into a single, deduplicated JSON array using the LLM.
        Returns the list of consolidated obligation dicts (same structure: DutyType, Responsible Party, Owner Responsibility, Reasoning, Citation).
        """
        try:
            self.logger.info("Consolidating results into JSON...")
            metadata_context = ""
            if self.party_metadata:
                metadata_context = "\n\nParty metadata (use Responsible Party as given or from metadata):\n"
                for ref_label, info in self.party_metadata.items():
                    actual_name = info.get("actual_name", "") or ref_label
                    metadata_context += f"- {ref_label}: {actual_name}\n"
            num_input = len(all_page_results)
            consolidation_prompt = f"""You are a legal analyst. You have been provided with {num_input} financial obligations extracted from multiple pages of a legal document.

Your task is to consolidate these into a single, deduplicated JSON array. The consolidated JSON must be produced directly from this input.

CRITICAL: You MUST output a non-empty JSON array. The input contains {num_input} obligations; your output must be a deduplicated/merged array of those obligations (fewer items after merging duplicates). Never return an empty array [].

Rules:
1. Merge duplicate or highly similar obligations (same Duty Type, same Party, same or similar obligation text).
2. Preserve all unique obligations.
3. Each element must have: "DutyType", "Responsible Party", "Owner Responsibility" (array of strings), "Reasoning" (array of strings), "Citation" (string), and "related_keywords" (array of strings).
4. DutyType: short, precise label (e.g. Rent Payment, Security Deposit, Property Tax Payment).
5. Responsible Party: use as given or from metadata below.
6. When merging, combine citations (e.g., "Page 3, Section A; Page 7, Section B").
7. related_keywords: for each obligation, add an array of 8–20 search keywords/phrases. You MUST include the DutyType itself (and normalised variations, e.g. "Rent Payment" → "rent payment", "rent") in this array. Add synonyms and related concepts so semantic search can find this obligation. Example: for DutyType "Property Insurance", related_keywords must include "Property Insurance" or "property insurance", plus e.g. ["insurance", "property insurance", "liability", "coverage", "premium", "tenant insurance"].
8. Maintain legal accuracy. Output ONLY a valid JSON array, no other text.
{metadata_context}
Extracted obligations from the document:

{json.dumps(all_page_results, indent=2)}

Output ONLY the consolidated JSON array (non-empty, deduplicated), starting with [ and ending with ]:"""
            response = self._generate_content(
                prompt=consolidation_prompt,
                temperature=0.1,
                response_mime_type="application/json",
                max_output_tokens=16384,
            )
            result_text = (response.text or "").strip()
            for prefix in ("```json", "```"):
                if result_text.startswith(prefix):
                    result_text = result_text[len(prefix):]
                    break
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()
            try:
                parsed = json.loads(result_text)
            except json.JSONDecodeError as e:
                self.logger.error(
                    "Consolidation JSON parse failed (possible truncation or invalid format). Error: %s. Response length: %d. First 500 chars: %s",
                    e, len(result_text), result_text[:500],
                )
                raise
            consolidated = _extract_obligations_list(parsed)
            if not consolidated and all_page_results:
                self.logger.error(
                    "LLM consolidation returned 0 obligations although %d were extracted from the PDF. Parsed type: %s, keys: %s. Raw response (first 800 chars): %s",
                    num_input,
                    type(parsed).__name__,
                    list(parsed.keys()) if isinstance(parsed, dict) else "n/a",
                    result_text[:800],
                )
            # Ensure DutyType is always in related_keywords for semantic search
            for ob in consolidated:
                ob.pop("_source_page", None)
                duty = ob.get("DutyType")
                if duty is not None:
                    duty_str = " ".join(duty).strip() if isinstance(duty, list) else str(duty).strip()
                    if duty_str:
                        kw = ob.get("related_keywords")
                        if not isinstance(kw, list):
                            ob["related_keywords"] = [duty_str]
                        else:
                            seen = {str(x).strip().lower() for x in kw if x}
                            if duty_str.lower() not in seen:
                                ob["related_keywords"] = [duty_str] + [x for x in kw if x]
            # Programmatic merge: same DutyType + Responsible Party → one entry (LLM often leaves duplicates)
            before_merge = len(consolidated)
            consolidated = _merge_duplicate_obligations(consolidated)
            if len(consolidated) < before_merge:
                self.logger.info(f"Programmatic dedup: {before_merge} → {len(consolidated)} (same DutyType + Responsible Party merged)")
            self.logger.info(f"Consolidated into JSON ({len(consolidated)} obligations)")
            return consolidated
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = any(k in error_str for k in [
                'rate limit', 'quota', 'resource exhausted', 'resource_exhausted',
                'too many requests', '429', 'throttl'
            ])
            if is_rate_limit:
                raise
            self.logger.error(f"Error consolidating to JSON (consolidated output will be empty): {e}", exc_info=True)
            return []


class LegalDocumentProcessor:
    """Legal document processing: local docs folder -> output folder. Uses Azure OpenAI or Gemini via llm_client."""

    def __init__(self,
                 local_docs_folder: Optional[str] = None,
                 local_output_folder: Optional[str] = None,
                 logs_folder: str = "logs",
                 cache_folder: str = "ocr_cache",
                 prompt_file: str = "prompt.txt",
                 model: str = "gemini-2.5-flash-lite",
                 tesseract_cmd: Optional[str] = None,
                 poppler_path: Optional[str] = None):
        """Initialize with local docs and output folders. Requires GEMINI_API_KEY or (when USE_AZURE_OPENAI) Azure env vars in .env."""
        self.logs_folder = Path(logs_folder)
        self.cache_folder = Path(cache_folder)
        self.logs_folder.mkdir(exist_ok=True)
        self.cache_folder.mkdir(exist_ok=True)
        self._setup_logging()
        self.logger = logging.getLogger(__name__)
        self.local_docs_folder = str(Path(local_docs_folder or os.getenv("DOCS_FOLDER", "docs")).resolve())
        self.local_output_folder = str(Path(local_output_folder or os.getenv("OUTPUT_FOLDER", "output")).resolve())
        Path(self.local_output_folder).mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Legal Document Processor (local): docs={self.local_docs_folder}, output={self.local_output_folder}")
        self.pdf_processor = PDFProcessor(tesseract_cmd, str(self.cache_folder), poppler_path)
        self.gemini_analyzer = GeminiAnalyzer(prompt_file, model)
    
    def _setup_logging(self):
        """Setup logging configuration"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.logs_folder / f"processing_{timestamp}.log"
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )
        
        self.logger = logging.getLogger(__name__)
    
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
            self.logger.info("=" * 80)
            self.logger.info(f"Processing document: {doc_name}")
            self.logger.info(f"From: {pdf_path}")
            self.logger.info("=" * 80)
            self.gemini_analyzer.reset_party_metadata()
            page_texts = self.pdf_processor.process_pdf(str(pdf_path))
            if not page_texts:
                self.logger.error("No text extracted from PDF")
                return None
            # Build full text and extract section structure (regex: ^\d+\. , ^\([a-z]\) , ^\(\d+\))
            sorted_pages = sorted(page_texts.keys())
            full_text = "\n\n".join(page_texts[p] for p in sorted_pages)
            structure = self.pdf_processor.extract_document_structure(full_text)
            sections_tree = structure.get("sections", [])
            # Flatten tree so Section 1, 1.1, 1.1.1 each get obligation extraction
            sections = self.pdf_processor.flatten_sections_for_processing(sections_tree)
            total_sections = len(sections)
            self.logger.info(f"Extracted {total_sections} sections (flattened); running obligation extraction per section")
            # Use ordinal index (1-based) as key so restarted numbering doesn't overwrite
            section_results = {}
            all_obligations = []
            page_delay = float(os.getenv("PAGE_PROCESSING_DELAY", "3.5"))
            for i, sec in enumerate(sections):
                section_index = str(i + 1)
                sec_num = (sec.get("section_number") or "").strip() or section_index
                sec_title = (sec.get("section_title") or "").strip()
                content = (sec.get("content") or "").strip()
                title_snippet = (sec_title[:50] + "…") if len(sec_title) > 50 else sec_title
                self.logger.info(f"--- Section [{i + 1}/{total_sections}]: {sec_num} — {title_snippet or '(no title)'} ---")
                if not content:
                    self.logger.info(f"  Skipping section {sec_num} (no content)")
                    section_results[section_index] = {
                        "section_number": sec_num,
                        "section_title": sec_title,
                        "obligations": [],
                    }
                    continue
                extract_parties = i == 0
                obligations = self.gemini_analyzer.analyze_section(
                    sec_num, sec_title, content, extract_parties=extract_parties
                )
                self.logger.info(f"  Section {sec_num}: extracted {len(obligations)} obligations")
                section_results[section_index] = {
                    "section_number": sec_num,
                    "section_title": sec_title,
                    "obligations": obligations,
                }
                all_obligations.extend(obligations)
                if i < len(sections) - 1:
                    time.sleep(page_delay)
            consolidated_results = self.gemini_analyzer.consolidate_results_to_json(all_obligations)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            section_based_filename = f"{doc_stem}_{timestamp}_section_based.json"
            section_based_data = {
                "document_name": doc_name,
                "processed_at": datetime.now().isoformat(),
                "total_sections": len(sections),
                "total_obligations_found": len(all_obligations),
                "party_metadata": self.gemini_analyzer.party_metadata,
                "section_results": section_results,
            }
            consolidated_data = {
                "document_name": doc_name,
                "processed_at": datetime.now().isoformat(),
                "total_pages": len(page_texts),
                "total_obligations_found": len(all_obligations),
                "consolidated_obligations_count": len(consolidated_results),
                "party_metadata": self.gemini_analyzer.party_metadata,
                "consolidated_results": consolidated_results,
            }
            out_dir = Path(self.local_output_folder)
            out_dir.mkdir(parents=True, exist_ok=True)
            section_based_path = out_dir / section_based_filename
            consolidated_json_path = out_dir / f"{doc_stem}_{timestamp}_consolidated.json"
            with open(section_based_path, "w", encoding="utf-8") as f:
                json.dump(section_based_data, f, indent=2, ensure_ascii=False)
            with open(consolidated_json_path, "w", encoding="utf-8") as f:
                json.dump(consolidated_data, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Section-based results saved to: {section_based_path}")
            self.logger.info(f"Consolidated JSON saved to: {consolidated_json_path}")
            # Vector store: one chunk per consolidated (deduplicated) obligation in ChromaDB
            # index_obligations deletes this document's old chunks first, then re-indexes (no orphans)
            try:
                from vector_store import index_obligations
                chroma_path = str(out_dir / "chroma_db")
                num_indexed = index_obligations(
                    document_name=doc_name,
                    consolidated_results=consolidated_results,
                    chroma_path=chroma_path,
                )
                self.logger.info(f"Vector index: {num_indexed} obligation chunks (from consolidated deduplicated list) indexed in ChromaDB at {chroma_path}")
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
            result_path = str(consolidated_json_path.resolve())
            self.logger.info(f"Processing complete! Sections: {len(sections)}, obligations: {len(all_obligations)}, consolidated: {len(consolidated_results)}")
            return result_path
        except Exception as e:
            self.logger.error(f"Error processing document: {e}", exc_info=True)
            return None
    
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
                        self.logger.info(f"✓ Successfully processed: {doc_name}")
                    else:
                        failed += 1
                        results.append({"document_name": doc_name, "status": "failed", "error": "Processing returned None"})
                        self.logger.error(f"✗ Failed to process: {doc_name}")
                except Exception as e:
                    failed += 1
                    error_msg = str(e)
                    results.append({
                        "document_name": doc_name,
                        "status": "failed",
                        "error": error_msg
                    })
                    self.logger.error(f"✗ Error processing {doc_name}: {error_msg}", exc_info=True)
            
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
                self.logger.info("Processing completed successfully!")
            else:
                self.logger.error("Processing failed")
        except Exception as e:
            self.logger.error(f"Fatal error: {e}", exc_info=True)


def main():
    """Main entry point: process PDF(s) and write section-based + consolidated JSON to output/."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Process legal PDFs: produces (1) section-based obligations JSON, (2) consolidated JSON."
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
            processor.logger.info("Done. Output files: 1) section-based JSON, 2) consolidated JSON.")
        else:
            processor.logger.error("Processing failed.")
    else:
        processor.process_all_documents()


if __name__ == "__main__":
    main()

