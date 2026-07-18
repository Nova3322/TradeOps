# WP-0042：INITIAL 冻结保证金经济基线

> 后续状态：WP-0043 已把 accepted canonical VenueFill 及原生手续费/返佣逐笔归属到 Campaign；
> 当前经济事实合同以 [WP-0043](WP-0043-Canonical成交费用归属账本.md) 为准。本文件保留 WP-0042
> 的 isolated 初始保证金分母合同。

## 1. 交付目标与边界

WP-0041 已证明新 Campaign 的 INITIAL 从 exact canonical 空仓开始，但初仓成交后仍没有冻结 Frozen
Return 所需的不可变分母。若后续直接读取场所 UI 的当前保证金，加仓、转账、价格变化或场所计算口径
变化都可能重写历史分母。

WP-0042 在 INITIAL 的 canonical `POSITION_RECONCILED` 事实被接受时，从同一仓位快照冻结唯一的
`CampaignEconomicBaseline`。本包只接受可精确归属的 isolated 仓位及严格正值 `initial_margin`；cross
margin 的共享抵押归属尚未认证，必须失败关闭。

本包建立后续 Frozen Return 和 Campaign PnL Ledger 的不可变分母，不等于完整实现了净浮盈、费用、
资金费、已实现 PnL、预计退出成本或 `E_campaign`。

## 2. 数据库与证据合同

迁移 `20260718_0033` 新增 `campaign_economic_baselines`。每个 Campaign、INITIAL OrderIntent、
`POSITION_RECONCILED` ExecutionFact 和 VenuePositionSnapshot 都只能绑定一次。记录包含：

- exact organization、venue、execution domain、account、instrument 和方向；
- position mode、position side、margin mode、collateral scope/pool 和 settlement currency；
- 初仓 quantity、entry/mark price、contract multiplier、mark notional；
- `frozen_initial_margin_reference` 与固定来源 `VENUE_POSITION_INITIAL_MARGIN`；
- position snapshot hash、execution fact evidence hash、baseline version/hash 和事实时间。

数据库 insert guard 再次读取 Intent、ExecutionFact、PositionSnapshot 和 RiskReservation，逐字段验证所有权、
scope、数量、经济字段、source hash 和事件时间。update/delete trigger 使记录不可变；只要仍有记录，迁移
拒绝降级。环境固定 `SHADOW`，`real_funds_eligible=false`，迁移不 seed 事实或开启现实能力。

## 3. 原子冻结时点

Execution reconciliation 在写入 INITIAL `POSITION_RECONCILED` ExecutionFact 之前先验证 margin source：

1. Intent 必须为 INITIAL；
2. PositionSnapshot 必须是同一 exact scope、`OPEN`、同方向、同数量和同 source hash；
3. margin mode 必须为 `ISOLATED`；
4. `initial_margin` 必须存在且严格大于零。

预验证通过后，ExecutionFact、经济基线、OrderIntentState、RiskExposureState、RiskLedgerEntry、审计与 outbox
在同一事务内写入。基线 ID/hash 进入命令返回和 `ExecutionFactReconciled` event；同一事实重放返回同一
基线，不允许生成第二个分母。

预验证放在 ExecutionFact 持久化之前，确保缺失保证金或不支持的归属不会留下部分执行事实。保护、
reduce-only、退出和对账入口不依赖 UI，也不会因本包开放外部发送。

## 4. 失败语义

```text
非 INITIAL 或事实所有权错误
    -> CAMPAIGN_ECONOMIC_BASELINE_OWNERSHIP_MISMATCH

scope / quantity / source hash 不一致
    -> CAMPAIGN_ECONOMIC_BASELINE_FACT_MISMATCH

cross margin 或其他未认证归属
    -> CAMPAIGN_MARGIN_BASELINE_UNSUPPORTED

initial_margin 缺失或不为正
    -> CAMPAIGN_INITIAL_MARGIN_REFERENCE_UNAVAILABLE

Campaign 已存在不同不可变基线
    -> CAMPAIGN_ECONOMIC_BASELINE_CONFLICT
```

所有业务拒绝都保持失败关闭。INITIAL 仓位状态不会越过 `FILLED`，不会新增第二条 ExecutionFact 或任何
经济基线；真实发送能力继续关闭。

## 5. 与 Frozen Return / E_campaign 的关系

当前可持久证明的只有：

```text
Frozen Return denominator = frozen_initial_margin_reference
```

当前不能可信计算：

```text
Frozen Return numerator
  = 整仓未实现 PnL
  - 已归属费用
  - 已归属资金费
  - 预计退出成本

E_campaign
  = 初始可归属经济权益
  + 已实现 PnL
  + 未实现 PnL
  - 费用
  - 资金费
  ± 已认证的资金/保证金归属变动
```

这些分子和当前经济权益必须由后续 fill/fee/funding/PnL ledger 及逐场所认证重建，不能由本包的一个
initial-margin 数值推断。cross margin 仍为 `RESEARCH_REQUIRED`，不能复制 isolated 公式。

## 6. 需求追踪与未完成范围

| 权威要求 | 本包证据 |
| --- | --- |
| Frozen Return 分母在初仓后冻结且不随加仓/转账/UI 改变 | 每 Campaign 唯一不可变 baseline |
| 经济归属必须来自真实执行事实 | exact INITIAL PositionSnapshot + ExecutionFact 双重绑定 |
| 未认证 cross 归属不得被推断 | 非 isolated 明确失败关闭 |
| 历史经济基线必须可重放 | baseline hash、source hashes、version 与时间永久落库 |

仍未完成：

- 成交、费用、资金费、已实现/未实现 PnL 与预计退出成本账本；
- Frozen Return 分子、30%/50%/100% 服务端判定与 `E_campaign`；
- cross margin 共享抵押归属和压力口径认证；
- 真实 venue collector、OMS/Freqtrade/VenueAdapter 与发送前二次核验；
- Web/PWA、Telegram、Margin、Vault/CTO、财务报表和运维认证；
- production/small-live CapabilityCertificate 与发送权限。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
