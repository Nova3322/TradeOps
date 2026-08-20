import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const webRoot = new URL('../src/trading_control_plane/web/', import.meta.url);
const indexSource = readFileSync(new URL('index.html', webRoot), 'utf8');
const shellSource = readFileSync(new URL('app-shell.js', webRoot), 'utf8');
const proposalSource = readFileSync(new URL('proposals.js', webRoot), 'utf8');
const sharedSource = readFileSync(new URL('shared.js', webRoot), 'utf8');

test('production approval requests current-password step-up instead of the mock-only route', () => {
  assert.match(
    indexSource,
    /id="confirm-password"[^>]+type="password"[^>]+autocomplete="current-password"/,
  );
  assert.match(shellSource, /function passwordStepUpRequired\(\)/);
  assert.match(shellSource, /'\/api\/auth\/step-up'/);
  assert.match(shellSource, /'\/api\/auth\/mock\/step-up'/);
  assert.match(proposalSource, /confirmStepUpAction\(\{/);
  assert.doesNotMatch(proposalSource, /api\('\/api\/auth\/mock\/step-up'/);
  assert.match(sharedSource, /STEP_UP_PASSWORD_INVALID:/);
  assert.match(sharedSource, /STEP_UP_RATE_LIMITED:/);
});

test('proposal approval is the final normal click before automatic execution', () => {
  assert.match(proposalSource, /达到所需审批票数并通过实时风控后/);
  assert.match(proposalSource, /自动签发授权、预留风险并由 Freqtrade 发送/);
  assert.match(proposalSource, /批准是最后一个常规人工节点/);
  assert.match(proposalSource, /相关事实、风险容量或政策发生变化后，系统会自动重新检查/);
  assert.doesNotMatch(proposalSource, /<button[^>]+data-risk/);
  assert.doesNotMatch(proposalSource, /async function runRisk/);
  assert.doesNotMatch(proposalSource, /data-authorize/);
  assert.doesNotMatch(proposalSource, /data-initial/);
  assert.doesNotMatch(proposalSource, /async function authorize/);
  assert.doesNotMatch(proposalSource, /async function createInitialIntent/);
});
