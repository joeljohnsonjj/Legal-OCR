// API Service for backend integration
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

export interface CitationItem {
  docId: string;
  pageNumbers: number[];
  section: string[];
}

export interface BackendObligation {
  /** Obligation group from consolidated / stream (e.g. "Maintenance & Repairs"). */
  category?: string;
  DutyType: string;
  'Responsible Party': string;
  'Owner Responsibility': string[];
  Reasoning: string[];
  Citation: CitationItem[];
}

export interface BackendQueryResponse {
  query: string;
  total_documents_searched: number;
  total_obligations_found: number;
  total_categories?: number;
  results: BackendObligation[];
  processed_at: string;
  error?: string;
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/** Parse API `pageNumbers` whether string, number, or number[]. */
export function normalizePageNumbers(value: unknown): number[] {
  if (Array.isArray(value)) {
    return value.map((x) => Number(x)).filter((n) => !Number.isNaN(n));
  }
  if (typeof value === 'number' && !Number.isNaN(value)) {
    return [value];
  }
  if (typeof value === 'string') {
    return value
      .split(/[,;\s]+/)
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => !Number.isNaN(n));
  }
  return [];
}

/** Normalize `section` whether string or string[]. */
export function normalizeSectionList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(String).map((s) => s.trim()).filter(Boolean);
  }
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return [];
}

const MAX_LOCATOR_PAGE_EXPAND = 150;

/**
 * Parse free-text obligation / assistant citation locators into pages, section labels, and optional .pdf name.
 * Covers formats from the financial-extraction prompt, e.g.:
 * `Page 5, Section 'Indemnification'`, `Pages 2-4`, `Article 8 — Rent`, trailing `(Lease.pdf)`.
 */
export function parseCitationLocatorString(locator: string): {
  pageNumbers: number[];
  section: string[];
  pdfHint: string;
} {
  const text = (locator || '').replace(/\s+/g, ' ').trim();
  if (!text) {
    return { pageNumbers: [], section: [], pdfHint: '' };
  }

  const pdfMatch = text.match(/\(\s*([\w.-]+\.pdf)\s*\)/i) || text.match(/\b([\w.-]+\.pdf)\b/i);
  const pdfHint = pdfMatch ? pdfMatch[1].trim() : '';

  const pages: number[] = [];

  const pushRange = (lo: number, hi: number) => {
    const a = Math.min(lo, hi);
    const b = Math.max(lo, hi);
    const span = b - a + 1;
    if (span <= MAX_LOCATOR_PAGE_EXPAND) {
      for (let p = a; p <= b; p++) pages.push(p);
    } else {
      pages.push(a);
    }
  };

  const range = text.match(/\bpages?\s*:?\s*(\d+)\s*[-–—]\s*(\d+)\b/i);
  if (range) {
    const lo = parseInt(range[1], 10);
    const hi = parseInt(range[2], 10);
    if (!Number.isNaN(lo) && !Number.isNaN(hi)) pushRange(lo, hi);
  }

  const pagesColon = text.match(/\bPages:\s*([^|]+?)(?:\s*\||$)/i);
  if (pagesColon) {
    for (const part of pagesColon[1].split(/[,;]/)) {
      const t = part.trim();
      if (!t) continue;
      const r = t.match(/^(\d+)\s*[-–—]\s*(\d+)$/);
      if (r) {
        const lo = parseInt(r[1], 10);
        const hi = parseInt(r[2], 10);
        if (!Number.isNaN(lo) && !Number.isNaN(hi)) pushRange(lo, hi);
      } else {
        const n = parseInt(t, 10);
        if (!Number.isNaN(n) && n > 0) pages.push(n);
      }
    }
  }

  for (const m of text.matchAll(/\bPage\s+(\d+)\b/gi)) {
    const n = parseInt(m[1], 10);
    if (!Number.isNaN(n)) pages.push(n);
  }

  const sections: string[] = [];
  for (const m of text.matchAll(/\bSection\s+(['"])([^'"\n]+)\1/gi)) {
    const s = m[2].trim();
    if (s) sections.push(s);
  }
  if (!sections.length) {
    const m = text.match(/\bSection\s+([^|;]+)/i);
    if (m) {
      const s = m[1].trim().replace(/^['"]|['"]$/g, '').trim();
      if (s) sections.push(s);
    }
  }
  const art = text.match(/\bArticle\s+([\d.a-z]+)\b/i);
  if (art) sections.push(`Article ${art[1]}`);
  const noHeading = /no\s+heading\s+provided/i.test(text);
  if (noHeading && !sections.length) sections.push('No heading provided');

  const uniqPages = [...new Set(pages)].sort((a, b) => a - b);
  return { pageNumbers: uniqPages, section: sections, pdfHint };
}

function normalizeCitationItem(raw: unknown): CitationItem {
  if (typeof raw === 'string') {
    const loc = parseCitationLocatorString(raw);
    return {
      docId: loc.pdfHint || 'Unknown Document',
      pageNumbers: loc.pageNumbers,
      section: loc.section,
    };
  }

  const o = asRecord(raw) ?? {};
  const docFromFields = String(o.docId ?? o.document_id ?? o.document ?? o.sourceDocument ?? '').trim();
  const pageNumbers = normalizePageNumbers(o.pageNumbers ?? o.page_numbers);
  const section = normalizeSectionList(o.section ?? o.sections);

  const citField = o.Citation ?? o.citation;
  if (typeof citField === 'string' && citField.trim()) {
    const loc = parseCitationLocatorString(citField.trim());
    const docId =
      docFromFields && docFromFields !== 'Unknown Document'
        ? docFromFields
        : loc.pdfHint || 'Unknown Document';
    return {
      docId,
      pageNumbers: pageNumbers.length ? pageNumbers : loc.pageNumbers,
      section: section.length ? section : loc.section,
    };
  }

  const docId = docFromFields || 'Unknown Document';
  return { docId, pageNumbers, section };
}

/** Normalize a single backend/chat citation object (docId, pageNumbers string|array, section string|array). */
export function normalizeChatCitationObject(raw: unknown): CitationItem {
  return normalizeCitationItem(raw);
}

/** One PDF source line for chat chips / parseCitationForPdf (combined pages + sections). */
export function formatCitationItemAsSourceLine(cit: CitationItem): string {
  let doc = String(cit.docId || '').trim();
  if (doc && doc !== 'Unknown Document' && !/\.pdf$/i.test(doc)) {
    doc = `${doc}.pdf`;
  }
  const pages = cit.pageNumbers.length ? cit.pageNumbers.join(', ') : '';
  const sections = cit.section.length ? cit.section.join(', ') : '';
  return `Document: ${doc} | Pages: ${pages}${sections ? ` | Sections: ${sections}` : ''}`;
}

function deriveDutyType(category: string | undefined, party: string, ownerLines: string[]): string {
  const first = ownerLines.find((s) => s.trim())?.trim() ?? '';
  if (first) {
    const short = first.length > 140 ? `${first.slice(0, 137)}...` : first;
    if (category?.trim()) {
      return `${category.trim()} — ${short}`;
    }
    return short;
  }
  if (category?.trim()) return category.trim();
  if (party.trim()) return party.trim();
  return 'Obligation';
}

function coerceStringArray(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((x) => String(x)).filter((s) => s.length > 0);
  }
  if (value === undefined || value === null) return [];
  return [String(value)];
}

/**
 * Maps current backend shapes (incl. lowercase `citations`, string page/section lists, no `DutyType`)
 * into the canonical {@link BackendObligation} used by the UI.
 */
export function normalizeBackendObligation(
  raw: Record<string, unknown>,
  options?: { category?: string }
): BackendObligation {
  const party = String(
    raw['Responsible Party'] ?? raw['responsible party'] ?? raw.responsibleParty ?? ''
  );
  const ownerResponsibility = coerceStringArray(raw['Owner Responsibility'] ?? raw['owner responsibility']);
  const reasoning = coerceStringArray(raw.Reasoning ?? raw.reasoning);

  const citationsRaw = raw.Citation ?? raw.citations ?? raw.citation;
  let Citation: CitationItem[];
  if (typeof citationsRaw === 'string' && citationsRaw.trim()) {
    Citation = [normalizeCitationItem({ Citation: citationsRaw.trim() })];
  } else if (Array.isArray(citationsRaw)) {
    Citation = citationsRaw.map(normalizeCitationItem);
  } else {
    Citation = [];
  }

  const categoryFromRaw =
    typeof raw.category === 'string' && raw.category.trim() ? raw.category.trim() : undefined;
  const categoryLabel = categoryFromRaw ?? (options?.category?.trim() ? options.category.trim() : undefined);

  const dutyRaw = raw.DutyType ?? raw.dutyType ?? raw.duty_type;
  const DutyType =
    typeof dutyRaw === 'string' && dutyRaw.trim()
      ? dutyRaw.trim()
      : deriveDutyType(categoryLabel, party, ownerResponsibility);

  const base: BackendObligation = {
    DutyType,
    'Responsible Party': party,
    'Owner Responsibility': ownerResponsibility.length ? ownerResponsibility : [''],
    Reasoning: reasoning.length ? reasoning : [''],
    Citation: Citation.length ? Citation : [normalizeCitationItem({})],
  };
  if (categoryLabel) {
    base.category = categoryLabel;
  }
  return base;
}

/**
 * Normalizes `/query` JSON: flat `results[]`, nested `results: { results: [...] }`,
 * or merge-style `results: [ { category, obligations[] }, ... ]` (from LLM merge / stream/raw).
 */
export function normalizeQueryEnvelope(data: unknown): BackendQueryResponse {
  const root = asRecord(data) ?? {};
  const topQuery = String(root.query ?? '');

  if (typeof root.error === 'string' && root.error) {
    return {
      query: topQuery,
      total_documents_searched: 0,
      total_obligations_found: 0,
      results: [],
      processed_at: '',
      error: root.error,
    };
  }

  const inner = root.results;

  // `results` as array: flat obligation rows OR merge payload `{ category, obligations[] }[]`
  if (Array.isArray(inner) && inner.length > 0) {
    const allLookLikeCategoryGroups = inner.every((item) => {
      const r = asRecord(item);
      return Boolean(r && Array.isArray(r.obligations));
    });
    if (allLookLikeCategoryGroups) {
      const flat: BackendObligation[] = [];
      for (const g of inner) {
        const gr = asRecord(g);
        if (!gr) continue;
        const category = String(gr.category ?? '');
        const obligations = gr.obligations as unknown[];
        for (const ob of obligations) {
          flat.push(normalizeBackendObligation(asRecord(ob) ?? {}, { category }));
        }
      }
      return {
        query: topQuery,
        total_documents_searched: Number(root.total_documents_searched ?? 0),
        total_obligations_found: Number(root.total_obligations_found ?? flat.length),
        total_categories:
          root.total_categories !== undefined && root.total_categories !== null
            ? Number(root.total_categories)
            : undefined,
        results: flat,
        processed_at: String(root.processed_at ?? ''),
      };
    }
  }

  // Legacy: `results` is a flat array of obligations
  if (Array.isArray(inner)) {
    const results = inner.map((item) => normalizeBackendObligation(asRecord(item) ?? {}));
    return {
      query: topQuery,
      total_documents_searched: Number(root.total_documents_searched ?? 0),
      total_obligations_found: Number(root.total_obligations_found ?? results.length),
      results,
      processed_at: String(root.processed_at ?? ''),
    };
  }

  // New: `results` is an object with nested category groups in `results.results`
  const nest = asRecord(inner);
  if (nest) {
    const nestedGroups = nest.results;
    const flat: BackendObligation[] = [];
    if (Array.isArray(nestedGroups)) {
      for (const g of nestedGroups) {
        const gr = asRecord(g);
        if (!gr) continue;
        const category = String(gr.category ?? '');
        const obligations = gr.obligations;
        if (!Array.isArray(obligations)) continue;
        for (const ob of obligations) {
          flat.push(normalizeBackendObligation(asRecord(ob) ?? {}, { category }));
        }
      }
    }
    return {
      query: String(nest.query ?? topQuery),
      total_documents_searched: Number(nest.total_documents_searched ?? 0),
      total_obligations_found: Number(nest.total_obligations_found ?? flat.length),
      total_categories:
        nest.total_categories !== undefined && nest.total_categories !== null
          ? Number(nest.total_categories)
          : undefined,
      results: flat,
      processed_at: String(nest.processed_at ?? ''),
    };
  }

  return {
    query: topQuery,
    total_documents_searched: 0,
    total_obligations_found: 0,
    results: [],
    processed_at: '',
  };
}

/**
 * Query legal obligations from the backend
 * @param query - Search query string (keywords from the search bar, can be empty)
 * @param documentIds - Array of document names/IDs to filter by (optional)
 * @returns Promise with query results
 */
export async function queryObligations(query: string, documentIds?: string[]): Promise<BackendQueryResponse> {
  try {
    // #region agent log
    console.log('[DEBUG apiService] queryObligations called', {query, documentIds, apiBaseUrl: API_BASE_URL});
    fetch('http://127.0.0.1:7242/ingest/c69e181c-4485-4aaa-8fb9-54a919c8d97a',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'apiService.ts:33',message:'queryObligations called',data:{query,documentIds,apiBaseUrl:API_BASE_URL},timestamp:Date.now(),sessionId:'debug-session',hypothesisId:'H1'})}).catch(()=>{});
    // #endregion
    
    const requestBody: {
      query: string;
      document_ids?: string[];
    } = {
      query: query || '',
    };
    
    // Include document_ids only if provided and not empty
    if (documentIds && documentIds.length > 0) {
      requestBody.document_ids = documentIds;
    }
    
    const url = `${API_BASE_URL}/query`;
    
    // #region agent log
    console.log('[DEBUG apiService] Making POST request', {url, body: requestBody});
    fetch('http://127.0.0.1:7242/ingest/c69e181c-4485-4aaa-8fb9-54a919c8d97a',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'apiService.ts:36',message:'Making POST request',data:{url,body:requestBody},timestamp:Date.now(),sessionId:'debug-session',hypothesisId:'H1'})}).catch(()=>{});
    // #endregion
    
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(requestBody),
    });
    
    // #region agent log
    console.log('[DEBUG apiService] Fetch response received', {status: response.status, ok: response.ok, statusText: response.statusText});
    fetch('http://127.0.0.1:7242/ingest/c69e181c-4485-4aaa-8fb9-54a919c8d97a',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'apiService.ts:38',message:'Fetch response received',data:{status:response.status,ok:response.ok,statusText:response.statusText},timestamp:Date.now(),sessionId:'debug-session',hypothesisId:'H1'})}).catch(()=>{});
    // #endregion
    
    if (!response.ok) {
      // Check if it's a service unavailable error (503) or other server errors that indicate service is down
      const isServiceDown = response.status === 503 || response.status === 502 || response.status === 504;
      const error = new Error(`API request failed with status ${response.status}`);
      (error as any).isConnectionError = isServiceDown;
      throw error;
    }
    
    const data = await response.json();
    
    // #region agent log
    console.log('[DEBUG apiService] JSON parsed successfully', {resultsCount: data.results?.length, totalObligations: data.total_obligations_found, query: data.query});
    fetch('http://127.0.0.1:7242/ingest/c69e181c-4485-4aaa-8fb9-54a919c8d97a',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'apiService.ts:42',message:'JSON parsed successfully',data:{resultsCount:data.results?.length,totalObligations:data.total_obligations_found,query:data.query},timestamp:Date.now(),sessionId:'debug-session',hypothesisId:'H2'})}).catch(()=>{});
    // #endregion
    
    return normalizeQueryEnvelope(data);
  } catch (error) {
    // #region agent log
    console.log('[DEBUG apiService] queryObligations error caught', {error: error instanceof Error ? error.message : String(error), errorName: error?.constructor?.name});
    fetch('http://127.0.0.1:7242/ingest/c69e181c-4485-4aaa-8fb9-54a919c8d97a',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'apiService.ts:44',message:'queryObligations error caught',data:{error:error instanceof Error?error.message:String(error),errorName:error?.constructor?.name},timestamp:Date.now(),sessionId:'debug-session',hypothesisId:'H1'})}).catch(()=>{});
    // #endregion
    
    console.error('Error querying obligations:', error);
    
    // Check if it's a connection error
    const isConnectionError = 
      error instanceof TypeError && error.message.includes('Failed to fetch') ||
      error instanceof TypeError && error.message.includes('NetworkError') ||
      (error instanceof Error && (
        error.message.includes('NetworkError') ||
        error.message.includes('Failed to fetch') ||
        error.message.includes('ERR_NETWORK') ||
        error.message.includes('ERR_INTERNET_DISCONNECTED') ||
        error.message.includes('ERR_CONNECTION_REFUSED')
      ));
    
    // Create a custom error with connection flag
    const enhancedError = error instanceof Error ? error : new Error(String(error));
    (enhancedError as any).isConnectionError = isConnectionError;
    
    throw enhancedError;
  }
}

/** Python /query/stream/raw-http (or legacy /query/stream/raw) may inject these between LLM chunks; they are not valid JSON. */
const RAW_STREAM_TOKEN_COUNT_MARKER = /\n\[\d+ tokens\]\n/g;

/** Lines that sometimes leak into the HTTP body alongside streamed text (break JSON extraction). */
function stripEmbeddedServerLogLines(s: string): string {
  return (
    s
      // Python logging: "2026-05-21 12:06:25,541 - httpx - INFO - ..."
      .replace(
        /^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2},\d+\s+-\s+[\w.-]+\s+-\s+(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\s+-.*$/gim,
        ''
      )
      // tqdm / transformers style progress
      .replace(/^Batches:\s*.+$/gim, '')
      // Uvicorn / FastAPI access log (sometimes mixed into captured stream text)
      .replace(/^INFO:\s+[\d.:]+\s+-\s+"[^"]+"\s+\d{3}\s+OK\s*$/gim, '')
  );
}

function findMatchingJsonObjectEnd(s: string, start: number): number {
  if (s[start] !== '{') return -1;
  let depth = 1;
  let inStr = false;
  let escape = false;
  for (let i = start + 1; i < s.length; i++) {
    const c = s[i];
    if (inStr) {
      if (escape) {
        escape = false;
        continue;
      }
      if (c === '\\') {
        escape = true;
        continue;
      }
      if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') {
      inStr = true;
      continue;
    }
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

function looksLikeMergePayload(obj: unknown): boolean {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
  const o = obj as Record<string, unknown>;
  if (Array.isArray(o.results)) return true;
  const nested = asRecord(o.results);
  if (nested && Array.isArray(nested.results)) return true;
  if (typeof o.query === 'string' && o.results !== undefined) return true;
  return false;
}

/** Stable key for deduping the same obligation when it appears in draft vs reconciled merge JSON. */
function obligationFingerprintForDedup(ob: BackendObligation): string {
  const owner = (ob['Owner Responsibility'] ?? []).join('\u001e');
  const reasoning = (ob.Reasoning ?? []).join('\u001e');
  const cit = (ob.Citation ?? [])
    .map((c) => `${c.docId}:${c.pageNumbers.join(',')}:${c.section.join(',')}`)
    .join('|');
  return `${ob['Responsible Party']}\u001f${ob.DutyType}\u001f${owner}\u001f${reasoning}\u001f${cit}`;
}

/**
 * True for a single obligation row inside `results[].obligations[]`, not merge envelope / category group / citation.
 */
function isObligationLikeStreamObject(parsed: unknown): boolean {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
  const r = parsed as Record<string, unknown>;
  const keys = Object.keys(r);

  if (Array.isArray(r.results)) return false;
  if (typeof r.query === 'string' && r.results !== undefined) return false;
  if (typeof r.processed_at === 'string' && r.results !== undefined) return false;
  if ('total_obligations_found' in r || 'total_documents_searched' in r) return false;
  if (typeof r.category === 'string' && Array.isArray(r.obligations)) return false;

  if (
    keys.length > 0 &&
    keys.every((k) =>
      ['docId', 'document_id', 'pageNumbers', 'page_numbers', 'section', 'sections'].includes(k)
    )
  ) {
    return false;
  }

  const hasParty =
    r['Responsible Party'] != null || r['responsible party'] != null || r.responsibleParty != null;
  const hasOwner = r['Owner Responsibility'] != null || r['owner responsibility'] != null;
  const hasReason = r.Reasoning != null || r.reasoning != null;
  const hasDuty = r.DutyType != null || r.dutyType != null || r.duty_type != null;
  const hasCitation = r.Citation != null || r.citations != null || r.citation != null;

  if (hasCitation && !hasParty && !hasOwner && !hasReason && !hasDuty) return false;
  return hasParty || hasOwner || hasReason || hasDuty;
}

/**
 * While `/query/stream/raw-http` is still receiving bytes, scan the buffer for **complete** brace-balanced
 * JSON objects that look like obligation rows. Each obligation becomes visible in the UI as soon as its
 * closing `}` arrives (same order as tokens in the terminal), without waiting for the full merge object.
 * Draft vs reconciled duplicates are deduped (later span in the buffer wins).
 */
export function extractStreamingObligationsFromRawBuffer(buffer: string): BackendObligation[] {
  const work = stripEmbeddedServerLogLines(buffer.replace(RAW_STREAM_TOKEN_COUNT_MARKER, '\n'));
  const hits: { start: number; end: number; obligation: BackendObligation }[] = [];

  for (let i = 0; i < work.length; i++) {
    if (work[i] !== '{') continue;
    const end = findMatchingJsonObjectEnd(work, i);
    if (end === -1) continue;
    const slice = work.slice(i, end + 1);
    let parsed: unknown;
    try {
      parsed = JSON.parse(slice);
    } catch {
      continue;
    }
    if (!isObligationLikeStreamObject(parsed)) continue;
    const obligation = normalizeBackendObligation(asRecord(parsed) ?? {}, {});
    hits.push({ start: i, end, obligation });
  }

  const byFp = new Map<string, { start: number; end: number; obligation: BackendObligation }>();
  for (const h of hits) {
    const fp = obligationFingerprintForDedup(h.obligation);
    const prev = byFp.get(fp);
    if (!prev || h.end > prev.end || (h.end === prev.end && h.start >= prev.start)) {
      byFp.set(fp, { start: h.start, end: h.end, obligation: h.obligation });
    }
  }

  return [...byFp.values()].sort((a, b) => a.start - b.start).map((h) => h.obligation);
}

/**
 * Short tail of human-readable stream lines (mirrors backend terminal noise without huge JSON lines).
 */
export function extractProgressLinesFromRawStream(buffer: string, maxLines = 24): string {
  const cleaned = stripEmbeddedServerLogLines(buffer.replace(RAW_STREAM_TOKEN_COUNT_MARKER, '\n'));
  const lines = cleaned.split(/\r?\n/);
  const picked: string[] = [];
  for (let i = lines.length - 1; i >= 0 && picked.length < maxLines; i--) {
    const raw = lines[i];
    const t = raw.trim();
    if (!t) continue;
    if (t.length > 220) continue;
    if (/^\s*"[^"]+"\s*:/.test(raw)) continue;
    if (/^[{}\[\]],?$/.test(t)) continue;
    picked.push(t);
  }
  return picked.reverse().join('\n');
}

/**
 * Parse the final merge JSON from the text/plain body of POST /query/stream/raw-http.
 * - Strips injected "[N tokens]" lines and embedded Python/httpx log lines.
 * - Scans the **full** response (do not truncate at `[COMPLETE]`; some backends emit the
 *   reconciled merge JSON **after** the `[COMPLETE]` line).
 * - Finds brace-balanced `{...}` slices, keeps the **last** one that looks like a /query merge envelope.
 */
export function parseRawQueryStreamPlainText(fullText: string): BackendQueryResponse {
  const text = fullText.trim();
  if (!text) {
    return normalizeQueryEnvelope({});
  }

  const errLine = text.match(/^\[ERROR\]\s*(.*)$/m);
  if (errLine && !text.includes('{')) {
    return {
      query: '',
      total_documents_searched: 0,
      total_obligations_found: 0,
      results: [],
      processed_at: '',
      error: (errLine[1] ?? '').trim() || 'Stream reported an error.',
    };
  }

  const withoutTokenCounts = text.replace(RAW_STREAM_TOKEN_COUNT_MARKER, '\n');
  const work = stripEmbeddedServerLogLines(withoutTokenCounts).trim();

  let lastMerge: unknown | null = null;
  for (let i = 0; i < work.length; i++) {
    if (work[i] !== '{') continue;
    const end = findMatchingJsonObjectEnd(work, i);
    if (end === -1) continue;
    const slice = work.slice(i, end + 1);
    try {
      const parsed: unknown = JSON.parse(slice);
      if (looksLikeMergePayload(parsed)) {
        lastMerge = parsed;
      }
    } catch {
      continue;
    }
  }

  if (lastMerge !== null) {
    return normalizeQueryEnvelope(lastMerge);
  }

  // Fallback: whole buffer is a single JSON object (no progress prefix)
  try {
    return normalizeQueryEnvelope(JSON.parse(work));
  } catch {
    // Last resort: segment after final [COMPLETE] (some pipelines only emit JSON there)
    const completeParts = withoutTokenCounts.split(/\r?\n\[COMPLETE\]/);
    const tailRaw = completeParts[completeParts.length - 1]?.trim() ?? '';
    const tail = stripEmbeddedServerLogLines(tailRaw).trim();
    if (tail && tail !== work) {
      for (let i = 0; i < tail.length; i++) {
        if (tail[i] !== '{') continue;
        const end = findMatchingJsonObjectEnd(tail, i);
        if (end === -1) continue;
        try {
          const parsed: unknown = JSON.parse(tail.slice(i, end + 1));
          if (looksLikeMergePayload(parsed)) {
            return normalizeQueryEnvelope(parsed);
          }
        } catch {
          continue;
        }
      }
    }

    const errMsg = text.match(/\[ERROR\]\s*([^\n]+)/);
    return {
      query: '',
      total_documents_searched: 0,
      total_obligations_found: 0,
      results: [],
      processed_at: '',
      error:
        errMsg?.[1]?.trim() ||
        'Could not parse merge JSON from raw stream response. Ensure the stream completed successfully.',
    };
  }
}

export interface QueryObligationsStreamRawOptions {
  save_output?: boolean;
  /**
   * Called after each decoded chunk with the full accumulated text (optional: progress UI / debug log).
   */
  onTextChunk?: (accumulated: string, chunk: string) => void;
  /**
   * When `onTextChunk` is set, await one animation frame after each chunk (and after the final flush) so
   * the browser can paint incremental UI. Set to `false` for maximum read throughput (e.g. tests).
   * @default true when `onTextChunk` is provided
   */
  yieldToUiEachChunk?: boolean;
}

/**
 * POST /query/stream/raw-http — text/plain stream (progress + LLM JSON), same request body as /query.
 * Buffers the full response, extracts the merge JSON, normalizes like POST /query.
 */
export async function queryObligationsStreamRaw(
  query: string,
  documentIds?: string[],
  options?: QueryObligationsStreamRawOptions
): Promise<BackendQueryResponse> {
  const { onTextChunk, save_output = false, yieldToUiEachChunk } = options ?? {};
  const shouldYieldToUi =
    typeof yieldToUiEachChunk === 'boolean' ? yieldToUiEachChunk : Boolean(onTextChunk);

  const yieldForUiPaint = async () => {
    if (!shouldYieldToUi || !onTextChunk) return;
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  };

  const requestBody: {
    query: string;
    document_ids?: string[] | null;
    save_output?: boolean;
  } = {
    query: query || '',
    save_output,
  };

  if (documentIds && documentIds.length > 0) {
    requestBody.document_ids = documentIds;
  }

  const url = `${API_BASE_URL}/query/stream/raw-http`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/plain, */*',
      },
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
      const isServiceDown = response.status === 503 || response.status === 502 || response.status === 504;
      const error = new Error(`API request failed with status ${response.status}`);
      (error as { isConnectionError?: boolean }).isConnectionError = isServiceDown;
      throw error;
    }

    if (!response.body) {
      const text = await response.text();
      return parseRawQueryStreamPlainText(text);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        buffer += decoder.decode(undefined, { stream: false });
        onTextChunk?.(buffer, '');
        await yieldForUiPaint();
        break;
      }
      const chunk = decoder.decode(value, { stream: true });
      buffer += chunk;
      onTextChunk?.(buffer, chunk);
      await yieldForUiPaint();
    }

    return parseRawQueryStreamPlainText(buffer);
  } catch (error) {
    console.error('[query/stream/raw-http] error', {
      message: error instanceof Error ? error.message : String(error),
    });

    const isConnectionError =
      (error instanceof TypeError && error.message.includes('Failed to fetch')) ||
      (error instanceof TypeError && error.message.includes('NetworkError')) ||
      (error instanceof Error &&
        (error.message.includes('NetworkError') ||
          error.message.includes('Failed to fetch') ||
          error.message.includes('ERR_NETWORK') ||
          error.message.includes('ERR_INTERNET_DISCONNECTED') ||
          error.message.includes('ERR_CONNECTION_REFUSED')));

    const enhancedError = error instanceof Error ? error : new Error(String(error));
    (enhancedError as { isConnectionError?: boolean }).isConnectionError = isConnectionError;
    throw enhancedError;
  }
}

/**
 * Transform backend obligation to snippet format
 * @param obligation - Backend obligation object
 * @param index - Index for generating unique ID
 * @returns Snippet object in the format expected by the frontend
 */
export function transformObligationToSnippet(obligation: BackendObligation, index: number) {
  const citations = Array.isArray(obligation.Citation) ? obligation.Citation : [];
  // Extract citation information (Citation is now an array)
  const firstCitation = citations.length > 0 ? citations[0] : null;

  const documentName = firstCitation?.docId || 'Unknown Document';
  const pageNumber =
    firstCitation?.pageNumbers && firstCitation.pageNumbers.length > 0
      ? firstCitation.pageNumbers[0]
      : 1;
  
  // Generate document ID from document name
  const documentId = `doc-${documentName.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  
  // Combine Owner Responsibility array into a single string
  const ownerResponsibility = obligation['Owner Responsibility'].join('; ');
  
  // Combine Reasoning array into a single string
  const reasoning = obligation.Reasoning.join('; ');
  
  // Build citation text from all citations
  const citationText = citations
    .map((cit) => formatCitationItemAsSourceLine(cit))
    .join('\n');

  const categoryLine = obligation.category ? `Category: ${obligation.category}\n\n` : '';
  const fullText = `${categoryLine}${obligation.DutyType}\n\n${obligation['Responsible Party']}\n\nResponsibilities:\n${obligation['Owner Responsibility'].join('\n')}\n\nReasoning:\n${obligation.Reasoning.join('\n')}\n\n${citationText}`;

  // Create page references from citations
  const pageReferences = citations.flatMap((cit) =>
    (cit.pageNumbers.length ? cit.pageNumbers : [1]).map((pageNum) => ({
      page: pageNum,
      fullText: `Page ${pageNum}${cit.section.length > 0 ? ` - ${cit.section.join(', ')}` : ''}`,
      highlights: [],
    }))
  );
  
  // Create highlights for the PDF reference
  const highlights = [
    {
      text: obligation['Responsible Party'],
      field: 'Responsible Party',
      color: 'bg-blue-200'
    },
    {
      text: obligation['Owner Responsibility'][0] || ownerResponsibility.substring(0, 100),
      field: 'Maintenance Owner Responsibility',
      color: 'bg-green-200'
    },
    {
      text: obligation.DutyType,
      field: 'Legal Notes',
      color: 'bg-yellow-200'
    }
  ];
  
  return {
    id: `backend-${index}`,
    documentId: documentId,
    title: obligation.DutyType,
    ...(obligation.category ? { category: obligation.category } : {}),
    pdfReference: {
      page: pageNumber,
      segment: obligation['Responsible Party'],
      fullText: fullText,
      highlights: highlights,
      pageReferences: pageReferences
    },
    fieldMappings: {
      responsibleParty: obligation['Responsible Party'],
      maintenanceOwnerResponsibility: ownerResponsibility,
      maintenanceReasoning: reasoning,
    },
    matchedFields: ['Responsible Party', 'Maintenance Owner Responsibility', 'Legal Notes'],
    status: 'normal',
    confidenceScore: 85, // Default confidence score, can be adjusted based on relevance
    citations, // Preserve all Citation data for PDF navigation
  };
}

// ——— Legal document chat — `/api/v1/chat` ———

export type ChatRole = 'user' | 'assistant';

export interface ChatMessage {
  role: ChatRole;
  content: string;
  timestamp?: string | null;
}

export interface ChatQueryRequest {
  message: string;
  document_id?: string | null;
  top_k?: number;
  user_id?: string | null;
  run_id?: string | null;
}

export interface ChatQueryResponse {
  answer: string;
  route: Record<string, unknown>;
  hit_count: number;
  block_count: number;
  context_was_empty: boolean;
}

export interface ChatResetResponse {
  user_id: string;
  run_id: string;
}

function enhanceFetchError(error: unknown): Error {
  const isConnectionError =
    error instanceof TypeError && error.message.includes('Failed to fetch') ||
    error instanceof TypeError && String(error).includes('NetworkError') ||
    (error instanceof Error &&
      (error.message.includes('NetworkError') ||
        error.message.includes('Failed to fetch') ||
        error.message.includes('ERR_NETWORK') ||
        error.message.includes('ERR_INTERNET_DISCONNECTED') ||
        error.message.includes('ERR_CONNECTION_REFUSED')));

  const enhanced = error instanceof Error ? error : new Error(String(error));
  (enhanced as { isConnectionError?: boolean }).isConnectionError = Boolean(isConnectionError);
  return enhanced;
}

/**
 * POST /chat — conversational Q&A.
 */
export async function chatQuery(request: ChatQueryRequest): Promise<ChatQueryResponse> {
  const url = `${API_BASE_URL}/chat`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const isServiceDown =
        response.status === 503 || response.status === 502 || response.status === 504;
      const err = new Error(`API request failed with status ${response.status}`);
      (err as { isConnectionError?: boolean }).isConnectionError = isServiceDown;
      throw err;
    }

    return (await response.json()) as ChatQueryResponse;
  } catch (error) {
    console.error('Error in chat query:', error);
    throw enhanceFetchError(error);
  }
}

export async function chatQueryStream(
  request: ChatQueryRequest,
  onToken: (chunk: string) => void
): Promise<void> {
  const url = `${API_BASE_URL}/chat/stream`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      const isServiceDown =
        response.status === 503 || response.status === 502 || response.status === 504;
      const err = new Error(`API request failed with status ${response.status}`);
      (err as { isConnectionError?: boolean }).isConnectionError = isServiceDown;
      throw err;
    }

    if (!response.body) {
      throw new Error('Response body is null');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      onToken(decoder.decode(value, { stream: true }));
    }
  } catch (error) {
    console.error('Error in chat stream:', error);
    throw enhanceFetchError(error);
  }
}

export async function chatReset(): Promise<ChatResetResponse> {
  const url = `${API_BASE_URL}/chat/reset`;

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) {
      const isServiceDown =
        response.status === 503 || response.status === 502 || response.status === 504;
      const err = new Error(`API request failed with status ${response.status}`);
      (err as { isConnectionError?: boolean }).isConnectionError = isServiceDown;
      throw err;
    }

    return (await response.json()) as ChatResetResponse;
  } catch (error) {
    console.error('Error resetting chat session:', error);
    throw enhanceFetchError(error);
  }
}
