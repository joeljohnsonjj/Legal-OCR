# API Reference

Base URL (default): `http://localhost:8000`  
Interactive docs: `http://localhost:8000/docs` (Swagger), `http://localhost:8000/redoc` (ReDoc).

---

## Available APIs

| Method | Path        | Description                          |
|--------|-------------|--------------------------------------|
| GET    | `/`         | Root / service info and endpoint list |
| GET    | `/health`   | Health check                         |
| POST   | `/query`    | Query legal obligations              |
| POST   | `/process`  | Process PDFs and produce consolidated JSON |
| GET    | `/documents`| List consolidated documents          |

---

## 1. GET `/` — Root

Returns service status and a list of endpoints.

**Request:** No body. No required query parameters.

**Response (schema):**

```json
{
  "status": "string",       // "online"
  "service": "string",      // "Legal Obligation Query System"
  "version": "string",      // "1.0.0"
  "endpoints": {
    "query_post": "string",   // "/query (POST)"
    "process": "string",     // "/process (POST)"
    "documents": "string",   // "/documents (GET)"
    "health": "string",      // "/health"
    "docs": "string"         // "/docs"
  }
}
```

**Example:** `GET http://localhost:8000/`

---

## 2. GET `/health` — Health check

Checks that the query system is initialized and reports document count and output folder.

**Request:** No body. No query parameters.

**Response (schema):**

```json
{
  "status": "string",           // "healthy" | "degraded"
  "query_system": "string",     // "initialized"
  "documents_available": 0,     // number of consolidated JSONs loaded
  "output_folder": "string",    // path to output folder
  "error": "string"             // optional; present if status is "degraded"
}
```

**Example:** `GET http://localhost:8000/health`

**Errors:** `503` if the query system is not initialized.

---

## 3. POST `/query` — Query obligations

Searches consolidated JSON files and returns obligations that match the query, ranked by relevance and monetary value. If the query is empty, returns utility-related obligations (water, gas, heat, electricity, HVAC, etc.). Optionally restrict by `document_ids`.

**Request**

- **Method:** POST  
- **Content-Type:** `application/json`

**Request body (schema):**

```json
{
  "query": "string",              // optional; default "". Search text; empty = utility obligations.
  "document_ids": ["string"],    // optional; null or omit = all documents. Filter by doc names/URLs.
  "save_output": false,          // optional; default false. Whether to save results to a file.
  "output_folder": "string"      // optional; default from env OUTPUT_FOLDER. Folder to search for consolidated JSONs.
}
```

**Example request body:**

```json
{
  "query": "Landlord HVAC Hazardous Materials",
  "document_ids": ["Commercial Lease Agreement - Buyer Triple Net.pdf"]
}
```

**Response (schema):**

```json
{
  "query": "string",                    // echo of search query (or default utilities query)
  "total_documents_searched": 0,        // number of consolidated docs searched
  "total_obligations_found": 0,         // number of obligations returned
  "processed_at": "string",             // ISO 8601 timestamp
  "results": [                          // list of obligation objects
    {
      "DutyType": "string",
      "Responsible Party": "string",
      "Owner Responsibility": ["string"],
      "Reasoning": ["string"],
      "Citation": [
        {
          "docId": "string",
          "pageNumbers": [0],
          "section": ["string"]
        }
      ]
    }
  ],
  "error": "string"                     // optional; present on failure
}
```

**Example:** `POST http://localhost:8000/query` with body as above.

**Errors:** `503` if query system not initialized; `500` on processing error (details in body).

---

## 4. POST `/process` — Process documents

Reads PDFs from the docs folder, extracts text (or OCR for scanned PDFs), runs Gemini to extract and consolidate obligations, and writes consolidated JSON (and page-wise JSON) to the output folder. Uses the first PDF found when using the default flow.

**Request**

- **Method:** POST  
- **Content-Type:** `application/json`

**Request body (schema):**

```json
{
  "docs_folder": "string",    // optional; default from env DOCS_FOLDER (e.g. "docs"). Folder containing PDFs.
  "output_folder": "string"   // optional; default from env OUTPUT_FOLDER (e.g. "output"). Folder for JSON output.
}
```

**Example request body:**

```json
{
  "docs_folder": "docs",
  "output_folder": "output"
}
```

**Response (schema):**

```json
{
  "status": "string",              // "success" | "error"
  "message": "string",             // human-readable message
  "document_name": "string",       // optional; name of processed PDF
  "output_path": "string",         // optional; path to consolidated JSON file
  "total_pages": 0,                // optional; number of pages
  "total_obligations": 0,          // optional; obligations before consolidation
  "consolidated_obligations": 0,   // optional; obligations after consolidation
  "error": "string"                // optional; present when status is "error"
}
```

**Example:** `POST http://localhost:8000/process` with body as above.

**Errors:** No PDFs in docs folder returns `status: "error"` with message/error; other failures also return `status: "error"` with `error` set.

---

## 5. GET `/documents` — List documents

Returns all consolidated JSON documents loaded from the output folder, with metadata.

**Request:** No body. No query parameters.

**Response (schema):**

```json
{
  "total_documents": 0,
  "documents": [
    {
      "document_name": "string",   // e.g. source PDF name
      "file_name": "string",       // consolidated JSON filename
      "processed_at": "string",    // ISO timestamp from consolidated JSON
      "total_pages": 0,
      "total_obligations": 0,      // consolidated_obligations_count
      "party_metadata": {}         // parties extracted from document
    }
  ]
}
```

**Example:** `GET http://localhost:8000/documents`

**Errors:** `503` if query system not initialized; `500` on list failure.

---

## Summary

- **GET `/`** — Service info and endpoint list (no request body).  
- **GET `/health`** — Health and document count (no request body).  
- **POST `/query`** — Search obligations; body: `query`, `document_ids`, `save_output`, `output_folder` (all optional).  
- **POST `/process`** — Run PDF → consolidated JSON pipeline; body: `docs_folder`, `output_folder` (optional).  
- **GET `/documents`** — List consolidated documents (no request body).

Use `Content-Type: application/json` for POST bodies. For full request/response examples, use the Swagger UI at `/docs`.
