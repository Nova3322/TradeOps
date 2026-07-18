# WP-0024：提案预检 Durable Exposure 强绑定

## 1. 交付目标

WP-0024 消除提案阶段 `RiskPrecheckService` 对调用方申报 funding、当前交易 Heat、内部 margin
reservation 和七层 scope 当前占用的信任。提案预检与最终 `ORDER_PRECHECK` 现在都通过同一个
`DurableExposureSnapshotService`，从 `RiskReservation + RiskExposureState` 重建组织级当前占用。

本包不创建第二套风险账本。`RiskExposureState` 仍是 `RiskLedgerEntry` 的受约束当前投影，提案和
最终预检只消费同一真源。

## 2. 公共聚合与并发合同

公共 service 先获取：

```text
risk-scope:PORTFOLIO:<organization_id>
```

对应的 PostgreSQL transaction advisory lock，再按 reservation UUID 固定顺序锁定组织内全部
`RiskExposureState`。这与最终增险预留使用的 portfolio lock 相同，可以阻断查询期间插入新的
reservation；已有 reservation 的迁移同时受行锁约束。

聚合器派生：

```text
Global Funding Used     = Σ state.funding_used
Global Funding Reserved = Σ state.funding_reserved
Global Funding Unknown  = Σ state.funding_unknown

Internal Margin Hold = Σ (state.margin_reserved + state.margin_unknown)
Risk Available Margin = max(0, canonical available margin - Internal Margin Hold)

active_ratio = (open_heat + reserved_heat + unknown_heat) / total_heat
scope current usage = Σ frozen allocation × active_ratio
```

funding/margin bucket 即使出现 active Heat 为零的异常组合也不会被静默忽略；它们仍计入全局占用。
scope allocation 只有 active Heat 大于零时才参与比例聚合。

## 3. 新初仓提案的 Heat 语义

`PROPOSAL_PRECHECK` 面向尚未创建 Campaign 的 SYSTEM/MANUAL 初仓候选，因此 snapshot 的
`campaign_id` 固定为 `null`，当前交易损失必须精确为：

```text
Open Heat                 = 0
Reserved Heat             = 0
Unknown Heat              = 0
Protected-Profit Giveback = 0
Cost Stress Add-on        = 0
```

组织内其他 Campaign 的风险不会混入该候选的 trade Heat，但会通过 global funding、内部 margin 和
七层 scope 使用量限制新提案。任何非零“当前交易 Heat”申报都返回
`DURABLE_EXPOSURE_INPUT_MISMATCH`。

## 4. 失败关闭规则

- 调用方 funding used/reserved 与 Ledger 聚合不一致：
  `DURABLE_EXPOSURE_INPUT_MISMATCH`；
- 任一 scope current planned/stress 使用量高报或低报：
  `DURABLE_EXPOSURE_INPUT_MISMATCH`；
- 任一 component 处于 `UNKNOWN`，或任一 Heat/funding/margin Unknown bucket 非零：
  `ORDER_RESULT_UNKNOWN`；
- 请求 margin 超过 canonical available margin 扣除内部 Reserved/Unknown 后的余额：
  `DURABLE_MARGIN_CAPACITY_EXCEEDED`。

这些拒绝发生在证书校验和风险数学之前，不会生成可被误认作可信快照的
`RiskDecisionSnapshot`。

## 5. 不可变证据

每个成功完成的确定性 ALLOW 或 DENY 提案决策都保存：

```text
input_snapshot.durable_exposure_snapshot
input_snapshot.durable_exposure_snapshot_hash
risk_decision_snapshots.durable_exposure_snapshot_hash
```

snapshot 包含全部 reservation/state component 的 ID、Campaign、状态、版本、Ledger sequence、
active ratio、Heat/funding/margin bucket 和最后 evidence ref/hash，以及组织聚合、扣减后 margin 和
七层 scope 结果。命令结果与 `RiskPrecheckDecisionRecorded` 事件同步携带同一 hash。

## 6. 迁移与回滚

迁移 `20260718_0024` 为 `risk_decision_snapshots` 增加非空 64 位
`durable_exposure_snapshot_hash`，并扩展数据库完整性约束。升级前若存在 legacy 提案决策则明确
失败，不伪造历史 Ledger 证据；仍有新提案决策时拒绝 downgrade。

空决策表的回滚路径为：

```text
0024 -> 0023 -> 0024
```

## 7. 明确未完成范围

- 动态 protected-profit giveback、费用/滑点/资金费压力尚未拆成独立可变 Ledger 组件；
- 真实私有 venue collector、场所权益公式、FX/stablecoin/depeg 与真钱能力证据尚未完成；
- Web/PWA、Telegram、Freqtrade/VenueAdapter、Margin、Vault/CTO、PnL 和完整运维目标仍待实现；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包仅使用常规架构设计、
数据库约束、静态检查和自动化测试。
