import { formatCitationItemAsSourceLine, normalizeChatCitationObject } from '../../services/apiService';

/**
 * Extract a top-level JSON array starting at `openIdx` (`[`),
 * respecting quoted strings so nested brackets inside strings are ignored.
 */
export function sliceBalancedJsonArray(s: string, openIdx: number): { json: string; endExclusive: number } | null {
  if (s[openIdx] !== '[') return null;
  let depth = 0;
  let inString: false | '"' | "'" = false;
  let escape = false;
  for (let i = openIdx; i < s.length; i++) {
    const c = s[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (inString) {
      if (c === '\\') {
        escape = true;
        continue;
      }
      if (c === inString) {
        inString = false;
      }
      continue;
    }
    if (c === '"' || c === "'") {
      inString = c;
      continue;
    }
    if (c === '[') depth++;
    else if (c === ']') {
      depth--;
      if (depth === 0) return { json: s.slice(openIdx, i + 1), endExclusive: i + 1 };
    }
  }
  return null;
}

function recordToSourceLine(raw: unknown): string | null {
  try {
    const cit = normalizeChatCitationObject(raw);
    if (!cit.docId || cit.docId === 'Unknown Document') {
      if (!cit.pageNumbers.length && !cit.section.length) return null;
    }
    return formatCitationItemAsSourceLine(cit);
  } catch {
    return null;
  }
}

/**
 * Find `Citation: [...]` or `citations: [...]` in assistant markdown, parse JSON arrays,
 * convert each object to a `Document: … | Pages: …` line, and remove the JSON from display text.
 * Supports structured citation objects and financial-obligation rows where `Citation` is a
 * free-text string (e.g. `Page 5, Section 'Indemnification'`).
 */
export function stripEmbeddedCitationObjectsFromAssistantText(input: string): {
  displayText: string;
  sourceLines: string[];
} {
  const sourceLines: string[] = [];
  const labelRe = /\b(Citation|citations|citation)\s*:\s*\[/gi;
  const spans: { start: number; end: number }[] = [];

  let m: RegExpExecArray | null;
  const str = input;
  while ((m = labelRe.exec(str))) {
    const bracketIdx = str.indexOf('[', m.index);
    if (bracketIdx === -1) continue;
    const sliced = sliceBalancedJsonArray(str, bracketIdx);
    if (!sliced) continue;
    try {
      const arr = JSON.parse(sliced.json) as unknown;
      if (Array.isArray(arr)) {
        for (const el of arr) {
          const line = recordToSourceLine(el);
          if (line) sourceLines.push(line);
        }
        spans.push({ start: m.index, end: sliced.endExclusive });
      }
    } catch {
      /* ignore invalid JSON */
    }
  }

  spans.sort((a, b) => b.start - a.start);
  let displayText = str;
  for (const { start, end } of spans) {
    const before = displayText.slice(0, start).replace(/\s+$/, '');
    const after = displayText.slice(end).replace(/^\s*/, '');
    const join = before && after ? '\n\n' : '';
    displayText = `${before}${join}${after}`;
  }

  return { displayText: displayText.replace(/\n{3,}/g, '\n\n').trim(), sourceLines };
}
