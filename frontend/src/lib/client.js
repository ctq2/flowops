/**
 * HTTP client with typed problems and a small retry policy.
 *
 * Retries are limited to idempotent verbs and to transport/5xx failures: retrying
 * a POST that may already have been applied is how duplicate tickets are born.
 * Callers that need that safety pass an `Idempotency-Key` instead.
 */

export class ApiError extends Error {
  constructor(status, problem, cause) {
    const detail = problem?.detail || problem?.title || `HTTP ${status}`;
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem ?? null;
    this.cause = cause;
  }

  get type() {
    return this.problem?.type ?? 'about:blank';
  }

  get isConflict() {
    return this.status === 409 || this.status === 412;
  }

  get isValidation() {
    return this.status === 422;
  }
}

const RETRYABLE_VERBS = new Set(['GET', 'HEAD']);

export function createClient({ baseUrl = '/api/v1', timeoutMs = 8000, retries = 2, fetchImpl } = {}) {
  const doFetch = fetchImpl ?? (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
  if (!doFetch) throw new Error('no fetch implementation available');

  async function request(path, { method = 'GET', body, headers = {}, retries: attemptLimit } = {}) {
    const allowed = attemptLimit ?? (RETRYABLE_VERBS.has(method.toUpperCase()) ? retries : 0);
    let lastError;
    for (let attempt = 0; attempt <= allowed; attempt += 1) {
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
      try {
        const response = await doFetch(`${baseUrl}${path}`, {
          method,
          headers: {
            Accept: 'application/json',
            ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
            ...headers,
          },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: controller?.signal,
        });
        const text = await response.text();
        const payload = text ? safeParse(text) : null;
        if (!response.ok) {
          throw new ApiError(response.status, payload, null);
        }
        return { data: payload, headers: response.headers, status: response.status };
      } catch (error) {
        lastError = error;
        const retryable =
          !(error instanceof ApiError) ||
          error.status >= 500 ||
          error.status === 429;
        if (!retryable || attempt === allowed) throw error;
        await delay(120 * 2 ** attempt);
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
    throw lastError;
  }

  return {
    baseUrl,
    get: (path) => request(path),
    post: (path, body, headers) => request(path, { method: 'POST', body, headers }),
    patch: (path, body, headers) => request(path, { method: 'PATCH', body, headers }),
    request,
  };
}

function safeParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return { title: 'Malformed response', detail: text.slice(0, 400) };
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function isApiError(error) {
  return error instanceof ApiError;
}

/** A stable, human-readable fingerprint for an idempotency key. */
export function fingerprint(payload) {
  const text = JSON.stringify(payload, Object.keys(payload ?? {}).sort());
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

export function newIdempotencyKey(prefix = 'web') {
  const random = Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${random}`;
}
