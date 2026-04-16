import React from 'react';
import { ChevronLeft, ChevronRight, Check } from 'lucide-react';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from './ui/tooltip';

interface Highlight {
  text: string;
  field: string;
  color: string;
}

interface PDFReference {
  page: number;
  segment: string;
  context?: string;
  fullText?: string;
  highlights?: Highlight[];
}

interface Snippet {
  id: string;
  title: string;
  pdfReference: PDFReference;
  fieldMappings: Record<string, string>;
  matchedFields: string[];
}

interface SnippetCarouselProps {
  snippets: Snippet[];
  currentIndex: number;
  onNext: () => void;
  onPrevious: () => void;
  onApply: (snippet: Snippet) => void;
  onClose: () => void;
}

export function SnippetCarousel({
  snippets,
  currentIndex,
  onNext,
  onPrevious,
  onApply,
  onClose,
}: SnippetCarouselProps) {
  if (snippets.length === 0) return null;

  const currentSnippet = snippets[currentIndex];

  // Function to render text with highlights
  const renderHighlightedText = (text: string, highlights?: Highlight[]) => {
    if (!highlights || highlights.length === 0) {
      return <span>{text}</span>;
    }

    let lastIndex = 0;
    const parts: React.ReactElement[] = [];

    // Sort highlights by their position in the text
    const sortedHighlights = [...highlights].sort((a, b) => {
      return text.indexOf(a.text) - text.indexOf(b.text);
    });

    sortedHighlights.forEach((highlight, idx) => {
      const highlightIndex = text.indexOf(highlight.text, lastIndex);
      
      if (highlightIndex === -1) return;

      // Add text before highlight
      if (highlightIndex > lastIndex) {
        parts.push(
          <span key={`text-${idx}`}>
            {text.substring(lastIndex, highlightIndex)}
          </span>
        );
      }

      // Add highlighted text with tooltip
      parts.push(
        <TooltipProvider key={`highlight-${idx}`}>
          <Tooltip delayDuration={200}>
            <TooltipTrigger asChild>
              <mark 
                className={`${highlight.color} px-0.5 cursor-help transition-all hover:ring-2 hover:ring-blue-400 hover:ring-offset-1 rounded-sm`}
              >
                {highlight.text}
              </mark>
            </TooltipTrigger>
            <TooltipContent side="top" className="bg-gray-900 text-white px-3 py-2">
              <p className="text-xs">Maps to: <span className="font-semibold">{highlight.field}</span></p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      );

      lastIndex = highlightIndex + highlight.text.length;
    });

    // Add remaining text
    if (lastIndex < text.length) {
      parts.push(
        <span key="text-end">{text.substring(lastIndex)}</span>
      );
    }

    return <>{parts}</>;
  };

  return (
    <div className="absolute bottom-6 left-6 right-6 z-30">
      <div className="bg-white rounded-xl shadow-2xl border border-gray-300 overflow-hidden max-w-3xl">
        {/* Carousel Header */}
        <div className="bg-gradient-to-r from-blue-600 to-teal-600 px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="bg-white/20 text-white text-xs font-medium px-2 py-1 rounded">
              PDF Evidence {currentIndex + 1}/{snippets.length}
            </span>
            <span className="text-white text-sm">
              Page {currentSnippet.pdfReference.page}
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-white hover:bg-white/20 rounded px-2 py-1 text-sm"
          >
            ✕
          </button>
        </div>

        {/* PDF Screenshot Preview */}
        <div className="px-6 py-5">
          <div className="bg-white border-2 border-gray-300 shadow-inner rounded-lg overflow-hidden">
            {/* PDF-like document display */}
            <div className="bg-gray-50 p-6 font-serif" style={{ fontFamily: 'Georgia, "Times New Roman", serif' }}>
              {/* Page number indicator (top right) */}
              <div className="flex justify-end mb-4">
                <span className="text-xs text-gray-400">- {currentSnippet.pdfReference.page} -</span>
              </div>
              
              {/* Document content with highlights */}
              <div className="text-sm leading-relaxed text-gray-800 whitespace-pre-line">
                {currentSnippet.pdfReference.fullText ? (
                  renderHighlightedText(
                    currentSnippet.pdfReference.fullText,
                    currentSnippet.pdfReference.highlights
                  )
                ) : (
                  // Fallback for old format
                  <div>
                    <p className="mb-3">{currentSnippet.pdfReference.context}</p>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Field Mappings Legend */}
          <div className="mt-4 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-600">💡 Maps to:</span>
            {currentSnippet.matchedFields.map((field) => (
              <span
                key={field}
                className="text-xs bg-blue-100 text-blue-700 px-2 py-1 rounded border border-blue-200"
              >
                {field}
              </span>
            ))}
          </div>
        </div>

        {/* Carousel Footer */}
        <div className="bg-gray-50 px-6 py-4 flex items-center justify-between border-t border-gray-200">
          <div className="flex items-center gap-2">
            <button
              onClick={onPrevious}
              disabled={currentIndex === 0}
              className="px-3 py-2 text-sm text-gray-600 hover:bg-gray-200 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
            >
              <ChevronLeft className="w-4 h-4" />
              Prev
            </button>
            <button
              onClick={onNext}
              disabled={currentIndex === snippets.length - 1}
              className="px-3 py-2 text-sm text-gray-600 hover:bg-gray-200 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
            >
              Next
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>

          <button
            onClick={() => onApply(currentSnippet)}
            className="px-6 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors flex items-center gap-2"
          >
            <Check className="w-4 h-4" />
            APPLY TO {currentSnippet.matchedFields.length} FIELD{currentSnippet.matchedFields.length !== 1 ? 'S' : ''}
          </button>
        </div>
      </div>
    </div>
  );
}