import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const executionSource = readFileSync(
  new URL('../src/trading_control_plane/web/execution.js', import.meta.url),
  'utf8',
);
const accountsSource = readFileSync(
  new URL('../src/trading_control_plane/web/accounts.js', import.meta.url),
  'utf8',
);
const sharedSource = readFileSync(
  new URL('../src/trading_control_plane/web/shared.js', import.meta.url),
  'utf8',
);
const routerSource = readFileSync(
  new URL('../src/trading_control_plane/web/router.js', import.meta.url),
  'utf8',
);
const indexSource = readFileSync(
  new URL('../src/trading_control_plane/web/index.html', import.meta.url),
  'utf8',
);
const serviceWorkerSource = readFileSync(
  new URL('../src/trading_control_plane/web/sw.js', import.meta.url),
  'utf8',
);

test('production account cards expose the exact-account detail workflow', () => {
  assert.match(
    executionSource,
    /href="\/venues\/\$\{encodeURIComponent\(item\.exchange_account_id\)\}" data-link>查看详情<\/a>/,
  );
  assert.match(routerSource, /venueAccountMatch = path\.match\(\/\^\\\/venues\\\/\(\[\^\/\]\+\)\$\/\)/);
  assert.match(routerSource, /renderVenueAccountDetail\(venueAccountMatch\[1\]\)/);
  assert.match(indexSource, /execution\.js\?v=195/);
  assert.match(indexSource, /shared\.js\?v=17/);
  assert.match(indexSource, /accounts\.js\?v=183/);
  assert.match(serviceWorkerSource, /trading-shell-v231/);
});

test('standard Binance account mode is rendered as known product truth', () => {
  assert.match(sharedSource, /STANDARD:'标准账户'/);
  assert.match(accountsSource, /STANDARD:'Standard account'/);
  assert.match(accountsSource, /account_mode:account\.account_mode \|\| 'STANDARD'/);
});

test('account detail trusts fresh exact-account facts instead of API-local worker flags', () => {
  assert.match(accountsSource, /health\?\.data\?\.data_status === 'CURRENT'/);
  assert.match(accountsSource, /external_boundaries\?\.fact_adapter\?\.reconciliation_seconds/);
  assert.doesNotMatch(accountsSource, /processRuntimeEnabled/);
});

test('Bybit testnet onboarding supports a pinned worker while OKX stays fact-only', () => {
  assert.match(executionSource, /<option value="\$\{venue\}">/);
  assert.match(executionSource, /venue === 'OKX' \? ' · 仅事实同步'/);
  assert.match(executionSource, /item\.venue === 'OKX'/);
  assert.match(executionSource, /该交易所测试环境仅支持事实同步；执行保持不可用/);
  assert.doesNotMatch(executionSource, /value="\$\{venue\}" \$\{selectedEnvironment.*\? 'disabled'/);
});
