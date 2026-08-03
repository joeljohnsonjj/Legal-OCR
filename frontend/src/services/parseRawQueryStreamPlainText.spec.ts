import { describe, expect, it } from 'vitest';
import {
  extractProgressLinesFromRawStream,
  extractStreamingObligationsFromRawBuffer,
  parseRawQueryStreamPlainText,
} from './apiService';

/**
 * Mirrors production /query/stream/raw-http (same body as /query/stream/raw): noisy log lines, incomplete first merge JSON
 * (stream cut off before closing), [COMPLETE], then reconciled JSON with processed_at.
 */
const SAMPLE_LIKE_USER_CAPTURE = `
INFO:     127.0.0.1:53542 - "POST /query/stream/raw HTTP/1.1" 200 OK
2026-05-21 12:37:26,719 - root - INFO - [STREAM/raw] pipeline=POST /query top_k=30
[QUERY] HVAC
[STEP 1] Merge input ready (30 obligation(s) after cap)
2026-05-21 12:37:30,267 - httpx - INFO - HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:streamGenerateContent?alt=sse "HTTP/1.1 200 OK"
{
  "query": "HVAC",
  "total_documents_searched": 1,
  "total_obligations_found": 8,
  "total_categories": 4,
  "results": [
    {
      "category": "Utilities",
      "obligations": [
        {
          "Responsible Party": "landlord",
          "Owner Responsibility": [
            "Provide reasonable heating, ventilation, and air conditioning during all hours of Tenant\\u2019s operation and at other times as reasonably requested"
          ],

================================================================================
[COMPLETE] Generated 33 tokens
================================================================================
2026-05-21 12:37:32,278 - root - INFO - [STREAM/raw] complete tokens=33
{
  "query": "HVAC",
  "total_documents_searched": 1,
  "total_obligations_found": 4,
  "total_categories": 2,
  "results": [
    {
      "category": "Maintenance & Repairs",
      "obligations": [
        {
          "Responsible Party": "landlord",
          "Owner Responsibility": ["Maintain shared HVAC"],
          "Reasoning": ["Envelope"],
          "citations": [{"docId": "MTNNN.pdf", "pageNumbers": "9", "section": "13"}]
        },
        {
          "Responsible Party": "tenant",
          "Owner Responsibility": ["Maintain dedicated HVAC"],
          "Reasoning": ["Interior"],
          "citations": [{"docId": "MTNNN.pdf", "pageNumbers": "9", "section": "13"}]
        }
      ]
    },
    {
      "category": "Utilities",
      "obligations": [
        {
          "Responsible Party": "landlord",
          "Owner Responsibility": ["After-hours HVAC"],
          "Reasoning": ["Service"],
          "citations": [{"docId": "MTNNN.pdf", "pageNumbers": "7", "section": "9"}]
        },
        {
          "Responsible Party": "landlord",
          "Owner Responsibility": ["Provide HVAC during tenant hours"],
          "Reasoning": ["Base services"],
          "citations": [{"docId": "Commercial_Triple_Net_Lease 4.pdf", "pageNumbers": [4], "section": ["6. LANDLORD'S SERVICES"]}]
        }
      ]
    }
  ],
  "processed_at": "2026-05-21T12:37:33.019902"
}
2026-05-21 12:37:33,028 - root - INFO - [STREAM/raw] emitted reconciled merge JSON obligations=4 categories=2
`;

describe('parseRawQueryStreamPlainText', () => {
  it('picks reconciled merge JSON after [COMPLETE] when first JSON is truncated (user raw stream shape)', () => {
    const out = parseRawQueryStreamPlainText(SAMPLE_LIKE_USER_CAPTURE);

    expect(out.error).toBeUndefined();
    expect(out.query).toBe('HVAC');
    expect(out.total_obligations_found).toBe(4);
    expect(out.results).toHaveLength(4);
    expect(out.results[0].DutyType).toBeDefined();
    expect(out.results[0]['Responsible Party']).toBeTruthy();
  });

  it('strips [N tokens] injections between LLM chunks', () => {
    const raw = `prefix\n[50 tokens]\n{"query":"q","total_documents_searched":0,"total_obligations_found":0,"results":[],"processed_at":"x"}`;
    const out = parseRawQueryStreamPlainText(raw);
    expect(out.error).toBeUndefined();
    expect(out.results).toHaveLength(0);
  });
});

describe('extractStreamingObligationsFromRawBuffer', () => {
  it('finds the same obligations as the final merge parser on a full raw stream capture', () => {
    const final = parseRawQueryStreamPlainText(SAMPLE_LIKE_USER_CAPTURE);
    const streamed = extractStreamingObligationsFromRawBuffer(SAMPLE_LIKE_USER_CAPTURE);
    expect(final.results).toHaveLength(4);
    expect(streamed).toHaveLength(4);
    expect(streamed.map((o) => o['Responsible Party']).sort()).toEqual(
      final.results.map((o) => o['Responsible Party']).sort()
    );
  });

  it('extracts a single obligation from a minimal noisy buffer', () => {
    const raw = `
[QUERY] roof
[STEP 1] ok
{"Responsible Party":"landlord","Owner Responsibility":["Maintain roof"],"Reasoning":["See lease"],"citations":[{"docId":"L.pdf","pageNumbers":"3","section":"5"}]}
`;
    const streamed = extractStreamingObligationsFromRawBuffer(raw);
    expect(streamed).toHaveLength(1);
    expect(streamed[0]['Responsible Party']).toBe('landlord');
  });

  it('accumulates obligations as a nested merge JSON grows (simulated stream)', () => {
    const prefix =
      '{"query":"q","total_documents_searched":1,"total_obligations_found":2,"results":[{"category":"C","obligations":[';
    const ob1 =
      '{"Responsible Party":"landlord","Owner Responsibility":["a"],"Reasoning":["r"],"citations":[{"docId":"x.pdf","pageNumbers":"1","section":"s"}]}';
    const ob2 =
      ',{"Responsible Party":"tenant","Owner Responsibility":["b"],"Reasoning":["t"],"citations":[{"docId":"y.pdf","pageNumbers":"2","section":"u"}]}]}],"processed_at":"now"}';

    expect(extractStreamingObligationsFromRawBuffer(prefix).length).toBe(0);
    expect(extractStreamingObligationsFromRawBuffer(prefix + ob1).length).toBe(1);
    expect(extractStreamingObligationsFromRawBuffer(prefix + ob1 + ob2).length).toBe(2);
  });

  it('does not treat merge envelope or citation-only objects as obligations', () => {
    const raw = `{"query":"q","total_documents_searched":1,"total_obligations_found":0,"results":[],"processed_at":"x"}
{"docId":"a.pdf","pageNumbers":[1],"section":["s"]}`;
    const streamed = extractStreamingObligationsFromRawBuffer(raw);
    expect(streamed).toHaveLength(0);
  });
});

describe('extractProgressLinesFromRawStream', () => {
  it('keeps short progress-style lines and drops long JSON-looking lines', () => {
    const raw = `[QUERY] HVAC\n[STEP 1] Merge input ready\n  "obligations": [\n`;
    const lines = extractProgressLinesFromRawStream(raw, 10);
    expect(lines).toContain('[QUERY] HVAC');
    expect(lines).toContain('[STEP 1] Merge input ready');
    expect(lines).not.toMatch(/"obligations"/);
  });
});
