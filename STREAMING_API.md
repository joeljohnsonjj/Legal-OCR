# Streaming query API — frontend integration (Angular)

This document describes how to call the **streaming** obligation query endpoints exposed by `run_api.py` (`query_system:app`, FastAPI + Uvicorn).

## Base URL

| Environment | Example base URL |
|-------------|------------------|
| Local default | `http://127.0.0.1:8000` |
| Custom | Set `API_HOST` / `API_PORT` in `.env` (see `run_api.py`) |

All paths below are relative to this base (e.g. `POST http://127.0.0.1:8000/query/stream`).

---

## Request body (both streaming routes + `POST /query`)

`Content-Type: application/json`

```typescript
interface QueryRequest {
  query?: string;           // search text (may be empty per backend behavior)
  document_ids?: string[] | null;  // optional filter: filenames or URLs matching consolidated docs
  save_output?: boolean;    // default false
  output_folder?: string | null;   // optional override for OUTPUT_FOLDER
}
```

**Example**

```json
{
  "query": "Landlord maintenance HVAC",
  "document_ids": null,
  "save_output": false
}
```

---

## Endpoint 1: `POST /query/stream` (recommended for UI)

| Item | Value |
|------|--------|
| **Method** | `POST` |
| **Path** | `/query/stream` |
| **Response `Content-Type`** | `application/x-ndjson` |
| **Semantics** | **NDJSON**: one JSON object per line (each line ends with `\n`). |

### Line event shapes

Each line is a single JSON object:

1. **`category_group`** — one merged category block (same shape as items in non-streaming `results`).

```json
{"type":"category_group","data":{"category":"...","obligations":[...]}}
```

2. **`metadata`** — sent **after** all category groups for that request.

```json
{
  "type": "metadata",
  "data": {
    "query": "...",
    "total_documents_searched": 0,
    "total_obligations_found": 0,
    "total_categories": 0,
    "processed_at": "2026-05-11T12:00:00"
  }
}
```

3. **`error`** — terminal error for this stream.

```json
{"type":"error","message":"..."}
```

Your UI should **append** each `category_group` to a list, then on **`metadata`** finalize counts / loading state. On **`error`**, show the message and stop.

---

## Endpoint 2: `POST /query/stream/raw` (debug / “typing” effect)

| Item | Value |
|------|--------|
| **Method** | `POST` |
| **Path** | `/query/stream/raw` |
| **Response `Content-Type`** | `text/plain` |
| **Semantics** | Incremental **raw text**: status lines like `[STEP 1] ...`, then **LLM token chunks** of the merge JSON (not line-delimited JSON). |

Use this when you want a live “model is typing” view. For structured obligation rows in the UI, prefer **`/query/stream`** NDJSON.

---

## Why not Angular `HttpClient` alone?

`HttpClient` typically emits the body **once** when the response completes; it does not expose a reliable byte stream API for NDJSON in all versions/setups.

Use the **`fetch` API** + **`ReadableStream`** (or `XMLHttpRequest` + `onprogress`) from a service/injectable, and keep an Angular-friendly pattern with **Observables** or **callbacks**.

---

## Angular integration pattern

### 1. Environment

```typescript
// environment.ts
export const environment = {
  apiBaseUrl: 'http://127.0.0.1:8000',
};
```

### 2. NDJSON stream service (`/query/stream`)

```typescript
import { Injectable, NgZone } from '@angular/core';
import { Observable, Subscriber } from 'rxjs';
import { environment } from '../environments/environment';

export type StreamEvent =
  | { type: 'category_group'; data: Record<string, unknown> }
  | { type: 'metadata'; data: Record<string, unknown> }
  | { type: 'error'; message: string };

export interface QueryRequestBody {
  query?: string;
  document_ids?: string[] | null;
  save_output?: boolean;
  output_folder?: string | null;
}

@Injectable({ providedIn: 'root' })
export class QueryStreamApiService {
  constructor(private readonly zone: NgZone) {}

  /**
   * POST /query/stream — yields parsed NDJSON lines as they arrive.
   */
  streamQuery(body: QueryRequestBody): Observable<StreamEvent> {
    return new Observable<StreamEvent>((sub: Subscriber<StreamEvent>) => {
      const ac = new AbortController();
      const url = `${environment.apiBaseUrl}/query/stream`;

      (async () => {
        try {
          const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/x-ndjson' },
            body: JSON.stringify(body ?? {}),
            signal: ac.signal,
          });

          if (!res.ok || !res.body) {
            sub.error(new Error(`HTTP ${res.status}: ${await res.text().catch(() => res.statusText)}`));
            return;
          }

          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            let idx: number;
            while ((idx = buffer.indexOf('\n')) >= 0) {
              const line = buffer.slice(0, idx).trim();
              buffer = buffer.slice(idx + 1);
              if (!line) continue;

              let evt: StreamEvent;
              try {
                evt = JSON.parse(line) as StreamEvent;
              } catch {
                sub.error(new Error(`Invalid NDJSON line: ${line.slice(0, 200)}`));
                ac.abort();
                return;
              }

              // Run inside NgZone so templates update on each chunk
              this.zone.run(() => {
                sub.next(evt);
                if (evt.type === 'error') {
                  sub.error(new Error(evt.message));
                }
              });
            }
          }

          this.zone.run(() => sub.complete());
        } catch (e) {
          this.zone.run(() => sub.error(e));
        }
      })();

      return () => ac.abort();
    });
  }
}
```

### 3. Component usage

```typescript
import { Component, OnDestroy } from '@angular/core';
import { Subscription } from 'rxjs';
import { QueryStreamApiService, StreamEvent } from './query-stream-api.service';

@Component({ /* ... */ })
export class QueryPanelComponent implements OnDestroy {
  categories: Record<string, unknown>[] = [];
  meta: Record<string, unknown> | null = null;
  loading = false;
  private sub?: Subscription;

  constructor(private readonly api: QueryStreamApiService) {}

  runStream(): void {
    this.loading = true;
    this.categories = [];
    this.meta = null;
    this.sub?.unsubscribe();

    this.sub = this.api
      .streamQuery({ query: 'HVAC maintenance', document_ids: null, save_output: false })
      .subscribe({
        next: (evt: StreamEvent) => {
          if (evt.type === 'category_group') {
            this.categories.push(evt.data);
          } else if (evt.type === 'metadata') {
            this.meta = evt.data;
            this.loading = false;
          }
        },
        error: () => {
          this.loading = false;
        },
        complete: () => {
          this.loading = false;
        },
      });
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe();
  }
}
```

### 4. Optional: raw token stream (`/query/stream/raw`)

Treat the body as **opaque text chunks**; append to a `string` or show in a `<pre>`.

```typescript
async *readRawStream(body: QueryRequestBody): AsyncGenerator<string, void, unknown> {
  const res = await fetch(`${environment.apiBaseUrl}/query/stream/raw`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/plain' },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    yield decoder.decode(value, { stream: true });
  }
}
```

---

## CORS (Angular dev server → FastAPI)

If the Angular app runs on another origin (e.g. `http://localhost:4200`), FastAPI must allow it. In `query_system.py`, ensure `CORSMiddleware` includes your dev origin, or use a **reverse proxy** so browser and API share one origin.

---

## OpenAPI / quick manual test

- Swagger UI: `{apiBaseUrl}/docs` — try **POST `/query/stream`**.
- **curl** (true streaming in terminal):

```bash
curl -N -X POST "http://127.0.0.1:8000/query/stream" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"test\",\"save_output\":false}"
```

---

## If the UI still shows one big update

**Backend**

1. **`pip install ijson`** in the same environment as `run_api.py`. Without it, merge JSON is buffered until the model finishes, so NDJSON lines arrive in one burst.
2. **`GEMINI_STREAM_TEXT_SLICE_CHARS`** (default `128`): the API splits large Gemini `text` parts into smaller chunks so incremental JSON parsing can progress. Lower = more frequent yields (more overhead); try `64` if needed.
3. Restart the API after changing code or `.env`.

**Angular**

1. Use **`fetch` + `ReadableStream`** as in this doc — **`HttpClient`** often buffers the full body.
2. **`ChangeDetectionStrategy.OnPush`**: call **`ChangeDetectorRef.detectChanges()`** (or `markForCheck()`) after each `snippets.push(...)` inside the stream `next` handler if the list does not repaint until the request ends.
3. Confirm you are appending on **`obligation`** (or `category_group`) events, not only reading the body when `complete` fires.

---

## Non-streaming counterpart

| Method | Path | Response |
|--------|------|----------|
| `POST` | `/query` | Single JSON (same merge logic; no NDJSON stream). |

Use `/query` when you do not need incremental UI updates.
