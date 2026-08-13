# Embedded JavaScript fixtures preserve product copy and are checked by Node.
# ruff: noqa: E501, RUF001, RUF100

import asyncio
import json
import shutil
import subprocess
import tempfile
import textwrap
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from trading_control_plane.api import (
    _perptape_runtime_status,
    _perptape_transport_status,
    create_app,
)
from trading_control_plane.config import Settings

WEB_ROOT = Path(__file__).parents[2] / "src" / "trading_control_plane" / "web"
FRONTEND_SCRIPTS = (
    "app-core.js",
    "workspace.js",
    "signals.js",
    "proposals.js",
    "risk.js",
    "execution.js",
    "capital.js",
    "reporting.js",
    "accounts.js",
    "app.js",
)


def frontend_source() -> str:
    return "\n".join((WEB_ROOT / name).read_text() for name in FRONTEND_SCRIPTS)


def test_webhook_signals_are_a_separate_dynamic_workspace() -> None:
    shell = (WEB_ROOT / "index.html").read_text()
    application = (WEB_ROOT / "app-core.js").read_text()
    signals = (WEB_ROOT / "signals.js").read_text()
    styles = (WEB_ROOT / "styles.css").read_text()

    assert 'class="nav-link-group" role="group" aria-label="实时信号"' in shell
    assert 'href="/opportunities"' in shell
    assert 'href="/webhook-signals"' in shell
    assert "path === '/webhook-signals'" in application
    assert "await renderWebhookSignals()" in application

    perptape = signals.split("const OPPORTUNITY_TIMEFRAME_ORDER", maxsplit=1)[1]
    webhook = signals.split("async function renderWebhookSignals", maxsplit=1)[1].split(
        "function signalSourceFormDirty", maxsplit=1
    )[0]
    assert "api('/api/opportunities')" in perptape
    assert "api(`/api/webhook-signals?" in webhook
    assert "sources.map(source =>" in webhook
    assert "source.name" in webhook
    assert 'tabindex="${selectedSource' in webhook
    assert "['ArrowLeft', 'ArrowRight', 'Home', 'End']" in webhook
    assert "restoreFocus" in webhook
    assert "TradingView BTC" not in webhook
    assert "策略系统 A" not in webhook
    assert "量化模型 B" not in webhook
    assert "data-signal-event-id" in signals
    assert "proposal_eligibility" in webhook
    assert "freshness" in webhook
    assert ".webhook-signal-facts" in styles
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in styles
    assert "grid-template-columns: 1fr" in styles


@cache
def frontend_bundle_path() -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="tradingops-frontend-",
        suffix=".js",
        delete=False,
    ) as bundle:
        bundle.write(frontend_source())
        return Path(bundle.name)


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


def test_perptape_transport_status_distinguishes_live_stream_and_polling_fallback() -> None:
    now = datetime.now(UTC)
    configured = settings().model_copy(update={"runtime_sync_enabled": True})
    polling = {
        "status": "SUCCESS",
        "checked_at": (now - timedelta(seconds=10)).isoformat(),
        "error_code": None,
    }
    live = _perptape_transport_status(
        configured,
        {
            "PERPTAPE": polling,
            "PERPTAPE_WEBSOCKET": {
                "status": "SUCCESS",
                "checked_at": (now - timedelta(seconds=5)).isoformat(),
                "error_code": None,
            },
        },
        now=now,
    )
    assert live["state"] == "WEBSOCKET_LIVE"
    assert live["primary_channel"] == "WEBSOCKET"
    assert live["fallback_active"] is False

    fallback = _perptape_transport_status(
        configured,
        {
            "PERPTAPE": polling,
            "PERPTAPE_WEBSOCKET": {
                "status": "FAILED",
                "checked_at": now.isoformat(),
                "error_code": "PERPTAPE_AUTH_FAILED",
            },
        },
        now=now,
    )
    assert fallback["state"] == "POLLING_FALLBACK"
    assert fallback["primary_channel"] == "HTTPS_POLLING"
    assert fallback["fallback_active"] is True
    assert fallback["error_code"] == "PERPTAPE_AUTH_FAILED"

    stale = _perptape_transport_status(
        configured,
        {
            "PERPTAPE_WEBSOCKET": {
                "status": "SUCCESS",
                "checked_at": (now - timedelta(hours=1)).isoformat(),
                "error_code": None,
            }
        },
        now=now,
    )
    assert stale["state"] == "WEBSOCKET_FAILED"
    assert stale["error_code"] == "PERPTAPE_WEBSOCKET_HEALTH_STALE"


def test_preference_configuration_and_legacy_theme_normalization_in_node() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("const PREFERENCE_OPTIONS");
        const to = source.indexOf("\nlet currentLanguage", from);
        if (from < 0 || to < 0) throw new Error("preference configuration missing");
        const context = vm.createContext({});
        vm.runInContext(
          source.slice(from, to) + `;
            this.options = PREFERENCE_OPTIONS;
            this.normalizeLanguage = normalizeLanguagePreference;
            this.normalizeTheme = normalizeThemePreference;`,
          context,
        );
        console.log(JSON.stringify({
          language:context.options.language,
          theme:context.options.theme,
          languageAliases:[
            context.normalizeLanguage("zh-cn"),
            context.normalizeLanguage("en-US"),
            context.normalizeLanguage("unsupported"),
          ],
          themeAliases:[
            context.normalizeTheme(null),
            context.normalizeTheme("auto"),
            context.normalizeTheme("DEFAULT"),
            context.normalizeTheme("DARK"),
            context.normalizeTheme('"light"'),
            context.normalizeTheme("unsupported"),
          ],
        }));
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "language": [
            {"value": "zh-CN", "label": "中文"},
            {"value": "en", "label": "English"},
        ],
        "theme": [
            {"value": "system", "label": "跟随系统"},
            {"value": "light", "label": "浅色"},
            {"value": "dark", "label": "深色"},
        ],
        "languageAliases": ["zh-CN", "en", "zh-CN"],
        "themeAliases": ["system", "system", "system", "dark", "light", "system"],
    }


def test_manual_proposal_instrument_picker_filters_and_requires_exact_symbol() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    source = app_path.read_text()
    assert 'select name="venue" aria-label="交易所"' in source
    assert 'input name="instrument_symbol" aria-label="币对"' in source
    assert 'list="manual-instrument-options"' in source
    assert '<option value="HYPERLIQUID">Hyperliquid</option>' in source
    assert '<option value="OKX">OKX</option>' in source
    assert '<option value="BYBIT">Bybit</option>' in source
    assert 'select name="account_id" required' in source
    assert "api('/api/exchange-accounts')" in source
    assert "venue === 'HYPERLIQUID' ? '（含 HIP-3）' : ''" in source
    assert "form.elements.instrument_symbol.reportValidity()" in source
    assert "delete data.instrument_symbol" in source

    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("function manualInstrumentOptions");
        const to = source.indexOf("\nfunction syncManualInstrumentPicker", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({});
        vm.runInContext(
          source.slice(from, to)
            + "; this.options = manualInstrumentOptions;"
            + " this.match = manualInstrumentMatch;"
            + " this.accountOptions = manualAccountOptions;",
          context,
        );
        const instruments = [
          {instrument_id:"b-btc",venue:"BINANCE",symbol:"BTCUSDT"},
          {instrument_id:"b-eth",venue:"BINANCE",symbol:"ETHUSDT"},
          {instrument_id:"h-btc",venue:"HYPERLIQUID",symbol:"BTC"},
          {instrument_id:"h-aapl",venue:"HYPERLIQUID",symbol:"xyz:AAPL"},
        ];
        assert.deepEqual(
          Array.from(context.options(instruments, "BINANCE"), item => item.instrument_id),
          ["b-btc", "b-eth"],
        );
        assert.equal(context.match(instruments, "BINANCE", " btcusdt ").instrument_id, "b-btc");
        assert.equal(context.match(instruments, "HYPERLIQUID", "XYZ:aapl").instrument_id, "h-aapl");
        assert.equal(context.match(instruments, "BINANCE", "BTC"), null);
        assert.equal(context.match(instruments, "HYPERLIQUID", "BTCUSDT"), null);
        const accounts = [
          {account_id:"okx-a",venue:"OKX",active:true},
          {account_id:"okx-disabled",venue:"OKX",active:false},
          {account_id:"bybit-a",venue:"BYBIT",active:true},
        ];
        assert.deepEqual(
          Array.from(context.accountOptions(accounts, "OKX"), item => item.account_id),
          ["okx-a"],
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


def test_closed_campaign_labels_are_flat_currency_aware_and_action_free() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("const fmtNumber");
        const to = source.indexOf("\nconst statusLabels", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({
          currentLanguage:"zh-CN",
          localizedText:value => value,
          Intl,
        });
        vm.runInContext(
          source.slice(from, to)
            + "; this.targetLabel = campaignTargetLabel;"
            + " this.pnlLabel = campaignPnlLabel;"
            + " this.closedFlat = isClosedFlatCampaign;",
          context,
        );
        const closed = {
          status:"CLOSED", current_target_quantity:"0",
          instrument:{collateral_currency:"USDC"},
        };
        assert.equal(context.closedFlat(closed), true);
        assert.equal(context.targetLabel(closed), "已平仓");
        assert.equal(context.pnlLabel(closed, "-0.00274"), "-0.00274 USDC");

        const open = {
          status:"OPEN", current_target_quantity:"0.25", collateral_currency:"USDT",
        };
        assert.equal(context.closedFlat(open), false);
        assert.equal(context.targetLabel(open), "0.25");
        assert.equal(context.pnlLabel(open, "1.5"), "1.5 USDT");

        const detailFrom = source.indexOf("async function renderCampaignDetail");
        const detailTo = source.indexOf("\nfunction campaignNextStep", detailFrom);
        const detailSource = source.slice(detailFrom, detailTo);
        assert.match(detailSource, /const management = closedFlat \? '' : managementPanel/);
        assert.match(
          detailSource,
          /\u4fdd\u62a4\u4e0d\u9002\u7528\uff08\u5f53\u524d\u65e0\u4ed3\u4f4d\uff09/,
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


def test_proposal_launch_window_projection_excludes_expired_approvals() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("const proposalAwaitingLaunch");
        const to = source.indexOf("\nconst statusLabels", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({localizedText:value => value});
        vm.runInContext(source.slice(from, to), context);

        assert.equal(vm.runInContext(`isCurrentProposalItem({status:"DRAFT"}, false)`, context), true);
        assert.equal(vm.runInContext(`isCurrentProposalItem({status:"PENDING_REVIEW"}, false)`, context), true);
        assert.equal(
          vm.runInContext(
            `isCurrentProposalItem({status:"APPROVED",execution_status:"AWAITING_LAUNCH"}, true)`,
            context,
          ),
          true,
        );
        assert.equal(
          vm.runInContext(
            `isCurrentProposalItem({status:"APPROVED",execution_status:"AWAITING_LAUNCH"}, false)`,
            context,
          ),
          false,
        );
        assert.equal(
          vm.runInContext(
            `isCurrentProposalItem({status:"APPROVED",execution_status:"WINDOW_EXPIRED"}, true)`,
            context,
          ),
          false,
        );
        assert.equal(
          vm.runInContext(
            `proposalStatusSupplement({execution_status:"WINDOW_EXPIRED"})`,
            context,
          ),
          "启动窗口已过期",
        );
        assert.equal(
          vm.runInContext(
            `proposalExpiryPresentation({status:"EXPIRED",expires_at:"2026-08-05T16:00:00Z",updated_at:"2026-08-05T12:00:00Z"}).state`,
            context,
          ),
          "已结束",
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


def test_reviewer_today_combines_proposal_and_risk_restore_work_without_false_zero() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("function reviewerHomeWorkload");
        const to = source.indexOf("\nasync function renderHome", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({});
        vm.runInContext(source.slice(from, to), context);

        const proposalsOnly = vm.runInContext(
          `reviewerHomeWorkload(14,{actions:{review_restore:{allowed:false},execute_restore:{allowed:false}}})`,
          context,
        );
        assert.equal(proposalsOnly.restoreTaskCount, 0);
        assert.equal(proposalsOnly.restoreCopy, "当前没有风险恢复审核待办");
        assert.equal(proposalsOnly.headline, "14 笔提案等待你的独立审核");

        const independentRestore = vm.runInContext(
          `reviewerHomeWorkload(2,{actions:{review_restore:{allowed:true},execute_restore:{allowed:false}}})`,
          context,
        );
        assert.equal(independentRestore.restoreTaskCount, 1);
        assert.equal(independentRestore.restoreCopy, "等待你的独立审核");
        assert.equal(independentRestore.headline, "2 笔提案和 1 项风险恢复待办");

        const executableRestore = vm.runInContext(
          `reviewerHomeWorkload(0,{actions:{review_restore:{allowed:false},execute_restore:{allowed:true}}})`,
          context,
        );
        assert.equal(executableRestore.restoreTaskCount, 1);
        assert.equal(executableRestore.restoreCopy, "已审核，等待满足条件后执行");

        const unavailable = vm.runInContext(`reviewerHomeWorkload(0,{error:{code:"NETWORK"}})`, context);
        assert.equal(unavailable.restoreStatus, "—");
        assert.equal(unavailable.restoreCopy, "风险恢复状态读取失败");
        assert.equal(unavailable.hasReviewWork, false);
        assert.equal(unavailable.needsAttention, true);
        assert.equal(unavailable.headline, "提案无待办；风险恢复状态暂不可用");
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_runtime_alert_copy_keeps_internal_codes_out_of_primary_labels() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("const exceptionGuidance");
        const to = source.indexOf("\nconst riskReasonGuidance", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({
          fmtDate: () => "8月5日 14:00",
          fmtSeconds: () => "5 分钟",
          fmtOperationalCopy: () => "技术详情已记录",
        });
        vm.runInContext(
          source.slice(from, to)
          + "; this.category = exceptionCategory; this.detail = formatExceptionDetail;",
          context,
        );

        assert.equal(context.category("ORDER_INTENT_UNKNOWN"), "结果未知");
        assert.equal(context.category("POSITION_UNKNOWN"), "事实缺失");
        assert.equal(context.category("POSITION_STALE"), "数据过期");
        assert.equal(context.category("PROTECTION_INSUFFICIENT"), "保护不足");
        assert.equal(context.category("RECONCILIATION_DIFFERENCE"), "对账差异");
        assert.equal(context.detail("observed_at=2026-08-05T06:00:00Z"), "最近有效事实：8月5日 14:00");
        assert.equal(context.detail("max_age_seconds=300"), "最长有效期：5 分钟");
        assert.equal(context.detail("POSITION_FACT_NEWER"), "仓位事实晚于最近对账");
        assert.equal(context.detail("ORDER_INTENT_NEWER"), "订单意图晚于最近对账");
        assert.equal(context.detail("UNMAPPED_INTERNAL_CODE"), "技术详情已记录");
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_opportunity_card_explains_exact_catalog_blocker() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
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
          fmtVenueLabel: value => ({BINANCE:"币安", HYPERLIQUID:"Hyperliquid"}[value] || value),
          fmtReadiness: value => value === "READY" ? "可用" : value,
          hasCapability: () => true,
          proposalDefaults: {configured:true, can_manage:false, data:{version:1}},
          currentLanguage: "zh",
          fmtTimeRemaining: () => "剩余 7 小时",
        };
        vm.createContext(context);
        context.opportunityViewState = item => item.active_proposal?.proposal_id
          ? "ACTIVE_PROPOSAL"
          : (
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
        assert.doesNotMatch(eligible, /<p class="subtle">1h · 向上突破<\/p>/);
        assert.doesNotMatch(eligible, />candidate</);

        const active = context.render({
          candidate_id:"pt_active", venue:"BINANCE", timeframe:"1h",
          symbol:"TUTUSDT", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:true, proposal_blocker:null,
          quote_volume:null, open_interest:null, rationale:"candidate",
          detail_url:"https://example.test", chart_url:"https://example.test",
          active_proposal:{
            proposal_id:"00000000-0000-0000-0000-000000000001",
            expires_at:"2026-08-02T18:00:00+00:00", active_count:1,
          },
        });
        assert.match(active, /已有待审核提案/);
        assert.match(active, /不会重复创建/);
        assert.match(active, /查看待审核提案/);
        assert.doesNotMatch(active, /一键创建|高级配置/);

        context.hasCapability = () => false;
        const readOnly = context.render({
          candidate_id:"pt_read_only", venue:"BINANCE", timeframe:"1h",
          symbol:"BTCUSDT", direction:"LONG", reference_price:"1", triggered_at:null,
          readiness:"READY", proposal_eligible:true, proposal_blocker:null,
          quote_volume:null, open_interest:null, rationale:"candidate",
          detail_url:"https://example.test", chart_url:"https://example.test",
        });
        assert.doesNotMatch(readOnly, /当前角色可观察候选|不能创建提案/);
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
    app_path = frontend_bundle_path()
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
        assert.equal(counts.active_proposal_opportunities, 0);
        assert.equal(counts.waiting_opportunities, 0);
        assert.equal(counts.watch_only_opportunities, 0);

        const occupiedGroups = JSON.parse(JSON.stringify(context.group([
          {
            ...base, candidate_id:"pt_active_scope", direction:"LONG", timeframe:"1h",
            active_proposal:{
              proposal_id:"00000000-0000-0000-0000-000000000001",
              status:"PENDING_REVIEW", active_count:1,
            },
          },
        ])));
        const occupiedCounts = context.counts([], occupiedGroups);
        assert.equal(context.viewState(occupiedGroups[0]), "ACTIVE_PROPOSAL");
        assert.equal(occupiedCounts.eligible_opportunities, 0);
        assert.equal(occupiedCounts.active_proposal_opportunities, 1);

        const duplicatePeriodGroups = JSON.parse(JSON.stringify(context.group([
          {
            ...base, candidate_id:"pt_ready_new", direction:"LONG", timeframe:"1h",
            observed_at:"2026-08-02T10:06:00+00:00", quote_volume:"110",
          },
          {
            ...base, candidate_id:"pt_ready_old", direction:"LONG", timeframe:"1h",
            observed_at:"2026-08-02T10:05:00+00:00", quote_volume:"999",
          },
          {
            ...base, candidate_id:"pt_4h", direction:"LONG", timeframe:"4h",
            observed_at:"2026-08-02T10:04:00+00:00", quote_volume:"120",
          },
        ])));
        const duplicatePeriodCounts = context.counts([], duplicatePeriodGroups);
        assert.equal(duplicatePeriodCounts.timeframe_hits, 2);
        assert.deepEqual(duplicatePeriodGroups[0].complete_timeframes, ["1h", "4h"]);
        assert.equal(duplicatePeriodGroups[0].quote_volume, 120);

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


def test_opportunity_filters_render_a_bounded_page_and_keep_all_matches() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("function opportunityMatchesFilters");
        const to = source.indexOf("\nfunction bindOpportunityActions", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({
          opportunityViewState: item => item.view_state,
        });
        vm.runInContext(
          `${source.slice(from, to)}; this.page = opportunityVisiblePage;`,
          context,
        );
        const groups = Array.from({length: 30}, (_, index) => ({
          symbol:`COIN${index}USDT`, canonical_symbol:`COIN${index}`,
          venue:index < 25 ? "BINANCE" : "HYPERLIQUID",
          direction:index % 2 ? "SHORT" : "LONG",
          timeframes:index % 3 ? ["1h"] : ["1h", "4h", "1d"],
          quote_volume:1000 + index, open_interest:500 + index,
          view_state:"ACTIONABLE",
        }));
        const first = context.page(
          groups,
          {view_state:"ACTIONABLE", resonance:"1"},
          ["1h", "4h", "1d", "1w"],
          24,
        );
        assert.equal(first.matches.length, 30);
        assert.equal(first.rendered.length, 24);

        const expanded = context.page(
          groups,
          {view_state:"ACTIONABLE", resonance:"1"},
          ["1h", "4h", "1d", "1w"],
          48,
        );
        assert.equal(expanded.matches.length, 30);
        assert.equal(expanded.rendered.length, 30);

        const filtered = context.page(
          groups,
          {view_state:"ACTIONABLE", venue:"HYPERLIQUID", resonance:"3"},
          ["1h", "4h", "1d"],
          24,
        );
        assert.equal(filtered.matches.length, 1);
        assert.equal(filtered.rendered[0].venue, "HYPERLIQUID");
        """
    )
    completed = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, str(app_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    source = app_path.read_text()
    assert "const OPPORTUNITY_PAGE_SIZE = 24" in source
    assert "data-load-more-opportunities" in source
    assert "显示更多（剩余 ${remaining}）" in source


def test_proposal_review_projection_uses_frozen_resonance_and_plain_risk_reasons() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
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
        assert.match(rationale, /仍需人工审核；不会自动授权或下单/);
        assert.doesNotMatch(rationale, /pending human review/);
        const legacyChineseRationale = context.rationale(
          "Perptape 当前同一精确合约、同一方向在 1h、4h、1d、1w 同时突破。"
          + "使用管理员保存的一键创建默认配置，仅创建待审核提案。 "
          + "系统仅创建冻结待审核提案，不会自动审核、授权或下单。",
        );
        assert.match(legacyChineseRationale, /^创建提案时，突破榜单中/);
        assert.match(legacyChineseRationale, /来源与参数已冻结/);
        assert.doesNotMatch(legacyChineseRationale, /突破榜单当前|Perptape 当前/);

        vm.runInContext(
          extract("function formatRiskActionReason", "\nfunction renderRiskControlPanel")
          + "; this.actionReason = formatRiskActionReason;",
          context,
        );
        const normal = context.actionReason("SYSTEM_ALREADY_NORMAL", {blockers:[]});
        assert.equal(normal, "无需恢复\uFF1A风险政策当前为正常状态");
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


def test_risk_workspace_prioritizes_current_actions_and_hides_closed_tasks() -> None:
    app_path = frontend_bundle_path()
    source = app_path.read_text()

    assert "系统管理员（最高权限）" in source  # noqa: RUF001
    assert "activeRequests = control.requests.filter" in source
    assert "历史恢复申请" in source
    assert "不计入当前待办" in source
    assert "risk-condition-details" in source
    assert "blockingChecks = (conditions.checks || []).filter" in source
    assert "passingChecks = (conditions.checks || []).filter" in source
    assert "阻塞优先展示；已通过条件收起" in source
    assert "项阻塞优先展示；已通过条件收起" not in source
    assert "项已通过" in source
    assert "通过项不占用当前待办" in source
    assert "政策正常不代表所有账户都能新增风险" in source
    assert "当前没有实时阻塞" in source
    assert "mode === 'risk' && !hasCapability('operations.view')" in source
    assert "details.filter(item => item.status !== 'CLOSED')" in source
    assert "当前没有运行中的风险任务" in source
    assert "hasCapability('operations.view') ? '<section class=\"empty-state" in source
    assert "自动加仓保持关闭" in source
    assert (
        "HYPERLIQUID_RATE_LIMITED:'Hyperliquid 只读接口限流，系统会按计划重试'"  # noqa: RUF001
        in source
    )
    assert "escapeHtml(fmtRole(check.role))" in source
    assert 'data-label="精确原因"' in source
    assert "wrapper.closest('.risk-condition-details')" in source
    assert "roleNames().join('、')" not in source
    styles = (WEB_ROOT / "styles.css").read_text()
    assert ".risk-condition-details tr { display: block;" in styles
    assert ".risk-condition-scroll-hint { display: none; }" in styles
    assert ".risk-passed-conditions" in styles


def test_venue_account_detail_route_serves_the_spa_shell() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))

    response = get(app, "/venues/binance-main")

    assert response.status_code == 200
    assert "交易控制台" in response.text
    assert "/assets/accounts.js?v=174" in response.text


def test_venue_snapshot_empty_states_do_not_claim_current_account_state() -> None:
    node = shutil.which("node")
    assert node is not None
    app_path = frontend_bundle_path()
    script = textwrap.dedent(
        r"""
        import assert from "node:assert/strict";
        import fs from "node:fs";
        import vm from "node:vm";

        const source = fs.readFileSync(process.argv[1], "utf8");
        const from = source.indexOf("function venueFactSections");
        const to = source.indexOf("\nfunction navigate", from);
        assert.notEqual(from, -1);
        assert.notEqual(to, -1);
        const context = vm.createContext({
          currentLanguage: "zh-CN",
          escapeHtml: value => String(value ?? ""),
          fmtNumber: value => String(value ?? "—"),
          fmtStatus: value => String(value ?? ""),
          factStatusLabel: value => String(value ?? ""),
          fmtDate: value => String(value ?? "—"),
          fmtSide: value => String(value ?? ""),
          shortId: value => String(value ?? ""),
        });
        vm.runInContext(
          source.slice(from, to) + "; this.renderVenueSections = venueFactSections;",
          context,
        );
        const emptyFacts = {
          equity:null, reconciliation:null, positions:[], orders:[], fills:[], funding:[],
        };
        const snapshot = context.renderVenueSections(emptyFacts, {snapshotMode:true});
        assert.match(snapshot, /最后快照中的仓位与风险保护/);
        assert.match(snapshot, /不能确认当前账户仍为空仓/);
        assert.match(snapshot, /不能确认当前仍无挂单/);
        assert.match(snapshot, /这不代表连接中断后没有成交/);
        assert.match(snapshot, /这不代表连接中断后没有资金费/);
        assert.equal((snapshot.match(/callout tone-attention/g) || []).length, 4);
        assert.doesNotMatch(snapshot, /当前账户没有持仓/);
        assert.doesNotMatch(snapshot, /当前账户没有未完成委托/);

        const current = context.renderVenueSections(emptyFacts, {snapshotMode:false});
        assert.match(current, /当前账户没有持仓/);
        assert.match(current, /当前账户没有未完成委托/);
        assert.doesNotMatch(current, /callout tone-attention/);

        context.currentLanguage = "en";
        const snapshotWithHistory = context.renderVenueSections(
          {...emptyFacts, orders:[{status:"FILLED", venue_order_id:"1"}]},
          {snapshotMode:true},
        );
        assert.match(snapshotWithHistory, /from the last snapshot/);
        assert.match(snapshotWithHistory, /do not confirm current open orders/);
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
    app_path = frontend_bundle_path()
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
          "\nconst preferredThemeMedia",
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
