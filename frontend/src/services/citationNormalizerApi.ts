/**
 * Citation normalizer API (dev: Vite proxies /api/normalize-citations → citation-normalize-server :8001).
 * Set VITE_CITATION_NORMALIZER_URL for production. Gemini runs only on the server.
 */

export type CitationExtractRequest = {
  preamble: string;
  blocks: string[];
  globalTail: string[];
};

export type CitationExtractResponse = {
  blockCites: string[][];
  globalCitations: string[];
};

function normalizerBaseUrl(): string {
  return (
    (import.meta.env.VITE_CITATION_NORMALIZER_URL as string | undefined)?.trim() ||
    (import.meta.env.DEV ? '/api/normalize-citations' : '')
  );
}

/** Legacy: normalize pre-extracted citation strings (same length). */
export async function fetchNormalizedCitationStrings(
  citations: string[],
  signal?: AbortSignal,
): Promise<string[] | null> {
  if (citations.length === 0) return [];
  const base = normalizerBaseUrl();
  if (!base) return null;
  try {
    const res = await fetch(base, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ citations }),
      signal,
    });
    if (!res.ok) return null;
    const data: unknown = await res.json();
    if (!data || typeof data !== 'object') return null;
    const normalized = (data as { normalized?: unknown }).normalized;
    if (!Array.isArray(normalized)) return null;
    if (normalized.length !== citations.length) return null;
    return normalized.map((x) => String(x ?? '').trim());
  } catch {
    return null;
  }
}

/** Full-message citation extraction for Sources chips (Gemini on server). */
export async function fetchExtractedCitationsFromAssistant(
  payload: CitationExtractRequest,
  signal?: AbortSignal,
): Promise<CitationExtractResponse | null> {
  const base = normalizerBaseUrl();
  if (!base) return null;
  try {
    const res = await fetch(base, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        preamble: payload.preamble,
        blocks: payload.blocks,
        globalTail: payload.globalTail,
      }),
      signal,
    });
    if (!res.ok) return null;
    const data: unknown = await res.json();
    if (!data || typeof data !== 'object') return null;
    const blockCites = (data as { blockCites?: unknown }).blockCites;
    const globalCitations = (data as { globalCitations?: unknown }).globalCitations;
    if (!Array.isArray(blockCites) || !Array.isArray(globalCitations)) return null;
    const blockCitesOut = blockCites.map((row) => {
      if (!Array.isArray(row)) return [];
      return row.map((c) => String(c ?? '').trim()).filter(Boolean);
    });
    const globalOut = globalCitations.map((c) => String(c ?? '').trim()).filter(Boolean);
    return { blockCites: blockCitesOut, globalCitations: globalOut };
  } catch {
    return null;
  }
}
