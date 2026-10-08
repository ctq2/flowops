/**
 * Filtering, sorting and aggregation over a list of tickets.
 *
 * Kept free of DOM and network so it can be unit-tested from Node and reused by
 * both the live-API path and the offline snapshot path.
 */

import { consumedRatio, isTerminal, riskOf, urgencyScore } from './risk.js';

export const DEFAULT_FILTERS = Object.freeze({
  status: [],
  priority: [],
  risk: [],
  team: null,
  search: '',
  sort: '-created_at',
});

const SORT_FIELDS = new Set(['created_at', 'updated_at', 'priority', 'sla_due_at', 'urgency']);

export function normaliseFilters(input = {}) {
  const filters = { ...DEFAULT_FILTERS, ...input };
  const asArray = (value) => {
    if (value === null || value === undefined || value === '') return [];
    return Array.isArray(value) ? value.filter(Boolean) : [value];
  };
  return {
    status: asArray(filters.status),
    priority: asArray(filters.priority),
    risk: asArray(filters.risk),
    team: filters.team || null,
    search: String(filters.search ?? '').trim(),
    sort: SORT_FIELDS.has(filters.sort) ? filters.sort : DEFAULT_FILTERS.sort,
  };
}

/** Case-insensitive substring match across the fields an operator searches by. */
export function matchesSearch(ticket, needle) {
  const query = String(needle ?? '').trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    ticket.id,
    ticket.title,
    ticket.body,
    ticket.reporter,
    ticket.assignee,
    ticket.team,
    ...(ticket.tags ?? []),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
  return haystack.includes(query);
}

export function matches(ticket, filters) {
  const active = normaliseFilters(filters);
  if (active.status.length && !active.status.includes(ticket.status)) return false;
  if (active.priority.length && !active.priority.includes(ticket.priority)) return false;
  if (active.risk.length && !active.risk.includes(riskOf(ticket))) return false;
  if (active.team && (ticket.team ?? '') !== active.team) return false;
  return matchesSearch(ticket, active.search);
}

function sortValue(ticket, field) {
  switch (field) {
    case 'priority':
      return { P0: 0, P1: 1, P2: 2, P3: 3 }[ticket.priority] ?? 9;
    case 'urgency':
      return urgencyScore(ticket);
    case 'sla_due_at':
      return ticket.sla_due_at ? new Date(ticket.sla_due_at).getTime() : Number.POSITIVE_INFINITY;
    default:
      return new Date(ticket[field] ?? 0).getTime();
  }
}

export function sortTickets(tickets, sort = DEFAULT_FILTERS.sort) {
  const descending = String(sort).startsWith('-');
  const field = String(sort).replace(/^[-+]/, '');
  const key = SORT_FIELDS.has(field) ? field : 'created_at';
  return [...tickets].sort((a, b) => {
    const left = sortValue(a, key);
    const right = sortValue(b, key);
    if (left === right) return String(a.id).localeCompare(String(b.id));
    return descending ? right - left : left - right;
  });
}

export function selectTickets(tickets, filters, { limit = null, offset = 0 } = {}) {
  const active = normaliseFilters(filters);
  const filtered = sortTickets(
    tickets.filter((ticket) => matches(ticket, active)),
    active.sort,
  );
  const window = limit === null ? filtered.slice(offset) : filtered.slice(offset, offset + limit);
  return {
    items: window,
    total: filtered.length,
    hasMore: offset + window.length < filtered.length,
    filters: active,
  };
}

export function riskHistogram(tickets) {
  const histogram = { breached: 0, critical: 0, warning: 0, healthy: 0, untracked: 0 };
  for (const ticket of tickets) {
    histogram[riskOf(ticket)] += 1;
  }
  return histogram;
}

export function countBy(tickets, keyFn) {
  const counts = new Map();
  for (const ticket of tickets) {
    const key = keyFn(ticket) ?? '—';
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([label, count]) => ({ label, count }))
    .sort((a, b) => b.count - a.count || String(a.label).localeCompare(String(b.label)));
}

export function teamOptions(tickets) {
  return countBy(tickets, (ticket) => ticket.team ?? 'unassigned').map((entry) => entry.label);
}

/** KPI block for the dashboard, derived from the tickets themselves. */
export function summarise(tickets, now = new Date()) {
  const open = tickets.filter((ticket) => !isTerminal(ticket.status));
  const dueSoon = open.filter((ticket) => {
    const ratio = consumedRatio(ticket);
    return ratio !== null && ratio >= 0.6 && ratio < 1;
  });
  const breached = tickets.filter((ticket) => ticket.breached || riskOf(ticket) === 'breached');
  return {
    total: tickets.length,
    open: open.length,
    breached: breached.length,
    dueSoon: dueSoon.length,
    unassigned: open.filter((ticket) => !ticket.assignee).length,
    needsReview: open.filter((ticket) => ticket.required_review).length,
    escalationDue: open.filter((ticket) => ticket.escalation_due).length,
    now,
  };
}

/** Human-facing SLA attainment for closed tickets. */
export function attainment(tickets) {
  const closed = tickets.filter((ticket) => ticket.closed_at && ticket.sla_due_at);
  if (!closed.length) return null;
  const met = closed.filter((ticket) => new Date(ticket.closed_at) <= new Date(ticket.sla_due_at)).length;
  return { closed: closed.length, met, ratio: met / closed.length };
}
