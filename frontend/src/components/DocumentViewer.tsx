import React, { useState } from 'react';
import { FileText, ChevronLeft, ChevronRight, ZoomIn, ZoomOut } from 'lucide-react';
import type { Document } from './DocumentSelector';

interface DocumentViewerProps {
  document: Document | null;
  highlightedSection?: {
    page: number;
    segment: string;
  } | null;
}

export function DocumentViewer({ document, highlightedSection }: DocumentViewerProps) {
  const [currentPage, setCurrentPage] = useState(1);
  const [zoomLevel, setZoomLevel] = useState(100);

  const handlePreviousPage = () => {
    if (document && currentPage > 1) {
      setCurrentPage(currentPage - 1);
    }
  };

  const handleNextPage = () => {
    if (document && currentPage < document.totalPages) {
      setCurrentPage(currentPage + 1);
    }
  };

  const handleZoomIn = () => {
    if (zoomLevel < 200) {
      setZoomLevel(Math.min(zoomLevel + 10, 200));
    }
  };

  const handleZoomOut = () => {
    if (zoomLevel > 50) {
      setZoomLevel(Math.max(zoomLevel - 10, 50));
    }
  };

  // Jump to highlighted page if available
  React.useEffect(() => {
    if (highlightedSection && document) {
      setCurrentPage(highlightedSection.page);
    }
  }, [highlightedSection, document]);

  if (!document) {
    return (
      <div className="h-full flex flex-col bg-gray-100">
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <FileText className="w-16 h-16 text-gray-400 mx-auto mb-4" />
            <p className="text-gray-600 mb-2">PDF Document Viewer</p>
            <p className="text-sm text-gray-500">Select a document to view</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-gray-100">
      {/* PDF Viewer Content */}
      <div className="flex-1 overflow-auto p-4">
      </div>

      {/* PDF Navigation Controls */}
      <div className="bg-white border-t border-gray-300 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <button
            onClick={handlePreviousPage}
            disabled={currentPage === 1}
            className="px-3 py-1 text-sm text-gray-600 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
          >
            <ChevronLeft className="w-4 h-4" />
            Previous
          </button>
          <span className="text-sm text-gray-600">
            Page {currentPage} of {document.totalPages}
          </span>
          <button
            onClick={handleNextPage}
            disabled={currentPage === document.totalPages}
            className="px-3 py-1 text-sm text-gray-600 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
          >
            Next
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleZoomOut}
            disabled={zoomLevel <= 50}
            className="px-3 py-1 text-sm text-gray-600 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
          >
            <ZoomOut className="w-4 h-4" />
            Zoom Out
          </button>
          <span className="text-sm text-gray-600 min-w-[60px] text-center">{zoomLevel}%</span>
          <button
            onClick={handleZoomIn}
            disabled={zoomLevel >= 200}
            className="px-3 py-1 text-sm text-gray-600 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
          >
            <ZoomIn className="w-4 h-4" />
            Zoom In
          </button>
        </div>
      </div>
    </div>
  );
}
