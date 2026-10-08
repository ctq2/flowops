/**
 * Ticket detail panel.
 *
 * The interesting tab is 「策略解释」: it re-runs the policy for this ticket and
 * shows which rule fired and what each action changed, before → after. That is
 * the question an operator actually asks — "why is this P1 in the payments
 * queue?" — and it is answerable because the engine records a trace.
 */

import { PRIORITY_LABELS, STATUS_LABELS, riskLabel, riskOf } from '../lib/risk.js';
import {
  clamp01,
  escapeHtml,
  formatDateTime,
  formatHours,
  formatPercent,
  relativeTime,
} from '../lib/format.js';

const TRANSITION_LABELS = {
  triaged: '确认分诊',
  in_progress: '开始处理',
  blocked: '标记阻塞',
  resolved: '标记已解决',
  closed: '归档关闭',
  open: '退回待分诊',
};

export async function renderDetail(root, context, ticketId) {
  const { store } = context;
  const ticket = store.find(ticketId);
  if (!ticket) {
    root.innerHTML = `<div class="empty-state"><h2>工单不存在</h2><p class="mono">${escapeHtml(ticketId)}</p></div>`;
    return;
  }

  const band = riskOf(ticket);
  const ratio = ticket.consumed_ratio ?? 0;
  const transitions = ticket.allowed_transitions ?? [];
  const tab = context.detailTab ?? 'overview';

  root.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${escapeHtml(ticket.title)}</h2>
        <div class="text-faint mono" style="font-size:12px">${escapeHtml(ticket.id)}</div>
      </div>
      <button class="btn ghost" id="close-detail" title="关闭（Esc）">✕</button>
    </div>

    <div class="detail-meta">
      <span class="pill ${String(ticket.priority).toLowerCase()}">${escapeHtml(ticket.priority)} · ${escapeHtml(PRIORITY_LABELS[ticket.priority] ?? '')}</span>
      <span class="pill status">${escapeHtml(STATUS_LABELS[ticket.status] ?? ticket.status)}</span>
      <span class="pill text-${band}">${escapeHtml(riskLabel(band))} ${formatPercent(ratio)}</span>
      ${ticket.required_review ? `<span class="pill" title="${escapeHtml(ticket.required_review)}">需复核</span>` : ''}
    </div>

    <div class="sla-gauge" style="height:8px"><i class="band-${band}" style="width:${(clamp01(ratio) * 100).toFixed(1)}%"></i></div>
    <div class="legend" style="margin-top:6px">
      <span>时限 <b class="mono">${formatHours(ticket.sla_duration_hours)}</b></span>
      <span>已消耗 <b class="mono">${formatHours(ticket.sla_consumed_hours)}</b></span>
      <span>剩余 <b class="mono text-${band}">${formatHours(ticket.remaining_hours)}</b></span>
    </div>

    ${
      transitions.length
        ? `<div class="detail-actions">
            ${transitions
              .map((target) => {
                const isDanger = target === 'closed';
                const label = TRANSITION_LABELS[target] ?? target;
                return `<button class="btn ${isDanger ? 'danger' : 'primary'}" data-transition="${escapeHtml(target)}"
                  ${store.canMutate ? '' : 'disabled title="静态快照模式为只读"'}>${escapeHtml(label)}</button>`;
              })
              .join('')}
          </div>`
        : '<p class="form-hint">该工单已处于终态，可重新打开以继续处理。</p>'
    }

    <div class="tabs-inline" role="tablist">
      ${['overview', 'policy', 'audit', 'raw']
        .map(
          (name) =>
            `<button role="tab" data-tab="${name}" aria-selected="${tab === name}">${{
              overview: '概览',
              policy: '策略解释',
              audit: '审计轨迹',
              raw: '原始数据',
            }[name]}</button>`,
        )
        .join('')}
    </div>

    <div id="detail-body">
      <div class="notice"><span class="spinner"></span> 正在加载…</div>
    </div>
  `;

  const body = root.querySelector('#detail-body');
  try {
    if (tab === 'overview') body.innerHTML = overviewHtml(ticket);
    else if (tab === 'policy') body.innerHTML = await policyHtml(store, ticket);
    else if (tab === 'audit') body.innerHTML = await auditHtml(store, ticket);
    else body.innerHTML = rawHtml(ticket);
  } catch (error) {
    body.innerHTML = `<div class="notice warn"><span>加载失败：${escapeHtml(error.message)}</span></div>`;
  }
}

function overviewHtml(ticket) {
  const fields = [
    ['报告人', ticket.reporter],
    ['渠道', ticket.channel],
    ['负责团队', ticket.team ?? '—'],
    ['处理人', ticket.assignee ?? '未指派'],
    ['创建时间', formatDateTime(ticket.created_at)],
    ['最近更新', formatDateTime(ticket.updated_at)],
    ['SLA 到期', formatDateTime(ticket.sla_due_at)],
    ['升级时间', formatDateTime(ticket.escalation_at)],
    ['升级对象', ticket.escalation_target ?? '—'],
    ['SLA 口径', ticket.sla_calendar === 'wall' ? '自然小时' : '工作日历'],
    ['暂停时长', formatHours(ticket.sla_snooze_hours)],
    ['策略指纹', ticket.policy_hash ?? '—'],
  ];
  return `
    <div class="field-grid">
      ${fields
        .map(
          ([label, value]) =>
            `<div class="field"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value ?? '—'))}</dd></div>`,
        )
        .join('')}
    </div>
    ${
      ticket.body
        ? `<h3 style="font-size:13px;margin:var(--space-4) 0 var(--space-2)">工单内容</h3>
           <pre class="code">${escapeHtml(ticket.body)}</pre>`
        : ''
    }
    ${
      (ticket.tags ?? []).length || Object.keys(ticket.labels ?? {}).length
        ? `<h3 style="font-size:13px;margin:var(--space-4) 0 var(--space-2)">标签</h3>
           <div class="detail-meta">
             ${(ticket.tags ?? []).map((tag) => `<span class="pill">${escapeHtml(tag)}</span>`).join('')}
             ${Object.entries(ticket.labels ?? {})
               .map(
                 ([key, value]) =>
                   `<span class="pill mono">${escapeHtml(key)}=${escapeHtml(String(value))}</span>`,
               )
               .join('')}
           </div>`
        : ''
    }
    <p class="form-hint" style="margin-top:var(--space-4)">
      创建于 ${escapeHtml(relativeTime(ticket.created_at, new Date()))}，工单版本 v${escapeHtml(String(ticket.version ?? 1))}。
    </p>
  `;
}

async function policyHtml(store, ticket) {
  const decision = await store.preview(ticketDraft(ticket));
  if (!decision) {
    return `<div class="notice warn">
      <span><strong>只读快照模式</strong><br />
      策略解释需要调用后端 <code>/policy/preview</code> 重新求值。启动后端
      （<code>python -m app.cli serve --store sqlite --seed</code>）后此处会显示完整的命中链路。</span>
    </div>
    ${explainStatic(store, ticket)}`;
  }
  const rows = (decision.trace ?? [])
    .map(
      (entry) => `<tr>
        <td class="rule-cell">${escapeHtml(entry.rule)}</td>
        <td>${escapeHtml(entry.action)}</td>
        <td class="before">${escapeHtml(render(entry.before))}</td>
        <td class="after">${escapeHtml(render(entry.after))}</td>
      </tr>`,
    )
    .join('');

  return `
    <div class="notice ok">
      <span>命中规则：<b>${escapeHtml((decision.fired ?? []).join(' → ') || '(未命中，走兜底)')}</b><br />
      策略 ${escapeHtml(decision.policy?.name ?? '')} v${escapeHtml(decision.policy?.version ?? '')}
      · 指纹 ${escapeHtml(decision.policy?.hash ?? '')}</span>
    </div>
    ${
      (decision.notifications ?? []).length
        ? `<div class="notice"><span>升级通知：${decision.notifications
            .map((item) => `${escapeHtml(item.channel)}（+${escapeHtml(String(item.after_hours ?? 0))}h → ${escapeHtml(item.target ?? '—')}）`)
            .join('、')}</span></div>`
        : ''
    }
    <div class="table-wrap">
      <table class="trace">
        <thead><tr><th>规则</th><th>动作</th><th>变更前</th><th>变更后</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="text-faint">没有动作被执行</td></tr>'}</tbody>
      </table>
    </div>
    ${
      (decision.notes ?? []).length
        ? `<h3 style="font-size:13px;margin:var(--space-4) 0 var(--space-2)">策略备注</h3>
           <ul class="timeline">${decision.notes.map((note) => `<li><p>${escapeHtml(note)}</p></li>`).join('')}</ul>`
        : ''
    }
    <details style="margin-top:var(--space-4)">
      <summary class="text-dim" style="cursor:pointer;font-size:12.5px">查看求值作用域（规则能看到的全部字段）</summary>
      <pre class="code" style="margin-top:var(--space-2)">${escapeHtml(JSON.stringify(decision.scope ?? {}, null, 2))}</pre>
    </details>
  `;
}

function explainStatic(store, ticket) {
  const steps = [
    { title: `定级 ${ticket.priority}`, body: `${PRIORITY_LABELS[ticket.priority] ?? ''}` },
    {
      title: `路由到 ${ticket.team ?? '未指派'}`,
      body: ticket.policy_hash ? `策略指纹 ${ticket.policy_hash}` : '',
    },
    {
      title: `承诺时限 ${formatHours(ticket.sla_duration_hours)}（${ticket.sla_calendar === 'wall' ? '自然小时' : '工作日历'}）`,
      body: `到期 ${formatDateTime(ticket.sla_due_at)}${ticket.sla_snooze_hours ? `，其中暂停 ${formatHours(ticket.sla_snooze_hours)}` : ''}`,
    },
    {
      title: ticket.escalation_at ? `升级于 ${formatDateTime(ticket.escalation_at)}` : '未设置升级',
      body: ticket.escalation_target ? `升级对象 ${ticket.escalation_target}` : '',
    },
  ];
  return `<ul class="timeline">${steps
    .map((step) => `<li><strong>${escapeHtml(step.title)}</strong>${step.body ? `<p>${escapeHtml(step.body)}</p>` : ''}</li>`)
    .join('')}</ul>`;
}

async function auditHtml(store, ticket) {
  const entries = await store.audit(ticket.id);
  if (entries === null) {
    return `<div class="notice warn"><span>审计轨迹保存在后端。静态快照模式下列表数据来自导出结果，不含审计表。</span></div>
      ${explainStatic(store, ticket)}`;
  }
  if (!entries.length) {
    return '<div class="notice"><span>暂无审计记录。</span></div>';
  }
  return `<ul class="timeline">
    ${entries
      .map((entry) => {
        const changes = Object.entries(entry.changes ?? {})
          .map(([key, value]) => `${key}=${render(value)}`)
          .join('，');
        const fired = entry.meta?.rules_fired ?? [];
        return `<li class="${entry.action === 'ticket.transitioned' ? 'warn' : ''}">
          <time>${escapeHtml(formatDateTime(entry.at))} · ${escapeHtml(entry.actor)}</time>
          <strong>${escapeHtml(entry.action)}</strong>
          ${changes ? `<p class="mono">${escapeHtml(changes)}</p>` : ''}
          ${fired.length ? `<p>命中规则：${escapeHtml(fired.join(' → '))}</p>` : ''}
          ${entry.meta?.note ? `<p>备注：${escapeHtml(entry.meta.note)}</p>` : ''}
        </li>`;
      })
      .join('')}
  </ul>`;
}

function rawHtml(ticket) {
  return `<pre class="code">${escapeHtml(JSON.stringify(ticket, null, 2))}</pre>`;
}

function render(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function ticketDraft(ticket) {
  return {
    id: ticket.id,
    title: ticket.title,
    body: ticket.body,
    status: ticket.status,
    priority: ticket.priority,
    team: ticket.team,
    reporter: ticket.reporter,
    assignee: ticket.assignee,
    channel: ticket.channel,
    tags: ticket.tags ?? [],
    labels: ticket.labels ?? {},
    created_at: ticket.created_at,
    sla_duration_hours: ticket.sla_duration_hours,
    sla_snooze_hours: ticket.sla_snooze_hours,
    sla_calendar: ticket.sla_calendar,
  };
}
