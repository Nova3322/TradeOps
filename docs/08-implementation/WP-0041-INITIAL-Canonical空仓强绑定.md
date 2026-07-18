# WP-0041：INITIAL Canonical 空仓强绑定

> 后续状态：WP-0042 已在 INITIAL `POSITION_RECONCILED` 时冻结 isolated 初始保证金经济基线；
> 当前执行与经济事实合同以 [WP-0043](WP-0043-Canonical成交费用归属账本.md) 为准。本文件保留
> WP-0041 的初仓前空仓合同。

## 1. 交付目标与边界

WP-0040 后，INITIAL 虽然要求调用方提交 `current_position_quantity=0`，但最终预检没有读取交易所
canonical 当前仓位。调用方因此可能在场所已有同范围仓位时仍创建一个新 Campaign 初仓意图，破坏仓位
归属、后续 PnL 和保证金基线。

WP-0041 要求每个 INITIAL 在最终预检事务内解析 exact scope 的当前仓位投影。只有 fresh、
venue-confirmed、`FLAT` 且数量为零的 canonical 快照才可形成风险决定、预留与 OrderIntent。快照 ID/hash
永久进入最终决定。本包只关闭初仓前空仓事实缺口；不实现真实 collector、订单发送、Campaign PnL 或
保证金归因。

## 2. 命令与数据库合同

Proposal RiskPrecheck 继续使用：

```text
risk.precheck.evaluate.v9      payload_schema_version=9
```

ExecutionIntent 升级为：

```text
execution.intent.create.v14    payload_schema_version=14
```

v1 至 v13 execution 命令返回 `COMMAND_TYPE_MISMATCH`；v14 携带非 14 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。INITIAL 的 caller `current_position_quantity` 仍保留在通用数量合同中，
但必须严格为零；非零输入返回 `EXECUTION_INPUT_INVALID`，不能替代 canonical 仓位事实。

迁移 `20260718_0032` 为 `execution_risk_decisions` 增加：

```text
initial_flat_position_snapshot_id
initial_flat_position_snapshot_hash
```

ID 以外键绑定不可变 `venue_position_snapshots`，ID/hash 必须同时为空或同时存在，且 ADD 不得写入该
绑定。旧 v1-v13 历史决定允许保持空值；Execution v14 对每个新 INITIAL 决定总是写入 exact 快照。
只要数据库仍有该绑定证据，迁移拒绝降级，防止静默删除最终预检来源。

## 3. Exact scope 与成功条件

Execution 使用 Campaign、冻结授权绑定和风险结算币种构造 `CurrentPositionScope`：

- organization、venue、execution domain、account；
- canonical instrument；
- position mode；ONE_WAY 固定读取 `BOTH`，hedge mode 读取 Campaign direction；
- margin mode、collateral pool、settlement currency。

POSITION freshness 上限来自当前版本化 Risk Policy，不存在代码默认值。成功必须同时满足：

1. current projection 为 `CONFIRMED / FRESH / VENUE_CONFIRMED`；
2. source snapshot ID/hash 完整；
3. `position_state=FLAT`、`direction=FLAT`、`quantity=0`；
4. caller 通用数量字段同样为零；
5. `facts_as_of + POSITIONS max_age_ms` 晚于最终预检时间。

成功时完整 projection 和 `valid_until` 写入 decision input snapshot；snapshot ID/hash 同时进入
`ExecutionRiskDecision`、decision payload、命令返回与 outbox event。最终 OrderIntent 有效期再与风险
结果、授权和策略评估有效期取最早值，旧空仓事实不能被更长授权窗口延长。

## 4. 失败语义

```text
missing / UNKNOWN / conflict / future / stale
    -> INITIAL_CURRENT_POSITION_UNAVAILABLE

confirmed OPEN or nonzero canonical quantity
    -> INITIAL_REQUIRES_CANONICAL_FLAT_POSITION

nonzero caller current_position_quantity
    -> EXECUTION_INPUT_INVALID
```

这些失败发生在任何最终风险决定、OrderIntent 或 RiskReservation 创建之前，因此事务无新增风险副作用。
保护、对账、reduce-only 与退出路径不使用该 INITIAL 门，也不会被它阻断。

## 5. 并发与现实边界

本包绑定的是最终预检读取时刻的 immutable venue fact，并用短有效期限制复用。它不声称冻结交易所外部
状态，也不开放发送：真实 sender/OMS 仍必须在认证后的发送合同中再次验证最新场所仓位、订单和
reconciliation epoch。当前 `execution_mode=SHADOW`、`dispatch_eligible=false`。

测试夹具写入的 FLAT 快照只证明控制合同；真实 collector、私有流重连、REST 校准、断线恢复和逐场所
position mode 认证仍未完成。

## 6. 需求追踪与未完成范围

| 权威要求 | 本包证据 |
| --- | --- |
| 仓位生命周期从真实空仓进入新 Campaign | INITIAL 必须解析 exact fresh canonical FLAT |
| 仓位事实未知时不得增险 | missing/UNKNOWN/conflict/future/stale 全部失败关闭 |
| 最终预检必须可重放 | decision 持久化 immutable snapshot ID/hash 与完整 projection |
| 旧批准不得盲目执行 | flat fact TTL 收紧最终 OrderIntent 有效期 |

仍未完成：

- Campaign PnL Ledger、资金费/费用/净平仓成本归因；WP-0042 已冻结 isolated 初始保证金参考额；
- 真实 venue collector、OMS/Freqtrade/VenueAdapter 与发送前二次核验；
- frozen return、`E_campaign`、真实 Strategy Evaluation evaluator；
- Web/PWA、Telegram、Margin、Vault/CTO、财务报表与运维认证；
- production/small-live CapabilityCertificate 和发送权限。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
