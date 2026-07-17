# WP-0013：Canonical Venue Fact 执行强绑定

> 状态：Implemented
> 上位合同：《OMS、Freqtrade 与 VenueAdapter 执行规范》第 7、8、13、14、16 节、
> 《领域模型与状态机》第 4、9、14、16、17 节、
> 《风险引擎规格》的风险预留、事实优先级与守恒账本合同、
> 《财务对账与 PnL 口径》的成交唯一性、费用与更正边界
> 前置包：WP-0001..0012

## 问题与边界

WP-0010 的 `execution.fact.record-reconciled.v2` 已把 ExecutionFact 绑定到 claim、current reconciliation run 和
exact input，但数量、终态和 external fact ID 仍来自该命令自身。即使 run/input 真实存在，调用方理论上仍可在
`VENUE_ORDERS` 或 `VENUE_FILLS` input 名义下自报累计成交和剩余数量。WP-0012 建立 canonical
VenueOrderObservation/VenueFill 后，若状态机不强制引用它们，这条弱路径仍会绕开场所事实层。

本包把唯一新写入口升级为：

```text
execution.fact.record-reconciled.v3
```

所有新 ExecutionFact 都是 `fact_contract_version=3`。历史 v1/v2 行继续只读，但 v1、v2 命令和直接数据库插入
全部关闭。对于 `VENUE_ORDER` / `VENUE_FILL`，v3 必须引用 exact canonical fact、该 run/input 的 immutable
membership 和 fact hash；调用方字段只能与 canonical projection 完全相等，不能再充当事实来源。

## v3 耐久绑定

ExecutionFact 新增：

| 字段 | 约束 |
| --- | --- |
| `venue_order_observation_id` | 仅 `VENUE_ORDER` 非空，且全表最多消费一次 |
| `venue_fill_id` | 仅 `VENUE_FILL` 非空，且全表最多消费一次 |
| `venue_fact_input_link_id` | order/fill 必填，且全表最多消费一次 |
| `venue_fact_hash` | 必须等于 canonical observation/fill hash 和 link fact hash |
| `canonical_venue_order_id` | 必须等于场所原生 order ID；同一 OrderIntent 后续事实不得改变 |

order/fill 的 `external_fact_id` 固定为 canonical fact UUID，不接受调用方另造 identity。命令 payload 也固定为
只包含 fact type、fact ID、fact hash、input link ID 和 canonical venue order ID 的投影；`source_ref`、
`evidence_ref`、event time、received time 分别来自 exact link/canonical fact。

非 order/fill 的 WORKER_RECEIPT、VENUE_POSITION、VENUE_PROTECTION 暂时仍使用 v3 的 reconciliation binding，
但 canonical refs 必须全部为空；它们将在各自权威对象工作包中继续收紧。

## Claim ownership 与订单身份

canonical order/fill 要影响某个 OrderIntent，必须同时满足：

1. organization、venue、execution domain、account 与 claim/intent 完全相同；
2. instrument 与 frozen OrderIntent 完全相同；
3. canonical `observed_client_order_id` 必须等于 immutable ShadowDispatchClaim 的 client order ID，缺失也拒绝；
4. side、reduce-only 与 intent 完全相同；
5. ONE_WAY scope 要求 venue position side 为 `BOTH`，HEDGE scope 要求等于 intent LONG/SHORT；
6. order observation 的 order type、time-in-force、original quantity 还必须等于 intent；
7. link 必须属于 request 的 exact current run/input/source/input hash，并引用同一个 canonical fact/hash；
8. 首条 order/fill 建立 canonical venue order ID，后续 fill、cancel 或 unknown 不得切换到另一原生订单。

这组条件阻断“同账户另一订单”“同交易对另一 client ID”“另一 input 的事实”“另一租户同名账户”和“后续换
venue order ID”被错误归属到当前 claim。

## 成交增量与订单观察映射

### VenueFill

VenueFill 只提供本次真实成交增量。状态机从当前已应用累计量计算：

```text
new_cumulative = current_cumulative + canonical_fill.quantity
new_remaining  = frozen_intent_quantity - new_cumulative
```

- `new_remaining > 0`：唯一合法目标是 `PARTIALLY_FILLED`，非终态；
- `new_remaining = 0`：唯一合法目标是 `FILLED`，终态；
- `new_remaining < 0`：拒绝，不能超过 frozen intent quantity。

费用、价格、trade ID 和 event time 保留在 canonical fill；本包只迁移风险数量/Heat/Funding/Margin 守恒桶，
尚不记 PnL 或财务分录。同一 `venue_fill_id` 与 link 有唯一约束，重放只返回原 ExecutionFact，不能再次消费风险。

### VenueOrderObservation

订单观察不能新增成交数量：其 cumulative fill 必须等于 OrderIntent 当前已由 canonical fills 应用的累计量。固定映射：

| Canonical order status | Execution target |
| --- | --- |
| `OPEN` | `VENUE_ACKNOWLEDGED` |
| `CANCEL_PENDING` | `CANCEL_PENDING` |
| `CANCELLED` / `EXPIRED` 且累计为 0 | `CANCELLED_ZERO_FILL` |
| `CANCELLED` / `EXPIRED` 且累计大于 0 | `CANCELLED_PARTIAL` |
| `REJECTED` | `REJECTED_ZERO_FILL` |
| `UNKNOWN` | `RESULT_UNKNOWN` |

`PARTIALLY_FILLED` / `FILLED` order observation 不能代替 individual VenueFill；`FAILED_SAFE` 也不再接受 VENUE_ORDER
自报。部分成交后撤单必须先消费 fill，再以同一 canonical venue order ID 的 terminal order observation 释放未成交
风险。zero-fill 继续只由 terminal order observation 证明。

## 服务和数据库双重门

服务按以下顺序持锁并验证：OrderIntent、RiskReservation、OrderIntentState、RiskExposureState、account/campaign/
collateral advisory locks、claim/run/input/current lease、canonical fact/link/ownership、状态 progression，随后在同一事务
写 ExecutionFact、风险账本、风险暴露、OrderIntentState、history、audit 和 outbox。

迁移 `20260718_0013` 独立增加 PostgreSQL 约束：

- v3 check constraint 强制 fact kind 与 canonical refs 的 exact one-of 关系；
- FK 绑定 canonical fact/link，三个 unique constraint 阻止重复消费；
- insert guard 重做 claim/run/input/current successor authority 校验，并独立复核 canonical ownership、payload、时间、
  数量推导、状态映射和连续 venue order ID；
- deferred application guard 要求提交时 OrderIntentState 与 ExecutionFact 完全一致，并要求 RiskExposureState 的
  reserved/open/unknown/released 数量桶与该 fact 推导结果完全一致；
- 因此直接 SQL 即使插入一条语义正确的 v3 fact，只要没有在同一事务原子应用状态和风险，也会在 commit 回滚；
- v1/v2 数据保留只读，新 insert 必须为 v3。

## 监控、错误和追踪

新增低基数指标：

```text
trading_execution_canonical_fact_bindings_total{fact_type,result}
```

`fact_type` 仅为 `VENUE_ORDER` / `VENUE_FILL`，result 仅为 `APPLIED` / `REPLAYED` / `REJECTED`。既有
`trading_execution_fact_bindings_total`、authority mode、command result、risk ledger 和 reconciliation 指标继续保留。

主要新增稳定错误：

- `EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND`
- `EXECUTION_FACT_CANONICAL_LINK_MISMATCH`
- `EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH`
- `EXECUTION_FACT_CANONICAL_QUANTITY_MISMATCH`
- `EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH`
- `EXECUTION_FACT_CANONICAL_STATUS_UNSUPPORTED`
- `EXECUTION_FACT_CANONICAL_ORDER_ID_MISMATCH`

追踪更新：`REQ-POS-006`、`REQ-EXEC-006/008/009`、`REQ-DATA-003`、`REQ-OPS-002`、
`RISK-005/006/009/012/025`、`STATE-003/004/005/009`、`TEST-003/004/008/009/011/016` 与
`EVID-004/006/009/011`。

## 失败、恢复和回滚

- canonical fact 缺失、link 不属于 exact input 或 hash 不同：拒绝，不更新状态或风险。
- client order ID、instrument、side、position mode 或 native order ID 不同：按错误归属处理，保持原 intent 不变。
- 同一 fill 重放：返回原 fact；同一 fill 改序号、数量或目标状态：identity conflict，不能二次消费。
- fill 超过 frozen intent quantity：拒绝并保持 risk reservation 不变。
- order observation 报告了尚无 individual fill 的新增累计量：拒绝，不把订单快照当成交。
- current successor lease 仍可沿 WP-0011 lineage 消费迟到 canonical fact，但不能重发原订单或改变 venue order ID。
- 数据库、审计、账本或任一 deferred graph 失败：fact、state、ledger、history、audit/outbox 整体回滚。

开发期回滚：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0012
```

回滚删除 v3 canonical refs/constraints 并恢复 WP-0011 的 v2 insert guard。执行前必须停止 writer、确认不存在必须保留
的 v3 ExecutionFact，并保持全部现实 capability gates 关闭；含 v3 数据的环境不能直接降级，必须先做前向兼容迁移
或归档。回滚不是恢复 v2 新写权限的运营方案。

## 明确未实现与现实边界

- Binance/Hyperliquid private stream、REST backfill、真实 client/native order 映射和分页缺口恢复；
- Freqtrade/VenueAdapter worker 及发送瞬间 fencing；
- canonical VenuePosition、Balance、Protection、Funding 与 liquidation facts；
- Fill fee、funding、realized/unrealized PnL、财务 ledger 和 correction/reversal；
- 真实场所部分成交、重复 trade、断线、乱序、双主、限频和时钟偏差认证；
- Web/PWA、Telegram、Margin Controller、Vault/CTO、正式告警与 Runbook。

本包证据仍来自 disposable PostgreSQL 和本地 SHADOW facts，不连接真实交易所、不发送订单、不激活任何现实资金
能力。它证明本地状态机只消费 canonical venue order/fill，不证明 collector、场所语义或实盘执行已经认证。
