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

test('proposal approval delegates the initial risk check to the server', () => {
  assert.match(proposalSource, /达到所需审批票数后会自动运行风控/);
  assert.match(
    proposalSource,
    /const canRunRisk =[^;]+\(riskDenied \|\| needsFreshRisk\)/,
  );
  assert.doesNotMatch(proposalSource, /canRunRisk =[^;]+!riskDone/);
});
