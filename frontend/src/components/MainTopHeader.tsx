import { Search, User } from 'lucide-react';
import { BRAND_RED } from '../constants/landRecord';

export function MainTopHeader() {
  return (
    <header
      className="flex flex-shrink-0 items-center justify-between bg-white"
      style={{
        minHeight: 56,
        paddingLeft: 24,
        paddingRight: 24,
        borderBottom: `2px solid ${BRAND_RED}`,
      }}
    >
      <span
        className="font-bold uppercase tracking-wide"
        style={{ color: BRAND_RED, fontSize: 18, letterSpacing: '0.06em' }}
      >
        LOCATION HQ
      </span>
      <div className="flex items-center" style={{ gap: 16 }}>
        <button
          type="button"
          className="flex items-center justify-center text-gray-700 hover:text-gray-900"
          style={{ width: 40, height: 40 }}
          aria-label="Search"
        >
          <Search className="h-5 w-5" strokeWidth={2} />
        </button>
        <button
          type="button"
          className="flex items-center justify-center rounded-full bg-gray-300 text-gray-700 hover:bg-gray-400"
          style={{ width: 40, height: 40 }}
          aria-label="Profile"
        >
          <User className="h-5 w-5" strokeWidth={1.75} />
        </button>
      </div>
    </header>
  );
}
