const money = n => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 6 }).format(n / 1e6);
const count = n => new Intl.NumberFormat('en-US').format(n);

export function validateEvidence(report) {
  if (report?.schema_version !== 1 || !Array.isArray(report.accounts) || !report.accounts.length) throw new Error('Unrecognized workload evidence.');
  if (!report.checks?.length || report.checks.some(check => check.passed !== true)) throw new Error('The workload contains missing or failed checks.');
  if (report.parameters.accounts !== report.accounts.length) throw new Error('Account count does not reconcile.');
  const sum = key => report.accounts.reduce((total, row) => total + row[key], 0);
  for (const row of report.accounts) {
    for (const field of ['accepted_events', 'original_microusd', 'corrected_microusd', 'repriced_microusd', 'final_units', 'revisions', 'reconciliation_difference_microusd']) {
      if (!Number.isSafeInteger(row[field])) throw new Error('Evidence values must be safe integers.');
    }
    if (row.reconciliation_difference_microusd !== 0 || row.revisions !== 3) throw new Error('A statement has not reconciled.');
  }
  if (sum('accepted_events') !== report.totals.accepted_events || sum('revisions') !== report.totals.statement_revisions || report.totals.reconciliation_difference_microusd !== 0) throw new Error('Workload totals do not reconcile.');
  return report;
}

export function accountStages(row) {
  return [
    { label: 'Original statement', amount: row.original_microusd, detail: 'Usage known at the first publication' },
    { label: 'Late usage + corrections', amount: row.corrected_microusd, detail: `Change: ${money(row.corrected_microusd - row.original_microusd)}` },
    { label: 'Backdated price received', amount: row.repriced_microusd, detail: `Change: ${money(row.repriced_microusd - row.corrected_microusd)}` },
  ];
}

async function mount() {
  const host = document.getElementById('landmark-output');
  if (!host) return;
  const element = (tag, text, className) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (className) node.className = className; return node; };
  try {
    const response = await fetch('./landmark.json');
    if (!response.ok) throw new Error(`Evidence download failed (${response.status}).`);
    const report = validateEvidence(await response.json());
    const stats = element('div', null, 'metrics');
    for (const [label, value, detail] of [
      ['ACCEPTED EVENTS', count(report.totals.accepted_events), `${report.parameters.accounts} isolated synthetic accounts`],
      ['RETRIES DEDUPLICATED', count(report.totals.replays_deduplicated), 'Committed usage did not increase'],
      ['STATEMENT REVISIONS', count(report.totals.statement_revisions), 'Original + corrected + repriced'],
      ['RECONCILIATION GAP', money(report.totals.reconciliation_difference_microusd), 'Independent SQL + fixture arithmetic'],
    ]) {
      const card = element('article', null, 'metric'); card.append(element('span', label, 'small-label'), element('strong', value), element('span', detail)); stats.append(card);
    }
    const picker = element('select'); picker.id = 'account-picker'; picker.setAttribute('aria-label', 'Inspect a synthetic account');
    report.accounts.forEach(row => { const option = element('option', row.account); option.value = row.account; picker.append(option); });
    const label = element('label', 'Follow one account through three publications', 'field-label'); label.htmlFor = picker.id;
    const stages = element('div', null, 'revision-stages'); stages.setAttribute('aria-live', 'polite');
    const render = () => {
      stages.replaceChildren();
      const selected = report.accounts.find(row => row.account === picker.value);
      accountStages(selected).forEach((stage, i) => { const card = element('article', null, 'revision-stage'); card.append(element('span', `REVISION ${i + 1}`, 'small-label'), element('h3', stage.label), element('strong', money(stage.amount)), element('p', stage.detail, 'caption')); stages.append(card); });
    };
    picker.addEventListener('change', render);
    const context = element('p', `Executed ${report.recorded_at.replace('T', ' ').slice(0, 19)} UTC. ${report.scope}. Every historical revision was checked again after the new price arrived.`, 'caption');
    host.replaceChildren(stats, label, picker, stages, context); render();
  } catch (error) { host.replaceChildren(element('p', `Evidence unavailable: ${error.message} No passing result is claimed.`, 'notice error')); }
}

if (typeof document !== 'undefined') mount();
