import React, { useRef, useEffect, useState, useCallback } from 'react';
import { RotateCcw, Lock, Unlock } from 'lucide-react';

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

interface PDFSnippetViewerProps {
  fullText: string;
  highlights?: Highlight[];
  pageNumber: number;
  title: string;
  pageReferences?: PageReference[]; // Additional pages with references
  onPageClick?: (page: number) => void; // Callback when reference tag is clicked
  onScrollToPageReady?: (scrollFn: (page: number) => void) => void; // Callback to expose scroll function
}

// Generate a PDF-like document view from text content
export function PDFSnippetViewer({ fullText, highlights, pageNumber, title, pageReferences = [], onPageClick, onScrollToPageReady }: PDFSnippetViewerProps) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const highlightRef = useRef<HTMLSpanElement>(null);
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const [showResetButton, setShowResetButton] = useState(false);
  const [isScrollLocked, setIsScrollLocked] = useState(true); // Locked by default to prevent accidental scrolling
  const [currentViewPage, setCurrentViewPage] = useState(pageNumber);

  // Update current view page when pageNumber changes
  useEffect(() => {
    setCurrentViewPage(pageNumber);
  }, [pageNumber]);

  // Function to scroll to highlight
  const scrollToHighlight = () => {
    if (highlightRef.current && scrollContainerRef.current) {
      // Use scrollTo instead of scrollIntoView to prevent affecting parent containers
      const container = scrollContainerRef.current;
      const highlight = highlightRef.current;
      const containerRect = container.getBoundingClientRect();
      const highlightRect = highlight.getBoundingClientRect();
      const scrollTop = container.scrollTop;
      const relativeTop = highlightRect.top - containerRect.top + scrollTop;
      
      container.scrollTo({
        top: relativeTop - (container.clientHeight / 2) + (highlightRect.height / 2),
        behavior: 'smooth',
      });
      setShowResetButton(false);
    }
  };

  // Auto-scroll to first highlight on mount or when page changes
  useEffect(() => {
    if (highlightRef.current && scrollContainerRef.current) {
      // Use a longer delay to ensure parent container has settled
      setTimeout(() => {
        scrollToHighlight();
      }, 300);
    }
  }, [highlights, currentViewPage]);

  // Prevent wheel scrolling when locked
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;

    const handleWheel = (e: WheelEvent) => {
      if (isScrollLocked) {
        e.preventDefault();
        e.stopPropagation();
      }
    };

    // Also prevent touch scrolling on mobile
    const handleTouchMove = (e: TouchEvent) => {
      if (isScrollLocked) {
        e.preventDefault();
        e.stopPropagation();
      }
    };

    container.addEventListener('wheel', handleWheel, { passive: false });
    container.addEventListener('touchmove', handleTouchMove, { passive: false });

    return () => {
      container.removeEventListener('wheel', handleWheel);
      container.removeEventListener('touchmove', handleTouchMove);
    };
  }, [isScrollLocked]);

  // Check if user has scrolled away from highlight
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container || !highlightRef.current) return;

    const checkScrollPosition = () => {
      if (!highlightRef.current || !container) return;
      
      const containerRect = container.getBoundingClientRect();
      const highlightRect = highlightRef.current.getBoundingClientRect();
      
      // Check if highlight is visible in viewport
      const isVisible = 
        highlightRect.top >= containerRect.top &&
        highlightRect.bottom <= containerRect.bottom;
      
      setShowResetButton(!isVisible);
    };

    // Only check scroll position if not locked
    if (!isScrollLocked) {
      container.addEventListener('scroll', checkScrollPosition);
      // Also check on mount
      setTimeout(checkScrollPosition, 200);
    }

    return () => {
      container.removeEventListener('scroll', checkScrollPosition);
    };
  }, [highlights, isScrollLocked]);

  const toggleScrollLock = () => {
    setIsScrollLocked(!isScrollLocked);
    // If unlocking, check if we need to show reset button
    if (!isScrollLocked && highlightRef.current && scrollContainerRef.current) {
      setTimeout(() => {
        const container = scrollContainerRef.current;
        const highlight = highlightRef.current;
        if (container && highlight) {
          const containerRect = container.getBoundingClientRect();
          const highlightRect = highlight.getBoundingClientRect();
          const isVisible = 
            highlightRect.top >= containerRect.top &&
            highlightRect.bottom <= containerRect.bottom;
          setShowResetButton(!isVisible);
        }
      }, 100);
    }
  };

  // Render text with highlights
  const renderHighlightedText = (text: string, highlights?: Highlight[], pageNum?: number) => {
    if (!highlights || highlights.length === 0) {
      return <span>{text}</span>;
    }

    let lastIndex = 0;
    const parts: JSX.Element[] = [];
    let isFirstHighlight = true;

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

      // Add highlighted text - only attach ref if this is the current view page
      const highlightElement = (
        <mark
          key={`highlight-${idx}`}
          ref={isFirstHighlight && pageNum === currentViewPage ? highlightRef : undefined}
          className={`${highlight.color} px-0.5 cursor-help transition-all hover:ring-2 hover:ring-blue-400 hover:ring-offset-1 rounded-sm`}
          title={`Maps to: ${highlight.field}`}
        >
          {highlight.text}
        </mark>
      );
      
      parts.push(highlightElement);
      if (isFirstHighlight) isFirstHighlight = false;

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

  // Scroll to a specific page
  const scrollToPage = useCallback((targetPage: number) => {
    if (pageRefs.current[targetPage] && scrollContainerRef.current) {
      const container = scrollContainerRef.current;
      const targetElement = pageRefs.current[targetPage];
      if (targetElement) {
        const containerRect = container.getBoundingClientRect();
        const targetRect = targetElement.getBoundingClientRect();
        const scrollTop = container.scrollTop;
        const relativeTop = targetRect.top - containerRect.top + scrollTop;
        
        container.scrollTo({
          top: relativeTop - 20, // 20px offset from top
          behavior: 'smooth',
        });
        setCurrentViewPage(targetPage);
        setIsScrollLocked(false); // Unlock when navigating to a page
      }
    }
    if (onPageClick) {
      onPageClick(targetPage);
    }
  }, [onPageClick]);

  // Expose scrollToPage function to parent
  useEffect(() => {
    if (onScrollToPageReady) {
      onScrollToPageReady(scrollToPage);
    }
  }, [onScrollToPageReady, scrollToPage]);

  // Generate full document content with multiple pages
  // Create realistic PDF document structure
  const generateFullDocument = () => {
    const pages: Array<{ pageNum: number; content: string; highlights?: Highlight[] }> = [];
    
    // Collect all referenced pages
    const allReferencedPages = new Set<number>();
    allReferencedPages.add(pageNumber);
    pageReferences.forEach(ref => allReferencedPages.add(ref.page));
    const sortedPages = Array.from(allReferencedPages).sort((a, b) => a - b);
    
    // Generate pages before the first referenced page
    const firstPage = sortedPages[0];
    for (let i = 1; i < firstPage; i++) {
      let prevPageContent = '';
      if (i === 1) {
        prevPageContent = `COMMERCIAL LEASE AGREEMENT\n\nThis Commercial Lease Agreement ("Agreement") is entered into on this date between the Landlord and Tenant as defined herein.\n\nThe parties agree to the following terms and conditions governing the lease of the commercial property located as specified in Schedule A attached hereto.\n\nARTICLE I - GENERAL PROVISIONS\n\nSection 1.1: Definitions\nFor purposes of this Agreement, the following terms shall have the meanings set forth below:\n\n"Property" means the commercial real estate described in Schedule A.\n"Landlord" refers to the property owner or authorized representative.\n"Tenant" refers to the party leasing the Property.\n\nSection 1.2: Lease Term\nThe initial term of this lease shall commence on the Commencement Date and continue for the period specified in Schedule B, unless earlier terminated in accordance with the terms of this Agreement.`;
      } else if (i === 2) {
        prevPageContent = `Section 1.3: Rent and Payment Terms\nBase rent shall be paid in accordance with Schedule B. All rent payments are due on the first day of each month without demand, deduction, or offset.\n\nLate fees may apply as specified in Section 1.4. The Tenant agrees to pay all applicable taxes, utilities, and other charges as outlined in this Agreement.\n\nSection 1.4: Security Deposit\nTenant shall provide a security deposit as specified in Schedule B. This deposit shall be held by Landlord as security for the performance of Tenant's obligations under this Agreement.`;
      } else {
        prevPageContent = `PAGE ${i}\n\nThis page contains additional provisions and terms of the commercial lease agreement. The document continues with detailed specifications regarding property use, maintenance obligations, and other standard lease provisions.\n\nAdditional clauses may include restrictions on use, assignment and subletting provisions, insurance requirements, and default remedies available to both parties under this Agreement.`;
      }
      pages.push({ pageNum: i, content: prevPageContent });
    }
    
    // Add all referenced pages
    sortedPages.forEach(pageNum => {
      if (pageNum === pageNumber) {
        // Main page with original highlights
        pages.push({ pageNum, content: fullText, highlights });
      } else {
        // Find the page reference
        const pageRef = pageReferences.find(ref => ref.page === pageNum);
        if (pageRef) {
          pages.push({ pageNum, content: pageRef.fullText, highlights: pageRef.highlights });
        } else {
          // Generate placeholder content
          pages.push({ 
            pageNum, 
            content: `PAGE ${pageNum}\n\nThis page contains additional provisions related to the lease agreement.\n\nFurther details and specifications are outlined in the subsequent sections of this document.` 
          });
        }
      }
    });
    
    // Generate pages after the last referenced page (up to 3 more)
    const lastPage = sortedPages[sortedPages.length - 1];
    const pagesAfter = Math.min(3, 25 - lastPage);
    for (let i = 1; i <= pagesAfter; i++) {
      const nextPageNum = lastPage + i;
      pages.push({ 
        pageNum: nextPageNum, 
        content: `PAGE ${nextPageNum}\n\nThis section continues the provisions outlined on the previous pages. Additional terms and conditions related to the lease agreement are specified herein.\n\nFurther details regarding maintenance schedules, inspection rights, and compliance requirements are addressed in subsequent sections of this document.` 
      });
    }
    
    return pages;
  };

  const documentPages = generateFullDocument();

  // Get all referenced page numbers
  const allReferencedPages = new Set<number>();
  allReferencedPages.add(pageNumber);
  pageReferences.forEach(ref => allReferencedPages.add(ref.page));
  const sortedReferencedPages = Array.from(allReferencedPages).sort((a, b) => a - b);

  return (
    <div className="relative">
      {/* Scroll lock/unlock button - always visible */}
      <button
        onClick={toggleScrollLock}
        className={`absolute top-4 right-4 z-20 rounded-full p-2 shadow-lg transition-all hover:scale-110 flex items-center justify-center ${
          isScrollLocked 
            ? 'bg-blue-600 text-white hover:bg-blue-700' 
            : 'bg-teal-600 text-white hover:bg-teal-700'
        }`}
        title={isScrollLocked ? 'Unlock scrolling' : 'Lock scrolling'}
      >
        {isScrollLocked ? (
          <Lock className="w-4 h-4" />
        ) : (
          <Unlock className="w-4 h-4" />
        )}
      </button>

      {/* Reset button - shows when scrolled away from highlight and unlocked */}
      {showResetButton && highlights && highlights.length > 0 && !isScrollLocked && (
        <button
          onClick={scrollToHighlight}
          className={`absolute top-4 z-20 bg-blue-600 text-white rounded-full p-2 shadow-lg hover:bg-blue-700 transition-all hover:scale-110 flex items-center justify-center ${
            sortedReferencedPages.length > 1 ? 'right-32' : 'right-16'
          }`}
          title="Scroll back to highlighted section"
        >
          <RotateCcw className="w-5 h-5" />
        </button>
      )}

      {/* Scroll lock indicator - subtle border */}
      {isScrollLocked && (
        <div className="absolute inset-0 z-10 pointer-events-none border-2 border-dashed border-blue-400/50 rounded-lg">
          <div className="absolute top-2 left-2 bg-blue-100/90 backdrop-blur-sm px-2 py-1 rounded text-xs font-medium text-blue-700 flex items-center gap-1">
            <Lock className="w-3 h-3" />
            <span>Scroll Locked</span>
          </div>
        </div>
      )}
      
      <div 
        ref={scrollContainerRef}
        className={`bg-gray-100 ${isScrollLocked ? 'overflow-hidden' : 'overflow-y-auto'}`}
        style={{ 
          maxHeight: '280px', // Reduced to show just the highlighted section (like image)
          minHeight: '250px'
        }}
      >
      {documentPages.map((pageData, pageIdx) => {
        const actualPageNum = pageData.pageNum;
        const pageContent = pageData.content;
        const pageHighlights = pageData.highlights;
        const isReferencedPage = sortedReferencedPages.includes(actualPageNum);
        
        return (
          <div
            key={pageIdx}
            ref={(el) => {
              if (el) {
                pageRefs.current[actualPageNum] = el;
              }
            }}
            className="mb-4 bg-white border border-gray-300 shadow-md p-4 mx-auto"
            style={{ 
              minHeight: '600px',
              maxWidth: '8.5in',
              width: '100%'
            }}
          >
            {/* Page number indicator - top right */}
            <div className="flex justify-end mb-3 pb-2 border-b border-gray-300">
              <span className={`text-xs font-medium ${isReferencedPage ? 'text-red-600' : 'text-gray-500'}`}>
                - {actualPageNum} -
              </span>
            </div>
            
            {/* Document content - PDF-like styling */}
            <div 
              className="whitespace-pre-line text-gray-900 leading-relaxed text-sm"
              style={{ 
                fontFamily: 'Georgia, "Times New Roman", serif',
                lineHeight: '1.6'
              }}
            >
              {pageHighlights && pageHighlights.length > 0 ? (
                renderHighlightedText(pageContent, pageHighlights, actualPageNum)
              ) : (
                <span>{pageContent}</span>
              )}
            </div>
            
            {/* Page footer */}
            <div className="mt-6 pt-3 border-t border-gray-200 text-xs text-gray-400 text-center">
              Commercial Lease Agreement - Page {actualPageNum}
            </div>
          </div>
        );
      })}
      </div>
    </div>
  );
}

