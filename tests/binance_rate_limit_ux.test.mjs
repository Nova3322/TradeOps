import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const shared = fs.readFileSync(
  new URL("../src/trading_control_plane/web/shared.js", import.meta.url),
  "utf8",
);
const accounts = fs.readFileSync(
  new URL("../src/trading_control_plane/web/accounts.js", import.meta.url),
  "utf8",
);
const apiClient = fs.readFileSync(
  new URL("../src/trading_control_plane/web/api-client.js", import.meta.url),
  "utf8",
);
const capital = fs.readFileSync(
  new URL("../src/trading_control_plane/web/capital.js", import.meta.url),
  "utf8",
);

test("Binance connection diagnostics distinguish rate limit causes and retry time", () => {
  for (const marker of [
    "ORDINARY_RATE_LIMIT:'普通请求限流'",
    "REQUEST_WEIGHT_EXCEEDED:'请求权重超限'",
    "IP_TEMPORARILY_BANNED:'IP 临时封禁'",
    "BINANCE_CONNECTION_WEIGHT_HEADROOM_DEFERRED",
    "BINANCE_CAPITAL_WEIGHT_HEADROOM_DEFERRED",
    "BINANCE_RATE_LIMITED_COOLDOWN:'币安当前进程仍在临时冷却，本次未向币安发送请求'",
    "BINANCE_CONNECTION_RETRY_DEFERRED:'币安当前进程仍在临时冷却，本次未向币安发送请求'",
    "fmtBinanceConnectionDiagnostic",
  ]) {
    assert.equal(shared.includes(marker), true, marker);
  }
  for (const marker of [
    "HTTP Status",
    "Binance error code",
    "Binance message",
    "Retry-After",
    "限流 / weight headers",
    "到期前不会重复请求 Binance",
  ]) {
    assert.equal(accounts.includes(marker), true, marker);
  }
  assert.equal(apiClient.includes("error.details = data?.error?.details"), true);
});

test("Binance receipt polling is progressive, resumable, and never loops submission", () => {
  for (const marker of [
    "delaysMs:[3000, 5000, 10000, 20000, 30000]",
    "reconcilePendingBinanceReceipts",
    "withBinanceReceiptBrowserSingleflight",
    "BINANCE_RECEIPT_CHECK_IN_PROGRESS",
    "error?.details?.retry_after_seconds",
    "error?.details?.next_retry_at",
  ]) {
    assert.equal(capital.includes(marker), true, marker);
  }
  assert.equal(capital.includes("{attempts:120, delayMs:3000}"), false);
  const submitStart = capital.indexOf("async function prepareAndSubmitBinanceWithdrawal");
  const submitEnd = capital.indexOf("const PUBLIC_RECEIPT_PENDING_CODES", submitStart);
  assert.equal(capital.slice(submitStart, submitEnd).includes("retryDirectPublicReceipt"), false);
});

test("Binance receipt browser lock merges simultaneous tabs before either calls the API", async () => {
  const start = capital.indexOf("async function withBinanceReceiptBrowserSingleflight");
  const end = capital.indexOf("\n\nfunction directCapitalCurrentPhase", start);
  const helper = capital.slice(start, end);
  let locked = false;
  const navigator = {
    locks:{
      async request(_name, options, callback) {
        assert.equal(options.mode, "exclusive");
        assert.equal(options.ifAvailable, true);
        if (locked) return callback(null);
        locked = true;
        try { return await callback({name:"fixture-lock"}); }
        finally { locked = false; }
      },
    },
  };
  const singleflight = vm.runInNewContext(
    `${helper}; withBinanceReceiptBrowserSingleflight`,
    {navigator, globalThis:{navigator}},
  );
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let apiCalls = 0;
  const first = singleflight("operation:stage", async () => { apiCalls += 1; await held; return {ok:true}; });
  const second = singleflight("operation:stage", async () => { apiCalls += 1; return {ok:true}; });

  const merged = await second;
  assert.equal(merged.pending, true);
  assert.equal(merged.active_reconciliation, true);
  assert.equal(apiCalls, 1);
  release();
  assert.deepEqual(await first, {ok:true});
});

test("Binance receipt retry makes five reads in the first minute and honors server delay", async () => {
  const start = capital.indexOf("const PUBLIC_RECEIPT_PENDING_CODES");
  const end = capital.indexOf("\n\nasync function executeDirectWalletAction", start);
  const waits = [];
  const helper = capital.slice(start, end).replace(
    /const waitForPublicReceipt = .*?;\n/,
    "const waitForPublicReceipt = milliseconds => { waits.push(milliseconds); return Promise.resolve(); };\n",
  );
  const retry = vm.runInNewContext(`${helper}; retryDirectPublicReceipt`, {waits, Date, Math, Number, Promise, Set});
  let requests = 0;
  await retry(
    async () => {
      requests += 1;
      throw {code:"BINANCE_CAPITAL_WITHDRAWAL_PENDING", details:{}};
    },
    {attempts:6, delaysMs:[3000, 5000, 10000, 20000, 30000]},
  );
  assert.equal(requests, 6);
  assert.deepEqual(waits, [3000, 5000, 10000, 20000, 30000]);
  assert.equal([0, ...waits].reduce((total, delay) => total + delay, 0), 68_000);
  assert.equal([0, 3000, 8000, 18000, 38000, 68000].filter(at => at < 60_000).length, 5);

  waits.length = 0;
  await retry(
    async () => { throw {code:"BINANCE_CAPITAL_RATE_LIMITED", details:{retry_after_seconds:120}}; },
    {attempts:2, delaysMs:[3000]},
  );
  assert.deepEqual(waits, [120_000]);
});
