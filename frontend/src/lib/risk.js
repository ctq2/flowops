/**
 * Risk model shared by the console.
 *
 * These thresholds are mirrored in the Python domain (`app.domain.values`). They
 * are duplicated *deliberately* rather than fetched, because the console must be
 * able to render a static snapshot with no server at all; `tests/risk.test.js`
 * pins the numbers so the two cannot drift apart unnoticed.
 */

export const RISK_BANDS = ['breached', 'critical', 'warning', 'healthy', 'untracked'];

export const RISK_LABELS = {
  breached: '已超时',
  critical: '临界',
  warning: '关注',
  healthy: '健康',
  untracked: '未纳管',
};

/** Band thresholds as "fraction of the SLA budget already consumed". */
export const RISK_THRESHOLDS = [
  [1.0, 'breached'],
  [0.85, 'critical'],
  [0.6, 'warning'],
  [0.0, 'healthy'],
];

/** Mirror of Python's `band_for()`, including the closed-ticket rule. */
export function bandFor(consumedRatio, { terminal = false } = {}) {
  if (consumedRatio === null || consumedRatio === undefined || Number.isNaN(consumedRatio)) {
    return 'untracked';
  }
  if (terminal) {
    // A closed ticket can be reported as breached; that is the number customers
    // argue about, so it is never hidden.
    return consumedRatio > 1 ? 'breached' : 'healthy';
  }
  for (const [threshold, band] of RISK_THRESHOLDS) {
    if (consumedRatio >= threshold) return band;
  }
  return 'untracked';
}

export function isTerminal(status) {
  return status === 'resolved' || status === 'closed';
}

export function riskOf(ticket) {
  if (!ticket) return 'untracked';
  if (ticket.risk) return ticket.risk;
  const duration = effectiveHours(ticket);
  if (!duration) return 'untracked';
  const consumed = Number(ticket.sla_consumed_hours ?? 0);
  return bandFor(consumed / duration, { terminal: isTerminal(ticket.status) });
}

export function effectiveHours(ticket) {
  if (!ticket) return null;
  const duration = Number(ticket.sla_duration_hours);
  if (!Number.isFinite(duration) || duration <= 0) return null;
  const snooze = Number(ticket.sla_snooze_hours ?? 0);
  return Math.max(0, duration - snooze);
}

export function consumedRatio(ticket) {
  const duration = effectiveHours(ticket);
  if (duration === null) return null;
  return Number(ticket.sla_consumed_hours ?? 0) / duration;
}

export function remainingHours(ticket) {
  const duration = effectiveHours(ticket);
  if (duration === null) return null;
  return duration - Number(ticket.sla_consumed_hours ?? 0);
}

/** Sort key that puts the most urgent ticket first: risk, then priority, then age. */
export function urgencyScore(ticket) {
  const band = riskOf(ticket);
  const bandWeight = { breached: 0, critical: 1, warning: 2, healthy: 3, untracked: 4 }[band] ?? 4;
  const priorityWeight = { P0: 0, P1: 1, P2: 2, P3: 3 }[ticket.priority] ?? 4;
  return bandWeight * 10 + priorityWeight;
}

export function orderByUrgency(tickets) {
  return [...tickets].sort((a, b) => {
    const diff = urgencyScore(a) - urgencyScore(b);
    if (diff !== 0) return diff;
    return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
  });
}

export const PRIORITY_LABELS = {
  P0: '一级故障',
  P1: '重要',
  P2: '常规',
  P3: '咨询',
};

export const STATUS_LABELS = {
  open: '待分诊',
  triaged: '已分诊',
  in_progress: '处理中',
  blocked: '阻塞',
  resolved: '已解决',
  closed: '已关闭',
};

export function riskLabel(band) {
  return RISK_LABELS[band] ?? band;
}

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? status;
}

export function priorityLabel(priority) {
  return PRIORITY_LABELS[priority] ?? priority;
}
