/**
 * Policy & DSL reference view.
 *
 * The action catalog and function signatures come from the running engine
 * (`/api/v1/meta`) or from the exported snapshot. They are never hand-written
 * here: documentation that is generated from the implementation cannot go stale.
 */

import { escapeHtml } from '../lib/format.js';

export function renderPolicy(root, context) {
  const { store } = context;
  const policy = store.policy ?? {};
  const meta = store.meta ?? {};
  const rules = policy.rules ?? [];
  const fallback = policy.fallback ?? [];
  const actions = meta.actions ?? store.snapshotActions ?? [];
  const functions = meta.functions ?? store.snapshotFunctions ?? [];

  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>策略文件</h2>
        <span class="hint">${escapeHtml(policy.name ?? '—')} v${escapeHtml(String(policy.version ?? ''))} · 指纹 ${escapeHtml(String(policy.hash ?? ''))}</span>
      </div>
      <div class="field-grid">
        <div class="field"><dt>匹配策略</dt><dd>${escapeHtml(policy.strategy === 'accumulate' ? '累加（全部命中）' : '首个命中即停止')}</dd></div>
        <div class="field"><dt>规则数</dt><dd>${escapeHtml(String(rules.length))}</dd></div>
        <div class="field"><dt>兜底规则</dt><dd>${escapeHtml(String(fallback.length))}</dd></div>
        <div class="field"><dt>工作日历</dt><dd style="font-size:12px">${escapeHtml(policy.calendar ?? '—')}</dd></div>
      </div>
      <p class="form-hint">
        「首个命中即停止」意味着<b>规则顺序就是优先级</b>：文件里的 <code>priority</code> 决定执行顺序，
        因此可以把相关规则分组书写，而执行仍然严格有序。
      </p>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>规则链</h2><span class="hint">从上到下依次尝试匹配</span></div>
      <div class="def-list">
        ${rules.map(ruleBlock).join('')}
        ${
          fallback.length
            ? `<div class="def" style="border-bottom:none">
                <code>兜底</code>
                <div>${fallback.map(ruleBlockInline).join('')}</div>
              </div>`
            : ''
        }
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>动作目录</h2><span class="hint">由引擎导出，写错动作名会在加载策略时直接失败</span></div>
      <div class="def-list">
        ${actions.map((action) => `<div class="def"><code>${escapeHtml(action.code)}</code><p>${escapeHtml(action.docs)}</p></div>`).join('')}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>表达式函数库</h2><span class="hint">沙箱里可直接调用，例如 business_hours_between(created_at, now())</span></div>
      <div class="def-list">
        ${functions.map((item) => `<div class="def"><code>${escapeHtml(item.signature)}</code><p>${escapeHtml(item.docs)}</p></div>`).join('')}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>语法要点</h2><span class="hint">刻意做窄，避免歧义</span></div>
      <pre class="code"># 条件：比较 / 布尔 / 函数调用 / 点号取值
ticket.priority == 'P0' and business_hours_between(ticket.created_at, now()) > 2
matches('(数据泄露|越权)', lower(ticket.title))
contains(ticket.labels.tier, 'vip') or 'vip' in ticket.tags

# 动作：字符串简写（末位冒号后为值）…
assign_team:payments
escalate:to:oncall-sre

# …或对象写法（可带多个参数，表达式中可用变量）
{ "action": "set_sla:duration", "value": "2 * 3" }
{ "action": "label", "value": ["category", "defect"] }</pre>
      <p class="form-hint">
        同名嵌套字段缺失时为 <code>null</code>（不会报错），顶层字段拼写错误会报错；
        <code>null</code> 不参与任何相等比较，因此缺失字段永远不会“恰好命中”某条规则。
      </p>
    </section>
  `;
}

function ruleBlock(rule) {
  return `<div class="def">
    <code>${escapeHtml(rule.name)}</code>
    <div>
      ${rule.description ? `<p>${escapeHtml(rule.description)}</p>` : ''}
      <pre class="code" style="margin-top:6px">when  ${escapeHtml(String(rule.when ?? 'true'))}
then  ${escapeHtml((rule.actions ?? []).map(formatAction).join('\n      '))}</pre>
    </div>
  </div>`;
}

function ruleBlockInline(rule) {
  return `<div style="margin-bottom:var(--space-2)">
    <code>${escapeHtml(rule.name)}</code>
    <pre class="code" style="margin-top:4px">then  ${escapeHtml((rule.actions ?? []).map(formatAction).join('\n      '))}</pre>
  </div>`;
}

function formatAction(action) {
  if (!action) return '';
  const args = action.args ?? [];
  return args.length ? `${action.code}(${args.map((arg) => JSON.stringify(arg)).join(', ')})` : `${action.code}()`;
}
