import test from 'node:test';
import assert from 'node:assert/strict';
import { MeterworkClient } from '../web/sdk.js';

const response = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

test('default fetch keeps its browser-global receiver', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = function () { assert.equal(this, globalThis); return Promise.resolve(response({ ok: true })); };
  try { assert.deepEqual(await new MeterworkClient(null).request('/api/demo'), { ok: true }); }
  finally { globalThis.fetch = original; }
});

test('retry reuses the same event payload; usage replay never creates a new ID', async () => {
  const requests = [];
  let usageCalls = 0;
  const client = new MeterworkClient('test-token', { actorId: 'a', fetcher: async (path, options) => {
    requests.push({ path, ...options, body: JSON.parse(options.body || '{}') });
    if (path === '/api/usage' && usageCalls++ === 0) return response({ detail: 'temporary' }, 503);
    return response({ status: 'accepted' });
  }});
  await client.sendUsage(10);
  await client.sendUsage(10, true);
  const usage = requests.filter(r => r.path === '/api/usage');
  assert.equal(usage.length, 3);
  assert.deepEqual(usage[0].body, usage[1].body);
  assert.deepEqual(usage[1].body, usage[2].body);
  assert.equal(usage[0].headers.Authorization, 'Bearer test-token');
});

test('assignment and exposure are distinct; failed exposure can be retried', async () => {
  const requests = [];
  let failExposure = true;
  const client = new MeterworkClient('t', { actorId: 'a', fetcher: async (path, options) => {
    const body = JSON.parse(options.body || '{}'); requests.push({ path, body });
    if (path === '/api/assignment') return response({ variant: 'control', experiment_id: 'onboarding-v1' });
    if (body.event_name === 'exposure' && failExposure) return response({ detail: 'invalid' }, 422);
    return response({ status: 'accepted' });
  }});
  await client.startOnboarding();
  assert.equal(requests.some(r => r.body.event_name === 'exposure'), false);
  await assert.rejects(client.expose(), /invalid/);
  assert.equal(client.exposed, false);
  failExposure = false;
  await client.expose(); await client.expose();
  assert.equal(client.exposed, true);
  assert.equal(requests.filter(r => r.body.event_name === 'exposure').length, 2);
  assert.equal(requests.filter(r => r.path === '/api/assignment').length, 1);
});

test('accepted usage survives a separate analytics failure and returns warning', async () => {
  const client = new MeterworkClient('t', { actorId: 'a', fetcher: async path =>
    path === '/api/usage' ? response({ status: 'accepted' }) : response({ detail: 'bad event' }, 422) });
  const result = await client.sendUsage(3);
  assert.equal(result.result.status, 'accepted');
  assert.equal(result.warnings.length, 2);
  assert.equal(client.lastUsage.units, 3);
  assert.equal(client.activated, false);
});

test('client errors are not retried and malformed units never reach the API', async () => {
  let requests = 0;
  const client = new MeterworkClient('t', { fetcher: async () => { requests += 1; return response({ detail: 'unauthorized' }, 401); } });
  await assert.rejects(client.request('/api/usage', { method: 'POST', body: { id: 'e' }, retry: true }), /unauthorized/);
  assert.equal(requests, 1);
  await assert.rejects(client.sendUsage(1.5), /whole number/);
  assert.equal(requests, 1);
});

test('bounded retry stops after three retryable failures', async () => {
  let requests = 0;
  const client = new MeterworkClient('t', { fetcher: async () => { requests += 1; return response({ detail: 'busy' }, 503); } });
  await assert.rejects(client.capture('api_call'), /busy/);
  assert.equal(requests, 3);
});
