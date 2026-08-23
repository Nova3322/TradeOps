import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

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
  assert.match(indexSource, /execution\.js\?v=200/);
  assert.match(indexSource, /shared\.js\?v=21/);
  assert.match(indexSource, /accounts\.js\?v=185/);
  assert.match(serviceWorkerSource, /trading-shell-v240/);
});

test('account history uses three independently filtered and paginated record lists', () => {
  for (const marker of [
    'data-venue-record-list="orders"',
    "{kind:'fills', controls:fillControls}",
    "{kind:'funding', controls:fundingControls}",
    'data-venue-record-list="${escapeHtml(recordList.kind)}"',
    "rootSelector,",
    "filterSelectors:['[data-venue-record-search]','[data-venue-record-filter]']",
    "paginationLabel:'最近委托分页'",
    "paginationLabel:'成交历史分页'",
    "paginationLabel:'资金费分页'",
  ]) assert.equal(accountsSource.includes(marker), true, marker);
  assert.match(accountsSource, /snapshotMode \? '最后快照中的委托记录' : '最近委托'/);
  assert.doesNotMatch(accountsSource, /snapshotMode \? '最后快照中的订单记录' : '最近订单记录'/);
});

test('configured API credentials use the compact update label without changing credential fields', () => {
  assert.match(accountsSource, /\['BINANCE','OKX','BYBIT'\]\.includes\(venue\) \? '更新 apikey' : '更新凭据'/);
  assert.match(executionSource, /\['BINANCE','OKX','BYBIT'\]\.includes\(item\.venue\) \? '更新 apikey' : '更新凭据'/);
  assert.match(accountsSource, /credentials\.state === 'CONFIGURED' \? exchangeCredentialUpdateLabel\(item\.venue\) : '添加加密凭据'/);
});

test('connection verification reports the unchanged current trading eligibility', () => {
  const helperSource = sharedSource.slice(
    sharedSource.indexOf('function fmtConnectionVerificationSuccess'),
    sharedSource.indexOf('const fmtRisk'),
  );
  const context = {};
  vm.runInNewContext(
    `${helperSource}; result = {
      enabled: fmtConnectionVerificationSuccess({trading:{status:'ELIGIBLE',enabled:true}}),
      disabled: fmtConnectionVerificationSuccess({trading:{status:'DISABLED',enabled:false}}),
    };`,
    context,
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.result)),
    {
      enabled:'连接测试成功；交易资格未改变，当前已开启',
      disabled:'连接测试成功；交易资格未改变，当前保持关闭',
    },
  );
  assert.match(accountsSource, /fmtConnectionVerificationSuccess\(result\)/);
  assert.match(executionSource, /fmtConnectionVerificationSuccess\(result\)/);
  assert.doesNotMatch(accountsSource, /连接验证成功；交易能力仍保持关闭/);
  assert.doesNotMatch(executionSource, /连接测试成功；交易能力仍保持关闭/);
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
