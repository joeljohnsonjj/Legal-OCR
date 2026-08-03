import type { CitationBlock, ParsedAssistantStructure } from './citationParse';
import { stripCitationLinesFromBlock } from './citationParse';

export type GeminiCitationExtract = {
  blockCites?: unknown;
  globalCitations?: unknown;
};

function asStringList(row: unknown): string[] {
  if (!Array.isArray(row)) return [];
  return row.map((x) => String(x ?? '').trim()).filter(Boolean);
}

/**
 * Build the final parsed structure for the UI: display bodies have citation lines stripped;
 * all `cites` come from Gemini. If `globalCitations` from Gemini is empty, uses `globalTail` from layout.
 */
export function mergeGeminiCitationExtraction(
  layout: { preamble: string; blocksRaw: string[]; globalTail: string[] },
  gemini: GeminiCitationExtract,
  opts?: { preferEmptyGlobal?: boolean },
): ParsedAssistantStructure {
  const rawRows = Array.isArray(gemini.blockCites) ? gemini.blockCites : [];
  const padded: unknown[] = [...rawRows];
  while (padded.length < layout.blocksRaw.length) padded.push([]);
  const rows = padded.slice(0, layout.blocksRaw.length);

  const blocks: CitationBlock[] = layout.blocksRaw.map((raw, i) => {
    const { body } = stripCitationLinesFromBlock(raw);
    return { body, cites: asStringList(rows[i]) };
  });

  const preambleBody = stripCitationLinesFromBlock(layout.preamble).body.trim();
  const globalFromGem = asStringList(gemini.globalCitations);
  const globalCitations =
    globalFromGem.length > 0
      ? globalFromGem
      : opts?.preferEmptyGlobal
        ? []
        : layout.globalTail.map((s) => s.trim()).filter(Boolean);

  return {
    preamble: preambleBody,
    blocks,
    globalCitations,
  };
}

/** Flatten citation strings in the same order the UI renders them (per block, then global). */
export function collectCitationsForNormalizer(parsed: ParsedAssistantStructure): string[] {
  const list: string[] = [];
  for (const b of parsed.blocks) {
    for (const c of b.cites) list.push(c);
  }
  for (const g of parsed.globalCitations) list.push(g);
  return list;
}

/** Replace citation strings with model-normalized lines (same count / order as collect). */
export function mergeNormalizedCitations(
  parsed: ParsedAssistantStructure,
  normalized: string[],
): ParsedAssistantStructure {
  const blocks: CitationBlock[] = parsed.blocks.map((b) => ({
    body: b.body,
    cites: [...b.cites],
  }));
  let idx = 0;
  for (const b of blocks) {
    for (let j = 0; j < b.cites.length; j++) {
      if (idx < normalized.length) b.cites[j] = normalized[idx]!;
      idx++;
    }
  }
  const globalCitations = [...parsed.globalCitations];
  for (let j = 0; j < globalCitations.length; j++) {
    if (idx < normalized.length) globalCitations[j] = normalized[idx]!;
    idx++;
  }
  return {
    preamble: parsed.preamble,
    blocks,
    globalCitations,
  };
}
