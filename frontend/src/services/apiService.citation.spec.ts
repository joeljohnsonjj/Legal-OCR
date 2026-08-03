import { describe, expect, it } from 'vitest';
import {
  formatCitationItemAsSourceLine,
  normalizeBackendObligation,
  normalizeChatCitationObject,
  parseCitationLocatorString,
} from './apiService';
import { parseCitationForPdf } from '../components/chat/citationParse';

describe('parseCitationLocatorString', () => {
  it('parses financial-prompt style Page + Section with quotes', () => {
    const loc = parseCitationLocatorString(`Page 5, Section 'Indemnification'`);
    expect(loc.pageNumbers).toEqual([5]);
    expect(loc.section).toContain('Indemnification');
  });

  it('extracts pdf from parentheses and pages range', () => {
    const loc = parseCitationLocatorString(`Pages 2-4 (MTNNN.pdf)`);
    expect(loc.pdfHint).toBe('MTNNN.pdf');
    expect(loc.pageNumbers).toEqual([2, 3, 4]);
  });
});

describe('normalizeChatCitationObject with string Citation field', () => {
  it('maps obligation row with string Citation to pages and sections', () => {
    const cit = normalizeChatCitationObject({
      DutyType: 'Rent',
      Citation: `Page 12, Section "Insurance" (Lease.pdf)`,
    });
    expect(cit.pageNumbers).toContain(12);
    expect(cit.section.join(' ')).toMatch(/Insurance/i);
    expect(cit.docId).toMatch(/Lease/i);
  });
});

describe('normalizeBackendObligation string Citation', () => {
  it('wraps string Citation into Citation array', () => {
    const ob = normalizeBackendObligation({
      DutyType: 'Tax',
      'Responsible Party': 'Acme LLC',
      'Owner Responsibility': ['Pay taxes'],
      Reasoning: [],
      Citation: `Page 3, Section 'Property Tax'`,
    });
    expect(ob.Citation.length).toBe(1);
    expect(ob.Citation[0].pageNumbers).toContain(3);
  });
});

describe('formatCitationItemAsSourceLine + parseCitationForPdf', () => {
  it('round-trips Pages: comma list for chips', () => {
    const line = formatCitationItemAsSourceLine({
      docId: 'MTNNN',
      pageNumbers: [1, 3, 5],
      section: ['Base Rent'],
    });
    expect(line).toContain('MTNNN.pdf');
    expect(line).toContain('Pages:');
    const { documentName, pageNumbers } = parseCitationForPdf(line);
    expect(documentName.toLowerCase()).toContain('mtnnn');
    expect(pageNumbers).toEqual([1, 3, 5]);
  });
});
