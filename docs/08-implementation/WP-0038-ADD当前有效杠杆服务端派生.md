# WP-0038：ADD 当前有效杠杆服务端派生

> 后续状态：WP-0039 已把 ExecutionIntent 升级为 v12，将 canonical venue UPNL 纳入
> `protected-position-risk-v2` 哈希，并在 UPNL 非正时阻断 ADD。当前执行合同以
> [WP-0039](WP-0039-ADD-Canonical正UPNL硬门.md) 为准。本文件保留 WP-0038 历史合同。

## 1. 交付目标与边界

WP-0037 后，ADD 调用方仍同时提交 `current_position_equity` 和
`current_effective_leverage`。两者在已知当前仓位名义价值时不是两个独立事实；允许调用方分别
提交会产生可伪造的不一致言论。

WP-0038 删除 caller `current_effective_leverage`，使用已强绑定的 canonical position 数量、Mark
和合约乘数，与尚未可信化的 submitted `E_campaign` 在服务端派生当前有效杠杆。本包只
关闭“重复杠杆字段”，不将 submitted `E_campaign` 伪装成 PnL Ledger 事实。

## 2. 命令合同

Proposal RiskPrecheck 不包含 ADD eligibility，继续使用：

```text
risk.precheck.evaluate.v9      payload_schema_version=9
```

ExecutionIntent 升级为：

```text
execution.intent.create.v11    payload_schema_version=11
```

v1 至 v10 execution 命令返回 `COMMAND_TYPE_MISMATCH`，v11 携带非 11 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。`AddEligibilitySnapshot` 仍为 `extra="forbid"`；v11 携带旧
`current_effective_leverage` 返回 `EXECUTION_INPUT_INVALID`。

## 3. 派生公式与保守精度

服务端在当前 position/protection projection 为 `CONFIRMED` 且与 Campaign、risk request、snapshot
ID/hash 精确一致后计算：

```text
current_position_notional
  = canonical_quantity × canonical_mark_price × canonical_contract_multiplier

current_effective_leverage
  = ceil_18(current_position_notional ÷ submitted_campaign_equity)
```

`ceil_18` 表示向风险更严格方向上取整到 18 位小数，不会因向下舍入让贴近
`L_min` 的仓位错误通过。派生结果必须严格小于冻结 `target_leverage_min`；相等也返回
`ADD_LEVERAGE_NOT_BELOW_MINIMUM`。

## 4. 计算证据

`AddLeverageCalculation` 固化：

- canonical position snapshot ID/hash；
- canonical quantity、Mark 和 contract multiplier；
- submitted campaign equity 及明示来源
  `CALLER_PENDING_CAMPAIGN_PNL_LEDGER`；
- 派生 current notional 和 current effective leverage；
- `add-effective-leverage-v1` 与自校验 calculation hash。

成功 ADD 的 ExecutionRiskDecision input snapshot 保存完整计算，decision payload、命令结果与领域
event 保存同一 calculation hash。后续风险 DENY 如果已完成该计算，也保存同样证据。

## 5. 安全顺序

ADD 有效杠杆只能在以下条件后派生：

1. 耐久 authorization/package/unit/campaign/system 状态已锁定；
2. 冻结收益里程碑、趋势 caller gate 和目标杠杆区间先通过；
3. exact policy 已加载，canonical current position/protection 已确认且 fresh；
4. canonical quantity/Mark/multiplier 与当前 request 精确一致；
5. 服务端派生 `L_effective` 并严格检查 `< L_min`。

任一一步失败都不会创建 OrderIntent 或 RiskReservation。caller 无法通过提交一个较小的杠杆
数字绕过门槛。

## 6. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| `L_effective = abs(Q_current) × P_mark ÷ E_campaign` | quantity/Mark/multiplier 来自 canonical projection，服务端派生 |
| `L_effective < L_min` 才能继续评估 Add | 派生值相等或更高均拒绝 |
| 旧客户端不能静默降级 | v11 `extra=forbid`，旧杠杆字段显式拒绝 |
| 决策必须可重放 | 输入、公式版本、派生值和 calculation hash 持久化 |

## 7. 明确未完成范围

- `current_position_equity` 仍是 submitted `E_campaign`，尚未由 Campaign PnL Ledger 重建；
- frozen return、真实净浮盈、初仓冻结保证金参考额尚未可信化；
- 趋势延续、布林回调和策略有效性仍未绑定已认证 Strategy Evaluation；
- target leverage 仍在冻结 min/max 区间内选择，正式参数仍受 `DEC-RISK-002` 阻塞；
- 真实 collector、OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO、PnL 与运维
  仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

本包不新增数据库结构，schema head 仍为 `20260718_0030`。按用户明确约束，Codex Security 及其
所有审计 Skill、插件和模块保持停用；只执行常规代码检查、严格类型检查和测试。
