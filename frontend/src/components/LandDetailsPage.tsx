import { useSearchParams } from 'react-router-dom';
import { Info } from 'lucide-react';
import { DEFAULT_LAND_RECORD_ID, LAND_ALIAS, LAND_PRIMARY_USE, BRAND_BLUE } from '../constants/landRecord';
import { AgreementsTableSection } from './AgreementsTableSection';

const TABS = [
  { id: 'information', label: 'Information' },
  { id: 'documents', label: 'Documents' },
  { id: 'campuses', label: 'Campuses' },
  { id: 'hierarchy', label: 'Hierarchy' },
  { id: 'parcels', label: 'Parcels' },
  { id: 'agreements', label: 'Agreements' },
  { id: 'history', label: 'History' },
  { id: 'notes', label: 'Notes' },
] as const;

type TabId = (typeof TABS)[number]['id'];

export function LandDetailsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab') as TabId | null;
  const activeTab: TabId =
    tabParam && TABS.some((t) => t.id === tabParam) ? tabParam : 'agreements';

  const setTab = (id: TabId) => {
    setSearchParams(id === 'agreements' ? {} : { tab: id });
  };

  return (
    <div className="flex min-h-full flex-col" style={{ backgroundColor: '#F5F5F5' }}>
      <div className="border-b bg-white" style={{ borderColor: '#E5E5E5' }}>
        <div style={{ padding: '24px 24px 16px' }}>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <h1
              className="font-bold text-gray-900"
              style={{ fontSize: 28, lineHeight: '34px', letterSpacing: '-0.02em' }}
            >
              Land details
            </h1>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="rounded-full text-sm font-medium text-white"
                style={{
                  backgroundColor: BRAND_BLUE,
                  paddingLeft: 20,
                  paddingRight: 20,
                  height: 40,
                  lineHeight: '40px',
                }}
              >
                Print Preview
              </button>
              <button
                type="button"
                className="rounded-full text-sm font-medium text-white"
                style={{
                  backgroundColor: BRAND_BLUE,
                  paddingLeft: 20,
                  paddingRight: 20,
                  height: 40,
                  lineHeight: '40px',
                }}
              >
                Edit Information
              </button>
            </div>
          </div>

          <div
            className="mt-5 flex flex-wrap items-center gap-x-10 gap-y-3 text-sm text-gray-900"
            style={{ lineHeight: '22px' }}
          >
            <div className="flex items-center gap-2">
              <span className="font-semibold">Record Id:</span>
              <span>{DEFAULT_LAND_RECORD_ID}</span>
              <button
                type="button"
                className="flex h-5 w-5 items-center justify-center rounded-full border border-gray-300 text-gray-500 hover:bg-gray-50"
                aria-label="Record Id information"
              >
                <Info className="h-3 w-3" strokeWidth={2} />
              </button>
            </div>
            <div>
              <span className="font-semibold">Alias: </span>
              {LAND_ALIAS}
            </div>
            <div>
              <span className="font-semibold">Primary use: </span>
              {LAND_PRIMARY_USE}
            </div>
          </div>
        </div>

        <nav
          className="flex gap-0 overflow-x-auto px-6"
          style={{ borderTop: '1px solid #E5E5E5' }}
          aria-label="Land sections"
        >
          {TABS.map((t) => {
            const isActive = activeTab === t.id;
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className="flex-shrink-0 border-b-2 border-transparent bg-transparent px-1 py-3 text-sm font-medium text-gray-600 hover:text-gray-900"
                style={{
                  marginRight: 24,
                  borderBottomColor: isActive ? '#000000' : 'transparent',
                  color: isActive ? '#111827' : undefined,
                  fontWeight: isActive ? 700 : 500,
                }}
              >
                {t.label}
              </button>
            );
          })}
        </nav>
      </div>

      <div className="flex-1" style={{ padding: 24 }}>
        {activeTab === 'agreements' && <AgreementsTableSection />}
        {activeTab !== 'agreements' && (
          <div
            className="rounded-lg border bg-white p-10 text-center text-sm text-gray-500"
            style={{ borderColor: '#E5E5E5' }}
          >
            {TABS.find((t) => t.id === activeTab)?.label} content is not available in this prototype.
          </div>
        )}
      </div>
    </div>
  );
}
