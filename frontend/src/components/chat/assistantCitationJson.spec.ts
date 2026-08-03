import { describe, expect, it } from 'vitest';
import { sliceBalancedJsonArray, stripEmbeddedCitationObjectsFromAssistantText } from './assistantCitationJson';

describe('sliceBalancedJsonArray', () => {
  it('extracts a simple array', () => {
    const s = 'x [1,2,3] y';
    const open = s.indexOf('[');
    const r = sliceBalancedJsonArray(s, open);
    expect(r?.json).toBe('[1,2,3]');
    expect(r?.endExclusive).toBe(s.indexOf(']') + 1);
  });
});

describe('stripEmbeddedCitationObjectsFromAssistantText', () => {
  it('parses Citation array with string pageNumbers and section', () => {
    const raw = `1. Do something

Citation: [{"docId":"MTNNN.pdf","pageNumbers":"4,5,6","section":"6,5.rent,Paragraph discussing maintenance"}]

More text.`;
    const { displayText, sourceLines } = stripEmbeddedCitationObjectsFromAssistantText(raw);
    expect(sourceLines.length).toBe(1);
    expect(sourceLines[0]).toContain('MTNNN.pdf');
    expect(sourceLines[0]).toContain('4, 5, 6');
    expect(sourceLines[0]).toContain('Sections:');
    expect(displayText).not.toContain('Citation:');
    expect(displayText).toContain('1. Do something');
  });

  it('accepts lowercase citations key', () => {
    const raw = `Intro\ncitations: [{"docId":"A.pdf","pageNumbers":"2","section":"s1"}]`;
    const { sourceLines } = stripEmbeddedCitationObjectsFromAssistantText(raw);
    expect(sourceLines[0]).toContain('A.pdf');
  });
});
