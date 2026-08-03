import { useState, useEffect, useRef } from 'react';
import { Search, Check, X, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import { FormField } from '../App';
import { GhostFormField } from './GhostFormField';
import { Input } from './ui/input';
import { CitationItem } from '../services/apiService';
import { PDFViewer } from './PDFViewer';

// Expandable Card Component
interface ExpandableCardProps {
  title: string;
  content: string;
  maxLength?: number;
  isBulletList?: boolean;
  minHeight?: string;
  maxHeight?: string;
}

function ExpandableCard({ title, content, maxLength = 150, isBulletList = false, minHeight, maxHeight }: ExpandableCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const shouldTruncate = content.length > maxLength;
  const displayContent = shouldTruncate && !isExpanded 
    ? content.substring(0, maxLength) + '...' 
    : content;

  const contentStyle: React.CSSProperties = {};
  if (minHeight) contentStyle.minHeight = minHeight;
  if (maxHeight) contentStyle.maxHeight = maxHeight;

  const renderContent = () => {
    if (isBulletList) {
      const parts = displayContent.split('\n\n');
      return (
        <div 
          className="text-sm text-gray-900 space-y-2" 
          style={contentStyle}
        >
          {parts.map((part, idx) => (
            <p key={idx} className="flex items-start gap-2 leading-relaxed">
              <span className="text-gray-500 mt-0.5">•</span>
              <span>{part}</span>
            </p>
          ))}
        </div>
      );
    }
    return (
      <p 
        className="text-sm text-gray-900 leading-relaxed" 
        style={contentStyle}
      >
        {displayContent}
      </p>
    );
  };

  return (
    <div className="bg-gray-50 rounded-lg p-3 border border-gray-200">
      <p className="text-xs font-bold text-gray-700 mb-1.5">{title}</p>
      {renderContent()}
      {shouldTruncate && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            setIsExpanded(!isExpanded);
          }}
          className="mt-2 text-xs text-blue-600 hover:text-blue-800 font-medium"
        >
          {isExpanded ? 'View Less' : 'View More'}
        </button>
      )}
    </div>
  );
}

interface PDFReference {
  page: number;
  segment: string;
  context?: string;
  fullText?: string;
}

interface Snippet {
  id: string;
  title: string;
  /** Backend obligation category (e.g. "Maintenance & Repairs"). */
  category?: string;
  pdfReference: PDFReference;
  fieldMappings: Record<string, string>;
  matchedFields: string[];
  confidenceScore?: number;
  status?: 'normal' | 'updated' | 'deleted';
  documentId?: string;
  citations?: CitationItem[]; // Citation data for PDF navigation
}

interface Document {
  id: string;
  name: string;
}

interface CategorySectionProps {
  category: string;
  title: string;
  fields: FormField[];
  formData: Record<string, string>;
  onFieldChange: (fieldId: string, value: string) => void;
  aiMode?: boolean;
  ghostValues?: Record<string, string>;
  onAcceptGhost?: (fieldId: string) => void;
  onFieldSearch?: (fieldId: string, value: string) => void;
  onToggleAiMode?: (enabled: boolean) => void;
  isAnalyzing?: boolean;
  snippetsCount?: number;
  snippets?: Snippet[];
  onApplySnippet?: (snippet: Snippet, keepSnippetsVisible?: boolean) => void;
  isAIApproved?: boolean;
  hasAIGeneratedFields?: Record<string, boolean>;
  globalSearchQuery?: string;
  onGlobalSearch?: (query: string) => void;
  /** After global AI Search returned zero obligations — show empty state instead of hiding the card. */
  aiSearchHadNoResults?: boolean;
  /** Short tail of readable stream lines (terminal-style) while raw query bytes arrive. */
  queryStreamProgressLog?: string;
  checkedDocuments?: string[];
  documents?: Document[];
  onDocumentCheckChange?: (documentId: string, checked: boolean) => void;
}

interface AIResponse {
  id: string;
  title: string;
  summary: string;
  fullDetails: string;
  fieldMappings: Record<string, string>;
  previewSections?: string[];
  citation?: string;
  pdfReferences?: Array<{
    page: number;
    segment: string;
    context: string;
  }>;
}

// Mock AI responses based on search query

export function CategorySection({
  category,
  title,
  formData,
  onFieldChange,
  aiMode = false,
  ghostValues = {},
  onAcceptGhost,
  onFieldSearch,
  onToggleAiMode,
  isAnalyzing = false,
  snippetsCount = 0,
  snippets = [],
  onApplySnippet,
  isAIApproved = false,
  hasAIGeneratedFields = {},
  globalSearchQuery = '',
  onGlobalSearch,
  aiSearchHadNoResults = false,
  queryStreamProgressLog: _queryStreamProgressLog = '',
  checkedDocuments = [],
  documents = [],
  onDocumentCheckChange,
}: CategorySectionProps) {
  const [localSearchQuery, setLocalSearchQuery] = useState('');
  const [showSearchRequiredWarning, setShowSearchRequiredWarning] = useState(false);
  const [showPreviewModal, setShowPreviewModal] = useState(false);
  const [selectedPreviewResponse] = useState<AIResponse | null>(null);
  const [currentSnippetIndex, setCurrentSnippetIndex] = useState(0);
  const [flippedSnippetId, setFlippedSnippetId] = useState<string | null>(null);
  const [pdfPageIndex, setPdfPageIndex] = useState(0);

  // While the model streams, follow the newest obligation row; when idle, clamp index to bounds.
  useEffect(() => {
    if (snippets.length === 0) {
      setCurrentSnippetIndex(0);
      return;
    }
    if (isAnalyzing) {
      setCurrentSnippetIndex(snippets.length - 1);
      return;
    }
    setCurrentSnippetIndex((prevIndex) => {
      if (prevIndex >= snippets.length) {
        return snippets.length - 1;
      }
      return prevIndex;
    });
  }, [snippets.length, isAnalyzing]);

  // Reset PDF page index when switching snippets or flipping view
  useEffect(() => {
    setPdfPageIndex(0);
  }, [currentSnippetIndex, flippedSnippetId]);
  
  // Field-level autocomplete
  const [activeField, setActiveField] = useState<string | null>(null);
  const [fieldSuggestions, setFieldSuggestions] = useState<AIResponse[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const suggestionRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // Handle search query sync with parent
  useEffect(() => {
    setLocalSearchQuery(globalSearchQuery || '');
  }, [globalSearchQuery]);

  // Handle search execution
  const handleSearch = () => {
    // #region UI debug logging (disabled)
    // console.log('[DEBUG CategorySection] handleSearch called', { ... });
    // #endregion
    if (onGlobalSearch) {
      onGlobalSearch(localSearchQuery || '');
    } else {
      console.warn('[CategorySection] onGlobalSearch is not defined');
    }
  };

  // Handle Enter key press in search input
  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (checkedDocuments.length === 0) {
        alert('Please select at least one document before searching');
        return;
      }
      if (!localSearchQuery.trim()) {
        setShowSearchRequiredWarning(true);
        return;
      }
      setShowSearchRequiredWarning(false);
      handleSearch();
    }
  };

  const handleApplyCitation = (response: AIResponse) => {
    // Apply all field mappings from the response
    Object.entries(response.fieldMappings).forEach(([fieldId, value]) => {
      onFieldChange(fieldId, value);
    });
    setShowPreviewModal(false);
    setLocalSearchQuery('');
  };

  // Field value matches - DISABLED since search bar is removed
  // This functionality was tied to the maintenance section search bar
  useEffect(() => {
    // Clear field suggestions since search is removed
    setFieldSuggestions([]);
    setShowSuggestions(false);
  }, [formData, activeField]);



  const handleSuggestionSelect = (response: AIResponse) => {
    // Apply all fields from this citation
    Object.entries(response.fieldMappings).forEach(([fieldId, value]) => {
      onFieldChange(fieldId, value);
    });
    setShowSuggestions(false);
    setActiveField(null);
  };

  const handleFieldFocus = (fieldId: string) => {
    setActiveField(fieldId);
  };

  const handleFieldBlur = () => {
    // Delay to allow click on suggestion
    setTimeout(() => {
      setActiveField(null);
      setShowSuggestions(false);
    }, 200);
  };

  const renderFieldWithAutocomplete = (
    fieldId: string,
    label: string,
    placeholder: string,
    value: string,
    isTextarea: boolean = false
  ) => {
    const hasSuggestions = showSuggestions && activeField === fieldId && fieldSuggestions.length > 0;

    // If in AI mode, use GhostFormField
    if (aiMode) {
      const ghostValue = ghostValues[fieldId];
      const hasGhost = Boolean(ghostValue && !value);
      
      return (
        <GhostFormField
          id={fieldId}
          label={label}
          value={value}
          ghostValue={ghostValue}
          placeholder={placeholder}
          isTextarea={isTextarea}
          onChange={(newValue) => onFieldChange(fieldId, newValue)}
          onFocus={() => handleFieldFocus(fieldId)}
          onBlur={handleFieldBlur}
          onAcceptGhost={onAcceptGhost}
          onFieldSearch={onFieldSearch}
          hasAISuggestion={hasGhost}
          sourceInfo={hasGhost ? {
            page: 12,
            snippet: `${ghostValue?.substring(0, 50) ?? ''}...`
          } : undefined}
          isAIApproved={isAIApproved}
          aiMode={aiMode}
          hasAIGeneratedValue={hasAIGeneratedFields[fieldId] || false}
        />
      );
    }

    return (
      <div id={`field-${fieldId}`} className="relative">
        <label className="block text-sm text-gray-700 mb-2">
          {label} <span className="text-red-500">*</span>
        </label>
        <div className="relative">
          {isTextarea ? (
            <textarea
              placeholder={placeholder}
              value={value}
              onChange={(e) => onFieldChange(fieldId, e.target.value)}
              onFocus={() => handleFieldFocus(fieldId)}
              onBlur={handleFieldBlur}
              rows={3}
              className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none transition-all"
            />
          ) : (
            <input
              type="text"
              placeholder={placeholder}
              value={value}
              onChange={(e) => onFieldChange(fieldId, e.target.value)}
              onFocus={() => handleFieldFocus(fieldId)}
              onBlur={handleFieldBlur}
              className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all"
            />
          )}
          
          {/* Autocomplete suggestions dropdown */}
          {hasSuggestions && (
            <div 
              ref={(el) => { suggestionRefs.current[fieldId] = el; }}
              className="absolute top-full left-0 right-0 mt-1 bg-white border border-gray-300 rounded-lg shadow-lg z-50 max-h-60 overflow-y-auto"
            >
              {fieldSuggestions.map((response) => (
                <button
                  key={response.id}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    handleSuggestionSelect(response);
                  }}
                  className="w-full px-4 py-3 text-left hover:bg-purple-50 border-b border-gray-100 last:border-b-0 transition-colors"
                >
                  <div className="text-sm font-medium text-gray-900 mb-1">
                    {response.title}
                  </div>
                  <div className="text-xs text-gray-600 line-clamp-1">
                    {response.fieldMappings[fieldId]}
                  </div>
                  {response.citation && (
                    <div className="text-xs text-gray-500 italic mt-1">
                      {response.citation}
                    </div>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  };

  const renderFormFields = () => {
    if (category === 'identification') {
      return (
        <>
          <div className="grid grid-cols-2 gap-4 mb-4">
            {renderFieldWithAutocomplete(
              'agreementName',
              'Agreement name',
              'Agreement name',
              formData.agreementName
            )}
            {renderFieldWithAutocomplete(
              'agreementDate',
              'Agreement date',
              'MM/DD/YYYY',
              formData.agreementDate
            )}
          </div>
          {renderFieldWithAutocomplete(
            'notes',
            'Notes',
            'Enter any additional notes about this agreement',
            formData.notes,
            true
          )}
        </>
      );
    }

    if (category === 'maintenance') {
      // Check if any maintenance field has AI-generated values (after Accept is pressed)
      const maintenanceFields = ['responsibleParty', 'maintenanceOwnerResponsibility', 'maintenanceReasoning'];
      const hasAIFilledValues = maintenanceFields.some(fieldId => hasAIGeneratedFields[fieldId]);
      
      const currentSnippet = snippets.length > 0 && currentSnippetIndex < snippets.length 
        ? snippets[currentSnippetIndex] 
        : null;
      
      return (
        <div className="space-y-4">
          {/* 1. Document Selection Table - Maintenance section starts here */}
          {documents.length > 0 && (
            <div className="mb-6">
              <p className="text-sm text-gray-500 mb-3">
                The documents below are available in the Documents tab on the Land details page
              </p>
              
              <div className="border border-gray-200 rounded-lg overflow-hidden">
                <table className="w-full">
                  <thead className="bg-gray-50 border-b border-gray-200">
                    <tr>
                      <th className="px-4 py-2 text-left">
                        <input
                          type="checkbox"
                          checked={checkedDocuments.length === documents.length}
                          onChange={(e) => {
                            if (onDocumentCheckChange) {
                              documents.forEach(doc => {
                                onDocumentCheckChange(doc.id, e.target.checked);
                              });
                            }
                          }}
                          className="w-4 h-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                        />
                      </th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-700">File name</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-700">Uploaded by</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-700">Uploaded date</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-700">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {documents.map((doc) => (
                      <tr key={doc.id} className="hover:bg-gray-50">
                        <td className="px-4 py-3">
                          <input
                            type="checkbox"
                            checked={checkedDocuments.includes(doc.id)}
                            onChange={(e) => {
                              if (onDocumentCheckChange) {
                                onDocumentCheckChange(doc.id, e.target.checked);
                              }
                            }}
                            className="w-4 h-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                          />
                        </td>
                        <td className="px-4 py-3">
                          <button className="text-blue-600 hover:text-blue-800 text-sm font-medium">
                            {doc.name}
                          </button>
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-900">svc_team_24247137</td>
                        <td className="px-4 py-3 text-sm text-gray-900">1/7/2026 2:08PM</td>
                        <td className="px-4 py-3">
                          <span className="inline-flex px-2 py-1 rounded-full text-xs font-medium bg-green-100 text-green-800">
                            New
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                
                <div className="bg-white border-t border-gray-200 px-4 py-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <select className="border border-gray-300 rounded px-2 py-1 text-sm">
                      <option>10</option>
                      <option>25</option>
                      <option>50</option>
                    </select>
                    <span className="text-sm text-gray-600">1 - 1 of 1 rows</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <button className="p-1 text-gray-400 hover:text-gray-600">
                      <ChevronLeft className="w-4 h-4" />
                    </button>
                    <button className="p-1 text-gray-400 hover:text-gray-600">
                      <ChevronRight className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* 2. Search bar + AI Search button */}
          <div
            className="flex flex-wrap items-center gap-2 w-full mb-1"
            style={{ marginTop: '20px', marginBottom: '20px' }}
          >
            <div className="relative flex-1 min-w-0">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
              <Input
                type="text"
                placeholder="Search"
                className="pl-9 w-full"
                value={localSearchQuery}
                onChange={(e) => {
                  setLocalSearchQuery(e.target.value);
                  setShowSearchRequiredWarning(false);
                }}
                onKeyDown={handleSearchKeyDown}
                style={{ paddingLeft: '33px' }}
              />
            </div>
            {onToggleAiMode && (
              <button
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  if (checkedDocuments.length === 0) {
                    alert('Please select at least one document before searching');
                    return;
                  }
                  if (!localSearchQuery.trim()) {
                    setShowSearchRequiredWarning(true);
                    return;
                  }
                  setShowSearchRequiredWarning(false);
                  if (!aiMode) onToggleAiMode(true);
                  handleSearch();
                }}
                disabled={checkedDocuments.length === 0}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 flex-shrink-0 ${
                  checkedDocuments.length === 0
                    ? 'bg-gray-200 text-gray-400 cursor-not-allowed'
                    : aiMode
                    ? 'bg-blue-600 text-white hover:bg-blue-700 shadow-sm'
                    : 'bg-gray-300 text-gray-700 hover:bg-gray-400'
                }`}
                title={checkedDocuments.length === 0 ? 'Please select at least one document' : 'Enter search text and click to search'}
                style={{ position: 'relative', borderRadius: '60px' }}
              >
                <Search className="w-4 h-4" />
                AI Search
              </button>
            )}
          </div>

          {/* Required field warning - show when AI Search clicked with empty search */}
          {showSearchRequiredWarning && (
            <p className="text-sm text-red-600 mb-4">Please enter a search term in the search bar to use AI Search.</p>
          )}

          {/* AI Filled Status Message - Show when fields have AI-generated values (after Accept) */}
          {hasAIFilledValues && (
            <div className="bg-green-50 border border-green-200 rounded-lg p-3 flex items-start gap-3">
              <div className="flex-shrink-0 mt-0.5">
                <div className="w-2 h-2 bg-green-500 rounded-full"></div>
              </div>
              <div className="flex-1">
                <p className="text-sm text-green-900">
                  <span className="font-semibold">These fields were filled by AI.</span>
                  {' '}You can edit them as needed.
                </p>
              </div>
            </div>
          )}

          {/* AI Snippets Section — show while loading, when rows exist, or when search returned no obligations */}
          {aiMode && (snippets.length > 0 || isAnalyzing || aiSearchHadNoResults) && (
            <div className="mb-6">
              {/* Main Card Container — loading overlay only until first obligation streams in */}
              <div className="relative bg-white border border-gray-200 rounded-lg shadow-sm overflow-hidden">
                <div
                  className={`transition-opacity duration-200 ${
                    isAnalyzing && snippets.length === 0 ? 'opacity-50' : 'opacity-100'
                  }`}
                >
                {/* Header Section */}
                <div className="px-4 py-3 border-b border-gray-200 bg-white flex items-center justify-between gap-3">
                  <div className="flex flex-col gap-1 min-w-0">
                    <h3 className="text-sm font-semibold text-gray-900">AI interpretations from agreement</h3>
                    {isAnalyzing && (
                      <p className="text-xs text-gray-500">
                        {snippets.length === 0
                          ? 'Streaming merge response — obligations appear in this list as each complete obligation object arrives in the JSON (same data the backend prints line by line).'
                          : `${snippets.length} obligation${snippets.length === 1 ? '' : 's'} received so far; the list updates as more objects finish streaming.`}
                      </p>
                    )}
                  </div>
                  
                  {/* Navigation Controls and View Legal Evidence - Header Right */}
                  <div className="flex items-center gap-3">
                    {isAnalyzing && snippets.length > 0 && (
                      <div
                        className="flex flex-shrink-0 items-center gap-1.5 text-red-600"
                        aria-live="polite"
                        aria-busy="true"
                        title="Still loading obligations"
                      >
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                      </div>
                    )}
                    {/* View Legal Evidence Button */}
                    {currentSnippet && currentSnippet.citations && currentSnippet.citations.length > 0 && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setFlippedSnippetId(flippedSnippetId === currentSnippet.id ? null : currentSnippet.id);
                        }}
                        className="text-sm text-red-600 hover:text-red-700 font-medium flex items-center gap-1"
                      >
                        {flippedSnippetId === currentSnippet.id ? 'View Snippets' : 'View Legal Evidence'}
                        {flippedSnippetId !== currentSnippet.id && <span className="text-lg"></span>}
                      </button>
                    )}
                    
                    {snippets.length > 1 && (
                      <div className="flex items-center gap-2 border-l border-gray-300 pl-3">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setCurrentSnippetIndex((prev) => (prev - 1 + snippets.length) % snippets.length);
                            setFlippedSnippetId(null);
                          }}
                          disabled={snippets.length <= 1}
                          className="p-1 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                          title="Previous snippet"
                        >
                          <ChevronLeft className="w-4 h-4" />
                        </button>
                        <span className="text-sm text-gray-600">
                          {currentSnippetIndex + 1} of {snippets.length} rows
                        </span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setCurrentSnippetIndex((prev) => (prev + 1) % snippets.length);
                            setFlippedSnippetId(null);
                          }}
                          disabled={snippets.length <= 1}
                          className="p-1 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                          title="Next snippet"
                        >
                          <ChevronRight className="w-4 h-4" />
                        </button>
                      </div>
                    )}
                  </div>
                </div>

                {/* Stream activity log (disabled — re-enable when debugging raw stream text)
                {queryStreamProgressLog.trim() && isAnalyzing && (
                  <div className="border-t border-gray-100 bg-slate-50 px-4 py-2">
                    <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-500 mb-1">
                      Stream activity
                    </p>
                    <pre
                      className="text-[11px] leading-snug font-mono text-slate-700 whitespace-pre-wrap break-words max-h-28 overflow-y-auto"
                      aria-live="polite"
                    >
                      {queryStreamProgressLog}
                    </pre>
                  </div>
                )}
                */}

                {/* Content Section — initial fetch: overlay on card shows spinner; keep min height for layout */}
                {snippets.length === 0 && isAnalyzing ? (
                  <div
                    className="min-h-[14rem] border-t border-gray-100 bg-gray-50/30"
                    aria-hidden
                  />
                ) : snippets.length === 0 && !isAnalyzing && aiSearchHadNoResults ? (
                  <div
                    className="px-4 py-12 flex flex-col items-center justify-center gap-2 text-gray-600 border-t border-gray-100 bg-gray-50/50"
                    role="status"
                    aria-live="polite"
                  >
                    <p className="text-sm font-medium text-gray-800 text-center">No obligations found</p>
                    <p className="text-xs text-gray-500 text-center max-w-md">
                      The search completed successfully, but no matching obligations were returned for your query and selected documents.
                    </p>
                  </div>
                ) : currentSnippet ? (
                  <div className="p-4">
                    {currentSnippet.category ? (
                      <div className="mb-3">
                        <span
                          className="inline-flex items-center rounded-full bg-indigo-50 text-indigo-900 border border-indigo-100 px-3 py-1 text-xs font-semibold tracking-tight"
                          title="Obligation category from the agreement index"
                        >
                          {currentSnippet.category}
                        </span>
                      </div>
                    ) : null}
                    {/* Flip Card Container */}
                    <div
                      className="relative w-full"
                      style={{ perspective: '1000px' }}
                    >
                      <div
                        className="relative w-full"
                        style={{ 
                          transformStyle: 'preserve-3d',
                          transform: flippedSnippetId === currentSnippet.id ? 'rotateY(180deg)' : 'rotateY(0deg)'
                        }}
                      >
                        {/* Front of Card - Snippet Content (fixed height to match PDF back) */}
                        <div
                          className="w-full"
                          style={{ 
                            backfaceVisibility: 'hidden',
                            WebkitBackfaceVisibility: 'hidden',
                            visibility: flippedSnippetId === currentSnippet.id ? 'hidden' : 'visible',
                            position: flippedSnippetId === currentSnippet.id ? 'absolute' : 'relative',
                            top: 0,
                            left: 0,
                            right: 0,
                            minHeight: '300px'
                          }}
                        >
                          <div className="space-y-2">
                            {/* Responsible Party Card - No minimum height */}
                            {currentSnippet.fieldMappings.responsibleParty && (
                              <ExpandableCard
                                title="Responsible Party"
                                content={currentSnippet.fieldMappings.responsibleParty}
                                maxLength={450}
                                minHeight='30px'
                                maxHeight="80px"
                              />
                            )}

                            {/* Maintenance Owner Responsibility Card - Minimum height equivalent to ~200 chars */}
                            {currentSnippet.fieldMappings.maintenanceOwnerResponsibility && (
                              <ExpandableCard
                                title="Maintenance Owner Responsibility"
                                content={currentSnippet.fieldMappings.maintenanceOwnerResponsibility}
                                maxLength={450}
                                minHeight="45px"
                                maxHeight="100px"
                              />
                            )}

                            {/* Maintenance Reasoning Card - Minimum height equivalent to ~200 chars */}
                            {(currentSnippet.fieldMappings.maintenanceReasoning || currentSnippet.pdfReference.segment || currentSnippet.pdfReference.context) && (
                              <ExpandableCard
                                title="Maintenance Reasoning"
                                content={(() => {
                                  const parts = [];
                                  if (currentSnippet.pdfReference.segment) {
                                    parts.push(`Per Section ${currentSnippet.pdfReference.page}, ${currentSnippet.pdfReference.segment}`);
                                  }
                                  if (currentSnippet.pdfReference.context) {
                                    parts.push(currentSnippet.pdfReference.context);
                                  }
                                  if (currentSnippet.fieldMappings.maintenanceReasoning) {
                                    parts.push(currentSnippet.fieldMappings.maintenanceReasoning);
                                  }
                                  return parts.join('\n\n');
                                })()}
                                maxLength={450}
                                isBulletList={true}
                                minHeight="45px"
                                maxHeight="100px"
                              />
                            )}

                          </div>
                        </div>

                        {/* Back of Card - PDF Viewer (same height as snippet front) */}
                        <div
                          className="w-full"
                          style={{ 
                            backfaceVisibility: 'hidden',
                            WebkitBackfaceVisibility: 'hidden',
                            transform: 'rotateY(180deg)',
                            visibility: flippedSnippetId === currentSnippet.id ? 'visible' : 'hidden',
                            position: flippedSnippetId === currentSnippet.id ? 'relative' : 'absolute',
                            top: 0,
                            left: 0,
                            right: 0,
                            minHeight: '300px'
                          }}
                        >
                          <div className="relative h-full">
                            {currentSnippet.citations && currentSnippet.citations.length > 0 ? (
                              (() => {
                                const allPageNumbers = currentSnippet.citations!.flatMap(cit => cit.pageNumbers);
                                const uniquePageNumbers = Array.from(new Set(allPageNumbers)).sort((a, b) => a - b);
                                const firstCitation = currentSnippet.citations![0];
                                const currentPage = uniquePageNumbers[pdfPageIndex] ?? uniquePageNumbers[0];
                                
                                return (
                                  <div>
                                    {/* Legal Evidence Header - contains title, doc link, View Snippets, AND page numbers */}
                                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                                      <div className="flex items-center gap-2">
                                        <h4 className="text-sm font-semibold text-gray-900">Legal Evidence</h4>
                                        <button className="text-blue-600 hover:text-blue-800 text-sm">
                                          {firstCitation.docId}
                                        </button>
                                      </div>
                                      <div className="flex items-center gap-2">
                                        {/* Page numbers - inside Legal Evidence div */}
                                        {uniquePageNumbers.length > 1 && (
                                          <div className="flex items-center gap-1">
                                            <button
                                              onClick={() => setPdfPageIndex(Math.max(0, pdfPageIndex - 1))}
                                              disabled={pdfPageIndex === 0}
                                              className="p-1 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                              title="Previous page"
                                            >
                                              <ChevronLeft className="w-4 h-4" />
                                            </button>
                                            <div className="flex items-center gap-1 flex-wrap">
                                              {uniquePageNumbers.map((pageNum, idx) => (
                                                <button
                                                  key={pageNum}
                                                  onClick={() => setPdfPageIndex(idx)}
                                                  className={`px-2 py-1 text-xs font-medium rounded transition-colors ${
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
                                              onClick={() => setPdfPageIndex(Math.min(uniquePageNumbers.length - 1, pdfPageIndex + 1))}
                                              disabled={pdfPageIndex >= uniquePageNumbers.length - 1}
                                              className="p-1 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                              title="Next page"
                                            >
                                              <ChevronRight className="w-4 h-4" />
                                            </button>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                    
                                    <PDFViewer
                                      documentName={firstCitation.docId}
                                      pageNumbers={uniquePageNumbers}
                                      hidePageNavigation
                                      controlledPageIndex={pdfPageIndex}
                                      containerHeight="260px"
                                    />
                                  </div>
                                );
                              })()
                            ) : (
                              <div className="flex items-center justify-center h-full bg-gray-50 rounded-lg border border-gray-200">
                                <p className="text-sm text-gray-500">No PDF reference available</p>
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Footer with Accept Button only */}
                    <div className="mt-4 pt-4  border-gray-200 flex justify-end">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onApplySnippet && currentSnippet) {
                            onApplySnippet(currentSnippet, false);
                          }
                        }}
                        className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors text-sm font-medium shadow-sm"
                        style={{ position:'relative', borderRadius:'60px', top:'-15px'}}
                      >
                        Accept
                      </button>
                    </div>
                  </div>
                ) : null}
                </div>

                {isAnalyzing && snippets.length === 0 && (
                  <div
                    className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 rounded-lg bg-white/40 backdrop-blur-sm"
                    aria-live="polite"
                    aria-busy="true"
                  >
                    <Loader2 className="h-10 w-10 animate-spin text-red-600" aria-hidden />
                    <p className="text-sm font-medium text-gray-800">Fetching obligations…</p>
                  </div>
                )}
              </div>
            </div>
          )}
          
          {renderFieldWithAutocomplete(
            'responsibleParty',
            'Responsible party',
            'Responsible party',
            formData.responsibleParty || ''
          )}
          {renderFieldWithAutocomplete(
            'maintenanceOwnerResponsibility',
            'Maintenance owner responsibility',
            'Maintenance owner responsibility',
            formData.maintenanceOwnerResponsibility || ''
          )}
          {renderFieldWithAutocomplete(
            'maintenanceReasoning',
            'Maintenance reasoning',
            'Maintenance reasoning',
            formData.maintenanceReasoning || ''
          )}
        </div>
      );
    }

    if (category === 'billing') {
      return (
        <div className="grid grid-cols-2 gap-4">
          {renderFieldWithAutocomplete(
            'billingContact',
            'Billing contact',
            'Billing contact',
            formData.billingContact,
            true
          )}
          {renderFieldWithAutocomplete(
            'billingAgreement',
            'Billing agreement',
            'Billing agreement',
            formData.billingAgreement,
            true
          )}
        </div>
      );
    }

    return null;
  };

  return (
    <div className="border-b border-gray-200" data-section={category}>
      <div className="px-6 py-6">
        {/* Category Header - For maintenance, only show heading (document table, search bar are in form fields below) */}
        <div className="mb-4">
          <h2 className="text-gray-900 font-bold text-xl">{title}</h2>
        </div>

        {/* Form Fields */}
        {renderFormFields()}
      </div>

      {/* Preview Modal for PDF References */}
      {showPreviewModal && selectedPreviewResponse && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => setShowPreviewModal(false)}
          />
          
          {/* Modal Content */}
          <div className="relative bg-white rounded-xl shadow-2xl max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col">
            {/* Modal Header */}
            <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
              <div>
                <h3 className="text-gray-900 mb-1">{selectedPreviewResponse.title}</h3>
                <p className="text-sm text-gray-600">{selectedPreviewResponse.citation}</p>
              </div>
              <button
                onClick={() => setShowPreviewModal(false)}
                className="p-1 hover:bg-gray-100 rounded-full transition-colors"
              >
                <X className="w-5 h-5 text-gray-600" />
              </button>
            </div>
            
            {/* Modal Body - PDF References */}
            <div className="flex-1 overflow-y-auto px-6 py-4">
              <div className="space-y-4">
                <h4 className="text-sm font-medium text-gray-700 mb-3">
                  Referenced from {selectedPreviewResponse.pdfReferences?.length || 0} location(s) in document:
                </h4>
                
                {selectedPreviewResponse.pdfReferences?.map((ref, index) => (
                  <div key={index} className="border border-gray-200 rounded-lg p-4 bg-gray-50">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-medium text-purple-600 bg-purple-100 px-2 py-1 rounded">
                        Page {ref.page}
                      </span>
                      <span className="text-xs text-gray-500">Reference {index + 1}</span>
                    </div>
                    
                    <div className="mb-2">
                      <div className="text-xs text-gray-600 mb-1">Segment:</div>
                      <div className="text-sm bg-yellow-100 px-2 py-1 rounded inline-block">
                        "{ref.segment}"
                      </div>
                    </div>
                    
                    <div className="mb-3">
                      <div className="text-xs text-gray-600 mb-1">Context:</div>
                      <p className="text-sm text-gray-700 leading-relaxed">
                        {ref.context}
                      </p>
                    </div>
                    
                    <button
                      onClick={() => {
                        // Open PDF in new tab at specific page
                        // In a real application, this would open the actual PDF file
                        const pdfUrl = `/documents/agreement.pdf#page=${ref.page}`;
                        window.open(pdfUrl, '_blank');
                      }}
                      className="w-full px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors text-sm flex items-center justify-center gap-2"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                      </svg>
                      Open PDF at Page {ref.page}
                    </button>
                  </div>
                ))}
              </div>
            </div>
            
            {/* Modal Footer */}
            <div className="px-6 py-4 border-t border-gray-200 flex items-center justify-end gap-3">
              <button
                onClick={() => setShowPreviewModal(false)}
                className="px-4 py-2 text-gray-700 hover:bg-gray-100 rounded-lg transition-colors"
              >
                Close
              </button>
              <button
                onClick={() => {
                  handleApplyCitation(selectedPreviewResponse);
                  setShowPreviewModal(false);
                }}
                className="px-6 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors flex items-center gap-2"
              >
                <Check className="w-4 h-4" />
                Apply to Form
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{`
        @keyframes chaseBorder {
          0% {
            transform: rotate(0deg);
          }
          100% {
            transform: rotate(360deg);
          }
        }
        
        .rotating-border-container {
          position: relative;
        }
        
        .rotating-border {
          position: absolute;
          top: -3px;
          left: -3px;
          right: -3px;
          bottom: -3px;
          border-radius: 10px;
          background: conic-gradient(
            from 0deg,
            transparent 0%,
            transparent 85%,
            rgb(147, 51, 234) 92%,
            rgb(147, 51, 234) 95%,
            transparent 98%,
            transparent 100%
          );
          animation: chaseBorder 1s linear infinite;
          z-index: 0;
        }
        
        .rotating-border::before {
          content: '';
          position: absolute;
          inset: 3px;
          background: white;
          border-radius: 8px;
          z-index: 1;
        }
        
        .rotating-border-container input {
          position: relative;
          z-index: 10;
        }

      `}</style>
    </div>
  );
}