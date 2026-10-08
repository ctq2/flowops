/**
 * Policy sandbox — "what would this policy do with this ticket?".
 *
 * Three evaluation paths, in order of preference:
 *
 * 1. the live API (`POST /policy/preview`) — the authoritative engine;
 * 2. Pyodide running the *same* Python engine bundle in the browser;
 * 3. the static snapshot's shipped rule list, in which case the form explains
 *    that a live evaluation is unavailable instead of inventing an answer.
 *
 * Never a JavaScript re-implementation of the DSL: a second implementation is a
 * second set of bugs, and the sandbox's whole value is that it tells the truth.
 */

import { escapeHtml, formatDateTime, formatHours } from '../lib/format.js';
import { PythonEngine } from '../lib/python-bridge.js';

export const PRESETS = [
  {
    label: '一级故障',
    ticket: {
      title: '核心支付网关 502，全站下单失败',
      body: '9:12 起支付回调全部 502，影响全部用户下单。',
      channel: 'alert',
      priority: 'P0',
      tags: ['incident'],
      labels: { env: 'prod' },
    },
  },
  {
    label: '数据泄露',
    ticket: {
      title: '疑似越权：普通账号可读取他人订单',
      body: '安全扫描发现 /api/orders/{id} 未校验归属。',
      channel: 'alert',
      tags: ['security'],
    },
  },
  {
    label: 'VIP 咨询',
    ticket: {
      title: '请问如何申请沙箱环境接口权限？',
      body: '需要测试密钥与接入文档。',
      channel: 'email',
      labels: { tier: 'vip' },
    },
  },
  {
    label: '国庆前的 4 小时时限',
    ticket: {
      title: '退款到账延迟，用户投诉',
      body: '支付渠道退款 3 天未到账。',
      channel: 'payments',
      createdAtOffsetHours: -34,
    },
  },
];

export function renderSandbox(root, context) {
  const { store, bridge, bridgeStatus, bridgeReason } = context;
  const draft = context.sandboxDraft ?? PRESETS[0].ticket;
  const result = context.sandboxResult ?? null;
  const error = context.sandboxError ?? '';
  const pending = context.sandboxPending ?? false;

  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>策略沙盘</h2>
        <span class="hint">改动工单字段，实时查看策略命中与 SLA 兜底结果</span>
      </div>
      ${bridgeNotice(store, bridge, bridgeStatus, bridgeReason)}
      <div class="sandbox-grid">
        <form id="sandbox-form">
          <div class="preset-list">
            ${PRESETS.map(
              (preset, index) =>
                `<button type="button" data-preset="${index}">${escapeHtml(preset.label)}</button>`,
            ).join('')}
          </div>

          <div class="form-row">
            <label for="sb-title">标题</label>
            <input id="sb-title" name="title" value="${escapeHtml(draft.title ?? '')}"
              placeholder="例如：核心支付网关 502" />
          </div>
          <div class="form-row">
            <label for="sb-body">内容</label>
            <textarea id="sb-body" name="body" placeholder="补充现象、影响面">${escapeHtml(draft.body ?? '')}</textarea>
          </div>
          <div class="form-row">
            <label for="sb-channel">渠道</label>
            <select id="sb-channel" name="channel">
              ${['web', 'email', 'alert', 'payments', 'phone', 'im']
                .map(
                  (channel) =>
                    `<option value="${channel}" ${(draft.channel ?? 'web') === channel ? 'selected' : ''}>${channel}</option>`,
                )
                .join('')}
            </select>
          </div>
          <div class="form-row">
            <label for="sb-priority">提交时优先级（策略可覆盖）</label>
            <select id="sb-priority" name="priority">
              ${['P3', 'P2', 'P1', 'P0']
                .map(
                  (priority) =>
                    `<option value="${priority}" ${(draft.priority ?? 'P3') === priority ? 'selected' : ''}>${priority}</option>`,
                )
                .join('')}
            </select>
          </div>
          <div class="form-row">
            <label for="sb-tags">标签（逗号分隔）</label>
            <input id="sb-tags" name="tags" value="${escapeHtml((draft.tags ?? []).join(', '))}" />
          </div>
          <div class="form-row">
            <label for="sb-tier">客户等级（labels.tier）</label>
            <input id="sb-tier" name="tier" value="${escapeHtml(draft.labels?.tier ?? '')}" placeholder="vip / normal" />
          </div>
          <div class="form-row">
            <label for="sb-created">创建时间（决定已消耗的 SLA 时长）</label>
            <input id="sb-created" name="created_at" type="datetime-local" value="${escapeHtml(toLocalInput(draft.created_at ?? defaultCreatedAt(draft)))}" />
            <span class="form-hint">留空则按“刚刚创建”计算；填过去的时间可以看到超时判定。</span>
          </div>

          <div class="detail-actions">
            <button class="btn primary" type="submit" ${pending ? 'disabled' : ''}>
              ${pending ? '<span class="spinner"></span> 求值中…' : '运行策略'}
            </button>
            <button class="btn ghost" type="reset">重置</button>
          </div>
        </form>

        <div id="sandbox-result">
          ${error ? `<div class="notice warn"><span>${escapeHtml(error)}</span></div>` : ''}
          ${result ? resultHtml(result) : placeholderHtml(store)}
        </div>
      </div>
    </section>
  `;
}

function placeholderHtml(store) {
  return `<div class="empty-state">
    <h2>等待运行</h2>
    <p>点击「运行策略」查看命中规则、动作链路与解析后的 SLA 到期时间。</p>
    ${
      store.source === 'live'
        ? '<p class="form-hint">当前由后端引擎求值，与线上完全一致。</p>'
        : '<p class="form-hint">当前为静态快照；若浏览器可访问 CDN，将自动使用 Pyodide 载入同一份 Python 引擎。</p>'
    }
  </div>`;
}

function bridgeNotice(store, bridge, status, reason) {
  if (store.source === 'live') {
    return `<div class="notice ok"><span><strong>求值来源：后端 Python 引擎</strong>（<code>POST /api/v1/policy/preview</code>），与生产完全同源。</span></div>`;
  }
  if (status === 'ready') {
    return `<div class="notice ok"><span><strong>求值来源：浏览器内 Pyodide</strong> — 载入的正是后端使用的同一份规则引擎代码。</span></div>`;
  }
  if (status === 'loading') {
    return `<div class="notice"><span class="spinner"></span> ${escapeHtml(reason || '正在载入 Python 引擎…')}</span></div>`;
  }
  if (status === 'unavailable') {
    return `<div class="notice warn"><span><strong>浏览器内引擎不可用</strong>：${escapeHtml(reason)}<br />
      可以启动后端（<code>python -m app.cli serve --store sqlite --seed</code>）后在此获得权威求值，
      或运行 <code>python -m app.cli demo</code> 在终端查看全部示例工单的真实命中链路。</span></div>`;
  }
  return `<div class="notice"><span>未检测到后端；若需要权威求值，请启动 <code>python -m app.cli serve</code>，或允许浏览器访问 Pyodide CDN。</span></div>`;
}

function resultHtml(decision) {
  const fields = decision.fields ?? {};
  const trace = decision.trace ?? [];
  const rows = trace
    .map(
      (entry) => `<tr>
        <td class="rule-cell">${escapeHtml(entry.rule)}</td>
        <td>${escapeHtml(entry.action)}</td>
        <td class="before">${escapeHtml(show(entry.before))}</td>
        <td class="after">${escapeHtml(show(entry.after))}</td>
      </tr>`,
    )
    .join('');

  return `
    <div class="notice ok">
      <span>命中规则 <b>${escapeHtml((decision.fired ?? []).join(' → ') || '(未命中，走兜底规则)')}</b>
      · 策略 v${escapeHtml(String(decision.policy?.version ?? ''))}
      · 指纹 ${escapeHtml(String(decision.policy?.hash ?? ''))}</span>
    </div>

    <div class="field-grid">
      <div class="field"><dt>定级</dt><dd>${escapeHtml(show(fields.priority))}</dd></div>
      <div class="field"><dt>负责团队</dt><dd>${escapeHtml(show(fields.team))}</dd></div>
      <div class="field"><dt>承诺时限</dt><dd>${escapeHtml(formatHours(fields.sla_duration_hours))}</dd></div>
      <div class="field"><dt>计时口径</dt><dd>${escapeHtml(fields.sla_calendar === 'wall' ? '自然小时' : '工作日历')}</dd></div>
      <div class="field"><dt>已消耗</dt><dd>${escapeHtml(formatHours(fields.sla_consumed_hours))}</dd></div>
      <div class="field"><dt>剩余</dt><dd>${escapeHtml(formatHours(fields.sla_remaining_hours))}</dd></div>
      <div class="field"><dt>SLA 到期</dt><dd>${escapeHtml(formatDateTime(fields.sla_due_at))}</dd></div>
      <div class="field"><dt>升级时间</dt><dd>${escapeHtml(formatDateTime(fields.escalation_at))}</dd></div>
    </div>

    ${
      (decision.notifications ?? []).length
        ? `<div class="notice"><span>升级通知：${decision.notifications
            .map(
              (item) =>
                `${escapeHtml(item.channel)}（+${escapeHtml(String(item.after_hours ?? 0))}h → ${escapeHtml(item.target ?? '—')}）`,
            )
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
      <summary class="text-dim" style="cursor:pointer;font-size:12.5px">查看求值作用域</summary>
      <pre class="code" style="margin-top:var(--space-2)">${escapeHtml(JSON.stringify(decision.scope ?? {}, null, 2))}</pre>
    </details>
  `;
}

function show(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function toLocalInput(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function defaultCreatedAt(draft) {
  const offsetHours = Number(draft.createdAtOffsetHours ?? 0);
  return new Date(Date.now() + offsetHours * 3600 * 1000).toISOString();
}

/** Read the form into the ticket draft the engine expects. */
export function readSandboxForm(form) {
  const data = new FormData(form);
  const tags = String(data.get('tags') ?? '')
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
  const tier = String(data.get('tier') ?? '').trim();
  const createdRaw = String(data.get('created_at') ?? '').trim();
  const draft = {
    title: String(data.get('title') ?? '').trim(),
    body: String(data.get('body') ?? '').trim(),
    channel: String(data.get('channel') ?? 'web'),
    priority: String(data.get('priority') ?? 'P3'),
    tags,
    labels: tier ? { tier } : {},
    status: 'open',
  };
  if (createdRaw) {
    const parsed = new Date(createdRaw);
    if (!Number.isNaN(parsed.getTime())) draft.created_at = parsed.toISOString();
  }
  return draft;
}

/** One-shot engine wrapper so the app can evaluate without importing Pyodide. */
export function createBridge(onStatus) {
  return new PythonEngine({ onStatus });
}
