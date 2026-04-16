# Streaming Implementation Documentation

## Overview

This document explains the streaming implementation for AI obligation suggestions, which mimics the backend's NDJSON streaming format. The streaming allows obligations to appear progressively in the UI as they're generated, providing better user experience with faster perceived response times.

## Architecture

### 1. Backend Stub Server (`server/stub-ocr-server.js`)

The stub server now provides **two endpoints**:

#### **POST /query** (Non-Streaming)
- Returns all results at once in a single JSON response
- Traditional request-response pattern
- Used for testing or when streaming is not needed

#### **POST /query/stream** (Streaming - NDJSON)
- Streams obligations one-by-one as NDJSON (Newline Delimited JSON)
- Mimics the real backend streaming behavior
- Each line is a complete JSON object followed by `\n`

### NDJSON Stream Format

The server sends three types of events:

```typescript
// 1. Obligation Event (sent for each obligation)
{
  "type": "obligation",
  "data": {
    "DutyType": "Base Rent Payment",
    "Responsible Party": "Tenant",
    "Owner Responsibility": ["Pay monthly rent..."],
    "Reasoning": ["Obligation to pay rent..."],
    "Citation": [{...}]
  }
}

// 2. Metadata Event (sent at the end)
{
  "type": "metadata",
  "data": {
    "query": "rent payment",
    "total_documents_searched": 2,
    "total_obligations_found": 35,
    "processed_at": "2026-02-16T12:00:00Z"
  }
}

// 3. Error Event (sent if an error occurs)
{
  "type": "error",
  "message": "Error description"
}
```

### 2. Frontend API Service (`src/services/apiService.ts`)

#### **New Types**

```typescript
export interface StreamObligationEvent {
  type: 'obligation';
  data: BackendObligation;
}

export interface StreamMetadataEvent {
  type: 'metadata';
  data: {
    query: string;
    total_documents_searched: number;
    total_obligations_found: number;
    processed_at: string;
  };
}

export interface StreamErrorEvent {
  type: 'error';
  message: string;
}

export type StreamEvent = StreamObligationEvent | StreamMetadataEvent | StreamErrorEvent;
```

#### **New Function: `queryObligationsStream()`**

```typescript
export async function queryObligationsStream(
  query: string,
  documentIds?: string[],
  callbacks?: {
    onObligation?: (obligation: BackendObligation, index: number) => void;
    onMetadata?: (metadata: StreamMetadataEvent['data']) => void;
    onError?: (error: string) => void;
    onComplete?: () => void;
  }
): Promise<void>
```

**How it works:**

1. **Sends POST request** to `/query/stream` endpoint
2. **Reads response body** as a stream using `ReadableStream` API
3. **Processes NDJSON** line by line
4. **Invokes callbacks** for each event type:
   - `onObligation`: Called for each obligation as it arrives
   - `onMetadata`: Called when metadata is received
   - `onError`: Called if an error occurs
   - `onComplete`: Called when streaming is complete

### 3. Frontend Integration (`src/App.tsx`)

The `handleToggleAiMode` function now uses streaming:

```typescript
const streamedSnippets: any[] = [];

await queryObligationsStream(
  query || '',
  documentNames.length > 0 ? documentNames : undefined,
  {
    onObligation: (obligation, index) => {
      // Transform to snippet format
      const snippet = transformObligationToSnippet(obligation, index);
      
      // Add confidence score
      const snippetWithConfidence = { ...snippet, confidenceScore: 95 - (index * 5) };
      
      // Accumulate snippets
      streamedSnippets.push(snippetWithConfidence);
      
      // **Progressive UI update** - show obligations as they arrive
      setSnippets([...streamedSnippets]);
    },
    onMetadata: (metadata) => {
      console.log('Streaming complete:', metadata);
    },
    onError: (errorMessage) => {
      alert('Error during streaming: ' + errorMessage);
    },
    onComplete: () => {
      setIsAnalyzing(false); // Hide loading animation
    }
  }
);
```

## Key Benefits

### 1. **Progressive Rendering**
- Obligations appear in the UI as they're generated
- Users see results immediately instead of waiting for all results
- Better perceived performance

### 2. **Real-time Feedback**
- Loading animation shows until streaming completes
- Users can see the AI "thinking" and generating results
- Engaging user experience

### 3. **Easy Backend Integration**
When the real backend is ready, you only need to:
- Change `API_BASE_URL` to point to the real server
- No frontend code changes required
- The streaming format is identical

### 4. **Error Handling**
- Connection errors are detected and reported
- Stream errors are handled gracefully
- User-friendly error messages

## Testing the Streaming

### 1. Start the Stub Server

```bash
node server/stub-ocr-server.js
```

You should see:
```
Stub OCR server at http://localhost:8000
  - POST /query (non-streaming, returns all results at once)
  - POST /query/stream (streaming NDJSON, progressive results)
```

### 2. Start the Frontend

```bash
npm run dev
```

### 3. Test Streaming in the UI

1. Create a new agreement
2. Toggle "AI Search" ON
3. Click "AI Search" button
4. **Watch the snippet carousel** - obligations will appear one by one
5. Each obligation appears ~100ms apart (simulating real streaming)

### 4. Test with cURL (Optional)

```bash
# Test streaming endpoint
curl -X POST http://localhost:8000/query/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "rent payment", "document_ids": ["doc1.pdf"]}' \
  --no-buffer
```

You'll see NDJSON lines appearing progressively.

## Timing Configuration

### Stub Server Timing

In `server/stub-ocr-server.js`, line 866:

```javascript
}, 100); // Send one obligation every 100ms
```

**Adjust this value to simulate different network speeds:**
- `50` - Faster streaming (2 obligations/second)
- `100` - Default (1 obligation/100ms)
- `200` - Slower streaming (0.5 obligations/second)

### Why 100ms?

- **Fast enough** to show progressive rendering
- **Slow enough** to see the streaming effect
- **Realistic** for network conditions
- The real backend will be faster or slower depending on LLM speed

## Migration to Real Backend

When ready to use the real backend:

### Step 1: Update API Base URL

In `src/services/apiService.ts`:

```typescript
const API_BASE_URL = 'https://your-backend-url.com'; // Change this
```

### Step 2: Verify Endpoint Path

Make sure the real backend uses `/query/stream` or update the path in `queryObligationsStream()`:

```typescript
const url = `${API_BASE_URL}/query/stream`; // Update if different
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
  if (done) break;
  
  buffer += decoder.decode(value, { stream: true });
  
  // Process complete lines
  const lines = buffer.split('\n');
  buffer = lines.pop() || ''; // Keep incomplete line
  
  for (const line of lines) {
    const event = JSON.parse(line);
    // Handle event...
  }
}
```

**Why this approach?**
- Native browser API (no dependencies)
- Efficient memory usage
- Handles partial JSON correctly
- Compatible with NDJSON format

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

| Feature | Non-Streaming (`/query`) | Streaming (`/query/stream`) |
|---------|-------------------------|---------------------------|
| **Response Time** | Wait for all obligations | First obligation appears quickly |
| **Format** | Single JSON object | NDJSON (one JSON per line) |
| **UI Update** | One update at end | Progressive updates |
| **User Experience** | Loading spinner | Live results |
| **Memory** | Buffers entire response | Streams piece by piece |
| **Backend Load** | Same | Same (just different delivery) |

## Troubleshooting

### Streaming not working?

1. **Check server logs** - Is the stub server running?
2. **Check browser console** - Are there CORS errors?
3. **Check network tab** - Is the response `application/x-ndjson`?
4. **Check timing** - Try increasing delay in stub server

### Obligations appear all at once?

- The stub server delay might be too fast
- Network might be too fast (local dev)
- Increase delay in `stub-ocr-server.js`

### Error: "Response body is null"

- Server might not be sending streaming response
- Check server is using `res.write()` not `res.end()` for streaming

## Future Enhancements

### 1. **Token-Level Streaming** (like backend's `/query/stream/raw`)
- Stream the LLM's raw text output
- Show JSON being "typed" in real-time
- More immersive experience

### 2. **Progress Indicators**
- Show "3/35 obligations loaded"
- Progress bar based on metadata
- Estimated time remaining

### 3. **Cancellation**
- Add "Cancel" button during streaming
- Use `AbortController` to stop stream
- Clean up resources properly

### 4. **Retry on Error**
- Automatic retry for failed streams
- Exponential backoff
- Resume from last successful obligation

## Summary

The streaming implementation provides:

✅ **Progressive UI updates** - Obligations appear as they're generated  
✅ **Backend compatibility** - Matches real backend's NDJSON format  
✅ **Easy migration** - Just change API URL when ready  
✅ **Better UX** - Faster perceived response times  
✅ **Error handling** - Graceful failure with user-friendly messages  
✅ **Production-ready** - Modern async patterns, efficient streaming  

The stub server accurately mimics the backend's streaming behavior, allowing full frontend development and testing before the real backend integration.
