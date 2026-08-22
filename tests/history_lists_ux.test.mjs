import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = (path) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
const index = read("../src/trading_control_plane/web/index.html");
const shared = read("../src/trading_control_plane/web/shared.js");
const proposals = read("../src/trading_control_plane/web/proposals.js");
const execution = read("../src/trading_control_plane/web/execution.js");
const reporting = read("../src/trading_control_plane/web/reporting.js");
const serviceWorker = read("../src/trading_control_plane/web/sw.js");

test("record pagination defaults to 50 rows and caps the selectable size at 100", () => {
  const helperSource = shared.slice(
    shared.indexOf("const normalizeRecordPageSize"),
    shared.indexOf("function recordPageSummary"),
  );
  const context = {};
  vm.runInNewContext(
    `${helperSource}; result = {
      first: recordPage(Array.from({length: 125}, (_, index) => index), 1),
      third: recordPage(Array.from({length: 125}, (_, index) => index), 3),
      hundred: recordPage(Array.from({length: 125}, (_, index) => index), 2, 100),
      invalid: normalizeRecordPageSize(200),
    };`,
    context,
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.result.first)),
    {items:Array.from({length: 50}, (_, index) => index), page:1, pageSize:50, total:125, totalPages:3},
  );
  assert.equal(context.result.third.items.length, 25);
  assert.equal(context.result.third.page, 3);
  assert.equal(context.result.hundred.items.length, 25);
  assert.equal(context.result.hundred.pageSize, 100);
  assert.equal(context.result.invalid, 50);
  assert.match(shared, /<option value="50" selected>50<\/option><option value="100">100<\/option>/);
  assert.doesNotMatch(shared, /<option value="200"/);
});

test("the review queue is newest-first and uses filtered page navigation instead of load-more", () => {
  assert.match(proposals, /new Date\(right\.created_at\) - new Date\(left\.created_at\)/);
  for (const marker of [
    "recordPaginationMarkup(items.length, '提案记录分页')",
    "bindRecordList({",
    "data-proposal-row",
    "#proposal-search",
    "#proposal-direction",
    "#proposal-risk",
  ]) assert.equal(proposals.includes(marker), true, marker);
  assert.equal(proposals.includes("data-load-more-proposals"), false);
});

test("the campaigns destination is trade history with filters and 50-or-100 pagination", () => {
  assert.match(index, /href="\/campaigns"[^>]*>交易历史<\/a>/);
  for (const marker of [
    "<h1>交易历史</h1>",
    "筛选交易历史",
    'id="campaign-search"',
    'id="campaign-direction"',
    'id="campaign-venue"',
    'id="campaign-status"',
    "recordPaginationMarkup(items.length, '交易历史分页')",
    "data-campaign-row",
  ]) assert.equal(execution.includes(marker), true, marker);
});

test("recent notification deliveries support bounded history filters and pagination", () => {
  assert.equal(reporting.includes("api('/api/notifications?limit=200')"), true);
  for (const marker of [
    "筛选投递记录",
    'id="notification-search"',
    'id="notification-channel"',
    'id="notification-status"',
    'id="notification-environment"',
    "recordPaginationMarkup(deliveries.length, '通知投递记录分页')",
    "data-notification-row",
  ]) assert.equal(reporting.includes(marker), true, marker);
  assert.equal(serviceWorker.includes("trading-shell-v235"), true);
});
