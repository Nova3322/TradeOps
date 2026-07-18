# WP-0037：ADD 冗余调用方布尔值移除

> 后续状态：WP-0038 已把 ExecutionIntent 升级为 v11，删除 caller
> `current_effective_leverage`，改由 canonical current notional 与 submitted `E_campaign` 服务端保守
> 派生并哈希留证。当前执行合同以 [WP-0038](WP-0038-ADD当前有效杠杆服务端派生.md) 为准。
> 本文件保留 WP-0037 历史合同。

## 1. 交付目标与边界

WP-0036 后，ADD 的 `AddEligibilitySnapshot` 仍要求调用方提交：

```text
protection_valid
authorization_valid
```

这两个布尔值不能证明真实保护或授权。服务端已经在同一最终预检事务内锁定并校验 TradingAuthorization、
Campaign、InitialAuthorization、AddPackage/AddUnit、SystemRiskState、Capability Certificate、exact 原生保护
能力和 current canonical position/protection projection；继续要求调用方把两个字段写成 `true` 只增加了一个可
伪造的平行断言。

WP-0037 删除这两个字段，并令旧字段显式失败。服务端耐久校验保持不变且成为唯一判定来源。本包不处理
`trend_valid`、冻结收益率、当前 campaign equity 或有效杠杆的可信真源，它们保留为后续独立工作包。

## 2. 命令合同

Proposal RiskPrecheck 没有 ADD eligibility 字段，因此继续使用：

```text
risk.precheck.evaluate.v9      payload_schema_version=9
```

ExecutionIntent 升级为：

```text
execution.intent.create.v10    payload_schema_version=10
```

v1 至 v9 execution 命令返回 `COMMAND_TYPE_MISMATCH`，v10 携带非 10 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。`AddEligibilitySnapshot` 现在使用 `extra="forbid"`；v10 携带任一旧布尔值
返回 `EXECUTION_INPUT_INVALID`，不会忽略旧字段或恢复 caller-trusted 语义。

## 3. 唯一耐久授权判定

ADD 最终预检继续在事务内锁定并验证：

- TradingAuthorization、Campaign 与 InitialAuthorization 精确 lineage；
- AddAuthorizationPackage 为 `ACTIVE`，目标 AddUnit 为 `AVAILABLE`；
- 前序 AddUnit 已 `CONSUMED`，后序状态没有异常锁定；
- Campaign 为 `OPEN`，Initial 为 `CONSUMED`；
- Add package 有效期、冻结目标杠杆区间和里程碑；
- SystemRiskState 为 `NORMAL`；
- 无冲突/Unknown OrderIntent 和耐久风险暴露。

`authorization_valid` 不再存在，也不参与任何判断。授权事实变化由上述耐久行、状态历史和锁决定。

## 4. 唯一耐久保护判定

ADD 继续要求：

- exact Instrument Catalog 与原生保护能力记录有效；
- capability 绑定 account、credential、worker、adapter、margin/collateral 和保护模板；
- current canonical position/protection projection 为 `CONFIRMED` 且 fresh；
- position/protection snapshot ID/hash 与 ADD evidence 精确一致；
- quantity、direction、Mark、multiplier、settlement currency 与 Campaign/risk request 一致；
- current Open Heat/Giveback 从 canonical projection 重算并进入 Trade/scope 风险。

`protection_valid` 不再存在。旧字段为 true 不能替代上述任何检查，删除它也不会放宽保护条件。

## 5. 当前 ADD eligibility 合同

v10 暂时保留：

```text
frozen_return_pct
trend_valid
current_effective_leverage
target_effective_leverage
current_position_equity
position_snapshot_ref/hash
protection_snapshot_ref/hash
```

其中 snapshot ref/hash 已绑定 canonical projection；target leverage 绑定冻结 package。`frozen_return_pct`、
`trend_valid`、`current_effective_leverage` 和 `current_position_equity` 仍是未关闭的调用方信任边界，不能因本包
删除两个冗余字段而宣称 ADD eligibility 已完整可信。

## 6. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| 授权状态只有一个真源 | 删除 caller `authorization_valid`，事务锁定耐久授权图 |
| 保护状态不得由调用方布尔值证明 | 删除 caller `protection_valid`，继续 exact capability/current projection 校验 |
| 旧客户端不能静默降级 | v10 `extra=forbid`，旧字段返回 `EXECUTION_INPUT_INVALID` |
| ADD 只在 NORMAL 且顺序正确时执行 | 既有 package/unit/campaign/system 状态门回归通过 |
| 删除冗余输入不得放宽风险门 | canonical protection mismatch、风险/授权失败测试继续通过 |

## 7. 明确未完成范围

- frozen return、真实净浮盈、campaign equity、current leverage 尚未从 PnL/campaign ledger 重建；
- 趋势延续、布林回调、策略有效性尚未绑定已认证 Strategy Evaluation；
- “原保护已上移”的策略级判定仍需基于 canonical protection 与冻结模板派生；
- 真实 collector、真实 OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO、PnL 与运维
  仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

本包不新增数据库结构，schema head 仍为 `20260718_0030`。按用户明确约束，Codex Security 及其所有审计
Skill、插件和模块保持停用；只执行常规代码检查、严格类型检查和测试。
