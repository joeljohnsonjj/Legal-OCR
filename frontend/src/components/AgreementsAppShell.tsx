import { useEffect, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { ChevronRight, ChevronUp } from 'lucide-react';
import { Sidebar } from './Sidebar';
import { ChatSidebar } from './ChatSidebar';
import { MainTopHeader } from './MainTopHeader';
import {
  CHAT_ASSISTANT_NAME,
  CHAT_PANEL_WIDTH,
  DEFAULT_LAND_RECORD_ID,
  BRAND_ORANGE,
  BRAND_ORANGE_HOVER,
} from '../constants/landRecord';

/**
 * Land record shell: collapsible left nav, top utility header; chat only on `/agreements`.
 */
/** Chat launcher + panel only on land details agreements list (`/agreements`). */
function isAgreementsListRoute(pathname: string): boolean {
  return pathname === '/agreements';
}

export function AgreementsAppShell() {
  const location = useLocation();
  const [chatOpen, setChatOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [launcherHover, setLauncherHover] = useState(false);

  const showChat = isAgreementsListRoute(location.pathname);

  useEffect(() => {
    if (!showChat) setChatOpen(false);
  }, [showChat]);

  return (
    <div
      className="relative flex overflow-hidden bg-gray-50"
      style={{ height: '100vh', maxHeight: '100dvh' }}
    >
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {!sidebarOpen && (
        <button
          type="button"
          className="fixed left-0 top-1/2 z-40 flex -translate-y-1/2 items-center rounded-r-md border border-l-0 border-gray-700 bg-gray-900 px-1 py-8 text-white shadow-md hover:bg-gray-800"
          onClick={() => setSidebarOpen(true)}
          aria-label="Open navigation"
        >
          <ChevronRight className="h-5 w-5" strokeWidth={2} />
        </button>
      )}

      <div
        className="fixed z-30 flex cursor-default items-center rounded-l-md border border-r-0 border-gray-700 bg-gray-800 text-xs font-medium text-white shadow-md"
        style={{
          right: 0,
          top: '50%',
          transform: 'translateY(-50%)',
          padding: '12px 6px',
          writingMode: 'vertical-rl',
          textOrientation: 'mixed',
          letterSpacing: '0.12em',
        }}
        title="Land"
      >
        Land
      </div>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <MainTopHeader />
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
          <Outlet />
        </div>
      </div>

      {showChat && (
        <ChatSidebar
          open={chatOpen}
          onClose={() => setChatOpen(false)}
          landRecordId={DEFAULT_LAND_RECORD_ID}
        />
      )}

      {showChat && (
        <div
          className={`fixed z-50 ${chatOpen ? 'pointer-events-none opacity-0' : 'opacity-100'}`}
          style={{ right: 24, bottom: 24 }}
          aria-hidden={chatOpen}
        >
          <button
            type="button"
            onClick={() => setChatOpen(true)}
            onMouseEnter={() => setLauncherHover(true)}
            onMouseLeave={() => setLauncherHover(false)}
            className="flex items-center justify-between gap-2 text-sm font-bold text-white"
            style={{
              width: CHAT_PANEL_WIDTH,
              maxWidth: 'calc(100vw - 48px)',
              minHeight: 48,
              paddingLeft: 16,
              paddingRight: 12,
              borderRadius: 16,
              backgroundColor: launcherHover ? BRAND_ORANGE_HOVER : BRAND_ORANGE,
              boxShadow: '0 4px 20px rgba(0, 0, 0, 0.08)',
              transition: 'background-color 0.2s ease',
            }}
            aria-label={`Open ${CHAT_ASSISTANT_NAME} assistant`}
          >
            <span className="truncate">{CHAT_ASSISTANT_NAME}</span>
            <ChevronUp className="h-5 w-5 flex-shrink-0" strokeWidth={2} />
          </button>
        </div>
      )}
    </div>
  );
}
