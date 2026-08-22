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

test("campaign history renders separate instrument and direction columns with an accessible compact id link", async () => {
  const context = {
    api:async () => ({data:[{
      campaign_id:"de7d9c51-cf96-40f7-a33f-2e96add6a8a2",
      environment:"LIVE",
      symbol:"BTC",
      account_id:"acct-1",
      venue:"HYPERLIQUID",
      direction:"LONG",
      status:"CLOSED",
      current_target_quantity:0,
      final_pnl:"1.25",
      updated_at:"2026-08-22T12:00:00Z",
    }]}),
    currentWorkflowEnvironment:() => "LIVE",
    currentLanguage:"zh-CN",
    localizedText:(value) => value,
    fmtCompactEnvironment:() => "生产",
    fmtEnvironment:() => "生产模式 · 实盘",
    fmtDirection:() => "做多",
    fmtDefaultAccountLabel:() => "默认账户",
    fmtVenueLabel:() => "Hyperliquid",
    campaignTargetLabel:() => "已平仓",
    campaignPnlLabel:() => "1.25 USDC",
    fmtStatus:() => "已结束",
    fmtDate:() => "8月22日 20:00",
    shortId:(value) => `${value.slice(0, 8)}…`,
    escapeHtml:(value) => String(value),
    recordPaginationMarkup:() => "",
    bindLinkedRows:() => {},
    bindRecordList:() => {},
    main:{innerHTML:""},
    window:{matchMedia:() => ({matches:true})},
  };
  vm.runInNewContext(`${execution}; renderPromise = renderCampaignList();`, context);
  await context.renderPromise;
  const html = context.main.innerHTML;

  assert.match(html, /<th>标的<\/th><th>方向<\/th>/);
  assert.match(html, /class="campaign-instrument-cell" data-label="标的"/);
  assert.match(html, /class="campaign-direction-cell" data-label="方向"/);
  assert.match(html, /class="row-link campaign-id-link"[^>]+aria-label="查看 BTC 交易详情"/);
  assert.match(html, />de7d9c51…<\/a>/);
  assert.doesNotMatch(html, /标的 \/ 方向|查看详情/);
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
  assert.equal(serviceWorker.includes("trading-shell-v236"), true);
});
