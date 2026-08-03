# Streaming Implementation Documentation

## Overview

AI Search uses **`POST /query/stream/raw-http`**: the backend returns **`Content-Type: text/plain`** with progress lines, optional injected `[N tokens]` markers, then merge JSON (often a draft chunk followed by **`[COMPLETE]`** and a reconciled object). The client reads **`ReadableStream`** chunks, can **incrementally** scan each growing buffer for complete obligation JSON objects (`extractStreamingObligationsFromRawBuffer`) to update snippet rows before the stream ends, then runs **`parseRawQueryStreamPlainText`** on the full buffer for the **authoritative** final list. The stub serves the same **`text/plain`** response on **`/query/stream/raw-http`** and **`/query/stream/raw`**. The stub still exposes **`POST /query/stream`** (NDJSON) for manual testing only; the app does not call it.

## Architecture

### 1. Backend Stub Server (`server/stub-ocr-server.js`)

The stub server provides:

#### **POST /query** (Non-Streaming)
- Returns all results at once in a single JSON response

#### **POST /query/stream/raw-http** (used by the app)

Same **`text/plain`** contract as legacy **`POST /query/stream/raw`** when the Python app exposes both. The local stub **chunks** the response body with a short delay so the browser exercises the same incremental **`onTextChunk`** path as a real streamed HTTP body (obligations appear as each complete nested obligation object closes in the JSON).

- Returns **text/plain**: step lines (`[QUERY]`, `[STEP …]`), separator blocks, then **pretty-printed JSON** matching `/query`, then a `[COMPLETE]` tail.
- Parsed by `parseRawQueryStreamPlainText` in `apiService.ts`.

#### **POST /query/stream** (NDJSON, legacy / manual)
- Still available on the stub for Postman or older experiments; **not** used by `App.tsx`.

### Text/plain merge extraction

1. Remove injected lines matching `\n[123 tokens]\n` (Python stream progress).
2. Drop everything after `\n[COMPLETE]` if present.
3. Scan the buffer for JSON objects with brace-aware string handling; keep the **last** object that looks like a merge payload (`results` array or nested `results.results`, or `query` + `results`).
4. **`normalizeQueryEnvelope`** → flat **`BackendObligation[]`** (same as **`queryObligations`**).

### 2. Frontend API Service (`src/services/apiService.ts`)

#### **`parseRawQueryStreamPlainText(fullText: string)`**

Exported for tests. Returns **`BackendQueryResponse`**.

#### **`queryObligationsStreamRaw(query, documentIds?, options?)`**

- **`fetch`** `POST ${API_BASE_URL}/query/stream/raw-http` with the same JSON body as **`/query`** (`query`, optional **`document_ids`**, optional **`save_output`**).
- **`Accept: text/plain, */*`** (does not assume NDJSON).
- Reads **`response.body`** with **`getReader()`**, decodes with **`TextDecoder`**, calls **`onTextChunk(accumulated, chunk)`** on each chunk so the UI can refresh (see incremental extraction below).
- On completion: **`parseRawQueryStreamPlainText(buffer)`** (final merge JSON wins over any draft objects in the buffer).

### 3. Frontend Integration (`src/App.tsx`)

`handleGlobalSearch` passes **`onTextChunk`** to **`queryObligationsStreamRaw`**. Each chunk runs **`extractStreamingObligationsFromRawBuffer`** → **`setSnippets`** immediately (no deferred `requestAnimationFrame` in `App`, because cancelling that rAF after `await` had been dropping the last flush). **`queryObligationsStreamRaw`** also **`await`s one animation frame** after each chunk when `onTextChunk` is used so the browser can paint between reads. When the reader finishes, **`parseRawQueryStreamPlainText`** runs and **`setSnippets`** is applied from **`data.results`** (source of truth). **`finally`** clears **`isAnalyzing`** and the stream log.

```typescript
const data = await queryObligationsStreamRaw(query || '', documentNames.length ? documentNames : undefined, {
  save_output: false,
  onTextChunk: (accumulated) => applyStreamBufferToUi(accumulated),
});
```

## Key Benefits

### 1. **Same merge contract as `/query`**
- Parsed with **`normalizeQueryEnvelope`** — flat obligations and nested category groups both work.

### 2. **Plain-text stream compatibility**
- Works with **`fetch` + `ReadableStream`**; no need for Angular-style progress hacks.
- Optional **`onTextChunk`** drives incremental snippets and the stream log.

If obligations still appear **all at once**, the client may be receiving **a single large read** (common with reverse proxies or response buffering). Ensure the API flushes chunks over HTTP (e.g. disable nginx buffering with `X-Accel-Buffering: no`, or equivalent) so `reader.read()` returns multiple times while the model streams.

### 3. **Backend alignment**
- Matches Python **`POST /query/stream/raw-http`** (same wire format as **`/query/stream/raw`** where both exist).

### 4. **Error handling**
- **`[ERROR]`** lines and **`response.ok`** checks; parse failures return **`BackendQueryResponse.error`**.

## Testing the Streaming

### 1. Start the Stub Server

```bash
node server/stub-ocr-server.js
```

You should see:

```
Stub OCR server at http://localhost:8000
  - POST /query (non-streaming, returns all results at once)
  - POST /query/stream/raw-http (text/plain: progress + final merge JSON; stub also accepts /query/stream/raw)
  - POST /query/stream (streaming NDJSON, progressive results — legacy)
```

### 2. Start the Frontend

```bash
npm run dev
```

### 3. Test in the UI

1. Create a new agreement
2. Toggle "AI Search" ON
3. Click "AI Search" button
4. While the spinner runs, the client is buffering **`/query/stream/raw-http`** plain text
5. When the merge JSON is extracted, **all** obligation snippets appear together

### 4. Test with cURL (Optional)

```bash
curl -X POST http://localhost:8000/query/stream/raw-http \
  -H "Content-Type: application/json" \
  -d '{"query": "rent payment", "document_ids": ["doc1.pdf"]}' \
  --no-buffer
```

You will see progress text followed by a JSON object (same general shape as **`POST /query`**).

## Stub vs real timing

- **`/query/stream/raw-http`** (and **`/query/stream/raw`** on the stub) respond in **one write** (no artificial delay). The real Python server streams LLM tokens over a longer period while the UI shows **`isAnalyzing`** until the response completes.
- The legacy stub **`/query/stream`** (NDJSON) still uses an interval to emit one obligation per tick if you want to demo progressive NDJSON in Postman only.

## Migration to Real Backend

When ready to use the real backend:

### Step 1: Update API Base URL

In `src/services/apiService.ts`:

```typescript
const API_BASE_URL = 'https://your-backend-url.com'; // Change this
```

### Step 2: Verify Endpoint Path

Make sure the real backend exposes **`POST /query/stream/raw-http`** (or change the path in **`queryObligationsStreamRaw()`** in `apiService.ts`):

```typescript
const url = `${API_BASE_URL}/query/stream/raw-http`;
```

### Step 3: Test

- All frontend code remains the same
- No changes needed in `App.tsx`
- Streaming will work automatically

## Technical Details

### ReadableStream API

The frontend uses the modern **Streams API** to read the response:

```typescript
const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) {
    buffer += decoder.decode(undefined, { stream: false });
    break;
  }
  buffer += decoder.decode(value, { stream: true });
  // Optional: onTextChunk(buffer, chunk) for progress UI
}
const data = parseRawQueryStreamPlainText(buffer);
```

**Why this approach?**

- Native browser API (no dependencies)
- Accumulates **text/plain** until the stream ends, then **one JSON parse** of the merge payload
- Strips Python **`[N tokens]`** injections before **`JSON.parse`**

### Error Handling

Connection errors are detected and flagged:

```typescript
const isConnectionError = 
  error instanceof TypeError && error.message.includes('Failed to fetch') ||
  /* ... other checks ... */

(error as any).isConnectionError = true;
```

This allows the UI to show specific messages like "Legal OCR Model is not running!"

## Comparison: Streaming vs Non-Streaming

| Feature | Non-Streaming (`/query`) | Raw text stream (`/query/stream/raw-http`) |
|---------|-------------------------|---------------------------------------------|
| **Response Time** | Wait for full JSON | Wait for full plain-text stream (merge JSON at end) |
| **Format** | Single JSON object | `text/plain`: progress + one merge JSON |
| **UI Update** | One update | One update after parse (spinner while buffering) |
| **Envelope** | `normalizeQueryEnvelope` | Same after extraction |

## Troubleshooting

### AI Search not working?

1. **Check server logs** — Is the backend (or stub) running on **`VITE_API_BASE_URL`**?
2. **Check browser console** — CORS or **`Failed to fetch`**?
3. **Check network tab** — Response should be **`text/plain`** (or compatible) for **`/query/stream/raw-http`**.
4. **Parse errors** — If the model emits invalid JSON, **`parseRawQueryStreamPlainText`** returns **`error`**; confirm the stream includes a complete top-level **`{ ... }`** merge object.

### Error: "Response body is null"

- Some environments omit **`body`**; **`queryObligationsStreamRaw`** falls back to **`response.text()`**.

## Future enhancements

- **`onTextChunk`** — surface `[STEP …]` lines in the UI as a live log.
- **`AbortController`** — cancel in-flight raw streams.
- **Incremental JSON repair** — if the server guarantees a delimiter, parse before EOF.

## Summary

- **`queryObligationsStreamRaw`** calls **`POST /query/stream/raw-http`**, buffers **text/plain**, extracts merge JSON, and **`normalizeQueryEnvelope`** aligns results with **`POST /query`**.
- **`App.tsx`** maps **`data.results`** to obligation snippets; incremental updates use **`onTextChunk`** during the stream.
- The **stub** implements the same handler for **`/query/stream/raw-http`** and **`/query/stream/raw`**; legacy **`/query/stream`** NDJSON remains for manual tests only.
