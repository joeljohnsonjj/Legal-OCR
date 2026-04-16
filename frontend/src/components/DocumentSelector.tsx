import React from 'react';
import { FileText, ChevronDown } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';

export interface Document {
  id: string;
  name: string;
  uploadDate: string;
  uploadedBy: string;
  totalPages: number;
  url?: string; // URL to the actual PDF file
}

interface DocumentSelectorProps {
  documents: Document[];
  selectedDocumentId: string | null;
  onSelectDocument: (documentId: string) => void;
}

export function DocumentSelector({
  documents,
  selectedDocumentId,
  onSelectDocument,
}: DocumentSelectorProps) {
  const selectedDocument = documents.find(doc => doc.id === selectedDocumentId);

  return (
    <div className="flex items-center gap-3">
      <FileText className="w-5 h-5 text-gray-600" />
      <span className="text-sm font-medium text-gray-700">Document:</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button className="flex items-center gap-2 px-3 py-1.5 text-sm text-gray-900 hover:bg-gray-100 rounded-md border border-gray-300 transition-colors">
            <span className="max-w-xs truncate">
              {selectedDocument ? selectedDocument.name : 'Select a document'}
            </span>
            <ChevronDown className="w-4 h-4 text-gray-500" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-80">
          {documents.map((document) => (
            <DropdownMenuItem
              key={document.id}
              onClick={() => onSelectDocument(document.id)}
              className={`cursor-pointer ${
                selectedDocumentId === document.id
                  ? 'bg-purple-50 text-purple-900'
                  : ''
              }`}
            >
              <div className="flex flex-col gap-1 w-full">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-sm">{document.name}</span>
                  {selectedDocumentId === document.id && (
                    <span className="text-xs text-purple-600">✓</span>
                  )}
                </div>
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <span>{document.totalPages} pages</span>
                  <span>•</span>
                  <span>{document.uploadDate}</span>
                </div>
                <span className="text-xs text-gray-400">{document.uploadedBy}</span>
              </div>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      {selectedDocument && (
        <span className="text-xs text-gray-500">
          {selectedDocument.totalPages} pages
        </span>
      )}
    </div>
  );
}

