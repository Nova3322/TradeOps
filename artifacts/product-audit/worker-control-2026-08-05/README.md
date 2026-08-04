# 交易账户只读快照验收（2026-08-05）

## 范围

- 页面：`/positions`、`/venues?venue=HYPERLIQUID`
- 目标：确认 Freqtrade 执行底座、Hyperliquid / HIP-3 只读状态与账户事实不会互相误导，并验证桌面与手机排版。
- 安全边界：只读验收；未下单、未签名、未广播、未划转资金。

## P0 / P1 / P2

- P0：未发现页面或 API 绕过 `LIVE_ORDER_SEND`、资金或自动加仓 Gate。
- P1（已修复）：Hyperliquid 只读连接限流时，页面虽声明“仅为最后快照”，空仓与空委托区仍写成“当前账户没有…”，可能把历史零仓误当实时零仓。现在仓位、委托、订单记录、成交、资金费和对账差异统一使用“最后快照”语义，并明确不能确认当前仍为空仓或无挂单。
- P1（外部受限）：Hyperliquid 当前只读探针仍被上游限流；页面如实显示 `HYPERLIQUID_RATE_LIMITED` 的用户可读分类、负责人和自动重试下一步，不采信旧事实为实时。
- P2（已修复）：窄屏交易账户状态原为两列，长连接状态和执行说明拥挤。`≤480px` 时状态卡改为单列；交易所切换仍保留两列，表格继续使用独立横向滚动区。

## 实际步骤与状态

1. 系统状态：健康。两个 Freqtrade dry-run worker 已接管；币安 680 个合约，链上永续 265 个合约，其中 HIP-3 88 个。证据：`01-system-status.png`。
2. 限流复现：存在且被准确分类。修复前历史零仓仍使用“当前”文案。证据：`02-hyperliquid-limited.png`。
3. 手机端：健康。状态卡单列、菜单和交易所切换可见；DOM 语义确认仓位、委托、成交、资金费均为“最后快照”。证据：`03-hyperliquid-mobile-top.png`。
4. 桌面端：健康。限流、自动重试、最后快照警告与历史仓位语义处于同一事实链。证据：`04-hyperliquid-desktop-fixed.png`。

## 验证

- 静态检查：Ruff、`git diff --check` 通过。
- 定向 Web/API：15 项通过。
- 完整回归：525 项通过。
- 运行：`/health/live` 与 `/health/ready` 通过；PostgreSQL 健康；Binance 与 Hyperliquid Freqtrade worker 均运行。
- Gate：`AUTO_ADD`、`AUTO_OPERATING_REFILL`、`AUTO_PROFIT_SWEEP`、`CAPITAL_TRANSFER`、`LIVE_ORDER_SEND` 全部为 `DISABLED`。

## 证据限制

- 本轮只证明本机当前只读状态投影、页面语义和安全关闭；Hyperliquid 上游限流尚未解除，不能据此宣称实时账户连接可用。
- 手机端下方长表格通过 DOM、计算布局和独立滚动区域验证；截图保留首屏，避免把长页面合成截图的滚动拼接误差当成实际排版。
