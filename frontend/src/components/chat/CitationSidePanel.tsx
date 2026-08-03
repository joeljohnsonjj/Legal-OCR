import { ArrowLeft, FileText } from 'lucide-react';
import { CHAT_BORDER, CHAT_TEXT_SECONDARY } from '../../constants/landRecord';
import type { SourcePanelItem } from './citationParse';

export type CitationPanelState = {
  title: string;
  subtitle?: string;
  items: SourcePanelItem[];
} | null;

type Props = {
  state: CitationPanelState;
  onClose: () => void;
  onCitationClick: (item: SourcePanelItem) => void;
};

function documentLabel(name: string): string {
  const n = (name || '').trim();
  if (!n || n === 'Document') return 'Document';
  return /\.pdf$/i.test(n) ? n : `${n}.pdf`;
}

/** Inline font sizes — arbitrary Tailwind text-[Npx] is often missing from the CSS bundle. */
const CARD = {
  doc: { fontSize: 11, lineHeight: 1.4, fontWeight: 500 as const },
  duty: { fontSize: 13, lineHeight: 1.4, fontWeight: 600 as const },
  preview: { fontSize: 12, lineHeight: 1.5, fontWeight: 400 as const },
  btn: { fontSize: 11, lineHeight: 1.25, fontWeight: 500 as const },
  hint: { fontSize: 11, lineHeight: 1.45, fontWeight: 400 as const },
};

/**
 * Full-bleed overlay over the chat message column.
 * No top chrome bar — compact back control sits in the scroll area; Escape also closes (handled in ChatSidebar).
 */
export function CitationSidePanel({ state, onClose, onCitationClick }: Props) {
  const open = Boolean(state?.items?.length);
  if (!open || !state) return null;

  return (
    <div
      className="absolute inset-0 z-30 flex flex-col bg-white shadow-[inset_0_1px_0_0_rgba(0,0,0,0.06)]"
      role="region"
      aria-label="Citation sources"
    >
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-3 pt-3 text-left">
          <div className="mb-3 flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg border-0 bg-slate-100 text-slate-700 hover:bg-slate-200"
              aria-label="Back to chat"
            >
              <ArrowLeft className="h-4 w-4" strokeWidth={2} />
            </button>
          </div>
          <p className="mb-3" style={{ ...CARD.hint, color: CHAT_TEXT_SECONDARY }}>
            Each card shows the document, duty type, and a short excerpt. Use{' '}
            <span style={{ fontWeight: 600 }}>Open in PDF</span> to view the source page.
          </p>
          <ul className="flex flex-col gap-3">
            {state.items.map((item) => (
              <li key={item.id}>
                <div
                  className="flex flex-col gap-1.5 rounded-xl border bg-white px-3 py-2.5 shadow-sm"
                  style={{
                    borderColor: CHAT_BORDER,
                    boxShadow: '0 1px 2px rgba(15, 23, 42, 0.05), 0 0 0 1px rgba(15, 23, 42, 0.04)',
                  }}
                >
                  <p
                    className="truncate tracking-wide text-slate-500"
                    style={CARD.doc}
                    title={documentLabel(item.documentName)}
                  >
                    <FileText
                      className="mr-1 inline-block align-text-bottom text-slate-400"
                      style={{ width: 12, height: 12 }}
                      strokeWidth={2}
                    />
                    {documentLabel(item.documentName)}
                  </p>
                  <p className="text-slate-900" style={CARD.duty}>
                    {item.dutyType}
                  </p>
                  {item.responsibilityPreview ? (
                    <p className="line-clamp-2 text-slate-600" style={CARD.preview}>
                      {item.responsibilityPreview}
                    </p>
                  ) : null}
                  <div className="flex justify-end pt-0.5">
                    <button
                      type="button"
                      onClick={() => onCitationClick(item)}
                      className="rounded-md border border-slate-200 bg-slate-50 text-slate-700 transition-colors hover:border-slate-300 hover:bg-slate-100"
                      style={{ ...CARD.btn, padding: '6px 12px' }}
                    >
                      Open in PDF
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
