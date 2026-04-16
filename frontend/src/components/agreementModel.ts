import type { StatusBadgeTone } from './ui/StatusBadge';

export interface AgreementTableBadge {
  tone: StatusBadgeTone;
  label: string;
}

export interface Agreement {
  id: string;
  agreementNumber: string;
  landRecordId?: string;
  name: string;
  date: string;
  location: string;
  status: string;
  tableBadge?: AgreementTableBadge;
  notes?: string;
  maintenance?: {
    responsibleParty: string;
    ownerResponsibility: string;
    reasoning: string;
  };
  documents?: string[];
  lastModified?: string;
  createdBy?: string;
  lastUpdatedBy?: string;
}

export type SortColumn =
  | 'name'
  | 'creationDate'
  | 'createdBy'
  | 'lastUpdated'
  | 'lastUpdatedBy'
  | 'status';

export type SortDirection = 'asc' | 'desc';

export function parseUsDate(dateStr: string): number {
  const parts = dateStr.split('/');
  if (parts.length !== 3) return 0;
  const month = Number(parts[0]);
  const day = Number(parts[1]);
  let year = Number(parts[2]);
  if (year < 100) year += 2000;
  const d = new Date(year, month - 1, day);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime();
}

export function getSortTimestamp(a: Agreement): number {
  if (a.lastModified) {
    const t = new Date(a.lastModified).getTime();
    if (!Number.isNaN(t)) return t;
  }
  return parseUsDate(a.date);
}

export function formatTableDateTime(a: Agreement): string {
  const d = a.lastModified ? new Date(a.lastModified) : null;
  const base =
    d && !Number.isNaN(d.getTime())
      ? d
      : new Date(parseUsDate(a.date) || Date.now());
  return base.toLocaleString('en-US', {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

/** Creation date column (date agreement was created). */
export function formatCreationDate(a: Agreement): string {
  const t = parseUsDate(a.date);
  if (!t) return a.date;
  return new Date(t).toLocaleDateString('en-US', {
    month: 'numeric',
    day: 'numeric',
    year: '2-digit',
  });
}

export function getTableBadge(agreement: Agreement): AgreementTableBadge {
  if (agreement.tableBadge) return agreement.tableBadge;
  const s = agreement.status;
  if (s === 'Active') {
    return { tone: 'blue', label: 'Active' };
  }
  if (s === 'Needs Review') {
    return { tone: 'yellow', label: 'Pending Update' };
  }
  if (s === 'Pending Addition') {
    return { tone: 'green', label: 'Pending Addition' };
  }
  if (s === 'Deleted' || s === 'Pending Deletion') {
    return { tone: 'red', label: 'Pending Deletion' };
  }
  return { tone: 'blue', label: 'Active' };
}

export function compareStrings(a: string, b: string, dir: SortDirection): number {
  const cmp = a.localeCompare(b, undefined, { sensitivity: 'base' });
  return dir === 'asc' ? cmp : -cmp;
}

export const mockAgreements: Agreement[] = [
  {
    id: '1',
    agreementNumber: 'AGR001234',
    name: 'Facilities Management Agreement',
    date: '01/11/2024',
    location: 'Building A - Corporate Office',
    status: 'Active',
    createdBy: 'J. Smith',
    lastUpdatedBy: 'J. Smith',
    lastModified: '2024-01-11T14:30:00',
    notes: 'Annual facilities maintenance and upkeep agreement for corporate office building.',
    maintenance: {
      responsibleParty: 'Facilities Corp',
      ownerResponsibility: 'Property oversight and compliance',
      reasoning: 'Specialized equipment requires certified maintenance',
    },
  },
  {
    id: '2',
    agreementNumber: 'AGR001235',
    name: 'HVAC Service Contract',
    date: '01/10/2024',
    location: 'Zone 5 - Industrial Complex',
    status: 'Needs Review',
    createdBy: 'A. Lee',
    lastUpdatedBy: 'M. Chen',
    lastModified: '2024-01-11T14:32:00',
    notes: 'Quarterly HVAC maintenance and emergency repair services.',
  },
  {
    id: '3',
    agreementNumber: 'AGR001236',
    name: 'Landscaping Services Agreement',
    date: '01/09/2024',
    location: 'Campus East - Research Facility',
    status: 'Pending Deletion',
    createdBy: 'R. Patel',
    lastUpdatedBy: 'R. Patel',
    lastModified: '2024-01-11T14:34:00',
    notes: 'Weekly landscaping and grounds maintenance for research campus.',
    maintenance: {
      responsibleParty: 'GreenScape LLC',
      ownerResponsibility: 'Environmental compliance',
      reasoning: 'Maintains professional appearance and environmental standards',
    },
  },
  {
    id: '4',
    agreementNumber: 'AGR001237',
    name: 'Security Monitoring Agreement',
    date: '12/05/2023',
    location: 'All Locations',
    status: 'Pending Addition',
    createdBy: 'S. Kim',
    lastUpdatedBy: 'S. Kim',
    lastModified: '2023-12-05T10:00:00',
    notes: '24/7 security monitoring and response services across all properties.',
  },
  {
    id: '5',
    agreementNumber: 'AGR001238',
    name: 'Waste Management Contract',
    date: '04/18/2024',
    location: 'Building C - Distribution Center',
    status: 'Active',
    createdBy: 'D. Ortiz',
    lastUpdatedBy: 'D. Ortiz',
    lastModified: '2024-04-18T09:15:00',
    notes: 'Bi-weekly waste collection and recycling services.',
  },
];
