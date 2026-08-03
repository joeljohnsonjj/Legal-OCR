import { describe, expect, it } from 'vitest';
import type { ParsedAssistantStructure } from './citationParse';
import {
  collectCitationsForNormalizer,
  mergeGeminiCitationExtraction,
  mergeNormalizedCitations,
} from './citationRenderMerge';

describe('citationRenderMerge', () => {
  const sample: ParsedAssistantStructure = {
    preamble: 'Intro',
    blocks: [
      { body: 'One', cites: ['a', 'b'] },
      { body: 'Two', cites: ['c'] },
    ],
    globalCitations: ['g1'],
  };

  it('collects in block then global order', () => {
    expect(collectCitationsForNormalizer(sample)).toEqual(['a', 'b', 'c', 'g1']);
  });

  it('merges normalized strings back in order', () => {
    const merged = mergeNormalizedCitations(sample, ['A', 'B', 'C', 'G']);
    expect(merged.blocks[0]!.cites).toEqual(['A', 'B']);
    expect(merged.blocks[1]!.cites).toEqual(['C']);
    expect(merged.globalCitations).toEqual(['G']);
    expect(merged.preamble).toBe('Intro');
  });

  it('mergeGeminiCitationExtraction maps Gemini cites to blocks', () => {
    const layout = {
      preamble: 'Pre\n- Citation: old',
      blocksRaw: ['1. A\n- Citation: x'],
      globalTail: ['tail'],
    };
    const merged = mergeGeminiCitationExtraction(layout, {
      blockCites: [['Document: L.pdf | Page 1']],
      globalCitations: ['Document: L.pdf | Page 2'],
    });
    expect(merged.blocks[0]!.cites).toEqual(['Document: L.pdf | Page 1']);
    expect(merged.globalCitations).toEqual(['Document: L.pdf | Page 2']);
    expect(merged.blocks[0]!.body).not.toContain('Citation:');
  });

  it('preferEmptyGlobal skips tail when Gemini returns no globals', () => {
    const layout = { preamble: '', blocksRaw: ['1. Hi'], globalTail: ['tail'] };
    const merged = mergeGeminiCitationExtraction(
      layout,
      { blockCites: [[]], globalCitations: [] },
      { preferEmptyGlobal: true },
    );
    expect(merged.globalCitations).toEqual([]);
  });
});
