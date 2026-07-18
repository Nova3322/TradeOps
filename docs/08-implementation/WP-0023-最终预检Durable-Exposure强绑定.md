# WP-0023：最终预检 Durable Exposure 强绑定

## 1. 交付目标

WP-0023 消除最终 `ORDER_PRECHECK` 对调用方申报 funding、Heat、内部 margin reservation 和七层
scope 当前占用的剩余信任。`DurableExposureResolver` 在创建 SHADOW OrderIntent 前，从同一事务内
锁定的 `RiskReservation + RiskExposureState` 重建完整当前占用，并把结果交给 Risk Engine。

本包不创建第二套风险账本；`RiskExposureState` 是 `RiskLedgerEntry` 的受约束当前态，Ledger 仍是
风险预留、迁移和释放的耐久事实链。

## 2. 精确派生合同

resolver 对组织内全部风险状态按 reservation UUID 固定顺序加行锁，并派生：

```text
Campaign Open Heat     = Σ same-campaign state.open_heat
Campaign Reserved Heat = Σ same-campaign state.reserved_heat
Campaign Unknown Heat  = Σ same-campaign state.unknown_heat

Global Funding Used     = Σ active state.funding_used
Global Funding Reserved = Σ active (funding_reserved + funding_unknown)

Internal Margin Hold    = Σ active (margin_reserved + margin_unknown)
Risk Available Margin   = max(0, canonical available margin - Internal Margin Hold)
```

`margin_used` 不在此重复扣除：已成交持仓保证金应已反映在 canonical venue `available_margin`；只有
Trading 已预留但场所尚未反映的 Reserved/Unknown margin 需要额外扣除。

每个 reservation 的七层 allocation 按当前 active Heat 比例收敛：

```text
active_ratio = (open_heat + reserved_heat + unknown_heat) / total_heat
current scope usage = Σ frozen allocation × active_ratio
```

因此部分成交、零成交释放、正成交、Unknown 和最终释放均引用同一守恒状态，不依赖调用方重演
状态机。

## 3. 不接受高报或低报

最终请求的以下字段必须与派生结果完全相同：

- `capital.funding_used / funding_reserved`；
- `current_trade_loss` 的 Open/Reserved/Unknown Heat；
- 七层 `scope_risks.current_planned_loss / current_stress_loss`。

任一高报或低报返回 `DURABLE_EXPOSURE_INPUT_MISMATCH`，不会运行风险数学或创建执行决策。高报虽然
通常只会收紧，但也会制造不可复算决策、隐藏上游陈旧状态，因此不作为兼容协议接受。

当前 `RiskExposureState.total_heat` 已在预留时包含 requested reserved Heat、profit giveback 与 cost
stress add-on；派生时不再重复叠加。尚未进入 Ledger 的新增动态 giveback/cost 没有可信来源，不能
由请求临时加入。

## 4. Unknown、margin 与并发

- 组织内任一 active `unknown_heat > 0` 返回 `ORDER_RESULT_UNKNOWN`，冻结所有新增风险；
- 请求 margin 超过扣除内部 Reserved/Unknown 后的余额时返回
  `DURABLE_MARGIN_CAPACITY_EXCEEDED`；
- 所有增险都已持有相同 `PORTFOLIO:<organization>` advisory transaction lock；resolver 再按确定顺序
  锁行，因此不同标的并发也不能在同一旧快照上分别通过并超额预留；
- 只有 resolver、canonical capital、证书和 Risk Engine 全部通过后，执行决策、OrderIntent、
  reservation、Ledger 和状态历史才原子提交。

## 5. 不可变证据

每个可信 ALLOW 或确定性 DENY 的 `input_snapshot` 新增：

```text
durable_exposure_snapshot
durable_exposure_snapshot_hash
```

snapshot 包含所有 reservation/state component 的 ID、campaign、状态、version、ledger sequence、
active ratio、Heat/funding/margin buckets 和最后 evidence ref/hash，以及聚合后的 campaign/global/scope
值。`execution_risk_decisions.durable_exposure_snapshot_hash` 为非空 64 位哈希，原有不可变 trigger
禁止事后修改。

命令结果和领域事件同时携带 snapshot hash；现有 execution decision、reservation transition 与
command rejection 指标/审计可以关联本次决策。

## 6. 迁移与回滚

迁移 `20260718_0023` 增加非空 `durable_exposure_snapshot_hash` 并扩展 DB check。升级前存在 legacy
execution decision 时明确失败，不伪造历史 Ledger 快照；仍有新决策时拒绝 downgrade。

空表回滚路径已实际验证：

```text
0023 -> 0022 -> 0023
```

## 7. 明确未完成范围

- 提案阶段 `RiskPrecheckService` 的 funding/Heat/scope 当前占用仍来自请求，本包只关闭最终发送前
  预检；
- 动态 protected-profit giveback、费用/滑点/资金费压力的独立可变 Ledger 组件尚未实现；
- 真实私有 venue collector、场所公式、FX/stablecoin/depeg、真钱证书和发送链仍未认证；
- Web/PWA、Telegram、Freqtrade/VenueAdapter、Margin、Vault/CTO、PnL 与运维目标仍待实现；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包仅使用常规架构设计、
数据库约束、静态检查和自动化测试。
