import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const executionSource = readFileSync(
  new URL('../src/trading_control_plane/web/execution.js', import.meta.url),
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
  assert.match(indexSource, /execution\.js\?v=192/);
  assert.match(serviceWorkerSource, /trading-shell-v222/);
});
