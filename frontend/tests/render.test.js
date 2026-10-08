/**
 * Headless render check.
 *
 * HTTP 200 proves a file was served, not that the console *works*.  This test
 * gives Node a minimal DOM, boots the real application against the real server,
 * and asserts that every view actually rendered HTML — which is the only cheap
 * way to catch the class of bug that shows up in a browser as a blank page.
 *
 * Usage: node tests/render.test.js [baseUrl]
 */

import test from 'node:test';
import assert from 'node:assert/strict';

const BASE = process.argv[2] ?? process.env.FLOWOPS_BASE ?? 'http://127.0.0.1:8787';

/* ------------------------------------------------------------------ DOM stub */

class ClassList {
  constructor() {
    this.tokens = new Set();
  }
  add(...names) {
    names.forEach((name) => this.tokens.add(name));
  }
  remove(...names) {
    names.forEach((name) => this.tokens.delete(name));
  }
  contains(name) {
    return this.tokens.has(name);
  }
  toggle(name, force) {
    const on = force === undefined ? !this.tokens.has(name) : Boolean(force);
    if (on) this.tokens.add(name);
    else this.tokens.delete(name);
    return on;
  }
  toString() {
    return [...this.tokens].join(' ');
  }
}

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this.dataset = {};
    this.hidden = false;
    this.value = '';
    this.textContent = '';
    this.title = '';
    this.disabled = false;
    this.classList = new ClassList();
    this.attributes = new Map();
    this.children = [];
    this.listeners = new Map();
    this._html = '';
    this.parentElement = null;
    this.ownerDocument = null;
  }

  set innerHTML(value) {
    this._html = String(value ?? '');
  }
  get innerHTML() {
    return this._html;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  removeEventListener(type, handler) {
    const list = this.listeners.get(type) ?? [];
    this.listeners.set(type, list.filter((item) => item !== handler));
  }
  dispatch(type, event = {}) {
    for (const handler of this.listeners.get(type) ?? []) handler({ type, target: this, ...event });
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }
    if (name === 'id') this.id = String(value);
  }
  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  closest() {
    return null;
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  focus() {}
  blur() {}
  remove() {}
}

function buildDocument() {
  const registry = new Map();
  const document = {
    body: new Element('body'),
    head: new Element('head'),
    activeElement: null,
    createElement: (tag) => {
      const element = new Element(tag);
      element.ownerDocument = document;
      return element;
    },
    getElementById: (id) => registry.get(id) ?? null,
    querySelector: (selector) => (selector === '.layout' ? document._layout : null),
    querySelectorAll: (selector) => {
      if (selector === '.tab') return TAB_BUTTONS;
      return [];
    },
    addEventListener: () => {},
    removeEventListener: () => {},
    _register: (id) => {
      const element = new Element('div', id);
      element.ownerDocument = document;
      registry.set(id, element);
      return element;
    },
    _registry: registry,
  };

  for (const id of ['view', 'detail', 'chip-mode', 'chip-policy', 'chip-calendar',
                    'footer-left', 'footer-right', 'global-search', 'toast']) {
    document._register(id);
  }
  document._register('view').className = 'view';
  const layout = new Element('main');
  layout.classList.add('layout');
  document._layout = layout;

  const TAB_BUTTONS = ['dashboard', 'tickets', 'sandbox', 'policy', 'calendar'].map((name) => {
    const button = new Element('button');
    button.dataset.view = name;
    return button;
  });
  document._tabs = TAB_BUTTONS;
  return document;
}

globalThis.document = buildDocument();
globalThis.window = globalThis;
globalThis.location = { search: '', protocol: 'http:', href: `${BASE}/` };
if (typeof globalThis.CustomEvent !== 'function') {
  globalThis.CustomEvent = class CustomEvent extends Event {
    constructor(type, options = {}) {
      super(type);
      this.detail = options.detail;
    }
  };
}
const realFetch = globalThis.fetch;
globalThis.fetch = (url, options) => realFetch(String(url).startsWith('http') ? url : `${BASE}${url}`, options);

/* ------------------------------------------------------------------- the test */

const { boot } = await import('../src/app.js');

test('the console boots against a live server and renders every view', async () => {
  await boot();

  const doc = globalThis.document;
  const view = doc.getElementById('view');
  assert.ok(view.innerHTML.length > 500, `dashboard rendered only ${view.innerHTML.length} chars`);
  assert.match(view.innerHTML, /运营总览|工单/);

  const mode = doc.getElementById('chip-mode');
  assert.match(mode.textContent, /后端 API|静态快照/, `unexpected source chip: ${mode.textContent}`);
  assert.match(doc.getElementById('chip-policy').textContent, /策略/, 'policy chip was not populated');

  for (const name of ['tickets', 'sandbox', 'policy', 'calendar', 'dashboard']) {
    const button = doc._tabs.find((tab) => tab.dataset.view === name);
    button.dispatch('click');
    const html = view.innerHTML;
    assert.ok(html.length > 400, `view "${name}" rendered only ${html.length} chars`);
    assert.ok(!/undefined|\[object Object\]/.test(html), `view "${name}" leaked a raw value: ${html.slice(0, 200)}`);
  }
});

test('the dashboard shows the seeded tickets and their risk bands', async () => {
  const view = globalThis.document.getElementById('view');
  globalThis.document._tabs.find((tab) => tab.dataset.view === 'dashboard').dispatch('click');
  const html = view.innerHTML;
  assert.match(html, /已超时|临期|SLA 风险分布/, 'dashboard is missing the SLA widgets');
  assert.match(html, /TCK-2025-\d+/, 'no ticket identifiers were rendered');
});
