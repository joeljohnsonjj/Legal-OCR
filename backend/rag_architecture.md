# Architecture Specification: Dual-Track Legal RAG & OCR Pipeline

## 1. System Objectives
The system processes legal contracts to extract financial obligations. 
* **Legacy Feature:** Users can search for specific obligations via exact keyword matching (e.g., "rent"). This must remain 100% accurate and bypass semantic search.
* **New Feature:** A natural language chatbot that can answer questions about specific financial obligations AND general contract clauses (e.g., "Are pets allowed?").

**Solution:** A unified vector database utilizing metadata payload injection, an LLM query router, and "Parent-Child" context fetching.

---

## 2. Document Processing Pipeline
Documents are processed on a page-by-page basis. The pipeline must output two distinct records per page to prevent information loss.

### 2.1 Track A: Raw Page Record (For General Chat)
* **Text Payload:** The complete, raw OCR text of the page.
* **Metadata Tags:** * `record_type`: "raw_page"
  * `document_id`: string
  * `page_number`: integer

### 2.2 Track B: Extracted Obligation Record (For UI & Specific Chat)
* **Text Payload (The Vector):** A concatenated English string of the JSON fields to optimize semantic matching. 
  * *Format:* `"Duty Type: [DutyType]. [Responsible Party] is responsible. Action: [Owner Responsibility]. Reasoning: [Reasoning]."`
* **Metadata Tags:**
  * `record_type`: "extracted_obligation"
  * `document_id`: string
  * `page_number`: integer
  * `exact_json_payload`: stringified JSON (The entire original JSON object containing DutyType, Responsible Party, Citation, and the related_keywords array).

---

## 3. Query Routing Orchestration

### 3.1 Legacy UI Routing (Keyword Search)
* **Trigger:** User uses the standard search bar.
* **Action:** DO NOT embed the query. Perform a pure metadata filter search.
* **Logic:** `record_type == "extracted_obligation"` AND `metadata.exact_json_payload.related_keywords CONTAINS [user_keyword]`.
* **Return:** The `exact_json_payload` directly to the frontend.

### 3.2 Chatbot Routing (LLM Router)
* **Trigger:** User sends a message in the chat interface.
* **Action:** Pass the query to a lightweight LLM configured with Function Calling/Tools.
* **Tools:**
  1. `search_financial_obligations`: Triggered when the user asks about money, duties, rent, or maintenance.
  2. `search_general_document`: Triggered when the user asks about general rules, termination, pets, or definitions.
* **Execution:**
  * If Tool 1 -> Filter vector search by `record_type: "extracted_obligation"`
  * If Tool 2 -> Filter vector search by `record_type: "raw_page"`

---

## 4. Context Assembly (Parent Fetch Logic)
To prevent the chatbot from hallucinating due to lack of surrounding context, implement the following retrieval logic:

1. Execute the vector search based on the Router's decision.
2. Iterate through the returned matches.
3. If a match has `record_type == "extracted_obligation"`:
   * Extract the `document_id` and `page_number` from its metadata.
   * Make a secondary query to the database to fetch the `raw_page` record matching that exact `document_id` and `page_number`.
   * Append the raw page text beneath the extracted obligation text in the final context block.
4. If a match has `record_type == "raw_page"`, use it as is.
5. Pass the assembled context blocks to the final Chatbot LLM alongside the system prompt and the user's query.

---

## 5. Target LLM System Prompt
```text
System: You are an expert legal document assistant. Answer the user's question using ONLY the context provided below. The context may include specific extracted obligations and the raw text of the page where that obligation was found. 

If the answer is not present in the context, clearly state: "I do not have enough information in the provided document to answer that." Do not invent or assume legal clauses.

Context:
{assembled_context}

User Question: {user_query}
```

---

## 6. Implementation (this repo)

Python package **`legal_rag/`** provides Qdrant schemas, upsert/search, LLM router (`router.py` — **Bedrock** when `LITELLM_MODEL`/`LLM_MODEL` is `bedrock/...`, else OpenAI/Azure), parent-fetch context assembly (`retrieval.py`), `run_chat_retrieval()` and `run_chat_turn()` in `pipeline.py`, and optional **`POST /chat`** in **`query_system.py`** (test with Postman or `/docs`). Set **`RAG_INDEX_QDRANT=true`** when processing PDFs to populate Qdrant. Legacy Chroma keyword/semantic query remains in **`vector_store.py`** / **`query_system.py`** (`/query`).