import { useState, type ReactNode } from 'react';
import { NavLink, useNavigate, useLocation } from 'react-router-dom';
import {
  MapPin,
  X,
  FileBarChart,
  Database,
  ChevronDown,
  ChevronRight,
  Code,
  Wrench,
} from 'lucide-react';

type NavItem =
  | { type: 'button'; label: string; icon?: ReactNode }
  | {
      type: 'section';
      id: string;
      label: string;
      icon?: ReactNode;
      children: { to?: string; label: string; landHighlight?: boolean }[];
    };

const NAV: NavItem[] = [
  { type: 'button', label: 'Home' },
  { type: 'button', label: 'Location 101' },
  {
    type: 'section',
    id: 'locationMgmt',
    label: 'Location management',
    icon: <MapPin className="h-4 w-4 flex-shrink-0 text-gray-400" />,
    children: [
      { to: '/agreements', label: 'Land', landHighlight: true },
      { label: 'Campus' },
      { label: 'Building' },
    ],
  },
  {
    type: 'section',
    id: 'reports',
    label: 'Reports',
    icon: <FileBarChart className="h-4 w-4 flex-shrink-0 text-gray-400" />,
    children: [{ label: 'Report 1' }, { label: 'Report 2' }],
  },
  {
    type: 'section',
    id: 'data',
    label: 'Data Management',
    icon: <Database className="h-4 w-4 flex-shrink-0 text-gray-400" />,
    children: [{ label: 'Imports' }, { label: 'Exports' }],
  },
  { type: 'button', label: 'Location services', icon: <Wrench className="h-4 w-4 flex-shrink-0 text-gray-400" /> },
  { type: 'button', label: 'Developer', icon: <Code className="h-4 w-4 flex-shrink-0 text-gray-400" /> },
];

export interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

function landNavActive(pathname: string, search: string): boolean {
  if (pathname === '/agreements') {
    const tab = new URLSearchParams(search).get('tab');
    return tab === null || tab === '' || tab === 'agreements';
  }
  // Agreement detail under same land record
  if (/^\/agreements\/[^/]+$/.test(pathname)) {
    return true;
  }
  return false;
}

export function Sidebar({ open, onClose }: SidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({
    locationMgmt: true,
    reports: false,
    data: false,
  });

  const toggle = (id: string) => {
    setOpenSections((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const rowStyle = { minHeight: 40, paddingTop: 10, paddingBottom: 10 } as const;

  return (
    <aside
      className="flex h-full flex-col overflow-hidden border-r text-white transition-[width] duration-200 ease-out"
      style={{
        width: open ? 248 : 0,
        flexShrink: 0,
        backgroundColor: '#000000',
        borderRightColor: '#000000',
      }}
      aria-hidden={!open}
    >
      <div
        className="flex flex-shrink-0 items-center px-3"
        style={{ height: 56, minHeight: 56, borderBottom: '1px solid #1a1a1a' }}
      >
        <button
          type="button"
          onClick={onClose}
          className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg text-gray-300 hover:bg-gray-900 hover:text-white"
          aria-label="Close navigation"
        >
          <X className="h-5 w-5" strokeWidth={2} />
        </button>
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-3">
        {NAV.map((item) => {
          if (item.type === 'button') {
            return (
              <button
                key={item.label}
                type="button"
                onClick={() => navigate('/agreements')}
                className="flex w-full items-center gap-3 rounded-lg px-3 text-left text-sm font-medium text-gray-300 hover:bg-gray-900 hover:text-white"
                style={rowStyle}
              >
                {item.icon}
                {item.label}
              </button>
            );
          }

          const expanded = openSections[item.id];
          return (
            <div key={item.id} className="flex flex-col">
              <button
                type="button"
                onClick={() => toggle(item.id)}
                className="flex w-full items-center gap-3 rounded-lg px-3 text-left text-sm font-medium text-gray-300 hover:bg-gray-900 hover:text-white"
                style={rowStyle}
              >
                {item.icon}
                <span className="flex-1">{item.label}</span>
                {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
              </button>
              {expanded && (
                <div className="ml-3 mt-1 flex flex-col border-l pl-3" style={{ borderColor: '#2a2a2a' }}>
                  {item.children.map((child) =>
                    child.to ? (
                      <NavLink
                        key={child.label}
                        to={child.to}
                        className={() => {
                          const active =
                            child.landHighlight === true
                              ? landNavActive(location.pathname, location.search)
                              : false;
                          return `rounded-md px-2 py-1.5 text-sm ${
                            active
                              ? 'bg-gray-800 font-bold text-white'
                              : 'text-gray-400 hover:bg-gray-900 hover:text-white'
                          }`;
                        }}
                      >
                        {child.label}
                      </NavLink>
                    ) : (
                      <span
                        key={child.label}
                        className="cursor-default rounded-md px-2 py-1.5 text-sm text-gray-500"
                      >
                        {child.label}
                      </span>
                    )
                  )}
                </div>
              )}
            </div>
          );
        })}
      </nav>
    </aside>
  );
}
