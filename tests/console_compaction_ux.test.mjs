import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = (path) => fs.readFileSync(new URL(path, import.meta.url), "utf8");
const index = read("../src/trading_control_plane/web/index.html");
const appShell = read("../src/trading_control_plane/web/app-shell.js");
const proposals = read("../src/trading_control_plane/web/proposals.js");
const workspace = read("../src/trading_control_plane/web/workspace.js");
const reporting = read("../src/trading_control_plane/web/reporting.js");
const i18n = read("../src/trading_control_plane/web/i18n.js");

test("the sidebar readiness label is driven by GET /health/ready and never starts as connected", async () => {
  const source = appShell.slice(
    appShell.indexOf("function setSidebarDatabaseStatus"),
    appShell.indexOf("function renderWorkspaceSwitcher"),
  );
  const status = {dataset:{}, textContent:""};
  const context = {
    sidebarDatabaseStatus:status,
    sidebarDatabaseRequestToken:0,
    localizedText:(value) => value,
    api:async () => ({status:"ready", durable_store:"postgresql"}),
  };
  vm.runInNewContext(`${source}; refresh = refreshSidebarDatabaseStatus;`, context);

  await context.refresh();
  assert.deepEqual(status, {dataset:{state:"connected"}, textContent:"数据库已连接"});

  context.api = async () => { throw {status:503, code:"DATABASE_UNAVAILABLE"}; };
  await context.refresh();
  assert.deepEqual(status, {dataset:{state:"disconnected"}, textContent:"数据库未连接"});

  context.api = async () => { throw {status:503, code:"SCHEMA_REVISION_MISMATCH"}; };
  await context.refresh();
  assert.deepEqual(status, {dataset:{state:"unknown"}, textContent:"数据库状态未知"});

  assert.match(index, /id="sidebar-database-status"[^>]+data-state="checking">数据库检查中<\/div>/);
  assert.doesNotMatch(index, />业务数据库已连接<|>数据缺失时自动阻止交易</);
});

test("compact review and normal-state copy removes repeated visible status text", () => {
  assert.doesNotMatch(proposals, /仍需你的独立判断|<b>已 \$\{Number\(item\.approval_count/);
  assert.match(proposals, /<b>\$\{Number\(item\.approval_count \|\| 0\)\} \/ /);
  assert.match(proposals, /fmtCompactEnvironment\(item\.environment\)/);
  assert.doesNotMatch(workspace, /当前无待办|无运行告警 \/ 无需处理/);
  assert.match(workspace, /暂无待办/);
  assert.match(workspace, /'无告警'/);
  assert.doesNotMatch(reporting, /无运行告警 \/ 当前无需处理/);
  assert.match(reporting, /<h2>无告警<\/h2>/);
});

test("home keeps its permission boundary and adds the localized Webhook opportunity entry", () => {
  const quickStart = workspace.slice(
    workspace.indexOf("const quickStart ="),
    workspace.indexOf("main.innerHTML =", workspace.indexOf("const quickStart =")),
  );
  assert.match(quickStart, /observerOnly \? '' :/);
  assert.match(
    quickStart,
    /<a class="secondary" href="\/webhook-signals" data-link>查看 Webhook 机会<\/a>/,
  );
  assert.match(quickStart, /canPropose \? '<a class="text-link" href="\/proposals\/new"/);
  assert.match(i18n, /'查看 Webhook 机会':'View Webhook opportunities'/);
  assert.match(i18n, /'移除':'Remove'/);
});
