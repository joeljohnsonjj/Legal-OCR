import type { ReactNode } from 'react';
import {
  CHAT_ACCENT_ON_LIGHT,
  CHAT_LINK_ON_USER_BUBBLE,
  CHAT_TEXT_PRIMARY,
  CHAT_TEXT_SECONDARY,
} from '../../constants/landRecord';

/** Linkify URLs and highlight “Clause … – …” spans. */
export function formatInlineSegment(
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

export function linkifyUrls(
  text: string,
  keyPrefix: string,
  linkColor: string = CHAT_ACCENT_ON_LIGHT
): ReactNode[] {
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

export function formatMessageBody(
  content: string,
  keyBase: string,
  variant: 'assistant' | 'user'
): ReactNode {
  const lines = content.split('\n');
  const accent = variant === 'user' ? CHAT_LINK_ON_USER_BUBBLE : CHAT_ACCENT_ON_LIGHT;
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
