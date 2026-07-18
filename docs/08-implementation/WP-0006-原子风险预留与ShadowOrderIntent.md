# WP-0006：原子风险预留与 Shadow OrderIntent

> 后续状态：WP-0038 已将创建命令升级为 `execution.intent.create.v11` / payload schema 11；WP-0031..0035
> 先后移除 caller giveback、scope stress、classification、protection boolean 和九类 facts，WP-0036 再绑定
> KNOWN MARKET payload，WP-0037 删除 ADD caller protection/authorization 布尔值，WP-0038 再删除
> caller current leverage 并服务端派生。当前输入合同以
> [WP-0038](WP-0038-ADD当前有效杠杆服务端派生.md) 为准。
> 本文件保留 WP-0006 历史证据。

> 状态：Implemented
> 上位合同：《风险引擎规格》第 5、8、11 节、《领域模型与状态机》第 7、10、11 节、
> 《策略合同与数值化验收门》第 8 节、《OMS、Freqtrade 与 VenueAdapter 执行规范》第 4、6、7 节
> 前置包：WP-0001..0005

## 交付边界

本包把已经人工批准的 `TradingAuthorization` 向执行前最后一道强事务边界推进：

```text
冻结 Proposal + 独立人工 Approval + TradingAuthorization
  + 当前九类事实/MTM/UPNL + 七类 Scope + SystemRiskState
  + Initial 或 Add 的一次性授权状态
  -> final ORDER_PRECHECK
  -> immutable ExecutionRiskDecision(ALLOW)
  -> immutable, SHADOW, dispatch_eligible=false OrderIntent
  -> immutable RiskReservation + RESERVE ledger entry
  -> conserved RiskExposureState + append-only histories
  -> Initial/Add claim + audit + transactional outbox
```

上述写入在 `IdempotentCommandExecutor` 的一个 PostgreSQL 事务内完成。数据库使用延迟约束触发器，
在提交点再次要求每个 ALLOW 决策恰好拥有一个 OrderIntent 和一个 RiskReservation，且决策数量、Heat、
Funding、Margin、授权、Campaign、有效期必须完全一致。任何一步失败都会连同 command receipt、audit、
outbox、Add claim 和所有已 flush 行整体回滚。

本包仍不实现发送：数据库固定 `execution_mode=SHADOW`、`dispatch_eligible=false`，没有 Freqtrade、
VenueAdapter 或交易所发送接口，也没有 sender leader/fencing token。`LIVE_ORDER_SEND`、`AUTO_ADD` 和
`CAPITAL_TRANSFER` 能力门仍为 `DISABLED`。

## 下单前风险复核与并发占用

`execution.intent.create.v1` 只接受固定的 `oms-risk-reservation-service` INTERNAL principal，并逐项
验证：

- authorization、proposal、approval 快照及 canonical hash 未变化且未过期；
- strategy/parameter、venue、execution domain、account、account abstraction、position/margin mode、
  collateral pool、settlement asset、adapter/worker、Catalog 与证书引用继续等于冻结绑定；
- proposal version、风险档位、总资本快照、初始失效价、订单类型、最大滑点和方向继续等于冻结事实；
- 九类风险事实齐全、KNOWN、新鲜且时间一致；Current Portfolio MTM Equity 包含最新 UPNL；
- 七类风险 scope 精确绑定 underlying、risk cluster、sector、execution domain、venue、collateral pool
  和 portfolio；
- 调用方上报的 Campaign Heat、组织 Funding 和每个 scope 敞口不低于持久化账本投影；现有 reservation
  也会占用尚未在场所扣除的可用 Margin；
- 组织内任意 `Unknown` 数量都会阻断所有新增风险；系统风险状态缺失或收紧会失败关闭；
- 冻结损失容量、动态 MTM 容量、冻结 Funding Envelope、政策 Funding Envelope、杠杆、Margin、
  数量步长和 planned/stress scope 上限全部通过。

事务按排序后的 Campaign、account、Instrument、collateral 和七类 scope key 获取 PostgreSQL advisory
transaction lock。因此不同 Campaign 只要共享 portfolio/account/collateral/scope 也会串行竞争；后到
事务在获得锁后重新读取持久化敞口，不能用旧快照重复占用。

相同 `(campaign_id, candidate_ref)` 与相同 canonical candidate hash 返回既有权威 OrderIntent；相同
引用配不同语义稳定拒绝。command idempotency key 的相同重放仍由耐久 receipt 处理。

## Initial 与 Add claim

Initial 只允许：Campaign=`PENDING_ENTRY`、Initial=`ACTIVE`、数量不超过冻结上限、可执行价仍在人工
授权价格边界内，且不存在未决、Unknown 或已有正成交的 Initial Intent。终态已证明零成交并释放全部
风险后，可用新 candidate 创建新尝试；旧 Intent 永不重放。

Add 只允许：

- 初仓已由正成交消费，Campaign=`OPEN`，Add package=`ACTIVE`，目标 unit=`AVAILABLE`；
- 前序 AddUnit 全部 `CONSUMED`，后续 unit 尚未被占用；
- SystemRiskState=`NORMAL`，冻结 30%/50%/100% 收益率里程碑仍满足；
- 趋势、保护、授权证据均为真，`L_effective < L_min`，目标杠杆仍在冻结区间；
- Add 数量按执行时仓位权益反算：

```text
Q_add = floor_to_step(
  (CurrentPositionEquity × L_target - CurrentPositionQty × ExecutablePrice × Multiplier)
  / (ExecutablePrice × Multiplier)
)
```

风险请求、目标仓位差额与该计算值必须完全相等。不存在固定手数、初仓百分比或固定风险份额路径。
满足后，AddUnit 的 `AVAILABLE -> CLAIMED` 与决策、Reservation、Intent、账本同事务发生。

当前仓位和保护引用属于 SHADOW 证据输入；正式 position/protection 事实库及其认证尚未实现，因此这些
引用不能作为生产 Add 的能力证据。

## 对账事实、部分成交与 Unknown

`execution.fact.record.v1` 只接受固定 `execution-reconciliation-service` INTERNAL principal。事实必须
绑定同一 venue/execution domain/account，外部事实 id 唯一，payload/evidence canonical hash 正确，
时间不在未来，且 sequence 恰好为上一事实加一。worker ACK 不等于成交；状态变化必须由持久化事实
驱动。

每份 reservation 的 Quantity、Heat、Funding 和 Margin 始终守恒于四个互斥桶：

```text
Reserved + Open + Unknown + Released = Total
```

- 部分成交把确认成交比例从 Reserved 迁到 Open，已知挂单余量继续 Reserved；
- 全部成交把全部风险迁到 Open；终态部分取消把未成交余量迁到 Released；
- 已证明终态零成交把全部风险迁到 Released；Initial 保持 ACTIVE，AddUnit 在授权仍有效且系统 NORMAL
  时 `CLAIMED -> AVAILABLE`；
- 结果 Unknown 把未确认余量从 Reserved 迁到 Unknown，不释放、不自动重试；之后只能由更高序号的
  对账事实解析；
- 任意大于零的 Initial 成交立即 `ACTIVE -> CONSUMED` 并令 Campaign `PENDING_ENTRY -> OPEN`；
- 任意大于零的 Add 成交立即 `CLAIMED -> CONSUMED`，即使剩余结果 Unknown 也不退回次数；
- 初仓正成交、position reconciled 且 protection confirmed 后，才将 DORMANT Add package 激活；若
  系统状态、有效期或保护门失败，则不可逆 INVALIDATED。

RiskLedger 是追加事实；当前 Exposure 和 OrderIntentState 只能逐版本更新。数据库触发器要求状态必须
精确对应同序号 ExecutionFact，RiskExposure 必须对应无缺口账本序号和最后证据 hash。根事实、账本、
事实和两类历史均拒绝 UPDATE/DELETE。

## 监控、错误与追踪

新增低基数指标：

- `trading_execution_risk_decisions_total{intent_kind,result,primary_reason}`；
- `trading_risk_reservation_transitions_total{transition}`；
- `trading_execution_fact_results_total{target_status,result}`。

稳定错误码区分内部 principal、对象/组织/route、冻结或证书绑定、候选语义、Initial/Add 状态、Add
里程碑/杠杆/权益目标差额、持久敞口低报、组织级 Unknown、Margin、乱序/倒退/溢出、零成交/部分成交
证明和外部事实 id 冲突。每个成功或业务拒绝命令仍产生 command receipt；领域结果通过 immutable
audit 和 transactional outbox 追踪 correlation/causation。

## 迁移与回滚

迁移 `20260718_0006` 新增：

- `execution_risk_decisions`；
- `order_intents / order_intent_states / order_intent_state_history`；
- `risk_reservations / risk_exposure_states / risk_exposure_state_history`；
- `risk_ledger_entries / execution_facts`；
- Initial/Add/Campaign 的复合身份约束、不可变守卫、状态守卫、自动历史和延迟原子图校验。

应用回滚优先保留 schema 和事实。仅在确认一次性环境或已导出全部新增事实后，停止 0006 writer 并执行：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0005
```

这会删除全部 0006 决策、Intent、Reservation、Ledger、Fact 和历史，是破坏性 schema 回滚，不是订单
恢复手段。重启或回滚应用均不得重放旧 Intent；未来 sender 只能先对账。

## 明确未实现与实盘边界

- OrderIntent dispatch、一次性消费 token、sender leader election/fencing 和双主阻断；
- VenueOrder、Fill、真实 position/protection 事实库与 VenueAdapter 私有流接入；
- Freqtrade 受控插件、Binance/Hyperliquid 适配、原生保护与重启对账；
- CapabilityCertificate 的耐久存储、逐 scope 验证、吊销和证据环境；
- ALLOW_WITH_CAP 的安全缩量合同；当前 final precheck 只能整量 ALLOW 或 DENY；
- Web/PWA、Telegram、Margin Controller、Vault/CTO、市场数据、PnL、备份恢复和运行 Runbook；
- 真实回放、实时影子、测试网、故障演练或受限小额实盘证据。

因此本包只证明本地 SHADOW 数据库事务和状态机边界，不构成真实订单发送、自动加仓激活或真实资金
准入，也不能标记系统或任何 venue/account/instrument scope 为 real-money ready。
