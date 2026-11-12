/** Minimal event client. Tokens and actor identity live only in page memory. */
export class MeterworkClient {
  constructor(token, { fetcher = fetch.bind(globalThis), actorId = crypto.randomUUID() } = {}) {
    this.token = token;
    this.fetcher = fetcher;
    this.actorId = actorId;
    this.assignment = null;
    this.exposed = false;
    this.signedUp = false;
    this.activated = false;
    this.lastUsage = null;
  }

  async request(path, { method = 'GET', body, retry = false } = {}) {
    let lastError;
    const attempts = retry ? 3 : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      let response;
      try {
        response = await this.fetcher(path, {
          method,
          headers: { ...(body ? { 'Content-Type': 'application/json' } : {}),
            ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}) },
          body: body ? JSON.stringify(body) : undefined,
          signal: AbortSignal.timeout(15000),
        });
      } catch (error) {
        lastError = error;
        if (attempt + 1 < attempts) {
          await new Promise(resolve => setTimeout(resolve, 250 * (2 ** attempt)));
          continue;
        }
        throw error;
      }
      const payload = await response.json().catch(() => ({}));
      if (response.ok) return payload;
      const detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail || payload.error || `HTTP ${response.status}`);
      lastError = new Error(detail);
      lastError.status = response.status;
      if (!retry || !(response.status === 429 || response.status >= 500) || attempt + 1 === attempts) throw lastError;
      const retryAfter = Number(response.headers.get('Retry-After'));
      const delay = Number.isFinite(retryAfter) && retryAfter > 0 ? Math.min(retryAfter * 1000, 3000) : 250 * (2 ** attempt);
      await new Promise(resolve => setTimeout(resolve, delay));
    }
    throw lastError;
  }

  async capture(eventName, extra = {}) {
    const body = { event_id: crypto.randomUUID(), actor_id: this.actorId,
      event_name: eventName, occurred_at: new Date().toISOString(), properties: { surface: 'meterwork-demo' }, ...extra };
    return this.request('/api/events', { method: 'POST', body, retry: true });
  }

  async startOnboarding() {
    if (this.assignment) return this.assignment;
    if (!this.signedUp) { await this.capture('signup'); this.signedUp = true; }
    this.assignment = await this.request('/api/assignment', { method: 'POST',
      body: { actor_id: this.actorId, experiment_id: 'onboarding-v1' } });
    return this.assignment;
  }

  async expose() {
    if (this.exposed) return;
    if (!this.assignment) throw new Error('Assign a variant before recording exposure.');
    await this.capture('exposure', { experiment_id: this.assignment.experiment_id,
      variant: this.assignment.variant });
    this.exposed = true;
  }

  async sendUsage(units, replay = false) {
    if (!Number.isSafeInteger(units) || units < 1 || units > 1000) throw new Error('Choose a whole number from 1 to 1,000 units.');
    if (replay && !this.lastUsage) throw new Error('Send an event before replaying it.');
    const body = replay ? this.lastUsage : { id: crypto.randomUUID(), source: 'meterwork-browser', occurred_at: new Date().toISOString(), units };
    let result;
    try {
      result = await this.request('/api/usage', { method: 'POST', body, retry: true });
    } catch (error) {
      await this.capture('api_error', { properties: { surface: 'meterwork-demo', operation: 'usage', status: error.status || 'network' } }).catch(() => {});
      throw error;
    }
    this.lastUsage = body;
    // Analytics is a separate write: its failure cannot undo or conceal accepted usage.
    const warnings = [];
    if (!this.activated) {
      try { await this.capture('activation'); this.activated = true; }
      catch (error) { warnings.push(`Activation instrumentation failed: ${error.message}`); }
    }
    try { await this.capture('api_call'); }
    catch (error) { warnings.push(`Call instrumentation failed: ${error.message}`); }
    return { result, event: body, warnings };
  }
}
