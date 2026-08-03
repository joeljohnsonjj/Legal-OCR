import { describe, expect, it } from 'vitest';
import {
  expandLeadingPdfSemicolonPageCitations,
  parseAssistantForCitations,
  parseCitationForPdf,
  shortCitationLabel,
} from './citationParse';

const AGGREGATE_TAIL =
  "MTNNN.pdf, Page 1, Section 1(d) Base Rent; Page 3, Section 4(a) 'Payment of Rent'; Page 5, Section 8 'Operating Costs'; Page 14, Section 21.a 'Failure To Pay'; Page 16, Section '25. HOLDOVER'";

describe('expandLeadingPdfSemicolonPageCitations', () => {
  it('splits one leading .pdf plus semicolon-separated Page N segments into Document lines', () => {
    const out = expandLeadingPdfSemicolonPageCitations(AGGREGATE_TAIL);
    expect(out).not.toBeNull();
    expect(out).toHaveLength(5);
    expect(out![0]).toBe('Document: MTNNN.pdf | Page 1, Section 1(d) Base Rent');
    expect(out![1]).toBe(`Document: MTNNN.pdf | Page 3, Section 4(a) 'Payment of Rent'`);
    expect(out![2]).toBe(`Document: MTNNN.pdf | Page 5, Section 8 'Operating Costs'`);
    expect(out![3]).toBe(`Document: MTNNN.pdf | Page 14, Section 21.a 'Failure To Pay'`);
    expect(out![4]).toBe(`Document: MTNNN.pdf | Page 16, Section '25. HOLDOVER'`);
  });

  it('returns null when no leading pdf pattern', () => {
    expect(expandLeadingPdfSemicolonPageCitations('Page 1, Section 2; Page 3')).toBeNull();
  });
});

describe('parseAssistantForCitations (global tail)', () => {
  it('parses trailing Citations: aggregate into five globalCitations', () => {
    const content = `Some answer text.\n\nCitations: ${AGGREGATE_TAIL}`;
    const { globalCitations } = parseAssistantForCitations(content);
    expect(globalCitations).toHaveLength(5);
    expect(globalCitations[0]).toContain('MTNNN.pdf');
    expect(globalCitations[0]).toContain('Page 1');
    expect(globalCitations[4]).toContain(`Section '25. HOLDOVER'`);
  });
});

describe('parseCitationForPdf', () => {
  it('parses document with Pages N-M range (comma-separated tail)', () => {
    const raw = 'MTNNN.pdf, Pages 2-30, Multiple Sections';
    const { documentName, pageNumbers } = parseCitationForPdf(raw);
    expect(documentName).toBe('MTNNN.pdf');
    expect(pageNumbers[0]).toBe(2);
    expect(pageNumbers[pageNumbers.length - 1]).toBe(30);
    expect(pageNumbers.length).toBe(29);
  });

  it('parses Pages: N-M with optional colon', () => {
    const { pageNumbers } = parseCitationForPdf('Lease.pdf Pages: 5-7');
    expect(pageNumbers).toEqual([5, 6, 7]);
  });

  it('caps very large ranges to first page only', () => {
    const { pageNumbers } = parseCitationForPdf('Doc.pdf Page 1-500');
    expect(pageNumbers).toEqual([1]);
  });

  it('parses Document: … | Page N from expanded aggregate line', () => {
    const line = `Document: MTNNN.pdf | Page 3, Section 4(a) 'Payment of Rent'`;
    const { documentName, pageNumbers } = parseCitationForPdf(line);
    expect(documentName).toBe('MTNNN.pdf');
    expect(pageNumbers).toEqual([3]);
  });
});

describe('shortCitationLabel', () => {
  it('shows pdf stem and page span for Pages N-M', () => {
    const label = shortCitationLabel('MTNNN.pdf, Pages 2-30, Multiple Sections');
    expect(label).toContain('p.2');
    expect(label).toContain('30');
    expect(label.toLowerCase()).toContain('mtnnn');
  });
});
