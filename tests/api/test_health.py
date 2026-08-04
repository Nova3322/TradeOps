import asyncio
import json
import shutil
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from trading_control_plane.api import _perptape_runtime_status, create_app
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


def test_perptape_runtime_status_distinguishes_configuration_and_freshness() -> None:
    now = datetime.now(UTC)
    empty_feed = {
        "available": False,
        "contract_version": None,
        "fetched_at": None,
    }
    assert _perptape_runtime_status(settings(), empty_feed, now=now) == "NOT_CONFIGURED"

    on_demand = settings().model_copy(update={"perptape_api_key": "configured"})
    assert _perptape_runtime_status(on_demand, empty_feed, now=now) == "ON_DEMAND"
    continuous = on_demand.model_copy(update={"runtime_sync_enabled": True})
    assert _perptape_runtime_status(continuous, empty_feed, now=now) == "WAITING"

    fresh_feed = {
        "available": True,
        "contract_version": "breakouts-v1",
        "fetched_at": (now - timedelta(seconds=10)).isoformat(),
    }
    assert _perptape_runtime_status(continuous, fresh_feed, now=now) == "SUCCESS"
    stale_feed = {**fresh_feed, "fetched_at": (now - timedelta(minutes=3)).isoformat()}
    assert _perptape_runtime_status(continuous, stale_feed, now=now) == "STALE"
    mismatched_feed = {**fresh_feed, "contract_version": "breakouts-v0"}
    assert _perptape_runtime_status(continuous, mismatched_feed, now=now) == "STALE"


def test_web_shell_is_served_without_claiming_business_readiness() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))
    response = get(app, "/")

    assert response.status_code == 200
    assert "交易控制台" in response.text
    assert "/assets/app.js?v=74" in response.text
    assert "/assets/styles.css?v=35" in response.text
    assert '<a href="/" data-link><span>⌂</span>今日</a>' in response.text
    assert 'id="mobile-nav-toggle"' in response.text
    assert 'id="confirm-dialog"' in response.text

    for route in ("/venues", "/venues/hyperliquid", "/opportunities/defaults"):
        routed_shell = get(app, route)
        assert routed_shell.status_code == 200
        assert "交易控制台" in routed_shell.text

    assert get(app, "/admin/users").status_code == 401

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
    assert 'href="/results"' not in response.text
    assert "renderActualResults" not in app_javascript.text
    assert "/api/results" not in app_javascript.text
    assert "/api/audit" not in app_javascript.text
    assert "'access.manage':['SYSTEM_ADMIN']" in app_javascript.text
    assert "'system.view':['OBSERVER','OPERATOR']" in app_javascript.text
    assert "[translate=\"no\"]" in app_javascript.text
    assert "管理所有成员并可访问资金中心" in app_javascript.text
    assert 'href="/proposals/new"' not in response.text
    assert "Binance只读" not in response.text
    assert "const routeCapability = (path)" in app_javascript.text
    assert "今日只显示你的资金职责" in app_javascript.text
    assert "当前职责不包含这个页面" in app_javascript.text
    assert "当前角色可观察候选，但不能创建提案" in app_javascript.text  # noqa: RUF001
    assert "error.handled = response.status === 401" in app_javascript.text
    assert "function handleUnauthorizedResponse" in app_javascript.text
    assert "function confirmAction" in app_javascript.text
    assert "无法登录：账号不存在、尚未分配岗位或已停用" in app_javascript.text  # noqa: RUF001
    assert 'placeholder="请输入分配给你的内部用户名"' in app_javascript.text
    assert "loginForm.querySelector('.form-error').textContent = ''" in app_javascript.text
    assert "function partitionCapitalRecords" in app_javascript.text
    assert "function capitalSourceSlots" in app_javascript.text
    assert "function capitalHistorySeries" in app_javascript.text
    assert "固定四条线：币安、Hyperliquid、资金库和三方汇总" in app_javascript.text  # noqa: RUF001
    assert 'class="capital-route-grid"' in app_javascript.text
    assert 'data-open-capital-path="${escapeHtml(path.path)}"' in app_javascript.text
    assert 'id="direct-capital-dialog"' in app_javascript.text
    assert "function liveCapitalInTransit" in app_javascript.text
    assert "const DIRECT_CAPITAL_PATHS" in app_javascript.text
    assert "/api/capital/direct-operations" in app_javascript.text
    assert "最终确认并检查" in app_javascript.text
    assert "检查转入币安条件" in app_javascript.text
    assert "检查 Hyperliquid 回流条件" in app_javascript.text
    assert "3 项阻断，查看详情" not in app_javascript.text  # noqa: RUF001
    assert "项阻断，查看详情" in app_javascript.text  # noqa: RUF001
    assert "生成无签名预检" in app_javascript.text
    assert "BLOCKED:'已安全阻断'" in app_javascript.text
    assert "NOT_SUBMITTED:'未提交'" in app_javascript.text
    assert "账户范围" in app_javascript.text
    assert "默认账户" in app_javascript.text
    assert "{BINANCE:'币安', HYPERLIQUID:'Hyperliquid'}" in app_javascript.text
    assert "series.points.length && capitalTrendVisibility" in app_javascript.text
    assert "未配置或未同步" in app_javascript.text
    assert "旧流程只保留为只读审计记录" in app_javascript.text
    assert "fmtNumber(item.in_transit)" not in app_javascript.text
    assert "fmtNumber(liveInTransit)" in app_javascript.text
    assert "最高管理员直接恢复" in app_javascript.text
    assert "risk.restore.review" in app_javascript.text
    assert "risk.restore.execute" in app_javascript.text
    assert "旧授权和旧的可用加仓次数不会恢复" in app_javascript.text
    assert "恢复条件" in app_javascript.text
    assert "LIVE_SCOPE_CONFIGURATION_REQUIRED" in app_javascript.text
    assert "item.readiness === 'READY' && item.proposal_eligible" in app_javascript.text
    assert "DEGRADED:'数据不完整'" in app_javascript.text
    assert "该合约尚未进入可交易合约目录" not in app_javascript.text
    assert "共振周期" in app_javascript.text
    assert "proposalResonanceTimeframes(details, candidate)" in app_javascript.text
    assert "formatRiskActionReason" in app_javascript.text
    assert "当前无需恢复" in app_javascript.text
    assert "已失效" in app_javascript.text
    assert "控制状态已变化" in app_javascript.text
    assert 'name="timeframes" type="checkbox"' in app_javascript.text
    assert "function updateManualProposalPreview" in app_javascript.text
    assert "只创建提案，不直接下单" in app_javascript.text  # noqa: RUF001
    assert "相同交易参数不会重复创建" in app_javascript.text
    assert "这笔交易要做什么" in app_javascript.text
    assert "查看技术载荷与语义哈希" not in app_javascript.text
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
    assert "交易任务 · 运行告警详情" in app_javascript.text
    assert "POSITION_STALE" in app_javascript.text
    assert "打开交易任务并按顺序处理" in app_javascript.text
    assert "new WebSocket(`${scheme}://${location.host}/ws/opportunities`)" in app_javascript.text
    assert "function groupOpportunities" in app_javascript.text
    assert "function opportunitySnapshotCounts" in app_javascript.text
    assert 'class="signal-chip-full"' in app_javascript.text
    assert 'class="signal-chip-short"' in app_javascript.text
    assert 'aria-label="${escapeHtml(`${timeframe} · ${signalLabel}`)}"' in app_javascript.text
    assert 'href="/opportunities/defaults"' in app_javascript.text
    assert "function renderOpportunityDefaults" in app_javascript.text
    assert "覆盖币对" in app_javascript.text
    assert "方向机会" in app_javascript.text
    assert "周期信号" in app_javascript.text
    assert "各周期分别计数，不是币对数量" in app_javascript.text  # noqa: RUF001
    assert 'name="view_state"' in app_javascript.text
    assert "function opportunityViewState" in app_javascript.text
    assert (
        "信号快照 ${fmtDate(result?.snapshot_generated_at || result?.as_of)}"
        in app_javascript.text
    )
    assert "Perptape 市场扫描" not in app_javascript.text
    assert "突破详情 ↗" in app_javascript.text
    assert "系统运行边界与技术状态" not in app_javascript.text
    assert "风险检查已完成" in app_javascript.text
    assert "api('/api/runtime/status')" in app_javascript.text
    assert "const opportunityHealthRequest" in app_javascript.text
    assert "Perptape 机会源" in app_javascript.text
    assert "Telegram 审核通知受限" in app_javascript.text
    assert "? '通知可用'" in app_javascript.text
    assert "? '通知受阻'" in app_javascript.text
    assert "使用 Web 审核" in app_javascript.text
    assert "系统机会 ${systemCount} 笔 · 人工判断 ${manualCount} 笔" in app_javascript.text
    assert "生产数据与资金连接" in app_javascript.text
    assert "等待资金库绑定" in app_javascript.text
    assert "当前无监控对象" in app_javascript.text
    assert "venue-sync-form" not in app_javascript.text
    assert "账户数据自动更新中" in app_javascript.text

    stylesheet = get(app, "/assets/styles.css")
    assert stylesheet.status_code == 200
    assert ".sidebar[hidden] ~ .main-content" in stylesheet.text
    assert ".table-scroll-hint" in stylesheet.text
    assert "visibility 0s linear .2s" in stylesheet.text
    assert "visibility: visible; transition-delay: 0s" in stylesheet.text
    assert ".simulation-panel" in stylesheet.text
    assert ".capital-status-missing" in stylesheet.text
    assert ".capital-route-grid" in stylesheet.text
    assert ".capital-trend-toggle" in stylesheet.text
    assert ".capital-blockers" in stylesheet.text
    assert ".opportunity-signals { display: flex; flex-wrap: nowrap" in stylesheet.text
    assert "@container (min-width: 520px)" in stylesheet.text
    assert ".opportunity-stats" in stylesheet.text
    assert ".proposal-detail-layout" in stylesheet.text
    assert ".proposal-preview" in stylesheet.text
    assert ".review-queue-summary" in stylesheet.text
    assert ".source-facts" in stylesheet.text

    service_worker = get(app, "/sw.js")
    assert service_worker.status_code == 200
    assert "trading-shell-v68" in service_worker.text
    assert "self.skipWaiting()" in service_worker.text
    assert "self.clients.claim()" in service_worker.text
    assert "await fetch(event.request)" in service_worker.text


def test_opportunity_card_explains_exact_catalog_blocker() -> None:
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
          fmtDirection: value => ({LONG:"做多", SHORT:"做空"}[value] || value),
          fmtReadiness: value => value === "READY" ? "可用" : value,
          hasCapability: () => true,
          proposalDefaults: {configured:true, can_manage:false, data:{version:1}},
          currentLanguage: "zh",
        };
        vm.createContext(context);
        context.opportunityViewState = item => (
          item.action_candidate_id
          || (!Array.isArray(item.candidates) && item.candidate_id)
        ) && item.proposal_eligible
          ? "ACTIONABLE"
          : [
              "PERPTAPE_REQUIRED_FIELDS_MISSING",
              "PERPTAPE_CANDIDATE_NOT_CURRENT",
            ].includes(item.proposal_blocker)
            ? "WAITING" : "WATCH_ONLY";
        vm.runInContext(`${source.slice(from, to)}; this.render = opportunityCard;`, context);
        const unavailable = context.render({
          candidate_id:"pt_unavailable", venue:"BINANCE", timeframe:"1h",
          symbol:"BTCUSDC", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:false,
          proposal_blocker:"INSTRUMENT_UNAVAILABLE", quote_volume:null, open_interest:null,
          rationale:"candidate", detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.match(unavailable, /可交易合约目录/);
        assert.match(unavailable, /仅查看/);
        assert.match(unavailable, /行情可用/);
        assert.doesNotMatch(unavailable, /数据暂不可用/);
        assert.doesNotMatch(unavailable, /一键创建|高级配置/);
        assert.doesNotMatch(unavailable, /后重试/);

        const hip3Unavailable = context.render({
          candidate_id:"pt_hip3", venue:"HYPERLIQUID", timeframe:"1h",
          symbol:"xyz:AAPL", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:false, retry_at:"2026-08-02T10:10:00+00:00",
          proposal_blocker:"INSTRUMENT_UNAVAILABLE", quote_volume:null, open_interest:null,
          rationale:"candidate", detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.match(hip3Unavailable, /该 HIP-3 市场尚未进入当前 Freqtrade worker 的精确合约目录/);
        assert.match(hip3Unavailable, /仅查看/);
        assert.match(hip3Unavailable, /行情可用/);
        assert.doesNotMatch(hip3Unavailable, /数据暂不可用/);
        assert.doesNotMatch(hip3Unavailable, /后重试/);

        const incomplete = context.render({
          candidate_id:"pt_incomplete", venue:"BINANCE", timeframe:"1h",
          symbol:"QUSDT", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"INCOMPLETE", data_health:"DEGRADED", proposal_eligible:false,
          proposal_blocker:"PERPTAPE_REQUIRED_FIELDS_MISSING",
          unavailable_timeframes:["1h"], missing_field_labels:["K 线就绪状态", "实时完整数据"],
          retry_at:"2026-08-02T10:10:00+00:00", quote_volume:null, open_interest:null,
          rationale:"candidate", detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.match(incomplete, /等待行情补齐/);
        assert.match(incomplete, /等待补齐/);
        assert.match(incomplete, /K 线就绪状态、实时完整数据/);
        assert.match(incomplete, /后重试/);
        assert.doesNotMatch(incomplete, /数据暂不可用/);
        assert.doesNotMatch(incomplete, /一键创建|高级配置/);

        const eligible = context.render({
          candidate_id:"pt_eligible", venue:"BINANCE", timeframe:"1h",
          symbol:"BTCUSDT", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:true, proposal_blocker:null,
          quote_volume:null, open_interest:null, rationale:"candidate",
          detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.doesNotMatch(eligible, /该合约尚未进入可交易合约目录/);
        assert.doesNotMatch(eligible, / disabled/);
        assert.match(eligible, /1h · 向上突破/);
        assert.doesNotMatch(eligible, />candidate</);

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


def test_opportunity_groups_keep_multiple_timeframes_and_direction_separate() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("const OPPORTUNITY_TIMEFRAME_ORDER");
        const to = source.indexOf("\nfunction currentOpportunityFilters", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = {};
        vm.createContext(context);
        vm.runInContext(
          `${source.slice(from, to)}; this.group = groupOpportunities; `
          + `this.counts = opportunitySnapshotCounts; this.breakdown = opportunityVenueBreakdown; `
          + `this.viewState = opportunityViewState;`,
          context,
        );
        const base = {
          venue:"BINANCE", source_exchange:"BN", symbol:"BTCUSDT", canonical_symbol:"BTC",
          observed_at:"2026-08-02T10:00:00+00:00", triggered_at:"2026-08-02T10:00:00+00:00",
          readiness:"READY", data_health:"CURRENT", proposal_eligible:true, proposal_blocker:null,
          quote_volume:"100", open_interest:"50",
        };
        const groups = JSON.parse(JSON.stringify(context.group([
          {...base, candidate_id:"pt_1h_long", direction:"LONG", timeframe:"1h"},
          {
            ...base, candidate_id:"pt_4h_long", direction:"LONG", timeframe:"4h",
            quote_volume:"120",
          },
          {...base, candidate_id:"pt_1d_short", direction:"SHORT", timeframe:"1d"},
        ])));
        assert.equal(groups.length, 2);
        const long = groups.find(item => item.direction === "LONG");
        const short = groups.find(item => item.direction === "SHORT");
        assert.deepEqual(long.timeframes, ["1h", "4h"]);
        assert.equal(long.candidates.length, 2);
        assert.equal(long.quote_volume, 120);
        assert.equal(long.action_candidate_id, "pt_1h_long");
        assert.deepEqual(short.timeframes, ["1d"]);
        const counts = context.counts([
          {...base, candidate_id:"pt_1h_long", direction:"LONG", timeframe:"1h"},
          {...base, candidate_id:"pt_4h_long", direction:"LONG", timeframe:"4h"},
          {...base, candidate_id:"pt_1d_short", direction:"SHORT", timeframe:"1d"},
        ], groups);
        assert.equal(counts.unique_symbols, 1);
        assert.equal(counts.symbols_by_venue.BINANCE, 1);
        assert.equal(context.breakdown(counts.symbols_by_venue), "币安 1");
        assert.equal(counts.directional_opportunities, 2);
        assert.equal(counts.timeframe_hits, 3);
        assert.equal(counts.eligible_opportunities, 2);
        assert.equal(counts.waiting_opportunities, 0);
        assert.equal(counts.watch_only_opportunities, 0);

        const catalogBlocked = JSON.parse(JSON.stringify(context.group([{
          ...base,
          candidate_id:"pt_eth_catalog_blocked",
          symbol:"ETHUSDT",
          canonical_symbol:"ETH",
          direction:"LONG",
          timeframe:"4h",
          proposal_eligible:false,
          proposal_blocker:"INSTRUMENT_UNAVAILABLE",
          missing_fields:["active Instrument Catalog match"],
          last_complete_at:"2026-08-02T10:00:00+00:00",
        }])))[0];
        assert.equal(catalogBlocked.proposal_blocker, "INSTRUMENT_UNAVAILABLE");
        assert.equal(catalogBlocked.incomplete_candidates.length, 0);
        assert.deepEqual(catalogBlocked.missing_fields, ["active Instrument Catalog match"]);
        assert.equal(context.viewState(catalogBlocked), "WATCH_ONLY");

        const stableWithOldIncomplete = JSON.parse(JSON.stringify(context.group([
          {
            ...base, candidate_id:"pt_ready", direction:"LONG", timeframe:"1h",
            observed_at:"2026-08-02T10:05:00+00:00",
            last_complete_at:"2026-08-02T10:05:00+00:00",
          },
          {
            ...base, candidate_id:"pt_old_incomplete", direction:"LONG", timeframe:"1h",
            observed_at:"2026-08-02T10:00:00+00:00", readiness:"INCOMPLETE",
            data_health:"DEGRADED", proposal_eligible:false,
            proposal_blocker:"PERPTAPE_REQUIRED_FIELDS_MISSING",
            missing_field_labels:["实时完整数据"], last_complete_at:null,
          },
        ])))[0];
        assert.deepEqual(stableWithOldIncomplete.pending_refresh_timeframes, []);
        assert.deepEqual(stableWithOldIncomplete.unavailable_timeframes, []);
        assert.equal(stableWithOldIncomplete.incomplete_candidates.length, 0);
        assert.equal(stableWithOldIncomplete.last_complete_at, "2026-08-02T10:05:00+00:00");

        const pendingRefresh = JSON.parse(JSON.stringify(context.group([
          {
            ...base, candidate_id:"pt_ready", direction:"LONG", timeframe:"1h",
            observed_at:"2026-08-02T10:05:00+00:00",
            last_complete_at:"2026-08-02T10:05:00+00:00",
          },
          {
            ...base, candidate_id:"pt_new_incomplete", direction:"LONG", timeframe:"1h",
            observed_at:"2026-08-02T10:06:00+00:00", readiness:"INCOMPLETE",
            data_health:"DEGRADED", proposal_eligible:false,
            proposal_blocker:"PERPTAPE_REQUIRED_FIELDS_MISSING",
            missing_field_labels:["实时完整数据"], last_complete_at:null,
          },
        ])))[0];
        assert.deepEqual(pendingRefresh.pending_refresh_timeframes, ["1h"]);
        assert.deepEqual(pendingRefresh.unavailable_timeframes, []);
        assert.equal(pendingRefresh.incomplete_candidates.length, 1);
        assert.equal(pendingRefresh.action_candidate_id, "pt_ready");

        const genuinelyIncomplete = JSON.parse(JSON.stringify(context.group([{
          ...base, candidate_id:"pt_incomplete", direction:"LONG", timeframe:"4h",
          readiness:"INCOMPLETE", data_health:"DEGRADED", proposal_eligible:false,
          proposal_blocker:"PERPTAPE_REQUIRED_FIELDS_MISSING",
          missing_field_labels:["K 线就绪状态", "实时完整数据"], last_complete_at:null,
        }])))[0];
        assert.deepEqual(genuinelyIncomplete.unavailable_timeframes, ["4h"]);
        assert.deepEqual(genuinelyIncomplete.pending_refresh_timeframes, []);
        assert.equal(genuinelyIncomplete.last_complete_at, null);
        assert.equal(context.viewState(genuinelyIncomplete), "WAITING");
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_proposal_review_projection_uses_frozen_resonance_and_plain_risk_reasons() -> None:
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
        const context = vm.createContext({});
        vm.runInContext(
          extract("function proposalResonanceTimeframes", "\nasync function renderProposalDetail")
          + "; this.timeframes = proposalResonanceTimeframes;"
          + " this.rationale = formatProposalRationale;",
          context,
        );
        assert.deepEqual(
          JSON.parse(JSON.stringify(context.timeframes(
            {resonance_timeframes:["1h", "4h", "1d"]},
            {timeframe:"1h"},
          ))),
          ["1h", "4h", "1d"],
        );
        assert.deepEqual(
          JSON.parse(JSON.stringify(context.timeframes({}, {timeframe:"4h"}))),
          ["4h"],
        );
        const rationale = context.rationale(
          "Perptape current exact-instrument resonance across 1h, 4h, 1d; "
          + "使用管理员默认配置。 Proposal only, pending human review.",
        );
        assert.match(rationale, /1h、4h、1d 同时突破/);
        assert.match(rationale, /不会自动审核、授权或下单/);
        assert.doesNotMatch(rationale, /pending human review/);

        vm.runInContext(
          extract("function formatRiskActionReason", "\nfunction renderRiskControlPanel")
          + "; this.actionReason = formatRiskActionReason;",
          context,
        );
        const normal = context.actionReason("SYSTEM_ALREADY_NORMAL", {blockers:[]});
        assert.equal(normal, "无需操作\uFF1A风险政策已经正常开放");
        assert.doesNotMatch(normal, /SYSTEM_ALREADY_NORMAL/);
        assert.equal(
          context.actionReason("REALTIME_CONDITIONS_BLOCKED", {blockers:["a", "b"]}),
          "实时安全条件未全部通过\uFF082 项\uFF09",
        );
        assert.equal(
          context.actionReason("NO_REVIEWABLE_REQUEST", {blockers:[]}),
          "当前没有可由你独立审核的恢复申请",
        );
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_capital_web_projection_only_renders_live_records() -> None:
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
        const records = [
          {environment:"LIVE", location_type:"VENUE", venue:"BINANCE", marker:"live-binance"},
          {environment:"SHADOW", location_type:"VENUE", venue:"HYPERLIQUID", marker:"shadow-10000"},
          {environment:"TESTNET", location_type:"VAULT", venue:"VAULT", marker:"test-vault"},
        ];
        const context = vm.createContext({records});
        vm.runInContext(source.slice(from, to), context);
        const historyFrom = source.indexOf("function capitalHistorySeries");
        const historyTo = source.indexOf("\nasync function renderCapitalCenter", historyFrom);
        vm.runInContext(source.slice(historyFrom, historyTo), context);
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
        assert.equal(slots[2].usd_equity, null);
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

        const history = JSON.parse(vm.runInContext(
          `JSON.stringify(capitalHistorySeries([
            {
              environment:"LIVE", location_type:"VENUE", venue:"BINANCE",
              usd_equity:"10", observed_at:"2026-08-02T10:00:00Z"
            },
            {
              environment:"LIVE", location_type:"VENUE", venue:"HYPERLIQUID",
              usd_equity:"20", observed_at:"2026-08-02T10:00:00Z"
            },
            {
              environment:"LIVE", location_type:"VAULT", venue:"VAULT",
              usd_equity:"30", observed_at:"2026-08-02T10:00:00Z"
            },
            {
              environment:"LIVE", location_type:"VAULT", venue:"VAULT",
              usd_equity:"5", observed_at:"2026-08-02T10:00:00Z"
            },
          ]))`,
          context,
        ));
        assert.deepEqual(
          history.map(item => item.source),
          ["BINANCE", "HYPERLIQUID", "VAULT", "TOTAL"],
        );
        assert.equal(history[2].points[0].value, 35);
        assert.equal(history[3].points[0].value, 65);

        const staggered = JSON.parse(vm.runInContext(
          `JSON.stringify(capitalHistorySeries([
            {environment:"LIVE",location_type:"VENUE",venue:"BINANCE",usd_equity:"10",observed_at:"2026-08-02T10:00:00Z"},
            {environment:"LIVE",location_type:"VENUE",venue:"HYPERLIQUID",usd_equity:"20",observed_at:"2026-08-02T10:01:00Z"},
            {environment:"LIVE",location_type:"VAULT",venue:"VAULT",usd_equity:"30",observed_at:"2026-08-02T10:02:00Z"},
            {environment:"LIVE",location_type:"VENUE",venue:"BINANCE",usd_equity:"11",observed_at:"2026-08-02T10:03:00Z"}
          ]))`,
          context,
        ));
        const staggeredTotal = staggered.find(item => item.source === "TOTAL");
        assert.equal(staggeredTotal.points.length, 2);
        assert.deepEqual(staggeredTotal.points.map(point => point.value), [60, 61]);
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_risk_workspace_prioritizes_current_actions_and_hides_closed_tasks() -> None:
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    source = app_path.read_text()

    assert "系统管理员（最高权限）" in source  # noqa: RUF001
    assert "activeRequests = control.requests.filter" in source
    assert "历史恢复申请" in source
    assert "不计入当前待办" in source
    assert "risk-condition-details" in source
    assert "mode === 'risk' && !hasCapability('operations.view')" in source
    assert "details.filter(item => item.status !== 'CLOSED')" in source
    assert "当前没有运行中的风险任务" in source
    assert "自动加仓已经关闭" in source
    assert "roleNames().join('、')" not in source


def test_system_and_venue_pages_distinguish_read_only_snapshots_from_live_execution() -> None:
    app_path = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web" / "app.js"
    source = app_path.read_text()

    assert "只读控制台可用，但 Freqtrade 执行底座尚未就绪" in source  # noqa: RUF001
    assert "Freqtrade 执行底座已就绪，但交易所只读连接受限" in source  # noqa: RUF001
    assert "两个 Freqtrade worker 已通过 dry-run 检查" in source
    assert "Freqtrade worker 未启动" in source
    assert "LIVE_ORDER_SEND 保持关闭" in source
    assert "fmtConnectionCategory(state.category)" in source
    assert "当前连接不可用，以下仅为最后一次保存快照" in source  # noqa: RUF001
    assert "自动同步等待连接恢复" in source
    assert "Number(item.quantity) !== 0" in source
    assert "不计入当前委托" in source
    assert "当前账户没有未完成委托" in source
    assert "执行由 Freqtrade worker 负责；本页不能下单" in source  # noqa: RUF001
    assert "执行底座为 Freqtrade；控制面尚未接入 worker" in source  # noqa: RUF001
    assert "status.execution_backend === 'FREQTRADE'" in source
    assert "status.worker_configured" in source
    assert "核心市场${status.hip3_available ? ` + HIP-3" in source
    assert "当前账户事实可用；历史记录待补全" in source  # noqa: RUF001
    assert "以下成交与资金费只代表已经保存的记录" in source
    assert "<b>默认账户</b>" in source
    assert "<b>${escapeHtml(accountId)}</b>" not in source
    assert "venue-technical-detail" in source
    assert "左右滑动查看完整${escapeHtml(title)}" in source
    assert "左右滑动查看完整订单记录" in source


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

        const pendingContext = vm.createContext({localizedText: value => value});
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
