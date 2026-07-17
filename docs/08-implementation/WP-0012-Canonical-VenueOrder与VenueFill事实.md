# WP-0012：Canonical VenueOrder 与 VenueFill 事实

> 状态：Implemented
> 上位合同：《OMS、Freqtrade 与 VenueAdapter 执行规范》第 7、8、13、14、16 节、
> 《领域模型与状态机》第 4、9、14、16、17 节、
> 《财务对账与 PnL 口径》的成交、费用与事实优先级合同、
> 《API 事件数据与审计契约》的幂等、顺序、证据与审计边界
> 前置包：WP-0001..0011
> 消费补充：WP-0013 已实现 exact canonical fact/link 到 OrderIntent 与风险暴露的 v3 强绑定

## 问题与边界

WP-0009 冻结七类 reconciliation input，WP-0010/0011 关闭弱 ExecutionFact 入口并建立 original/successor
lease 权威，但 `VENUE_ORDERS` 和 `VENUE_FILLS` 仍只是带 item count 的输入清单。若没有独立、不可变、可去重的
场所事实对象，执行状态仍可能直接相信聚合 payload、重复消费同一成交，或在适配器重启后把同一事实重复计入
仓位和费用。

本包只建立 private-venue order/fill 的 canonical evidence layer：

- 场所订单的每次生命周期更新成为一条不可变 `VenueOrderObservation`；
- 每个场所成交 ID 成为一条不可变 `VenueFill`，不因 reconciliation run 重启而复制；
- canonical fact 与每次 frozen input 的归属分开保存为 `VenueFactInputLink`；
- 只有 venue order/fill input 的 link 数与冻结 `item_count` 完全相等，run 才能进入 `COMPARING`；
- 本包不修改 OrderIntent、Heat、Position、PnL 或风险账。WP-0013 才能通过 exact fact reference 消费这些事实。

## 三个耐久对象

### VenueOrderObservation

租户内外部身份为 `organization + venue + execution_domain + account_id + venue_order_id + venue_update_id`。同一订单可以有多个有序或
乱序到达的 update，但同一个 update identity 只能对应一份 immutable semantics。

冻结字段包括 route、instrument、observed client order ID、原生 order/update ID、side、position side、
reduce-only、order type、time-in-force、原始数量、累计成交、已知剩余、terminal、zero-fill 证明、原始 payload
hash、规范化版本、event/observed/received 时间和证据 hash。

固定状态语义：

| 状态 | 数量与终态约束 |
| --- | --- |
| `OPEN` | 累计成交为 0，剩余等于原始数量，非终态 |
| `PARTIALLY_FILLED` | 成交与剩余都大于 0，二者之和等于原始数量，非终态 |
| `FILLED` | 累计成交等于原始数量，剩余为 0，终态 |
| `CANCEL_PENDING` | 成交加剩余仍等于原始数量，非终态 |
| `CANCELLED` / `EXPIRED` | 剩余为 0，终态；只有累计成交为 0 时才能声明 zero-fill |
| `REJECTED` | 成交和剩余都为 0，终态且必须有 zero-fill 证明 |
| `UNKNOWN` | 非终态、不得声明 zero-fill，不产生确定成交或未成交结论 |

### VenueFill

租户内外部身份为 `organization + venue + execution_domain + account_id + venue_trade_id`。同一成交可出现在后续 reconciliation input，
但数据库只保留一条 canonical fill，并为每个 input 新增独立 link。相同 trade ID 复用不同数量、价格、订单、费用、
route 或 venue event time 时稳定返回冲突，不能覆盖第一份事实；不同采集批次的 raw/normalization evidence 保存在
各自 link 中，不会制造第二条经济事实。

每条 fill 必须满足：

- `quantity > 0`、`price > 0`、`contract_multiplier > 0`；
- `notional = quantity × price × contract_multiplier`；
- `venue_confirmed=true`、`fact_authority=VENUE_PRIVATE`；
- fee `CHARGE > 0`、`REBATE < 0`、`ZERO = 0`，费用币种和结算币种不可缺失；
- event time 不晚于 venue observed time，后者不晚于 received/recorded time；
- SHADOW 环境且 `live_dispatch_eligible=false`。

部分成交由多条独立 VenueFill 表达；成交到达顺序不改变其 event-time 事实。终态零成交只能由符合上表的
VenueOrderObservation 证明，禁止制造 quantity=0 的 VenueFill。

### VenueFactInputLink

link 是 canonical fact 在一次 exact reconciliation input 中的 immutable membership。它保存 run/input、source、
input hash、fact hash、本次采集的 raw/evidence refs/hashes、observed/received/linked 时间与 link hash。

同一 input 对同一 fact 最多一条 link；同一 canonical fill 可以属于多个后续 input。这样既能验证冻结 payload 的
完整数量，又不会因 collector 或 reconciler 重启重复生成经济事实。

## 命令和权威边界

新增两个 internal-only 命令：

```text
execution.venue-order-observation.record.v1
execution.venue-fill.record.v1
```

只有 `execution-reconciliation-service` 能调用。每次写入同时要求：

1. object 绑定 exact `ExecutionReconciliationRun` 和相同 organization；
2. run 是该 sender scope 最新 run，状态 `RUNNING`、phase `COLLECTING`、deadline 未过；
3. run 为 SHADOW 且永远不可 live dispatch；
4. input 属于该 run、source 分别为 `VENUE_ORDERS` / `VENUE_FILLS`、状态 `COMPLETE`；
5. source version、input hash、event watermark 与 request 完全相等；
6. venue/domain/account 与 frozen sender scope 完全相等；
7. run lease/token 是当前唯一 sender authority，尚未 fence 或过期；
8. 新 link 不得超过 frozen input `item_count`。

外部身份先使用 transaction advisory lock 串行化；服务在任何 canonical insert 前先验证 link ID 和 input 容量，
避免“业务拒绝但事务仍提交”留下孤立事实。成功、冲突和拒绝都通过既有 command receipt、audit event 与 outbox
合同记录；命令重放不重复写事实。

## 数据库不可绕过门

迁移 `20260718_0012` 创建三张表和独立 PostgreSQL guard：

- canonical facts 与 links 都禁止 UPDATE/DELETE；
- canonical insert 独立复核 run/input/source/window/route/current lease/latest run/deadline；
- link insert 独立复核 fact hash、input hash、source、route、current lease 和 item-count 上限；
- deferred first-link constraint 要求新 canonical fact 必须在同一事务内拥有第一条 exact input link；
- deferred run-state constraint 在进入 `COMPARING`/`ADJUSTING` 或 `SUCCEEDED` 时复核 order/fill link 数等于各自
  frozen input item count；
- FK、unique、check constraints 同时约束外部身份、费用符号、数量守恒、时间顺序和 SHADOW authority。

因此，直接 SQL 插入孤立 fact、提前推进 run、超量 link、改写费用或复用 trade ID 都不能绕过应用服务。

## 监控、错误和追踪

低基数指标：

```text
trading_venue_fact_normalizations_total{fact_type,result}
trading_venue_fact_input_links_total{source_type,result}
```

主要稳定错误：

- `VENUE_FACT_SERVICE_REQUIRED`
- `VENUE_FACT_COLLECTION_CLOSED` / `VENUE_FACT_COLLECTION_EXPIRED`
- `VENUE_FACT_RUN_NOT_LATEST`
- `VENUE_FACT_INPUT_MISMATCH` / `VENUE_FACT_OUTSIDE_INPUT_WINDOW`
- `VENUE_FACT_ROUTE_MISMATCH` / `VENUE_FACT_SENDER_LEASE_STALE`
- `VENUE_ORDER_OBSERVATION_CONFLICT` / `VENUE_FILL_CONFLICT`
- `VENUE_FACT_INPUT_LINK_CONFLICT` / `VENUE_FACT_INPUT_LINK_ID_CONFLICT`
- `VENUE_FACT_INPUT_COUNT_EXCEEDED`
- `RECONCILIATION_NORMALIZED_FACT_COUNT_MISMATCH`

追踪更新：`REQ-EXEC-006/008/009`、`REQ-DATA-003`、`RISK-005/006/012/025`、`STATE-004/005/009`、
`TEST-003/004/008/009/011/016` 和 `EVID-004/006/009/011`。

## 失败、恢复和回滚

- input 少事实：保持 `COLLECTING`，不能进入比较；补齐 exact membership 后再推进。
- input 超量或 identity 冲突：整条命令拒绝，不能保留孤立 canonical fact。
- 乱序/迟到但仍在 frozen watermark 内：保留其原 event time；本包不自行重排业务状态。
- event 超出 watermark、run/lease 过期或被 fence：拒绝；不得放宽水位或借用旧 authority。
- 同一 fact 在后续 run 再次出现：复用 canonical fact，只新增该 input 的 membership。
- 数据库、审计或 command transaction 失败：整个 fact/link/audit/outbox 事务回滚。

回滚前必须停止 venue fact normalizer，确认没有活动的 WP-0012 writer，并保持现实 capability gates 关闭：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0011
```

0012 downgrade 删除 canonical fact/link 表和对应 guard，因此只适用于尚无需要保留的场所事实环境。upgrade 可重建
空表，但不会恢复已删除数据。生产数据环境未来必须使用归档/前向迁移方案，不能直接执行此开发期回滚。

## 明确未实现与现实边界

- Binance/Hyperliquid private stream、REST backfill、签名凭据和真实 collector；
- VenueAdapter/Freqtrade worker、真实 native order/status 映射和逐场所 contract-test；
- canonical fact 到 ShadowDispatchClaim/OrderIntent 的 exact ownership binding（WP-0013）；
- VenuePosition、Balance、Protection、Funding、财务 ledger、PnL 与 correction flow；
- 真实时钟偏差、API 分页缺口、断线、限频、双主和数据保留演练；
- Web/PWA、Telegram、Margin Controller、Vault/CTO、正式告警和 Runbook。

本包测试全部使用 disposable PostgreSQL 和虚构 SHADOW facts，没有网络调用、真实账户或订单发送。它不能激活
`LIVE_ORDER_SEND`，不能证明 Binance/Hyperliquid 已接入或认证，也不能把 canonical fact 自动视为内部仓位。
