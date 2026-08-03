/**
 * Standalone dev server: Gemini citation extraction for chat Sources chips.
 * Loads ../.env when present (GEMINI_API_KEY, GEMINI_CITATION_MODEL).
 *
 * Start: npm run citation-normalize-server
 * POST /normalize-citations — body either { preamble, blocks, globalTail } (extract) or { citations } (legacy).
 */

const http = require('http');
const path = require('path');

try {
  // eslint-disable-next-line global-require, import/no-extraneous-dependencies
  require('dotenv').config({ path: path.join(__dirname, '..', '.env') });
} catch (_) {
  /* optional dependency */
}

const PORT = Number(process.env.CITATION_NORMALIZE_PORT || 8001, 10);

const MAX_BLOCK_CHARS = 28000;

function padBlockCites(blocksLen, rows) {
  const out = Array.isArray(rows) ? rows.map((r) => (Array.isArray(r) ? r : [])) : [];
  while (out.length < blocksLen) out.push([]);
  return out.slice(0, blocksLen).map((row) => row.map((c) => String(c ?? '').trim()).filter(Boolean));
}

function sendJson(res, code, obj) {
  res.setHeader('Content-Type', 'application/json');
  res.writeHead(code);
  res.end(JSON.stringify(obj));
}

async function runGeminiExtract({ preamble, blocks, globalTail }) {
  const key = process.env.GEMINI_API_KEY;
  const blocksSafe = blocks.map((b) => String(b ?? '').slice(0, MAX_BLOCK_CHARS));
  const n = blocksSafe.length;

  if (!key) {
    return {
      blockCites: blocksSafe.map(() => []),
      globalCitations: globalTail.map((s) => String(s ?? '').trim()).filter(Boolean),
    };
  }

  const { GoogleGenerativeAI } = await import('@google/generative-ai');
  const modelName = process.env.GEMINI_CITATION_MODEL || 'gemini-2.0-flash-lite';
  const genAI = new GoogleGenerativeAI(key);
  const model = genAI.getGenerativeModel({
    model: modelName,
    generationConfig: {
      temperature: 0.1,
      responseMimeType: 'application/json',
    },
  });

  const payload = JSON.stringify({
    preamble: String(preamble ?? '').slice(0, MAX_BLOCK_CHARS),
    blocks: blocksSafe,
    globalTail,
  });

  const prompt = `You extract PDF source lines for a legal land-document chat UI (source chips open a file at a page).

Input shape:
- preamble: text before the first numbered "1." / "2." block (if any).
- blocks: array of length ${n}; each item is one numbered responsibility/answer block (may include bullets, JSON fragments, or inline citation text).
- globalTail: zero or more strings from a trailing "Citations:" or "Sources:" footer. May be ONE long string with semicolons, parentheses, or mixed prose. May include lines already shaped like: Document: <file.pdf> | Pages: … | Sections: …

Your job: find every concrete PDF reference and page (or page range) that supports each block or the global/footer. Ignore prose that is not a locator.

Output ONLY valid JSON (no markdown, no fences):
{
  "blockCites": string[][],
  "globalCitations": string[]
}

Hard rules:
- blockCites MUST have exactly ${n} entries (arrays), same order as blocks.
- Each citation line MUST be parseable by a simple viewer. Prefer EXACTLY this pattern when a .pdf filename is known or clearly implied in the same block/footer:
  Document: <filename.pdf> | Page <n>
  OR for a contiguous range only:
  Document: <filename.pdf> | Pages <lo>-<hi>
- If the assistant used plural list form, you may output ONE line:
  Document: <file.pdf> | Pages: <comma-separated pages and/or hyphen ranges>   (example: Pages: 1, 3, 5-7)
- After "Document: … |" you may append section context for humans ONLY as:
  | Sections: <short label>   (omit if nothing reliable)
- When the text names a .pdf once and then only "Page N" / "Pages …" / "Section …" clauses, REPEAT the same Document: filename on each line you emit.
- If only page + section/article text appears (financial-extraction style), still emit Document: when ANY .pdf appears in that block, preamble, or globalTail; otherwise use the filename that is clearly the active lease/record for the whole answer if stated once (e.g. in the first line or footer); if truly unknown use: Document: Unknown.pdf | Page <n>  (only as last resort).

Recognize ALL of these locator styles (non-exhaustive; combine as needed):
- "Page 5, Section 'Indemnification'" / Page 12, Section "Insurance" / "Pages 4-6"
- "Article 8", "ARTICLE V — RENT", "§ 2.3", "clause 7", "Paragraph discussing maintenance costs"
- "No heading provided" with page numbers
- "MTNNN.pdf, Page 1, Section 1(d) …; Page 3, Section 4(a) …" (one doc, many pages separated by ;)
- "(LeaseName.pdf)" at end of a clause; "see Exhibit A (file.pdf)"
- Structured JSON obligation fields embedded in text: "Citation": "…" with page/section inside the string
- Backend-style: docId / pageNumbers / section fields in JSON or pseudo-JSON

Do NOT invent page numbers or filenames not grounded in the input strings.

Dedupe identical Document+Page lines within each block and within globalCitations.

If a block has no citable PDF+page, use [] for that index.

globalCitations: only whole-answer or footer-only sources; merge duplicate lines; do not duplicate a source that is already fully listed under every block unless it is footer-only.

Input JSON:
${payload}`;

  const result = await model.generateContent(prompt);
  const text = result.response.text();
  const out = JSON.parse(text);
  const blockCites = padBlockCites(n, out.blockCites);
  const globalCitations = Array.isArray(out.globalCitations)
    ? out.globalCitations.map((s) => String(s ?? '').trim()).filter(Boolean)
    : [];
  return { blockCites, globalCitations };
}

async function runGeminiNormalize(citations) {
  const key = process.env.GEMINI_API_KEY;
  if (!key) return citations;

  const { GoogleGenerativeAI } = await import('@google/generative-ai');
  const modelName = process.env.GEMINI_CITATION_MODEL || 'gemini-2.0-flash-lite';
  const genAI = new GoogleGenerativeAI(key);
  const model = genAI.getGenerativeModel({
    model: modelName,
    generationConfig: {
      temperature: 0.1,
      responseMimeType: 'application/json',
    },
  });

  const userPayload = JSON.stringify({ citations });
  const prompt = `You normalize legal land-document citation snippets into one line each for a PDF viewer.

Output ONLY valid JSON (no markdown fences): {"normalized": string[]}
- "normalized" MUST have exactly ${citations.length} entries in the same order as input.

Target format (choose the tightest that fits the input):
- Document: <filename.pdf> | Page <n>
- Document: <filename.pdf> | Pages <lo>-<hi>   (only for a single contiguous range)
- Document: <filename.pdf> | Pages: <list>   (comma-separated pages and/or small ranges, e.g. 1, 3, 5-7)
Optional: | Sections: <short heading or locator>

Recognize the same free-text styles as extraction: "Page N, Section 'Title'", Articles, §, "No heading provided", trailing "(file.pdf)", semicolon-separated page clauses under one filename, and structured docId/pageNumbers/section blobs.

Never drop a citation: if unsure, pass through the input string for that index unchanged.
Never invent page numbers or filenames.

Input:
${userPayload}`;

  const result = await model.generateContent(prompt);
  const text = result.response.text();
  const out = JSON.parse(text);
  if (Array.isArray(out.normalized) && out.normalized.length === citations.length) {
    return out.normalized.map((x) => String(x ?? '').trim());
  }
  return citations;
}

const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method === 'POST' && req.url === '/normalize-citations') {
    const reqStarted = Date.now();
    console.log(
      `[citation-server] ${new Date().toISOString()} POST /normalize-citations (body incoming…)`,
    );
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
    });
    req.on('end', async () => {
      try {
        const parsed = body ? JSON.parse(body) : {};

        if (Array.isArray(parsed.blocks)) {
          const blocks = parsed.blocks.map((x) => String(x ?? ''));
          if (blocks.length > 80) {
            console.warn('[citation-server] extract rejected: too many blocks', blocks.length);
            sendJson(res, 400, { error: 'Too many blocks (max 80)' });
            return;
          }
          const preamble = String(parsed.preamble ?? '');
          const globalTail = Array.isArray(parsed.globalTail)
            ? parsed.globalTail.map((x) => String(x ?? '').trim()).filter(Boolean)
            : [];
          const geminiConfigured = Boolean(process.env.GEMINI_API_KEY);
          console.log(
            `[citation-server] extract mode | blocks=${blocks.length} globalTailItems=${globalTail.length} preambleChars=${preamble.length} gemini=${geminiConfigured ? 'on' : 'off'}`,
          );

          try {
            const out = await runGeminiExtract({ preamble, blocks, globalTail });
            const blockTotal = out.blockCites.reduce((n, row) => n + row.length, 0);
            console.log(
              `[citation-server] extract ok in ${Date.now() - reqStarted}ms | citesPerBlock=[${out.blockCites.map((r) => r.length).join(',')}] global=${out.globalCitations.length}`,
            );
            sendJson(res, 200, out);
          } catch (e) {
            console.warn('[normalize-citations] extract failed:', e?.message || e);
            console.warn(`[citation-server] extract fallback response (${Date.now() - reqStarted}ms)`);
            sendJson(res, 200, {
              blockCites: blocks.map(() => []),
              globalCitations: globalTail,
            });
          }
          return;
        }

        const citations = Array.isArray(parsed.citations)
          ? parsed.citations.map((x) => String(x ?? '').trim())
          : [];
        if (citations.length > 60) {
          console.warn('[citation-server] legacy rejected: too many citations', citations.length);
          sendJson(res, 400, { error: 'Too many citations (max 60)' });
          return;
        }
        console.log(
          `[citation-server] legacy normalize mode | citations=${citations.length} gemini=${process.env.GEMINI_API_KEY ? 'on' : 'off'}`,
        );

        let normalized = citations;
        try {
          normalized = await runGeminiNormalize(citations);
        } catch (geminiErr) {
          console.warn('[normalize-citations] normalize failed, echoing input:', geminiErr?.message || geminiErr);
        }
        console.log(`[citation-server] legacy ok in ${Date.now() - reqStarted}ms`);
        sendJson(res, 200, { normalized });
      } catch (e) {
        console.warn('[citation-server] invalid JSON body', e?.message || e);
        sendJson(res, 400, { error: 'Invalid JSON body' });
      }
    });
    return;
  }

  res.writeHead(404);
  res.end('Not found');
});

server.listen(PORT, () => {
  console.log(`Citation normalize server at http://localhost:${PORT}`);
  console.log(`  POST /normalize-citations`);
  console.log(`    - extract: { preamble, blocks: string[], globalTail: string[] } (Gemini)`);
  console.log(`    - legacy:  { citations: string[] }`);
});
