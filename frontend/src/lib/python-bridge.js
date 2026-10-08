/**
 * Pyodide bridge — runs the *real* Python rule engine in the browser.
 *
 * Why this exists: the policy sandbox is the centrepiece of the demo, and a
 * JavaScript re-implementation of the DSL would inevitably drift from the Python
 * one. Instead the console ships `flowops-engine.zip` (built by
 * `backend/scripts/build_frontend_engine.py`) and executes it verbatim.
 *
 * Everything here degrades gracefully. Offline, or on a browser without
 * WebAssembly, `status` becomes `unavailable` with a reason and the UI falls back
 * to the decisions pre-computed into the snapshot — the console never breaks
 * because a CDN is unreachable.
 */

const DEFAULT_CDN = 'https://cdn.jsdelivr.net/pyodide/v0.26.2/full/';
const ENGINE_ZIP = './data/flowops-engine.zip';

export class PythonEngine {
  constructor({ cdnUrl = DEFAULT_CDN, zipUrl = ENGINE_ZIP, onStatus = () => {} } = {}) {
    this.cdnUrl = cdnUrl;
    this.zipUrl = zipUrl;
    this.onStatus = onStatus;
    this.status = 'idle';
    this.reason = '';
    this.pyodide = null;
    this._loading = null;
  }

  setStatus(status, reason = '') {
    this.status = status;
    this.reason = reason;
    this.onStatus(status, reason);
  }

  /** Load Pyodide and the engine zip exactly once; concurrent callers share it. */
  async load() {
    if (this.status === 'ready') return this.pyodide;
    if (this._loading) return this._loading;
    this._loading = this._load().catch((error) => {
      this._loading = null;
      this.setStatus('unavailable', describe(error));
      throw error;
    });
    return this._loading;
  }

  async _load() {
    this.setStatus('loading', '正在从 CDN 载入 Pyodide…');
    const module = await import(/* @vite-ignore */ `${this.cdnUrl}pyodide.mjs`).catch(async () => {
      // Some environments cannot resolve the ESM entry; fall back to the UMD build.
      await injectScript(`${this.cdnUrl}pyodide.js`);
      return { loadPyodide: globalThis.loadPyodide };
    });
    const pyodide = await module.loadPyodide({ indexURL: this.cdnUrl });
    this.setStatus('loading', '正在挂载 Python 规则引擎…');

    const response = await fetch(this.zipUrl);
    if (!response.ok) throw new Error(`engine bundle ${this.zipUrl} -> HTTP ${response.status}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    pyodide.FS.writeFile('/tmp/flowops-engine.zip', bytes);
    pyodide.runPython(`
import sys, zipfile
zipfile.ZipFile('/tmp/flowops-engine.zip').extractall('/engine')
if '/engine' not in sys.path:
    sys.path.insert(0, '/engine')
`);

    pyodide.runPython(PRELUDE);
    this.pyodide = pyodide;
    this.setStatus('ready', '沙盘使用与后端完全相同的 Python 规则引擎');
    return pyodide;
  }

  /** Evaluate a policy document against a ticket draft. */
  async evaluate(document, ticket, nowIso) {
    const pyodide = await this.load();
    pyodide.globals.set('_policy_json', JSON.stringify(document));
    pyodide.globals.set('_ticket_json', JSON.stringify(ticket));
    pyodide.globals.set('_now_iso', nowIso ?? new Date().toISOString());
    const raw = pyodide.runPython('flowops_evaluate(_policy_json, _ticket_json, _now_iso)');
    return JSON.parse(raw);
  }

  /** Compile a candidate policy and return its summary, or throw a typed error. */
  async validate(document) {
    const pyodide = await this.load();
    pyodide.globals.set('_policy_json', JSON.stringify(document));
    const raw = pyodide.runPython('flowops_validate(_policy_json)');
    return JSON.parse(raw);
  }

  async catalog() {
    const pyodide = await this.load();
    return JSON.parse(pyodide.runPython('flowops_catalog()'));
  }

  dispose() {
    this.pyodide = null;
    this._loading = null;
    this.setStatus('idle', '');
  }
}

/** The Python side of the bridge: thin, exception-transparent wrappers. */
const PRELUDE = `
import json
from datetime import datetime, timezone

from app.domain.rules.engine import Engine, Policy, describe_actions, describe_functions, policy_summary
from app.domain.rules.evaluator import to_jsonable


def _parse_now(text):
    moment = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _blank_ticket(now):
    return {
        'id': 'TCK-SANDBOX',
        'title': '',
        'body': '',
        'status': 'open',
        'priority': 'P3',
        'team': None,
        'reporter': 'sandbox',
        'assignee': None,
        'channel': 'web',
        'tags': [],
        'labels': {},
        'sla_duration_hours': None,
        'sla_consumed_hours': None,
        'sla_snooze_hours': 0.0,
        'sla_calendar': 'business',
        'escalation_target': None,
        'required_review': None,
        'created_at': now.isoformat(),
    }


def flowops_evaluate(policy_json, ticket_json, now_iso):
    document = json.loads(policy_json)
    now = _parse_now(now_iso)
    draft = _blank_ticket(now)
    draft.update(json.loads(ticket_json) or {})
    if not draft.get('created_at'):
        draft['created_at'] = now.isoformat()
    policy = Policy.from_dict(document)
    decision = Engine(policy.compiled).evaluate(draft, now=now, include_scope=True)
    return json.dumps(decision.as_dict(include_scope=True), ensure_ascii=False, default=str)


def flowops_validate(policy_json):
    document = json.loads(policy_json)
    policy = Policy.from_dict(document)
    return json.dumps(policy_summary(policy.compiled), ensure_ascii=False, default=str)


def flowops_catalog():
    return json.dumps(
        {
            'actions': describe_actions(),
            'functions': describe_functions(),
        },
        ensure_ascii=False,
    )
`;

function describe(error) {
  const message = error && error.message ? error.message : String(error);
  if (/Failed to fetch|NetworkError|ERR_/.test(message)) {
    return '无法访问 Pyodide CDN（离线或网络受限）';
  }
  return message.slice(0, 200);
}

function injectScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-flowops="${src}"]`);
    if (existing) {
      if (existing.dataset.loaded === 'true') resolve();
      else existing.addEventListener('load', () => resolve());
      return;
    }
    const script = document.createElement('script');
    script.src = src;
    script.dataset.flowops = src;
    script.onload = () => {
      script.dataset.loaded = 'true';
      resolve();
    };
    script.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(script);
  });
}
