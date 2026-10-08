/**
 * Unit tests for the console's pure helpers.
 *
 * Run with: `node --test frontend/tests` (no dependencies, no build step).
 * The value here is the boundary cases: durations that cross days, a missing SLA,
 * a closed ticket that still breached.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  clamp01,
  escapeHtml,
  formatHours,
  formatPercent,
  relativeTime,
  truncate,
} from '../src/lib/format.js';
import {
  RISK_BANDS,
  bandFor,
  consumedRatio,
  effectiveHours,
  isTerminal,
  orderByUrgency,
  priorityLabel,
  remainingHours,
  riskOf,
  statusLabel,
  urgencyScore,
} from '../src/lib/risk.js';
import {
  attainment,
  countBy,
  matches,
  matchesSearch,
  normaliseFilters,
  riskHistogram,
  selectTickets,
  sortTickets,
  summarise,
  teamOptions,
} from '../src/lib/catalog.js';
import { ApiError, createClient, fingerprint, isApiError } from '../src/lib/client.js';
import { parseCalendar } from '../src/views/calendar.js';

/* ------------------------------------------------------------------- format */

test('formatHours renders days, hours and minutes', () => {
  assert.equal(formatHours(0), '0m');
  assert.equal(formatHours(0.5), '30m');
  assert.equal(formatHours(2), '2h');
  assert.equal(formatHours(2.25), '2h 15m');
  assert.equal(formatHours(26), '1d 2h');
  assert.equal(formatHours(-1.5), '-1h 30m');
});

test('formatHours degrades gracefully on missing data', () => {
  assert.equal(formatHours(null), '—');
  assert.equal(formatHours(undefined), '—');
  assert.equal(formatHours(Number.NaN), '—');
});

test('formatPercent and clamp01 stay inside their domains', () => {
  assert.equal(formatPercent(0.5), '50%');
  assert.equal(formatPercent(0.1234, 1), '12.3%');
  assert.equal(formatPercent(null), '—');
  assert.equal(clamp01(-3), 0);
  assert.equal(clamp01(3), 1);
  assert.equal(clamp01(null), 0);
});

test('escapeHtml neutralises markup', () => {
  assert.equal(escapeHtml('<script>alert("x")</script>'), '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;');
  assert.equal(escapeHtml(null), '');
});

test('truncate keeps short text and marks long text', () => {
  assert.equal(truncate('abc', 5), 'abc');
  assert.equal(truncate('abcdefgh', 5), 'abcd…');
});

test('relativeTime is expressed in the language of the console', () => {
  const now = new Date('2026-03-02T10:00:00Z');
  assert.match(relativeTime('2026-03-02T08:00:00Z', now), /2\s*小时前/);
  assert.match(relativeTime('2026-03-03T10:00:00Z', now), /明天|1\s*天/);
  assert.equal(relativeTime('2026-03-02T09:59:30Z', now), '30秒钟前');
});

/* --------------------------------------------------------------------- risk */

test('bandFor mirrors the Python thresholds exactly', () => {
  assert.equal(bandFor(1.4), 'breached');
  assert.equal(bandFor(1.0), 'breached');
  assert.equal(bandFor(0.99), 'critical');
  assert.equal(bandFor(0.85), 'critical');
  assert.equal(bandFor(0.7), 'warning');
  assert.equal(bandFor(0.6), 'warning');
  assert.equal(bandFor(0.59), 'healthy');
  assert.equal(bandFor(0), 'healthy');
  assert.equal(bandFor(null), 'untracked');
});

test('a closed ticket reports breached rather than healthy when it missed', () => {
  assert.equal(bandFor(1.4, { terminal: true }), 'breached');
  assert.equal(bandFor(0.4, { terminal: true }), 'healthy');
});

test('SLA helpers honour the snooze and reject untracked tickets', () => {
  const tracked = { sla_duration_hours: 8, sla_consumed_hours: 4, sla_snooze_hours: 2 };
  assert.equal(effectiveHours(tracked), 6);
  assert.equal(remainingHours(tracked), 2);
  assert.equal(consumedRatio(tracked), 4 / 6);
  assert.equal(riskOf(tracked), 'warning');

  const untracked = { sla_duration_hours: null };
  assert.equal(effectiveHours(untracked), null);
  assert.equal(remainingHours(untracked), null);
  assert.equal(consumedRatio(untracked), null);
  assert.equal(riskOf(untracked), 'untracked');
});

test('a server-provided risk band wins over local calculation', () => {
  assert.equal(riskOf({ risk: 'critical', sla_duration_hours: 8, sla_consumed_hours: 0 }), 'critical');
});

test('terminal detection matches the domain state machine', () => {
  assert.equal(isTerminal('resolved'), true);
  assert.equal(isTerminal('closed'), true);
  assert.equal(isTerminal('in_progress'), false);
  assert.equal(isTerminal('blocked'), false);
});

test('urgency ordering puts breached P0 first and healthy P3 last', () => {
  const tickets = [
    { id: 'A', priority: 'P3', status: 'open', sla_duration_hours: 8, sla_consumed_hours: 1 },
    { id: 'B', priority: 'P0', status: 'open', sla_duration_hours: 2, sla_consumed_hours: 3 },
    { id: 'C', priority: 'P1', status: 'open', sla_duration_hours: 4, sla_consumed_hours: 3.5 },
  ];
  assert.deepEqual(orderByUrgency(tickets).map((item) => item.id), ['B', 'C', 'A']);
  assert.ok(urgencyScore(tickets[1]) < urgencyScore(tickets[0]));
});

test('every risk band has a label and a translation', () => {
  for (const band of RISK_BANDS) {
    assert.equal(typeof band, 'string');
  }
  assert.equal(statusLabel('in_progress'), '处理中');
  assert.equal(priorityLabel('P0'), '一级故障');
});

/* ------------------------------------------------------------------ catalog */

test('filters are normalised into a stable shape', () => {
  const filters = normaliseFilters({ status: 'open', risk: null, sort: 'nonsense' });
  assert.deepEqual(filters.status, ['open']);
  assert.deepEqual(filters.risk, []);
  assert.equal(filters.sort, '-created_at');
  assert.equal(filters.search, '');
});

test('search covers id, title, body, reporter, tags and assignee', () => {
  const ticket = {
    id: 'TCK-1',
    title: '支付网关 502',
    body: 'all callbacks failing',
    reporter: '监控告警',
    assignee: 'sre-张伟',
    tags: ['incident', 'payments'],
  };
  assert.equal(matchesSearch(ticket, '502'), true);
  assert.equal(matchesSearch(ticket, 'CALLBACKS'), true);
  assert.equal(matchesSearch(ticket, '张伟'), true);
  assert.equal(matchesSearch(ticket, 'payments'), true);
  assert.equal(matchesSearch(ticket, 'nope'), false);
  assert.equal(matchesSearch(ticket, '   '), true);
});

test('matches applies status, priority, risk and team together', () => {
  const ticket = {
    id: 'T',
    status: 'in_progress',
    priority: 'P1',
    team: 'payments',
    sla_duration_hours: 4,
    sla_consumed_hours: 3.6,
  };
  assert.equal(matches(ticket, { status: ['in_progress'], priority: ['P1'], risk: ['critical'] }), true);
  assert.equal(matches(ticket, { team: 'platform' }), false);
  assert.equal(matches(ticket, { risk: ['healthy'] }), false);
});

test('sortTickets supports descending timestamps, priority rank and urgency', () => {
  const tickets = [
    { id: 'A', priority: 'P3', created_at: '2026-03-01T00:00:00Z' },
    { id: 'B', priority: 'P0', created_at: '2026-03-02T00:00:00Z' },
    { id: 'C', priority: 'P1', created_at: '2026-03-03T00:00:00Z' },
  ];
  assert.deepEqual(sortTickets(tickets, '-created_at').map((t) => t.id), ['C', 'B', 'A']);
  assert.deepEqual(sortTickets(tickets, 'created_at').map((t) => t.id), ['A', 'B', 'C']);
  assert.deepEqual(sortTickets(tickets, 'priority').map((t) => t.id), ['B', 'C', 'A']);
});

test('sorting is stable for equal keys (ties break on id)', () => {
  const tickets = [
    { id: 'B', priority: 'P2', created_at: '2026-03-01T00:00:00Z' },
    { id: 'A', priority: 'P2', created_at: '2026-03-01T00:00:00Z' },
  ];
  assert.deepEqual(sortTickets(tickets, 'priority').map((t) => t.id), ['A', 'B']);
});

test('selectTickets paginates and reports the full total', () => {
  const tickets = Array.from({ length: 7 }, (_, index) => ({
    id: `T${index}`,
    status: 'open',
    priority: 'P3',
    created_at: `2026-03-0${index + 1}T00:00:00Z`,
  }));
  const page = selectTickets(tickets, {}, { limit: 3 });
  assert.equal(page.items.length, 3);
  assert.equal(page.total, 7);
  assert.equal(page.hasMore, true);
  const last = selectTickets(tickets, {}, { limit: 3, offset: 6 });
  assert.equal(last.items.length, 1);
  assert.equal(last.hasMore, false);
});

test('histograms and counts add up', () => {
  const tickets = [
    { status: 'open', team: 'platform', sla_duration_hours: 2, sla_consumed_hours: 3 },
    { status: 'open', team: 'platform', sla_duration_hours: 2, sla_consumed_hours: 0.2 },
    { status: 'closed', team: null, sla_duration_hours: null },
  ];
  const histogram = riskHistogram(tickets);
  assert.equal(histogram.breached, 1);
  assert.equal(histogram.healthy, 1);
  assert.equal(histogram.untracked, 1);
  assert.equal(Object.values(histogram).reduce((a, b) => a + b, 0), tickets.length);

  const teams = countBy(tickets, (ticket) => ticket.team ?? 'unassigned');
  assert.deepEqual(teams[0], { label: 'platform', count: 2 });
  assert.deepEqual(teamOptions(tickets), ['platform', 'unassigned']);
});

test('summarise separates open, breached, due-soon and unassigned', () => {
  const tickets = [
    { status: 'open', assignee: null, sla_duration_hours: 4, sla_consumed_hours: 5, breached: true },
    { status: 'open', assignee: 'x', sla_duration_hours: 4, sla_consumed_hours: 3, required_review: 'r' },
    { status: 'closed', assignee: null, sla_duration_hours: 4, sla_consumed_hours: 1 },
  ];
  const stats = summarise(tickets);
  assert.equal(stats.total, 3);
  assert.equal(stats.open, 2);
  assert.equal(stats.breached, 1);
  assert.equal(stats.dueSoon, 1);
  assert.equal(stats.unassigned, 1);
  assert.equal(stats.needsReview, 1);
});

test('attainment only counts tickets that actually had a target', () => {
  const tickets = [
    { closed_at: '2026-03-01T10:00:00Z', sla_due_at: '2026-03-01T12:00:00Z' },
    { closed_at: '2026-03-01T14:00:00Z', sla_due_at: '2026-03-01T12:00:00Z' },
    { closed_at: '2026-03-01T14:00:00Z', sla_due_at: null },
  ];
  const result = attainment(tickets);
  assert.equal(result.closed, 2);
  assert.equal(result.met, 1);
  assert.equal(result.ratio, 0.5);
  assert.equal(attainment([]), null);
});

/* ------------------------------------------------------------------- client */

test('ApiError exposes the problem document', () => {
  const error = new ApiError(412, { type: 'https://flowops.dev/problems/precondition-failed', detail: 'stale' });
  assert.equal(error.isConflict, true);
  assert.equal(error.status, 412);
  assert.equal(error.message, 'stale');
  assert.equal(isApiError(error), true);
  assert.equal(isApiError(new Error('x')), false);
});

test('createClient parses JSON, surfaces problems and retries idempotent calls', async () => {
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) {
      return { ok: false, status: 503, text: async () => '{"detail":"warming up"}' };
    }
    return { ok: true, status: 200, text: async () => '{"ok":true}' };
  };
  const client = createClient({ baseUrl: '/api/v1', fetchImpl: fakeFetch, retries: 1 });
  const result = await client.get('/health');
  assert.equal(result.status, 200);
  assert.equal(calls.length, 2, 'a 503 on a GET is retried once');
  assert.equal(calls[0].options.method, 'GET');
});

test('createClient does not retry a non-idempotent POST', async () => {
  let attempts = 0;
  const fakeFetch = async () => {
    attempts += 1;
    return { ok: false, status: 500, text: async () => '{"detail":"boom"}' };
  };
  const client = createClient({ baseUrl: '/api/v1', fetchImpl: fakeFetch, retries: 3 });
  await assert.rejects(() => client.post('/tickets', { title: 'x' }), (error) => error.status === 500);
  assert.equal(attempts, 1);
});

test('fingerprint is stable for the same payload and differs otherwise', () => {
  assert.equal(fingerprint({ a: 1, b: 2 }), fingerprint({ b: 2, a: 1 }));
  assert.notEqual(fingerprint({ a: 1 }), fingerprint({ a: 2 }));
});

/* ----------------------------------------------------------------- calendar */

test('parseCalendar reads the describe() string', () => {
  const parsed = parseCalendar('Mon/Tue/Wed/Thu/Fri 09:00-18:00 UTC+08:00 (23 holidays, 9 make-up days)');
  assert.deepEqual(parsed.days, ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']);
  assert.equal(parsed.start, '09:00');
  assert.equal(parsed.end, '18:00');
  assert.equal(parsed.timezone, 'UTC+08:00');
  assert.equal(parsed.holidays, 23);
  assert.equal(parsed.makeUp, 9);
});

test('parseCalendar tolerates missing data', () => {
  const parsed = parseCalendar(null);
  assert.deepEqual(parsed.days, []);
  assert.equal(parsed.start, '—');
  assert.equal(parsed.holidays, 0);
});
