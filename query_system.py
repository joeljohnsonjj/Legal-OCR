"""
Interactive Query System for Legal Document Obligations
Searches through consolidated JSON files and returns relevant obligations based on user queries
"""

import os
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from urllib.parse import unquote, urlparse

# Gemini API (REST + API key only)
from gemini_client import generate_content as gemini_generate_content

# FastAPI
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Environment Variables
from dotenv import load_dotenv
load_dotenv()


# GCS Client Helper (for future GCS integration)
def get_storage_client():
    """
    Get a Google Cloud Storage client, configured for fake GCS if STORAGE_EMULATOR_HOST is set.
    
    Returns:
        storage.Client: Configured GCS client (connects to fake GCS if STORAGE_EMULATOR_HOST is set)
    
    Note:
        This function is provided for future GCS integration. The current code uses local file system.
        When STORAGE_EMULATOR_HOST is set, the client automatically connects to fake GCS server.
    """
    try:
        from google.cloud import storage
        # The storage client automatically uses STORAGE_EMULATOR_HOST if set
        # No special configuration needed - just create the client normally
        client = storage.Client()
        return client
    except ImportError:
        logging.getLogger(__name__).warning(
            "google-cloud-storage not available. GCS functionality will not work."
        )
        return None
    except Exception as e:
        logging.getLogger(__name__).error(f"Error creating storage client: {e}")
        return None


def is_fake_gcs_mode():
    """
    Check if fake GCS mode is enabled (STORAGE_EMULATOR_HOST is set).
    
    Returns:
        bool: True if fake GCS mode is enabled, False otherwise
    """
    return bool(os.getenv("STORAGE_EMULATOR_HOST"))


class ObligationQuerySystem:
    """Query legal obligations from consolidated JSON in output folder. Uses Gemini API key only."""

    def __init__(
        self,
        local_output_folder: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize with local output folder. Requires GEMINI_API_KEY in .env."""
        self.local_output_folder = str(Path(local_output_folder or os.getenv("OUTPUT_FOLDER", "output")).resolve())
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.logger = logging.getLogger(__name__)
        self._setup_logging()
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
        """Call Gemini via REST using GEMINI_API_KEY only."""
        return gemini_generate_content(prompt, model=self.model, temperature=temperature, response_mime_type=response_mime_type)
    
    def load_consolidated_jsons(self) -> List[Dict[str, Any]]:
        """Load all *_consolidated.json files from output folder."""
        root = Path(self.local_output_folder)
        if not root.exists():
            self.logger.warning(f"Output folder does not exist: {root}")
            return []
        patterns = list(root.rglob("*_consolidated.json"))
        loaded_data = []
        for p in sorted(patterns):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_data.append({
                    "file_path": str(p.resolve()),
                    "file_name": p.name,
                    "document_name": data.get("document_name", "Unknown"),
                    "data": data
                })
                self.logger.info(f"Loaded: {p.name}")
            except Exception as e:
                self.logger.error(f"Error loading {p}: {e}")
        return loaded_data
    
    def filter_obligations_by_query(self, user_query: str, consolidated_data: Dict[str, Any], 
                                   document_name: str) -> Dict[str, Any]:
        """
        Step 1: Filter obligations from a single consolidated JSON based on user query
        
        Args:
            user_query: User's search query
            consolidated_data: Single consolidated JSON data
            document_name: Name of the source document
            
        Returns:
            Filtered JSON with only relevant obligations
        """
        try:
            # Extract obligations from consolidated data
            obligations = consolidated_data.get("consolidated_results", [])
            
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
            
            # Call Gemini API
            response = self._generate_content(
                prompt=filter_prompt,
                temperature=0.1,
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
            
            # Parse JSON
            filtered_result = json.loads(result_text)
            
            # Ensure it has the required structure
            if "consolidated_results" not in filtered_result:
                filtered_result["consolidated_results"] = []
            
            num_results = len(filtered_result.get("consolidated_results", []))
            self.logger.info(f"  Found {num_results} relevant obligations in {document_name}")
            
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
    
    def merge_and_rank_results(self, user_query: str, filtered_results: List[Dict[str, Any]], 
                              document_name_to_id: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Step 3: Merge all filtered results and rank by relevance and monetary value
        
        Args:
            user_query: User's search query
            filtered_results: List of filtered results from all documents
            document_name_to_id: Optional mapping of document names to document IDs (URLs)
            
        Returns:
            Single merged and ranked JSON
        """
        try:
            # Filter out empty results
            non_empty_results = [r for r in filtered_results if r.get("consolidated_results")]
            
            if not non_empty_results:
                self.logger.info("No relevant obligations found across all documents")
                return {
                    "query": user_query,
                    "total_documents_searched": len(filtered_results),
                    "total_obligations_found": 0,
                    "results": []
                }
            
            # Prepare merge and rank prompt
            merge_prompt = f"""You are a legal document analyst. You have been provided with filtered financial obligations from multiple legal documents, all relevant to a user's query.

Your task is to merge these results into a single JSON and order them by:
1. RELEVANCE to the user query (most relevant first)
2. MONETARY VALUE (highest to lowest)

CRITICAL INSTRUCTIONS:
1. MERGE DUPLICATE OR HIGHLY SIMILAR OBLIGATIONS:
   - If the same obligation appears in multiple documents (same DutyType, Responsible Party, and similar Owner Responsibility), merge them into a single obligation
   - When merging, combine all citations from all sources into one Citation field
   - Example: If "Rent Payment" appears in Document A (Page 3) and Document B (Page 5), create one obligation with Citation: "Document: A.pdf | Page 3, Section X; Document: B.pdf | Page 5, Section Y"
   - Only merge obligations that are truly the same or highly similar (same duty type, same party, same monetary amount if specified)
   - Preserve all unique obligations - do not merge obligations that are different

2. COMBINE ALL OBLIGATIONS:
   - Combine all obligations from all documents into a single array
   - Do not drop obligations unless they are exact duplicates

3. ORDER BY RELEVANCE AND VALUE:
   - Order by relevance to the query first (most relevant first)
   - Then by monetary value (highest amounts first)

4. PRESERVE OBLIGATION CONTENT:
   - Do NOT modify the content of any obligation - preserve exactly as given
   - Keep all fields: "DutyType", "Responsible Party", "Owner Responsibility", "Reasoning", "Citation"
   - Do not add, remove, or modify any other fields
   - When merging duplicates, use the most complete/accurate version of each field

5. UPDATE CITATION FORMAT:
   - IMPORTANT: Update each obligation's "Citation" field to include the source document filename
   - Format: "Document: [filename] | [original citation]"
   - Example: "Document: Commercial Lease Agreement.pdf | Page 3, Section 5(a)"
   - When merging duplicates, combine citations: "Document: A.pdf | Page 3, Section X; Document: B.pdf | Page 5, Section Y"
   - Preserve the original citation format for each source

6. OUTPUT:
   - Output ONLY valid JSON, no commentary
   - The total_obligations_found should reflect the count AFTER deduplication/merging

User Query: "{user_query}"

Filtered results from multiple documents:
{json.dumps(non_empty_results, indent=2)}

Return a JSON object with this EXACT structure:
{{
  "query": "{user_query}",
  "total_documents_searched": {len(filtered_results)},
  "total_obligations_found": <integer count of obligations after merging>,
  "results": [
    {{
      "DutyType": "string",
      "Responsible Party": "string",
      "Owner Responsibility": ["array of strings"],
      "Reasoning": ["array of strings"],
      "Citation": "string in format: Document: [filename] | [original citation]"
      // Note: Citation will be converted to structured format later, so keep as string for now
      // Format: "Document: [filename] | [original citation]"
      // When merging duplicates, combine: "Document: A.pdf | Page 3, Section X; Document: B.pdf | Page 5, Section Y"
    }}
    // Array of all obligations, ordered by relevance (most relevant first) and monetary value (highest first)
  ]
}}

IMPORTANT OUTPUT FORMAT REQUIREMENTS:
- "query": string (the user's query)
- "total_documents_searched": integer (number of documents searched)
- "total_obligations_found": integer (count of obligations AFTER merging duplicates)
- "results": array of obligation objects, each with:
  - "DutyType": string (short label describing the obligation)
  - "Responsible Party": string (party responsible for the obligation)
  - "Owner Responsibility": array of strings (list of responsibilities)
  - "Reasoning": array of strings (explanations for the obligation)
  - "Citation": string (format: "Document: [filename] | [original citation]")

Output the merged and ranked JSON:"""

            self.logger.info(f"Merging and ranking results for query: '{user_query}'")
            
            # Call Gemini API
            response = self._generate_content(
                prompt=merge_prompt,
                temperature=0.1,
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
            
            # Parse JSON
            final_result = json.loads(result_text)
            
            # Post-process: Convert Citation strings to structured format
            if document_name_to_id:
                for obligation in final_result.get("results", []):
                    citation_str = obligation.get("Citation", "")
                    if isinstance(citation_str, str) and citation_str:
                        # Extract document name from citation string
                        doc_name = None
                        if "Document:" in citation_str:
                            doc_name = citation_str.split("Document:")[1].split("|")[0].strip()
                        
                        # Find matching doc_id
                        doc_id = None
                        if doc_name and doc_name in document_name_to_id:
                            doc_id = document_name_to_id[doc_name]
                        else:
                            # Try to find by partial match
                            for name, id_val in document_name_to_id.items():
                                if name in doc_name or doc_name in name:
                                    doc_id = id_val
                                    break
                        
                        # Parse citation string into structured format
                        parsed_citations = self._parse_citation(citation_str, doc_id)
                        
                        if parsed_citations:
                            obligation["Citation"] = parsed_citations
                        else:
                            # Fallback: create basic citation structure
                            obligation["Citation"] = [{
                                "docId": doc_id or "",
                                "pageNumbers": [],
                                "section": []
                            }]
            
            num_results = len(final_result.get("results", []))
            
            # Fix: Ensure total_obligations_found matches the actual count in results array
            # Gemini sometimes reports an incorrect count, so we use the actual array length
            final_result["total_obligations_found"] = num_results
            
            self.logger.info(
                f"Final result: {num_results} obligations ranked and merged "
                f"(updated total_obligations_found to match actual count)"
            )
            
            return final_result
            
        except Exception as e:
            self.logger.error(f"Error merging and ranking results: {e}")
            return {
                "query": user_query,
                "total_documents_searched": len(filtered_results),
                "total_obligations_found": 0,
                "results": [],
                "error": str(e)
            }
    
    def query(self, user_query: str, save_output: bool = True, document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Main query function - orchestrates the entire search process
        
        Args:
            user_query: User's search query (if empty, returns utility-related obligations)
            save_output: Whether to save the output to a JSON file
            document_ids: Optional list of document URLs to filter by
            
        Returns:
            Final ranked results as JSON
        """
        try:
            # If no query provided, default to utilities query
            if not user_query or user_query.strip() == "":
                user_query = "utilities including water, gas, heat, light, electricity, telephone service, HVAC, sprinkler system, electrical and plumbing systems"
                self.logger.info("No query provided - defaulting to utilities query")
            
            self.logger.info("=" * 80)
            self.logger.info(f"Processing query: '{user_query}'")
            if document_ids:
                self.logger.info(f"Filtering by document_ids: {document_ids}")
            self.logger.info("=" * 80)
            
            # Load all consolidated JSONs
            consolidated_files = self.load_consolidated_jsons()
            
            if not consolidated_files:
                return {
                    "query": user_query,
                    "total_documents_searched": 0,
                    "total_obligations_found": 0,
                    "results": [],
                    "error": "No consolidated JSON files found"
                }
            
            # Filter by document_ids if provided
            if document_ids:
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
            
            # Create mapping of document_name to document_id for citation restructuring
            document_name_to_id = {}
            if document_ids:
                # Use the matched_doc_id stored in file_info
                for file_info in consolidated_files:
                    doc_name = file_info.get("document_name", "")
                    matched_doc_id = file_info.get("matched_doc_id")
                    if matched_doc_id:
                        document_name_to_id[doc_name] = matched_doc_id
                        self.logger.info(f"Mapped document name '{doc_name}' to doc_id '{matched_doc_id}'")
            
            # Step 2: Process each consolidated JSON (Step 1 is called for each)
            filtered_results = []
            for file_info in consolidated_files:
                filtered = self.filter_obligations_by_query(
                    user_query,
                    file_info["data"],
                    file_info["document_name"]
                )
                filtered_results.append(filtered)
            
            # Step 3: Merge and rank all results with document_id mapping
            final_result = self.merge_and_rank_results(user_query, filtered_results, document_name_to_id)
            
            # Add timestamp
            final_result["processed_at"] = datetime.now().isoformat()
            
            # Save output if requested
            if save_output:
                self._save_query_result(user_query, final_result)
            
            self.logger.info("=" * 80)
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
    query: str = Field(default="", description="Search query for legal obligations (if empty, returns utility-related obligations)")
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
    document_name: Optional[str] = None
    output_path: Optional[str] = None
    total_pages: Optional[int] = None
    total_obligations: Optional[int] = None
    consolidated_obligations: Optional[int] = None
    error: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "message": "Document processed successfully",
                "document_name": "Commercial Lease Agreement.pdf",
                "output_path": "gs://heb-legal/Output/Commercial_Lease_Agreement_20251209_143000_consolidated.json",
                "total_pages": 25,
                "total_obligations": 45,
                "consolidated_obligations": 32
            }
        }


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
    """Initialize the query system on startup (local output folder + Gemini API key)."""
    global query_system_instance
    try:
        local_out = os.getenv("OUTPUT_FOLDER", "output")
        model = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
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
async def query_obligations_post(request: QueryRequest):
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
        request: QueryRequest containing the search query, optional document_ids filter, and optional output folder
        
    Returns:
        QueryResponse with matched obligations
    """
    if query_system_instance is None:
        raise HTTPException(status_code=503, detail="Query system not initialized")
    
    try:
        if request.output_folder:
            qs = ObligationQuerySystem(local_output_folder=request.output_folder, model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash'))
            result = qs.query(
                user_query=request.query,
                save_output=request.save_output,
                document_ids=request.document_ids
            )
        else:
            # Use default query system instance
            result = query_system_instance.query(
                user_query=request.query,
                save_output=request.save_output,
                document_ids=request.document_ids
            )
        
        # Check for errors in result
        if "error" in result and result.get("total_obligations_found", 0) == 0:
            raise HTTPException(status_code=500, detail=result["error"])
        
        return JSONResponse(content=result)
        
    except Exception as e:
        logging.error(f"Error processing query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}")


@app.post("/process", response_model=ProcessResponse, tags=["Processing"])
async def process_document(request: ProcessRequest):
    """Process PDFs from docs folder and save consolidated JSON to output folder. Uses Gemini API key only."""
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
            model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash'),
            tesseract_cmd=os.getenv('TESSERACT_CMD')
        )
        pdf_path = processor.get_first_pdf()
        if not pdf_path:
            return ProcessResponse(
                status="error",
                message=f"No PDF files found in {docs_folder}",
                error="No PDF files found in docs folder"
            )
        output_path = processor.process_document(pdf_path)
        doc_name = Path(pdf_path).name
        if output_path:
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ProcessResponse(
                    status="success",
                    message="Document processed successfully",
                    document_name=doc_name,
                    output_path=output_path,
                    total_pages=data.get("total_pages", 0),
                    total_obligations=data.get("total_obligations_found", 0),
                    consolidated_obligations=data.get("consolidated_obligations_count", 0)
                )
            except Exception as e:
                logging.warning(f"Could not extract statistics from output: {e}")
                return ProcessResponse(
                    status="success",
                    message="Document processed successfully",
                    document_name=doc_name,
                    output_path=output_path
                )
        return ProcessResponse(
            status="error",
            message="Document processing failed",
            error="Processing failed - check logs for details"
        )
    except Exception as e:
        logging.error(f"Error processing document: {e}", exc_info=True)
        return ProcessResponse(status="error", message=str(e), error=str(e))


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


# ============================================================================
# Command Line Interface
# ============================================================================

def main():
    """Main entry point for command-line usage"""
    import sys
    
    # Get query from command line or prompt user
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:])
    else:
        user_query = input("Enter your query: ").strip()
    
    if not user_query:
        print("Error: Query cannot be empty")
        return
    
    query_system = ObligationQuerySystem(local_output_folder=os.getenv('OUTPUT_FOLDER', 'output'), model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash'))
    print(f"Output folder: {query_system.local_output_folder}")
    
    # Execute query
    result = query_system.query(user_query)
    
    # Print results
    print("\n" + "=" * 80)
    print("QUERY RESULTS")
    print("=" * 80)
    print(f"Query: {result.get('query', '')}")
    print(f"Documents Searched: {result.get('total_documents_searched', 0)}")
    print(f"Obligations Found: {result.get('total_obligations_found', 0)}")
    print("\n" + json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

