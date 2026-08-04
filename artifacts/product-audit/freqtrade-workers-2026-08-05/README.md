# Freqtrade dry-run worker 接入验收

日期：2026-08-05（Asia/Kuala_Lumpur）

## 变更范围

- 本地启动脚本同时启动 PostgreSQL、Binance Freqtrade worker 和 Hyperliquid Freqtrade worker。
- 控制台默认监听 `127.0.0.1:8014`，仍允许通过显式环境变量覆盖。
- 控制面启用 worker 只读探针；本地 worker 凭据不写入日志或页面。
- 修正系统状态的组合判断：worker 已就绪但交易所只读连接受限时，不再误报 worker 检查失败。
- 更新 Web 资源版本，避免已安装 Service Worker 的页面继续显示旧结论。

## API 与运行证据

- `/health/live`：HTTP 200。
- `/health/ready`：HTTP 200。
- `/api/execution/freqtrade/status`：
  - Binance：`READY`、`dry_run=true`、`order_send=false`、680 个合约。
  - Hyperliquid：`READY`、`dry_run=true`、`order_send=false`、265 个合约，其中 HIP-3 88 个。
  - 控制面：`direct_venue_send=false`、`live_order_send=false`。
- Docker：两个 Freqtrade worker 为 `running`；PostgreSQL 为 `healthy`。

合约数量来自本轮 worker 实时白名单，只用于证明 worker 的当前读取范围，不表示这些合约已经满足提案、风控、授权或真实交易条件。

## 页面验收

- 桌面系统状态页显示“Freqtrade 执行底座已就绪，但交易所只读连接受限”。
- 执行卡显示 Binance、Hyperliquid 与 HIP-3 的当前合约数量，并明确本地 dry-run、真实下单关闭。
- Binance 与 Hyperliquid 交易账户页均显示“执行由 Freqtrade worker 负责；本页不能下单”。
- Hyperliquid 页面显示核心市场与 HIP-3 `xyz` 范围。
- 交易所只读探针失败时，页面降级为最后快照并显示具体失败分类；不会把 worker 就绪冒充生产账户实时连接。
- 桌面与手机宽度均无页面级横向溢出。

## 测试

- `bash -n scripts/run_local.sh`：通过。
- `node --check src/trading_control_plane/web/app.js`：通过。
- Ruff：通过。
- Unit：320 项均覆盖并通过；其中 Perptape 压力组 45 项耗时约 120 秒，单独完成。
- API：14 passed。
- PostgreSQL integration：188 passed，使用隔离的 `trading_test` 数据库。
- 总计：522 项。

## 安全边界

最终运行复核：

- `AUTO_ADD=DISABLED`
- `AUTO_OPERATING_REFILL=DISABLED`
- `AUTO_PROFIT_SWEEP=DISABLED`
- `CAPITAL_TRANSFER=DISABLED`
- `LIVE_ORDER_SEND=DISABLED`

未发送订单、未转账、未签名、未广播。

## 外部限制

Binance / Hyperliquid 的生产只读账户探针仍可能受网络、上游限流或接口可达性影响。本轮验收观察到连接在失败分类下诚实降级；该状态与 Freqtrade dry-run worker 是否健康是两个独立事实。
