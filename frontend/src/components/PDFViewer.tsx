import { useState, useEffect, useRef, useMemo } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
// Note: react-pdf v10 may bundle styles automatically
// If styling issues occur, uncomment and adjust paths:
// import 'react-pdf/dist/Page/AnnotationLayer.css';
// import 'react-pdf/dist/Page/TextLayer.css';

// Set up PDF.js worker
// Use the actual version from pdfjs (which react-pdf uses internally)
const pdfjsVersion = pdfjs.version || '5.4.296';
console.log('[PDFViewer] PDF.js version:', pdfjsVersion);

// Use unpkg CDN with the correct version and path
// The worker file is at: https://unpkg.com/pdfjs-dist@VERSION/build/pdf.worker.min.mjs
pdfjs.GlobalWorkerOptions.workerSrc = `https://unpkg.com/pdfjs-dist@${pdfjsVersion}/build/pdf.worker.min.mjs`;

interface PDFViewerProps {
  documentName: string;
  pageNumbers: number[];
  onPageChange?: (page: number) => void;
  className?: string;
  /** When true, hides the internal page navigation (for use when parent renders nav elsewhere) */
  hidePageNavigation?: boolean;
  /** Controlled page index - when provided, viewer uses this instead of internal state */
  controlledPageIndex?: number;
  /** Optional fixed height for the PDF container (e.g. to match snippet size) */
  containerHeight?: string;
}

export function PDFViewer({ documentName, pageNumbers, onPageChange, className = '', hidePageNavigation = false, controlledPageIndex, containerHeight }: PDFViewerProps) {
  const [numPages, setNumPages] = useState<number | null>(null);
  const [currentPageIndex, setCurrentPageIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const containerRef = useRef<HTMLDivElement>(null);

  // Get PDF URL - assuming PDFs are in public/docs folder (Vite serves public folder from root)
  const getPdfUrl = (docName: string): string => {
    // Normalize the document name: remove any .pdf extension, then add it back
    // This handles cases where API might return with or without .pdf
    let cleanName = docName.trim();
    
    // Remove .pdf extension if present (case-insensitive)
    while (cleanName.toLowerCase().endsWith('.pdf')) {
      cleanName = cleanName.slice(0, -4);
    }
    
    const fileName = `${cleanName}.pdf`;
    // In Vite, files in public folder are served from root, so /docs/file.pdf
    return `/docs/${encodeURIComponent(fileName)}`;
  };

  const pdfUrl = getPdfUrl(documentName);
  
  // Memoize PDF options to prevent unnecessary reloads
  const pdfOptions = useMemo(() => ({
    cMapUrl: 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/cmaps/',
    cMapPacked: true,
    standardFontDataUrl: 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/standard_fonts/',
  }), []); // Empty deps - these options never change
  
  // Debug: Log the PDF URL being requested and test accessibility
  useEffect(() => {
    const normalizedName = documentName.trim();
    let cleanName = normalizedName;
    while (cleanName.toLowerCase().endsWith('.pdf')) {
      cleanName = cleanName.slice(0, -4);
    }
    const finalFileName = `${cleanName}.pdf`;
    
    console.log('[PDFViewer] Loading PDF:', {
      originalDocumentName: documentName,
      normalizedFileName: finalFileName,
      pdfUrl,
      encodedUrl: encodeURIComponent(finalFileName),
      fullPath: `/docs/${encodeURIComponent(finalFileName)}`
    });
    
    // Test if the file is accessible
    fetch(pdfUrl, { method: 'HEAD' })
      .then(response => {
        console.log('[PDFViewer] File accessibility test:', {
          url: pdfUrl,
          status: response.status,
          statusText: response.statusText,
          ok: response.ok,
          headers: Object.fromEntries(response.headers.entries())
        });
      })
      .catch(error => {
        console.error('[PDFViewer] File accessibility test failed:', error);
      });
  }, [documentName, pdfUrl]);

  // Sort page numbers
  const sortedPages = [...pageNumbers].sort((a, b) => a - b);
  const isControlled = controlledPageIndex !== undefined;
  const effectivePageIndex = isControlled ? controlledPageIndex : currentPageIndex;
  const currentPage = sortedPages[effectivePageIndex] ?? sortedPages[0] ?? 1;

  useEffect(() => {
    if (!isControlled) {
      setCurrentPageIndex(0);
    }
  }, [pageNumbers, isControlled]);

  const scrollToPage = (pageNum: number) => {
    const pageElement = pageRefs.current[pageNum];
    const container = containerRef.current;
    if (pageElement && container) {
      const containerRect = container.getBoundingClientRect();
      const pageRect = pageElement.getBoundingClientRect();
      const scrollTop = container.scrollTop;
      const relativeTop = pageRect.top - containerRect.top + scrollTop;
      container.scrollTo({ top: relativeTop - 20, behavior: 'smooth' });
      onPageChange?.(pageNum);
    }
  };

  // When controlled, scroll to the selected page when it changes
  useEffect(() => {
    if (isControlled && sortedPages[effectivePageIndex]) {
      scrollToPage(sortedPages[effectivePageIndex]);
    }
  }, [effectivePageIndex, isControlled]);

  const handleDocumentLoadSuccess = ({ numPages }: { numPages: number }) => {
    setNumPages(numPages);
    setLoading(false);
    setError(null);
    
    // Scroll to first referenced page after load
    setTimeout(() => {
      scrollToPage(currentPage);
    }, 300);
  };

  const handleDocumentLoadError = (error: Error) => {
    console.error('Error loading PDF:', error);
    console.error('Error message:', error.message);
    console.error('Error stack:', error.stack);
    console.error('PDF URL attempted:', pdfUrl);
    console.error('Document name from API:', documentName);
    console.error('Full error details:', {
      name: error.name,
      message: error.message,
      stack: error.stack,
      pdfUrl,
      documentName
    });
    setError(`Failed to load PDF: ${documentName}. Check console for details.`);
    setLoading(false);
  };

  const handlePageClick = (pageNum: number) => {
    scrollToPage(pageNum);
    if (!isControlled) {
      const index = sortedPages.indexOf(pageNum);
      if (index !== -1) {
        setCurrentPageIndex(index);
      }
    }
  };

  const handlePreviousPage = () => {
    const idx = effectivePageIndex;
    if (idx > 0) {
      const newIndex = idx - 1;
      if (!isControlled) setCurrentPageIndex(newIndex);
      scrollToPage(sortedPages[newIndex]);
      onPageChange?.(sortedPages[newIndex]);
    }
  };

  const handleNextPage = () => {
    const idx = effectivePageIndex;
    if (idx < sortedPages.length - 1) {
      const newIndex = idx + 1;
      if (!isControlled) setCurrentPageIndex(newIndex);
      scrollToPage(sortedPages[newIndex]);
      onPageChange?.(sortedPages[newIndex]);
    }
  };

  if (error) {
    return (
      <div className={`flex items-center justify-center p-8 bg-gray-50 rounded-lg border border-gray-200 ${className}`}>
        <div className="text-center">
          <p className="text-sm text-red-600 mb-2">{error}</p>
          <p className="text-xs text-gray-500 mb-1">Please ensure the PDF file exists in the public/docs folder.</p>
          <p className="text-xs text-gray-400">Expected path: /docs/{documentName.endsWith('.pdf') ? documentName : `${documentName}.pdf`}</p>
        </div>
      </div>
    );
  }

  return (
    <div className={`relative ${className}`}>
      {/* Page Navigation - Show if multiple pages and not hidden (e.g. when parent renders it) */}
      {!hidePageNavigation && sortedPages.length > 1 && (
        <div className="mb-3 flex items-center justify-between bg-gray-50 px-3 py-2 rounded-lg border border-gray-200">
          <button
            onClick={handlePreviousPage}
            disabled={currentPageIndex === 0}
            className="p-1.5 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            title="Previous page"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          
          <div className="flex items-center gap-2 flex-wrap justify-center">
            {sortedPages.map((pageNum) => (
              <button
                key={pageNum}
                onClick={() => handlePageClick(pageNum)}
                className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
                  pageNum === currentPage
                    ? 'bg-blue-600 text-white'
                    : 'bg-white text-gray-700 hover:bg-gray-100 border border-gray-300'
                }`}
                title={`Go to page ${pageNum}`}
              >
                Page {pageNum}
              </button>
            ))}
          </div>
          
          <button
            onClick={handleNextPage}
            disabled={currentPageIndex === sortedPages.length - 1}
            className="p-1.5 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            title="Next page"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* PDF Document */}
      <div
        ref={containerRef}
        className="bg-gray-100 rounded-lg border border-gray-300 overflow-y-auto"
        style={{ 
          maxHeight: containerHeight ?? '400px', 
          minHeight: containerHeight ?? '300px',
          height: containerHeight,
          width: '100%',
          padding: '10px'
        }}
      >
        {loading && (
          <div className="flex items-center justify-center p-8">
            <Loader2 className="w-6 h-6 animate-spin text-blue-600" />
            <span className="ml-2 text-sm text-gray-600">Loading PDF...</span>
          </div>
        )}
        
        <Document
          file={pdfUrl}
          onLoadSuccess={handleDocumentLoadSuccess}
          onLoadError={handleDocumentLoadError}
          loading={
            <div className="flex items-center justify-center p-8">
              <Loader2 className="w-6 h-6 animate-spin text-blue-600" />
              <span className="ml-2 text-sm text-gray-600">Loading PDF...</span>
            </div>
          }
          className="flex flex-col items-center w-full"
          options={pdfOptions}
        >
          {/* Render all pages from the document (scrollable from top to end) */}
          {/* Highlight only the referenced pages */}
          {numPages && Array.from({ length: numPages }, (_, i) => i + 1).map((pageNum) => {
            const isReferenced = sortedPages.includes(pageNum);
            
            return (
              <div
                key={pageNum}
                ref={(el) => {
                  if (el) {
                    pageRefs.current[pageNum] = el;
                  }
                }}
                className={`mb-4 flex justify-center ${isReferenced ? 'ring-2 ring-blue-500 ring-offset-2 rounded-lg' : ''}`}
                style={{ width: '100%' }}
              >
                <div 
                  className="relative" 
                  style={{ 
                    display: 'flex', 
                    flexDirection: 'column', 
                    alignItems: 'center', 
                    width: '100%',
                    margin: '0 auto'
                  }}
                >
                  <div style={{ 
                    display: 'flex', 
                    justifyContent: 'center', 
                    width: '100%',
                    backgroundColor: 'white',
                    position: 'relative',
                    transform: 'scale(0.7)',
                    transformOrigin: 'top center'
                  }}>
                    <Page
                      pageNumber={pageNum}
                      width={containerRef.current?.clientWidth ? (containerRef.current.clientWidth - 50) / 0.7 : 1140}
                      renderTextLayer={false}
                      renderAnnotationLayer={false}
                      className="shadow-md"
                      loading={
                        <div className="flex items-center justify-center p-8 bg-white min-h-[600px] w-full">
                          <Loader2 className="w-4 h-4 animate-spin text-blue-600" />
                        </div>
                      }
                      scale={1.0}
                    />
                  </div>
                  {isReferenced && (
                    <div className="text-center mt-2">
                      <span className="text-xs font-medium text-blue-600 bg-blue-50 px-2 py-1 rounded">
                        Referenced Page {pageNum}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </Document>
      </div>
    </div>
  );
}

