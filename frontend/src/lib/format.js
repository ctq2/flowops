/**
 * Formatting helpers.
 *
 * Pure functions with no DOM and no clock of their own — `now` is always passed
 * in. That is what makes them unit-testable in Node and what keeps the console's
 * numbers reproducible when you replay a snapshot from last week.
 */

/** Format hours as a compact human duration: `2h 15m`, `3d 4h`, `-45m`. */
export function formatHours(hours) {
  if (hours === null || hours === undefined || Number.isNaN(hours)) return '—';
  const negative = hours < 0;
  let minutes = Math.round(Math.abs(hours) * 60);
  const days = Math.floor(minutes / 1440);
  minutes -= days * 1440;
  const hrs = Math.floor(minutes / 60);
  minutes -= hrs * 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hrs) parts.push(`${hrs}h`);
  if (!days && minutes) parts.push(`${minutes}m`);
  if (!parts.length) parts.push('0m');
  return `${negative ? '-' : ''}${parts.join(' ')}`;
}

/** `2026-03-02T10:00:00+08:00` -> `03-02 10:00` in the viewer's locale. */
export function formatDateTime(iso, locale = undefined) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const pad = (value) => String(value).padStart(2, '0');
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  return `${month}-${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatClock(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const pad = (value) => String(value).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** Relative age such as `about 2 hours ago` / `in 3 days` (Intl-backed). */
export function relativeTime(iso, now) {
  if (!iso) return '—';
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return String(iso);
  const base = now instanceof Date ? now.getTime() : new Date(now ?? Date.now()).getTime();
  const deltaSeconds = (target - base) / 1000;
  const absolute = Math.abs(deltaSeconds);
  const formatter = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' });
  const units = [
    ['year', 31536000],
    ['month', 2592000],
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
    ['second', 1],
  ];
  for (const [unit, secondsPerUnit] of units) {
    if (absolute >= secondsPerUnit || unit === 'second') {
      return formatter.format(Math.round(deltaSeconds / secondsPerUnit), unit);
    }
  }
  return '—';
}

export function formatPercent(ratio, digits = 0) {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat('zh-CN').format(value);
}

/** Clamp to the 0..1 domain the SLA gauge expects. */
export function clamp01(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/** Escape text for safe interpolation into an HTML template. */
export function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function truncate(text, max = 90) {
  const value = String(text ?? '');
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

export function initials(text) {
  const value = String(text ?? '').trim();
  if (!value) return '?';
  return value.slice(0, 2).toUpperCase();
}
