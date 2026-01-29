# Gemini API Calls - Complete Reference

This document lists **all places** where the Gemini API is called in the HEB-Legal-OCR system.

---

## Overview

The Gemini API is called in **5 different scenarios** across 2 main files:

1. **`process_legal_documents.py`** - Document processing (3 calls)
2. **`query_system.py`** - Query/search operations (2 calls)

---

## 1. Document Processing (`process_legal_documents.py`)

### Call #1: Extract Party Metadata
**Function**: `GeminiAnalyzer.extract_party_metadata()`  
**File**: `process_legal_documents.py` (line ~416)  
**When**: Called for the **first 5 pages** of each document  
**Purpose**: Extract party names and relationships (e.g., "Tenant" = "ABC Company", "Landlord" = "XYZ Corp")

**Triggered by**:
- `/process` endpoint (POST request)
- `process_document()` method
- Only on pages 1-5 (to reduce API calls)

**API Call Details**:
```python
response = self.client.models.generate_content(
    model=self.model,
    contents=metadata_prompt,
    config=config
)
```

**Frequency**: 
- **1 call per document** (only for first 5 pages combined)
- If document has 10 pages → 1 call
- If document has 100 pages → 1 call

---

### Call #2: Analyze Page for Obligations
**Function**: `GeminiAnalyzer.analyze_page()`  
**File**: `process_legal_documents.py` (line ~515)  
**When**: Called for **EVERY page** of the document  
**Purpose**: Extract financial obligations from each page's text

**Triggered by**:
- `/process` endpoint (POST request)
- `process_document()` method
- Called in a loop for each page

**API Call Details**:
```python
response = self.client.models.generate_content(
    model=self.model,
    contents=full_prompt,  # Includes page text + party metadata
    config=config
)
```

**Frequency**: 
- **N calls per document** (where N = number of pages)
- If document has 10 pages → 10 calls
- If document has 100 pages → 100 calls

**Note**: Pages 1-5 also extract party metadata, but that's handled by Call #1 above.

---

### Call #3: Consolidate Results
**Function**: `GeminiAnalyzer.consolidate_results()`  
**File**: `process_legal_documents.py` (line ~616)  
**When**: Called **once per document** after all pages are analyzed  
**Purpose**: Merge duplicate obligations, deduplicate, and create final consolidated list

**Triggered by**:
- `/process` endpoint (POST request)
- `process_document()` method
- After all pages are analyzed

**API Call Details**:
```python
response = self.client.models.generate_content(
    model=self.model,
    contents=consolidation_prompt,  # All obligations from all pages
    config=config
)
```

**Frequency**: 
- **1 call per document**
- If document has 10 pages → 1 call (after all 10 pages processed)
- If document has 100 pages → 1 call (after all 100 pages processed)

---

## 2. Query System (`query_system.py`)

### Call #4: Filter Obligations by Query
**Function**: `ObligationQuerySystem.filter_obligations_by_query()`  
**File**: `query_system.py` (line ~278)  
**When**: Called for **each document** that matches the search criteria  
**Purpose**: Filter obligations from a document based on user's query (e.g., "Tenant", "Rent payment")

**Triggered by**:
- `/query` endpoint (GET or POST request)
- `query()` method
- Called in a loop for each matching document

**API Call Details**:
```python
response = self.client.models.generate_content(
    model=self.model,
    contents=filter_prompt,  # User query + all obligations from one document
    config=config
)
```

**Frequency**: 
- **M calls per query** (where M = number of documents being searched)
- If searching 2 documents → 2 calls
- If searching 10 documents → 10 calls

**Example**:
- User queries "Tenant" across 2 documents
- Call #4 happens 2 times (once per document)

---

### Call #5: Merge and Rank Results
**Function**: `ObligationQuerySystem.merge_and_rank_results()`  
**File**: `query_system.py` (line ~516)  
**When**: Called **once per query** after all documents are filtered  
**Purpose**: Merge filtered results from all documents, deduplicate, and rank by relevance/monetary value

**Triggered by**:
- `/query` endpoint (GET or POST request)
- `query()` method
- After all documents are filtered (Call #4)

**API Call Details**:
```python
response = self.client.models.generate_content(
    model=self.model,
    contents=merge_prompt,  # All filtered obligations from all documents
    config=config
)
```

**Frequency**: 
- **1 call per query**
- Regardless of how many documents were searched

**Example**:
- User queries "Tenant" across 2 documents
- After 2 filter calls (Call #4), this merge call happens once

---

## Summary Table

| # | Function | File | When | Frequency | Purpose |
|---|----------|------|------|-----------|---------|
| 1 | `extract_party_metadata()` | `process_legal_documents.py` | First 5 pages of document | 1 per document | Extract party names |
| 2 | `analyze_page()` | `process_legal_documents.py` | Every page | N per document (N = pages) | Extract obligations from page |
| 3 | `consolidate_results()` | `process_legal_documents.py` | After all pages analyzed | 1 per document | Merge & deduplicate obligations |
| 4 | `filter_obligations_by_query()` | `query_system.py` | For each document in search | M per query (M = documents) | Filter obligations by query |
| 5 | `merge_and_rank_results()` | `query_system.py` | After all documents filtered | 1 per query | Merge & rank all results |

---

## Total API Calls Examples

### Example 1: Processing a 25-page Document
- **Call #1**: Extract party metadata (pages 1-5) → **1 call**
- **Call #2**: Analyze each page → **25 calls**
- **Call #3**: Consolidate results → **1 call**
- **Total**: **27 Gemini API calls**

### Example 2: Querying "Tenant" Across 2 Documents
- **Call #4**: Filter document 1 → **1 call**
- **Call #4**: Filter document 2 → **1 call**
- **Call #5**: Merge and rank → **1 call**
- **Total**: **3 Gemini API calls**

### Example 3: Processing 3 Documents (10, 20, 30 pages) + 1 Query
**Processing**:
- Document 1: 1 + 10 + 1 = **12 calls**
- Document 2: 1 + 20 + 1 = **22 calls**
- Document 3: 1 + 30 + 1 = **32 calls**
- **Processing Total**: **66 calls**

**Querying** (across all 3 documents):
- Filter 3 documents: **3 calls**
- Merge results: **1 call**
- **Query Total**: **4 calls**

**Grand Total**: **70 Gemini API calls**

---

## Cost Implications

**Important**: Each API call incurs cost based on:
- Input tokens (prompt size)
- Output tokens (response size)
- Model used (`gemini-2.5-flash-lite` is cheaper than `gemini-2.5-flash`)

**Optimization Tips**:
1. **Party metadata extraction** is limited to first 5 pages (reduces calls)
2. **Consolidation** happens once per document (not per page)
3. **Filtering** could be optimized by caching results
4. Consider using **batch processing** for multiple documents

---

## API Call Flow Diagrams

### Document Processing Flow:
```
PDF Document (N pages)
    ↓
[Call #1] Extract Party Metadata (pages 1-5) → 1 call
    ↓
[Call #2] Analyze Page 1 → 1 call
[Call #2] Analyze Page 2 → 1 call
...
[Call #2] Analyze Page N → 1 call
    ↓
[Call #3] Consolidate All Results → 1 call
    ↓
Consolidated JSON saved to GCS
```

### Query Flow:
```
User Query + Document IDs
    ↓
Load Consolidated JSONs from GCS
    ↓
[Call #4] Filter Document 1 → 1 call
[Call #4] Filter Document 2 → 1 call
...
[Call #4] Filter Document M → 1 call
    ↓
[Call #5] Merge & Rank All → 1 call
    ↓
Final Results Returned
```

---

## Configuration

All Gemini API calls use:
- **Model**: From `GEMINI_MODEL` env var (default: `gemini-2.5-flash-lite`)
- **Temperature**: `0.1` (low for consistent, factual extraction)
- **Response Format**: `application/json` (structured JSON output)
- **Authentication**: Vertex AI (if `GOOGLE_GENAI_USE_VERTEXAI=True`) or Gemini API key

---

## Notes

- **No caching**: Each call is made fresh (no caching of results)
- **Sequential processing**: Calls are made one after another (not parallel)
- **Error handling**: Each call has try/except blocks for error handling
- **Retry logic**: Some calls may have retry logic (check `process_legal_documents.py` for `@retry_with_exponential_backoff`)
