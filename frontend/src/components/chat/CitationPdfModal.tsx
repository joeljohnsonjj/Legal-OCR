import { useEffect, useRef, type CSSProperties } from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { PDFViewer } from '../PDFViewer';
import { CHAT_TEXT_PRIMARY, CHAT_TEXT_SECONDARY } from '../../constants/landRecord';

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  documentName: string;
  pageNumbers: number[];
  citationTitle: string;
};

/** Z-index above chat shell (z-60) and app chrome; inline styles avoid stacking/animation bugs. */
const PDF_MODAL_OVERLAY_Z = 10000;
const PDF_MODAL_CONTENT_Z = 10001;

/** Wider than half-viewport (~60vw+), capped at 98vw; vertically centered on screen. */
const MODAL_CONTENT_STYLE: CSSProperties = {
  zIndex: PDF_MODAL_CONTENT_Z,
  position: 'fixed',
  left: '50%',
  top: '50%',
  transform: 'translate(-50%, -50%)',
  pointerEvents: 'auto',
  boxSizing: 'border-box',
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'stretch',
  width: 'min(98vw, max(60vw, 52rem))',
  minWidth: 'max(58vw, 22rem)',
  maxHeight: 'min(92vh, 56rem)',
  minHeight: 'min(56vh, 36rem)',
};

/**
 * Large PDF preview above the chat overlay. Uses public/docs/{name}.pdf.
 */
export function CitationPdfModal({
  open,
  onOpenChange,
  documentName,
  pageNumbers,
  citationTitle,
}: Props) {
  const pages = pageNumbers.length ? pageNumbers : [1];
  const invalidDoc = !documentName || documentName === 'Unknown';

  /** Radix treats the click that opened the dialog as "outside" the content and dismisses immediately when there is no DialogTrigger. */
  const allowOutsideCloseRef = useRef(true);
  useEffect(() => {
    if (!open) {
      allowOutsideCloseRef.current = true;
      return;
    }
    allowOutsideCloseRef.current = false;
    const id = window.setTimeout(() => {
      allowOutsideCloseRef.current = true;
    }, 300);
    return () => window.clearTimeout(id);
  }, [open]);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className="fixed inset-0 bg-black/55"
          style={{
            zIndex: PDF_MODAL_OVERLAY_Z,
            pointerEvents: 'auto',
          }}
        />
        <DialogPrimitive.Content
          className="flex min-w-0 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-0 shadow-2xl"
          style={MODAL_CONTENT_STYLE}
          aria-describedby={undefined}
          onOpenAutoFocus={(e) => e.preventDefault()}
          onPointerDownOutside={(e) => {
            if (!allowOutsideCloseRef.current) e.preventDefault();
          }}
          onInteractOutside={(e) => {
            if (!allowOutsideCloseRef.current) e.preventDefault();
          }}
        >
          <div className="flex flex-shrink-0 flex-col items-center gap-0.5 border-b border-slate-200 px-5 py-3 text-center pr-14 pl-14">
            <DialogPrimitive.Title className="w-full text-base font-semibold leading-snug" style={{ color: CHAT_TEXT_PRIMARY }}>
              {citationTitle}
            </DialogPrimitive.Title>
            <p className="w-full text-xs" style={{ color: CHAT_TEXT_SECONDARY }}>
              {invalidDoc ? 'Could not detect document name in citation.' : documentName}
              {!invalidDoc ? ` · Pages: ${pages.join(', ')}` : null}
            </p>
          </div>
          <DialogPrimitive.Close
            className="absolute top-3 right-3 flex h-9 w-9 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100"
            aria-label="Close PDF"
          >
            <span className="text-xl leading-none">×</span>
          </DialogPrimitive.Close>

          <div
            className="flex min-h-0 min-w-0 flex-1 flex-col items-center overflow-hidden px-4 pb-3 pt-2"
            style={{ minHeight: 'min(50vh, 480px)', maxHeight: 'min(76vh, calc(92vh - 5.5rem))' }}
          >
            {invalidDoc ? (
              <div className="flex items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50 p-6 text-center text-sm text-slate-600" style={{ minHeight: 'min(50vh, 480px)' }}>
                Add the PDF under <code className="mx-1 rounded bg-slate-200 px-1">public/docs</code> and ensure the
                citation includes <code className="mx-1 rounded bg-slate-200 px-1">Document: filename.pdf</code>.
              </div>
            ) : (
              <PDFViewer
                key={`${documentName}-${pages.join('-')}`}
                documentName={documentName}
                pageNumbers={pages}
                hidePageNavigation={pages.length <= 1}
                layout="modal"
                containerHeight="min(70vh, calc(92vh - 6rem))"
                className="flex min-h-0 w-full min-w-0 max-w-full flex-1 flex-col"
              />
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
