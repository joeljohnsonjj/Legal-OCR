import React, { useRef, useEffect, useState } from 'react';
import { Check, X, FileText, ChevronLeft, ChevronRight, ChevronDown } from 'lucide-react';
import { PDFSnippetViewer } from './PDFSnippetViewer';
import { AIAnalyzingAnimation } from './AIAnalyzingAnimation';

interface Highlight {
  text: string;
  field: string;
  color: string;
}

interface PageReference {
  page: number;
  fullText: string;
  highlights?: Highlight[];
}

interface DocumentReference {
  documentId: string;
  documentName: string;
  fullText: string;
  highlights?: Highlight[];
  pageNumber: number;
  pageReferences?: PageReference[]; // Additional pages with references within this document
}

interface PDFReference {
  page: number;
  segment: string;
  context?: string;
  fullText?: string;
  highlights?: Highlight[];
  pageReferences?: PageReference[]; // Additional pages with references
  documentReferences?: DocumentReference[]; // Multiple document references
}

interface Snippet {
  id: string;
  title: string;
  category?: string;
  pdfReference: PDFReference;
  fieldMappings: Record<string, string>;
  matchedFields: string[];
  confidenceScore?: number;
  status?: 'normal' | 'updated' | 'deleted';
  documentId?: string;
}

interface SnippetListProps {
  snippets: Snippet[];
  onApply: (snippet: Snippet, keepSnippetsVisible?: boolean) => void;
  onClose: () => void;
  searchQuery?: string;
  isAnalyzing?: boolean;
}

export function SnippetList({
  snippets,
  onApply,
  onClose,
  searchQuery,
  isAnalyzing = false,
}: SnippetListProps) {
  const snippetsContainerRef = useRef<HTMLDivElement>(null);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [isFlipped, setIsFlipped] = useState(false);

  // Reset to first snippet when snippets change
  useEffect(() => {
    setCurrentIndex(0);
  }, [snippets]);

  if (snippets.length === 0) return null;

  const handleNext = () => {
    setCurrentIndex((prev) => (prev + 1) % snippets.length);
  };

  const handlePrevious = () => {
    setCurrentIndex((prev) => (prev - 1 + snippets.length) % snippets.length);
  };

  const currentSnippet = snippets[currentIndex];

  // Function to render text with highlights

  // Component for individual scrollable snippet with flip card
  const ScrollableSnippet = ({ snippet, index, flipped, onFlip }: { 
    snippet: Snippet; 
    index: number;
    flipped: boolean;
    onFlip: () => void;
  }) => {
    const scrollToPageRef = useRef<((page: number) => void) | null>(null);
    const [selectedDocumentIndex, setSelectedDocumentIndex] = useState(0);
    const [showDocumentDropdown, setShowDocumentDropdown] = useState(false);

    const handleCardClick = (e: React.MouseEvent) => {
      // Don't flip if clicking on buttons or interactive elements
      const target = e.target as HTMLElement;
      if (target.closest('button') || target.closest('[role="button"]')) {
        return;
      }
      onFlip();
    };

    const isUpdated = snippet.status === 'updated';
    const isDeleted = snippet.status === 'deleted';
    const [showDeletedPopup, setShowDeletedPopup] = useState(false);

    // Check if snippet has multiple document references
    const hasMultipleDocuments = snippet.pdfReference.documentReferences && snippet.pdfReference.documentReferences.length > 0;
    
    // Get current document data
    const currentDocumentData = hasMultipleDocuments 
      ? snippet.pdfReference.documentReferences![selectedDocumentIndex]
      : null;

    return (
      <div
        key={snippet.id}
        className="bg-white rounded-lg shadow-md border border-gray-200 overflow-hidden animate-in fade-in slide-in-from-bottom-2 duration-300 transition-all hover:shadow-lg hover:border-gray-300 cursor-pointer relative"
        style={{ 
          animationDelay: `${index * 50}ms`,
          opacity: isDeleted ? 0.55 : 1,
        }}
        onClick={handleCardClick}
        onMouseEnter={() => isDeleted && setShowDeletedPopup(true)}
        onMouseLeave={() => isDeleted && setShowDeletedPopup(false)}
      >
        {/* Updated Banner */}
        {isUpdated && (
          <div className="bg-yellow-100 border-b border-yellow-300 px-4 py-2">
            <div className="flex items-center gap-2">
              <div className="w-2 h-2 bg-yellow-500 rounded-full"></div>
              <p className="text-xs font-semibold text-yellow-900">This snippet has been updated</p>
            </div>
          </div>
        )}

        {/* Deleted Popup */}
        {isDeleted && showDeletedPopup && (
          <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/10 rounded-lg pointer-events-none">
            <div className="bg-white border-2 border-red-300 rounded-lg shadow-xl p-4 max-w-xs mx-4 pointer-events-auto">
              <div className="flex items-start gap-3">
                <div className="flex-shrink-0 mt-0.5">
                  <div className="w-2 h-2 bg-red-500 rounded-full"></div>
                </div>
                <div className="flex-1">
                  <p className="text-sm font-semibold text-red-900 mb-1">Document Removed</p>
                  <p className="text-xs text-red-800">
                    This snippet was deleted as the corresponding document got removed. Please select a different snippet.
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Card Container with Flip Effect */}
        <div className="relative" style={{ perspective: '1000px', minHeight: '400px' }}>
          <div
            className="relative w-full transition-transform duration-500"
            style={{
              transformStyle: 'preserve-3d',
              transform: flipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
            }}
          >
            {/* Front Side - AI Summary */}
            <div
              className="w-full"
              style={{ backfaceVisibility: 'hidden', WebkitBackfaceVisibility: 'hidden' }}
            >
              {/* AI Summary Content */}
              <div className="p-4">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <h4 className="text-sm font-semibold text-gray-900 mb-3 flex items-center gap-2">
                      <FileText className="w-4 h-4 text-blue-600" />
                      AI Summary
                    </h4>
                    <p className="text-xs text-gray-500 mb-4">
                      This snippet will fill the following fields:
                    </p>
                    {snippet.category ? (
                      <p className="text-xs font-semibold text-indigo-800 mb-3">
                        Category: <span className="font-normal">{snippet.category}</span>
                      </p>
                    ) : null}
                  </div>
                </div>

                {/* Field Mappings Display */}
                <div className="space-y-3">
                  {Object.entries(snippet.fieldMappings).map(([fieldId, value]) => {
                    // Find the matched field name
                    const fieldName = snippet.matchedFields.find(f => 
                      f.toLowerCase().replace(/\s+/g, '') === fieldId.toLowerCase().replace(/\s+/g, '')
                    ) || fieldId;
                    
                    return (
                      <div key={fieldId} className="bg-gray-50 rounded-lg p-3 border border-gray-200">
                        <div className="flex items-start gap-2">
                          <div className="flex-shrink-0 mt-0.5">
                            <div className="w-2 h-2 rounded-full bg-red-500"></div>
                          </div>
                          <div className="flex-1 min-w-0">
                            <p className="text-xs font-semibold text-gray-700 mb-1">
                              {fieldName}
                            </p>
                            <p className="text-sm text-gray-900 leading-relaxed">
                              {value as string}
                            </p>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Click to view reference hint */}
                <div className="mt-4 pt-3 border-t border-gray-200">
                  <p className="text-xs text-gray-500 text-center flex items-center justify-center gap-1">
                    <FileText className="w-3 h-3 text-red-500" />
                    Click card to view PDF reference
                  </p>
                </div>
              </div>

              {/* Accept/Approve Button */}
              <div className="px-4 pb-4" onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center justify-center">
                  <button
                    onClick={() => onApply(snippet, false)}
                    disabled={isDeleted}
                    className={`px-6 py-2.5 border rounded-lg transition-colors flex items-center justify-center gap-2 ${
                      isDeleted
                        ? 'border-gray-300 text-gray-400 cursor-not-allowed'
                        : isUpdated
                        ? 'border-yellow-400 bg-yellow-50 hover:bg-yellow-100 hover:border-yellow-500 text-yellow-800'
                        : 'border-red-300 hover:bg-red-50 hover:border-red-400 text-red-700'
                    }`}
                  >
                    <Check className="w-4 h-4" />
                    <span className="text-sm font-medium">{isUpdated ? 'Approve Changes' : 'Accept'}</span>
                  </button>
                </div>
              </div>
            </div>

            {/* Back Side - PDF Snippet */}
            <div
              className="absolute inset-0 w-full"
              style={{
                backfaceVisibility: 'hidden',
                WebkitBackfaceVisibility: 'hidden',
                transform: 'rotateY(180deg)',
              }}
            >
              {/* Header with References */}
              <div className="px-4 pt-4 pb-2 flex items-center gap-3">
                {/* Left side - Reference Tags - show if there are multiple pages */}
                {((hasMultipleDocuments && currentDocumentData && currentDocumentData.pageReferences && currentDocumentData.pageReferences.length > 0) ||
                  (!hasMultipleDocuments && snippet.pdfReference.pageReferences && snippet.pdfReference.pageReferences.length > 0)) && (
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-medium text-gray-700">References:</span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (scrollToPageRef.current) {
                          const pageNum = hasMultipleDocuments && currentDocumentData 
                            ? currentDocumentData.pageNumber 
                            : snippet.pdfReference.page;
                          scrollToPageRef.current(pageNum);
                        }
                      }}
                      className="px-2.5 py-1 text-xs rounded-md transition-colors font-medium bg-white border border-blue-300 text-blue-700 hover:bg-blue-50 hover:border-blue-400"
                      title={`Go to page ${hasMultipleDocuments && currentDocumentData ? currentDocumentData.pageNumber : snippet.pdfReference.page}`}
                    >
                      Page {hasMultipleDocuments && currentDocumentData ? currentDocumentData.pageNumber : snippet.pdfReference.page}
                    </button>
                    {(hasMultipleDocuments && currentDocumentData ? currentDocumentData.pageReferences : snippet.pdfReference.pageReferences)?.map((pageRef) => (
                      <button
                        key={pageRef.page}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (scrollToPageRef.current) {
                            scrollToPageRef.current(pageRef.page);
                          }
                        }}
                        className="px-2.5 py-1 text-xs rounded-md transition-colors font-medium bg-white border border-blue-300 text-blue-700 hover:bg-blue-50 hover:border-blue-400"
                        title={`Go to page ${pageRef.page}`}
                      >
                        Page {pageRef.page}
                      </button>
                    ))}
                  </div>
                )}

                {/* Spacer to push document dropdown to the right */}
                <div className="flex-1"></div>

                {/* Right side - Document Dropdown - Only show if multiple documents */}
                {hasMultipleDocuments && snippet.pdfReference.documentReferences && (
                  <div className="relative" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() => setShowDocumentDropdown(!showDocumentDropdown)}
                      className="px-3 py-1.5 text-xs rounded-md transition-colors font-medium bg-white border border-purple-300 text-purple-700 hover:bg-purple-50 hover:border-purple-400 flex items-center gap-1.5"
                      title="Select document"
                    >
                      <FileText className="w-3.5 h-3.5" />
                      <span className="max-w-[120px] truncate">
                        {snippet.pdfReference.documentReferences[selectedDocumentIndex].documentName}
                      </span>
                      <ChevronDown className={`w-3.5 h-3.5 transition-transform ${showDocumentDropdown ? 'rotate-180' : ''}`} />
                    </button>

                    {/* Dropdown Menu */}
                    {showDocumentDropdown && (
                      <div className="absolute right-0 mt-1 bg-white border border-gray-300 rounded-md shadow-lg z-50 min-w-[200px] max-w-[300px]">
                        {snippet.pdfReference.documentReferences.map((docRef, idx) => (
                          <button
                            key={docRef.documentId}
                            onClick={() => {
                              setSelectedDocumentIndex(idx);
                              setShowDocumentDropdown(false);
                            }}
                            className={`w-full text-left px-3 py-2 text-xs hover:bg-purple-50 transition-colors flex items-center gap-2 ${
                              idx === selectedDocumentIndex ? 'bg-purple-100 text-purple-900 font-semibold' : 'text-gray-700'
                            } ${idx === 0 ? 'rounded-t-md' : ''} ${idx === snippet.pdfReference.documentReferences!.length - 1 ? 'rounded-b-md' : 'border-b border-gray-200'}`}
                          >
                            <FileText className="w-3.5 h-3.5 flex-shrink-0" />
                            <span className="truncate">{docRef.documentName}</span>
                            {idx === selectedDocumentIndex && (
                              <Check className="w-3.5 h-3.5 ml-auto flex-shrink-0" />
                            )}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* PDF-like Content - Scrollable */}
              <div className="p-4">
                <div className="bg-white border border-gray-300 shadow-inner rounded overflow-hidden">
                  {/* Scrollable PDF container - full document view */}
                  <div className="bg-gray-100 p-2">
                    {snippet.pdfReference.fullText ? (
                      <PDFSnippetViewer
                        fullText={hasMultipleDocuments && currentDocumentData ? currentDocumentData.fullText : snippet.pdfReference.fullText}
                        highlights={hasMultipleDocuments && currentDocumentData ? currentDocumentData.highlights : snippet.pdfReference.highlights}
                        pageNumber={hasMultipleDocuments && currentDocumentData ? currentDocumentData.pageNumber : snippet.pdfReference.page}
                        title={snippet.title}
                        pageReferences={hasMultipleDocuments && currentDocumentData ? currentDocumentData.pageReferences : snippet.pdfReference.pageReferences}
                        onPageClick={() => {
                          // Scroll to page is handled internally by PDFSnippetViewer
                        }}
                        onScrollToPageReady={(scrollFn) => {
                          scrollToPageRef.current = scrollFn;
                        }}
                      />
                    ) : (
                      <div className="p-4 bg-white border border-gray-300 rounded">
                        <p>{snippet.pdfReference.context}</p>
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Accept/Approve Button */}
              <div className="px-4 pb-4" onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center justify-center">
                  <button
                    onClick={() => onApply(snippet, false)}
                    disabled={isDeleted}
                    className={`px-6 py-2.5 border rounded-lg transition-colors flex items-center justify-center gap-2 ${
                      isDeleted
                        ? 'border-gray-300 text-gray-400 cursor-not-allowed'
                        : isUpdated
                        ? 'border-yellow-400 bg-yellow-50 hover:bg-yellow-100 hover:border-yellow-500 text-yellow-800'
                        : 'border-red-300 hover:bg-red-50 hover:border-red-400 text-red-700'
                    }`}
                  >
                    <Check className="w-4 h-4" />
                    <span className="text-sm font-medium">{isUpdated ? 'Approve Changes' : 'Accept'}</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="h-full flex flex-col bg-gray-50 animate-in fade-in slide-in-from-left-4 duration-300">
      {/* Header */}
      <div className="bg-gradient-to-r from-blue-600 to-teal-600 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3 flex-1">
          {isAnalyzing ? (
            <AIAnalyzingAnimation message="Analyzing documents..." size="sm" />
          ) : (
            <>
              <div className="flex items-center gap-2">
                <span className="text-black font-medium">
                  {searchQuery ? `Results for "${searchQuery}"` : 'AI Auto Fill'}
                </span>
                <span className="bg-white/20 text-red-600 text-xs px-2 py-1 rounded font-semibold">
                  {snippets.length} {snippets.length === 1 ? 'snippet' : 'snippets'} fetched
                </span>
              </div>
              
              {/* Navigation Controls */}
              {snippets.length > 1 && (
                <div className="flex items-center ml-auto gap-2 mr-2">
                  <button
                    onClick={handlePrevious}
                    className="text-black hover:bg-white/20 rounded p-1.5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Previous snippet"
                  >
                    <ChevronLeft className="w-5 h-5" />
                  </button>
                  <span className="text-black text-sm font-medium px-2">
                    {currentIndex + 1} / {snippets.length}
                  </span>
                  <button
                    onClick={handleNext}
                    className="text-black hover:bg-white/20 rounded p-1.5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Next snippet"
                  >
                    <ChevronRight className="w-5 h-5" />
                  </button>
                </div>
              )}
            </>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-white hover:bg-white/20 rounded p-1 transition-colors"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Snippet Carousel - Shows one snippet at a time */}
      <div 
        ref={snippetsContainerRef}
        className="flex-1 overflow-y-auto p-4" 
        style={{ maxHeight: 'calc(100vh - 250px)' }}
      >
        {currentSnippet && (
          <ScrollableSnippet 
            key={currentSnippet.id} 
            snippet={currentSnippet} 
            index={0}
            flipped={isFlipped}
            onFlip={() => setIsFlipped(!isFlipped)}
          />
        )}
      </div>
    </div>
  );
}