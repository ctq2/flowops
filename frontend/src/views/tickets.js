/**
 * Ticket worklist: filters, sortable columns, pagination.
 *
 * Filter state lives in the store, not in the DOM, so switching views or
 * reloading data never silently discards what the operator typed.
 */

import { selectTickets, teamOptions } from '../lib/catalog.js';
import { RISK_BANDS, priorityLabel, riskLabel, riskOf, statusLabel } from '../lib/risk.js';
import { clamp01, escapeHtml, formatHours, formatPercent, relativeTime, truncate } from '../lib/format.js';

const SORTS = [
  { value: '-created_at', label: '最新创建' },
  { value: 'created_at', label: '最早创建' },
  { value: 'urgency', label: '最紧急' },
  { value: 'sla_due_at', label: '最快到期' },
  { value: 'priority', label: '优先级' },
  { value: '-updated_at', label: '最近更新' },
];

const STATUS_FILTERS = ['open', 'triaged', 'in_progress', 'blocked', 'resolved', 'closed'];

export function renderTickets(root, context) {
  const { store } = context;
  const result = selectTickets(store.all(), store.filters, { limit: store.visible });
  const teams = teamOptions(store.all());

  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>工单队列</h2>
        <span class="hint">命中 <b class="mono">${result.total}</b> 张 · 显示 ${result.items.length} 张</span>
      </div>

      <div class="filters">
        <div class="seg" role="group" aria-label="状态筛选">
          <button data-status="" aria-pressed="${result.filters.status.length === 0}">全部</button>
          ${STATUS_FILTERS.map(
            (status) =>
              `<button data-status="${status}" aria-pressed="${result.filters.status.includes(status)}">${statusLabel(status)}</button>`,
          ).join('')}
        </div>

        <div class="seg" role="group" aria-label="优先级筛选">
          ${['P0', 'P1', 'P2', 'P3']
            .map(
              (priority) =>
                `<button data-priority="${priority}" aria-pressed="${result.filters.priority.includes(priority)}">${priority}</button>`,
            )
            .join('')}
        </div>

        <div class="seg" role="group" aria-label="风险筛选">
          ${RISK_BANDS.map(
            (band) =>
              `<button data-risk="${band}" aria-pressed="${result.filters.risk.includes(band)}">${riskLabel(band)}</button>`,
          ).join('')}
        </div>

        <select class="filter" id="filter-team" aria-label="团队">
          <option value="">全部团队</option>
          ${teams
            .map(
              (team) =>
                `<option value="${escapeHtml(team)}" ${result.filters.team === team ? 'selected' : ''}>${escapeHtml(team)}</option>`,
            )
            .join('')}
        </select>

        <select class="filter" id="filter-sort" aria-label="排序">
          ${SORTS.map(
            (option) =>
              `<option value="${option.value}" ${result.filters.sort === option.value ? 'selected' : ''}>${option.label}</option>`,
          ).join('')}
        </select>

        ${
          hasActiveFilters(result.filters)
            ? '<button class="btn ghost" id="reset-filters">清除筛选</button>'
            : ''
        }
      </div>

      ${
        result.items.length
          ? `<div class="table-wrap">
              <table class="tickets">
                <thead>
                  <tr>
                    <th>工单</th>
                    <th>状态</th>
                    <th>优先级</th>
                    <th>团队 / 处理人</th>
                    <th>SLA</th>
                    <th>风险</th>
                    <th>创建</th>
                  </tr>
                </thead>
                <tbody>${result.items.map(ticketRow).join('')}</tbody>
              </table>
            </div>
            <div class="load-more">
              ${
                result.hasMore
                  ? `<button class="btn" id="load-more">加载更多（还有 ${result.total - result.items.length} 张）</button>`
                  : '<span class="text-faint">已显示全部结果</span>'
              }
            </div>`
          : `<div class="empty-state">
              <h2>没有符合条件的工单</h2>
              <p>调整筛选条件，或清除筛选查看全部 ${store.all().length} 张工单。</p>
            </div>`
      }
    </section>
  `;
}

function ticketRow(ticket) {
  const band = riskOf(ticket);
  const ratio = ticket.consumed_ratio ?? 0;
  const gauge = clamp01(ratio);
  const label = riskLabel(band);
  return `<tr data-ticket="${escapeHtml(ticket.id)}" tabindex="0">
    <td class="title-cell">
      <strong>${escapeHtml(truncate(ticket.title, 62))}</strong>
      <span class="mono">${escapeHtml(ticket.id)}</span>
      ${
        ticket.required_review
          ? `<span class="pill" title="${escapeHtml(ticket.required_review)}">需复核</span>`
          : ''
      }
      ${(ticket.tags ?? [])
        .slice(0, 3)
        .map((tag) => `<span class="pill">${escapeHtml(tag)}</span>`)
        .join('')}
    </td>
    <td><span class="pill status">${escapeHtml(statusLabel(ticket.status))}</span></td>
    <td><span class="pill ${String(ticket.priority).toLowerCase()}" title="${escapeHtml(priorityLabel(ticket.priority))}">${escapeHtml(ticket.priority)}</span></td>
    <td class="text-dim">${escapeHtml(ticket.team ?? '—')}${ticket.assignee ? `<br /><span class="text-faint">${escapeHtml(ticket.assignee)}</span>` : ''}</td>
    <td class="sla-cell">
      <span class="text-${band}">${escapeHtml(label)} · ${formatPercent(ratio)}</span>
      <div class="sla-gauge"><i class="band-${band}" style="width:${(gauge * 100).toFixed(1)}%"></i></div>
      <small>剩余 ${formatHours(ticket.remaining_hours)} · 时限 ${formatHours(ticket.sla_duration_hours)}</small>
    </td>
    <td class="text-${band}">${escapeHtml(label)}</td>
    <td class="text-faint">${escapeHtml(relativeTime(ticket.created_at, new Date()))}</td>
  </tr>`;
}

function hasActiveFilters(filters) {
  return (
    filters.status.length > 0 ||
    filters.priority.length > 0 ||
    filters.risk.length > 0 ||
    Boolean(filters.team) ||
    Boolean(filters.search)
  );
}

/** Toggle a value inside a multi-select filter (used by the segmented buttons). */
export function toggleValue(list, value) {
  const set = new Set(list);
  if (set.has(value)) set.delete(value);
  else set.add(value);
  return [...set];
}
