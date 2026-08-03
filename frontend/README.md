# Legal OCR Chatbot Frontend

React + Vite frontend for legal document analysis, streaming obligation extraction, and citation source chips.

## Prerequisites

- Node.js 18+ (Node 20 recommended)
- npm 9+

## 1) Install

```bash
npm install
```

## 2) Environment Setup

Create a local `.env` in the project root. You can start from `.env.example`.

```bash
copy .env.example .env
```

Key variables:

- `VITE_API_BASE_URL`  
  Base URL for the legal backend API (defaults to `http://localhost:8000` in code).
- `GEMINI_API_KEY`  
  Required only for AI citation extraction/normalization server.
- `GEMINI_CITATION_MODEL`  
  Optional model name (default: `gemini-2.0-flash-lite`).
- `CITATION_NORMALIZE_PORT`  
  Optional port for citation normalizer server (default: `8001`).
- `VITE_CITATION_NORMALIZER_URL`  
  Optional explicit URL for citation normalizer endpoint in non-dev environments.
- `VITE_AI_CITATIONS=false`  
  Optional flag to disable AI citation extraction from the frontend.

## 3) Add PDF Documents

Place lease/legal PDFs under:

`public/docs/`

The document list is auto-generated before `dev` and `build`.

## Running the Project

## Recommended Local Development (full features)

Use three terminals:

1. Backend API (real backend or stub)
2. Citation normalizer server (for AI source chips)
3. Frontend app

### A) Start backend API

Real backend:

```bash
# run your Python/backend service separately
```

or stub backend:

```bash
npm run stub-server
```

### B) Start citation normalizer server

```bash
npm run citation-normalize-server
```

Server details:

- Endpoint: `POST http://localhost:8001/normalize-citations`
- Uses `.env` from project root via `dotenv`
- In dev, Vite proxies `/api/normalize-citations` to this server

### C) Start frontend

```bash
npm run dev
```

This runs on:

- Frontend URL: `http://localhost:3000`

## Production Build

```bash
npm run build
```

Build output directory:

- `build/`

## Tests

Run all tests:

```bash
npm test
```

## Current Runtime/Port Defaults

- Frontend (Vite): `3000`
- Main backend default in frontend code: `http://localhost:8000`
- Citation normalizer server: `8001`

## Important Notes

- `src/generated/documents.ts` is auto-generated; do not edit manually.
- `predev` and `prebuild` automatically run `npm run generate-documents`.
- Citation extraction for source chips is designed to handle mixed citation styles, including:
  - `Document: file.pdf | Page N`
  - `Pages: 1, 3, 5-7`
  - inline/global text like `Page 5, Section 'X'`, `Article 8`, `(Lease.pdf)`, and semicolon-separated tails.

## Useful Scripts

- `npm run dev` - start frontend in dev mode
- `npm run build` - create production build
- `npm run stub-server` - run mock backend server
- `npm run citation-normalize-server` - run Gemini-powered citation extraction server
- `npm run generate-documents` - regenerate `src/generated/documents.ts`
- `npm test` - run Vitest suite
