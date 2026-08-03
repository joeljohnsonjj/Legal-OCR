import type { ReactNode } from 'react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  openCitationSourceInNewTab,
  parseAssistantForCitations,
  parseAssistantLayoutForExtraction,
  type ParsedAssistantStructure,
  shortCitationLabel,
  stripAggregatedCitationFooter,
  stripTrailingSourceCountLines,
} from './citationParse';
import { mergeGeminiCitationExtraction } from './citationRenderMerge';
import { stripEmbeddedCitationObjectsFromAssistantText } from './assistantCitationJson';
import { fetchExtractedCitationsFromAssistant } from '../../services/citationNormalizerApi';
import { formatMessageBody } from './chatFormatting';
import { CHAT_ACCENT_ON_LIGHT, CHAT_TEXT_SECONDARY } from '../../constants/landRecord';

type Props = {
  content: string;
  messageKey: string;
  isStreaming: boolean;
};

function SourceCitationButtons({ cites }: { cites: string[] }) {
  if (!cites.length) return null;
  return (
    <div className="mt-1.5 flex flex-col gap-1">
      <span className="text-[11px] font-semibold tracking-wide" style={{ color: CHAT_TEXT_SECONDARY }}>
        Sources
      </span>
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Citation sources">
        {cites.map((raw, i) => (
          <button
            key={i}
            type="button"
            onClick={() => openCitationSourceInNewTab(raw)}
            title={shortCitationLabel(raw)}
            className="inline-flex h-7 min-w-[1.75rem] cursor-pointer items-center justify-center rounded-md border border-slate-200/90 bg-white px-2 text-xs font-semibold text-slate-800 shadow-sm transition-colors hover:border-slate-300 hover:bg-slate-50"
            style={{ color: CHAT_ACCENT_ON_LIGHT }}
          >
            {i + 1}
          </button>
        ))}
      </div>
    </div>
  );
}

function renderStructuredMessage(
  parsed: ParsedAssistantStructure,
  messageKey: string,
  opts?: { resolvingNote?: boolean },
): ReactNode {
  const parts: ReactNode[] = [];

  if (parsed.preamble.trim()) {
    parts.push(
      <div key={`${messageKey}-pre`} className="mb-2">
        {formatMessageBody(parsed.preamble, `${messageKey}-pre`, 'assistant')}
      </div>,
    );
  }

  parsed.blocks.forEach((block, bi) => {
    if (!block.body.trim() && !block.cites.length) return;
    const chip = block.cites.length > 0 ? <SourceCitationButtons cites={block.cites} /> : null;
    parts.push(
      <div key={`${messageKey}-blk-${bi}`} className="mb-3 last:mb-0">
        {block.body.trim() ? (
          <>
            <div>{formatMessageBody(block.body, `${messageKey}-blk-${bi}`, 'assistant')}</div>
            {chip}
          </>
        ) : (
          chip
        )}
      </div>,
    );
  });

  const anyBlockCites = parsed.blocks.some((b) => b.cites.length > 0);
  if (parsed.globalCitations.length > 0 && !anyBlockCites) {
    const citesForFooter =
      parsed.blocks.length > 0
        ? [parsed.globalCitations.map((s) => s.trim()).filter(Boolean).join('; ')].filter(Boolean)
        : parsed.globalCitations;
    parts.push(
      <div key={`${messageKey}-global-cites`} className="mb-2">
        <SourceCitationButtons cites={citesForFooter} />
      </div>,
    );
  }

  if (opts?.resolvingNote) {
    parts.push(
      <p
        key={`${messageKey}-resolving`}
        className="mt-1 text-[11px] italic"
        style={{ color: CHAT_TEXT_SECONDARY }}
      >
        Resolving sources…
      </p>,
    );
  }

  return <div className="assistant-cited-content">{parts}</div>;
}

function layoutWantsStructuredView(layout: {
  preamble: string;
  blocksRaw: string[];
  globalTail: string[];
}): boolean {
  return Boolean(
    layout.preamble.trim() ||
      layout.blocksRaw.length > 0 ||
      layout.globalTail.some((s) => s.trim()),
  );
}

function citationNormalizerConfigured(): boolean {
  return Boolean(
    (import.meta.env.VITE_CITATION_NORMALIZER_URL as string | undefined)?.trim() || import.meta.env.DEV,
  );
}

export function AssistantBubbleContent({ content, messageKey, isStreaming }: Props): ReactNode {
  const embedded = useMemo(
    () => stripEmbeddedCitationObjectsFromAssistantText(content.trim()),
    [content],
  );
  const cleaned = useMemo(
    () =>
      embedded.displayText ? stripTrailingSourceCountLines(embedded.displayText) : '',
    [embedded.displayText],
  );
  const layout = useMemo(() => {
    const base = parseAssistantLayoutForExtraction(cleaned);
    return {
      ...base,
      globalTail: [...base.globalTail, ...embedded.sourceLines],
    };
  }, [cleaned, embedded.sourceLines]);
  const regexFallback = useMemo(() => {
    const p = parseAssistantForCitations(cleaned);
    if (!embedded.sourceLines.length) return p;
    return {
      ...p,
      globalCitations: [...p.globalCitations, ...embedded.sourceLines],
    };
  }, [cleaned, embedded.sourceLines]);

  const [geminiParsed, setGeminiParsed] = useState<ParsedAssistantStructure | null>(null);
  const [extracting, setExtracting] = useState(false);
  const reqSeq = useRef(0);

  useLayoutEffect(() => {
    if (isStreaming || !cleaned.trim()) return;
    if (import.meta.env.VITE_AI_CITATIONS === 'false' || !citationNormalizerConfigured()) return;
    setExtracting(true);
  }, [isStreaming, cleaned]);

  useEffect(() => {
    setGeminiParsed(null);
    if (isStreaming || !cleaned.trim()) {
      setExtracting(false);
      return;
    }

    if (import.meta.env.VITE_AI_CITATIONS === 'false' || !citationNormalizerConfigured()) {
      setGeminiParsed(regexFallback);
      setExtracting(false);
      return;
    }

    const seq = ++reqSeq.current;
    const ac = new AbortController();

    void (async () => {
      try {
        const res = await fetchExtractedCitationsFromAssistant(
          {
            preamble: layout.preamble,
            blocks: layout.blocksRaw,
            globalTail: layout.globalTail,
          },
          ac.signal,
        );
        if (ac.signal.aborted || seq !== reqSeq.current) return;
        if (res) {
          setGeminiParsed(mergeGeminiCitationExtraction(layout, res));
        } else {
          setGeminiParsed(regexFallback);
        }
      } finally {
        if (seq === reqSeq.current) setExtracting(false);
      }
    })();

    return () => ac.abort();
  }, [cleaned, layout, isStreaming, regexFallback]);

  if (isStreaming || !content.trim()) {
    return formatMessageBody(content, messageKey, 'assistant');
  }

  const structured = layoutWantsStructuredView(layout);
  if (!structured) {
    return formatMessageBody(stripAggregatedCitationFooter(cleaned), messageKey, 'assistant');
  }

  const aiOn =
    import.meta.env.VITE_AI_CITATIONS !== 'false' && citationNormalizerConfigured();
  const loadingSkeleton: ParsedAssistantStructure | null =
    aiOn && extracting && geminiParsed === null
      ? mergeGeminiCitationExtraction(
          layout,
          { blockCites: [], globalCitations: [] },
          { preferEmptyGlobal: true },
        )
      : null;

  const displayParsed = geminiParsed ?? loadingSkeleton ?? regexFallback;
  const showResolving = loadingSkeleton !== null;

  return renderStructuredMessage(displayParsed, messageKey, { resolvingNote: showResolving });
}
