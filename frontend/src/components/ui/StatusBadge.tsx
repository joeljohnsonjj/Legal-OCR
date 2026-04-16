import type { ReactNode } from 'react';

export type StatusBadgeTone = 'blue' | 'green' | 'yellow' | 'red';

const TONE_STYLES: Record<
  StatusBadgeTone,
  { background: string; border: string; color: string }
> = {
  blue: {
    background: '#E3F2FD',
    border: '#1582CF',
    color: '#0d4d8c',
  },
  green: {
    background: '#C7F3B1',
    border: '#34982B',
    color: '#1a4d16',
  },
  yellow: {
    background: '#FFFBE2',
    border: '#C19800',
    color: '#5c4a00',
  },
  red: {
    background: '#FCD8D9',
    border: '#DD1F26',
    color: '#7a0f14',
  },
};

export interface StatusBadgeProps {
  tone: StatusBadgeTone;
  children: ReactNode;
  className?: string;
}

/** Table status pill matching SVG (green / yellow / red). */
export function StatusBadge({ tone, children, className = '' }: StatusBadgeProps) {
  const s = TONE_STYLES[tone];
  return (
    <span
      className={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium ${className}`}
      style={{
        backgroundColor: s.background,
        borderColor: s.border,
        color: s.color,
        borderWidth: 1,
      }}
    >
      {children}
    </span>
  );
}
