/**
 * Data layer.
 *
 * Two interchangeable sources behind one interface:
 *
 * * **live** — the FlowOps HTTP API, so transitions and comments actually persist;
 * * **snapshot** — `data/snapshot.json`, exported by `python -m app.cli export`,
 *   so the console works from a static host (or a file) with no backend at all.
 *
 * The UI never asks which one is active; it asks whether `canMutate` is true.
 */

import { createClient, fingerprint, isApiError, newIdempotencyKey } from './lib/client.js';
import { normaliseFilters, selectTickets } from './lib/catalog.js';

const SNAPSHOT_URL = './data/snapshot.json';

export class Store extends EventTarget {
  constructor({ baseUrl = resolveApiBase(), snapshotUrl = SNAPSHOT_URL } = {}) {
    super();
    this.snapshotUrl = snapshotUrl;
    this.source = 'snapshot';
    this.reason = '';
    this.tickets = [];
    this.policy = null;
    this.document = null;
    this.meta = null;
    this.sla = null;
    this.histograms = {};
    this.upcoming = [];
    this.generatedAt = null;
    this.visible = 40;
    this.filters = normaliseFilters();
    this.client = createClient({ baseUrl });
  }

  get canMutate() {
    return this.source === 'live';
  }

  get isReady() {
    return this.tickets.length > 0 || this.source === 'live';
  }

  emit(name, detail = {}) {
    this.dispatchEvent(new CustomEvent(name, { detail }));
  }

  /** Probe the API, then fall back to the static snapshot. */
  async load() {
    try {
      const { data } = await this.client.get('/health');
      this.source = 'live';
      this.reason = '已连接后端 API';
      this.meta = (await this.client.get('/meta')).data;
      this.policy = this.meta?.policy ?? null;
      const policyResponse = await this.client.get('/policy');
      this.document = policyResponse.data?.document ?? null;
      this.sla = (await this.client.get('/sla/snapshot?horizon_hours=8')).data;
      await this.refreshTickets();
      this.emit('loaded', { source: this.source });
      return this;
    } catch (error) {
      if (isApiError(error) && error.status >= 500) {
        // A reachable-but-broken API is worth reporting; falling back silently
        // would hide a real outage behind a pretty demo.
        this.reason = `后端返回 ${error.status}：${error.message}（已回退到静态快照）`;
      } else {
        this.reason = '未检测到运行中的后端，使用静态快照（只读）';
      }
    }
    await this.loadSnapshot();
    this.emit('loaded', { source: this.source });
    return this;
  }

  async loadSnapshot() {
    const response = await fetch(this.snapshotUrl, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(
        `快照 ${this.snapshotUrl} 不可用（HTTP ${response.status}）。请先运行：python -m app.cli export`,
      );
    }
    const snapshot = await response.json();
    this.source = 'snapshot';
    this.tickets = snapshot.tickets ?? [];
    this.policy = snapshot.policy ?? null;
    this.document = snapshot.document ?? null;
    this.sla = snapshot.sla ?? null;
    this.histograms = snapshot.histograms ?? {};
    this.upcoming = snapshot.upcoming ?? [];
    this.generatedAt = snapshot.generated_at ?? null;
    this.meta = null;
    return this;
  }

  async refreshTickets() {
    const collected = [];
    let cursor = null;
    for (let page = 0; page < 40; page += 1) {
      const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : '';
      const { data } = await this.client.get(`/tickets?limit=200${suffix}`);
      collected.push(...(data?.items ?? []));
      cursor = data?.page?.next_cursor ?? null;
      if (!cursor) break;
    }
    this.tickets = collected;
    return this.tickets;
  }

  async reload() {
    if (this.source === 'live') {
      this.sla = (await this.client.get('/sla/snapshot?horizon_hours=8')).data;
      await this.refreshTickets();
    } else {
      await this.loadSnapshot();
    }
    this.emit('data', { source: this.source });
  }

  setFilters(patch) {
    this.filters = normaliseFilters({ ...this.filters, ...patch });
    this.visible = 40;
    this.emit('filters', { filters: this.filters });
  }

  setVisible(count) {
    this.visible = count;
    this.emit('filters', { filters: this.filters });
  }

  all() {
    return this.tickets;
  }

  find(ticketId) {
    return this.tickets.find((ticket) => ticket.id === ticketId) ?? null;
  }

  select(filters = this.filters) {
    const result = selectTickets(this.tickets, filters, { limit: this.visible, offset: 0 });
    return result;
  }

  async audit(ticketId) {
    if (this.source !== 'live') return null;
    const { data } = await this.client.get(`/tickets/${encodeURIComponent(ticketId)}/audit`);
    return data?.entries ?? [];
  }

  async preview(payload) {
    if (this.source !== 'live') return null;
    const { data } = await this.client.post('/policy/preview', payload);
    return data?.decision ?? null;
  }

  async validatePolicy(document) {
    if (this.source !== 'live') return null;
    const { data } = await this.client.post('/policy/validate', { document });
    return data;
  }

  async createTicket(payload) {
    this.#requireLive();
    const { data } = await this.client.post('/tickets', payload, {
      'Idempotency-Key': newIdempotencyKey(`web-${fingerprint(payload)}`),
    });
    await this.reload();
    return data;
  }

  async patchTicket(ticketId, patch) {
    this.#requireLive();
    const ticket = this.find(ticketId);
    const { data } = await this.client.patch(`/tickets/${encodeURIComponent(ticketId)}`, patch, {
      'If-Match': `"${ticket?.version ?? 0}"`,
    });
    await this.reload();
    return data;
  }

  async transition(ticketId, status, note) {
    this.#requireLive();
    const ticket = this.find(ticketId);
    const { data } = await this.client.post(
      `/tickets/${encodeURIComponent(ticketId)}/transition`,
      { status, note },
      { 'If-Match': `"${ticket?.version ?? 0}"` },
    );
    await this.reload();
    return data;
  }

  #requireLive() {
    if (!this.canMutate) {
      throw new Error('当前为静态快照模式（只读）。启动后端后即可在此执行写操作。');
    }
  }
}

/** Same-origin by default; `?api=` overrides it, and file:// needs an absolute URL. */
export function resolveApiBase() {
  try {
    const params = new URLSearchParams(globalThis.location?.search ?? '');
    const override = params.get('api');
    if (override) return override.replace(/\/$/, '');
    if (globalThis.location?.protocol === 'file:') return 'http://127.0.0.1:8787/api/v1';
    return '/api/v1';
  } catch {
    return '/api/v1';
  }
}
