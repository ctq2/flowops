/**
 * Calendar view — the part of the system that is easiest to get wrong and
 * hardest to notice: which days actually consume SLA time.
 */

import { escapeHtml } from '../lib/format.js';

export function renderCalendar(root, context) {
  const { store } = context;
  const upcoming = store.upcoming ?? [];
  const policy = store.policy ?? {};
  const calendar = parseCalendar(policy.calendar);

  root.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>工作日历</h2>
        <span class="hint">${escapeHtml(policy.calendar ?? '—')}</span>
      </div>
      <div class="field-grid">
        <div class="field"><dt>工作日</dt><dd>${escapeHtml(calendar.days.join(' / ') || '—')}</dd></div>
        <div class="field"><dt>上班时间</dt><dd>${escapeHtml(calendar.start)}</dd></div>
        <div class="field"><dt>下班时间</dt><dd>${escapeHtml(calendar.end)}</dd></div>
        <div class="field"><dt>时区</dt><dd>${escapeHtml(calendar.timezone)}</dd></div>
      </div>
      <p class="form-hint">
        SLA 默认按<b>工作日历</b>计时：非工作时段不计入消耗，因此周五 17:00 提交的 4 小时时限通常落在下周一。
        需要按自然小时计算时，规则里用 <code>{ "action": "set_sla:calendar", "value": "wall" }</code> 覆盖。
      </p>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>即将到来的假日与调休</h2>
        <span class="hint">数据来自 config/calendars/cn-holidays.json</span>
      </div>
      ${
        upcoming.length
          ? `<div class="cal-grid">
              ${upcoming
                .map(
                  (entry) => `<div class="cal-day ${entry.kind === 'holiday' ? 'holiday' : 'make-up'}">
                    <strong>${escapeHtml(entry.date)}</strong>
                    <span>${entry.kind === 'holiday' ? '法定假日 · 不计 SLA' : '调休工作日 · 计 SLA'}</span>
                  </div>`,
                )
                .join('')}
            </div>`
          : `<div class="notice"><span>快照中没有未来假日数据。运行 <code>python -m app.cli export</code> 重新导出即可包含。</span></div>`
      }
    </section>

    <section class="panel">
      <div class="panel-head"><h2>为什么这很重要</h2><span class="hint">三个真实场景</span></div>
      <div class="def-list">
        <div class="def"><code>国庆长假</code><p>9 月 30 日 17:00 提交、承诺 4 小时：当晚只剩 1 小时，剩下 3 小时顺延到 10 月 9 日（10-01～10-08 全部闭馆），到期时间为 10-09 12:00。</p></div>
        <div class="def"><code>春节调休</code><p>2026-02-28（周六）是国务院公布的调休工作日，因此它<b>计</b>入 SLA；而 2026-03-01（周日）不计。只按“周末休息”写的日历会把这两类日期全部算错。</p></div>
        <div class="def"><code>夜班日历</code><p>把上班时间设为 22:00–06:00 也能正确工作：跨零点的班次归属开始的那一天，凌晨 05:00 仍算同一班。</p></div>
      </div>
      <pre class="code">$ python -m app.cli calendar --limit 12     # 查看年度假日安排
$ python -m app.cli demo                    # 查看每张示例工单的 SLA 推导过程</pre>
    </section>
  `;
}

/** `Mon/Tue/Wed 09:00-18:00 UTC+08:00 (23 holidays, 9 make-up days)` */
export function parseCalendar(text) {
  const value = String(text ?? '');
  const result = { days: [], start: '—', end: '—', timezone: '—', holidays: 0, makeUp: 0 };
  const days = value.match(/^([A-Za-z/]+)/);
  if (days) result.days = days[1].split('/');
  const hours = value.match(/(\d{2}:\d{2})-(\d{2}:\d{2})/);
  if (hours) {
    result.start = hours[1];
    result.end = hours[2];
  }
  const tz = value.match(/(UTC[+-]\d{2}:\d{2})/);
  if (tz) result.timezone = tz[1];
  const holidays = value.match(/(\d+)\s*holidays?/i);
  if (holidays) result.holidays = Number(holidays[1]);
  const makeUp = value.match(/(\d+)\s*make-up/i);
  if (makeUp) result.makeUp = Number(makeUp[1]);
  return result;
}
