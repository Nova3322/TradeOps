# WP-0018：Canonical VenueAccountEquitySnapshot 事实

## 1. 交付目标

WP-0018 把 `VENUE_BALANCES` 从只有原始对账输入的占位来源，升级为可逐项核验的 canonical
`VenueAccountEquitySnapshot` 私有事实。

唯一新增命令为：

```text
execution.venue-account-equity-snapshot.record.v1
```

本工作包只规范交易场所已经报告的账户与抵押池经济字段。它不计算 Exchange Risk Equity、
Current Portfolio MTM Equity 或最终 PnL，也不连接真实场所。

## 2. 事实合同

每个快照冻结以下作用域：

- organization、venue、execution domain、account；
- margin mode、collateral pool；
- settlement currency；
- venue update identity 与事件时间。

`CONFIRMED` 快照必须同时保存：

- wallet balance；
- exchange margin equity；
- available margin；
- total unrealized PnL；
- total initial margin 与 total maintenance margin；
- total liability；
- unsettled fee 与 unsettled funding；
- `includes_unrealized_pnl = true`。

这些值是场所私有接口报告值的规范化投影，不在本包中重新推导交易所公式。费用和资金费保留
有符号值；初始保证金、维持保证金和负债不得为负。

## 3. Unknown 不是零

`equity_state` 只有：

```text
CONFIRMED | UNKNOWN
```

`UNKNOWN` 时全部经济字段必须为 `NULL`，且 `includes_unrealized_pnl = false`。任何把未知余额、
权益或 UPNL 写成零的请求都会被 Pydantic 与 PostgreSQL 双重拒绝。后续 Projection 不得把
`UNKNOWN` 当成空仓或零权益继续扩大风险。

## 4. 全局身份与精确输入归属

canonical 外部身份由下列字段组成：

```text
organization + venue + execution_domain + account_id
+ margin_mode + collateral_pool_id + settlement_currency + venue_update_id
```

相同身份、相同经济语义可以幂等 replay，也可以在后续 reconciliation run 中创建新的 input
membership link；相同身份出现不同 snapshot hash 时失败关闭。

每条 `VENUE_BALANCES` 完整输入的 `item_count` 必须与 immutable
`VenueFactInputLink` 数量精确相等。缺失、超额、错 input hash、错时间窗、错 route、陈旧 lease
或非最新 run 均不能进入 `COMPARING`。

## 5. 数据库绕过防护

迁移 `20260718_0018` 增加：

- `venue_account_equity_snapshots` 不可变事实表；
- `venue_fact_input_links.venue_account_equity_snapshot_id` 外键、唯一约束和 exact-one check；
- canonical fact insert guard、first-link deferred guard 和 immutable trigger；
- `VENUE_BALANCES` manifest 精确计数门；
- account/margin/collateral route、事件窗、source version、hash、raw evidence 和 lease 校验；
- 存在 canonical account-equity facts 时拒绝降级，避免静默丢失证据。

因此应用服务拒绝不是唯一防线，直接数据库写入同样不能绕过事实作用域、语义或首条输入归属。

## 6. 明确边界

本工作包仍处于 `SHADOW`：

- `AUTO_ADD`、`CAPITAL_TRANSFER`、`LIVE_ORDER_SEND` 均保持 `DISABLED`；
- 没有真实 Binance/Hyperliquid 凭据、collector 或 venue adapter；
- 没有跨结算币 FX、haircut、抵押品折算或跨账户聚合；
- 没有生成 Exchange Risk Equity、Current Portfolio MTM Equity 或最终 PnL；
- 交易所字段映射和公式仍须按场所、账户抽象和保证金模式完成实证认证；
- 本包不能作为真钱能力证书或实盘就绪证明；
- 按用户要求，本工作包未运行 Codex Security 审计，只执行常规工程测试与数据库验证。
