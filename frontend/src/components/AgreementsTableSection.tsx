import { useMemo, useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Search, ChevronDown, ChevronUp, MoreHorizontal } from 'lucide-react';
import { Input } from './ui/input';
import { StatusBadge } from './ui/StatusBadge';
import { getAgreements, getDeletedMockAgreements } from '../utils/agreementStorage';
import type { Agreement, SortColumn, SortDirection } from './agreementModel';
import {
  mockAgreements,
  getTableBadge,
  getSortTimestamp,
  formatTableDateTime,
  formatCreationDate,
  compareStrings,
} from './agreementModel';

export function AgreementsTableSection() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchTerm, setSearchTerm] = useState('');
  const [allAgreements, setAllAgreements] = useState<Agreement[]>([]);
  const [sortColumn, setSortColumn] = useState<SortColumn>('name');
  const [sortDirection, setSortDirection] = useState<SortDirection>('asc');
  const [showSortDropdown, setShowSortDropdown] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);

  useEffect(() => {
    const storedAgreements = getAgreements();
    const deletedMockIds = new Set(getDeletedMockAgreements());
    const activeMockAgreements = mockAgreements.filter((a) => !deletedMockIds.has(a.id));
    const mockIds = new Set(activeMockAgreements.map((a) => a.id));
    const uniqueStored = storedAgreements.filter((a) => !mockIds.has(a.id));
    const merged = [...activeMockAgreements, ...uniqueStored];
    setAllAgreements(merged);
  }, [location.pathname]);

  const filteredAndSorted = useMemo(() => {
    const searchLower = searchTerm.toLowerCase();
    let list = allAgreements;

    if (searchTerm.trim()) {
      list = list.filter((agreement) => {
        const matchesBasic =
          agreement.name.toLowerCase().includes(searchLower) ||
          agreement.agreementNumber.toLowerCase().includes(searchLower) ||
          agreement.status.toLowerCase().includes(searchLower) ||
          agreement.location.toLowerCase().includes(searchLower) ||
          (agreement.createdBy?.toLowerCase().includes(searchLower) ?? false) ||
          (agreement.lastUpdatedBy?.toLowerCase().includes(searchLower) ?? false);

        const creationDate = agreement.date;
        const lastModifiedDate = agreement.lastModified
          ? new Date(agreement.lastModified).toLocaleDateString('en-US', {
              month: '2-digit',
              day: '2-digit',
              year: 'numeric',
            })
          : agreement.date;

        const matchesDate =
          creationDate.includes(searchTerm) || lastModifiedDate.includes(searchTerm);

        const badge = getTableBadge(agreement);
        const matchesBadge = badge.label.toLowerCase().includes(searchLower);

        return matchesBasic || matchesDate || matchesBadge;
      });
    }

    const sorted = [...list];
    const dir = sortDirection;

    sorted.sort((a, b) => {
      switch (sortColumn) {
        case 'name':
          return compareStrings(a.name, b.name, dir);
        case 'creationDate':
          return dir === 'asc'
            ? parseUsDateSafe(a.date) - parseUsDateSafe(b.date)
            : parseUsDateSafe(b.date) - parseUsDateSafe(a.date);
        case 'createdBy':
          return compareStrings(a.createdBy ?? '', b.createdBy ?? '', dir);
        case 'lastUpdatedBy':
          return compareStrings(a.lastUpdatedBy ?? '', b.lastUpdatedBy ?? '', dir);
        case 'status': {
          const la = getTableBadge(a).label;
          const lb = getTableBadge(b).label;
          return compareStrings(la, lb, dir);
        }
        case 'lastUpdated':
        default: {
          const ta = getSortTimestamp(a);
          const tb = getSortTimestamp(b);
          return dir === 'asc' ? ta - tb : tb - ta;
        }
      }
    });

    return sorted;
  }, [allAgreements, searchTerm, sortColumn, sortDirection]);

  const pageCount = Math.max(1, Math.ceil(filteredAndSorted.length / pageSize));

  useEffect(() => {
    setPage((p) => Math.min(p, pageCount));
  }, [pageCount]);

  useEffect(() => {
    setPage(1);
  }, [searchTerm, sortColumn, sortDirection, pageSize]);

  const pageSlice = useMemo(() => {
    const start = (page - 1) * pageSize;
    return filteredAndSorted.slice(start, start + pageSize);
  }, [filteredAndSorted, page, pageSize]);

  const sortLabel = () => {
    switch (sortColumn) {
      case 'name':
        return 'agreement name';
      case 'creationDate':
        return 'creation date';
      case 'createdBy':
        return 'created by';
      case 'lastUpdated':
        return 'last updated';
      case 'lastUpdatedBy':
        return 'last updated by';
      case 'status':
        return 'status';
      default:
        return 'agreement name';
    }
  };

  const toggleSort = (column: SortColumn) => {
    if (sortColumn === column) {
      setSortDirection((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortColumn(column);
      setSortDirection(column === 'lastUpdated' || column === 'creationDate' ? 'desc' : 'asc');
    }
  };

  const sortIcon = (column: SortColumn) => {
    if (sortColumn !== column) {
      return <ChevronDown className="ml-1 inline h-3 w-3 opacity-40" aria-hidden />;
    }
    return sortDirection === 'asc' ? (
      <ChevronUp className="ml-1 inline h-3 w-3" aria-hidden />
    ) : (
      <ChevronDown className="ml-1 inline h-3 w-3" aria-hidden />
    );
  };

  const handleViewAgreement = (agreementId: string) => {
    navigate(`/agreements/${agreementId}`);
  };

  const handleNewAgreement = () => {
    navigate('/agreements/new');
  };

  const fromRow = (page - 1) * pageSize + 1;
  const toRow = Math.min(page * pageSize, filteredAndSorted.length);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
        <h2 className="font-bold text-gray-900" style={{ fontSize: 18, lineHeight: '24px' }}>
          Agreements
        </h2>
        <button
          type="button"
          onClick={handleNewAgreement}
          className="rounded-full border-2 text-sm font-semibold transition-colors hover:bg-blue-50"
          style={{
            borderColor: '#1582CF',
            color: '#1582CF',
            height: 40,
            paddingLeft: 22,
            paddingRight: 22,
          }}
        >
          + Add Agreement
        </button>
      </div>

      <div className="relative mb-4 w-full">
        <Search
          className="pointer-events-none absolute text-gray-400"
          style={{ left: 12, top: '50%', width: 18, height: 18, transform: 'translateY(-50%)' }}
          strokeWidth={2}
        />
        <Input
          type="text"
          placeholder="Search"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          className="w-full border pl-10 text-sm text-gray-900 placeholder:text-gray-400"
          style={{
            height: 40,
            borderColor: '#D1D5DB',
            borderRadius: 8,
            paddingRight: 12,
          }}
          aria-label="Search agreements"
        />
      </div>

      <div className="mb-4 flex flex-wrap items-center justify-end gap-3">
        <div className="relative flex-shrink-0">
          <button
            type="button"
            onClick={() => setShowSortDropdown(!showSortDropdown)}
            className="flex min-w-[196px] items-center justify-between gap-2 rounded-lg border bg-white text-sm font-medium text-gray-700 hover:bg-gray-50"
            style={{
              height: 40,
              borderColor: '#D1D5DB',
              paddingLeft: 16,
              paddingRight: 14,
            }}
          >
            <span>
              Sort by: {sortLabel()}
              {sortDirection === 'desc' ? ' (desc)' : ''}
            </span>
            <ChevronDown className="h-4 w-4 flex-shrink-0" />
          </button>
          {showSortDropdown && (
            <>
              <button
                type="button"
                className="fixed inset-0 z-10 cursor-default bg-transparent"
                aria-label="Close sort menu"
                onClick={() => setShowSortDropdown(false)}
              />
              <div className="absolute right-0 z-20 mt-2 w-56 rounded-lg border border-gray-200 bg-white shadow-lg">
                <div className="py-1">
                  {(
                    [
                      ['name', 'Agreement name'],
                      ['creationDate', 'Creation date'],
                      ['createdBy', 'Created by'],
                      ['lastUpdated', 'Last updated date'],
                      ['lastUpdatedBy', 'Last updated by'],
                      ['status', 'Status'],
                    ] as const
                  ).map(([col, label]) => (
                    <button
                      key={col}
                      type="button"
                      onClick={() => {
                        setSortColumn(col);
                        setSortDirection(
                          col === 'lastUpdated' || col === 'creationDate' ? 'desc' : 'asc'
                        );
                        setShowSortDropdown(false);
                      }}
                      className={`flex w-full items-center justify-between px-4 py-2 text-left text-sm hover:bg-gray-50 ${
                        sortColumn === col ? 'bg-blue-50 font-medium text-blue-700' : 'text-gray-700'
                      }`}
                    >
                      {label}
                      {sortColumn === col && <span className="text-blue-600">✓</span>}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      <div
        className="overflow-hidden border"
        style={{
          backgroundColor: '#FFFFFF',
          borderColor: '#E5E5E5',
          borderRadius: 8,
          boxShadow: '0 1px 2px rgba(0, 0, 0, 0.04)',
        }}
      >
        <div className="overflow-x-auto">
          <table
            className="w-full min-w-[960px]"
            style={{ tableLayout: 'fixed', borderCollapse: 'collapse' }}
          >
            <colgroup>
              <col style={{ width: '22%' }} />
              <col style={{ width: '10%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '16%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '14%' }} />
              <col style={{ width: '14%' }} />
            </colgroup>
            <thead style={{ backgroundColor: '#FAFAFA', borderBottom: '1px solid #E5E5E5' }}>
              <tr>
                <th style={{ padding: '14px 16px', textAlign: 'left', verticalAlign: 'middle' }}>
                  <button
                    type="button"
                    onClick={() => toggleSort('name')}
                    className="inline-flex max-w-full min-w-0 items-center gap-1 text-left text-xs font-semibold text-gray-700 hover:text-gray-900"
                    style={{ letterSpacing: '0.02em' }}
                  >
                    <span className="min-w-0 truncate">Agreement name</span>
                    {sortIcon('name')}
                  </button>
                </th>
                <th style={{ padding: '14px 16px', textAlign: 'left', verticalAlign: 'middle' }}>
                  <button
                    type="button"
                    onClick={() => toggleSort('creationDate')}
                    className="inline-flex w-full items-center gap-1 text-left text-xs font-semibold text-gray-700 hover:text-gray-900"
                    style={{ letterSpacing: '0.02em' }}
                  >
                    <span>Creation date</span>
                    {sortIcon('creationDate')}
                  </button>
                </th>
                <th style={{ padding: '14px 16px', textAlign: 'left', verticalAlign: 'middle' }}>
                  <button
                    type="button"
                    onClick={() => toggleSort('createdBy')}
                    className="inline-flex w-full items-center gap-1 text-left text-xs font-semibold text-gray-700 hover:text-gray-900"
                    style={{ letterSpacing: '0.02em' }}
                  >
                    <span>Created by</span>
                    {sortIcon('createdBy')}
                  </button>
                </th>
                <th style={{ padding: '14px 16px', textAlign: 'left', verticalAlign: 'middle' }}>
                  <button
                    type="button"
                    onClick={() => toggleSort('lastUpdated')}
                    className="inline-flex w-full min-w-0 items-center gap-1 text-left text-xs font-semibold text-gray-700 hover:text-gray-900"
                    style={{ letterSpacing: '0.02em' }}
                  >
                    <span className="min-w-0 leading-tight">Last updated date</span>
                    {sortIcon('lastUpdated')}
                  </button>
                </th>
                <th style={{ padding: '14px 16px', textAlign: 'left', verticalAlign: 'middle' }}>
                  <button
                    type="button"
                    onClick={() => toggleSort('lastUpdatedBy')}
                    className="inline-flex w-full min-w-0 items-center gap-1 text-left text-xs font-semibold text-gray-700 hover:text-gray-900"
                    style={{ letterSpacing: '0.02em' }}
                  >
                    <span className="min-w-0 leading-tight">Last updated by</span>
                    {sortIcon('lastUpdatedBy')}
                  </button>
                </th>
                <th style={{ padding: '14px 16px', textAlign: 'left', verticalAlign: 'middle' }}>
                  <button
                    type="button"
                    onClick={() => toggleSort('status')}
                    className="inline-flex w-full items-center gap-1 text-left text-xs font-semibold text-gray-700 hover:text-gray-900"
                    style={{ letterSpacing: '0.02em' }}
                  >
                    <span>Status</span>
                    {sortIcon('status')}
                  </button>
                </th>
                <th
                  className="text-xs font-semibold text-gray-700"
                  style={{ padding: '14px 16px', textAlign: 'right', verticalAlign: 'middle' }}
                >
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {pageSlice.map((agreement) => {
                const badge = getTableBadge(agreement);
                return (
                  <tr
                    key={agreement.id}
                    className="hover:bg-gray-50"
                    style={{ borderBottom: '1px solid #E5E5E5' }}
                  >
                    <td
                      style={{
                        padding: '14px 16px',
                        verticalAlign: 'middle',
                        textAlign: 'left',
                        overflow: 'hidden',
                      }}
                    >
                      <button
                        type="button"
                        onClick={() => handleViewAgreement(agreement.id)}
                        className="block max-w-full truncate text-left text-sm font-medium hover:underline"
                        style={{ color: '#1582CF', lineHeight: '20px' }}
                        title={agreement.name}
                      >
                        {agreement.name}
                      </button>
                    </td>
                    <td
                      className="text-sm text-gray-900"
                      style={{
                        padding: '14px 16px',
                        lineHeight: '20px',
                        verticalAlign: 'middle',
                        textAlign: 'left',
                        fontVariantNumeric: 'tabular-nums',
                      }}
                    >
                      {formatCreationDate(agreement)}
                    </td>
                    <td
                      className="text-sm text-gray-900"
                      style={{
                        padding: '14px 16px',
                        lineHeight: '20px',
                        verticalAlign: 'middle',
                        textAlign: 'left',
                        overflow: 'hidden',
                      }}
                    >
                      <span className="block truncate" title={agreement.createdBy ?? undefined}>
                        {agreement.createdBy ?? '—'}
                      </span>
                    </td>
                    <td
                      className="text-sm text-gray-900"
                      style={{
                        padding: '14px 16px',
                        lineHeight: '20px',
                        verticalAlign: 'middle',
                        textAlign: 'left',
                        fontVariantNumeric: 'tabular-nums',
                      }}
                    >
                      {formatTableDateTime(agreement)}
                    </td>
                    <td
                      className="text-sm text-gray-900"
                      style={{
                        padding: '14px 16px',
                        lineHeight: '20px',
                        verticalAlign: 'middle',
                        textAlign: 'left',
                        overflow: 'hidden',
                      }}
                    >
                      <span className="block truncate" title={agreement.lastUpdatedBy ?? undefined}>
                        {agreement.lastUpdatedBy ?? '—'}
                      </span>
                    </td>
                    <td
                      style={{
                        padding: '14px 16px',
                        verticalAlign: 'middle',
                        textAlign: 'left',
                      }}
                    >
                      <StatusBadge tone={badge.tone}>{badge.label}</StatusBadge>
                    </td>
                    <td
                      className="relative"
                      style={{
                        padding: '14px 16px',
                        verticalAlign: 'middle',
                        textAlign: 'right',
                      }}
                    >
                      <div className="flex justify-end">
                        <button
                          type="button"
                          onClick={() =>
                            setOpenMenuId((id) => (id === agreement.id ? null : agreement.id))
                          }
                          className="inline-flex rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-800"
                          aria-label="Row actions"
                        >
                          <MoreHorizontal className="h-5 w-5" />
                        </button>
                      </div>
                      {openMenuId === agreement.id && (
                        <>
                          <button
                            type="button"
                            className="fixed inset-0 z-10 cursor-default"
                            aria-hidden
                            onClick={() => setOpenMenuId(null)}
                          />
                          <div className="absolute right-4 z-20 mt-1 w-44 rounded-lg border border-gray-200 bg-white py-1 shadow-lg">
                            <button
                              type="button"
                              className="w-full px-3 py-2 text-left text-sm hover:bg-gray-50"
                              onClick={() => {
                                setOpenMenuId(null);
                                handleViewAgreement(agreement.id);
                              }}
                            >
                              View agreement
                            </button>
                          </div>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {filteredAndSorted.length === 0 && (
        <p className="mt-8 text-center text-sm text-gray-500">No agreements match your search.</p>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <select
            className="rounded border border-gray-300 px-2 py-1 text-sm"
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
            aria-label="Rows per page"
          >
            <option value={10}>10</option>
            <option value={25}>25</option>
            <option value={50}>50</option>
          </select>
          <span className="text-sm text-gray-600">
            {filteredAndSorted.length === 0
              ? '0 rows'
              : `${fromRow} – ${toRow} of ${filteredAndSorted.length} rows`}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="rounded p-2 text-gray-600 hover:bg-gray-200 disabled:opacity-40"
            aria-label="Previous page"
          >
            <ChevronDown className="h-4 w-4 rotate-90" />
          </button>
          <span className="text-sm text-gray-600">
            Page {page} / {pageCount}
          </span>
          <button
            type="button"
            disabled={page >= pageCount}
            onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
            className="rounded p-2 text-gray-600 hover:bg-gray-200 disabled:opacity-40"
            aria-label="Next page"
          >
            <ChevronDown className="h-4 w-4 -rotate-90" />
          </button>
        </div>
      </div>
    </div>
  );
}

function parseUsDateSafe(dateStr: string): number {
  const parts = dateStr.split('/');
  if (parts.length !== 3) return 0;
  const month = Number(parts[0]);
  const day = Number(parts[1]);
  let year = Number(parts[2]);
  if (year < 100) year += 2000;
  const d = new Date(year, month - 1, day);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime();
}
