import { Search, User } from 'lucide-react';
import { BRAND_BLACK, BRAND_HEADER_ACCENT } from '../constants/landRecord';

export function MainTopHeader() {
  return (
    <header
      className="flex flex-shrink-0 items-center justify-end bg-white"
      style={{
        minHeight: 56,
        paddingLeft: 24,
        paddingRight: 24,
        borderBottom: `2px solid ${BRAND_HEADER_ACCENT}`,
      }}
    >
      <div className="flex items-center" style={{ gap: 16 }}>
        <button
          type="button"
          className="flex items-center justify-center hover:opacity-80"
          style={{ width: 40, height: 40, color: BRAND_BLACK }}
          aria-label="Search"
        >
          <Search className="h-5 w-5" strokeWidth={2} />
        </button>
        <button
          type="button"
          className="flex items-center justify-center rounded-full hover:opacity-90"
          style={{
            width: 40,
            height: 40,
            backgroundColor: BRAND_BLACK,
            color: '#FFFFFF',
          }}
          aria-label="Profile"
        >
          <User className="h-5 w-5" strokeWidth={1.75} />
        </button>
      </div>
    </header>
  );
}
