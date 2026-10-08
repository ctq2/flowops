/**
 * Dashboard view: what an operations lead looks at first.
 *
 * Every number is derived from the ticket list at render time rather than read
 * from a stale aggregate, so the KPI row and the tables below it can never
 * disagree with each other.
 */

import { attainment, countBy, riskHistogram, summarise } from '../lib/catalog.js';
import {
  RISK_BANDS,
  orderByUrgency,
  priorityLabel,
  riskLabel,
  riskOf,
  statusLabel,
} from '../lib/risk.js';
import { escapeHtml, formatHours, formatNumber, formatPercent, relativeTime, truncate } from '../lib/format.js';

export function renderDashboard(root, context) {
  const { store } = context;
  const tickets = store.all();
  const stats = summarise(tickets);
  const histogram = riskHistogram(tickets);
  const attainmentRatio = attainment(tickets);
  const teams = countBy(tickets, (ticket) => ticket.team ?? 'unassigned');
  const priorities = countBy(tickets, (ticket) => ticket.priority);
  const attention = orderByUrgency(
    tickets.filter((ticket) => riskOf(ticket) === 'breached' || riskOf(ticket) === 'critical'),
  ).slice(0, 8);

  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>运营总览</h2>
        <span class="hint">${escapeHtml(describeSource(store))}</span>
      </div>
      <div class="kpi-grid">
        ${kpi('工单总数', formatNumber(stats.total), `未结 ${stats.open} 张`, '')}
        ${kpi('已超时', formatNumber(stats.breached), '超出 SLA 目标', stats.breached ? 'danger' : 'ok')}
        ${kpi('临期（≥60%）', formatNumber(stats.dueSoon), '需要立刻接手', stats.dueSoon ? 'warn' : 'ok')}
        ${kpi('待指派', formatNumber(stats.unassigned), '未结且无人负责', stats.unassigned ? 'warn' : 'ok')}
        ${kpi(
          'SLA 达成率',
          attainmentRatio ? formatPercent(attainmentRatio.ratio) : '—',
          attainmentRatio ? `${attainmentRatio.met}/${attainmentRatio.closed} 张按时解决` : '暂无已解决工单',
          attainmentRatio && attainmentRatio.ratio >= 0.9 ? 'ok' : 'warn',
        )}
        ${kpi('需人工复核', formatNumber(stats.needsReview), '策略要求值班经理确认', '')}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>SLA 风险分布</h2>
        <span class="hint">按“已消耗预算 / 承诺时限”分档，时限按工作日历计算</span>
      </div>
      <div class="risk-bar" role="img" aria-label="SLA 风险分布">
        ${RISK_BANDS.map((band) => {
          const share = stats.total ? (histogram[band] / stats.total) * 100 : 0;
          return share > 0
            ? `<span class="band-${band}" style="width:${share.toFixed(2)}%" title="${riskLabel(band)} ${histogram[band]}"></span>`
            : '';
        }).join('')}
      </div>
      <div class="legend">
        ${RISK_BANDS.map(
          (band) =>
            `<span><i class="band-${band}"></i>${riskLabel(band)} <b class="mono">${histogram[band]}</b></span>`,
        ).join('')}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>需要立刻处理</h2>
        <span class="hint">超时与临界工单，按紧急度排序</span>
      </div>
      ${
        attention.length
          ? `<div class="table-wrap"><table class="tickets">
              <thead><tr><th>工单</th><th>风险</th><th>优先级</th><th>团队</th><th>剩余</th><th>状态</th></tr></thead>
              <tbody>
                ${attention
                  .map((ticket) => {
                    const band = riskOf(ticket);
                    return `<tr data-ticket="${escapeHtml(ticket.id)}">
                      <td class="title-cell"><strong>${escapeHtml(truncate(ticket.title, 46))}</strong><span class="mono">${escapeHtml(ticket.id)}</span></td>
                      <td><span class="pill text-${band}">${riskLabel(band)}</span></td>
                      <td><span class="pill ${String(ticket.priority).toLowerCase()}">${escapeHtml(ticket.priority)}</span></td>
                      <td class="text-dim">${escapeHtml(ticket.team ?? '—')}</td>
                      <td class="mono text-${band}">${formatHours(ticket.remaining_hours)}</td>
                      <td class="text-dim">${escapeHtml(statusLabel(ticket.status))}</td>
                    </tr>`;
                  })
                  .join('')}
              </tbody></table></div>`
          : '<div class="notice ok"><span>目前没有超时或临界工单。</span></div>'
      }
    </section>

    <section class="panel">
      <div class="panel-head"><h2>队列负荷</h2><span class="hint">按团队与优先级</span></div>
      <div class="kpi-grid">
        ${teams
          .slice(0, 6)
          .map((entry) =>
            kpi(
              entry.label === 'unassigned' ? '未指派团队' : entry.label,
              formatNumber(entry.count),
              `${formatPercent(entry.count / Math.max(1, stats.total))} 占比`,
              '',
            ),
          )
          .join('')}
      </div>
      <div class="legend" style="margin-top:var(--space-4)">
        ${priorities
          .map(
            (entry) =>
              `<span><span class="pill ${String(entry.label).toLowerCase()}">${escapeHtml(entry.label)}</span> ${escapeHtml(priorityLabel(entry.label))} · <b class="mono">${entry.count}</b></span>`,
          )
          .join('')}
      </div>
    </section>

    ${renderCalendarStrip(store)}
  `;
}

function renderCalendarStrip(store) {
  const upcoming = store.upcoming ?? [];
  const calendar = store.policy?.calendar ?? '';
  if (!upcoming.length && !calendar) return '';
  return `
    <section class="panel">
      <div class="panel-head"><h2>工作日历</h2><span class="hint">${escapeHtml(calendar)}</span></div>
      ${
        upcoming.length
          ? `<div class="cal-grid">
              ${upcoming
                .map(
                  (entry) => `<div class="cal-day ${entry.kind === 'holiday' ? 'holiday' : 'make-up'}">
                    <strong>${escapeHtml(entry.date)}</strong>
                    <span>${entry.kind === 'holiday' ? '法定假日（不计 SLA）' : '调休工作日（计 SLA）'}</span>
                  </div>`,
                )
                .join('')}
            </div>`
          : '<div class="notice">快照未包含未来节假日数据，请运行 <code>python -m app.cli export</code> 重新生成。</div>'
      }
      <p class="form-hint" style="margin-top:var(--space-3)">
        节假日与调休都是<b>数据</b>：新一年的国务院放假通知只需改 JSON，不需要发版。
      </p>
    </section>`;
}

function kpi(label, value, foot, tone) {
  const cls = tone ? ` is-${tone}` : '';
  return `<div class="kpi${cls}">
    <div class="kpi-label">${escapeHtml(label)}</div>
    <div class="kpi-value">${escapeHtml(value)}</div>
    <div class="kpi-foot">${escapeHtml(foot)}</div>
  </div>`;
}

function describeSource(store) {
  if (store.source === 'live') {
    return `数据源：后端 API · 策略 ${store.policy?.name ?? ''} v${store.policy?.version ?? ''}（${store.policy?.hash ?? ''}）`;
  }
  const generated = store.generatedAt ? `快照生成于 ${relativeTime(store.generatedAt, new Date())}` : '静态快照';
  return `数据源：${generated}（只读）`;
}
