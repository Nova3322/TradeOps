# WP-0043：Canonical 成交费用归属账本

> 后续状态：WP-0044 已从 immutable opening fill entries 重建累计数量/名义价值、原生费用和
> realized PnL 完整性，并固定声明 Campaign equity 不可用；当前经济投影合同以
> [WP-0044](WP-0044-可重建Opening-Fill经济投影.md) 为准。本文件保留 WP-0043 的逐笔归属合同。

## 1. 交付目标与边界

WP-0042 已冻结 isolated INITIAL 的初始保证金分母，但 Campaign 仍没有一条不可变链路把每笔已接受
VenueFill 的真实价格、数量、手续费/返佣和场所 realized PnL 归属到具体 INITIAL 或 ADD 意图。直接从
当前仓位均价或场所 UI 反推会丢失逐笔成本、Add 归因和原生币种。

WP-0043 在每个 canonical `VENUE_FILL` ExecutionFact 被接受时，原子创建一个唯一的
`CampaignFillEconomicEntry`。它保存原始成交和费用事实，不做 FX、不汇总不同币种，也不计算成本基础、
资金费、Frozen Return 或 `E_campaign`。

当前 OrderIntent 只有 INITIAL/ADD，因此 entry 的经济效果固定为 `POSITION_INCREASE`。未来 reduce/exit
必须使用独立合同扩展，不能把本包的正向增仓语义复用于减仓或方向反转。

## 2. Exact 归属合同

每条 entry 永久绑定：

- Campaign、OrderIntent、ExecutionFact、VenueFill 和可选 AddUnit；
- organization、venue、execution domain、account、instrument 和 Campaign direction；
- side、position side、margin mode、collateral scope/pool 与风险报告币种；
- venue order/trade ID、quantity、price、contract multiplier、notional 和 liquidity role；
- 原生 `fee_amount / fee_currency / fee_effect`；
- 场所 `realized_pnl`、其 KNOWN/UNKNOWN 状态和 settlement currency；
- fill hash、execution fact evidence hash、entry version/hash 和事实时间。

`fee_amount` 沿用 canonical VenueFill 的有符号合同：CHARGE 为正、REBATE 为负、ZERO 为零。费用币种可与
settlement/risk currency 不同；例如 BNB 手续费不会因 risk currency 为 USD 被自动视为 USD。

`realized_pnl_status` 显式区分：

```text
KNOWN + realized_pnl=0         场所明确报告零
UNKNOWN + realized_pnl=NULL   尚无可信值
```

未知不能按零进入任何 PnL 或 Add 判定。

## 3. 原子写入与重放

Execution 在写入 VENUE_FILL ExecutionFact 前，先验证 fill 与 Intent 的 organization、venue/domain、
account、instrument、side、position side、reduce-only 与 source hash。通过后在同一数据库事务内写入：

- ExecutionFact；
- CampaignFillEconomicEntry；
- OrderIntentState、RiskExposureState 和 RiskLedgerEntry 迁移；
- command receipt、审计和 outbox event。

entry ID/hash 进入命令返回和 `ExecutionFactReconciled` event。同一 VenueFill/ExecutionFact 只能各绑定
一条 entry；重放返回原 entry，不生成第二份费用或成交归属。

## 4. 数据库保护

迁移 `20260718_0034` 新增 `campaign_fill_economic_entries`。insert guard 重新读取 Intent、ExecutionFact、
VenueFill 与 RiskReservation，逐字段验证 Campaign、AddUnit、organization、scope、成交经济字段、费用、
realized PnL、source hashes 和事件时间。update/delete trigger 拒绝修改，存在记录时迁移拒绝降级。

记录固定 `environment=SHADOW`、`real_funds_eligible=false`。迁移不 seed fill/entry，不创建现实 sender，
也不打开任何能力门。

## 5. 失败与保守语义

```text
非 INITIAL/ADD、reduce-only 或错误事实所有权
    -> CAMPAIGN_FILL_ECONOMIC_ENTRY_OWNERSHIP_MISMATCH

scope / side / position side / fill hash 不一致
    -> CAMPAIGN_FILL_ECONOMIC_ENTRY_FACT_MISMATCH

同一 VenueFill 已归属到不同 Campaign/Intent/Fact
    -> CAMPAIGN_FILL_ECONOMIC_ENTRY_CONFLICT
```

预验证在 ExecutionFact 持久化前完成，业务拒绝不会留下部分成交账本。数据库约束或完整性错误导致整个
事务回滚。保护、对账、reduce-only 与退出路径不依赖 Web/Telegram，也未因本包获得外部发送权限。

## 6. 与完整 Campaign PnL 的关系

本包可回答“哪一笔受信任成交及其原生费用属于哪个 Campaign/INITIAL/ADD”，但仍不能回答完整收益：

- 尚无版本化成本基础方法和 reduce/exit fill 合同；
- venue realized PnL 可为 UNKNOWN，且尚未与内部成本基础对账；
- 尚无资金费事实/归属 entry；
- 尚无费用/收益的 FX 与稳定币折扣；
- 尚无当前 Mark 下的 Campaign 未实现净收益投影；
- 尚未把逐笔 entry、WP-0042 分母和 current position 合成为 Frozen Return / `E_campaign`。

因此任何费用跨币种求和、UNKNOWN 置零、用当前仓位均价替代 fill 序列或直接开放 Add 都属于旁路。

## 7. 需求追踪与未完成范围

| 权威要求 | 本包证据 |
| --- | --- |
| 成交是仓位与成本变化的唯一事实起点 | 只在 accepted canonical VENUE_FILL 时创建 entry |
| Campaign 必须追溯 INITIAL 与每个 Add | intent kind 与 AddUnit exact 绑定 |
| 费用/返佣保存原生币种且不双计 | 每 VenueFill 唯一 immutable entry，保留 signed fee |
| 未知事实不得伪装为零 | realized PnL 使用独立 KNOWN/UNKNOWN 状态 |

仍未完成：

- reduce/exit Intent、成本基础、已实现价格 PnL 与关闭 Campaign 状态机；
- funding facts/entries、FX/稳定币估值与跨币种汇总；
- 当前未实现净收益、Frozen Return 分子和 `E_campaign`；
- 真实 venue collector、OMS/Freqtrade/VenueAdapter 与逐场所费用/realized PnL 认证；
- Web/PWA、Telegram、Margin、Vault/CTO、报表和运维认证；
- production/small-live CapabilityCertificate 与发送权限。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
