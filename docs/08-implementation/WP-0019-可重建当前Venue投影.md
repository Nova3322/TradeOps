# WP-0019：可重建当前 Venue 投影

> 历史说明：WP-0029 已在相同只读、事件时间和失败关闭原则下增加 current protection projection，
> 并要求保护事实仍精确绑定 current position source snapshot。当前保护投影合同以
> [WP-0029](WP-0029-可重建当前保护投影.md) 为准。

## 1. 交付目标

WP-0019 从 canonical `VenuePositionSnapshot` 与 `VenueAccountEquitySnapshot` 建立两张可丢弃、
可重建、不可接受业务命令的当前查询投影：

```text
venue_position_current_projection
venue_account_equity_current_projection
```

读取入口为 `VenueCurrentProjectionService`。它只查询权威事实的 SQL 视图，没有写模型、业务状态
转换、外部发送或风险裁决副作用。

## 2. 精确 Scope

仓位投影按下列完整 scope 独立选取当前事实：

```text
organization + venue + execution_domain + account + instrument
+ position_mode + position_side + margin_mode + collateral_pool + settlement_currency
```

账户权益投影按下列完整 scope 独立选取当前事实：

```text
organization + venue + execution_domain + account
+ margin_mode + collateral_pool + settlement_currency
```

不同账户、执行域、保证金模式、抵押池、仓位腿、标的或结算币不会因展示方便而合并。

## 3. 事件时间与冲突语义

当前事实按 `event_time` 选择，不按数据库到达顺序选择。迟到的旧事实不能覆盖更新的场所事实。

如果同一完整 scope 的最大 `event_time` 有多条不同 canonical 快照，本包不推断 opaque
`venue_update_id` 的大小关系，而是返回：

```text
projection_state = UNKNOWN
reason_code = MAX_EVENT_TIME_COLLISION
```

此时 source ID、仓位数量、权益、UPNL、保证金和其他经济字段全部遮蔽。只有后续可证明顺序的场所
事实或认证 adapter 序列合同才能解除冲突。

## 4. Query 新鲜度与成熟度

每次查询必须显式提供：

- timezone-aware `as_of`；
- `max_age_ms`，由调用方绑定的有效策略或证书提供，本包没有生产默认值。

响应始终携带：

- `facts_as_of`、venue observed time、received time；
- `age_ms` 与 `FRESH / STALE / MISSING / UNKNOWN`；
- `VENUE_CONFIRMED / UNKNOWN` 成熟度；
- canonical source snapshot ID/hash、source/normalization version；
- `venue-current-v1` projection version。

源事实 `UNKNOWN`、最大事件时间冲突、源事实晚于查询 `as_of`、超出显式 freshness 上限或 scope
缺失时，服务统一返回 `projection_state = UNKNOWN` 并遮蔽全部经济字段。Unknown 不会显示为零。

## 5. 可重建与只读边界

迁移 `20260718_0019` 只创建普通 PostgreSQL view，不创建第二套经济事实表。投影没有独立生命周期，
也不保存唯一风险、订单或资金状态。

回滚只执行：

```text
DROP VIEW venue_account_equity_current_projection
DROP VIEW venue_position_current_projection
```

canonical facts 不变；重新升级会从同一事实确定性恢复相同投影。两张含 window function 的 view
不能通过普通 `UPDATE` 写入，数据库直写尝试会被 PostgreSQL 拒绝。

## 6. 监控与错误语义

新增有界指标：

- `trading_venue_current_projection_queries_total`：按 projection type、state、freshness 统计；
- `trading_venue_current_projection_age_seconds`：按 projection type 观察事实年龄。

稳定失败原因包括：

```text
SOURCE_MISSING
SOURCE_UNKNOWN
SOURCE_FROM_FUTURE
SOURCE_STALE
MAX_EVENT_TIME_COLLISION
```

这些状态只影响查询可用性，不会触发外部动作或自动重放旧 OrderIntent。

## 7. 需求追踪

本工作包直接落实：

- 《领域模型与状态机》建模原则第 9 条、3.7：查询视图是可重建只读投影，不是第二写模型；
- 《API、事件、数据与审计契约》第二章、第六章、第十章、第十一章：Query 携带 as-of、
  新鲜度、成熟度、projection version，重放不能执行外部动作；
- 《财务对账与 PnL 口径》16、18、19：Unknown 不显示为零，保留事实时间和版本，投影可丢弃重建；
- 《风险引擎规格》4.4、13：最新 UPNL 必须可见，陈旧或不完整输入不得沿用大值；
- 总目标业务规则 13、15 与架构规则：最新 MTM 输入、Unknown 失败关闭、关系事实库与可重建查询
  投影。

## 8. 明确未完成范围

本工作包不声称完成组合级 Current Portfolio MTM Equity：

- 尚无权威、完整、版本化的受管账户 scope manifest；
- 不同 settlement currency 尚无认证 FX/价格事实和折扣；
- Vault、在途资本和外部资本流尚未接入；
- 查询投影尚未绑定 Risk Engine 的 `CapitalInput`；
- 没有真实 Binance/Hyperliquid collector 或场所公式认证。

因此不能把已知账户的局部和冒充完整组合权益，也不能据此签发真钱能力。`AUTO_ADD`、
`CAPITAL_TRANSFER`、`LIVE_ORDER_SEND` 继续保持 `DISABLED`。

按用户特别约束，Codex Security 及其所有审计 Skill、插件和模块保持停用；本工作包只执行常规
架构设计、代码检查、迁移和工程测试。
