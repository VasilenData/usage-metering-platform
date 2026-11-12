import test from 'node:test';
import assert from 'node:assert/strict';
import { validateEvidence, accountStages } from '../web/landmark.js';

const fixture = () => ({ schema_version: 1, parameters: { accounts: 1 }, totals: { accepted_events: 490, statement_revisions: 3, reconciliation_difference_microusd: 0 }, checks: [{ passed: true }], accounts: [{ account: 'a', accepted_events: 490, original_microusd: 10000, corrected_microusd: 12000, repriced_microusd: 11000, final_units: 55, revisions: 3, reconciliation_difference_microusd: 0 }] });
test('reconciles the three actual revisions', () => { assert.equal(validateEvidence(fixture()).accounts.length, 1); assert.deepEqual(accountStages(fixture().accounts[0]).map(row => row.amount), [10000, 12000, 11000]); });
test('blocks a failed or missing check', () => { for (const checks of [[], [{ passed: false }], [{ passed: 'true' }]]) assert.throws(() => validateEvidence({ ...fixture(), checks })); });
test('blocks corrupted aggregate or account differences', () => { const data = fixture(); data.totals.accepted_events++; assert.throws(() => validateEvidence(data)); const other = fixture(); other.accounts[0].reconciliation_difference_microusd = 1; assert.throws(() => validateEvidence(other)); });
test('blocks unsafe amounts and missing revisions', () => { for (const [field, value] of [['repriced_microusd', Number.MAX_SAFE_INTEGER + 1], ['revisions', 2]]) { const data = fixture(); data.accounts[0][field] = value; assert.throws(() => validateEvidence(data)); } });
