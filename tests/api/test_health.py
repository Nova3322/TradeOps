import asyncio
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings


class FakeDatabase:
    def __init__(self, ready: bool = True, error_code: str | None = None) -> None:
        self.ready = ready
        self.error_code = error_code
        self.disposed = False

    def is_ready(self) -> tuple[bool, str | None]:
        return self.ready, self.error_code

    def dispose(self) -> None:
        self.disposed = True


def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+psycopg://test:test@localhost/test",
        _env_file=None,
    )


async def async_get(app: FastAPI, path: str) -> Response:
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)


def get(app: FastAPI, path: str) -> Response:
    return asyncio.run(async_get(app, path))


def test_liveness_does_not_claim_database_readiness() -> None:
    database = FakeDatabase(ready=False, error_code="DATABASE_UNAVAILABLE")

    response = get(create_app(settings(), database), "/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert database.disposed is True


def test_readiness_requires_durable_store_and_control_gates() -> None:
    response = get(create_app(settings(), FakeDatabase()), "/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "durable_store": "postgresql"}


def test_readiness_fails_closed_with_stable_error_code() -> None:
    database = FakeDatabase(ready=False, error_code="CONTROL_GATES_MISSING")

    response = get(create_app(settings(), database), "/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "not_ready",
        "error_code": "CONTROL_GATES_MISSING",
    }


def test_metrics_endpoint_exposes_control_plane_metrics() -> None:
    response = get(create_app(settings(), FakeDatabase()), "/metrics")

    assert response.status_code == 200
    assert "trading_database_ready" in response.text


def test_web_shell_is_served_without_claiming_business_readiness() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))
    response = get(app, "/")

    assert response.status_code == 200
    assert "Trading Console" in response.text
    assert "/assets/app.js?v=31" in response.text
    assert "/assets/styles.css?v=18" in response.text
    assert '<a href="/" data-link><span>⌂</span>今日</a>' in response.text
    assert 'id="mobile-nav-toggle"' in response.text
    assert 'id="confirm-dialog"' in response.text

    for route in ("/venues", "/venues/hyperliquid", "/admin/users"):
        routed_shell = get(app, route)
        assert routed_shell.status_code == 200
        assert "Trading Console" in routed_shell.text

    app_javascript = get(app, "/assets/app.js")
    assert app_javascript.status_code == 200
    assert "history.replaceState({}, '', loginDestination());" in app_javascript.text
    assert "const destination = `${location.pathname}${location.search}`;" in app_javascript.text
    assert "timeoutError.code = 'REQUEST_TIMEOUT'" in app_javascript.text
    assert "networkError.code = 'NETWORK_ERROR'" in app_javascript.text
    assert "const REQUEST_TIMEOUT_MS = 15000" in app_javascript.text
    assert "全局风险恢复由管理员控制" in app_javascript.text
    assert "if (error.status !== 403) throw error" in app_javascript.text
    assert "actionable_for_current_user" in app_javascript.text
    assert "你的审核已记录" in app_javascript.text
    assert 'data-nav-capability="opportunity.view"' in response.text
    assert 'href="/admin/users"' in response.text
    assert 'href="/proposals/new"' not in response.text
    assert "Binance只读" not in response.text
    assert "const routeCapability = (path)" in app_javascript.text
    assert "今日只显示你的资金职责" in app_javascript.text
    assert "当前职责不包含这个页面" in app_javascript.text
    assert "当前角色可观察候选，但不能创建提案" in app_javascript.text  # noqa: RUF001
    assert "error.handled = response.status === 401" in app_javascript.text
    assert "function handleUnauthorizedResponse" in app_javascript.text
    assert "function confirmAction" in app_javascript.text
    assert "function partitionCapitalRecords" in app_javascript.text
    assert "function capitalSourceSlots" in app_javascript.text
    assert "function liveCapitalInTransit" in app_javascript.text
    assert "模拟数据" in app_javascript.text
    assert "独立隔离" in app_javascript.text
    assert "未配置或未同步" in app_javascript.text
    assert app_javascript.text.count("${capitalProposalForm}") == 1
    assert "${capitalProposalForm}${mockFactForm}${automationPanel}" in app_javascript.text
    assert "fmtNumber(item.in_transit)" not in app_javascript.text
    assert "fmtNumber(liveInTransit)" in app_javascript.text
    assert "申请受审核恢复" in app_javascript.text
    assert "risk.restore.review" in app_javascript.text
    assert "risk.restore.execute" in app_javascript.text
    assert "旧提案、旧授权和旧 AddUnit 永远不会复活" in app_javascript.text
    assert "本地条件满足" in app_javascript.text
    assert "LIVE_SCOPE_CONFIGURATION_REQUIRED" in app_javascript.text
    assert "i.readiness === 'READY' && i.proposal_eligible" in app_javascript.text
    assert "Catalog 未认证此交易合约" in app_javascript.text
    assert "function updateManualProposalPreview" in app_javascript.text
    assert "只创建提案，不直接下单" in app_javascript.text  # noqa: RUF001
    assert "这笔交易要做什么" in app_javascript.text
    assert "查看技术载荷与语义哈希" in app_javascript.text
    assert "const canReview = Boolean(item.actionable_for_current_user);" in app_javascript.text
    assert "INITIAL_INTENT_ALREADY_EXISTS" in app_javascript.text
    assert "账户事实已经过期" in app_javascript.text
    assert "系统允许开多少" in app_javascript.text
    assert "当前唯一推荐动作" in app_javascript.text
    assert "data-close-campaign" in app_javascript.text
    assert "/api/campaigns/${item.campaign_id}/close" in app_javascript.text
    assert "现在按这个顺序处理" in app_javascript.text
    assert "没有必须立即处理的事项" in app_javascript.text
    assert "当前作用域无异常" in app_javascript.text
    assert "api('/api/campaign-exceptions')" in app_javascript.text
    assert "if (error.status === 403) return null" in app_javascript.text
    assert "全局风险恢复仍由管理员控制" in app_javascript.text
    assert "异常与恢复" in app_javascript.text
    assert "POSITION_STALE" in app_javascript.text
    assert "打开 Campaign 按顺序处理" in app_javascript.text
    assert "function actualResultsVerdict" in app_javascript.text
    assert "先看实际盈亏和当前结论" in app_javascript.text
    assert "待处理 Campaign" in app_javascript.text
    assert "系统运行边界与技术状态" in app_javascript.text

    stylesheet = get(app, "/assets/styles.css")
    assert stylesheet.status_code == 200
    assert ".sidebar[hidden] ~ .main-content" in stylesheet.text
    assert ".table-scroll-hint" in stylesheet.text
    assert "visibility 0s linear .2s" in stylesheet.text
    assert "visibility: visible; transition-delay: 0s" in stylesheet.text
    assert ".simulation-panel" in stylesheet.text
    assert ".capital-status-missing" in stylesheet.text
    assert ".proposal-detail-layout" in stylesheet.text
    assert ".proposal-preview" in stylesheet.text
    assert ".source-facts" in stylesheet.text

    service_worker = get(app, "/sw.js")
    assert service_worker.status_code == 200
    assert "trading-shell-v31" in service_worker.text
    assert "await fetch(event.request)" in service_worker.text


def test_opportunity_card_disables_creation_when_catalog_rejects_raw_contract() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("function opportunityCard");
        const to = source.indexOf("\nfunction openSystemDialog", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = {
          escapeHtml: String,
          fmtNumber: String,
          fmtDate: String,
          fmtCompact: String,
          hasCapability: () => true,
        };
        vm.createContext(context);
        vm.runInContext(`${source.slice(from, to)}; this.render = opportunityCard;`, context);
        const unavailable = context.render({
          candidate_id:"pt_unavailable", venue:"BINANCE", timeframe:"1h",
          symbol:"BTCUSDC", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:false,
          proposal_blocker:"INSTRUMENT_UNAVAILABLE", quote_volume:null, open_interest:null,
          rationale:"candidate", detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.match(unavailable, /Catalog 未认证此交易合约/);
        assert.equal((unavailable.match(/ disabled/g) || []).length, 2);

        const eligible = context.render({
          candidate_id:"pt_eligible", venue:"BINANCE", timeframe:"1h",
          symbol:"BTCUSDT", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:true, proposal_blocker:null,
          quote_volume:null, open_interest:null, rationale:"candidate",
          detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.doesNotMatch(eligible, /Catalog 未认证此交易合约/);
        assert.doesNotMatch(eligible, / disabled/);

        context.hasCapability = () => false;
        const readOnly = context.render({
          candidate_id:"pt_read_only", venue:"BINANCE", timeframe:"1h",
          symbol:"BTCUSDT", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:true, proposal_blocker:null,
          quote_volume:null, open_interest:null, rationale:"candidate",
          detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.match(readOnly, /当前角色可观察候选/);
        assert.match(readOnly, /不能创建提案/);
        assert.doesNotMatch(readOnly, /一键创建|高级配置/);
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_actual_results_verdict_prioritizes_exceptions_and_settlement_state() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("function signedResult");
        const to = source.indexOf("\nasync function renderActualResults", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = {fmtNumber: value => String(Number(value))};
        vm.createContext(context);
        vm.runInContext(
          `${source.slice(from, to)}; this.verdict = actualResultsVerdict; ` +
          `this.signed = signedResult; this.valueClass = resultValueClass;`,
          context,
        );

        const exception = context.verdict(
          [{campaign_id:"campaign-1", status:"RUNNING"}],
          [
            {campaign_id:"campaign-1", code:"POSITION_UNKNOWN"},
            {campaign_id:"campaign-1", code:"RECONCILIATION_DIFF"},
          ],
        );
        assert.equal(exception.tone, "danger");
        assert.match(exception.title, /^1 个 Campaign/);
        assert.equal(exception.href, "/exceptions");

        const active = context.verdict(
          [{campaign_id:"campaign-1", status:"RUNNING"}],
          [],
        );
        assert.equal(active.tone, "attention");
        assert.match(active.copy, /结果才会固定/);

        const settled = context.verdict(
          [{campaign_id:"campaign-1", status:"CLOSED"}],
          [],
        );
        assert.equal(settled.tone, "success");
        assert.match(settled.title, /当前没有待处理异常/);

        const empty = context.verdict([], []);
        assert.equal(empty.tone, "clear");
        assert.equal(empty.href, "/opportunities");
        assert.equal(context.signed("10"), "+10");
        assert.equal(context.signed("-2"), "-2");
        assert.equal(context.valueClass("10"), "result-positive");
        assert.equal(context.valueClass("-2"), "result-negative");
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_capital_web_projection_separates_live_and_simulation_records() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("const LIVE_CAPITAL_SOURCES");
        const to = source.indexOf("\nfunction capitalBalanceRows", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const capitalFormFrom = source.indexOf("const capitalProposalForm");
        const capitalFormTo = source.indexOf("const mockFactForm", capitalFormFrom);
        const capitalFormSource = source.slice(capitalFormFrom, capitalFormTo);
        assert.match(capitalFormSource, /<option>TESTNET<\/option>/);
        assert.match(capitalFormSource, /<option>SHADOW<\/option>/);
        assert.doesNotMatch(capitalFormSource, /<option>LIVE<\/option>/);

        const records = [
          {environment:"LIVE", location_type:"VENUE", venue:"BINANCE", marker:"live-binance"},
          {environment:"SHADOW", location_type:"VENUE", venue:"HYPERLIQUID", marker:"shadow-10000"},
          {environment:"TESTNET", location_type:"VAULT", venue:"VAULT", marker:"test-vault"},
        ];
        const context = vm.createContext({records});
        vm.runInContext(source.slice(from, to), context);
        const split = JSON.parse(vm.runInContext(
          "JSON.stringify(partitionCapitalRecords(records))",
          context,
        ));
        assert.deepEqual(split.live.map(item => item.marker), ["live-binance"]);
        assert.deepEqual(
          split.simulated.map(item => item.marker),
          ["shadow-10000", "test-vault"],
        );

        const slots = JSON.parse(vm.runInContext(
          `JSON.stringify(capitalSourceSlots(records, {
            chains:[{chain_id:42161, vault_configured:false}],
          }))`,
          context,
        ));
        assert.equal(slots.length, 3);
        assert.equal(slots[0].marker, "live-binance");
        assert.equal(slots[1].venue, "HYPERLIQUID");
        assert.equal(slots[1].fact_status, "MISSING");
        assert.equal(slots[2].venue, "VAULT");
        assert.equal(slots[2].usd_equity, "0");
        assert.equal(slots[2].fact_status, "MISSING");
        assert.equal(slots[2].missing_detail, "未配置或未同步");
        assert.equal(slots.some(item => item.marker === "shadow-10000"), false);

        const liveInTransit = vm.runInContext(
          `liveCapitalInTransit([
            {environment:"LIVE", status:"IN_FLIGHT", reserved_amount:"1.200000000000000000"},
            {environment:"LIVE", status:"UNKNOWN", reserved_amount:"0.050000000000000000"},
            {environment:"LIVE", status:"SETTLED", reserved_amount:"500"},
            {environment:"SHADOW", status:"IN_FLIGHT", reserved_amount:"10000"},
            {environment:"TESTNET", status:"MANUAL_REQUIRED", reserved_amount:"20000"},
          ])`,
          context,
        );
        assert.equal(liveInTransit, "1.250000000000000000");
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_web_request_lifecycle_in_node() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const extract = (start, end) => {
          const from = source.indexOf(start);
          const to = source.indexOf(end, from);
          assert.notEqual(from, -1);
          assert.notEqual(to, -1);
          return source.slice(from, to);
        };
        const apiSource = extract("async function api", "\nfunction showToast");
        const pendingSource = extract(
          "async function withPending",
          "\nfunction formNumber",
        );
        const navigationSource = extract(
          "function cancelMobileNavFocus",
          "\nfunction bindLinkedRows",
        );
        const navigationInteractionSource = extract(
          "mobileNavToggle.addEventListener('mousedown'",
          "\nnavBackdrop.addEventListener",
        );
        const logoutSource = extract(
          "document.querySelector('#logout-button')",
          "\ndocument.querySelector('#theme-toggle')",
        );
        const unauthorizedSource = extract(
          "function handleUnauthorizedResponse",
          "\nfunction showApiError",
        );

        const realSetTimeout = globalThis.setTimeout;
        let configuredDelay = 0;
        const timeoutContext = vm.createContext({
          AbortController,
          REQUEST_TIMEOUT_MS: 15000,
          clearTimeout,
          handleUnauthorizedResponse: () => false,
          setTimeout(callback, delay) {
            configuredDelay = delay;
            return realSetTimeout(callback, 5);
          },
          fetch: (_path, { signal }) =>
            new Promise((_resolve, reject) => {
              signal.addEventListener("abort", () => {
                const error = new Error("aborted");
                error.name = "AbortError";
                reject(error);
              }, { once: true });
            }),
        });
        vm.runInContext(apiSource, timeoutContext);
        await assert.rejects(
          timeoutContext.api("/mutation", { method: "POST" }),
          (error) => {
            assert.equal(error.code, "REQUEST_TIMEOUT");
            assert.equal(error.outcomeUnknown, true);
            assert.match(error.message, /按钮已恢复/);
            return true;
          },
        );
        assert.equal(configuredDelay, 15000);

        const networkContext = vm.createContext({
          AbortController,
          REQUEST_TIMEOUT_MS: 15000,
          clearTimeout,
          handleUnauthorizedResponse: () => false,
          setTimeout,
          fetch: async () => {
            throw new TypeError("offline");
          },
        });
        vm.runInContext(apiSource, networkContext);
        await assert.rejects(
          networkContext.api("/mutation", { method: "POST" }),
          (error) => {
            assert.equal(error.code, "NETWORK_ERROR");
            assert.equal(error.outcomeUnknown, true);
            assert.match(error.message, /按钮已恢复/);
            return true;
          },
        );

        let unauthorizedCalls = 0;
        const response401Context = vm.createContext({
          AbortController,
          REQUEST_TIMEOUT_MS: 15000,
          clearTimeout,
          handleUnauthorizedResponse() {
            unauthorizedCalls += 1;
            return true;
          },
          setTimeout,
          fetch: async () => ({
            status: 401,
            ok: false,
            json: async () => ({
              error: { code: "SESSION_EXPIRED", message: "expired" },
            }),
          }),
        });
        vm.runInContext(apiSource, response401Context);
        await assert.rejects(
          response401Context.api("/mutation", { method: "POST" }),
          (error) => {
            assert.equal(error.status, 401);
            assert.equal(error.handled, true);
            return true;
          },
        );
        assert.equal(unauthorizedCalls, 1);

        const lifecycle = {
          enhance: 0,
          login: 0,
          shell: 0,
          clearedTimer: null,
          removedToastClasses: [],
        };
        const toastAttributes = new Map([
          ["role", "alert"],
          ["aria-live", "assertive"],
        ]);
        const unauthorizedContext = vm.createContext({
          authFailureActive: false,
          clearTimeout(timer) { lifecycle.clearedTimer = timer; },
          confirmDialog: { open: false, close() {} },
          dialog: { open: false, close() {} },
          enhanceRenderedPage() { lifecycle.enhance += 1; },
          renderLogin() { lifecycle.login += 1; },
          session: { username: "operator" },
          sessionNotice: "",
          setShell() { lifecycle.shell += 1; },
          toast: {
            classList: {
              remove(...names) { lifecycle.removedToastClasses = names; },
            },
            setAttribute(name, value) { toastAttributes.set(name, value); },
            textContent: "stale request timeout",
          },
          toastTimer: 41,
        });
        vm.runInContext(unauthorizedSource, unauthorizedContext);
        assert.equal(unauthorizedContext.handleUnauthorizedResponse(), true);
        assert.equal(unauthorizedContext.handleUnauthorizedResponse(), true);
        assert.equal(unauthorizedContext.session, null);
        assert.equal(unauthorizedContext.toastTimer, null);
        assert.equal(unauthorizedContext.toast.textContent, "");
        assert.equal(toastAttributes.get("role"), "status");
        assert.equal(toastAttributes.get("aria-live"), "polite");
        assert.deepEqual(lifecycle, {
          enhance: 1,
          login: 1,
          shell: 1,
          clearedTimer: 41,
          removedToastClasses: ["show", "error"],
        });

        const pendingContext = vm.createContext({});
        vm.runInContext(pendingSource, pendingContext);
        const attributes = new Map();
        const button = {
          dataset: {},
          disabled: false,
          isConnected: false,
          textContent: "刷新 PnL",
          removeAttribute(name) { attributes.delete(name); },
          setAttribute(name, value) { attributes.set(name, value); },
        };
        let rejectAction;
        let actionCalls = 0;
        const first = pendingContext.withPending(
          button,
          "刷新中…",
          () => {
            actionCalls += 1;
            return new Promise((_resolve, reject) => { rejectAction = reject; });
          },
        );
        const duplicate = pendingContext.withPending(
          button,
          "刷新中…",
          () => { actionCalls += 1; },
        );
        assert.equal(await duplicate, undefined);
        assert.equal(actionCalls, 1);
        assert.equal(button.disabled, true);
        assert.equal(button.dataset.pending, "true");
        assert.equal(attributes.get("aria-busy"), "true");
        rejectAction(new Error("timeout"));
        await assert.rejects(first, /timeout/);
        assert.equal(button.disabled, false);
        assert.equal(button.dataset.pending, undefined);
        assert.equal(attributes.has("aria-busy"), false);
        assert.equal(button.textContent, "刷新 PnL");

        const sidebarClasses = new Set();
        const bodyClasses = new Set();
        const sidebarAttributes = new Map();
        const toggleAttributes = new Map([["aria-expanded", "false"]]);
        const frameCallbacks = [];
        const cancelledFrames = new Set();
        const toggleListeners = new Map();
        let nextFrameId = 1;
        let activeElement = null;
        const firstNavigationLink = {
          isConnected: true,
          focus() { activeElement = firstNavigationLink; },
        };
        const logoutButton = {
          focus() { activeElement = logoutButton; },
        };
        const toggle = {
          hidden: false,
          addEventListener(name, listener) { toggleListeners.set(name, listener); },
          focus() { activeElement = toggle; },
          setAttribute(name, value) { toggleAttributes.set(name, value); },
        };
        const navigationContext = vm.createContext({
          document: {
            body: {
              classList: {
                add(name) { bodyClasses.add(name); },
                remove(name) { bodyClasses.delete(name); },
              },
            },
          },
          cancelAnimationFrame(id) { cancelledFrames.add(id); },
          getComputedStyle() {
            return { visibility: sidebarClasses.has("open") ? "visible" : "hidden" };
          },
          main: { inert: false },
          matchMedia: () => ({ matches: true }),
          mobileNavFocusFrame: null,
          mobileNavFocusToken: 0,
          mobileNavToggle: toggle,
          navBackdrop: { hidden: true },
          requestAnimationFrame(callback) {
            const id = nextFrameId;
            nextFrameId += 1;
            frameCallbacks.push({ id, callback });
            return id;
          },
          session: { username: "operator" },
          sidebar: {
            hidden: false,
            inert: true,
            classList: {
              add(name) { sidebarClasses.add(name); },
              contains(name) { return sidebarClasses.has(name); },
              remove(name) { sidebarClasses.delete(name); },
            },
            querySelector(selector) {
              assert.equal(selector, "nav a");
              return firstNavigationLink;
            },
            setAttribute(name, value) { sidebarAttributes.set(name, value); },
          },
        });
        vm.runInContext(navigationSource, navigationContext);
        vm.runInContext(navigationInteractionSource, navigationContext);

        let prevented = false;
        toggleListeners.get("mousedown")({
          preventDefault() { prevented = true; },
        });
        assert.equal(prevented, true);
        toggleListeners.get("click")();
        toggle.focus();
        assert.equal(activeElement, toggle);
        assert.equal(sidebarClasses.has("open"), true);
        assert.equal(navigationContext.sidebar.inert, false);
        assert.equal(sidebarAttributes.get("aria-hidden"), "false");
        assert.equal(toggleAttributes.get("aria-expanded"), "true");
        assert.equal(bodyClasses.has("nav-open"), true);
        assert.equal(navigationContext.main.inert, true);
        const coldStartFrame = frameCallbacks.shift();
        coldStartFrame.callback();
        assert.equal(activeElement, firstNavigationLink);
        assert.equal(navigationContext.mobileNavFocusFrame, null);
        const navigationFocusStable = activeElement === firstNavigationLink;

        navigationContext.closeMobileNav();
        assert.equal(activeElement, toggle);
        assert.equal(sidebarClasses.has("open"), false);
        assert.equal(navigationContext.sidebar.inert, true);
        assert.equal(sidebarAttributes.get("aria-hidden"), "true");
        assert.equal(toggleAttributes.get("aria-expanded"), "false");
        assert.equal(bodyClasses.has("nav-open"), false);
        assert.equal(navigationContext.main.inert, false);

        navigationContext.openMobileNav();
        const closeStaleFrame = frameCallbacks.shift();
        navigationContext.closeMobileNav();
        assert.equal(cancelledFrames.has(closeStaleFrame.id), true);
        closeStaleFrame.callback();
        assert.equal(activeElement, toggle);

        navigationContext.openMobileNav();
        const rapidStaleFrame = frameCallbacks.shift();
        navigationContext.closeMobileNav();
        navigationContext.openMobileNav();
        const rapidCurrentFrame = frameCallbacks.shift();
        rapidStaleFrame.callback();
        assert.equal(activeElement, toggle);
        rapidCurrentFrame.callback();
        assert.equal(activeElement, firstNavigationLink);
        assert.equal(sidebarClasses.has("open"), true);

        navigationContext.closeMobileNav();
        prevented = false;
        toggleListeners.get("keydown")({
          key: "Enter",
          preventDefault() { prevented = true; },
        });
        assert.equal(prevented, true);
        frameCallbacks.shift().callback();
        assert.equal(activeElement, firstNavigationLink);
        navigationContext.closeMobileNav();

        prevented = false;
        toggleListeners.get("keydown")({
          key: " ",
          preventDefault() { prevented = true; },
        });
        assert.equal(prevented, true);
        frameCallbacks.shift().callback();
        assert.equal(activeElement, firstNavigationLink);
        navigationContext.closeMobileNav();

        prevented = false;
        toggleListeners.get("keydown")({
          key: "Tab",
          preventDefault() { prevented = true; },
        });
        assert.equal(prevented, false);
        assert.equal(sidebarClasses.has("open"), false);

        navigationContext.openMobileNav();
        const logoutStaleFrame = frameCallbacks.shift();
        logoutButton.focus();
        navigationContext.cancelMobileNavFocus();
        assert.equal(cancelledFrames.has(logoutStaleFrame.id), true);
        logoutStaleFrame.callback();
        assert.equal(activeElement, logoutButton);
        assert.ok(
          logoutSource.indexOf("cancelMobileNavFocus();") <
            logoutSource.indexOf("await api('/api/auth/logout'"),
        );

        navigationContext.closeMobileNav();
        navigationContext.openMobileNav();
        const desktopStaleFrame = frameCallbacks.shift();
        toggle.focus();
        navigationContext.matchMedia = () => ({ matches: false });
        navigationContext.syncNavigationMode();
        assert.equal(cancelledFrames.has(desktopStaleFrame.id), true);
        desktopStaleFrame.callback();
        assert.equal(activeElement, toggle);
        assert.equal(sidebarClasses.has("open"), false);
        assert.equal(toggleAttributes.get("aria-expanded"), "false");
        assert.equal(navigationContext.sidebar.inert, false);
        assert.equal(sidebarAttributes.get("aria-hidden"), "false");
        assert.equal(navigationContext.navBackdrop.hidden, true);
        assert.equal(bodyClasses.has("nav-open"), false);
        assert.equal(navigationContext.main.inert, false);

        navigationContext.matchMedia = () => ({ matches: true });
        navigationContext.syncNavigationMode();
        assert.equal(sidebarClasses.has("open"), false);
        assert.equal(toggleAttributes.get("aria-expanded"), "false");
        assert.equal(navigationContext.sidebar.inert, true);
        assert.equal(sidebarAttributes.get("aria-hidden"), "true");
        navigationContext.openMobileNav();
        frameCallbacks.shift().callback();
        assert.equal(activeElement, firstNavigationLink);

        console.log(JSON.stringify({
          configuredDelay,
          duplicateCalls: actionCalls,
          lifecycle: {
            enhance: lifecycle.enhance,
            login: lifecycle.login,
            shell: lifecycle.shell,
          },
          navigationFocusStable,
          pendingRestored: !button.disabled,
        }));
        """
    )

    result = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "configuredDelay": 15000,
        "duplicateCalls": 1,
        "lifecycle": {"enhance": 1, "login": 1, "shell": 1},
        "navigationFocusStable": True,
        "pendingRestored": True,
    }


def test_mock_login_is_not_available_unless_explicitly_enabled() -> None:
    async def post() -> Response:
        app = create_app(settings(), FakeDatabase())
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post("/api/auth/mock/login", json={"username": "admin"})

    response = asyncio.run(post())

    assert response.status_code == 404
