import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { ChevronDown, ChevronUp, RotateCcw, Send } from 'lucide-react';
import { chatQueryStream, chatReset } from '../services/apiService';
import {
  CHAT_ACCENT_ON_LIGHT,
  CHAT_ACCENT_ON_RED,
  CHAT_AREA_BG,
  CHAT_ASSISTANT_NAME,
  CHAT_BORDER,
  CHAT_CARD_BG,
  CHAT_PANEL_WIDTH,
  CHAT_TEXT_PRIMARY,
  CHAT_TEXT_SECONDARY,
  CHAT_USER_BUBBLE_BG,
  DEFAULT_LAND_RECORD_ID,
  HEB_RED,
  HEB_RED_HOVER,
} from '../constants/landRecord';

const SHEET_MS = 280;

const PANEL_RADIUS = 16;
const BUBBLE_RADIUS = 18;

/** Max chat bubble width as % of the scroll column (fluid with panel resize). */
const BUBBLE_MAX_WIDTH_PCT = 80;

const MIN_CHAT_W = 280;
const MIN_CHAT_H = 220;
const MAX_CHAT_W_CAP = 720;

/** Fixed title bar height (expanded or collapsed chrome). */
const HEADER_H = 48;
/** Hit width for top-left / top-right diagonal resize inside the header row. */
const HEADER_CORNER_HANDLE_W = 18;

const HANDLE = 8;
const CORNER = 16;

type ResizeEdge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

function defaultChatHeightPx(): number {
  if (typeof window === 'undefined') return 520;
  return Math.min(560, Math.max(MIN_CHAT_H, Math.round(window.innerHeight * 0.72 - 120)));
}

function clampSize(w: number, h: number): { w: number; h: number } {
  const maxW = Math.min(
    MAX_CHAT_W_CAP,
    typeof window !== 'undefined' ? window.innerWidth - 48 : MAX_CHAT_W_CAP
  );
  const maxH =
    typeof window !== 'undefined'
      ? Math.min(Math.round(window.innerHeight * 0.92), window.innerHeight - 48)
      : 800;
  return {
    w: Math.round(Math.min(maxW, Math.max(MIN_CHAT_W, w))),
    h: Math.round(Math.min(maxH, Math.max(MIN_CHAT_H, h))),
  };
}

function sizeFromEdge(
  edge: ResizeEdge,
  dx: number,
  dy: number,
  sw: number,
  sh: number
): { w: number; h: number } {
  let w = sw;
  let h = sh;
  if (edge === 'e' || edge === 'ne' || edge === 'se') w += dx;
  if (edge === 'w' || edge === 'nw' || edge === 'sw') w -= dx;
  if (edge === 's' || edge === 'se' || edge === 'sw') h += dy;
  if (edge === 'n' || edge === 'ne' || edge === 'nw') h -= dy;
  return clampSize(w, h);
}

export interface ChatSidebarProps {
  open: boolean;
  onClose: () => void;
  landRecordId?: string;
}

type UiMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
};

type ChatSessionIds = {
  userId: string;
  runId: string;
};

type ChatStorageState = {
  messages: UiMessage[];
  session: ChatSessionIds;
};

function normalizeAssistantAnswer(text: string): string {
  return text.replace(/^answer:\s*/i, '').trim();
}

function storageKey(landRecordId: string): string {
  return `legal-ocr-chat:${landRecordId}`;
}

function introMessage(): UiMessage[] {
  return [{ id: 'intro', role: 'assistant', content: 'How can I help you?' }];
}

function createSessionIds(): ChatSessionIds {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return { userId: crypto.randomUUID(), runId: crypto.randomUUID() };
  }
  const stamp = Date.now();
  return {
    userId: `user-${stamp}-${Math.random().toString(16).slice(2)}`,
    runId: `run-${stamp}-${Math.random().toString(16).slice(2)}`,
  };
}

/** Linkify URLs and highlight “Clause … – …” spans like the reference mock. */
function formatInlineSegment(
  text: string,
  keyPrefix: string,
  accentColor: string = CHAT_ACCENT_ON_LIGHT
): ReactNode[] {
  const parts = text.split(/(Clause\s+\d+(?:\.\d+)*\s*[–—\-]\s*[^\n]+)/gi);
  const out: ReactNode[] = [];
  parts.forEach((part, i) => {
    if (!part) return;
    if (i % 2 === 1) {
      out.push(
        <span
          key={`${keyPrefix}-cl-${i}`}
          className="cursor-pointer underline"
          style={{ color: accentColor }}
        >
          {part}
        </span>
      );
    } else {
      out.push(...linkifyUrls(part, `${keyPrefix}-u-${i}`, accentColor));
    }
  });
  return out.length ? out : linkifyUrls(text, keyPrefix, accentColor);
}

function linkifyUrls(text: string, keyPrefix: string, linkColor: string = CHAT_ACCENT_ON_LIGHT): ReactNode[] {
  const urlRe = /(https?:\/\/[^\s]+)/g;
  const parts = text.split(urlRe);
  return parts.map((part, i) => {
    if (/^https?:\/\//.test(part)) {
      return (
        <a
          key={`${keyPrefix}-u-${i}`}
          href={part}
          className="underline"
          style={{ color: linkColor }}
          target="_blank"
          rel="noreferrer"
        >
          {part}
        </a>
      );
    }
    return <span key={`${keyPrefix}-t-${i}`}>{part}</span>;
  });
}

function formatMessageBody(
  content: string,
  keyBase: string,
  variant: 'assistant' | 'user'
): ReactNode {
  const lines = content.split('\n');
  const accent = variant === 'user' ? CHAT_ACCENT_ON_RED : CHAT_ACCENT_ON_LIGHT;
  const quoteStyle =
    variant === 'user'
      ? { color: 'rgba(255,255,255,0.95)' }
      : { color: CHAT_TEXT_SECONDARY };

  return lines.map((line, li) => {
    const trimmed = line.trim();
    const quoteChar = trimmed.length >= 2 ? trimmed[0] : '';
    const isFullyQuoted =
      (quoteChar === '"' || quoteChar === "'") && trimmed.endsWith(quoteChar);
    const body = isFullyQuoted ? (
      <em style={quoteStyle}>
        {quoteChar}
        {formatInlineSegment(trimmed.slice(1, -1), `${keyBase}-q-${li}`, accent)}
        {quoteChar}
      </em>
    ) : (
      <span
        className={variant === 'user' ? 'text-white' : ''}
        style={variant === 'assistant' ? { color: CHAT_TEXT_PRIMARY } : undefined}
      >
        {line
          .split(/(".*?"|'.*?')/g)
          .filter((segment) => segment.length > 0)
          .map((segment, si) => {
            const segQuote = segment.length >= 2 ? segment[0] : '';
            const isQuotedSegment =
              (segQuote === '"' || segQuote === "'") && segment.endsWith(segQuote);
            if (!isQuotedSegment) {
              return (
                <span key={`${keyBase}-s-${li}-${si}`}>
                  {formatInlineSegment(segment, `${keyBase}-s-${li}-${si}`, accent)}
                </span>
              );
            }
            const inner = segment.slice(1, -1);
            return (
              <em key={`${keyBase}-q-${li}-${si}`} style={quoteStyle}>
                {segQuote}
                {formatInlineSegment(inner, `${keyBase}-q-${li}-${si}`, accent)}
                {segQuote}
              </em>
            );
          })}
      </span>
    );
    return (
      <span key={`${keyBase}-ln-${li}`}>
        {li > 0 ? <br /> : null}
        {body}
      </span>
    );
  });
}

export function ChatSidebar({
  open,
  onClose,
  landRecordId = DEFAULT_LAND_RECORD_ID,
}: ChatSidebarProps) {
  const [messages, setMessages] = useState<UiMessage[]>(introMessage());
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [sendHovered, setSendHovered] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [sessionIds, setSessionIds] = useState<ChatSessionIds | null>(null);
  const [panelSize, setPanelSize] = useState(() => ({
    w: CHAT_PANEL_WIDTH,
    h: defaultChatHeightPx(),
  }));
  const resizeSessionRef = useRef<{
    edge: ResizeEdge;
    startX: number;
    startY: number;
    startW: number;
    startH: number;
    pointerId: number;
  } | null>(null);

  const [renderOverlay, setRenderOverlay] = useState(open);
  const [sheetEntered, setSheetEntered] = useState(false);

  const listRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevLandRef = useRef(landRecordId);

  useEffect(() => {
    if (open) {
      setRenderOverlay(true);
      setCollapsed(false);
      const id = requestAnimationFrame(() => {
        requestAnimationFrame(() => setSheetEntered(true));
      });
      return () => cancelAnimationFrame(id);
    }
    setSheetEntered(false);
    const t = window.setTimeout(() => setRenderOverlay(false), SHEET_MS);
    return () => window.clearTimeout(t);
  }, [open]);

  useEffect(() => {
    if (prevLandRef.current !== landRecordId) {
      prevLandRef.current = landRecordId;
    }
    if (typeof window === 'undefined') {
      return;
    }
    const raw = window.localStorage.getItem(storageKey(landRecordId));
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as ChatStorageState;
        if (parsed?.messages?.length) {
          setMessages(parsed.messages);
        } else {
          setMessages(introMessage());
        }
        setSessionIds(parsed?.session || createSessionIds());
      } catch {
        setMessages(introMessage());
        setSessionIds(createSessionIds());
      }
    } else {
      setMessages(introMessage());
      setSessionIds(createSessionIds());
    }
    setInput('');
    setError(null);
    setCollapsed(false);
  }, [landRecordId]);

  useEffect(() => {
    if (typeof window === 'undefined' || !sessionIds) {
      return;
    }
    const payload: ChatStorageState = { messages, session: sessionIds };
    window.localStorage.setItem(storageKey(landRecordId), JSON.stringify(payload));
  }, [messages, sessionIds, landRecordId]);

  useEffect(() => {
    const onWin = () => setPanelSize((s) => clampSize(s.w, s.h));
    window.addEventListener('resize', onWin);
    return () => window.removeEventListener('resize', onWin);
  }, []);

  const handleResizePointerMove = useCallback((e: React.PointerEvent) => {
    const s = resizeSessionRef.current;
    if (!s || e.pointerId !== s.pointerId) return;
    e.preventDefault();
    const dx = e.clientX - s.startX;
    const dy = e.clientY - s.startY;
    const next = sizeFromEdge(s.edge, dx, dy, s.startW, s.startH);
    setPanelSize(next);
  }, []);

  const handleResizePointerUp = useCallback((e: React.PointerEvent) => {
    const s = resizeSessionRef.current;
    if (!s || e.pointerId !== s.pointerId) return;
    resizeSessionRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  }, []);

  const handleResizeLostCapture = useCallback(() => {
    resizeSessionRef.current = null;
  }, []);

  const onResizePointerDown = useCallback(
    (edge: ResizeEdge) => (e: React.PointerEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const el = e.currentTarget;
      el.setPointerCapture(e.pointerId);
      resizeSessionRef.current = {
        edge,
        startX: e.clientX,
        startY: e.clientY,
        startW: panelSize.w,
        startH: panelSize.h,
        pointerId: e.pointerId,
      };
    },
    [panelSize.w, panelSize.h]
  );

  useEffect(() => {
    if (!renderOverlay) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [renderOverlay, onClose]);

  useLayoutEffect(() => {
    if (!renderOverlay || !sheetEntered || collapsed) return;
    bottomRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' });
  }, [messages, loading, renderOverlay, sheetEntered, collapsed]);

  const handleReset = useCallback(async () => {
    if (loading) return;
    setError(null);
    try {
      const next = await chatReset();
      setMessages(introMessage());
      setSessionIds({ userId: next.user_id, runId: next.run_id });
      setInput('');
      setStreamingMessageId(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Reset failed';
      setError(msg);
    }
  }, [loading]);

  const send = useCallback(async () => {
    const trimmed = input.trim();
    if (!trimmed || loading) return;

    setInput('');
    setError(null);

    const stamp = Date.now();
    const userId = `user-${stamp}-${Math.random().toString(16).slice(2)}`;
    const assistantId = `assistant-${stamp}-${Math.random().toString(16).slice(2)}`;
    const activeSession = sessionIds ?? createSessionIds();
    if (!sessionIds) {
      setSessionIds(activeSession);
    }
    setMessages((prev) => [...prev, { id: userId, role: 'user', content: trimmed }]);

    setLoading(true);
    setStreamingMessageId(assistantId);
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', content: '' }]);

    try {
      await chatQueryStream(
        { message: trimmed, user_id: activeSession.userId, run_id: activeSession.runId },
        (chunk) => {
          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.id !== assistantId) return msg;
              return { ...msg, content: msg.content + chunk };
            })
          );
        }
      );

      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== assistantId) return msg;
          const final = normalizeAssistantAnswer(msg.content);
          return { ...msg, content: final || '(No response)' };
        })
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Request failed';
      setError(msg);
      const fallback = "Sorry, I couldn't reach the assistant. Please try again.";
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantId ? { ...msg, content: fallback } : msg
        )
      );
    } finally {
      setLoading(false);
      setStreamingMessageId(null);
    }
  }, [input, loading, landRecordId, sessionIds]);

  if (!renderOverlay) return null;

  const backdropOpacity = sheetEntered ? 0.35 : 0;

  return (
    <div
      className="pointer-events-none fixed inset-0 flex flex-col justify-end"
      style={{ zIndex: 60 }}
    >
      <button
        type="button"
        className="pointer-events-auto absolute inset-0 border-0 bg-black transition-opacity"
        style={{
          opacity: backdropOpacity,
          transitionDuration: `${SHEET_MS}ms`,
          transitionTimingFunction: 'cubic-bezier(0.32, 0.72, 0, 1)',
        }}
        aria-label="Close chat"
        onClick={onClose}
      />

      <div
        className="pointer-events-auto flex w-full justify-end"
        style={{ paddingRight: 24, paddingBottom: 24 }}
      >
        <div
          className="relative flex-shrink-0"
          style={{
            width: `min(${panelSize.w}px, ${MAX_CHAT_W_CAP}px, calc(100vw - 48px))`,
            transform: sheetEntered ? 'translateY(0)' : 'translateY(100%)',
            transitionProperty: 'transform',
            transitionDuration: `${SHEET_MS}ms`,
            transitionTimingFunction: 'cubic-bezier(0.32, 0.72, 0, 1)',
          }}
        >
          <aside
            className="relative flex flex-col overflow-hidden border bg-white"
            style={{
              width: '100%',
              minWidth: 0,
              borderRadius: PANEL_RADIUS,
              borderColor: CHAT_BORDER,
              backgroundColor: CHAT_CARD_BG,
              height: collapsed ? HEADER_H : panelSize.h,
              maxHeight: collapsed
                ? HEADER_H
                : `min(${panelSize.h}px, calc(100dvh - 72px))`,
              boxShadow: '0 4px 20px rgba(0, 0, 0, 0.08)',
            }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="chat-sidebar-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div
              className="relative z-30 flex w-full flex-shrink-0 items-stretch"
              style={{
                height: HEADER_H,
                minHeight: HEADER_H,
                maxHeight: HEADER_H,
                backgroundColor: HEB_RED,
                borderTopLeftRadius: PANEL_RADIUS,
                borderTopRightRadius: PANEL_RADIUS,
                paddingLeft: 10,
                paddingRight: 10,
                isolation: 'isolate',
              }}
            >
              {!collapsed && (
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="Resize panel from top left"
                  onPointerDown={onResizePointerDown('nw')}
                  onPointerMove={handleResizePointerMove}
                  onPointerUp={handleResizePointerUp}
                  onLostPointerCapture={handleResizeLostCapture}
                  className="relative z-40 flex-shrink-0 border-0 bg-transparent p-0"
                  style={{
                    width: HEADER_CORNER_HANDLE_W,
                    minWidth: HEADER_CORNER_HANDLE_W,
                    cursor: 'nwse-resize',
                    touchAction: 'none',
                  }}
                />
              )}
              <div className="relative min-h-0 min-w-0 flex-1">
                {!collapsed && (
                  <button
                    type="button"
                    tabIndex={-1}
                    aria-label="Resize panel height from top"
                    onPointerDown={onResizePointerDown('n')}
                    onPointerMove={handleResizePointerMove}
                    onPointerUp={handleResizePointerUp}
                    onLostPointerCapture={handleResizeLostCapture}
                    className="absolute inset-0 z-10 border-0 bg-transparent p-0"
                    style={{ cursor: 'ns-resize', touchAction: 'none' }}
                  />
                )}
                <h2
                  id="chat-sidebar-title"
                  className="pointer-events-none relative z-20 flex h-full items-center truncate pl-1 pr-1 text-sm font-bold text-white"
                  style={{ letterSpacing: '-0.01em' }}
                >
                  {CHAT_ASSISTANT_NAME}
                </h2>
              </div>
              <button
                type="button"
                onClick={handleReset}
                onPointerDown={(e) => e.stopPropagation()}
                className="relative z-50 mr-1 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-md text-white transition-colors hover:bg-white/15"
                style={{ transitionDuration: '0.2s' }}
                aria-label="Reset chat"
                disabled={loading}
              >
                <RotateCcw className="h-4 w-4" strokeWidth={2} />
              </button>
              {!collapsed && (
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="Resize panel from top right"
                  onPointerDown={onResizePointerDown('ne')}
                  onPointerMove={handleResizePointerMove}
                  onPointerUp={handleResizePointerUp}
                  onLostPointerCapture={handleResizeLostCapture}
                  className="relative z-40 flex-shrink-0 border-0 bg-transparent p-0"
                  style={{
                    width: HEADER_CORNER_HANDLE_W,
                    minWidth: HEADER_CORNER_HANDLE_W,
                    cursor: 'nesw-resize',
                    touchAction: 'none',
                  }}
                />
              )}
              <button
                type="button"
                onClick={() => setCollapsed((c) => !c)}
                onPointerDown={(e) => e.stopPropagation()}
                className="relative z-50 flex h-9 w-9 flex-shrink-0 cursor-pointer items-center justify-center self-center rounded-md text-white transition-colors hover:bg-white/15"
                style={{ transitionDuration: '0.2s' }}
                aria-expanded={!collapsed}
                aria-label={collapsed ? 'Expand chat' : 'Minimize chat'}
              >
                {collapsed ? (
                  <ChevronUp className="h-5 w-5" strokeWidth={2} />
                ) : (
                  <ChevronDown className="h-5 w-5" strokeWidth={2} />
                )}
              </button>
            </div>

          {!collapsed && (
            <>
              <div
                className="relative z-[11] flex min-w-0 flex-1 flex-col overflow-hidden"
                style={{ flex: '1 1 0%', minHeight: 0 }}
              >
              <div
                ref={listRef}
                className="flex flex-1 flex-col overflow-y-auto overflow-x-hidden px-4 py-4"
                style={{
                  flex: '1 1 0%',
                  minHeight: 0,
                  overscrollBehavior: 'contain',
                  background: 'linear-gradient(180deg, #f8f9fb 0%, #f2f4f7 100%)',
                }}
                role="log"
                aria-live="polite"
              >
                <div className="flex w-full min-w-0 flex-col gap-4">
                  {messages.map((m, i) => {
                    const isUser = m.role === 'user';
                    const isStreaming = m.id === streamingMessageId;
                    const showThinking = !isUser && isStreaming && !m.content.trim();
                    return (
                      <div
                        key={m.id}
                        className="flex w-full min-w-0 flex-col gap-1"
                        style={{ alignItems: isUser ? 'flex-end' : 'flex-start' }}
                      >
                        <div
                          className="flex w-full min-w-0 items-center gap-2 text-xs font-semibold"
                          style={{
                            lineHeight: '16px',
                            color: CHAT_TEXT_SECONDARY,
                            textAlign: isUser ? 'right' : 'left',
                            textTransform: 'uppercase',
                            letterSpacing: '0.04em',
                            justifyContent: isUser ? 'flex-end' : 'flex-start',
                          }}
                        >
                          {!isUser && (
                            <span
                              style={{
                                width: 6,
                                height: 6,
                                borderRadius: 999,
                                backgroundColor: HEB_RED,
                                boxShadow: '0 0 0 2px rgba(220, 38, 38, 0.15)',
                              }}
                            />
                          )}
                          <span>{isUser ? 'You' : 'Assistant'}</span>
                          {isUser && (
                            <span
                              style={{
                                width: 6,
                                height: 6,
                                borderRadius: 999,
                                backgroundColor: CHAT_USER_BUBBLE_BG,
                                boxShadow: '0 0 0 2px rgba(0, 119, 204, 0.15)',
                              }}
                            />
                          )}
                        </div>
                        <div
                          className="text-sm leading-relaxed"
                          style={{
                            boxSizing: 'border-box',
                            width: 'fit-content',
                            maxWidth: `${BUBBLE_MAX_WIDTH_PCT}%`,
                            minWidth: 0,
                            overflowWrap: 'break-word',
                            wordBreak: 'break-word',
                            padding: '16px 18px',
                            borderRadius: BUBBLE_RADIUS,
                            border: isUser
                              ? 'none'
                              : `1px solid ${CHAT_BORDER}`,
                            background: isUser 
                              ? `linear-gradient(135deg, ${CHAT_USER_BUBBLE_BG} 0%, #005aa3 100%)` 
                              : 'linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)',
                            boxShadow: isUser
                              ? '0 10px 24px rgba(0, 119, 204, 0.28), 0 4px 10px rgba(0, 119, 204, 0.16)'
                              : '0 10px 24px rgba(15, 23, 42, 0.08), 0 4px 10px rgba(15, 23, 42, 0.05)',
                            color: isUser ? '#FFFFFF' : CHAT_TEXT_PRIMARY,
                            textAlign: 'left',
                            transition: 'all 0.2s ease',
                            transform: 'translateY(0)',
                          }}
                        >
                          {showThinking ? (
                            <span className="flex items-center gap-2">
                              <svg
                                className="h-4 w-4 animate-spin"
                                style={{ color: HEB_RED }}
                                viewBox="0 0 24 24"
                                aria-hidden="true"
                              >
                                <path
                                  className="opacity-75"
                                  fill="currentColor"
                                  d="M12 2a10 10 0 0 1 10 10h-3a7 7 0 0 0-7-7V2z"
                                />
                              </svg>
                              <span className="text-sm font-medium" style={{ color: HEB_RED }}>
                                Thinking…
                              </span>
                            </span>
                          ) : (
                            formatMessageBody(m.content, `m-${i}`, isUser ? 'user' : 'assistant')
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {loading && !streamingMessageId && (
                    <div
                      className="flex w-full min-w-0 flex-col gap-1"
                      style={{ alignItems: 'flex-start' }}
                    >
                      <div
                        className="flex w-full min-w-0 items-center gap-2 text-xs font-semibold"
                        style={{
                          lineHeight: '16px',
                          color: CHAT_TEXT_SECONDARY,
                          textTransform: 'uppercase',
                          letterSpacing: '0.04em',
                        }}
                      >
                        <span
                          style={{
                            width: 6,
                            height: 6,
                            borderRadius: 999,
                            backgroundColor: HEB_RED,
                            boxShadow: '0 0 0 2px rgba(220, 38, 38, 0.15)',
                          }}
                        />
                        <span>Assistant</span>
                      </div>
                      <div
                        className="text-sm leading-relaxed"
                        style={{
                          boxSizing: 'border-box',
                          width: 'fit-content',
                          maxWidth: `${BUBBLE_MAX_WIDTH_PCT}%`,
                          minWidth: 0,
                          overflowWrap: 'break-word',
                          wordBreak: 'break-word',
                          padding: '14px 16px',
                          borderRadius: BUBBLE_RADIUS,
                          border: `1px solid ${CHAT_BORDER}`,
                          background: 'linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)',
                          color: CHAT_TEXT_SECONDARY,
                          boxShadow: '0 6px 18px rgba(15, 23, 42, 0.08), 0 2px 6px rgba(15, 23, 42, 0.05)',
                        }}
                      >
                        Thinking…
                      </div>
                    </div>
                  )}
                  <div ref={bottomRef} className="h-px w-full flex-shrink-0" aria-hidden />
                </div>
              </div>

              {error && (
                <div
                  className="flex-shrink-0 border-t px-4 py-3 text-xs rounded-b-lg"
                  style={{
                    borderColor: 'rgba(238, 40, 36, 0.2)',
                    background: 'linear-gradient(135deg, rgba(238, 40, 36, 0.06) 0%, rgba(238, 40, 36, 0.12) 100%)',
                    color: '#7A1816',
                    backdropFilter: 'blur(8px)',
                  }}
                >
                  <div className="flex items-center gap-2">
                    <svg className="h-3 w-3 text-red-500" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
                    </svg>
                    {error}
                  </div>
                </div>
              )}

              <form
                className="flex w-full min-w-0 flex-shrink-0 gap-3 border-t bg-white px-4 py-4"
                style={{ 
                  borderColor: CHAT_BORDER, 
                  minWidth: 0,
                  background: 'linear-gradient(to bottom, #ffffff 0%, #fafafa 100%)'
                }}
                onSubmit={(e) => {
                  e.preventDefault();
                  void send();
                }}
              >
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="Ask me anything about the legal documents…"
                  className="min-w-0 flex-1 border bg-white px-4 text-sm outline-none transition-all duration-200"
                  style={{
                    width: 0,
                    minWidth: 0,
                    height: 44,
                    borderRadius: 22,
                    borderWidth: 1.5,
                    borderStyle: 'solid',
                    borderColor: CHAT_BORDER,
                    color: CHAT_TEXT_PRIMARY,
                    boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)',
                  }}
                  onFocus={(e) => {
                    e.currentTarget.style.borderColor = HEB_RED;
                    e.currentTarget.style.boxShadow = `0 0 0 3px rgba(220, 38, 38, 0.12), 0 2px 8px rgba(0, 0, 0, 0.1)`;
                    e.currentTarget.style.transform = 'translateY(-1px)';
                  }}
                  onBlur={(e) => {
                    e.currentTarget.style.borderColor = CHAT_BORDER;
                    e.currentTarget.style.boxShadow = '0 1px 3px rgba(0, 0, 0, 0.05)';
                    e.currentTarget.style.transform = 'translateY(0)';
                  }}
                  disabled={loading}
                  aria-label="Message"
                />
                <button
                  type="submit"
                  disabled={loading || !input.trim()}
                  onMouseEnter={() => setSendHovered(true)}
                  onMouseLeave={() => setSendHovered(false)}
                  className="flex flex-shrink-0 cursor-pointer items-center justify-center rounded-full text-white disabled:cursor-not-allowed disabled:opacity-50"
                  style={{
                    width: 44,
                    height: 44,
                    background: loading || !input.trim()
                      ? 'linear-gradient(135deg, #9ca3af 0%, #6b7280 100%)'
                      : sendHovered
                        ? `linear-gradient(135deg, ${HEB_RED_HOVER} 0%, #b91c1c 100%)`
                        : `linear-gradient(135deg, ${HEB_RED} 0%, #b91c1c 100%)`,
                    boxShadow: loading || !input.trim()
                      ? 'none'
                      : sendHovered
                        ? '0 6px 20px rgba(220, 38, 38, 0.4), 0 2px 8px rgba(220, 38, 38, 0.2)'
                        : '0 4px 16px rgba(220, 38, 38, 0.3), 0 2px 8px rgba(220, 38, 38, 0.1)',
                    transition: 'all 0.2s ease',
                    transform: sendHovered && !loading && input.trim() ? 'translateY(-1px) scale(1.05)' : 'translateY(0) scale(1)',
                  }}
                  aria-label="Send"
                >
                  <Send className="h-4 w-4" strokeWidth={2} />
                </button>
              </form>
              </div>
            </>
          )}

          {!collapsed && (
            <>
              {/* No full-bleed wrapper — only these strips capture pointers (avoids blocking the input/scroll area). */}
              <button
                type="button"
                tabIndex={-1}
                aria-label="Resize width"
                onPointerDown={onResizePointerDown('w')}
                onPointerMove={handleResizePointerMove}
                onPointerUp={handleResizePointerUp}
                onLostPointerCapture={handleResizeLostCapture}
                className="absolute border-0 bg-transparent p-0"
                style={{
                  left: 0,
                  top: HEADER_H,
                  bottom: CORNER,
                  width: HANDLE,
                  zIndex: 12,
                  cursor: 'ew-resize',
                  touchAction: 'none',
                }}
              />
              <button
                type="button"
                tabIndex={-1}
                aria-label="Resize width"
                onPointerDown={onResizePointerDown('e')}
                onPointerMove={handleResizePointerMove}
                onPointerUp={handleResizePointerUp}
                onLostPointerCapture={handleResizeLostCapture}
                className="absolute border-0 bg-transparent p-0"
                style={{
                  right: 0,
                  top: HEADER_H,
                  bottom: CORNER,
                  width: HANDLE,
                  zIndex: 12,
                  cursor: 'ew-resize',
                  touchAction: 'none',
                }}
              />
              <button
                type="button"
                tabIndex={-1}
                aria-label="Resize height"
                onPointerDown={onResizePointerDown('s')}
                onPointerMove={handleResizePointerMove}
                onPointerUp={handleResizePointerUp}
                onLostPointerCapture={handleResizeLostCapture}
                className="absolute border-0 bg-transparent p-0"
                style={{
                  bottom: 0,
                  left: CORNER,
                  right: CORNER,
                  height: HANDLE,
                  zIndex: 12,
                  cursor: 'ns-resize',
                  touchAction: 'none',
                }}
              />
              <button
                type="button"
                tabIndex={-1}
                aria-label="Resize panel"
                onPointerDown={onResizePointerDown('sw')}
                onPointerMove={handleResizePointerMove}
                onPointerUp={handleResizePointerUp}
                onLostPointerCapture={handleResizeLostCapture}
                className="absolute border-0 bg-transparent p-0"
                style={{
                  left: 0,
                  bottom: 0,
                  width: CORNER,
                  height: CORNER,
                  zIndex: 12,
                  cursor: 'nesw-resize',
                  touchAction: 'none',
                }}
              />
              <button
                type="button"
                tabIndex={-1}
                aria-label="Resize panel"
                onPointerDown={onResizePointerDown('se')}
                onPointerMove={handleResizePointerMove}
                onPointerUp={handleResizePointerUp}
                onLostPointerCapture={handleResizeLostCapture}
                className="absolute border-0 bg-transparent p-0"
                style={{
                  right: 0,
                  bottom: 0,
                  width: CORNER,
                  height: CORNER,
                  zIndex: 12,
                  cursor: 'nwse-resize',
                  touchAction: 'none',
                }}
              />
            </>
          )}
          </aside>
        </div>
      </div>
    </div>
  );
}
