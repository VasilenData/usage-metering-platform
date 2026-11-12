import { MeterworkClient } from './sdk.js?v=1';

const $ = id => document.getElementById(id);
const snapshotMode = location.hostname.endsWith('.github.io') || new URLSearchParams(location.search).get('mode') === 'snapshot';
const number = value => value == null ? '—' : new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value);
const percentage = value => value == null ? '—' : `${number(value * 100)}%`;
const money = value => value == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(value / 1000000);
const readableDate = value => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '—' : date.toISOString().replace('T', ' ').slice(0, 19); };
const text = (id, value) => { $(id).textContent = value; };
const node = (tag, value, className) => { const result = document.createElement(tag); if (value != null) result.textContent = value; if (className) result.className = className; return result; };
let client = new MeterworkClient(null);
let overview = null;
let lastChecks = [];
let busy = false;
let toastTimer;

function toast(message) {
  text('toast', message); $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5500);
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll('button').forEach(button => { button.disabled = value; });
  if (!value) {
    $('send-usage').disabled = snapshotMode || !client.token;
    $('replay-usage').disabled = !client.lastUsage;
    $('issue-statement').disabled = snapshotMode || !client.token;
    $('export').disabled = !overview;
    if (snapshotMode) document.querySelectorAll('[data-scenario], #start-sandbox, #connect-token, #replay-usage').forEach(button => { button.disabled = true; });
  }
  $('main').setAttribute('aria-busy', String(value));
}

async function action(fn) {
  if (busy) return;
  setBusy(true);
  try { await fn(); }
  catch (error) { toast(error.message || 'The request failed.'); }
  finally { setBusy(false); }
}

function renderChart(points = []) {
  const container = $('usage-chart'); container.replaceChildren();
  if (!points.length) { container.append(node('p', 'No usage events in this workspace yet.', 'empty')); return; }
  const data = points.slice(-31);
  const width = 620, height = 235, left = 47, right = 12, top = 18, bottom = 35;
  const innerW = width - left - right, innerH = height - top - bottom;
  const values = data.map(p => Number(p.units || 0));
  const maximum = Math.max(1, ...values), minimum = Math.min(0, ...values);
  const range = maximum - minimum;
  const y = value => top + ((maximum - value) / range) * innerH;
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('role', 'img');
  const title = document.createElementNS(ns, 'title'); title.textContent = `Usage by UTC day: ${data.map(p => `${p.date}: ${p.units} units`).join('; ')}`; svg.append(title);
  function shape(tag, attrs, content) { const element = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k,v]) => element.setAttribute(k, String(v))); if (content != null) element.textContent = content; svg.append(element); return element; }
  for (let i = 0; i < 4; i += 1) {
    const value = minimum + range * i / 3, position = y(value);
    shape('line', { x1: left, x2: width - right, y1: position, y2: position, stroke: '#304044', 'stroke-dasharray': '3 5' });
    shape('text', { x: left - 10, y: position + 4, fill: '#a7b8b7', 'font-size': 10, 'text-anchor': 'end' }, number(value));
  }
  const step = innerW / data.length, barWidth = Math.max(3, Math.min(43, step * 0.55));
  data.forEach((point, index) => {
    const value = Number(point.units || 0), x = left + step * index + (step - barWidth) / 2;
    const rect = shape('rect', { x, y: Math.min(y(value), y(0)), width: barWidth, height: Math.max(1, Math.abs(y(0) - y(value))), rx: 3, fill: value >= 0 ? '#8de5c5' : '#f5d28f' });
    const tip = document.createElementNS(ns, 'title'); tip.textContent = `${point.date}: ${number(value)} units`; rect.append(tip);
    if (data.length <= 7 || index === 0 || index === data.length - 1 || index % Math.ceil(data.length / 5) === 0) {
      shape('text', { x: x + barWidth / 2, y: height - 9, fill: '#a7b8b7', 'font-size': 10, 'text-anchor': 'middle' }, String(point.date).slice(5, 10));
    }
  });
  container.append(svg);
}

function emptyRow(target, columns, message) {
  const tr = node('tr'), td = node('td', message, 'empty'); td.colSpan = columns; tr.append(td); target.append(tr);
}

function renderStatements(rows = []) {
  const body = $('statements-body'); body.replaceChildren();
  if (!rows.length) { emptyRow(body, 5, client.token ? 'No statements yet. Choose a month and generate one.' : 'No statements in this snapshot. Start a sandbox to generate one.'); return; }
  rows.forEach(statement => {
    const tr = node('tr'), period = node('td');
    period.append(node('strong', statement.month || '—'), node('small', statement.id || '', 'mono'));
    const fingerprint = node('td');
    const hash = String(statement.source_sha256 || '');
    const code = node('code', hash ? `${hash.slice(0, 14)}…` : '—', 'mono'); code.title = hash; fingerprint.append(code);
    if (statement.pricing_version_ids?.length) fingerprint.append(node('small', `Pricing: ${statement.pricing_version_ids.join(', ')}`));
    tr.append(period, node('td', `v${statement.revision ?? 1}`), node('td', number(statement.total_units), 'numeric'), node('td', money(statement.amount_microusd), 'numeric'), fingerprint); body.append(tr);
  });
}

function renderEvents(rows = []) {
  const body = $('events-body'); body.replaceChildren();
  if (!rows.length) { emptyRow(body, 4, 'No usage events yet.'); return; }
  rows.slice(0, 12).forEach(event => {
    const tr = node('tr'), identity = node('td');
    identity.append(node('code', event.id || '—', 'mono'));
    if (event.correction_of) identity.append(node('small', `Corrects ${event.correction_of}`));
    const status = node('td'); status.append(node('span', String(event.status || 'accepted').toUpperCase(), 'tag neutral'));
    tr.append(identity, node('td', readableDate(event.occurred_at), 'mono'), node('td', number(event.units), 'numeric'), status); body.append(tr);
  });
}

function renderAnalytics(analytics = {}) {
  const funnel = $('funnel'); funnel.replaceChildren();
  const stages = analytics.funnel || [], maximum = Math.max(1, ...stages.map(s => s.actors));
  if (!stages.length) funnel.append(node('p', 'Product analytics is not available in this response.', 'caption'));
  stages.forEach(stage => { const row = node('div', null, 'funnel-row'), track = node('div', null, 'funnel-track'), bar = node('div', null, 'funnel-bar'); bar.style.width = `${Math.max(0, Math.min(100, stage.actors / maximum * 100))}%`; track.append(bar); row.append(node('span', stage.event.replaceAll('_', ' '), 'funnel-label'), track, node('span', number(stage.actors), 'funnel-count')); funnel.append(row); });
  text('sessions', number(analytics.sessions));
  const retention = analytics.day1_retention || {};
  text('retention', percentage(retention.rate));
  text('retention-detail', `${number(retention.retained_actors)} retained / ${number(retention.eligible_actors)} fully observed actors`);
  const definitions = $('metric-definitions'); definitions.replaceChildren();
  Object.entries(analytics.definitions || {}).forEach(([key, value]) => definitions.append(node('dt', key.replaceAll('_', ' ')), node('dd', value)));
  const experiment = analytics.experiment || {}, arms = $('experiment-arms'); arms.replaceChildren();
  ['control', 'treatment'].forEach(variant => {
    const data = experiment.arms?.[variant] || {}, arm = node('div', null, 'arm');
    arm.append(node('span', variant.toUpperCase(), 'small-label'), node('strong', percentage(data.conversion_rate)), node('p', `${number(data.converted_actors)} / ${number(data.mature_exposed_actors)} mature exposed`));
    arms.append(arm);
  });
  text('experiment-decision', experiment.decision === 'eligible_for_review' ? 'REVIEW REQUIRED' : 'DO NOT LAUNCH');
  text('experiment-lift', experiment.absolute_lift == null ? '—' : `${number(experiment.absolute_lift * 100)} pp`);
  const interval = experiment.absolute_lift_interval_95;
  text('experiment-interval', interval ? `Approximate 95% interval: ${number(interval[0] * 100)} to ${number(interval[1] * 100)} percentage points` : 'An interval requires mature observations in both variants.');
  const reasons = $('experiment-reasons'); reasons.replaceChildren();
  (experiment.reasons || ['No experiment report is available.']).forEach(reason => reasons.append(node('li', reason)));
  const protocol = $('experiment-protocol'), p = experiment.protocol || {}; protocol.replaceChildren();
  const fields = { 'Primary metric': p.primary_metric || '—', 'Experiment window': `${p.starts_at || '—'} → ${p.ends_at || '—'}`, 'Minimum mature actors': `${p.minimum_mature_per_variant ?? '—'} per variant`, 'Minimum absolute lift': percentage(p.minimum_absolute_lift), 'Assignment SRM p-value': experiment.assignment_srm_pvalue == null ? 'Unknown' : number(experiment.assignment_srm_pvalue), 'Protocol fingerprint': experiment.protocol_sha256 || '—', 'Analytics fingerprint': analytics.source_sha256 || '—' };
  Object.entries(fields).forEach(([key,value]) => protocol.append(node('dt', key), node('dd', value)));
}

function render(data) {
  overview = data;
  const metering = data.metering || {};
  text('data-state', snapshotMode ? 'Read-only snapshot · not live' : 'API snapshot loaded');
  text('snapshot-time', `${readableDate(data.generated_at || data.analytics?.as_of)} UTC`);
  text('tenant-name', data.tenant?.name || 'Demonstration workspace');
  text('tenant-id', data.tenant?.id || data.analytics?.tenant_id || '');
  text('accepted', number(metering.accepted_events)); text('units', number(metering.total_units));
  text('charges', money(metering.estimated_amount_microusd)); text('duplicates', number(metering.duplicate_events));
  renderChart(metering.daily_usage); renderStatements(data.statements); renderEvents(metering.recent_events); renderAnalytics(data.analytics);
  const pricing = $('pricing-versions'); pricing.replaceChildren();
  if (!metering.pricing_versions?.length) pricing.append(node('p', 'No pricing versions returned.', 'caption'));
  (metering.pricing_versions || []).forEach(version => pricing.append(node('p', `${version.id} · effective ${readableDate(version.effective_from)} UTC · ${money(version.unit_price_microusd)} / unit`, 'caption')));
  $('connection-error').hidden = true; $('export').disabled = false;
}

async function refresh() {
  try { render(await client.request(snapshotMode ? './demo.json' : client.token ? '/api/overview' : '/api/demo')); }
  catch (error) { text('data-state', overview ? 'Refresh failed · previous snapshot shown' : snapshotMode ? 'Snapshot unavailable' : 'API unavailable'); text('connection-error', `${error.message} ${overview ? 'The displayed snapshot has not been updated.' : 'No data is being substituted.'}`); $('connection-error').hidden = false; throw error; }
}

async function ensureSandbox() {
  if (client.token) return;
  const sandbox = await client.request('/api/sandbox', { method: 'POST' });
  client = new MeterworkClient(sandbox.token);
  text('sandbox-state', 'ACTIVE'); text('sandbox-detail', `Isolated demo workspace. Expires ${readableDate(sandbox.expires_at)} UTC. Your browser interactions are recorded as demo onboarding events.`);
}

async function renderOnboarding() {
  const assignment = await client.startOnboarding();
  const treatment = assignment.variant === 'treatment';
  text('send-usage', treatment ? 'Send + inspect receipt ↗' : 'Send usage ↗');
  text('onboarding-guidance', treatment ? 'Send an event, then replay its identifier. The usage total should stay the same. Inspect the ledger below to trace both requests.' : 'Choose the number of units and send an API request.');
  $('onboarding-guidance').hidden = false;
  // The assigned experience is now in the DOM before an exposure is recorded.
  await client.expose();
}

async function sendUsage(replay) {
  const result = $('request-result'); result.className = 'request-result';
  try {
    const units = Number($('demo-units').value);
    if (!Number.isSafeInteger(units) || units < 1 || units > 1000) throw new Error('Choose a whole number from 1 to 1,000 units.');
    await ensureSandbox();
    if (!client.exposed) await renderOnboarding();
    const response = await client.sendUsage(units, replay);
    result.className = 'request-result success';
    const status = response.result.status || (response.result.duplicate ? 'duplicate' : 'accepted');
    result.textContent = `${String(status).toUpperCase()} · ${response.event.id} · ${response.event.units} units${response.warnings.length ? ` · ${response.warnings.join(' ')}` : ''}`;
    await refresh();
  } catch (error) { result.className = 'request-result failure'; result.textContent = error.message; throw error; }
}

function showChecks(checks, scenario) {
  lastChecks = checks;
  const result = $('scenario-result'); result.replaceChildren(node('span', `${scenario.toUpperCase()} / EXECUTION RESULTS`, 'small-label'));
  const list = node('ul', null, 'checks');
  checks.forEach(check => { const li = node('li'), detail = node('div', check.name); detail.append(node('small', check.detail || '')); li.append(node('strong', check.passed ? 'PASS' : 'FAIL', check.passed ? 'check-pass' : 'check-fail'), detail); list.append(li); });
  if (!checks.length) result.append(node('p', 'The API returned no checks. This execution is not verified.', 'body-copy'));
  result.append(list);
}

function resetWorkspaceEvidence() {
  lastChecks = [];
  $('scenario-result').replaceChildren(node('span', 'CHECK OUTPUT', 'small-label'), node('p', 'Choose a scenario to inspect an actual execution in a fresh workspace.', 'body-copy'));
  $('onboarding-guidance').hidden = true;
  text('send-usage', 'Send usage ↗');
  text('request-result', 'No usage request sent in this workspace yet.');
  $('request-result').className = 'request-result';
}

$('refresh').addEventListener('click', () => action(refresh));
$('start-sandbox').addEventListener('click', () => action(async () => {
  client = new MeterworkClient(null); resetWorkspaceEvidence();
  await ensureSandbox(); await renderOnboarding(); await refresh();
  text('request-result', 'Sandbox ready. Send your first usage event when you are ready.');
  $('request-result').className = 'request-result';
  $('send-usage').focus();
}));
$('send-usage').addEventListener('click', () => action(() => sendUsage(false)));
$('replay-usage').addEventListener('click', () => action(() => sendUsage(true)));
$('connect-token').addEventListener('click', () => action(async () => {
  const token = $('api-token').value.trim();
  if (!token) throw new Error('Enter a workspace token.');
  const candidate = new MeterworkClient(token), data = await candidate.request('/api/overview');
  client = candidate; resetWorkspaceEvidence(); $('api-token').value = ''; render(data); text('sandbox-state', 'CONNECTED');
  text('sandbox-detail', 'Connected using a bearer token. Tenant identity is resolved by the server.'); toast('Workspace connected.');
}));
$('issue-statement').addEventListener('click', () => action(async () => {
  const month = $('statement-month').value; if (!/^\d{4}-\d{2}$/.test(month)) throw new Error('Choose a statement month.');
  await client.request('/api/statements', { method: 'POST', body: { month } }); await refresh(); toast('Statement generated. Inspect its revision and fingerprint below.');
}));
document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => action(async () => {
  const scenario = button.dataset.scenario;
  const result = await client.request('/api/demo/run', { method: 'POST', body: { scenario } });
  if (result.token) client = new MeterworkClient(result.token);
  resetWorkspaceEvidence();
  if (result.overview) render(result.overview);
  showChecks(result.checks || [], scenario); text('sandbox-state', 'SCENARIO');
  text('sandbox-detail', 'A fresh, isolated workspace contains this scenario. Send another event to continue exploring it.');
  $('scenario-result').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'nearest' });
})));
$('export').addEventListener('click', () => {
  if (!overview) return;
  const artifact = { exported_at: new Date().toISOString(), scope: 'Engineering demonstration; no commercial or causal claim.', overview, checks: lastChecks };
  const url = URL.createObjectURL(new Blob([JSON.stringify(artifact, null, 2)], { type: 'application/json' }));
  const a = node('a'); a.href = url; a.download = `meterwork-evidence-${new Date().toISOString().slice(0, 10)}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$('statement-month').value = new Date().toISOString().slice(0, 7);
if (snapshotMode) {
  const repository = 'https://github.com/VasilenData/usage-metering-platform';
  document.querySelector('.scope-note .tag').textContent = 'DATED SYNTHETIC SNAPSHOT';
  document.querySelector('.scope-note p').textContent = 'Read-only evidence. No live backend is connected. No customer traffic or money collected.';
  text('data-state', 'Loading the published snapshot');
  const apiLink = document.querySelector('.topbar > .text-link'); apiLink.href = `${repository}#run-locally`; apiLink.textContent = 'Run locally ↗';
  $('refresh').hidden = true;
  document.querySelector('.demo-panel').hidden = true;
  const panel = node('aside', null, 'panel snapshot-panel');
  panel.append(node('span', 'READ-ONLY PREVIEW', 'small-label'), node('h2', 'Inspect it here. Run it locally.'), node('p', 'These figures are exported from a real execution against synthetic fixtures. They are a dated snapshot, not a live service or a claim of customer adoption.', 'body-copy'));
  const link = node('a', 'Run the sandbox and failure exercises ↗', 'button primary'); link.href = `${repository}#run-locally`; link.target = '_blank'; link.rel = 'noopener'; panel.append(link);
  panel.append(node('p', 'The repository includes PostgreSQL row-level security, immutable statement revisions, bounded retries and a separate assignment/exposure event flow. The full console calls the real API when run locally.', 'caption'));
  document.querySelector('.console-grid').append(panel);
  $('issue-statement').closest('.toolbar').hidden = true;
  document.querySelector('#evidence .section-heading > p').textContent = 'The live application executes replay, late-data, pricing and tenant-isolation checks. This static preview does not simulate API calls.';
  document.querySelector('.scenario-grid').hidden = true;
  $('scenario-result').replaceChildren(node('span', 'EXECUTABLE EVIDENCE', 'small-label'), node('p', 'Review the integration tests and run the controlled exercises from the repository. No interactive check has been run from this read-only page.', 'body-copy'));
  const evidenceLink = node('a', 'Inspect the PostgreSQL acceptance tests ↗', 'text-link'); evidenceLink.href = `${repository}/blob/main/tests/test_postgres.py`; evidenceLink.target = '_blank'; evidenceLink.rel = 'noopener'; $('scenario-result').append(evidenceLink);
  document.querySelector('footer p').textContent = 'Read-only snapshot · synthetic data · estimated statements';
}
action(refresh);
