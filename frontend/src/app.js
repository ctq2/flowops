/**
 * Console shell: routing, event wiring, keyboard shortcuts.
 *
 * All state lives in one object and every interaction funnels through `update()`,
 * which re-renders the active view. At this size a virtual DOM would be more
 * machinery than the UI has state to justify; one explicit render path is easier
 * to reason about and to test.
 */

import { Store } from './store.js';
import { isApiError } from './lib/client.js';
import { toggleValue, renderTickets } from './views/tickets.js';
import { renderDashboard } from './views/dashboard.js';
import { PRESETS, renderSandbox, readSandboxForm, createBridge } from './views/sandbox.js';
import { renderPolicy } from './views/policy.js';
import { renderCalendar } from './views/calendar.js';
import { renderDetail } from './views/detail.js';
import { escapeHtml } from './lib/format.js';

const VIEWS = ['dashboard', 'tickets', 'sandbox', 'policy', 'calendar'];

const state = {
  view: 'dashboard',
  selected: null,
  detailTab: 'overview',
  sandboxDraft: null,
  sandboxResult: null,
  sandboxError: '',
  sandboxPending: false,
  bridgeStatus: 'idle',
  bridgeReason: '',
  toastTimer: null,
};

let store;
let bridge;

export async function boot() {
  store = new Store();
  bridge = createBridge((status, reason) => {
    state.bridgeStatus = status;
    state.bridgeReason = reason;
    if (state.view === 'sandbox') render();
  });

  store.addEventListener('loaded', () => {
    paintChrome();
    render();
  });
  store.addEventListener('data', () => render());
  store.addEventListener('filters', () => render());

  wireGlobalEvents();
  await store.load();
  paintChrome();
  render();
}

/* ------------------------------------------------------------------ chrome */

function paintChrome() {
  const chipMode = document.getElementById('chip-mode');
  const chipPolicy = document.getElementById('chip-policy');
  const chipCalendar = document.getElementById('chip-calendar');
  const footerLeft = document.getElementById('footer-left');
  const footerRight = document.getElementById('footer-right');

  const live = store.source === 'live';
  chipMode.textContent = live ? '数据源：后端 API（可写）' : '数据源：静态快照（只读）';
  chipMode.className = `chip ${live ? 'is-live' : 'is-static'}`;
  chipMode.title = store.reason || '';

  chipPolicy.textContent = store.policy
    ? `策略 ${store.policy.name} v${store.policy.version} · ${store.policy.hash}`
    : '策略：未知';
  chipCalendar.textContent = store.policy?.calendar ? `日历：${shortCalendar(store.policy.calendar)}` : '';
  chipCalendar.title = store.policy?.calendar ?? '';

  footerLeft.textContent = `FlowOps · ${store.all().length} 张工单`;
  footerRight.textContent = live
    ? '写操作会真实落库（SQLite）'
    : `静态快照${store.generatedAt ? ` · ${new Date(store.generatedAt).toLocaleString('zh-CN')}` : ''}`;
}

function shortCalendar(text) {
  const value = String(text ?? '');
  const hours = value.match(/(\d{2}:\d{2}-\d{2}:\d{2})/);
  const days = value.match(/^([A-Za-z/]+)/);
  return [days?.[1], hours?.[1]].filter(Boolean).join(' ');
}

/* ------------------------------------------------------------------- render */

function render() {
  const view = document.getElementById('view');
  const detail = document.getElementById('detail');
  const layout = document.querySelector('.layout');

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.setAttribute('aria-current', tab.dataset.view === state.view ? 'page' : 'false');
  });

  const context = { store, bridge, ...state };

  switch (state.view) {
    case 'tickets':
      renderTickets(view, context);
      break;
    case 'sandbox':
      renderSandbox(view, context);
      break;
    case 'policy':
      renderPolicy(view, context);
      break;
    case 'calendar':
      renderCalendar(view, context);
      break;
    default:
      renderDashboard(view, context);
  }

  const showDetail = Boolean(state.selected);
  detail.hidden = !showDetail;
  // Defensive: a missing layout element must never blank the whole console.
  layout?.classList.toggle('has-detail', showDetail);
  if (showDetail) {
    renderDetail(detail, context, state.selected).catch((error) => {
      detail.innerHTML = `<div class="notice warn"><span>${escapeHtml(error.message)}</span></div>`;
    });
  } else {
    detail.innerHTML = '';
  }
}

function update(patch = {}, { keepDetail = true } = {}) {
  Object.assign(state, patch);
  if (!keepDetail && !('selected' in patch)) state.selected = null;
  render();
}

/* -------------------------------------------------------------------- events */

function wireGlobalEvents() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => update({ view: tab.dataset.view }));
  });

  const search = document.getElementById('global-search');
  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      store.setFilters({ search: search.value });
      if (state.view !== 'tickets' && search.value.trim()) update({ view: 'tickets' });
    }, 180);
  });

  // Delegated clicks: one listener covers rows, filters and detail actions, which
  // keeps re-rendered markup from leaking listeners.
  document.addEventListener('click', onDocumentClick);
  document.addEventListener('change', onDocumentChange);
  document.addEventListener('submit', onDocumentSubmit);
  document.addEventListener('keydown', onKeyDown);

  const detail = document.getElementById('detail');
  detail.addEventListener('click', onDetailClick);
  detail.addEventListener('change', onDetailChange);
}

function onDocumentClick(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;

  const tab = target.closest('.tab');
  if (tab) return; // handled directly

  const row = target.closest('tr[data-ticket]');
  if (row) {
    update({ selected: row.dataset.ticket, detailTab: 'overview' });
    return;
  }

  const statusButton = target.closest('[data-status]');
  if (statusButton) {
    const value = statusButton.dataset.status;
    store.setFilters({ status: value ? toggleValue(store.filters.status, value) : [] });
    return;
  }

  const priorityButton = target.closest('[data-priority]');
  if (priorityButton) {
    store.setFilters({ priority: toggleValue(store.filters.priority, priorityButton.dataset.priority) });
    return;
  }

  const riskButton = target.closest('[data-risk]');
  if (riskButton) {
    store.setFilters({ risk: toggleValue(store.filters.risk, riskButton.dataset.risk) });
    return;
  }

  if (target.closest('#reset-filters')) {
    store.setFilters({ status: [], priority: [], risk: [], team: null, search: '' });
    const search = document.getElementById('global-search');
    if (search) search.value = '';
    return;
  }

  if (target.closest('#load-more')) {
    store.setVisible(store.visible + 40);
    return;
  }

  const preset = target.closest('[data-preset]');
  if (preset) {
    applyPreset(Number(preset.dataset.preset));
    return;
  }
}

function onDocumentSubmit(event) {
  const form = event.target;
  if (form instanceof HTMLFormElement && form.id === 'sandbox-form') {
    event.preventDefault();
    runSandbox(form);
  }
}

function onDocumentChange(event) {
  const target = event.target;
  if (!(target instanceof HTMLSelectElement)) return;
  if (target.id === 'filter-team') {
    store.setFilters({ team: target.value || null });
  } else if (target.id === 'filter-sort') {
    store.setFilters({ sort: target.value });
  }
}

function onDetailClick(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;

  if (target.closest('#close-detail')) {
    update({ selected: null });
    return;
  }

  const tabButton = target.closest('[data-tab]');
  if (tabButton) {
    update({ detailTab: tabButton.dataset.tab });
    return;
  }

  const transitionButton = target.closest('[data-transition]');
  if (transitionButton) {
    applyTransition(transitionButton.dataset.transition);
    return;
  }

  const assignButton = target.closest('#assign-me');
  if (assignButton) {
    applyPatch({ assignee: '控制台操作员' });
  }
}

function onDetailChange(event) {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.id === 'detail-priority') {
    applyPatch({ priority: target.value });
  }
}

function onKeyDown(event) {
  const active = document.activeElement;
  const typing = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement;

  if (event.key === '/' && !typing) {
    event.preventDefault();
    document.getElementById('global-search')?.focus();
    return;
  }
  if (event.key === 'Escape') {
    if (state.selected) update({ selected: null });
    else if (typing) active.blur();
    return;
  }
  if (typing) return;

  const index = Number(event.key);
  if (Number.isInteger(index) && index >= 1 && index <= VIEWS.length) {
    update({ view: VIEWS[index - 1] });
  }
}

/* ------------------------------------------------------------------ sandbox */

async function applyPreset(index) {
  const preset = PRESETS[index] ?? PRESETS[0];
  const draft = { ...preset.ticket };
  if (preset.ticket.createdAtOffsetHours) {
    draft.created_at = new Date(Date.now() + preset.ticket.createdAtOffsetHours * 3600 * 1000).toISOString();
    delete draft.createdAtOffsetHours;
  }
  update({ sandboxDraft: draft, sandboxResult: null, sandboxError: '' });
  toast(`已载入预设：${preset.label}`);
}

/** Run the sandbox: live API first, then the in-browser Python engine. */
export async function runSandbox(form) {
  const draft = readSandboxForm(form);
  if (!draft.title) {
    update({ sandboxError: '请先填写标题。', sandboxResult: null });
    return;
  }
  update({ sandboxPending: true, sandboxError: '', sandboxDraft: draft });

  try {
    if (store.canMutate) {
      const decision = await store.preview(draft);
      update({ sandboxPending: false, sandboxResult: decision, sandboxDraft: draft });
      return;
    }
    const decision = await bridge.evaluate(store.document ?? defaultDocument(), draft, new Date().toISOString());
    update({ sandboxPending: false, sandboxResult: decision, sandboxDraft: draft });
  } catch (error) {
    const message = isApiError(error)
      ? `后端拒绝：${error.status} ${error.message}`
      : bridge.status === 'unavailable'
        ? '静态快照模式下无法求值：浏览器内 Python 引擎不可用。启动后端或联网后重试。'
        : `求值失败：${error.message}`;
    update({ sandboxPending: false, sandboxError: message, sandboxResult: null, sandboxDraft: draft });
  }
}

/** The in-browser engine needs the policy *as authored*: the snapshot carries it
 * under `document`, while the live API serves the same thing from `/policy`. */
function defaultDocument() {
  return store.document ?? { name: 'unknown', rules: [] };
}

/* ------------------------------------------------------------------ mutations */

async function applyTransition(status) {
  try {
    await store.transition(state.selected, status);
    toast(`已流转到 ${status}`);
    update({});
  } catch (error) {
    toast(error.message, 'err');
  }
}

async function applyPatch(patch) {
  try {
    await store.patchTicket(state.selected, patch);
    toast('已更新工单');
    update({});
  } catch (error) {
    toast(error.message, 'err');
  }
}

/* ---------------------------------------------------------------------- toast */

export function toast(message, kind = '') {
  const element = document.getElementById('toast');
  if (!element) return;
  element.textContent = message;
  element.className = `toast ${kind}`;
  element.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    element.hidden = true;
  }, 4200);
}
